"""Connection lifecycle and the global engine/dialect holders.

The Rust engine is opaque to the rest of the package; everything goes through
the accessors here so the model layer never imports the native module directly.
"""

from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING, Any

from . import _engine, registry
from .dialects import BaseDialect
from .dialects import get_dialect as resolve_dialect
from .exceptions import ConfigurationError, TransactionManagementError, UnSupportedError

if TYPE_CHECKING:
    from .models import Model

_ENGINE = None
_DIALECT: BaseDialect | None = None

#: Named connections: name -> (engine, dialect). "default" is the primary.
_CONNECTIONS: dict = {}
#: Optional router selecting a connection per model (see docs/Router example).
_ROUTER = None

#: When inside ``in_transaction()``, the active pinned connection. Query
#: execution routes through this instead of the pool so all statements share
#: one transaction.
_active_tx: contextvars.ContextVar = contextvars.ContextVar("orm_active_tx", default=None)


class IsolationLevel:
    """The four standard SQL transaction isolation levels.

    Pass one to ``in_transaction(isolation=...)`` / ``@atomic(isolation=...)``.
    SQLite only supports ``SERIALIZABLE``; the others raise ``UnSupportedError``.
    """

    READ_UNCOMMITTED = "READ UNCOMMITTED"
    READ_COMMITTED = "READ COMMITTED"
    REPEATABLE_READ = "REPEATABLE READ"
    SERIALIZABLE = "SERIALIZABLE"


_ISOLATION_LEVELS = frozenset(
    {
        IsolationLevel.READ_UNCOMMITTED,
        IsolationLevel.READ_COMMITTED,
        IsolationLevel.REPEATABLE_READ,
        IsolationLevel.SERIALIZABLE,
    }
)


def _normalize_isolation(isolation: str, dialect_name: str) -> str:
    """Validate an isolation level for a dialect and return its canonical form.

    Args:
        isolation: The requested isolation level (case-insensitive).
        dialect_name: The active dialect's name (e.g. ``"postgres"``).

    Raises:
        ConfigurationError: If the level is not a recognised SQL isolation level.
        UnSupportedError: If the dialect cannot honour the level (SQLite only
            supports ``SERIALIZABLE``).

    Returns:
        The canonical upper-case isolation level.
    """
    level = isolation.upper()
    if level not in _ISOLATION_LEVELS:
        raise ConfigurationError(f"Unknown isolation level: {isolation!r}")
    if dialect_name == "sqlite" and level != IsolationLevel.SERIALIZABLE:
        raise UnSupportedError(
            f"SQLite only supports the SERIALIZABLE isolation level, not {isolation!r}"
        )
    return level


def get_engine() -> Any:
    """Return the default engine, raising if the ORM is not initialised.

    Returns:
        The native default engine object.
    """
    if _ENGINE is None:
        raise ConfigurationError(
            "ORM is not initialised. Call `await YaraOrm.init(db_url=...)` first."
        )
    return _ENGINE


def _route(model: type[Model] | None, write: bool) -> str:
    """Connection name for ``model`` per the router, defaulting to 'default'.

    Args:
        model: Model class used to consult the router, or None.
        write: Whether the route is for a write operation.

    Returns:
        The resolved connection name.
    """
    if _ROUTER is not None and model is not None:
        name = _ROUTER.db_for_write(model) if write else _ROUTER.db_for_read(model)
        if name:
            return name
    return "default"


def get_executor(model: type[Model] | None = None, write: bool = False) -> Any:
    """Return the object statements run on for ``model``.

    This is the active transaction, or the connection chosen for ``model`` by
    the router (falling back to the default pool). All such objects expose
    ``execute`` / ``fetch_row`` / ``fetch_rows`` / ``fetch_all``.

    Args:
        model: Model class used to route to a connection, or None.
        write: Whether the executor is for a write operation.

    Returns:
        The active transaction or the routed connection object.
    """
    tx = _active_tx.get()
    if tx is not None:
        return tx
    # Fast path: no router -> the default engine, skipping route resolution.
    if _ROUTER is None:
        return get_engine()
    return _CONNECTIONS[_route(model, write)][0]


def get_dialect(model: type[Model] | None = None) -> BaseDialect:
    """Return the SQL dialect for ``model``'s connection.

    Args:
        model: Model class used to route to a connection, or None.

    Returns:
        The dialect for the resolved connection.
    """
    if _ROUTER is None:
        if _DIALECT is None:
            raise ConfigurationError(
                "ORM is not initialised. Call `await YaraOrm.init(db_url=...)` first."
            )
        return _DIALECT
    return _CONNECTIONS[_route(model, False)][1]


class YaraOrm:
    """Entry point: initialise connections, generate schemas and resolve relations."""

    @classmethod
    async def init(cls, db_url: str, router: Any = None) -> None:
        """Connect to ``db_url`` (the default connection) and resolve relations.

        Pass ``router`` to direct per-model reads/writes; register additional
        connections with :meth:`add_connection`.

        Args:
            db_url: Database URL for the default connection.
            router: Optional router selecting a connection per model.

        Returns:
            None
        """
        global _ENGINE, _DIALECT, _ROUTER
        _ENGINE = await _engine.connect(db_url)
        _DIALECT = resolve_dialect(_ENGINE.dialect)
        _CONNECTIONS["default"] = (_ENGINE, _DIALECT)
        _ROUTER = router
        registry.resolve_relations()

    @classmethod
    async def add_connection(cls, name: str, db_url: str) -> None:
        """Register an additional named connection.

        Args:
            name: Name to register the connection under.
            db_url: Database URL to connect to.

        Returns:
            None
        """
        engine = await _engine.connect(db_url)
        _CONNECTIONS[name] = (engine, resolve_dialect(engine.dialect))

    @classmethod
    def set_router(cls, router: Any) -> None:
        """Set the active per-model connection router.

        Args:
            router: Router object selecting connections per model.

        Returns:
            None
        """
        global _ROUTER
        _ROUTER = router

    @classmethod
    async def generate_schemas(
        cls, safe: bool = True, models: list[type[Model]] | None = None
    ) -> None:
        """Create model tables (on each write connection) and their join tables.

        Args:
            safe: Whether to use ``IF NOT EXISTS``-style safe creation.
            models: Models to create, in dependency order; defaults to every
                registered model. Pass a subset to build only those tables (the
                caller is responsible for ordering them so foreign-key targets
                come first).

        Returns:
            None
        """
        registry.resolve_relations()
        targets = list(models) if models is not None else registry.all_models()
        for model in targets:
            engine = get_executor(model, write=True)
            dialect = get_dialect(model)
            for statement in dialect.create_table_sql(model._meta, safe=safe):
                await engine.execute(statement)
        # Join tables for many-to-many relations (created once per through name).
        seen = set()
        for model in targets:
            for info in model._meta.m2m.values():
                info.finalize()
                if info.through in seen:  # pragma: no cover - defensive de-dup
                    continue
                seen.add(info.through)
                engine = get_executor(model, write=True)
                dialect = get_dialect(model)
                for statement in dialect.create_m2m_table_sql(info, safe=safe):
                    await engine.execute(statement)

    @classmethod
    async def close(cls) -> None:
        """Close all connections and reset the global engine state.

        Returns:
            None
        """
        global _ENGINE, _DIALECT, _ROUTER
        for engine, _ in _CONNECTIONS.values():
            await engine.close()
        _CONNECTIONS.clear()
        _ENGINE = None
        _DIALECT = None
        _ROUTER = None


# Backward-compatible alias for YaraOrm.
Tortoise = YaraOrm


class TransactionWrapper:
    """Adapts a native transaction handle to the executor interface."""

    def __init__(self, tx: Any) -> None:
        """Wrap a native transaction handle.

        Args:
            tx: The native transaction object to adapt.

        Returns:
            None
        """
        self._tx = tx
        #: Monotonic counter producing unique savepoint names for nested blocks.
        self._savepoint_seq = 0

    def new_savepoint(self) -> str:
        """Return a fresh, unique savepoint name for this transaction.

        Returns:
            A savepoint identifier unique within the transaction.
        """
        self._savepoint_seq += 1
        return f"yara_sp_{self._savepoint_seq}"

    async def savepoint(self, name: str) -> None:
        """Establish a savepoint on the transaction.

        Args:
            name: The savepoint name.

        Returns:
            None
        """
        await self._tx.savepoint(name)

    async def release(self, name: str) -> None:
        """Release (merge) a savepoint, keeping its work.

        Args:
            name: The savepoint name.

        Returns:
            None
        """
        await self._tx.release(name)

    async def rollback_to(self, name: str) -> None:
        """Roll back to a savepoint, discarding work since it was set.

        Args:
            name: The savepoint name.

        Returns:
            None
        """
        await self._tx.rollback_to(name)

    async def execute(self, sql: str, params: list[Any] | None = None) -> Any:
        """Execute a statement on the transaction.

        Args:
            sql: SQL statement to execute.
            params: Bind parameters, or None for no parameters.

        Returns:
            The native driver's execute result.
        """
        return await self._tx.execute(sql, params or [])

    async def fetch_rows(self, sql: str, params: list[Any] | None = None) -> Any:
        """Fetch multiple rows for a query on the transaction.

        Args:
            sql: SQL query to execute.
            params: Bind parameters, or None for no parameters.

        Returns:
            The fetched rows.
        """
        return await self._tx.fetch_rows(sql, params or [])

    async def fetch_row(self, sql: str, params: list[Any] | None = None) -> Any:
        """Fetch a single row for a query on the transaction.

        Args:
            sql: SQL query to execute.
            params: Bind parameters, or None for no parameters.

        Returns:
            The fetched row, if any.
        """
        return await self._tx.fetch_row(sql, params or [])

    async def fetch_all(self, sql: str, params: list[Any] | None = None) -> Any:
        """Fetch all results for a query on the transaction.

        Args:
            sql: SQL query to execute.
            params: Bind parameters, or None for no parameters.

        Returns:
            The fetched results.
        """
        return await self._tx.fetch_all(sql, params or [])

    async def commit(self) -> None:
        """Commit the underlying transaction.

        Returns:
            None
        """
        await self._tx.commit()

    async def rollback(self) -> None:
        """Roll back the underlying transaction.

        Returns:
            None
        """
        await self._tx.rollback()


class _Connections:
    """Minimal ``connections``-style accessor for manual SQL.

    ``connections.get(name)`` returns the active executor (transaction or pool),
    exposing ``execute`` / ``fetch_all`` / ``fetch_rows``.
    """

    def get(self, name: str = "default") -> Any:
        """Return the active executor for ``name``.

        Args:
            name: Connection name to look up.

        Returns:
            The active transaction, the named connection, or the default
            executor.
        """
        tx = _active_tx.get()
        if tx is not None:
            return tx
        if name in _CONNECTIONS:
            return _CONNECTIONS[name][0]
        return get_executor()


connections = _Connections()


class in_transaction:
    """Async context manager running its body in a single DB transaction.

    Commits on clean exit, rolls back if the block raises. While active, all
    model/queryset statements route through the pinned connection.

    Nesting is supported: a block entered while another transaction is active
    opens a **savepoint** instead of a new transaction, so the inner block can
    roll back (on error) without aborting the outer one, and its work is merged
    into the outer transaction on success. An ``isolation`` level may be set on
    the outermost block only.
    """

    def __init__(self, connection_name: str = "default", isolation: str | None = None) -> None:
        """Initialise the transaction context manager.

        Args:
            connection_name: Name of the connection to open a transaction on.
            isolation: SQL isolation level for the outermost transaction (see
                :class:`IsolationLevel`), or None for the database default.

        Returns:
            None
        """
        self.connection_name = connection_name
        self.isolation = isolation
        self._conn: TransactionWrapper | None = None
        self._token: contextvars.Token | None = None
        self._savepoint: str | None = None

    async def __aenter__(self) -> Any:
        """Begin a transaction (or savepoint) and pin it as the active executor.

        Raises:
            TransactionManagementError: If an isolation level is requested for a
                nested block (it can only be set when the transaction begins).

        Returns:
            The active transaction wrapper.
        """
        existing = _active_tx.get()
        if existing is not None:
            if self.isolation is not None:
                raise TransactionManagementError(
                    "isolation level cannot be set on a nested transaction"
                )
            self._conn = existing
            self._savepoint = existing.new_savepoint()
            await existing.savepoint(self._savepoint)
            return existing
        engine = get_engine()
        isolation = None
        if self.isolation is not None:
            isolation = _normalize_isolation(self.isolation, get_dialect().name)
        self._conn = TransactionWrapper(await engine.begin(isolation))
        self._token = _active_tx.set(self._conn)
        return self._conn

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> bool:
        """Commit/release on clean exit, roll back on error, and unpin.

        Args:
            exc_type: Exception type raised in the block, or None.
            exc: Exception instance raised in the block, or None.
            tb: Traceback for the raised exception, or None.

        Returns:
            False, so any exception is propagated.
        """
        assert self._conn is not None
        if self._savepoint is not None:
            if exc_type is None:
                await self._conn.release(self._savepoint)
            else:
                await self._conn.rollback_to(self._savepoint)
            return False
        assert self._token is not None
        try:
            if exc_type is None:
                await self._conn.commit()
            else:
                await self._conn.rollback()
        finally:
            _active_tx.reset(self._token)
        return False

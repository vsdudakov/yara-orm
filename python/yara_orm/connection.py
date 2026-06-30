"""Connection lifecycle and the global engine/dialect holders.

The Rust engine is opaque to the rest of the package; everything goes through
the accessors here so the model layer never imports the native module directly.
"""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from . import _engine, registry
from . import timezone as _tz
from .dialects import BaseDialect
from .dialects import get_dialect as resolve_dialect
from .exceptions import (
    ConfigurationError,
    OperationalError,
    TransactionManagementError,
    UnSupportedError,
)

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

#: Optional pre-execute query hooks (register a hook to observe/annotate the SQL
#: of the Python query path). Each hook is
#: called as ``hook(sql, params)`` before a statement runs. Empty by default, so
#: the hot path pays nothing and ``get_executor`` returns the raw engine.
_QUERY_HOOKS: list = []

#: URL schemes treated as PostgreSQL (driver aliases normalised to ``postgres``).
_POSTGRES_URL_SCHEMES = frozenset({"postgres", "postgresql", "psycopg", "psycopg2", "asyncpg"})


def register_query_hook(hook: Any) -> None:
    """Register a callable invoked as ``hook(sql, params)`` before each query.

    Lets you wrap the query path (e.g. SQLCommenter,
    tracing, SQL logging) even though execution happens in the Rust engine.
    While any hook is registered, model and manual statements both route through
    a proxy that calls the hooks; with none registered there is zero overhead.

    Args:
        hook: A callable taking ``(sql, params)``; its return value is ignored.

    Returns:
        None
    """
    _QUERY_HOOKS.append(hook)


def clear_query_hooks() -> None:
    """Remove all registered query hooks.

    Returns:
        None
    """
    _QUERY_HOOKS.clear()


def _run_hooks(sql: str, params: list[Any] | None) -> None:
    """Invoke every registered query hook with the statement about to run.

    Args:
        sql: The SQL statement.
        params: The bind parameters, or None.

    Returns:
        None
    """
    for hook in _QUERY_HOOKS:
        hook(sql, params)


async def _run_query(method: Any, sql: str, params: list[Any] | None) -> Any:
    """Run one engine call: fire hooks, then translate engine errors.

    The native engine raises a bare ``RuntimeError`` for SQL failures; these are
    surfaced as ``OperationalError``, so callers' ``except OperationalError``
    handlers (retry/translation) keep working when re-raised as one here.

    Args:
        method: The bound engine/transaction coroutine method to call.
        sql: The SQL statement.
        params: The bind parameters, or None.

    Returns:
        The method's result.
    """
    _run_hooks(sql, params)
    try:
        return await method(sql, params or [])
    except RuntimeError as exc:
        raise OperationalError(str(exc)) from exc


def _split_sql_statements(script: str) -> list[str]:
    """Split a multi-statement SQL script into individual statements on ``;``.

    The native engine runs one command per call (prepared statement), so scripts
    must be split. The split respects dollar-quoted bodies (``$$ ... $$`` /
    ``$tag$ ... $tag$``), single-quoted strings (incl. ``''`` escapes) and
    ``--`` / ``/* */`` comments, so semicolons inside them do not terminate a
    statement (e.g. a ``DO $$ ... $$;`` PL/pgSQL block stays intact).

    Args:
        script: The SQL script possibly containing several statements.

    Returns:
        The non-empty statements, in order.
    """
    statements: list[str] = []
    buf: list[str] = []
    i, n = 0, len(script)
    dollar_tag: str | None = None
    in_squote = in_line_comment = in_block_comment = False
    while i < n:
        ch = script[i]
        two = script[i : i + 2]
        if in_line_comment:
            buf.append(ch)
            if ch == "\n":
                in_line_comment = False
            i += 1
        elif in_block_comment:
            buf.append(ch)
            if two == "*/":
                buf.append(script[i + 1])
                in_block_comment = False
                i += 2
            else:
                i += 1
        elif dollar_tag is not None:
            if script.startswith(dollar_tag, i):
                buf.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = None
            else:
                buf.append(ch)
                i += 1
        elif in_squote:
            buf.append(ch)
            if ch == "'":
                if i + 1 < n and script[i + 1] == "'":
                    buf.append("'")
                    i += 2
                    continue
                in_squote = False
            i += 1
        elif two == "--":
            in_line_comment = True
            buf.append(two)
            i += 2
        elif two == "/*":
            in_block_comment = True
            buf.append(two)
            i += 2
        elif ch == "'":
            in_squote = True
            buf.append(ch)
            i += 1
        elif ch == "$":
            j = i + 1
            while j < n and (script[j].isalnum() or script[j] == "_"):
                j += 1
            if j < n and script[j] == "$":
                dollar_tag = script[i : j + 1]
                buf.append(dollar_tag)
                i = j + 1
            else:
                buf.append(ch)
                i += 1
        elif ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
        else:
            buf.append(ch)
            i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


class Record(dict):
    """A raw-SQL result row allowing positional **and** key access.

    ``asyncpg.Record`` supports both
    ``row["col"]`` and ``row[0]``; yara's raw rows are dicts, so this subclass
    restores positional/slice indexing for ported code. Column names are always
    strings, so an integer key is unambiguously positional.
    """

    def __getitem__(self, key: Any) -> Any:
        """Return a column by name (str) or by position (int/slice).

        Args:
            key: A column name, or a positional index/slice into the row's
                values in column order.

        Returns:
            The matching column value (or a tuple of values for a slice).
        """
        if isinstance(key, (int, slice)):
            return tuple(self.values())[key]
        return super().__getitem__(key)


def _as_records(rows: Any) -> Any:
    """Wrap a list of dict rows as :class:`Record` for positional access.

    Args:
        rows: The rows returned by the engine (each a ``dict``).

    Returns:
        The rows as :class:`Record` instances (non-dict rows pass through).
    """
    return [Record(r) if isinstance(r, dict) else r for r in rows]


class _ManualSQLCompat:
    """Raw-SQL compatibility methods shared by manual-SQL executors.

    Mixed into the pooled-connection proxy and the transaction wrapper so raw
    SQL using ``execute_query`` / ``execute_query_dict`` /
    ``execute_script`` keeps working. Implementations build on the host's
    ``execute`` / ``fetch_all`` (both already translate engine errors).
    """

    async def execute(self, sql: str, params: list[Any] | None = None) -> Any:
        """Execute a statement (provided by the concrete host).

        Args:
            sql: SQL statement to execute.
            params: Bind parameters, or None.

        Returns:
            The host's execute result.
        """
        raise NotImplementedError  # pragma: no cover - provided by host

    async def fetch_all(self, sql: str, params: list[Any] | None = None) -> Any:
        """Fetch rows as dicts (provided by the concrete host).

        Args:
            sql: SQL query to execute.
            params: Bind parameters, or None.

        Returns:
            The fetched rows as dicts.
        """
        raise NotImplementedError  # pragma: no cover - provided by host

    async def execute_query(
        self, sql: str, params: list[Any] | None = None
    ) -> tuple[int, list[dict[str, Any]]]:
        """Run ``sql`` and return ``(rowcount, rows)``.

        Rows are dicts, as returned for SELECTs; ``rowcount`` is the
        number of rows returned. Callers that only need the rows can unpack as
        ``_, rows = await conn.execute_query(...)``.

        Args:
            sql: The SQL statement.
            params: Bind parameters, or None.

        Returns:
            A ``(rowcount, rows)`` tuple.
        """
        rows = await self.fetch_all(sql, params)
        return len(rows), rows

    async def execute_query_dict(
        self, sql: str, params: list[Any] | None = None
    ) -> list[dict[str, Any]]:
        """Run ``sql`` and return the rows as dicts.

        Args:
            sql: The SQL statement.
            params: Bind parameters, or None.

        Returns:
            The rows as a list of dicts.
        """
        return await self.fetch_all(sql, params)

    async def execute_script(self, script: str) -> None:
        """Run a multi-statement SQL script, one statement at a time.

        ``execute_script`` accepts whole scripts; the native engine
        runs one command per call, so the script is split (dollar-quote / string
        / comment aware) and each statement executed in order.

        Args:
            script: The SQL script to run.

        Returns:
            None
        """
        for statement in _split_sql_statements(script):
            await self.execute(statement)


@runtime_checkable
class BaseDBAsyncClient(Protocol):
    """Structural type for a database executor.

    Both the pooled-connection proxy and the transaction wrapper satisfy it, so
    annotations like ``using_db: BaseDBAsyncClient | None`` keep
    their meaning. It is the public type for objects returned by
    ``connections.get()`` / yielded by ``in_transaction()``.
    """

    async def execute(self, sql: str, params: list[Any] | None = ...) -> Any:
        """Execute a statement and return the driver result."""
        ...

    async def fetch_all(self, sql: str, params: list[Any] | None = ...) -> Any:
        """Fetch rows as dicts."""
        ...

    async def fetch_row(self, sql: str, params: list[Any] | None = ...) -> Any:
        """Fetch a single positional row, or None."""
        ...

    async def execute_query(self, sql: str, params: list[Any] | None = ...) -> Any:
        """Run a statement and return ``(rowcount, rows)``."""
        ...


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
    if model is not None:
        # A model's ``Meta.default_connection`` pins it to a named connection.
        dc = getattr(model._meta, "default_connection", None)
        if dc:
            return dc
    return "default"


def get_executor(
    model: type[Model] | None = None, write: bool = False, using: str | None = None
) -> Any:
    """Return the object statements run on for ``model``.

    This is the active transaction, or the connection chosen for ``model`` by
    the router (falling back to the default pool). All such objects expose
    ``execute`` / ``fetch_row`` / ``fetch_rows`` / ``fetch_all``.

    Args:
        model: Model class used to route to a connection, or None.
        write: Whether the executor is for a write operation.
        using: Explicit connection (from ``QuerySet.using_db``) overriding the
            router: a registered connection name, or a connection/executor
            object used directly. An active transaction still takes precedence.

    Returns:
        The active transaction or the routed connection object.
    """
    tx = _active_tx.get()
    if tx is not None:
        return tx
    if using is not None and not isinstance(using, str):
        # ``using_db`` was given a connection/executor object; use it directly.
        return using
    if using is not None:
        engine = _named_connection(using)[0]
    else:
        name = _route(model, write)
        # "default" goes through get_engine() so an uninitialised ORM errors clearly.
        engine = get_engine() if name == "default" else _named_connection(name)[0]
    # While query hooks are registered, route the hot path through the proxy so
    # model statements fire them too; otherwise return the raw engine (no cost).
    return _EngineProxy(engine) if _QUERY_HOOKS else engine


def get_dialect(model: type[Model] | None = None, using: str | None = None) -> BaseDialect:
    """Return the SQL dialect for ``model``'s connection.

    Args:
        model: Model class used to route to a connection, or None.
        using: Explicit connection name (from ``QuerySet.using_db``) that
            overrides the router.

    Returns:
        The dialect for the resolved connection.
    """
    # An active transaction pins the connection, so SQL must be rendered for
    # that connection's dialect — mirroring get_executor, which gives the
    # transaction precedence over the router and any ``using`` override.
    tx = _active_tx.get()
    if tx is not None and tx.dialect is not None:
        return tx.dialect
    if using is not None:
        return _named_connection(using)[1]
    name = _route(model, False)
    if name == "default":
        if _DIALECT is None:
            raise ConfigurationError(
                "ORM is not initialised. Call `await YaraOrm.init(db_url=...)` first."
            )
        return _DIALECT
    return _named_connection(name)[1]


def _named_connection(name: str) -> tuple[Any, BaseDialect]:
    """Return the ``(engine, dialect)`` pair registered under ``name``.

    Args:
        name: The connection name.

    Raises:
        ConfigurationError: If no connection is registered under ``name``.

    Returns:
        The ``(engine, dialect)`` tuple for the named connection.
    """
    try:
        return _CONNECTIONS[name]
    except KeyError as exc:
        raise ConfigurationError(f"No connection named {name!r}") from exc


def _topo_sort_models(models: list[type[Model]]) -> list[type[Model]]:
    """Order models so each model's foreign-key targets precede it.

    ``create_table_sql`` emits inline foreign-key constraints, so a referencing
    table must be created after its target. Self-references are ignored (Postgres
    resolves them within the same ``CREATE TABLE``); a residual cycle falls back
    to the input order so creation still makes progress.

    Args:
        models: The models to order (a subset or all registered models).

    Returns:
        The models in foreign-key dependency order.
    """
    from .relations import model_name

    included = {m.__name__: m for m in models}
    deps: dict[str, set[str]] = {}
    for model in models:
        targets: set[str] = set()
        for info in model._meta.relations.values():
            try:
                target = registry.get_model(model_name(info.reference))
            except KeyError:  # pragma: no cover - unresolved cross-set reference
                continue
            if target is model or target.__name__ not in included:
                continue
            targets.add(target.__name__)
        deps[model.__name__] = targets

    ordered: list[type[Model]] = []
    emitted: set[str] = set()
    remaining = [m.__name__ for m in models]
    while remaining:
        ready = [name for name in remaining if deps[name] <= emitted]
        if not ready:  # pragma: no cover - defensive cycle fallback
            ready = remaining
        for name in ready:
            ordered.append(included[name])
            emitted.add(name)
        remaining = [name for name in remaining if name not in emitted]
    return ordered


class YaraOrm:
    """Entry point: initialise connections, generate schemas and resolve relations."""

    @classmethod
    async def init(
        cls,
        db_url: str | None = None,
        router: Any = None,
        use_tz: bool = False,
        timezone: str = "UTC",
        *,
        config: dict[str, Any] | None = None,
    ) -> None:
        """Connect the default connection and resolve relations.

        Pass ``db_url`` for the modern URL form, or ``config`` with a
        config dict (``{"connections": {...}, "use_tz": ...,
        "timezone": ...}``) to migrate existing config-driven setups. Register
        further connections with :meth:`add_connection`.

        Args:
            db_url: Database URL for the default connection (URL form).
            router: Optional router selecting a connection per model.
            use_tz: When True, :func:`yara_orm.timezone.now` returns timezone-aware
                UTC datetimes (used for ``auto_now``/``auto_now_add``).
            timezone: IANA name of the timezone datetimes are presented in.
            config: Config dict, as an alternative to ``db_url``.

        Returns:
            None
        """
        if config is not None:
            await cls._init_from_config(config, router=router)
            return
        if db_url is None:
            raise ConfigurationError("YaraOrm.init requires either db_url or config=")
        global _ENGINE, _DIALECT, _ROUTER
        _tz._set_config(timezone=timezone, use_tz=use_tz)
        _ENGINE = await _engine.connect(cls._normalize_url(db_url))
        _DIALECT = resolve_dialect(_ENGINE.dialect)
        _CONNECTIONS["default"] = (_ENGINE, _DIALECT)
        _ROUTER = router
        registry.resolve_relations()

    @staticmethod
    def _normalize_url(db_url: str) -> str:
        """Rewrite diff-style postgres URL schemes to ``postgres://``.

        Existing ``DATABASE_URI`` values often use a driver-qualified
        scheme (``psycopg://``, ``asyncpg://``, ``postgresql+asyncpg://``); the
        engine only understands ``postgres``/``postgresql``, so the driver alias
        is normalised away. Non-postgres URLs (e.g. ``sqlite://``) pass through.

        Args:
            db_url: The connection URL as provided by the caller.

        Returns:
            The URL with a postgres-family scheme rewritten to ``postgres://``.
        """
        scheme, sep, rest = db_url.partition("://")
        if sep and scheme.split("+", 1)[0].lower() in _POSTGRES_URL_SCHEMES:
            return f"postgres://{rest}"
        return db_url

    @staticmethod
    def _connection_url(spec: Any) -> str:
        """Resolve a connection spec to a database URL.

        Accepts a URL string directly, or a ``{"engine", "credentials": {...}}``
        mapping (the structured form) which is rendered into a postgres URL.

        Args:
            spec: A URL string or a connection mapping.

        Returns:
            The database URL string.
        """
        if isinstance(spec, str):
            return spec
        creds = spec.get("credentials", {})
        user = creds.get("user", "")
        password = creds.get("password", "")
        auth = f"{user}:{password}@" if user else ""
        host = creds.get("host", "localhost")
        port = creds.get("port", 5432)
        database = creds.get("database", "")
        return f"postgres://{auth}{host}:{port}/{database}"

    @classmethod
    async def _init_from_config(cls, config: dict[str, Any], router: Any = None) -> None:
        """Initialise from a config dict.

        Args:
            config: The config dict.
            router: Optional router selecting a connection per model.

        Returns:
            None
        """
        connections_cfg = config.get("connections", {})
        if "default" not in connections_cfg:
            raise ConfigurationError("config must define a 'default' connection")
        await cls.init(
            cls._connection_url(connections_cfg["default"]),
            router=router,
            use_tz=bool(config.get("use_tz", False)),
            timezone=config.get("timezone", "UTC"),
        )
        for name, spec in connections_cfg.items():
            if name != "default":
                await cls.add_connection(name, cls._connection_url(spec))

    @classmethod
    async def add_connection(cls, name: str, db_url: str) -> None:
        """Register an additional named connection.

        Args:
            name: Name to register the connection under.
            db_url: Database URL to connect to.

        Returns:
            None
        """
        engine = await _engine.connect(cls._normalize_url(db_url))
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
    def get_connection(cls, name: str = "default") -> Any:
        """Return the manual-SQL executor for ``name``.

        Args:
            name: Connection name to look up.

        Returns:
            The active transaction or a proxy over the named connection.
        """
        return connections.get(name)

    @classmethod
    async def close_connections(cls) -> None:
        """Close all connections (alias for :meth:`close`).

        Returns:
            None
        """
        await cls.close()

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
        targets = _topo_sort_models(list(models) if models is not None else registry.all_models())
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
    def get_schema_sql(cls, safe: bool = True, models: list[type[Model]] | None = None) -> str:
        """Return the schema DDL for the models without executing it.

        The read-only counterpart of :meth:`generate_schemas`: it builds the
        ``CREATE TABLE`` / index / join-table statements for the registered
        models (or the given subset) and returns them as one SQL string, handy
        for previewing or dumping a schema. Requires the ORM to be initialised
        (the active dialect determines the SQL).

        Args:
            safe: Whether to emit ``IF NOT EXISTS``-style guards.
            models: Models to render, in dependency order; defaults to every
                registered model.

        Returns:
            The schema DDL as a single ``;``-terminated string (empty when
            there are no models).
        """
        registry.resolve_relations()
        targets = _topo_sort_models(list(models) if models is not None else registry.all_models())
        statements: list[str] = []
        for model in targets:
            dialect = get_dialect(model)
            statements.extend(dialect.create_table_sql(model._meta, safe=safe))
        seen: set[str] = set()
        for model in targets:
            dialect = get_dialect(model)
            for info in model._meta.m2m.values():
                info.finalize()
                if info.through in seen:  # pragma: no cover - defensive de-dup
                    continue
                seen.add(info.through)
                statements.extend(dialect.create_m2m_table_sql(info, safe=safe))
        return "\n".join(f"{statement};" for statement in statements)

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
        _tz._set_config(timezone="UTC", use_tz=False)


def run_async(coro: Coroutine[Any, Any, Any]) -> None:
    """Run a coroutine to completion, then close all connections.

    A convenience for scripts and one-off tasks: it drives the event loop with
    ``asyncio.run`` and guarantees :meth:`YaraOrm.close` runs even if the
    coroutine raises, so connections never leak.

    Args:
        coro: The coroutine to run (typically your ``main()``).

    Returns:
        None
    """

    async def _runner() -> None:
        try:
            await coro
        finally:
            await YaraOrm.close()

    asyncio.run(_runner())


class TransactionWrapper(_ManualSQLCompat):
    """Adapts a native transaction handle to the executor interface."""

    def __init__(self, tx: Any, dialect: BaseDialect | None = None) -> None:
        """Wrap a native transaction handle.

        Args:
            tx: The native transaction object to adapt.
            dialect: The dialect of the connection the transaction runs on, so
                statements routed to the transaction render for the right SQL.

        Returns:
            None
        """
        self._tx = tx
        #: Dialect of the pinned connection (read by ``get_dialect``).
        self.dialect = dialect
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
        return await _run_query(self._tx.execute, sql, params)

    async def fetch_rows(self, sql: str, params: list[Any] | None = None) -> Any:
        """Fetch multiple rows for a query on the transaction.

        Args:
            sql: SQL query to execute.
            params: Bind parameters, or None for no parameters.

        Returns:
            The fetched rows.
        """
        return await _run_query(self._tx.fetch_rows, sql, params)

    async def fetch_row(self, sql: str, params: list[Any] | None = None) -> Any:
        """Fetch a single row for a query on the transaction.

        Args:
            sql: SQL query to execute.
            params: Bind parameters, or None for no parameters.

        Returns:
            The fetched row, if any.
        """
        return await _run_query(self._tx.fetch_row, sql, params)

    async def fetch_all(self, sql: str, params: list[Any] | None = None) -> Any:
        """Fetch all results for a query on the transaction.

        Args:
            sql: SQL query to execute.
            params: Bind parameters, or None for no parameters.

        Returns:
            The fetched results.
        """
        return _as_records(await _run_query(self._tx.fetch_all, sql, params))

    async def fetch_one(self, sql: str, params: list[Any] | None = None) -> Any:
        """Fetch a single row as a dict on the transaction.

        Args:
            sql: SQL query to execute.
            params: Bind parameters, or None for no parameters.

        Returns:
            The fetched row as a dict, or None.
        """
        # The native transaction has no ``fetch_one``; derive it from the dict
        # rows so the manual-SQL surface matches the pooled connection's.
        rows = await self.fetch_all(sql, params)
        return rows[0] if rows else None

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


class _EngineProxy(_ManualSQLCompat):
    """Wraps the native engine to add compatibility manual-SQL methods.

    Returned by ``connections.get()`` (and by ``get_executor`` while query hooks
    are registered) so raw-SQL call sites get ``execute_query`` /
    ``execute_query_dict`` / ``execute_script`` / ``fetch_one``, every statement
    fires the query hooks, and engine ``RuntimeError``s surface as
    ``OperationalError``. Unknown attributes (``begin``, ``close``,
    ``execute_many``, ``dialect``) pass through to the wrapped engine.
    """

    def __init__(self, engine: Any) -> None:
        """Wrap a native engine.

        Args:
            engine: The native engine to delegate to.

        Returns:
            None
        """
        self._engine = engine

    def __getattr__(self, name: str) -> Any:
        """Delegate unknown attributes to the wrapped engine.

        Args:
            name: The attribute name.

        Returns:
            The engine's attribute.
        """
        return getattr(self._engine, name)

    async def execute(self, sql: str, params: list[Any] | None = None) -> Any:
        """Execute a statement on the pooled connection.

        Args:
            sql: SQL statement to execute.
            params: Bind parameters, or None.

        Returns:
            The native driver's execute result.
        """
        return await _run_query(self._engine.execute, sql, params)

    async def fetch_rows(self, sql: str, params: list[Any] | None = None) -> Any:
        """Fetch rows as positional tuples on the pooled connection.

        Args:
            sql: SQL query to execute.
            params: Bind parameters, or None.

        Returns:
            The fetched rows.
        """
        return await _run_query(self._engine.fetch_rows, sql, params)

    async def fetch_row(self, sql: str, params: list[Any] | None = None) -> Any:
        """Fetch a single positional row on the pooled connection.

        Args:
            sql: SQL query to execute.
            params: Bind parameters, or None.

        Returns:
            The fetched row, or None.
        """
        return await _run_query(self._engine.fetch_row, sql, params)

    async def fetch_all(self, sql: str, params: list[Any] | None = None) -> Any:
        """Fetch all rows as dicts on the pooled connection.

        Args:
            sql: SQL query to execute.
            params: Bind parameters, or None.

        Returns:
            The fetched rows as dicts.
        """
        return _as_records(await _run_query(self._engine.fetch_all, sql, params))

    async def fetch_one(self, sql: str, params: list[Any] | None = None) -> Any:
        """Fetch a single row as a dict on the pooled connection.

        Args:
            sql: SQL query to execute.
            params: Bind parameters, or None.

        Returns:
            The fetched row as a dict, or None.
        """
        return await _run_query(self._engine.fetch_one, sql, params)


class _Connections:
    """Minimal ``connections``-style accessor for manual SQL.

    ``connections.get(name)`` returns the active executor (transaction or pool),
    exposing ``execute`` / ``fetch_all`` / ``fetch_rows`` plus the compatibility
    ``execute_query`` / ``execute_query_dict`` / ``execute_script``.
    """

    def get(self, name: str = "default") -> Any:
        """Return the active executor for ``name``.

        Args:
            name: Connection name to look up.

        Returns:
            The active transaction, or a proxy over the named/default
            connection that adds the compatibility raw-SQL methods.
        """
        tx = _active_tx.get()
        if tx is not None:
            return tx
        if name in _CONNECTIONS:
            return _EngineProxy(_CONNECTIONS[name][0])
        return _EngineProxy(get_executor())


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
        engine, dialect = _named_connection(self.connection_name)
        isolation = None
        if self.isolation is not None:
            isolation = _normalize_isolation(self.isolation, dialect.name)
        self._conn = TransactionWrapper(await engine.begin(isolation), dialect)
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

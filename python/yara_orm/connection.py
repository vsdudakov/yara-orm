"""Connection lifecycle and the global engine/dialect holders.

The Rust engine is opaque to the rest of the package; everything goes through
the accessors here so the model layer never imports the native module directly.
"""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Awaitable, Callable, Coroutine
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
    from types import TracebackType

    from .models import Model

    class Router(Protocol):
        """Structural type for a per-model connection router.

        Any object with ``db_for_read`` / ``db_for_write`` methods returning a
        connection name (or a falsy value to fall through) qualifies.
        """

        def db_for_read(self, model: type[Model]) -> str | None:
            """Return the connection name for reads on ``model``, or None."""
            ...

        def db_for_write(self, model: type[Model]) -> str | None:
            """Return the connection name for writes on ``model``, or None."""
            ...


_ENGINE: _engine.Engine | None = None
_DIALECT: BaseDialect | None = None

#: Named connections: name -> (engine, dialect). "default" is the primary.
_CONNECTIONS: dict[str, tuple[_engine.Engine, BaseDialect]] = {}
#: Optional router selecting a connection per model (see docs/Router example).
_ROUTER: Router | None = None

#: When inside ``in_transaction()``, the active pinned connections keyed by
#: connection name (``None`` outside any transaction). Query execution routes
#: through the wrapper registered for the statement's *resolved* connection
#: name, so a transaction on one connection never absorbs statements destined
#: for another database. The dict is treated as immutable: entering a
#: transaction sets a *copy* with the new entry (contextvars snapshot values,
#: they do not track mutation).
_active_tx: contextvars.ContextVar[dict[str, TransactionWrapper] | None] = contextvars.ContextVar(
    "orm_active_tx", default=None
)


def _active_tx_for(name: str) -> TransactionWrapper | None:
    """Return the active transaction pinned to connection ``name``, if any.

    Args:
        name: The resolved connection name.

    Returns:
        The pinned :class:`TransactionWrapper`, or None.
    """
    txs = _active_tx.get()
    if txs is None:
        return None
    return txs.get(name)


#: Optional pre-execute query hooks (register a hook to observe/annotate the SQL
#: of the Python query path). Each hook is
#: called as ``hook(sql, params)`` before a statement runs. Empty by default, so
#: the hot path pays nothing and ``get_executor`` returns the raw engine.
_QUERY_HOOKS: list[Callable[[str, list[Any] | None], object]] = []

#: Optional query annotators (see :func:`register_query_annotator`): zero-arg
#: callables returning an attribution string (or None/"" to skip). While any is
#: registered, every statement on the Python query path executes with one
#: leading ``/* ... */`` comment composed from the non-empty results. Empty by
#: default, so the hot path pays nothing (``get_executor`` returns the raw
#: engine).
_QUERY_ANNOTATORS: list[Callable[[], str | None]] = []

#: URL schemes treated as PostgreSQL (driver aliases normalised to ``postgres``).
_POSTGRES_URL_SCHEMES = frozenset({"postgres", "postgresql", "psycopg", "psycopg2", "asyncpg"})

#: URL schemes treated as MySQL (driver aliases normalised to ``mysql``; the
#: engine's driver also speaks the MariaDB protocol).
_MYSQL_URL_SCHEMES = frozenset({"mysql", "mariadb", "aiomysql", "asyncmy", "pymysql"})

#: URL schemes treated as Microsoft SQL Server (normalised to ``mssql``).
_MSSQL_URL_SCHEMES = frozenset({"mssql", "sqlserver"})


def register_query_hook(hook: Callable[[str, list[Any] | None], object]) -> None:
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


def register_query_annotator(annotator: Callable[[], str | None]) -> Callable[[], str | None]:
    """Register a callable whose return value is embedded in a SQL comment.

    Enables per-request query attribution (pg_stat_statements, Datadog, ...):
    each annotator is called before every statement on the Python query path
    and typically pulls values from the application's own contextvars. The
    non-empty results of all annotators are joined with ``,`` in registration
    order into one leading comment, so a statement executes as::

        /* http_path=/api/calls,caller=list_calls */ SELECT ...

    Usable as a decorator (returns ``annotator`` unchanged)::

        @yara_orm.register_query_annotator
        def annotator() -> str | None:
            return f"http_path={path.get()}"

    Returning None (or an empty string) skips that annotator for the
    statement. Exceptions raised by an annotator propagate to the query
    caller, matching query-hook behaviour. Returned values are sanitised
    (comment delimiters and control characters are stripped) so a value can
    never terminate the comment early. Query hooks observe the final SQL,
    including the comment. Like hooks, annotators add zero overhead while none
    are registered.

    Note:
        The PostgreSQL statement cache is keyed on the SQL text, so
        high-cardinality values (request ids, timestamps) defeat
        prepared-statement reuse. Prefer low-cardinality attribution values
        (route, caller), or disable the cache with ``statement_cache_size=0``
        in the connection URL.

    Args:
        annotator: A zero-arg callable returning the attribution string, or
            None/"" to contribute nothing for this statement.

    Returns:
        The annotator, unchanged (decorator-friendly).
    """
    _QUERY_ANNOTATORS.append(annotator)
    return annotator


def clear_query_annotators() -> None:
    """Remove all registered query annotators.

    Returns:
        None
    """
    _QUERY_ANNOTATORS.clear()


def _sanitize_comment_value(value: str) -> str:
    """Make an annotator's value safe to embed inside a ``/* ... */`` comment.

    Strips control characters/newlines, then removes every ``*/`` (which would
    terminate the comment and let the remainder execute as SQL) and ``/*``
    (PostgreSQL nests block comments, so an unbalanced opener would swallow
    the statement). The removal loops because deleting one delimiter can
    splice a new one together (e.g. ``*␀/`` or ``*/*/``).

    Args:
        value: The raw string an annotator returned.

    Returns:
        The sanitised value (possibly empty).
    """
    value = "".join(ch for ch in value if ch.isprintable())
    while "*/" in value or "/*" in value:
        value = value.replace("*/", "").replace("/*", "")
    return value.strip()


def _compose_annotation() -> str:
    """Build the leading SQL comment from the registered annotators.

    Returns:
        ``"/* joined,values */ "`` (trailing space included), or ``""`` when
        every annotator returned nothing.
    """
    parts: list[str] = []
    for annotator in _QUERY_ANNOTATORS:
        raw = annotator()
        if not raw:
            continue
        value = _sanitize_comment_value(raw)
        if value:
            parts.append(value)
    if not parts:
        return ""
    return f"/* {','.join(parts)} */ "


def _annotate_sql(sql: str) -> str:
    """Prepend the composed annotation comment to ``sql``, if any.

    Args:
        sql: The SQL statement about to run.

    Returns:
        The statement, prefixed with the annotation comment when annotators
        are registered and produced a value.
    """
    if not _QUERY_ANNOTATORS:
        return sql
    return _compose_annotation() + sql


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


async def _run_query(
    method: Callable[[str, list[Any]], Awaitable[Any]], sql: str, params: list[Any] | None
) -> Any:
    """Run one engine call: annotate, fire hooks, then translate engine errors.

    The single choke point of the Python query path: the annotation comment is
    prepended here, so every statement that reaches the engine through the
    transaction wrapper or the engine proxy carries it, and hooks observe the
    final SQL (comment included). The engine maps SQL failures to
    ``OperationalError`` at its source, so callers' ``except OperationalError``
    handlers work uniformly; the ``RuntimeError`` fallback below only catches a
    bare ``RuntimeError`` from the engine's rare typed-exception fallback path.

    Args:
        method: The bound engine/transaction coroutine method to call.
        sql: The SQL statement.
        params: The bind parameters, or None.

    Returns:
        The method's result.
    """
    sql = _annotate_sql(sql)
    _run_hooks(sql, params)
    try:
        return await method(sql, params or [])
    except RuntimeError as exc:  # pragma: no cover - defensive; engine maps at source
        raise OperationalError(str(exc)) from exc


def _split_sql_statements(script: str, nest_block_comments: bool = False) -> list[str]:
    """Split a multi-statement SQL script into individual statements on ``;``.

    The native engine runs one command per call (prepared statement), so scripts
    must be split. The split respects dollar-quoted bodies (``$$ ... $$`` /
    ``$tag$ ... $tag$``), single-quoted strings (incl. ``''`` escapes),
    double-quoted identifiers (incl. ``""`` escapes) and ``--`` / ``/* */``
    comments, so semicolons inside them do not terminate a statement (e.g. a
    ``DO $$ ... $$;`` PL/pgSQL block stays intact).

    Args:
        script: The SQL script possibly containing several statements.
        nest_block_comments: Whether ``/* */`` block comments nest. Only
            PostgreSQL does; for every other dialect an inner ``/*`` is literal
            and the comment ends at the first ``*/``. Passing ``True`` where the
            engine does not nest would swallow a real ``;`` and merge statements.

    Returns:
        The non-empty statements, in order.
    """
    statements: list[str] = []
    buf: list[str] = []
    i, n = 0, len(script)
    dollar_tag: str | None = None
    in_squote = in_dquote = in_line_comment = False
    # PostgreSQL block comments nest, so track depth rather than a boolean: the
    # comment ends only when the outermost ``*/`` closes. Without this, an inner
    # ``*/`` is mistaken for the end and a following ``;`` wrongly splits. On
    # dialects that do not nest (``nest_block_comments=False``) an inner ``/*``
    # is literal and the first ``*/`` closes, so depth never exceeds 1.
    block_depth = 0
    while i < n:
        ch = script[i]
        two = script[i : i + 2]
        if in_line_comment:
            buf.append(ch)
            if ch == "\n":
                in_line_comment = False
            i += 1
        elif block_depth > 0:
            if nest_block_comments and two == "/*":
                block_depth += 1
                buf.append(two)
                i += 2
            elif two == "*/":
                block_depth -= 1
                buf.append(two)
                i += 2
            else:
                buf.append(ch)
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
        elif in_dquote:
            buf.append(ch)
            if ch == '"':
                # A doubled quote inside a quoted identifier is an escape.
                if i + 1 < n and script[i + 1] == '"':
                    buf.append('"')
                    i += 2
                    continue
                in_dquote = False
            i += 1
        elif two == "--":
            in_line_comment = True
            buf.append(two)
            i += 2
        elif two == "/*":
            block_depth += 1
            buf.append(two)
            i += 2
        elif ch == "'":
            in_squote = True
            buf.append(ch)
            i += 1
        elif ch == '"':
            in_dquote = True
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

    def __getitem__(self, key: str | int | slice) -> Any:
        """Return a column by name (str) or by position (int/slice).

        Args:
            key: A column name, or a positional index/slice into the row's
                values in column order.

        Returns:
            The matching column value (or a tuple of values for a slice).
        """
        if isinstance(key, (int, slice)):
            # Cache the values tuple: a result row is not mutated, so reading it
            # positionally (row[0], row[1], ...) would otherwise rebuild the
            # whole tuple on every access — O(n) per read, O(n^2) per row.
            values = getattr(self, "_values", None)
            if values is None:
                values = tuple(self.values())
                self._values = values
            return values[key]
        return super().__getitem__(key)


def _as_records(rows: list[dict[str, Any]]) -> list[Record]:
    """Wrap a list of dict rows as :class:`Record` for positional access.

    Args:
        rows: The rows returned by the engine (each a ``dict``).

    Returns:
        The rows as :class:`Record` instances (non-dict rows pass through).
    """
    return [Record(r) if isinstance(r, dict) else r for r in rows]


def _arrayify(value: Any) -> Any:
    """Bind a raw-SQL ``list``/``tuple`` parameter as a PostgreSQL array.

    A bare list otherwise binds as JSON (so a ``JSONField`` round-trips), but in
    a raw query ``WHERE col = ANY($1)`` / ``unnest($1::int[])`` the caller means
    a real array — matching asyncpg, which binds lists to arrays natively. Lists
    and tuples are wrapped in :class:`~yara_orm.Array` (recursively, so nested
    lists become multi-dimensional arrays); element types (UUID/Decimal/date/…)
    are coerced by the engine's array encoder. ``str``/``bytes``/``dict`` and an
    already-``Array`` value pass through unchanged.

    Args:
        value: A single raw-SQL bind parameter.

    Returns:
        The value, with lists/tuples wrapped as ``Array``.
    """
    from .expressions import Array

    if isinstance(value, Array) or not isinstance(value, (list, tuple)):
        return value
    return Array(_arrayify(v) for v in value)


def _arrayify_params(params: list[Any] | None) -> list[Any] | None:
    """Apply :func:`_arrayify` to every raw-SQL bind parameter.

    Args:
        params: The raw-SQL bind parameters, or None.

    Returns:
        The parameters with list/tuple values wrapped as arrays, or None.
    """
    return None if params is None else [_arrayify(p) for p in params]


class _ManualSQLCompat:
    """Raw-SQL compatibility methods shared by manual-SQL executors.

    Mixed into the pooled-connection proxy and the transaction wrapper so raw
    SQL using ``execute_query`` / ``execute_query_dict`` /
    ``execute_script`` keeps working. Implementations build on the host's
    ``execute`` / ``fetch_all`` (both already translate engine errors).
    """

    async def execute(self, sql: str, params: list[Any] | None = None) -> int:
        """Execute a statement (provided by the concrete host).

        Args:
            sql: SQL statement to execute.
            params: Bind parameters, or None.

        Returns:
            The host's execute result.
        """
        raise NotImplementedError  # pragma: no cover - provided by host

    async def fetch_all(self, sql: str, params: list[Any] | None = None) -> list[Record]:
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
    ) -> tuple[int, list[Record]]:
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

    async def execute_query_dict(self, sql: str, params: list[Any] | None = None) -> list[Record]:
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
        nest = getattr(getattr(self, "dialect", None), "nests_block_comments", False)
        for statement in _split_sql_statements(script, nest):
            await self.execute(statement)


@runtime_checkable
class BaseDBAsyncClient(Protocol):
    """Structural type for a database executor.

    The raw engine, the pooled-connection proxy and the transaction wrapper
    all satisfy it, so annotations like ``using_db: BaseDBAsyncClient | None``
    keep their meaning. It is the public type for objects returned by
    ``get_executor()`` / ``connections.get()`` / yielded by
    ``in_transaction()``. Members are declared as ``Awaitable``-returning
    methods (which ``async def`` implementations satisfy) so the native
    engine's PyO3 methods match too.
    """

    def execute(self, sql: str, params: list[Any] = ...) -> Awaitable[Any]:
        """Execute a statement and return the driver result."""
        ...

    def fetch_all(self, sql: str, params: list[Any] = ...) -> Awaitable[Any]:
        """Fetch rows as dicts."""
        ...

    def fetch_rows(self, sql: str, params: list[Any] = ...) -> Awaitable[Any]:
        """Fetch rows as positional lists."""
        ...

    def fetch_row(self, sql: str, params: list[Any] = ...) -> Awaitable[Any]:
        """Fetch a single positional row, or None."""
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


def get_engine() -> _engine.Engine:
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
    model: type[Model] | None = None,
    write: bool = False,
    using: str | BaseDBAsyncClient | None = None,
) -> BaseDBAsyncClient:
    """Return the object statements run on for ``model``.

    This is the active transaction, or the connection chosen for ``model`` by
    the router (falling back to the default pool). All such objects expose
    ``execute`` / ``fetch_row`` / ``fetch_rows`` / ``fetch_all``.

    Args:
        model: Model class used to route to a connection, or None.
        write: Whether the executor is for a write operation.
        using: Explicit connection (from ``QuerySet.using_db``) overriding the
            router: a registered connection name, or a connection/executor
            object used directly. An active transaction on the *resolved*
            connection name takes precedence over its pool; a transaction on a
            different connection does not capture the statement.

    Returns:
        The active transaction for the resolved connection, or the routed
        connection object.
    """
    if using is not None and not isinstance(using, str):
        # ``using_db`` was given a connection/executor object; use it directly
        # (it may itself be a transaction wrapper).
        return using
    name = using if using is not None else _route(model, write)
    # A transaction pins only statements resolved to *its* connection name, so
    # a model routed to connection B is never absorbed by an open transaction
    # on connection A (which would silently write to the wrong database).
    tx = _active_tx_for(name)
    if tx is not None:
        return tx
    if not write and using is None:
        # Read-your-own-writes under a read/write-splitting router: a read the
        # router sends to a replica must still see rows the open transaction
        # wrote on the model's write connection, so that transaction captures
        # the read instead of the replica pool.
        write_name = _route(model, True)
        if write_name != name:
            tx = _active_tx_for(write_name)
            if tx is not None:
                return tx
    # "default" goes through get_engine() so an uninitialised ORM errors clearly.
    engine = get_engine() if name == "default" else _named_connection(name)[0]
    # While query hooks or annotators are registered, route the hot path
    # through the proxy so model statements fire/carry them too; otherwise
    # return the raw engine (no cost).
    return _EngineProxy(engine) if _QUERY_HOOKS or _QUERY_ANNOTATORS else engine


def get_dialect(
    model: type[Model] | None = None, using: str | BaseDBAsyncClient | None = None
) -> BaseDialect:
    """Return the SQL dialect for ``model``'s connection.

    Args:
        model: Model class used to route to a connection, or None.
        using: Explicit connection (from ``QuerySet.using_db``) that overrides
            the router: a registered connection name, or a connection/executor
            object — statements execute on that object, so SQL must render for
            *its* dialect, not the model-routed one.

    Returns:
        The dialect for the resolved connection.
    """
    # Resolution mirrors get_executor: the connection *name* decides. An active
    # transaction on that name was opened on the same connection, so its
    # dialect and the named connection's dialect are the same object; a
    # transaction on a different connection must not skew rendering.
    if using is not None:
        if isinstance(using, str):
            return _named_connection(using)[1]
        dialect = getattr(using, "dialect", None)
        if isinstance(dialect, BaseDialect):
            # A TransactionWrapper (or wrapper-like executor) carries the
            # dialect of the connection it is pinned to.
            return dialect
        if isinstance(dialect, str):
            # A raw engine / engine proxy exposes its dialect by name.
            return resolve_dialect(dialect)
        # Unknown executor object: fall back to model routing below.
    name = _route(model, False)
    tx = _active_tx_for(name)
    if tx is not None and tx.dialect is not None:
        return tx.dialect
    # Mirror get_executor's read-your-own-writes fallback: a read the router
    # sends elsewhere executes on the open write-connection transaction, so it
    # must render for that transaction's dialect too.
    write_name = _route(model, True)
    if write_name != name:
        tx = _active_tx_for(write_name)
        if tx is not None and tx.dialect is not None:
            return tx.dialect
    if name == "default":
        if _DIALECT is None:
            raise ConfigurationError(
                "ORM is not initialised. Call `await YaraOrm.init(db_url=...)` first."
            )
        return _DIALECT
    return _named_connection(name)[1]


def _named_connection(name: str) -> tuple[_engine.Engine, BaseDialect]:
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
    included = {m.__name__: m for m in models}
    deps: dict[str, set[str]] = {}
    for model in models:
        targets: set[str] = set()
        for info in model._meta.relations.values():
            try:
                target = registry.get_model(info.reference)
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
        router: Router | None = None,
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
        # Re-initialising without close() would leak the previous engine's
        # pool; close it once the replacement connected (so a failed connect
        # leaves the old connection usable).
        previous = _CONNECTIONS.get("default")
        _ENGINE = await _engine.connect(cls._normalize_url(db_url))
        _DIALECT = resolve_dialect(_ENGINE.dialect)
        _CONNECTIONS["default"] = (_ENGINE, _DIALECT)
        _ROUTER = router
        if previous is not None:
            await previous[0].close()
        registry.resolve_relations()

    @staticmethod
    def _normalize_url(db_url: str) -> str:
        """Rewrite driver-qualified URL schemes to their canonical form.

        Existing ``DATABASE_URI`` values often use a driver-qualified
        scheme (``psycopg://``, ``postgresql+asyncpg://``,
        ``mysql+aiomysql://``); the engine only understands
        ``postgres``/``postgresql``/``sqlite``/``mysql``, so the driver alias
        is normalised away. Other URLs (e.g. ``sqlite://``) pass through.

        Args:
            db_url: The connection URL as provided by the caller.

        Returns:
            The URL with a postgres-/mysql-family scheme rewritten to its
            canonical ``postgres://`` / ``mysql://`` form.
        """
        scheme, sep, rest = db_url.partition("://")
        if not sep:
            return db_url
        base = scheme.split("+", 1)[0].lower()
        if base in _POSTGRES_URL_SCHEMES:
            return f"postgres://{rest}"
        if base in _MYSQL_URL_SCHEMES:
            return f"mysql://{rest}"
        if base in _MSSQL_URL_SCHEMES:
            return f"mssql://{rest}"
        return db_url

    @staticmethod
    def _connection_url(spec: str | dict[str, Any]) -> str:
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
    async def _init_from_config(cls, config: dict[str, Any], router: Router | None = None) -> None:
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
        # Re-registering a name must not leak the old engine's pool: connect
        # the replacement first (a failed connect keeps the old one usable),
        # then close the engine being replaced.
        previous = _CONNECTIONS.get(name)
        engine = await _engine.connect(cls._normalize_url(db_url))
        _CONNECTIONS[name] = (engine, resolve_dialect(engine.dialect))
        if previous is not None:
            await previous[0].close()

    @classmethod
    def set_router(cls, router: Router | None) -> None:
        """Set the active per-model connection router.

        Args:
            router: Router object selecting connections per model.

        Returns:
            None
        """
        global _ROUTER
        _ROUTER = router

    @classmethod
    def get_connection(cls, name: str = "default") -> TransactionWrapper | _EngineProxy:
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

        Dialects that declare backend extensions for the models (PostgreSQL
        ``CREATE EXTENSION IF NOT EXISTS ...`` via ``extensions_sql``) have
        those statements executed first, on each model's write connection, so
        extension-provided column types exist before the tables that use them.
        The statements are ``IF NOT EXISTS``-idempotent, so they are safe to
        run regardless of ``safe``.

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
        # Backend extensions first, grouped per write connection so each
        # database receives exactly the extensions its own models need.
        # ``getattr`` guard: extensions_sql is an optional dialect capability.
        groups: dict[str, list[type[Model]]] = {}
        for model in targets:
            groups.setdefault(_route(model, True), []).append(model)
        for group in groups.values():
            dialect = get_dialect(group[0])
            extensions_sql = getattr(dialect, "extensions_sql", None)
            if extensions_sql is None:
                continue
            engine = get_executor(group[0], write=True)
            for statement in extensions_sql(group):
                await engine.execute(statement)
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
        try:
            # Close every pool even if one raises, so a single failing teardown
            # cannot leak the rest; surface the first error after cleanup.
            results = await asyncio.gather(
                *(engine.close() for engine, _ in _CONNECTIONS.values()),
                return_exceptions=True,
            )
        finally:
            _CONNECTIONS.clear()
            _ENGINE = None
            _DIALECT = None
            _ROUTER = None
            _tz._set_config(timezone="UTC", use_tz=False)
        for result in results:
            if isinstance(result, BaseException):
                raise result


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

    def __init__(
        self,
        tx: _engine.Transaction,
        dialect: BaseDialect | None = None,
        connection_name: str = "default",
    ) -> None:
        """Wrap a native transaction handle.

        Args:
            tx: The native transaction object to adapt.
            dialect: The dialect of the connection the transaction runs on, so
                statements routed to the transaction render for the right SQL.
            connection_name: Name of the connection the transaction is pinned
                to (used for routing and error messages).

        Returns:
            None
        """
        self._tx = tx
        #: Dialect of the pinned connection (read by ``get_dialect``).
        self.dialect = dialect
        #: Name of the connection this transaction runs on.
        self.connection_name = connection_name
        #: Monotonic counter producing unique savepoint names for nested blocks.
        self._savepoint_seq = 0
        #: Names handed out by :meth:`new_savepoint` (ORM-managed savepoints).
        self._own_savepoints: set[str] = set()
        #: ORM-managed savepoints currently open, in creation order. Nested
        #: ``in_transaction`` blocks release strictly LIFO, so an out-of-order
        #: release means concurrent tasks are interleaving savepoints in one
        #: transaction — which cannot be untangled, so it is surfaced early.
        self._sp_stack: list[str] = []

    async def _control(self, method: Callable[..., Awaitable[Any]], *args: Any) -> Any:
        """Run a transaction-control call, translating engine errors.

        The native layer raises ``TransactionManagementError`` for use of a
        finished transaction; any other native failure (a bare ``RuntimeError``
        from the driver) is surfaced as :class:`OperationalError`, matching the
        statement path (:func:`_run_query`).

        Args:
            method: The bound native coroutine method (commit/rollback/...).
            *args: Arguments forwarded to the method.

        Returns:
            The method's result.
        """
        try:
            return await method(*args)
        except RuntimeError as exc:  # pragma: no cover - defensive; engine maps at source
            raise OperationalError(str(exc)) from exc

    def new_savepoint(self) -> str:
        """Return a fresh, unique savepoint name for this transaction.

        Returns:
            A savepoint identifier unique within the transaction.
        """
        self._savepoint_seq += 1
        name = f"yara_sp_{self._savepoint_seq}"
        self._own_savepoints.add(name)
        return name

    def _check_savepoint_order(self, name: str) -> None:
        """Raise if an ORM-managed savepoint is resolved out of LIFO order.

        Args:
            name: The savepoint about to be released / rolled back to.

        Raises:
            TransactionManagementError: If ``name`` is ORM-managed but not the
                innermost open savepoint — the signature of concurrent tasks
                sharing one transaction, which is unsupported.

        Returns:
            None
        """
        if name in self._own_savepoints and self._sp_stack and self._sp_stack[-1] != name:
            raise TransactionManagementError(
                f"savepoint {name!r} resolved out of order (innermost is "
                f"{self._sp_stack[-1]!r}): concurrent tasks must not share one "
                "in_transaction() block — run each task in its own transaction"
            )

    def _pop_savepoint(self, name: str) -> None:
        """Drop ``name`` (and anything stacked on it) from the open-savepoint stack.

        Args:
            name: The savepoint that was released or rolled back to.

        Returns:
            None
        """
        if name in self._sp_stack:
            del self._sp_stack[self._sp_stack.index(name) :]

    async def savepoint(self, name: str) -> None:
        """Establish a savepoint on the transaction.

        Args:
            name: The savepoint name.

        Returns:
            None
        """
        await self._control(self._tx.savepoint, name)
        if name in self._own_savepoints:
            self._sp_stack.append(name)

    async def release(self, name: str) -> None:
        """Release (merge) a savepoint, keeping its work.

        Args:
            name: The savepoint name.

        Returns:
            None
        """
        self._check_savepoint_order(name)
        await self._control(self._tx.release, name)
        self._pop_savepoint(name)

    async def rollback_to(self, name: str) -> None:
        """Roll back to a savepoint, discarding work since it was set.

        Args:
            name: The savepoint name.

        Returns:
            None
        """
        self._check_savepoint_order(name)
        await self._control(self._tx.rollback_to, name)
        self._pop_savepoint(name)

    async def execute(self, sql: str, params: list[Any] | None = None) -> int:
        """Execute a statement on the transaction.

        Args:
            sql: SQL statement to execute.
            params: Bind parameters, or None for no parameters.

        Returns:
            The native driver's execute result.
        """
        return await _run_query(self._tx.execute, sql, params)

    async def fetch_rows(self, sql: str, params: list[Any] | None = None) -> list[list[Any]]:
        """Fetch multiple rows for a query on the transaction.

        Args:
            sql: SQL query to execute.
            params: Bind parameters, or None for no parameters.

        Returns:
            The fetched rows.
        """
        return await _run_query(self._tx.fetch_rows, sql, params)

    async def fetch_row(self, sql: str, params: list[Any] | None = None) -> list[Any] | None:
        """Fetch a single row for a query on the transaction.

        Args:
            sql: SQL query to execute.
            params: Bind parameters, or None for no parameters.

        Returns:
            The fetched row, if any.
        """
        return await _run_query(self._tx.fetch_row, sql, params)

    async def fetch_all(self, sql: str, params: list[Any] | None = None) -> list[Record]:
        """Fetch all results for a query on the transaction.

        Args:
            sql: SQL query to execute.
            params: Bind parameters, or None for no parameters.

        Returns:
            The fetched results.
        """
        return _as_records(await _run_query(self._tx.fetch_all, sql, _arrayify_params(params)))

    async def fetch_one(self, sql: str, params: list[Any] | None = None) -> Record | None:
        """Fetch a single row as a dict on the transaction.

        Args:
            sql: SQL query to execute.
            params: Bind parameters, or None for no parameters.

        Returns:
            The fetched row as a dict, or None.
        """
        # The native transaction has no ``fetch_one``; derive it from the dict
        # rows so the manual-SQL surface matches the pooled connection's
        # (fetch_all also applies the raw-SQL list->array binding).
        rows = await self.fetch_all(sql, params)
        return rows[0] if rows else None

    async def commit(self) -> None:
        """Commit the underlying transaction.

        Returns:
            None
        """
        await self._control(self._tx.commit)

    async def rollback(self) -> None:
        """Roll back the underlying transaction.

        Returns:
            None
        """
        await self._control(self._tx.rollback)


class _EngineProxy(_ManualSQLCompat):
    """Wraps the native engine to add compatibility manual-SQL methods.

    Returned by ``connections.get()`` (and by ``get_executor`` while query hooks
    or annotators are registered) so raw-SQL call sites get ``execute_query`` /
    ``execute_query_dict`` / ``execute_script`` / ``fetch_one``, every statement
    fires the query hooks and carries the annotation comment, and engine
    ``RuntimeError``s surface as ``OperationalError``. Unknown attributes (``begin``, ``close``,
    ``execute_many``, ``dialect``) pass through to the wrapped engine.

    Array binding: the dict-returning raw methods (``fetch_all`` /
    ``fetch_one`` / ``execute_query`` / ``execute_query_dict``) bind a bare
    ``list`` parameter as a PostgreSQL array (asyncpg-compatible). The
    positional methods (``execute`` / ``fetch_rows`` / ``fetch_row``) are
    shared with the model layer — where a bare list means a JSON value — so
    they bind lists as JSON; wrap the parameter in :class:`yara_orm.Array` to
    bind an array there.
    """

    def __init__(self, engine: _engine.Engine) -> None:
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

    async def execute(self, sql: str, params: list[Any] | None = None) -> int:
        """Execute a statement on the pooled connection.

        Args:
            sql: SQL statement to execute.
            params: Bind parameters, or None.

        Returns:
            The native driver's execute result.
        """
        return await _run_query(self._engine.execute, sql, params)

    async def fetch_rows(self, sql: str, params: list[Any] | None = None) -> list[list[Any]]:
        """Fetch rows as positional tuples on the pooled connection.

        Args:
            sql: SQL query to execute.
            params: Bind parameters, or None.

        Returns:
            The fetched rows.
        """
        return await _run_query(self._engine.fetch_rows, sql, params)

    async def fetch_row(self, sql: str, params: list[Any] | None = None) -> list[Any] | None:
        """Fetch a single positional row on the pooled connection.

        Args:
            sql: SQL query to execute.
            params: Bind parameters, or None.

        Returns:
            The fetched row, or None.
        """
        return await _run_query(self._engine.fetch_row, sql, params)

    async def fetch_all(self, sql: str, params: list[Any] | None = None) -> list[Record]:
        """Fetch all rows as dicts on the pooled connection.

        Args:
            sql: SQL query to execute.
            params: Bind parameters, or None.

        Returns:
            The fetched rows as dicts.
        """
        return _as_records(await _run_query(self._engine.fetch_all, sql, _arrayify_params(params)))

    async def fetch_one(self, sql: str, params: list[Any] | None = None) -> dict[str, Any] | None:
        """Fetch a single row as a dict on the pooled connection.

        Args:
            sql: SQL query to execute.
            params: Bind parameters, or None.

        Returns:
            The fetched row as a dict, or None.
        """
        # Like fetch_all, this is a raw-SQL-only method, so a bare list binds
        # as a PostgreSQL array (asyncpg-compatible) rather than JSON. Wrap the
        # row in a Record for positional access (row[0]), matching both
        # fetch_all here and TransactionWrapper.fetch_one — otherwise the same
        # call raises KeyError on the pooled path but works inside a transaction.
        row = await _run_query(self._engine.fetch_one, sql, _arrayify_params(params))
        return Record(row) if isinstance(row, dict) else row

    async def execute_script(self, script: str) -> None:
        """Run a multi-statement SQL script on one pinned pooled connection.

        The pooled path would otherwise run each statement on whichever
        connection the pool hands out, so session state (PRAGMA, SET, temp
        tables) and any explicit BEGIN/COMMIT would be split across
        connections. Every statement runs sequentially on a single connection
        in autocommit — no wrapping transaction — so non-transactional
        statements (``VACUUM``, ``CREATE INDEX CONCURRENTLY``) work and
        PRAGMAs take effect. A transaction the script leaves open is rolled
        back before the connection returns to the pool.

        Args:
            script: The SQL script to run.

        Returns:
            None
        """
        nest = resolve_dialect(self._engine.dialect).nests_block_comments
        statements = _split_sql_statements(script, nest)
        if not statements:
            return
        # This path hands the split statements straight to the engine (no
        # _run_query), so annotate here: each statement gets the comment (per-
        # statement attribution) and hooks observe the annotated script.
        comment = _compose_annotation() if _QUERY_ANNOTATORS else ""
        if comment:
            statements = [comment + statement for statement in statements]
            script = comment + script
        _run_hooks(script, None)
        try:
            await self._engine.execute_script(statements)
        except RuntimeError as exc:  # pragma: no cover - defensive; engine maps at source
            raise OperationalError(str(exc)) from exc


class _Connections:
    """Minimal ``connections``-style accessor for manual SQL.

    ``connections.get(name)`` returns the active executor (transaction or pool),
    exposing ``execute`` / ``fetch_all`` / ``fetch_rows`` plus the compatibility
    ``execute_query`` / ``execute_query_dict`` / ``execute_script``.
    """

    def get(self, name: str = "default") -> TransactionWrapper | _EngineProxy:
        """Return the active executor for ``name``.

        Args:
            name: Connection name to look up.

        Raises:
            ConfigurationError: If no connection is registered under ``name``
                (a typo must not silently run statements on the default
                database).

        Returns:
            The active transaction pinned to ``name``, or a proxy over the
            named connection that adds the compatibility raw-SQL methods.
        """
        # Only a transaction opened on *this* connection name is returned; a
        # transaction on another connection must not capture the statements.
        tx = _active_tx_for(name)
        if tx is not None:
            return tx
        if name == "default":
            # Wrap the raw default engine, not get_executor(): the latter
            # already returns an _EngineProxy while query hooks/annotators are
            # registered, which would double-wrap and fire the hooks twice for
            # raw SQL on this path. get_engine() errors clearly when
            # uninitialised.
            return _EngineProxy(get_engine())
        return _EngineProxy(_named_connection(name)[0])


connections = _Connections()


class in_transaction:
    """Async context manager running its body in a single DB transaction.

    Commits on clean exit, rolls back if the block raises. While active, all
    model/queryset statements *resolved to the same connection name* route
    through the pinned connection; statements routed to other connections run
    on their own pools (or their own transactions).

    Nesting is supported: a block entered while a transaction is active **on
    the same connection name** opens a **savepoint** instead of a new
    transaction, so the inner block can roll back (on error) without aborting
    the outer one, and its work is merged into the outer transaction on
    success. A nested block naming a *different* connection opens an
    independent sibling transaction on that connection (committed/rolled back
    on its own). An ``isolation`` level may be set where a transaction begins
    (the outermost block per connection name).

    .. warning::
        Spawning concurrent tasks (``asyncio.gather`` / ``create_task``) that
        share one open transaction is **unsupported**: sibling tasks would
        interleave statements and savepoints on the single pinned connection,
        destructively releasing each other's savepoints. Detected out-of-order
        savepoint handling raises :class:`TransactionManagementError`. (Tasks
        created inside the block inherit the transaction pin via their copied
        context.) Run each concurrent task in its own ``in_transaction()``
        block instead.
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
        self._token: contextvars.Token[dict[str, TransactionWrapper] | None] | None = None
        self._savepoint: str | None = None

    async def __aenter__(self) -> TransactionWrapper:
        """Begin a transaction (or savepoint) and pin it as the active executor.

        Raises:
            TransactionManagementError: If an isolation level is requested for a
                nested block (it can only be set when the transaction begins).

        Returns:
            The active transaction wrapper.
        """
        existing = _active_tx_for(self.connection_name)
        if existing is not None:
            # Same connection name: nest as a savepoint inside that transaction.
            if self.isolation is not None:
                raise TransactionManagementError(
                    "isolation level cannot be set on a nested transaction"
                )
            self._conn = existing
            self._savepoint = existing.new_savepoint()
            await existing.savepoint(self._savepoint)
            return existing
        # No transaction on *this* connection: begin one. A transaction open on
        # a different connection name stays active alongside (a sibling, not a
        # savepoint) — its statements and this block's must not share a
        # connection, or writes would land in the wrong database.
        engine, dialect = _named_connection(self.connection_name)
        isolation = None
        if self.isolation is not None:
            isolation = _normalize_isolation(self.isolation, dialect.name)
        self._conn = TransactionWrapper(
            await engine.begin(isolation), dialect, self.connection_name
        )
        current = _active_tx.get()
        # Copy-on-set: contextvars snapshot the value, so the mapping itself is
        # never mutated in place (sibling tasks keep their own view).
        updated = dict(current) if current else {}
        updated[self.connection_name] = self._conn
        self._token = _active_tx.set(updated)
        return self._conn

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
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

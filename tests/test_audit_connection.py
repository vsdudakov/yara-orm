"""Audit regression tests: connection routing, transaction lifecycle and the
manual-SQL surface.

Covers: per-connection-name transaction pinning (nested transactions on other
connections are independent siblings; routed models are not absorbed by a
foreign transaction), task-cancellation safety around COMMIT (a cancelled
commit must never recycle a mid-transaction connection into the pool),
``connections.get`` rejecting unknown names, ``execute_script`` atomicity on a
single connection, transaction-control error types, savepoint interleaving
detection, engine replacement closing the old pool, the double-quote-aware
script splitter and all-or-nothing ``execute_many``.
"""

import asyncio
import contextlib
import os
import tempfile
from types import SimpleNamespace

import pytest

from yara_orm import Model, YaraOrm, connections, fields, in_transaction
from yara_orm.connection import (
    TransactionWrapper,
    _engine,
    _named_connection,
    _split_sql_statements,
    clear_query_hooks,
    get_dialect,
    get_engine,
    get_executor,
    register_query_hook,
)
from yara_orm.exceptions import (
    ConfigurationError,
    DBConnectionError,
    IntegrityError,
    OperationalError,
    TransactionManagementError,
)

PG_URL = os.environ.get("ORM_TEST_DB", "postgres://localhost/orm_demo")
BACKENDS = os.environ.get("ORM_TEST_BACKENDS", "sqlite,postgres").split(",")

requires_pg = pytest.mark.skipif(
    "postgres" not in BACKENDS, reason="postgres backend not configured"
)


class AcItem(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "ac_item"


class AcStar(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "ac_star"


class AcPlanet(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "ac_planet"


MODELS = [AcItem, AcStar, AcPlanet]


class _PlanetRouter:
    """Route AcPlanet to the 'second' connection; everything else to default."""

    def db_for_read(self, model):
        return "second" if model.__name__ == "AcPlanet" else "default"

    def db_for_write(self, model):
        return self.db_for_read(model)


def _tmp_sqlite_paths(count: int) -> list[str]:
    """Reserve ``count`` temporary sqlite file paths (files not yet created)."""
    paths = []
    for _ in range(count):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(path)
        paths.append(path)
    return paths


def _cleanup_sqlite(paths: list[str]) -> None:
    """Remove sqlite database files and their WAL/SHM sidecars."""
    for path in paths:
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(FileNotFoundError):
                os.remove(path + suffix)


@contextlib.asynccontextmanager
async def _two_sqlite_connections(router=None):
    """Initialise 'default' and 'second' sqlite connections on temp files."""
    paths = _tmp_sqlite_paths(2)
    await YaraOrm.init(f"sqlite://{paths[0]}", router=router)
    await YaraOrm.add_connection("second", f"sqlite://{paths[1]}")
    try:
        yield paths
    finally:
        await YaraOrm.close()
        _cleanup_sqlite(paths)


async def _create_name_table(conn_name: str) -> None:
    """Create a one-column scratch table on the named connection."""
    await connections.get(conn_name).execute("CREATE TABLE ac_t (name TEXT)")


async def _names(conn_name: str) -> list[str]:
    """Return the scratch-table rows on the named connection."""
    rows = await connections.get(conn_name).fetch_rows("SELECT name FROM ac_t ORDER BY name")
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# Finding 1: per-connection-name transaction pinning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nested_transactions_on_different_connections_commit_to_own_dbs():
    """
    GIVEN transactions nested on two different named connections
    WHEN each block inserts through its own connection and commits
    THEN each write lands in its own database (the inner block is a sibling
         transaction, not a savepoint on the outer connection)
    """
    async with _two_sqlite_connections():
        await _create_name_table("default")
        await _create_name_table("second")

        async with in_transaction() as outer:
            await connections.get("default").execute("INSERT INTO ac_t VALUES ($1)", ["d1"])
            async with in_transaction("second") as inner:
                assert inner is not outer  # independent transaction, no savepoint
                await connections.get("second").execute("INSERT INTO ac_t VALUES ($1)", ["s1"])

        assert await _names("default") == ["d1"]
        assert await _names("second") == ["s1"]


@pytest.mark.asyncio
async def test_nested_transaction_on_other_connection_rolls_back_independently():
    """
    GIVEN a failing nested transaction on a different named connection
    WHEN the inner block raises and rolls back
    THEN only the inner connection's write is discarded; the outer commits
    """
    async with _two_sqlite_connections():
        await _create_name_table("default")
        await _create_name_table("second")

        async with in_transaction():
            await connections.get("default").execute("INSERT INTO ac_t VALUES ($1)", ["d1"])
            with pytest.raises(RuntimeError):
                async with in_transaction("second"):
                    await connections.get("second").execute("INSERT INTO ac_t VALUES ($1)", ["s1"])
                    raise RuntimeError("inner boom")

        assert await _names("default") == ["d1"]
        assert await _names("second") == []


@pytest.mark.asyncio
async def test_model_routed_to_other_connection_not_absorbed_by_open_tx():
    """
    GIVEN a model routed to connection 'second' and an open default transaction
    WHEN the routed model writes inside the (later rolled back) transaction
    THEN the routed write runs on its own connection and survives the rollback
    """
    async with _two_sqlite_connections(router=_PlanetRouter()):
        await YaraOrm.generate_schemas(models=[AcStar, AcPlanet])

        with pytest.raises(RuntimeError):
            async with in_transaction():
                await AcStar.create(name="sun")  # default connection: in the tx
                await AcPlanet.create(name="earth")  # 'second': NOT absorbed
                raise RuntimeError("boom")

        assert await AcStar.all().count() == 0  # rolled back with the tx
        assert await AcPlanet.all().count() == 1  # committed on its own conn


@pytest.mark.asyncio
async def test_connections_get_other_name_inside_tx_returns_its_own_pool():
    """
    GIVEN an open transaction on the default connection
    WHEN connections.get('second') is resolved inside the block
    THEN it is the second connection's executor, not the default transaction
    """
    async with _two_sqlite_connections():
        await _create_name_table("second")
        async with in_transaction() as tx:
            second = connections.get("second")
            assert second is not tx
            await second.execute("INSERT INTO ac_t VALUES ($1)", ["s1"])
            assert connections.get("default") is tx
        assert await _names("second") == ["s1"]


@pytest.mark.asyncio
async def test_same_name_nesting_still_uses_savepoints(db):
    """
    GIVEN a nested block naming the same connection as the outer transaction
    WHEN the inner block rolls back
    THEN it acted as a savepoint: the inner write is gone, the outer commits
    """
    async with in_transaction() as outer:
        await AcItem.create(name="keep")
        with pytest.raises(ValueError):
            async with in_transaction("default") as inner:
                assert inner is outer  # same wrapper: a savepoint, not a sibling
                await AcItem.create(name="drop")
                raise ValueError("inner boom")
        assert await AcItem.filter(name="drop").count() == 0
    assert await AcItem.filter(name="keep").count() == 1


# ---------------------------------------------------------------------------
# Finding 2: cancellation around COMMIT must never recycle a dirty connection
# ---------------------------------------------------------------------------


async def _assert_cancelled_commit_leaves_pool_clean(url: str, probe_url: str) -> None:
    """Drive cancel-around-commit on a max_size=1 pool and check consistency.

    A max_size=1 pool guarantees the connection the transaction ran on is the
    one every later pooled query gets. If a cancelled commit recycled it
    mid-transaction, the pooled view would see uncommitted rows that an
    independent probe connection cannot see.
    """
    sep = "&" if "?" in url else "?"
    await YaraOrm.init(f"{url}{sep}max_size=1")
    probe = await _engine.connect(probe_url)
    conn = connections.get()
    try:
        await conn.execute("DROP TABLE IF EXISTS ac_cancel")
        await conn.execute("CREATE TABLE ac_cancel (id INTEGER)")
        for attempt in range(6):
            tx = TransactionWrapper(await get_engine().begin(None))
            await tx.execute("INSERT INTO ac_cancel VALUES ($1)", [attempt])
            # Cancel the commit at whatever await point the timeout lands on
            # (before/after the transaction handle is taken, mid-COMMIT, ...).
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(tx.commit(), timeout=0)
            del tx
            # Whatever the cancellation hit, the pooled connection must agree
            # with an independent connection: the commit either fully happened
            # or fully didn't — never a half-open transaction in the pool.
            pool_view = (await conn.fetch_rows("SELECT count(*) FROM ac_cancel"))[0][0]
            probe_view = (await probe.fetch_rows("SELECT count(*) FROM ac_cancel"))[0][0]
            assert pool_view == probe_view
    finally:
        with contextlib.suppress(Exception):
            await probe.execute("DROP TABLE IF EXISTS ac_cancel")
        await probe.close()
        await YaraOrm.close()


@requires_pg
@pytest.mark.asyncio
async def test_cancelled_commit_never_recycles_dirty_connection_postgres():
    """
    GIVEN a single-connection pool and a transaction whose commit is cancelled
    WHEN the next pooled query runs
    THEN it sees committed state only (never a recycled mid-transaction session)
    """
    await _assert_cancelled_commit_leaves_pool_clean(PG_URL, PG_URL)


@pytest.mark.asyncio
async def test_cancelled_commit_never_recycles_dirty_connection_sqlite():
    """
    GIVEN a single-connection sqlite pool and a cancelled transaction commit
    WHEN the next pooled query runs
    THEN it sees committed state only (never a recycled mid-transaction session)
    """
    [path] = _tmp_sqlite_paths(1)
    try:
        await _assert_cancelled_commit_leaves_pool_clean(f"sqlite://{path}", f"sqlite://{path}")
    finally:
        _cleanup_sqlite([path])


# ---------------------------------------------------------------------------
# Finding 5: unknown connection names must not silently fall back to default
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connections_get_unknown_name_raises(db):
    """
    GIVEN an initialised ORM
    WHEN connections.get is called with an unregistered name
    THEN it raises ConfigurationError instead of running on the default pool
    """
    with pytest.raises(ConfigurationError):
        connections.get("no_such_connection")
    # The default and registered names keep resolving.
    assert (await connections.get("default").fetch_rows("SELECT 1"))[0][0] == 1
    assert (await connections.get().fetch_rows("SELECT 1"))[0][0] == 1


# ---------------------------------------------------------------------------
# Finding 8: execute_script runs on one connection, atomically
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_script_wrapped_in_begin_commit_is_atomic(db):
    """
    GIVEN a script bracketed by explicit BEGIN/COMMIT whose body fails
    WHEN it runs via the pooled execute_script (one pinned connection)
    THEN the open transaction is rolled back — nothing is applied — and the
         connection returns to the pool clean for the next statement
    """
    conn = connections.get()
    with pytest.raises(OperationalError):
        await conn.execute_script(
            "BEGIN; INSERT INTO ac_item (name) VALUES ('a'); "
            "INSERT INTO ac_no_such_table VALUES (1); COMMIT;"
        )
    assert await AcItem.all().count() == 0


@pytest.mark.asyncio
async def test_execute_script_honours_explicit_transaction_control(db):
    """
    GIVEN a script containing explicit BEGIN/COMMIT around its statements
    WHEN it runs via the pooled execute_script
    THEN the whole script executes on one connection and commits together
    """
    conn = connections.get()
    await conn.execute_script(
        "BEGIN; INSERT INTO ac_item (name) VALUES ('a'); "
        "INSERT INTO ac_item (name) VALUES ('b'); COMMIT;"
    )
    assert await AcItem.all().count() == 2


@pytest.mark.asyncio
async def test_execute_script_statements_run_in_autocommit(db):
    """
    GIVEN a script without transaction control whose last statement fails
    WHEN it runs via the pooled execute_script
    THEN earlier statements are already committed (per-statement autocommit,
         matching execute()) — wrap in BEGIN/COMMIT for all-or-nothing
    """
    conn = connections.get()
    with pytest.raises(OperationalError):
        await conn.execute_script(
            "INSERT INTO ac_item (name) VALUES ('a'); INSERT INTO ac_no_such_table VALUES (1);"
        )
    assert await AcItem.all().count() == 1


@pytest.mark.asyncio
async def test_execute_script_handles_quoted_identifiers(db):
    """
    GIVEN a script using a double-quoted identifier containing a semicolon
    WHEN it is split and executed
    THEN the quoted semicolon does not break the statement apart
    """
    conn = connections.get()
    await conn.execute_script(
        'CREATE TABLE "ac;odd" (id INTEGER); INSERT INTO "ac;odd" VALUES (1);'
    )
    try:
        assert (await conn.fetch_rows('SELECT count(*) FROM "ac;odd"'))[0][0] == 1
    finally:
        await conn.execute('DROP TABLE "ac;odd"')


# ---------------------------------------------------------------------------
# Finding 9: transaction-control failures land in the ORM error hierarchy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_after_exit_raises_transaction_management_error(db):
    """
    GIVEN a transaction that already committed via its context manager
    WHEN commit is called again on the wrapper
    THEN it raises TransactionManagementError (not a bare RuntimeError)
    """
    async with in_transaction() as tx:
        await AcItem.create(name="x")
    with pytest.raises(TransactionManagementError):
        await tx.commit()
    with pytest.raises(TransactionManagementError):
        await tx.rollback()


@pytest.mark.asyncio
async def test_release_of_unknown_savepoint_is_operational_error(db):
    """
    GIVEN an open transaction
    WHEN a nonexistent savepoint is released
    THEN the driver failure surfaces as OperationalError
    """
    with pytest.raises(OperationalError):
        async with in_transaction() as tx:
            with pytest.raises(OperationalError):
                await tx.release("ac_no_such_savepoint")
            # PostgreSQL aborts the transaction after the failed statement, so
            # let the block roll back cleanly instead of committing.
            raise OperationalError("unwind")


@pytest.mark.asyncio
async def test_unreachable_database_raises_db_connection_error():
    """
    GIVEN a postgres URL pointing at a closed port
    WHEN the ORM connects
    THEN it raises DBConnectionError (an OperationalError subclass)
    """
    with pytest.raises(DBConnectionError):
        await YaraOrm.init("postgres://127.0.0.1:1/nope")
    assert issubclass(DBConnectionError, OperationalError)


# ---------------------------------------------------------------------------
# Finding 10: interleaved savepoints from concurrent tasks are detected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_tasks_sharing_transaction_detected(db):
    """
    GIVEN two concurrent tasks opening nested blocks inside one transaction
    WHEN their savepoints interleave (release out of LIFO order)
    THEN a TransactionManagementError explains the unsupported usage
    """
    a_entered = asyncio.Event()
    b_entered = asyncio.Event()

    async def task_a() -> None:
        async with in_transaction():  # savepoint 1
            a_entered.set()
            await b_entered.wait()
        # exiting here releases savepoint 1 while savepoint 2 is still open

    async def task_b() -> None:
        await a_entered.wait()
        async with in_transaction():  # savepoint 2
            b_entered.set()
            await asyncio.sleep(0.05)  # keep it open while task_a exits

    async with in_transaction():
        results = await asyncio.gather(task_a(), task_b(), return_exceptions=True)
    assert any(isinstance(r, TransactionManagementError) for r in results)


# ---------------------------------------------------------------------------
# Finding 13: replacing an engine closes the old pool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_double_init_closes_previous_engine():
    """
    GIVEN an initialised ORM re-initialised without close()
    WHEN the old engine is used afterwards
    THEN it is closed (no leaked pool); the new engine works
    """
    paths = _tmp_sqlite_paths(2)
    await YaraOrm.init(f"sqlite://{paths[0]}")
    old = get_engine()
    await YaraOrm.init(f"sqlite://{paths[1]}")
    try:
        assert (await connections.get().fetch_rows("SELECT 1"))[0][0] == 1
        with pytest.raises(OperationalError):
            await old.execute("SELECT 1")
    finally:
        await YaraOrm.close()
        _cleanup_sqlite(paths)


@pytest.mark.asyncio
async def test_add_connection_reusing_name_closes_previous_engine():
    """
    GIVEN a named connection registered twice
    WHEN the name is re-registered
    THEN the first engine's pool is closed, not leaked
    """
    paths = _tmp_sqlite_paths(3)
    await YaraOrm.init(f"sqlite://{paths[0]}")
    await YaraOrm.add_connection("dup", f"sqlite://{paths[1]}")
    old = _named_connection("dup")[0]
    await YaraOrm.add_connection("dup", f"sqlite://{paths[2]}")
    try:
        assert (await connections.get("dup").fetch_rows("SELECT 1"))[0][0] == 1
        with pytest.raises(OperationalError):
            await old.execute("SELECT 1")
    finally:
        await YaraOrm.close()
        _cleanup_sqlite(paths)


# ---------------------------------------------------------------------------
# Finding 14: the script splitter honours double-quoted identifiers
# ---------------------------------------------------------------------------


def test_split_sql_handles_double_quoted_identifiers():
    """
    GIVEN SQL with semicolons inside double-quoted identifiers
    WHEN the script is split into statements
    THEN quoted semicolons (incl. "" escapes) do not terminate a statement
    """
    stmts = _split_sql_statements('SELECT 1 FROM "a;b"; SELECT \'x;y\' FROM "c""d;e";')
    assert stmts == ['SELECT 1 FROM "a;b"', 'SELECT \'x;y\' FROM "c""d;e"']


# ---------------------------------------------------------------------------
# Finding 12: execute_many is all-or-nothing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_many_applies_nothing_on_failure(db):
    """
    GIVEN a batch whose second row violates the primary key
    WHEN execute_many runs it
    THEN the whole batch rolls back — the first row is not applied
    """
    engine = get_engine()
    ph = ("?", "?") if db == "mysql" else ("$1", "$2")
    with pytest.raises(IntegrityError):
        await engine.execute_many(
            f"INSERT INTO ac_item (id, name) VALUES ({ph[0]}, {ph[1]})",
            [[1, "a"], [1, "b"]],
        )
    assert await AcItem.all().count() == 0


# ---------------------------------------------------------------------------
# Coverage: transaction-wrapper execute_script splits and runs each statement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transaction_execute_script_runs_each_statement(db):
    """
    GIVEN a multi-statement script
    WHEN it runs via execute_script on an open transaction wrapper
    THEN each statement executes on the transaction and shares its atomicity
    """
    with pytest.raises(OperationalError):
        async with in_transaction() as tx:
            await tx.execute_script(
                "INSERT INTO ac_item (name) VALUES ('scr-1');"
                "INSERT INTO ac_item (name) VALUES ('scr-2')"
            )
            assert await AcItem.filter(name__startswith="scr-").count() == 2
            raise OperationalError("unwind")
    assert await AcItem.filter(name__startswith="scr-").count() == 0


# ---------------------------------------------------------------------------
# Coverage: user-named savepoints bypass the ORM's LIFO savepoint stack
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_named_savepoint_establish_and_release(db):
    """
    GIVEN a manually named savepoint (not handed out by new_savepoint)
    WHEN it is established and then released on the transaction wrapper
    THEN both calls succeed and the ORM's own-savepoint stack stays empty
    """
    async with in_transaction() as tx:
        await tx.savepoint("ac_user_sp")
        await AcItem.create(name="sp-row")
        await tx.release("ac_user_sp")
        assert tx._sp_stack == []
    assert await AcItem.filter(name="sp-row").count() == 1


# ---------------------------------------------------------------------------
# Coverage: routed reads use the replica pool when no transaction is open
# ---------------------------------------------------------------------------


class _ReplicaRouter:
    """Routes every read to the 'second' connection, writes to default."""

    def db_for_read(self, model):
        return "second"

    def db_for_write(self, model):
        return "default"


@pytest.mark.asyncio
async def test_routed_read_uses_replica_pool_without_open_transaction():
    """
    GIVEN a read/write-splitting router with no transaction open anywhere
    WHEN the executor and dialect resolve for a read
    THEN both resolve to the replica connection (nothing to capture on the
         write connection, so no read-your-own-writes redirect)
    """
    async with _two_sqlite_connections(router=_ReplicaRouter()):
        engine, dialect = _named_connection("second")
        assert get_executor(AcItem, write=False) is engine
        assert get_dialect(AcItem) is dialect


# ---------------------------------------------------------------------------
# Coverage: get_dialect falls back to routing for opaque executor objects
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_dialect_with_opaque_executor_falls_back_to_routing(db):
    """
    GIVEN a using_db-style executor object exposing no usable dialect
    WHEN the dialect is resolved
    THEN resolution falls back to the model-routed connection's dialect
    """
    default_dialect = get_dialect(AcItem)
    assert get_dialect(AcItem, using=SimpleNamespace(dialect=None)) is default_dialect


# ---------------------------------------------------------------------------
# Coverage: an empty script through the hook proxy is a no-op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_engine_proxy_empty_script_is_a_noop(db):
    """
    GIVEN a registered query hook (statements route through the engine proxy)
    WHEN an empty script is executed
    THEN nothing runs and no hook fires
    """
    seen = []
    register_query_hook(lambda sql, params: seen.append(sql))
    try:
        await get_executor().execute_script("")
        assert seen == []
    finally:
        clear_query_hooks()

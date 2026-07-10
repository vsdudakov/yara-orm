"""SQLite opt-in synchronous fast path (``sqlite://...?sync_fast_path=1``).

With the flag set, engine statement calls run the SQLite work synchronously on
the calling thread (GIL released) and return an already-completed awaitable, so
``await`` resumes immediately instead of round-tripping the event loop. These
tests assert the semantics are indistinguishable from the default async path:
same results, same transactions/savepoints, same exceptions — plus the flag
parsing and the postgres-URL guard.
"""

import asyncio
import contextlib
import os
import tempfile
import threading

import pytest
import pytest_asyncio

from yara_orm import Model, YaraOrm, fields, in_transaction
from yara_orm._engine import connect as engine_connect
from yara_orm.connection import get_engine
from yara_orm.exceptions import IntegrityError, OperationalError


class SPAuthor(Model):
    name = fields.CharField(max_length=50, unique=True)

    class Meta:
        table = "sp_author"


class SPBook(Model):
    title = fields.CharField(max_length=50)
    author = fields.ForeignKeyField("SPAuthor", related_name="books")

    class Meta:
        table = "sp_book"


MODELS = [SPAuthor, SPBook]


@pytest_asyncio.fixture
async def sync_orm():
    """Fresh temporary SQLite database opened with ``sync_fast_path=1``."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    await YaraOrm.init(f"sqlite://{path}?sync_fast_path=1")
    await YaraOrm.generate_schemas(models=MODELS)
    try:
        yield
    finally:
        await YaraOrm.close()
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(FileNotFoundError):
                os.remove(path + suffix)


@pytest.mark.asyncio
async def test_crud_round_trip(sync_orm):
    """
    GIVEN an ORM initialised with sync_fast_path=1
    WHEN a row is created, fetched, updated and deleted
    THEN every step behaves exactly like the default async path
    """
    author = await SPAuthor.create(name="Ada")
    fetched = await SPAuthor.get(id=author.id)
    assert fetched.name == "Ada"

    fetched.name = "Ada L."
    await fetched.save()
    assert (await SPAuthor.get(id=author.id)).name == "Ada L."

    await fetched.delete()
    assert await SPAuthor.filter(id=author.id).first() is None


@pytest.mark.asyncio
async def test_transaction_commit_and_rollback(sync_orm):
    """
    GIVEN the fast path is active
    WHEN one transaction commits and another rolls back on error
    THEN only the committed transaction's rows persist
    """
    async with in_transaction():
        await SPAuthor.create(name="kept")

    with pytest.raises(RuntimeError):
        async with in_transaction():
            await SPAuthor.create(name="discarded")
            raise RuntimeError("boom")

    names = {a.name for a in await SPAuthor.all()}
    assert names == {"kept"}


@pytest.mark.asyncio
async def test_nested_savepoints(sync_orm):
    """
    GIVEN a transaction with nested in_transaction blocks (savepoints)
    WHEN an inner block fails and a sibling inner block succeeds
    THEN only the failed block's work is rolled back
    """
    async with in_transaction():
        await SPAuthor.create(name="outer")
        with pytest.raises(RuntimeError):
            async with in_transaction():  # savepoint, rolled back
                await SPAuthor.create(name="inner-fail")
                raise RuntimeError("inner boom")
        async with in_transaction():  # savepoint, released
            await SPAuthor.create(name="inner-ok")

    names = {a.name for a in await SPAuthor.all()}
    assert names == {"outer", "inner-ok"}


@pytest.mark.asyncio
async def test_bulk_create(sync_orm):
    """
    GIVEN the fast path is active
    WHEN rows are inserted with bulk_create (the execute_many path)
    THEN all rows land with primary keys assigned
    """
    authors = await SPAuthor.bulk_create([SPAuthor(name=f"a{i}") for i in range(20)])
    assert len(authors) == 20
    assert all(a.id is not None for a in authors)
    assert await SPAuthor.all().count() == 20


@pytest.mark.asyncio
async def test_select_related_fetch(sync_orm):
    """
    GIVEN books referencing authors
    WHEN fetched with select_related (a join hydrating both models)
    THEN the related author is populated without further queries
    """
    ada = await SPAuthor.create(name="Ada")
    await SPBook.create(title="Notes", author=ada)

    books = await SPBook.all().select_related("author")
    assert len(books) == 1
    assert books[0].author.name == "Ada"


@pytest.mark.asyncio
async def test_concurrent_tasks_interleave_correctly(sync_orm):
    """
    GIVEN two concurrent asyncio tasks doing interleaved writes and reads
    WHEN they run under the fast path (awaits may not yield to the loop)
    THEN both tasks' rows are all persisted and reads see consistent counts
    """

    async def writer(prefix: str, n: int) -> None:
        for i in range(n):
            await SPAuthor.create(name=f"{prefix}{i}")
            # Force a real scheduling point so the tasks genuinely interleave
            # (a completed awaitable resumes without yielding to the loop).
            await asyncio.sleep(0)
            assert await SPAuthor.filter(name=f"{prefix}{i}").count() == 1

    await asyncio.gather(writer("x", 10), writer("y", 10))
    assert await SPAuthor.all().count() == 20


@pytest.mark.asyncio
async def test_integrity_error_surfaces_identically(sync_orm):
    """
    GIVEN a unique constraint on the name column
    WHEN a duplicate row is inserted under the fast path
    THEN IntegrityError is raised at the await, like the async path
    """
    await SPAuthor.create(name="dup")
    with pytest.raises(IntegrityError):
        await SPAuthor.create(name="dup")


@pytest.mark.asyncio
async def test_errors_are_deferred_to_the_await(sync_orm):
    """
    GIVEN a raw engine call against a missing table
    WHEN the statement method is called on the fast path
    THEN the call itself does not raise; the error surfaces on await as an
         OperationalError (query failures route through the ORM hierarchy)
    """
    engine = get_engine()
    awaitable = engine.fetch_rows("SELECT * FROM sp_missing")  # no raise here
    with pytest.raises(OperationalError, match="sp_missing"):
        await awaitable


@pytest.mark.asyncio
async def test_completed_awaitable_protocol(sync_orm):
    """
    GIVEN an already-completed awaitable from a fast-path engine call
    WHEN it is awaited, wrapped in ensure_future, and awaited twice
    THEN it resolves normally, works as a task, and rejects reuse
    """
    engine = get_engine()
    awaitable = engine.fetch_rows("SELECT 42")
    assert await awaitable == [[42]]

    # asyncio wraps generic awaitables fine (gather/ensure_future).
    task = asyncio.ensure_future(engine.fetch_rows("SELECT 7"))
    assert await task == [[7]]

    # Like a coroutine, a completed awaitable is single-use.
    with pytest.raises(RuntimeError, match="already awaited"):
        await awaitable


@pytest.mark.asyncio
async def test_manual_sql_error_translation(sync_orm):
    """
    GIVEN the raw-SQL executor surface (connections.get / _EngineProxy)
    WHEN a bad statement runs under the fast path
    THEN it surfaces as OperationalError, matching the async path
    """
    conn = YaraOrm.get_connection()
    with pytest.raises(OperationalError):
        await conn.execute("SELECT * FROM sp_missing")


@pytest.mark.asyncio
async def test_flag_values_zero_and_off_keep_the_async_path():
    """
    GIVEN sync_fast_path=0 and sync_fast_path=off URLs
    WHEN an engine connects with each
    THEN the URL is accepted and statements still work (default path)
    """
    for value in ("0", "off"):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(path)
        engine = await engine_connect(f"sqlite://{path}?sync_fast_path={value}")
        try:
            assert await engine.fetch_rows("SELECT 1") == [[1]]
        finally:
            await engine.close()
            for suffix in ("", "-wal", "-shm"):
                with contextlib.suppress(FileNotFoundError):
                    os.remove(path + suffix)


@pytest.mark.asyncio
async def test_invalid_flag_values_are_rejected():
    """
    GIVEN a sync_fast_path value outside 1/0/off
    WHEN connecting
    THEN a ValueError names the parameter (matching other URL-param errors)
    """
    for bad in ("true", "yes", "2", ""):
        with pytest.raises(ValueError, match="sync_fast_path"):
            await engine_connect(f"sqlite://x.db?sync_fast_path={bad}")


@pytest.mark.asyncio
async def test_unknown_param_error_lists_sync_fast_path():
    """
    GIVEN a typo'd sqlite URL parameter
    WHEN connecting
    THEN the error's supported-parameter list mentions sync_fast_path
    """
    with pytest.raises(ValueError, match="sync_fast_path"):
        await engine_connect("sqlite://x.db?sync_fastpath=1")


@pytest.mark.asyncio
async def test_postgres_urls_reject_the_flag():
    """
    GIVEN a postgres URL carrying sync_fast_path
    WHEN connecting
    THEN it is rejected as SQLite-only before any connection is attempted
    """
    with pytest.raises(ValueError, match="SQLite-only"):
        await engine_connect("postgres://localhost:1/nope?sync_fast_path=1")


# ---------------------------------------------------------------------------
# Pool-exhaustion fallback: no event-loop deadlock while a transaction holds
# the pool's only connection.
# ---------------------------------------------------------------------------
class RfaItem(Model):
    name = fields.CharField(max_length=50)

    class Meta:
        table = "rfa_item"


def test_sync_fast_path_query_during_open_transaction_does_not_deadlock():
    """
    GIVEN sync_fast_path=1 on an in-memory database (the pool pins exactly one
        connection)
    WHEN task A holds an open transaction and task B runs a plain query on the
        same event loop
    THEN task B falls back to the async path and completes after A commits,
        instead of block_on-ing the event-loop thread into a permanent
        deadlock

    The scenario runs on its own thread: a regression blocks that loop's
    thread outright (asyncio.wait_for could never fire there), so the guard is
    a bounded join that fails the test rather than hanging the suite.
    """
    result: dict = {}

    async def scenario():
        await YaraOrm.init("sqlite://:memory:?sync_fast_path=1")
        try:
            await YaraOrm.generate_schemas(models=[RfaItem])
            in_tx = asyncio.Event()

            async def holder():
                async with in_transaction():
                    await RfaItem.create(name="pinned")
                    in_tx.set()
                    # Keep the transaction (and the only connection) open while
                    # the concurrent query runs; this sleep only ever finishes
                    # if the event loop stays responsive.
                    await asyncio.sleep(0.3)

            async def prober():
                await in_tx.wait()
                # Plain query with no free connection: must not block the
                # loop; it completes once the holder commits.
                return await RfaItem.all().count()

            _, count = await asyncio.wait_for(asyncio.gather(holder(), prober()), timeout=15)
            result["count"] = count
        finally:
            await YaraOrm.close()

    def run() -> None:
        try:
            asyncio.run(scenario())
        except BaseException as exc:  # noqa: BLE001 - re-raised on the main thread
            result["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout=30)
    if thread.is_alive():
        pytest.fail(
            "deadlock: the sync fast path blocked the event loop while a "
            "transaction pinned the pool's only connection"
        )
    if "error" in result:
        raise result["error"]
    assert result["count"] == 1

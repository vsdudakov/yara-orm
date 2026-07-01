"""Corner-case concurrency tests: get_or_create fan-out on both branches,
concurrent bulk_create, contextvar-scoped transaction isolation (a tx pinned in
one asyncio task must not bleed into a sibling task), and a sibling-task write
that is unaffected by another task's open-then-rolled-back transaction.

These run on both backends via the ``db`` fixture. The pure connection-pool
saturation behaviour (multiple physical connections) is PostgreSQL-only and is
skipped on SQLite, which has a single connection.
"""

import asyncio

import pytest

from yara_orm import Model, fields, in_transaction
from yara_orm.connection import _active_tx
from yara_orm.transactions import atomic


class MtCnRow(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50, unique=True)
    n = fields.IntField(default=0)

    class Meta:
        table = "mt_cn_row"


MODELS = [MtCnRow]


@pytest.mark.asyncio
async def test_get_or_create_concurrent_all_created_distinct(db):
    """
    GIVEN 20 get_or_create calls for distinct keys issued concurrently
    WHEN they run together
    THEN each reports created=True and every distinct row lands exactly once
    """
    results = await asyncio.gather(*(MtCnRow.get_or_create(name=f"new-{i}") for i in range(20)))
    assert all(created for _obj, created in results)
    assert await MtCnRow.all().count() == 20
    assert {r.name for r in await MtCnRow.all()} == {f"new-{i}" for i in range(20)}


@pytest.mark.asyncio
async def test_get_or_create_concurrent_all_existing(db):
    """
    GIVEN a pre-seeded row and 20 concurrent get_or_create for the same key
    WHEN they all run together
    THEN each reports created=False and no duplicate row is inserted
    """
    seed, made = await MtCnRow.get_or_create(name="shared", defaults={"n": 1})
    assert made is True
    results = await asyncio.gather(
        *(MtCnRow.get_or_create(name="shared", defaults={"n": 99}) for _ in range(20))
    )
    assert not any(created for _obj, created in results)
    assert await MtCnRow.filter(name="shared").count() == 1
    # defaults were ignored on the get path (row keeps its seeded value).
    assert (await MtCnRow.get(id=seed.id)).n == 1


@pytest.mark.asyncio
async def test_concurrent_bulk_creates(db):
    """
    GIVEN several bulk_create batches issued concurrently
    WHEN they all run together
    THEN every row from every batch is persisted with no loss or duplication
    """

    async def batch(prefix: str) -> None:
        await MtCnRow.bulk_create([MtCnRow(name=f"{prefix}-{i}") for i in range(10)])

    await asyncio.gather(*(batch(p) for p in ("a", "b", "c")))
    assert await MtCnRow.all().count() == 30


@pytest.mark.asyncio
async def test_concurrent_updates_are_all_applied(db):
    """
    GIVEN many rows updated concurrently, each by its own coroutine
    WHEN the updates run together
    THEN every row reflects its own update (no lost writes across coroutines)
    """
    rows = await asyncio.gather(*(MtCnRow.create(name=f"u-{i}") for i in range(15)))

    async def bump(row: MtCnRow) -> None:
        await MtCnRow.filter(id=row.id).update(n=row.id * 10)

    await asyncio.gather(*(bump(r) for r in rows))
    for r in rows:
        assert (await MtCnRow.get(id=r.id)).n == r.id * 10


@pytest.mark.asyncio
async def test_no_cross_task_transaction_bleed(db):
    """
    GIVEN one task holding an open transaction while a sibling task runs
    WHEN the sibling inspects the active-transaction contextvar
    THEN it sees no transaction (the pin is scoped to the owning task's context)
    """
    tx_open = asyncio.Event()
    sibling_checked = asyncio.Event()
    observed: dict[str, object] = {}

    async def holder() -> None:
        async with in_transaction():
            tx_open.set()
            await sibling_checked.wait()  # keep the tx open while sibling checks

    async def sibling() -> None:
        await tx_open.wait()
        # This task never opened a transaction; its context must be clean even
        # though a sibling task has one active right now.
        observed["active"] = _active_tx.get()
        sibling_checked.set()

    await asyncio.gather(holder(), sibling())
    assert observed["active"] is None


@pytest.mark.asyncio
async def test_sibling_write_survives_other_tasks_rollback(db):
    """
    GIVEN one task whose atomic block writes then rolls back, and a sibling task
          that commits an independent write
    WHEN both run concurrently
    THEN the rolled-back row is gone and the sibling's committed row persists
    """
    doomed_started = asyncio.Event()

    @atomic()
    async def doomed() -> None:
        await MtCnRow.create(name="doomed")
        doomed_started.set()
        raise RuntimeError("rollback me")

    async def keeper() -> None:
        await doomed_started.wait()
        await MtCnRow.create(name="keeper")

    results = await asyncio.gather(doomed(), keeper(), return_exceptions=True)
    assert isinstance(results[0], RuntimeError)
    names = {r.name for r in await MtCnRow.all()}
    assert "keeper" in names
    assert "doomed" not in names


@pytest.mark.asyncio
async def test_pool_saturation_queues_and_completes(db):
    """
    GIVEN more concurrent queries than pooled connections
    WHEN they contend for the pool (PostgreSQL only; SQLite is single-connection)
    THEN checkout queues rather than failing and every query completes
    """
    if db != "postgres":
        pytest.skip("multi-connection pool saturation is PostgreSQL-only")
    from yara_orm.connection import get_engine

    engine = get_engine()

    async def slow() -> int:
        rows = await engine.fetch_rows("SELECT pg_sleep(0.01), 1")
        return rows[0][1]

    results = await asyncio.gather(*(slow() for _ in range(16)))
    assert results == [1] * 16

"""Concurrency and connection-pool behaviour under load.

These exercise paths the existing suite did not: many in-flight queries sharing
a bounded pool, concurrent independent transactions on separate pooled
connections, and pool checkout when more work is queued than the pool can serve
at once. PostgreSQL only — it is the backend with a real multi-connection pool.
"""

import asyncio
import os

import pytest

from yara_orm import Model, YaraOrm, fields
from yara_orm.connection import get_engine
from yara_orm.transactions import atomic

DB_URL = os.environ.get("ORM_TEST_DB", "postgres://localhost/orm_demo")


class CnCounter(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50, unique=True)

    class Meta:
        table = "cn_counter"


async def _reset():
    await get_engine().execute("DROP TABLE IF EXISTS cn_counter CASCADE")
    await YaraOrm.generate_schemas()


@pytest.mark.asyncio
async def test_many_concurrent_inserts(orm):
    """50 concurrent inserts all commit and are individually retrievable."""
    await _reset()
    await asyncio.gather(*(CnCounter.create(name=f"row-{i}") for i in range(50)))
    assert await CnCounter.all().count() == 50
    # No interleaving corruption: every distinct name persisted exactly once.
    names = {c.name for c in await CnCounter.all()}
    assert names == {f"row-{i}" for i in range(50)}


@pytest.mark.asyncio
async def test_concurrent_reads_under_load(orm):
    """Many concurrent SELECTs return consistent results without errors."""
    await _reset()
    await CnCounter.create(name="seed")
    results = await asyncio.gather(
        *(CnCounter.all().count() for _ in range(40)),
    )
    assert results == [1] * 40


@pytest.mark.asyncio
async def test_concurrent_independent_transactions(orm):
    """Parallel atomic blocks each commit on their own pinned connection."""
    await _reset()

    @atomic()
    async def insert(name: str) -> None:
        await CnCounter.create(name=name)

    await asyncio.gather(*(insert(f"tx-{i}") for i in range(20)))
    assert await CnCounter.all().count() == 20


@pytest.mark.asyncio
async def test_one_transaction_rollback_does_not_affect_others(orm):
    """A rollback in one concurrent transaction leaves the others intact."""
    await _reset()

    @atomic()
    async def ok(name: str) -> None:
        await CnCounter.create(name=name)

    @atomic()
    async def boom() -> None:
        await CnCounter.create(name="doomed")
        raise RuntimeError("forced rollback")

    results = await asyncio.gather(ok("keep-1"), boom(), ok("keep-2"), return_exceptions=True)
    assert isinstance(results[1], RuntimeError)
    names = {c.name for c in await CnCounter.all()}
    assert names == {"keep-1", "keep-2"}  # "doomed" rolled back


@pytest.mark.asyncio
async def test_pool_queues_more_work_than_connections():
    """With a 2-connection pool, 12 concurrent queries still all complete.

    Verifies checkout queues rather than failing when demand exceeds pool size.
    """
    await YaraOrm.init(f"{DB_URL}?max_size=2")
    try:
        engine = get_engine()

        # Each query holds its connection for ~50ms; 12 of them through 2 slots
        # forces the pool to queue and recycle connections.
        async def slow():
            rows = await engine.fetch_rows("SELECT pg_sleep(0.05), 1")
            return rows[0][1]

        results = await asyncio.gather(*(slow() for _ in range(12)))
        assert results == [1] * 12
    finally:
        await YaraOrm.close()

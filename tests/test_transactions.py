"""Transactions: in_transaction, atomic and rollback."""

import pytest

from yara_orm import Model, YaraOrm, atomic, fields, in_transaction
from yara_orm.connection import get_engine


class TxAccount(Model):
    name = fields.CharField(max_length=100)
    balance = fields.IntField(default=0)

    class Meta:
        table = "t_account"


async def _reset():
    engine = get_engine()
    await engine.execute("DROP TABLE IF EXISTS t_account CASCADE")
    await YaraOrm.generate_schemas()


@pytest.mark.asyncio
async def test_commit_persists(orm):
    """
    GIVEN an in_transaction block
    WHEN rows are created and the block exits cleanly
    THEN the changes are committed and visible afterwards
    """
    await _reset()
    async with in_transaction():
        await TxAccount.create(name="A", balance=100)
        await TxAccount.create(name="B", balance=50)
    assert await TxAccount.all().count() == 2


@pytest.mark.asyncio
async def test_rollback_on_error(orm):
    """
    GIVEN an in_transaction block that raises after a write
    WHEN the exception propagates out of the block
    THEN every change in the block is rolled back
    """
    await _reset()
    with pytest.raises(RuntimeError):
        async with in_transaction():
            await TxAccount.create(name="A", balance=100)
            raise RuntimeError("boom")
    assert await TxAccount.all().count() == 0


@pytest.mark.asyncio
async def test_transaction_isolation_atomic_update(orm):
    """
    GIVEN two balances modified together inside a transaction
    WHEN the block commits
    THEN both updates are applied atomically
    """
    await _reset()
    a = await TxAccount.create(name="A", balance=100)
    b = await TxAccount.create(name="B", balance=0)
    async with in_transaction():
        await TxAccount.filter(id=a.id).update(balance=70)
        await TxAccount.filter(id=b.id).update(balance=30)
    assert (await TxAccount.get(id=a.id)).balance == 70
    assert (await TxAccount.get(id=b.id)).balance == 30


@pytest.mark.asyncio
async def test_atomic_decorator_rolls_back(orm):
    """
    GIVEN a coroutine wrapped with @atomic()
    WHEN it raises after writing
    THEN the decorator rolls the transaction back
    """
    await _reset()

    @atomic()
    async def transfer():
        await TxAccount.create(name="X", balance=1)
        raise ValueError("nope")

    with pytest.raises(ValueError):
        await transfer()
    assert await TxAccount.all().count() == 0


@pytest.mark.asyncio
async def test_reads_inside_transaction_see_writes(orm):
    """
    GIVEN a write performed earlier in a transaction
    WHEN a subsequent read runs in the same transaction
    THEN the read observes the uncommitted write
    """
    await _reset()
    async with in_transaction():
        await TxAccount.create(name="Z", balance=5)
        found = await TxAccount.get(name="Z")
        assert found.balance == 5

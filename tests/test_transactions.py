"""Transactions: in_transaction, atomic, rollback, nested savepoints and
isolation levels."""

import pytest

from yara_orm import IsolationLevel, Model, YaraOrm, atomic, fields, in_transaction
from yara_orm.connection import get_engine
from yara_orm.exceptions import (
    ConfigurationError,
    TransactionManagementError,
    UnSupportedError,
)


class TxAccount(Model):
    name = fields.CharField(max_length=100)
    balance = fields.IntField(default=0)

    class Meta:
        table = "t_account"


#: Used by the cross-backend ``db`` fixture for the nesting/isolation tests.
MODELS = [TxAccount]


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


# -- nested savepoints ------------------------------------------------------
@pytest.mark.asyncio
async def test_nested_savepoint_inner_rollback(db):
    """
    GIVEN a nested in_transaction (savepoint) that raises
    WHEN the inner block rolls back but the outer block continues and commits
    THEN the inner write is discarded and the outer write persists
    """
    async with in_transaction():
        await TxAccount.create(name="A", balance=10)
        with pytest.raises(RuntimeError):
            async with in_transaction():  # savepoint
                await TxAccount.create(name="B", balance=20)
                raise RuntimeError("inner boom")
        # The savepoint rolled back B; A is still pending in the outer tx.
        assert await TxAccount.filter(name="B").exists() is False
        assert await TxAccount.filter(name="A").exists() is True
    assert {a.name for a in await TxAccount.all()} == {"A"}


@pytest.mark.asyncio
async def test_nested_savepoint_inner_commit(db):
    """
    GIVEN a nested in_transaction that exits cleanly
    WHEN the outer block commits
    THEN both the outer and inner writes persist
    """
    async with in_transaction():
        await TxAccount.create(name="A", balance=1)
        async with in_transaction():  # savepoint released on clean exit
            await TxAccount.create(name="B", balance=2)
    assert {a.name for a in await TxAccount.all()} == {"A", "B"}


@pytest.mark.asyncio
async def test_outer_rollback_discards_released_savepoint(db):
    """
    GIVEN a nested savepoint released into the outer transaction
    WHEN the outer transaction later rolls back
    THEN every write — outer and the released inner — is discarded
    """
    with pytest.raises(RuntimeError):
        async with in_transaction():
            await TxAccount.create(name="A", balance=1)
            async with in_transaction():
                await TxAccount.create(name="B", balance=2)
            raise RuntimeError("outer boom")
    assert await TxAccount.all().count() == 0


@pytest.mark.asyncio
async def test_deeply_nested_partial_rollback(db):
    """
    GIVEN three levels of nesting where only the deepest rolls back
    WHEN the outer transaction commits
    THEN the two outer levels persist and only the deepest write is discarded
    """
    async with in_transaction():
        await TxAccount.create(name="L1", balance=1)
        async with in_transaction():
            await TxAccount.create(name="L2", balance=2)
            with pytest.raises(ValueError):
                async with in_transaction():
                    await TxAccount.create(name="L3", balance=3)
                    raise ValueError("deepest boom")
    assert {a.name for a in await TxAccount.all()} == {"L1", "L2"}


# -- isolation levels -------------------------------------------------------
@pytest.mark.asyncio
async def test_isolation_serializable_both_backends(db):
    """
    GIVEN a transaction requesting SERIALIZABLE isolation
    WHEN it runs on either backend (both support SERIALIZABLE)
    THEN the work commits normally
    """
    async with in_transaction(isolation=IsolationLevel.SERIALIZABLE):
        await TxAccount.create(name="S", balance=1)
    assert await TxAccount.all().count() == 1


@pytest.mark.asyncio
async def test_isolation_repeatable_read_per_backend(db):
    """
    GIVEN a transaction requesting REPEATABLE READ isolation
    WHEN it runs on PostgreSQL/MySQL (supported) versus SQLite
    (serializable-only)
    THEN PostgreSQL and MySQL apply it and SQLite raises UnSupportedError
    """
    if db in ("postgres", "mysql"):
        async with in_transaction(isolation=IsolationLevel.REPEATABLE_READ):
            await TxAccount.create(name="R", balance=1)
        assert await TxAccount.all().count() == 1
    else:
        with pytest.raises(UnSupportedError):
            async with in_transaction(isolation=IsolationLevel.REPEATABLE_READ):
                pass


@pytest.mark.asyncio
async def test_unknown_isolation_level_raises(db):
    """
    GIVEN an unrecognised isolation level
    WHEN a transaction is opened with it
    THEN a ConfigurationError is raised
    """
    with pytest.raises(ConfigurationError):
        async with in_transaction(isolation="TURBO"):
            pass


@pytest.mark.asyncio
async def test_isolation_rejected_on_nested(db):
    """
    GIVEN an active transaction
    WHEN a nested block requests an isolation level
    THEN a TransactionManagementError is raised (it can only be set at BEGIN)
    """
    async with in_transaction():
        with pytest.raises(TransactionManagementError):
            async with in_transaction(isolation=IsolationLevel.SERIALIZABLE):
                pass


@pytest.mark.asyncio
async def test_atomic_decorator_with_isolation(db):
    """
    GIVEN an @atomic decorator carrying an isolation level
    WHEN the wrapped coroutine runs
    THEN it commits inside a transaction at that isolation level
    """

    @atomic(isolation=IsolationLevel.SERIALIZABLE)
    async def seed():
        await TxAccount.create(name="D", balance=1)

    await seed()
    assert await TxAccount.all().count() == 1

"""Corner-case transaction tests: read-your-writes rollback, @atomic return
value, deeply nested savepoints with a middle-level rollback, contextvar
cleanup after an exception, m2m writes rolling back with the tx, and
select_for_update SQL emission (no-op on SQLite, clauses on PostgreSQL).

Uses the cross-backend ``db`` fixture; table names are prefixed ``mt_`` to
avoid collisions with the rest of the suite.
"""

import pytest

from yara_orm import IsolationLevel, Model, fields, in_transaction
from yara_orm.connection import _active_tx, connections
from yara_orm.exceptions import TransactionManagementError
from yara_orm.transactions import atomic


class MtTxAcct(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50, unique=True)
    balance = fields.IntField(default=0)

    class Meta:
        table = "mt_tx_acct"


class MtTxTag(Model):
    id = fields.IntField(pk=True)
    label = fields.CharField(max_length=50)

    class Meta:
        table = "mt_tx_tag"


class MtTxDoc(Model):
    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=50)
    tags = fields.ManyToManyField("MtTxTag", through="mt_tx_doc_tag")

    class Meta:
        table = "mt_tx_doc"


MODELS = [MtTxAcct, MtTxTag, MtTxDoc]


@pytest.mark.asyncio
async def test_update_inside_tx_rolls_back(db):
    """
    GIVEN a committed row updated inside a transaction that then raises
    WHEN the block rolls back
    THEN the update is discarded and the row keeps its original value
    """
    a = await MtTxAcct.create(name="A", balance=100)
    with pytest.raises(RuntimeError):
        async with in_transaction():
            await MtTxAcct.filter(id=a.id).update(balance=999)
            # read-your-writes: the change is visible inside the tx
            assert (await MtTxAcct.get(id=a.id)).balance == 999
            raise RuntimeError("boom")
    assert (await MtTxAcct.get(id=a.id)).balance == 100


@pytest.mark.asyncio
async def test_atomic_returns_wrapped_value(db):
    """
    GIVEN an @atomic-wrapped coroutine that returns a value
    WHEN it commits cleanly
    THEN the decorator forwards the return value to the caller
    """

    @atomic()
    async def make() -> int:
        acct = await MtTxAcct.create(name="ret", balance=7)
        return acct.id

    new_id = await make()
    assert isinstance(new_id, int)
    assert (await MtTxAcct.get(id=new_id)).balance == 7


@pytest.mark.asyncio
async def test_middle_savepoint_rollback_keeps_outer_and_inner(db):
    """
    GIVEN three nesting levels where the *middle* level rolls back
    WHEN the outer commits
    THEN only the middle level's write (and anything under it) is discarded
    """
    async with in_transaction():
        await MtTxAcct.create(name="L1", balance=1)
        with pytest.raises(ValueError):
            async with in_transaction():  # middle savepoint
                await MtTxAcct.create(name="L2", balance=2)
                async with in_transaction():  # deepest, commits into middle
                    await MtTxAcct.create(name="L3", balance=3)
                raise ValueError("middle boom")  # unwinds L2 and L3
    assert {a.name for a in await MtTxAcct.all()} == {"L1"}


@pytest.mark.asyncio
async def test_active_tx_contextvar_reset_after_exception(db):
    """
    GIVEN a transaction that raises out of its block
    WHEN the block exits (rolling back)
    THEN the active-transaction contextvar is cleared for subsequent work
    """
    assert _active_tx.get() is None
    with pytest.raises(RuntimeError):
        async with in_transaction():
            assert _active_tx.get() is not None  # pinned while active
            raise RuntimeError("boom")
    # After a failed tx the pin is released and plain work runs uncontained.
    assert _active_tx.get() is None
    await MtTxAcct.create(name="after", balance=0)
    assert await MtTxAcct.filter(name="after").exists() is True


@pytest.mark.asyncio
async def test_connections_get_switches_to_tx_inside_block(db):
    """
    GIVEN the active executor resolved via connections.get()
    WHEN queried outside vs inside a transaction block
    THEN inside the block it resolves to the pinned transaction wrapper
    """
    outside = connections.get()
    async with in_transaction() as tx:
        inside = connections.get()
        assert inside is tx
    assert connections.get() is not tx
    # Sanity: outside resolver is not the tx wrapper.
    assert outside is not tx


@pytest.mark.asyncio
async def test_m2m_add_rolls_back_with_transaction(db):
    """
    GIVEN m2m links created inside a transaction that then raises
    WHEN the block rolls back
    THEN the join-table rows are discarded along with the transaction
    """
    doc = await MtTxDoc.create(title="doc")
    t1 = await MtTxTag.create(label="t1")
    t2 = await MtTxTag.create(label="t2")

    with pytest.raises(RuntimeError):
        async with in_transaction():
            await doc.tags.add(t1, t2)
            assert await doc.tags.all().count() == 2  # visible inside the tx
            raise RuntimeError("boom")
    assert await doc.tags.all().count() == 0


@pytest.mark.asyncio
async def test_m2m_add_commits_with_transaction(db):
    """
    GIVEN m2m links created inside a transaction that commits
    WHEN the block exits cleanly
    THEN the join-table rows persist
    """
    doc = await MtTxDoc.create(title="doc2")
    t1 = await MtTxTag.create(label="c1")
    async with in_transaction():
        await doc.tags.add(t1)
    assert await doc.tags.all().count() == 1


@pytest.mark.asyncio
async def test_select_for_update_sql_per_backend(db):
    """
    GIVEN a select_for_update() query with NOWAIT / SKIP LOCKED / OF variants
    WHEN its SQL is rendered
    THEN PostgreSQL and MySQL emit the FOR UPDATE clauses and SQLite emits
    none (no-op)
    """
    base = MtTxAcct.filter(balance__gt=0)
    if db in ("postgres", "mysql"):
        assert base.select_for_update().sql().rstrip().endswith("FOR UPDATE")
        assert "NOWAIT" in base.select_for_update(nowait=True).sql()
        assert "SKIP LOCKED" in base.select_for_update(skip_locked=True).sql()
        of_sql = base.select_for_update(of=("mt_tx_acct",)).sql()
        assert "FOR UPDATE OF" in of_sql
        # nowait wins over skip_locked when both are set.
        both = base.select_for_update(nowait=True, skip_locked=True).sql()
        assert "NOWAIT" in both and "SKIP LOCKED" not in both
    else:
        # SQLite: the lock clause is silently dropped.
        assert "FOR UPDATE" not in base.select_for_update(nowait=True).sql()


@pytest.mark.asyncio
async def test_select_for_update_returns_rows_inside_tx(db):
    """
    GIVEN a select_for_update() run inside a transaction
    WHEN the locked rows are fetched
    THEN it returns the matching rows on both backends (a no-op lock on SQLite)
    """
    await MtTxAcct.create(name="lockme", balance=50)
    async with in_transaction():
        rows = await MtTxAcct.filter(name="lockme").select_for_update()
        assert [r.name for r in rows] == ["lockme"]


@pytest.mark.asyncio
async def test_isolation_on_nested_atomic_raises(db):
    """
    GIVEN an active transaction
    WHEN an @atomic(isolation=...) coroutine runs nested inside it
    THEN entering the nested block raises TransactionManagementError
    """

    @atomic(isolation=IsolationLevel.SERIALIZABLE)
    async def inner():
        await MtTxAcct.create(name="never", balance=0)

    async with in_transaction():
        with pytest.raises(TransactionManagementError):
            await inner()
    # The failed nested attempt wrote nothing.
    assert await MtTxAcct.filter(name="never").exists() is False

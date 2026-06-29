"""Correctness regression tests for engine/transaction/binding fixes.

Covers: SQLite foreign-key enforcement + cascade, M2M operations honouring the
active transaction, Coalesce binding its default (no SQL injection / quote
breakage), empty M2M ``__in``, range-checked integer binding on PostgreSQL,
``auto_now`` honouring ``use_tz``, and ``RandomHex`` width honouring ``size``.
"""

import os

import pytest
import pytest_asyncio

from yara_orm import (
    Model,
    RandomHex,
    YaraOrm,
    fields,
    in_transaction,
)
from yara_orm.exceptions import IntegrityError
from yara_orm.functions import Coalesce


class A4Country(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20)

    class Meta:
        table = "a4_country"


class A4City(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20, null=True)
    nick = fields.CharField(max_length=20, null=True)
    country = fields.ForeignKeyField("A4Country", related_name="cities")

    class Meta:
        table = "a4_city"


class A4Tag(Model):
    id = fields.IntField(pk=True)
    label = fields.CharField(max_length=20)

    class Meta:
        table = "a4_tag"


class A4Post(Model):
    id = fields.IntField(pk=True)
    tags = fields.ManyToManyField("A4Tag", related_name="posts", through="a4_post_tag")

    class Meta:
        table = "a4_post"


MODELS = [A4Country, A4Tag, A4Post, A4City]


# --- SQLite foreign-key enforcement (was silently off) -----------------------
@pytest.mark.asyncio
async def test_foreign_key_is_enforced(db):
    """
    GIVEN a child row referencing a non-existent parent
    WHEN it is inserted
    THEN the database rejects it (FK enforced on SQLite and PostgreSQL alike)
    """
    with pytest.raises(IntegrityError):
        await A4City.create(name="orphan", country_id=999999)


@pytest.mark.asyncio
async def test_foreign_key_cascade_delete(db):
    """
    GIVEN a parent with children and ON DELETE CASCADE
    WHEN the parent is deleted
    THEN the children are removed too (cascade actually runs on SQLite)
    """
    c = await A4Country.create(name="UK")
    await A4City.create(name="London", country=c)
    await A4City.create(name="Leeds", country=c)
    assert await A4City.all().count() == 2
    await c.delete()
    assert await A4City.all().count() == 0


# --- M2M operations honour the active transaction ----------------------------
@pytest.mark.asyncio
async def test_m2m_add_rolls_back_with_transaction(db):
    """
    GIVEN an M2M link added inside a transaction that then rolls back
    WHEN the block raises
    THEN the link is discarded (M2M writes route through the transaction)
    """
    post = await A4Post.create()
    tag = await A4Tag.create(label="x")
    with pytest.raises(RuntimeError):
        async with in_transaction():
            await post.tags.add(tag)
            assert len(await post.tags) == 1  # read-your-write inside the tx
            raise RuntimeError("boom")
    assert len(await post.tags) == 0


@pytest.mark.asyncio
async def test_m2m_add_commits_with_transaction(db):
    """
    GIVEN an M2M link added inside a transaction that commits
    WHEN the block exits cleanly
    THEN the link persists
    """
    post = await A4Post.create()
    tag = await A4Tag.create(label="y")
    async with in_transaction():
        await post.tags.add(tag)
    assert {t.label for t in await post.tags} == {"y"}


# --- Coalesce binds its default (no quote breakage / injection) --------------
@pytest.mark.asyncio
async def test_coalesce_binds_default_with_quote(db):
    """
    GIVEN a Coalesce default containing a single quote
    WHEN it is used as an annotation
    THEN the value is bound as a parameter and returned verbatim
    """
    c = await A4Country.create(name="US")
    await A4City.create(name="NYC", nick=None, country=c)
    [row] = await A4City.filter(name="NYC").annotate(display=Coalesce("nick", "O'Hara"))
    # The quote-bearing default is bound (not spliced), so it round-trips intact.
    assert row.display == "O'Hara"


# --- empty M2M __in -> no rows, no SQL error ---------------------------------
@pytest.mark.asyncio
async def test_m2m_in_empty_list(db):
    """
    GIVEN an empty membership list
    WHEN filtering an M2M relation with __in=[]
    THEN no rows match and no invalid ``IN ()`` is emitted
    """
    post = await A4Post.create()
    tag = await A4Tag.create(label="z")
    await post.tags.add(tag)
    assert await A4Post.filter(tags__in=[]).count() == 0
    assert await A4Post.filter(tags__in=[tag]).count() == 1


# --- range-checked integer binding (PostgreSQL) ------------------------------
class A4Small(Model):
    id = fields.IntField(pk=True)
    n = fields.SmallIntField()

    class Meta:
        table = "a4_small"


@pytest.mark.asyncio
async def test_smallint_overflow_raises_on_postgres(orm):
    """
    GIVEN a SMALLINT column on PostgreSQL
    WHEN a value beyond its range is bound
    THEN the bind errors instead of silently wrapping to a wrong number
    """
    from yara_orm.connection import get_engine

    await get_engine().execute("DROP TABLE IF EXISTS a4_small")
    await YaraOrm.generate_schemas(models=[A4Small])
    try:
        with pytest.raises(Exception):  # noqa: B017 - any error beats silent corruption
            await A4Small.create(n=100000)  # > 32767
    finally:
        await get_engine().execute("DROP TABLE IF EXISTS a4_small")


# --- auto_now honours use_tz -------------------------------------------------
class A4Stamped(Model):
    id = fields.IntField(pk=True)
    created = fields.DatetimeField(auto_now_add=True)
    updated = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "a4_stamped"


@pytest_asyncio.fixture
async def sqlite_use_tz():
    fd_path = "/tmp/a4_use_tz.db"
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(fd_path + suffix):
            os.remove(fd_path + suffix)
    await YaraOrm.init(f"sqlite://{fd_path}", use_tz=True)
    await YaraOrm.generate_schemas(models=[A4Stamped])
    try:
        yield
    finally:
        await YaraOrm.close()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(fd_path + suffix):
                os.remove(fd_path + suffix)


@pytest.mark.asyncio
async def test_auto_now_is_aware_when_use_tz(sqlite_use_tz):
    """
    GIVEN use_tz is enabled
    WHEN a row with auto_now/auto_now_add columns is created
    THEN those columns are timezone-aware (not a naive/aware mix)
    """
    row = await A4Stamped.create()
    assert row.created.tzinfo is not None
    assert row.updated.tzinfo is not None


# --- RandomHex width honours size on both backends ---------------------------
class A4Token(Model):
    id = fields.IntField(pk=True)
    token = fields.CharField(max_length=64, default=RandomHex(8))

    class Meta:
        table = "a4_token"


@pytest.mark.asyncio
async def test_random_hex_width(db):
    """
    GIVEN a RandomHex(size=8) server default
    WHEN a row is inserted relying on the default
    THEN the generated hex string is 16 chars wide on both backends
    """
    from yara_orm.connection import get_engine

    eng = get_engine()
    await eng.execute("DROP TABLE IF EXISTS a4_token")
    await YaraOrm.generate_schemas(models=[A4Token])
    try:
        await eng.execute("INSERT INTO a4_token (id) VALUES (1)")
        rows = await eng.fetch_rows("SELECT token FROM a4_token WHERE id = 1")
        assert len(rows[0][0]) == 16
    finally:
        await eng.execute("DROP TABLE IF EXISTS a4_token")

"""Correctness regression tests for engine/transaction/binding fixes.

Covers: SQLite foreign-key enforcement + cascade, M2M operations honouring the
active transaction, Coalesce binding its default (no SQL injection / quote
breakage), empty M2M ``__in``, range-checked integer binding on PostgreSQL,
``auto_now`` honouring ``use_tz``, and ``RandomHex`` width honouring ``size``.
"""

import contextlib
import os

import pytest
import pytest_asyncio
from test_bulk_get_or_create import RfxAuthor, RfxBook, RfxItem

from yara_orm import (
    Count,
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


MODELS = [A4Country, A4Tag, A4Post, A4City, RfxAuthor, RfxBook, RfxItem]


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
    # Saving again is an UPDATE: ``updated`` (auto_now) bumps, but ``created``
    # (auto_now_add) is left untouched.
    first_created = row.created
    await row.save()
    assert row.created == first_created


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
        if db == "mssql":
            # SQL Server rejects an explicit value for an IDENTITY column unless
            # IDENTITY_INSERT is toggled on for the statement.
            await eng.execute(
                "SET IDENTITY_INSERT a4_token ON; "
                "INSERT INTO a4_token (id) VALUES (1); "
                "SET IDENTITY_INSERT a4_token OFF"
            )
        else:
            await eng.execute("INSERT INTO a4_token (id) VALUES (1)")
        rows = await eng.fetch_rows("SELECT token FROM a4_token WHERE id = 1")
        assert len(rows[0][0]) == 16
    finally:
        await eng.execute("DROP TABLE IF EXISTS a4_token")


# -- read paths hydrate with the decode plan captured before the await --------


class _PlanSwappingExecutor:
    """Executor proxy that swaps the model's shared decode plan mid-fetch.

    Simulates a concurrent ``meta.compile`` for a *different* dialect landing
    between the fetch await and hydration: ``fetch_rows`` resolves, then the
    shared plan attributes are overwritten before control returns to the read
    path. A read path that re-reads ``meta`` after the await decodes with the
    corrupted plan; one that snapshotted it beforehand is unaffected.
    """

    def __init__(self, inner, swap):
        self._inner = inner
        self._swap = swap

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def fetch_rows(self, sql, params):
        rows = await self._inner.fetch_rows(sql, params)
        self._swap()
        return rows


def _corrupt_plan(meta):
    """Overwrite ``meta``'s shared decode plan with visibly-wrong entries."""
    meta.decoder_names = [f"wrong_{n}" for n in meta.decoder_names]
    meta.active_decoders = [
        (i, name, lambda v: "SWAPPED") for i, (name, _) in enumerate(meta.decoders)
    ]


@contextlib.contextmanager
def _plan_swapped_on_fetch(monkeypatch, *metas):
    """Swap the given metas' decode plans during every queryset fetch await."""
    import yara_orm.queryset as queryset_mod

    saved = [(m, m.decoder_names, m.active_decoders) for m in metas]
    real_get_executor = queryset_mod.get_executor

    def swap():
        for m in metas:
            _corrupt_plan(m)

    def fake_get_executor(model, *args, **kwargs):
        return _PlanSwappingExecutor(real_get_executor(model, *args, **kwargs), swap)

    monkeypatch.setattr(queryset_mod, "get_executor", fake_get_executor)
    try:
        yield
    finally:
        for m, names, active in saved:
            m.decoder_names = names
            m.active_decoders = active


@pytest.mark.asyncio
async def test_fetch_uses_decode_plan_snapshotted_before_await(db, monkeypatch):
    """
    GIVEN a plain fetch whose shared decode plan is swapped during the await
        (as a concurrent other-dialect compile would)
    WHEN the rows are hydrated
    THEN the values decode with the plan captured before the await
    """
    await RfxItem.create(name="keep", qty=7)
    with _plan_swapped_on_fetch(monkeypatch, RfxItem._meta):
        items = await RfxItem.all()
    assert [(i.name, i.qty) for i in items] == [("keep", 7)]


@pytest.mark.asyncio
async def test_select_related_uses_decode_plan_snapshotted_before_await(db, monkeypatch):
    """
    GIVEN a select_related fetch whose base and target decode plans are both
        swapped during the await
    WHEN the rows are hydrated
    THEN base and related instances decode with the pre-await plans
    """
    ada = await RfxAuthor.create(name="Ada")
    await RfxBook.create(title="T", author=ada, qty=1)
    with _plan_swapped_on_fetch(monkeypatch, RfxBook._meta, RfxAuthor._meta):
        books = await RfxBook.all().select_related("author")
    assert books[0].title == "T"
    assert (await books[0].author).name == "Ada"


@pytest.mark.asyncio
async def test_annotated_fetch_uses_decode_plan_snapshotted_before_await(db, monkeypatch):
    """
    GIVEN an annotated fetch whose shared decode plan is swapped during the await
    WHEN the rows are hydrated
    THEN the base instance decodes with the pre-await plan and keeps the annotation
    """
    ada = await RfxAuthor.create(name="Ada")
    await RfxBook.create(title="T", author=ada)
    with _plan_swapped_on_fetch(monkeypatch, RfxAuthor._meta):
        authors = await RfxAuthor.all().annotate(n=Count("books"))
    assert [(a.name, a.n) for a in authors] == [("Ada", 1)]

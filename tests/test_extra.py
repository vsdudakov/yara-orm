"""Coverage: registry, relation managers, conversions and small branches."""

import uuid
from decimal import Decimal
from enum import IntEnum

import pytest

from yara_orm import Count, Model, Sum, fields, registry
from yara_orm.dialects import SqliteDialect


class CvEAuthor(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)
    age = fields.IntField(default=0)

    class Meta:
        table = "cov_e_author"


class CvETag(Model):
    id = fields.IntField(pk=True)
    label = fields.CharField(max_length=50)

    class Meta:
        table = "cov_e_tag"


class CvEBook(Model):
    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=50)
    rating = fields.IntField(default=0)
    author = fields.ForeignKeyField("CvEAuthor", related_name="books")
    tags = fields.ManyToManyField("CvETag", related_name="books", through="cov_e_book_tag")

    class Meta:
        table = "cov_e_book"


class CvEUuid(Model):
    id = fields.UUIDField(pk=True)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "cov_e_uuid"


class CvENoRel(Model):
    """A foreign key declared without a related_name (no reverse accessor)."""

    id = fields.IntField(pk=True)
    ref = fields.ForeignKeyField("CvEAuthor", null=True)

    class Meta:
        table = "cov_e_norel"


MODELS = [CvEAuthor, CvETag, CvEBook, CvEUuid, CvENoRel]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
def test_registry_resolution_and_clear():
    """
    GIVEN registered models
    WHEN resolving by qualified/bare/ambiguous/unknown names and clearing
    THEN each resolution branch and clear behave correctly
    """
    qualified = f"{CvEAuthor.__module__}.CvEAuthor"
    assert registry.get_model(qualified) is CvEAuthor
    assert registry.get_model("CvEAuthor") is CvEAuthor
    with pytest.raises(KeyError):
        registry.get_model("NoSuchModel123")

    saved = dict(registry._MODELS)
    try:
        # Two models sharing a bare name: the most recently defined wins.
        first = type("Dupe", (), {})
        second = type("Dupe", (), {})
        registry._MODELS["m1.Dupe"] = first
        registry._MODELS["m2.Dupe"] = second
        assert registry.get_model("Dupe") is second

        registry.clear()
        assert registry.all_models() == []
    finally:
        registry._MODELS.clear()
        registry._MODELS.update(saved)


# ---------------------------------------------------------------------------
# Relations
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_forward_relation_setattr(db):
    """
    GIVEN a forward foreign-key descriptor
    WHEN the attribute is set to None, an instance, and a raw id
    THEN the backing column reflects each assignment
    """
    a1 = await CvEAuthor.create(name="a1")
    a2 = await CvEAuthor.create(name="a2")
    book = await CvEBook.create(title="t", author=a1)
    book.author = a2
    assert book.author_id == a2.id
    book.author = None
    assert book.author_id is None
    book.author = a1.id
    assert book.author_id == a1.id


@pytest.mark.asyncio
async def test_reverse_manager_all_and_async_for(db):
    """
    GIVEN an author with books
    WHEN iterating the reverse manager via all() and async for
    THEN every related book is yielded
    """
    a = await CvEAuthor.create(name="a")
    await CvEBook.create(title="x", author=a)
    await CvEBook.create(title="y", author=a)
    assert await a.books.all().count() == 2
    seen = [b.title async for b in a.books]
    assert sorted(seen) == ["x", "y"]


@pytest.mark.asyncio
async def test_aggregation_forward_fk_and_m2m_joins(db):
    """
    GIVEN books linked to authors and tags
    WHEN aggregating over a forward-FK column and an m2m relation
    THEN the join-based aggregates compute correctly
    """
    a = await CvEAuthor.create(name="a", age=30)
    b1 = await CvEBook.create(title="b1", author=a)
    tag = await CvETag.create(label="sci")
    await b1.tags.add(tag)

    rows = (
        await CvEBook.annotate(s=Sum("author__age")).group_by("author_id").values("author_id", "s")
    )
    assert rows[0]["s"] == 30
    counts = await CvEBook.annotate(nt=Count("tags"))
    assert {b.title: b.nt for b in counts}["b1"] == 1


@pytest.mark.asyncio
async def test_m2m_manager_empty_ops_and_cached_prefetch(db):
    """
    GIVEN an m2m manager
    WHEN add/remove are called with no objects and after prefetch
    THEN no-op calls are safe and prefetched results are served from cache
    """
    a = await CvEAuthor.create(name="a")
    book = await CvEBook.create(title="t", author=a)
    tag = await CvETag.create(label="x")
    await book.tags.add()  # no-op
    await book.tags.remove()  # no-op
    await book.tags.add(tag)

    [loaded] = await CvEBook.all().prefetch_related("tags")
    assert [t.label for t in await loaded.tags] == ["x"]


@pytest.mark.asyncio
async def test_prefetch_empty_instances(db):
    """
    GIVEN a query that matches no rows
    WHEN prefetch_related is requested
    THEN prefetching short-circuits without error
    """
    assert await CvEAuthor.filter(id=999999).prefetch_related("books") == []


@pytest.mark.asyncio
async def test_uuid_primary_key_on_sqlite(db):
    """
    GIVEN a model keyed by a UUID primary key
    WHEN a row is created on SQLite
    THEN the non-auto-increment pk path builds the table and stores the key
    """
    row = await CvEUuid.create(name="z")
    assert isinstance(row.pk, uuid.UUID)
    assert (await CvEUuid.get(id=row.pk)).name == "z"


# ---------------------------------------------------------------------------
# Field conversions (direct), dialect, CLI, routing
# ---------------------------------------------------------------------------
class _Service(IntEnum):
    A = 1
    B = 2


def test_field_value_conversions():
    """
    GIVEN field conversion helpers
    WHEN to_db / to_python are called with values and None
    THEN they convert or pass through as documented
    """
    assert fields.FloatField().to_python(1.5) == 1.5
    assert fields.FloatField().to_python(None) is None
    assert fields.BooleanField().to_python(1) is True
    assert fields.BooleanField().to_python(None) is None
    assert fields.IntField().to_python(None) is None
    assert fields.BigIntField().to_python("4") == 4
    assert fields.DecimalField().to_db(None) is None
    assert fields.DecimalField().to_python(None) is None
    assert fields.DecimalField().to_db(Decimal("1.5")) == 1.5
    enum_field = fields.IntEnumField(_Service)
    assert enum_field.to_db(_Service.B) == 2
    assert enum_field.to_db(2) == 2
    assert enum_field.to_db(None) is None
    assert enum_field.to_python(None) is None
    assert fields.UUIDField(pk=True).get_default() is not None  # default uuid4 set


def test_sqlite_dialect_has_no_comments():
    """
    GIVEN the SQLite dialect
    WHEN comment SQL is requested for a model
    THEN it returns an empty list (SQLite has no COMMENT)
    """
    assert SqliteDialect()._comment_sql(CvEAuthor._meta) == []


def test_cli_load_models_empty_spec():
    """
    GIVEN an empty --models spec
    WHEN _load_models is called
    THEN it returns immediately without importing anything
    """
    from yara_orm.__main__ import _load_models

    _load_models("")


def test_route_falls_back_to_default():
    """
    GIVEN a router whose methods return None
    WHEN _route resolves a connection name
    THEN it falls back to the default connection
    """
    from yara_orm import YaraOrm
    from yara_orm.connection import _route

    class NullRouter:
        def db_for_read(self, model):
            return None

        def db_for_write(self, model):
            return None

    YaraOrm.set_router(NullRouter())
    try:
        assert _route(CvEAuthor, False) == "default"
        assert _route(None, False) == "default"  # model is None branch
    finally:
        YaraOrm.set_router(None)


def test_aggregate_is_top_level_export():
    """
    GIVEN aggregate-base imports written against the functions module
    WHEN Aggregate is imported from yara_orm
    THEN the top-level name resolves to the aggregation base class
    """
    import yara_orm
    from yara_orm.aggregations import Aggregate

    assert yara_orm.Aggregate is Aggregate

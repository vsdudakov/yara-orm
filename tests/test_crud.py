"""Core CRUD: create/get, rich types, filtering, update/delete, bulk, errors.

Integration tests against a live PostgreSQL.
Set ORM_TEST_DB to override the connection (default postgres://localhost/orm_demo).
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest

from yara_orm import DoesNotExist, Model, MultipleObjectsReturned, fields
from yara_orm.connection import get_engine


class CrudAuthor(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=100, unique=True)
    age = fields.IntField(null=True)

    class Meta:
        table = "c_author"


class CrudBook(Model):
    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=200, index=True)
    price = fields.DecimalField(max_digits=8, decimal_places=2, null=True)
    meta = fields.JSONField(null=True)
    code = fields.UUIDField(null=True)
    published = fields.DateField(null=True)
    in_print = fields.BooleanField(default=True)
    author = fields.ForeignKeyField("CrudAuthor", related_name="books", on_delete="CASCADE")

    class Meta:
        table = "c_book"


async def _reset():
    engine = get_engine()
    await engine.execute("DROP TABLE IF EXISTS c_book CASCADE")
    await engine.execute("DROP TABLE IF EXISTS c_author CASCADE")
    from yara_orm import YaraOrm

    await YaraOrm.generate_schemas()


@pytest.mark.asyncio
async def test_create_and_get(orm):
    """
    GIVEN a fresh schema
    WHEN an Author is created and then fetched by id
    THEN the persisted fields and primary key round-trip correctly
    """
    await _reset()
    a = await CrudAuthor.create(name="Ada", age=36)
    assert a.pk == 1
    assert a._in_db is True

    fetched = await CrudAuthor.get(id=a.id)
    assert fetched.name == "Ada"
    assert fetched.age == 36


@pytest.mark.asyncio
async def test_rich_types_roundtrip(orm):
    """
    GIVEN a Book with decimal, json, uuid, date and boolean fields
    WHEN it is created and re-read
    THEN every value round-trips to its native Python type
    """
    await _reset()
    a = await CrudAuthor.create(name="Grace")
    code = uuid.uuid4()
    b = await CrudBook.create(
        title="Compilers",
        price=Decimal("42.50"),
        meta={"tags": ["lang", "vm"], "stars": 5},
        code=code,
        published=date(2020, 1, 2),
        author=a,
    )
    got = await CrudBook.get(id=b.id)
    assert got.price == Decimal("42.50")
    assert got.meta == {"tags": ["lang", "vm"], "stars": 5}
    assert isinstance(got.code, uuid.UUID) and got.code == code
    assert got.published == date(2020, 1, 2)
    assert got.in_print is True


@pytest.mark.asyncio
async def test_filtering_and_ordering(orm):
    """
    GIVEN several Books for one Author
    WHEN filtering with lookups and ordering
    THEN the matching rows come back in the requested order
    """
    await _reset()
    a = await CrudAuthor.create(name="Don")
    for title in ["Alpha", "Beta", "Gamma", "Algorithms"]:
        await CrudBook.create(title=title, author=a)

    al = await CrudBook.filter(title__startswith="Al").order_by("title")
    assert [b.title for b in al] == ["Algorithms", "Alpha"]

    assert await CrudBook.filter(author=a).count() == 4
    assert await CrudBook.filter(title__icontains="a").exists() is True
    assert await CrudBook.exclude(title__startswith="Al").order_by("title").first() is not None


@pytest.mark.asyncio
async def test_update_and_delete(orm):
    """
    GIVEN a persisted Author
    WHEN it is updated via instance.save and queryset.update, then deleted
    THEN each mutation is reflected in the database
    """
    await _reset()
    a = await CrudAuthor.create(name="Linus", age=20)
    a.age = 54
    await a.save()
    assert (await CrudAuthor.get(id=a.id)).age == 54

    n = await CrudAuthor.filter(name="Linus").update(age=55)
    assert n == 1
    assert (await CrudAuthor.get(id=a.id)).age == 55

    await a.delete()
    assert await CrudAuthor.filter(id=a.id).exists() is False


@pytest.mark.asyncio
async def test_get_errors(orm):
    """
    GIVEN a query that matches zero or many rows
    WHEN get() / get_or_none() are called
    THEN DoesNotExist / MultipleObjectsReturned / None behave as documented
    """
    await _reset()
    a = await CrudAuthor.create(name="Solo")
    await CrudBook.create(title="Dup", author=a)
    await CrudBook.create(title="Dup", author=a)

    with pytest.raises(DoesNotExist):
        await CrudAuthor.get(name="nobody")
    with pytest.raises(MultipleObjectsReturned):
        await CrudBook.get(title="Dup")
    assert await CrudAuthor.get_or_none(name="nobody") is None


@pytest.mark.asyncio
async def test_bulk_create(orm):
    """
    GIVEN 1500 unsaved Book instances
    WHEN bulk_create persists them in batches
    THEN all rows are inserted and generated primary keys are written back
    """
    await _reset()
    a = await CrudAuthor.create(name="Bulk")
    books = [CrudBook(title=f"B{i}", author=a) for i in range(1500)]
    created = await CrudBook.bulk_create(books, batch_size=400)
    assert len(created) == 1500
    assert all(b.pk is not None for b in created)
    assert len({b.pk for b in created}) == 1500
    assert await CrudBook.filter(author=a).count() == 1500


@pytest.mark.asyncio
async def test_values_and_values_list(orm):
    """
    GIVEN persisted Books and Authors
    WHEN projecting with values() and values_list()
    THEN dict and tuple/scalar projections are returned without model objects
    """
    await _reset()
    a = await CrudAuthor.create(name="Vee", age=40)
    await CrudBook.create(title="One", author=a)
    await CrudBook.create(title="Two", author=a)

    titles = await CrudBook.all().order_by("title").values_list("title", flat=True)
    assert titles == ["One", "Two"]

    pairs = await CrudBook.all().order_by("title").values_list("id", "title")
    assert [p[1] for p in pairs] == ["One", "Two"]
    assert all(isinstance(p, tuple) and len(p) == 2 for p in pairs)

    dicts = await CrudAuthor.all().values("name", "age")
    assert dicts == [{"name": "Vee", "age": 40}]


@pytest.mark.asyncio
async def test_in_and_isnull(orm):
    """
    GIVEN Authors with and without an age
    WHEN filtering with __isnull and __in lookups
    THEN null-aware and membership filters select the right rows
    """
    await _reset()
    await CrudAuthor.create(name="Kay", age=None)
    await CrudAuthor.create(name="Bob", age=30)
    assert await CrudAuthor.filter(age__isnull=True).count() == 1
    names = {r.name for r in await CrudAuthor.filter(name__in=["Kay", "Bob"])}
    assert names == {"Kay", "Bob"}
    assert (await CrudAuthor.filter(age__isnull=True).first()).name == "Kay"

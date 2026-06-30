"""Tortoise-compatibility fixes surfaced by the callbear and wiserfunding
migrations: chainable ``first()``/``QuerySetSingle`` projections, model
identity (``__eq__``/``__hash__``), ``BooleanField`` write coercion, lenient
``JSONField`` serialisation, inherited ``Meta.extra_kwargs``, the ``db_table``
setter, per-column index operator classes, and the top-level ``Aggregate``
export.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

import pytest

from yara_orm import Model, fields
from yara_orm import migrations as m


class CmpAuthor(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "cmp_author"


class CmpBook(Model):
    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=50)
    active = fields.BooleanField(default=False)
    payload = fields.JSONField(null=True)
    author = fields.ForeignKeyField("CmpAuthor", related_name="books")

    class Meta:
        table = "cmp_book"


MODELS = [CmpAuthor, CmpBook]


async def _seed() -> tuple[CmpAuthor, CmpBook, CmpBook]:
    """Create one author with two books.

    Returns:
        The author and its two books (ordered by title).
    """
    ada = await CmpAuthor.create(name="Ada")
    b1 = await CmpBook.create(title="B1", author=ada, active=True)
    b2 = await CmpBook.create(title="B2", author=ada)
    return ada, b1, b2


# -- chainable first() / QuerySetSingle projections ---------------------------


@pytest.mark.asyncio
async def test_first_then_values_returns_single_dict(db):
    """
    GIVEN matching rows
    WHEN first() is chained into values()
    THEN a single dict (not a list) of the requested columns is returned
    """
    await _seed()
    row = await CmpBook.filter(title="B1").first().values("title")
    assert row == {"title": "B1"}


@pytest.mark.asyncio
async def test_first_then_only_returns_single_model(db):
    """
    GIVEN matching rows
    WHEN first() is chained into only()
    THEN a single model instance restricted to those columns is returned
    """
    _, b1, _ = await _seed()
    book = await CmpBook.filter(title="B1").first().only("id", "title")
    assert book is not None
    assert book.id == b1.id
    assert book.title == "B1"


@pytest.mark.asyncio
async def test_first_then_values_list_flat_returns_scalar(db):
    """
    GIVEN matching rows
    WHEN first() is chained into values_list(flat=True)
    THEN the single scalar value is returned
    """
    _, b1, _ = await _seed()
    value = await CmpBook.filter(title="B1").first().values_list("id", flat=True)
    assert value == b1.id


@pytest.mark.asyncio
async def test_first_projection_on_no_match_returns_none(db):
    """
    GIVEN no matching row
    WHEN first() is chained into a projection
    THEN the result resolves to None rather than raising
    """
    await _seed()
    assert await CmpBook.filter(title="missing").first().values("title") is None
    assert await CmpBook.filter(title="missing").first().only("id") is None
    assert await CmpBook.filter(title="missing").first() is None


@pytest.mark.asyncio
async def test_get_then_values_returns_single_dict(db):
    """
    GIVEN exactly one matching row
    WHEN get() is chained into values()
    THEN a single dict is returned
    """
    await _seed()
    assert await CmpBook.get(title="B1").values("title") == {"title": "B1"}


@pytest.mark.asyncio
async def test_first_select_related_then_values_traverses_relation(db):
    """
    GIVEN a book linked to an author
    WHEN first() eager-loads the relation and values() selects a related column
    THEN the related value appears under the path key
    """
    await _seed()
    row = (
        await CmpBook.filter(title="B1")
        .first()
        .select_related("author")
        .values("title", "author__name")
    )
    assert row == {"title": "B1", "author__name": "Ada"}


# -- model identity (__eq__ / __hash__) ---------------------------------------


@pytest.mark.asyncio
async def test_same_pk_instances_compare_equal(db):
    """
    GIVEN a row fetched twice into separate instances
    WHEN they are compared and tested for list/set membership
    THEN same-model same-pk instances are equal and hash alike
    """
    _, b1, _ = await _seed()
    again = await CmpBook.get(id=b1.id)
    assert b1 == again
    assert again in [b1]
    assert b1 in {again}


@pytest.mark.asyncio
async def test_different_pk_and_cross_model_not_equal(db):
    """
    GIVEN two different rows and an unrelated model
    WHEN they are compared
    THEN distinct pks and cross-model instances are not equal
    """
    ada, b1, b2 = await _seed()
    assert b1 != b2
    assert b1 != ada
    assert b1 != "not-a-model"


def test_unsaved_instances_are_equal_only_to_self():
    """
    GIVEN two unsaved instances (no primary key)
    WHEN they are compared
    THEN each is equal only to itself
    """
    a = CmpAuthor(name="x")
    b = CmpAuthor(name="x")
    assert a == a
    assert a != b


# -- BooleanField write coercion ----------------------------------------------


@pytest.mark.asyncio
async def test_boolean_field_coerces_non_bool_writes(db):
    """
    GIVEN a boolean column assigned truthy/falsy non-bool values
    WHEN the rows are saved and read back
    THEN the values round-trip as real booleans (Tortoise bool() semantics)
    """
    ada = await CmpAuthor.create(name="Ada")
    truthy = await CmpBook.create(title="T", author=ada, active="yes")
    falsy = await CmpBook.create(title="F", author=ada, active=0)
    assert (await CmpBook.get(id=truthy.id)).active is True
    assert (await CmpBook.get(id=falsy.id)).active is False


# -- lenient JSONField serialisation ------------------------------------------


@pytest.mark.asyncio
async def test_jsonfield_serialises_exotic_python_types(db):
    """
    GIVEN a JSON column holding UUID/Decimal/datetime/date/set values
    WHEN the row is saved and read back
    THEN the values are coerced to JSON-native forms instead of raising
    """
    ada = await CmpAuthor.create(name="Ada")
    uid = uuid.uuid4()
    payload = {
        "uid": uid,
        "price": Decimal("1.50"),
        "when": datetime(2026, 6, 30, 12, 0, 0),
        "day": date(2026, 6, 30),
        "tags": {"a", "b"},
    }
    book = await CmpBook.create(title="J", author=ada, payload=payload)
    stored = (await CmpBook.get(id=book.id)).payload
    assert stored["uid"] == str(uid)
    assert stored["price"] == "1.50"
    assert stored["when"] == "2026-06-30T12:00:00"
    assert stored["day"] == "2026-06-30"
    assert sorted(stored["tags"]) == ["a", "b"]


# -- inherited Meta.extra_kwargs ----------------------------------------------


class StoreBase(Model):
    id = fields.IntField(pk=True)

    class Meta:
        abstract = True
        extra_kwargs = "store"


class StoreChild(StoreBase):
    name = fields.CharField(max_length=20)

    class Meta:
        table = "store_child"


def test_extra_kwargs_inherited_from_abstract_base():
    """
    GIVEN an abstract base declaring Meta.extra_kwargs = "store"
    WHEN a concrete subclass with its own Meta is constructed with unknown kwargs
    THEN the option is inherited and the unknown kwargs are stored, not rejected
    """
    assert StoreChild._meta.extra_kwargs == "store"
    obj = StoreChild(name="x", computed="kept")
    assert obj.computed == "kept"


# -- MetaInfo.db_table setter -------------------------------------------------


def test_db_table_setter_renames_table():
    """
    GIVEN a model's _meta
    WHEN db_table is assigned (Tortoise alias)
    THEN the underlying table name is updated
    """

    class Renamable(Model):
        id = fields.IntField(pk=True)

        class Meta:
            table = "renamable_old"

    Renamable._meta.db_table = "renamable_new"
    assert Renamable._meta.table == "renamable_new"
    assert Renamable._meta.db_table == "renamable_new"


# -- per-column index operator class ------------------------------------------


def test_index_opclass_renders_on_postgres_and_drops_on_sqlite():
    """
    GIVEN a composite index carrying a per-column operator class
    WHEN it is rendered on each dialect
    THEN PostgreSQL appends the opclass and SQLite omits it
    """
    from yara_orm.dialects import PostgresDialect, SqliteDialect

    op = m.AddCompositeIndex("t", "idx_trgm", ["a"], using="gin", opclass="gin_trgm_ops")
    [pg_sql] = op.forward_sql(PostgresDialect(), {})
    assert '("a" gin_trgm_ops)' in pg_sql
    [lite_sql] = op.forward_sql(SqliteDialect(), {})
    assert '("a")' in lite_sql
    assert "gin_trgm_ops" not in lite_sql


def test_index_opclass_round_trips_through_migration_source():
    """
    GIVEN a composite index op with an operator class
    WHEN it is rendered to migration source
    THEN the opclass keyword is emitted so re-runs preserve it
    """
    op = m.AddCompositeIndex("t", "idx_trgm", ["a"], using="gin", opclass="gin_trgm_ops")
    assert "opclass='gin_trgm_ops'" in op.to_source()


def test_meta_index_opclass_emitted_in_create_table_sql():
    """
    GIVEN a model declaring Index(opclass=...) in Meta.indexes
    WHEN its table DDL is generated on PostgreSQL
    THEN the index statement carries the operator class
    """
    from yara_orm import Index
    from yara_orm.dialects import PostgresDialect

    # Abstract so the model is not registered globally (it carries an index that
    # needs the pg_trgm extension, which other tests' generate_schemas() lack).
    class Doc(Model):
        id = fields.IntField(pk=True)
        body = fields.TextField()

        class Meta:
            abstract = True
            table = "cmp_doc"
            indexes = [Index(fields=["body"], using="gin", opclass="gin_trgm_ops")]

    sql = "\n".join(PostgresDialect().create_table_sql(Doc._meta))
    assert "gin_trgm_ops" in sql


# -- top-level Aggregate export -----------------------------------------------


def test_aggregate_is_top_level_export():
    """
    GIVEN code migrating from `from tortoise.functions import Aggregate`
    WHEN Aggregate is imported from yara_orm
    THEN it resolves to the aggregation base class
    """
    import yara_orm
    from yara_orm.aggregations import Aggregate

    assert yara_orm.Aggregate is Aggregate

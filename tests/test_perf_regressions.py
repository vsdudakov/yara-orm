"""Behavioral regression coverage for the Python-layer performance work.

- the ``__setattr__`` override that un-marks never-fetched database-default
  columns is installed only on models that declare a ``DatabaseDefault``
  column (plain models keep ``object.__setattr__``), and is inherited by
  subclasses of such models;
- explicit ``None`` assignment on a db-default model still clears the
  never-fetched mark and persists;
- ``bulk_create`` stamps every ``auto_now``/``auto_now_add`` column of a batch
  with one shared timestamp, while single ``save()`` keeps a per-call ``now``;
- ``Model.get(...)`` builds its queryset lazily but still chains
  (``select_related``) and resolves identically;
- the memoised simple-equality SELECT distinguishes ``IS NULL`` lookups from
  value lookups on the same field names.
"""

import pytest

from yara_orm import Model, SqlDefault, fields
from yara_orm.fields import Field
from yara_orm.models import _setattr_unmark_db_default


class PrPlain(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20)
    val = fields.CharField(max_length=20, null=True)

    class Meta:
        table = "pr_plain"


class PrDefaulted(Model):
    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=20)
    flag = fields.IntField(default=SqlDefault("7"), null=True)

    class Meta:
        table = "pr_defaulted"


class PrDefaultedBase(Model):
    id = fields.IntField(pk=True)
    flag = fields.IntField(default=SqlDefault("7"), null=True)

    class Meta:
        abstract = True


class PrDefaultedChild(PrDefaultedBase):
    label = fields.CharField(max_length=20, null=True)

    class Meta:
        table = "pr_defaulted_child"


class PrStamped(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "pr_stamped"


class PrStampedDefault(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20, unique=True)
    flag = fields.IntField(default=SqlDefault("7"), null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "pr_stamped_default"


class PrCountry(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20)

    class Meta:
        table = "pr_country"


class PrAgent(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20)

    class Meta:
        table = "pr_agent"


class PrAuthor(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20)
    country = fields.ForeignKeyField("PrCountry", related_name="authors", null=True)
    agent = fields.ForeignKeyField("PrAgent", related_name="authors", null=True)

    class Meta:
        table = "pr_author"


class PrBook(Model):
    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=20)
    author = fields.ForeignKeyField("PrAuthor", related_name="books")

    class Meta:
        table = "pr_book"


MODELS = [
    PrPlain,
    PrDefaulted,
    PrDefaultedChild,
    PrStamped,
    PrStampedDefault,
    PrCountry,
    PrAgent,
    PrAuthor,
    PrBook,
]


# -- conditional __setattr__ override ------------------------------------------


def test_plain_model_uses_object_setattr():
    """
    GIVEN a model without any database-default column
    WHEN its class is created
    THEN it keeps plain ``object.__setattr__`` (no unmarking override installed)
    """
    assert "__setattr__" not in vars(PrPlain)
    assert PrPlain.__setattr__ is object.__setattr__
    assert Model.__setattr__ is object.__setattr__


def test_base_field_to_python_value_is_identity():
    """
    GIVEN the base ``Field.to_python_value`` — the sentinel the construction
      plan compares against to skip per-value coercion for plain fields
    WHEN it is called directly
    THEN it returns its argument unchanged (the identity default)
    """
    sentinel = object()
    assert Field.to_python_value(fields.CharField(max_length=1), sentinel) is sentinel


def test_db_default_model_gets_unmarking_setattr():
    """
    GIVEN a model declaring a ``SqlDefault`` column
    WHEN its class is created
    THEN the unmarking ``__setattr__`` override is installed on that class
    """
    assert vars(PrDefaulted)["__setattr__"] is _setattr_unmark_db_default


def test_subclass_of_db_default_base_gets_unmarking_setattr():
    """
    GIVEN a concrete model inheriting a database-default column from an
      abstract base
    WHEN its class is created
    THEN it also carries the unmarking ``__setattr__`` override
    """
    assert PrDefaultedChild._meta.db_default_fields
    assert PrDefaultedChild.__setattr__ is _setattr_unmark_db_default


@pytest.mark.asyncio
async def test_explicit_none_still_persists_on_db_default_model(sqlite_db):
    """
    GIVEN a row whose database-default column was never fetched back
    WHEN ``None`` is explicitly assigned before a full save()
    THEN the assignment clears the never-fetched mark and NULL is persisted
    """
    doc = await PrDefaulted.create(title="hi")
    assert doc.flag is None  # never fetched; fetch_db_defaults is off

    doc.flag = None  # explicit: the in-memory value becomes authoritative
    await doc.save()

    fresh = await PrDefaulted.get(id=doc.id)
    assert fresh.flag is None


# -- bulk_create shared timestamp ----------------------------------------------


@pytest.mark.asyncio
async def test_bulk_create_batch_shares_one_timestamp(sqlite_db):
    """
    GIVEN many unsaved instances of an auto_now/auto_now_add model
    WHEN one bulk_create call inserts them
    THEN every row carries the identical creation/update timestamp
    """
    objs = [PrStamped(name=f"n{i}") for i in range(50)]

    await PrStamped.bulk_create(objs)

    stamps = {(o.created_at, o.updated_at) for o in objs}
    assert len(stamps) == 1
    created, updated = stamps.pop()
    assert created == updated
    fresh = await PrStamped.all()
    assert {(o.created_at, o.updated_at) for o in fresh} == {(created, updated)}


@pytest.mark.asyncio
async def test_single_save_keeps_per_call_timestamp(sqlite_db):
    """
    GIVEN a saved auto_now row
    WHEN it is saved again later
    THEN updated_at is bumped to a fresh now() while created_at is untouched
    """
    row = await PrStamped.create(name="one")
    first_created, first_updated = row.created_at, row.updated_at

    await row.save()

    assert row.created_at == first_created
    assert row.updated_at > first_updated


@pytest.mark.asyncio
async def test_bulk_upsert_of_persisted_rows_keeps_add_stamp_and_unmarks(sqlite_db):
    """
    GIVEN an already-persisted auto_now row whose db-default column was never
      fetched back
    WHEN a bulk_create upsert re-writes it
    THEN created_at is untouched, updated_at is bumped, and the never-fetched
      mark handling matches an explicit assignment
    """
    row = await PrStampedDefault.create(name="one")
    assert row.__dict__["_unfetched_db_defaults"] == {"flag"}
    first_created, first_updated = row.created_at, row.updated_at

    await PrStampedDefault.bulk_create([row], update_fields=["updated_at"], on_conflict=["name"])

    assert row.created_at == first_created  # auto_now_add never rewritten in-db
    assert row.updated_at > first_updated
    assert row.__dict__["_unfetched_db_defaults"] == {"flag"}
    fresh = await PrStampedDefault.get(name="one")
    assert fresh.id == row.id  # updated in place, not duplicated
    assert fresh.updated_at > first_updated
    assert fresh.flag == 7  # the DB-supplied default survived the upsert


# -- lazy Model.get queryset ----------------------------------------------------


@pytest.mark.asyncio
async def test_get_fast_path_and_chaining_agree(sqlite_db):
    """
    GIVEN a book row with a related author
    WHEN Model.get is awaited directly and with a chained select_related
    THEN both resolve to the same row and the chained form loads the relation
    """
    author = await PrAuthor.create(name="ann")
    book = await PrBook.create(title="t", author=author)

    plain = await PrBook.get(id=book.id)
    chained = await PrBook.get(id=book.id).select_related("author")

    assert plain == chained == book
    assert chained.author.name == "ann"


@pytest.mark.asyncio
async def test_get_missing_row_raises_on_both_paths(sqlite_db):
    """
    GIVEN no matching row
    WHEN Model.get is awaited directly and with a chained method
    THEN both the fast path and the queryset fallback raise DoesNotExist
    """
    with pytest.raises(PrBook.DoesNotExist):
        await PrBook.get(id=99999)
    with pytest.raises(PrBook.DoesNotExist):
        await PrBook.get(id=99999).select_related("author")


@pytest.mark.asyncio
async def test_select_related_two_nested_relations_share_parent(sqlite_db):
    """
    GIVEN books whose author has two forward relations (country and agent)
    WHEN both nested paths are select_related in one query
    THEN the shared author instance carries both children (and a NULL relation
      hydrates as None)
    """
    country = await PrCountry.create(name="fr")
    agent = await PrAgent.create(name="max")
    linked = await PrAuthor.create(name="ann", country=country, agent=agent)
    bare = await PrAuthor.create(name="bob")
    await PrBook.create(title="a", author=linked)
    await PrBook.create(title="b", author=bare)

    books = await PrBook.all().select_related("author__country", "author__agent").order_by("title")

    assert books[0].author.country.name == "fr"
    assert books[0].author.agent.name == "max"
    assert books[1].author.country is None
    assert books[1].author.agent is None


# -- memoised simple-equality SELECT ---------------------------------------------


@pytest.mark.asyncio
async def test_cached_lookup_distinguishes_null_from_value(sqlite_db):
    """
    GIVEN rows where a nullable column is NULL on one and set on another
    WHEN get_or_none looks up by None and then by a value on the same field
    THEN each lookup matches its own row (the NULL shape is cached separately)
    """
    empty = await PrPlain.create(name="empty", val=None)
    full = await PrPlain.create(name="full", val="x")

    assert (await PrPlain.get_or_none(val=None)).id == empty.id
    assert (await PrPlain.get_or_none(val="x")).id == full.id
    # Repeat both shapes so the second pass runs off the memoised statements.
    assert (await PrPlain.get_or_none(val=None)).id == empty.id
    assert (await PrPlain.get_or_none(val="x")).id == full.id

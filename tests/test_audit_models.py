"""Audit regression coverage for model persistence and field semantics.

- database-default columns: preserved on full save(), explicit values honoured
  on insert (single and bulk), ``Meta.fetch_db_defaults`` refreshes instances;
- ``save``/``delete`` render SQL with the dialect of ``using_db``;
- mutable field defaults are copied per instance;
- managers: shared instances are not rebound, abstract-base managers inherit;
- bulk upserts: default update_fields spans all records, key matching coerces
  loose record values;
- ``get_or_none`` honours ``Meta.ordering`` on its fast path;
- ``BooleanField`` coerces boolean strings semantically;
- ``NumericValidator`` rejects non-finite values;
- unsaved instances are unhashable.
"""

import datetime as dt
from decimal import Decimal

import pytest

from yara_orm import Manager, Model, SqlDefault, YaraOrm, connections, fields
from yara_orm.validators import NumericValidator, ValidationError


class AmDoc(Model):
    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=20)
    flag = fields.IntField(default=SqlDefault("7"))

    class Meta:
        table = "am_doc"


class AmDocFetch(Model):
    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=20)
    flag = fields.IntField(default=SqlDefault("7"))
    created = fields.DatetimeField(default=SqlDefault("CURRENT_TIMESTAMP"))

    class Meta:
        table = "am_doc_fetch"
        fetch_db_defaults = True


class AmScored(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20)
    score = fields.IntField(default=0)

    class Meta:
        table = "am_scored"
        ordering = ["-score"]


class AmKeyed(Model):
    id = fields.IntField(pk=True)
    num = fields.IntField()
    day = fields.DateField(null=True)
    val = fields.CharField(max_length=20, null=True)

    class Meta:
        table = "am_keyed"


class AmPair(Model):
    id = fields.IntField(pk=True)
    k = fields.IntField()
    a = fields.CharField(max_length=20, null=True)
    b = fields.CharField(max_length=20, null=True)

    class Meta:
        table = "am_pair"


class AmFlagged(Model):
    id = fields.IntField(pk=True)
    active = fields.BooleanField(default=False)

    class Meta:
        table = "am_flagged"


class _ActiveManager(Manager):
    def get_queryset(self):
        return super().get_queryset().filter(deleted=False)


_shared_manager = _ActiveManager()


class AmSharedA(Model):
    id = fields.IntField(pk=True)
    deleted = fields.BooleanField(default=False)

    class Meta:
        table = "am_shared_a"
        manager = _shared_manager


class AmSharedB(Model):
    id = fields.IntField(pk=True)
    deleted = fields.BooleanField(default=False)

    class Meta:
        table = "am_shared_b"
        manager = _shared_manager


class AmSoftBase(Model):
    id = fields.IntField(pk=True)
    deleted = fields.BooleanField(default=False)

    class Meta:
        abstract = True
        manager = _ActiveManager()


class AmSoftNote(AmSoftBase):
    text = fields.CharField(max_length=20, null=True)

    class Meta:
        table = "am_soft_note"


class AmOnlyDefault(Model):
    id = fields.IntField(pk=True)
    flag = fields.IntField(default=SqlDefault("7"))

    class Meta:
        table = "am_only_default"


MODELS = [
    AmDoc,
    AmDocFetch,
    AmScored,
    AmKeyed,
    AmPair,
    AmFlagged,
    AmSharedA,
    AmSharedB,
    AmSoftNote,
    AmOnlyDefault,
]


# -- database-default columns (findings 1 & 2) --------------------------------


@pytest.mark.asyncio
async def test_full_save_preserves_db_default_columns(sqlite_db):
    """
    GIVEN a row created without its database-default column fetched back
    WHEN the instance is mutated and fully saved again
    THEN the UPDATE leaves the DB-supplied value intact instead of NULL-ing it
    """
    doc = await AmDoc.create(title="hi")
    assert doc.flag is None  # never fetched; fetch_db_defaults is off

    doc.title = "hello"
    await doc.save()

    fresh = await AmDoc.get(id=doc.id)
    assert fresh.title == "hello"
    assert fresh.flag == 7


@pytest.mark.asyncio
async def test_full_save_with_every_column_unfetched_is_skipped(sqlite_db):
    """
    GIVEN a model whose only non-pk column is a never-fetched database default
    WHEN a full save() runs right after create()
    THEN the UPDATE is skipped entirely and the DB-computed value survives
    """
    doc = await AmOnlyDefault.create()
    assert doc.flag is None  # never fetched; fetch_db_defaults is off

    await doc.save()

    fresh = await AmOnlyDefault.get(id=doc.id)
    assert fresh.flag == 7


@pytest.mark.asyncio
async def test_update_or_create_preserves_db_default_columns(sqlite_db):
    """
    GIVEN an existing row with a database-default column
    WHEN update_or_create updates an unrelated field
    THEN the database-default column keeps its stored value
    """
    doc = await AmDoc.create(title="first")
    obj, created = await AmDoc.update_or_create(defaults={"title": "second"}, id=doc.id)
    assert created is False

    fresh = await AmDoc.get(id=doc.id)
    assert fresh.title == "second"
    assert fresh.flag == 7


@pytest.mark.asyncio
async def test_explicit_value_for_db_default_column_is_inserted(sqlite_db):
    """
    GIVEN a database-default column supplied explicitly on create()
    WHEN the row is inserted (auto-increment pk fast path)
    THEN the explicit value is stored, not silently replaced by the default
    """
    doc = await AmDoc.create(title="t", flag=3)
    fresh = await AmDoc.get(id=doc.id)
    assert fresh.flag == 3


@pytest.mark.asyncio
async def test_explicit_pk_and_db_default_value_inserted(sqlite_db):
    """
    GIVEN an explicit primary key and an explicit database-default value
    WHEN the row is inserted via the explicit-pk path
    THEN both values are stored as given
    """
    await AmDoc.create(id=99, title="explicit", flag=5)
    fresh = await AmDoc.get(id=99)
    assert fresh.flag == 5


@pytest.mark.asyncio
async def test_bulk_create_honours_explicit_db_default_values(sqlite_db):
    """
    GIVEN a bulk_create batch mixing set and unset database-default columns
    WHEN the rows are inserted
    THEN explicit values are stored while unset rows get the database default
    """
    await AmDoc.bulk_create([AmDoc(title="dflt"), AmDoc(title="set", flag=3), AmDoc(title="dflt2")])
    by_title = {d.title: d for d in await AmDoc.all()}
    assert by_title["set"].flag == 3
    assert by_title["dflt"].flag == 7
    assert by_title["dflt2"].flag == 7


@pytest.mark.asyncio
async def test_fetch_db_defaults_refreshes_instance_on_create(sqlite_db):
    """
    GIVEN a model with Meta.fetch_db_defaults = True
    WHEN a row is created without its database-default columns
    THEN the INSERT ... RETURNING writes the DB-supplied values onto the instance
    """
    doc = await AmDocFetch.create(title="hi")
    assert doc.flag == 7
    assert doc.created is not None

    # A later full save() then round-trips the fetched values unchanged.
    doc.title = "hello"
    await doc.save()
    fresh = await AmDocFetch.get(id=doc.id)
    assert fresh.flag == 7 and fresh.title == "hello"


# -- save/delete dialect routing with using_db (finding 6) ---------------------


@pytest.mark.asyncio
async def test_save_and_delete_route_dialect_through_using_db(sqlite_db, tmp_path):
    """
    GIVEN a second named connection
    WHEN an instance is saved to and deleted from it via using_db
    THEN the statements run on that connection (row absent from the default DB)
    """
    await YaraOrm.add_connection("am_aux", f"sqlite://{tmp_path}/aux.db")
    await connections.get("am_aux").execute(
        "CREATE TABLE am_doc (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "title VARCHAR(20) NOT NULL, flag INTEGER NOT NULL DEFAULT 7)"
    )

    doc = AmDoc(title="aux-only")
    await doc.save(using_db="am_aux")
    assert doc.id is not None
    # The row lives in the aux database only.
    assert await AmDoc.get_or_none(id=doc.id) is None
    aux_rows = await connections.get("am_aux").fetch_rows("SELECT title FROM am_doc")
    assert [r[0] for r in aux_rows] == ["aux-only"]

    await doc.delete(using_db="am_aux")
    aux_rows = await connections.get("am_aux").fetch_rows("SELECT title FROM am_doc")
    assert aux_rows == []


# -- mutable field defaults (finding 7) ----------------------------------------


def test_mutable_defaults_are_copied_per_instance():
    """
    GIVEN fields declared with mutable defaults (dict / list)
    WHEN two values are resolved and one is mutated
    THEN each get_default() call yields an independent copy
    """
    json_field = fields.JSONField(default={"tags": []})
    first = json_field.get_default()
    first["tags"].append("x")
    assert json_field.get_default() == {"tags": []}

    list_field = fields.JSONField(default=[1, 2])
    a = list_field.get_default()
    a.append(3)
    assert list_field.get_default() == [1, 2]


# -- manager binding and inheritance (finding 10) -------------------------------


def test_shared_manager_instance_is_not_rebound_across_models():
    """
    GIVEN one manager instance shared by two models' Meta
    WHEN the classes are created
    THEN each model gets its own bound copy (the first is not silently rebound)
    """
    assert AmSharedA._meta.manager is not AmSharedB._meta.manager
    assert AmSharedA._meta.manager._model is AmSharedA
    assert AmSharedB._meta.manager._model is AmSharedB
    assert type(AmSharedA._meta.manager) is _ActiveManager


@pytest.mark.asyncio
async def test_manager_inherited_from_abstract_base(sqlite_db):
    """
    GIVEN an abstract base whose Meta declares a soft-delete manager
    WHEN a concrete subclass without its own Meta.manager queries rows
    THEN the inherited manager scopes the subclass's queries
    """
    assert type(AmSoftNote._meta.manager) is _ActiveManager
    assert AmSoftNote._meta.manager._model is AmSoftNote

    await AmSoftNote.create(text="live", deleted=False)
    await AmSoftNote.create(text="gone", deleted=True)
    assert [n.text for n in await AmSoftNote.all()] == ["live"]
    assert await AmSoftNote.get_or_none(text="gone") is None


# -- bulk upsert defaults and key coercion (findings 11 & 12) -------------------


@pytest.mark.asyncio
async def test_bulk_update_or_create_updates_fields_from_all_records(sqlite_db):
    """
    GIVEN heterogeneous records where later ones carry fields the first lacks
    WHEN bulk_update_or_create runs without explicit update_fields
    THEN every non-key field present in any record is written back
    """
    await AmPair.create(k=1, a="old-a")
    await AmPair.create(k=2, b="old-b")

    await AmPair.bulk_update_or_create(
        [{"k": 1, "a": "new-a"}, {"k": 2, "b": "new-b"}], key_fields=["k"]
    )
    by_k = {p.k: p for p in await AmPair.all()}
    assert by_k[1].a == "new-a"
    assert by_k[2].b == "new-b"


@pytest.mark.asyncio
async def test_bulk_get_or_create_matches_loose_typed_keys(sqlite_db):
    """
    GIVEN existing rows keyed by an int and a date column
    WHEN bulk_get_or_create receives the keys as strings (e.g. from JSON)
    THEN the rows are matched instead of silently duplicated
    """
    await AmKeyed.create(num=42, day=dt.date(2026, 1, 2), val="orig")

    results = await AmKeyed.bulk_get_or_create(
        [{"num": "42", "day": "2026-01-02", "val": "dup?"}], key_fields=["num", "day"]
    )
    assert [created for _, created in results] == [False]
    assert await AmKeyed.all().count() == 1
    assert results[0][0].val == "orig"


# -- get_or_none fast path honours Meta.ordering (finding 13) -------------------


@pytest.mark.asyncio
async def test_get_or_none_multi_match_follows_meta_ordering(sqlite_db):
    """
    GIVEN several rows matching a simple equality lookup
    WHEN get_or_none resolves via its fast path
    THEN it returns the same deterministic row as the queryset fallback path
    """
    await AmScored.create(name="x", score=1)
    await AmScored.create(name="x", score=9)
    await AmScored.create(name="x", score=5)

    fast = await AmScored.get_or_none(name="x")
    fallback = await AmScored.filter(name="x").first()
    assert fast.score == 9
    assert fast.id == fallback.id


# -- BooleanField string coercion (finding 14) ----------------------------------


@pytest.mark.asyncio
async def test_boolean_field_coerces_strings_semantically(sqlite_db):
    """
    GIVEN boolean values supplied as strings
    WHEN they are bound and persisted
    THEN "false"/"0" store False, "true"/"1" store True, junk raises ValueError
    """
    field = fields.BooleanField()
    assert field.to_db("false") is False
    assert field.to_db("0") is False
    assert field.to_db(" F ") is False
    assert field.to_db("TRUE") is True
    assert field.to_db("1") is True
    assert field.to_db(1) is True
    assert field.to_db(0) is False
    assert field.to_db(None) is None
    with pytest.raises(ValueError, match="Invalid boolean string"):
        field.to_db("bogus")

    row = await AmFlagged.create(active="false")
    fresh = await AmFlagged.get(id=row.id)
    assert fresh.active is False


# -- NumericValidator finiteness (finding 15) ------------------------------------


def test_numeric_validator_rejects_non_finite_values():
    """
    GIVEN nan / infinity spellings and values
    WHEN NumericValidator runs
    THEN non-finite input raises while ordinary numbers pass
    """
    validator = NumericValidator()
    validator("12.5")
    validator(3)
    validator(Decimal("1e10"))
    for bad in ("nan", "inf", "Infinity", "-inf", Decimal("Infinity"), float("nan")):
        with pytest.raises(ValidationError):
            validator(bad)
    with pytest.raises(ValidationError):
        validator("not-a-number")


# -- unsaved instances are unhashable (finding 16) --------------------------------


@pytest.mark.asyncio
async def test_unsaved_instances_are_unhashable(sqlite_db):
    """
    GIVEN an unsaved instance (no primary key yet)
    WHEN it is hashed before and after save()
    THEN hashing raises TypeError until the primary key exists
    """
    doc = AmDoc(title="x")
    with pytest.raises(TypeError, match="unhashable"):
        hash(doc)
    with pytest.raises(TypeError):
        {doc}  # noqa: B018 - the set literal itself must raise

    await doc.save()
    assert doc in {doc}
    again = await AmDoc.get(id=doc.id)
    assert hash(doc) == hash(again)

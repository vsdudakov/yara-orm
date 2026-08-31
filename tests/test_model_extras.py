"""Model-level query shortcuts, clone/describe, Meta.constraints, FK db_constraint,
the Random function and the extra validators (reference parity)."""

import datetime

import pytest

from yara_orm import (
    CheckConstraint,
    IntegrityError,
    Model,
    Random,
    UniqueConstraint,
    ValidationError,
    fields,
    timezone,
)
from yara_orm.dialects import SqliteDialect
from yara_orm.validators import CommaSeparatedIntegerListValidator, NumericValidator


class MxTag(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)
    value = fields.IntField(default=0)
    payload = fields.JSONField(null=True, default=dict)  # callable default

    class Meta:
        table = "mx_tag"


class MxConstrained(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)
    age = fields.IntField(default=0)

    class Meta:
        table = "mx_constrained"
        constraints = [
            CheckConstraint(check="age >= 0", name="mx_age_nonneg"),
            UniqueConstraint(fields=["name"], name="mx_uq_name"),
        ]


class MxRef(Model):
    id = fields.IntField(pk=True)
    tag = fields.ForeignKeyField("MxTag", related_name="refs", db_constraint=False)

    class Meta:
        table = "mx_ref"


class MxStamped(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "mx_stamped"
        extra_kwargs = "store"


MODELS = [MxTag, MxConstrained, MxRef, MxStamped]


# -- Model-level query shortcuts ----------------------------------------------


@pytest.mark.asyncio
async def test_model_first_last(db):
    """
    GIVEN several rows
    WHEN first()/last() are called on the model
    THEN the first and last rows by pk are returned (None when empty)
    """
    assert await MxTag.first() is None
    a = await MxTag.create(name="a", value=1)
    b = await MxTag.create(name="b", value=2)
    assert (await MxTag.first()).pk == a.pk
    assert (await MxTag.last()).pk == b.pk


@pytest.mark.asyncio
async def test_model_earliest_latest(db):
    """
    GIVEN rows with differing values
    WHEN earliest()/latest() order by a field
    THEN the min/max rows are returned
    """
    await MxTag.create(name="a", value=5)
    await MxTag.create(name="b", value=1)
    assert (await MxTag.earliest("value")).value == 1
    assert (await MxTag.latest("value")).value == 5


@pytest.mark.asyncio
async def test_model_exists(db):
    """
    GIVEN a model
    WHEN exists() is called with and without lookups
    THEN it reports presence correctly
    """
    assert await MxTag.exists() is False
    await MxTag.create(name="a")
    assert await MxTag.exists() is True
    assert await MxTag.exists(name="a") is True
    assert await MxTag.exists(name="z") is False


@pytest.mark.asyncio
async def test_model_values_and_values_list(db):
    """
    GIVEN rows
    WHEN values()/values_list() are called on the model
    THEN dict and tuple/scalar projections come back
    """
    await MxTag.create(name="a", value=1)
    assert await MxTag.values("name") == [{"name": "a"}]
    assert await MxTag.values_list("name", flat=True) == ["a"]
    assert await MxTag.values_list("name", "value") == [("a", 1)]


@pytest.mark.asyncio
async def test_model_distinct_and_select_for_update(db):
    """
    GIVEN a model
    WHEN distinct()/select_for_update() are called on the model
    THEN they return chainable query sets
    """
    await MxTag.create(name="a")
    assert len(await MxTag.distinct()) == 1
    qs = MxTag.select_for_update(nowait=True)
    assert qs._for_update and qs._for_update_nowait
    assert len(await qs) == 1


# -- clone() ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clone_creates_new_row(db):
    """
    GIVEN a persisted instance
    WHEN it is cloned and saved
    THEN a new row with a new pk and the copied fields is created
    """
    a = await MxTag.create(name="orig", value=7)
    clone = a.clone()
    assert clone._in_db is False
    clone.name = "copy"
    await clone.save()
    assert clone.pk != a.pk and clone.value == 7
    assert await MxTag.all().count() == 2


@pytest.mark.asyncio
async def test_clone_with_explicit_pk(db):
    """
    GIVEN a persisted instance
    WHEN cloned with an explicit pk
    THEN the clone carries that primary key
    """
    a = await MxTag.create(name="orig")
    clone = a.clone(pk=999)
    assert clone.id == 999


# -- describe() ---------------------------------------------------------------


def test_describe_structure():
    """
    GIVEN a model
    WHEN describe() is called
    THEN it returns a structured schema description
    """
    d = MxConstrained.describe()
    assert d["name"] == "MxConstrained"
    assert d["table"] == "mx_constrained"
    assert d["pk_field"] == "id"
    names = {f["name"] for f in d["data_fields"]}
    assert {"id", "name", "age"} <= names
    assert "fk_fields" in d and "m2m_fields" in d


def test_describe_default_value_branches():
    """
    GIVEN fields with simple and callable defaults
    WHEN describe() is called
    THEN simple defaults are reported and non-simple ones become None
    """
    by_name = {f["name"]: f for f in MxTag.describe()["data_fields"]}
    assert by_name["value"]["default"] == 0  # simple default kept
    assert by_name["payload"]["default"] is None  # callable default -> None


# -- FK db_constraint ---------------------------------------------------------


def test_fk_db_constraint_false_omits_foreign_key():
    """
    GIVEN a FK declared with db_constraint=False
    WHEN the table DDL is rendered
    THEN no FOREIGN KEY clause is emitted (the column still exists)
    """
    ddl = " ".join(SqliteDialect().create_table_sql(MxRef._meta))
    assert "FOREIGN KEY" not in ddl
    assert '"tag_id"' in ddl


def test_fk_db_constraint_flag_defaults_true():
    """
    GIVEN ForeignKeyField
    WHEN db_constraint is left default vs set False
    THEN the flag reflects the choice (True by default)
    """
    assert fields.ForeignKeyField("MxTag").db_constraint is True
    assert MxRef._meta.get_field("tag_id").db_constraint is False


# -- Meta.constraints ---------------------------------------------------------


def test_meta_constraints_render_in_ddl():
    """
    GIVEN Meta.constraints with a check and a unique constraint
    WHEN the table DDL is rendered
    THEN both constraint clauses appear
    """
    ddl = " ".join(SqliteDialect().create_table_sql(MxConstrained._meta))
    assert "CHECK (age >= 0)" in ddl
    assert 'UNIQUE ("name")' in ddl


@pytest.mark.asyncio
async def test_meta_check_constraint_enforced(db):
    """
    GIVEN a CHECK constraint from Meta.constraints
    WHEN a violating row is inserted
    THEN the database rejects it
    """
    with pytest.raises(IntegrityError):
        await MxConstrained.create(name="x", age=-1)


@pytest.mark.asyncio
async def test_meta_unique_constraint_enforced(db):
    """
    GIVEN a UNIQUE constraint from Meta.constraints
    WHEN a duplicate value is inserted
    THEN the database rejects it
    """
    await MxConstrained.create(name="dup", age=1)
    with pytest.raises(IntegrityError):
        await MxConstrained.create(name="dup", age=2)


# -- Random function ----------------------------------------------------------


@pytest.mark.asyncio
async def test_random_function(db):
    """
    GIVEN rows
    WHEN annotated with Random() and ordered by it
    THEN the query runs and returns every row
    """
    await MxTag.create(name="a")
    await MxTag.create(name="b")
    rows = await MxTag.all().annotate(r=Random()).order_by("r")
    assert len(rows) == 2


def test_random_renders_sql():
    """
    GIVEN the Random function
    WHEN rendered per dialect
    THEN it produces RANDOM() (PostgreSQL/SQLite) and RAND() (MySQL)
    """
    from yara_orm.dialects import MySQLDialect, PostgresDialect

    assert Random().render_params(lambda n: n, PostgresDialect(), [], 1) == ("RANDOM()", 1)
    assert Random().render_params(lambda n: n, MySQLDialect(), [], 1) == ("RAND()", 1)


# -- validators ---------------------------------------------------------------


def test_numeric_validator():
    """
    GIVEN the NumericValidator
    WHEN numeric and non-numeric values are checked
    THEN only non-numeric values raise
    """
    NumericValidator()(12)
    NumericValidator()("12.5")
    with pytest.raises(ValidationError):
        NumericValidator()("abc")
    with pytest.raises(ValidationError):
        NumericValidator()(["not", "numeric"])


def test_comma_separated_integer_list_validator():
    """
    GIVEN the CommaSeparatedIntegerListValidator
    WHEN valid and invalid lists are checked
    THEN only malformed lists raise
    """
    CommaSeparatedIntegerListValidator()("1,2,3")
    CommaSeparatedIntegerListValidator()("-1,2,-3")
    with pytest.raises(ValidationError):
        CommaSeparatedIntegerListValidator()("1,x,3")
    with pytest.raises(ValidationError):
        CommaSeparatedIntegerListValidator()("1,,3")


# -- Construction semantics ----------------------------------------------------


@pytest.mark.asyncio
async def test_explicit_relation_id_wins_over_relation_object(db):
    """
    GIVEN a relation passed as an object alongside an explicit ``<name>_id``
    WHEN the instance is constructed
    THEN the explicit id is the one stored and persisted

    A factory's ``SubFactory`` declaration is evaluated even when the caller
    also passes the raw id; resolving the relation last would silently replace
    the id that was asked for.
    """
    wanted = await MxTag.create(name="wanted")
    other = await MxTag.create(name="other")

    ref = MxRef(tag=other, tag_id=wanted.id)
    assert ref.tag_id == wanted.id

    await ref.save()
    assert (await MxRef.get(id=ref.id)).tag_id == wanted.id


@pytest.mark.asyncio
async def test_explicit_relation_id_does_not_cache_a_mismatched_object(db):
    """
    GIVEN a relation object whose pk differs from the explicit ``<name>_id``
    WHEN the relation is awaited
    THEN the row the id names is fetched, not the object that was passed
    """
    wanted = await MxTag.create(name="wanted")
    other = await MxTag.create(name="other")

    ref = await MxRef.create(tag=other, tag_id=wanted.id)

    assert (await ref.tag).id == wanted.id


@pytest.mark.asyncio
async def test_relation_object_still_sets_the_id_when_no_explicit_id(db):
    """
    GIVEN only a relation object (the ordinary case)
    WHEN the instance is constructed
    THEN its id is taken from the object and the object is cached as prefetched
    """
    tag = await MxTag.create(name="only")

    ref = await MxRef.create(tag=tag)

    assert ref.tag_id == tag.id
    assert (await ref.tag) is tag


@pytest.mark.asyncio
async def test_auto_now_add_keeps_an_explicit_created_at(db):
    """
    GIVEN a row created with an explicit ``auto_now_add`` value
    WHEN it is inserted
    THEN the supplied stamp is kept, not replaced with the current time

    ``auto_now_add`` fills the column when nothing was set; backdated fixtures,
    imports and backfills supply their own.
    """
    # Derived from the ORM's clock so the value matches the session's ``use_tz``
    # awareness on every backend.
    backdated = timezone.now() - datetime.timedelta(days=365)

    row = await MxStamped.create(name="imported", created_at=backdated)

    assert row.created_at == backdated
    again = await MxStamped.get(id=row.id)
    # PostgreSQL hands back an aware UTC value even when ``use_tz`` is off, and
    # both stamps are UTC, so the persisted one is compared in its naive form.
    assert again.created_at.replace(tzinfo=None) == backdated.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_auto_now_add_fills_an_unset_created_at(db):
    """
    GIVEN a row created without a ``created_at``
    WHEN it is inserted
    THEN the column is stamped with the current time
    """
    before = timezone.now()

    row = await MxStamped.create(name="fresh")

    assert row.created_at >= before - datetime.timedelta(seconds=1)


@pytest.mark.asyncio
async def test_auto_now_always_stamps_even_when_supplied(db):
    """
    GIVEN a row created with an explicit ``auto_now`` value
    WHEN it is inserted
    THEN the column is stamped anyway — ``auto_now`` owns its value
    """
    backdated = timezone.now() - datetime.timedelta(days=365)

    row = await MxStamped.create(name="stamped", updated_at=backdated)

    assert row.updated_at > backdated


@pytest.mark.asyncio
async def test_model_instance_iterates_as_name_value_pairs(db):
    """
    GIVEN a saved instance, including an attribute kept by ``extra_kwargs``
    WHEN it is iterated (``dict(instance)``)
    THEN every column comes back as a ``(name, value)`` pair, extras included,
         and private attributes are omitted
    """
    row = await MxStamped.create(name="iterable", label="extra")

    as_dict = dict(row)

    assert as_dict["id"] == row.id
    assert as_dict["name"] == "iterable"
    assert as_dict["created_at"] == row.created_at
    assert as_dict["label"] == "extra"
    assert not [key for key in as_dict if key.startswith("_")]


@pytest.mark.asyncio
async def test_deferred_column_is_skipped_by_iteration(db):
    """
    GIVEN an instance fetched with ``only()``
    WHEN it is iterated
    THEN the columns it does not carry are skipped rather than fetched
    """
    await MxStamped.create(name="partial")

    row = await MxStamped.all().only("id", "name").first()

    assert dict(row).keys() == {"id", "name"}

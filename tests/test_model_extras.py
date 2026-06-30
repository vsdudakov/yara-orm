"""Model-level query shortcuts, clone/describe, Meta.constraints, FK db_constraint,
the Random function and the extra validators (reference parity)."""

import pytest

from yara_orm import (
    CheckConstraint,
    IntegrityError,
    Model,
    Random,
    UniqueConstraint,
    ValidationError,
    fields,
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


MODELS = [MxTag, MxConstrained, MxRef]


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
    WHEN rendered
    THEN it produces RANDOM()
    """
    assert Random().render(lambda n: n) == "RANDOM()"


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

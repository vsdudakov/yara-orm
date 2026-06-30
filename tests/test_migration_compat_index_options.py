"""Custom index options (``unique`` / ``USING`` / ``INCLUDE``) on ``Meta.indexes`` (P1-9).

Tortoise apps lean on ``BtreeIndex``/``GinIndex``-style declarative indexes that
carry a uniqueness flag, an access method (``USING gin``/``gist``/``btree``) and
covering columns (``INCLUDE (...)``). These tests pin that yara renders those
options in both schema paths -- ``generate_schemas`` (``get_schema_sql``) and the
migration round-trip -- that a unique composite index actually enforces
uniqueness, that ``USING``/``INCLUDE`` reach the PostgreSQL DDL while SQLite stays
valid by silently omitting them, and that re-running makemigrations on an
unchanged model produces no new ops.
"""

import pytest

from yara_orm import Index, IntegrityError, Model, YaraOrm, fields
from yara_orm.migrations import diff_states, model_state


class IdxThing(Model):
    id = fields.IntField(pk=True)
    email = fields.CharField(max_length=50)
    name = fields.CharField(max_length=50)
    tags = fields.JSONField()

    class Meta:
        table = "idx_thing"
        indexes = [
            Index(fields=["email"], unique=True),
            Index(fields=["tags"], using="gin"),
            Index(fields=["name"], include=["email"]),
        ]


MODELS = [IdxThing]


@pytest.mark.asyncio
async def test_unique_composite_index_enforces_uniqueness(db):
    """
    GIVEN a model whose Meta.indexes declares Index(unique=True) on a column
    WHEN the schema is generated and a duplicate value is inserted
    THEN the second insert raises IntegrityError (uniqueness is enforced)
    """
    await IdxThing.create(email="a@x.io", name="Ada", tags=["x"])
    await IdxThing.create(email="b@x.io", name="Bob", tags=["y"])  # distinct email
    with pytest.raises(IntegrityError):
        await IdxThing.create(email="a@x.io", name="Cara", tags=["z"])  # duplicate email


@pytest.mark.asyncio
async def test_using_and_include_render_in_schema_sql(db):
    """
    GIVEN a model declaring USING gin and INCLUDE (...) indexes
    WHEN its PostgreSQL schema DDL is rendered (and generate_schemas applied it)
    THEN the CREATE INDEX statements carry the USING method and INCLUDE columns
    """
    if db != "postgres":
        pytest.skip("USING / INCLUDE index clauses are PostgreSQL-only")
    sql = YaraOrm.get_schema_sql(models=MODELS)
    assert "USING gin" in sql
    assert 'INCLUDE ("email")' in sql
    assert "CREATE UNIQUE INDEX" in sql


@pytest.mark.asyncio
async def test_sqlite_omits_using_and_include_but_keeps_unique(db):
    """
    GIVEN the same model rendered for SQLite (which has no USING/INCLUDE syntax)
    WHEN its schema DDL is rendered (and generate_schemas applied it)
    THEN USING/INCLUDE are dropped while CREATE UNIQUE INDEX is still emitted
    """
    if db != "sqlite":
        pytest.skip("asserts the SQLite-only omission of USING / INCLUDE")
    sql = YaraOrm.get_schema_sql(models=MODELS)
    assert "USING" not in sql
    assert "INCLUDE" not in sql
    assert "CREATE UNIQUE INDEX" in sql


def test_index_options_round_trip_to_source():
    """
    GIVEN the CreateModel op generated for a model with custom index options
    WHEN it is rendered to migration source
    THEN the source reconstructs each index's unique/using/include options
    """
    ops = diff_states({"tables": {}}, model_state([IdxThing]))
    source = ops[0].to_source()
    assert "'unique': True" in source
    assert "'using': 'gin'" in source
    assert "'include': ['email']" in source


def test_index_options_autogenerate_is_idempotent():
    """
    GIVEN an initial migration generated for a model with custom index options
    WHEN its state is replayed and the model is diffed again
    THEN no further operations are produced (the options round-trip exactly)
    """
    target = model_state([IdxThing])
    recorded = {"tables": {}}
    for op in diff_states({"tables": {}}, target):
        op.apply_state(recorded)
    assert diff_states(recorded, model_state([IdxThing])) == []

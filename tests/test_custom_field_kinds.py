"""Custom field kinds registered via the public ``register_field_kind`` API.

Covers registration validation, per-dialect SQL type rendering, the module
``__getattr__`` round-trip (``fields.<ClassName>`` in generated migrations),
and the full makemigrations -> upgrade -> re-diff cycle on SQLite.
"""

import pytest

from yara_orm import (
    ConfigurationError,
    MigrationManager,
    Model,
    fields,
    register_field_kind,
    unregister_field_kind,
)
from yara_orm import (
    migrations as m,
)
from yara_orm.dialects import PostgresDialect, SqliteDialect


# ---------------------------------------------------------------------------
# Module-level registrations (the recommended pattern: register at import time
# of the module defining the field classes).
# ---------------------------------------------------------------------------
class VectorField(fields.Field):
    """A pgvector-style embedding column (TEXT on SQLite)."""

    field_kind = "vector"

    def __init__(self, dim: int = 3, **kwargs):
        super().__init__(**kwargs)
        self.type_params = {"dim": dim}


register_field_kind(
    "vector",
    field_cls=VectorField,
    sql={"postgres": "vector({dim})", "sqlite": "TEXT", "mysql": "LONGTEXT"},
)


class MoneyField(fields.Field):
    """A float-backed money column, safe to create on both test backends."""

    field_kind = "money"
    read_identity = False

    def to_python(self, value):
        return None if value is None else float(value)


register_field_kind(
    "money",
    field_cls=MoneyField,
    sql={
        "postgres": "DOUBLE PRECISION",
        "sqlite": "REAL",
        "mysql": "DOUBLE",
        "oracle": "FLOAT",
    },
)


class MoneyDoc(Model):
    id = fields.IntField(pk=True)
    amount = MoneyField()

    class Meta:
        table = "cfk_money"


MODELS = [MoneyDoc]


# Abstract: keeps the model out of the global registry so other modules'
# all-model ``generate_schemas()`` never renders ``vector(3)`` on PostgreSQL
# (pgvector is deliberately not required by the test environment).
class VecDoc(Model):
    id = fields.IntField(pk=True)
    emb = VectorField(dim=3)

    class Meta:
        abstract = True
        table = "cfk_docs"


class VecDocV2(Model):
    id = fields.IntField(pk=True)
    emb = VectorField(dim=4)

    class Meta:
        abstract = True
        table = "cfk_docs"


# ---------------------------------------------------------------------------
# Registration validation
# ---------------------------------------------------------------------------
def test_register_builtin_kind_collision_raises():
    """
    GIVEN a field class whose kind shadows a built-in kind
    WHEN it is registered
    THEN a ConfigurationError names the collision
    """

    class BadInt(fields.Field):
        field_kind = "int"

    with pytest.raises(ConfigurationError, match="built in"):
        register_field_kind("int", field_cls=BadInt, sql="INTEGER")


def test_register_non_field_class_raises():
    """
    GIVEN a class that is not a Field subclass (and a non-class value)
    WHEN either is registered
    THEN a ConfigurationError rejects it
    """
    with pytest.raises(ConfigurationError, match="Field subclass"):
        register_field_kind("cfk_bogus", field_cls=object, sql="TEXT")
    with pytest.raises(ConfigurationError, match="Field subclass"):
        register_field_kind("cfk_bogus", field_cls=VectorField(dim=1), sql="TEXT")


def test_register_mismatched_field_kind_raises():
    """
    GIVEN a Field subclass whose field_kind differs from the registered kind
    WHEN it is registered
    THEN a ConfigurationError reports the mismatch
    """
    with pytest.raises(ConfigurationError, match="does not match"):
        register_field_kind("cfk_other", field_cls=VectorField, sql="TEXT")


def test_register_empty_sql_raises():
    """
    GIVEN a registration without any SQL type template
    WHEN it is registered
    THEN a ConfigurationError demands a non-empty template
    """

    class NoSql(fields.Field):
        field_kind = "cfk_nosql"

    with pytest.raises(ConfigurationError, match="non-empty SQL"):
        register_field_kind("cfk_nosql", field_cls=NoSql, sql="")
    with pytest.raises(ConfigurationError, match="non-empty SQL"):
        register_field_kind("cfk_nosql", field_cls=NoSql, sql={})


def test_reregister_same_class_is_noop():
    """
    GIVEN an already-registered kind
    WHEN the same kind is registered again with the same class
    THEN the call is a silent no-op (import-time registration is idempotent)
    """
    register_field_kind(
        "vector",
        field_cls=VectorField,
        sql={"postgres": "vector({dim})", "sqlite": "TEXT"},
    )
    assert fields.VectorField is VectorField


def test_reregister_different_class_raises():
    """
    GIVEN an already-registered kind
    WHEN a different class is registered under the same kind
    THEN a ConfigurationError points at unregister_field_kind
    """

    class OtherVector(fields.Field):
        field_kind = "vector"

    with pytest.raises(ConfigurationError, match="already registered"):
        register_field_kind("vector", field_cls=OtherVector, sql="TEXT")


def test_register_duplicate_class_name_raises():
    """
    GIVEN a second kind whose class shares a registered class's name
    WHEN it is registered
    THEN a ConfigurationError rejects the ambiguous name (migration files
         resolve ``fields.<ClassName>`` by name)
    """
    homonym = type("VectorField", (fields.Field,), {"field_kind": "cfk_dup"})
    with pytest.raises(ConfigurationError, match="already registered"):
        register_field_kind("cfk_dup", field_cls=homonym, sql="TEXT")


def test_unregister_removes_module_attribute():
    """
    GIVEN a registered throwaway kind resolvable as fields.<ClassName>
    WHEN the kind is unregistered (twice)
    THEN the module attribute is gone and the second call is a no-op
    """

    class ThrowawayField(fields.Field):
        field_kind = "cfk_throwaway"

    register_field_kind("cfk_throwaway", field_cls=ThrowawayField, sql="TEXT")
    assert fields.ThrowawayField is ThrowawayField
    unregister_field_kind("cfk_throwaway")
    with pytest.raises(AttributeError):
        _ = fields.ThrowawayField
    unregister_field_kind("cfk_throwaway")  # idempotent


def test_module_getattr_unknown_attribute_raises():
    """
    GIVEN the fields module
    WHEN an unknown attribute is looked up
    THEN an AttributeError is raised (registry misses do not swallow typos)
    """
    with pytest.raises(AttributeError, match="NoSuchField"):
        _ = fields.NoSuchField


# ---------------------------------------------------------------------------
# SQL type rendering
# ---------------------------------------------------------------------------
def test_column_type_renders_per_dialect():
    """
    GIVEN a model column of a registered custom kind with per-dialect SQL
    WHEN its DDL is rendered on each dialect
    THEN PostgreSQL gets the template filled from type_params and SQLite its own
    """
    pg_sql = "\n".join(PostgresDialect().create_table_sql(VecDoc._meta))
    lite_sql = "\n".join(SqliteDialect().create_table_sql(VecDoc._meta))
    assert '"emb" vector(3) NOT NULL' in pg_sql
    assert '"emb" TEXT NOT NULL' in lite_sql


def test_missing_dialect_template_raises():
    """
    GIVEN a kind registered with SQL for only one dialect
    WHEN another dialect renders the column type
    THEN a ConfigurationError lists the dialects that are covered
    """

    class PgOnlyField(fields.Field):
        field_kind = "cfk_pgonly"

    register_field_kind("cfk_pgonly", field_cls=PgOnlyField, sql={"postgres": "TEXT"})
    try:
        assert PostgresDialect().column_type(PgOnlyField()) == "TEXT"
        with pytest.raises(ConfigurationError, match="no SQL type template for dialect"):
            SqliteDialect().column_type(PgOnlyField())
    finally:
        unregister_field_kind("cfk_pgonly")


def test_unregistered_kind_raises_with_hint():
    """
    GIVEN a field whose kind is neither built in nor registered
    WHEN a dialect renders its column type
    THEN the ConfigurationError hints at register_field_kind
    """

    class MysteryField(fields.Field):
        field_kind = "cfk_mystery"

    with pytest.raises(ConfigurationError, match="register_field_kind"):
        SqliteDialect().column_type(MysteryField())


def test_template_placeholder_mismatch_raises():
    """
    GIVEN a registered kind whose SQL template names a missing type parameter
    WHEN the column type is rendered
    THEN a ConfigurationError shows the template and the actual type_params
    """

    class TypoField(fields.Field):
        field_kind = "cfk_typo"

        def __init__(self, dim: int = 3, **kwargs):
            super().__init__(**kwargs)
            self.type_params = {"dim": dim}

    register_field_kind("cfk_typo", field_cls=TypoField, sql="vector({dims})")
    try:
        with pytest.raises(ConfigurationError, match="does not match type_params"):
            PostgresDialect().column_type(TypoField(dim=3))
    finally:
        unregister_field_kind("cfk_typo")


# ---------------------------------------------------------------------------
# Migration source rendering
# ---------------------------------------------------------------------------
def test_default_migration_source_renders_type_params_and_flags():
    """
    GIVEN a custom-kind field with type params and column flags
    WHEN it is rendered into migration source
    THEN the default renderer emits fields.<ClassName>(<params>, <flags>)
    """
    field = VectorField(dim=2, null=True, unique=True, index=True)
    source = m.AddField("t", "emb", field).to_source()
    assert "fields.VectorField(dim=2, null=True, unique=True, index=True)" in source


def test_custom_source_callable_wins():
    """
    GIVEN a kind registered with a custom source callable
    WHEN a field of that kind is rendered into migration source
    THEN the callable's output is used verbatim
    """

    class SourcedField(fields.Field):
        field_kind = "cfk_sourced"

        def __init__(self, dim: int = 3, **kwargs):
            super().__init__(**kwargs)
            self.type_params = {"dim": dim}

    register_field_kind(
        "cfk_sourced",
        field_cls=SourcedField,
        sql="TEXT",
        source=lambda f: f"fields.SourcedField(**{f.type_params!r})",
    )
    try:
        source = m.AddField("t", "v", SourcedField(dim=5)).to_source()
        assert "fields.SourcedField(**{'dim': 5})" in source
    finally:
        unregister_field_kind("cfk_sourced")


def test_unregistered_kind_source_raises_with_hint():
    """
    GIVEN a field whose kind is neither built in nor registered
    WHEN the migration writer renders it as source
    THEN the ConfigurationError hints at register_field_kind
    """

    class MysteryField(fields.Field):
        field_kind = "cfk_mystery"

    with pytest.raises(ConfigurationError, match="register_field_kind"):
        m.AddField("t", "x", MysteryField()).to_source()


# ---------------------------------------------------------------------------
# Full migration cycle on SQLite
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_makemigrations_roundtrip_and_dim_diff(sqlite_empty, tmp_path):
    """
    GIVEN a model with a registered custom-kind column
    WHEN makemigrations writes the initial migration and it is applied
    THEN the file round-trips (loadable fields.VectorField source, applies on
         SQLite, re-run reports no changes) and a later dim change diffs to a
         single AlterField
    """
    mgr = MigrationManager(directory=str(tmp_path), models=[VecDoc])
    filename = mgr.make_migrations()
    assert filename == "0001_initial.py"
    text = (tmp_path / filename).read_text()
    assert "fields.VectorField(dim=3)" in text

    assert await mgr.upgrade() == ["0001_initial"]
    assert mgr.make_migrations() is None  # no spurious re-diff

    mgr_v2 = MigrationManager(directory=str(tmp_path), models=[VecDocV2])
    filename2 = mgr_v2.make_migrations()
    assert filename2 is not None
    text2 = (tmp_path / filename2).read_text()
    assert "m.AlterField('cfk_docs', 'emb'" in text2
    assert "fields.VectorField(dim=4)" in text2
    assert "old=fields.VectorField(dim=3)" in text2
    assert await mgr_v2.upgrade() == [filename2.removesuffix(".py")]
    assert mgr_v2.make_migrations() is None


# ---------------------------------------------------------------------------
# End-to-end CRUD through generate_schemas on both backends
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_custom_kind_crud_via_generated_schema(db):
    """
    GIVEN a schema generated for a model with a registered custom-kind column
    WHEN a row is created and fetched on each backend
    THEN the value round-trips through the custom field's conversions
    """
    row = await MoneyDoc.create(amount=12.5)
    fetched = await MoneyDoc.get(id=row.id)
    assert fetched.amount == pytest.approx(12.5)

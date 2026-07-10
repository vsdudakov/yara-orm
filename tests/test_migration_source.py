"""Migration source rendering: ``_fmt`` / ``_call`` / ``_field_source`` helpers
and per-operation ``to_source``.

These cover the readable serialisation of generated migrations: short specs stay
inline, long maps break out per item, fields render as ``fields.XxxField(...)``
calls, and — most importantly — everything the generator emits re-parses to an
equivalent operation (the round-trip property a migration file relies on).
"""

import ast

import pytest

from yara_orm import MigrationManager, Model, fields
from yara_orm import migrations as m
from yara_orm.migrations import diff


def _roundtrip(op: m.Operation) -> m.Operation:
    """Render an op to source, re-evaluate it, and return the rebuilt op.

    Asserts the source is syntactically valid and stable under a second render
    (so the rebuilt op serialises identically to the original).
    """
    src = op.to_source()
    ast.parse(src)  # raises SyntaxError if the rendering is malformed
    rebuilt = eval(src, {"m": m, "fields": fields})  # noqa: S307 - generator-produced
    assert rebuilt.to_source() == src
    return rebuilt


# -- _fmt -------------------------------------------------------------------
def test_fmt_short_values_stay_inline():
    """
    GIVEN scalars and short containers under the wrap threshold
    WHEN they are formatted with ``_fmt``
    THEN each renders as a plain ``repr`` on a single line
    """
    assert m._fmt("hello") == "'hello'"
    assert m._fmt(42) == "42"
    assert m._fmt(None) == "None"
    assert m._fmt({}) == "{}"
    assert m._fmt([]) == "[]"


def test_fmt_non_container_never_wraps_even_when_long():
    """
    GIVEN a scalar (e.g. a big SQL string) whose repr exceeds ``_WRAP``
    WHEN it is formatted with ``_fmt``
    THEN it is emitted as its repr without being broken across lines
    """
    long_sql = "SELECT " + ", ".join(f"col_{i}" for i in range(80))
    assert len(repr(long_sql)) > m._WRAP
    out = m._fmt(long_sql)
    assert "\n" not in out
    assert out == repr(long_sql)


def test_fmt_long_dict_and_list_break_one_item_per_line():
    """
    GIVEN a dict and a list whose single-line forms exceed ``_WRAP``
    WHEN formatted with ``_fmt``
    THEN each expands to one item per line and re-evaluates to the original
    """
    mapping = {f"key_{i}": f"value_number_{i}" for i in range(20)}
    out = m._fmt(mapping, indent=0)
    assert out.startswith("{\n")
    assert eval(out) == mapping  # noqa: S307

    items = [f"column_number_{i}" for i in range(40)]
    out = m._fmt(items, indent=0)
    assert out.startswith("[\n") and out.rstrip().endswith("]")
    assert eval(out) == items  # noqa: S307


def test_fmt_indentation_is_relative_to_argument():
    """
    GIVEN a long list formatted with a non-zero ``indent``
    WHEN ``_fmt`` wraps it across lines
    THEN the closing bracket aligns to ``indent`` and contents sit four deeper
    """
    items = [f"value_number_{i}" for i in range(40)]
    out = m._fmt(items, indent=8)
    lines = out.splitlines()
    assert lines[0] == "["
    assert lines[1].startswith(" " * 12)  # inner = indent + 4
    assert lines[-1] == " " * 8 + "]"  # closing bracket at indent


# -- _call ------------------------------------------------------------------
def test_call_short_stays_on_one_line():
    """
    GIVEN a call whose single-line form fits within ``_WRAP``
    WHEN it is rendered with ``_call``
    THEN it stays on one line, unwrapped
    """
    assert m._call("m.RemoveIndex", ["'t'", "'col'"]) == "m.RemoveIndex('t', 'col')"


def test_call_long_wraps_one_arg_per_line():
    """
    GIVEN a call whose single-line form exceeds ``_WRAP``
    WHEN it is rendered with ``_call``
    THEN each argument is placed on its own line, indented eight spaces
    """
    args = [repr(f"argument_value_{i}") for i in range(20)]
    out = m._call("m.SomeOp", args)
    assert out.startswith("m.SomeOp(\n")
    assert out.endswith("\n    )")
    assert out.count("\n        ") == len(args)


def test_call_wraps_when_an_argument_is_already_multiline():
    """
    GIVEN an otherwise short call where one argument already contains a newline
    WHEN it is rendered with ``_call``
    THEN the whole call is wrapped across lines
    """
    multiline_arg = "fields={\n            'id': fields.IntField(pk=True),\n        }"
    out = m._call("m.CreateModel", ["'t'", multiline_arg])
    assert out.startswith("m.CreateModel(\n")


# -- _field_source ----------------------------------------------------------
def test_field_source_scalar_variants():
    """
    GIVEN scalar fields with various options
    WHEN rendered with ``_field_source``
    THEN the constructor call carries the schema-relevant arguments
    """
    assert m._field_source(fields.IntField(pk=True)) == "fields.IntField(pk=True)"
    assert m._field_source(fields.IntField(null=True)) == "fields.IntField(null=True)"
    assert m._field_source(fields.CharField(max_length=100)) == "fields.CharField(max_length=100)"
    assert (
        m._field_source(fields.CharField(max_length=20, unique=True, index=True))
        == "fields.CharField(max_length=20, unique=True, index=True)"
    )
    assert (
        m._field_source(fields.DecimalField(max_digits=8, decimal_places=3))
        == "fields.DecimalField(max_digits=8, decimal_places=3)"
    )
    assert m._field_source(fields.TextField()) == "fields.TextField()"


def test_field_source_enum_fields_render_as_scalar_equivalents():
    """
    GIVEN enum-backed fields whose DDL matches a plain scalar
    WHEN rendered with ``_field_source``
    THEN they render as the canonical scalar field (no user-enum import needed)
    """
    from enum import Enum, IntEnum

    class Color(IntEnum):
        RED = 1

    class Size(str, Enum):
        S = "s"

    assert m._field_source(fields.IntEnumField(Color)) == "fields.IntField()"
    assert (
        m._field_source(fields.CharEnumField(Size, max_length=8))
        == "fields.CharField(max_length=8)"
    )


def test_field_source_foreign_keys():
    """
    GIVEN foreign-key and one-to-one fields
    WHEN rendered with ``_field_source``
    THEN reference, non-default on_delete and flags are emitted
    """
    assert m._field_source(fields.ForeignKeyField("User")) == "fields.ForeignKeyField('User')"
    assert (
        m._field_source(
            fields.ForeignKeyField("User", on_delete=fields.OnDelete.SET_NULL, null=True)
        )
        == "fields.ForeignKeyField('User', on_delete='SET NULL', null=True)"
    )
    assert (
        m._field_source(fields.ForeignKeyField("User", unique=True, index=True))
        == "fields.ForeignKeyField('User', unique=True, index=True)"
    )
    assert m._field_source(fields.OneToOneField("User")) == "fields.OneToOneField('User')"


def test_fields_source_empty_is_braces():
    """
    GIVEN an empty field mapping
    WHEN rendered with ``_fields_source``
    THEN it renders as an empty dict literal
    """
    assert m._fields_source({}, 8) == "{}"


# -- constraint definitions -------------------------------------------------
def test_constraint_definitions_render_to_spec_and_source():
    """
    GIVEN unique and check constraint definitions
    WHEN their spec and source are rendered
    THEN the spec mapping and constructor source are produced
    """
    uniq = m.UniqueConstraint(fields=["a", "b"], name="uq")
    assert uniq.to_spec() == {"kind": "unique", "name": "uq", "fields": ["a", "b"]}
    assert uniq.to_source() == "m.UniqueConstraint(fields=['a', 'b'], name='uq')"

    check = m.CheckConstraint(check="a > 0", name="ck")
    assert check.to_spec() == {"kind": "check", "name": "ck", "check": "a > 0"}
    assert check.to_source() == "m.CheckConstraint(check='a > 0', name='ck')"


def test_constraint_base_is_abstract():
    """
    GIVEN the constraint base class
    WHEN to_spec / to_source are called
    THEN they raise NotImplementedError (subclasses must override)
    """
    base = m.Constraint(name="x")
    with pytest.raises(NotImplementedError):
        base.to_spec()
    with pytest.raises(NotImplementedError):
        base.to_source()


# -- to_source per operation, with round-trip -------------------------------
def test_create_model_roundtrip_with_fk_and_index():
    """
    GIVEN a CreateModel op with a pk, an indexed column and a foreign key
    WHEN it is rendered to source and re-evaluated
    THEN it renders multi-line and rebuilds with all fields intact
    """
    op = m.CreateModelIfNotExists(
        "post",
        fields={
            "id": fields.IntField(pk=True),
            "title": fields.CharField(max_length=200, index=True),
            "author_id": fields.ForeignKeyField("User"),
        },
    )
    rebuilt = _roundtrip(op)
    assert rebuilt.table == "post"
    assert set(rebuilt.fields) == {"id", "title", "author_id"}
    assert "fields={\n" in op.to_source()


def test_create_model_composite_pk_roundtrip():
    """
    GIVEN CreateModel ops with and without a composite primary key
    WHEN each is rendered to source and round-tripped
    THEN composite_pk is only emitted when set and round-trips intact
    """
    plain = m.CreateModel("t", fields={"id": fields.IntField(pk=True)})
    assert "composite_pk" not in plain.to_source()

    joined = m.CreateModel(
        "j",
        fields={"a_id": fields.ForeignKeyField("A"), "b_id": fields.ForeignKeyField("B")},
        composite_pk=["a_id", "b_id"],
    )
    rebuilt = _roundtrip(joined)
    assert rebuilt.composite_pk == ["a_id", "b_id"]


@pytest.mark.parametrize(
    "op",
    [
        m.DeleteModelIfExists("t", fields={"id": fields.IntField(pk=True)}),
        m.DeleteModel(
            "j",
            fields={"a_id": fields.ForeignKeyField("A"), "b_id": fields.ForeignKeyField("B")},
            composite_pk=["a_id", "b_id"],
        ),
        m.AddFieldIfNotExists("t", "c", fields.IntField(null=True)),
        m.RemoveFieldIfExists("t", "c", fields.IntField()),
        m.AlterField(
            "t", "c", fields.CharField(max_length=200), old=fields.CharField(max_length=100)
        ),
        m.AddIndexIfNotExists("t", "c"),
        m.AddIndexConcurrently("t", "c"),
        m.AddUniqueIndexConcurrently("t", "c"),
        m.RemoveIndexIfExists("t", "c"),
        m.RemoveIndexConcurrently("t", "c"),
        m.RenameModel("old_t", "new_t"),
        m.RenameField("t", "old_c", "new_c"),
        m.RenameIndex("t", "c", "idx_old", "idx_new"),
        m.RenameIndex("t", "c", "idx_old", "idx_new", unique=True),
        m.AddConstraint("t", m.UniqueConstraint(fields=["a", "b"], name="uq_t")),
        m.AddConstraint("t", m.CheckConstraint(check="a > 0", name="ck_t")),
        m.RemoveConstraint("t", m.UniqueConstraint(fields=["a"], name="uq_t")),
        m.RenameConstraint("t", "uq_old", "uq_new"),
        m.RunSQL("SELECT 1", reverse_sql="SELECT 2"),
        m.RunSQL(["A", "B"]),
        m.RunSQL("SELECT 1"),
    ],
)
def test_operation_roundtrip(op):
    """
    GIVEN any serialisable migration operation
    WHEN it is rendered to source and re-evaluated
    THEN the rebuilt op re-parses to an identical rendering
    """
    _roundtrip(op)


def test_short_ops_stay_on_one_line():
    """
    GIVEN compact operations (AddIndex, RemoveIndex, short RunSQL)
    WHEN they are rendered to source
    THEN each stays on a single line without being exploded across lines
    """
    assert "\n" not in m.AddIndex("t", "c").to_source()
    assert "\n" not in m.RemoveIndex("t", "c").to_source()
    assert "\n" not in m.RunSQL("SELECT 1").to_source()


def test_runsql_normalises_str_to_list_and_roundtrips():
    """
    GIVEN a RunSQL op built from a single SQL string
    WHEN it is rendered to source and round-tripped
    THEN the SQL is normalised to a list and that form is preserved on reload
    """
    op = m.RunSQL("SELECT 1")
    rebuilt = _roundtrip(op)
    assert rebuilt.sql == ["SELECT 1"]
    assert rebuilt.reverse_sql == []


def test_quotes_and_unicode_in_values_survive_roundtrip():
    """
    GIVEN a RunSQL op whose SQL contains quotes, newlines and unicode
    WHEN it is rendered to source and round-tripped
    THEN the SQL survives byte-for-byte
    """
    sql = "INSERT INTO t (s) VALUES ('O''Brien — café\n\t')"
    op = m.RunSQL(sql)
    rebuilt = _roundtrip(op)
    assert rebuilt.sql == [sql]


# -- full generated file ----------------------------------------------------
class SrcUser(Model):
    name = fields.CharField(max_length=100)

    class Meta:
        table = "src_user"


class SrcPost(Model):
    title = fields.CharField(max_length=200, index=True)
    author = fields.ForeignKeyField("SrcUser", related_name="posts")

    class Meta:
        table = "src_post"


def test_generated_migration_file_is_valid_python(tmp_path):
    """
    GIVEN a migration file generated for two related models
    WHEN the file is parsed, reloaded and its ops are round-tripped
    THEN it is valid Python defining a Migration class with real operations
    """
    mgr = MigrationManager(directory=str(tmp_path), app="src", models=[SrcUser, SrcPost])
    filename = mgr.make_migrations(name="initial")
    text = (tmp_path / filename).read_text()

    ast.parse(text)  # whole file is valid Python
    module = mgr._load_module(tmp_path / filename)
    assert issubclass(module.Migration, m.Migration)
    assert module.Migration.atomic is True
    assert module.Migration.dependencies == []
    assert [type(o).__name__ for o in module.Migration.operations] == [
        "CreateModelIfNotExists",
        "CreateModelIfNotExists",
    ]

    for op in module.Migration.operations:
        _roundtrip(op)

    # The wide field maps are broken out (readability is the point).
    assert "fields={\n" in text
    assert "from yara_orm import fields" in text


# -- _field_source emits the FK/O2O field's own pk flag ----------------------
class RfeUser(Model):
    id = fields.IntField(pk=True)

    class Meta:
        table = "rfe_user"


class RfeProfile(Model):
    user = fields.OneToOneField("RfeUser", pk=True)
    bio = fields.CharField(max_length=50, null=True)

    class Meta:
        table = "rfe_profile"


def test_o2o_pk_survives_field_source_roundtrip():
    """
    GIVEN a OneToOneField used as the model's primary key
    WHEN it is rendered by ``_field_source`` and the source is re-executed
    THEN the rebuilt field keeps pk=True (pre-fix the flag was dropped, so the
    migrated table was created with no PRIMARY KEY)
    """
    field = RfeProfile._meta.fields["user_id"]
    assert field.pk is True
    src = m._field_source(field)
    ast.parse(src)
    # The field's own flag lives inside the constructor call; the resolved_fk
    # wrapper's ``pk=`` names the TARGET model's pk column — a different thing.
    assert "fields.OneToOneField('RfeUser', pk=True)" in src
    assert "pk='id'" in src
    rebuilt = eval(src, {"m": m, "fields": fields})  # noqa: S307 - generator-produced
    assert rebuilt.pk is True
    assert rebuilt.is_o2o is True
    assert rebuilt.unique is True  # O2O default, not double-emitted


def test_o2o_pk_migration_state_converges():
    """
    GIVEN the live model state of an O2O-as-pk model
    WHEN every field is serialised to migration source and rebuilt
    THEN the rebuilt state produces a table spec with the pk intact and a diff
    against the live state yields no operations (no phantom AlterField loop)
    """
    live = diff.model_state([RfeUser, RfeProfile])
    rebuilt_tables = {}
    for tname, tstate in live["tables"].items():
        rebuilt_fields = {
            col: eval(m._field_source(f), {"m": m, "fields": fields})  # noqa: S307
            for col, f in tstate["fields"].items()
        }
        rebuilt_tables[tname] = {**tstate, "fields": rebuilt_fields}
    assert m._tspec(rebuilt_tables["rfe_profile"])["pk"] == "user_id"
    assert diff.diff_states({"tables": rebuilt_tables}, live) == []

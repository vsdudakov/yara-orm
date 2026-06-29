"""Migration source rendering: ``_fmt`` / ``_call`` helpers and ``to_source``.

These cover the readable multi-line serialisation of generated migrations: that
short specs stay on one line, long maps break out per item, and — most
importantly — that everything the generator emits re-parses to an equivalent
operation (the round-trip property a migration file silently relies on).
"""

import ast

import pytest

from yara_orm import MigrationManager, Model, fields
from yara_orm import migrations as m

# A realistic column spec, comfortably under the wrap threshold on its own.
INT = {
    "kind": "int",
    "type_params": {},
    "null": False,
    "unique": False,
    "pk": False,
    "auto_increment": False,
}
PK = {**INT, "pk": True, "auto_increment": True}
FK = {"table": "other", "pk": "id", "on_delete": "CASCADE"}


def _roundtrip(op: m.Operation) -> m.Operation:
    """Render an op to source, re-evaluate it, and return the rebuilt op.

    Asserts the source is syntactically valid and stable under a second render
    (so the rebuilt op serialises identically to the original).
    """
    src = op.to_source()
    ast.parse(src)  # raises SyntaxError if the rendering is malformed
    rebuilt = eval(src, {"m": m})  # noqa: S307 - trusted, generator-produced source
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
    assert m._fmt(True) == "True"
    assert m._fmt({}) == "{}"
    assert m._fmt([]) == "[]"
    assert m._fmt(INT) == repr(INT)  # < _WRAP, so single line


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


def test_fmt_long_dict_breaks_one_item_per_line():
    """
    GIVEN a dict whose single-line form is wider than ``_WRAP``
    WHEN it is formatted with ``_fmt``
    THEN it expands to one key per line and re-evaluates to the original dict
    """
    columns = {"id": PK, "name": INT, "email": INT, "age": INT}
    out = m._fmt(columns, indent=0)
    assert out.startswith("{\n")
    # Each top-level key sits on its own line at the inner indent.
    for key in columns:
        assert f"    {key!r}: " in out
    # Leaf specs are short enough to stay on a single line.
    assert "'kind': 'int'" in out and "\n        'kind'" not in out
    assert eval(out) == columns  # noqa: S307


def test_fmt_long_list_breaks_one_item_per_line():
    """
    GIVEN a list whose single-line form is wider than ``_WRAP``
    WHEN it is formatted with ``_fmt``
    THEN it expands to one element per line and re-evaluates to the original list
    """
    items = [f"column_number_{i}" for i in range(40)]
    out = m._fmt(items, indent=0)
    assert out.startswith("[\n") and out.rstrip().endswith("]")
    assert eval(out) == items  # noqa: S307


def test_fmt_indentation_is_relative_to_argument():
    """
    GIVEN a long dict formatted with a non-zero ``indent``
    WHEN ``_fmt`` wraps it across lines
    THEN the closing bracket aligns to ``indent`` and contents sit four spaces deeper
    """
    columns = {"id": PK, "name": INT, "email": INT}
    out = m._fmt(columns, indent=8)
    lines = out.splitlines()
    assert lines[0] == "{"
    assert lines[1].startswith(" " * 12 + "'id'")  # inner = indent + 4
    assert lines[-1] == " " * 8 + "}"  # closing bracket at indent


# -- _call ------------------------------------------------------------------
def test_call_short_stays_on_one_line():
    """
    GIVEN a call whose single-line form fits within ``_WRAP``
    WHEN it is rendered with ``_call``
    THEN it stays on one line, unwrapped
    """
    out = m._call("m.DropIndex", ["'t'", "'col'"])
    assert out == "m.DropIndex('t', 'col')"


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
    assert out.count("\n        ") == len(args)  # every arg indented 8 spaces


def test_call_wraps_when_an_argument_is_already_multiline():
    """
    GIVEN an otherwise short call where one argument already contains a newline
    WHEN it is rendered with ``_call``
    THEN the whole call is wrapped across lines
    """
    multiline_arg = "columns={\n            'id': 1,\n        }"
    out = m._call("m.CreateTable", ["'t'", multiline_arg])
    assert out.startswith("m.CreateTable(\n")


# -- to_source per operation, with round-trip -------------------------------
def test_create_table_roundtrip_with_fks_and_indexes():
    """
    GIVEN a CreateTable op with columns, pk, fks and indexes
    WHEN it is rendered to source and re-evaluated
    THEN it renders multi-line and rebuilds with all fields intact
    """
    op = m.CreateTable(
        "post",
        columns={"id": PK, "title": INT, "author_id": INT},
        pk="id",
        fks={"author_id": FK},
        indexes=["title"],
    )
    rebuilt = _roundtrip(op)
    assert rebuilt.table == "post"
    assert rebuilt.columns == {"id": PK, "title": INT, "author_id": INT}
    assert rebuilt.pk == "id"
    assert rebuilt.fks == {"author_id": FK}
    assert rebuilt.indexes == ["title"]
    # Long column map is actually broken out per line.
    assert "columns={\n" in op.to_source()


def test_create_table_composite_pk_roundtrip():
    """
    GIVEN CreateTable ops with and without a composite primary key
    WHEN each is rendered to source and round-tripped
    THEN composite_pk is only emitted when set and round-trips intact
    """
    plain = m.CreateTable("t", columns={"id": PK})
    assert "composite_pk" not in plain.to_source()

    joined = m.CreateTable("j", columns={"a": INT, "b": INT}, composite_pk=["a", "b"])
    rebuilt = _roundtrip(joined)
    assert rebuilt.composite_pk == ["a", "b"]


def test_create_table_empty_defaults_roundtrip():
    """
    GIVEN a CreateTable op with no fks or indexes supplied
    WHEN it is rendered to source and round-tripped
    THEN the missing fks/indexes serialise as empty containers, not ``None``
    """
    op = m.CreateTable("t", columns={"id": PK})
    rebuilt = _roundtrip(op)
    assert rebuilt.fks == {}
    assert rebuilt.indexes == []


@pytest.mark.parametrize(
    "op",
    [
        m.DropTable("t", spec={"columns": {"id": PK}, "pk": "id", "fks": {}, "indexes": []}),
        m.AddColumn("t", "c", spec=INT, fk=FK),
        m.AddColumn("t", "c", spec=INT, fk=None),
        m.DropColumn("t", "c", spec=INT, fk=None),
        m.CreateIndex("t", "c"),
        m.DropIndex("t", "c"),
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
    GIVEN compact operations (CreateIndex, DropIndex, short RunSQL)
    WHEN they are rendered to source
    THEN each stays on a single line without being exploded across lines
    """
    assert "\n" not in m.CreateIndex("t", "c").to_source()
    assert "\n" not in m.DropIndex("t", "c").to_source()
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


# -- special characters in rendered values ----------------------------------
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


def test_default_with_special_chars_in_column_spec_roundtrips():
    """
    GIVEN an AddColumn op whose column spec carries an awkward default value
    WHEN it is rendered to source and round-tripped
    THEN the spec stays faithful through the round-trip
    """
    spec = {**INT, "type_params": {"default": "a'b\"c\\d"}}
    op = m.AddColumn("t", "weird", spec=spec, fk=None)
    rebuilt = _roundtrip(op)
    assert rebuilt.spec == spec


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
    THEN it is valid Python that reloads into real ops with wide maps broken out
    """
    mgr = MigrationManager(directory=str(tmp_path), app="src", models=[SrcUser, SrcPost])
    filename = mgr.make_migrations(name="initial")
    text = (tmp_path / filename).read_text()

    ast.parse(text)  # whole file is valid Python
    module = mgr._load_module(tmp_path / filename)
    assert [type(o).__name__ for o in module.operations] == ["CreateTable", "CreateTable"]

    # The reloaded ops re-render identically to a fresh render — full round-trip.
    for op in module.operations:
        _roundtrip(op)

    # The wide column maps are broken out (readability is the point of the change).
    assert "columns={\n" in text

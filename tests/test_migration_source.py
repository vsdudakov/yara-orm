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
    """Scalars and short containers render as a plain ``repr`` (no wrapping)."""
    assert m._fmt("hello") == "'hello'"
    assert m._fmt(42) == "42"
    assert m._fmt(None) == "None"
    assert m._fmt(True) == "True"
    assert m._fmt({}) == "{}"
    assert m._fmt([]) == "[]"
    assert m._fmt(INT) == repr(INT)  # < _WRAP, so single line


def test_fmt_non_container_never_wraps_even_when_long():
    """A long scalar (e.g. a big SQL string) is never broken across lines."""
    long_sql = "SELECT " + ", ".join(f"col_{i}" for i in range(80))
    assert len(repr(long_sql)) > m._WRAP
    out = m._fmt(long_sql)
    assert "\n" not in out
    assert out == repr(long_sql)


def test_fmt_long_dict_breaks_one_item_per_line():
    """A dict wider than ``_WRAP`` expands to one key per line and re-evaluates."""
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
    """A list wider than ``_WRAP`` expands to one element per line."""
    items = [f"column_number_{i}" for i in range(40)]
    out = m._fmt(items, indent=0)
    assert out.startswith("[\n") and out.rstrip().endswith("]")
    assert eval(out) == items  # noqa: S307


def test_fmt_indentation_is_relative_to_argument():
    """Closing bracket aligns to ``indent``; contents sit four spaces deeper."""
    columns = {"id": PK, "name": INT, "email": INT}
    out = m._fmt(columns, indent=8)
    lines = out.splitlines()
    assert lines[0] == "{"
    assert lines[1].startswith(" " * 12 + "'id'")  # inner = indent + 4
    assert lines[-1] == " " * 8 + "}"  # closing bracket at indent


# -- _call ------------------------------------------------------------------
def test_call_short_stays_on_one_line():
    """A call whose single-line form fits within ``_WRAP`` is not wrapped."""
    out = m._call("m.DropIndex", ["'t'", "'col'"])
    assert out == "m.DropIndex('t', 'col')"


def test_call_long_wraps_one_arg_per_line():
    """A call exceeding ``_WRAP`` puts each argument on its own line."""
    args = [repr(f"argument_value_{i}") for i in range(20)]
    out = m._call("m.SomeOp", args)
    assert out.startswith("m.SomeOp(\n")
    assert out.endswith("\n    )")
    assert out.count("\n        ") == len(args)  # every arg indented 8 spaces


def test_call_wraps_when_an_argument_is_already_multiline():
    """Even a 'short' call wraps if one argument already contains a newline."""
    multiline_arg = "columns={\n            'id': 1,\n        }"
    out = m._call("m.CreateTable", ["'t'", multiline_arg])
    assert out.startswith("m.CreateTable(\n")


# -- to_source per operation, with round-trip -------------------------------
def test_create_table_roundtrip_with_fks_and_indexes():
    """CreateTable renders multi-line and rebuilds with all fields intact."""
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
    """A composite-pk table round-trips and only emits composite_pk when set."""
    plain = m.CreateTable("t", columns={"id": PK})
    assert "composite_pk" not in plain.to_source()

    joined = m.CreateTable("j", columns={"a": INT, "b": INT}, composite_pk=["a", "b"])
    rebuilt = _roundtrip(joined)
    assert rebuilt.composite_pk == ["a", "b"]


def test_create_table_empty_defaults_roundtrip():
    """Missing fks/indexes serialise as empty containers, not ``None``."""
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
    """Every serialisable operation re-parses to an identical rendering."""
    _roundtrip(op)


def test_short_ops_stay_on_one_line():
    """Compact operations are not needlessly exploded across lines."""
    assert "\n" not in m.CreateIndex("t", "c").to_source()
    assert "\n" not in m.DropIndex("t", "c").to_source()
    assert "\n" not in m.RunSQL("SELECT 1").to_source()


def test_runsql_normalises_str_to_list_and_roundtrips():
    """RunSQL stores SQL as lists; the rendering preserves that on reload."""
    op = m.RunSQL("SELECT 1")
    rebuilt = _roundtrip(op)
    assert rebuilt.sql == ["SELECT 1"]
    assert rebuilt.reverse_sql == []


# -- special characters in rendered values ----------------------------------
def test_quotes_and_unicode_in_values_survive_roundtrip():
    """SQL containing quotes/newlines/unicode round-trips byte-for-byte."""
    sql = "INSERT INTO t (s) VALUES ('O''Brien — café\n\t')"
    op = m.RunSQL(sql)
    rebuilt = _roundtrip(op)
    assert rebuilt.sql == [sql]


def test_default_with_special_chars_in_column_spec_roundtrips():
    """A column spec carrying an awkward default value stays faithful."""
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
    """A full generated migration file parses and reloads into real ops."""
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

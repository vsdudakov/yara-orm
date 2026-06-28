"""Coverage: remaining reachable branches."""

from enum import Enum

import pytest
from test_cov_extra import CvEAuthor, CvEBook, CvENoRel, CvETag

from yara_orm import Count, FieldError, MigrationManager, Model, fields
from yara_orm import migrations as m
from yara_orm.dialects import SqliteDialect


class CvNoThrough(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20)
    tags = fields.ManyToManyField("CvETag", related_name="nothrough")

    class Meta:
        table = "cov_nothrough"


class Colour(str, Enum):
    RED = "red"
    BLUE = "blue"


def test_char_enum_conversions():
    """
    GIVEN a CharEnumField
    WHEN to_db is called with an enum, a raw value and None
    THEN it serialises each to the stored string form
    """
    field = fields.CharEnumField(Colour, max_length=8)
    assert field.to_db(Colour.RED) == "red"
    assert field.to_db("blue") == "blue"
    assert field.to_db(None) is None


@pytest.mark.asyncio
async def test_annotate_unknown_relation_raises(sqlite_db):
    """
    GIVEN an aggregate over a name that is neither a column nor a relation
    WHEN the query compiles
    THEN a FieldError is raised
    """
    await CvEAuthor.create(name="a")
    with pytest.raises(FieldError):
        await CvEAuthor.annotate(x=Count("not_a_relation"))


@pytest.mark.asyncio
async def test_lookup_edges(sqlite_db):
    """
    GIVEN unusual filters
    WHEN using an unrecognised lookup suffix, a nested empty Q, and a raw-id
        relation update
    THEN the suffix is treated as a field name, empty Q is a no-op, and the
        relation update binds the raw id
    """
    from yara_orm import Q

    a = await CvEAuthor.create(name="a")
    b = await CvEAuthor.create(name="b")
    await CvEBook.create(title="t", author=a)

    with pytest.raises(FieldError):
        await CvEBook.filter(title__weird="x")
    assert await CvEBook.filter(Q(Q())).count() == 1
    # Filter by a relation name with a raw id (non-model value).
    assert await CvEBook.filter(author=a.id).count() == 1
    assert await CvEBook.filter(author=a).update(author=b.id) == 1
    assert await CvENoRel.all().count() == 0
    # Grouped projection with no explicit fields requested.
    grouped = await CvEBook.annotate(c=Count("id")).group_by("author_id").values()
    assert grouped and "c" in grouped[0]


@pytest.mark.asyncio
async def test_grouped_values_list(sqlite_db):
    """
    GIVEN an annotated, grouped query
    WHEN projected with values_list
    THEN tuple rows are returned
    """
    a = await CvEAuthor.create(name="a")
    await CvEBook.create(title="t", author=a)
    rows = await CvEBook.annotate(c=Count("id")).group_by("author_id").values_list("author_id", "c")
    assert rows[0] == (a.id, 1)


@pytest.mark.asyncio
async def test_m2m_without_explicit_through_and_direct_aiter(sqlite_db):
    """
    GIVEN a many-to-many field declared without an explicit through table
    WHEN it is used and its manager is iterated directly
    THEN default join-table/key names are derived and iteration works
    """
    obj = await CvNoThrough.create(name="n")
    tag = await CvETag.create(label="x")
    await obj.tags.add(tag)
    iterator = obj.tags.__aiter__()
    labels = [t.label async for t in iterator]
    assert labels == ["x"]


@pytest.mark.asyncio
async def test_no_pk_table_rendering():
    """
    GIVEN a CreateTable operation for a table without a primary key
    WHEN it is rendered
    THEN the primary-key clause is omitted
    """
    spec_col = {
        "kind": "int",
        "type_params": {},
        "null": True,
        "unique": False,
        "pk": False,
        "auto_increment": False,
    }
    sql = m.CreateTable("nopk", columns={"x": spec_col}).forward_sql(SqliteDialect())
    assert "PRIMARY KEY" not in sql[0]


class CvUpUser(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20)

    class Meta:
        table = "cov_up_user"


@pytest.mark.asyncio
async def test_upgrade_to_target_and_runpython_downgrade(sqlite_empty, tmp_path):
    """
    GIVEN two migrations including a RunPython data migration
    WHEN upgrading to a target and downgrading
    THEN upgrade stops at the target and RunPython.backward runs
    """
    mgr = MigrationManager(directory=str(tmp_path), app="up", models=[CvUpUser])
    mgr.make_migrations(name="initial")
    (tmp_path / "0002_py.py").write_text(
        "from yara_orm import migrations as m\n"
        "marks = []\n\n"
        "async def fwd():\n    marks.append('f')\n\n"
        "async def bwd():\n    marks.append('b')\n\n"
        "dependencies = ['0001_initial']\n"
        "operations = [m.RunPython(fwd, bwd)]\n"
    )
    # Upgrade only as far as the initial migration (target stops the loop).
    applied = await mgr.upgrade(target="0001_initial")
    assert applied == ["0001_initial"]
    # Now apply the rest, then downgrade it (running RunPython.backward).
    await mgr.upgrade()
    reverted = await mgr.downgrade(steps=1)
    assert reverted == ["0002_py"]

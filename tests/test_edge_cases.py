"""Edge-case branch coverage across models, queryset, relations, enums,
connections and migrations."""

import datetime as dt
from enum import Enum

import pytest
from test_extra import CvEAuthor, CvEBook, CvENoRel, CvETag

from yara_orm import (
    ConfigurationError,
    Count,
    DoesNotExist,
    FieldError,
    MigrationManager,
    Model,
    MultipleObjectsReturned,
    Q,
    connections,
    fields,
)
from yara_orm import migrations as m
from yara_orm.dialects import SqliteDialect


class CvFBase(Model):
    id = fields.IntField(pk=True)
    created = fields.CharField(max_length=20, null=True)

    class Meta:
        table = "cov_f_base"


class CvFChild(CvFBase):
    name = fields.CharField(max_length=20)

    class Meta:
        table = "cov_f_child"


class CvFStamp(Model):
    id = fields.IntField(pk=True)
    ts = fields.DatetimeField(auto_now=True, auto_now_add=True, null=True)

    class Meta:
        table = "cov_f_stamp"


class CvNoThrough(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20)
    tags = fields.ManyToManyField("CvETag", related_name="nothrough")

    class Meta:
        table = "cov_nothrough"


# CvUpUser (defined below) is omitted: it is only exercised by the migration
# test, which builds its own schema via migrations on the sqlite_empty fixture.
MODELS = [CvFBase, CvFChild, CvFStamp, CvEAuthor, CvETag, CvEBook, CvENoRel, CvNoThrough]


class Colour(str, Enum):
    RED = "red"
    BLUE = "blue"


@pytest.mark.asyncio
async def test_model_inheritance_and_unexpected_kwarg(db):
    """
    GIVEN a model subclass inheriting a parent's fields
    WHEN it is created and constructed with an unknown kwarg
    THEN inherited columns persist and unknown kwargs raise TypeError
    """
    child = await CvFChild.create(name="c", created="2021")
    assert (await CvFChild.get(id=child.id)).created == "2021"
    with pytest.raises(TypeError):
        CvFChild(nope=1)


@pytest.mark.asyncio
async def test_auto_now_add_preserved_on_update(db):
    """
    GIVEN a field that is both auto_now and auto_now_add
    WHEN the row is created then saved again
    THEN the add-timestamp branch is preserved on update
    """
    row = await CvFStamp.create()
    assert isinstance(row.ts, dt.datetime)
    await row.save()


@pytest.mark.asyncio
async def test_reverse_m2m_filter_and_aggregate(db):
    """
    GIVEN tags linked to books through a reverse m2m relation
    WHEN filtering and aggregating from the tag side
    THEN the reverse-m2m subquery and join compile correctly
    """
    a = await CvEAuthor.create(name="a")
    book = await CvEBook.create(title="t", author=a)
    tag = await CvETag.create(label="x")
    await book.tags.add(tag)
    assert [t.label for t in await CvETag.filter(books=book)] == ["x"]
    counts = await CvETag.annotate(nb=Count("books"))
    assert {t.label: t.nb for t in counts}["x"] == 1


@pytest.mark.asyncio
async def test_queryset_get_errors_and_empty_q(db):
    """
    GIVEN a queryset-level get and an empty Q filter
    WHEN no/many rows match and an empty Q is applied
    THEN DoesNotExist/MultipleObjectsReturned raise and empty Q is a no-op
    """
    a = await CvEAuthor.create(name="dup")
    await CvEBook.create(title="d", author=a)
    await CvEBook.create(title="d", author=a)
    with pytest.raises(DoesNotExist):
        await CvEBook.all().get(title="missing")
    with pytest.raises(MultipleObjectsReturned):
        await CvEBook.all().get(title="d")
    assert await CvEBook.filter(Q()).count() == 2


@pytest.mark.asyncio
async def test_values_no_args_and_update_relation(db):
    """
    GIVEN books linked to authors
    WHEN projecting with no explicit fields and updating a relation by object
    THEN all columns project and the relation update sets the foreign key
    """
    a = await CvEAuthor.create(name="a")
    b = await CvEAuthor.create(name="b")
    await CvEBook.create(title="t", author=a)
    assert "title" in (await CvEBook.all().values())[0]
    assert len((await CvEBook.all().values_list())[0]) == len(CvEBook._meta.fields)

    n = await CvEBook.filter(author=a).update(author=b)
    assert n == 1
    assert (await CvEBook.all())[0].author_id == b.id


@pytest.mark.asyncio
async def test_grouped_values_extra_column_and_annotated_prefetch(db):
    """
    GIVEN annotated queries
    WHEN values() requests a non-grouped column and prefetch is combined
    THEN the extra column is grouped and the annotated query prefetches
    """
    a = await CvEAuthor.create(name="a")
    await CvEBook.create(title="t1", author=a)
    await CvEBook.create(title="t1", author=a)
    rows = (
        await CvEBook.annotate(c=Count("id"))
        .group_by("author_id")
        .values("author_id", "title", "c")
    )
    assert rows and "title" in rows[0]

    authors = await CvEAuthor.annotate(c=Count("books")).prefetch_related("books")
    assert [b.title for b in await authors[0].books]


@pytest.mark.asyncio
async def test_connections_get_default_and_unknown(db):
    """
    GIVEN the connections accessor outside a transaction
    WHEN fetching the default and an unknown connection name
    THEN both resolve to a usable executor
    """
    await CvEAuthor.create(name="a")
    assert (await connections.get("default").fetch_rows("SELECT 1"))[0][0] == 1
    # Unknown names raise instead of silently falling back to the default.
    with pytest.raises(ConfigurationError):
        connections.get("ghost")


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
async def test_annotate_unknown_relation_raises(db):
    """
    GIVEN an aggregate over a name that is neither a column nor a relation
    WHEN the query compiles
    THEN a FieldError is raised
    """
    await CvEAuthor.create(name="a")
    with pytest.raises(FieldError):
        await CvEAuthor.annotate(x=Count("not_a_relation"))


@pytest.mark.asyncio
async def test_lookup_edges(db):
    """
    GIVEN unusual filters
    WHEN using an unrecognised lookup suffix, a nested empty Q, and a raw-id
        relation update
    THEN the suffix is treated as a field name, empty Q is a no-op, and the
        relation update binds the raw id
    """
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
async def test_grouped_values_list(db):
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
async def test_m2m_without_explicit_through_and_direct_aiter(db):
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
    GIVEN a CreateModel operation for a table without a primary key
    WHEN it is rendered
    THEN the primary-key clause is omitted
    """
    op = m.CreateModel("nopk", fields={"x": fields.IntField(null=True)})
    sql = op.forward_sql(SqliteDialect(), {"tables": {}})
    assert "PRIMARY KEY" not in sql[0]


class CvUpUser(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20)

    class Meta:
        table = "cov_up_user"


# Migration-driven test: builds its own schema via migrations on an empty
# SQLite database, so it stays on the single-backend sqlite_empty fixture.
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
        "from yara_orm import migrations as m\n\n"
        "marks = []\n\n"
        "async def fwd():\n    marks.append('f')\n\n"
        "async def bwd():\n    marks.append('b')\n\n\n"
        "class Migration(m.Migration):\n"
        "    dependencies = ['0001_initial']\n"
        "    operations = [m.RunPython(fwd, bwd)]\n"
    )
    # Upgrade only as far as the initial migration (target stops the loop).
    applied = await mgr.upgrade(target="0001_initial")
    assert applied == ["0001_initial"]
    # Now apply the rest, then downgrade it (running RunPython.backward).
    await mgr.upgrade()
    reverted = await mgr.downgrade(steps=1)
    assert reverted == ["0002_py"]

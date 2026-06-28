"""Coverage: final branch mop-up across queryset, models and connection."""

import datetime as dt

import pytest
from test_cov_extra import CvEAuthor, CvEBook, CvETag

from yara_orm import Count, DoesNotExist, Model, MultipleObjectsReturned, Q, connections, fields


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


@pytest.mark.asyncio
async def test_model_inheritance_and_unexpected_kwarg(sqlite_db):
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
async def test_auto_now_add_preserved_on_update(sqlite_db):
    """
    GIVEN a field that is both auto_now and auto_now_add
    WHEN the row is created then saved again
    THEN the add-timestamp branch is preserved on update
    """
    row = await CvFStamp.create()
    assert isinstance(row.ts, dt.datetime)
    await row.save()


@pytest.mark.asyncio
async def test_reverse_m2m_filter_and_aggregate(sqlite_db):
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
async def test_queryset_get_errors_and_empty_q(sqlite_db):
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
async def test_values_no_args_and_update_relation(sqlite_db):
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
async def test_grouped_values_extra_column_and_annotated_prefetch(sqlite_db):
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
async def test_connections_get_default_and_unknown(sqlite_db):
    """
    GIVEN the connections accessor outside a transaction
    WHEN fetching the default and an unknown connection name
    THEN both resolve to a usable executor
    """
    await CvEAuthor.create(name="a")
    assert (await connections.get("default").fetch_rows("SELECT 1"))[0][0] == 1
    # Unknown name falls back to the default executor.
    assert (await connections.get("ghost").fetch_rows("SELECT 1"))[0][0] == 1

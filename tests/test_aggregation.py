"""Aggregation: annotate, Count/Sum/Avg/Min/Max, group_by."""

import pytest

from yara_orm import Avg, Count, Max, Min, Model, Sum, YaraOrm, fields
from yara_orm.connection import get_engine


class AggAuthor(Model):
    name = fields.CharField(max_length=100)

    class Meta:
        table = "g_author"


class AggBook(Model):
    title = fields.CharField(max_length=100)
    rating = fields.IntField()
    author = fields.ForeignKeyField("AggAuthor", related_name="books")

    class Meta:
        table = "g_book"


async def _reset():
    engine = get_engine()
    for table in ("g_book", "g_author"):
        await engine.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    await YaraOrm.generate_schemas()


async def _seed():
    a = await AggAuthor.create(name="Ada")
    b = await AggAuthor.create(name="Bob")
    await AggBook.create(title="A1", rating=5, author=a)
    await AggBook.create(title="A2", rating=3, author=a)
    await AggBook.create(title="B1", rating=4, author=b)
    return a, b


@pytest.mark.asyncio
async def test_annotate_count_relation_and_filter(orm):
    """
    GIVEN Authors with differing numbers of Books
    WHEN annotating with Count("books") and filtering on the annotation
    THEN only Authors meeting the HAVING condition are returned with the count
    """
    await _reset()
    a, b = await _seed()
    await AggAuthor.create(name="Carol")  # no books

    rows = await AggAuthor.annotate(num=Count("books")).filter(num__gte=1).order_by("name")
    result = {r.name: r.num for r in rows}
    assert result == {"Ada": 2, "Bob": 1}


@pytest.mark.asyncio
async def test_group_by_values_sum(orm):
    """
    GIVEN Books grouped by author
    WHEN annotating Sum("rating") with group_by("author_id").values(...)
    THEN one aggregated dict per author is returned
    """
    await _reset()
    a, b = await _seed()

    rows = (
        await AggBook.annotate(total=Sum("rating"))
        .group_by("author_id")
        .values("author_id", "total")
    )
    by_author = {r["author_id"]: r["total"] for r in rows}
    assert by_author[a.id] == 8
    assert by_author[b.id] == 4


@pytest.mark.asyncio
async def test_group_by_count(orm):
    """
    GIVEN Books per author
    WHEN annotating Count("id") grouped by author_id
    THEN the per-group row counts are returned
    """
    await _reset()
    a, b = await _seed()
    rows = (
        await AggBook.annotate(count=Count("id")).group_by("author_id").values("author_id", "count")
    )
    counts = {r["author_id"]: r["count"] for r in rows}
    assert counts == {a.id: 2, b.id: 1}


@pytest.mark.asyncio
async def test_avg_min_max(orm):
    """
    GIVEN a set of AggBook ratings
    WHEN aggregating with Avg/Min/Max via values over a single group
    THEN the computed statistics match the data
    """
    await _reset()
    await _seed()
    [row] = (
        await AggBook.annotate(avg=Avg("rating"), lo=Min("rating"), hi=Max("rating"))
        .group_by()
        .values("avg", "lo", "hi")
    )
    assert round(float(row["avg"]), 2) == 4.0
    assert row["lo"] == 3
    assert row["hi"] == 5

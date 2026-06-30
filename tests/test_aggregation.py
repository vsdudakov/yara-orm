"""Aggregation: annotate, Count/Sum/Avg/Min/Max, group_by."""

import pytest

from yara_orm import Avg, Count, Max, Min, Model, Sum, fields


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


MODELS = [AggAuthor, AggBook]


async def _seed():
    a = await AggAuthor.create(name="Ada")
    b = await AggAuthor.create(name="Bob")
    await AggBook.create(title="A1", rating=5, author=a)
    await AggBook.create(title="A2", rating=3, author=a)
    await AggBook.create(title="B1", rating=4, author=b)
    return a, b


@pytest.mark.asyncio
async def test_annotate_count_relation_and_filter(db):
    """
    GIVEN Authors with differing numbers of Books
    WHEN annotating with Count("books") and filtering on the annotation
    THEN only Authors meeting the HAVING condition are returned with the count
    """
    a, b = await _seed()
    await AggAuthor.create(name="Carol")  # no books

    rows = await AggAuthor.annotate(num=Count("books")).filter(num__gte=1).order_by("name")
    result = {r.name: r.num for r in rows}
    assert result == {"Ada": 2, "Bob": 1}


@pytest.mark.asyncio
async def test_group_by_values_sum(db):
    """
    GIVEN Books grouped by author
    WHEN annotating Sum("rating") with group_by("author_id").values(...)
    THEN one aggregated dict per author is returned
    """
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
async def test_group_by_count(db):
    """
    GIVEN Books per author
    WHEN annotating Count("id") grouped by author_id
    THEN the per-group row counts are returned
    """
    a, b = await _seed()
    rows = (
        await AggBook.annotate(count=Count("id")).group_by("author_id").values("author_id", "count")
    )
    counts = {r["author_id"]: r["count"] for r in rows}
    assert counts == {a.id: 2, b.id: 1}


@pytest.mark.asyncio
async def test_avg_min_max(db):
    """
    GIVEN a set of AggBook ratings
    WHEN aggregating with Avg/Min/Max via values over a single group
    THEN the computed statistics match the data
    """
    await _seed()
    [row] = (
        await AggBook.annotate(avg=Avg("rating"), lo=Min("rating"), hi=Max("rating"))
        .group_by()
        .values("avg", "lo", "hi")
    )
    assert round(float(row["avg"]), 2) == 4.0
    assert row["lo"] == 3
    assert row["hi"] == 5


@pytest.mark.asyncio
async def test_annotated_values_list_flat_is_flattened(db):
    """
    GIVEN an annotated, grouped query
    WHEN values_list(flat=True) is taken over the grouped path
    THEN scalars are returned, not one-element tuples
    """
    ada = await AggAuthor.create(name="Ada")
    await AggBook.create(title="A", rating=3, author=ada)
    await AggBook.create(title="B", rating=5, author=ada)
    rows = await AggBook.annotate(c=Count("id")).group_by("author_id").values_list("c", flat=True)
    assert rows == [2]


@pytest.mark.asyncio
async def test_annotated_values_list_no_fields_returns_full_rows(db):
    """
    GIVEN an annotated, grouped query and no explicit fields
    WHEN values_list() is taken
    THEN the full grouped rows (group keys + annotations) are returned as tuples
    """
    ada = await AggAuthor.create(name="Ada")
    await AggBook.create(title="A", rating=3, author=ada)
    await AggBook.create(title="B", rating=5, author=ada)
    rows = await AggBook.annotate(c=Count("id")).group_by("author_id").values_list()
    assert rows == [(ada.id, 2)]


@pytest.mark.asyncio
async def test_annotated_values_list_flat_requires_single_field(db):
    """
    GIVEN an annotated, grouped query
    WHEN values_list(flat=True) names more than one field
    THEN a FieldError is raised
    """
    from yara_orm import FieldError

    ada = await AggAuthor.create(name="Ada")
    await AggBook.create(title="A", rating=3, author=ada)
    with pytest.raises(FieldError):
        await (
            AggBook.annotate(c=Count("id"))
            .group_by("author_id")
            .values_list("author_id", "c", flat=True)
        )

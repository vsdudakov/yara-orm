"""Corner cases for values()/values_list() projection and the awaitable /
async-iterable / first() behaviour of their lazy results.

Covers field subsets, all-fields default, relation-path traversal, aliasing,
naming a relation directly (pk projection), pk keyword, ordering, empty results,
flat scalars, tuples, multi-field, and streaming/single-row access — plus the
grouped variants layered on top.
"""

import pytest

from yara_orm import Count, FieldError, Model, Sum, fields


class AggvAuthor(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=30)

    class Meta:
        table = "aggv_author"


class AggvPublisher(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=30)

    class Meta:
        table = "aggv_publisher"


class AggvBook(Model):
    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=30)
    price = fields.IntField(default=0)
    author = fields.ForeignKeyField("AggvAuthor", related_name="books")
    publisher = fields.ForeignKeyField("AggvPublisher", related_name="books", null=True)

    class Meta:
        table = "aggv_book"


MODELS = [AggvAuthor, AggvPublisher, AggvBook]


async def _seed():
    """Seed authors, a publisher and three books.

    Returns:
        A mapping with the created ``ada``/``bob`` authors and ``pub``.
    """
    ada = await AggvAuthor.create(name="Ada")
    bob = await AggvAuthor.create(name="Bob")
    pub = await AggvPublisher.create(name="Acme")
    await AggvBook.create(title="A1", price=10, author=ada, publisher=pub)
    await AggvBook.create(title="A2", price=30, author=ada, publisher=None)
    await AggvBook.create(title="B1", price=20, author=bob, publisher=pub)
    return {"ada": ada, "bob": bob, "pub": pub}


# ---------------------------------------------------------------------------
# values() shape
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_values_subset_keys(db):
    """
    GIVEN seeded books
    WHEN values() names a subset of columns
    THEN each dict carries exactly those keys
    """
    await _seed()
    rows = await AggvBook.all().order_by("title").values("title")
    assert rows == [{"title": "A1"}, {"title": "A2"}, {"title": "B1"}]


@pytest.mark.asyncio
async def test_values_all_fields_default(db):
    """
    GIVEN a book row
    WHEN values() is called with no arguments
    THEN every model field (including the FK id columns) is a key
    """
    await _seed()
    [row] = await AggvBook.filter(title="A1").values()
    assert set(row) == {"id", "title", "price", "author_id", "publisher_id"}


@pytest.mark.asyncio
async def test_values_pk_keyword(db):
    """
    GIVEN books
    WHEN values("pk", ...) uses the pk alias
    THEN the primary key is projected under the "pk" key
    """
    seed = await _seed()
    row = await AggvBook.filter(title="A1").values("pk", "title")
    assert row[0]["title"] == "A1"
    assert isinstance(row[0]["pk"], int)
    assert seed


@pytest.mark.asyncio
async def test_values_relation_path(db):
    """
    GIVEN books linked to authors
    WHEN values() traverses one forward relation (author__name)
    THEN the dotted path is the dict key carrying the related value
    """
    await _seed()
    rows = await AggvBook.all().order_by("title").values("title", "author__name")
    assert rows == [
        {"title": "A1", "author__name": "Ada"},
        {"title": "A2", "author__name": "Ada"},
        {"title": "B1", "author__name": "Bob"},
    ]


@pytest.mark.asyncio
async def test_values_alias_key(db):
    """
    GIVEN a relation path
    WHEN it is aliased via a keyword argument
    THEN the clean alias replaces the dotted path key
    """
    await _seed()
    rows = await AggvBook.filter(title="A1").values("title", writer="author__name")
    assert rows == [{"title": "A1", "writer": "Ada"}]


@pytest.mark.asyncio
async def test_values_name_relation_projects_pk(db):
    """
    GIVEN books with a forward FK
    WHEN values() names the relation itself (not a path)
    THEN the relation's primary key is projected
    """
    seed = await _seed()
    rows = await AggvBook.all().order_by("title").values("title", "author")
    assert rows[0] == {"title": "A1", "author": seed["ada"].id}


@pytest.mark.asyncio
async def test_values_null_fk_traversal_is_none(db):
    """
    GIVEN a book whose publisher FK is NULL
    WHEN values() traverses that nullable relation (publisher__name)
    THEN the projected value is None (LEFT JOIN, no matching row)
    """
    await _seed()
    rows = await AggvBook.filter(title="A2").values("title", "publisher__name")
    assert rows == [{"title": "A2", "publisher__name": None}]


@pytest.mark.asyncio
async def test_values_ordering_respected(db):
    """
    GIVEN books with a price column
    WHEN values() is ordered by price descending
    THEN the dict rows come back in that order
    """
    await _seed()
    rows = await AggvBook.all().order_by("-price").values("title", "price")
    assert [r["price"] for r in rows] == [30, 20, 10]


@pytest.mark.asyncio
async def test_values_empty(db):
    """
    GIVEN a filter matching nothing
    WHEN values() runs
    THEN an empty list is returned
    """
    await _seed()
    rows = await AggvBook.filter(title="nope").values("title")
    assert rows == []


# ---------------------------------------------------------------------------
# values_list() shape
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_values_list_flat_scalars(db):
    """
    GIVEN books
    WHEN values_list(flat=True) names a single field
    THEN a flat list of scalars is returned (not one-element tuples)
    """
    await _seed()
    titles = await AggvBook.all().order_by("title").values_list("title", flat=True)
    assert titles == ["A1", "A2", "B1"]


@pytest.mark.asyncio
async def test_values_list_multi_field_tuples(db):
    """
    GIVEN books
    WHEN values_list names multiple fields
    THEN each row is a positional tuple in field order
    """
    await _seed()
    rows = await AggvBook.all().order_by("title").values_list("title", "price")
    assert rows == [("A1", 10), ("A2", 30), ("B1", 20)]


@pytest.mark.asyncio
async def test_values_list_relation_path(db):
    """
    GIVEN books linked to authors
    WHEN values_list traverses a relation path
    THEN the related column appears positionally in the tuple
    """
    await _seed()
    rows = await AggvBook.all().order_by("title").values_list("title", "author__name")
    assert rows == [("A1", "Ada"), ("A2", "Ada"), ("B1", "Bob")]


@pytest.mark.asyncio
async def test_values_list_flat_requires_single_field(db):
    """
    GIVEN a plain (non-grouped) query
    WHEN values_list(flat=True) names more than one field
    THEN a FieldError is raised
    """
    await _seed()
    with pytest.raises(FieldError):
        await AggvBook.all().values_list("title", "price", flat=True)


@pytest.mark.asyncio
async def test_values_list_all_fields_default(db):
    """
    GIVEN a single book
    WHEN values_list() is called with no field names
    THEN a tuple covering every model field is returned
    """
    await _seed()
    rows = await AggvBook.filter(title="A1").values_list()
    assert len(rows) == 1
    # id, title, price, author_id, publisher_id -> 5 columns.
    assert len(rows[0]) == 5


@pytest.mark.asyncio
async def test_values_list_empty_flat(db):
    """
    GIVEN a filter matching nothing
    WHEN values_list(flat=True) runs
    THEN an empty list is returned
    """
    await _seed()
    rows = await AggvBook.filter(title="nope").values_list("title", flat=True)
    assert rows == []


@pytest.mark.asyncio
async def test_values_list_order_by(db):
    """
    GIVEN books
    WHEN values_list is ordered by price ascending
    THEN scalars come back sorted by that order
    """
    await _seed()
    prices = await AggvBook.all().order_by("price").values_list("price", flat=True)
    assert prices == [10, 20, 30]


# ---------------------------------------------------------------------------
# Awaitable / async-iterable / first() on lazy results
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_values_is_async_iterable(db):
    """
    GIVEN a values() result
    WHEN it is streamed with ``async for``
    THEN each yielded item is a projected dict
    """
    await _seed()
    seen = []
    async for row in AggvBook.all().order_by("title").values("title"):
        seen.append(row["title"])
    assert seen == ["A1", "A2", "B1"]


@pytest.mark.asyncio
async def test_values_list_is_async_iterable_flat(db):
    """
    GIVEN a flat values_list() result
    WHEN it is streamed with ``async for``
    THEN scalars are yielded one by one
    """
    await _seed()
    seen = []
    async for title in AggvBook.all().order_by("title").values_list("title", flat=True):
        seen.append(title)
    assert seen == ["A1", "A2", "B1"]


@pytest.mark.asyncio
async def test_values_first_returns_single_dict(db):
    """
    GIVEN a values() result
    WHEN .first() is awaited
    THEN the first projected dict is returned
    """
    await _seed()
    row = await AggvBook.all().order_by("title").values("title", "price").first()
    assert row == {"title": "A1", "price": 10}


@pytest.mark.asyncio
async def test_values_list_first_flat_scalar(db):
    """
    GIVEN a flat values_list() result
    WHEN .first() is awaited
    THEN a single scalar (not a tuple) is returned
    """
    await _seed()
    title = await AggvBook.all().order_by("title").values_list("title", flat=True).first()
    assert title == "A1"


@pytest.mark.asyncio
async def test_values_first_empty_is_none(db):
    """
    GIVEN a values() result matching no rows
    WHEN .first() is awaited
    THEN None is returned
    """
    await _seed()
    row = await AggvBook.filter(title="nope").values("title").first()
    assert row is None


@pytest.mark.asyncio
async def test_values_list_first_empty_is_none(db):
    """
    GIVEN a values_list() result matching no rows
    WHEN .first() is awaited
    THEN None is returned
    """
    await _seed()
    val = await AggvBook.filter(title="nope").values_list("title", flat=True).first()
    assert val is None


@pytest.mark.asyncio
async def test_queryset_first_values_projection(db):
    """
    GIVEN a first() single-row result
    WHEN .values(...) is awaited on it
    THEN the single matched row is projected to a dict
    """
    await _seed()
    row = await AggvBook.filter(title="A1").first().values("title", writer="author__name")
    assert row == {"title": "A1", "writer": "Ada"}


@pytest.mark.asyncio
async def test_queryset_first_values_list_flat(db):
    """
    GIVEN a first() single-row result
    WHEN .values_list(flat=True) is awaited on it
    THEN the single field's scalar value is returned
    """
    await _seed()
    val = await AggvBook.filter(title="B1").first().values_list("price", flat=True)
    assert val == 20


# ---------------------------------------------------------------------------
# Grouped values()/values_list() layered on top
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_grouped_values_dict(db):
    """
    GIVEN books grouped by author
    WHEN grouped values() names the group key and annotation
    THEN one dict per group carries both
    """
    seed = await _seed()
    rows = (
        await AggvBook.annotate(n=Count("id"), total=Sum("price"))
        .group_by("author_id")
        .order_by("author_id")
        .values("author_id", "n", "total")
    )
    by_author = {r["author_id"]: r for r in rows}
    assert by_author[seed["ada"].id]["n"] == 2
    assert by_author[seed["ada"].id]["total"] == 40
    assert by_author[seed["bob"].id]["n"] == 1


@pytest.mark.asyncio
async def test_grouped_values_list_flat(db):
    """
    GIVEN a grouped query
    WHEN grouped values_list(flat=True) selects a single annotation
    THEN scalars are returned rather than one-element tuples
    """
    await _seed()
    totals = (
        await AggvBook.annotate(total=Sum("price"))
        .group_by("author_id")
        .order_by("author_id")
        .values_list("total", flat=True)
    )
    assert totals == [40, 20]


@pytest.mark.asyncio
async def test_grouped_values_list_no_fields_full_rows(db):
    """
    GIVEN a grouped query with no explicit projection
    WHEN values_list() is taken
    THEN full grouped rows (group keys + annotations) come back as tuples
    """
    seed = await _seed()
    rows = (
        await AggvBook.annotate(n=Count("id"))
        .group_by("author_id")
        .order_by("author_id")
        .values_list()
    )
    assert rows == [(seed["ada"].id, 2), (seed["bob"].id, 1)]


@pytest.mark.asyncio
async def test_grouped_values_first(db):
    """
    GIVEN a grouped values() result
    WHEN .first() is awaited
    THEN the first group dict is returned
    """
    seed = await _seed()
    row = (
        await AggvBook.annotate(n=Count("id"))
        .group_by("author_id")
        .order_by("author_id")
        .values("author_id", "n")
        .first()
    )
    assert row == {"author_id": seed["ada"].id, "n": 2}

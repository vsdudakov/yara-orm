"""values()/values_list() corner cases, multi-level relation traversal in
values(), GROUP BY edge cases, and random ordering (order_by("?")).

These exercise the dict/tuple projection paths and the multi-hop forward-FK
join resolution that ``values()`` relies on, across both backends.
"""

import pytest

from yara_orm import Avg, Count, Model, Sum, fields


class VtCountry(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20)

    class Meta:
        table = "vt_country"


class VtPublisher(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20)
    country = fields.ForeignKeyField("VtCountry", related_name="publishers", null=True)

    class Meta:
        table = "vt_publisher"


class VtAuthor(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20)
    publisher = fields.ForeignKeyField("VtPublisher", related_name="authors", null=True)

    class Meta:
        table = "vt_author"


class VtBook(Model):
    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=20)
    price = fields.IntField(default=0)
    author = fields.ForeignKeyField("VtAuthor", related_name="books")

    class Meta:
        table = "vt_book"


MODELS = [VtCountry, VtPublisher, VtAuthor, VtBook]


async def _seed():
    uk = await VtCountry.create(name="UK")
    us = await VtCountry.create(name="US")
    pub_uk = await VtPublisher.create(name="PubUK", country=uk)
    pub_us = await VtPublisher.create(name="PubUS", country=us)
    ada = await VtAuthor.create(name="Ada", publisher=pub_uk)
    bob = await VtAuthor.create(name="Bob", publisher=pub_us)
    await VtBook.create(title="A1", price=10, author=ada)
    await VtBook.create(title="A2", price=30, author=ada)
    await VtBook.create(title="B1", price=20, author=bob)
    return {"ada": ada, "bob": bob}


@pytest.mark.asyncio
async def test_values_plain_and_subset(db):
    """
    GIVEN seeded books
    WHEN selecting values() with and without an explicit field subset
    THEN each row is a dict keyed by the requested (or all) field names
    """
    await _seed()
    rows = await VtBook.all().order_by("title").values("title", "price")
    assert rows == [
        {"title": "A1", "price": 10},
        {"title": "A2", "price": 30},
        {"title": "B1", "price": 20},
    ]
    # No args -> every model field is present as a key.
    full = await VtBook.filter(title="A1").values()
    assert set(full[0]) == {"id", "title", "price", "author_id"}


@pytest.mark.asyncio
async def test_values_single_level_traversal_dict(db):
    """
    GIVEN books linked to authors
    WHEN values() traverses one forward relation (author__name)
    THEN the dict carries the traversed value under the dotted path key
    """
    await _seed()
    rows = await VtBook.all().order_by("title").values("title", "author__name")
    assert rows == [
        {"title": "A1", "author__name": "Ada"},
        {"title": "A2", "author__name": "Ada"},
        {"title": "B1", "author__name": "Bob"},
    ]


@pytest.mark.asyncio
async def test_values_two_level_traversal_dict(db):
    """
    GIVEN a book -> author -> publisher -> country chain
    WHEN values() traverses two/three forward relations
    THEN the deepest related column is projected into the dict
    """
    await _seed()
    rows = (
        await VtBook.all()
        .order_by("title")
        .values("title", "author__publisher__name", "author__publisher__country__name")
    )
    assert rows == [
        {
            "title": "A1",
            "author__publisher__name": "PubUK",
            "author__publisher__country__name": "UK",
        },
        {
            "title": "A2",
            "author__publisher__name": "PubUK",
            "author__publisher__country__name": "UK",
        },
        {
            "title": "B1",
            "author__publisher__name": "PubUS",
            "author__publisher__country__name": "US",
        },
    ]


@pytest.mark.asyncio
async def test_values_traversal_alias(db):
    """
    GIVEN a multi-hop relation path
    WHEN it is aliased via a keyword in values()
    THEN the dict uses the clean alias key instead of the dotted path
    """
    await _seed()
    rows = await VtBook.filter(title="A1").values(
        "title", country="author__publisher__country__name"
    )
    assert rows == [{"title": "A1", "country": "UK"}]


@pytest.mark.asyncio
async def test_values_list_tuple_and_flat(db):
    """
    GIVEN seeded books
    WHEN reading values_list() with multiple columns and with flat=True
    THEN tuples are returned for multiple columns and scalars for flat
    """
    await _seed()
    pairs = await VtBook.all().order_by("title").values_list("title", "author__name")
    assert pairs == [("A1", "Ada"), ("A2", "Ada"), ("B1", "Bob")]
    flat = await VtBook.all().order_by("title").values_list("title", flat=True)
    assert flat == ["A1", "A2", "B1"]


@pytest.mark.asyncio
async def test_group_by_aggregate_dict(db):
    """
    GIVEN books grouped by author
    WHEN aggregating counts/sums/averages per group
    THEN one dict per group carries the aggregate values
    """
    seed = await _seed()
    rows = (
        await VtBook.annotate(n=Count("id"), total=Sum("price"), avg=Avg("price"))
        .group_by("author_id")
        .order_by("author_id")
        .values("author_id", "n", "total", "avg")
    )
    by_author = {r["author_id"]: r for r in rows}
    assert by_author[seed["ada"].id]["n"] == 2
    assert by_author[seed["ada"].id]["total"] == 40
    assert by_author[seed["bob"].id]["n"] == 1
    assert by_author[seed["bob"].id]["total"] == 20


@pytest.mark.asyncio
async def test_group_by_having_filters_groups(db):
    """
    GIVEN books grouped by author
    WHEN a HAVING filter is applied on the aggregate
    THEN only groups satisfying the aggregate predicate remain
    """
    seed = await _seed()
    rows = (
        await VtBook.annotate(total=Sum("price"))
        .group_by("author_id")
        .filter(total__gte=30)
        .values("author_id", "total")
    )
    assert rows == [{"author_id": seed["ada"].id, "total": 40}]


@pytest.mark.asyncio
async def test_group_by_having_range_on_aggregate(db):
    """
    GIVEN books grouped by author
    WHEN a HAVING uses a special lookup (range) on the aggregate
    THEN the range predicate is honoured (regression: HAVING special ops)
    """
    await _seed()
    rows = (
        await VtBook.annotate(n=Count("id"))
        .group_by("author_id")
        .filter(n__range=(2, 5))
        .values("author_id", "n")
    )
    assert [r["n"] for r in rows] == [2]


@pytest.mark.asyncio
async def test_order_by_random(db):
    """
    GIVEN a set of rows
    WHEN ordering by the random token "?"
    THEN every row is still returned exactly once (RANDOM() ordering)
    """
    await _seed()
    titles = await VtBook.all().order_by("?").values_list("title", flat=True)
    assert sorted(titles) == ["A1", "A2", "B1"]

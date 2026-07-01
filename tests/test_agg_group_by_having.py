"""Corner cases for annotate/group_by/having/aggregates.

Exercises Count/Sum/Avg/Min/Max with distinct and FILTER (Q), grouping on a
plain column, HAVING via ``.filter(annotation__...)``, multiple annotations,
aggregating over a related column, ordering by an annotation, empty groups, and
the forward-relation group_by path (including ordering by that same path, which
resolves via the shared LEFT JOIN rather than a correlated subquery).
"""

import pytest

from yara_orm import Avg, Count, F, FieldError, Max, Min, Model, Q, Sum, fields


class AggghAuthor(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)
    country = fields.CharField(max_length=20, null=True)

    class Meta:
        table = "aggh_author"


class AggghBook(Model):
    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=50)
    rating = fields.IntField()
    price = fields.IntField(default=0)
    genre = fields.CharField(max_length=20)
    author = fields.ForeignKeyField("AggghAuthor", related_name="books")

    class Meta:
        table = "aggh_book"


MODELS = [AggghAuthor, AggghBook]


async def _seed():
    """Seed two authors with a spread of ratings/prices/genres.

    Returns:
        The two created authors ``(ada, bob)``.
    """
    ada = await AggghAuthor.create(name="Ada", country="UK")
    bob = await AggghAuthor.create(name="Bob", country="US")
    await AggghBook.create(title="A1", rating=5, price=10, genre="sci-fi", author=ada)
    await AggghBook.create(title="A2", rating=3, price=30, genre="sci-fi", author=ada)
    await AggghBook.create(title="A3", rating=3, price=20, genre="drama", author=ada)
    await AggghBook.create(title="B1", rating=4, price=40, genre="drama", author=bob)
    return ada, bob


# ---------------------------------------------------------------------------
# Aggregate kinds & modifiers
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_all_aggregate_kinds_single_group(db):
    """
    GIVEN a seeded set of books
    WHEN every aggregate kind is computed over the whole set (group_by())
    THEN Count/Sum/Avg/Min/Max each match the underlying data
    """
    await _seed()
    [row] = (
        await AggghBook.annotate(
            n=Count("id"),
            total=Sum("price"),
            avg=Avg("rating"),
            lo=Min("rating"),
            hi=Max("rating"),
        )
        .group_by()
        .values("n", "total", "avg", "lo", "hi")
    )
    assert row["n"] == 4
    assert row["total"] == 100
    assert round(float(row["avg"]), 2) == 3.75
    assert row["lo"] == 3
    assert row["hi"] == 5


@pytest.mark.asyncio
async def test_count_distinct(db):
    """
    GIVEN books whose ratings repeat (5, 3, 3, 4)
    WHEN Count("rating", distinct=True) is aggregated
    THEN only the distinct rating values are counted (3 of them)
    """
    await _seed()
    [row] = await AggghBook.annotate(n=Count("rating", distinct=True)).group_by().values("n")
    assert row["n"] == 3


@pytest.mark.asyncio
async def test_sum_distinct(db):
    """
    GIVEN ratings 5, 3, 3, 4 across all books
    WHEN Sum("rating", distinct=True) is aggregated
    THEN duplicate 3s collapse and the distinct sum is 5+3+4 = 12
    """
    await _seed()
    [row] = await AggghBook.annotate(s=Sum("rating", distinct=True)).group_by().values("s")
    assert row["s"] == 12


@pytest.mark.asyncio
async def test_count_with_filter_q(db):
    """
    GIVEN books of mixed genres
    WHEN Count is restricted with _filter=Q(genre="sci-fi") (FILTER clause)
    THEN only the sci-fi rows feed the count while other groups stay visible
    """
    ada, bob = await _seed()
    rows = (
        await AggghBook.annotate(sci=Count("id", _filter=Q(genre="sci-fi")))
        .group_by("author_id")
        .order_by("author_id")
        .values("author_id", "sci")
    )
    by_author = {r["author_id"]: r["sci"] for r in rows}
    assert by_author[ada.id] == 2
    assert by_author[bob.id] == 0


@pytest.mark.asyncio
async def test_sum_with_filter_q(db):
    """
    GIVEN books with prices split across genres
    WHEN Sum(price) is filtered to the drama genre via _filter=Q(...)
    THEN only drama prices are summed per group
    """
    ada, bob = await _seed()
    rows = (
        await AggghBook.annotate(drama_total=Sum("price", _filter=Q(genre="drama")))
        .group_by("author_id")
        .order_by("author_id")
        .values("author_id", "drama_total")
    )
    by_author = {r["author_id"]: r["drama_total"] for r in rows}
    assert by_author[ada.id] == 20
    # Bob's only book is drama (40); no non-drama to exclude.
    assert by_author[bob.id] == 40


@pytest.mark.asyncio
async def test_aggregate_over_expression(db):
    """
    GIVEN books with rating and price columns
    WHEN Sum is applied to an arithmetic expression F("rating") * F("price")
    THEN the summed product matches a hand computation
    """
    await _seed()
    [row] = (
        await AggghBook.annotate(weighted=Sum(F("rating") * F("price")))
        .group_by()
        .values("weighted")
    )
    # 5*10 + 3*30 + 3*20 + 4*40 = 50+90+60+160 = 360
    assert row["weighted"] == 360


# ---------------------------------------------------------------------------
# group_by on a plain column
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_group_by_plain_column(db):
    """
    GIVEN books tagged with a genre column
    WHEN grouping by the plain (non-relation) genre column
    THEN one aggregated row per genre is returned
    """
    await _seed()
    rows = (
        await AggghBook.annotate(n=Count("id"), total=Sum("price"))
        .group_by("genre")
        .order_by("genre")
        .values("genre", "n", "total")
    )
    by_genre = {r["genre"]: (r["n"], r["total"]) for r in rows}
    assert by_genre == {"drama": (2, 60), "sci-fi": (2, 40)}


@pytest.mark.asyncio
async def test_grouped_random_ordering(db):
    """
    GIVEN a grouped/aggregated query ordered randomly
    WHEN order_by("?") is applied to the groups
    THEN it runs (RANDOM()) and returns one row per group
    """
    await _seed()
    rows = (
        await AggghBook.annotate(n=Count("id")).group_by("genre").order_by("?").values("genre", "n")
    )
    assert {r["genre"] for r in rows} == {"drama", "sci-fi"}


@pytest.mark.asyncio
async def test_group_by_multiple_columns(db):
    """
    GIVEN books grouped by both author and genre
    WHEN grouping on two plain columns at once
    THEN each (author, genre) combination is its own group
    """
    ada, bob = await _seed()
    rows = (
        await AggghBook.annotate(n=Count("id"))
        .group_by("author_id", "genre")
        .values("author_id", "genre", "n")
    )
    grouped = {(r["author_id"], r["genre"]): r["n"] for r in rows}
    assert grouped[(ada.id, "sci-fi")] == 2
    assert grouped[(ada.id, "drama")] == 1
    assert grouped[(bob.id, "drama")] == 1


# ---------------------------------------------------------------------------
# HAVING via .filter(annotation__...)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_having_gt_on_annotation(db):
    """
    GIVEN books grouped by author
    WHEN a HAVING filter (annotation__gt) is applied on the count
    THEN only groups whose count exceeds the threshold survive
    """
    ada, bob = await _seed()
    rows = (
        await AggghBook.annotate(n=Count("id"))
        .group_by("author_id")
        .filter(n__gt=1)
        .values("author_id", "n")
    )
    assert rows == [{"author_id": ada.id, "n": 3}]
    assert bob  # Bob (1 book) is filtered out.


@pytest.mark.asyncio
async def test_having_combines_with_where(db):
    """
    GIVEN a WHERE on a base column and a HAVING on the aggregate
    WHEN both a plain filter and an annotation filter are chained
    THEN the WHERE restricts rows before grouping and HAVING restricts groups
    """
    ada, bob = await _seed()
    rows = (
        await AggghBook.filter(genre="sci-fi")
        .annotate(n=Count("id"))
        .group_by("author_id")
        .filter(n__gte=2)
        .values("author_id", "n")
    )
    # Only Ada has 2 sci-fi books; Bob has none.
    assert rows == [{"author_id": ada.id, "n": 2}]
    assert bob


@pytest.mark.asyncio
async def test_having_on_sum_and_avg(db):
    """
    GIVEN books grouped by author with per-group sums
    WHEN HAVING filters on a Sum annotation
    THEN only groups whose summed price clears the bound remain
    """
    ada, bob = await _seed()
    rows = (
        await AggghBook.annotate(total=Sum("price"))
        .group_by("author_id")
        .filter(total__gt=50)
        .values("author_id", "total")
    )
    by_author = {r["author_id"]: r["total"] for r in rows}
    assert by_author == {ada.id: 60}
    assert bob


@pytest.mark.asyncio
async def test_having_range_lookup(db):
    """
    GIVEN books grouped by author
    WHEN HAVING uses a range special lookup on the annotation
    THEN groups whose count falls in the inclusive range are kept
    """
    ada, bob = await _seed()
    rows = (
        await AggghBook.annotate(n=Count("id"))
        .group_by("author_id")
        .filter(n__range=(1, 2))
        .values("author_id", "n")
    )
    assert rows == [{"author_id": bob.id, "n": 1}]
    assert ada


@pytest.mark.asyncio
async def test_having_no_group_matches(db):
    """
    GIVEN a HAVING threshold no group can satisfy
    WHEN the aggregate filter excludes every group
    THEN an empty result set is returned
    """
    await _seed()
    rows = (
        await AggghBook.annotate(n=Count("id"))
        .group_by("author_id")
        .filter(n__gt=1000)
        .values("author_id", "n")
    )
    assert rows == []


# ---------------------------------------------------------------------------
# Aggregating over a related column / reverse relation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_aggregate_over_reverse_relation(db):
    """
    GIVEN authors with differing numbers of books
    WHEN annotating Count over the reverse relation ("books")
    THEN each author carries their own book count
    """
    ada, bob = await _seed()
    rows = await AggghAuthor.annotate(n=Count("books")).order_by("name")
    counts = {r.name: r.n for r in rows}
    assert counts == {"Ada": 3, "Bob": 1}


@pytest.mark.asyncio
async def test_aggregate_over_related_column(db):
    """
    GIVEN authors linked to books
    WHEN annotating Sum over the related column ("books__price")
    THEN the per-author sum of the related column is computed
    """
    ada, bob = await _seed()
    rows = await AggghAuthor.annotate(total=Sum("books__price")).order_by("name")
    totals = {r.name: r.total for r in rows}
    assert totals == {"Ada": 60, "Bob": 40}


@pytest.mark.asyncio
async def test_aggregate_reverse_relation_zero_group(db):
    """
    GIVEN an author with no books at all
    WHEN Count over the reverse relation is taken with a LEFT JOIN
    THEN the childless author still appears with a count of 0
    """
    ada, bob = await _seed()
    carol = await AggghAuthor.create(name="Carol", country="UK")
    rows = await AggghAuthor.annotate(n=Count("books")).order_by("name")
    counts = {r.name: r.n for r in rows}
    assert counts == {"Ada": 3, "Bob": 1, "Carol": 0}
    assert carol


# ---------------------------------------------------------------------------
# Ordering by an annotation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_order_by_annotation_desc(db):
    """
    GIVEN authors with different book counts
    WHEN ordering by the annotation descending
    THEN the busiest author comes first
    """
    await _seed()
    rows = (
        await AggghBook.annotate(n=Count("id"))
        .group_by("author_id")
        .order_by("-n")
        .values("author_id", "n")
    )
    assert [r["n"] for r in rows] == [3, 1]


@pytest.mark.asyncio
async def test_order_by_annotation_then_filter(db):
    """
    GIVEN grouped counts filtered by HAVING and ordered by the annotation
    WHEN both an annotation HAVING and an annotation ordering apply
    THEN surviving groups come back sorted by the aggregate
    """
    await _seed()
    rows = (
        await AggghBook.annotate(total=Sum("price"))
        .group_by("genre")
        .filter(total__gt=0)
        .order_by("total")
        .values("genre", "total")
    )
    assert [r["total"] for r in rows] == [40, 60]


# ---------------------------------------------------------------------------
# Empty result / no rows
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_grouped_empty_table(db):
    """
    GIVEN no books at all
    WHEN a grouped aggregate query runs over the empty table
    THEN no group rows are produced
    """
    rows = (
        await AggghBook.annotate(n=Count("id")).group_by("author_id").values("author_id", "n")
    )
    assert rows == []


@pytest.mark.asyncio
async def test_ungrouped_count_empty_table_is_zero(db):
    """
    GIVEN an empty table
    WHEN an ungrouped Count runs (single implicit group)
    THEN a single row with a zero count is returned
    """
    [row] = await AggghBook.annotate(n=Count("id")).group_by().values("n")
    assert row["n"] == 0


# ---------------------------------------------------------------------------
# group_by on a FORWARD-RELATION path — BUG on PostgreSQL.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_group_by_forward_relation_path_no_order(db):
    """
    GIVEN books joined to their author
    WHEN grouping by a forward-relation path (author__name), no ordering
    THEN one aggregated row per related value is returned (works on both DBs)
    """
    await _seed()
    rows = (
        await AggghBook.annotate(n=Count("id"))
        .group_by("author__name")
        .values("author__name", "n")
    )
    by_name = {r["author__name"]: r["n"] for r in rows}
    assert by_name == {"Ada": 3, "Bob": 1}


@pytest.mark.asyncio
async def test_group_by_forward_relation_path_ordered(db):
    """
    GIVEN books grouped by a forward-relation path (author__name)
    WHEN the result is also ordered by that same relation path
    THEN groups should come back ordered by the related value

    On PostgreSQL this raises 42803: order_by on a forward-relation path renders
    a correlated scalar subquery ``(SELECT ... WHERE author.id = book.author_id)``
    that references the ungrouped FK column ``book.author_id``. SQLite tolerates
    it (this passes there, which strict=False allows).
    """
    await _seed()
    rows = (
        await AggghBook.annotate(n=Count("id"))
        .group_by("author__name")
        .order_by("author__name")
        .values("author__name", "n")
    )
    assert [r["author__name"] for r in rows] == ["Ada", "Bob"]
    assert [r["n"] for r in rows] == [3, 1]


@pytest.mark.asyncio
async def test_flat_multi_field_rejected_when_grouped(db):
    """
    GIVEN a grouped, annotated query
    WHEN values_list(flat=True) names more than one field
    THEN a FieldError is raised
    """
    await _seed()
    with pytest.raises(FieldError):
        await (
            AggghBook.annotate(n=Count("id"))
            .group_by("author_id")
            .values_list("author_id", "n", flat=True)
        )

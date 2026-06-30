"""Tortoise-migration compatibility for the query set.

Covers three gaps the migrated apps needed: a public ``get_parameterized_sql``
accessor (so a grouped query can be wrapped in ``SELECT COUNT(*) FROM (...)``
without touching privates), filtered/conditional aggregates (``_filter`` and
``Case``), and ``using_db`` accepting a connection object rather than a name.
"""

import pytest

from yara_orm import Case, Count, F, Model, Q, Sum, Value, When, fields
from yara_orm.connection import connections


class McAuthor(Model):
    name = fields.CharField(max_length=50)

    class Meta:
        table = "mc_author"


class McBook(Model):
    title = fields.CharField(max_length=50)
    rating = fields.IntField()
    author = fields.ForeignKeyField("McAuthor", related_name="books")

    class Meta:
        table = "mc_book"


MODELS = [McAuthor, McBook]


async def _seed() -> tuple[McAuthor, McAuthor, McAuthor]:
    """Create three authors; only the first two have books.

    Returns:
        The ``(ada, bob, carol)`` authors; Ada has ratings 5/4, Bob 4/2, Carol
        none.
    """
    ada = await McAuthor.create(name="Ada")
    bob = await McAuthor.create(name="Bob")
    carol = await McAuthor.create(name="Carol")
    await McBook.create(title="A1", rating=5, author=ada)
    await McBook.create(title="A2", rating=4, author=ada)
    await McBook.create(title="B1", rating=4, author=bob)
    await McBook.create(title="B2", rating=2, author=bob)
    return ada, bob, carol


@pytest.mark.asyncio
async def test_get_parameterized_sql_wraps_grouped_count(db):
    """
    GIVEN a grouped, filtered, annotated query set
    WHEN get_parameterized_sql() feeds a SELECT COUNT(*) FROM (...) wrapper
    THEN the bound params carry through and the group count comes back
    """
    await _seed()

    qs = McBook.filter(rating__gte=3).annotate(total=Sum("rating")).group_by("author_id")
    sql, params = qs.get_parameterized_sql()

    assert params == [3]  # the WHERE bind survived
    assert "GROUP BY" in sql

    wrapped = f"SELECT COUNT(*) FROM ({sql}) x"
    _, rows = await connections.get().execute_query(wrapped, params)
    # Ada (5, 4) and Bob (4) survive rating>=3; Carol has no books -> 2 groups.
    assert next(iter(rows[0].values())) == 2


@pytest.mark.asyncio
async def test_filtered_aggregate_per_group(db):
    """
    GIVEN books grouped per author
    WHEN a Count carries a _filter=Q(...) restricting which rows it counts
    THEN each group's filtered count is correct alongside the plain count
    """
    if db != "postgres":
        pytest.skip("FILTER (WHERE ...) is PostgreSQL-only")
    ada, bob, _ = await _seed()

    rows = await (
        McBook.annotate(
            total=Count("id"),
            hi=Count("id", _filter=Q(rating__gte=4)),
        )
        .group_by("author_id")
        .values("author_id", "total", "hi")
    )
    by_author = {r["author_id"]: (r["total"], r["hi"]) for r in rows}
    assert by_author[ada.id] == (2, 2)  # ratings 5, 4 -> both >= 4
    assert by_author[bob.id] == (2, 1)  # ratings 4, 2 -> one >= 4


@pytest.mark.asyncio
async def test_conditional_aggregate_over_expression(db):
    """
    GIVEN books grouped per author
    WHEN a Sum wraps a Case (and an F arithmetic) expression
    THEN the aggregate is computed over the rendered expression per group
    """
    ada, bob, _ = await _seed()

    rows = await (
        McBook.annotate(
            weighted=Sum(Case(When(rating__gte=4, then=F("rating")), default=Value(0))),
            bumped=Sum(F("rating") + 1),
        )
        .group_by("author_id")
        .values("author_id", "weighted", "bumped")
    )
    by_author = {r["author_id"]: (r["weighted"], r["bumped"]) for r in rows}
    # weighted sums rating only when >= 4: Ada 5+4=9; Bob 4+0=4.
    assert by_author[ada.id][0] == 9
    assert by_author[bob.id][0] == 4
    # bumped = sum(rating + 1): Ada (5+1)+(4+1)=11; Bob (4+1)+(2+1)=8.
    assert by_author[ada.id][1] == 11
    assert by_author[bob.id][1] == 8


@pytest.mark.asyncio
async def test_using_db_accepts_connection_object(db):
    """
    GIVEN a connection object obtained from connections.get()
    WHEN using_db is handed that object instead of a name
    THEN the query runs on it and returns the expected rows
    """
    await _seed()

    conn = connections.get()  # a connection/executor object, not a name
    assert await McBook.all().using_db(conn).count() == 4
    titles = {b.title for b in await McBook.all().using_db(conn)}
    assert titles == {"A1", "A2", "B1", "B2"}

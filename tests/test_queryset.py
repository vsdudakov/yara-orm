"""QuerySet/Model: lookups, ordering, projections, mutations, plus the
ergonomics helpers (get_or_create, in_bulk, bulk_update, slicing, distinct,
last/earliest/latest, refresh_from_db, update_from_dict, select_for_update)."""

import pytest

from yara_orm import (
    BaseDBAsyncClient,
    Case,
    Count,
    F,
    FieldError,
    Model,
    Q,
    Subquery,
    Sum,
    Value,
    When,
    connections,
    fields,
)


class CvItem(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)
    value = fields.IntField()

    class Meta:
        table = "cov_item"


class QsWidget(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50, unique=True)
    qty = fields.IntField(default=0)

    class Meta:
        table = "qs_widget"


class QsParent(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20)

    class Meta:
        table = "qs_parent"


class QsChild(Model):
    id = fields.IntField(pk=True)
    parent = fields.ForeignKeyField("QsParent", related_name="children")

    class Meta:
        table = "qs_child"


class McAuthor(Model):
    name = fields.CharField(max_length=50)

    class Meta:
        table = "mc_author"


class McBook(Model):
    title = fields.CharField(max_length=50)
    rating = fields.IntField()
    note = fields.CharField(max_length=50, null=True)
    author = fields.ForeignKeyField("McAuthor", related_name="books")

    class Meta:
        table = "mc_book"


class ObCountry(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20)

    class Meta:
        table = "ob_country"


class ObAuthor(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20)
    country = fields.ForeignKeyField("ObCountry", related_name="authors")

    class Meta:
        table = "ob_author"


class ObBook(Model):
    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=20)
    author = fields.ForeignKeyField("ObAuthor", related_name="books")

    class Meta:
        table = "ob_book"


MODELS = [
    CvItem,
    QsWidget,
    QsParent,
    QsChild,
    McAuthor,
    McBook,
    ObCountry,
    ObAuthor,
    ObBook,
]


async def _seed():
    await CvItem.create(name="alpha", value=1)
    await CvItem.create(name="Beta", value=2)
    await CvItem.create(name="gamma", value=3)


async def _seed_books() -> tuple[McAuthor, McAuthor, McAuthor]:
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


async def _param_sql(qs):
    """Return ``get_parameterized_sql()`` for a query set.

    Args:
        qs: The query set to compile.

    Returns:
        The ``(sql, params)`` tuple.
    """
    return qs.get_parameterized_sql()


@pytest.mark.asyncio
async def test_comparison_lookups(db):
    """
    GIVEN seeded items
    WHEN filtering with gt/gte/lt/lte/not lookups
    THEN each comparison selects the right rows
    """
    await _seed()
    assert {i.value for i in await CvItem.filter(value__gt=1)} == {2, 3}
    assert {i.value for i in await CvItem.filter(value__gte=2)} == {2, 3}
    assert {i.value for i in await CvItem.filter(value__lt=3)} == {1, 2}
    assert {i.value for i in await CvItem.filter(value__lte=2)} == {1, 2}
    assert {i.value for i in await CvItem.filter(value__not=2)} == {1, 3}


@pytest.mark.asyncio
async def test_text_lookups(db):
    """
    GIVEN seeded items
    WHEN filtering with contains/startswith/endswith and case-insensitive forms
    THEN the LIKE/ILIKE patterns select the right rows
    """
    await _seed()
    assert [i.name for i in await CvItem.filter(name__contains="lph")] == ["alpha"]
    assert [i.name for i in await CvItem.filter(name__icontains="BET")] == ["Beta"]
    assert [i.name for i in await CvItem.filter(name__startswith="al")] == ["alpha"]
    assert [i.name for i in await CvItem.filter(name__istartswith="be")] == ["Beta"]
    assert [i.name for i in await CvItem.filter(name__endswith="ma")] == ["gamma"]
    # Every seeded name ends in "a"; iendswith matches case-insensitively.
    assert {i.name for i in await CvItem.filter(name__iendswith="A")} == {
        "alpha",
        "Beta",
        "gamma",
    }


@pytest.mark.asyncio
async def test_in_empty_and_isnull(db):
    """
    GIVEN seeded items
    WHEN filtering with an empty __in and with __isnull
    THEN an empty __in matches nothing and isnull works both ways
    """
    await _seed()
    assert await CvItem.filter(value__in=[]).count() == 0
    assert await CvItem.filter(name__isnull=False).count() == 3
    assert await CvItem.filter(name__isnull=True).count() == 0


@pytest.mark.asyncio
async def test_q_negation_and_exclude(db):
    """
    GIVEN seeded items
    WHEN using ~Q and exclude()
    THEN negated conditions remove the matching rows
    """
    await _seed()
    assert [i.name for i in await CvItem.filter(~Q(name="alpha")).order_by("name")] == [
        "Beta",
        "gamma",
    ]
    assert [i.name for i in await CvItem.exclude(value=2).order_by("value")] == ["alpha", "gamma"]


@pytest.mark.asyncio
async def test_ordering_limit_offset(db):
    """
    GIVEN seeded items
    WHEN ordering descending with limit and offset
    THEN the right slice is returned in order
    """
    await _seed()
    rows = await CvItem.all().order_by("-value").limit(2).offset(1)
    assert [i.value for i in rows] == [2, 1]


@pytest.mark.asyncio
async def test_first_get_or_none_exists(db):
    """
    GIVEN seeded items
    WHEN calling first/get_or_none/exists
    THEN they return the expected single/optional/boolean results
    """
    await _seed()
    assert (await CvItem.all().order_by("value").first()).value == 1
    assert await CvItem.all().filter(value=99).first() is None
    assert await CvItem.get_or_none(value=99) is None
    assert (await CvItem.get_or_none(value=1)).name == "alpha"
    assert await CvItem.filter(value=1).exists() is True
    assert await CvItem.filter(value=99).exists() is False


@pytest.mark.asyncio
async def test_values_and_values_list(db):
    """
    GIVEN seeded items
    WHEN projecting with values/values_list (incl. flat)
    THEN dicts, tuples and scalars are returned without model objects
    """
    await _seed()
    assert await CvItem.all().order_by("value").values_list("value", flat=True) == [1, 2, 3]
    pairs = await CvItem.all().order_by("value").values_list("name", "value")
    assert pairs[0] == ("alpha", 1)
    dicts = await CvItem.all().order_by("value").values("value")
    assert dicts == [{"value": 1}, {"value": 2}, {"value": 3}]
    with pytest.raises(FieldError):
        await CvItem.all().values_list("name", "value", flat=True)


@pytest.mark.asyncio
async def test_update_and_delete_queryset(db):
    """
    GIVEN seeded items
    WHEN updating and deleting via the queryset
    THEN the affected counts and remaining rows are correct
    """
    await _seed()
    assert await CvItem.filter(value__lte=2).update(value=0) == 2
    assert await CvItem.filter(value=0).count() == 2
    assert await CvItem.filter(value=0).delete() == 2
    assert await CvItem.all().count() == 1


@pytest.mark.asyncio
async def test_unknown_field_raises(db):
    """
    GIVEN a model
    WHEN filtering by an unknown field
    THEN a FieldError is raised
    """
    with pytest.raises(FieldError):
        await CvItem.filter(missing=1)


@pytest.mark.asyncio
async def test_get_or_create(db):
    """
    GIVEN an empty table
    WHEN get_or_create runs for a key twice
    THEN the first call creates the row and the second returns it unchanged
    """
    obj, created = await QsWidget.get_or_create(name="a", defaults={"qty": 1})
    assert created is True and obj.qty == 1
    obj, created = await QsWidget.get_or_create(name="a", defaults={"qty": 99})
    assert created is False and obj.qty == 1  # defaults ignored on hit


@pytest.mark.asyncio
async def test_update_or_create(db):
    """
    GIVEN a key that is first absent then present
    WHEN update_or_create runs twice with differing defaults
    THEN it creates on the miss and persists the new defaults on the hit
    """
    obj, created = await QsWidget.update_or_create(name="b", defaults={"qty": 7})
    assert created is True and obj.qty == 7
    obj, created = await QsWidget.update_or_create(name="b", defaults={"qty": 8})
    assert created is False and obj.qty == 8
    assert (await QsWidget.get(name="b")).qty == 8  # persisted
    # No defaults on an existing row: returns it unchanged without an update.
    obj, created = await QsWidget.update_or_create(name="b")
    assert created is False and obj.qty == 8


@pytest.mark.asyncio
async def test_in_bulk(db):
    """
    GIVEN several rows
    WHEN in_bulk is called with pk values, an empty list, and a non-pk field
    THEN it returns instances keyed by the lookup field (and {} for empty)
    """
    await QsWidget.bulk_create([QsWidget(name=f"x{i}", qty=i) for i in range(4)])
    ids = [w.id for w in await QsWidget.all().order_by("id")]
    out = await QsWidget.in_bulk(ids[:3])
    assert set(out) == set(ids[:3])
    assert all(isinstance(v, QsWidget) for v in out.values())
    assert await QsWidget.in_bulk([]) == {}
    by_name = await QsWidget.in_bulk(["x1", "x2"], field_name="name")
    assert set(by_name) == {"x1", "x2"}


@pytest.mark.asyncio
async def test_bulk_update(db):
    """
    GIVEN rows mutated in memory on two fields
    WHEN bulk_update is asked to write only one field
    THEN that field is persisted, the other is not, and the row count returns
    """
    await QsWidget.bulk_create([QsWidget(name=f"y{i}", qty=i) for i in range(5)])
    objs = await QsWidget.filter(name__in=["y0", "y1", "y2"]).order_by("name")
    for o in objs:
        o.qty = 100
        o.name = o.name + "!"  # not in fields -> must NOT be written
    n = await QsWidget.bulk_update(objs, ["qty"])
    assert n == 3
    assert (await QsWidget.get(name="y1")).qty == 100  # name unchanged, qty written
    assert await QsWidget.bulk_update([], ["qty"]) == 0


@pytest.mark.asyncio
async def test_slicing(db):
    """
    GIVEN an ordered query set
    WHEN it is indexed with a slice and an integer
    THEN the slice applies offset/limit and the index fetches one row (bounds
    and negative indices raise)
    """
    await QsWidget.bulk_create([QsWidget(name=f"s{i}", qty=i) for i in range(10)])
    page = await QsWidget.all().order_by("qty")[2:5]
    assert [w.qty for w in page] == [2, 3, 4]
    third = await QsWidget.all().order_by("qty")[3]
    assert third.qty == 3
    with pytest.raises(IndexError):
        await QsWidget.all().order_by("qty")[999]
    with pytest.raises(ValueError):
        QsWidget.all()[-1]


@pytest.mark.asyncio
async def test_distinct(db):
    """
    GIVEN six rows whose qty is one of two values
    WHEN a distinct projection of qty is fetched
    THEN only the two distinct values are returned
    """
    await QsWidget.bulk_create([QsWidget(name=f"d{i}", qty=i % 2) for i in range(6)])
    qtys = await QsWidget.all().distinct().values_list("qty", flat=True)
    assert sorted(qtys) == [0, 1]


@pytest.mark.asyncio
async def test_not_in_subquery_keeps_null_rows(db):
    """
    GIVEN a nullable column with some NULL rows
    WHEN filtering with __not_in against a Subquery
    THEN NULL rows are kept, matching the literal-list __not_in semantics

    Regression: the subquery path emitted a bare ``NOT IN`` (``NULL NOT IN
    (...)`` is UNKNOWN), silently dropping the NULL rows.
    """
    a = await McAuthor.create(name="A")
    await McBook.create(title="x", rating=1, note="keep-out", author=a)
    await McBook.create(title="y", rating=2, note="stay", author=a)
    await McBook.create(title="z", rating=3, note=None, author=a)
    sub = Subquery(McBook.filter(title="x").only("note"))
    notes = {b.note for b in await McBook.filter(note__not_in=sub)}
    assert notes == {"stay", None}


@pytest.mark.asyncio
async def test_last_earliest_latest(db):
    """
    GIVEN rows with distinct qty values
    WHEN last/earliest/latest run
    THEN last reverses the ordering and earliest/latest order asc/desc then
    take the first row
    """
    await QsWidget.bulk_create([QsWidget(name=f"o{i}", qty=i) for i in range(5)])
    assert (await QsWidget.all().order_by("qty").last()).qty == 4
    assert (await QsWidget.all().last()).qty == 4  # defaults to pk desc
    assert (await QsWidget.all().earliest("qty")).qty == 0
    assert (await QsWidget.all().latest("qty")).qty == 4


@pytest.mark.asyncio
async def test_refresh_from_db(db):
    """
    GIVEN an instance gone stale after a separate UPDATE
    WHEN refresh_from_db runs
    THEN its column values are reloaded from the database
    """
    w = await QsWidget.create(name="r", qty=1)
    await QsWidget.filter(name="r").update(qty=42)
    assert w.qty == 1  # stale in memory
    await w.refresh_from_db()
    assert w.qty == 42


@pytest.mark.asyncio
async def test_update_from_dict(db):
    """
    GIVEN an instance
    WHEN update_from_dict is given known and unknown keys
    THEN known fields are set (and persist on save) and unknown keys raise
    """
    w = await QsWidget.create(name="u", qty=1)
    assert w.update_from_dict({"qty": 5}) is w
    assert w.qty == 5
    await w.save()
    assert (await QsWidget.get(name="u")).qty == 5
    with pytest.raises(FieldError):
        w.update_from_dict({"nope": 1})


@pytest.mark.asyncio
async def test_select_for_update(db):
    """
    GIVEN a row
    WHEN a query set adds select_for_update()
    THEN the FOR UPDATE clause is emitted on PostgreSQL, is a no-op on SQLite,
    and the query returns the row on both
    """
    await QsWidget.create(name="lock", qty=1)
    rows = await QsWidget.filter(name="lock").select_for_update()
    assert len(rows) == 1


def test_slicing_errors():
    """
    GIVEN a query set
    WHEN it is indexed with a step, a negative bound, or a non-int/slice key
    THEN the appropriate ValueError / TypeError is raised (no DB access)
    """
    with pytest.raises(ValueError):
        QsWidget.all()[::2]
    with pytest.raises(ValueError):
        QsWidget.all()[-2:]
    with pytest.raises(TypeError):
        QsWidget.all()["x"]


@pytest.mark.asyncio
async def test_queryset_all_terminator(db):
    """
    GIVEN a built query set
    WHEN ``.all()`` terminates the chain
    THEN it returns the rows (no-op clone)
    """
    await QsParent.create(name="p")
    assert len(await QsParent.filter(name="p").all()) == 1


@pytest.mark.asyncio
async def test_get_single_select_related_and_using_db(db):
    """
    GIVEN a chainable ``Model.get(...)``
    WHEN ``.select_related(...)`` and ``.using_db(...)`` are chained
    THEN both resolve to the single instance
    """
    p = await QsParent.create(name="p")
    c = await QsChild.create(parent=p)

    via_select = await QsChild.get(id=c.id).select_related("parent")
    assert via_select.id == c.id
    via_using = await QsChild.get(id=c.id).using_db("default")
    assert via_using.id == c.id


@pytest.mark.asyncio
async def test_get_parameterized_sql_annotated(db):
    """
    GIVEN an annotated (non-grouped) query
    WHEN ``get_parameterized_sql()`` is called
    THEN it returns SQL and params for the annotated SELECT
    """
    sql, params = await _param_sql(QsParent.annotate(n=Count("children")))
    assert "SELECT" in sql
    # select_related path of get_parameterized_sql.
    sql2, _ = await _param_sql(QsChild.all().select_related("parent"))
    assert "SELECT" in sql2


@pytest.mark.asyncio
async def test_aggregate_empty_filter_skips_filter_clause(db):
    """
    GIVEN an aggregate with an empty ``_filter=Q()``
    WHEN the annotated query runs
    THEN no FILTER clause is emitted and the query succeeds
    """
    await QsParent.create(name="p")
    rows = await QsParent.annotate(n=Count("id", _filter=Q())).group_by("id").values("n")
    assert rows[0]["n"] == 1


@pytest.mark.asyncio
async def test_order_by_middle_segment_not_relation_raises(db):
    """
    GIVEN an order_by path whose middle segment is a column, not a relation
    WHEN the SQL is built
    THEN a FieldError is raised
    """
    with pytest.raises(FieldError):
        await _param_sql(QsChild.all().order_by("parent__name__nope"))


@pytest.mark.asyncio
async def test_get_parameterized_sql_wraps_grouped_count(db):
    """
    GIVEN a grouped, filtered, annotated query set
    WHEN get_parameterized_sql() feeds a SELECT COUNT(*) FROM (...) wrapper
    THEN the bound params carry through and the group count comes back
    """
    await _seed_books()

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
    ada, bob, _ = await _seed_books()

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
    ada, bob, _ = await _seed_books()

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
    await _seed_books()

    conn = connections.get()  # a connection/executor object, not a name
    assert await McBook.all().using_db(conn).count() == 4
    titles = {b.title for b in await McBook.all().using_db(conn)}
    assert titles == {"A1", "A2", "B1", "B2"}


@pytest.mark.asyncio
async def test_order_by_forward_relation_column(db):
    """
    GIVEN books whose authors sort differently from the books themselves
    WHEN ordering by ``author__name`` (a forward-relation column)
    THEN rows come back ordered by the related column
    """
    ada = await ObAuthor.create(name="Ada", country=await ObCountry.create(name="UK"))
    bob = await ObAuthor.create(name="Bob", country=await ObCountry.create(name="US"))
    await ObBook.create(title="zeta", author=ada)
    await ObBook.create(title="alpha", author=bob)

    books = await ObBook.all().order_by("author__name")
    assert [b.title for b in books] == ["zeta", "alpha"]  # Ada before Bob

    desc = await ObBook.all().order_by("-author__name")
    assert [b.title for b in desc] == ["alpha", "zeta"]


@pytest.mark.asyncio
async def test_order_by_multi_hop_forward_relation(db):
    """
    GIVEN a two-hop forward path book -> author -> country
    WHEN ordering by ``author__country__name``
    THEN rows are ordered by the far related column
    """
    uk = await ObCountry.create(name="AA")
    us = await ObCountry.create(name="ZZ")
    ada = await ObAuthor.create(name="Ada", country=us)
    bob = await ObAuthor.create(name="Bob", country=uk)
    await ObBook.create(title="from_ada", author=ada)
    await ObBook.create(title="from_bob", author=bob)

    books = await ObBook.all().order_by("author__country__name")
    assert [b.title for b in books] == ["from_bob", "from_ada"]  # AA before ZZ


@pytest.mark.asyncio
async def test_order_by_reverse_relation_rejected(db):
    """
    GIVEN a reverse relation (one-to-many)
    WHEN ordering by it as a relation path
    THEN a FieldError is raised (no single orderable value)
    """
    with pytest.raises(FieldError):
        await ObAuthor.all().order_by("books__title")


@pytest.mark.asyncio
async def test_connections_get_satisfies_base_db_async_client(orm):
    """
    GIVEN the public ``BaseDBAsyncClient`` executor protocol
    WHEN the active connection is inspected
    THEN it is a structural instance of the protocol
    """
    assert isinstance(connections.get(), BaseDBAsyncClient)


# -- chainable first() / single-row projections -------------------------------


async def _seed_mc() -> tuple[McAuthor, McBook]:
    """Create one author with two books.

    Returns:
        The author and its first book.
    """
    ada = await McAuthor.create(name="Ada")
    b1 = await McBook.create(title="B1", rating=4, author=ada)
    await McBook.create(title="B2", rating=5, author=ada)
    return ada, b1


@pytest.mark.asyncio
async def test_first_then_values_returns_single_dict(db):
    """
    GIVEN matching rows
    WHEN first() is chained into values()
    THEN a single dict (not a list) of the requested columns is returned
    """
    await _seed_mc()
    assert await McBook.filter(title="B1").first().values("title") == {"title": "B1"}


@pytest.mark.asyncio
async def test_first_then_only_returns_single_model(db):
    """
    GIVEN matching rows
    WHEN first() is chained into only()
    THEN a single model instance restricted to those columns is returned
    """
    _, b1 = await _seed_mc()
    book = await McBook.filter(title="B1").first().only("id", "title")
    assert book is not None
    assert book.id == b1.id
    assert book.title == "B1"


@pytest.mark.asyncio
async def test_first_then_values_list_flat_returns_scalar(db):
    """
    GIVEN matching rows
    WHEN first() is chained into values_list(flat=True)
    THEN the single scalar value is returned
    """
    _, b1 = await _seed_mc()
    assert await McBook.filter(title="B1").first().values_list("id", flat=True) == b1.id


@pytest.mark.asyncio
async def test_first_projection_on_no_match_returns_none(db):
    """
    GIVEN no matching row
    WHEN first() is chained into a projection
    THEN the result resolves to None rather than raising
    """
    await _seed_mc()
    assert await McBook.filter(title="missing").first().values("title") is None
    assert await McBook.filter(title="missing").first().only("id") is None
    assert await McBook.filter(title="missing").first() is None


@pytest.mark.asyncio
async def test_get_then_values_returns_single_dict(db):
    """
    GIVEN exactly one matching row
    WHEN get() is chained into values()
    THEN a single dict is returned
    """
    await _seed_mc()
    assert await McBook.get(title="B1").values("title") == {"title": "B1"}


@pytest.mark.asyncio
async def test_first_select_related_then_values_traverses_relation(db):
    """
    GIVEN a book linked to an author
    WHEN first() eager-loads the relation and values() selects a related column
    THEN the related value appears under the path key
    """
    await _seed_mc()
    row = (
        await McBook.filter(title="B1")
        .first()
        .select_related("author")
        .values("title", "author__name")
    )
    assert row == {"title": "B1", "author__name": "Ada"}


@pytest.mark.asyncio
async def test_async_for_iterates_queryset(db):
    """
    GIVEN a queryset
    WHEN iterated with async for
    THEN each matching instance is yielded
    """
    await _seed_mc()
    titles = [book.title async for book in McBook.all().order_by("title")]
    assert titles == ["B1", "B2"]


@pytest.mark.asyncio
async def test_update_accepts_coalesce_function(db):
    """
    GIVEN a row with a NULL column
    WHEN update() assigns a Coalesce(column, fallback) function expression
    THEN the fallback is written rather than raising on the function value
    """
    from yara_orm import Coalesce

    ada = await McAuthor.create(name="Ada")
    book = await McBook.create(title="T", rating=1, note=None, author=ada)
    await McBook.filter(id=book.id).update(note=Coalesce("note", "fallback"))
    assert (await McBook.get(id=book.id)).note == "fallback"


@pytest.mark.asyncio
async def test_float_param_compares_against_int_column(db):
    """
    GIVEN an integer column and a float comparison literal
    WHEN filtering int_col <= 1.5 / <= 4.5
    THEN the float is compared as a float (not rounded or rejected)
    """
    await _seed_mc()  # ratings 4 and 5
    assert await McBook.filter(rating__lte=4.5).count() == 1
    assert await McBook.filter(rating__lte=5.5).count() == 2


@pytest.mark.asyncio
async def test_annotated_grouped_filter_param_binds(db):
    """
    GIVEN a filter param combined with an annotation and group_by
    WHEN the grouped SELECT is built and run
    THEN the filter param binds correctly alongside the annotation
    """
    await _seed_mc()
    rows = await McBook.filter(title="B1").annotate(c=Count("id")).group_by("author_id").values("c")
    assert rows == [{"c": 1}]


@pytest.mark.asyncio
async def test_values_is_awaitable_async_iterable_and_first(db):
    """
    GIVEN a projection over a queryset
    WHEN values() is awaited, iterated with async for, and .first() is called
    THEN all three forms work and .first() returns the row or None
    """
    await _seed_mc()
    listed = await McBook.filter(title="B1").values("title")
    assert listed == [{"title": "B1"}]
    streamed = [r async for r in McBook.all().order_by("title").values("title")]
    assert streamed == [{"title": "B1"}, {"title": "B2"}]
    assert await McBook.filter(title="B1").values("title").first() == {"title": "B1"}
    assert await McBook.filter(title="missing").values("title").first() is None


@pytest.mark.asyncio
async def test_values_list_is_awaitable_async_iterable_and_first(db):
    """
    GIVEN a projection over a queryset
    WHEN values_list() is awaited, iterated with async for, and .first() is called
    THEN all three forms work and .first() returns the first tuple or None
    """
    await _seed_mc()
    listed = await McBook.all().order_by("title").values_list("title", flat=True)
    assert listed == ["B1", "B2"]
    streamed = [t async for t in McBook.all().order_by("title").values_list("title", flat=True)]
    assert streamed == ["B1", "B2"]
    assert await McBook.filter(title="B1").values_list("title").first() == ("B1",)
    assert await McBook.filter(title="missing").values_list("title").first() is None


@pytest.mark.asyncio
async def test_only_restricts_base_columns_with_select_related(db):
    """
    GIVEN a query that eager-loads a relation and restricts base columns
    WHEN only() names base fields alongside select_related()
    THEN the base instance loads just those columns and the relation loads fully
    """
    await _seed_mc()
    [book] = (
        await McBook.filter(title="B1").select_related("author").only("id", "title", "author_id")
    )
    assert book.title == "B1"
    assert (await book.author).name == "Ada"
    with pytest.raises(FieldError):
        _ = book.rating  # deferred: not in only()


@pytest.mark.asyncio
async def test_defer_drops_base_columns_with_select_related(db):
    """
    GIVEN a query that eager-loads a relation and defers a base column
    WHEN defer() names a base field alongside select_related()
    THEN the deferred column is omitted and the relation still loads fully
    """
    await _seed_mc()
    [book] = await McBook.filter(title="B1").select_related("author").defer("rating")
    assert book.title == "B1"
    assert (await book.author).name == "Ada"
    with pytest.raises(FieldError):
        _ = book.rating  # deferred


@pytest.mark.asyncio
async def test_only_combines_with_annotate(db):
    """
    GIVEN only() combined with an aggregate annotate()
    WHEN the query runs
    THEN the narrowed base projection loads, the annotation is attached and
         the unselected columns stay deferred
    """
    await _seed_mc()
    [book] = await McBook.filter(title="B1").annotate(c=Count("id")).only("id", "title")
    assert book.title == "B1"
    assert book.c == 1
    with pytest.raises(FieldError):
        _ = book.rating  # deferred: not in only()

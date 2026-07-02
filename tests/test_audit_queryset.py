"""Regression tests for the queryset/expression audit fixes.

Each test guards one verified finding:
- delete()/update() now honour annotation (HAVING) filters and reject slices.
- count()/exists() honour HAVING, GROUP BY and LIMIT/OFFSET.
- Slicing/indexing composes relative to an existing window.
- Relation joins are aliased per path (two FKs to one table, self-FK).
- M2M ``__isnull`` compiles to a through-table membership test.
- select_for_update() is emitted (or rejected) on every select shape.
- LIKE/ILIKE lookups escape ``%``/``_``/``\\`` in user values.
- last() uses Meta.ordering and rejects sliced query sets.
- select_related() + annotate() compiles to one joined, annotated SELECT.
- exclude() targets annotations (negated HAVING).
- filter()/exclude() reject non-Q positional arguments.
- ``Subquery(qs.only("col"))`` projects exactly the named column.
- ``When()`` requires conditions; expressions support ``literal / F``.
"""

import pytest

from yara_orm import (
    Count,
    F,
    FieldError,
    Model,
    Subquery,
    Sum,
    UnSupportedError,
    When,
    fields,
    in_transaction,
)
from yara_orm.dialects import PostgresDialect
from yara_orm.expressions import CombinedExpression


class AudqAuthor(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "audq_author"


class AudqBook(Model):
    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=100)
    price = fields.IntField(default=0)
    genre = fields.CharField(max_length=20, default="none")
    author = fields.ForeignKeyField("AudqAuthor", related_name="books")

    class Meta:
        table = "audq_book"


class AudqUser(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "audq_user"


class AudqTag(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "audq_tag"


class AudqTicket(Model):
    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=100)
    created_by = fields.ForeignKeyField("AudqUser", related_name="created_tickets")
    updated_by = fields.ForeignKeyField("AudqUser", related_name="updated_tickets", null=True)
    parent = fields.ForeignKeyField("AudqTicket", related_name="children", null=True)
    tags = fields.ManyToManyField("AudqTag", related_name="tickets", through="audq_ticket_tag")

    class Meta:
        table = "audq_ticket"


class AudqNote(Model):
    id = fields.IntField(pk=True)
    body = fields.CharField(max_length=50)
    rank = fields.IntField(default=0)

    class Meta:
        table = "audq_note"
        ordering = ["rank"]


MODELS = [AudqAuthor, AudqBook, AudqUser, AudqTag, AudqTicket, AudqNote]


async def _seed_authors():
    """Seed three authors with 2 / 1 / 0 books.

    Returns:
        The created authors ``(ada, bob, cal)``.
    """
    ada = await AudqAuthor.create(name="Ada")
    bob = await AudqAuthor.create(name="Bob")
    cal = await AudqAuthor.create(name="Cal")
    await AudqBook.create(title="A1", price=10, genre="sci-fi", author=ada)
    await AudqBook.create(title="A2", price=30, genre="drama", author=ada)
    await AudqBook.create(title="B1", price=20, genre="sci-fi", author=bob)
    return ada, bob, cal


# ---------------------------------------------------------------------------
# delete()/update() with annotation filters (HAVING) and slices
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_delete_honours_annotation_filter(db):
    """
    GIVEN authors with 2, 1 and 0 books
    WHEN annotate(n=Count("books")).filter(n=0).delete() runs
    THEN only the bookless author is deleted (not the whole table)
    """
    ada, bob, cal = await _seed_authors()
    deleted = await AudqAuthor.annotate(n=Count("books")).filter(n=0).delete()
    assert deleted == 1
    remaining = {a.name for a in await AudqAuthor.all()}
    assert remaining == {"Ada", "Bob"}


@pytest.mark.asyncio
async def test_update_honours_annotation_filter(db):
    """
    GIVEN authors with 2, 1 and 0 books
    WHEN annotate(n=Count("books")).filter(n__gte=2).update(...) runs
    THEN only the multi-book author is updated
    """
    await _seed_authors()
    updated = await AudqAuthor.annotate(n=Count("books")).filter(n__gte=2).update(name="Popular")
    assert updated == 1
    names = sorted(a.name for a in await AudqAuthor.all())
    assert names == ["Bob", "Cal", "Popular"]


@pytest.mark.asyncio
async def test_delete_and_update_reject_sliced_querysets(db):
    """
    GIVEN a sliced query set
    WHEN delete() or update() is called on it
    THEN a TypeError is raised instead of silently ignoring the slice
    """
    await _seed_authors()
    with pytest.raises(TypeError):
        await AudqAuthor.all()[:1].delete()
    with pytest.raises(TypeError):
        await AudqAuthor.all()[1:].update(name="x")
    assert await AudqAuthor.all().count() == 3  # nothing was wiped


# ---------------------------------------------------------------------------
# count()/exists() with HAVING, GROUP BY and slices
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_count_honours_having(db):
    """
    GIVEN authors with 2, 1 and 0 books
    WHEN counting with an annotation filter (HAVING)
    THEN only the groups surviving the filter are counted
    """
    await _seed_authors()
    assert await AudqAuthor.annotate(n=Count("books")).filter(n__gte=2).count() == 1
    assert await AudqAuthor.annotate(n=Count("books")).filter(n__gte=1).count() == 2


@pytest.mark.asyncio
async def test_count_and_exists_honour_slice(db):
    """
    GIVEN three authors
    WHEN counting / probing existence on a sliced query set
    THEN the slice window bounds the result
    """
    await _seed_authors()
    assert await AudqAuthor.all()[:2].count() == 2
    assert await AudqAuthor.all()[10:].count() == 0
    assert await AudqAuthor.all()[1:].exists() is True
    assert await AudqAuthor.all()[10:].exists() is False


@pytest.mark.asyncio
async def test_count_counts_groups_under_group_by(db):
    """
    GIVEN books in two genres
    WHEN counting a group_by("genre") query set
    THEN the number of groups is returned, not the number of rows
    """
    await _seed_authors()
    assert await AudqBook.all().group_by("genre").count() == 2


# ---------------------------------------------------------------------------
# Slice / index composition
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_slices_compose_relative_to_existing_window(db):
    """
    GIVEN ten ordered notes
    WHEN slices are chained (and combined with offset())
    THEN each slice applies to the previous window, not the whole table
    """
    for i in range(1, 11):
        await AudqNote.create(body=f"n{i}", rank=i)
    qs = AudqNote.filter().order_by("rank")
    assert [n.rank for n in await qs[2:8][1:3]] == [4, 5]
    assert [n.rank for n in await qs.offset(5)[:3]] == [6, 7, 8]
    assert [n.rank for n in await qs[7:]] == [8, 9, 10]


@pytest.mark.asyncio
async def test_empty_or_inverted_slice_yields_no_rows(db):
    """
    GIVEN three notes
    WHEN an inverted (start > stop) or empty slice is taken
    THEN it resolves to no rows instead of LIMIT -2 / everything
    """
    for i in range(1, 4):
        await AudqNote.create(body=f"n{i}", rank=i)
    assert await AudqNote.filter()[5:3] == []
    assert await AudqNote.filter()[2:2] == []


@pytest.mark.asyncio
async def test_index_composes_with_existing_window(db):
    """
    GIVEN ten ordered notes
    WHEN an integer index is applied after a slice
    THEN it indexes into the sliced window (and IndexError past its end)
    """
    for i in range(1, 11):
        await AudqNote.create(body=f"n{i}", rank=i)
    qs = AudqNote.filter().order_by("rank")
    assert (await qs[2:][3]).rank == 6
    with pytest.raises(IndexError):
        await qs[:2][5]


# ---------------------------------------------------------------------------
# Aliased relation joins (two FKs to one table, self-FK)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_values_over_two_fks_to_same_table(db):
    """
    GIVEN a ticket whose created_by and updated_by point at the users table
    WHEN both relation paths are projected in one values() call
    THEN each path resolves through its own aliased join
    """
    maker = await AudqUser.create(name="maker")
    editor = await AudqUser.create(name="editor")
    await AudqTicket.create(title="t1", created_by=maker, updated_by=editor)
    [row] = await AudqTicket.filter(title="t1").values("created_by__name", "updated_by__name")
    assert row == {"created_by__name": "maker", "updated_by__name": "editor"}


@pytest.mark.asyncio
async def test_values_over_self_fk(db):
    """
    GIVEN a ticket with a parent ticket (self-FK)
    WHEN the parent's column is projected via values()
    THEN the self-join is aliased and resolves the parent's row
    """
    maker = await AudqUser.create(name="maker")
    parent = await AudqTicket.create(title="root", created_by=maker)
    await AudqTicket.create(title="child", created_by=maker, parent=parent)
    [row] = await AudqTicket.filter(title="child").values("parent__title")
    assert row == {"parent__title": "root"}


# ---------------------------------------------------------------------------
# M2M __isnull
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_m2m_isnull(db):
    """
    GIVEN one tagged and one untagged ticket
    WHEN filtering on tags__isnull
    THEN True selects the untagged ticket and False the tagged one
    """
    maker = await AudqUser.create(name="maker")
    tagged = await AudqTicket.create(title="tagged", created_by=maker)
    await AudqTicket.create(title="bare", created_by=maker)
    tag = await AudqTag.create(name="red")
    await tagged.tags.add(tag)
    assert [t.title for t in await AudqTicket.filter(tags__isnull=True)] == ["bare"]
    assert [t.title for t in await AudqTicket.filter(tags__isnull=False)] == ["tagged"]


# ---------------------------------------------------------------------------
# select_for_update() coverage on every select shape
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_select_for_update_emitted_on_joined_and_values_shapes(db):
    """
    GIVEN select_for_update() query sets in the select_related / values shapes
    WHEN they compile for PostgreSQL
    THEN the lock clause is present (OF the base table under a LEFT JOIN)
    """
    dialect = PostgresDialect()
    qs = AudqTicket.filter().select_related("created_by").select_for_update()
    sql, *_ = qs._select_related_plan(dialect)
    assert 'FOR UPDATE OF "audq_ticket"' in sql
    sql, _, _ = AudqTicket.filter().select_for_update()._projection_select_sql(("title",), dialect)
    assert sql.rstrip().endswith("FOR UPDATE")


@pytest.mark.asyncio
async def test_select_for_update_rejected_with_annotate_and_group_by(db):
    """
    GIVEN select_for_update() combined with annotate() / group_by()
    WHEN the query executes
    THEN a clear error is raised instead of silently dropping the lock
    """
    await _seed_authors()
    with pytest.raises(UnSupportedError):
        await AudqAuthor.annotate(n=Count("books")).select_for_update()
    with pytest.raises(UnSupportedError):
        await AudqBook.filter().group_by("genre").select_for_update().values("genre")


# ---------------------------------------------------------------------------
# LIKE/ILIKE wildcard escaping
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_like_lookups_escape_wildcards(db):
    """
    GIVEN names containing literal %, _ and plain lookalikes
    WHEN pattern lookups receive % / _ in the user value
    THEN they match literally instead of acting as wildcards
    """
    for name in ("100%", "100x", "a_b", "axb"):
        await AudqAuthor.create(name=name)
    assert [a.name for a in await AudqAuthor.filter(name__contains="0%")] == ["100%"]
    assert [a.name for a in await AudqAuthor.filter(name__startswith="a_")] == ["a_b"]
    assert [a.name for a in await AudqAuthor.filter(name__icontains="_")] == ["a_b"]
    assert [a.name for a in await AudqAuthor.filter(name__iexact="100%")] == ["100%"]
    assert [a.name for a in await AudqAuthor.filter(name__endswith="_b")] == ["a_b"]


@pytest.mark.asyncio
async def test_like_lookups_escape_backslash(db):
    """
    GIVEN a name containing a literal backslash and a plain one
    WHEN a pattern lookup receives a backslash in the user value
    THEN it matches only the literal backslash
    """
    await AudqAuthor.create(name="a\\b")
    await AudqAuthor.create(name="ab")
    assert [a.name for a in await AudqAuthor.filter(name__contains="\\")] == ["a\\b"]


@pytest.mark.asyncio
async def test_iexact_still_matches_case_insensitively(db):
    """
    GIVEN an author named Ada
    WHEN iexact is used with a differently-cased value
    THEN it still matches (escaping did not break the lookup)
    """
    await AudqAuthor.create(name="Ada")
    assert (await AudqAuthor.filter(name__iexact="aDA").first()).name == "Ada"


# ---------------------------------------------------------------------------
# last()
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_last_uses_meta_ordering(db):
    """
    GIVEN notes with Meta.ordering by ascending rank (created out of order)
    WHEN last() is called with no explicit order_by
    THEN the Meta.ordering is reversed (highest rank), not pk-descending
    """
    await AudqNote.create(body="mid", rank=3)
    await AudqNote.create(body="top", rank=5)
    await AudqNote.create(body="low", rank=1)  # newest row, lowest rank
    assert (await AudqNote.filter().last()).rank == 5
    assert (await AudqNote.filter().order_by("-rank").last()).rank == 1


@pytest.mark.asyncio
async def test_last_rejects_sliced_queryset(db):
    """
    GIVEN a sliced query set
    WHEN last() is called
    THEN a TypeError is raised (reversing a slice is ambiguous)
    """
    await AudqNote.create(body="n", rank=1)
    with pytest.raises(TypeError):
        await AudqNote.filter()[1:].last()


# ---------------------------------------------------------------------------
# select_related + annotate
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_select_related_with_annotate_works(db):
    """
    GIVEN a query combining select_related() with an aggregate annotate()
    WHEN it executes
    THEN the joins are kept: each instance carries the hydrated relation and
         the annotation value (grouped by the base and joined pks)
    """
    await _seed_authors()
    books = await AudqBook.annotate(n=Count("id")).select_related("author").order_by("title")
    assert [b.title for b in books] == ["A1", "A2", "B1"]
    assert [b.author.name for b in books] == ["Ada", "Ada", "Bob"]
    assert [b.n for b in books] == [1, 1, 1]  # grouped per book row, not inflated


# ---------------------------------------------------------------------------
# exclude() and filter() argument handling
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_exclude_targets_annotation(db):
    """
    GIVEN authors with 2, 1 and 0 books
    WHEN exclude() names the annotation
    THEN it compiles to a negated HAVING clause
    """
    await _seed_authors()
    names = {a.name for a in await AudqAuthor.annotate(n=Count("books")).exclude(n__gte=2)}
    assert names == {"Bob", "Cal"}


@pytest.mark.asyncio
async def test_exclude_rejects_mixed_annotation_and_column_lookups(db):
    """
    GIVEN an exclude() mixing an annotation lookup with a column lookup
    WHEN the query set is built
    THEN a FieldError is raised (the negation cannot be split soundly)
    """
    await _seed_authors()
    with pytest.raises(FieldError):
        AudqAuthor.annotate(n=Count("books")).exclude(n__gte=2, name="Ada")


@pytest.mark.asyncio
async def test_filter_and_exclude_reject_non_q_positional_args(db):
    """
    GIVEN a non-Q positional argument
    WHEN passed to filter() or exclude()
    THEN a TypeError is raised instead of silently discarding it
    """
    with pytest.raises(TypeError):
        AudqAuthor.filter().filter({"name": "Ada"})
    with pytest.raises(TypeError):
        AudqAuthor.filter().exclude({"name": "Ada"})


# ---------------------------------------------------------------------------
# Subquery(qs.only(...)) projection
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_subquery_only_projects_exactly_the_named_column(db):
    """
    GIVEN a Subquery over qs.only("title") used as an IN membership set
    WHEN an author name matches a book title
    THEN the subquery selects the single named column (no pk prepended)
    """
    ada = await AudqAuthor.create(name="Ada")
    await AudqAuthor.create(name="Bob")
    await AudqBook.create(title="Ada", author=ada)  # a book titled like an author
    matches = await AudqAuthor.filter(name__in=Subquery(AudqBook.filter().only("title")))
    assert [a.name for a in matches] == ["Ada"]


# ---------------------------------------------------------------------------
# When() and reflected division
# ---------------------------------------------------------------------------
def test_when_requires_conditions():
    """
    GIVEN a When with a then-value but no conditions
    WHEN it is constructed
    THEN a ValueError is raised (it would render ``WHEN  THEN ...``)
    """
    with pytest.raises(ValueError):
        When(then=1)


@pytest.mark.asyncio
async def test_expression_rtruediv(db):
    """
    GIVEN a literal divided by an F expression
    WHEN used in an update()
    THEN it builds a combined expression and computes literal / column
    """
    expr = 100 / F("price")
    assert isinstance(expr, CombinedExpression)
    ada = await AudqAuthor.create(name="Ada")
    book = await AudqBook.create(title="T", price=20, author=ada)
    await AudqBook.filter(id=book.id).update(price=100 / F("price"))
    assert (await AudqBook.get(id=book.id)).price == 5


# ---------------------------------------------------------------------------
# select_for_update() on the values() shape with a joined relation path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_select_for_update_values_with_joined_path_locks_base_table(db):
    """
    GIVEN a select_for_update() values() query traversing a relation
    WHEN it executes inside a transaction
    THEN the lock narrows to FOR UPDATE OF the base table (PostgreSQL rejects
         locking the nullable side of the LEFT JOIN) and rows still return
    """
    user = await AudqUser.create(name="owner")
    await AudqTicket.create(title="locked", created_by=user)
    async with in_transaction():
        rows = (
            await AudqTicket.filter(title="locked")
            .select_for_update()
            .values("title", "created_by__name")
        )
    assert len(rows) == 1
    assert rows[0]["title"] == "locked"
    assert "owner" in rows[0].values()


# ---------------------------------------------------------------------------
# HAVING: a multi-lookup conjunction group is parenthesised
# ---------------------------------------------------------------------------


def test_having_multi_lookup_group_renders_parenthesised():
    """
    GIVEN a HAVING group holding several lookups without negation
    WHEN the HAVING clause compiles
    THEN the conjunction is wrapped in parentheses so it composes soundly
         with any further HAVING groups ANDed after it
    """
    qs = AudqBook.annotate(n=Count("id"), s=Sum("price")).group_by("genre")
    qs._having = [([("n", "gt", 1), ("s", "lt", 100)], False)]
    having, params, _ = qs._compile_having(PostgresDialect(), 1, {})
    assert having.startswith(" HAVING (")
    assert having.endswith(")")
    assert " AND " in having
    assert params == [1, 100]

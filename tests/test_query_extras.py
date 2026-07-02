"""Query features for parity with the documented behavior: relation-spanning filters, extra lookups,
only/defer, get_or_none/get_or_create/update_or_create, sql/explain, using_db,
select_for_update options and Subquery.
"""

import datetime as dt

import pytest

from yara_orm import (
    ConfigurationError,
    Count,
    FieldError,
    Model,
    Subquery,
    UnSupportedError,
    fields,
)
from yara_orm.dialects import PostgresDialect, SqliteDialect


class QxCountry(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "qx_country"


class QxAuthor(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)
    bio = fields.CharField(max_length=200, null=True)
    country = fields.ForeignKeyField("QxCountry", related_name="authors", null=True)

    class Meta:
        table = "qx_author"


class QxTag(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "qx_tag"


class QxBook(Model):
    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=50)
    rating = fields.IntField(default=0)
    published = fields.DatetimeField(null=True)
    author = fields.ForeignKeyField("QxAuthor", related_name="books")
    tags = fields.ManyToManyField("QxTag", related_name="books", through="qx_book_tag")

    class Meta:
        table = "qx_book"


MODELS = [QxCountry, QxAuthor, QxTag, QxBook]


async def _seed():
    us = await QxCountry.create(name="US")
    uk = await QxCountry.create(name="UK")
    ada = await QxAuthor.create(name="Ada", bio="pioneer", country=uk)
    bob = await QxAuthor.create(name="Bob", country=us)
    b1 = await QxBook.create(
        title="B1",
        rating=5,
        author=ada,
        published=dt.datetime(2024, 5, 1, 9, tzinfo=dt.timezone.utc),
    )
    b2 = await QxBook.create(
        title="B2", rating=2, author=bob, published=dt.datetime(2023, 1, 2, tzinfo=dt.timezone.utc)
    )
    py = await QxTag.create(name="python")
    await b1.tags.add(py)
    return {"us": us, "uk": uk, "ada": ada, "bob": bob, "b1": b1, "b2": b2, "py": py}


# -- relation-spanning filters -------------------------------------------------


@pytest.mark.asyncio
async def test_filter_forward_fk(db):
    """
    GIVEN books linked to authors
    WHEN filtering on a forward-FK field path
    THEN only books whose author matches are returned
    """
    await _seed()
    assert [b.title for b in await QxBook.filter(author__name="Ada")] == ["B1"]
    assert [b.title for b in await QxBook.filter(author__name__icontains="bo")] == ["B2"]


@pytest.mark.asyncio
async def test_filter_multi_level_forward(db):
    """
    GIVEN a book -> author -> country chain
    WHEN filtering across two relations
    THEN the deep condition is applied
    """
    await _seed()
    assert [b.title for b in await QxBook.filter(author__country__name="UK")] == ["B1"]


@pytest.mark.asyncio
async def test_filter_reverse_fk(db):
    """
    GIVEN authors with books
    WHEN filtering an author by a reverse-FK book field
    THEN matching authors are returned
    """
    await _seed()
    assert [a.name for a in await QxAuthor.filter(books__rating__gte=5)] == ["Ada"]


@pytest.mark.asyncio
async def test_filter_m2m_span_both_directions(db):
    """
    GIVEN a book tagged 'python'
    WHEN filtering across the m2m relation in each direction
    THEN the related rows are returned
    """
    await _seed()
    assert [b.title for b in await QxBook.filter(tags__name="python")] == ["B1"]
    assert [t.name for t in await QxTag.filter(books__title="B1")] == ["python"]


@pytest.mark.asyncio
async def test_exclude_across_relation(db):
    """
    GIVEN two books by different authors
    WHEN excluding by a forward-FK field
    THEN the matching book is removed
    """
    await _seed()
    assert [b.title for b in await QxBook.exclude(author__name="Ada")] == ["B2"]


@pytest.mark.asyncio
async def test_filter_unknown_relation_raises(db):
    """
    GIVEN a model
    WHEN filtering across a name that is not a relation
    THEN a FieldError is raised
    """
    await _seed()
    with pytest.raises(FieldError):
        await QxBook.filter(nope__name="x").count()


# -- extra field lookups -------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_iexact_range_not_in(db):
    """
    GIVEN seeded rows
    WHEN using iexact / range / not_in / in(empty)
    THEN each lookup filters as expected
    """
    await _seed()
    assert [a.name for a in await QxAuthor.filter(name__iexact="ada")] == ["Ada"]
    assert sorted(b.title for b in await QxBook.filter(rating__range=(3, 9))) == ["B1"]
    assert [b.title for b in await QxBook.filter(title__not_in=["B1"])] == ["B2"]
    assert await QxBook.filter(rating__in=[]).count() == 0
    assert await QxBook.filter(rating__not_in=[]).count() == 2


@pytest.mark.asyncio
async def test_lookup_date_parts(db):
    """
    GIVEN rows with datetime columns
    WHEN filtering on extracted date/time parts
    THEN rows with the matching part are returned
    """
    await _seed()
    assert [b.title for b in await QxBook.filter(published__year=2024)] == ["B1"]
    assert [b.title for b in await QxBook.filter(published__month=5)] == ["B1"]
    assert [b.title for b in await QxBook.filter(published__day=1)] == ["B1"]
    assert [b.title for b in await QxBook.filter(published__hour=9)] == ["B1"]


@pytest.mark.asyncio
async def test_regex_and_search_unsupported_on_sqlite(db):
    """
    GIVEN the SQLite backend
    WHEN using regex / search lookups
    THEN an UnSupportedError is raised (PostgreSQL supports them)
    """
    if db != "sqlite":
        pytest.skip("sqlite-only guard test")
    await _seed()
    with pytest.raises(UnSupportedError):
        await QxBook.filter(title__regex="^B").count()
    with pytest.raises(UnSupportedError):
        await QxBook.filter(title__iregex="^b").count()
    with pytest.raises(UnSupportedError):
        await QxBook.filter(title__search="hello").count()


# -- only() / defer() ----------------------------------------------------------


@pytest.mark.asyncio
async def test_only_loads_subset(db):
    """
    GIVEN an author with several columns
    WHEN fetched via only('name')
    THEN name (and pk) load, and a deferred field raises on access
    """
    await _seed()
    a = (await QxAuthor.filter(name="Ada").only("name"))[0]
    assert a.id is not None and a.name == "Ada"
    with pytest.raises(FieldError):
        _ = a.bio


@pytest.mark.asyncio
async def test_defer_omits_field(db):
    """
    GIVEN an author
    WHEN fetched via defer('bio')
    THEN bio is not loaded but other fields are
    """
    await _seed()
    a = (await QxAuthor.filter(name="Ada").defer("bio"))[0]
    assert a.name == "Ada"
    with pytest.raises(FieldError):
        _ = a.bio


@pytest.mark.asyncio
async def test_only_unknown_field_raises(db):
    """
    GIVEN a model
    WHEN only() names a non-existent field
    THEN a FieldError is raised
    """
    await _seed()
    with pytest.raises(FieldError):
        QxAuthor.all().only("nope")
    with pytest.raises(FieldError):
        QxAuthor.all().defer("nope")


@pytest.mark.asyncio
async def test_only_with_annotate_combines(db):
    """
    GIVEN a query combining only() and an aggregate annotate()
    WHEN it is executed
    THEN the annotation rides along the narrowed projection and the
         unselected columns stay deferred
    """
    await _seed()
    authors = await QxAuthor.all().annotate(n=Count("books")).only("name").order_by("name")
    assert [(a.name, a.n) for a in authors] == [("Ada", 1), ("Bob", 1)]
    with pytest.raises(FieldError):
        _ = authors[0].bio  # deferred: not in only()


@pytest.mark.asyncio
async def test_only_distinct_combines(db):
    """
    GIVEN duplicate names projected via only()
    WHEN distinct() is also applied
    THEN the SELECT is DISTINCT and rows load
    """
    await _seed()
    rows = await QxAuthor.all().only("name").distinct()
    assert {a.name for a in rows} == {"Ada", "Bob"}


# -- get_or_none / get_or_create / update_or_create ----------------------------


@pytest.mark.asyncio
async def test_queryset_get_or_none(db):
    """
    GIVEN a queryset
    WHEN get_or_none matches zero / one / many rows
    THEN it returns None / the row / raises
    """
    await _seed()
    assert await QxAuthor.filter(name="Zed").get_or_none() is None
    assert (await QxAuthor.all().get_or_none(name="Ada")).name == "Ada"
    from yara_orm import MultipleObjectsReturned

    with pytest.raises(MultipleObjectsReturned):
        await QxBook.all().get_or_none()


@pytest.mark.asyncio
async def test_queryset_get_or_create(db):
    """
    GIVEN a queryset
    WHEN get_or_create is called for a missing then existing row
    THEN it creates then fetches, reporting the created flag
    """
    await _seed()
    author = await QxAuthor.get(name="Ada")
    obj, created = await QxBook.all().get_or_create(title="New", defaults={"author": author})
    assert created is True and obj.title == "New"
    obj2, created2 = await QxBook.all().get_or_create(title="New", defaults={"author": author})
    assert created2 is False and obj2.pk == obj.pk


@pytest.mark.asyncio
async def test_queryset_update_or_create(db):
    """
    GIVEN a queryset
    WHEN update_or_create targets a missing then existing row
    THEN it creates, then updates in place
    """
    await _seed()
    obj, created = await QxAuthor.all().update_or_create(defaults={"bio": "x"}, name="Carol")
    assert created is True and obj.bio == "x"
    obj2, created2 = await QxAuthor.all().update_or_create(defaults={"bio": "y"}, name="Carol")
    assert created2 is False and obj2.bio == "y"
    assert (await QxAuthor.get(name="Carol")).bio == "y"
    # No defaults on an existing row: a plain fetch, nothing written.
    obj3, created3 = await QxAuthor.all().update_or_create(name="Carol")
    assert created3 is False and obj3.bio == "y"


# -- sql() / explain() ---------------------------------------------------------


@pytest.mark.asyncio
async def test_sql_and_explain(db):
    """
    GIVEN a query set
    WHEN sql() and explain() are called
    THEN sql() returns the statement and explain() returns a plan
    """
    await _seed()
    text = QxBook.filter(rating__gte=3).sql()
    assert text.startswith("SELECT") and "qx_book" in text
    assert isinstance(await QxBook.filter(rating__gte=3).explain(), str)


@pytest.mark.asyncio
async def test_sql_explain_unsupported_with_annotate(db):
    """
    GIVEN an annotated query
    WHEN sql()/explain() are called
    THEN an UnSupportedError is raised
    """
    await _seed()
    with pytest.raises(UnSupportedError):
        QxAuthor.all().annotate(n=Count("books")).sql()
    with pytest.raises(UnSupportedError):
        await QxAuthor.all().annotate(n=Count("books")).explain()


# -- using_db ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_using_db_default_and_unknown(db):
    """
    GIVEN the default connection
    WHEN using_db names it (then an unknown one)
    THEN the query runs (then raises ConfigurationError)
    """
    await _seed()
    assert await QxBook.all().using_db("default").count() == 2
    with pytest.raises(ConfigurationError):
        await QxBook.all().using_db("nope").count()


# -- Subquery ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subquery_in_annotation(db):
    """
    GIVEN a single-row subquery selecting an author id
    WHEN used in annotate()
    THEN every row carries the subquery's scalar value
    """
    await _seed()
    ada = await QxAuthor.get(name="Ada")
    rows = (
        await QxBook.all()
        .annotate(ada_id=Subquery(QxAuthor.filter(name="Ada").only("id")))
        .values("title", "ada_id")
    )
    assert all(r["ada_id"] == ada.id for r in rows)


# -- select_for_update rendering (dialect unit tests, no DB) -------------------


def test_select_for_update_lock_sql_postgres():
    """
    GIVEN select_for_update with options
    WHEN the lock clause is rendered for PostgreSQL
    THEN NOWAIT / SKIP LOCKED / OF are emitted correctly
    """
    pg = PostgresDialect()
    assert QxBook.all().select_for_update()._lock_sql(pg) == " FOR UPDATE"
    assert QxBook.all().select_for_update(nowait=True)._lock_sql(pg) == " FOR UPDATE NOWAIT"
    assert (
        QxBook.all().select_for_update(skip_locked=True)._lock_sql(pg) == " FOR UPDATE SKIP LOCKED"
    )
    assert (
        QxBook.all().select_for_update(of=("qx_book",))._lock_sql(pg) == ' FOR UPDATE OF "qx_book"'
    )


def test_select_for_update_noop_on_sqlite():
    """
    GIVEN select_for_update
    WHEN the lock clause is rendered for SQLite
    THEN it is a no-op (empty string)
    """
    assert QxBook.all().select_for_update(nowait=True)._lock_sql(SqliteDialect()) == ""


# -- dialect lookup rendering (unit tests, no DB) ------------------------------


def test_postgres_lookup_rendering():
    """
    GIVEN the PostgreSQL dialect
    WHEN rendering regex / date-part / full-text fragments
    THEN the expected SQL is produced
    """
    pg = PostgresDialect()
    assert pg.regex_ops["regex"] == "~" and pg.regex_ops["iregex"] == "~*"
    assert pg.regex_ops["posix_regex"] == "~" and pg.regex_ops["iposix_regex"] == "~*"
    assert pg.supports_search is True
    assert pg.date_part_sql("year", '"t"."c"') == 'EXTRACT(YEAR FROM "t"."c")'
    assert "to_tsvector" in pg.search_sql('"c"', "$1")


def test_compile_field_op_regex_postgres():
    """
    GIVEN the PostgreSQL dialect
    WHEN a regex lookup is compiled
    THEN the ``~`` operator and bound value are produced
    """
    qs = QxBook.all()
    field = QxBook._meta.get_field("title")
    sql, params, idx = qs._compile_field_op(
        '"t"."title"', field, "regex", "^B", PostgresDialect(), 1
    )
    assert sql == '"t"."title" ~ $1'
    assert params == ["^B"] and idx == 2


def test_selected_fields_defaults_to_all():
    """
    GIVEN a query set with neither only() nor defer()
    WHEN its selected fields are computed
    THEN every model field is selected
    """
    assert QxBook.all()._selected_fields() == QxBook._meta.field_list


def test_sqlite_lookup_rendering():
    """
    GIVEN the SQLite dialect
    WHEN rendering a date part and attempting full-text search
    THEN strftime is used and search raises UnSupportedError
    """
    sq = SqliteDialect()
    assert sq.date_part_sql("year", '"c"') == "CAST(strftime('%Y', \"c\") AS INTEGER)"
    assert sq.regex_ops == {}
    with pytest.raises(UnSupportedError):
        sq.search_sql('"c"', "?1")

"""Second-pass parity gaps: Prefetch(to_attr), values()/values_list()
relation traversal, and bulk_create upsert (ignore_conflicts / update_fields /
on_conflict)."""

import pytest

from yara_orm import Model, Prefetch, fields


class PgAuthor(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "pg_author"


class PgTag(Model):
    id = fields.IntField(pk=True)
    label = fields.CharField(max_length=50)

    class Meta:
        table = "pg_tag"


class PgBook(Model):
    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=50)
    code = fields.CharField(max_length=20, null=True, unique=True)
    author = fields.ForeignKeyField("PgAuthor", related_name="books")
    tags = fields.ManyToManyField("PgTag", related_name="books", through="pg_book_tag")

    class Meta:
        table = "pg_book"


class PgStat(Model):
    id = fields.IntField(pk=True)
    key = fields.CharField(max_length=50, unique=True)
    hits = fields.IntField(default=0)

    class Meta:
        table = "pg_stat"


class PgCoded(Model):
    # A non-auto-increment primary key: bulk_create sends it, so it is itself a
    # valid conflict target (unlike an auto pk) and needs no unique-column
    # substitution on SQL Server's MERGE.
    code = fields.CharField(max_length=20, pk=True)
    hits = fields.IntField(default=0)

    class Meta:
        table = "pg_coded"


class PgBare(Model):
    # An auto pk and no unique columns: SQL Server's MERGE has no legal conflict
    # target (the auto pk is never inserted), so an upsert must raise.
    id = fields.IntField(pk=True)
    hits = fields.IntField(default=0)

    class Meta:
        table = "pg_bare"


MODELS = [PgAuthor, PgTag, PgBook, PgStat, PgCoded, PgBare]


async def _seed():
    ada = await PgAuthor.create(name="Ada")
    b1 = await PgBook.create(title="B1", author=ada)
    await PgBook.create(title="B2", author=ada)
    tag = await PgTag.create(label="py")
    await b1.tags.add(tag)
    return ada, b1, tag


# -- values()/values_list() relation traversal --------------------------------


@pytest.mark.asyncio
async def test_values_traverses_relation(db):
    """
    GIVEN books linked to an author
    WHEN values() selects a related-model column
    THEN the related value appears under the path key
    """
    await _seed()
    rows = await PgBook.all().order_by("title").values("title", "author__name")
    assert rows == [
        {"title": "B1", "author__name": "Ada"},
        {"title": "B2", "author__name": "Ada"},
    ]


@pytest.mark.asyncio
async def test_values_keyword_alias(db):
    """
    GIVEN a related-model column
    WHEN values() aliases it with a keyword argument
    THEN the alias is used as the dict key
    """
    await _seed()
    rows = await PgBook.all().order_by("title").values("title", author_name="author__name")
    assert rows[0] == {"title": "B1", "author_name": "Ada"}


@pytest.mark.asyncio
async def test_values_list_traverses_relation(db):
    """
    GIVEN books linked to an author
    WHEN values_list() selects a related column (flat and tuple)
    THEN the related values come back
    """
    await _seed()
    assert await PgBook.all().order_by("title").values_list("author__name", flat=True) == [
        "Ada",
        "Ada",
    ]
    assert await PgBook.all().order_by("title").values_list("title", "author__name") == [
        ("B1", "Ada"),
        ("B2", "Ada"),
    ]


# -- Prefetch(to_attr) --------------------------------------------------------


@pytest.mark.asyncio
async def test_prefetch_to_attr_reverse_fk(db):
    """
    GIVEN an author with books
    WHEN prefetching a constrained reverse FK into a custom attribute
    THEN the attribute holds the filtered result
    """
    ada, _, _ = await _seed()
    authors = await PgAuthor.all().prefetch_related(
        Prefetch("books", PgBook.filter(title="B1"), to_attr="b1_books")
    )
    assert [b.title for b in authors[0].b1_books] == ["B1"]


@pytest.mark.asyncio
async def test_prefetch_to_attr_forward_fk(db):
    """
    GIVEN books with a forward FK
    WHEN prefetching the FK into a custom attribute
    THEN the attribute holds the related instance
    """
    await _seed()
    books = await PgBook.all().prefetch_related(Prefetch("author", PgAuthor.all(), to_attr="who"))
    assert books[0].who.name == "Ada"


@pytest.mark.asyncio
async def test_prefetch_to_attr_m2m(db):
    """
    GIVEN a book with tags
    WHEN prefetching the m2m into a custom attribute
    THEN the attribute holds the related rows
    """
    await _seed()
    books = await PgBook.filter(title="B1").prefetch_related(
        Prefetch("tags", PgTag.all(), to_attr="my_tags")
    )
    assert [t.label for t in books[0].my_tags] == ["py"]


# -- bulk_create upsert -------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_create_plain_still_populates_pks(db):
    """
    GIVEN no conflict handling
    WHEN bulk_create inserts rows
    THEN primary keys are written back (regression guard)
    """
    rows = await PgStat.bulk_create([PgStat(key="x"), PgStat(key="y")])
    assert all(r.pk is not None for r in rows)


@pytest.mark.asyncio
async def test_bulk_create_ignore_conflicts(db):
    """
    GIVEN an existing unique key
    WHEN bulk_create runs with ignore_conflicts
    THEN the conflicting row is skipped and the new one is inserted
    """
    await PgStat.create(key="a", hits=1)
    await PgStat.bulk_create(
        [PgStat(key="a", hits=99), PgStat(key="b", hits=5)], ignore_conflicts=True
    )
    rows = {s.key: s.hits for s in await PgStat.all()}
    assert rows == {"a": 1, "b": 5}  # 'a' untouched, 'b' inserted


@pytest.mark.asyncio
async def test_bulk_create_upsert_update_fields(db):
    """
    GIVEN an existing unique key
    WHEN bulk_create upserts with update_fields/on_conflict
    THEN the existing row is updated and the new one inserted
    """
    await PgStat.create(key="a", hits=1)
    await PgStat.bulk_create(
        [PgStat(key="a", hits=99), PgStat(key="b", hits=5)],
        update_fields=["hits"],
        on_conflict=["key"],
    )
    rows = {s.key: s.hits for s in await PgStat.all()}
    assert rows == {"a": 99, "b": 5}


@pytest.mark.asyncio
async def test_bulk_create_update_fields_defaults_conflict_to_pk(db):
    """
    GIVEN update_fields without on_conflict
    WHEN bulk_create runs
    THEN the conflict target defaults to the primary key (statement is valid)
    """
    # Auto pk is not inserted, so no conflict actually fires; this exercises the
    # default-target branch and must run cleanly.
    await PgStat.bulk_create([PgStat(key="solo", hits=1)], update_fields=["hits"])
    assert await PgStat.filter(key="solo").count() == 1


@pytest.mark.asyncio
async def test_bulk_create_upsert_manual_pk_is_its_own_conflict_target(db):
    """
    GIVEN a model with a non-auto-increment primary key
    WHEN bulk_create upserts with update_fields and no explicit on_conflict
    THEN the pk itself is the conflict target (no unique-column substitution)
         and the existing row is updated in place
    """
    await PgCoded.create(code="x", hits=1)
    await PgCoded.bulk_create(
        [PgCoded(code="x", hits=9), PgCoded(code="y", hits=2)],
        update_fields=["hits"],
    )
    rows = {r.code: r.hits for r in await PgCoded.all()}
    assert rows == {"x": 9, "y": 2}


@pytest.mark.asyncio
async def test_bulk_create_upsert_without_conflict_target(db):
    """
    GIVEN a model with an auto pk and no unique columns (no legal conflict target)
    WHEN bulk_create upserts with update_fields
    THEN the MERGE-based backends (SQL Server, Oracle) raise (they need a real
         target) while the others default to the pk and insert cleanly (the auto
         pk never fires)
    """
    from yara_orm.exceptions import UnSupportedError

    rows = [PgBare(hits=1), PgBare(hits=2)]
    if db in ("mssql", "oracle"):
        with pytest.raises(UnSupportedError):
            await PgBare.bulk_create(rows, update_fields=["hits"])
    else:
        await PgBare.bulk_create(rows, update_fields=["hits"])
        assert await PgBare.all().count() == 2


@pytest.mark.asyncio
async def test_bulk_create_upsert_with_relation_update_field(db):
    """
    GIVEN update_fields naming a relation
    WHEN bulk_create upserts on a unique column
    THEN the relation resolves to its FK column and the row is updated
    """
    a1 = await PgAuthor.create(name="A1")
    a2 = await PgAuthor.create(name="A2")
    await PgBook.create(title="orig", code="c1", author=a1)
    # Conflict on the unique 'code'; update both a plain column and a relation FK.
    await PgBook.bulk_create(
        [PgBook(title="new", code="c1", author=a2)],
        update_fields=["title", "author"],
        on_conflict=["code"],
    )
    book = await PgBook.get(code="c1")
    assert book.title == "new" and book.author_id == a2.id

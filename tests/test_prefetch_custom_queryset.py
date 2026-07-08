"""Custom-queryset prefetch: pk decoding, per-owner slices, instance isolation."""

import pytest

from yara_orm import Model, Prefetch, fields, in_transaction


class CqAuthor(Model):
    id = fields.UUIDField(pk=True)
    name = fields.CharField(max_length=100)

    class Meta:
        table = "cq_author"


class CqBook(Model):
    id = fields.UUIDField(pk=True)
    title = fields.CharField(max_length=100)
    year = fields.IntField(default=2000)
    author = fields.ForeignKeyField("CqAuthor", related_name="books")

    class Meta:
        table = "cq_book"


class CqTag(Model):
    id = fields.UUIDField(pk=True)
    name = fields.CharField(max_length=100)
    meta = fields.JSONField(null=True)
    books = fields.ManyToManyField("CqBook", related_name="tags", through="cq_book_tag")

    class Meta:
        table = "cq_tag"


MODELS = [CqAuthor, CqBook, CqTag]


async def _library():
    """Two authors, two books each, tags shared across the books."""
    ada = await CqAuthor.create(name="Ada")
    bob = await CqAuthor.create(name="Bob")
    b1 = await CqBook.create(title="A1", year=2001, author=ada)
    b2 = await CqBook.create(title="A2", year=2002, author=ada)
    b3 = await CqBook.create(title="B1", year=2003, author=bob)
    b4 = await CqBook.create(title="B2", year=2004, author=bob)
    sci = await CqTag.create(name="sci", meta={"seen": False})
    fi = await CqTag.create(name="fi", meta={"seen": False})
    await b1.tags.add(sci, fi)
    await b2.tags.add(sci)
    await b3.tags.add(fi)
    return ada, bob, b1, b2, b3, b4, sci, fi


@pytest.mark.asyncio
async def test_prefetch_m2m_uuid_pks(db):
    """
    GIVEN models whose pks are UUIDs (decoded on read by some dialects)
    WHEN an m2m relation is prefetched without a custom queryset
    THEN the raw through-table link values group against the decoded pks
    """
    await _library()
    books = await CqBook.all().prefetch_related("tags").order_by("title")
    by_title = {b.title: sorted(t.name for t in await b.tags) for b in books}
    assert by_title == {"A1": ["fi", "sci"], "A2": ["sci"], "B1": ["fi"], "B2": []}


@pytest.mark.asyncio
async def test_prefetch_m2m_custom_queryset_uuid_pks(db):
    """
    GIVEN models whose pks are UUIDs (decoded on read by some dialects)
    WHEN an m2m relation is prefetched with a custom (ordered) queryset
    THEN every owner receives its linked targets, not silently empty lists
    """
    await _library()
    books = (
        await CqBook.all()
        .prefetch_related(Prefetch("tags", queryset=CqTag.all().order_by("name")))
        .order_by("title")
    )
    by_title = {b.title: [t.name for t in await b.tags] for b in books}
    assert by_title == {"A1": ["fi", "sci"], "A2": ["sci"], "B1": ["fi"], "B2": []}


@pytest.mark.asyncio
async def test_prefetch_m2m_custom_queryset_shared_target_mutation_isolated(db):
    """
    GIVEN two books sharing one tag whose JSON column holds a dict
    WHEN one book's prefetched tag instance mutates that dict
    THEN the other book's instance is unaffected (no shared mutable state)
    """
    await _library()
    books = (
        await CqBook.filter(title__in=["A1", "A2"])
        .prefetch_related(Prefetch("tags", queryset=CqTag.all().order_by("name")))
        .order_by("title")
    )
    a1_sci = next(t for t in await books[0].tags if t.name == "sci")
    a2_sci = next(t for t in await books[1].tags if t.name == "sci")
    assert a1_sci is not a2_sci
    a1_sci.meta["seen"] = True
    assert a2_sci.meta == {"seen": False}


@pytest.mark.asyncio
async def test_prefetch_m2m_sliced_queryset_applies_per_owner(db):
    """
    GIVEN a Prefetch whose queryset carries a slice
    WHEN an m2m relation is prefetched
    THEN the slice caps each owner's group, not the batch globally
    """
    await _library()
    books = (
        await CqBook.all()
        .prefetch_related(Prefetch("tags", queryset=CqTag.all().order_by("name")[:1]))
        .order_by("title")
    )
    by_title = {b.title: [t.name for t in await b.tags] for b in books}
    assert by_title == {"A1": ["fi"], "A2": ["sci"], "B1": ["fi"], "B2": []}


@pytest.mark.asyncio
async def test_prefetch_reverse_fk_sliced_queryset_applies_per_owner(db):
    """
    GIVEN a Prefetch on a reverse FK whose queryset carries a slice
    WHEN several owners are prefetched
    THEN every owner gets its own window (not only owners inside a global one)
    """
    await _library()
    authors = (
        await CqAuthor.all()
        .prefetch_related(Prefetch("books", queryset=CqBook.all().order_by("-year")[:1]))
        .order_by("name")
    )
    by_name = {a.name: [b.title for b in await a.books] for a in authors}
    assert by_name == {"Ada": ["A2"], "Bob": ["B2"]}


@pytest.mark.asyncio
async def test_prefetch_forward_fk_sliced_queryset_applies_per_owner(db):
    """
    GIVEN a Prefetch on a forward FK whose queryset carries a slice
    WHEN books are prefetched
    THEN a [:1] slice is a per-owner no-op and an offset empties every owner
    """
    await _library()
    books = (
        await CqBook.all()
        .prefetch_related(Prefetch("author", queryset=CqAuthor.all()[:1]))
        .order_by("title")
    )
    assert [b.author.name for b in books] == ["Ada", "Ada", "Bob", "Bob"]

    books = (
        await CqBook.all()
        .prefetch_related(Prefetch("author", queryset=CqAuthor.all()[1:]))
        .order_by("title")
    )
    assert [b.author for b in books] == [None, None, None, None]  # cached as _CachedNone


@pytest.mark.asyncio
async def test_prefetch_sliced_queryset_inside_transaction(db):
    """
    GIVEN an open transaction (per-owner queries must not run concurrently)
    WHEN a sliced Prefetch runs inside it
    THEN the per-owner results are still correct and see uncommitted rows
    """
    async with in_transaction():
        ada = await CqAuthor.create(name="Ada")
        bob = await CqAuthor.create(name="Bob")
        await CqBook.create(title="A1", year=2001, author=ada)
        await CqBook.create(title="A2", year=2002, author=ada)
        await CqBook.create(title="B1", year=2003, author=bob)
        authors = (
            await CqAuthor.all()
            .prefetch_related(Prefetch("books", queryset=CqBook.all().order_by("-year")[:1]))
            .order_by("name")
        )
        by_name = {a.name: [b.title for b in await a.books] for a in authors}
        assert by_name == {"Ada": ["A2"], "Bob": ["B1"]}

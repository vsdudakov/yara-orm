"""Eager loading: prefetch_related and fetch_related."""

import pytest

from yara_orm import Model, Prefetch, fields


class PfAuthor(Model):
    name = fields.CharField(max_length=100)

    class Meta:
        table = "p_author"


class PfBook(Model):
    title = fields.CharField(max_length=100)
    author = fields.ForeignKeyField("PfAuthor", related_name="books")

    class Meta:
        table = "p_book"


class PfTag(Model):
    name = fields.CharField(max_length=100)
    books = fields.ManyToManyField("PfBook", related_name="tags", through="p_book_tag")

    class Meta:
        table = "p_tag"


MODELS = [PfAuthor, PfBook, PfTag]


@pytest.mark.asyncio
async def test_prefetch_reverse_fk(db):
    """
    GIVEN Authors each with several Books
    WHEN Authors are fetched with prefetch_related("books")
    THEN each PfAuthor's books are populated without further queries
    """
    a1 = await PfAuthor.create(name="Ada")
    a2 = await PfAuthor.create(name="Bob")
    await PfBook.create(title="A1", author=a1)
    await PfBook.create(title="A2", author=a1)
    await PfBook.create(title="B1", author=a2)

    authors = await PfAuthor.all().prefetch_related("books").order_by("name")
    ada_books = sorted(b.title for b in await authors[0].books)
    assert ada_books == ["A1", "A2"]
    assert [b.title for b in await authors[1].books] == ["B1"]


@pytest.mark.asyncio
async def test_prefetch_forward_fk(db):
    """
    GIVEN Books each linked to an PfAuthor
    WHEN Books are fetched with prefetch_related("author")
    THEN the forward author is cached and accessible synchronously (no query)
    """
    a = await PfAuthor.create(name="Grace")
    await PfBook.create(title="X", author=a)
    await PfBook.create(title="Y", author=a)

    books = await PfBook.all().prefetch_related("author").order_by("title")
    # A prefetched forward FK is served synchronously, matching the documented behavior.
    assert books[0].author.name == "Grace"
    assert books[0].__dict__["_prefetch"]["author"].id == a.id


@pytest.mark.asyncio
async def test_prefetch_with_queryset(db):
    """
    GIVEN an PfAuthor with multiple Books
    WHEN prefetching with Prefetch("books", queryset=filtered)
    THEN only the matching related rows are loaded
    """
    a = await PfAuthor.create(name="Don")
    await PfBook.create(title="Keep", author=a)
    await PfBook.create(title="Drop", author=a)

    authors = await PfAuthor.all().prefetch_related(
        Prefetch("books", queryset=PfBook.filter(title="Keep"))
    )
    titles = [b.title for b in await authors[0].books]
    assert titles == ["Keep"]


@pytest.mark.asyncio
async def test_prefetch_m2m(db):
    """
    GIVEN Books tagged via a many-to-many relation
    WHEN Books are fetched with prefetch_related("tags")
    THEN each PfBook's tags are grouped and cached
    """
    a = await PfAuthor.create(name="Kay")
    b1 = await PfBook.create(title="One", author=a)
    b2 = await PfBook.create(title="Two", author=a)
    t1 = await PfTag.create(name="sci")
    t2 = await PfTag.create(name="fi")
    await b1.tags.add(t1, t2)
    await b2.tags.add(t1)

    books = await PfBook.all().prefetch_related("tags").order_by("title")
    assert sorted(t.name for t in await books[0].tags) == ["fi", "sci"]
    assert [t.name for t in await books[1].tags] == ["sci"]


@pytest.mark.asyncio
async def test_fetch_related_instance(db):
    """
    GIVEN a single PfBook instance
    WHEN fetch_related("author") is awaited on it
    THEN the forward relation is populated on that instance
    """
    a = await PfAuthor.create(name="Lin")
    b = await PfBook.create(title="Solo", author=a)

    fresh = await PfBook.get(id=b.id)
    await fresh.fetch_related("author")
    assert fresh.__dict__["_prefetch"]["author"].name == "Lin"


@pytest.mark.asyncio
async def test_prefetch_m2m_custom_queryset_limit_is_per_owner(db):
    """
    GIVEN two Books each tagged with three distinct Tags
    WHEN prefetching tags with Prefetch(queryset=Tag.order_by(...).limit(2))
    THEN the LIMIT applies per owner (each PfBook gets its own top two tags),
         not once globally across the whole batch
    """
    a = await PfAuthor.create(name="Mel")
    b1 = await PfBook.create(title="One", author=a)
    b2 = await PfBook.create(title="Two", author=a)
    b1_tags = [await PfTag.create(name=f"a{n}") for n in range(3)]
    b2_tags = [await PfTag.create(name=f"b{n}") for n in range(3)]
    await b1.tags.add(*b1_tags)
    await b2.tags.add(*b2_tags)

    books = (
        await PfBook.all()
        .prefetch_related(Prefetch("tags", queryset=PfTag.all().order_by("-name").limit(2)))
        .order_by("title")
    )
    # Per-owner top-2 by descending name: b1 -> a2,a1 ; b2 -> b2,b1.
    assert [t.name for t in await books[0].tags] == ["a2", "a1"]
    assert [t.name for t in await books[1].tags] == ["b2", "b1"]


@pytest.mark.asyncio
async def test_prefetch_m2m_custom_queryset_shared_target_is_distinct_per_owner(db):
    """
    GIVEN two Books sharing the same Tag, prefetched via a custom queryset with
        no slice (the batched single-query path)
    WHEN one owner's prefetched target instance is mutated
    THEN the other owner's copy is unaffected (each owner gets its own instance,
         matching the join-based prefetch path)
    """
    a = await PfAuthor.create(name="Ned")
    b1 = await PfBook.create(title="One", author=a)
    b2 = await PfBook.create(title="Two", author=a)
    shared = await PfTag.create(name="shared")
    await b1.tags.add(shared)
    await b2.tags.add(shared)

    books = (
        await PfBook.all()
        .prefetch_related(Prefetch("tags", queryset=PfTag.all().order_by("name")))
        .order_by("title")
    )
    t1 = (await books[0].tags)[0]
    t2 = (await books[1].tags)[0]
    assert t1 is not t2
    t1.name = "mutated"
    assert t2.name == "shared"


class PfnPublisher(Model):
    name = fields.CharField(max_length=100)

    class Meta:
        table = "pfn_publisher"


class PfnAuthor(Model):
    name = fields.CharField(max_length=100)
    publisher = fields.ForeignKeyField("PfnPublisher", related_name="pfn_authors", null=True)

    class Meta:
        table = "pfn_author"


class PfnBook(Model):
    title = fields.CharField(max_length=100)
    author = fields.ForeignKeyField("PfnAuthor", related_name="pfn_books", null=True)

    class Meta:
        table = "pfn_book"


MODELS += [PfnPublisher, PfnAuthor, PfnBook]


# Unit-only model (not in MODELS, no table): exercises the mutable-column copy.
class PfNote(Model):
    data = fields.JSONField(default=dict)

    class Meta:
        table = "p_note"


@pytest.mark.asyncio
async def test_prefetch_multi_hop_with_null_fk(db):
    """
    GIVEN books whose nullable author FK is NULL (or whose author has a NULL
        publisher FK)
    WHEN prefetching the multi-hop path ``author__publisher``
    THEN the query succeeds and each hop resolves to None where the FK is NULL

    Regression: the ``_CachedNone`` marker stored for a None forward hop was
    fed into the next hop as if it were a model instance, crashing the query.
    """
    pub = await PfnPublisher.create(name="Pub")
    full = await PfnAuthor.create(name="Full", publisher=pub)
    bare = await PfnAuthor.create(name="Bare", publisher=None)
    await PfnBook.create(title="orphan", author=None)
    await PfnBook.create(title="bare", author=bare)
    await PfnBook.create(title="full", author=full)

    books = await PfnBook.all().prefetch_related("author__publisher").order_by("title")

    assert books[0].author.name == "Bare"
    assert books[0].author.publisher is None
    assert books[1].author.publisher.name == "Pub"
    assert books[2].author is None


def test_assign_none_forward_fk_stores_cached_none_marker():
    """
    GIVEN a prefetch that resolved a forward FK to None
    WHEN ``_assign`` stores the result
    THEN the cache holds a ``_CachedNone`` carrying the FK that produced it,
         while a resolved instance (or a non-forward relation) is stored as-is
    """
    from yara_orm.prefetch import _assign
    from yara_orm.relations import _CachedNone

    dangling = PfnBook(id=1, title="dangling", author_id=999)
    _assign([dangling], "author", None, {dangling: None})
    assert dangling.__dict__["_prefetch"]["author"] == _CachedNone(999)

    author = PfnAuthor(id=1, name="A")
    hit = PfnBook(id=2, title="hit", author_id=1)
    _assign([hit], "author", None, {hit: author})
    assert hit.__dict__["_prefetch"]["author"] is author


def test_gather_related_skips_cached_none_marker():
    """
    GIVEN a batch where one instance's forward hop resolved to None
    WHEN ``_gather_related`` collects the loaded children for the next hop
    THEN the ``_CachedNone`` marker is skipped, not returned as an instance
    """
    from yara_orm.prefetch import _assign, _gather_related

    author = PfnAuthor(id=1, name="A")
    hit = PfnBook(id=1, title="hit", author_id=1)
    miss = PfnBook(id=2, title="miss", author_id=None)
    _assign([hit, miss], "author", None, {hit: author, miss: None})

    assert _gather_related([hit, miss], "author") == [author]


def test_clone_shared_copies_mutable_columns_but_shares_prefetch_cache():
    """
    GIVEN a related instance shared by several owners, carrying a mutable JSON
        column and a nested prefetched instance
    WHEN ``_clone_shared`` copies it for an additional owner
    THEN the JSON column is deep-copied (no cross-owner mutation leaks) while
         the nested prefetched instance stays shared (aliasing preserved)
    """
    from yara_orm.prefetch import _clone_shared

    nested = PfnPublisher(id=1, name="Pub")
    note = PfNote(id=1, data={"tags": ["a"]})
    note.__dict__["_prefetch"] = {"publisher": nested}

    clone = _clone_shared(note)

    assert clone.data is not note.data
    clone.data["tags"].append("b")
    assert note.data == {"tags": ["a"]}
    assert clone.__dict__["_prefetch"]["publisher"] is nested

"""Corner cases for eager loading and relation access.

Covers ``select_related`` (single/multi-level forward FK & O2O, nullable
intermediates, combined with ``only()``/``defer()``, ordering by a related
column, synchronous access), ``prefetch_related`` (reverse FK/O2O sets, M2M both
directions, multi-hop and nested paths, empty relations, ``Prefetch`` querysets
and ``to_attr``, single-instance prefetch, no extra fetch on cache hit) and the
relation managers (reverse chaining, forward FK assignment caching, M2M
add/remove/clear with raw pks and duplicates).

These target gaps not already exercised by ``test_relations.py`` /
``test_related_name.py`` / ``test_only_related_paths.py``.
"""

import pytest

from yara_orm import Model, Prefetch, fields
from yara_orm.exceptions import FieldError


class RlCountry(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20)
    code = fields.CharField(max_length=5, null=True)

    class Meta:
        table = "rl_country"


class RlPublisher(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20)
    country = fields.ForeignKeyField("RlCountry", related_name="publishers", null=True)

    class Meta:
        table = "rl_publisher"


class RlAuthor(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20)
    publisher = fields.ForeignKeyField("RlPublisher", related_name="authors", null=True)

    class Meta:
        table = "rl_author"


class RlTag(Model):
    id = fields.IntField(pk=True)
    label = fields.CharField(max_length=20)

    class Meta:
        table = "rl_tag"


class RlBook(Model):
    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=20)
    rating = fields.IntField(default=0)
    author = fields.ForeignKeyField("RlAuthor", related_name="books")
    tags = fields.ManyToManyField("RlTag", related_name="books", through="rl_book_tag")

    class Meta:
        table = "rl_book"


class RlProfile(Model):
    id = fields.IntField(pk=True)
    bio = fields.CharField(max_length=20)
    author = fields.OneToOneField("RlAuthor", related_name="profile")

    class Meta:
        table = "rl_profile"


class RlEmployee(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20)
    manager = fields.ForeignKeyField("RlEmployee", related_name="reports", null=True)

    class Meta:
        table = "rl_employee"


MODELS = [RlCountry, RlPublisher, RlTag, RlAuthor, RlBook, RlProfile, RlEmployee]


async def _seed():
    """Seed a Book -> Author -> Publisher -> Country chain plus a stray author."""
    uk = await RlCountry.create(name="UK", code="GB")
    pub = await RlPublisher.create(name="PubUK", country=uk)
    ada = await RlAuthor.create(name="Ada", publisher=pub)
    # Bob has NO publisher (nullable forward FK left None).
    bob = await RlAuthor.create(name="Bob", publisher=None)
    await RlBook.create(title="A1", rating=5, author=ada)
    await RlBook.create(title="A2", rating=3, author=ada)
    await RlBook.create(title="B1", rating=4, author=bob)
    return {"uk": uk, "pub": pub, "ada": ada, "bob": bob}


# ---------------------------------------------------------------------------
# select_related — forward FK / O2O, multi-level, nullable, projections
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_select_related_multi_level_forward(db):
    """
    GIVEN a Book -> Author -> Publisher -> Country chain
    WHEN select_related joins the full forward path
    THEN every hop is hydrated and reachable synchronously
    """
    await _seed()
    [book] = await RlBook.filter(title="A1").select_related("author__publisher__country")
    assert book.author.name == "Ada"
    assert book.author.publisher.name == "PubUK"
    assert book.author.publisher.country.code == "GB"


@pytest.mark.asyncio
async def test_select_related_nullable_intermediate_is_none(db):
    """
    GIVEN a Book whose Author has no Publisher (nullable forward FK)
    WHEN select_related walks past the null hop
    THEN the null relation hydrates as None and the deeper hop is None too
    """
    await _seed()
    [book] = await RlBook.filter(title="B1").select_related("author__publisher__country")
    assert book.author.name == "Bob"
    assert book.author.publisher is None


@pytest.mark.asyncio
async def test_select_related_only_multi_level(db):
    """
    GIVEN a multi-hop select_related
    WHEN only() restricts a leaf related column
    THEN the leaf loads partially while ancestors load fully
    """
    await _seed()
    rows = (
        await RlBook.filter(title="A1")
        .select_related("author__publisher__country")
        .only("title", "author__publisher__country__code")
    )
    book = rows[0]
    assert book.title == "A1"
    # Intermediates load fully; the leaf (country) loads only the named column.
    assert book.author.publisher.name == "PubUK"
    assert book.author.publisher.country.code == "GB"
    with pytest.raises(FieldError):
        _ = book.author.publisher.country.name


@pytest.mark.asyncio
async def test_select_related_defer_related_column(db):
    """
    GIVEN select_related with defer() of a related column
    WHEN rows are fetched
    THEN the related instance loads every column but the deferred one
    """
    await _seed()
    [book] = await RlBook.filter(title="A1").select_related("author").defer("author__name")
    assert book.author.id is not None
    with pytest.raises(FieldError):
        _ = book.author.name


@pytest.mark.asyncio
async def test_select_related_o2o_forward(db):
    """
    GIVEN a Profile with a forward O2O to an Author
    WHEN select_related joins it
    THEN the author is hydrated synchronously
    """
    seed = await _seed()
    await RlProfile.create(bio="hi", author=seed["ada"])
    [prof] = await RlProfile.select_related("author")
    assert prof.author.name == "Ada"


@pytest.mark.asyncio
async def test_select_related_order_by_related_column(db):
    """
    GIVEN books joined to their authors
    WHEN ordering by a related column (author__name) with select_related
    THEN rows come back ordered by the related value
    """
    await _seed()
    rows = await RlBook.select_related("author").order_by("author__name", "title")
    assert [(b.title, b.author.name) for b in rows] == [
        ("A1", "Ada"),
        ("A2", "Ada"),
        ("B1", "Bob"),
    ]


@pytest.mark.asyncio
async def test_select_related_cache_hit_no_reload(db):
    """
    GIVEN a book fetched with select_related("author")
    WHEN the forward relation is awaited (not just attribute-accessed)
    THEN the cached instance is served (awaiting yields the same object)
    """
    await _seed()
    [book] = await RlBook.filter(title="A1").select_related("author")
    sync = book.author  # served synchronously from cache
    awaited = await book.author  # cache hit path, no reload
    assert sync is awaited
    assert awaited.name == "Ada"


@pytest.mark.asyncio
async def test_select_related_sibling_relation_still_awaitable(db):
    """
    GIVEN a book with select_related("author") only
    WHEN a different, un-selected forward relation is accessed
    THEN it is NOT served synchronously but returns the lazy awaitable
    """
    seed = await _seed()
    await RlProfile.create(bio="x", author=seed["ada"])
    [book] = await RlBook.filter(title="A1").select_related("author")
    # author is cached (an instance); tags is a m2m manager, not an instance.
    assert isinstance(book.author, RlAuthor)
    # The author's own forward relation (publisher) was not selected here, so
    # it must be awaited rather than served synchronously.
    pub = await book.author.publisher
    assert pub.name == "PubUK"


@pytest.mark.asyncio
async def test_select_related_base_only_pk_plus_related(db):
    """
    GIVEN only a related path in only()
    WHEN combined with select_related
    THEN the base loads just its pk and the related column loads
    """
    await _seed()
    rows = await RlBook.select_related("author").only("author__name").order_by("id")
    book = rows[0]
    assert book.author.name == "Ada"
    with pytest.raises(FieldError):
        _ = book.title


@pytest.mark.asyncio
async def test_select_related_unknown_deep_path_raises(db):
    """
    GIVEN a deep select_related path whose leaf segment is not a relation
    WHEN the query is built
    THEN a FieldError is raised
    """
    await _seed()
    with pytest.raises(FieldError):
        await RlBook.select_related("author__name")  # name is a column, not a relation


@pytest.mark.asyncio
async def test_select_related_self_fk_order_by_related(db):
    """
    GIVEN employees in a self-referential manager hierarchy
    WHEN ordering by the self-FK related column (manager__name) with select_related
    THEN each employee is ordered by its manager's name

    The correlated ORDER BY subquery references the target table (== base table
    for a self-FK) unaliased, so the correlation collapses to a single row.
    """
    boss = await RlEmployee.create(name="Boss")
    await RlEmployee.create(name="Zoe", manager=boss)
    ann_boss = await RlEmployee.create(name="AnnBoss")
    await RlEmployee.create(name="Yan", manager=ann_boss)
    # Order workers (exclude the two bosses) by their manager's name:
    # AnnBoss < Boss, so Yan (mgr AnnBoss) should precede Zoe (mgr Boss).
    rows = await (
        RlEmployee.filter(manager_id__isnull=False)
        .select_related("manager")
        .order_by("manager__name")
    )
    assert [e.name for e in rows] == ["Yan", "Zoe"]


# ---------------------------------------------------------------------------
# prefetch_related — reverse FK/O2O, M2M, multi-hop, nested, to_attr
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_prefetch_reverse_fk_set_and_empty(db):
    """
    GIVEN authors, one with books and one with none
    WHEN prefetching the reverse FK set
    THEN the loaded manager serves the cached list (empty for the childless one)
    """
    await _seed()
    authors = await RlAuthor.all().prefetch_related("books").order_by("name")
    ada, bob = authors
    assert sorted(b.title for b in await ada.books) == ["A1", "A2"]
    assert [b.title for b in await bob.books] == ["B1"]
    # An author with no books gets an empty cached list, not a query error.
    solo = await RlAuthor.create(name="Solo")
    [solo] = await RlAuthor.filter(id=solo.id).prefetch_related("books")
    assert await solo.books == []


@pytest.mark.asyncio
async def test_prefetch_reverse_o2o(db):
    """
    GIVEN an author with a reverse O2O profile
    WHEN authors are prefetched with the reverse O2O
    THEN the accessor serves the cached instance (None when absent)
    """
    seed = await _seed()
    await RlProfile.create(bio="ada-bio", author=seed["ada"])
    authors = await RlAuthor.all().prefetch_related("profile").order_by("name")
    ada, bob = authors
    prof = await ada.profile
    assert prof.bio == "ada-bio"
    assert await bob.profile is None


@pytest.mark.asyncio
async def test_prefetch_m2m_forward(db):
    """
    GIVEN books tagged via M2M
    WHEN prefetching the forward m2m relation
    THEN the manager serves the cached tag list
    """
    seed = await _seed()
    book = await RlBook.get(title="A1")
    sci = await RlTag.create(label="sci")
    fic = await RlTag.create(label="fic")
    await book.tags.add(sci, fic)
    [book] = await RlBook.filter(id=book.id).prefetch_related("tags")
    assert sorted(t.label for t in await book.tags) == ["fic", "sci"]
    assert seed["ada"].id  # keep seed referenced


@pytest.mark.asyncio
async def test_prefetch_m2m_reverse(db):
    """
    GIVEN a tag applied to books
    WHEN prefetching the reverse m2m relation (tag.books)
    THEN the reverse manager serves the cached book list
    """
    await _seed()
    b1 = await RlBook.get(title="A1")
    b2 = await RlBook.get(title="A2")
    tag = await RlTag.create(label="shared")
    await b1.tags.add(tag)
    await b2.tags.add(tag)
    [tag] = await RlTag.filter(id=tag.id).prefetch_related("books")
    assert sorted(b.title for b in await tag.books) == ["A1", "A2"]


@pytest.mark.asyncio
async def test_prefetch_m2m_empty(db):
    """
    GIVEN a book with no tags
    WHEN prefetching its m2m relation
    THEN the cached list is empty
    """
    await _seed()
    [book] = await RlBook.filter(title="A1").prefetch_related("tags")
    assert await book.tags == []


@pytest.mark.asyncio
async def test_prefetch_multi_hop_reverse_then_reverse(db):
    """
    GIVEN a Publisher -> Authors -> Books chain
    WHEN prefetching the two-hop reverse path "authors__books"
    THEN each publisher's authors are cached and each of those authors' books
         are cached too (one query per hop, no N+1)
    """
    seed = await _seed()
    [pub] = await RlPublisher.filter(id=seed["pub"].id).prefetch_related("authors__books")
    authors = await pub.authors
    assert [a.name for a in authors] == ["Ada"]
    # The nested hop is already cached on the fetched author instances.
    assert sorted(b.title for b in await authors[0].books) == ["A1", "A2"]


@pytest.mark.asyncio
async def test_prefetch_multi_hop_forward_fk(db):
    """
    GIVEN books
    WHEN prefetching a two-hop forward path "author__publisher"
    THEN the forward FK and its parent are cached and served synchronously
    """
    await _seed()
    [book] = await RlBook.filter(title="A1").prefetch_related("author__publisher")
    author = book.author  # forward FK served from prefetch cache
    assert author.name == "Ada"
    assert author.publisher.name == "PubUK"  # nested forward FK cached too


@pytest.mark.asyncio
async def test_prefetch_nested_multiple_specs(db):
    """
    GIVEN books
    WHEN two independent relations are prefetched in one call
    THEN both caches are populated
    """
    seed = await _seed()
    book = await RlBook.get(title="A1")
    tag = await RlTag.create(label="t")
    await book.tags.add(tag)
    [book] = await RlBook.filter(id=book.id).prefetch_related("author", "tags")
    assert book.author.name == "Ada"
    assert [t.label for t in await book.tags] == ["t"]
    assert seed["ada"].id


@pytest.mark.asyncio
async def test_prefetch_custom_queryset_filter_and_order(db):
    """
    GIVEN an author with several books
    WHEN prefetching with a constrained + ordered Prefetch queryset
    THEN only matching rows are cached, in the queryset's order
    """
    seed = await _seed()
    [ada] = await RlAuthor.filter(id=seed["ada"].id).prefetch_related(
        Prefetch("books", queryset=RlBook.filter(rating__gte=4).order_by("-rating"))
    )
    # Only A1 (rating 5) qualifies for Ada (A2 is rating 3).
    assert [b.title for b in await ada.books] == ["A1"]


@pytest.mark.asyncio
async def test_prefetch_to_attr(db):
    """
    GIVEN a Prefetch with to_attr
    WHEN the relation is prefetched
    THEN the result is stored on the plain attribute (not the relation accessor)
    """
    seed = await _seed()
    [ada] = await RlAuthor.filter(id=seed["ada"].id).prefetch_related(
        Prefetch("books", queryset=RlBook.all().order_by("title"), to_attr="loaded_books")
    )
    loaded = ada.loaded_books
    assert isinstance(loaded, list)
    assert [b.title for b in loaded] == ["A1", "A2"]


@pytest.mark.asyncio
async def test_prefetch_on_single_instance_get(db):
    """
    GIVEN a single instance fetched via get(...).prefetch_related(...)
    WHEN the relation is accessed
    THEN it is served from the prefetch cache
    """
    seed = await _seed()
    ada = await RlAuthor.get(id=seed["ada"].id).prefetch_related("books")
    assert sorted(b.title for b in await ada.books) == ["A1", "A2"]


@pytest.mark.asyncio
async def test_prefetched_manager_chaining_requeries(db):
    """
    GIVEN a prefetched reverse manager
    WHEN it is chained with .filter()/.count()
    THEN the chain builds a fresh queryset (cache serves only the direct await)
    """
    await _seed()
    [ada] = await RlAuthor.filter(name="Ada").prefetch_related("books")
    # Direct await hits the cache.
    assert len(await ada.books) == 2
    # Chaining re-queries the DB and still returns the right subset.
    assert await ada.books.filter(rating__gte=5).count() == 1
    assert [b.title for b in await ada.books.order_by("-title")] == ["A2", "A1"]


@pytest.mark.asyncio
async def test_prefetch_unknown_relation_raises(db):
    """
    GIVEN a model
    WHEN prefetching an unknown relation name
    THEN a ValueError is raised
    """
    await _seed()
    with pytest.raises(ValueError):
        await RlBook.all().prefetch_related("nope")


# ---------------------------------------------------------------------------
# relation managers — forward assignment, reverse chaining, M2M mutation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_forward_fk_assignment_caches_instance(db):
    """
    GIVEN a fresh book and an author instance
    WHEN the author is assigned to the forward FK
    THEN the FK id is set and the instance is served synchronously (cached)
    """
    seed = await _seed()
    book = RlBook(title="New", rating=1)
    book.author = seed["ada"]
    assert book.author_id == seed["ada"].id
    # Assignment caches the instance -> synchronous access, no await needed.
    assert isinstance(book.author, RlAuthor)
    assert book.author.name == "Ada"


@pytest.mark.asyncio
async def test_reverse_manager_limit_and_order(db):
    """
    GIVEN an author with several books
    WHEN the reverse manager is chained with order_by().limit()
    THEN the chain behaves like a scoped queryset
    """
    seed = await _seed()
    ada = seed["ada"]
    top = await ada.books.order_by("-rating").limit(1)
    assert [b.title for b in top] == ["A1"]
    assert await ada.books.filter(rating__lt=5).count() == 1


@pytest.mark.asyncio
async def test_m2m_add_by_raw_pk_and_duplicate(db):
    """
    GIVEN a book and tags
    WHEN tags are added by raw pk and the same tag is added twice
    THEN membership is deduplicated (ON CONFLICT DO NOTHING)
    """
    await _seed()
    book = await RlBook.get(title="A1")
    tag = await RlTag.create(label="dup")
    await book.tags.add(tag.id)  # raw pk
    await book.tags.add(tag)  # same tag again as instance
    assert [t.label for t in await book.tags] == ["dup"]
    assert await book.tags.count() == 1


@pytest.mark.asyncio
async def test_m2m_remove_multiple_and_clear(db):
    """
    GIVEN a book with several tags
    WHEN some are removed (mixed instance/pk) and then all cleared
    THEN the manager reflects each mutation
    """
    await _seed()
    book = await RlBook.get(title="A1")
    t1 = await RlTag.create(label="t1")
    t2 = await RlTag.create(label="t2")
    t3 = await RlTag.create(label="t3")
    await book.tags.add(t1, t2, t3)
    await book.tags.remove(t1, t2.id)  # mixed instance + raw pk
    assert [t.label for t in await book.tags] == ["t3"]
    await book.tags.clear()
    assert await book.tags == []


@pytest.mark.asyncio
async def test_m2m_reverse_manager_add(db):
    """
    GIVEN a tag and books
    WHEN books are added through the reverse m2m manager (tag.books.add)
    THEN the forward side reflects the membership too
    """
    await _seed()
    b1 = await RlBook.get(title="A1")
    b2 = await RlBook.get(title="A2")
    tag = await RlTag.create(label="rev")
    await tag.books.add(b1, b2)
    assert sorted(b.title for b in await tag.books) == ["A1", "A2"]
    # Forward side sees it as well.
    assert [t.label for t in await b1.tags] == ["rev"]

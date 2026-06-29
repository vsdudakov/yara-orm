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
    THEN the forward author is cached and awaiting it makes no query
    """
    a = await PfAuthor.create(name="Grace")
    await PfBook.create(title="X", author=a)
    await PfBook.create(title="Y", author=a)

    books = await PfBook.all().prefetch_related("author").order_by("title")
    assert (await books[0].author).name == "Grace"
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

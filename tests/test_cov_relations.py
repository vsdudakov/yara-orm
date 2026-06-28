"""Coverage: relation managers, aggregation joins and prefetch variants."""

import pytest

from yara_orm import Avg, Count, Max, Min, Model, Prefetch, Sum, fields


class CvAuthor(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)
    age = fields.IntField(default=0)

    class Meta:
        table = "cov_author"


class CvBook(Model):
    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=50)
    rating = fields.IntField(default=0)
    author = fields.ForeignKeyField("CvAuthor", related_name="books")
    tags = fields.ManyToManyField("CvTag", related_name="books", through="cov_book_tag")

    class Meta:
        table = "cov_book"


class CvTag(Model):
    id = fields.IntField(pk=True)
    label = fields.CharField(max_length=50)

    class Meta:
        table = "cov_tag"


class CvProfile(Model):
    id = fields.IntField(pk=True)
    bio = fields.CharField(max_length=50)
    author = fields.OneToOneField("CvAuthor", related_name="profile")

    class Meta:
        table = "cov_profile"


@pytest.mark.asyncio
async def test_reverse_manager_create_filter_order(sqlite_db):
    """
    GIVEN an author with related books
    WHEN using the reverse manager's create/all/filter/order_by
    THEN each chained operation behaves like a scoped queryset
    """
    a = await CvAuthor.create(name="Ada")
    await a.books.create(title="B", rating=2)
    await a.books.create(title="A", rating=5)
    assert [b.title for b in await a.books.order_by("title")] == ["A", "B"]
    assert [b.title for b in await a.books.all().order_by("-rating")] == ["A", "B"]
    assert await a.books.filter(rating__gte=5).count() == 1


@pytest.mark.asyncio
async def test_one_to_one_reverse_cached_by_prefetch(sqlite_db):
    """
    GIVEN an author with a one-to-one profile
    WHEN authors are prefetched with their reverse o2o
    THEN the reverse accessor serves the cached instance
    """
    a = await CvAuthor.create(name="Bo")
    await CvProfile.create(bio="hi", author=a)
    [author] = await CvAuthor.all().prefetch_related("profile")
    prof = await author.profile
    assert prof.bio == "hi"


@pytest.mark.asyncio
async def test_aggregations_over_columns_and_relations(sqlite_db):
    """
    GIVEN authors with books of varying ratings
    WHEN aggregating with Count/Sum/Avg/Min/Max over columns and relations
    THEN the grouped and annotated results match the data
    """
    a = await CvAuthor.create(name="A", age=30)
    b = await CvAuthor.create(name="B", age=40)
    await CvBook.create(title="a1", rating=5, author=a)
    await CvBook.create(title="a2", rating=3, author=a)
    await CvBook.create(title="b1", rating=4, author=b)

    counts = await CvAuthor.annotate(n=Count("books")).order_by("name")
    assert [(x.name, x.n) for x in counts] == [("A", 2), ("B", 1)]

    rows = (
        await CvBook.annotate(
            total=Sum("rating"), avg=Avg("rating"), lo=Min("rating"), hi=Max("rating")
        )
        .group_by("author_id")
        .values("author_id", "total", "lo", "hi")
    )
    by_author = {r["author_id"]: r for r in rows}
    assert by_author[a.id]["total"] == 8
    assert by_author[a.id]["lo"] == 3 and by_author[a.id]["hi"] == 5


@pytest.mark.asyncio
async def test_annotation_filter_and_order(sqlite_db):
    """
    GIVEN authors with different book counts
    WHEN filtering and ordering on an annotation
    THEN HAVING and ORDER BY the alias work
    """
    a = await CvAuthor.create(name="A")
    b = await CvAuthor.create(name="B")
    await CvBook.create(title="x", author=a)
    await CvBook.create(title="y", author=a)
    await CvBook.create(title="z", author=b)
    rows = await CvAuthor.annotate(n=Count("books")).filter(n__gte=2).order_by("-n")
    assert [x.name for x in rows] == ["A"]


@pytest.mark.asyncio
async def test_m2m_membership_filters(sqlite_db):
    """
    GIVEN books tagged via a many-to-many relation
    WHEN filtering by membership (=, __in)
    THEN the subquery selects books with the tag
    """
    a = await CvAuthor.create(name="A")
    book = await CvBook.create(title="t", author=a)
    other = await CvBook.create(title="u", author=a)
    tag = await CvTag.create(label="sci")
    await book.tags.add(tag)
    assert [x.title for x in await CvBook.filter(tags=tag)] == ["t"]
    assert [x.title for x in await CvBook.filter(tags__in=[tag.id])] == ["t"]
    assert other.id != book.id


@pytest.mark.asyncio
async def test_prefetch_with_custom_queryset(sqlite_db):
    """
    GIVEN an author with several books
    WHEN prefetching with a constrained Prefetch queryset
    THEN only the matching related rows are cached
    """
    a = await CvAuthor.create(name="A")
    await CvBook.create(title="keep", rating=5, author=a)
    await CvBook.create(title="drop", rating=1, author=a)
    [author] = await CvAuthor.all().prefetch_related(
        Prefetch("books", queryset=CvBook.filter(rating__gte=5))
    )
    assert [b.title for b in await author.books] == ["keep"]


@pytest.mark.asyncio
async def test_prefetch_unknown_relation_raises(sqlite_db):
    """
    GIVEN a model
    WHEN prefetching an unknown relation name
    THEN a ValueError is raised
    """
    await CvAuthor.create(name="A")
    with pytest.raises(ValueError):
        await CvAuthor.all().prefetch_related("nope")

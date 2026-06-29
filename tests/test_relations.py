"""Relations: FK forward/reverse, O2O, M2M, recursive self-FK, plus relation
managers, aggregation joins and prefetch variants."""

import pytest

from yara_orm import Avg, Count, Max, Min, Model, Prefetch, Sum, fields


class Tournament(Model):
    name = fields.CharField(max_length=100)

    class Meta:
        table = "r_tournament"


class Team(Model):
    name = fields.CharField(max_length=100)

    class Meta:
        table = "r_team"


class Event(Model):
    name = fields.CharField(max_length=100)
    tournament = fields.ForeignKeyField("Tournament", related_name="events")
    participants = fields.ManyToManyField("Team", related_name="events", through="r_event_team")

    class Meta:
        table = "r_event"


class Address(Model):
    line = fields.CharField(max_length=100)
    event = fields.OneToOneField("Event", related_name="address")

    class Meta:
        table = "r_address"


class Employee(Model):
    name = fields.CharField(max_length=100)
    manager = fields.ForeignKeyField("Employee", related_name="reports", null=True)

    class Meta:
        table = "r_employee"


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


MODELS = [Tournament, Team, Event, Address, Employee, CvAuthor, CvTag, CvBook, CvProfile]


@pytest.mark.asyncio
async def test_forward_fk_access(db):
    """
    GIVEN an Event linked to a Tournament via a foreign key
    WHEN the forward relation is awaited
    THEN it resolves to the related Tournament instance
    """
    t = await Tournament.create(name="World Cup")
    e = await Event.create(name="Final", tournament=t)

    reloaded = await Event.get(id=e.id)
    assert reloaded.tournament_id == t.id
    related = await reloaded.tournament
    assert related.id == t.id and related.name == "World Cup"


@pytest.mark.asyncio
async def test_forward_fk_none(db):
    """
    GIVEN a nullable self-FK that is unset
    WHEN the forward relation is awaited
    THEN it resolves to None
    """
    boss = await Employee.create(name="Boss")
    assert await boss.manager is None


@pytest.mark.asyncio
async def test_reverse_fk_manager(db):
    """
    GIVEN a Tournament with several Events
    WHEN its reverse `events` manager is awaited and filtered
    THEN it yields the related Events and supports chaining
    """
    t = await Tournament.create(name="Cup")
    await Event.create(name="Final", tournament=t)
    await Event.create(name="Semi", tournament=t)

    names = sorted(e.name for e in await t.events)
    assert names == ["Final", "Semi"]
    assert await t.events.filter(name="Final").count() == 1


@pytest.mark.asyncio
async def test_fk_filter_by_object(db):
    """
    GIVEN Events under different Tournaments
    WHEN filtering by a Tournament instance
    THEN only that Tournament's Events are returned
    """
    a = await Tournament.create(name="A")
    b = await Tournament.create(name="B")
    await Event.create(name="ea", tournament=a)
    await Event.create(name="eb", tournament=b)

    rows = await Event.filter(tournament=a)
    assert [e.name for e in rows] == ["ea"]


@pytest.mark.asyncio
async def test_one_to_one(db):
    """
    GIVEN an Address with a OneToOne link to an Event
    WHEN the forward and reverse accessors are awaited
    THEN both resolve to the single linked instance
    """
    e = await Event.create(name="Final", tournament=await Tournament.create(name="C"))
    addr = await Address.create(line="Main St", event=e)

    assert (await addr.event).id == e.id
    back = await e.address
    assert back.id == addr.id and back.line == "Main St"


@pytest.mark.asyncio
async def test_m2m_add_query_iterate(db):
    """
    GIVEN an Event and several Teams
    WHEN teams are added to the m2m manager
    THEN awaiting and async-iterating the manager yields those teams
    """
    e = await Event.create(name="Final", tournament=await Tournament.create(name="D"))
    t1 = await Team.create(name="Alpha")
    t2 = await Team.create(name="Beta")
    await e.participants.add(t1, t2)

    names = sorted(t.name for t in await e.participants)
    assert names == ["Alpha", "Beta"]

    collected = [team.id async for team in e.participants]
    assert sorted(collected) == sorted([t1.id, t2.id])


@pytest.mark.asyncio
async def test_m2m_reverse(db):
    """
    GIVEN a Team added to an Event's participants
    WHEN the Team's reverse m2m manager is awaited
    THEN it includes that Event
    """
    e = await Event.create(name="Final", tournament=await Tournament.create(name="E"))
    team = await Team.create(name="Gamma")
    await e.participants.add(team)

    events = await team.events
    assert [ev.id for ev in events] == [e.id]


@pytest.mark.asyncio
async def test_m2m_remove_and_clear(db):
    """
    GIVEN an Event with two participating Teams
    WHEN one team is removed and then all are cleared
    THEN the manager reflects each change
    """
    e = await Event.create(name="Final", tournament=await Tournament.create(name="F"))
    t1 = await Team.create(name="One")
    t2 = await Team.create(name="Two")
    await e.participants.add(t1, t2)

    await e.participants.remove(t1)
    assert [t.name for t in await e.participants] == ["Two"]

    await e.participants.clear()
    assert await e.participants == []


@pytest.mark.asyncio
async def test_m2m_filter(db):
    """
    GIVEN Events with different participating Teams
    WHEN filtering Events by a participant and by exclusion
    THEN membership subqueries select the right Events
    """
    t = await Tournament.create(name="G")
    e1 = await Event.create(name="e1", tournament=t)
    await Event.create(name="e2", tournament=t)
    team = await Team.create(name="Solo")
    await e1.participants.add(team)

    have = await Event.filter(participants=team)
    assert [e.name for e in have] == ["e1"]
    without = await Event.filter(participants__not=team.id).order_by("name")
    assert [e.name for e in without] == ["e2"]


@pytest.mark.asyncio
async def test_recursive_fk(db):
    """
    GIVEN Employees linked to a manager via a recursive self-FK
    WHEN the reverse `reports` manager and forward `manager` are awaited
    THEN the self-referential hierarchy resolves correctly
    """
    boss = await Employee.create(name="Boss")
    await Employee.create(name="Worker A", manager=boss)
    await Employee.create(name="Worker B", manager=boss)

    reports = sorted(e.name for e in await boss.reports)
    assert reports == ["Worker A", "Worker B"]
    worker = await Employee.get(name="Worker A")
    assert (await worker.manager).id == boss.id


@pytest.mark.asyncio
async def test_reverse_manager_create_filter_order(db):
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
async def test_one_to_one_reverse_cached_by_prefetch(db):
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
async def test_aggregations_over_columns_and_relations(db):
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
async def test_annotation_filter_and_order(db):
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
async def test_m2m_membership_filters(db):
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
async def test_prefetch_with_custom_queryset(db):
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
async def test_prefetch_unknown_relation_raises(db):
    """
    GIVEN a model
    WHEN prefetching an unknown relation name
    THEN a ValueError is raised
    """
    await CvAuthor.create(name="A")
    with pytest.raises(ValueError):
        await CvAuthor.all().prefetch_related("nope")

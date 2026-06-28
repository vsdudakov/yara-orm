"""Relations: FK forward/reverse, O2O, M2M and recursive self-FK."""

import pytest

from yara_orm import Model, YaraOrm, fields
from yara_orm.connection import get_engine


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


async def _reset():
    engine = get_engine()
    for table in ("r_address", "r_event_team", "r_event", "r_team", "r_tournament", "r_employee"):
        await engine.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    await YaraOrm.generate_schemas()


@pytest.mark.asyncio
async def test_forward_fk_access(orm):
    """
    GIVEN an Event linked to a Tournament via a foreign key
    WHEN the forward relation is awaited
    THEN it resolves to the related Tournament instance
    """
    await _reset()
    t = await Tournament.create(name="World Cup")
    e = await Event.create(name="Final", tournament=t)

    reloaded = await Event.get(id=e.id)
    assert reloaded.tournament_id == t.id
    related = await reloaded.tournament
    assert related.id == t.id and related.name == "World Cup"


@pytest.mark.asyncio
async def test_forward_fk_none(orm):
    """
    GIVEN a nullable self-FK that is unset
    WHEN the forward relation is awaited
    THEN it resolves to None
    """
    await _reset()
    boss = await Employee.create(name="Boss")
    assert await boss.manager is None


@pytest.mark.asyncio
async def test_reverse_fk_manager(orm):
    """
    GIVEN a Tournament with several Events
    WHEN its reverse `events` manager is awaited and filtered
    THEN it yields the related Events and supports chaining
    """
    await _reset()
    t = await Tournament.create(name="Cup")
    await Event.create(name="Final", tournament=t)
    await Event.create(name="Semi", tournament=t)

    names = sorted(e.name for e in await t.events)
    assert names == ["Final", "Semi"]
    assert await t.events.filter(name="Final").count() == 1


@pytest.mark.asyncio
async def test_fk_filter_by_object(orm):
    """
    GIVEN Events under different Tournaments
    WHEN filtering by a Tournament instance
    THEN only that Tournament's Events are returned
    """
    await _reset()
    a = await Tournament.create(name="A")
    b = await Tournament.create(name="B")
    await Event.create(name="ea", tournament=a)
    await Event.create(name="eb", tournament=b)

    rows = await Event.filter(tournament=a)
    assert [e.name for e in rows] == ["ea"]


@pytest.mark.asyncio
async def test_one_to_one(orm):
    """
    GIVEN an Address with a OneToOne link to an Event
    WHEN the forward and reverse accessors are awaited
    THEN both resolve to the single linked instance
    """
    await _reset()
    e = await Event.create(name="Final", tournament=await Tournament.create(name="C"))
    addr = await Address.create(line="Main St", event=e)

    assert (await addr.event).id == e.id
    back = await e.address
    assert back.id == addr.id and back.line == "Main St"


@pytest.mark.asyncio
async def test_m2m_add_query_iterate(orm):
    """
    GIVEN an Event and several Teams
    WHEN teams are added to the m2m manager
    THEN awaiting and async-iterating the manager yields those teams
    """
    await _reset()
    e = await Event.create(name="Final", tournament=await Tournament.create(name="D"))
    t1 = await Team.create(name="Alpha")
    t2 = await Team.create(name="Beta")
    await e.participants.add(t1, t2)

    names = sorted(t.name for t in await e.participants)
    assert names == ["Alpha", "Beta"]

    collected = [team.id async for team in e.participants]
    assert sorted(collected) == sorted([t1.id, t2.id])


@pytest.mark.asyncio
async def test_m2m_reverse(orm):
    """
    GIVEN a Team added to an Event's participants
    WHEN the Team's reverse m2m manager is awaited
    THEN it includes that Event
    """
    await _reset()
    e = await Event.create(name="Final", tournament=await Tournament.create(name="E"))
    team = await Team.create(name="Gamma")
    await e.participants.add(team)

    events = await team.events
    assert [ev.id for ev in events] == [e.id]


@pytest.mark.asyncio
async def test_m2m_remove_and_clear(orm):
    """
    GIVEN an Event with two participating Teams
    WHEN one team is removed and then all are cleared
    THEN the manager reflects each change
    """
    await _reset()
    e = await Event.create(name="Final", tournament=await Tournament.create(name="F"))
    t1 = await Team.create(name="One")
    t2 = await Team.create(name="Two")
    await e.participants.add(t1, t2)

    await e.participants.remove(t1)
    assert [t.name for t in await e.participants] == ["Two"]

    await e.participants.clear()
    assert await e.participants == []


@pytest.mark.asyncio
async def test_m2m_filter(orm):
    """
    GIVEN Events with different participating Teams
    WHEN filtering Events by a participant and by exclusion
    THEN membership subqueries select the right Events
    """
    await _reset()
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
async def test_recursive_fk(orm):
    """
    GIVEN Employees linked to a manager via a recursive self-FK
    WHEN the reverse `reports` manager and forward `manager` are awaited
    THEN the self-referential hierarchy resolves correctly
    """
    await _reset()
    boss = await Employee.create(name="Boss")
    await Employee.create(name="Worker A", manager=boss)
    await Employee.create(name="Worker B", manager=boss)

    reports = sorted(e.name for e in await boss.reports)
    assert reports == ["Worker A", "Worker B"]
    worker = await Employee.get(name="Worker A")
    assert (await worker.manager).id == boss.id

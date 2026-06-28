"""SQLite backend: CRUD, rich types, relations, transactions, aggregation.

Exercises the second database backend via a temporary SQLite file, proving the
backend abstraction (one Rust trait + one Python dialect) works end-to-end.
"""

import os
import tempfile
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio

from yara_orm import Count, Model, Q, YaraOrm, fields, in_transaction


class SqTournament(Model):
    name = fields.CharField(max_length=100)

    class Meta:
        table = "sq_tournament"


class SqTeam(Model):
    name = fields.CharField(max_length=100)

    class Meta:
        table = "sq_team"


class SqEvent(Model):
    name = fields.CharField(max_length=100)
    rating = fields.IntField(default=0)
    price = fields.DecimalField(max_digits=8, decimal_places=2, null=True)
    tags = fields.JSONField(null=True)
    code = fields.UUIDField(null=True)
    created = fields.DatetimeField(auto_now_add=True)
    tournament = fields.ForeignKeyField("SqTournament", related_name="events")
    participants = fields.ManyToManyField("SqTeam", related_name="events", through="sq_event_team")

    class Meta:
        table = "sq_event"


@pytest_asyncio.fixture
async def sqlite_orm():
    """Fresh temporary SQLite database per test."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    await YaraOrm.init(f"sqlite://{path}")
    try:
        await YaraOrm.generate_schemas()
        yield
    finally:
        await YaraOrm.close()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(path + suffix):
                os.remove(path + suffix)


@pytest.mark.asyncio
async def test_crud_and_rich_types(sqlite_orm):
    """
    GIVEN a SQLite database
    WHEN an Event with decimal/json/uuid/datetime fields is created and re-read
    THEN every value round-trips to its native Python type
    """
    t = await SqTournament.create(name="Cup")
    code = uuid.uuid4()
    e = await SqEvent.create(
        name="Final",
        rating=5,
        price=Decimal("19.99"),
        tags={"k": [1, 2, 3]},
        code=code,
        tournament=t,
    )
    got = await SqEvent.get(id=e.id)
    assert got.pk == e.pk
    assert got.rating == 5
    assert got.price == Decimal("19.99")
    assert got.tags == {"k": [1, 2, 3]}
    assert isinstance(got.code, uuid.UUID) and got.code == code
    assert got.created is not None


@pytest.mark.asyncio
async def test_relations(sqlite_orm):
    """
    GIVEN events, teams and a tournament
    WHEN FK forward/reverse and M2M relations are traversed
    THEN they resolve correctly on the SQLite backend
    """
    t = await SqTournament.create(name="Cup")
    e = await SqEvent.create(name="Final", tournament=t)
    red = await SqTeam.create(name="Red")
    blue = await SqTeam.create(name="Blue")
    await e.participants.add(red, blue)

    assert (await e.tournament).name == "Cup"
    assert [ev.name for ev in await t.events] == ["Final"]
    assert sorted(team.name for team in await e.participants) == ["Blue", "Red"]
    assert [ev.id for ev in await red.events] == [e.id]


@pytest.mark.asyncio
async def test_transaction_rollback(sqlite_orm):
    """
    GIVEN a transaction that raises after a write
    WHEN the block exits with an exception
    THEN the write is rolled back
    """
    with pytest.raises(RuntimeError):
        async with in_transaction():
            await SqTeam.create(name="Temp")
            raise RuntimeError("boom")
    assert await SqTeam.filter(name="Temp").exists() is False


@pytest.mark.asyncio
async def test_bulk_create_and_count(sqlite_orm):
    """
    GIVEN many Team instances
    WHEN bulk_create persists them
    THEN all rows are inserted and counted on the SQLite backend
    """
    teams = [SqTeam(name=f"T{i}") for i in range(250)]
    created = await SqTeam.bulk_create(teams, batch_size=100)
    assert len(created) == 250
    assert await SqTeam.all().count() == 250


@pytest.mark.asyncio
async def test_q_filter_and_aggregation(sqlite_orm):
    """
    GIVEN events with different ratings
    WHEN filtering with Q and aggregating with Count over a relation
    THEN the SQLite query compiler returns the expected results
    """
    t = await SqTournament.create(name="Cup")
    await SqEvent.create(name="A", rating=1, tournament=t)
    await SqEvent.create(name="B", rating=5, tournament=t)

    rows = await SqEvent.filter(Q(rating__gte=5) | Q(name="A")).order_by("name")
    assert [e.name for e in rows] == ["A", "B"]

    [agg] = await SqTournament.annotate(n=Count("events")).filter(n__gte=1)
    assert agg.n == 2

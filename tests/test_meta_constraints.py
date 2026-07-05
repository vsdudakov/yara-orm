"""Meta.unique_together and Meta.indexes composite schema constraints."""

import pytest

from yara_orm import IntegrityError, Model, fields
from yara_orm.connection import get_engine


class Slot(Model):
    id = fields.IntField(pk=True)
    room = fields.CharField(max_length=10)
    hour = fields.IntField()
    day = fields.CharField(max_length=10)

    class Meta:
        table = "mc_slot"
        unique_together = ("room", "hour")
        indexes = (("day", "hour"),)


class McTeam(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20)

    class Meta:
        table = "mc_team"


class Member(Model):
    id = fields.IntField(pk=True)
    team = fields.ForeignKeyField("McTeam", related_name="members")
    role = fields.CharField(max_length=20)

    class Meta:
        table = "mc_member"
        # A relation name in unique_together resolves to its FK column.
        unique_together = ("team", "role")


MODELS = [Slot, McTeam, Member]


@pytest.mark.asyncio
async def test_unique_together_enforced(db):
    """
    GIVEN a model with a composite unique_together constraint
    WHEN a duplicate (room, hour) pair is inserted
    THEN it raises IntegrityError while distinct pairs are accepted
    """
    await Slot.create(room="A", hour=9, day="mon")
    await Slot.create(room="B", hour=9, day="mon")  # different room
    await Slot.create(room="A", hour=10, day="mon")  # different hour
    with pytest.raises(IntegrityError):
        await Slot.create(room="A", hour=9, day="tue")  # duplicate (room, hour)


@pytest.mark.asyncio
async def test_composite_index_created(db):
    """
    GIVEN a model with a composite Meta.indexes entry
    WHEN the schema is generated
    THEN an index over (day, hour) exists on the table
    """
    engine = get_engine()
    if db == "postgres":
        rows = await engine.fetch_rows(
            "SELECT indexdef FROM pg_indexes WHERE tablename = 'mc_slot'"
        )
        defs = " ".join(r[0] for r in rows)
    elif db in ("mysql", "mariadb"):
        rows = await engine.fetch_rows(
            "SELECT index_name, column_name FROM information_schema.statistics "
            "WHERE table_name = 'mc_slot'"
        )
        defs = " ".join(f"{r[0]} {r[1]}" for r in rows)
    elif db == "mssql":
        rows = await engine.fetch_rows(
            "SELECT c.name FROM sys.indexes i "
            "JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id "
            "JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id "
            "WHERE i.object_id = OBJECT_ID('mc_slot')"
        )
        defs = " ".join(r[0] for r in rows)
    else:
        rows = await engine.fetch_rows(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'mc_slot'"
        )
        defs = " ".join(r[0] for r in rows if r[0])
    assert "day" in defs and "hour" in defs


@pytest.mark.asyncio
async def test_unique_together_on_relation(db):
    """
    GIVEN a unique_together that names a foreign-key relation
    WHEN a duplicate (team, role) pair is inserted
    THEN the constraint (resolved to the FK column) raises IntegrityError
    """
    team = await McTeam.create(name="Red")
    await Member.create(team=team, role="lead")
    with pytest.raises(IntegrityError):
        await Member.create(team=team, role="lead")

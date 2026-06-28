"""End-to-end demo mirroring Tortoise's basic example.

Run against a local PostgreSQL:

    createdb orm_demo
    python examples/basic.py
"""

import asyncio
import os
import uuid
from datetime import date

from yara_orm import Model, YaraOrm, fields

DB_URL = os.environ.get("ORM_TEST_DB", "postgres://localhost/orm_demo")


class Tournament(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=100)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "tournaments"


class Event(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=100, index=True)
    prize = fields.DecimalField(max_digits=10, decimal_places=2, null=True)
    tags = fields.JSONField(null=True)
    ref = fields.UUIDField(null=True)
    day = fields.DateField(null=True)
    tournament = fields.ForeignKeyField("Tournament", related_name="events", on_delete="CASCADE")

    class Meta:
        table = "events"


async def _reset_demo_tables() -> None:
    """Drop demo tables so the example is repeatable."""
    from yara_orm.connection import get_engine

    engine = get_engine()
    await engine.execute("DROP TABLE IF EXISTS events CASCADE")
    await engine.execute("DROP TABLE IF EXISTS tournaments CASCADE")


async def main() -> None:
    await YaraOrm.init(DB_URL)
    await _reset_demo_tables()
    await YaraOrm.generate_schemas()

    cup = await Tournament.create(name="World Cup")
    print("created tournament:", cup.pk, cup.created_at)

    final = await Event.create(
        name="Final",
        prize="1000.50",
        tags={"round": "final", "teams": ["A", "B"]},
        ref=uuid.uuid4(),
        day=date(2026, 7, 19),
        tournament_id=cup.id,
    )
    await Event.create(name="Semi Final", tournament_id=cup.id)
    await Event.create(name="Quarter Final", tournament_id=cup.id)

    print("event count:", await Event.filter(tournament_id=cup.id).count())

    finals = await Event.filter(name__icontains="final").order_by("name")
    print("matching 'final':", [e.name for e in finals])

    fetched = await Event.get(id=final.id)
    print("round-tripped json:", fetched.tags, "prize:", fetched.prize)
    print("uuid type:", type(fetched.ref).__name__, "date:", fetched.day)

    await Event.filter(name="Semi Final").update(name="Semi-Final")
    print("after update exists:", await Event.filter(name="Semi-Final").exists())

    await Event.filter(name="Quarter Final").delete()
    print("remaining events:", await Event.all().count())

    await YaraOrm.close()


if __name__ == "__main__":
    asyncio.run(main())

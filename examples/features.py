"""A tour of the Tortoise-style features, mirroring the examples page.

Run against a local PostgreSQL:

    createdb orm_demo
    python examples/features.py
"""

import asyncio
import os
from enum import IntEnum

from yara_orm import (
    Avg,
    Count,
    Model,
    Prefetch,
    Q,
    YaraOrm,
    atomic,
    connections,
    fields,
    in_transaction,
    post_save,
)

DB_URL = os.environ.get("ORM_TEST_DB", "postgres://localhost/orm_demo")


class Service(IntEnum):
    BACKEND = 1
    FRONTEND = 2


class Tournament(Model):
    name = fields.CharField(max_length=100, description="display name")

    class Meta:
        table = "ex_tournament"
        table_description = "competition tournaments"


class Team(Model):
    name = fields.CharField(max_length=100)

    class Meta:
        table = "ex_team"


class Event(Model):
    name = fields.CharField(max_length=100)
    rating = fields.IntField(default=0)
    service = fields.IntEnumField(Service, default=Service.BACKEND)
    tournament = fields.ForeignKeyField("Tournament", related_name="events")
    participants = fields.ManyToManyField("Team", related_name="events", through="ex_event_team")

    class Meta:
        table = "ex_event"


@post_save(Event)
async def _log_event(sender, instance, created, using_db, update_fields):
    if created:
        print(f"  signal: created Event {instance.name!r}")


async def reset():
    engine = connections.get("default")
    # Dropped in dependency order so no CASCADE is needed (works on SQLite too).
    for table in ("ex_event_team", "ex_event", "ex_team", "ex_tournament"):
        await engine.execute(f"DROP TABLE IF EXISTS {table}")
    await YaraOrm.generate_schemas()


async def main() -> None:
    await YaraOrm.init(DB_URL)
    await reset()

    # --- Relations + signals --------------------------------------------
    cup = await Tournament.create(name="World Cup")
    final = await Event.create(name="Final", rating=5, tournament=cup, service=Service.FRONTEND)
    await Event.create(name="Semi", rating=3, tournament=cup)

    red = await Team.create(name="Red")
    blue = await Team.create(name="Blue")
    await final.participants.add(red, blue)

    print("forward FK :", (await final.tournament).name)
    print("reverse FK :", [e.name for e in await cup.events])
    print("m2m        :", sorted([t.name async for t in final.participants]))
    print("enum       :", final.service)

    # --- Prefetch (no N+1) ----------------------------------------------
    tours = await Tournament.all().prefetch_related(
        Prefetch("events", queryset=Event.filter(rating__gte=4))
    )
    print("prefetch   :", [(t.name, [e.name for e in await t.events]) for t in tours])

    # --- Q filtering -----------------------------------------------------
    rows = await Event.filter(Q(name="Final") | Q(rating__lt=4)).order_by("name")
    print("Q filter   :", [e.name for e in rows])

    # --- Aggregation -----------------------------------------------------
    counts = await Tournament.annotate(num=Count("events")).filter(num__gte=1)
    print("annotate   :", [(t.name, t.num) for t in counts])
    [stats] = await Event.annotate(avg=Avg("rating")).group_by().values("avg")
    print("avg rating :", float(stats["avg"]))

    # --- Transactions ----------------------------------------------------
    try:
        async with in_transaction():
            await Team.create(name="Temp")
            raise RuntimeError("rollback please")
    except RuntimeError:
        pass
    print("rolled back:", await Team.filter(name="Temp").exists(), "(False expected)")

    @atomic()
    async def add_team():
        await Team.create(name="Green")

    await add_team()
    print("committed  :", await Team.filter(name="Green").exists())

    # --- Manual SQL ------------------------------------------------------
    raw = await Event.raw("SELECT * FROM ex_event WHERE rating = $1", [5])
    print("raw SQL    :", [e.name for e in raw])

    await YaraOrm.close()


if __name__ == "__main__":
    asyncio.run(main())

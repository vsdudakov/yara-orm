"""Manual SQL: connections.get(...).execute_query-style access and Model.raw."""

import pytest

from yara_orm import Model, YaraOrm, connections, fields
from yara_orm.connection import get_engine


class Thing(Model):
    name = fields.CharField(max_length=50)

    class Meta:
        table = "m_thing"


async def _reset():
    engine = get_engine()
    await engine.execute("DROP TABLE IF EXISTS m_thing CASCADE")
    await YaraOrm.generate_schemas()


@pytest.mark.asyncio
async def test_raw_execute_and_fetch(orm):
    """
    GIVEN the default connection from connections.get()
    WHEN raw INSERT and SELECT statements are run
    THEN execute() reports affected rows and fetch_all() returns dict rows
    """
    await _reset()
    conn = connections.get("default")
    await conn.execute("INSERT INTO m_thing (name) VALUES ($1)", ["x"])
    affected = await conn.execute("INSERT INTO m_thing (name) VALUES ($1)", ["y"])
    assert affected == 1
    rows = await conn.fetch_all("SELECT name FROM m_thing ORDER BY name")
    assert [r["name"] for r in rows] == ["x", "y"]


@pytest.mark.asyncio
async def test_model_raw_returns_instances(orm):
    """
    GIVEN rows created via the ORM
    WHEN Model.raw runs a hand-written SELECT
    THEN it returns fully built model instances
    """
    await _reset()
    await Thing.create(name="alpha")
    await Thing.create(name="beta")

    objs = await Thing.raw("SELECT * FROM m_thing WHERE name = $1", ["alpha"])
    assert len(objs) == 1
    assert isinstance(objs[0], Thing)
    assert objs[0].name == "alpha"

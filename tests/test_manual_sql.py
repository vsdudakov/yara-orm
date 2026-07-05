"""Manual SQL: connections.get(...).execute_query-style access and Model.raw."""

import pytest

from yara_orm import Model, connections, fields


class Thing(Model):
    name = fields.CharField(max_length=50)

    class Meta:
        table = "m_thing"


MODELS = [Thing]


@pytest.mark.asyncio
async def test_raw_execute_and_fetch(db):
    """
    GIVEN the default connection from connections.get()
    WHEN raw INSERT and SELECT statements are run
    THEN execute() reports affected rows and fetch_all() returns dict rows
    """
    conn = connections.get("default")
    # raw SQL carries the driver's placeholder
    ph = {"mysql": "?", "mariadb": "?", "oracle": ":1", "mssql": "@P1"}.get(db, "$1")
    await conn.execute(f"INSERT INTO m_thing (name) VALUES ({ph})", ["x"])
    affected = await conn.execute(f"INSERT INTO m_thing (name) VALUES ({ph})", ["y"])
    assert affected == 1
    rows = await conn.fetch_all("SELECT name FROM m_thing ORDER BY name")
    assert [r["name"] for r in rows] == ["x", "y"]


@pytest.mark.asyncio
async def test_model_raw_returns_instances(db):
    """
    GIVEN rows created via the ORM
    WHEN Model.raw runs a hand-written SELECT
    THEN it returns fully built model instances
    """
    await Thing.create(name="alpha")
    await Thing.create(name="beta")

    ph = {"mysql": "?", "mariadb": "?", "mssql": "@P1"}.get(db, "$1")
    objs = await Thing.raw(f"SELECT * FROM m_thing WHERE name = {ph}", ["alpha"])
    assert len(objs) == 1
    assert isinstance(objs[0], Thing)
    assert objs[0].name == "alpha"

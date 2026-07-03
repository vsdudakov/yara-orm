"""Raw-SQL array binding, reverse-relation filtering, and JSON key-path lookups
(1.6.x follow-ups).

- a bare ``list``/``tuple`` raw-SQL parameter binds as a PostgreSQL array
  (``WHERE col = ANY($1)`` / ``unnest($1::int[])``), matching asyncpg.
- a reverse FK ``related_name`` is filterable: ``__isnull`` (existence) and
  field traversal (``related__field``).
- a ``JSONField`` supports key-path lookups (``data__key`` / ``data__a__b``)
  with the usual operators.
"""

import uuid

import pytest

from yara_orm import Model, connections, fields


class ArPortfolio(Model):
    id = fields.IntField(pk=True)

    class Meta:
        table = "ar_portfolio"


class ArAlert(Model):
    id = fields.IntField(pk=True)
    status = fields.CharField(max_length=10, null=True)
    portfolio = fields.ForeignKeyField("ArPortfolio", related_name="alerts")

    class Meta:
        table = "ar_alert"


class ArThing(Model):
    id = fields.UUIDField(pk=True)

    class Meta:
        table = "ar_thing"


class ArTask(Model):
    id = fields.IntField(pk=True)
    data = fields.JSONField(null=True)

    class Meta:
        table = "ar_task"


MODELS = [ArPortfolio, ArAlert, ArThing, ArTask]


@pytest.mark.asyncio
async def test_raw_int_list_binds_as_array(db):
    """
    GIVEN a raw query using an int array
    WHEN a bare list is passed as the parameter
    THEN it binds as an array (unnest yields the elements)
    """
    if db != "postgres":
        pytest.skip("arrays / unnest are PostgreSQL-only")
    conn = connections.get()
    rows = await conn.execute_query_dict("SELECT unnest($1::int[]) AS x", [[1, 2, 3]])
    assert sorted(r["x"] for r in rows) == [1, 2, 3]


@pytest.mark.asyncio
async def test_raw_uuid_list_any_filter(db):
    """
    GIVEN rows keyed by UUID
    WHEN filtered with `id = ANY($1)` and a bare list of UUIDs
    THEN the UUID elements are coerced and the matching rows return
    """
    if db != "postgres":
        pytest.skip("ANY(array) is PostgreSQL-only")
    t1 = await ArThing.create()
    t2 = await ArThing.create()
    await ArThing.create()
    conn = connections.get()
    rows = await conn.execute_query_dict(
        "SELECT id FROM ar_thing WHERE id = ANY($1) ORDER BY id", [[t1.id, t2.id]]
    )
    assert {uuid.UUID(str(r["id"])) for r in rows} == {t1.id, t2.id}


@pytest.mark.asyncio
async def test_uuid_and_array_params_mixed(db):
    """
    GIVEN a statement binding both a UUID param and an array param
    WHEN executed in either order (uuid + int[], text + uuid[], int[] + uuid)
    THEN the mixed binary encoding is correct (no 22P03)
    """
    if db != "postgres":
        pytest.skip("arrays / ::uuid casts are PostgreSQL-only")
    import uuid

    conn = connections.get()
    u = str(uuid.uuid4())

    r1 = await conn.execute_query_dict("SELECT $1::uuid AS u, $2::int[] AS a", [u, [1, 2]])
    assert r1[0]["a"] == [1, 2]
    assert str(r1[0]["u"]) == u

    r2 = await conn.execute_query_dict("SELECT $1::text AS s, $2::uuid[] AS a", ["hi", [u]])
    assert r2[0]["s"] == "hi"
    assert str(r2[0]["a"][0]) == u

    r3 = await conn.execute_query_dict("SELECT $1::int[] AS a, $2::uuid AS u", [[1, 2], u])
    assert r3[0]["a"] == [1, 2]
    assert str(r3[0]["u"]) == u


@pytest.mark.asyncio
async def test_reverse_relation_isnull(db):
    """
    GIVEN portfolios, one with an alert and one without
    WHEN filtered by the reverse related_name with __isnull
    THEN existence is tested correctly (has no / has related rows)
    """
    p1 = await ArPortfolio.create()
    p2 = await ArPortfolio.create()
    await ArAlert.create(portfolio=p1, status="open")

    assert [p.id for p in await ArPortfolio.filter(alerts__isnull=True)] == [p2.id]
    assert [p.id for p in await ArPortfolio.filter(alerts__isnull=False)] == [p1.id]
    assert [p.id for p in await ArPortfolio.filter(alerts__not_isnull=True)] == [p1.id]
    assert await ArPortfolio.filter(alerts__isnull=True).count() == 1


@pytest.mark.asyncio
async def test_reverse_relation_field_traversal(db):
    """
    GIVEN portfolios with alerts of different statuses
    WHEN filtered by a reverse related_name field path
    THEN only portfolios with a matching related row return
    """
    p1 = await ArPortfolio.create()
    p2 = await ArPortfolio.create()
    await ArAlert.create(portfolio=p1, status="open")
    await ArAlert.create(portfolio=p2, status="closed")

    assert [p.id for p in await ArPortfolio.filter(alerts__status="open")] == [p1.id]


def test_json_extract_sql_rendering():
    """
    GIVEN the JSON extraction helper on each dialect
    WHEN rendered with a key path (and with no keys)
    THEN it emits the dialect's operator (and returns the column unchanged)
    """
    from yara_orm.dialects import PostgresDialect, SqliteDialect

    pg = PostgresDialect()
    assert pg.json_extract_sql('"d"', ["a", "b"]) == "\"d\" -> 'a' ->> 'b'"
    assert pg.json_extract_sql('"d"', []) == '"d"'

    sq = SqliteDialect()
    assert sq.json_extract_sql('"d"', ["a", "b"]) == "json_extract(\"d\", '$.a.b')"
    assert sq.json_extract_sql('"d"', []) == '"d"'


@pytest.mark.asyncio
async def test_json_key_path_lookups(db):
    """
    GIVEN a JSON column with nested keys
    WHEN filtered by key paths and operators
    THEN the addressed values compare correctly
    """
    await ArTask.create(data={"a": {"b": "deep"}, "key": "open", "tags": ["x"]})
    await ArTask.create(data={"key": "closed"})

    assert len(await ArTask.filter(data__key="open")) == 1
    assert len(await ArTask.filter(data__a__b="deep")) == 1
    assert len(await ArTask.filter(data__key__contains="pe")) == 1
    assert len(await ArTask.filter(data__key__isnull=True)) == 0
    assert len(await ArTask.filter(data__missing__isnull=True)) == 2

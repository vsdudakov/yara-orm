"""Tortoise-migration compatibility: connection / lifecycle behaviours.

Covers the manual-SQL compat methods (execute_query / execute_query_dict /
execute_script / fetch_one), engine-error translation to OperationalError, the
pre-execute query hook, ``init(config=...)``, the Tortoise lifecycle aliases,
and foreign-key topological sorting in ``generate_schemas``.
"""

import os

import pytest

from yara_orm import (
    Model,
    YaraOrm,
    clear_query_hooks,
    connections,
    fields,
    register_query_hook,
)
from yara_orm.connection import _split_sql_statements, get_engine
from yara_orm.exceptions import OperationalError

DB_URL = os.environ.get("ORM_TEST_DB", "postgres://localhost/orm_demo")


class TpParent(Model):
    id = fields.IntField(pk=True)

    class Meta:
        table = "tp_parent"


class TpChild(Model):
    id = fields.IntField(pk=True)
    parent = fields.ForeignKeyField("TpParent", related_name="kids")

    class Meta:
        table = "tp_child"


def test_split_sql_statements_respects_dollar_quotes():
    """
    GIVEN a script with a dollar-quoted PL/pgSQL block containing semicolons
    WHEN it is split into statements
    THEN the block stays intact and the surrounding statements split correctly
    """
    script = "SELECT 1; DO $$ BEGIN PERFORM 1; END $$; SELECT 2;"
    stmts = _split_sql_statements(script)
    assert len(stmts) == 3
    assert stmts[1].startswith("DO $$") and "END $$" in stmts[1]


@pytest.mark.asyncio
async def test_execute_query_returns_rowcount_and_rows(orm):
    """
    GIVEN the Tortoise ``execute_query`` shape
    WHEN a SELECT is run via the manual connection
    THEN it returns a ``(rowcount, rows)`` tuple with dict rows
    """
    rowcount, rows = await connections.get().execute_query("SELECT 1 AS n")
    assert rowcount == 1
    assert rows == [{"n": 1}]


@pytest.mark.asyncio
async def test_execute_query_dict_and_fetch_one(orm):
    """
    GIVEN the Tortoise ``execute_query_dict`` / ``fetch_one`` methods
    WHEN a SELECT is run via the manual connection
    THEN dict rows and a single dict row are returned
    """
    conn = connections.get()
    assert await conn.execute_query_dict("SELECT 1 AS n") == [{"n": 1}]
    assert await conn.fetch_one("SELECT 1 AS n") == {"n": 1}


@pytest.mark.asyncio
async def test_execute_script_runs_multiple_statements(orm):
    """
    GIVEN a multi-statement SQL script
    WHEN it is run via ``execute_script``
    THEN every statement executes in order
    """
    conn = connections.get()
    await conn.execute_script(
        "DROP TABLE IF EXISTS cc_script; "
        "CREATE TABLE cc_script (id int); "
        "INSERT INTO cc_script VALUES (1);"
    )
    rows = await conn.execute_query_dict("SELECT id FROM cc_script")
    await conn.execute("DROP TABLE cc_script")
    assert rows == [{"id": 1}]


@pytest.mark.asyncio
async def test_sql_error_raises_operational_error(orm):
    """
    GIVEN a statement that fails in the engine (bare RuntimeError natively)
    WHEN it is run via the manual connection
    THEN it surfaces as OperationalError (Tortoise-compatible)
    """
    with pytest.raises(OperationalError):
        await connections.get().execute("SELECT * FROM no_such_table_xyz")


@pytest.mark.asyncio
async def test_query_hook_observes_sql(orm):
    """
    GIVEN a registered pre-execute query hook
    WHEN a statement runs via the manual connection
    THEN the hook observes the SQL; clearing hooks restores zero overhead
    """
    seen: list[str] = []
    register_query_hook(lambda sql, params: seen.append(sql))
    try:
        await connections.get().execute_query_dict("SELECT 1 AS n")
    finally:
        clear_query_hooks()
    assert any("SELECT 1" in s for s in seen)


@pytest.mark.asyncio
async def test_init_from_tortoise_config_dict():
    """
    GIVEN a Tortoise-style config dict with a default connection URL
    WHEN the ORM is initialised via ``init(config=...)``
    THEN the default connection works and ``close_connections`` tears it down
    """
    await YaraOrm.init(config={"connections": {"default": DB_URL}, "use_tz": False})
    try:
        assert await YaraOrm.get_connection().execute_query_dict("SELECT 1 AS n") == [{"n": 1}]
    finally:
        await YaraOrm.close_connections()


@pytest.mark.asyncio
async def test_generate_schemas_topo_sorts_fk_dependencies(orm):
    """
    GIVEN models passed in the wrong order (child before its FK target)
    WHEN ``generate_schemas`` builds them
    THEN it topologically reorders so the FK target table exists first
    """
    eng = get_engine()
    await eng.execute("DROP TABLE IF EXISTS tp_child CASCADE")
    await eng.execute("DROP TABLE IF EXISTS tp_parent CASCADE")
    await YaraOrm.generate_schemas(models=[TpChild, TpParent])
    try:
        p = await TpParent.create()
        c = await TpChild.create(parent=p)
        assert c.parent_id == p.id
    finally:
        await eng.execute("DROP TABLE IF EXISTS tp_child CASCADE")
        await eng.execute("DROP TABLE IF EXISTS tp_parent CASCADE")

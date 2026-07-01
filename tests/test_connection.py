"""Coverage: connection routing, named connections, transactions, errors."""

import os
import tempfile

import pytest

from yara_orm import (
    ConfigurationError,
    Model,
    OperationalError,
    YaraOrm,
    clear_query_hooks,
    connections,
    fields,
    in_transaction,
    register_query_hook,
)
from yara_orm.connection import _split_sql_statements, get_engine
from yara_orm.dialects import BaseDialect, get_dialect, register_dialect

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


class CvStar(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "cov_star"


class CvPlanet(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "cov_planet"


MODELS = [CvStar, CvPlanet]


class _Router:
    def db_for_read(self, model):
        return "second" if model.__name__ == "CvPlanet" else "default"

    def db_for_write(self, model):
        return self.db_for_read(model)


def test_get_engine_and_dialect_before_init():
    """
    GIVEN the ORM has not been initialised
    WHEN get_engine / get_dialect are called
    THEN both raise ConfigurationError
    """
    from yara_orm.connection import get_dialect as conn_get_dialect
    from yara_orm.connection import get_engine

    with pytest.raises(ConfigurationError):
        get_engine()
    with pytest.raises(ConfigurationError):
        conn_get_dialect()


@pytest.mark.asyncio
async def test_unsupported_url_rejected():
    """
    GIVEN an unsupported database URL scheme
    WHEN YaraOrm.init is called
    THEN the engine rejects it with a ValueError
    """
    with pytest.raises(ValueError):
        await YaraOrm.init("mysql://localhost/nope")


def test_dialect_registry():
    """
    GIVEN the dialect registry
    WHEN resolving an unknown name and registering a custom dialect
    THEN unknown names raise and registered ones resolve
    """
    with pytest.raises(ConfigurationError):
        get_dialect("nosuch")

    class MyDialect(BaseDialect):
        name = "mydb"

    register_dialect("mydb", MyDialect)
    assert isinstance(get_dialect("mydb"), MyDialect)


# SQLite-only: exercises routing across two SQLite files it sets up itself.
@pytest.mark.asyncio
async def test_router_directs_models_between_sqlite_files():
    """
    GIVEN two SQLite connections and a router
    WHEN models are created and read
    THEN each model routes to its configured connection
    """
    paths = []
    for _ in range(2):
        fd, p = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(p)
        paths.append(p)
    await YaraOrm.init(f"sqlite://{paths[0]}", router=_Router())
    await YaraOrm.add_connection("second", f"sqlite://{paths[1]}")
    try:
        await YaraOrm.generate_schemas()
        await CvStar.create(name="Sun")
        await CvPlanet.create(name="Earth")

        # Planets live only in the second database.
        default_planet = await connections.get("default").fetch_rows(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='cov_planet'"
        )
        assert default_planet[0][0] == 0
        assert (await connections.get("second").fetch_rows("SELECT count(*) FROM cov_planet"))[0][
            0
        ] == 1
        assert await CvPlanet.all().count() == 1
        assert await CvStar.all().count() == 1
    finally:
        await YaraOrm.close()
        for p in paths:
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(p + suffix):
                    os.remove(p + suffix)


@pytest.mark.asyncio
async def test_set_router_and_transaction_fetch_all(db):
    """
    GIVEN an initialised ORM
    WHEN set_router is toggled and a transaction runs manual SQL
    THEN routing is configurable and the transaction wrapper serves fetch_all
    """
    YaraOrm.set_router(None)
    await CvStar.create(name="x")
    async with in_transaction():
        conn = connections.get("default")
        await conn.execute("INSERT INTO cov_star (name) VALUES ($1)", ["y"])
        rows = await conn.fetch_all("SELECT name FROM cov_star ORDER BY name")
        assert [r["name"] for r in rows] == ["x", "y"]
        assert (await conn.fetch_row("SELECT count(*) FROM cov_star"))[0] == 2


def test_column_type_unknown_kind():
    """
    GIVEN a field with an unmapped kind
    WHEN the dialect renders its column type
    THEN a ConfigurationError is raised
    """
    from yara_orm.dialects import PostgresDialect

    field = fields.Field()
    field.field_kind = "bogus"
    field.db_column = "x"
    with pytest.raises(ConfigurationError):
        PostgresDialect().column_type(field)


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


def test_split_sql_statements_quotes_comments_and_no_trailing_semicolon():
    """
    GIVEN a script with line/block comments, a quoted ``''`` escape, a bare ``$``
        and no trailing semicolon
    WHEN it is split into statements
    THEN those constructs are handled and the final statement is kept
    """
    script = (
        ";;"  # empty statements are skipped
        "SELECT 1; -- a line comment ; not a split\n"
        "/* block ; comment */ SELECT 'O''Brien' AS who; "
        "DO $tag$ BEGIN PERFORM 1; END $tag$; "
        "SELECT 2 $ 3"
    )
    stmts = _split_sql_statements(script)
    assert stmts[0] == "SELECT 1"
    assert "O''Brien" in stmts[1]
    assert "$tag$" in stmts[2] and "END $tag$" in stmts[2]  # named dollar tag intact
    assert stmts[-1] == "SELECT 2 $ 3"  # no trailing ';' -> tail kept


def test_connection_url_from_credentials_dict():
    """
    GIVEN a structured connection spec (credentials dict)
    WHEN it is resolved to a URL
    THEN a postgres URL is built from the credentials
    """
    url = YaraOrm._connection_url(
        {"credentials": {"user": "u", "password": "p", "host": "h", "port": 6, "database": "d"}}
    )
    assert url == "postgres://u:p@h:6/d"


@pytest.mark.asyncio
async def test_execute_query_returns_rowcount_and_rows(orm):
    """
    GIVEN the ``execute_query`` shape
    WHEN a SELECT is run via the manual connection
    THEN it returns a ``(rowcount, rows)`` tuple with dict rows
    """
    rowcount, rows = await connections.get().execute_query("SELECT 1 AS n")
    assert rowcount == 1
    assert rows == [{"n": 1}]


@pytest.mark.asyncio
async def test_execute_query_dict_and_fetch_one(orm):
    """
    GIVEN the ``execute_query_dict`` / ``fetch_one`` methods
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
    THEN it surfaces as OperationalError
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
async def test_init_from_config_dict():
    """
    GIVEN a config dict with a default connection URL
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


@pytest.mark.asyncio
async def test_init_requires_db_url_or_config():
    """
    GIVEN no db_url and no config
    WHEN init is called
    THEN it raises ConfigurationError
    """
    with pytest.raises(ConfigurationError):
        await YaraOrm.init()


@pytest.mark.asyncio
async def test_init_config_requires_default_connection():
    """
    GIVEN a config dict without a 'default' connection
    WHEN init is called
    THEN it raises ConfigurationError
    """
    with pytest.raises(ConfigurationError):
        await YaraOrm.init(config={"connections": {}})


@pytest.mark.asyncio
async def test_init_config_registers_extra_connections():
    """
    GIVEN a config dict with a default and an extra named connection
    WHEN init runs
    THEN both connections are registered and usable
    """
    await YaraOrm.init(config={"connections": {"default": DB_URL, "reader": DB_URL}})
    try:
        rows = await connections.get("reader").execute_query_dict("SELECT 1 AS n")
        assert rows == [{"n": 1}]
    finally:
        await YaraOrm.close()


@pytest.mark.asyncio
async def test_transaction_fetch_one_and_engine_proxy_passthrough(orm):
    """
    GIVEN a transaction connection and the pooled engine proxy
    WHEN fetch_one and a passthrough attribute are used
    THEN fetch_one returns a dict row and the proxy exposes engine attributes
    """
    async with in_transaction() as conn:
        assert await conn.fetch_one("SELECT 1 AS n") == {"n": 1}
    assert connections.get().dialect in {"postgres", "sqlite"}


@pytest.mark.asyncio
async def test_execute_query_rows_support_positional_access(db):
    """
    GIVEN a raw SQL query run through the manual-SQL connection
    WHEN a returned row is indexed positionally and by key
    THEN both forms work (asyncpg.Record-like), including a slice
    """
    star = await CvStar.create(name="Sun")
    _, rows = await connections.get().execute_query(
        f"SELECT id, name FROM cov_star WHERE id = {star.id}"
    )
    assert rows[0][0] == star.id
    assert rows[0]["name"] == "Sun"
    assert tuple(rows[0][0:2]) == (star.id, "Sun")


def test_normalize_url_rewrites_postgres_aliases():
    """
    GIVEN driver-qualified postgres URL schemes
    WHEN _normalize_url processes them
    THEN postgres-family schemes become postgres:// and others pass through
    """
    assert YaraOrm._normalize_url("psycopg://u@h/db") == "postgres://u@h/db"
    assert YaraOrm._normalize_url("asyncpg://u@h/db") == "postgres://u@h/db"
    assert YaraOrm._normalize_url("postgresql+asyncpg://u@h/db") == "postgres://u@h/db"
    assert YaraOrm._normalize_url("sqlite:///app.db") == "sqlite:///app.db"


@pytest.mark.asyncio
async def test_raw_scalar_params_bind_without_cast(orm):
    """
    GIVEN raw SQL with an uncast positional parameter
    WHEN non-string scalars are bound (no ``::type`` cast)
    THEN each binds via its declared type and round-trips with that type
    """
    import uuid

    code = uuid.uuid4()
    conn = connections.get()
    assert (await conn.execute_query("SELECT $1 AS v", [5]))[1][0]["v"] == 5
    assert (await conn.execute_query("SELECT $1 AS v", [code]))[1][0]["v"] == code
    assert (await conn.execute_query("SELECT $1 AS v", [1.5]))[1][0]["v"] == 1.5
    assert (await conn.execute_query("SELECT $1 AS v", [True]))[1][0]["v"] is True


@pytest.mark.asyncio
async def test_array_param_binds_and_round_trips(orm):
    """
    GIVEN a sequence wrapped in Array
    WHEN it is bound as a PostgreSQL array parameter
    THEN it binds/round-trips as a real array (incl. NULL elements and ANY())
    """
    import uuid

    from yara_orm import Array

    conn = connections.get()
    ids = [uuid.uuid4(), uuid.uuid4()]
    assert (await conn.execute_query("SELECT $1::uuid[] AS a", [Array(ids)]))[1][0]["a"] == ids
    assert (await conn.execute_query("SELECT $1::int[] AS a", [Array([1, 2, 3])]))[1][0]["a"] == [
        1,
        2,
        3,
    ]
    nulls = (await conn.execute_query("SELECT $1::text[] AS a", [Array(["x", None, "y"])]))[1][0][
        "a"
    ]
    assert nulls == ["x", None, "y"]

    await conn.execute("DROP TABLE IF EXISTS arr_t")
    await conn.execute("CREATE TABLE arr_t (id int)")
    await conn.execute("INSERT INTO arr_t VALUES (1), (2), (3)")
    rows = (
        await conn.execute_query(
            "SELECT id FROM arr_t WHERE id = ANY($1) ORDER BY id", [Array([1, 3])]
        )
    )[1]
    assert [r["id"] for r in rows] == [1, 3]


@pytest.mark.asyncio
async def test_raw_list_param_binds_as_array(orm):
    """
    GIVEN a bare Python list bound as a raw-SQL parameter
    WHEN it is used with an array context (asyncpg-style)
    THEN it encodes as a PostgreSQL array, while a dict still binds as JSON and a
    JSON array is bound via a JSON string
    """
    conn = connections.get()
    # A bare list now binds as an array (matching asyncpg), so ANY()/unnest work.
    rows = await conn.execute_query_dict("SELECT unnest($1::int[]) AS x", [[1, 2, 3]])
    assert [r["x"] for r in rows] == [1, 2, 3]
    # A dict still binds as JSON (dicts have no array interpretation).
    assert (await conn.execute_query("SELECT $1::jsonb AS j", [{"a": 1}]))[1][0]["j"] == {"a": 1}
    # A JSON array in a raw query is passed as a JSON string.
    assert (await conn.execute_query("SELECT $1::jsonb AS j", ["[1, 2, 3]"]))[1][0]["j"] == [
        1,
        2,
        3,
    ]

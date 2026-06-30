"""Connection-URL pool/cache parameters (``max_size`` / ``min_size`` /
``statement_cache_size``).

These are stripped from the URL by the Rust backend before the driver parses
it, so they must not leak into the driver and must not break ordinary queries.
Exercised here end-to-end against SQLite (no live PostgreSQL needed).
"""

import os
import tempfile

import pytest

from yara_orm import Model, YaraOrm, connections, fields


class PoolRow(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20)

    class Meta:
        table = "pool_row"


async def _roundtrip(url: str) -> str:
    """Init against ``url``, create+read a row, return the read-back name."""
    await YaraOrm.init(url)
    try:
        await YaraOrm.generate_schemas()
        await PoolRow.create(name="x")
        row = await PoolRow.get(name="x")
        return row.name
    finally:
        await YaraOrm.close()


def _tmp_db_url(query: str = "") -> tuple[str, str]:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    return f"sqlite://{path}{query}", path


def _cleanup(path: str) -> None:
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)


@pytest.mark.asyncio
async def test_max_size_param_is_stripped_and_honored():
    """
    GIVEN a connection URL carrying ?max_size=
    WHEN the ORM connects and runs a query
    THEN the param is consumed (not passed to the driver) and queries work
    """
    url, path = _tmp_db_url("?max_size=4")
    try:
        assert await _roundtrip(url) == "x"
    finally:
        _cleanup(path)


@pytest.mark.asyncio
async def test_statement_cache_disabled_still_works():
    """
    GIVEN ?statement_cache_size=0 (the PgBouncer-safe setting)
    WHEN queries run
    THEN they succeed using uncached prepared statements
    """
    url, path = _tmp_db_url("?statement_cache_size=0")
    try:
        assert await _roundtrip(url) == "x"
    finally:
        _cleanup(path)


@pytest.mark.asyncio
async def test_all_pool_params_combined():
    """
    GIVEN every recognised pool/cache param at once
    WHEN the ORM connects
    THEN they are all consumed and queries work
    """
    url, path = _tmp_db_url("?max_size=4&min_size=2&statement_cache_size=0")
    try:
        assert await _roundtrip(url) == "x"
    finally:
        _cleanup(path)


@pytest.mark.asyncio
async def test_invalid_pool_param_value_raises():
    """
    GIVEN a non-numeric pool param value
    WHEN the ORM connects
    THEN a ValueError (config error) is raised, not a silent ignore
    """
    url, path = _tmp_db_url("?max_size=lots")
    try:
        with pytest.raises(ValueError):
            await YaraOrm.init(url)
    finally:
        await YaraOrm.close()
        _cleanup(path)


_PG_URL = os.environ.get("ORM_TEST_DB", "postgres://localhost/orm_demo")


@pytest.mark.asyncio
async def test_application_name_and_server_settings_via_url():
    """
    GIVEN a PostgreSQL URL with ?application_name= and a libpq ?options= setting
    WHEN the ORM connects
    THEN both take effect on the connection (the application_name /
        server_settings, carried through the URL alongside the pool params)
    """
    if not _PG_URL.startswith("postgres"):
        pytest.skip("application_name / server_settings are PostgreSQL-specific")
    sep = "&" if "?" in _PG_URL else "?"
    extra = "application_name=yara_conn_test&options=-c%20search_path%3Dpublic&max_size=4"
    await YaraOrm.init(f"{_PG_URL}{sep}{extra}")
    try:
        conn = connections.get()
        [row] = await conn.fetch_all(
            "SELECT application_name FROM pg_stat_activity WHERE pid = pg_backend_pid()"
        )
        assert row["application_name"] == "yara_conn_test"
        [sp] = await conn.fetch_all("SHOW search_path")
        assert sp["search_path"] == "public"
    finally:
        await YaraOrm.close()

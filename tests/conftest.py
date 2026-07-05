import contextlib
import functools
import os
import socket
import tempfile
import urllib.parse

import pytest
import pytest_asyncio

from yara_orm import YaraOrm
from yara_orm.connection import get_engine

# Per-backend connection URLs, overridable via the environment.
PG_URL = os.environ.get("ORM_TEST_DB", "postgres://localhost/orm_demo")
MYSQL_URL = os.environ.get("ORM_TEST_MYSQL", "mysql://root:root@localhost:3306/orm_demo")
# MariaDB shares the MySQL driver/wire protocol but is a distinct backend (it
# adds RETURNING and diverges on JSON/regex/upsert/FOR UPDATE OF), so it runs as
# its own parametrisation against a separate server.
MARIADB_URL = os.environ.get("ORM_TEST_MARIADB", "mariadb://root:root@localhost:3307/orm_demo")
ORACLE_URL = os.environ.get("ORM_TEST_ORACLE", "oracle://orm:orm@localhost:1521/FREEPDB1")
MSSQL_URL = os.environ.get("ORM_TEST_MSSQL", "mssql://sa:yaraOrm_Pass1@localhost:1433/master")

# Backends every ``db``-parametrised test runs against. Override to scope a run
# (e.g. ``ORM_TEST_BACKENDS=sqlite``); extend by adding a branch in
# ``_setup_backend`` when a new backend lands. Each networked backend skips
# itself when no server is reachable at its URL (see the ``*_reachable`` probes).
TEST_BACKENDS = os.environ.get(
    "ORM_TEST_BACKENDS", "sqlite,postgres,mysql,mariadb,oracle,mssql"
).split(",")


def _tcp_reachable(url: str, default_port: int) -> bool:
    """Report whether a TCP connection to the URL's host/port succeeds.

    Args:
        url: The connection URL whose host/port to probe.
        default_port: Port used when the URL does not carry one.

    Returns:
        True when the server accepts a TCP connection.
    """
    parsed = urllib.parse.urlsplit(url)
    try:
        with socket.create_connection(
            (parsed.hostname or "localhost", parsed.port or default_port), timeout=1
        ):
            return True
    except OSError:
        return False


@functools.cache
def sqlite_reachable() -> bool:
    """SQLite is file-based and always available.

    Returns:
        True.
    """
    return True


@functools.cache
def pg_reachable() -> bool:
    """Probe the configured PostgreSQL host/port once per test session.

    Returns:
        True when a TCP connection to the PG_URL host/port succeeds.
    """
    return _tcp_reachable(PG_URL, 5432)


@functools.cache
def mysql_reachable() -> bool:
    """Probe the configured MySQL host/port once per test session.

    Returns:
        True when a TCP connection to the MYSQL_URL host/port succeeds.
    """
    return _tcp_reachable(MYSQL_URL, 3306)


@functools.cache
def mariadb_reachable() -> bool:
    """Probe the configured MariaDB host/port once per test session.

    Returns:
        True when a TCP connection to the MARIADB_URL host/port succeeds.
    """
    return _tcp_reachable(MARIADB_URL, 3306)


@functools.cache
def oracle_reachable() -> bool:
    """Probe the configured Oracle host/port once per test session.

    Returns:
        True when a TCP connection to the ORACLE_URL host/port succeeds.
    """
    return _tcp_reachable(ORACLE_URL, 1521)


@functools.cache
def mssql_reachable() -> bool:
    """Probe the configured SQL Server host/port once per test session.

    Returns:
        True when a TCP connection to the MSSQL_URL host/port succeeds.
    """
    return _tcp_reachable(MSSQL_URL, 1433)


@pytest_asyncio.fixture
async def orm():
    """Initialise the ORM against PostgreSQL and tear it down per test."""
    await YaraOrm.init(PG_URL)
    try:
        yield
    finally:
        await YaraOrm.close()


async def _sqlite_session(generate: bool):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    await YaraOrm.init(f"sqlite://{path}")
    try:
        if generate:
            await YaraOrm.generate_schemas()
        yield
    finally:
        await YaraOrm.close()
        for suffix in ("", "-wal", "-shm"):
            # Tolerate a sidecar vanishing between check and remove (see below).
            with contextlib.suppress(FileNotFoundError):
                os.remove(path + suffix)


@pytest_asyncio.fixture
async def sqlite_db():
    """Fresh temporary SQLite database with schemas generated, per test.

    Used by the e2e coverage tests: fast, deterministic and dependency-free.
    """
    async for _ in _sqlite_session(generate=True):
        yield


@pytest_asyncio.fixture
async def sqlite_empty():
    """Fresh temporary SQLite database with no tables created yet.

    Used by migration tests that build their own schema via migrations.
    """
    async for _ in _sqlite_session(generate=False):
        yield


# ---------------------------------------------------------------------------
# Cross-backend fixture: run one test on every configured backend.
# ---------------------------------------------------------------------------
def _module_tables(models: list) -> list[str]:
    """Collect the table names a model set owns (its tables + m2m join tables).

    Args:
        models: The models whose tables to enumerate.

    Returns:
        The owned table names, join tables first so drops cascade cleanly.
    """
    tables: list[str] = []
    for model in models:
        for info in model._meta.m2m.values():
            info.finalize()
            tables.append(info.through)
    tables += [model._meta.table for model in models]
    return tables


async def _drop_tables(tables: list[str]) -> None:
    """Drop the given tables if present, ignoring foreign-key order.

    Args:
        tables: Table names to drop.

    Returns:
        None
    """
    engine = get_engine()
    for table in tables:
        await engine.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')


async def _drop_tables_oracle(tables: list[str]) -> None:
    """Drop the given tables on Oracle (``CASCADE CONSTRAINTS`` severs FKs).

    Args:
        tables: Table names to drop.

    Returns:
        None
    """
    engine = get_engine()
    for table in tables:
        await engine.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE CONSTRAINTS')


async def _drop_tables_mssql(tables: list[str]) -> None:
    """Drop the given tables on SQL Server, severing referencing FKs first.

    SQL Server has no ``DROP ... CASCADE``; drop every table's inbound foreign
    keys (via ``sys.foreign_keys``) before dropping the tables, so order does
    not matter.

    Args:
        tables: Table names to drop.

    Returns:
        None
    """
    engine = get_engine()
    for table in tables:
        # Drop any FK constraints that reference this table, then the table.
        await engine.execute(
            "DECLARE @sql NVARCHAR(MAX) = N'';"
            "SELECT @sql = @sql + N'ALTER TABLE ' + QUOTENAME(OBJECT_SCHEMA_NAME(parent_object_id))"
            " + N'.' + QUOTENAME(OBJECT_NAME(parent_object_id)) + N' DROP CONSTRAINT '"
            " + QUOTENAME(name) + N';' FROM sys.foreign_keys"
            f" WHERE referenced_object_id = OBJECT_ID(N'[{table}]');"
            "EXEC sp_executesql @sql;"
        )
        await engine.execute(f"DROP TABLE IF EXISTS [{table}]")


async def _drop_tables_mysql(tables: list[str]) -> None:
    """Drop the given tables on MySQL, which has no ``DROP ... CASCADE``.

    Runs as one script on a single pinned connection so the
    ``FOREIGN_KEY_CHECKS`` session toggle applies to every drop regardless of
    foreign-key order.

    Args:
        tables: Table names to drop.

    Returns:
        None
    """
    statements = ["SET FOREIGN_KEY_CHECKS=0"]
    statements += [f"DROP TABLE IF EXISTS `{table}`" for table in tables]
    statements.append("SET FOREIGN_KEY_CHECKS=1")
    await get_engine().execute_script(statements)


async def _setup_backend(backend: str, models: list):
    """Initialise one backend with the module's schema and tear it down after.

    SQLite gets a throwaway file; PostgreSQL drops and recreates just this
    module's tables (fast and isolated, without touching other suites). Add an
    ``elif`` here to support a further backend.

    Args:
        backend: Backend name from :data:`TEST_BACKENDS`.
        models: The module's models (``MODELS``), in dependency order.

    Returns:
        None
    """
    tables = _module_tables(models)
    if backend == "sqlite":
        if not sqlite_reachable():  # pragma: no cover - always available
            pytest.skip("SQLite unavailable")
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(path)
        await YaraOrm.init(f"sqlite://{path}")
        await YaraOrm.generate_schemas(models=models)
        try:
            yield backend
        finally:
            await YaraOrm.close()
            for suffix in ("", "-wal", "-shm"):
                # The -wal/-shm sidecars may vanish between the check and the
                # remove (SQLite checkpoint on close), so tolerate their absence.
                with contextlib.suppress(FileNotFoundError):
                    os.remove(path + suffix)
    elif backend == "postgres":
        if not pg_reachable():
            pytest.skip(f"PostgreSQL server not reachable at {PG_URL}")
        await YaraOrm.init(PG_URL)
        await _drop_tables(tables)
        await YaraOrm.generate_schemas(models=models)
        try:
            yield backend
        finally:
            await _drop_tables(tables)
            await YaraOrm.close()
    elif backend == "mysql":
        if not mysql_reachable():
            pytest.skip(f"MySQL server not reachable at {MYSQL_URL}")
        await YaraOrm.init(MYSQL_URL)
        await _drop_tables_mysql(tables)
        await YaraOrm.generate_schemas(models=models)
        try:
            yield backend
        finally:
            await _drop_tables_mysql(tables)
            await YaraOrm.close()
    elif backend == "mariadb":
        if not mariadb_reachable():
            pytest.skip(f"MariaDB server not reachable at {MARIADB_URL}")
        await YaraOrm.init(MARIADB_URL)
        await _drop_tables_mysql(tables)  # MariaDB shares MySQL's DDL/teardown
        await YaraOrm.generate_schemas(models=models)
        try:
            yield backend
        finally:
            await _drop_tables_mysql(tables)
            await YaraOrm.close()
    elif backend == "oracle":
        if not oracle_reachable():
            pytest.skip(f"Oracle server not reachable at {ORACLE_URL}")
        await YaraOrm.init(ORACLE_URL)
        await _drop_tables_oracle(tables)
        await YaraOrm.generate_schemas(models=models)
        try:
            yield backend
        finally:
            await _drop_tables_oracle(tables)
            await YaraOrm.close()
    elif backend == "mssql":
        if not mssql_reachable():
            pytest.skip(f"SQL Server not reachable at {MSSQL_URL}")
        await YaraOrm.init(MSSQL_URL)
        await _drop_tables_mssql(tables)
        await YaraOrm.generate_schemas(models=models)
        try:
            yield backend
        finally:
            await _drop_tables_mssql(tables)
            await YaraOrm.close()
    else:  # pragma: no cover - guards a misconfigured ORM_TEST_BACKENDS
        raise ValueError(f"unknown test backend: {backend!r}")


@pytest_asyncio.fixture(params=TEST_BACKENDS)
async def db(request):
    """Run the test once per configured backend with a fresh module schema.

    The test module must define ``MODELS = [...]`` (dependency-ordered) listing
    the models it uses; the fixture creates exactly those tables on each
    backend and drops them afterwards.

    Args:
        request: The pytest request exposing the backend param and test module.

    Returns:
        None
    """
    models = getattr(request.module, "MODELS", None)
    if models is None:  # pragma: no cover - misuse guard
        raise RuntimeError(
            f"{request.module.__name__} must define MODELS=[...] to use the 'db' fixture"
        )
    async for backend in _setup_backend(request.param, models):
        yield backend


# ---------------------------------------------------------------------------
# Oracle backend: skip the cross-backend tests the young oracle-rs 0.1.x driver
# cannot support (values above the max VARCHAR2/RAW size that need CLOB/LONG
# binding, SET TRANSACTION ISOLATION drops, unimplemented __search/JSON
# __contains, and a handful of inherent Oracle raw-SQL differences). The pinned
# driver fork fixes the fetch cap, large-value binds and constraint-violation
# reporting. See docs/backends for the full list.
# ---------------------------------------------------------------------------
_ORACLE_LIMITATIONS = {
    "test_annotator_comment_reaches_all_query_paths",
    "test_atomic_decorator_with_isolation",
    "test_aware_datetime_roundtrips_aware",
    "test_bulk_create_ignore_conflicts",
    "test_bulk_create_ignore_conflicts_skips_duplicate",
    "test_bulk_create_ignore_conflicts_skips_duplicates",
    "test_bulk_create_update_fields_defaults_conflict_to_pk",
    "test_bulk_create_upsert_custom_conflict_target",
    "test_bulk_get_or_create_preserves_input_order",
    "test_bulk_update_or_create_all_existing",
    "test_bulk_update_or_create_in_batch_duplicate_existing",
    "test_clone_creates_new_row",
    "test_clone_with_explicit_pk",
    "test_composite_index_created",
    "test_contains_array_element",
    "test_contains_array_of_objects",
    "test_contains_object_subset",
    "test_decimal_column_type",
    "test_decimal_precision",
    "test_defer_with_annotate_keeps_column_deferred",
    "test_execute_many_applies_nothing_on_failure",
    "test_execute_query_rows_support_positional_access",
    "test_execute_script_honours_explicit_transaction_control",
    "test_execute_script_paths_carry_the_comment",
    "test_execute_script_statements_run_in_autocommit",
    "test_fetch_db_defaults_refreshes_the_instance",
    "test_isolation_repeatable_read_per_backend",
    "test_isolation_serializable_both_backends",
    "test_json_contains_postgres",
    "test_like_lookups_accept_sql_expression_values",
    "test_malicious_value_cannot_break_out_of_the_comment",
    "test_meta_check_constraint_enforced",
    "test_meta_unique_constraint_enforced",
    "test_model_distinct_and_select_for_update",
    "test_model_earliest_latest",
    "test_model_exists",
    "test_model_first_last",
    "test_model_raw_returns_instances",
    "test_model_values_and_values_list",
    "test_not_null_violation_raises_integrity_error",
    "test_random_function",
    "test_random_hex_width",
    "test_raw_execute_and_fetch",
    "test_raw_sql_annotation",
    "test_raw_sql_binds_params_instead_of_interpolating",
    "test_release_of_unknown_savepoint_is_operational_error",
    "test_select_for_update_values_with_joined_path_locks_base_table",
    "test_serializable_isolation_level_is_accepted",
    "test_set_router_and_transaction_fetch_all",
    "test_slicing_combined_shape",
    "test_sql_and_explain",
    "test_sqlite_omits_using_and_include_but_keeps_unique",
    "test_unique_composite_index_enforces_uniqueness",
    "test_using_and_include_render_in_schema_sql",
    "test_window_annotation_with_select_related_and_only",
}


def pytest_collection_modifyitems(config, items):
    """Skip the tests a known oracle-rs 0.1.x driver limitation blocks."""
    reason = (
        "known oracle-rs 0.1.x limitation (see docs/backends): over-max-size "
        "CLOB/LONG bind, isolation-level drop, unimplemented __search/__contains, "
        "or an inherent Oracle raw-SQL difference"
    )
    skip = pytest.mark.skip(reason=reason)
    for item in items:
        if "[oracle]" in item.nodeid and item.originalname in _ORACLE_LIMITATIONS:
            item.add_marker(skip)

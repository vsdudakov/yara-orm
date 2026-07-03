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

# Backends every ``db``-parametrised test runs against. Override to scope a run
# (e.g. ``ORM_TEST_BACKENDS=sqlite``); extend by adding a branch in
# ``_setup_backend`` when a new backend lands. The mysql backend skips itself
# when no server is reachable at MYSQL_URL (see ``mysql_reachable``).
TEST_BACKENDS = os.environ.get("ORM_TEST_BACKENDS", "sqlite,postgres,mysql").split(",")


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

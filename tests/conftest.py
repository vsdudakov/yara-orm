import os
import tempfile

import pytest_asyncio

from yara_orm import YaraOrm

DB_URL = os.environ.get("ORM_TEST_DB", "postgres://localhost/orm_demo")


@pytest_asyncio.fixture
async def orm():
    """Initialise the ORM against PostgreSQL and tear it down per test."""
    await YaraOrm.init(DB_URL)
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
            if os.path.exists(path + suffix):
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

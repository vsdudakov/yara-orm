"""Password rotation on a live PostgreSQL pool, and connect-error text.

PostgreSQL checks the password only when a connection opens. ``set_password``
replaces the password the next new connection will present and leaves
connections already in the pool alone. A failed connect reports the server
message and SQLSTATE rather than the driver's ``db error``.
"""

import os
import socket
import urllib.parse

import pytest

from yara_orm import YaraOrm
from yara_orm._engine import connect as engine_connect
from yara_orm.exceptions import ConfigurationError, DBConnectionError

_PG_URL = os.environ.get("ORM_TEST_DB", "postgres://localhost/orm_demo")


def _pg_reachable() -> bool:
    """Return whether the configured PostgreSQL host accepts a TCP connection."""
    parsed = urllib.parse.urlsplit(_PG_URL)
    try:
        with socket.create_connection(
            (parsed.hostname or "localhost", parsed.port or 5432), timeout=1
        ):
            return True
    except OSError:
        return False


def _with_password(url: str, password: str) -> str:
    """Return ``url`` with its userinfo password replaced."""
    parsed = urllib.parse.urlsplit(url)
    user = urllib.parse.quote(parsed.username or "postgres", safe="")
    secret = urllib.parse.quote(password, safe="")
    host = parsed.hostname or "localhost"
    port = f":{parsed.port}" if parsed.port else ""
    netloc = f"{user}:{secret}@{host}{port}"
    return urllib.parse.urlunsplit(
        (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
    )


def _with_pool(url: str, *, max_size: int, min_size: int) -> str:
    """Return ``url`` with pool bounds added to the query string."""
    parsed = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    query["max_size"] = str(max_size)
    query["min_size"] = str(min_size)
    encoded = urllib.parse.urlencode(query)
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, encoded, parsed.fragment)
    )


@pytest.mark.asyncio
async def test_set_password_is_rejected_for_sqlite() -> None:
    """
    GIVEN a SQLite connection
    WHEN set_password is called
    THEN it raises ValueError, and an unknown connection name is rejected first
    """
    await YaraOrm.init("sqlite://:memory:")
    try:
        with pytest.raises(ConfigurationError, match="reader"):
            YaraOrm.set_password("secret", connection="reader")
        with pytest.raises(ValueError, match="PostgreSQL"):
            YaraOrm.set_password("secret")
        with pytest.raises(ValueError, match="PostgreSQL"):
            YaraOrm.set_password(lambda: "secret")
    finally:
        await YaraOrm.close()


@pytest.mark.asyncio
async def test_rejected_password_includes_the_sqlstate() -> None:
    """
    GIVEN a PostgreSQL URL whose password the server will refuse
    WHEN the engine connects
    THEN the error carries the server message and SQLSTATE 28P01
    """
    if not _pg_reachable() or urllib.parse.urlsplit(_PG_URL).password is None:
        pytest.skip("PostgreSQL with a password in ORM_TEST_DB is required")
    url = _with_password(_PG_URL, "not-the-password")
    with pytest.raises(
        DBConnectionError,
        match=r"password authentication failed for user .+ \(SQLSTATE 28P01\)",
    ):
        await engine_connect(url)


@pytest.mark.asyncio
async def test_set_password_applies_to_the_next_connection_only() -> None:
    """
    GIVEN a pool with one live connection
    WHEN the password is replaced, then restored
    THEN the connection already held keeps working, a new one uses the new
    password, and restoring the password lets the next connection in
    """
    parsed = urllib.parse.urlsplit(_PG_URL)
    password = urllib.parse.unquote(parsed.password) if parsed.password else None
    if not _pg_reachable() or password is None:
        pytest.skip("PostgreSQL with a password in ORM_TEST_DB is required")
    engine = await engine_connect(_with_pool(_PG_URL, max_size=2, min_size=1))
    try:
        assert (await engine.fetch_row("SELECT 1"))[0] == 1
        held = await engine.begin()
        try:
            engine.set_password("not-the-password")
            with pytest.raises(DBConnectionError, match="SQLSTATE 28P01"):
                await engine.fetch_row("SELECT 1")
            engine.set_password(password)
            assert (await engine.fetch_row("SELECT 1"))[0] == 1
        finally:
            await held.rollback()
        assert (await engine.fetch_row("SELECT 1"))[0] == 1
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_password_callable_runs_when_a_connection_opens() -> None:
    """
    GIVEN a pool with one live connection
    WHEN the password is a callable
    THEN the connection already held keeps working, and the callable supplies
    the password of the next connection the pool opens
    """
    parsed = urllib.parse.urlsplit(_PG_URL)
    password = urllib.parse.unquote(parsed.password) if parsed.password else None
    if not _pg_reachable() or password is None:
        pytest.skip("PostgreSQL with a password in ORM_TEST_DB is required")
    engine = await engine_connect(_with_pool(_PG_URL, max_size=2, min_size=1))
    calls = {"n": 0}

    def provider() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            return "not-the-password"
        return password

    try:
        assert (await engine.fetch_row("SELECT 1"))[0] == 1
        held = await engine.begin()
        try:
            engine.set_password(provider)
            assert calls["n"] == 0
            with pytest.raises(DBConnectionError, match="SQLSTATE 28P01"):
                await engine.fetch_row("SELECT 1")
            assert calls["n"] == 1
            assert (await engine.fetch_row("SELECT 1"))[0] == 1
            assert calls["n"] == 2
        finally:
            await held.rollback()
        assert (await engine.fetch_row("SELECT 1"))[0] == 1
    finally:
        await engine.close()

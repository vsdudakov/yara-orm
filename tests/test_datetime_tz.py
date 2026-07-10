"""Timezone-aware datetime handling.

Covers a previously-broken case: binding a tz-aware datetime with a non-UTC
offset raised ``expected a datetime without tzinfo``. tz-aware values are now
normalised to UTC on the way in and read back as UTC-aware datetimes, so the
instant is preserved across a round-trip. Runs on every configured backend.

Also covers the variable-offset ``zoneinfo.ZoneInfo`` shape (e.g.
``Europe/Berlin``) that ``use_tz=True`` produces for ``auto_now_add`` /
``auto_now``, which previously raised the same bind ``TypeError``.
"""

import contextlib
import datetime as dt
import os
import tempfile
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from yara_orm import Model, YaraOrm, fields
from yara_orm import timezone as tz

UTC = dt.timezone.utc
PLUS5 = dt.timezone(dt.timedelta(hours=5))
MINUS8 = dt.timezone(dt.timedelta(hours=-8))
BERLIN = ZoneInfo("Europe/Berlin")


class TzEvent(Model):
    id = fields.IntField(pk=True)
    naive = fields.DatetimeField(null=True)
    aware = fields.DatetimeField(null=True)

    class Meta:
        table = "tz_event"


MODELS = [TzEvent]


async def _roundtrip(value: dt.datetime, field: str = "aware") -> dt.datetime:
    """Create a row holding ``value`` and read the column back.

    Args:
        value: The datetime to persist.
        field: The column to write and read (``aware`` or ``naive``).

    Returns:
        The value as returned by the database.
    """
    row = await TzEvent.create(**{field: value})
    return getattr(await TzEvent.get(id=row.id), field)


@pytest.mark.asyncio
async def test_aware_offset_roundtrip(db):
    """
    GIVEN a tz-aware datetime with a +05:00 offset
    WHEN it is written and re-read
    THEN it round-trips to the same instant as an aware value (regression);
    MySQL's DATETIME has no timezone, so there the UTC instant returns naive
    (aware under ``use_tz``)
    """
    value = dt.datetime(2021, 6, 15, 12, 30, 45, 123456, tzinfo=PLUS5)
    out = await _roundtrip(value)
    if db in ("mysql", "mariadb", "oracle", "mssql"):
        assert out.tzinfo is None
        assert out == value.astimezone(UTC).replace(tzinfo=None)
    else:
        assert out.tzinfo is not None
        assert out == value  # same instant, even though stored/returned as UTC


@pytest.mark.asyncio
@pytest.mark.parametrize("tz", [UTC, PLUS5, MINUS8])
async def test_various_offsets_same_instant(db, tz):
    """
    GIVEN the same instant expressed in different timezone offsets
    WHEN each value is written and re-read
    THEN it stores identically and is surfaced as the same UTC instant
    """
    value = dt.datetime(2022, 1, 1, 0, 0, 0, tzinfo=tz)
    out = await _roundtrip(value)
    if db in ("mysql", "mariadb", "oracle", "mssql"):
        # DATETIME/TIMESTAMP is naive: every offset stores as the same UTC instant.
        assert out == value.astimezone(UTC).replace(tzinfo=None)
    else:
        assert out == value
        assert out.utcoffset() == dt.timedelta(0)  # surfaced in UTC


@pytest.mark.asyncio
async def test_naive_datetime_roundtrip(db):
    """
    GIVEN a naive datetime
    WHEN it is written and re-read
    THEN its wall-clock value is preserved (returned naive on SQLite, aware on
    PostgreSQL's TIMESTAMPTZ column)
    """
    value = dt.datetime(2021, 6, 15, 12, 30, 45, 123456)
    out = await _roundtrip(value, field="naive")
    if db == "sqlite":
        assert out.tzinfo is None
        assert out == value
    else:
        assert out.replace(tzinfo=None) == value


@pytest.mark.asyncio
async def test_microsecond_precision_preserved(db):
    """
    GIVEN a tz-aware datetime carrying microseconds
    WHEN it is written and re-read
    THEN the microseconds survive the round-trip without truncation to seconds
    """
    value = dt.datetime(2021, 6, 15, 12, 30, 45, 654321, tzinfo=UTC)
    out = await _roundtrip(value)
    assert out.microsecond == 654321


# ---------------------------------------------------------------------------
# use_tz=True with a variable-offset ZoneInfo timezone (Europe/Berlin)
# ---------------------------------------------------------------------------
class RfaEvent(Model):
    created = fields.DatetimeField(auto_now_add=True)
    at = fields.DatetimeField(null=True)

    class Meta:
        table = "rfa_event"


@pytest_asyncio.fixture
async def berlin_orm():
    """Fresh temporary SQLite database with ``use_tz=True`` in Europe/Berlin."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    await YaraOrm.init(f"sqlite://{path}", use_tz=True, timezone="Europe/Berlin")
    await YaraOrm.generate_schemas(models=[RfaEvent])
    try:
        yield
    finally:
        await YaraOrm.close()
        # init() set process-wide tz config; restore the defaults so later
        # test modules are unaffected.
        tz._set_config(timezone="UTC", use_tz=False)
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(FileNotFoundError):
                os.remove(path + suffix)


@pytest.mark.asyncio
async def test_auto_now_add_with_zoneinfo_timezone(berlin_orm):
    """
    GIVEN use_tz=True with a variable-offset zone (Europe/Berlin has DST, so
        its ZoneInfo has no datetime-independent utcoffset)
    WHEN a row with auto_now_add is created (binds timezone.now(), a
        ZoneInfo-aware datetime)
    THEN the bind succeeds (regression: TypeError "expected a datetime without
        tzinfo") and the stored value round-trips to the same instant, aware
    """
    row = await RfaEvent.create()
    fetched = await RfaEvent.get(id=row.id)
    assert fetched.created.utcoffset() is not None
    assert fetched.created == row.created  # same instant, tz-independent


@pytest.mark.asyncio
async def test_zoneinfo_aware_datetime_roundtrip_and_filter(berlin_orm):
    """
    GIVEN explicit ZoneInfo-aware datetimes in both DST (+02:00) and standard
        (+01:00) Berlin offsets
    WHEN they are written, read back and used as filter values
    THEN each round-trips to its exact UTC instant and filters match
    """
    summer = dt.datetime(2024, 7, 1, 12, 30, 45, 123456, tzinfo=BERLIN)  # +02:00
    winter = dt.datetime(2024, 1, 15, 12, 30, 45, 123456, tzinfo=BERLIN)  # +01:00
    row_summer = await RfaEvent.create(at=summer)
    row_winter = await RfaEvent.create(at=winter)

    got_summer = (await RfaEvent.get(id=row_summer.id)).at
    got_winter = (await RfaEvent.get(id=row_winter.id)).at
    # The per-instant offset was resolved correctly (value, not just no-error).
    assert got_summer.astimezone(UTC) == dt.datetime(2024, 7, 1, 10, 30, 45, 123456, tzinfo=UTC)
    assert got_winter.astimezone(UTC) == dt.datetime(2024, 1, 15, 11, 30, 45, 123456, tzinfo=UTC)

    # Filtering binds the ZoneInfo-aware datetime as a parameter.
    assert await RfaEvent.filter(at=summer).count() == 1
    assert await RfaEvent.filter(at__lt=summer).count() == 1  # only the winter row
    assert await RfaEvent.filter(at__lte=summer).count() == 2

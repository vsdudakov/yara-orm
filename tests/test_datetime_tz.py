"""Timezone-aware datetime handling.

Covers a previously-broken case: binding a tz-aware datetime with a non-UTC
offset raised ``expected a datetime without tzinfo``. tz-aware values are now
normalised to UTC on the way in and read back as UTC-aware datetimes, so the
instant is preserved across a round-trip on both backends.
"""

import datetime as dt

import pytest

from yara_orm import Model, fields
from yara_orm.connection import get_engine

UTC = dt.timezone.utc
PLUS5 = dt.timezone(dt.timedelta(hours=5))
MINUS8 = dt.timezone(dt.timedelta(hours=-8))


class TzEvent(Model):
    id = fields.IntField(pk=True)
    naive = fields.DatetimeField(null=True)
    aware = fields.DatetimeField(null=True)

    class Meta:
        table = "tz_event"


async def _reset_pg():
    from yara_orm import YaraOrm

    await get_engine().execute("DROP TABLE IF EXISTS tz_event CASCADE")
    await YaraOrm.generate_schemas()


async def _roundtrip(value, field="aware"):
    row = await TzEvent.create(**{field: value})
    return getattr(await TzEvent.get(id=row.id), field)


# -- tz-aware: instant preserved -------------------------------------------
@pytest.mark.asyncio
async def test_aware_offset_roundtrip_sqlite(sqlite_db):
    """A +05:00 datetime round-trips to the same instant (regression)."""
    value = dt.datetime(2021, 6, 15, 12, 30, 45, 123456, tzinfo=PLUS5)
    out = await _roundtrip(value)
    assert out.tzinfo is not None
    assert out == value  # same instant, even though stored/returned as UTC


@pytest.mark.asyncio
async def test_aware_offset_roundtrip_postgres(orm):
    """A +05:00 datetime round-trips to the same instant on PostgreSQL."""
    await _reset_pg()
    value = dt.datetime(2021, 6, 15, 12, 30, 45, 123456, tzinfo=PLUS5)
    out = await _roundtrip(value)
    assert out.tzinfo is not None
    assert out == value


@pytest.mark.asyncio
@pytest.mark.parametrize("tz", [UTC, PLUS5, MINUS8])
async def test_various_offsets_same_instant_sqlite(sqlite_db, tz):
    """Equivalent instants in different zones store identically."""
    value = dt.datetime(2022, 1, 1, 0, 0, 0, tzinfo=tz)
    out = await _roundtrip(value)
    assert out == value
    assert out.utcoffset() == dt.timedelta(0)  # surfaced in UTC


@pytest.mark.asyncio
async def test_naive_datetime_roundtrip_sqlite(sqlite_db):
    """A naive datetime keeps its wall-clock value (returned naive on SQLite)."""
    value = dt.datetime(2021, 6, 15, 12, 30, 45, 123456)
    out = await _roundtrip(value, field="naive")
    assert out.tzinfo is None
    assert out == value


@pytest.mark.asyncio
async def test_naive_datetime_roundtrip_postgres(orm):
    """A naive datetime preserves its instant on PostgreSQL (TIMESTAMPTZ)."""
    await _reset_pg()
    value = dt.datetime(2021, 6, 15, 12, 30, 45, 123456)
    out = await _roundtrip(value, field="naive")
    # TIMESTAMPTZ returns an aware value; the wall-clock fields are preserved.
    assert out.replace(tzinfo=None) == value


@pytest.mark.asyncio
async def test_microsecond_precision_preserved_sqlite(sqlite_db):
    """Microseconds survive the round-trip (no truncation to seconds)."""
    value = dt.datetime(2021, 6, 15, 12, 30, 45, 654321, tzinfo=UTC)
    out = await _roundtrip(value)
    assert out.microsecond == 654321

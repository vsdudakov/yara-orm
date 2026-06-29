"""Timezone-aware datetime handling.

Covers a previously-broken case: binding a tz-aware datetime with a non-UTC
offset raised ``expected a datetime without tzinfo``. tz-aware values are now
normalised to UTC on the way in and read back as UTC-aware datetimes, so the
instant is preserved across a round-trip. Runs on every configured backend.
"""

import datetime as dt

import pytest

from yara_orm import Model, fields

UTC = dt.timezone.utc
PLUS5 = dt.timezone(dt.timedelta(hours=5))
MINUS8 = dt.timezone(dt.timedelta(hours=-8))


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
    THEN it round-trips to the same instant as an aware value (regression)
    """
    value = dt.datetime(2021, 6, 15, 12, 30, 45, 123456, tzinfo=PLUS5)
    out = await _roundtrip(value)
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

"""Corner-case coverage for the temporal fields.

DatetimeField (``auto_now`` / ``auto_now_add``, ISO-string parsing incl. a
trailing ``Z``, microseconds, tz-aware vs naive under ``use_tz``), DateField
(from a datetime string), TimeField, date-part lookups (``__year`` ...
``__microsecond``), the ``__date`` truncation lookup, range and ordering, and
TimeDeltaField round-trips (including microseconds and negative durations).
"""

import datetime as dt
import os

import pytest
import pytest_asyncio

from yara_orm import Model, YaraOrm, fields
from yara_orm import timezone as tz
from yara_orm.exceptions import UnSupportedError

UTC = dt.timezone.utc


class JdStamp(Model):
    id = fields.IntField(pk=True)
    label = fields.CharField(max_length=20, null=True)
    created = fields.DatetimeField(auto_now_add=True)
    updated = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "jd_stamp"


class JdMoment(Model):
    id = fields.IntField(pk=True)
    at = fields.DatetimeField(null=True)
    day = fields.DateField(null=True)
    clock = fields.TimeField(null=True)
    span = fields.TimeDeltaField(null=True)

    class Meta:
        table = "jd_moment"


class RfeEvent(Model):
    id = fields.IntField(pk=True)
    at = fields.DatetimeField(null=True)
    on_day = fields.DateField(null=True)

    class Meta:
        table = "rfe_event"


MODELS = [JdStamp, JdMoment, RfeEvent]


# ---------------------------------------------------------------------------
# auto_now / auto_now_add
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_auto_now_add_set_on_create_only(db):
    """
    GIVEN a model with auto_now_add and auto_now columns
    WHEN a row is created
    THEN both timestamps are populated on insert
    """
    row = await JdStamp.create(label="x")
    assert row.created is not None
    assert row.updated is not None


@pytest.mark.asyncio
async def test_auto_now_add_frozen_auto_now_bumps_on_update(db):
    """
    GIVEN a persisted row with auto_now_add + auto_now columns
    WHEN it is saved again as an update
    THEN auto_now_add stays fixed while auto_now advances
    """
    row = await JdStamp.create(label="x")
    created0, updated0 = row.created, row.updated
    # Ensure the wall clock advances enough to see a difference.
    await _sleep_tick()
    row.label = "y"
    await row.save()

    assert row.created == created0
    assert row.updated >= updated0
    reread = await JdStamp.get(id=row.id)
    # PG returns a TIMESTAMPTZ (aware); the in-memory value is naive when
    # use_tz is off, so compare wall-clock to stay backend-agnostic.
    assert _naive(reread.created) == _naive(created0)


@pytest.mark.asyncio
async def test_auto_now_not_bumped_when_not_in_update_fields(db):
    """
    GIVEN a persisted row
    WHEN it is saved with update_fields that excludes the auto_now column
    THEN the auto_now column is not bumped (only listed fields persist)
    """
    row = await JdStamp.create(label="x")
    updated0 = row.updated
    await _sleep_tick()
    row.label = "y"
    await row.save(update_fields=["label"])
    assert row.updated == updated0


# ---------------------------------------------------------------------------
# ISO-string parsing on assignment
# ---------------------------------------------------------------------------
def test_datetime_iso_string_assignment_coerces():
    """
    GIVEN a DatetimeField
    WHEN an ISO-8601 string (with and without a trailing Z) is assigned
    THEN it is coerced to a datetime, the Z resolving to a UTC offset
    """
    f = fields.DatetimeField()
    naive = f.to_python_value("2021-06-15T12:30:45.123456")
    assert naive == dt.datetime(2021, 6, 15, 12, 30, 45, 123456)

    zulu = f.to_python_value("2021-06-15T12:30:45Z")
    assert zulu.tzinfo is not None
    assert zulu.utcoffset() == dt.timedelta(0)


def test_datefield_from_datetime_string_narrows_to_date():
    """
    GIVEN a DateField
    WHEN a full timestamp string or a datetime is assigned
    THEN it is narrowed to the date part
    """
    f = fields.DateField()
    assert f.to_python_value("2026-07-01T10:20:30") == dt.date(2026, 7, 1)
    assert f.to_python_value("2026-07-01 10:20:30") == dt.date(2026, 7, 1)
    assert f.to_python_value("2026-07-01") == dt.date(2026, 7, 1)
    assert f.to_python_value(dt.datetime(2026, 7, 1, 5, 0, 0)) == dt.date(2026, 7, 1)


def test_timefield_from_string_and_datetime():
    """
    GIVEN a TimeField
    WHEN a time string or a datetime is assigned
    THEN it is coerced to a time-of-day
    """
    f = fields.TimeField()
    assert f.to_python_value("13:45:07.000500") == dt.time(13, 45, 7, 500)
    assert f.to_python_value(dt.datetime(2026, 7, 1, 8, 9, 10)) == dt.time(8, 9, 10)


@pytest.mark.asyncio
async def test_datetime_iso_string_create_roundtrip(db):
    """
    GIVEN an ISO string assigned to a datetime column at create time
    WHEN the row is persisted and read back
    THEN the value stored is the parsed datetime, not raw text
    """
    row = await JdMoment.create(at="2021-06-15T12:30:45")
    stored = (await JdMoment.get(id=row.id)).at
    assert stored.replace(tzinfo=None) == dt.datetime(2021, 6, 15, 12, 30, 45)


# ---------------------------------------------------------------------------
# microseconds
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_datetime_microseconds_preserved(db):
    """
    GIVEN a datetime carrying microseconds
    WHEN stored and read back
    THEN the microsecond component survives without truncation
    """
    value = dt.datetime(2022, 3, 4, 5, 6, 7, 654321)
    row = await JdMoment.create(at=value)
    stored = (await JdMoment.get(id=row.id)).at
    assert stored.microsecond == 654321


@pytest.mark.asyncio
async def test_time_and_date_roundtrip(db):
    """
    GIVEN a bare date and a bare time value
    WHEN stored in DateField / TimeField and read back
    THEN each round-trips to the same value
    """
    row = await JdMoment.create(day=dt.date(2026, 7, 1), clock=dt.time(23, 59, 58))
    got = await JdMoment.get(id=row.id)
    assert got.day == dt.date(2026, 7, 1)
    assert got.clock.hour == 23 and got.clock.minute == 59 and got.clock.second == 58


# ---------------------------------------------------------------------------
# date-part lookups
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_date_part_year_month_day(db):
    """
    GIVEN rows on distinct calendar dates
    WHEN filtered by __year / __month / __day parts
    THEN each part lookup selects the matching rows
    """
    await JdMoment.create(at=dt.datetime(2020, 1, 15, 10, 0, 0))
    await JdMoment.create(at=dt.datetime(2021, 6, 30, 10, 0, 0))

    assert await JdMoment.filter(at__year=2021).count() == 1
    assert await JdMoment.filter(at__month=1).count() == 1
    assert await JdMoment.filter(at__day=30).count() == 1


@pytest.mark.asyncio
async def test_date_part_hour_minute_second(db):
    """
    GIVEN a stored wall-clock time (session tz is UTC on both backends)
    WHEN filtered by __hour / __minute / __second
    THEN the time parts match the stored value
    """
    await JdMoment.create(at=dt.datetime(2022, 5, 5, 14, 37, 21))

    assert await JdMoment.filter(at__hour=14).count() == 1
    assert await JdMoment.filter(at__minute=37).count() == 1
    assert await JdMoment.filter(at__second=21).count() == 1


@pytest.mark.asyncio
async def test_date_part_quarter(db):
    """
    GIVEN rows in different quarters
    WHEN filtered by __quarter (derived from month on SQLite)
    THEN the quarter lookup selects the right rows
    """
    await JdMoment.create(at=dt.datetime(2022, 2, 1, 0, 0, 0))  # Q1
    await JdMoment.create(at=dt.datetime(2022, 11, 1, 0, 0, 0))  # Q4

    assert await JdMoment.filter(at__quarter=1).count() == 1
    assert await JdMoment.filter(at__quarter=4).count() == 1


@pytest.mark.asyncio
async def test_microsecond_part_unsupported_on_sqlite(db):
    """
    GIVEN the __microsecond date-part lookup
    WHEN compiled on SQLite (which cannot extract it reliably)
    THEN it raises UnSupportedError; on PostgreSQL it filters normally
    """
    await JdMoment.create(at=dt.datetime(2022, 1, 1, 0, 0, 0, 123456))
    if db == "sqlite":
        with pytest.raises(UnSupportedError):
            await JdMoment.filter(at__microsecond=123456).count()
    else:
        assert await JdMoment.filter(at__microsecond=123456).count() == 1


@pytest.mark.asyncio
async def test_date_truncation_lookup(db):
    """
    GIVEN datetimes on the same calendar day but different times
    WHEN filtered with the ``__date`` truncation lookup
    THEN all rows sharing that date match regardless of time
    """
    await JdMoment.create(at=dt.datetime(2023, 8, 9, 1, 2, 3))
    await JdMoment.create(at=dt.datetime(2023, 8, 9, 23, 59, 59))
    await JdMoment.create(at=dt.datetime(2023, 8, 10, 0, 0, 0))

    assert await JdMoment.filter(at__date=dt.date(2023, 8, 9)).count() == 2


# ---------------------------------------------------------------------------
# range and ordering
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_datetime_range_filter(db):
    """
    GIVEN datetimes across a span
    WHEN filtered with __range (inclusive BETWEEN)
    THEN only rows within the bounds return
    """
    await JdMoment.create(at=dt.datetime(2020, 1, 1, 0, 0, 0))
    await JdMoment.create(at=dt.datetime(2020, 6, 1, 0, 0, 0))
    await JdMoment.create(at=dt.datetime(2021, 1, 1, 0, 0, 0))

    rows = await JdMoment.filter(at__range=(dt.datetime(2020, 3, 1), dt.datetime(2020, 12, 31)))
    assert len(rows) == 1
    assert rows[0].at.month == 6


@pytest.mark.asyncio
async def test_date_range_and_ordering(db):
    """
    GIVEN several DateField rows
    WHEN ordered ascending and descending
    THEN ordering follows chronological order
    """
    for d in (dt.date(2021, 5, 1), dt.date(2020, 1, 1), dt.date(2022, 9, 9)):
        await JdMoment.create(day=d)

    asc = [m.day for m in await JdMoment.all().order_by("day")]
    desc = [m.day for m in await JdMoment.all().order_by("-day")]
    assert asc == sorted(asc)
    assert desc == sorted(asc, reverse=True)


# ---------------------------------------------------------------------------
# TimeDeltaField
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_timedelta_roundtrip_with_microseconds(db):
    """
    GIVEN a timedelta with days, seconds and microseconds
    WHEN stored (as total microseconds) and read back
    THEN it round-trips to an equal timedelta
    """
    value = dt.timedelta(days=3, hours=4, minutes=5, seconds=6, microseconds=789012)
    row = await JdMoment.create(span=value)
    assert (await JdMoment.get(id=row.id)).span == value


@pytest.mark.asyncio
async def test_timedelta_negative(db):
    """
    GIVEN a negative timedelta
    WHEN stored and read back
    THEN the sign and magnitude are preserved
    """
    value = dt.timedelta(seconds=-90, microseconds=-5)
    row = await JdMoment.create(span=value)
    assert (await JdMoment.get(id=row.id)).span == value


@pytest.mark.asyncio
async def test_timedelta_zero_and_null(db):
    """
    GIVEN a zero timedelta and a NULL span
    WHEN stored and read back
    THEN zero stays a zero timedelta and NULL stays None
    """
    z = await JdMoment.create(span=dt.timedelta(0))
    n = await JdMoment.create(span=None)
    assert (await JdMoment.get(id=z.id)).span == dt.timedelta(0)
    assert (await JdMoment.get(id=n.id)).span is None


# ---------------------------------------------------------------------------
# tz-aware vs naive under use_tz
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def sqlite_use_tz():
    """A SQLite session with ``use_tz`` enabled and the temporal schema."""
    path = "/tmp/jd_use_tz.db"
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    await YaraOrm.init(f"sqlite://{path}", use_tz=True)
    await YaraOrm.generate_schemas(models=[JdStamp, JdMoment])
    try:
        yield
    finally:
        await YaraOrm.close()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(path + suffix):
                os.remove(path + suffix)


@pytest.mark.asyncio
async def test_auto_now_aware_under_use_tz(sqlite_use_tz):
    """
    GIVEN use_tz enabled
    WHEN a row with auto_now columns is created
    THEN the stamped columns are timezone-aware (no naive/aware mix)
    """
    row = await JdStamp.create(label="tz")
    assert row.created.tzinfo is not None
    assert row.updated.tzinfo is not None


@pytest.mark.asyncio
async def test_aware_datetime_roundtrip_under_use_tz(sqlite_use_tz):
    """
    GIVEN use_tz enabled and a tz-aware datetime with a non-UTC offset
    WHEN stored and read back
    THEN it surfaces as the same UTC instant, aware
    """
    value = dt.datetime(2021, 6, 15, 12, 30, 45, tzinfo=dt.timezone(dt.timedelta(hours=5)))
    row = await JdMoment.create(at=value)
    stored = (await JdMoment.get(id=row.id)).at
    assert stored.tzinfo is not None
    assert stored == value


def _naive(value: dt.datetime) -> dt.datetime:
    """Strip tzinfo so naive (SQLite/use_tz off) and aware (PG) values compare."""
    return value.replace(tzinfo=None)


async def _sleep_tick() -> None:
    """Yield long enough that the wall clock advances past microsecond ties."""
    import asyncio

    await asyncio.sleep(0.005)


# ---------------------------------------------------------------------------
# DatetimeField widens a bare date to a midnight datetime
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_datetime_field_bare_date_roundtrips_as_midnight(db):
    """
    GIVEN a DatetimeField assigned a bare ``date``
    WHEN the row is created and re-read
    THEN the column round-trips as a ``datetime`` at midnight (not a string:
    binding the date as-is stores date-only text on SQLite, which the engine's
    datetime decoder cannot parse back)
    """
    row = await RfeEvent.create(at=dt.date(2024, 5, 3))
    fresh = await RfeEvent.get(id=row.id)
    assert isinstance(fresh.at, dt.datetime)
    if db == "sqlite":
        assert fresh.at == dt.datetime(2024, 5, 3, 0, 0)
        assert fresh.at.tzinfo is None
    else:
        assert fresh.at.replace(tzinfo=None) == dt.datetime(2024, 5, 3, 0, 0)


@pytest.mark.asyncio
async def test_datetime_field_bare_date_under_use_tz_localises(db):
    """
    GIVEN ``use_tz`` enabled (default timezone UTC)
    WHEN a bare ``date`` is written to a DatetimeField and re-read
    THEN it stores as the aware midnight instant, matching what ``timezone.now``
    produces for other datetimes under ``use_tz``
    """
    tz._set_config(use_tz=True)
    try:
        row = await RfeEvent.create(at=dt.date(2024, 6, 1))
        fresh = await RfeEvent.get(id=row.id)
    finally:
        tz._set_config(use_tz=False)
    expected = dt.datetime(2024, 6, 1, tzinfo=UTC)
    if db in ("mysql", "mariadb", "oracle", "mssql"):
        # DATETIME/TIMESTAMP is naive on these backends: same UTC instant, no tz.
        assert fresh.at == expected.replace(tzinfo=None)
    else:
        assert fresh.at == expected
        assert fresh.at.tzinfo is not None


@pytest.mark.asyncio
async def test_datetime_range_filters_accept_bare_dates(db):
    """
    GIVEN a same-day datetime row and a row created from a bare date
    WHEN filtering with ``__gte``/``__lt`` bounds given as bare dates
    THEN both rows match (pre-fix, SQLite compared 'YYYY-MM-DD' text against
    'YYYY-MM-DD HH:MM:SS' text and dropped rows from same-day ranges)
    """
    a = await RfeEvent.create(at=dt.date(2024, 5, 3))
    b = await RfeEvent.create(at=dt.datetime(2024, 5, 3, 10, 30))

    got = {e.id for e in await RfeEvent.filter(at__gte=dt.date(2024, 5, 3))}
    assert {a.id, b.id} <= got
    got = {e.id for e in await RfeEvent.filter(at__lt=dt.date(2024, 5, 4))}
    assert {a.id, b.id} <= got
    # The midnight row also matches a datetime lower bound at midnight.
    got = {e.id for e in await RfeEvent.filter(at__gte=dt.datetime(2024, 5, 3, 0, 0))}
    assert {a.id, b.id} <= got
    # And ordering by the column keeps the bare-date row first (midnight < 10:30).
    ordered = [e.id for e in await RfeEvent.filter(at__lt=dt.date(2024, 5, 4)).order_by("at")]
    assert ordered.index(a.id) < ordered.index(b.id)


@pytest.mark.asyncio
async def test_date_field_still_stores_bare_dates(db):
    """
    GIVEN a DateField (the date-only column)
    WHEN a bare ``date`` is written and re-read
    THEN it round-trips as a ``date`` — the datetime widening does not leak in
    """
    row = await RfeEvent.create(on_day=dt.date(2024, 5, 3))
    fresh = await RfeEvent.get(id=row.id)
    assert fresh.on_day == dt.date(2024, 5, 3)
    assert not isinstance(fresh.on_day, dt.datetime)

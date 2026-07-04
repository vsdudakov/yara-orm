"""Audit regression tests: value encoding/decoding across backends.

Covers: the canonical SQLite datetime text format (aware values share the
naive layout so lexicographic comparisons order mixed rows correctly, while
old RFC 3339 rows keep decoding), bytes inside bound arrays on SQLite,
unsupported PostgreSQL OIDs raising a clear error instead of silently
decoding to None, sqlite URL query parameters, raw-SQL list->array binding on
``fetch_one``, and concurrent read-then-write sqlite transactions (BEGIN
IMMEDIATE + busy_timeout).
"""

import asyncio
import base64
import datetime as dt
import json

import pytest

from yara_orm import Model, YaraOrm, connections, fields, in_transaction
from yara_orm.exceptions import OperationalError
from yara_orm.expressions import Array

UTC = dt.timezone.utc
PLUS5 = dt.timezone(dt.timedelta(hours=5))


class AvStamp(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20, null=True)
    at = fields.DatetimeField(null=True)

    class Meta:
        table = "av_stamp"


MODELS = [AvStamp]


# ---------------------------------------------------------------------------
# Finding 3: one canonical datetime layout on SQLite
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aware_datetime_roundtrips_aware(db):
    """
    GIVEN a tz-aware datetime with a non-UTC offset
    WHEN it is written and re-read
    THEN it comes back aware, at the same instant (surfaced in UTC)
    """
    value = dt.datetime(2021, 6, 15, 12, 30, 45, 123456, tzinfo=PLUS5)
    row = await AvStamp.create(name="rt", at=value)
    out = (await AvStamp.get(id=row.id)).at
    if db in ("mysql", "mariadb"):
        # DATETIME has no timezone: the UTC instant returns naive (aware
        # under ``use_tz``).
        assert out.tzinfo is None
        assert out == value.astimezone(dt.timezone.utc).replace(tzinfo=None)
    else:
        assert out.tzinfo is not None
        assert out == value


@pytest.mark.asyncio
async def test_mixed_naive_and_aware_rows_order_chronologically(db):
    """
    GIVEN naive and aware rows interleaved on the same day
    WHEN ordered by the datetime column
    THEN the order is chronological (aware rows no longer sort after every
         naive row of the same day due to the old 'T'-separator format)
    """
    values = [
        ("n1", dt.datetime(2021, 6, 15, 6, 0, 0)),
        ("a2", dt.datetime(2021, 6, 15, 8, 0, 0, tzinfo=UTC)),
        ("n3", dt.datetime(2021, 6, 15, 10, 0, 0)),
        ("a4", dt.datetime(2021, 6, 15, 12, 0, 0, tzinfo=UTC)),
    ]
    # Insert shuffled so ordering can't come from insertion order.
    for name, at in [values[2], values[0], values[3], values[1]]:
        await AvStamp.create(name=name, at=at)

    rows = await AvStamp.all().order_by("at")
    assert [r.name for r in rows] == ["n1", "a2", "n3", "a4"]


@pytest.mark.asyncio
async def test_aware_write_filtered_by_naive_bound(db):
    """
    GIVEN rows written with aware datetimes
    WHEN filtered with a naive (UTC wall-clock) bound
    THEN the comparison is correct across the two forms
    """
    await AvStamp.create(name="early", at=dt.datetime(2021, 6, 15, 6, 0, tzinfo=UTC))
    await AvStamp.create(name="late", at=dt.datetime(2021, 6, 15, 12, 0, tzinfo=UTC))

    rows = await AvStamp.filter(at__gt=dt.datetime(2021, 6, 15, 9, 0))
    assert [r.name for r in rows] == ["late"]
    rows = await AvStamp.filter(at__lt=dt.datetime(2021, 6, 15, 9, 0))
    assert [r.name for r in rows] == ["early"]


@pytest.mark.asyncio
async def test_old_rfc3339_sqlite_rows_still_decode_aware(db):
    """
    GIVEN a row stored in the old RFC 3339 text format ('T' + offset)
    WHEN it is read back on SQLite
    THEN it still decodes to the same aware UTC instant (back-compat)
    """
    if db != "sqlite":
        pytest.skip("exercises the SQLite text-storage back-compat path")
    conn = connections.get()
    await conn.execute(
        "INSERT INTO av_stamp (name, at) VALUES ($1, $2)",
        ["old", "2021-06-15T12:30:45.123456+05:00"],
    )
    out = (await AvStamp.get(name="old")).at
    assert out == dt.datetime(2021, 6, 15, 12, 30, 45, 123456, tzinfo=PLUS5)
    assert out.utcoffset() == dt.timedelta(0)  # surfaced in UTC


@pytest.mark.asyncio
async def test_sqlite_stores_aware_in_space_separated_utc_text(db):
    """
    GIVEN an aware non-UTC datetime written on SQLite
    WHEN the raw stored text is inspected
    THEN it uses the canonical space-separated UTC layout (naive-format prefix)
    """
    if db != "sqlite":
        pytest.skip("inspects SQLite's raw text storage")
    await AvStamp.create(name="fmt", at=dt.datetime(2021, 6, 15, 12, 30, 45, 123456, tzinfo=PLUS5))
    raw = (
        await connections.get().fetch_rows(
            "SELECT CAST(at AS TEXT) FROM av_stamp WHERE name = $1", ["fmt"]
        )
    )[0][0]
    assert raw == "2021-06-15 07:30:45.123456+00:00"


# ---------------------------------------------------------------------------
# Finding 6: bytes inside a bound Array on SQLite
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bytes_in_bound_array_encode_base64_on_sqlite(db):
    """
    GIVEN an Array parameter containing bytes bound on SQLite
    WHEN the stored JSON text is read back
    THEN the bytes are base64-encoded (matching py_to_json), not null
    """
    if db != "sqlite":
        pytest.skip("SQLite stores bound arrays as JSON text")
    conn = connections.get()
    rows = await conn.fetch_all("SELECT $1 AS v", [Array([b"hi", b"yo"])])
    assert json.loads(rows[0]["v"]) == [
        base64.b64encode(b"hi").decode(),
        base64.b64encode(b"yo").decode(),
    ]


# ---------------------------------------------------------------------------
# Finding 7: unsupported PostgreSQL OIDs raise instead of decoding to None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unsupported_pg_oid_raises_clear_error(db):
    """
    GIVEN a query returning a type the engine cannot decode (interval)
    WHEN the row is fetched
    THEN a clear OperationalError names the OID and column (and suggests a
         text cast), while a genuine NULL still decodes to None
    """
    if db != "postgres":
        pytest.skip("exercises PostgreSQL OID decoding")
    conn = connections.get()

    with pytest.raises(OperationalError) as excinfo:
        await conn.fetch_all("SELECT interval '1 day' AS iv")
    message = str(excinfo.value)
    assert "OID" in message and "iv" in message and "text" in message

    # A SQL NULL of the same unsupported type still decodes as None.
    rows = await conn.fetch_all("SELECT NULL::interval AS iv")
    assert rows[0]["iv"] is None

    # The suggested workaround (cast to text) works.
    rows = await conn.fetch_all("SELECT (interval '1 day')::text AS iv")
    assert rows[0]["iv"] == "1 day"


# ---------------------------------------------------------------------------
# Finding 15: sqlite URL query parameters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sqlite_url_unknown_param_rejected(tmp_path):
    """
    GIVEN a sqlite URL carrying an unsupported query parameter
    WHEN the ORM connects
    THEN it raises instead of opening a literal 'file.db?param' path
    """
    with pytest.raises(ValueError):
        await YaraOrm.init(f"sqlite://{tmp_path}/x.db?cache=shared")
    await YaraOrm.close()
    assert not (tmp_path / "x.db?cache=shared").exists()


@pytest.mark.asyncio
async def test_sqlite_url_mode_memory(tmp_path):
    """
    GIVEN a sqlite URL with mode=memory
    WHEN the ORM connects and runs statements
    THEN an in-memory database is used and no file is created
    """
    await YaraOrm.init(f"sqlite://{tmp_path}/x.db?mode=memory")
    try:
        conn = connections.get()
        await conn.execute("CREATE TABLE av_mem (id INTEGER)")
        await conn.execute("INSERT INTO av_mem VALUES (1)")
        assert (await conn.fetch_rows("SELECT count(*) FROM av_mem"))[0][0] == 1
        assert not (tmp_path / "x.db").exists()
    finally:
        await YaraOrm.close()


# ---------------------------------------------------------------------------
# Finding 11: raw-SQL list parameters bind as arrays on fetch_one too
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_one_binds_bare_list_as_pg_array(db):
    """
    GIVEN a bare list parameter in a raw query using an array cast
    WHEN fetched via fetch_one (previously only fetch_all arrayified)
    THEN the list binds as a PostgreSQL array, matching fetch_all
    """
    if db != "postgres":
        pytest.skip("array binding is PostgreSQL-specific")
    conn = connections.get()
    row = await conn.fetch_one("SELECT ($1::int[])[2] AS v", [[10, 20, 30]])
    assert row["v"] == 20
    rows = await conn.fetch_all("SELECT ($1::int[])[2] AS v", [[10, 20, 30]])
    assert rows[0]["v"] == 20


# ---------------------------------------------------------------------------
# Finding 4: concurrent read-then-write sqlite transactions serialize
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_read_then_write_transactions(db):
    """
    GIVEN several concurrent transactions that read before writing
    WHEN they run against a file-backed SQLite database (BEGIN IMMEDIATE)
    THEN they serialize on the write lock instead of failing with
         'database is locked' (and PostgreSQL handles them natively)
    """

    async def worker(n: int) -> None:
        async with in_transaction():
            await AvStamp.all().count()  # read first (deferred BEGIN trap)
            await AvStamp.create(name=f"w{n}")

    await asyncio.gather(*(worker(n) for n in range(4)))
    assert await AvStamp.all().count() == 4

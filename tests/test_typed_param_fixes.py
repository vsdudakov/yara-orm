"""Typed-parameter binding fixes after removing app-side shims:

- ``DateField``/``DatetimeField``/``TimeField`` coerce ISO-8601 string input
  before binding (previously bound as text → 42804).
- ``Subquery`` accepts a single-column ``values_list()`` projection
  (``id__in=Subquery(qs.values_list("col", flat=True))``).
- ``Coalesce(F("col"), value)`` resolves as an ``update()`` value.
- ``LIKE``/``ILIKE`` lookups against a non-text column (e.g. ``uuid``) cast the
  column to text instead of raising 'operator does not exist: uuid ~~* text'.
"""

import datetime as dt

import pytest

from yara_orm import Count, F, Model, Subquery, fields
from yara_orm.dialects import PostgresDialect
from yara_orm.exceptions import FieldError
from yara_orm.functions import Coalesce
from yara_orm.queryset import _ValuesQuery


class TpThing(Model):
    id = fields.IntField(pk=True)
    d = fields.DateField(null=True)
    ts = fields.DatetimeField(null=True)
    t = fields.TimeField(null=True)

    class Meta:
        table = "tp_thing"


class TpReport(Model):
    id = fields.IntField(pk=True)

    class Meta:
        table = "tp_report"


class TpEvent(Model):
    id = fields.IntField(pk=True)
    report_id = fields.IntField(null=True)

    class Meta:
        table = "tp_event"


class TpRun(Model):
    id = fields.IntField(pk=True)
    started_at = fields.DatetimeField(null=True)

    class Meta:
        table = "tp_run"


class TpDoc(Model):
    id = fields.UUIDField(pk=True)
    name = fields.CharField(max_length=50, null=True)

    class Meta:
        table = "tp_doc"


class TpJson(Model):
    id = fields.IntField(pk=True)
    properties = fields.JSONField(null=True)

    class Meta:
        table = "tp_json"


MODELS = [TpThing, TpReport, TpEvent, TpRun, TpDoc, TpJson]


@pytest.mark.asyncio
async def test_pattern_lookups_on_json_column(db):
    """
    GIVEN a JSON column
    WHEN filtering with icontains/startswith/endswith (text patterns)
    THEN the column is cast to text and the serialized JSON matches

    (``__contains`` on a JSON column is structural containment, ``@>`` — see the
    dedicated JSON containment tests — not a text pattern.)
    """
    await TpJson.create(properties={"email": "alice@example.com"})
    assert await TpJson.filter(properties__icontains="ALICE").count() == 1
    assert await TpJson.filter(properties__startswith="{").count() == 1
    assert await TpJson.filter(properties__endswith="}").count() == 1
    assert await TpJson.filter(properties__icontains="nobody").count() == 0


@pytest.mark.asyncio
async def test_date_datetime_time_string_coercion(db):
    """
    GIVEN date/datetime/time columns
    WHEN rows are created from ISO-8601 strings
    THEN the strings are coerced to date/datetime/time and round-trip
    """
    row = await TpThing.create(
        d="2026-07-01",
        ts="2026-07-01T12:00:00+00:00",
        t="13:45:00",
    )
    got = await TpThing.get(id=row.id)
    assert isinstance(got.d, dt.date)
    assert got.d == dt.date(2026, 7, 1)
    assert isinstance(got.ts, dt.datetime)
    assert isinstance(got.t, dt.time)
    assert got.t == dt.time(13, 45)


@pytest.mark.asyncio
async def test_date_string_in_filter(db):
    """
    GIVEN a date column populated from a string
    WHEN filtering with a string value
    THEN the string is coerced and matches the stored row
    """
    await TpThing.create(d="2026-07-01")
    assert await TpThing.filter(d="2026-07-01").count() == 1
    assert await TpThing.filter(d="2026-07-02").count() == 0


@pytest.mark.asyncio
async def test_datetime_string_with_z_suffix(db):
    """
    GIVEN a datetime column
    WHEN created from an ISO string with a trailing 'Z'
    THEN it is parsed as UTC and stored
    """
    row = await TpThing.create(ts="2026-07-01T12:00:00Z")
    got = await TpThing.get(id=row.id)
    assert isinstance(got.ts, dt.datetime)


@pytest.mark.asyncio
async def test_subquery_accepts_values_list(db):
    """
    GIVEN a values_list projection of a single column
    WHEN it is wrapped in Subquery for an id__in / exclude filter
    THEN it renders as a single-column membership subquery
    """
    report = await TpReport.create()
    await TpEvent.create(report_id=report.id)
    await TpEvent.create(report_id=None)

    excluded = TpEvent.filter(report_id__isnull=False).values_list("report_id", flat=True)
    remaining = await TpReport.all().exclude(id__in=Subquery(excluded))
    assert remaining == []

    referenced = await TpReport.filter(id__in=Subquery(excluded))
    assert [r.id for r in referenced] == [report.id]


@pytest.mark.asyncio
async def test_subquery_accepts_values_projection(db):
    """
    GIVEN a single-column values() projection
    WHEN it is wrapped in Subquery
    THEN it renders as a membership subquery
    """
    report = await TpReport.create()
    await TpEvent.create(report_id=report.id)
    sub = TpEvent.filter(report_id__isnull=False).values("report_id")
    referenced = await TpReport.filter(id__in=Subquery(sub))
    assert [r.id for r in referenced] == [report.id]


@pytest.mark.asyncio
async def test_coalesce_f_in_update(db):
    """
    GIVEN a nullable datetime column
    WHEN update(col=Coalesce(F("col"), value)) runs
    THEN NULL is filled but an existing value is preserved
    """
    run = await TpRun.create(started_at=None)
    first = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)

    assert await TpRun.filter(id=run.id).update(started_at=Coalesce(F("started_at"), first)) == 1
    stored = (await TpRun.get(id=run.id)).started_at
    assert stored is not None

    # A second coalesce must not overwrite the now-non-NULL value.
    later = dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc)
    await TpRun.filter(id=run.id).update(started_at=Coalesce(F("started_at"), later))
    assert (await TpRun.get(id=run.id)).started_at == stored


@pytest.mark.asyncio
async def test_ilike_on_uuid_column(db):
    """
    GIVEN a UUID primary-key column
    WHEN filtering with __icontains / __istartswith
    THEN the column is cast to text and the substring matches
    """
    doc = await TpDoc.create(name="hello")
    fragment = str(doc.id)[:8]
    matched = await TpDoc.filter(id__icontains=fragment)
    assert [d.id for d in matched] == [doc.id]

    prefix = await TpDoc.filter(id__istartswith=str(doc.id)[:4])
    assert doc.id in [d.id for d in prefix]


@pytest.mark.asyncio
async def test_annotate_values_keeps_base_columns(db):
    """
    GIVEN a pure annotate() query with no explicit field list
    WHEN values()/values_list()/first().values() project the rows
    THEN the base model columns are returned alongside the annotation
    """
    await TpRun.create(started_at=None)

    rows = await TpRun.all().annotate(n=Count("id")).values()
    assert set(rows[0].keys()) == {"id", "started_at", "n"}

    one = await TpRun.all().annotate(n=Count("id")).first().values()
    assert set(one.keys()) == {"id", "started_at", "n"}

    tup = await TpRun.all().annotate(n=Count("id")).values_list()
    assert len(tup[0]) == 3


# -- direct to_db / subquery unit tests (no database) -------------------------


def test_datefield_to_db_coercions():
    """
    GIVEN a DateField
    WHEN to_db receives a datetime, a full-timestamp string, or a foreign type
    THEN it narrows datetimes/strings to a date and passes other types through
    """
    field = fields.DateField()
    assert field.to_db(dt.datetime(2026, 7, 1, 12, 30)) == dt.date(2026, 7, 1)
    assert field.to_db("2026-07-01T12:00:00") == dt.date(2026, 7, 1)
    assert field.to_db(12345) == 12345  # unknown type passes through


def test_timefield_to_db_coercions():
    """
    GIVEN a TimeField
    WHEN to_db receives a datetime or a foreign type
    THEN it narrows datetimes to a time and passes other types through
    """
    field = fields.TimeField()
    assert field.to_db(dt.datetime(2026, 7, 1, 13, 45)) == dt.time(13, 45)
    assert field.to_db(99) == 99  # unknown type passes through


def test_floatfield_to_db_coerces_numeric_strings():
    """
    GIVEN a FloatField filtered/populated with a numeric string
    WHEN to_db is called
    THEN the string is coerced to float (so PostgreSQL does not reject
         'double precision = text'), while non-strings pass through unchanged
    """
    field = fields.FloatField()
    assert field.to_db("1.5") == 1.5
    assert isinstance(field.to_db("2"), float)
    assert field.to_db(3.0) == 3.0  # already a float
    assert field.to_db(None) is None
    assert field.to_db(F("other")) is not None  # F expression passes through


def test_subquery_rejects_grouped_values_list():
    """
    GIVEN a grouped/annotated values_list projection
    WHEN it is rendered as a Subquery
    THEN a FieldError explains it has no single-column subquery form
    """
    grouped = TpEvent.all().annotate(n=Count("id")).values_list("n", flat=True)
    with pytest.raises(FieldError):
        grouped._plain_select_sql(PostgresDialect())


def test_values_query_without_source_cannot_be_subquery():
    """
    GIVEN a bare _ValuesQuery not bound to a source query set
    WHEN it is rendered as a Subquery
    THEN a TypeError is raised
    """

    async def _noop():
        return []

    with pytest.raises(TypeError):
        _ValuesQuery(_noop)._plain_select_sql(PostgresDialect())

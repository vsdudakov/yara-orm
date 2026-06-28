"""Coverage: every field type round-trips through the engine."""

import datetime as dt
import uuid
from decimal import Decimal

import pytest

from yara_orm import Model, fields


class CvAll(Model):
    id = fields.IntField(pk=True)
    small = fields.SmallIntField(null=True)
    big = fields.BigIntField(null=True)
    flt = fields.FloatField(null=True)
    txt = fields.TextField(null=True)
    blob = fields.BinaryField(null=True)
    flag = fields.BooleanField(null=True)
    day = fields.DateField(null=True)
    clock = fields.TimeField(null=True)
    stamp = fields.DatetimeField(null=True)
    uid = fields.UUIDField(null=True)
    js = fields.JSONField(null=True)
    dec = fields.DecimalField(max_digits=8, decimal_places=2, null=True)
    ch = fields.CharField(max_length=20, null=True)

    class Meta:
        table = "cov_all"


class CvSmallPk(Model):
    id = fields.SmallIntField(pk=True)
    name = fields.CharField(max_length=20)

    class Meta:
        table = "cov_smallpk"


class CvBigPk(Model):
    id = fields.BigIntField(pk=True)
    name = fields.CharField(max_length=20)

    class Meta:
        table = "cov_bigpk"


@pytest.mark.asyncio
async def test_all_field_types_roundtrip(sqlite_db):
    """
    GIVEN a model declaring every supported field type
    WHEN a fully-populated row is created and re-read
    THEN each value round-trips to its native Python type
    """
    code = uuid.uuid4()
    row = await CvAll.create(
        small=7,
        big=2**40,
        flt=1.5,
        txt="hello",
        blob=b"\x00\x01\x02",
        flag=True,
        day=dt.date(2021, 5, 6),
        clock=dt.time(13, 14, 15),
        stamp=dt.datetime(2021, 5, 6, 13, 14, 15),
        uid=code,
        js={"a": [1, 2]},
        dec=Decimal("12.34"),
        ch="abc",
    )
    got = await CvAll.get(id=row.id)
    assert got.small == 7 and isinstance(got.small, int)
    assert got.big == 2**40
    assert got.flt == 1.5
    assert got.txt == "hello"
    assert got.blob == b"\x00\x01\x02"
    assert got.flag is True
    assert got.day == dt.date(2021, 5, 6)
    assert got.clock == dt.time(13, 14, 15)
    assert got.stamp == dt.datetime(2021, 5, 6, 13, 14, 15)
    assert got.uid == code
    assert got.js == {"a": [1, 2]}
    assert got.dec == Decimal("12.34")
    assert got.ch == "abc"


@pytest.mark.asyncio
async def test_null_values_roundtrip(sqlite_db):
    """
    GIVEN a model with nullable columns left unset
    WHEN the row is created and re-read
    THEN every nullable column reads back as None
    """
    row = await CvAll.create()
    got = await CvAll.get(id=row.id)
    for name in ("small", "big", "flt", "txt", "blob", "flag", "day", "clock", "dec", "uid", "js"):
        assert getattr(got, name) is None


@pytest.mark.asyncio
async def test_smallint_and_bigint_primary_keys(sqlite_db):
    """
    GIVEN models keyed by SmallIntField and BigIntField primary keys
    WHEN rows are created
    THEN the auto-increment primary keys are assigned
    """
    a = await CvSmallPk.create(name="a")
    b = await CvBigPk.create(name="b")
    assert a.pk == 1
    assert b.pk == 1


@pytest.mark.asyncio
async def test_uuid_field_accepts_string(sqlite_db):
    """
    GIVEN a UUID provided as a string
    WHEN it is stored and re-read
    THEN it round-trips to a uuid.UUID instance
    """
    code = uuid.uuid4()
    row = await CvAll.create(uid=str(code))
    got = await CvAll.get(id=row.id)
    assert got.uid == code


def test_field_repr():
    """
    GIVEN a model field
    WHEN it is repr'd
    THEN the representation names the field type and attribute
    """
    assert "CharField" in repr(CvAll._meta.fields["ch"])
    assert "ch" in repr(CvAll._meta.fields["ch"])

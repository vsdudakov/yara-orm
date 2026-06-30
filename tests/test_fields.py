"""Coverage: every field type round-trips through the engine."""

import datetime as dt
import json
import uuid
from decimal import Decimal
from enum import Enum

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


class CompatParent(Model):
    id = fields.UUIDField(primary_key=True)
    name = fields.CharField(max_length=20)

    class Meta:
        table = "compat_parent"


class CompatChild(Model):
    id = fields.UUIDField(pk=True, default=uuid.uuid4)
    parent = fields.ForeignKeyField("CompatParent", related_name="children")
    payload = fields.JSONField(
        null=True,
        encoder=lambda v: {**v, "encoded": True},
        decoder=lambda v: {**v, "decoded": True},
    )

    class Meta:
        table = "compat_child"


class FldParent(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20)

    class Meta:
        table = "fld_parent"


class FldChild(Model):
    id = fields.IntField(pk=True)
    parent = fields.ForeignKeyField("FldParent", related_name="children")
    data = fields.JSONField(null=True, decoder=lambda v: v)

    class Meta:
        table = "fld_child"


class FldEnc(Model):
    id = fields.IntField(pk=True)
    data = fields.JSONField(encoder=lambda v: json.dumps({"wrapped": v}))

    class Meta:
        table = "fld_enc"


MODELS = [CvAll, CvSmallPk, CvBigPk, CompatParent, CompatChild, FldParent, FldChild, FldEnc]


@pytest.mark.asyncio
async def test_all_field_types_roundtrip(db):
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
    # Postgres TIMESTAMPTZ returns a UTC-aware value; SQLite returns naive.
    # Compare wall-clock fields so the round-trip check holds on both.
    assert got.stamp.replace(tzinfo=None) == dt.datetime(2021, 5, 6, 13, 14, 15)
    assert got.uid == code
    assert got.js == {"a": [1, 2]}
    assert got.dec == Decimal("12.34")
    assert got.ch == "abc"


@pytest.mark.asyncio
async def test_null_values_roundtrip(db):
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
async def test_smallint_and_bigint_primary_keys(db):
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
async def test_uuid_field_accepts_string(db):
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


@pytest.mark.asyncio
async def test_uuidfield_primary_key_alias_applies_uuid4_default(db):
    """
    GIVEN a model whose pk is declared with the ``primary_key=True``
    WHEN a row is created without supplying an id
    THEN a ``uuid4`` default is applied instead of inserting a NULL id
    """
    p = await CompatParent.create(name="root")
    assert isinstance(p.id, uuid.UUID)


@pytest.mark.asyncio
async def test_foreign_key_coerces_str_value_to_target_uuid(db):
    """
    GIVEN a UUID-pk parent and a child whose FK is set from a string id
    WHEN the child is created with ``parent_id=str(parent.id)``
    THEN the FK value is coerced to the target pk type and binds/round-trips
    """
    p = await CompatParent.create(name="root")
    # Creation succeeding is the gap: the str is coerced to UUID at bind time
    # instead of raising an incorrect-binary-format error.
    c = await CompatChild.create(parent_id=str(p.id), payload={"a": 1})

    again = await CompatChild.get(id=c.id)
    assert isinstance(again.parent_id, uuid.UUID)
    assert again.parent_id == p.id


@pytest.mark.asyncio
async def test_jsonfield_encoder_and_decoder_hooks_apply(db):
    """
    GIVEN a JSONField with ``encoder``/``decoder`` value-transform hooks
    WHEN a row is stored and re-read
    THEN the encoder transforms on write and the decoder transforms on read
    """
    p = await CompatParent.create(name="root")
    c = await CompatChild.create(parent=p, payload={"a": 1})

    fetched = await CompatChild.get(id=c.id)
    assert fetched.payload["encoded"] is True  # encoder ran before storage
    assert fetched.payload["decoded"] is True  # decoder ran on read


def test_field_has_db_field_flag():
    """
    GIVEN any concrete field
    WHEN ``has_db_field`` is read
    THEN it is True (compat flag for column-backed fields)
    """
    assert fields.CharField(max_length=5).has_db_field is True


def test_field_classes_are_subscriptable_for_annotations():
    """
    GIVEN a field class used in a generic type annotation
    WHEN it is subscripted (e.g. ``JSONField[dict]``)
    THEN it returns the class itself rather than raising
    """
    assert fields.JSONField[dict | None] is fields.JSONField
    assert fields.CharField[str] is fields.CharField


def test_bare_on_delete_constants_alias_ondelete_members():
    """
    GIVEN the bare ``fields.SET_NULL`` / ``fields.CASCADE`` names
    WHEN they are compared to the ``OnDelete`` members
    THEN the bare names exist and carry identical string values
    """
    assert fields.SET_NULL == fields.OnDelete.SET_NULL
    assert fields.CASCADE == fields.OnDelete.CASCADE
    assert fields.RESTRICT == fields.OnDelete.RESTRICT
    assert fields.SET_DEFAULT == fields.OnDelete.SET_DEFAULT
    assert fields.NO_ACTION == fields.OnDelete.NO_ACTION


def test_field_accepts_and_ignores_blank_and_max_length_kwargs():
    """
    GIVEN compat kwargs with no yara effect (``blank``, ``max_length`` on a
        non-length field)
    WHEN such fields are constructed
    THEN construction succeeds (the kwargs are accepted and ignored)
    """
    assert fields.UUIDField(max_length=255, null=True).field_kind == "uuid"
    assert fields.TextField(max_length=1000, null=True).field_kind == "text"
    assert fields.CharField(max_length=10, blank=True).max_length == 10


def test_m2m_through_fields_alias_sets_forward_and_backward_keys():
    """
    GIVEN a ManyToManyField declared with a ``through_fields`` tuple
    WHEN the field is constructed
    THEN ``forward_key``/``backward_key`` are filled from the tuple
    """
    m2m = fields.ManyToManyField("CompatParent", through_fields=("fwd", "bwd"))
    assert m2m.forward_key == "fwd"
    assert m2m.backward_key == "bwd"


def test_jsonfield_to_python_without_decoder_returns_value():
    """
    GIVEN a JSONField with no decoder
    WHEN to_python is called with a value
    THEN the value is returned unchanged
    """
    assert fields.JSONField().to_python({"x": 1}) == {"x": 1}


def test_foreign_key_to_db_passes_through_when_target_unresolvable():
    """
    GIVEN a foreign key whose target model is not registered
    WHEN to_db is called with a value
    THEN it returns the value unchanged (no coercion possible)
    """
    fk = fields.ForeignKeyField("NoSuchModelXYZ", related_name="x")
    assert fk.to_db("abc") == "abc"


@pytest.mark.asyncio
async def test_jsonfield_decoder_passes_through_null(db):
    """
    GIVEN a JSONField with a decoder and a NULL value
    WHEN the row is read back
    THEN the decoder is skipped for None and None is returned
    """
    p = await FldParent.create(name="p")
    c = await FldChild.create(parent=p, data=None)
    assert (await FldChild.get(id=c.id)).data is None


@pytest.mark.asyncio
async def test_boolean_field_coerces_non_bool_writes(db):
    """
    GIVEN a boolean column assigned truthy/falsy non-bool values
    WHEN the rows are saved and read back
    THEN the values round-trip as real booleans via bool()
    """
    truthy = await CvAll.create(flag="yes")
    falsy = await CvAll.create(flag=0)
    assert (await CvAll.get(id=truthy.id)).flag is True
    assert (await CvAll.get(id=falsy.id)).flag is False


@pytest.mark.asyncio
async def test_jsonfield_serialises_exotic_python_types(db):
    """
    GIVEN a JSON column holding UUID/Decimal/datetime/date/set values
    WHEN the row is saved and read back
    THEN the values are coerced to JSON-native forms instead of raising
    """
    uid = uuid.uuid4()
    payload = {
        "uid": uid,
        "price": Decimal("1.50"),
        "when": dt.datetime(2026, 6, 30, 12, 0, 0),
        "day": dt.date(2026, 6, 30),
        "tags": {"a", "b"},
    }
    row = await CvAll.create(js=payload)
    stored = (await CvAll.get(id=row.id)).js
    assert stored["uid"] == str(uid)
    assert stored["price"] == "1.50"
    assert stored["when"] == "2026-06-30T12:00:00"
    assert stored["day"] == "2026-06-30"
    assert sorted(stored["tags"]) == ["a", "b"]


@pytest.mark.asyncio
async def test_jsonfield_encoder_returning_string_is_parsed_back(db):
    """
    GIVEN a JSONField whose encoder returns a serialised JSON string
    WHEN a value is saved and read back
    THEN the string is parsed to a native value rather than corrupting the column
    """
    row = await FldEnc.create(data={"x": 1})
    assert (await FldEnc.get(id=row.id)).data == {"wrapped": {"x": 1}}


def test_json_safe_handles_enum_coerces_bytes_and_raises_on_unknown():
    """
    GIVEN a plain Enum member, a bytes value and an unhandled object
    WHEN _json_safe processes them
    THEN the Enum unwraps, bytes become base64, and an unknown leaf raises a
    FieldError naming its location
    """
    import base64

    from yara_orm.exceptions import FieldError
    from yara_orm.fields import _json_safe

    class Colour(Enum):
        RED = "red"

    assert _json_safe(Colour.RED) == "red"
    assert _json_safe({"b": b"hi"}) == {"b": base64.b64encode(b"hi").decode()}

    with pytest.raises(FieldError, match=r"data\.bad"):
        _json_safe({"data": {"bad": object()}})

"""Create-path type coercion: loose input lands as the canonical Python type.

``get()`` decodes rows through each field's read decoder, so a fetched
instance always carries ``Decimal``/``UUID``/enum members/``bool``/
``timedelta``. The write-side constructors (``create``, ``get_or_create``,
``update_or_create``, ``bulk_create``) and ``update_from_dict`` must produce
the same in-memory types when given loose input (e.g. ``"12.34"`` for a
decimal column, a hex string for a UUID column) — previously the raw string
was kept until the row was re-fetched.

Runs on every configured backend so the returned-instance types can be
compared with what that backend's read path yields.
"""

import uuid
from datetime import timedelta
from decimal import Decimal
from enum import Enum, IntEnum

import pytest

from yara_orm import Model, fields


class Color(Enum):
    RED = "red"
    GREEN = "green"


class Prio(IntEnum):
    LOW = 1
    HIGH = 2


class TypedThing(Model):
    id = fields.IntField(pk=True)
    balance = fields.DecimalField(max_digits=12, decimal_places=2)
    token = fields.UUIDField()
    color = fields.CharEnumField(Color, max_length=16)
    prio = fields.IntEnumField(Prio)
    flag = fields.BooleanField()
    dur = fields.TimeDeltaField()

    class Meta:
        table = "typed_thing"


MODELS = [TypedThing]

TOKEN = uuid.uuid4()

#: Loose spellings of every value, as a JSON-ish layer would supply them.
LOOSE = {
    "balance": "12.34",
    "token": str(TOKEN),
    "color": "green",
    "prio": 2,
    "flag": "false",
    "dur": 1_500_000,
}

#: The canonical typed values the same attributes must hold after coercion.
TYPED = {
    "balance": Decimal("12.34"),
    "token": TOKEN,
    "color": Color.GREEN,
    "prio": Prio.HIGH,
    "flag": False,
    "dur": timedelta(seconds=1.5),
}


def assert_typed(obj):
    """Assert every LOOSE-initialised attribute holds its canonical typed value."""
    for name, expected in TYPED.items():
        got = getattr(obj, name)
        assert type(got) is type(expected), (
            f"{name}: {type(got).__name__} != {type(expected).__name__}"
        )
        assert got == expected, f"{name}: {got!r} != {expected!r}"


def test_field_to_python_value_coerces_loose_input():
    """
    GIVEN each coercing field type and a loose input value
    WHEN to_python_value is called
    THEN it returns the canonical Python type, passes canonical values and
         None through unchanged
    """
    dec = fields.DecimalField(max_digits=12, decimal_places=2)
    assert dec.to_python_value("12.34") == Decimal("12.34")
    assert dec.to_python_value(Decimal("1.5")) == Decimal("1.5")
    assert dec.to_python_value(None) is None

    uid = fields.UUIDField()
    assert uid.to_python_value(str(TOKEN)) == TOKEN
    assert uid.to_python_value(TOKEN) is TOKEN
    assert uid.to_python_value(None) is None

    boolean = fields.BooleanField()
    assert boolean.to_python_value("false") is False
    assert boolean.to_python_value("true") is True
    assert boolean.to_python_value(1) is True
    assert boolean.to_python_value(None) is None

    dur = fields.TimeDeltaField()
    assert dur.to_python_value(1_500_000) == timedelta(seconds=1.5)
    assert dur.to_python_value(timedelta(days=1)) == timedelta(days=1)
    assert dur.to_python_value(None) is None

    int_enum = fields.IntEnumField(Prio)
    assert int_enum.to_python_value(2) is Prio.HIGH
    assert int_enum.to_python_value(Prio.LOW) is Prio.LOW
    assert int_enum.to_python_value(None) is None

    char_enum = fields.CharEnumField(Color, max_length=16)
    assert char_enum.to_python_value("red") is Color.RED
    assert char_enum.to_python_value(Color.GREEN) is Color.GREEN
    assert char_enum.to_python_value(None) is None


@pytest.mark.asyncio
async def test_create_returns_typed_attributes(db):
    """
    GIVEN loose (string/int) input for decimal, uuid, enum, bool and timedelta
    WHEN Model.create runs
    THEN the returned instance carries the same canonical types as a get()
    """
    created = await TypedThing.create(**LOOSE)
    assert_typed(created)
    fetched = await TypedThing.get(id=created.id)
    assert_typed(fetched)


@pytest.mark.asyncio
async def test_get_or_create_typed_on_both_paths(db):
    """
    GIVEN loose input
    WHEN get_or_create first inserts, then matches the same row
    THEN both the created and the fetched instance carry canonical types
    """
    created, was_created = await TypedThing.get_or_create(defaults=dict(LOOSE), id=1)
    assert was_created is True
    assert_typed(created)

    fetched, was_created = await TypedThing.get_or_create(defaults=dict(LOOSE), id=1)
    assert was_created is False
    assert fetched.id == created.id
    assert_typed(fetched)


@pytest.mark.asyncio
async def test_update_or_create_typed_on_both_paths(db):
    """
    GIVEN loose input
    WHEN update_or_create first inserts, then updates the row via defaults
    THEN both returned instances carry canonical types (update_from_dict
         coerces the loose defaults, and the row round-trips)
    """
    created, was_created = await TypedThing.update_or_create(defaults=dict(LOOSE), id=1)
    assert was_created is True
    assert_typed(created)

    updated_loose = {**LOOSE, "balance": "99.50", "flag": "true", "color": "red"}
    updated, was_created = await TypedThing.update_or_create(defaults=updated_loose, id=1)
    assert was_created is False
    assert type(updated.balance) is Decimal and updated.balance == Decimal("99.50")
    assert updated.flag is True
    assert updated.color is Color.RED
    assert type(updated.token) is uuid.UUID and updated.token == TOKEN

    fetched = await TypedThing.get(id=1)
    assert fetched.balance == Decimal("99.50")
    assert fetched.flag is True
    assert fetched.color is Color.RED


@pytest.mark.asyncio
async def test_bulk_create_returns_typed_attributes(db):
    """
    GIVEN instances constructed from loose input
    WHEN bulk_create persists them
    THEN the in-memory instances and the re-fetched rows carry canonical types
    """
    objs = [TypedThing(**LOOSE) for _ in range(3)]
    for obj in objs:
        assert_typed(obj)
    await TypedThing.bulk_create(objs)
    for row in await TypedThing.all():
        assert_typed(row)


@pytest.mark.asyncio
async def test_queryset_get_or_create_typed(db):
    """
    GIVEN loose input
    WHEN the QuerySet-level get_or_create / update_or_create insert a row
    THEN the returned instances carry canonical types
    """
    created, was_created = await TypedThing.all().get_or_create(defaults=dict(LOOSE), id=7)
    assert was_created is True
    assert_typed(created)

    updated, was_created = await TypedThing.all().update_or_create(
        defaults={**LOOSE, "balance": "1.25"}, id=7
    )
    assert was_created is False
    assert type(updated.balance) is Decimal and updated.balance == Decimal("1.25")

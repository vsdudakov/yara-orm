"""Regression: filtering across a relation whose terminal field is named like a
lookup operator (``date``/``year``/``range``/...) must traverse to that field,
not apply the transform to the foreign-key column."""

import datetime as dt

import pytest

from yara_orm import Model, fields


class RlSlot(Model):
    id = fields.IntField(pk=True)
    # A field named exactly like a date-transform lookup operator.
    date = fields.DateField()

    class Meta:
        table = "rl_slot"


class RlAppointment(Model):
    id = fields.IntField(pk=True)
    slot = fields.ForeignKeyField("RlSlot", related_name="appointments")

    class Meta:
        table = "rl_appointment"


MODELS = [RlSlot, RlAppointment]


@pytest.mark.asyncio
async def test_filter_relation_field_named_like_lookup(db):
    """
    GIVEN a related model with a field named like a lookup operator (``date``)
    WHEN filtering across the relation on that field (``slot__date=...``)
    THEN it traverses to the related field rather than applying a ``date``
         transform to the FK's integer column (which matched no rows before)
    """
    d1 = dt.date(2026, 7, 1)
    d2 = dt.date(2026, 7, 2)
    s1 = await RlSlot.create(date=d1)
    s2 = await RlSlot.create(date=d2)
    await RlAppointment.create(slot=s1)
    await RlAppointment.create(slot=s2)

    matched = await RlAppointment.filter(slot__date=d1)
    assert len(matched) == 1
    assert matched[0].slot_id == s1.id

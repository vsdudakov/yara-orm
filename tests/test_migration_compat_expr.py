"""Tortoise-migration compatibility: expression/relation-hint helpers.

Covers the ``Value`` literal expression, the ``Q.AND``/``Q.OR`` connector
constants, the relation typing-hint placeholders, and awaiting a forward FK
that was prefetched.
"""

import uuid

import pytest

from yara_orm import Case, F, Model, Q, Value, When, fields


class ExOrder(Model):
    id = fields.IntField(pk=True)
    qty = fields.IntField()
    flag = fields.BooleanField(default=False)

    class Meta:
        table = "ex_order"


class ExParent(Model):
    id = fields.UUIDField(pk=True, default=uuid.uuid4)
    name = fields.CharField(max_length=20)

    class Meta:
        table = "ex_parent"


class ExChild(Model):
    id = fields.UUIDField(pk=True, default=uuid.uuid4)
    parent = fields.ForeignKeyField("ExParent", related_name="children")

    class Meta:
        table = "ex_child"


MODELS = [ExOrder, ExParent, ExChild]


def test_q_connector_constants():
    """
    GIVEN the Tortoise ``Q.AND`` / ``Q.OR`` constants
    WHEN a Q node's connector is compared to them
    THEN the constants exist and match the connector strings
    """
    assert Q.AND == "AND"
    assert Q.OR == "OR"
    assert (Q(a=1) | Q(b=2)).connector == Q.OR
    assert (Q(a=1) & Q(b=2)).connector == Q.AND


def test_relation_hint_placeholders_are_subscriptable():
    """
    GIVEN Tortoise relation typing generics re-exposed on ``fields``
    WHEN they are subscripted in an annotation position
    THEN subscripting succeeds (annotation-only, returns None)
    """
    assert fields.ForeignKeyNullableRelation[ExParent] is None
    assert fields.ReverseRelation["ExChild"] is None
    assert fields.ManyToManyRelation[ExParent] is None


@pytest.mark.asyncio
async def test_value_literal_in_case_annotation(db):
    """
    GIVEN a Case annotation whose default is wrapped in ``Value`` (Tortoise form)
    WHEN the annotated query is evaluated
    THEN ``Value`` binds the literal and the CASE resolves correctly
    """
    await ExOrder.create(qty=5, flag=True)
    await ExOrder.create(qty=7, flag=False)

    rows = await ExOrder.annotate(
        bonus=Case(When(qty__gte=6, then=F("qty")), default=Value(0))
    ).order_by("qty")
    assert [r.bonus for r in rows] == [0, 7]


@pytest.mark.asyncio
async def test_await_prefetched_forward_fk(db):
    """
    GIVEN a child whose forward FK was eager-loaded via prefetch_related
    WHEN the forward relation is awaited (Tortoise ``await self.fk`` idiom)
    THEN awaiting the already-loaded relation returns the instance
    """
    p = await ExParent.create(name="root")
    await ExChild.create(parent=p)

    [child] = await ExChild.all().prefetch_related("parent")
    loaded = await child.parent
    assert loaded.id == p.id

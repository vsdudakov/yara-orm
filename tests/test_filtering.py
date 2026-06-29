"""Complex filtering with Q objects (AND / OR / NOT)."""

import pytest

from yara_orm import Model, Q, fields


class Item(Model):
    name = fields.CharField(max_length=100)
    value = fields.IntField()

    class Meta:
        table = "f_item"


MODELS = [Item]


async def _seed():
    await Item.create(name="alpha", value=1)
    await Item.create(name="beta", value=2)
    await Item.create(name="gamma", value=3)


@pytest.mark.asyncio
async def test_q_or(db):
    """
    GIVEN several Items
    WHEN filtering with Q(value=1) | Q(name="gamma")
    THEN rows matching either branch are returned
    """
    await _seed()
    rows = await Item.filter(Q(value=1) | Q(name="gamma")).order_by("name")
    assert [r.name for r in rows] == ["alpha", "gamma"]


@pytest.mark.asyncio
async def test_q_and_with_kwargs(db):
    """
    GIVEN several Items
    WHEN combining a Q with keyword filters (implicit AND)
    THEN only rows satisfying every condition are returned
    """
    await _seed()
    rows = await Item.filter(Q(value__gte=1), name__in=["alpha", "beta"]).order_by("name")
    assert [r.name for r in rows] == ["alpha", "beta"]


@pytest.mark.asyncio
async def test_q_negation(db):
    """
    GIVEN several Items
    WHEN filtering with ~Q(name="alpha")
    THEN rows not matching the negated branch are returned
    """
    await _seed()
    rows = await Item.filter(~Q(name="alpha")).order_by("name")
    assert [r.name for r in rows] == ["beta", "gamma"]


@pytest.mark.asyncio
async def test_q_nested_or_and(db):
    """
    GIVEN several Items
    WHEN filtering with (Q(value=1) | Q(value=3)) & ~Q(name="gamma")
    THEN the nested boolean logic selects the right rows
    """
    await _seed()
    rows = await Item.filter((Q(value=1) | Q(value=3)) & ~Q(name="gamma")).order_by("name")
    assert [r.name for r in rows] == ["alpha"]

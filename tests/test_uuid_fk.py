"""UUID primary keys and foreign keys that reference them.

yara's own examples use integer PKs, so this exercises the UUID bind/round-trip
path explicitly: a UUID-PK parent, a child whose FK column is therefore a UUID,
and the forward/reverse relation accessors across that UUID FK.
"""

import uuid

import pytest

from yara_orm import Model, fields


class UuidParent(Model):
    id = fields.UUIDField(pk=True, default=uuid.uuid4)
    name = fields.CharField(max_length=20)

    class Meta:
        table = "uuid_parent"


class UuidChild(Model):
    id = fields.UUIDField(pk=True, default=uuid.uuid4)
    parent = fields.ForeignKeyField("UuidParent", related_name="children")
    label = fields.CharField(max_length=20)

    class Meta:
        table = "uuid_child"


MODELS = [UuidParent, UuidChild]


@pytest.mark.asyncio
async def test_uuid_pk_roundtrips(db):
    """
    GIVEN a model with a UUID primary key
    WHEN a row is created and re-fetched
    THEN the pk is a UUID and survives the round-trip
    """
    p = await UuidParent.create(name="root")
    assert isinstance(p.id, uuid.UUID)
    fetched = await UuidParent.get(id=p.id)
    assert fetched.id == p.id and fetched.name == "root"


@pytest.mark.asyncio
async def test_uuid_fk_column_is_uuid_and_binds(db):
    """
    GIVEN a child whose foreign key references a UUID-pk parent
    WHEN the child is created with a parent instance
    THEN the backing FK column holds the parent's UUID and round-trips
    """
    p = await UuidParent.create(name="root")
    c = await UuidChild.create(parent=p, label="leaf")
    assert isinstance(c.parent_id, uuid.UUID)
    assert c.parent_id == p.id

    again = await UuidChild.get(id=c.id)
    assert again.parent_id == p.id


@pytest.mark.asyncio
async def test_uuid_fk_forward_and_filter(db):
    """
    GIVEN a child linked to a UUID-pk parent
    WHEN the forward accessor and a FK filter are used
    THEN the UUID FK value binds correctly in both directions
    """
    p = await UuidParent.create(name="root")
    c = await UuidChild.create(parent=p, label="leaf")

    loaded_parent = await c.parent
    assert loaded_parent.id == p.id

    matched = await UuidChild.filter(parent=p)
    assert [m.id for m in matched] == [c.id]


@pytest.mark.asyncio
async def test_uuid_fk_reverse_relation(db):
    """
    GIVEN multiple children of one UUID-pk parent
    WHEN the reverse manager is iterated
    THEN all children are returned, bound by the UUID FK
    """
    p = await UuidParent.create(name="root")
    c1 = await UuidChild.create(parent=p, label="a")
    c2 = await UuidChild.create(parent=p, label="b")

    children = await p.children.all()
    assert {c.id for c in children} == {c1.id, c2.id}

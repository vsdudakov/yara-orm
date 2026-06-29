"""Model lifecycle signals: pre/post save and delete."""

import pytest

from yara_orm import Model, fields, post_delete, post_save, pre_delete, pre_save

EVENTS = []


class Signal(Model):
    name = fields.CharField(max_length=100)
    slug = fields.CharField(max_length=120, null=True)

    class Meta:
        table = "s_signal"


@pre_save(Signal)
async def _pre_save(sender, instance, using_db, update_fields):
    instance.slug = instance.name.lower()
    EVENTS.append(("pre_save", instance.name))


@post_save(Signal)
async def _post_save(sender, instance, created, using_db, update_fields):
    EVENTS.append(("post_save", instance.name, created))


@pre_delete(Signal)
async def _pre_delete(sender, instance, using_db):
    EVENTS.append(("pre_delete", instance.name))


@post_delete(Signal)
async def _post_delete(sender, instance, using_db):
    EVENTS.append(("post_delete", instance.name))


MODELS = [Signal]


@pytest.mark.asyncio
async def test_pre_save_mutates_and_fires(db):
    """
    GIVEN a pre_save handler that derives a slug
    WHEN an instance is created
    THEN the handler runs before persistence and the slug is stored
    """
    EVENTS.clear()
    obj = await Signal.create(name="Hello")
    assert ("pre_save", "Hello") in EVENTS
    assert obj.slug == "hello"
    reloaded = await Signal.get(id=obj.id)
    assert reloaded.slug == "hello"


@pytest.mark.asyncio
async def test_post_save_created_flag(db):
    """
    GIVEN a post_save handler receiving `created`
    WHEN an instance is first created and then updated
    THEN created is True on insert and False on update
    """
    EVENTS.clear()
    obj = await Signal.create(name="Item")
    obj.name = "Item2"
    await obj.save()
    created_flags = [e[2] for e in EVENTS if e[0] == "post_save"]
    assert created_flags == [True, False]


@pytest.mark.asyncio
async def test_delete_signals(db):
    """
    GIVEN pre/post delete handlers
    WHEN an instance is deleted
    THEN both delete signals fire in order
    """
    EVENTS.clear()
    obj = await Signal.create(name="Gone")
    EVENTS.clear()
    await obj.delete()
    kinds = [e[0] for e in EVENTS]
    assert kinds == ["pre_delete", "post_delete"]

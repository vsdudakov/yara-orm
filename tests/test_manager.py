"""Custom model managers scope every query entry point."""

import pytest

from yara_orm import Manager, Model, fields


class ActiveManager(Manager):
    def get_queryset(self):
        return super().get_queryset().filter(deleted=False)


class MgrItem(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20, unique=True)
    deleted = fields.BooleanField(default=False)

    class Meta:
        table = "mgr_item"
        manager = ActiveManager()


MODELS = [MgrItem]


@pytest.mark.asyncio
async def test_custom_manager_scopes_queries(db):
    """
    GIVEN a model whose Meta.manager filters out soft-deleted rows
    WHEN querying via all/filter/get/get_or_none
    THEN only non-deleted rows are visible through the manager
    """
    await MgrItem.create(name="live", deleted=False)
    await MgrItem.create(name="gone", deleted=True)

    assert [i.name for i in await MgrItem.all()] == ["live"]
    assert await MgrItem.filter(name="gone").count() == 0
    assert (await MgrItem.get(name="live")).name == "live"
    assert await MgrItem.get_or_none(name="gone") is None

    # A row excluded by the manager is still absent after it would match.
    assert await MgrItem.all().count() == 1

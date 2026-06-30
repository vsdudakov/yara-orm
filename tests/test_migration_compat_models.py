"""Tortoise-migration compatibility: model/metaclass-layer behaviours.

Covers abstract-base relation inheritance, the Tortoise ``_meta`` aliases,
awaitable instances, the ``_saved_in_db`` alias, opt-in tolerant ``__init__``,
and the chainable ``Model.get(...)``.
"""

import uuid

import pytest

from yara_orm import Model, fields


class MBOrg(Model):
    id = fields.UUIDField(pk=True, default=uuid.uuid4)
    name = fields.CharField(max_length=20)

    class Meta:
        table = "mb_org"


class MBAbstractOwned(Model):
    id = fields.UUIDField(pk=True, default=uuid.uuid4)
    org = fields.ForeignKeyField("MBOrg", related_name="owned")

    class Meta:
        abstract = True


class MBWidget(MBAbstractOwned):
    label = fields.CharField(max_length=20)

    class Meta:
        table = "mb_widget"


class MBLoose(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20)

    class Meta:
        table = "mb_loose"
        extra_kwargs = "store"


MODELS = [MBOrg, MBWidget, MBLoose]


@pytest.mark.asyncio
async def test_abstract_base_fk_relation_inherited_on_subclass(db):
    """
    GIVEN a foreign key declared on an abstract base model
    WHEN a concrete subclass is created with the relation and it is awaited
    THEN both the relation accessor and its backing column work on the subclass
    """
    org = await MBOrg.create(name="acme")
    widget = await MBWidget.create(org=org, label="w1")

    assert widget.org_id == org.id
    fresh = await MBWidget.get(id=widget.id)
    loaded = await fresh.org
    assert loaded.id == org.id


def test_meta_tortoise_aliases():
    """
    GIVEN a model with fields and a relation
    WHEN the Tortoise ``_meta`` aliases are read
    THEN they map onto the yara equivalents
    """
    meta = MBWidget._meta
    assert meta.db_table == meta.table == "mb_widget"
    assert "org" in meta.fields_map  # relation surfaces in fields_map
    assert "label" in meta.fields_map
    assert "org_id" in meta.db_fields
    assert meta.fields_db_projection["org_id"].db_column == "org_id"


@pytest.mark.asyncio
async def test_instance_is_awaitable_returns_self(db):
    """
    GIVEN a model instance (e.g. from a factory that awaits the result again)
    WHEN the instance itself is awaited
    THEN awaiting is a no-op returning the same instance
    """
    org = await MBOrg.create(name="acme")
    assert (await org) is org


@pytest.mark.asyncio
async def test_saved_in_db_alias_tracks_in_db(db):
    """
    GIVEN a freshly constructed instance
    WHEN it is inspected before and after ``save()``
    THEN the Tortoise ``_saved_in_db`` alias mirrors ``_in_db``
    """
    org = MBOrg(name="acme")
    assert org._saved_in_db is False
    await org.save()
    assert org._saved_in_db is True


def test_extra_kwargs_store_keeps_unknown_attributes():
    """
    GIVEN a model declaring ``Meta.extra_kwargs = "store"``
    WHEN it is constructed with a factory-only kwarg
    THEN the unknown kwarg is stored as a plain attribute instead of raising
    """
    obj = MBLoose(name="x", _factory_only=42)
    assert obj._factory_only == 42
    assert obj.name == "x"


def test_strict_init_still_rejects_unknown_kwargs():
    """
    GIVEN a model without the opt-in ``extra_kwargs`` setting
    WHEN it is constructed with an unknown kwarg
    THEN yara stays strict and raises TypeError
    """
    with pytest.raises(TypeError):
        MBOrg(name="x", bogus=1)


def test_model_is_subscriptable_for_annotations():
    """
    GIVEN a model class used in a generic annotation
    WHEN it is subscripted (e.g. ``QuerySet[Model]`` style hints)
    THEN it returns the class itself rather than raising
    """
    assert MBOrg[int] is MBOrg


@pytest.mark.asyncio
async def test_get_is_chainable_with_prefetch(db):
    """
    GIVEN a parent with reverse-related children
    WHEN ``Model.get(...).prefetch_related(...)`` is awaited (Tortoise idiom)
    THEN the single row is returned with the relation prefetched
    """
    org = await MBOrg.create(name="acme")
    await MBWidget.create(org=org, label="w1")
    await MBWidget.create(org=org, label="w2")

    fetched = await MBOrg.get(id=org.id).prefetch_related("owned")
    assert fetched.id == org.id
    assert {w.label for w in await fetched.owned} == {"w1", "w2"}

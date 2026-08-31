"""Construction semantics: explicit ``<name>_id`` precedence over a relation
object, ``auto_now_add`` honouring a supplied stamp, and ``Model.__iter__``.

These live apart from ``test_model_extras`` so they run on every backend: that
module's schema carries a raw ``CHECK (age >= 0)`` Oracle cannot create, which
keeps all of its ``db`` tests on the Oracle skip list."""

import datetime

import pytest

from yara_orm import Model, fields, timezone


class CsTag(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "cs_tag"


class CsRef(Model):
    id = fields.IntField(pk=True)
    tag = fields.ForeignKeyField("CsTag", related_name="refs", db_constraint=False)

    class Meta:
        table = "cs_ref"


class CsStamped(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "cs_stamped"
        extra_kwargs = "store"


MODELS = [CsTag, CsRef, CsStamped]


@pytest.mark.asyncio
async def test_explicit_relation_id_wins_over_relation_object(db):
    """
    GIVEN a relation passed as an object alongside an explicit ``<name>_id``
    WHEN the instance is constructed
    THEN the explicit id is the one stored and persisted

    A factory's ``SubFactory`` declaration is evaluated even when the caller
    also passes the raw id; resolving the relation last would silently replace
    the id that was asked for.
    """
    wanted = await CsTag.create(name="wanted")
    other = await CsTag.create(name="other")

    ref = CsRef(tag=other, tag_id=wanted.id)
    assert ref.tag_id == wanted.id

    await ref.save()
    assert (await CsRef.get(id=ref.id)).tag_id == wanted.id


@pytest.mark.asyncio
async def test_explicit_relation_id_does_not_cache_a_mismatched_object(db):
    """
    GIVEN a relation object whose pk differs from the explicit ``<name>_id``
    WHEN the relation is awaited
    THEN the row the id names is fetched, not the object that was passed
    """
    wanted = await CsTag.create(name="wanted")
    other = await CsTag.create(name="other")

    ref = await CsRef.create(tag=other, tag_id=wanted.id)

    assert (await ref.tag).id == wanted.id


@pytest.mark.asyncio
async def test_relation_object_still_sets_the_id_when_no_explicit_id(db):
    """
    GIVEN only a relation object (the ordinary case)
    WHEN the instance is constructed
    THEN its id is taken from the object and the object is cached as prefetched
    """
    tag = await CsTag.create(name="only")

    ref = await CsRef.create(tag=tag)

    assert ref.tag_id == tag.id
    assert (await ref.tag) is tag


@pytest.mark.asyncio
async def test_auto_now_add_keeps_an_explicit_created_at(db):
    """
    GIVEN a row created with an explicit ``auto_now_add`` value
    WHEN it is inserted
    THEN the supplied stamp is kept, not replaced with the current time

    ``auto_now_add`` fills the column when nothing was set; backdated fixtures,
    imports and backfills supply their own.
    """
    # Derived from the ORM's clock so the value matches the session's ``use_tz``
    # awareness on every backend.
    backdated = timezone.now() - datetime.timedelta(days=365)

    row = await CsStamped.create(name="imported", created_at=backdated)

    assert row.created_at == backdated
    again = await CsStamped.get(id=row.id)
    # PostgreSQL hands back an aware UTC value even when ``use_tz`` is off, and
    # both stamps are UTC, so the persisted one is compared in its naive form.
    assert again.created_at.replace(tzinfo=None) == backdated.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_auto_now_add_fills_an_unset_created_at(db):
    """
    GIVEN a row created without a ``created_at``
    WHEN it is inserted
    THEN the column is stamped with the current time
    """
    before = timezone.now()

    row = await CsStamped.create(name="fresh")

    assert row.created_at >= before - datetime.timedelta(seconds=1)


@pytest.mark.asyncio
async def test_auto_now_always_stamps_even_when_supplied(db):
    """
    GIVEN a row created with an explicit ``auto_now`` value
    WHEN it is inserted
    THEN the column is stamped anyway — ``auto_now`` owns its value
    """
    backdated = timezone.now() - datetime.timedelta(days=365)

    row = await CsStamped.create(name="stamped", updated_at=backdated)

    assert row.updated_at > backdated


@pytest.mark.asyncio
async def test_model_instance_iterates_as_name_value_pairs(db):
    """
    GIVEN a saved instance, including an attribute kept by ``extra_kwargs``
    WHEN it is iterated (``dict(instance)``)
    THEN every column comes back as a ``(name, value)`` pair, extras included,
         and private attributes are omitted
    """
    row = await CsStamped.create(name="iterable", label="extra")

    as_dict = dict(row)

    assert as_dict["id"] == row.id
    assert as_dict["name"] == "iterable"
    assert as_dict["created_at"] == row.created_at
    assert as_dict["label"] == "extra"
    assert not [key for key in as_dict if key.startswith("_")]


@pytest.mark.asyncio
async def test_deferred_column_is_skipped_by_iteration(db):
    """
    GIVEN an instance fetched with ``only()``
    WHEN it is iterated
    THEN the columns it does not carry are skipped rather than fetched
    """
    await CsStamped.create(name="partial")

    row = await CsStamped.all().only("id", "name").first()

    assert dict(row).keys() == {"id", "name"}

"""Tortoise-migration compatibility: field-layer behaviours.

Covers the field/expression gaps surfaced migrating real Tortoise apps onto
yara: the ``primary_key=`` UUID default, str→UUID foreign-key coercion, JSON
value-transform hooks, and the accepted-and-ignored Tortoise kwargs/aliases.
"""

import uuid

import pytest

from yara_orm import Model, fields


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


MODELS = [CompatParent, CompatChild]


@pytest.mark.asyncio
async def test_uuidfield_primary_key_alias_applies_uuid4_default(db):
    """
    GIVEN a model whose pk is declared with the Tortoise ``primary_key=True``
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
    THEN it is True (Tortoise-compat flag for column-backed fields)
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
    GIVEN Tortoise's bare ``fields.SET_NULL`` / ``fields.CASCADE`` names
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
    GIVEN Tortoise kwargs with no yara effect (``blank``, ``max_length`` on a
        non-length field)
    WHEN such fields are constructed
    THEN construction succeeds (the kwargs are accepted and ignored)
    """
    assert fields.UUIDField(max_length=255, null=True).field_kind == "uuid"
    assert fields.TextField(max_length=1000, null=True).field_kind == "text"
    assert fields.CharField(max_length=10, blank=True).max_length == 10


def test_m2m_through_fields_alias_sets_forward_and_backward_keys():
    """
    GIVEN a ManyToManyField declared with Tortoise's ``through_fields`` tuple
    WHEN the field is constructed
    THEN ``forward_key``/``backward_key`` are filled from the tuple
    """
    m2m = fields.ManyToManyField("CompatParent", through_fields=("fwd", "bwd"))
    assert m2m.forward_key == "fwd"
    assert m2m.backward_key == "bwd"

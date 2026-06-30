"""``only()`` / ``defer()`` over ``rel__col`` paths.

A related-field path projects only (or all-but) the named columns of a joined
relation and hydrates a *partial* related instance, the same way ``only()`` /
``defer()`` already restrict the base model's own columns.
"""

import pytest

from yara_orm import Model, fields
from yara_orm.exceptions import FieldError


class OrCountry(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20)
    code = fields.CharField(max_length=5, null=True)

    class Meta:
        table = "or_country"


class OrContact(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20, null=True)
    properties = fields.JSONField(null=True)
    country = fields.ForeignKeyField("OrCountry", related_name="contacts", null=True)

    class Meta:
        table = "or_contact"


class OrCall(Model):
    id = fields.IntField(pk=True)
    note = fields.CharField(max_length=20, null=True)
    contact = fields.ForeignKeyField("OrContact", related_name="calls", null=True)

    class Meta:
        table = "or_call"


MODELS = [OrCountry, OrContact, OrCall]


async def _seed():
    country = await OrCountry.create(name="Wonderland", code="WL")
    contact = await OrContact.create(name="Alice", properties={"a": 1}, country=country)
    await OrCall.create(note="hi", contact=contact)
    await OrCall.create(note="orphan", contact=None)


@pytest.mark.asyncio
async def test_only_related_path_loads_partial_relation(db):
    """
    GIVEN a Call with a related Contact
    WHEN only("contact__properties") is used
    THEN the contact loads partially (properties set, other columns deferred)
    """
    await _seed()
    rows = await OrCall.all().only("contact__properties").order_by("id")

    contact = rows[0].contact
    assert contact is not None
    assert contact.properties == {"a": 1}
    with pytest.raises(FieldError):
        _ = contact.name


@pytest.mark.asyncio
async def test_only_related_path_restricts_base_to_pk(db):
    """
    GIVEN only a related path is named in only()
    WHEN the rows are fetched
    THEN the base model loads just its primary key
    """
    await _seed()
    rows = await OrCall.all().only("contact__properties").order_by("id")
    assert rows[0].id == 1
    with pytest.raises(FieldError):
        _ = rows[0].note


@pytest.mark.asyncio
async def test_only_base_and_related_paths_combine(db):
    """
    GIVEN both a base field and a related path
    WHEN only("note", "contact__properties") is used
    THEN the base field and the partial relation both load
    """
    await _seed()
    rows = await OrCall.all().only("note", "contact__properties").order_by("id")
    assert rows[0].note == "hi"
    assert rows[0].contact.properties == {"a": 1}


@pytest.mark.asyncio
async def test_only_related_path_null_relation(db):
    """
    GIVEN a Call with no contact
    WHEN only("contact__properties") is used
    THEN the relation hydrates as None
    """
    await _seed()
    rows = await OrCall.all().only("contact__properties").order_by("id")
    assert rows[1].contact is None


@pytest.mark.asyncio
async def test_defer_related_path_loads_all_but_column(db):
    """
    GIVEN a Call with a related Contact
    WHEN defer("contact__properties") is used
    THEN the base loads fully and the contact loads every column but properties
    """
    await _seed()
    rows = await OrCall.all().defer("contact__properties").order_by("id")
    assert rows[0].note == "hi"  # base is full under defer of a related column
    assert rows[0].contact.name == "Alice"
    with pytest.raises(FieldError):
        _ = rows[0].contact.properties


@pytest.mark.asyncio
async def test_only_nested_related_path(db):
    """
    GIVEN a two-hop relation Call -> Contact -> Country
    WHEN only("contact__country__code") is used
    THEN the leaf country loads partially while intermediates load fully
    """
    await _seed()
    rows = await OrCall.all().only("contact__country__code").order_by("id")
    assert rows[0].contact.country.code == "WL"
    with pytest.raises(FieldError):
        _ = rows[0].contact.country.name


@pytest.mark.asyncio
async def test_select_related_then_only_restricts(db):
    """
    GIVEN select_related("contact") combined with only("contact__properties")
    WHEN the rows are fetched
    THEN the relation is restricted to the named column
    """
    await _seed()
    rows = await OrCall.all().select_related("contact").only("contact__properties").order_by("id")
    assert rows[0].contact.properties == {"a": 1}
    with pytest.raises(FieldError):
        _ = rows[0].contact.name


@pytest.mark.asyncio
async def test_only_invalid_related_path_raises(db):
    """
    GIVEN a path whose leading segment is not a forward relation
    WHEN only() is called
    THEN a FieldError is raised
    """
    with pytest.raises(FieldError):
        OrCall.all().only("note__nope")

"""Batch ``bulk_get_or_create`` / ``bulk_update_or_create``.

Existing rows are matched by a natural key in a single query; missing rows are
inserted with one ``bulk_create`` (and, for update-or-create, existing rows are
written back with one ``bulk_update``). A ``(instance, created)`` tuple is
returned per input record, in order.
"""

import pytest

from yara_orm import Model, fields


class BgItem(Model):
    id = fields.IntField(pk=True)
    sku = fields.CharField(max_length=20)
    name = fields.CharField(max_length=20)
    qty = fields.IntField(default=0)

    class Meta:
        table = "bg_item"


class BgPair(Model):
    id = fields.IntField(pk=True)
    a = fields.IntField()
    b = fields.IntField()
    label = fields.CharField(max_length=20)

    class Meta:
        table = "bg_pair"


MODELS = [BgItem, BgPair]


@pytest.mark.asyncio
async def test_bulk_get_or_create_creates_and_reuses(db):
    """
    GIVEN a mix of new and already-present natural keys
    WHEN bulk_get_or_create runs twice
    THEN new rows are created once, and a second run reuses them (created=False)
    """
    recs = [
        {"sku": "A", "name": "Apple", "qty": 1},
        {"sku": "B", "name": "Banana", "qty": 2},
    ]
    first = await BgItem.bulk_get_or_create(recs, key_fields=["sku"])
    assert [created for _, created in first] == [True, True]
    assert await BgItem.all().count() == 2

    # Second run: same keys plus one new key -> only the new one is created.
    recs2 = recs + [{"sku": "C", "name": "Cherry", "qty": 3}]
    second = await BgItem.bulk_get_or_create(recs2, key_fields=["sku"])
    assert [created for _, created in second] == [False, False, True]
    assert await BgItem.all().count() == 3
    # Reused instances carry the persisted primary keys.
    assert second[0][0].id == first[0][0].id


@pytest.mark.asyncio
async def test_bulk_get_or_create_defaults_and_in_batch_duplicates(db):
    """
    GIVEN defaults and a key repeated within one batch
    WHEN bulk_get_or_create runs
    THEN defaults apply only to created rows, and the repeat reuses one instance
    """
    recs = [
        {"sku": "X", "name": "First"},
        {"sku": "X", "name": "Dup"},  # same key within the batch
    ]
    out = await BgItem.bulk_get_or_create(recs, key_fields=["sku"], defaults={"qty": 9})
    assert [created for _, created in out] == [True, False]
    # Both results reference the single created row (created from the first record).
    assert out[0][0] is out[1][0]
    assert await BgItem.all().count() == 1
    row = await BgItem.get(sku="X")
    assert row.name == "First" and row.qty == 9  # default applied on create


@pytest.mark.asyncio
async def test_bulk_get_or_create_composite_key(db):
    """
    GIVEN a composite natural key spanning two columns
    WHEN bulk_get_or_create matches existing rows
    THEN only genuinely-new (a, b) pairs are created
    """
    await BgPair.bulk_get_or_create(
        [{"a": 1, "b": 1, "label": "one"}, {"a": 1, "b": 2, "label": "two"}],
        key_fields=["a", "b"],
    )
    out = await BgPair.bulk_get_or_create(
        [{"a": 1, "b": 1, "label": "dup"}, {"a": 1, "b": 3, "label": "new"}],
        key_fields=["a", "b"],
    )
    assert [created for _, created in out] == [False, True]
    assert await BgPair.all().count() == 3


@pytest.mark.asyncio
async def test_bulk_update_or_create_updates_existing_and_creates_missing(db):
    """
    GIVEN some pre-existing rows and some new ones
    WHEN bulk_update_or_create runs
    THEN existing rows are updated in place and missing rows are inserted
    """
    await BgItem.create(sku="A", name="old", qty=1)
    recs = [
        {"sku": "A", "name": "new", "qty": 10},  # exists -> update
        {"sku": "B", "name": "fresh", "qty": 20},  # missing -> create
    ]
    out = await BgItem.bulk_update_or_create(recs, key_fields=["sku"])
    assert [created for _, created in out] == [False, True]

    a = await BgItem.get(sku="A")
    b = await BgItem.get(sku="B")
    assert (a.name, a.qty) == ("new", 10)
    assert (b.name, b.qty) == ("fresh", 20)


@pytest.mark.asyncio
async def test_bulk_update_or_create_respects_update_fields(db):
    """
    GIVEN update_fields restricting which columns are written on existing rows
    WHEN bulk_update_or_create runs
    THEN only the listed fields change on the existing row
    """
    await BgItem.create(sku="A", name="keep", qty=1)
    out = await BgItem.bulk_update_or_create(
        [{"sku": "A", "name": "ignored", "qty": 99}],
        key_fields=["sku"],
        update_fields=["qty"],
    )
    assert out[0][1] is False
    a = await BgItem.get(sku="A")
    assert a.qty == 99  # updated
    assert a.name == "keep"  # not in update_fields -> unchanged


@pytest.mark.asyncio
async def test_bulk_get_or_create_all_existing_creates_nothing(db):
    """
    GIVEN a batch whose keys all already exist
    WHEN bulk_get_or_create runs
    THEN no INSERT happens and every result is created=False
    """
    await BgItem.create(sku="A", name="a", qty=1)
    await BgItem.create(sku="B", name="b", qty=2)
    out = await BgItem.bulk_get_or_create(
        [{"sku": "A", "name": "a"}, {"sku": "B", "name": "b"}], key_fields=["sku"]
    )
    assert [created for _, created in out] == [False, False]
    assert await BgItem.all().count() == 2


@pytest.mark.asyncio
async def test_bulk_update_or_create_all_new_with_in_batch_duplicate(db):
    """
    GIVEN only new keys, one of them repeated within the batch
    WHEN bulk_update_or_create runs
    THEN the row is created once (no update pass), and the repeat reuses it
    """
    recs = [
        {"sku": "N", "name": "first", "qty": 1},
        {"sku": "N", "name": "dup", "qty": 2},  # same new key within the batch
        {"sku": "M", "name": "other", "qty": 3},
    ]
    out = await BgItem.bulk_update_or_create(recs, key_fields=["sku"])
    assert [created for _, created in out] == [True, False, True]
    assert out[0][0] is out[1][0]
    assert await BgItem.all().count() == 2


@pytest.mark.asyncio
async def test_bulk_ops_empty_input_and_missing_key(db):
    """
    GIVEN empty input or no key fields
    WHEN the batch upsert helpers are called
    THEN empty input returns [] and a missing key raises ValueError
    """
    assert await BgItem.bulk_get_or_create([], key_fields=["sku"]) == []
    assert await BgItem.bulk_update_or_create([], key_fields=["sku"]) == []
    with pytest.raises(ValueError, match="key field"):
        await BgItem.bulk_get_or_create([{"sku": "A"}], key_fields=[])
    with pytest.raises(ValueError, match="key field"):
        await BgItem.bulk_update_or_create([{"sku": "A"}], key_fields=[])

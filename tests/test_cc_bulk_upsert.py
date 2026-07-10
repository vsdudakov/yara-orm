"""Batch insert / upsert corner cases (cc = crud corner).

Extends the existing bulk_get_or_create suite with conflict-handling paths on
``bulk_create`` (ignore_conflicts, DO UPDATE via update_fields / custom
on_conflict target), multi-batch ``bulk_update``, and further corner cases for
``bulk_get_or_create`` / ``bulk_update_or_create`` (order preservation,
defaults untouched on existing rows, composite-key updates, empty inputs).
"""

import pytest

from yara_orm import Model, fields


class CcuItem(Model):
    id = fields.IntField(pk=True)
    sku = fields.CharField(max_length=20, unique=True)
    qty = fields.IntField(default=0)
    name = fields.CharField(max_length=20, default="")

    class Meta:
        table = "ccu_item"


class CcuPair(Model):
    id = fields.IntField(pk=True)
    a = fields.IntField()
    b = fields.IntField()
    label = fields.CharField(max_length=20)

    class Meta:
        table = "ccu_pair"


class RfbUser(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "rfb_user"


MODELS = [CcuItem, CcuPair, RfbUser]


# -- bulk_create conflict handling -------------------------------------------


@pytest.mark.asyncio
async def test_bulk_create_ignore_conflicts_skips_duplicate(db):
    """
    GIVEN a row already present on a unique column
    WHEN bulk_create with ignore_conflicts inserts a duplicate and a new row
    THEN the duplicate is skipped, the new row lands, and the original is intact
    """
    await CcuItem.create(sku="A", qty=5)
    await CcuItem.bulk_create(
        [CcuItem(sku="A", qty=99), CcuItem(sku="B", qty=2)],
        ignore_conflicts=True,
    )
    assert await CcuItem.all().count() == 2
    assert (await CcuItem.get(sku="A")).qty == 5  # untouched by the skipped insert
    assert (await CcuItem.get(sku="B")).qty == 2


@pytest.mark.asyncio
async def test_bulk_create_upsert_mixed_conflict_and_new(db):
    """
    GIVEN one existing row on the unique ``sku`` target
    WHEN bulk_create upserts a batch mixing that sku and a new one
    THEN the conflicting row's named field is overwritten (others kept) and the
    new row is inserted

    Note: the conflict target is ``sku`` (not the pk) because ``bulk_create``
    never sends an auto-increment primary key, so a pk-target upsert can never
    match an explicit id.
    """
    await CcuItem.create(sku="A", qty=5, name="keep")
    await CcuItem.bulk_create(
        [
            CcuItem(sku="A", qty=99, name="ignored"),  # conflicts on sku
            CcuItem(sku="C", qty=7, name="new"),  # brand new
        ],
        on_conflict=["sku"],
        update_fields=["qty"],
    )
    a = await CcuItem.get(sku="A")
    assert a.qty == 99  # updated
    assert a.name == "keep"  # not in update_fields -> unchanged
    assert (await CcuItem.get(sku="C")).qty == 7
    assert await CcuItem.all().count() == 2


@pytest.mark.asyncio
async def test_bulk_create_upsert_custom_conflict_target(db):
    """
    GIVEN an existing row on a unique non-pk column
    WHEN bulk_create upserts with on_conflict=["sku"], update_fields=["qty"]
    THEN the row matching that target is updated in place (no duplicate inserted)
    """
    await CcuItem.create(sku="A", qty=5)
    await CcuItem.bulk_create(
        [CcuItem(sku="A", qty=50)],
        on_conflict=["sku"],
        update_fields=["qty"],
    )
    assert await CcuItem.all().count() == 1
    assert (await CcuItem.get(sku="A")).qty == 50


@pytest.mark.asyncio
async def test_bulk_create_empty_with_conflict_flags_is_noop(db):
    """
    GIVEN an empty iterable
    WHEN bulk_create runs with ignore_conflicts set
    THEN it returns [] and inserts nothing
    """
    assert await CcuItem.bulk_create([], ignore_conflicts=True) == []
    assert await CcuItem.all().count() == 0


# -- bulk_update across batches ----------------------------------------------


@pytest.mark.asyncio
async def test_bulk_update_multiple_batches_only_named_field(db):
    """
    GIVEN more rows than a small batch_size, each mutated on two fields
    WHEN bulk_update writes only one field with batch_size=2
    THEN every row's named field persists across batches; the other does not
    """
    await CcuItem.bulk_create([CcuItem(sku=f"s{i}", qty=i, name="orig") for i in range(5)])
    objs = await CcuItem.all().order_by("sku")
    for o in objs:
        o.qty = o.qty + 100
        o.name = "MUT"  # not written
    n = await CcuItem.bulk_update(objs, ["qty"], batch_size=2)
    assert n == 5
    reload = {i.sku: (i.qty, i.name) for i in await CcuItem.all()}
    assert reload["s0"] == (100, "orig")
    assert reload["s4"] == (104, "orig")


# -- bulk_get_or_create extra corners ----------------------------------------


@pytest.mark.asyncio
async def test_bulk_get_or_create_preserves_input_order(db):
    """
    GIVEN a batch mixing pre-existing and new keys out of insertion order
    WHEN bulk_get_or_create runs
    THEN the (instance, created) results align 1:1 with the input order
    """
    await CcuItem.create(sku="B", qty=1)
    recs = [
        {"sku": "A", "qty": 1},  # new
        {"sku": "B", "qty": 1},  # existing
        {"sku": "C", "qty": 1},  # new
    ]
    out = await CcuItem.bulk_get_or_create(recs, key_fields=["sku"])
    assert [inst.sku for inst, _ in out] == ["A", "B", "C"]
    assert [created for _, created in out] == [True, False, True]


@pytest.mark.asyncio
async def test_bulk_get_or_create_defaults_not_applied_to_existing(db):
    """
    GIVEN an existing row and a batch supplying defaults
    WHEN bulk_get_or_create runs
    THEN defaults populate only newly-created rows, never the existing one
    """
    await CcuItem.create(sku="A", qty=1, name="orig")
    out = await CcuItem.bulk_get_or_create(
        [{"sku": "A"}, {"sku": "Z"}],
        key_fields=["sku"],
        defaults={"name": "defaulted"},
    )
    assert [created for _, created in out] == [False, True]
    assert (await CcuItem.get(sku="A")).name == "orig"  # untouched
    assert (await CcuItem.get(sku="Z")).name == "defaulted"  # default on create


@pytest.mark.asyncio
async def test_bulk_get_or_create_all_new_composite_key(db):
    """
    GIVEN an all-new batch keyed on two columns
    WHEN bulk_get_or_create runs
    THEN every (a, b) pair is created exactly once
    """
    recs = [
        {"a": 1, "b": 1, "label": "x"},
        {"a": 1, "b": 2, "label": "y"},
        {"a": 2, "b": 1, "label": "z"},
    ]
    out = await CcuPair.bulk_get_or_create(recs, key_fields=["a", "b"])
    assert [created for _, created in out] == [True, True, True]
    assert await CcuPair.all().count() == 3


# -- bulk_update_or_create extra corners -------------------------------------


@pytest.mark.asyncio
async def test_bulk_update_or_create_composite_key_updates_and_creates(db):
    """
    GIVEN a composite-key table with one existing pair
    WHEN bulk_update_or_create updates that pair and adds a new one
    THEN the existing pair's label is rewritten and the new pair is inserted
    """
    await CcuPair.create(a=1, b=1, label="old")
    out = await CcuPair.bulk_update_or_create(
        [
            {"a": 1, "b": 1, "label": "new"},  # exists -> update
            {"a": 1, "b": 2, "label": "fresh"},  # missing -> create
        ],
        key_fields=["a", "b"],
    )
    assert [created for _, created in out] == [False, True]
    assert (await CcuPair.get(a=1, b=1)).label == "new"
    assert (await CcuPair.get(a=1, b=2)).label == "fresh"


@pytest.mark.asyncio
async def test_bulk_update_or_create_in_batch_duplicate_existing(db):
    """
    GIVEN an existing key repeated within one batch
    WHEN bulk_update_or_create runs
    THEN the last write wins for the update and both results reuse one instance
    """
    await CcuItem.create(sku="A", qty=0)
    out = await CcuItem.bulk_update_or_create(
        [{"sku": "A", "qty": 1}, {"sku": "A", "qty": 2}],
        key_fields=["sku"],
    )
    assert [created for _, created in out] == [False, False]
    assert out[0][0] is out[1][0]  # same instance reused for the dup key
    assert (await CcuItem.get(sku="A")).qty == 2  # last value in the batch wins
    assert await CcuItem.all().count() == 1


@pytest.mark.asyncio
async def test_bulk_update_or_create_all_existing(db):
    """
    GIVEN a batch whose keys all already exist
    WHEN bulk_update_or_create runs
    THEN nothing is created and every row is updated in place
    """
    await CcuItem.create(sku="A", qty=1)
    await CcuItem.create(sku="B", qty=1)
    out = await CcuItem.bulk_update_or_create(
        [{"sku": "A", "qty": 10}, {"sku": "B", "qty": 20}],
        key_fields=["sku"],
    )
    assert [created for _, created in out] == [False, False]
    assert (await CcuItem.get(sku="A")).qty == 10
    assert (await CcuItem.get(sku="B")).qty == 20
    assert await CcuItem.all().count() == 2


# ---------------------------------------------------------------------------
# bulk_create with explicit auto-increment pks
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_bulk_create_explicit_pks_are_preserved(db):
    """
    GIVEN instances carrying explicit values for an auto-increment pk
    WHEN they are inserted with bulk_create
    THEN the supplied ids reach the database and stay on the objects
    """
    created = await RfbUser.bulk_create(
        [RfbUser(id=100, name="a"), RfbUser(id=205, name="b"), RfbUser(id=301, name="c")]
    )
    assert [u.id for u in created] == [100, 205, 301]
    rows = await RfbUser.all().order_by("id")
    assert [(u.id, u.name) for u in rows] == [(100, "a"), (205, "b"), (301, "c")]
    # The instances round-trip: an explicit-pk object can update its own row.
    created[0].name = "a2"
    await created[0].save()
    assert (await RfbUser.get(id=100)).name == "a2"


@pytest.mark.asyncio
async def test_bulk_create_auto_pks_unchanged(db):
    """
    GIVEN instances without pk values
    WHEN they are inserted with bulk_create
    THEN generated ids are backfilled onto the objects, matching the rows
    """
    created = await RfbUser.bulk_create([RfbUser(name="x"), RfbUser(name="y")])
    assert all(u.id is not None for u in created)
    by_name = {u.name: u.id async for u in RfbUser.all()}
    assert {u.name: u.id for u in created} == by_name


@pytest.mark.asyncio
async def test_bulk_create_mixed_pks_raise(db):
    """
    GIVEN a batch mixing explicit and unset auto-increment pks
    WHEN bulk_create runs
    THEN a ValueError is raised (silent splitting would reorder the inserts)
    """
    with pytest.raises(ValueError, match="mix of instances"):
        await RfbUser.bulk_create([RfbUser(id=7, name="a"), RfbUser(name="b")])
    assert await RfbUser.all().count() == 0


@pytest.mark.asyncio
async def test_bulk_create_explicit_pks_after_auto_rows(db):
    """
    GIVEN rows already inserted with generated ids
    WHEN a later bulk_create supplies explicit non-clashing ids
    THEN both sets coexist with the ids the caller chose
    """
    await RfbUser.bulk_create([RfbUser(name="auto1"), RfbUser(name="auto2")])
    await RfbUser.bulk_create([RfbUser(id=500, name="manual")])
    assert (await RfbUser.get(id=500)).name == "manual"
    assert await RfbUser.all().count() == 3

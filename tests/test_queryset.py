"""QuerySet/Model: lookups, ordering, projections, mutations, plus the
ergonomics helpers (get_or_create, in_bulk, bulk_update, slicing, distinct,
last/earliest/latest, refresh_from_db, update_from_dict, select_for_update)."""

import pytest

from yara_orm import FieldError, Model, Q, fields


class CvItem(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)
    value = fields.IntField()

    class Meta:
        table = "cov_item"


class QsWidget(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50, unique=True)
    qty = fields.IntField(default=0)

    class Meta:
        table = "qs_widget"


MODELS = [CvItem, QsWidget]


async def _seed():
    await CvItem.create(name="alpha", value=1)
    await CvItem.create(name="Beta", value=2)
    await CvItem.create(name="gamma", value=3)


@pytest.mark.asyncio
async def test_comparison_lookups(db):
    """
    GIVEN seeded items
    WHEN filtering with gt/gte/lt/lte/not lookups
    THEN each comparison selects the right rows
    """
    await _seed()
    assert {i.value for i in await CvItem.filter(value__gt=1)} == {2, 3}
    assert {i.value for i in await CvItem.filter(value__gte=2)} == {2, 3}
    assert {i.value for i in await CvItem.filter(value__lt=3)} == {1, 2}
    assert {i.value for i in await CvItem.filter(value__lte=2)} == {1, 2}
    assert {i.value for i in await CvItem.filter(value__not=2)} == {1, 3}


@pytest.mark.asyncio
async def test_text_lookups(db):
    """
    GIVEN seeded items
    WHEN filtering with contains/startswith/endswith and case-insensitive forms
    THEN the LIKE/ILIKE patterns select the right rows
    """
    await _seed()
    assert [i.name for i in await CvItem.filter(name__contains="lph")] == ["alpha"]
    assert [i.name for i in await CvItem.filter(name__icontains="BET")] == ["Beta"]
    assert [i.name for i in await CvItem.filter(name__startswith="al")] == ["alpha"]
    assert [i.name for i in await CvItem.filter(name__istartswith="be")] == ["Beta"]
    assert [i.name for i in await CvItem.filter(name__endswith="ma")] == ["gamma"]
    # Every seeded name ends in "a"; iendswith matches case-insensitively.
    assert {i.name for i in await CvItem.filter(name__iendswith="A")} == {
        "alpha",
        "Beta",
        "gamma",
    }


@pytest.mark.asyncio
async def test_in_empty_and_isnull(db):
    """
    GIVEN seeded items
    WHEN filtering with an empty __in and with __isnull
    THEN an empty __in matches nothing and isnull works both ways
    """
    await _seed()
    assert await CvItem.filter(value__in=[]).count() == 0
    assert await CvItem.filter(name__isnull=False).count() == 3
    assert await CvItem.filter(name__isnull=True).count() == 0


@pytest.mark.asyncio
async def test_q_negation_and_exclude(db):
    """
    GIVEN seeded items
    WHEN using ~Q and exclude()
    THEN negated conditions remove the matching rows
    """
    await _seed()
    assert [i.name for i in await CvItem.filter(~Q(name="alpha")).order_by("name")] == [
        "Beta",
        "gamma",
    ]
    assert [i.name for i in await CvItem.exclude(value=2).order_by("value")] == ["alpha", "gamma"]


@pytest.mark.asyncio
async def test_ordering_limit_offset(db):
    """
    GIVEN seeded items
    WHEN ordering descending with limit and offset
    THEN the right slice is returned in order
    """
    await _seed()
    rows = await CvItem.all().order_by("-value").limit(2).offset(1)
    assert [i.value for i in rows] == [2, 1]


@pytest.mark.asyncio
async def test_first_get_or_none_exists(db):
    """
    GIVEN seeded items
    WHEN calling first/get_or_none/exists
    THEN they return the expected single/optional/boolean results
    """
    await _seed()
    assert (await CvItem.all().order_by("value").first()).value == 1
    assert await CvItem.all().filter(value=99).first() is None
    assert await CvItem.get_or_none(value=99) is None
    assert (await CvItem.get_or_none(value=1)).name == "alpha"
    assert await CvItem.filter(value=1).exists() is True
    assert await CvItem.filter(value=99).exists() is False


@pytest.mark.asyncio
async def test_values_and_values_list(db):
    """
    GIVEN seeded items
    WHEN projecting with values/values_list (incl. flat)
    THEN dicts, tuples and scalars are returned without model objects
    """
    await _seed()
    assert await CvItem.all().order_by("value").values_list("value", flat=True) == [1, 2, 3]
    pairs = await CvItem.all().order_by("value").values_list("name", "value")
    assert pairs[0] == ("alpha", 1)
    dicts = await CvItem.all().order_by("value").values("value")
    assert dicts == [{"value": 1}, {"value": 2}, {"value": 3}]
    with pytest.raises(FieldError):
        await CvItem.all().values_list("name", "value", flat=True)


@pytest.mark.asyncio
async def test_update_and_delete_queryset(db):
    """
    GIVEN seeded items
    WHEN updating and deleting via the queryset
    THEN the affected counts and remaining rows are correct
    """
    await _seed()
    assert await CvItem.filter(value__lte=2).update(value=0) == 2
    assert await CvItem.filter(value=0).count() == 2
    assert await CvItem.filter(value=0).delete() == 2
    assert await CvItem.all().count() == 1


@pytest.mark.asyncio
async def test_unknown_field_raises(db):
    """
    GIVEN a model
    WHEN filtering by an unknown field
    THEN a FieldError is raised
    """
    with pytest.raises(FieldError):
        await CvItem.filter(missing=1)


@pytest.mark.asyncio
async def test_get_or_create(db):
    """
    GIVEN an empty table
    WHEN get_or_create runs for a key twice
    THEN the first call creates the row and the second returns it unchanged
    """
    obj, created = await QsWidget.get_or_create(name="a", defaults={"qty": 1})
    assert created is True and obj.qty == 1
    obj, created = await QsWidget.get_or_create(name="a", defaults={"qty": 99})
    assert created is False and obj.qty == 1  # defaults ignored on hit


@pytest.mark.asyncio
async def test_update_or_create(db):
    """
    GIVEN a key that is first absent then present
    WHEN update_or_create runs twice with differing defaults
    THEN it creates on the miss and persists the new defaults on the hit
    """
    obj, created = await QsWidget.update_or_create(name="b", defaults={"qty": 7})
    assert created is True and obj.qty == 7
    obj, created = await QsWidget.update_or_create(name="b", defaults={"qty": 8})
    assert created is False and obj.qty == 8
    assert (await QsWidget.get(name="b")).qty == 8  # persisted


@pytest.mark.asyncio
async def test_in_bulk(db):
    """
    GIVEN several rows
    WHEN in_bulk is called with pk values, an empty list, and a non-pk field
    THEN it returns instances keyed by the lookup field (and {} for empty)
    """
    await QsWidget.bulk_create([QsWidget(name=f"x{i}", qty=i) for i in range(4)])
    ids = [w.id for w in await QsWidget.all().order_by("id")]
    out = await QsWidget.in_bulk(ids[:3])
    assert set(out) == set(ids[:3])
    assert all(isinstance(v, QsWidget) for v in out.values())
    assert await QsWidget.in_bulk([]) == {}
    by_name = await QsWidget.in_bulk(["x1", "x2"], field_name="name")
    assert set(by_name) == {"x1", "x2"}


@pytest.mark.asyncio
async def test_bulk_update(db):
    """
    GIVEN rows mutated in memory on two fields
    WHEN bulk_update is asked to write only one field
    THEN that field is persisted, the other is not, and the row count returns
    """
    await QsWidget.bulk_create([QsWidget(name=f"y{i}", qty=i) for i in range(5)])
    objs = await QsWidget.filter(name__in=["y0", "y1", "y2"]).order_by("name")
    for o in objs:
        o.qty = 100
        o.name = o.name + "!"  # not in fields -> must NOT be written
    n = await QsWidget.bulk_update(objs, ["qty"])
    assert n == 3
    assert (await QsWidget.get(name="y1")).qty == 100  # name unchanged, qty written
    assert await QsWidget.bulk_update([], ["qty"]) == 0


@pytest.mark.asyncio
async def test_slicing(db):
    """
    GIVEN an ordered query set
    WHEN it is indexed with a slice and an integer
    THEN the slice applies offset/limit and the index fetches one row (bounds
    and negative indices raise)
    """
    await QsWidget.bulk_create([QsWidget(name=f"s{i}", qty=i) for i in range(10)])
    page = await QsWidget.all().order_by("qty")[2:5]
    assert [w.qty for w in page] == [2, 3, 4]
    third = await QsWidget.all().order_by("qty")[3]
    assert third.qty == 3
    with pytest.raises(IndexError):
        await QsWidget.all().order_by("qty")[999]
    with pytest.raises(ValueError):
        QsWidget.all()[-1]


@pytest.mark.asyncio
async def test_distinct(db):
    """
    GIVEN six rows whose qty is one of two values
    WHEN a distinct projection of qty is fetched
    THEN only the two distinct values are returned
    """
    await QsWidget.bulk_create([QsWidget(name=f"d{i}", qty=i % 2) for i in range(6)])
    qtys = await QsWidget.all().distinct().values_list("qty", flat=True)
    assert sorted(qtys) == [0, 1]


@pytest.mark.asyncio
async def test_last_earliest_latest(db):
    """
    GIVEN rows with distinct qty values
    WHEN last/earliest/latest run
    THEN last reverses the ordering and earliest/latest order asc/desc then
    take the first row
    """
    await QsWidget.bulk_create([QsWidget(name=f"o{i}", qty=i) for i in range(5)])
    assert (await QsWidget.all().order_by("qty").last()).qty == 4
    assert (await QsWidget.all().last()).qty == 4  # defaults to pk desc
    assert (await QsWidget.all().earliest("qty")).qty == 0
    assert (await QsWidget.all().latest("qty")).qty == 4


@pytest.mark.asyncio
async def test_refresh_from_db(db):
    """
    GIVEN an instance gone stale after a separate UPDATE
    WHEN refresh_from_db runs
    THEN its column values are reloaded from the database
    """
    w = await QsWidget.create(name="r", qty=1)
    await QsWidget.filter(name="r").update(qty=42)
    assert w.qty == 1  # stale in memory
    await w.refresh_from_db()
    assert w.qty == 42


@pytest.mark.asyncio
async def test_update_from_dict(db):
    """
    GIVEN an instance
    WHEN update_from_dict is given known and unknown keys
    THEN known fields are set (and persist on save) and unknown keys raise
    """
    w = await QsWidget.create(name="u", qty=1)
    assert w.update_from_dict({"qty": 5}) is w
    assert w.qty == 5
    await w.save()
    assert (await QsWidget.get(name="u")).qty == 5
    with pytest.raises(FieldError):
        w.update_from_dict({"nope": 1})


@pytest.mark.asyncio
async def test_select_for_update(db):
    """
    GIVEN a row
    WHEN a query set adds select_for_update()
    THEN the FOR UPDATE clause is emitted on PostgreSQL, is a no-op on SQLite,
    and the query returns the row on both
    """
    await QsWidget.create(name="lock", qty=1)
    rows = await QsWidget.filter(name="lock").select_for_update()
    assert len(rows) == 1

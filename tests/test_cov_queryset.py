"""Coverage: queryset lookups, ordering, projections and mutations."""

import pytest

from yara_orm import FieldError, Model, Q, fields


class CvItem(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)
    value = fields.IntField()

    class Meta:
        table = "cov_item"


async def _seed():
    await CvItem.create(name="alpha", value=1)
    await CvItem.create(name="Beta", value=2)
    await CvItem.create(name="gamma", value=3)


@pytest.mark.asyncio
async def test_comparison_lookups(sqlite_db):
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
async def test_text_lookups(sqlite_db):
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
async def test_in_empty_and_isnull(sqlite_db):
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
async def test_q_negation_and_exclude(sqlite_db):
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
async def test_ordering_limit_offset(sqlite_db):
    """
    GIVEN seeded items
    WHEN ordering descending with limit and offset
    THEN the right slice is returned in order
    """
    await _seed()
    rows = await CvItem.all().order_by("-value").limit(2).offset(1)
    assert [i.value for i in rows] == [2, 1]


@pytest.mark.asyncio
async def test_first_get_or_none_exists(sqlite_db):
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
async def test_values_and_values_list(sqlite_db):
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
async def test_update_and_delete_queryset(sqlite_db):
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
async def test_unknown_field_raises(sqlite_db):
    """
    GIVEN a model
    WHEN filtering by an unknown field
    THEN a FieldError is raised
    """
    with pytest.raises(FieldError):
        await CvItem.filter(missing=1)

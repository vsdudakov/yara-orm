"""CRUD / query corner cases (cc = crud corner).

Focuses on NULL-aware negative lookups (__not / __not_in / __in with NULLs),
range bounds, F expressions in update and filter, get/get_or_none error and
multiplicity semantics, slicing edges, ordering tie-breaks and the projection
of ``pk``. These probe boundaries the existing suites do not cover.
"""

import pytest

from yara_orm import DoesNotExist, F, FieldError, Model, Q, fields
from yara_orm.exceptions import MultipleObjectsReturned


class CcThing(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20, null=True)
    value = fields.IntField(null=True)
    qty = fields.IntField(default=0)

    class Meta:
        table = "cc_thing"


class CcPair(Model):
    id = fields.IntField(pk=True)
    a = fields.IntField()
    b = fields.IntField()

    class Meta:
        table = "cc_pair"


MODELS = [CcThing, CcPair]


async def _seed_values(vals):
    """Create one CcThing per value (name mirrors the value for identification)."""
    for v in vals:
        await CcThing.create(name=None if v is None else f"n{v}", value=v)


# -- NULL-aware negative lookups ---------------------------------------------


@pytest.mark.asyncio
async def test_not_lookup_keeps_null_rows(db):
    """
    GIVEN a nullable column with some NULL rows
    WHEN filtering with ``value__not=1``
    THEN non-matching non-NULL rows AND the NULL rows survive (NULL != x is kept)
    """
    await _seed_values([1, 2, None])
    got = {t.value for t in await CcThing.filter(value__not=1)}
    assert got == {2, None}


@pytest.mark.asyncio
async def test_not_in_keeps_null_rows(db):
    """
    GIVEN a nullable column
    WHEN filtering with ``value__not_in=[1, 2]``
    THEN rows outside the set plus the NULL rows are returned
    """
    await _seed_values([1, 2, 3, None])
    got = {t.value for t in await CcThing.filter(value__not_in=[1, 2])}
    assert got == {3, None}


@pytest.mark.asyncio
async def test_not_in_empty_returns_all(db):
    """
    GIVEN any rows
    WHEN filtering with an empty ``value__not_in=[]``
    THEN every row matches (``x NOT IN ()`` is always true)
    """
    await _seed_values([1, 2, None])
    assert await CcThing.filter(value__not_in=[]).count() == 3


@pytest.mark.asyncio
async def test_in_with_null_element_does_not_match_null_rows(db):
    """
    GIVEN a NULL among the IN values and NULL-valued rows
    WHEN filtering ``value__in=[None, 1]``
    THEN only the literal 1 matches; SQL NULL never equals NULL so NULL rows drop
    """
    await _seed_values([1, 2, None])
    got = [t.value for t in await CcThing.filter(value__in=[None, 1])]
    assert got == [1]


@pytest.mark.asyncio
async def test_exclude_drops_null_rows(db):
    """
    GIVEN a nullable column with a NULL row
    WHEN excluding ``value=1``
    THEN the NULL row IS dropped: exclude wraps the predicate in NOT(...), which
    is UNKNOWN for NULL (unlike the single-lookup ``__not`` that adds OR IS NULL)
    """
    await _seed_values([1, 2, None])
    got = {t.value for t in await CcThing.exclude(value=1)}
    assert got == {2}


# -- range / isnull ----------------------------------------------------------


@pytest.mark.asyncio
async def test_range_is_inclusive_on_both_ends(db):
    """
    GIVEN integer rows 1..5
    WHEN filtering ``value__range=(2, 4)``
    THEN both endpoints are included (BETWEEN is inclusive)
    """
    await _seed_values([1, 2, 3, 4, 5])
    got = sorted(t.value for t in await CcThing.filter(value__range=(2, 4)))
    assert got == [2, 3, 4]


@pytest.mark.asyncio
async def test_isnull_true_false(db):
    """
    GIVEN a mix of NULL and non-NULL rows
    WHEN filtering isnull True/False
    THEN each selects exactly the NULL / non-NULL partition
    """
    await _seed_values([1, None, None])
    assert await CcThing.filter(value__isnull=True).count() == 2
    assert await CcThing.filter(value__isnull=False).count() == 1


# -- nested Q logic ----------------------------------------------------------


@pytest.mark.asyncio
async def test_nested_q_and_or_not(db):
    """
    GIVEN a compound predicate mixing AND, OR and NOT
    WHEN filtering ``(value>=2 AND value<=4) AND NOT value=3``
    THEN only the endpoints of the range that are not 3 remain
    """
    await _seed_values([1, 2, 3, 4, 5])
    q = Q(value__gte=2) & Q(value__lte=4) & ~Q(value=3)
    got = sorted(t.value for t in await CcThing.filter(q))
    assert got == [2, 4]


@pytest.mark.asyncio
async def test_exclude_with_q_or(db):
    """
    GIVEN an OR predicate handed to exclude()
    WHEN excluding ``Q(value=1) | Q(value=2)``
    THEN rows matching either branch are removed; NULL rows are also dropped
    (NOT(NULL=1 OR NULL=2) is UNKNOWN under three-valued logic)
    """
    await _seed_values([1, 2, 3, None])
    got = {t.value for t in await CcThing.exclude(Q(value=1) | Q(value=2))}
    assert got == {3}


# -- distinct ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_distinct_over_multiple_columns(db):
    """
    GIVEN duplicate qty rows
    WHEN projecting distinct values over the (repeated) qty column
    THEN DISTINCT applies to the projected column, collapsing duplicates
    """
    for q in (0, 0, 1, 1, 2):
        await CcThing.create(name="x", value=1, qty=q)
    qtys = await CcThing.all().distinct().values_list("qty", flat=True)
    assert sorted(qtys) == [0, 1, 2]


# -- F expressions -----------------------------------------------------------


@pytest.mark.asyncio
async def test_f_increment_in_update(db):
    """
    GIVEN a row with a numeric column
    WHEN update() assigns ``F('qty') + 1``
    THEN the stored value is incremented relative to itself
    """
    t = await CcThing.create(name="a", value=1, qty=10)
    assert await CcThing.filter(id=t.id).update(qty=F("qty") + 1) == 1
    assert (await CcThing.get(id=t.id)).qty == 11


@pytest.mark.asyncio
async def test_f_compound_arithmetic_in_update(db):
    """
    GIVEN a row with a numeric column
    WHEN update() assigns a compound ``F('qty') * 2 + 3`` expression
    THEN the arithmetic is evaluated in SQL against the current value
    """
    t = await CcThing.create(name="a", value=1, qty=4)
    await CcThing.filter(id=t.id).update(qty=F("qty") * 2 + 3)
    assert (await CcThing.get(id=t.id)).qty == 11


@pytest.mark.asyncio
async def test_f_copies_another_column_in_update(db):
    """
    GIVEN a row with two numeric columns
    WHEN update() sets ``qty=F('value')``
    THEN qty takes the other column's value
    """
    t = await CcThing.create(name="a", value=42, qty=0)
    await CcThing.filter(id=t.id).update(qty=F("value"))
    assert (await CcThing.get(id=t.id)).qty == 42


@pytest.mark.asyncio
async def test_f_compares_two_columns_in_filter(db):
    """
    GIVEN rows where column a may be greater/less/equal to column b
    WHEN filtering ``a__gt=F('b')``
    THEN only rows whose a exceeds b are returned
    """
    await CcPair.create(a=5, b=3)  # a > b
    await CcPair.create(a=2, b=8)  # a < b
    await CcPair.create(a=4, b=4)  # a == b
    got = {(p.a, p.b) for p in await CcPair.filter(a__gt=F("b"))}
    assert got == {(5, 3)}


# -- get / get_or_none semantics ---------------------------------------------


@pytest.mark.asyncio
async def test_get_raises_does_not_exist(db):
    """
    GIVEN no matching row
    WHEN get() runs
    THEN DoesNotExist is raised
    """
    with pytest.raises(DoesNotExist):
        await CcThing.get(value=999)


@pytest.mark.asyncio
async def test_get_raises_multiple_objects(db):
    """
    GIVEN two rows sharing a lookup value
    WHEN get() runs on that value
    THEN MultipleObjectsReturned is raised
    """
    await _seed_values([7, 7])
    with pytest.raises(MultipleObjectsReturned):
        await CcThing.get(value=7)


@pytest.mark.asyncio
async def test_get_or_none_returns_first_on_multiple(db):
    """
    GIVEN multiple rows matching the lookup
    WHEN get_or_none runs
    THEN it returns a single instance (limit 1) rather than raising
    """
    await _seed_values([7, 7])
    obj = await CcThing.get_or_none(value=7)
    assert obj is not None and obj.value == 7


# -- slicing edges -----------------------------------------------------------


@pytest.mark.asyncio
async def test_slice_limit_only(db):
    """
    GIVEN an ordered query set
    WHEN sliced with a limit-only form ``[:2]``
    THEN the leading window is returned
    """
    await _seed_values([0, 1, 2, 3, 4])
    head = await CcThing.all().order_by("value")[:2]
    assert [t.value for t in head] == [0, 1]


@pytest.mark.xfail(
    reason="BUG: offset-only slice emits OFFSET with no LIMIT -> SQLite syntax error",
    strict=False,
)
@pytest.mark.asyncio
async def test_slice_offset_only(db):
    """
    GIVEN an ordered query set
    WHEN sliced with an offset-only form ``[3:]``
    THEN the trailing window is returned (SQLite needs ``LIMIT -1 OFFSET n``)
    """
    await _seed_values([0, 1, 2, 3, 4])
    tail = await CcThing.all().order_by("value")[3:]
    assert [t.value for t in tail] == [3, 4]


@pytest.mark.asyncio
async def test_slice_empty_and_beyond_end(db):
    """
    GIVEN an ordered query set
    WHEN sliced to an empty window or past the end
    THEN empty and clamped results come back without error
    """
    await _seed_values([0, 1, 2])
    assert await CcThing.all().order_by("value")[1:1] == []
    beyond = await CcThing.all().order_by("value")[2:100]
    assert [t.value for t in beyond] == [2]


# -- ordering tie-break ------------------------------------------------------


@pytest.mark.asyncio
async def test_order_by_multiple_fields_tiebreak(db):
    """
    GIVEN rows tying on the first sort key
    WHEN ordering by ``qty`` then ``-value``
    THEN ties on qty are broken by the descending secondary key
    """
    await CcThing.create(name="a", value=1, qty=1)
    await CcThing.create(name="b", value=9, qty=1)  # ties on qty with a
    await CcThing.create(name="c", value=5, qty=0)
    rows = await CcThing.all().order_by("qty", "-value")
    assert [t.name for t in rows] == ["c", "b", "a"]


# -- update / delete counts --------------------------------------------------


@pytest.mark.asyncio
async def test_update_returns_zero_when_no_rows_match(db):
    """
    GIVEN no rows matching the filter
    WHEN update() runs
    THEN it reports zero affected rows
    """
    await _seed_values([1])
    assert await CcThing.filter(value=999).update(qty=1) == 0


@pytest.mark.asyncio
async def test_delete_instance_leaves_others(db):
    """
    GIVEN several rows
    WHEN one instance is deleted
    THEN only that row is removed and the rest remain
    """
    await _seed_values([1, 2, 3])
    victim = await CcThing.get(value=2)
    await victim.delete()
    assert sorted(t.value for t in await CcThing.all()) == [1, 3]


# -- save insert vs update path ----------------------------------------------


@pytest.mark.asyncio
async def test_save_insert_then_update_path(db):
    """
    GIVEN a freshly created (INSERTed) row
    WHEN a field is mutated and save() runs again
    THEN the second save takes the UPDATE path and persists the change in place
    """
    t = await CcThing.create(name="a", value=1, qty=1)
    first_id = t.id
    t.qty = 2
    await t.save()
    assert t.id == first_id  # same row, not a new insert
    assert await CcThing.all().count() == 1
    assert (await CcThing.get(id=first_id)).qty == 2


# -- projection of pk --------------------------------------------------------


@pytest.mark.asyncio
async def test_values_list_pk_alias(db):
    """
    GIVEN rows
    WHEN projecting ``values_list('pk', flat=True)``
    THEN the primary keys are returned via the pk alias
    """
    await _seed_values([1, 2])
    ids = sorted(t.id for t in await CcThing.all())
    got = sorted(await CcThing.all().values_list("pk", flat=True))
    assert got == ids


# -- in_bulk edges -----------------------------------------------------------


@pytest.mark.asyncio
async def test_in_bulk_ignores_missing_and_dedups(db):
    """
    GIVEN a set of ids including a duplicate and a non-existent id
    WHEN in_bulk is called
    THEN only existing ids appear, keyed uniquely
    """
    await _seed_values([1, 2])
    ids = sorted(t.id for t in await CcThing.all())
    out = await CcThing.in_bulk([ids[0], ids[0], 999999])
    assert set(out) == {ids[0]}


# -- count / exists edges ----------------------------------------------------


@pytest.mark.asyncio
async def test_count_and_exists_on_empty(db):
    """
    GIVEN an empty table
    WHEN count() and exists() run
    THEN they report 0 and False
    """
    assert await CcThing.all().count() == 0
    assert await CcThing.all().exists() is False


# -- get_or_create / update_or_create corner cases ---------------------------


@pytest.mark.xfail(
    reason="BUG: equality lookup value=None compiles to `col = NULL` (never true) "
    "instead of `col IS NULL`, so get_or_create inserts a duplicate NULL row",
    strict=False,
)
@pytest.mark.asyncio
async def test_get_or_create_matches_on_null_lookup(db):
    """
    GIVEN a row whose lookup column is NULL
    WHEN get_or_create runs with ``value=None``
    THEN the existing NULL row is matched (created=False), not duplicated
    """
    await CcThing.create(name="only", value=None)
    obj, created = await CcThing.get_or_create(value=None, defaults={"name": "new"})
    assert created is False
    assert obj.name == "only"
    assert await CcThing.filter(value__isnull=True).count() == 1


@pytest.mark.xfail(
    reason="BUG: equality-to-None compiles to `col = NULL`; should be `col IS NULL`",
    strict=False,
)
@pytest.mark.asyncio
async def test_filter_equality_none_matches_null_rows(db):
    """
    GIVEN NULL and non-NULL rows
    WHEN filtering with the equality shorthand ``value=None``
    THEN it should match the NULL rows (Django/Tortoise translate to IS NULL)
    """
    await _seed_values([1, None, None])
    assert await CcThing.filter(value=None).count() == 2


@pytest.mark.asyncio
async def test_update_or_create_creates_then_updates(db):
    """
    GIVEN a key first absent then present
    WHEN update_or_create runs twice with different defaults
    THEN it inserts on the miss and overwrites on the hit
    """
    obj, created = await CcThing.update_or_create(value=5, defaults={"qty": 1})
    assert created is True and obj.qty == 1
    obj, created = await CcThing.update_or_create(value=5, defaults={"qty": 2})
    assert created is False and obj.qty == 2
    assert (await CcThing.get(value=5)).qty == 2


@pytest.mark.asyncio
async def test_get_or_create_defaults_ignored_on_hit(db):
    """
    GIVEN an existing row
    WHEN get_or_create supplies defaults for a matching key
    THEN the defaults are ignored and the stored row is returned unchanged
    """
    await CcThing.create(name="keep", value=3, qty=7)
    obj, created = await CcThing.get_or_create(value=3, defaults={"qty": 100})
    assert created is False and obj.qty == 7


@pytest.mark.asyncio
async def test_update_from_dict_unknown_field_raises(db):
    """
    GIVEN a model instance
    WHEN update_from_dict is given an unknown field
    THEN FieldError is raised (no silent attribute creation on a strict model)
    """
    t = await CcThing.create(name="a", value=1)
    with pytest.raises(FieldError):
        t.update_from_dict({"nope": 1})

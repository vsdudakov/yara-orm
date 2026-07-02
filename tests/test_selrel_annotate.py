"""Combining ``select_related()``, ``only()``/``defer()`` and ``annotate()``.

One queryset may now join-and-hydrate forward relations, narrow the base
projection and carry annotations in a single SELECT:

- Non-aggregate annotations (window ``RawSQL``, ``F`` arithmetic) add no
  ``GROUP BY``.
- Aggregate annotations group by the base pk plus each joined relation's pk,
  so a reverse-relation aggregate never inflates the row count.
- ``only()``/``defer()`` keep unselected columns deferred; an annotation may
  not reuse a model field name under a narrowed projection.
- ``select_for_update()`` still rejects every annotated shape.
"""

import datetime as dt

import pytest

from yara_orm import Count, F, FieldError, Max, Model, RawSQL, UnSupportedError, fields


class SraOrg(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=30)

    class Meta:
        table = "sra_org"


class SraContact(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=30)
    email = fields.CharField(max_length=50, null=True)
    org = fields.ForeignKeyField("SraOrg", related_name="contacts")

    class Meta:
        table = "sra_contact"


class SraDisposition(Model):
    id = fields.IntField(pk=True)
    label = fields.CharField(max_length=30)

    class Meta:
        table = "sra_disposition"


class SraCall(Model):
    id = fields.IntField(pk=True)
    org_id_num = fields.IntField(default=1)
    to_number = fields.CharField(max_length=20)
    started = fields.DatetimeField(null=True)
    duration = fields.IntField(default=0)
    contact = fields.ForeignKeyField("SraContact", related_name="calls", null=True)
    disposition = fields.ForeignKeyField("SraDisposition", related_name="calls", null=True)

    class Meta:
        table = "sra_call"


MODELS = [SraOrg, SraContact, SraDisposition, SraCall]

_T0 = dt.datetime(2026, 1, 1, 9, 0, tzinfo=dt.timezone.utc)


async def _seed():
    """Seed one org, two contacts (2 / 1 calls), a contactless call and one
    disposition.

    Returns:
        The ``(org, alice, bob, disposition)`` tuple.
    """
    org = await SraOrg.create(name="Acme")
    alice = await SraContact.create(name="Alice", email="a@x.io", org=org)
    bob = await SraContact.create(name="Bob", org=org)
    dispo = await SraDisposition.create(label="answered")
    await SraCall.create(
        to_number="+111", started=_T0, duration=300, contact=alice, disposition=dispo
    )
    await SraCall.create(to_number="+222", started=_T0, duration=100, contact=alice)
    await SraCall.create(to_number="+333", started=_T0, duration=200, contact=bob)
    await SraCall.create(to_number="+444", duration=50, contact=None, disposition=None)
    return org, alice, bob, dispo


# ---------------------------------------------------------------------------
# Window annotation + two select_related + only() (the headline shape)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_window_annotation_with_select_related_and_only(db):
    """
    GIVEN calls with related contacts/dispositions and a window annotation
    WHEN select_related + annotate(RawSQL window) + only() run as one queryset
    THEN instances carry hydrated relations, the annotation, the narrowed base
         projection — and no GROUP BY inflates or collapses the rows
    """
    await _seed()
    qs = (
        SraCall.filter(org_id_num=1)
        .select_related("contact", "disposition")
        .annotate(duration_rank=RawSQL("RANK() OVER (ORDER BY duration DESC)"))
        .only("id", "to_number", "started")
        .order_by("id")
    )
    rows = await qs

    assert [c.to_number for c in rows] == ["+111", "+222", "+333", "+444"]
    assert [c.duration_rank for c in rows] == [1, 3, 2, 4]
    # Relations are hydrated objects, available synchronously.
    assert rows[0].contact.name == "Alice"
    assert rows[0].disposition.label == "answered"
    assert rows[2].contact.name == "Bob"
    # Narrowed base projection: the unselected column stays deferred.
    assert rows[0].started is not None
    with pytest.raises(FieldError):
        _ = rows[0].duration


@pytest.mark.asyncio
async def test_nullable_fk_null_side_hydrates_none_with_annotation(db):
    """
    GIVEN a call whose nullable FKs are NULL (the LEFT JOIN null side)
    WHEN select_related is combined with an F-arithmetic annotation
    THEN the relation hydrates as None and the annotation is still attached
    """
    await _seed()
    rows = (
        await SraCall.filter(to_number="+444")
        .select_related("contact", "disposition")
        .annotate(double_duration=F("duration") * 2)
    )
    [call] = rows
    assert call.contact is None
    assert call.disposition is None
    assert call.double_duration == 100


@pytest.mark.asyncio
async def test_slicing_combined_shape(db):
    """
    GIVEN the combined select_related + annotate + only queryset
    WHEN a slice is applied
    THEN LIMIT/OFFSET narrow the joined, annotated rows
    """
    await _seed()
    qs = (
        SraCall.filter(org_id_num=1)
        .select_related("contact")
        .annotate(duration_rank=RawSQL("RANK() OVER (ORDER BY duration DESC)"))
        .only("id", "to_number")
        .order_by("id")
    )
    rows = await qs[1:3]
    assert [c.to_number for c in rows] == ["+222", "+333"]
    assert [c.duration_rank for c in rows] == [3, 2]


# ---------------------------------------------------------------------------
# Aggregates + select_related: GROUP BY base pk + joined pks
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_aggregate_over_reverse_relation_with_select_related(db):
    """
    GIVEN contacts with 2 and 1 calls and a forward FK to their org
    WHEN a reverse-relation Count is annotated alongside select_related("org")
    THEN one row per contact comes back (the reverse join is collapsed by the
         GROUP BY) with the correct count and the hydrated org
    """
    await _seed()
    contacts = (
        await SraContact.all().select_related("org").annotate(n=Count("calls")).order_by("name")
    )
    assert [(c.name, c.n) for c in contacts] == [("Alice", 2), ("Bob", 1)]
    assert [c.org.name for c in contacts] == ["Acme", "Acme"]


@pytest.mark.asyncio
async def test_having_filter_on_aggregate_with_select_related(db):
    """
    GIVEN the aggregate + select_related shape
    WHEN filter() names the annotation
    THEN it routes to HAVING and keeps only the qualifying groups, with the
         relation still hydrated and counts not inflated by the joins
    """
    await _seed()
    contacts = (
        await SraContact.all().select_related("org").annotate(n=Count("calls")).filter(n__gte=2)
    )
    assert [(c.name, c.n) for c in contacts] == [("Alice", 2)]
    assert contacts[0].org.name == "Acme"


@pytest.mark.asyncio
async def test_aggregate_over_selected_relation_shares_the_join(db):
    """
    GIVEN an aggregate whose target path traverses a select_related relation
    WHEN the combined query runs
    THEN the annotation reuses the join plan's LEFT JOIN (no duplicate alias)
    """
    await _seed()
    contacts = (
        await SraContact.all()
        .select_related("org")
        .annotate(org_name=Max("org__name"))
        .order_by("name")
    )
    assert [(c.name, c.org_name) for c in contacts] == [("Alice", "Acme"), ("Bob", "Acme")]
    assert contacts[0].org.name == "Acme"


@pytest.mark.asyncio
async def test_order_by_annotation_with_select_related(db):
    """
    GIVEN the aggregate + select_related shape
    WHEN order_by names the annotation
    THEN rows come back ordered by the aggregate value
    """
    await _seed()
    contacts = (
        await SraContact.all().select_related("org").annotate(n=Count("calls")).order_by("-n")
    )
    assert [(c.name, c.n) for c in contacts] == [("Alice", 2), ("Bob", 1)]


@pytest.mark.asyncio
async def test_count_and_exists_honour_having_on_combined_shape(db):
    """
    GIVEN the combined shape with a HAVING filter (and then a slice)
    WHEN count() / exists() run
    THEN they wrap the full joined, annotated SELECT and stay correct
    """
    await _seed()
    qs = SraContact.all().select_related("org").annotate(n=Count("calls")).filter(n__gte=1)
    assert await qs.count() == 2
    assert await qs.exists() is True
    assert await qs[:1].count() == 1
    assert await qs.filter(n__gte=3).count() == 0
    assert await qs.filter(n__gte=3).exists() is False


# ---------------------------------------------------------------------------
# only()/defer() interplay
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_defer_with_annotate_keeps_column_deferred(db):
    """
    GIVEN defer("duration") combined with an annotation over that column
    WHEN the query runs (no select_related)
    THEN the annotation computes from the column while the column itself stays
         deferred on the instance
    """
    await _seed()
    [call] = (
        await SraCall.filter(to_number="+111")
        .defer("duration")
        .annotate(double_duration=F("duration") * 2)
    )
    assert call.double_duration == 600
    with pytest.raises(FieldError):
        _ = call.duration


@pytest.mark.asyncio
async def test_annotation_name_colliding_with_field_raises_under_only(db):
    """
    GIVEN an annotation named after a model column deferred by only()
    WHEN the query runs (with and without select_related)
    THEN a FieldError rejects the ambiguous name instead of un-deferring it
    """
    await _seed()
    with pytest.raises(FieldError, match="collides"):
        await SraCall.all().only("id").annotate(duration=RawSQL("1"))
    with pytest.raises(FieldError, match="collides"):
        await SraCall.all().select_related("contact").only("id").annotate(duration=RawSQL("1"))


# ---------------------------------------------------------------------------
# Locking, prefetch and values() interactions
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_select_for_update_still_raises_on_annotated_shapes(db):
    """
    GIVEN select_for_update() on annotated querysets (joined or not)
    WHEN they execute
    THEN UnSupportedError is raised — every annotated shape is rejected, since
         even the ungrouped one may carry an unlockable window expression
    """
    await _seed()
    with pytest.raises(UnSupportedError):
        await SraContact.all().select_related("org").annotate(n=Count("calls")).select_for_update()
    with pytest.raises(UnSupportedError):
        await (
            SraCall.all()
            .select_related("contact")
            .annotate(r=RawSQL("RANK() OVER (ORDER BY duration DESC)"))
            .select_for_update()
        )


@pytest.mark.asyncio
async def test_prefetch_related_still_combines_with_annotate(db):
    """
    GIVEN annotate() combined with prefetch_related (and select_related)
    WHEN the query runs
    THEN the annotation, the joined relation and the prefetched reverse
         relation all load
    """
    await _seed()
    contacts = (
        await SraContact.all()
        .select_related("org")
        .prefetch_related("calls")
        .annotate(n=Count("calls"))
        .order_by("name")
    )
    assert [(c.name, c.n) for c in contacts] == [("Alice", 2), ("Bob", 1)]
    assert contacts[0].org.name == "Acme"
    assert {call.to_number for call in await contacts[0].calls} == {"+111", "+222"}


@pytest.mark.asyncio
async def test_values_with_annotations_unchanged(db):
    """
    GIVEN an annotated queryset that also select_relates a relation
    WHEN values() / values_list() project it
    THEN the grouped projection behaves as before (select_related is
         irrelevant to dict/tuple rows)
    """
    await _seed()
    rows = (
        await SraContact.all()
        .select_related("org")
        .annotate(n=Count("calls"))
        .order_by("name")
        .values("name", "n")
    )
    assert rows == [{"name": "Alice", "n": 2}, {"name": "Bob", "n": 1}]
    pairs = (
        await SraContact.all().annotate(n=Count("calls")).order_by("name").values_list("name", "n")
    )
    assert pairs == [("Alice", 2), ("Bob", 1)]

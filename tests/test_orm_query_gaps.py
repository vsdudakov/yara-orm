"""Tortoise-parity query gaps: str↔int param coercion, JSON ``__contains``/
``__filter``, FK-traversal in group_by/values, and bulk-op ``auto_now``.
"""

from enum import Enum

import pytest

from yara_orm import Count, Model, fields
from yara_orm.exceptions import UnSupportedError


class GRole(Enum):
    ADMIN = "admin"
    OWNER = "owner"
    MEMBER = "member"


class GDisp(Model):
    id = fields.IntField(pk=True)
    user_defined = fields.BooleanField(default=False)
    name = fields.CharField(max_length=20)

    class Meta:
        table = "g_disp"


class GRec(Model):
    id = fields.IntField(pk=True)
    ext_id = fields.IntField(null=True)
    big = fields.BigIntField(null=True)
    small = fields.SmallIntField(null=True)
    data = fields.JSONField(null=True)
    disp = fields.ForeignKeyField("GDisp", related_name="recs", null=True)

    class Meta:
        table = "g_rec"


class GStamped(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "g_stamped"


class GUser(Model):
    id = fields.IntField(pk=True)
    role = fields.CharEnumField(GRole, max_length=10)
    outcome = fields.CharField(max_length=20, null=True)

    class Meta:
        table = "g_user"


MODELS = [GDisp, GRec, GStamped, GUser]


@pytest.mark.asyncio
async def test_not_and_not_in_keep_null_rows(db):
    """
    GIVEN a nullable column with a NULL row
    WHEN filtered with __not / __not_in
    THEN NULL rows are kept (Tortoise semantics), not silently dropped
    """
    await GUser.create(role=GRole.ADMIN, outcome="Finished")
    await GUser.create(role=GRole.MEMBER, outcome=None)
    await GUser.create(role=GRole.OWNER, outcome="canceled")

    # NULL outcome + "canceled" both survive `!= "Finished"`.
    assert await GUser.filter(outcome__not="Finished").count() == 2
    assert await GUser.filter(outcome__not_in=["Finished"]).count() == 2
    # The NULL row survives even when both real values are excluded.
    assert await GUser.filter(outcome__not_in=["Finished", "canceled"]).count() == 1


@pytest.mark.asyncio
async def test_enum_members_in_in_list(db):
    """
    GIVEN a CharEnumField
    WHEN filtered with __in over enum members
    THEN each member is coerced to its value and matches
    """
    await GUser.create(role=GRole.ADMIN)
    await GUser.create(role=GRole.OWNER)
    await GUser.create(role=GRole.MEMBER)

    assert await GUser.filter(role=GRole.ADMIN).count() == 1
    assert await GUser.filter(role__in=[GRole.ADMIN, GRole.OWNER]).count() == 2


@pytest.mark.asyncio
async def test_int_column_accepts_string_values(db):
    """
    GIVEN integer columns filtered with string values
    WHEN using = and __in with str
    THEN the strings are coerced to int (no 'integer = text')
    """
    await GRec.create(ext_id=123, big=2**40, small=7)
    await GRec.create(ext_id=456, big=1, small=1)
    assert await GRec.filter(ext_id="123").count() == 1
    assert await GRec.filter(ext_id__in={"123", "456"}).count() == 2
    assert await GRec.filter(big=str(2**40)).count() == 1
    assert await GRec.filter(small__in=["7"]).count() == 1


@pytest.mark.asyncio
async def test_json_filter_path_conditions(db):
    """
    GIVEN a JSON column with nested keys
    WHEN filtered with __filter={"path__op": value}
    THEN each entry becomes a JSON key-path condition, ANDed
    """
    await GRec.create(data={"properties": {"subject": "Call"}, "status": "open"})
    await GRec.create(data={"properties": {"subject": "Email"}, "status": "resolved"})

    assert await GRec.filter(data__filter={"properties__subject": "Call"}).count() == 1
    assert await GRec.filter(data__filter={"properties__subject__icontains": "mail"}).count() == 1
    assert (
        await GRec.filter(data__filter={"properties__subject__in": ["Call", "Email"]}).count() == 2
    )
    assert await GRec.filter(data__filter={"status__not": "resolved"}).count() == 1
    # An empty filter matches everything.
    assert await GRec.filter(data__filter={}).count() == 2


@pytest.mark.asyncio
async def test_group_by_and_values_across_relation(db):
    """
    GIVEN an annotated query grouped by a related-model column
    WHEN values() aliases the related columns
    THEN the FK path is joined/resolved and the aliases map correctly
    """
    d1 = await GDisp.create(user_defined=True, name="Custom")
    d2 = await GDisp.create(user_defined=False, name="Standard")
    for _ in range(3):
        await GRec.create(disp=d1)
    for _ in range(2):
        await GRec.create(disp=d2)

    rows = await (
        GRec.all()
        .annotate(n=Count("id"))
        .group_by("disp__user_defined", "disp__name")
        .order_by("-n")
        .values(user_defined="disp__user_defined", name="disp__name", n="n")
    )
    assert rows[0] == {"user_defined": True, "name": "Custom", "n": 3}
    assert rows[1] == {"user_defined": False, "name": "Standard", "n": 2}

    # values_list over a related column
    vl = (
        await GRec.all()
        .annotate(n=Count("id"))
        .group_by("disp__name")
        .values_list("disp__name", "n")
    )
    assert sorted(vl) == [("Custom", 3), ("Standard", 2)]


@pytest.mark.asyncio
async def test_bulk_update_bumps_auto_now(db):
    """
    GIVEN a model with created_at (auto_now_add) and updated_at (auto_now)
    WHEN bulk_create then bulk_update run
    THEN both timestamps are set on create and updated_at bumps on update
    """
    objs = [GStamped(name=f"a{i}") for i in range(3)]
    await GStamped.bulk_create(objs)
    fetched = await GStamped.all().order_by("id")
    assert all(o.created_at is not None and o.updated_at is not None for o in fetched)

    before = fetched[0].updated_at
    for o in fetched:
        o.name = o.name + "!"
    await GStamped.bulk_update(fetched, fields=["name"])
    after = await GStamped.get(id=fetched[0].id)
    assert after.name.endswith("!")
    assert after.updated_at != before  # auto_now bumped even though not listed

    # Explicitly listing the auto_now column is fine (it is not double-added).
    prev = after.updated_at
    for o in fetched:
        o.name = o.name + "?"
    await GStamped.bulk_update(fetched, fields=["name", "updated_at"])
    assert (await GStamped.get(id=fetched[0].id)).updated_at != prev


@pytest.mark.asyncio
async def test_json_contains_postgres(db):
    """
    GIVEN a JSON column (jsonb) holding arrays/objects
    WHEN filtered with __contains
    THEN it uses structural containment (@>) on PostgreSQL, and raises on SQLite
    """
    await GRec.create(data={"tags": ["a", "b"], "role": "admin"})
    if db == "sqlite":
        with pytest.raises(UnSupportedError):
            await GRec.filter(data__contains={"role": "admin"}).count()
        return
    assert await GRec.filter(data__contains={"role": "admin"}).count() == 1
    assert await GRec.filter(data__contains={"role": "guest"}).count() == 0
    assert await GRec.filter(data__contains={"tags": ["a"]}).count() == 1

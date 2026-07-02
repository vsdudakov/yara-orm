"""Query expressions and scalar functions.

Covers F() column references in filters and arithmetic updates, the DB
functions Lower/Upper/Length/Trim/Concat/Coalesce, and Case/When and RawSQL
annotations. Runs on every configured backend; the SQL is rendered portably
(Concat uses ``||``).
"""

import uuid

import pytest

from yara_orm import (
    Case,
    Coalesce,
    Concat,
    F,
    Length,
    Lower,
    Model,
    Q,
    RawSQL,
    Trim,
    Upper,
    Value,
    When,
    fields,
)


class ExprRow(Model):
    id = fields.IntField(pk=True)
    first = fields.CharField(max_length=20)
    last = fields.CharField(max_length=20, null=True)
    a = fields.IntField(default=0)
    b = fields.IntField(default=0)

    class Meta:
        table = "expr_row"


class ExOrder(Model):
    id = fields.IntField(pk=True)
    qty = fields.IntField()
    flag = fields.BooleanField(default=False)

    class Meta:
        table = "ex_order"


class ExParent(Model):
    id = fields.UUIDField(pk=True, default=uuid.uuid4)
    name = fields.CharField(max_length=20)

    class Meta:
        table = "ex_parent"


class ExChild(Model):
    id = fields.UUIDField(pk=True, default=uuid.uuid4)
    parent = fields.ForeignKeyField("ExParent", related_name="children")

    class Meta:
        table = "ex_child"


MODELS = [ExprRow, ExOrder, ExParent, ExChild]


@pytest.mark.asyncio
async def test_f_arithmetic_update(db):
    """
    GIVEN rows with integer columns
    WHEN update() assigns an F arithmetic expression
    THEN each row's column is updated relative to its own value
    """
    await ExprRow.create(first="x", a=5, b=3)
    await ExprRow.create(first="y", a=2, b=9)
    assert await ExprRow.all().update(a=F("a") + 10) == 2
    assert sorted(r.a for r in await ExprRow.all()) == [12, 15]


@pytest.mark.asyncio
async def test_f_column_to_column_update(db):
    """
    GIVEN a row with two integer columns
    WHEN update() assigns one column to another via F
    THEN the target column takes the source column's value
    """
    await ExprRow.create(first="x", a=7, b=1)
    await ExprRow.all().update(b=F("a"))
    row = await ExprRow.get(first="x")
    assert row.b == 7


@pytest.mark.asyncio
async def test_f_in_filter(db):
    """
    GIVEN rows comparing two columns
    WHEN filtering with a column referenced by F
    THEN only rows satisfying the column-to-column comparison match
    """
    await ExprRow.create(first="x", a=5, b=3)
    await ExprRow.create(first="y", a=2, b=9)
    rows = await ExprRow.filter(a__gt=F("b"))
    assert [r.first for r in rows] == ["x"]


@pytest.mark.asyncio
async def test_text_functions(db):
    """
    GIVEN a row with text columns
    WHEN annotating with Lower/Upper/Length/Trim
    THEN each function is applied per row
    """
    await ExprRow.create(first="  Ada  ", last="LOVELACE")
    [r] = await ExprRow.annotate(
        lo=Lower("last"), up=Upper("last"), n=Length("last"), tr=Trim("first")
    )
    assert r.lo == "lovelace"
    assert r.up == "LOVELACE"
    assert r.n == 8
    assert r.tr == "Ada"


@pytest.mark.asyncio
async def test_concat_function(db):
    """
    GIVEN a row with two text columns
    WHEN annotating with Concat
    THEN the columns are joined via the portable || operator
    """
    await ExprRow.create(first="Ada", last="Lovelace")
    [r] = await ExprRow.annotate(full=Concat("first", "last"))
    assert r.full == "AdaLovelace"


@pytest.mark.asyncio
async def test_coalesce_function(db):
    """
    GIVEN rows with a nullable column
    WHEN annotating with Coalesce and a fallback literal
    THEN NULL values fall back and present values are kept
    """
    await ExprRow.create(first="x", last=None)
    await ExprRow.create(first="y", last="real")
    rows = await ExprRow.annotate(v=Coalesce("last", "anon")).order_by("first")
    assert [r.v for r in rows] == ["anon", "real"]


@pytest.mark.asyncio
async def test_case_when_literal(db):
    """
    GIVEN rows with an integer column
    WHEN annotating with a Case of When arms and a default
    THEN each row takes the first matching arm's value (or the default)
    """
    await ExprRow.create(first="hi", a=95)
    await ExprRow.create(first="mid", a=75)
    await ExprRow.create(first="lo", a=40)
    rows = await ExprRow.annotate(
        grade=Case(When(a__gte=90, then="A"), When(a__gte=60, then="C"), default="F")
    ).order_by("-a")
    assert [r.grade for r in rows] == ["A", "C", "F"]


@pytest.mark.asyncio
async def test_case_when_with_f_value(db):
    """
    GIVEN rows with an integer column
    WHEN a Case arm's THEN value is an F column reference
    THEN matching rows take the column value and others take the default
    """
    await ExprRow.create(first="hi", a=95)
    await ExprRow.create(first="lo", a=40)
    rows = await ExprRow.annotate(bonus=Case(When(a__gte=90, then=F("a")), default=0)).order_by(
        "-a"
    )
    assert [r.bonus for r in rows] == [95, 0]


@pytest.mark.asyncio
async def test_raw_sql_annotation(db):
    """
    GIVEN a row with an integer column
    WHEN annotating with a RawSQL fragment
    THEN the fragment is evaluated per row
    """
    await ExprRow.create(first="x", a=21)
    [r] = await ExprRow.annotate(dbl=RawSQL("a * 2"))
    assert r.dbl == 42


@pytest.mark.asyncio
async def test_raw_sql_binds_params_instead_of_interpolating(db):
    """
    GIVEN a RawSQL fragment with ``?`` markers and matching params
    WHEN it is used as an annotation
    THEN each marker binds as a parameter (values never touch the SQL text)
    """
    await ExprRow.create(first="x", a=5)
    [r] = await ExprRow.annotate(shifted=RawSQL("a + ? * ?", [10, 2]))
    assert r.shifted == 25


def test_raw_sql_param_count_mismatch_raises():
    """
    GIVEN a RawSQL fragment whose ``?`` count differs from its params
    WHEN it is compiled
    THEN a clear ValueError is raised (no silent mis-binding)
    """
    from yara_orm.dialects import PostgresDialect

    with pytest.raises(ValueError, match="placeholder"):
        RawSQL("a + ?", [1, 2]).as_sql(None, PostgresDialect(), {}, [], 1)


def test_f_operators_build_expressions():
    """
    GIVEN an F column reference
    WHEN combined with every supported arithmetic operator (both operand sides)
    THEN each yields a CombinedExpression
    """
    from yara_orm.expressions import CombinedExpression

    for expr in (
        F("a") + 1,
        F("a") - 1,
        F("a") * 2,
        F("a") / 2,
        1 + F("a"),
        1 - F("a"),
        2 * F("a"),
    ):
        assert isinstance(expr, CombinedExpression)


@pytest.mark.asyncio
async def test_case_without_default(db):
    """
    GIVEN a Case with no default
    WHEN a row matches no arm
    THEN its annotation is NULL (the ELSE clause is omitted)
    """
    await ExprRow.create(first="hi", a=95)
    await ExprRow.create(first="lo", a=1)
    rows = await ExprRow.annotate(tag=Case(When(a__gte=90, then="A"))).order_by("-a")
    assert [r.tag for r in rows] == ["A", None]


@pytest.mark.asyncio
async def test_coalesce_numeric_default(db):
    """
    GIVEN a numeric column and a numeric Coalesce fallback
    WHEN the annotation renders
    THEN the numeric literal is inlined and the present value is returned
    """
    await ExprRow.create(first="x", a=7)
    [r] = await ExprRow.annotate(v=Coalesce("a", 0))
    assert r.v == 7


def test_q_connector_constants():
    """
    GIVEN the ``Q.AND`` / ``Q.OR`` constants
    WHEN a Q node's connector is compared to them
    THEN the constants exist and match the connector strings
    """
    assert Q.AND == "AND"
    assert Q.OR == "OR"
    assert (Q(a=1) | Q(b=2)).connector == Q.OR
    assert (Q(a=1) & Q(b=2)).connector == Q.AND


def test_relation_typing_aliases_are_subscriptable():
    """
    GIVEN the relation typing aliases re-exposed on ``fields``
    WHEN they are subscripted in an annotation position
    THEN each yields a real generic alias parameterised by the model
    """
    assert fields.ForeignKeyNullableRelation[ExParent] is not None
    assert fields.ReverseRelation["ExChild"] is not None
    assert fields.ManyToManyRelation[ExParent] is not None


@pytest.mark.asyncio
async def test_value_literal_in_case_annotation(db):
    """
    GIVEN a Case annotation whose default is wrapped in ``Value``
    WHEN the annotated query is evaluated
    THEN ``Value`` binds the literal and the CASE resolves correctly
    """
    await ExOrder.create(qty=5, flag=True)
    await ExOrder.create(qty=7, flag=False)

    rows = await ExOrder.annotate(
        bonus=Case(When(qty__gte=6, then=F("qty")), default=Value(0))
    ).order_by("qty")
    assert [r.bonus for r in rows] == [0, 7]


@pytest.mark.asyncio
async def test_await_prefetched_forward_fk(db):
    """
    GIVEN a child whose forward FK was eager-loaded via prefetch_related
    WHEN the forward relation is awaited (the ``await self.fk`` idiom)
    THEN awaiting the already-loaded relation returns the instance
    """
    p = await ExParent.create(name="root")
    await ExChild.create(parent=p)

    [child] = await ExChild.all().prefetch_related("parent")
    loaded = await child.parent
    assert loaded.id == p.id


@pytest.mark.asyncio
async def test_subquery_rejects_non_queryset(db):
    """
    GIVEN a Subquery wrapping something that is not a lazy QuerySet
    WHEN the owning query is executed
    THEN a clear TypeError is raised instead of an opaque attribute error
    """
    from yara_orm import Subquery

    with pytest.raises(TypeError, match="expects a QuerySet"):
        await ExprRow.exclude(id__in=Subquery(object()))

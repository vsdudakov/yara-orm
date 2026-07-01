"""WHERE-builder precedence and text-column scalar coercion (1.8.x follow-ups).

- an ``OR``-group (``Q(a) | Q(b)``) or a chained ``.filter()`` combined with
  keyword filters is wrapped in parentheses before being AND-joined, so the
  tighter-binding SQL ``AND`` cannot swallow the kwargs into one ``OR`` branch
  (``a OR (b AND c)`` — the precedence-corruption bug).
- a ``CharField``/``TextField`` filtered or populated with a non-``bool`` ``int``
  binds the value as ``str`` (Postgres rejects ``character varying = bigint``);
  ``bool``/``None``/``str`` pass through unchanged.
"""

import pytest

from yara_orm import Model, fields
from yara_orm.dialects import PostgresDialect
from yara_orm.queryset import Q


class WpRow(Model):
    id = fields.IntField(pk=True)
    a = fields.BooleanField(default=False)
    b = fields.BooleanField(default=False)
    country = fields.CharField(max_length=8)
    org = fields.IntField()

    class Meta:
        table = "wp_row"


class WpUser(Model):
    id = fields.IntField(pk=True)
    email = fields.CharField(max_length=64)

    class Meta:
        table = "wp_user"


MODELS = [WpRow, WpUser]


async def _seed_rows() -> dict[str, int]:
    """Create the discriminating rows and return their ids by label."""
    # (a OR b) AND country="US" AND org=1 selects exactly r1 and r4.
    r1 = await WpRow.create(a=True, b=False, country="US", org=1)  # a -> in
    r2 = await WpRow.create(a=True, b=False, country="CA", org=1)  # a but wrong country
    r3 = await WpRow.create(a=False, b=False, country="US", org=1)  # neither a nor b
    r4 = await WpRow.create(a=False, b=True, country="US", org=1)  # b -> in
    r5 = await WpRow.create(a=False, b=True, country="US", org=2)  # b but wrong org
    return {"r1": r1.id, "r2": r2.id, "r3": r3.id, "r4": r4.id, "r5": r5.id}


@pytest.mark.asyncio
async def test_positional_or_group_with_kwargs_is_parenthesised(db):
    """
    GIVEN rows where only (a OR b) AND country AND org should match
    WHEN a positional ``Q(a) | Q(b)`` OR-group is filtered alongside kwargs
    THEN the OR-group is parenthesised so the kwargs bind to every branch
    """
    ids = await _seed_rows()

    rows = await WpRow.filter(Q(a=True) | Q(b=True), country="US", org=1).order_by("id")
    got = {r.id for r in rows}

    # The pre-fix bug parsed this as ``a OR (b AND country AND org)`` and would
    # wrongly include r2 (a=True, wrong country).
    assert got == {ids["r1"], ids["r4"]}


@pytest.mark.asyncio
async def test_chained_or_group_after_kwargs_is_parenthesised(db):
    """
    GIVEN rows where only country AND org AND (a OR b) should match
    WHEN a chained ``.filter(Q(a) | Q(b))`` follows a keyword ``.filter()``
    THEN the chained OR-group is parenthesised, not merged into one branch
    """
    ids = await _seed_rows()

    rows = await WpRow.filter(country="US", org=1).filter(Q(a=True) | Q(b=True)).order_by("id")
    got = {r.id for r in rows}

    # The pre-fix bug parsed this as ``(country AND org AND a) OR b`` and would
    # wrongly include r5 (b=True, wrong org).
    assert got == {ids["r1"], ids["r4"]}


def test_or_group_with_kwargs_compiles_with_wrapping_parens():
    """
    GIVEN a positional OR-group combined with keyword filters
    WHEN the WHERE clause is compiled
    THEN each top-level condition is wrapped so AND cannot bind across the OR
    """
    where, _params, _idx = WpRow.filter(
        Q(a=True) | Q(b=True), country="US", org=1
    )._compile_conditions(PostgresDialect())

    assert where == (
        ' WHERE (("wp_row"."a" = $1) OR ("wp_row"."b" = $2)) '
        'AND ("wp_row"."country" = $3 AND "wp_row"."org" = $4)'
    )


def test_charfield_in_coerces_int_operand_to_text():
    """
    GIVEN a CharField filtered with a mix of ``int`` and ``str`` in ``__in``
    WHEN the condition is compiled
    THEN the int operand binds as ``str`` (Postgres rejects varchar = bigint)
    """
    _where, params, _idx = WpUser.filter(email__in=[101, "rep@x.com"])._compile_conditions(
        PostgresDialect()
    )

    assert params == ["101", "rep@x.com"]


def test_text_field_to_db_coerces_only_non_bool_ints():
    """
    GIVEN the text-column scalar coercion
    WHEN values of each kind are converted for binding
    THEN a non-bool ``int`` becomes ``str`` while bool/None/str pass through
    """
    for field in (fields.CharField(max_length=8), fields.TextField()):
        assert field.to_db(101) == "101"
        assert field.to_db(True) is True  # bool is an int subclass — left alone
        assert field.to_db(None) is None
        assert field.to_db("rep@x.com") == "rep@x.com"

"""Regression tests for two correctness fixes:

* DecimalField is bound as an exact NUMERIC value instead of being routed
  through ``float`` (which silently dropped precision for large/high-scale
  decimals); the Postgres dialect declares decimal columns as ``NUMERIC(p, s)``
  and SQLite as TEXT-affinity ``VARCHAR``.
* Database constraint violations surface as :class:`IntegrityError` (with the
  real server message) instead of a bare ``RuntimeError``.

Runs on every configured backend; the conversion and error-mapping paths differ
between them, so the per-backend column type and message are asserted directly.
"""

from decimal import Decimal

import pytest

from yara_orm import Model, fields
from yara_orm.connection import get_engine
from yara_orm.exceptions import IntegrityError

# Values chosen to exceed float64's 53-bit mantissa: each one is corrupted if
# the bind path passes through ``float`` at any stage.
PRECISE = [
    Decimal("1234567890.1234567890"),
    Decimal("9999999999.9999999999"),
    Decimal("0.0000000001"),
    Decimal("99999999999999999999.0"),
]


class DecAcct(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50, unique=True)
    balance = fields.DecimalField(max_digits=30, decimal_places=10, null=True)

    class Meta:
        table = "dec_acct"


MODELS = [DecAcct]


def test_to_db_keeps_decimal_exact():
    """
    GIVEN a DecimalField and high-precision Decimal (and string) inputs
    WHEN ``to_db`` is called on each
    THEN it yields an exact Decimal (and None for None), never a lossy float
    """
    f = fields.DecimalField(max_digits=30, decimal_places=10)
    for value in PRECISE:
        out = f.to_db(value)
        assert isinstance(out, Decimal)
        assert out == value
    assert f.to_db(None) is None
    assert f.to_db("12.34") == Decimal("12.34")


@pytest.mark.asyncio
async def test_decimal_precision(db):
    """
    GIVEN high-precision decimal values
    WHEN each is written and then re-read
    THEN every value survives the full write/read cycle unchanged
    """
    for i, value in enumerate(PRECISE):
        row = await DecAcct.create(name=f"v{i}", balance=value)
        got = (await DecAcct.get(id=row.id)).balance
        assert got == value, f"{value} corrupted to {got}"


@pytest.mark.asyncio
async def test_decimal_column_type(db):
    """
    GIVEN a DecimalField model whose schema is generated
    WHEN the column's declared type is inspected
    THEN PostgreSQL uses NUMERIC(p, s), MySQL DECIMAL(p, s) and SQLite a
    TEXT-affinity VARCHAR (never a lossy floating type)
    """
    engine = get_engine()
    if db in ("postgres", "mysql"):
        rows = await engine.fetch_rows(
            "SELECT data_type, numeric_precision, numeric_scale "
            "FROM information_schema.columns "
            "WHERE table_name = 'dec_acct' AND column_name = 'balance'"
        )
        data_type, precision, scale = rows[0]
        assert data_type.lower() == ("decimal" if db == "mysql" else "numeric")
        assert (int(precision), int(scale)) == (30, 10)
    else:
        rows = await engine.fetch_rows("SELECT sql FROM sqlite_master WHERE name = 'dec_acct'")
        assert "VARCHAR" in rows[0][0].upper()


@pytest.mark.asyncio
async def test_unique_violation_raises_integrity_error(db):
    """
    GIVEN a row already holding a unique name
    WHEN a second row with the same name is created
    THEN an IntegrityError is raised (carrying the real message on PostgreSQL)
    """
    await DecAcct.create(name="dup")
    with pytest.raises(IntegrityError) as exc:
        await DecAcct.create(name="dup")
    if db == "postgres":
        # The real server message is preserved (not the opaque "db error").
        assert "dec_acct" in str(exc.value) or "unique" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_not_null_violation_raises_integrity_error(db):
    """
    GIVEN a NOT NULL column
    WHEN a row omitting that column is inserted via raw SQL
    THEN an IntegrityError is raised, not a bare RuntimeError
    """
    with pytest.raises(IntegrityError):
        await get_engine().execute("INSERT INTO dec_acct (balance) VALUES (1.0)")

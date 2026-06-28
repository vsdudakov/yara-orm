"""Regression tests for two correctness fixes:

* DecimalField is bound as an exact NUMERIC value instead of being routed
  through ``float`` (which silently dropped precision for large/high-scale
  decimals), and the Postgres dialect declares decimal columns as
  ``NUMERIC(p, s)`` rather than ``DOUBLE PRECISION``.
* Database constraint violations surface as :class:`IntegrityError` (with the
  real server message) instead of a bare ``RuntimeError``.

Each case is exercised on both backends — SQLite via ``sqlite_db`` and
PostgreSQL via ``orm`` — because the conversion and error-mapping paths differ
between them.
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


async def _reset_pg():
    from yara_orm import YaraOrm

    await get_engine().execute("DROP TABLE IF EXISTS dec_acct CASCADE")
    await YaraOrm.generate_schemas()


# -- to_db unit (no DB) -----------------------------------------------------
def test_to_db_keeps_decimal_exact():
    """``to_db`` yields a Decimal, never a lossy float."""
    f = fields.DecimalField(max_digits=30, decimal_places=10)
    for value in PRECISE:
        out = f.to_db(value)
        assert isinstance(out, Decimal)
        assert out == value
    assert f.to_db(None) is None
    # A non-Decimal input is coerced exactly via its string form.
    assert f.to_db("12.34") == Decimal("12.34")


# -- decimal precision round-trip ------------------------------------------
@pytest.mark.asyncio
async def test_decimal_precision_sqlite(sqlite_db):
    """High-precision decimals survive a full write/read cycle on SQLite."""
    for i, value in enumerate(PRECISE):
        row = await DecAcct.create(name=f"s{i}", balance=value)
        got = (await DecAcct.get(id=row.id)).balance
        assert got == value, f"{value} corrupted to {got}"


@pytest.mark.asyncio
async def test_decimal_precision_postgres(orm):
    """High-precision decimals survive a full write/read cycle on PostgreSQL."""
    await _reset_pg()
    for i, value in enumerate(PRECISE):
        row = await DecAcct.create(name=f"p{i}", balance=value)
        got = (await DecAcct.get(id=row.id)).balance
        assert got == value, f"{value} corrupted to {got}"


@pytest.mark.asyncio
async def test_postgres_decimal_column_is_numeric(orm):
    """The generated Postgres column is NUMERIC(p, s), not DOUBLE PRECISION."""
    await _reset_pg()
    rows = await get_engine().fetch_rows(
        "SELECT data_type, numeric_precision, numeric_scale "
        "FROM information_schema.columns "
        "WHERE table_name = 'dec_acct' AND column_name = 'balance'"
    )
    data_type, precision, scale = rows[0]
    assert data_type == "numeric"
    assert (precision, scale) == (30, 10)


# -- integrity error fidelity ----------------------------------------------
@pytest.mark.asyncio
async def test_unique_violation_raises_integrity_error_sqlite(sqlite_db):
    """A duplicate unique key raises IntegrityError on SQLite."""
    await DecAcct.create(name="dup")
    with pytest.raises(IntegrityError):
        await DecAcct.create(name="dup")


@pytest.mark.asyncio
async def test_unique_violation_raises_integrity_error_postgres(orm):
    """A duplicate unique key raises IntegrityError on PostgreSQL, with message."""
    await _reset_pg()
    await DecAcct.create(name="dup")
    with pytest.raises(IntegrityError) as exc:
        await DecAcct.create(name="dup")
    # The real server message is preserved (not the opaque "db error").
    assert "dec_acct" in str(exc.value) or "unique" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_not_null_violation_raises_integrity_error_postgres(orm):
    """A NULL into a NOT NULL column raises IntegrityError, not RuntimeError."""
    await _reset_pg()
    with pytest.raises(IntegrityError):
        # name is NOT NULL; insert a row omitting it via raw SQL.
        await get_engine().execute("INSERT INTO dec_acct (balance) VALUES (1.0)")

"""Regression tests for decimals with more fractional digits than rust_decimal holds.

rust_decimal stores at most 28 decimal places, so a derived value such as
``1 / Decimal("162.35")`` (30 places) used to fail the bind with
``invalid decimal ...: Failed to parse`` — even though NUMERIC, and asyncpg
before the ORM swap, accept any scale. Two layers now handle it:

* ``DecimalField.to_db`` rounds to the field's ``decimal_places`` (half away
  from zero, as PostgreSQL rounds on insert), covering model writes.
* The engine binder rounds the excess fractional digits of any raw ``Decimal``
  parameter, covering raw SQL that bypasses field conversion.
"""

from decimal import Decimal, InvalidOperation

import pytest

from yara_orm import Model, fields
from yara_orm.connection import connections, get_dialect

INVERSE_RATE = Decimal(1) / Decimal("162.35")


class DsRate(Model):
    id = fields.IntField(pk=True)
    rate = fields.DecimalField(max_digits=20, decimal_places=10)

    class Meta:
        table = "ds_rate"


MODELS = [DsRate]


def test_inverse_rate_exceeds_rust_decimal_scale():
    """
    GIVEN the fixture rate used below
    WHEN its scale is inspected
    THEN it has more than 28 decimal places (the case that used to fail)
    """
    assert -INVERSE_RATE.as_tuple().exponent > 28


def test_to_db_rounds_to_decimal_places():
    """
    GIVEN a DecimalField(20, 10)
    WHEN ``to_db`` gets values beyond its scale, at a half-way point, and non-finite
    THEN it rounds to 10 places half away from zero and passes NaN/inf through
    """
    f = fields.DecimalField(max_digits=20, decimal_places=10)
    out = f.to_db(INVERSE_RATE)
    assert out == Decimal("0.0061595319")
    assert out.as_tuple().exponent == -10
    assert f.to_db(Decimal("0.00000000005")) == Decimal("0.0000000001")
    assert f.to_db(Decimal("-0.00000000005")) == Decimal("-0.0000000001")
    assert f.to_db("1.5") == Decimal("1.5000000000")
    assert f.to_db(Decimal("NaN")).is_nan()
    assert f.to_db(Decimal("Infinity")) == Decimal("Infinity")


def test_to_db_wide_field_does_not_hit_context_precision():
    """
    GIVEN a DecimalField wider than the default 28-digit decimal context
    WHEN ``to_db`` rounds a value that needs all 30 digits, including a carry
    THEN quantize succeeds instead of raising InvalidOperation
    """
    f = fields.DecimalField(max_digits=30, decimal_places=10)
    assert f.to_db(Decimal("99999999999999999999.0")) == Decimal("99999999999999999999.0")
    assert f.to_db(Decimal("99999999999999999999.99999999995")) == Decimal("1E+20")
    with pytest.raises(InvalidOperation):
        # The stock context would have raised here; keep that assumption honest.
        Decimal("99999999999999999999.0").quantize(Decimal("1E-10"))


@pytest.mark.asyncio
async def test_model_write_rounds_high_scale_decimal(db):
    """
    GIVEN a value with 30 decimal places
    WHEN it is saved through the model and re-read
    THEN the write succeeds and stores the value rounded to the column scale
    """
    row = await DsRate.create(rate=INVERSE_RATE)
    got = (await DsRate.get(id=row.id)).rate
    assert got == Decimal("0.0061595319")


@pytest.mark.asyncio
async def test_raw_sql_binds_high_scale_decimal(db):
    """
    GIVEN raw SQL that bypasses field conversion
    WHEN a 30-place Decimal is bound as a parameter
    THEN the engine rounds the excess digits instead of rejecting the value
    """
    ph = get_dialect(DsRate).placeholder
    conn = connections.get()
    await conn.execute_query(
        f"INSERT INTO ds_rate (id, rate) VALUES ({ph(1)}, {ph(2)})", [1, INVERSE_RATE]
    )
    got = (await DsRate.get(id=1)).rate
    if db == "sqlite":
        # TEXT storage has no column scale, so the binder's 28-place rounding
        # is what lands.
        assert got == Decimal("0.0061595318755774561133353865")
    else:
        assert got == Decimal("0.0061595319")


@pytest.mark.asyncio
async def test_raw_sql_binds_tiny_exponent_decimal(db):
    """
    GIVEN a Decimal whose ``str`` is in exponent form beyond 28 places (1E-40)
    WHEN it is bound as a raw parameter
    THEN it rounds to zero rather than failing to parse
    """
    ph = get_dialect(DsRate).placeholder
    conn = connections.get()
    await conn.execute_query(
        f"INSERT INTO ds_rate (id, rate) VALUES ({ph(1)}, {ph(2)})", [2, Decimal("1E-40")]
    )
    assert (await DsRate.get(id=2)).rate == 0


@pytest.mark.asyncio
async def test_raw_sql_rejects_out_of_range_decimal(db):
    """
    GIVEN Decimals the engine cannot represent (30 integer digits, NaN)
    WHEN they are bound as raw parameters
    THEN a ValueError names the value instead of silently changing it
    """
    ph = get_dialect(DsRate).placeholder
    conn = connections.get()
    for bad in (Decimal("123456789012345678901234567890"), Decimal("NaN")):
        with pytest.raises(ValueError, match="invalid decimal"):
            await conn.execute_query(
                f"INSERT INTO ds_rate (id, rate) VALUES ({ph(1)}, {ph(2)})", [3, bad]
            )

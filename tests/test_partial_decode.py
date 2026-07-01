"""Partial-selection hydration (``only()``/``defer()``) over non-identity fields.

Exercises the cached partial decode plan and its active-decoder loop for both the
batch (`_from_db_rows_fields`) and single-row (`_from_db_row_fields`, via a
partial ``select_related``) builders, including the NULL skip path.
"""

from decimal import Decimal

import pytest

from yara_orm import Model, fields


class PdAccount(Model):
    id = fields.IntField(pk=True)
    balance = fields.DecimalField(max_digits=12, decimal_places=2, null=True)
    name = fields.CharField(max_length=20, null=True)

    class Meta:
        table = "pd_account"


class PdEntry(Model):
    id = fields.IntField(pk=True)
    account = fields.ForeignKeyField("PdAccount", related_name="entries")

    class Meta:
        table = "pd_entry"


MODELS = [PdAccount, PdEntry]


@pytest.mark.asyncio
async def test_only_with_decimal_field_batch(db):
    """
    GIVEN an only() selection including a non-identity Decimal field
    WHEN rows (one with a value, one NULL) are fetched
    THEN the batch partial builder decodes the Decimal and skips NULL
    """
    await PdAccount.create(balance=Decimal("12.34"), name="a")
    await PdAccount.create(balance=None, name="b")

    rows = await PdAccount.all().only("id", "balance").order_by("id")
    assert rows[0].balance == Decimal("12.34")
    assert isinstance(rows[0].balance, Decimal)
    assert rows[1].balance is None


@pytest.mark.asyncio
async def test_defer_keeps_decimal_active(db):
    """
    GIVEN a defer() that keeps the Decimal field
    WHEN a row is fetched
    THEN the Decimal is decoded via the active-decoder path
    """
    await PdAccount.create(balance=Decimal("9.99"), name="c")
    row = (await PdAccount.all().defer("name"))[0]
    assert row.balance == Decimal("9.99")


@pytest.mark.asyncio
async def test_select_related_partial_decimal(db):
    """
    GIVEN a partial select_related projecting a related non-identity field
    WHEN rows (one with a value, one NULL) are fetched
    THEN the single-row partial builder decodes the related Decimal and skips NULL
    """
    a1 = await PdAccount.create(balance=Decimal("5.00"))
    a2 = await PdAccount.create(balance=None)
    await PdEntry.create(account=a1)
    await PdEntry.create(account=a2)

    entries = await PdEntry.all().only("account__balance").order_by("id")
    assert entries[0].account.balance == Decimal("5.00")
    assert entries[1].account.balance is None

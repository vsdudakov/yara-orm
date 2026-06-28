"""Enumeration fields: IntEnumField and CharEnumField."""

from enum import Enum, IntEnum

import pytest

from yara_orm import Model, YaraOrm, fields
from yara_orm.connection import get_engine


class Service(IntEnum):
    PYTHON = 1
    RUST = 2


class Currency(str, Enum):
    HUF = "HUF"
    USD = "USD"


class EnumAccount(Model):
    service = fields.IntEnumField(Service)
    currency = fields.CharEnumField(Currency, max_length=3, default=Currency.HUF)

    class Meta:
        table = "e_account"


async def _reset():
    engine = get_engine()
    await engine.execute("DROP TABLE IF EXISTS e_account CASCADE")
    await YaraOrm.generate_schemas()


@pytest.mark.asyncio
async def test_int_enum_roundtrip(orm):
    """
    GIVEN an IntEnumField storing an IntEnum
    WHEN an instance is created and re-read
    THEN the value round-trips back to the enum member
    """
    await _reset()
    acc = await EnumAccount.create(service=Service.RUST)
    reloaded = await EnumAccount.get(id=acc.id)
    assert reloaded.service is Service.RUST
    assert isinstance(reloaded.service, Service)


@pytest.mark.asyncio
async def test_char_enum_default_and_filter(orm):
    """
    GIVEN a CharEnumField with an enum default
    WHEN an instance is created without specifying it and filtered by enum
    THEN the default is applied and filtering by the enum member works
    """
    await _reset()
    await EnumAccount.create(service=Service.PYTHON)
    await EnumAccount.create(service=Service.PYTHON, currency=Currency.USD)

    huf = await EnumAccount.filter(currency=Currency.HUF)
    assert len(huf) == 1
    assert huf[0].currency is Currency.HUF

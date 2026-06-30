"""Validators, TimeDeltaField, the Signals enum and exception parity.

The validator classes and exception/enum checks are pure-Python; the field
validation and TimeDeltaField round-trip run on every configured backend.
"""

import datetime as dt
import re

import pytest

from yara_orm import (
    BaseORMException,
    IntegrityError,
    Model,
    OperationalError,
    ORMError,
    Signals,
    ValidationError,
    fields,
)
from yara_orm.validators import (
    MaxLengthValidator,
    MaxValueValidator,
    MinLengthValidator,
    MinValueValidator,
    RegexValidator,
    validate_ipv4_address,
    validate_ipv6_address,
    validate_ipv46_address,
)


class VdRow(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(
        max_length=20, validators=[MinLengthValidator(2), MaxLengthValidator(5)]
    )
    count = fields.IntField(default=0, validators=[MinValueValidator(0), MaxValueValidator(100)])
    note = fields.CharField(max_length=10, null=True, validators=[MinLengthValidator(2)])
    elapsed = fields.TimeDeltaField(null=True)

    class Meta:
        table = "vd_row"


MODELS = [VdRow]


def test_value_and_length_validators():
    """
    GIVEN the value/length validator classes
    WHEN called with in-range and out-of-range inputs
    THEN out-of-range inputs raise ValidationError and in-range ones pass
    """
    MinValueValidator(0)(0)
    MaxValueValidator(10)(10)
    MinLengthValidator(2)("ab")
    MaxLengthValidator(3)("abc")
    for bad in (
        lambda: MinValueValidator(1)(0),
        lambda: MaxValueValidator(1)(2),
        lambda: MinLengthValidator(2)("a"),
        lambda: MaxLengthValidator(2)("abc"),
    ):
        with pytest.raises(ValidationError):
            bad()


def test_regex_validator():
    """
    GIVEN a RegexValidator
    WHEN a matching and a non-matching string are validated
    THEN the non-matching string raises ValidationError
    """
    slug = RegexValidator(r"^[a-z]+$", re.IGNORECASE)
    slug("Hello")
    with pytest.raises(ValidationError):
        slug("has space")


def test_ip_validators():
    """
    GIVEN the IP-address validators
    WHEN valid and invalid addresses are validated
    THEN invalid addresses raise ValidationError
    """
    validate_ipv4_address("127.0.0.1")
    validate_ipv6_address("::1")
    validate_ipv46_address("127.0.0.1")
    validate_ipv46_address("::1")
    for bad in (
        lambda: validate_ipv4_address("999.0.0.1"),
        lambda: validate_ipv6_address("127.0.0.1"),
        lambda: validate_ipv46_address("nope"),
    ):
        with pytest.raises(ValidationError):
            bad()


def test_exception_hierarchy_parity():
    """
    GIVEN the exception classes
    WHEN their relationships are inspected
    THEN BaseORMException aliases ORMError and the exception hierarchy holds
    """
    assert BaseORMException is ORMError
    assert issubclass(OperationalError, ORMError)
    assert issubclass(IntegrityError, OperationalError)
    assert issubclass(ValidationError, ORMError)


def test_signals_enum():
    """
    GIVEN the Signals enum
    WHEN its members are listed
    THEN it names the four lifecycle signals
    """
    assert [s.value for s in Signals] == ["pre_save", "post_save", "pre_delete", "post_delete"]


@pytest.mark.asyncio
async def test_field_validators_run_on_save(db):
    """
    GIVEN a model whose fields carry validators
    WHEN instances with valid and invalid values are saved
    THEN valid values persist and invalid values raise ValidationError
    """
    await VdRow.create(name="ok", count=5)
    # A nullable validated field left None skips validation (no error).
    await VdRow.create(name="ok", count=5, note=None)
    with pytest.raises(ValidationError):
        await VdRow.create(name="waytoolong", count=1)  # MaxLength
    with pytest.raises(ValidationError):
        await VdRow.create(name="ok", count=-1)  # MinValue
    with pytest.raises(ValidationError):
        await VdRow.create(name="x", count=1)  # MinLength
    with pytest.raises(ValidationError):
        await VdRow.create(name="ok", note="z")  # note MinLength(2)


@pytest.mark.asyncio
async def test_timedelta_field_roundtrip(db):
    """
    GIVEN a TimeDeltaField holding a sub-second-precise duration
    WHEN the row is written and re-read
    THEN the timedelta round-trips exactly (and None stays None)
    """
    td = dt.timedelta(days=1, hours=2, minutes=3, seconds=4, microseconds=567)
    row = await VdRow.create(name="td", elapsed=td)
    got = (await VdRow.get(id=row.id)).elapsed
    assert got == td
    assert isinstance(got, dt.timedelta)

    none_row = await VdRow.create(name="no", elapsed=None)
    assert (await VdRow.get(id=none_row.id)).elapsed is None


def test_timedelta_field_conversions():
    """
    GIVEN a TimeDeltaField
    WHEN to_db/to_python receive a timedelta, an int, and None
    THEN a timedelta becomes microseconds, an int passes through, an already-
    timedelta value is returned as-is, and None stays None
    """
    f = fields.TimeDeltaField()
    assert f.to_db(dt.timedelta(seconds=1)) == 1_000_000
    assert f.to_db(500) == 500  # already-int passes through
    assert f.to_db(None) is None
    assert f.to_python(2_000_000) == dt.timedelta(seconds=2)
    already = dt.timedelta(minutes=1)
    assert f.to_python(already) is already  # already a timedelta
    assert f.to_python(None) is None

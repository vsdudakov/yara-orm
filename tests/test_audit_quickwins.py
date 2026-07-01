"""Regression tests for the post-1.8 audit quick-wins.

Each guards a specific fix surfaced by the library audit:
- ``SmallIntField.to_python`` now coerces DB values to ``int`` (it previously
  had no ``to_python``, so small ints read back as the raw driver value).
- ``CommaSeparatedIntegerListValidator`` rejects multi-dash tokens like
  ``"--5"`` (the old ``lstrip("-")`` stripped every leading dash).
- ``Record`` positional access caches the values tuple (correctness of the
  memoisation — repeated positional reads must stay stable).
"""

import pytest

from yara_orm import fields
from yara_orm.connection import Record
from yara_orm.exceptions import ValidationError
from yara_orm.validators import CommaSeparatedIntegerListValidator


def test_smallint_field_to_python_coerces_to_int():
    """
    GIVEN a SmallIntField
    WHEN a database value (or a numeric string) is converted to Python
    THEN it returns an ``int``, and ``None`` passes through
    """
    field = fields.SmallIntField()
    assert field.to_python("7") == 7
    assert isinstance(field.to_python("7"), int)
    assert field.to_python(7) == 7
    assert field.to_python(None) is None


def test_comma_separated_integer_validator_accepts_valid_and_signed():
    """
    GIVEN the comma-separated integer list validator
    WHEN a well-formed list (including negatives) is checked
    THEN it does not raise
    """
    validator = CommaSeparatedIntegerListValidator()
    validator("1,2,3")
    validator("-1, 2, -30")


@pytest.mark.parametrize("bad", ["--5", "1,--2,3", "1,,2", "1,-,2", "1,x,2"])
def test_comma_separated_integer_validator_rejects_malformed(bad):
    """
    GIVEN the comma-separated integer list validator
    WHEN a malformed token (double dash, empty, lone dash, non-digit) appears
    THEN it raises ValidationError
    """
    with pytest.raises(ValidationError):
        CommaSeparatedIntegerListValidator()(bad)


def test_record_positional_access_is_stable_and_matches_named():
    """
    GIVEN a Record wrapping an ordered row dict
    WHEN read positionally (and repeatedly, exercising the cached values tuple)
    THEN positional reads match column order and named access still works
    """
    row = Record({"id": 10, "name": "a", "org": 3})
    assert row[0] == 10
    assert row[0] == 10  # second read hits the cached values tuple
    assert row[2] == 3
    assert row[:2] == (10, "a")
    assert row["name"] == "a"

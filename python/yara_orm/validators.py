"""Field value validators.

Attach validators to a field via ``validators=[...]``; they run on ``save()``
and raise :class:`ValidationError` when a value is invalid.
"""

from __future__ import annotations

import ipaddress
import math
import re
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from .exceptions import ValidationError

if TYPE_CHECKING:
    from collections.abc import Sized


class Validator:
    """Base class for field validators; subclasses implement ``__call__``."""

    def __call__(self, value: Any) -> None:
        """Validate ``value``, raising :class:`ValidationError` on failure.

        Args:
            value: The field value to validate.

        Returns:
            None
        """
        raise NotImplementedError


class MinValueValidator(Validator):
    """Reject values below ``min_value``."""

    def __init__(self, min_value: Any) -> None:
        """Store the inclusive lower bound.

        Args:
            min_value: The smallest allowed value.

        Returns:
            None
        """
        self.min_value = min_value

    def __call__(self, value: Any) -> None:
        """Raise if ``value`` is below the bound.

        Args:
            value: The value to check.

        Returns:
            None
        """
        if value < self.min_value:
            raise ValidationError(f"{value} is less than {self.min_value}")


class MaxValueValidator(Validator):
    """Reject values above ``max_value``."""

    def __init__(self, max_value: Any) -> None:
        """Store the inclusive upper bound.

        Args:
            max_value: The largest allowed value.

        Returns:
            None
        """
        self.max_value = max_value

    def __call__(self, value: Any) -> None:
        """Raise if ``value`` is above the bound.

        Args:
            value: The value to check.

        Returns:
            None
        """
        if value > self.max_value:
            raise ValidationError(f"{value} is greater than {self.max_value}")


class MinLengthValidator(Validator):
    """Reject sequences shorter than ``min_length``."""

    def __init__(self, min_length: int) -> None:
        """Store the minimum length.

        Args:
            min_length: The smallest allowed length.

        Returns:
            None
        """
        self.min_length = min_length

    def __call__(self, value: Sized) -> None:
        """Raise if ``len(value)`` is below the minimum.

        Args:
            value: The sized value to check.

        Returns:
            None
        """
        if len(value) < self.min_length:
            raise ValidationError(f"Length {len(value)} is less than {self.min_length}")


class MaxLengthValidator(Validator):
    """Reject sequences longer than ``max_length``."""

    def __init__(self, max_length: int) -> None:
        """Store the maximum length.

        Args:
            max_length: The largest allowed length.

        Returns:
            None
        """
        self.max_length = max_length

    def __call__(self, value: Sized) -> None:
        """Raise if ``len(value)`` exceeds the maximum.

        Args:
            value: The sized value to check.

        Returns:
            None
        """
        if len(value) > self.max_length:
            raise ValidationError(f"Length {len(value)} is greater than {self.max_length}")


class RegexValidator(Validator):
    """Reject strings that do not match a regular expression."""

    def __init__(self, pattern: str, flags: int | re.RegexFlag = 0) -> None:
        """Compile the pattern.

        Args:
            pattern: The regular expression the value must match.
            flags: Optional ``re`` flags.

        Returns:
            None
        """
        self.regex = re.compile(pattern, flags)

    def __call__(self, value: str) -> None:
        """Raise if ``value`` does not match the pattern.

        Args:
            value: The string to check.

        Returns:
            None
        """
        if not self.regex.match(value):
            raise ValidationError(f"{value!r} does not match {self.regex.pattern!r}")


class NumericValidator(Validator):
    """Reject values that are not numeric (a number or a numeric string)."""

    def __call__(self, value: Any) -> None:
        """Raise if ``value`` is not numeric.

        Args:
            value: The value to check.

        Returns:
            None
        """
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
            raise ValidationError(f"{value!r} is not numeric")
        if isinstance(value, str):
            try:
                parsed = Decimal(value)
            except InvalidOperation as exc:
                raise ValidationError(f"{value!r} is not numeric") from exc
            # ``Decimal`` parses "nan"/"inf"/"Infinity"; a numeric column can
            # never hold those, so reject non-finite values here.
            if not parsed.is_finite():
                raise ValidationError(f"{value!r} is not a finite number")
        elif isinstance(value, Decimal):
            if not value.is_finite():
                raise ValidationError(f"{value!r} is not a finite number")
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValidationError(f"{value!r} is not a finite number")


class CommaSeparatedIntegerListValidator(Validator):
    """Reject strings that are not a comma-separated list of integers."""

    def __call__(self, value: Any) -> None:
        """Raise if ``value`` is not a comma-separated list of integers.

        Args:
            value: The string to check (e.g. ``"1,2,3"``).

        Returns:
            None
        """
        parts = str(value).split(",")
        for part in parts:
            token = part.strip()
            # A single optional leading sign then digits; ``lstrip("-")`` used to
            # accept ``"--5"`` (it strips every leading dash) — reject that here.
            body = token[1:] if token[:1] == "-" else token
            if not (body.isdigit() and body):
                raise ValidationError(f"{value!r} is not a comma-separated list of integers")


def validate_ipv4_address(value: str) -> None:
    """Validate that ``value`` is an IPv4 address.

    Args:
        value: The address string to validate.

    Returns:
        None
    """
    try:
        ipaddress.IPv4Address(value)
    except ValueError as exc:
        raise ValidationError(f"{value!r} is not a valid IPv4 address") from exc


def validate_ipv6_address(value: str) -> None:
    """Validate that ``value`` is an IPv6 address.

    Args:
        value: The address string to validate.

    Returns:
        None
    """
    try:
        ipaddress.IPv6Address(value)
    except ValueError as exc:
        raise ValidationError(f"{value!r} is not a valid IPv6 address") from exc


def validate_ipv46_address(value: str) -> None:
    """Validate that ``value`` is either an IPv4 or IPv6 address.

    Args:
        value: The address string to validate.

    Returns:
        None
    """
    try:
        ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValidationError(f"{value!r} is not a valid IPv4 or IPv6 address") from exc


__all__ = [
    "Validator",
    "MinValueValidator",
    "MaxValueValidator",
    "MinLengthValidator",
    "MaxLengthValidator",
    "RegexValidator",
    "NumericValidator",
    "CommaSeparatedIntegerListValidator",
    "validate_ipv4_address",
    "validate_ipv6_address",
    "validate_ipv46_address",
]

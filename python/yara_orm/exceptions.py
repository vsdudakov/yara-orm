"""Exception hierarchy for the ORM."""

from __future__ import annotations


class ORMError(Exception):
    """Base class for all ORM errors."""


class ConfigurationError(ORMError):
    """Raised when the ORM is used before being initialised, or misconfigured."""


class DoesNotExist(ORMError):
    """Raised by ``get()`` when no row matches."""


class MultipleObjectsReturned(ORMError):
    """Raised by ``get()`` when more than one row matches."""


class IntegrityError(ORMError):
    """Raised when a database integrity constraint is violated."""


class FieldError(ORMError):
    """Raised for invalid field declarations or lookups."""

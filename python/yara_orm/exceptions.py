"""Exception hierarchy for the ORM.

The names mirror Tortoise ORM so existing ``except`` blocks port across. The
common base is :class:`ORMError` (aliased as :class:`BaseORMException`).
"""

from __future__ import annotations


class ORMError(Exception):
    """Base class for all ORM errors."""


#: Alias matching Tortoise's base exception name.
BaseORMException = ORMError


class ConfigurationError(ORMError):
    """Raised when the ORM is used before being initialised, or misconfigured."""


class OperationalError(ORMError):
    """Raised for database operational failures (bad SQL, runtime errors)."""


class DBConnectionError(OperationalError):
    """Raised when a database connection cannot be established or is lost."""


class TransactionManagementError(OperationalError):
    """Raised for invalid transaction usage (commit/rollback out of order)."""


class NotExistOrMultiple(OperationalError):
    """Common base for the "no row" / "too many rows" lookup errors."""


class DoesNotExist(NotExistOrMultiple):
    """Raised by ``get()`` when no row matches."""


#: Alias matching Tortoise's ``ObjectDoesNotExistError`` name.
ObjectDoesNotExistError = DoesNotExist


class MultipleObjectsReturned(NotExistOrMultiple):
    """Raised by ``get()`` when more than one row matches."""


class IntegrityError(OperationalError):
    """Raised when a database integrity constraint is violated."""


class FieldError(ORMError):
    """Raised for invalid field declarations or lookups."""


class ParamsError(FieldError):
    """Raised when a method is called with invalid parameters."""


class ValidationError(FieldError):
    """Raised when a field value fails validation."""


class NoValuesFetched(OperationalError):
    """Raised when a relation is accessed before it has been fetched."""


class IncompleteInstanceError(ORMError):
    """Raised when an operation needs fields that were not loaded."""


class UnSupportedError(ORMError):
    """Raised when an operation is not supported by the active backend."""

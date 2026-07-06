"""yara_orm: an async Python ORM with an ergonomic API and a Rust engine.

Quick start::

    from yara_orm import YaraOrm, Model, fields

    class User(Model):
        id = fields.IntField(pk=True)
        name = fields.CharField(max_length=120)

    await YaraOrm.init("postgres://user:pass@localhost/db")
    await YaraOrm.generate_schemas()
    await User.create(name="Ada")
"""

from . import fields, migrations, timezone, validators
from .aggregations import Aggregate, Avg, Count, Max, Min, Sum
from .connection import (
    BaseDBAsyncClient,
    IsolationLevel,
    YaraOrm,
    clear_query_annotators,
    clear_query_hooks,
    connections,
    in_transaction,
    register_query_annotator,
    register_query_hook,
    run_async,
)
from .db_defaults import DatabaseDefault, Now, RandomHex, SqlDefault
from .dialects import BaseDialect, PostgresDialect, SqliteDialect, register_dialect
from .exceptions import (
    BaseORMException,
    ConfigurationError,
    DBConnectionError,
    DoesNotExist,
    FieldError,
    IncompleteInstanceError,
    IntegrityError,
    MultipleObjectsReturned,
    NotExistOrMultiple,
    NoValuesFetched,
    ObjectDoesNotExistError,
    OperationalError,
    ORMError,
    ParamsError,
    TransactionManagementError,
    UnSupportedError,
    ValidationError,
)
from .expressions import Array, Case, F, RawSQL, Subquery, Value, When
from .fields import register_field_kind, unregister_field_kind
from .functions import Coalesce, Concat, Length, Lower, Random, Trim, Upper
from .manager import Manager
from .migrations import CheckConstraint, MigrationManager, UniqueConstraint
from .models import Index, Model
from .prefetch import Prefetch
from .queryset import Q, QuerySet
from .signals import Signals, post_delete, post_save, pre_delete, pre_save
from .transactions import atomic

try:  # populated by maturin; absent only in source checkouts pre-build
    from ._engine import __version__ as _engine_version
except ImportError:  # pragma: no cover
    _engine_version = "unbuilt"

__version__ = "1.14.1"

__all__ = [
    "YaraOrm",
    "Model",
    "Index",
    "QuerySet",
    "Q",
    "F",
    "Case",
    "When",
    "RawSQL",
    "Subquery",
    "Value",
    "Array",
    "Now",
    "RandomHex",
    "SqlDefault",
    "DatabaseDefault",
    "fields",
    "register_field_kind",
    "unregister_field_kind",
    "migrations",
    "MigrationManager",
    "Manager",
    "Aggregate",
    "Count",
    "Sum",
    "Avg",
    "Min",
    "Max",
    "Lower",
    "Upper",
    "Length",
    "Trim",
    "Concat",
    "Coalesce",
    "Random",
    "UniqueConstraint",
    "CheckConstraint",
    "Prefetch",
    "validators",
    "timezone",
    "connections",
    "in_transaction",
    "register_query_hook",
    "clear_query_hooks",
    "register_query_annotator",
    "clear_query_annotators",
    "BaseDBAsyncClient",
    "atomic",
    "run_async",
    "IsolationLevel",
    "Signals",
    "pre_save",
    "post_save",
    "pre_delete",
    "post_delete",
    "BaseDialect",
    "PostgresDialect",
    "SqliteDialect",
    "register_dialect",
    "ORMError",
    "BaseORMException",
    "ConfigurationError",
    "OperationalError",
    "DBConnectionError",
    "TransactionManagementError",
    "NotExistOrMultiple",
    "DoesNotExist",
    "ObjectDoesNotExistError",
    "MultipleObjectsReturned",
    "IntegrityError",
    "FieldError",
    "ParamsError",
    "ValidationError",
    "NoValuesFetched",
    "IncompleteInstanceError",
    "UnSupportedError",
]

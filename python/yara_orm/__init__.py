"""yara_orm: an async Python ORM with a Tortoise-style API and a Rust engine.

Quick start::

    from yara_orm import YaraOrm, Model, fields

    class User(Model):
        id = fields.IntField(pk=True)
        name = fields.CharField(max_length=120)

    await YaraOrm.init("postgres://user:pass@localhost/db")
    await YaraOrm.generate_schemas()
    await User.create(name="Ada")
"""

from . import fields, migrations
from .aggregations import Avg, Count, Max, Min, Sum
from .connection import Tortoise, YaraOrm, connections, in_transaction
from .dialects import BaseDialect, PostgresDialect, SqliteDialect, register_dialect
from .exceptions import (
    ConfigurationError,
    DoesNotExist,
    FieldError,
    IntegrityError,
    MultipleObjectsReturned,
    ORMError,
)
from .expressions import Case, F, RawSQL, When
from .functions import Coalesce, Concat, Length, Lower, Trim, Upper
from .migrations import MigrationManager
from .models import Model
from .prefetch import Prefetch
from .queryset import Q, QuerySet
from .signals import post_delete, post_save, pre_delete, pre_save
from .transactions import atomic

try:  # populated by maturin; absent only in source checkouts pre-build
    from ._engine import __version__ as _engine_version
except ImportError:  # pragma: no cover
    _engine_version = "unbuilt"

__version__ = "0.1.0"

__all__ = [
    "YaraOrm",
    "Tortoise",
    "Model",
    "QuerySet",
    "Q",
    "F",
    "Case",
    "When",
    "RawSQL",
    "fields",
    "migrations",
    "MigrationManager",
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
    "Prefetch",
    "connections",
    "in_transaction",
    "atomic",
    "pre_save",
    "post_save",
    "pre_delete",
    "post_delete",
    "BaseDialect",
    "PostgresDialect",
    "SqliteDialect",
    "register_dialect",
    "ORMError",
    "ConfigurationError",
    "DoesNotExist",
    "MultipleObjectsReturned",
    "IntegrityError",
    "FieldError",
]

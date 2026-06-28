"""Field types.

A :class:`Field` describes one column. It carries an abstract *kind* (e.g.
``"int"``, ``"varchar"``) rather than a concrete SQL type; the active dialect
maps that kind onto database-specific DDL. This keeps every database-specific
decision in the dialect layer.
"""

from __future__ import annotations

import uuid as _uuid
from decimal import Decimal
from enum import Enum, IntEnum
from typing import Any


class Field:
    """Describe a single database column.

    A field carries an abstract :attr:`field_kind` rather than a concrete SQL
    type, leaving the active dialect to map it onto database-specific DDL.
    """

    #: Abstract type token resolved to concrete SQL by the dialect; every
    #: concrete field subclass overrides this with a non-empty token.
    field_kind: str = ""
    #: When True, the engine already returns this field's native Python type, so
    #: the read path can assign DB values directly and skip ``to_python``.
    read_identity: bool = True

    def __init__(
        self,
        *,
        pk: bool = False,
        null: bool = False,
        default: Any = None,
        unique: bool = False,
        index: bool = False,
        db_column: str | None = None,
        description: str | None = None,
    ) -> None:
        """Initialize the field with its column options.

        Args:
            pk: Whether this column is the primary key.
            null: Whether the column allows ``NULL`` values.
            default: Default value, or a callable producing one.
            unique: Whether the column carries a unique constraint.
            index: Whether to create an index on the column.
            db_column: Explicit column name; the metaclass fills it when blank.
            description: Human-readable comment emitted as a column COMMENT.

        Returns:
            None
        """
        self.pk = pk
        self.null = null
        self.default = default
        self.unique = unique
        self.index = index
        #: Column name; the metaclass fills this in when left blank.
        self.db_column: str = db_column or ""
        #: Human-readable comment, emitted as a column COMMENT in DDL.
        self.description = description
        #: Attribute name on the model; filled in by the metaclass.
        self.model_field_name: str = ""
        #: Whether the database assigns this column's value (serial pk).
        self.auto_increment = False
        #: Extra parameters consumed by the dialect type templates.
        self.type_params: dict[str, Any] = {}

    # -- value conversion -------------------------------------------------
    def get_default(self) -> Any:
        """Resolve the configured default value.

        Returns:
            The default value, invoking it first if it is callable.
        """
        return self.default() if callable(self.default) else self.default

    def to_db(self, value: Any) -> Any:
        """Convert a Python value into something the engine can bind.

        Args:
            value: The Python value to convert.

        Returns:
            A value suitable for binding to the database engine.
        """
        return value

    def to_python(self, value: Any) -> Any:
        """Convert a value returned by the engine into a Python value.

        Args:
            value: The value returned by the database engine.

        Returns:
            The corresponding Python value.
        """
        return value

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        """Return a debugging representation of the field."""
        return f"<{type(self).__name__} {self.model_field_name!r}>"


# ---------------------------------------------------------------------------
# Numeric
# ---------------------------------------------------------------------------
class SmallIntField(Field):
    """A small integer column."""

    field_kind = "smallint"

    def __init__(self, *, pk: bool = False, **kwargs: Any) -> None:
        """Initialize the field, enabling auto-increment for primary keys.

        Args:
            pk: Whether this column is the primary key.
            **kwargs: Additional options forwarded to :class:`Field`.

        Returns:
            None
        """
        super().__init__(pk=pk, **kwargs)
        if pk:
            self.auto_increment = True


class IntField(Field):
    """A standard integer column."""

    field_kind = "int"

    def __init__(self, *, pk: bool = False, **kwargs: Any) -> None:
        """Initialize the field, enabling auto-increment for primary keys.

        Args:
            pk: Whether this column is the primary key.
            **kwargs: Additional options forwarded to :class:`Field`.

        Returns:
            None
        """
        super().__init__(pk=pk, **kwargs)
        if pk:
            self.auto_increment = True

    def to_python(self, value: Any) -> Any:
        """Convert a database value into an ``int``.

        Args:
            value: The value returned by the database engine.

        Returns:
            The value as an ``int``, or ``None``.
        """
        return None if value is None else int(value)


class BigIntField(Field):
    """A 64-bit integer column."""

    field_kind = "bigint"

    def __init__(self, *, pk: bool = False, **kwargs: Any) -> None:
        """Initialize the field, enabling auto-increment for primary keys.

        Args:
            pk: Whether this column is the primary key.
            **kwargs: Additional options forwarded to :class:`Field`.

        Returns:
            None
        """
        super().__init__(pk=pk, **kwargs)
        if pk:
            self.auto_increment = True

    def to_python(self, value: Any) -> Any:
        """Convert a database value into an ``int``.

        Args:
            value: The value returned by the database engine.

        Returns:
            The value as an ``int``, or ``None``.
        """
        return None if value is None else int(value)


class FloatField(Field):
    """A floating-point column."""

    field_kind = "float"

    def to_python(self, value: Any) -> Any:
        """Convert a database value into a ``float``.

        Args:
            value: The value returned by the database engine.

        Returns:
            The value as a ``float``, or ``None``.
        """
        return None if value is None else float(value)


class DecimalField(Field):
    """A fixed-precision decimal column."""

    field_kind = "decimal"
    # Engine returns a float for our MVP NUMERIC mapping; convert on read.
    read_identity = False

    def __init__(self, max_digits: int = 12, decimal_places: int = 2, **kwargs: Any) -> None:
        """Initialize the field with its precision parameters.

        Args:
            max_digits: Total number of significant digits.
            decimal_places: Number of digits after the decimal point.
            **kwargs: Additional options forwarded to :class:`Field`.

        Returns:
            None
        """
        super().__init__(**kwargs)
        self.type_params = {"max_digits": max_digits, "decimal_places": decimal_places}

    def to_db(self, value: Any) -> Any:
        """Convert a value into a ``float`` for binding.

        Args:
            value: The Python value to convert.

        Returns:
            The value as a ``float``, or ``None``.
        """
        return None if value is None else float(value)

    def to_python(self, value: Any) -> Any:
        """Convert a database value into a ``Decimal``.

        Args:
            value: The value returned by the database engine.

        Returns:
            The value as a ``Decimal``, or ``None``.
        """
        return None if value is None else Decimal(str(value))


# ---------------------------------------------------------------------------
# Text / binary
# ---------------------------------------------------------------------------
class CharField(Field):
    """A variable-length string column."""

    field_kind = "varchar"

    def __init__(self, max_length: int = 255, **kwargs: Any) -> None:
        """Initialize the field with its maximum length.

        Args:
            max_length: Maximum number of characters allowed.
            **kwargs: Additional options forwarded to :class:`Field`.

        Returns:
            None
        """
        super().__init__(**kwargs)
        self.max_length = max_length
        self.type_params = {"max_length": max_length}


class TextField(Field):
    """An unbounded text column."""

    field_kind = "text"


class BinaryField(Field):
    """A binary (bytes) column."""

    field_kind = "bytes"


class BooleanField(Field):
    """A boolean column."""

    field_kind = "bool"

    def to_python(self, value: Any) -> Any:
        """Convert a database value into a ``bool``.

        Args:
            value: The value returned by the database engine.

        Returns:
            The value as a ``bool``, or ``None``.
        """
        return None if value is None else bool(value)


# ---------------------------------------------------------------------------
# Date / time
# ---------------------------------------------------------------------------
class DatetimeField(Field):
    """A date-and-time column."""

    field_kind = "datetime"

    def __init__(self, auto_now: bool = False, auto_now_add: bool = False, **kwargs: Any) -> None:
        """Initialize the field with its automatic-timestamp options.

        Args:
            auto_now: Whether to set the value to now on every save.
            auto_now_add: Whether to set the value to now on creation only.
            **kwargs: Additional options forwarded to :class:`Field`.

        Returns:
            None
        """
        super().__init__(**kwargs)
        self.auto_now = auto_now
        self.auto_now_add = auto_now_add


class DateField(Field):
    """A calendar-date column."""

    field_kind = "date"


class TimeField(Field):
    """A time-of-day column."""

    field_kind = "time"


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------
class UUIDField(Field):
    """A UUID column."""

    field_kind = "uuid"

    def __init__(self, *, pk: bool = False, **kwargs: Any) -> None:
        """Initialize the field, defaulting primary keys to ``uuid4``.

        Args:
            pk: Whether this column is the primary key.
            **kwargs: Additional options forwarded to :class:`Field`.

        Returns:
            None
        """
        if pk and kwargs.get("default") is None:
            kwargs["default"] = _uuid.uuid4
        super().__init__(pk=pk, **kwargs)

    def to_db(self, value: Any) -> Any:
        """Convert a value into a ``UUID`` for binding.

        Args:
            value: The Python value to convert.

        Returns:
            A ``UUID`` instance, or ``None``.
        """
        if value is None:
            return None
        return value if isinstance(value, _uuid.UUID) else _uuid.UUID(str(value))


class JSONField(Field):
    """A JSON column."""

    field_kind = "json"


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------
class IntEnumField(Field):
    """Stores an ``IntEnum`` as its integer value; reads back enum members."""

    field_kind = "int"
    read_identity = False

    def __init__(self, enum_type: type[IntEnum], **kwargs: Any) -> None:
        """Initialize the field with its enum type.

        Args:
            enum_type: The ``IntEnum`` subclass backing this column.
            **kwargs: Additional options forwarded to :class:`Field`.

        Returns:
            None
        """
        super().__init__(**kwargs)
        self.enum_type = enum_type

    def to_db(self, value: Any) -> Any:
        """Convert an enum member into its integer value.

        Args:
            value: An enum member or raw integer.

        Returns:
            The integer value, or ``None``.
        """
        if value is None:
            return None
        return int(value.value if isinstance(value, self.enum_type) else value)

    def to_python(self, value: Any) -> Any:
        """Convert a database integer into an enum member.

        Args:
            value: The integer returned by the database engine.

        Returns:
            The corresponding enum member, or ``None``.
        """
        return None if value is None else self.enum_type(value)


class CharEnumField(Field):
    """Stores a string ``Enum`` as its ``.value``; reads back enum members."""

    field_kind = "varchar"
    read_identity = False

    def __init__(self, enum_type: type[Enum], max_length: int = 255, **kwargs: Any) -> None:
        """Initialize the field with its enum type and length.

        Args:
            enum_type: The string ``Enum`` subclass backing this column.
            max_length: Maximum number of characters allowed.
            **kwargs: Additional options forwarded to :class:`Field`.

        Returns:
            None
        """
        super().__init__(**kwargs)
        self.enum_type = enum_type
        self.max_length = max_length
        self.type_params = {"max_length": max_length}

    def to_db(self, value: Any) -> Any:
        """Convert an enum member into its string value.

        Args:
            value: An enum member or raw string.

        Returns:
            The string value, or ``None``.
        """
        if value is None:
            return None
        return str(value.value if isinstance(value, self.enum_type) else value)

    def to_python(self, value: Any) -> Any:
        """Convert a database string into an enum member.

        Args:
            value: The string returned by the database engine.

        Returns:
            The corresponding enum member, or ``None``.
        """
        return None if value is None else self.enum_type(value)


# ---------------------------------------------------------------------------
# Relations
# ---------------------------------------------------------------------------
class OnDelete:
    """``ON DELETE`` referential actions (mirrors ``fields.OnDelete``)."""

    CASCADE = "CASCADE"
    RESTRICT = "RESTRICT"
    SET_NULL = "SET NULL"
    SET_DEFAULT = "SET DEFAULT"
    NO_ACTION = "NO ACTION"


class ForeignKeyField(Field):
    """A foreign key to another model.

    Declared under the relation name (e.g. ``tournament``); the metaclass
    synthesises a concrete ``<name>_id`` column and installs a forward accessor
    (``await obj.tournament``) plus a reverse manager (``related_name``) on the
    target model.
    """

    field_kind = "fk"
    is_relation = True
    is_m2m = False
    is_o2o = False

    def __init__(
        self,
        reference: str,
        related_name: str | None = None,
        on_delete: str = OnDelete.CASCADE,
        source_field: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the foreign key relation.

        Args:
            reference: Dotted path or name of the target model.
            related_name: Name of the reverse accessor on the target model.
            on_delete: Referential action applied on deletion.
            source_field: Target field referenced; defaults to its primary key.
            **kwargs: Additional options forwarded to :class:`Field`.

        Returns:
            None
        """
        super().__init__(**kwargs)
        self.reference = reference
        self.related_name = related_name
        self.on_delete = on_delete
        self.source_field = source_field


class OneToOneField(ForeignKeyField):
    """A unique foreign key; the reverse accessor yields a single instance."""

    is_o2o = True

    def __init__(self, reference: str, **kwargs: Any) -> None:
        """Initialize the one-to-one relation, enforcing uniqueness.

        Args:
            reference: Dotted path or name of the target model.
            **kwargs: Additional options forwarded to :class:`ForeignKeyField`.

        Returns:
            None
        """
        kwargs.setdefault("unique", True)
        super().__init__(reference, **kwargs)


class ManyToManyField(Field):
    """A many-to-many relation realised through a join table.

    No column is added to the owning table; the metaclass installs a manager
    supporting ``add``/``remove``/``clear`` and querying through the join table.
    """

    field_kind = "m2m"
    is_relation = True
    is_m2m = True
    is_o2o = False

    def __init__(
        self,
        reference: str,
        related_name: str | None = None,
        through: str | None = None,
        forward_key: str | None = None,
        backward_key: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the many-to-many relation.

        Args:
            reference: Dotted path or name of the target model.
            related_name: Name of the reverse accessor on the target model.
            through: Name of the join table; synthesised when omitted.
            forward_key: Join-table column referencing the owning model.
            backward_key: Join-table column referencing the target model.
            **kwargs: Additional options forwarded to :class:`Field`.

        Returns:
            None
        """
        super().__init__(**kwargs)
        self.reference = reference
        self.related_name = related_name
        self.through = through
        self.forward_key = forward_key
        self.backward_key = backward_key


__all__ = [
    "Field",
    "SmallIntField",
    "IntField",
    "BigIntField",
    "FloatField",
    "DecimalField",
    "CharField",
    "TextField",
    "BinaryField",
    "BooleanField",
    "DatetimeField",
    "DateField",
    "TimeField",
    "UUIDField",
    "JSONField",
    "IntEnumField",
    "CharEnumField",
    "OnDelete",
    "ForeignKeyField",
    "OneToOneField",
    "ManyToManyField",
]

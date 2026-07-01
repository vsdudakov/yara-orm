"""Field types.

A :class:`Field` describes one column. It carries an abstract *kind* (e.g.
``"int"``, ``"varchar"``) rather than a concrete SQL type; the active dialect
maps that kind onto database-specific DDL. This keeps every database-specific
decision in the dialect layer.
"""

from __future__ import annotations

import json
import uuid as _uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum, IntEnum
from typing import TYPE_CHECKING, Any

from .db_defaults import DatabaseDefault
from .exceptions import FieldError

if TYPE_CHECKING:
    from .validators import Validator


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
    #: Compatibility flag: every concrete yara field backs a real column, so
    #: code that branches on ``field.has_db_field`` keeps working.
    has_db_field: bool = True

    def __class_getitem__(cls, _item: Any) -> type:
        """Make field classes subscriptable for type annotations (no-op).

        Annotations like ``JSONField[list[dict] | None]`` are evaluated at
        class-definition time; returning the class keeps those annotations
        valid without affecting runtime behaviour.

        Args:
            _item: The (ignored) type argument.

        Returns:
            The field class itself.
        """
        return cls

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
        validators: list[Validator] | None = None,
        primary_key: bool | None = None,
        db_index: bool | None = None,
        source_field: str | None = None,
        db_default: Any = None,
        blank: bool | None = None,
        max_length: int | None = None,
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
            validators: Validators run against the value on ``save()``.
            primary_key: Modern alias for ``pk``.
            db_index: Modern alias for ``index``.
            source_field: Modern alias for ``db_column``.
            db_default: Modern alias for ``default``; used only when ``default``
                is not given.
            blank: Form-validation flag with no DB effect; accepted and ignored
                so existing ``blank=True`` declarations keep working.
            max_length: Accepted and ignored on fields that have no length (e.g.
                ``UUIDField``/``TextField``). ``CharField`` and ``CharEnumField``
                take their own ``max_length`` before this.

        Returns:
            None
        """
        # Accept the modern parameter spellings as aliases.
        if primary_key is not None:
            pk = primary_key
        if db_index is not None:
            index = db_index
        if source_field is not None:
            db_column = source_field
        if db_default is not None and default is None:
            default = db_default
        self.pk = pk
        self.null = null
        self.default = default
        self.unique = unique
        self.index = index
        #: Validators applied to the field's value before persisting.
        self.validators: list[Validator] = list(validators) if validators else []
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

    def __get__(self, instance: Any, owner: type | None = None) -> Any:
        """Non-data descriptor: guard access to columns not loaded into an instance.

        A normally-constructed or fully-fetched instance has every field in its
        ``__dict__``, so this descriptor is never consulted (``__dict__`` wins)
        and the hot path pays nothing. It fires only for an instance that omits
        the column — i.e. one produced by ``only()`` / ``defer()`` — turning a
        silent wrong value into a clear error.

        Args:
            instance: The instance being accessed, or None for class access.
            owner: The owning class.

        Raises:
            FieldError: When accessed on an instance that did not load this field.

        Returns:
            The field itself for class-level access.
        """
        if instance is None:
            return self
        raise FieldError(
            f"Field {self.model_field_name!r} was not loaded on this instance "
            f"(deferred via only()/defer()); re-fetch without deferring it to read it"
        )

    # -- value conversion -------------------------------------------------
    def get_default(self) -> Any:
        """Resolve the configured Python-side default value.

        Returns:
            The default value (invoked if callable), or ``None`` for a
            database-side default — the database supplies that value.
        """
        if isinstance(self.default, DatabaseDefault):
            return None
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

    def to_python_value(self, value: Any) -> Any:
        """Normalise a user-supplied value to the field's canonical Python type.

        Applied when a value is assigned on the model (``Model(...)`` /
        ``create(...)``), so the in-memory attribute matches what a fetched row
        would hold. The default is identity; fields that accept loose input
        (e.g. an ISO string for a date column) override it.

        Args:
            value: The value supplied by the caller.

        Returns:
            The value coerced to the field's canonical Python type.
        """
        return value

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        """Return a debugging representation of the field."""
        return f"<{type(self).__name__} {self.model_field_name!r}>"


# ---------------------------------------------------------------------------
# Numeric
# ---------------------------------------------------------------------------
def _int_to_db(value: Any) -> Any:
    """Coerce a numeric string to ``int`` before binding.

    So an integer column filtered with string values (``id__in={"1", "2"}``, a
    ``str(ext_id)`` from an external system) binds as ``int`` rather than text —
    otherwise PostgreSQL rejects it with 'operator does not exist: integer =
    text'. Non-string values (incl. ``bool``, ``F``, ``None``) pass through.

    Args:
        value: The Python value about to be bound.

    Returns:
        ``int(value)`` for a string, otherwise ``value`` unchanged.
    """
    return int(value) if isinstance(value, str) else value


def _str_to_db(value: Any) -> Any:
    """Coerce a non-string scalar to ``str`` before binding to a text column.

    The mirror of :func:`_int_to_db`: a ``varchar``/``text`` column filtered or
    populated with an ``int`` (``email__in=[101, "rep@x.com"]``, a numeric code
    stored as text) binds as ``str`` rather than an integer — otherwise
    PostgreSQL rejects it with 'operator does not exist: character varying =
    bigint'. ``bool`` (an ``int`` subclass), ``F``/``None`` and everything else
    pass through unchanged.

    Args:
        value: The Python value about to be bound.

    Returns:
        ``str(value)`` for a non-bool ``int``, otherwise ``value`` unchanged.
    """
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else value


class _IntegerField(Field):
    """Shared base for the integer column types.

    Centralises the primary-key auto-increment wiring and the ``int`` coercion
    both directions (numeric string in, ``int`` out); concrete subclasses only
    set their ``field_kind``.
    """

    def __init__(self, *, pk: bool = False, **kwargs: Any) -> None:
        """Initialize the field, enabling auto-increment for primary keys.

        Args:
            pk: Whether this column is the primary key.
            **kwargs: Additional options forwarded to :class:`Field`.

        Returns:
            None
        """
        super().__init__(pk=pk, **kwargs)
        if self.pk:  # honors the `primary_key=` alias reconciled by the base Field
            self.auto_increment = True

    def to_db(self, value: Any) -> Any:
        """Coerce a numeric string to ``int`` before binding.

        Args:
            value: The Python value to convert.

        Returns:
            ``int(value)`` for a string, otherwise ``value`` unchanged.
        """
        return _int_to_db(value)

    def to_python(self, value: Any) -> Any:
        """Convert a database value into an ``int``.

        Args:
            value: The value returned by the database engine.

        Returns:
            The value as an ``int``, or ``None``.
        """
        return None if value is None else int(value)


class SmallIntField(_IntegerField):
    """A small integer column."""

    field_kind = "smallint"


class IntField(_IntegerField):
    """A standard integer column."""

    field_kind = "int"


class BigIntField(_IntegerField):
    """A 64-bit integer column."""

    field_kind = "bigint"


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
    # PostgreSQL returns a native ``Decimal`` (NUMERIC), so the read-path
    # ``to_python`` short-circuits it; SQLite stores decimals as text (VARCHAR
    # affinity keeps them exact), so there ``to_python`` reconstructs the
    # ``Decimal`` from the string — hence not read-identity.
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
        """Convert a value into a ``Decimal`` for binding.

        The engine binds ``Decimal`` straight to a NUMERIC parameter, so values
        are kept exact instead of being routed through ``float`` (which would
        silently lose precision for large or high-scale decimals).

        Args:
            value: The Python value to convert.

        Returns:
            The value as a ``Decimal``, or ``None``.
        """
        if value is None:
            return None
        return value if isinstance(value, Decimal) else Decimal(str(value))

    def to_python(self, value: Any) -> Any:
        """Convert a database value into a ``Decimal``.

        A value already decoded as a ``Decimal`` by the engine (PostgreSQL
        NUMERIC) is returned unchanged, skipping a redundant ``str`` round-trip;
        a text value (SQLite) is reconstructed exactly.

        Args:
            value: The value returned by the database engine.

        Returns:
            The value as a ``Decimal``, or ``None``.
        """
        if value is None or isinstance(value, Decimal):
            return value
        return Decimal(str(value))


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

    def to_db(self, value: Any) -> Any:
        """Coerce a non-string scalar (e.g. ``int``) to ``str`` before binding.

        Args:
            value: The Python value to convert.

        Returns:
            ``str(value)`` for a non-bool ``int``, otherwise ``value`` unchanged.
        """
        return _str_to_db(value)


class TextField(Field):
    """An unbounded text column."""

    field_kind = "text"

    def to_db(self, value: Any) -> Any:
        """Coerce a non-string scalar (e.g. ``int``) to ``str`` before binding.

        Args:
            value: The Python value to convert.

        Returns:
            ``str(value)`` for a non-bool ``int``, otherwise ``value`` unchanged.
        """
        return _str_to_db(value)


class BinaryField(Field):
    """A binary (bytes) column."""

    field_kind = "bytes"


class BooleanField(Field):
    """A boolean column."""

    field_kind = "bool"

    def to_db(self, value: Any) -> Any:
        """Coerce the Python value to a ``bool`` before binding.

        Coerces with ``bool(value)`` so truthy non-bool inputs (``1``/``0``, a
        non-empty string) round-trip instead of reaching the engine as a type
        the boolean column rejects.

        Args:
            value: The Python value to convert.

        Returns:
            ``None`` when value is ``None``, otherwise ``bool(value)``.
        """
        return None if value is None else bool(value)

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
def _parse_iso_datetime(value: str) -> datetime:
    """Parse an ISO-8601 string into a :class:`datetime`.

    Accepts a trailing ``Z`` (UTC) by rewriting it to ``+00:00`` so it parses
    on every supported Python version.

    Args:
        value: The ISO-8601 timestamp string.

    Returns:
        The parsed ``datetime``.
    """
    text = value.strip()
    if text and text[-1] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


class _TemporalField(Field):
    """Shared base for date/time columns.

    Assignment coercion and bind coercion are the same for these types, so
    ``to_db`` delegates to ``to_python_value`` once here; subclasses only define
    ``to_python_value``.
    """

    def to_db(self, value: Any) -> Any:
        """Coerce the value the same way assignment does before binding.

        Args:
            value: The Python value to convert.

        Returns:
            The value coerced by ``to_python_value``.
        """
        return self.to_python_value(value)


class DatetimeField(_TemporalField):
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

    def to_python_value(self, value: Any) -> Any:
        """Coerce an ISO-8601 string to a ``datetime``.

        Values often arrive as ISO strings (e.g. from a JSON layer); coercing
        on assignment keeps the in-memory attribute a ``datetime`` (and binding
        a string verbatim would otherwise reach the engine as text, which the
        timestamp column rejects). Non-string values (incl. a bare ``date``,
        which the database implicitly casts) pass through unchanged.

        Args:
            value: The value supplied by the caller.

        Returns:
            A ``datetime`` parsed from a string; otherwise ``value`` unchanged.
        """
        if isinstance(value, str):
            return _parse_iso_datetime(value)
        return value


class DateField(_TemporalField):
    """A calendar-date column."""

    field_kind = "date"

    def to_python_value(self, value: Any) -> Any:
        """Coerce an ISO-8601 string (or ``datetime``) to a ``date``.

        Args:
            value: The value supplied by the caller.

        Returns:
            A ``date`` (or ``None``); other types pass through unchanged.
        """
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            text = value.strip()
            # A full timestamp string ("2026-07-01T..." / "2026-07-01 ...") is
            # parsed as a datetime first, then narrowed to its date part.
            if "T" in text or " " in text:
                return _parse_iso_datetime(text).date()
            return date.fromisoformat(text)
        return value


class TimeField(_TemporalField):
    """A time-of-day column."""

    field_kind = "time"

    def to_python_value(self, value: Any) -> Any:
        """Coerce an ISO-8601 string (or ``datetime``) to a ``time``.

        Args:
            value: The value supplied by the caller.

        Returns:
            A ``time`` (or ``None``); other types pass through unchanged.
        """
        if value is None or isinstance(value, time):
            return value
        if isinstance(value, datetime):
            return value.timetz()
        if isinstance(value, str):
            return time.fromisoformat(value.strip())
        return value


class TimeDeltaField(Field):
    """A duration column, stored as an integer number of microseconds."""

    field_kind = "timedelta"
    read_identity = False

    def to_db(self, value: Any) -> Any:
        """Convert a ``timedelta`` to its total microseconds for binding.

        Args:
            value: The Python value to convert.

        Returns:
            The duration as an ``int`` of microseconds, or ``None``.
        """
        if value is None:
            return None
        if isinstance(value, timedelta):
            return (value.days * 86400 + value.seconds) * 1_000_000 + value.microseconds
        return int(value)

    def to_python(self, value: Any) -> Any:
        """Convert stored microseconds back into a ``timedelta``.

        Args:
            value: The value returned by the database engine.

        Returns:
            The duration as a ``timedelta``, or ``None``.
        """
        if value is None or isinstance(value, timedelta):
            return value
        return timedelta(microseconds=int(value))


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
        # Honour the ``primary_key=`` spelling too: without this, a
        # ``UUIDField(primary_key=True)`` would skip the ``uuid4`` default and
        # insert a NULL id (NOT-NULL violation).
        is_pk = pk or bool(kwargs.get("primary_key"))
        if is_pk and kwargs.get("default") is None and kwargs.get("db_default") is None:
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
    """A JSON column.

    ``encoder``/``decoder`` are optional Python value-transform hooks:
    ``encoder`` runs on the Python value before it is handed to the engine to
    serialise, and ``decoder`` runs on the value read back. Rather than storing
    whatever string the encoder produced, yara serialises JSON in its engine,
    so these are value→value transforms (e.g. to make oversized integers
    JS-safe), not full (de)serialisers.
    """

    field_kind = "json"
    read_identity = False

    def __init__(
        self,
        *,
        encoder: Any = None,
        decoder: Any = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the JSON field with optional value-transform hooks.

        Args:
            encoder: Optional callable applied to the Python value before the
                engine serialises it.
            decoder: Optional callable applied to the value read back from the
                engine.
            **kwargs: Additional options forwarded to :class:`Field`.

        Returns:
            None
        """
        super().__init__(**kwargs)
        self.encoder = encoder
        self.decoder = decoder
        # No transforms → the engine handles JSON natively on both paths.
        self.read_identity = decoder is None

    def to_db(self, value: Any) -> Any:
        """Apply the encode hook (if any) before the engine serialises.

        With no ``encoder`` the value is bound as-is: the engine's JSON encoder
        coerces the exotic stdlib types apps store in a ``JSONField``
        (UUID/Decimal/datetime/date/time/bytes/set/enum) to their JSON form in a
        single native pass — no Python pre-walk. An ``encoder`` that returns a
        serialised JSON *string* is parsed back to a native value (the engine
        serialises JSON itself, so binding the string verbatim would corrupt a
        ``jsonb`` column).

        Args:
            value: The Python value to convert.

        Returns:
            The (optionally transformed) value to bind.
        """
        if value is None:
            return value
        if self.encoder is not None:
            encoded = self.encoder(value)
            return json.loads(encoded) if isinstance(encoded, str) else encoded
        return value

    def to_python(self, value: Any) -> Any:
        """Apply the decode hook (if any) to a value read from the engine.

        Args:
            value: The value returned by the database engine.

        Returns:
            The (optionally transformed) Python value.
        """
        if value is None or self.decoder is None:
            return value
        return self.decoder(value)


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
        reference: str | None = None,
        related_name: str | None = None,
        on_delete: str = OnDelete.CASCADE,
        source_field: str | None = None,
        db_constraint: bool = True,
        to: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the foreign key relation.

        Args:
            reference: Dotted path or name of the target model.
            related_name: Name of the reverse accessor on the target model.
            on_delete: Referential action applied on deletion.
            source_field: Explicit name for this table's FK column; defaults to
                ``<name>_id``. The referenced target column is always the target
                model's primary key.
            db_constraint: Whether to emit a database ``FOREIGN KEY`` constraint
                (set ``False`` to keep the column without enforcing referential
                integrity at the database level).
            to: Modern alias for ``reference``.
            **kwargs: Additional options forwarded to :class:`Field`.

        Returns:
            None
        """
        if to is not None:
            reference = to
        if reference is None:
            raise TypeError("ForeignKeyField requires a target model (pass reference= or to=)")
        super().__init__(**kwargs)
        self.reference = reference
        self.related_name = related_name
        self.on_delete = on_delete
        self.source_field = source_field
        self.db_constraint = db_constraint
        #: Cached pk field of the target model, used to coerce bound values.
        self._target_pk_field: Field | None = None

    def to_db(self, value: Any) -> Any:
        """Coerce a foreign-key value to the target primary key's type.

        The metaclass uses this field as the ``<name>_id`` backing column, so a
        value assigned as ``str`` (e.g. ``str(instance.id)``, common in caller
        code) must be coerced to the target pk's Python type (e.g. ``UUID``)
        before binding, or the engine rejects the binary format. Non-string
        values and int-pk targets pass through unchanged.

        Args:
            value: The Python value to convert.

        Returns:
            A value suitable for binding, coerced via the target pk field.
        """
        if value is None:
            return None
        target_pk = self._resolve_target_pk_field()
        if target_pk is not None:
            return target_pk.to_db(value)
        return value

    def _resolve_target_pk_field(self) -> Field | None:
        """Resolve and cache the referenced model's primary-key field.

        Returns:
            The target model's pk field, or None if it cannot be resolved yet
            (e.g. relations are not registered).
        """
        if self._target_pk_field is not None:
            return self._target_pk_field
        # Lazy import avoids a circular import at module load.
        from . import registry
        from .relations import model_name

        try:
            target = registry.get_model(model_name(self.reference))
        except KeyError:
            return None
        self._target_pk_field = target._meta.pk_field
        return self._target_pk_field


class OneToOneField(ForeignKeyField):
    """A unique foreign key; the reverse accessor yields a single instance."""

    is_o2o = True

    def __init__(self, reference: str | None = None, **kwargs: Any) -> None:
        """Initialize the one-to-one relation, enforcing uniqueness.

        Args:
            reference: Dotted path or name of the target model (or pass ``to=``).
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
        reference: str | None = None,
        related_name: str | None = None,
        through: str | None = None,
        forward_key: str | None = None,
        backward_key: str | None = None,
        through_fields: tuple[str, str] | None = None,
        to: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the many-to-many relation.

        Args:
            reference: Dotted path or name of the target model.
            related_name: Name of the reverse accessor on the target model.
            through: Name of the join table; synthesised when omitted.
            forward_key: Join-table column referencing the owning model.
            backward_key: Join-table column referencing the target model.
            through_fields: Alternate spelling of ``(forward_key, backward_key)``;
                used to fill those when they are not given explicitly.
            to: Modern alias for ``reference``.
            **kwargs: Additional options forwarded to :class:`Field`.

        Returns:
            None
        """
        if to is not None:
            reference = to
        if through_fields is not None:
            forward_key = forward_key or through_fields[0]
            backward_key = backward_key or through_fields[1]
        if reference is None:
            raise TypeError("ManyToManyField requires a target model (pass reference= or to=)")
        super().__init__(**kwargs)
        self.reference = reference
        self.related_name = related_name
        self.through = through
        self.forward_key = forward_key
        self.backward_key = backward_key


class _RelationHint:
    """Subscriptable no-op standing in for relation type hints.

    Relation attributes may be annotated as ``ForeignKeyRelation[X]`` /
    ``ReverseRelation[X]`` etc. (evaluated at class-definition time). yara derives
    accessors from the FK declaration, so these are annotation-only: subscripting
    returns ``None`` so the annotation is harmless at runtime.
    """

    def __class_getitem__(cls, _item: Any) -> None:
        """Return ``None`` for any subscription (annotation-only).

        Args:
            _item: The (ignored) type argument.

        Returns:
            None
        """
        return None


# Relation typing generics, re-exposed so existing annotations like
# ``ForeignKeyNullableRelation[BillingPlan]`` keep importing and evaluating.
ForeignKeyRelation = _RelationHint
ForeignKeyNullableRelation = _RelationHint
OneToOneRelation = _RelationHint
OneToOneNullableRelation = _RelationHint
ReverseRelation = _RelationHint
ManyToManyRelation = _RelationHint


# The on-delete actions are also exposed as bare module-level names
# (``fields.CASCADE`` / ``fields.SET_NULL`` ...). Re-expose them as aliases of
# the ``OnDelete`` members so existing FK declarations keep working.
CASCADE = OnDelete.CASCADE
RESTRICT = OnDelete.RESTRICT
SET_NULL = OnDelete.SET_NULL
SET_DEFAULT = OnDelete.SET_DEFAULT
NO_ACTION = OnDelete.NO_ACTION


__all__ = [
    "CASCADE",
    "RESTRICT",
    "SET_NULL",
    "SET_DEFAULT",
    "NO_ACTION",
    "ForeignKeyRelation",
    "ForeignKeyNullableRelation",
    "OneToOneRelation",
    "OneToOneNullableRelation",
    "ReverseRelation",
    "ManyToManyRelation",
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
    "TimeDeltaField",
    "UUIDField",
    "JSONField",
    "IntEnumField",
    "CharEnumField",
    "OnDelete",
    "ForeignKeyField",
    "OneToOneField",
    "ManyToManyField",
]

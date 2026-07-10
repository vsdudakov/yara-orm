"""Field types.

A :class:`Field` describes one column. It carries an abstract *kind* (e.g.
``"int"``, ``"varchar"``) rather than a concrete SQL type; the active dialect
maps that kind onto database-specific DDL. This keeps every database-specific
decision in the dialect layer.
"""

from __future__ import annotations

import copy
import json
import uuid as _uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum, IntEnum
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast

from . import timezone as _tz
from .db_defaults import DatabaseDefault
from .exceptions import ConfigurationError, FieldError

#: The Python value type a field's instance access resolves to (e.g. ``int``
#: for an ``IntField``, ``int | None`` for a nullable one).
VT = TypeVar("VT")

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Literal, overload

    from typing_extensions import Self

    #: The enum class an enum field stores (type-checking only; the ``__new__``
    #: overloads referencing these never execute at runtime).
    _EnumT = TypeVar("_EnumT", bound=Enum)
    _IntEnumT = TypeVar("_IntEnumT", bound=IntEnum)

    from .relations import (
        ForeignKeyNullableRelation as ForeignKeyNullableRelation,
    )
    from .relations import (
        ForeignKeyRelation as ForeignKeyRelation,
    )
    from .relations import (
        ManyToManyRelation as ManyToManyRelation,
    )
    from .relations import (
        OneToOneNullableRelation as OneToOneNullableRelation,
    )
    from .relations import (
        OneToOneRelation as OneToOneRelation,
    )
    from .relations import (
        ReverseRelation as ReverseRelation,
    )
    from .validators import Validator

#: Relation typing aliases re-exported from ``relations`` (lazily, via the
#: module ``__getattr__`` below — ``relations`` imports this module's field
#: classes, so a top-level import here would be circular).
_RELATION_TYPE_EXPORTS = frozenset(
    {
        "ForeignKeyRelation",
        "ForeignKeyNullableRelation",
        "OneToOneRelation",
        "OneToOneNullableRelation",
        "ReverseRelation",
        "ManyToManyRelation",
    }
)


def __getattr__(name: str) -> Any:
    """Resolve relation typing aliases and registered field classes lazily (PEP 562).

    Registered custom field classes (see :func:`register_field_kind`) resolve
    here so generated migration files can round-trip a custom field as
    ``fields.<ClassName>(...)``.

    Args:
        name: The attribute being looked up on the module.

    Returns:
        The alias from :mod:`yara_orm.relations`, or a registered field class.

    Raises:
        AttributeError: For any other missing module attribute.
    """
    if name in _RELATION_TYPE_EXPORTS:
        from . import relations

        return getattr(relations, name)
    registered = _REGISTERED_FIELD_CLASSES.get(name)
    if registered is not None:
        return registered
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class Field(Generic[VT]):
    """Describe a single database column.

    A field carries an abstract :attr:`field_kind` rather than a concrete SQL
    type, leaving the active dialect to map it onto database-specific DDL.

    Generic over ``VT``, the Python value type instance access resolves to
    (``Field[VT]`` being a real ``Generic`` also keeps annotations like
    ``JSONField[list[dict] | None]`` valid). To the type checker the field is
    a typed data descriptor (see the ``TYPE_CHECKING`` block below); at
    runtime it stays a *non-data* descriptor — only ``__get__``, the
    deferred-column guard — so instance ``__dict__`` keeps winning attribute
    lookup and the hydration/read hot path pays nothing.
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
        #: Set by the metaclass on the ``id`` pk it synthesises when a model
        #: declares none, so a subclass that later declares its own pk can tell
        #: the injected default apart from a user-declared field and drop it.
        self._auto_pk = False
        #: Extra parameters consumed by the dialect type templates.
        self.type_params: dict[str, int] = {}

    if TYPE_CHECKING:
        # Typed descriptor protocol, visible to the type checker only: class
        # access reveals the field object itself, instance access reveals the
        # field's value type, and assignment is checked against it. The typed
        # ``__set__`` must NOT exist at runtime — a data descriptor would beat
        # instance ``__dict__`` and break value storage and the read fast path.
        @overload
        def __get__(self, instance: None, owner: type | None = None) -> Self: ...
        @overload
        def __get__(self, instance: object, owner: type | None = None) -> VT: ...
        def __get__(self, instance: object | None, owner: type | None = None) -> Self | VT:
            """Reveal the field for class access, its value type for instances."""
            ...

        def __set__(self, instance: object, value: VT) -> None:
            """Check assigned values against the field's value type."""
            ...
    else:

        def __get__(self, instance, owner=None):
            """Non-data descriptor: guard access to columns not loaded into an instance.

            A normally-constructed or fully-fetched instance has every field in
            its ``__dict__``, so this descriptor is never consulted (``__dict__``
            wins) and the hot path pays nothing. It fires only for an instance
            that omits the column — i.e. one produced by ``only()`` / ``defer()``
            — turning a silent wrong value into a clear error.

            Args:
                instance: The instance being accessed, or None for class access.
                owner: The owning class.

            Raises:
                FieldError: When accessed on an instance that did not load this
                    field.

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
            database-side default — the database supplies that value. A mutable
            default (``dict``/``list``/``set``) is deep-copied so instances
            never share (and mutate) one object.
        """
        if isinstance(self.default, DatabaseDefault):
            return None
        if callable(self.default):
            return self.default()
        if isinstance(self.default, (dict, list, set)):
            return copy.deepcopy(self.default)
        return self.default

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
# Custom field kinds
# ---------------------------------------------------------------------------
#: The abstract kinds built into the dialects (plus the relation pseudo-kinds).
#: A custom registration may not shadow any of these.
_BUILTIN_KINDS = frozenset(
    {
        "smallint",
        "int",
        "bigint",
        "float",
        "decimal",
        "varchar",
        "text",
        "bytes",
        "bool",
        "datetime",
        "date",
        "time",
        "timedelta",
        "uuid",
        "json",
        "fk",
        "m2m",
    }
)


class FieldKindRegistration:
    """One :func:`register_field_kind` entry: a kind's class, SQL and options."""

    __slots__ = ("kind", "field_cls", "sql", "source", "requires_extension")

    def __init__(
        self,
        kind: str,
        field_cls: type[Field],
        sql: str | dict[str, str],
        source: Callable[[Field], str] | None,
        requires_extension: str | None,
    ) -> None:
        """Store the registration data.

        Args:
            kind: The abstract field kind (matches ``field_cls.field_kind``).
            field_cls: The :class:`Field` subclass implementing the kind.
            sql: SQL type template, or a per-dialect mapping of templates.
            source: Optional callable rendering a field's migration source.
            requires_extension: Optional PostgreSQL extension the kind needs.

        Returns:
            None
        """
        self.kind = kind
        self.field_cls = field_cls
        self.sql = sql
        self.source = source
        self.requires_extension = requires_extension

    def sql_template(self, dialect_name: str) -> str:
        """Resolve the SQL type template for one dialect.

        Args:
            dialect_name: The active dialect's name (e.g. ``"postgres"``).

        Raises:
            ConfigurationError: When the per-dialect mapping has no entry for
                the dialect.

        Returns:
            The ``str.format`` template filled from a field's ``type_params``.
        """
        if isinstance(self.sql, str):
            return self.sql
        try:
            return self.sql[dialect_name]
        except KeyError:
            # MariaDB is MySQL-compatible for column types, so a kind registered
            # only for "mysql" applies unchanged — fall back rather than force
            # every registration to duplicate the entry under "mariadb".
            if dialect_name == "mariadb" and "mysql" in self.sql:
                return self.sql["mysql"]
            raise ConfigurationError(
                f"field kind {self.kind!r} has no SQL type template for dialect "
                f"{dialect_name!r} (registered dialects: {sorted(self.sql)})"
            ) from None


#: kind -> its registration (consulted by the dialects and the migration writer).
_FIELD_KIND_REGISTRY: dict[str, FieldKindRegistration] = {}
#: class name -> registered class, resolved by the module ``__getattr__`` so
#: generated migrations' ``fields.<ClassName>(...)`` calls import cleanly.
_REGISTERED_FIELD_CLASSES: dict[str, type[Field]] = {}


def register_field_kind(
    kind: str,
    *,
    field_cls: type[Field],
    sql: str | dict[str, str],
    source: Callable[[Field], str] | None = None,
    requires_extension: str | None = None,
) -> None:
    """Register a custom field kind: its class, SQL type and migration source.

    Teaches every layer about a downstream field type in one call: the dialects
    render its column type from ``sql``, the migration writer emits
    ``fields.<ClassName>(...)`` (or the custom ``source``) for it, and
    ``fields.<ClassName>`` resolves so generated migration files import
    cleanly. Call it at import time of the module defining the field class, so
    any process that loads the models (including migration replay) sees the
    registration.

    Args:
        kind: The abstract field kind; must equal ``field_cls.field_kind`` and
            not shadow a built-in kind.
        field_cls: The :class:`Field` subclass implementing the kind. The
            default migration source renders its ``type_params`` as keyword
            arguments, so the constructor must accept them as such.
        sql: SQL type template (``str.format`` filled from a field's
            ``type_params``, e.g. ``"vector({dim})"``), or a per-dialect
            mapping such as ``{"postgres": "vector({dim})", "sqlite": "TEXT"}``.
        source: Optional callable rendering a field as constructor source for
            generated migration files; defaults to
            ``fields.<ClassName>(<type_params as kwargs>, null=..., ...)``.
        requires_extension: Optional PostgreSQL extension the kind needs (e.g.
            ``"vector"``); ``generate_schemas`` and generated migrations emit
            ``CREATE EXTENSION IF NOT EXISTS`` for it on PostgreSQL.

    Raises:
        ConfigurationError: When the kind shadows a built-in, ``field_cls`` is
            not a :class:`Field` subclass, its ``field_kind`` does not match,
            ``sql`` is empty, or the kind/class name is already registered
            differently.

    Returns:
        None
    """
    if kind in _BUILTIN_KINDS:
        raise ConfigurationError(f"field kind {kind!r} is built in and cannot be re-registered")
    if not (isinstance(field_cls, type) and issubclass(field_cls, Field)):
        raise ConfigurationError(f"field_cls must be a Field subclass, got {field_cls!r}")
    if field_cls.field_kind != kind:
        raise ConfigurationError(
            f"field_cls {field_cls.__name__!r} declares field_kind "
            f"{field_cls.field_kind!r}, which does not match the registered kind {kind!r}"
        )
    if not sql:
        raise ConfigurationError(
            f"field kind {kind!r} needs a non-empty SQL type template "
            "(a string or a per-dialect mapping)"
        )
    existing = _FIELD_KIND_REGISTRY.get(kind)
    if existing is not None:
        if existing.field_cls is field_cls:
            return  # idempotent re-registration
        raise ConfigurationError(
            f"field kind {kind!r} is already registered with "
            f"{existing.field_cls.__name__!r}; unregister_field_kind({kind!r}) first"
        )
    homonym = _REGISTERED_FIELD_CLASSES.get(field_cls.__name__)
    if homonym is not None and homonym is not field_cls:
        raise ConfigurationError(
            f"a different field class named {field_cls.__name__!r} is already registered; "
            "migration files resolve classes by name, so names must be unique"
        )
    _FIELD_KIND_REGISTRY[kind] = FieldKindRegistration(
        kind, field_cls, sql, source, requires_extension
    )
    _REGISTERED_FIELD_CLASSES[field_cls.__name__] = field_cls


def unregister_field_kind(kind: str) -> None:
    """Remove a custom field kind registration (a no-op when absent).

    Intended for tests that register throwaway kinds.

    Args:
        kind: The kind to unregister.

    Returns:
        None
    """
    registration = _FIELD_KIND_REGISTRY.pop(kind, None)
    if registration is not None:
        _REGISTERED_FIELD_CLASSES.pop(registration.field_cls.__name__, None)


def registered_field_kind(kind: str) -> FieldKindRegistration | None:
    """Look up a custom kind's registration (``None`` for unregistered kinds).

    Args:
        kind: The abstract field kind to look up.

    Returns:
        The registration, or ``None`` when the kind is not registered.
    """
    return _FIELD_KIND_REGISTRY.get(kind)


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


def _float_to_db(value: Any) -> Any:
    """Coerce a numeric string to ``float`` before binding.

    The float analog of :func:`_int_to_db`: a float column filtered or populated
    with a string (``score__in={"1.5", "2.0"}``, a ``str`` from an external
    system) binds as ``float`` rather than text — otherwise PostgreSQL rejects it
    with 'operator does not exist: double precision = text'. Non-string values
    (incl. ``bool``, ``F``, ``None``) pass through unchanged.

    Args:
        value: The Python value about to be bound.

    Returns:
        ``float(value)`` for a string, otherwise ``value`` unchanged.
    """
    return float(value) if isinstance(value, str) else value


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


class _IntegerField(Field[VT]):
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

    def to_python(self, value: Any) -> int | None:
        """Convert a database value into an ``int``.

        Args:
            value: The value returned by the database engine.

        Returns:
            The value as an ``int``, or ``None``.
        """
        return None if value is None else int(value)


class SmallIntField(_IntegerField[VT]):
    """A small integer column."""

    field_kind = "smallint"

    if TYPE_CHECKING:

        @overload
        def __new__(
            cls, *, null: Literal[True], pk: bool = ..., **kwargs: Any
        ) -> SmallIntField[int | None]: ...
        @overload
        def __new__(
            cls, *, null: Literal[False] = ..., pk: bool = ..., **kwargs: Any
        ) -> SmallIntField[int]: ...
        def __new__(cls, *, null: bool = ..., pk: bool = ..., **kwargs: Any) -> SmallIntField[Any]:
            """Resolve the value type (``int`` or ``int | None``) from ``null``."""
            ...


class IntField(_IntegerField[VT]):
    """A standard integer column."""

    field_kind = "int"

    if TYPE_CHECKING:

        @overload
        def __new__(
            cls, *, null: Literal[True], pk: bool = ..., **kwargs: Any
        ) -> IntField[int | None]: ...
        @overload
        def __new__(
            cls, *, null: Literal[False] = ..., pk: bool = ..., **kwargs: Any
        ) -> IntField[int]: ...
        def __new__(cls, *, null: bool = ..., pk: bool = ..., **kwargs: Any) -> IntField[Any]:
            """Resolve the value type (``int`` or ``int | None``) from ``null``."""
            ...


class BigIntField(_IntegerField[VT]):
    """A 64-bit integer column."""

    field_kind = "bigint"

    if TYPE_CHECKING:

        @overload
        def __new__(
            cls, *, null: Literal[True], pk: bool = ..., **kwargs: Any
        ) -> BigIntField[int | None]: ...
        @overload
        def __new__(
            cls, *, null: Literal[False] = ..., pk: bool = ..., **kwargs: Any
        ) -> BigIntField[int]: ...
        def __new__(cls, *, null: bool = ..., pk: bool = ..., **kwargs: Any) -> BigIntField[Any]:
            """Resolve the value type (``int`` or ``int | None``) from ``null``."""
            ...


class FloatField(Field[VT]):
    """A floating-point column."""

    field_kind = "float"

    if TYPE_CHECKING:

        @overload
        def __new__(cls, *, null: Literal[True], **kwargs: Any) -> FloatField[float | None]: ...
        @overload
        def __new__(cls, *, null: Literal[False] = ..., **kwargs: Any) -> FloatField[float]: ...
        def __new__(cls, *, null: bool = ..., **kwargs: Any) -> FloatField[Any]:
            """Resolve the value type (``float`` or ``float | None``) from ``null``."""
            ...

    def to_python(self, value: Any) -> float | None:
        """Convert a database value into a ``float``.

        Args:
            value: The value returned by the database engine.

        Returns:
            The value as a ``float``, or ``None``.
        """
        return None if value is None else float(value)

    def to_db(self, value: Any) -> Any:
        """Coerce a numeric-string value to ``float`` before binding.

        Args:
            value: The Python value about to be bound.

        Returns:
            ``float(value)`` for a string, otherwise ``value`` unchanged.
        """
        return _float_to_db(value)


class DecimalField(Field[VT]):
    """A fixed-precision decimal column."""

    if TYPE_CHECKING:

        @overload
        def __new__(
            cls,
            max_digits: int = ...,
            decimal_places: int = ...,
            *,
            null: Literal[True],
            **kwargs: Any,
        ) -> DecimalField[Decimal | None]: ...
        @overload
        def __new__(
            cls,
            max_digits: int = ...,
            decimal_places: int = ...,
            *,
            null: Literal[False] = ...,
            **kwargs: Any,
        ) -> DecimalField[Decimal]: ...
        def __new__(
            cls,
            max_digits: int = ...,
            decimal_places: int = ...,
            *,
            null: bool = ...,
            **kwargs: Any,
        ) -> DecimalField[Any]:
            """Resolve the value type (``Decimal`` or ``Decimal | None``) from ``null``."""
            ...

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

    def to_db(self, value: Any) -> Decimal | None:
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

    def to_python(self, value: Any) -> Decimal | None:
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
class CharField(Field[VT]):
    """A variable-length string column."""

    field_kind = "varchar"

    if TYPE_CHECKING:

        @overload
        def __new__(
            cls, max_length: int = ..., *, null: Literal[True], **kwargs: Any
        ) -> CharField[str | None]: ...
        @overload
        def __new__(
            cls, max_length: int = ..., *, null: Literal[False] = ..., **kwargs: Any
        ) -> CharField[str]: ...
        def __new__(
            cls, max_length: int = ..., *, null: bool = ..., **kwargs: Any
        ) -> CharField[Any]:
            """Resolve the value type (``str`` or ``str | None``) from ``null``."""
            ...

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


class TextField(Field[VT]):
    """An unbounded text column."""

    field_kind = "text"

    if TYPE_CHECKING:

        @overload
        def __new__(cls, *, null: Literal[True], **kwargs: Any) -> TextField[str | None]: ...
        @overload
        def __new__(cls, *, null: Literal[False] = ..., **kwargs: Any) -> TextField[str]: ...
        def __new__(cls, *, null: bool = ..., **kwargs: Any) -> TextField[Any]:
            """Resolve the value type (``str`` or ``str | None``) from ``null``."""
            ...

    def to_db(self, value: Any) -> Any:
        """Coerce a non-string scalar (e.g. ``int``) to ``str`` before binding.

        Args:
            value: The Python value to convert.

        Returns:
            ``str(value)`` for a non-bool ``int``, otherwise ``value`` unchanged.
        """
        return _str_to_db(value)


class BinaryField(Field[VT]):
    """A binary (bytes) column."""

    field_kind = "bytes"

    if TYPE_CHECKING:

        @overload
        def __new__(cls, *, null: Literal[True], **kwargs: Any) -> BinaryField[bytes | None]: ...
        @overload
        def __new__(cls, *, null: Literal[False] = ..., **kwargs: Any) -> BinaryField[bytes]: ...
        def __new__(cls, *, null: bool = ..., **kwargs: Any) -> BinaryField[Any]:
            """Resolve the value type (``bytes`` or ``bytes | None``) from ``null``."""
            ...


class BooleanField(Field[VT]):
    """A boolean column."""

    field_kind = "bool"

    if TYPE_CHECKING:

        @overload
        def __new__(cls, *, null: Literal[True], **kwargs: Any) -> BooleanField[bool | None]: ...
        @overload
        def __new__(cls, *, null: Literal[False] = ..., **kwargs: Any) -> BooleanField[bool]: ...
        def __new__(cls, *, null: bool = ..., **kwargs: Any) -> BooleanField[Any]:
            """Resolve the value type (``bool`` or ``bool | None``) from ``null``."""
            ...

    #: String spellings accepted (case-insensitively) as boolean input.
    _TRUE_STRINGS = frozenset({"true", "t", "1", "yes", "y", "on"})
    _FALSE_STRINGS = frozenset({"false", "f", "0", "no", "n", "off"})

    def to_db(self, value: Any) -> bool | None:
        """Coerce the Python value to a ``bool`` before binding.

        Strings are coerced semantically (``"true"``/``"t"``/``"1"`` and
        ``"false"``/``"f"``/``"0"``, case-insensitive) — ``bool("false")`` would
        silently bind ``True``. Other values coerce with ``bool(value)`` so
        ``1``/``0`` round-trip.

        Args:
            value: The Python value to convert.

        Raises:
            ValueError: For a string that spells neither true nor false.

        Returns:
            ``None`` when value is ``None``, otherwise the coerced ``bool``.
        """
        if value is None:
            return None
        if isinstance(value, str):
            text = value.strip().lower()
            if text in self._TRUE_STRINGS:
                return True
            if text in self._FALSE_STRINGS:
                return False
            raise ValueError(
                f"Invalid boolean string {value!r} for field {self.model_field_name!r}; "
                f"expected a true/false spelling such as 'true'/'false', 't'/'f', "
                f"'1'/'0', 'yes'/'no' or 'on'/'off'"
            )
        return bool(value)

    def to_python(self, value: Any) -> bool | None:
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


class _TemporalField(Field[VT]):
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


class DatetimeField(_TemporalField[VT]):
    """A date-and-time column."""

    field_kind = "datetime"

    if TYPE_CHECKING:

        @overload
        def __new__(
            cls,
            auto_now: bool = ...,
            auto_now_add: bool = ...,
            *,
            null: Literal[True],
            **kwargs: Any,
        ) -> DatetimeField[datetime | None]: ...
        @overload
        def __new__(
            cls,
            auto_now: bool = ...,
            auto_now_add: bool = ...,
            *,
            null: Literal[False] = ...,
            **kwargs: Any,
        ) -> DatetimeField[datetime]: ...
        def __new__(
            cls, auto_now: bool = ..., auto_now_add: bool = ..., *, null: bool = ..., **kwargs: Any
        ) -> DatetimeField[Any]:
            """Resolve the value type (``datetime`` or ``datetime | None``) from ``null``."""
            ...

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

    def to_db(self, value: Any) -> Any:
        """Coerce the value for binding, widening a bare ``date`` to midnight.

        Mirrors :meth:`to_python_value`: writes that bypass assignment coercion
        (plain ``setattr`` + ``save()``, ``QuerySet.update()``,
        ``bulk_update()``) would otherwise bind the bare ``date`` as-is, which
        stores date-only ``YYYY-MM-DD`` text on SQLite — unreadable by the
        engine's datetime decoder and mis-sorted against same-day timestamps.
        The ``__date`` truncation lookup narrows its comparison value to a
        ``date`` itself (before this method's widening can apply), so
        date-truncated comparisons still bind date-only values.

        Args:
            value: The Python value to convert.

        Returns:
            A ``datetime`` parsed from a string or widened from a ``date``;
            otherwise ``value`` unchanged.
        """
        if isinstance(value, str):
            value = _parse_iso_datetime(value)
        if isinstance(value, date) and not isinstance(value, datetime):
            value = datetime(value.year, value.month, value.day)
            if _tz.get_use_tz():
                value = _tz.make_aware(value)
        return value

    def to_python_value(self, value: Any) -> Any:
        """Coerce an ISO-8601 string or a bare ``date`` to a ``datetime``.

        Values often arrive as ISO strings (e.g. from a JSON layer); coercing
        on assignment keeps the in-memory attribute a ``datetime`` (and binding
        a string verbatim would otherwise reach the engine as text, which the
        timestamp column rejects). A bare ``date`` widens to midnight: bound
        as-is it would store as date-only ``YYYY-MM-DD`` text on SQLite, which
        the engine's datetime decoder cannot parse back (refetch returns a
        plain ``str``) and which sorts before every same-day timestamp in TEXT
        comparisons, breaking range filters. Under ``use_tz`` the midnight
        value is localised to the default timezone — the same form
        :func:`yara_orm.timezone.now` gives other datetimes — so the stored
        representation stays uniform. Other values pass through unchanged.

        Args:
            value: The value supplied by the caller.

        Returns:
            A ``datetime`` parsed from a string or widened from a ``date``;
            otherwise ``value`` unchanged.
        """
        if isinstance(value, str):
            return _parse_iso_datetime(value)
        if isinstance(value, date) and not isinstance(value, datetime):
            value = datetime(value.year, value.month, value.day)
            if _tz.get_use_tz():
                value = _tz.make_aware(value)
        return value


class DateField(_TemporalField[VT]):
    """A calendar-date column."""

    field_kind = "date"

    if TYPE_CHECKING:

        @overload
        def __new__(cls, *, null: Literal[True], **kwargs: Any) -> DateField[date | None]: ...
        @overload
        def __new__(cls, *, null: Literal[False] = ..., **kwargs: Any) -> DateField[date]: ...
        def __new__(cls, *, null: bool = ..., **kwargs: Any) -> DateField[Any]:
            """Resolve the value type (``date`` or ``date | None``) from ``null``."""
            ...

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


class TimeField(_TemporalField[VT]):
    """A time-of-day column."""

    field_kind = "time"

    if TYPE_CHECKING:

        @overload
        def __new__(cls, *, null: Literal[True], **kwargs: Any) -> TimeField[time | None]: ...
        @overload
        def __new__(cls, *, null: Literal[False] = ..., **kwargs: Any) -> TimeField[time]: ...
        def __new__(cls, *, null: bool = ..., **kwargs: Any) -> TimeField[Any]:
            """Resolve the value type (``time`` or ``time | None``) from ``null``."""
            ...

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


class TimeDeltaField(Field[VT]):
    """A duration column, stored as an integer number of microseconds."""

    field_kind = "timedelta"
    read_identity = False

    if TYPE_CHECKING:

        @overload
        def __new__(
            cls, *, null: Literal[True], **kwargs: Any
        ) -> TimeDeltaField[timedelta | None]: ...
        @overload
        def __new__(
            cls, *, null: Literal[False] = ..., **kwargs: Any
        ) -> TimeDeltaField[timedelta]: ...
        def __new__(cls, *, null: bool = ..., **kwargs: Any) -> TimeDeltaField[Any]:
            """Resolve the value type (``timedelta`` or ``timedelta | None``) from ``null``."""
            ...

    def to_db(self, value: Any) -> int | None:
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

    def to_python(self, value: Any) -> timedelta | None:
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
class UUIDField(Field[VT]):
    """A UUID column."""

    field_kind = "uuid"

    if TYPE_CHECKING:

        @overload
        def __new__(
            cls, *, null: Literal[True], pk: bool = ..., **kwargs: Any
        ) -> UUIDField[_uuid.UUID | None]: ...
        @overload
        def __new__(
            cls, *, null: Literal[False] = ..., pk: bool = ..., **kwargs: Any
        ) -> UUIDField[_uuid.UUID]: ...
        def __new__(cls, *, null: bool = ..., pk: bool = ..., **kwargs: Any) -> UUIDField[Any]:
            """Resolve the value type (``UUID`` or ``UUID | None``) from ``null``."""
            ...

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

    def to_db(self, value: Any) -> _uuid.UUID | None:
        """Convert a value into a ``UUID`` for binding.

        Args:
            value: The Python value to convert.

        Returns:
            A ``UUID`` instance, or ``None``.
        """
        if value is None:
            return None
        return value if isinstance(value, _uuid.UUID) else _uuid.UUID(str(value))

    def to_python(self, value: Any) -> _uuid.UUID | None:
        """Reconstruct a ``UUID`` from a database value.

        The value is already a ``UUID`` on backends whose driver decodes the
        type natively (PostgreSQL), but arrives as text where it is stored in a
        character column and read back as a string (Oracle's ``RETURNING`` OUT
        binds, which the model layer coerces through ``to_python`` rather than
        the row decoder).

        Args:
            value: The value returned by the database engine.

        Returns:
            A ``UUID`` instance, or ``None``.
        """
        if value is None:
            return None
        return value if isinstance(value, _uuid.UUID) else _uuid.UUID(str(value))


class JSONField(Field[VT]):
    """A JSON column.

    ``encoder``/``decoder`` are optional Python value-transform hooks:
    ``encoder`` runs on the Python value before it is handed to the engine to
    serialise, and ``decoder`` runs on the value read back. Rather than storing
    whatever string the encoder produced, yara serialises JSON in its engine,
    so these are value→value transforms (e.g. to make oversized integers
    JS-safe), not full (de)serialisers.

    Genuinely generic: a bare ``JSONField()`` reveals ``Any`` for its values
    (JSON is schemaless), while an annotated declaration such as
    ``data: JSONField[list[dict] | None] = JSONField(null=True)`` types
    instance access as the annotation's value type.
    """

    field_kind = "json"
    read_identity = False

    if TYPE_CHECKING:

        def __new__(
            cls,
            *,
            encoder: Callable[[Any], Any] | None = ...,
            decoder: Callable[[Any], Any] | None = ...,
            null: bool = ...,
            **kwargs: Any,
        ) -> JSONField[Any]:
            """Type JSON values as ``Any`` (JSON is schemaless) regardless of ``null``."""
            ...

    def __init__(
        self,
        *,
        encoder: Callable[[Any], Any] | None = None,
        decoder: Callable[[Any], Any] | None = None,
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
class IntEnumField(Field[VT]):
    """Stores an ``IntEnum`` as its integer value; reads back enum members."""

    field_kind = "int"
    read_identity = False

    if TYPE_CHECKING:

        @overload
        def __new__(
            cls, enum_type: type[_IntEnumT], *, null: Literal[True], **kwargs: Any
        ) -> IntEnumField[_IntEnumT | None]: ...
        @overload
        def __new__(
            cls, enum_type: type[_IntEnumT], *, null: Literal[False] = ..., **kwargs: Any
        ) -> IntEnumField[_IntEnumT]: ...
        def __new__(
            cls, enum_type: type[_IntEnumT], *, null: bool = ..., **kwargs: Any
        ) -> IntEnumField[Any]:
            """Resolve the value type from the stored enum class and ``null``."""
            ...

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

    def to_db(self, value: Any) -> int | None:
        """Convert an enum member into its integer value.

        Args:
            value: An enum member or raw integer.

        Returns:
            The integer value, or ``None``.
        """
        if value is None:
            return None
        return int(value.value if isinstance(value, self.enum_type) else value)

    def to_python(self, value: Any) -> IntEnum | None:
        """Convert a database integer into an enum member.

        Args:
            value: The integer returned by the database engine.

        Returns:
            The corresponding enum member, or ``None``.
        """
        return None if value is None else self.enum_type(value)


class CharEnumField(Field[VT]):
    """Stores a string ``Enum`` as its ``.value``; reads back enum members."""

    field_kind = "varchar"
    read_identity = False

    if TYPE_CHECKING:

        @overload
        def __new__(
            cls,
            enum_type: type[_EnumT],
            max_length: int = ...,
            *,
            null: Literal[True],
            **kwargs: Any,
        ) -> CharEnumField[_EnumT | None]: ...
        @overload
        def __new__(
            cls,
            enum_type: type[_EnumT],
            max_length: int = ...,
            *,
            null: Literal[False] = ...,
            **kwargs: Any,
        ) -> CharEnumField[_EnumT]: ...
        def __new__(
            cls, enum_type: type[_EnumT], max_length: int = ..., *, null: bool = ..., **kwargs: Any
        ) -> CharEnumField[Any]:
            """Resolve the value type from the stored enum class and ``null``."""
            ...

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

    def to_db(self, value: Any) -> str | None:
        """Convert an enum member into its string value.

        Args:
            value: An enum member or raw string.

        Returns:
            The string value, or ``None``.
        """
        if value is None:
            return None
        return str(value.value if isinstance(value, self.enum_type) else value)

    def to_python(self, value: Any) -> Enum | None:
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


#: The accepted ``ON DELETE`` actions. ``on_delete`` is interpolated into DDL
#: verbatim (a referential action is not a bindable value), so it is validated
#: against this closed set to keep arbitrary SQL out of the ``FOREIGN KEY`` clause.
_ON_DELETE_ACTIONS = frozenset(
    {
        OnDelete.CASCADE,
        OnDelete.RESTRICT,
        OnDelete.SET_NULL,
        OnDelete.SET_DEFAULT,
        OnDelete.NO_ACTION,
    }
)


class ForeignKeyFieldInstance(Field[Any]):
    """The field object behind a :func:`ForeignKeyField` declaration.

    Declared under the relation name (e.g. ``tournament``); the metaclass
    synthesises a concrete ``<name>_id`` column and installs a forward accessor
    (``await obj.tournament``) plus a reverse manager (``related_name``) on the
    target model. Constructed via the :func:`ForeignKeyField` factory (whose
    return is typed as the relation, Tortoise-style); use this class for
    ``isinstance`` checks.
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
        # Normalise case/whitespace ("set null" -> "SET NULL") and reject anything
        # outside the closed set: on_delete is spliced into DDL, not bound.
        normalized_on_delete = " ".join(str(on_delete).upper().split())
        if normalized_on_delete not in _ON_DELETE_ACTIONS:
            raise ValueError(
                f"invalid on_delete {on_delete!r}; expected one of {sorted(_ON_DELETE_ACTIONS)}"
            )
        self.on_delete = normalized_on_delete
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

        try:
            target = registry.get_model(self.reference)
        except KeyError:
            return None
        self._target_pk_field = target._meta.pk_field
        return self._target_pk_field


class OneToOneFieldInstance(ForeignKeyFieldInstance):
    """A unique foreign key; the reverse accessor yields a single instance."""

    is_o2o = True

    def __init__(self, reference: str | None = None, **kwargs: Any) -> None:
        """Initialize the one-to-one relation, enforcing uniqueness.

        Args:
            reference: Dotted path or name of the target model (or pass ``to=``).
            **kwargs: Additional options forwarded to
                :class:`ForeignKeyFieldInstance`.

        Returns:
            None
        """
        kwargs.setdefault("unique", True)
        super().__init__(reference, **kwargs)


class ManyToManyFieldInstance(Field[Any]):
    """The field object behind a :func:`ManyToManyField` declaration.

    No column is added to the owning table; the metaclass installs a manager
    supporting ``add``/``remove``/``clear`` and querying through the join table.
    Constructed via the :func:`ManyToManyField` factory; use this class for
    ``isinstance`` checks.
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
            forward_key: Join-table column referencing the target model.
            backward_key: Join-table column referencing the owning model.
            through_fields: Django-order ``(owner_column, target_column)``
                spelling — i.e. ``(backward_key, forward_key)``; used to fill
                those when they are not given explicitly.
            to: Modern alias for ``reference``.
            **kwargs: Additional options forwarded to :class:`Field`.

        Returns:
            None
        """
        if to is not None:
            reference = to
        if through_fields is not None:
            # Django's through_fields order is (source, target): the column
            # referencing the OWNING model first — the reverse of the
            # (forward, backward) attribute order here, where every consumer
            # (M2MInfo, dialects, prefetch, migrations) reads ``forward_key``
            # as the target-referencing column.
            backward_key = backward_key or through_fields[0]
            forward_key = forward_key or through_fields[1]
        if reference is None:
            raise TypeError("ManyToManyField requires a target model (pass reference= or to=)")
        super().__init__(**kwargs)
        self.reference = reference
        self.related_name = related_name
        self.through = through
        self.forward_key = forward_key
        self.backward_key = backward_key


# ---------------------------------------------------------------------------
# Relation field factories (Tortoise-style)
# ---------------------------------------------------------------------------
# The factories construct the ``*FieldInstance`` objects but are *typed* as
# returning the relation the attribute resolves to, so a declared annotation
# is the attribute's static type and an unannotated declaration stays valid:
#
#     author: ForeignKeyRelation[Author] = ForeignKeyField("Author")
#     tags: ManyToManyRelation[Tag] = ManyToManyField("Tag")


def ForeignKeyField(
    reference: str | None = None,
    related_name: str | None = None,
    on_delete: str = OnDelete.CASCADE,
    source_field: str | None = None,
    db_constraint: bool = True,
    to: str | None = None,
    **kwargs: Any,
) -> ForeignKeyRelation[Any]:
    """Declare a foreign key to another model.

    Args:
        reference: Dotted path or name of the target model.
        related_name: Name of the reverse accessor on the target model.
        on_delete: Referential action applied on deletion.
        source_field: Explicit name for this table's FK column; defaults to
            ``<name>_id``.
        db_constraint: Whether to emit a database ``FOREIGN KEY`` constraint.
        to: Modern alias for ``reference``.
        **kwargs: Additional options forwarded to :class:`Field`.

    Returns:
        The field object (typed as the relation the attribute resolves to).
    """
    return cast(
        "ForeignKeyRelation[Any]",
        ForeignKeyFieldInstance(
            reference,
            related_name=related_name,
            on_delete=on_delete,
            source_field=source_field,
            db_constraint=db_constraint,
            to=to,
            **kwargs,
        ),
    )


def OneToOneField(
    reference: str | None = None,
    **kwargs: Any,
) -> OneToOneRelation[Any]:
    """Declare a unique foreign key (one-to-one) to another model.

    Args:
        reference: Dotted path or name of the target model (or pass ``to=``).
        **kwargs: Additional options forwarded to :func:`ForeignKeyField`.

    Returns:
        The field object (typed as the relation the attribute resolves to).
    """
    return cast("OneToOneRelation[Any]", OneToOneFieldInstance(reference, **kwargs))


def ManyToManyField(
    reference: str | None = None,
    related_name: str | None = None,
    through: str | None = None,
    forward_key: str | None = None,
    backward_key: str | None = None,
    through_fields: tuple[str, str] | None = None,
    to: str | None = None,
    **kwargs: Any,
) -> ManyToManyRelation[Any]:
    """Declare a many-to-many relation realised through a join table.

    Args:
        reference: Dotted path or name of the target model.
        related_name: Name of the reverse accessor on the target model.
        through: Name of the join table; synthesised when omitted.
        forward_key: Join-table column referencing the target model.
        backward_key: Join-table column referencing the owning model.
        through_fields: Django-order ``(owner_column, target_column)`` spelling
            — i.e. ``(backward_key, forward_key)``. .. versionchanged:: 1.14.4
            Earlier releases read this tuple in the opposite,
            ``(forward_key, backward_key)`` order; declarations written
            against that order must swap their two elements (or switch to the
            explicit ``forward_key=``/``backward_key=`` kwargs, whose meaning
            has never changed).
        to: Modern alias for ``reference``.
        **kwargs: Additional options forwarded to :class:`Field`.

    Returns:
        The field object (typed as the relation the attribute resolves to).
    """
    return cast(
        "ManyToManyRelation[Any]",
        ManyToManyFieldInstance(
            reference,
            related_name=related_name,
            through=through,
            forward_key=forward_key,
            backward_key=backward_key,
            through_fields=through_fields,
            to=to,
            **kwargs,
        ),
    )


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
    "FieldKindRegistration",
    "register_field_kind",
    "unregister_field_kind",
    "registered_field_kind",
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
    "ForeignKeyFieldInstance",
    "OneToOneFieldInstance",
    "ManyToManyFieldInstance",
]

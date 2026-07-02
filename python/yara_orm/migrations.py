"""A class-based, operation-driven migration system.

Each migration file defines a ``class Migration(m.Migration)`` carrying
``operations`` (and ``dependencies`` / ``atomic``). Operations are built from
**live field objects** -- ``CreateModel`` lists ``fields={col: Field}``,
``AddField``/``AlterField`` carry a single ``Field`` -- and render to SQL per
active dialect at apply time, so the same migration runs on PostgreSQL or
SQLite.

Workflow (see :class:`MigrationManager` and the ``python -m yara_orm`` CLI):

    makemigrations -> writes NNNN_name.py from the model diff
    upgrade        -> applies pending migrations, records them
    downgrade      -> reverts applied migrations
    history/heads  -> inspect applied vs on-disk
    sqlmigrate     -> print a migration's SQL without running it

Generated migrations emit the **idempotent** analog of each operation
(``CreateModelIfNotExists``, ``AddFieldIfNotExists``, ...). An ``atomic``
migration (the default) is all-or-nothing: a failure rolls the whole migration
back, so it can simply be re-run. Re-applying a half-applied **non-atomic**
migration is only safe for the guarded operations (``CreateModelIfNotExists``,
``DeleteModelIfExists``, the ``*IfNotExists``/``*IfExists`` index and field
ops on PostgreSQL); ``RenameField``/``RenameModel``, constraint ops, and — on
SQLite, which has no ``IF [NOT] EXISTS`` for ``ADD``/``DROP COLUMN`` — the
field ops are *not* re-run safe. On SQLite a column or constraint change
rebuilds the table, and the rebuild must toggle ``PRAGMA foreign_keys``
outside a transaction, so an atomic migration containing a rebuild commits the
operations preceding it before the rebuild runs (the rebuild itself is still
transactional). ``AlterField`` is detected automatically when a column's type,
nullability, uniqueness, database default or foreign-key target changes. The
``*Concurrently`` index operations are for hand-written, ``atomic = False``
migrations (PostgreSQL builds those indexes outside a transaction).
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
from collections.abc import Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from . import registry
from .connection import get_dialect, get_executor, in_transaction
from .db_defaults import DatabaseDefault, Now, RandomHex, SqlDefault
from .dialects import PRAGMA_FK_OFF, PRAGMA_FK_ON
from .exceptions import ConfigurationError, UnSupportedError
from .fields import ForeignKeyFieldInstance, OnDelete

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from types import ModuleType

    from .connection import BaseDBAsyncClient
    from .dialects import BaseDialect
    from .fields import Field
    from .models import MetaInfo, Model

MIGRATION_TABLE = "orm_migrations"
_FILENAME_RE = re.compile(r"^(\d+)_.*\.py$")

# Reprs longer than this are expanded one item per line in generated migrations.
_WRAP = 200

#: Abstract field *kind* -> the canonical scalar field class that reproduces it.
#: Migrations only care about a column's schema, so enum/validator/default
#: behaviour is dropped: an ``IntEnumField`` renders as a plain ``IntField`` and
#: a ``CharEnumField`` as a ``CharField`` -- identical DDL, no user enum import.
_KIND_FIELD = {
    "smallint": "SmallIntField",
    "int": "IntField",
    "bigint": "BigIntField",
    "float": "FloatField",
    "decimal": "DecimalField",
    "varchar": "CharField",
    "text": "TextField",
    "bytes": "BinaryField",
    "bool": "BooleanField",
    "datetime": "DatetimeField",
    "date": "DateField",
    "time": "TimeField",
    "timedelta": "TimeDeltaField",
    "uuid": "UUIDField",
    "json": "JSONField",
}


# ---------------------------------------------------------------------------
# Source rendering helpers
# ---------------------------------------------------------------------------
def _fmt(value: Any, indent: int = 0) -> str:
    """Render a value as Python source, expanding long dicts/lists per line.

    Short values (and any scalar) render as their plain ``repr``; long dicts and
    lists break onto one item per line, indented under ``indent`` spaces.

    Args:
        value: Value to render as Python source.
        indent: Number of leading spaces for the closing bracket.

    Returns:
        The Python source for ``value``.
    """
    text = repr(value)
    if len(text) <= _WRAP or not isinstance(value, (dict, list)):
        return text
    pad, inner = " " * indent, " " * (indent + 4)
    if isinstance(value, dict):
        body = ",\n".join(f"{inner}{k!r}: {_fmt(v, indent + 4)}" for k, v in value.items())
        return f"{{\n{body},\n{pad}}}"
    body = ",\n".join(f"{inner}{_fmt(item, indent + 4)}" for item in value)
    return f"[\n{body},\n{pad}]"


def _call(name: str, args: list[str]) -> str:
    """Render a constructor call, wrapping to one argument per line when long.

    A call whose single-line form fits within ``_WRAP`` (and whose arguments are
    not themselves already wrapped) stays on one line; otherwise each argument
    goes on its own line.

    Args:
        name: Dotted name of the operation constructor (e.g. ``m.CreateModel``).
        args: Pre-rendered positional/keyword argument source fragments.

    Returns:
        The constructor call source (no trailing indentation).
    """
    oneline = f"{name}({', '.join(args)})"
    if len(oneline) <= _WRAP and "\n" not in oneline:
        return oneline
    body = ",\n".join(f"        {arg}" for arg in args)
    return f"{name}(\n{body},\n    )"


def _default_source(default: DatabaseDefault) -> str:
    """Render a database-side default as ``db_defaults.Xxx(...)`` source.

    Args:
        default: The database default to render.

    Returns:
        The constructor-call source reconstructing an equivalent default.

    Raises:
        ConfigurationError: For a custom ``DatabaseDefault`` subclass migrations
            cannot serialise (use ``SqlDefault`` instead).
    """
    if isinstance(default, RandomHex):
        return f"db_defaults.RandomHex(size={default.size})"
    if isinstance(default, SqlDefault):
        return f"db_defaults.SqlDefault({default.sql!r})"
    if isinstance(default, Now):
        return "db_defaults.Now()"
    raise ConfigurationError(
        f"cannot serialise database default {type(default).__name__!r} into a migration; "
        "use db_defaults.SqlDefault(...) (or Now/RandomHex) for migratable defaults"
    )


def _default_spec(default: Any) -> dict[str, Any] | None:
    """Build the canonical, comparable spec for a column's database default.

    Python-side defaults (values/callables) never reach the DDL, so they map to
    ``None``; only :class:`~yara_orm.db_defaults.DatabaseDefault` instances are
    captured.

    Args:
        default: The field's ``default`` attribute.

    Returns:
        The default spec mapping, or ``None`` when the column has no DB default.

    Raises:
        ConfigurationError: For a custom ``DatabaseDefault`` subclass migrations
            cannot serialise.
    """
    if not isinstance(default, DatabaseDefault):
        return None
    if isinstance(default, RandomHex):
        return {"kind": "random_hex", "size": default.size}
    if isinstance(default, SqlDefault):
        return {"kind": "sql", "sql": default.sql}
    if isinstance(default, Now):
        return {"kind": "now"}
    raise ConfigurationError(
        f"cannot serialise database default {type(default).__name__!r} into a migration; "
        "use db_defaults.SqlDefault(...) (or Now/RandomHex) for migratable defaults"
    )


def resolved_fk(
    field: Field,
    *,
    table: str,
    pk: str,
    kind: str,
    type_params: dict[str, Any] | None = None,
) -> Field:
    """Stamp a foreign-key field with its resolved target (for migration files).

    Generated migrations wrap foreign keys in this call so replaying them never
    needs the live model registry: the target table, primary-key column and its
    scalar type are recorded at ``makemigrations`` time. Older migration files
    without the wrapper still work while the target model remains registered.

    Args:
        field: The foreign-key field to stamp.
        table: The referenced table name.
        pk: The referenced primary-key column name.
        kind: The referenced primary key's abstract field kind.
        type_params: The referenced primary key's type parameters, if any.

    Returns:
        The same field, carrying the resolved target info.
    """
    # Dynamic stamp: only fields routed through this wrapper carry it.
    field._resolved_target = {  # ty: ignore[unresolved-attribute]
        "table": table,
        "pk": pk,
        "kind": kind,
        "type_params": dict(type_params or {}),
    }
    return field


def _fk_target(field: Field, strict: bool = True) -> dict[str, Any] | None:
    """Resolve a foreign key's target table/pk/type, preferring recorded info.

    A field stamped by :func:`resolved_fk` answers from its recorded target;
    otherwise the live registry is consulted (the pre-recording behaviour).

    Args:
        field: The foreign-key field to resolve.
        strict: Whether an unresolvable target raises (or returns ``None``).

    Returns:
        A mapping with ``table``, ``pk``, ``kind`` and ``type_params``, or
        ``None`` when unresolvable and ``strict`` is false.

    Raises:
        KeyError: When the target model is not registered and ``strict`` is set.
    """
    target = getattr(field, "_resolved_target", None)
    if target is not None:
        return target
    reference: str = getattr(field, "reference", "")
    try:
        ref = registry.get_model(reference)
    except KeyError:
        if not strict:
            return None
        raise KeyError(
            f"cannot resolve foreign-key target {reference!r}: the model is no longer "
            "registered. This migration predates recorded FK targets (m.resolved_fk); either "
            "keep the target model importable or stamp the field with m.resolved_fk(...)"
        ) from None
    pkf = ref._meta.pk_field
    return {
        "table": ref._meta.table,
        "pk": pkf.db_column,
        "kind": pkf.field_kind,
        "type_params": dict(pkf.type_params),
    }


def _field_source(field: Field) -> str:
    """Render a field as a ``fields.XxxField(...)`` constructor call.

    Only schema-relevant arguments are emitted, so the rebuilt field produces the
    same column spec (Python-side defaults, validators and enum types are
    irrelevant to DDL and intentionally dropped; **database-side** defaults are
    part of the DDL and are emitted). Foreign keys whose target is resolvable are
    wrapped in :func:`resolved_fk` so the migration file is registry-independent.

    Args:
        field: The field to render as source.

    Returns:
        The constructor-call source reconstructing an equivalent field.
    """
    if isinstance(field, ForeignKeyFieldInstance):
        cls = "OneToOneField" if field.is_o2o else "ForeignKeyField"
        args = [repr(field.reference)]
        if field.on_delete != OnDelete.CASCADE:
            args.append(f"on_delete={field.on_delete!r}")
        if field.null:
            args.append("null=True")
        if field.unique and not field.is_o2o:
            args.append("unique=True")
        if field.index:
            args.append("index=True")
        source = f"fields.{cls}({', '.join(args)})"
        target = _fk_target(field, strict=False)
        if target is None:
            return source
        extra = [f"table={target['table']!r}", f"pk={target['pk']!r}", f"kind={target['kind']!r}"]
        if target["type_params"]:
            extra.append(f"type_params={target['type_params']!r}")
        return f"m.resolved_fk({source}, {', '.join(extra)})"

    args = []
    if field.field_kind == "varchar":
        args.append(f"max_length={field.type_params['max_length']}")
    elif field.field_kind == "decimal":
        args.append(f"max_digits={field.type_params['max_digits']}")
        args.append(f"decimal_places={field.type_params['decimal_places']}")
    if field.pk:
        args.append("pk=True")
    if field.null:
        args.append("null=True")
    if field.unique:
        args.append("unique=True")
    if field.index:
        args.append("index=True")
    if isinstance(field.default, DatabaseDefault):
        args.append(f"default={_default_source(field.default)}")
    return f"fields.{_KIND_FIELD[field.field_kind]}({', '.join(args)})"


def _fields_source(fields: dict[str, Field], indent: int) -> str:
    """Render a ``{column: Field}`` mapping as a multi-line source dict.

    Args:
        fields: Mapping of column name to field.
        indent: Number of leading spaces for the closing brace.

    Returns:
        The source for the fields mapping (``{}`` when empty).
    """
    if not fields:
        return "{}"
    pad, inner = " " * indent, " " * (indent + 4)
    body = ",\n".join(f"{inner}{col!r}: {_field_source(f)}" for col, f in fields.items())
    return f"{{\n{body},\n{pad}}}"


# ---------------------------------------------------------------------------
# Field -> spec helpers (the shared spec-builder)
# ---------------------------------------------------------------------------
def _column_spec(field: Field) -> dict[str, Any]:
    """Build a dialect column spec from a field, resolving FK target types.

    Args:
        field: The field to describe. Foreign keys resolve to their target
            primary key's scalar type via the recorded target (see
            :func:`resolved_fk`) or, failing that, the model registry.

    Returns:
        The column specification mapping.
    """
    if isinstance(field, ForeignKeyFieldInstance):
        target = cast(dict[str, Any], _fk_target(field))
        return {
            "kind": target["kind"],
            "type_params": dict(target["type_params"]),
            "null": field.null,
            "unique": field.unique,
            "pk": field.pk,
            "auto_increment": False,
            "default": None,
            "fk": {
                "table": target["table"],
                "pk": target["pk"],
                "on_delete": field.on_delete,
            },
        }
    return {
        "kind": field.field_kind,
        "type_params": dict(field.type_params),
        "null": field.null,
        "unique": field.unique,
        "pk": field.pk,
        "auto_increment": field.auto_increment,
        "default": _default_spec(field.default),
    }


def _fk_spec(field: Field) -> dict[str, Any] | None:
    """Build a foreign-key spec from a field, or ``None`` for non-FK fields.

    Args:
        field: The field to inspect.

    Returns:
        The foreign-key spec, or ``None`` when the field is not a foreign key.
    """
    if not isinstance(field, ForeignKeyFieldInstance):
        return None
    target = cast(dict[str, Any], _fk_target(field))
    return {
        "table": target["table"],
        "pk": target["pk"],
        "on_delete": field.on_delete,
    }


def _derived_indexes(fields: dict[str, Field]) -> list[str]:
    """Return the columns a field set implicitly indexes (``index=True``).

    Args:
        fields: Mapping of column name to field.

    Returns:
        The indexed column names (excluding unique and primary-key columns).
    """
    return [c for c, f in fields.items() if f.index and not f.unique and not f.pk]


def _index_spec(
    columns: list[str],
    condition: str | None,
    unique: bool,
    using: str | None,
    include: list[str] | None,
    opclass: str | None = None,
) -> dict[str, Any]:
    """Build the canonical composite-index state spec.

    Centralised so every producer (meta scan, ``CreateModel``, ``AddCompositeIndex``)
    stores an identically-keyed dict, keeping re-run diffs idempotent.

    Args:
        columns: The ordered db columns the index covers.
        condition: Optional partial-index predicate, or None.
        unique: Whether the index enforces uniqueness.
        using: Optional access method, or None.
        include: Optional covering columns, or None.
        opclass: Optional per-column operator class, or None.

    Returns:
        The composite-index spec mapping.
    """
    return {
        "columns": list(columns),
        "condition": condition,
        "unique": unique,
        "using": using,
        "include": list(include) if include else None,
        "opclass": opclass,
    }


def _index_option_source(
    condition: str | None,
    unique: bool,
    using: str | None,
    include: list[str] | None,
    opclass: str | None = None,
) -> list[str]:
    """Render the non-default composite-index options as keyword-argument source.

    Args:
        condition: Optional partial-index predicate, or None.
        unique: Whether the index enforces uniqueness.
        using: Optional access method, or None.
        include: Optional covering columns, or None.
        opclass: Optional per-column operator class, or None.

    Returns:
        The ``key=value`` source fragments for the options that differ from their
        defaults (possibly empty).
    """
    args: list[str] = []
    if condition is not None:
        args.append(f"condition={condition!r}")
    if unique:
        args.append(f"unique={unique!r}")
    if using is not None:
        args.append(f"using={using!r}")
    if include is not None:
        args.append(f"include={include!r}")
    if opclass is not None:
        args.append(f"opclass={opclass!r}")
    return args


def _meta_index_specs(meta: MetaInfo) -> dict[str, dict[str, Any]]:
    """Return the composite indexes a model declares via ``Meta.indexes``.

    Args:
        meta: The model metadata.

    Returns:
        A mapping of index name to the ordered db columns it covers. The name
        matches the one :meth:`create_table_sql` generates, so migration- and
        ``generate_schemas``-built schemas stay consistent.
    """

    def resolve(names: list[str]) -> list[str]:
        """Resolve field/forward-relation names to their db column names.

        Args:
            names: Field or forward-relation names.

        Returns:
            The corresponding db column names.
        """
        return [meta.resolve_writable_field(n).db_column for n in names]

    out: dict[str, dict[str, Any]] = {}
    for index in meta.indexes:
        name = index.resolve_name(meta.table)
        out[name] = _index_spec(
            resolve(index.fields),
            index.condition,
            index.unique,
            index.using,
            resolve(index.include) if index.include else None,
            index.opclass,
        )
    return out


def _meta_named_constraint_specs(meta: MetaInfo) -> list[dict[str, Any]]:
    """Return the **named** declarative constraints from ``Meta.constraints``.

    Only named constraints are tracked for diffing: ALTER TABLE drops a
    constraint by name, so an unnamed one (like a bare ``unique_together``,
    rendered inline) cannot be added/removed by a later migration.

    Args:
        meta: The model metadata.

    Returns:
        The list of constraint specs (each with a non-empty ``name``).
    """
    specs: list[dict[str, Any]] = []
    for constraint in meta.constraints:
        spec = constraint.to_spec()
        if spec.get("name"):
            specs.append(spec)
    return specs


def _meta_unique_together_specs(meta: MetaInfo) -> list[dict[str, Any]]:
    """Return ``UNIQUE`` constraint specs for a model's ``Meta.unique_together``.

    ``generate_schemas`` renders each group as an inline ``UNIQUE (...)`` clause,
    so the autogenerator must emit a matching constraint or the migrated schema
    silently loses the uniqueness guarantee. Each group is given a deterministic
    name (``uniq_<table>_<col>...``) so it routes through the same named-constraint
    diff machinery as ``Meta.constraints`` -- recorded in table state, diffed by
    name, and therefore idempotent across re-runs.

    Args:
        meta: The model metadata.

    Returns:
        One unique-constraint spec per ``unique_together`` group, each with a
        generated ``name`` and the group's resolved db columns.
    """
    groups = [[meta.resolve_writable_field(n).db_column for n in g] for g in meta.unique_together]
    names = [_unique_together_name(meta.table, cols, groups) for cols in groups]
    return [
        {"kind": "unique", "name": name, "fields": cols}
        for name, cols in zip(names, groups, strict=True)
    ]


#: PostgreSQL truncates identifiers beyond this many bytes, silently colliding
#: long constraint names; generated names are kept within it.
_MAX_IDENTIFIER = 63


def _unique_together_name(table: str, cols: list[str], groups: list[list[str]]) -> str:
    """Build the deterministic constraint name for one ``unique_together`` group.

    The common case keeps the historical ``uniq_<table>_<col>...`` join so
    existing deployments' constraint names still match. A hash suffix is added
    only when that join is ambiguous — two groups like ``("a", "b_c")`` and
    ``("a_b", "c")`` join identically — or when the name would exceed
    PostgreSQL's 63-character identifier limit (beyond which names silently
    truncate and collide).

    Args:
        table: The table name.
        cols: The group's resolved db columns.
        groups: Every group on the table (to detect join collisions).

    Returns:
        The constraint name.
    """
    base = f"uniq_{table}_" + "_".join(cols)
    joined = "_".join(cols)
    ambiguous = sum(1 for g in groups if "_".join(g) == joined) > 1
    if not ambiguous and len(base) <= _MAX_IDENTIFIER:
        return base
    digest = hashlib.sha1("\x1f".join(cols).encode()).hexdigest()[:8]
    return f"{base[: _MAX_IDENTIFIER - 9]}_{digest}"


def _constraint_from_spec(spec: dict[str, Any]) -> Constraint:
    """Rebuild a :class:`Constraint` object from its spec mapping.

    Args:
        spec: A constraint spec (``kind`` plus ``name`` and ``fields``/``check``).

    Returns:
        The reconstructed ``UniqueConstraint`` or ``CheckConstraint``.
    """
    if spec["kind"] == "check":
        return CheckConstraint(check=spec["check"], name=spec["name"])
    return UniqueConstraint(fields=list(spec["fields"]), name=spec["name"])


def _tspec(tstate: dict[str, Any]) -> dict[str, Any]:
    """Build a full dialect table spec from a table's migration state.

    Args:
        tstate: Table state holding ``fields`` and optional ``composite_pk`` /
            ``indexes``.

    Returns:
        The table specification mapping consumed by the dialect renderers.
    """
    fields = tstate["fields"]
    columns: dict[str, Any] = {}
    fks: dict[str, Any] = {}
    pk = None
    for col, field in fields.items():
        columns[col] = _column_spec(field)
        fk = _fk_spec(field)
        if fk:
            fks[col] = fk
        if field.pk:
            pk = col
    indexes = tstate.get("indexes")
    if indexes is None:
        indexes = _derived_indexes(fields)
    spec = {"columns": columns, "pk": pk, "fks": fks, "indexes": list(indexes)}
    if tstate.get("composite_pk"):
        spec["composite_pk"] = tstate["composite_pk"]
    if tstate.get("composite_indexes"):
        spec["composite_indexes"] = dict(tstate["composite_indexes"])
    if tstate.get("constraints"):
        spec["constraints"] = list(tstate["constraints"])
    return spec


def _new_tstate(fields: dict[str, Field], composite_pk: list[str] | None) -> dict[str, Any]:
    """Build a fresh table state from a field set (copying the field mapping).

    Args:
        fields: Mapping of column name to field.
        composite_pk: Column names forming a composite primary key, if any.

    Returns:
        A table-state mapping with ``fields``, ``composite_pk`` and ``indexes``.
    """
    return {
        "fields": dict(fields),
        "composite_pk": composite_pk,
        "indexes": _derived_indexes(fields),
        "composite_indexes": {},
        "constraints": [],
    }


def _rename_in_table(tstate: dict[str, Any], old: str, new: str) -> None:
    """Rename a column within a table state (fields, indexes, pk, constraints).

    Composite-index column/include lists and unique-constraint field lists
    follow the rename so a later diff does not spuriously re-emit them. Check
    constraints hold raw SQL and cannot be rewritten reliably; a rename of a
    column referenced by a ``CheckConstraint`` needs a hand-written migration.

    Args:
        tstate: The table state to mutate in place.
        old: The current column name.
        new: The new column name.

    Returns:
        None
    """

    def follow(cols: list[str]) -> list[str]:
        """Apply the rename to a column-name list.

        Args:
            cols: The column names to rewrite.

        Returns:
            The names with ``old`` replaced by ``new``.
        """
        return [new if c == old else c for c in cols]

    tstate["fields"] = {(new if c == old else c): f for c, f in tstate["fields"].items()}
    tstate["indexes"] = follow(tstate.get("indexes", []))
    if tstate.get("composite_pk"):
        tstate["composite_pk"] = follow(tstate["composite_pk"])
    for spec in (tstate.get("composite_indexes") or {}).values():
        spec["columns"] = follow(spec["columns"])
        if spec.get("include"):
            spec["include"] = follow(spec["include"])
    for constraint in tstate.get("constraints") or []:
        if constraint.get("fields"):
            constraint["fields"] = follow(constraint["fields"])


# ---------------------------------------------------------------------------
# Constraint definitions
# ---------------------------------------------------------------------------
class Constraint:
    """Base class for a table constraint definition (unique or check)."""

    def __init__(self, name: str | None = None) -> None:
        """Store the optional constraint name.

        Args:
            name: The constraint name (required to reverse an ``AddConstraint``).

        Returns:
            None
        """
        self.name = name

    def to_spec(self) -> dict[str, Any]:
        """Render the constraint as a dialect spec mapping.

        Returns:
            The constraint specification mapping.
        """
        raise NotImplementedError

    def to_source(self) -> str:
        """Render the constraint as Python source for a migration file.

        Returns:
            The source code constructing this constraint.
        """
        raise NotImplementedError


class UniqueConstraint(Constraint):
    """A ``UNIQUE`` constraint over one or more columns."""

    def __init__(self, *, fields: list[str], name: str | None = None) -> None:
        """Store the constrained columns and optional name.

        Args:
            fields: The column names covered by the unique constraint.
            name: The constraint name.

        Returns:
            None
        """
        super().__init__(name)
        self.fields = list(fields)

    def to_spec(self) -> dict[str, Any]:
        """Render the unique constraint as a dialect spec mapping.

        Returns:
            The constraint specification mapping.
        """
        return {"kind": "unique", "name": self.name, "fields": list(self.fields)}

    def to_source(self) -> str:
        """Render the unique constraint as Python source.

        Returns:
            The source code constructing this constraint.
        """
        return f"m.UniqueConstraint(fields={self.fields!r}, name={self.name!r})"


class CheckConstraint(Constraint):
    """A ``CHECK`` constraint over a boolean SQL expression."""

    def __init__(self, *, check: str, name: str | None = None) -> None:
        """Store the check expression and optional name.

        Args:
            check: The SQL boolean expression the constraint enforces.
            name: The constraint name.

        Returns:
            None
        """
        super().__init__(name)
        self.check = check

    def to_spec(self) -> dict[str, Any]:
        """Render the check constraint as a dialect spec mapping.

        Returns:
            The constraint specification mapping.
        """
        return {"kind": "check", "name": self.name, "check": self.check}

    def to_source(self) -> str:
        """Render the check constraint as Python source.

        Returns:
            The source code constructing this constraint.
        """
        return f"m.CheckConstraint(check={self.check!r}, name={self.name!r})"


# ---------------------------------------------------------------------------
# Migration base class
# ---------------------------------------------------------------------------
class Migration:
    """Base class for a migration file's ``class Migration`` declaration."""

    #: Whether the migration's operations run inside a single transaction.
    atomic: bool = True
    #: Names of migrations this one depends on (applied before it).
    dependencies: list[str] = []
    #: The ordered operations the migration applies.
    operations: list[Operation] = []


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------
class Operation:
    """Base class: render SQL both ways and evolve the in-memory schema state."""

    def forward_sql(self, dialect: BaseDialect, state: dict[str, Any]) -> list[str]:
        """Render the SQL statements that apply this operation.

        Args:
            dialect: Active dialect used to render SQL.
            state: Schema state as it exists before this operation runs.

        Returns:
            The SQL statements to run when applying the operation.
        """
        raise NotImplementedError

    def backward_sql(self, dialect: BaseDialect, state: dict[str, Any]) -> list[str]:
        """Render the SQL statements that revert this operation.

        Args:
            dialect: Active dialect used to render SQL.
            state: Schema state as it exists before this operation is reverted
                (i.e. with the operation applied).

        Returns:
            The SQL statements to run when reverting the operation.
        """
        raise NotImplementedError

    def apply_state(self, state: dict[str, Any]) -> None:
        """Evolve the schema state forward to reflect this operation.

        Args:
            state: Mutable schema state to update in place.

        Returns:
            None
        """

    def revert_state(self, state: dict[str, Any]) -> None:
        """Evolve the schema state backward, undoing :meth:`apply_state`.

        Args:
            state: Mutable schema state to update in place.

        Returns:
            None
        """

    def to_source(self) -> str:
        """Render this operation as Python source for a migration file.

        Returns:
            The source code constructing this operation.
        """
        raise NotImplementedError


class _ReversibleOp(Operation):
    """Base for a paired ``Add``/``Remove`` operation.

    A subclass implements the forward primitives once — ``_do_sql`` (the
    create/add action), ``_undo_sql`` (the drop/remove action) and the two state
    transitions ``_do_state``/``_undo_state`` — plus ``__init__`` and
    ``to_source``. The ``Remove`` side sets ``_reverse = True`` to run the same
    primitives backwards, so the SQL and state logic is single-sourced instead of
    mirrored across two near-identical classes.

    The ``safe`` (``IF [NOT] EXISTS``) guard always applies to the operation's
    own *forward* action; the reverse SQL uses ``_backward_safe`` (constant per
    pair, so e.g. a composite index recreated on reverse keeps its guard).
    """

    #: ``IF [NOT] EXISTS`` guard on the forward action.
    safe = False
    #: Guard passed to the reverse SQL (recreation/removal); constant per pair.
    _backward_safe = False
    #: Whether this op is the reverse (``Remove``) side of the pair.
    _reverse = False

    def forward_sql(self, dialect: BaseDialect, state: dict[str, Any]) -> list[str]:
        """Render the forward SQL: the reverse primitive when ``_reverse``.

        Args:
            dialect: Active dialect used to render SQL.
            state: Current schema state (unused).

        Returns:
            The SQL statements that apply the operation.
        """
        prim = self._undo_sql if self._reverse else self._do_sql
        return prim(dialect, self.safe)

    def backward_sql(self, dialect: BaseDialect, state: dict[str, Any]) -> list[str]:
        """Render the reverse SQL: the forward primitive when ``_reverse``.

        Args:
            dialect: Active dialect used to render SQL.
            state: Current schema state (unused).

        Returns:
            The SQL statements that revert the operation.
        """
        prim = self._do_sql if self._reverse else self._undo_sql
        return prim(dialect, self._backward_safe)

    def apply_state(self, state: dict[str, Any]) -> None:
        """Evolve the state forward (the ``_undo`` transition when ``_reverse``).

        Args:
            state: Mutable schema state to update in place.

        Returns:
            None
        """
        (self._undo_state if self._reverse else self._do_state)(state)

    def revert_state(self, state: dict[str, Any]) -> None:
        """Evolve the state backward (the ``_do`` transition when ``_reverse``).

        Args:
            state: Mutable schema state to update in place.

        Returns:
            None
        """
        (self._do_state if self._reverse else self._undo_state)(state)

    def _do_sql(self, dialect: BaseDialect, safe: bool) -> list[str]:  # pragma: no cover - abstract
        """Render the create/add action's SQL."""
        raise NotImplementedError

    def _undo_sql(self, dialect: BaseDialect, safe: bool) -> list[str]:  # pragma: no cover
        """Render the drop/remove action's SQL."""
        raise NotImplementedError

    def _do_state(self, state: dict[str, Any]) -> None:  # pragma: no cover - abstract
        """Apply the create/add action to the schema state."""
        raise NotImplementedError

    def _undo_state(self, state: dict[str, Any]) -> None:  # pragma: no cover - abstract
        """Apply the drop/remove action to the schema state."""
        raise NotImplementedError


class CreateModel(Operation):
    """Create a table from a field set (columns, pk, foreign keys, indexes)."""

    #: Whether the rendered ``CREATE TABLE`` carries an ``IF NOT EXISTS`` guard.
    safe = False

    def __init__(
        self,
        table: str,
        fields: dict[str, Field],
        composite_pk: list[str] | None = None,
        composite_indexes: dict[str, list[str]] | None = None,
        constraints: list[Constraint] | None = None,
    ) -> None:
        """Store the table name and its field set.

        Args:
            table: Name of the table to create.
            fields: Mapping of column name to field.
            composite_pk: Column names forming a composite primary key, if any.
            composite_indexes: ``Meta.indexes`` as a mapping of index name to its
                ordered columns, rendered inline so the table is created with
                them (works on SQLite, which cannot ALTER them in afterwards).
            constraints: ``Meta.constraints`` (named ``UniqueConstraint`` /
                ``CheckConstraint`` objects), rendered inline in the
                ``CREATE TABLE``.

        Returns:
            None
        """
        self.table = table
        self.fields = fields
        self.composite_pk = composite_pk
        self.composite_indexes = composite_indexes or {}
        self.constraints: list[Constraint] = list(constraints or [])

    def forward_sql(self, dialect: BaseDialect, state: dict[str, Any]) -> list[str]:
        """Render the ``CREATE TABLE`` statements.

        Args:
            dialect: Active dialect used to render SQL.
            state: Current schema state (unused).

        Returns:
            The SQL statements that create the table.
        """
        spec = _tspec(
            {
                "fields": self.fields,
                "composite_pk": self.composite_pk,
                "composite_indexes": self.composite_indexes,
                "constraints": [c.to_spec() for c in self.constraints],
            }
        )
        return dialect.render_create_table(self.table, spec, safe=self.safe)

    def backward_sql(self, dialect: BaseDialect, state: dict[str, Any]) -> list[str]:
        """Render the ``DROP TABLE`` statements.

        Args:
            dialect: Active dialect used to render SQL.
            state: Current schema state (unused).

        Returns:
            The SQL statements that drop the table.
        """
        return dialect.render_drop_table(self.table)

    def apply_state(self, state: dict[str, Any]) -> None:
        """Record the new table in the schema state.

        Args:
            state: Mutable schema state to update in place.

        Returns:
            None
        """
        tstate = _new_tstate(self.fields, self.composite_pk)
        tstate["composite_indexes"] = dict(self.composite_indexes)
        tstate["constraints"] = [c.to_spec() for c in self.constraints]
        state["tables"][self.table] = tstate

    def revert_state(self, state: dict[str, Any]) -> None:
        """Remove the table from the schema state.

        Args:
            state: Mutable schema state to update in place.

        Returns:
            None
        """
        state["tables"].pop(self.table, None)

    def to_source(self) -> str:
        """Render this operation as Python source for a migration file.

        Returns:
            The source code constructing this operation.
        """
        args = [repr(self.table), f"fields={_fields_source(self.fields, 8)}"]
        if self.composite_pk:
            args.append(f"composite_pk={self.composite_pk!r}")
        if self.composite_indexes:
            args.append(f"composite_indexes={self.composite_indexes!r}")
        if self.constraints:
            cons_src = ", ".join(c.to_source() for c in self.constraints)
            args.append(f"constraints=[{cons_src}]")
        return _call(f"m.{type(self).__name__}", args)


class CreateModelIfNotExists(CreateModel):
    """Idempotent :class:`CreateModel`: emits an ``IF NOT EXISTS`` guard."""

    safe = True


class DeleteModel(Operation):
    """Drop a table, keeping its field set so the operation can be reversed."""

    def __init__(
        self,
        table: str,
        fields: dict[str, Field],
        composite_pk: list[str] | None = None,
        composite_indexes: dict[str, Any] | None = None,
        constraints: list[Constraint] | None = None,
    ) -> None:
        """Store the table name and the field set needed to recreate it.

        Args:
            table: Name of the table to drop.
            fields: Mapping of column name to field, used to recreate on reverse.
            composite_pk: Column names forming a composite primary key, if any.
            composite_indexes: The table's composite-index specs, kept so a
                reverse (recreate) restores them instead of silently dropping
                ``Meta.indexes``.
            constraints: The table's named constraints, kept for the reverse.

        Returns:
            None
        """
        self.table = table
        self.fields = fields
        self.composite_pk = composite_pk
        self.composite_indexes = dict(composite_indexes or {})
        self.constraints: list[Constraint] = list(constraints or [])

    def forward_sql(self, dialect: BaseDialect, state: dict[str, Any]) -> list[str]:
        """Render the ``DROP TABLE`` statements.

        Args:
            dialect: Active dialect used to render SQL.
            state: Current schema state (unused).

        Returns:
            The SQL statements that drop the table.
        """
        return dialect.render_drop_table(self.table)

    def backward_sql(self, dialect: BaseDialect, state: dict[str, Any]) -> list[str]:
        """Render the ``CREATE TABLE`` statements that restore the table.

        Args:
            dialect: Active dialect used to render SQL.
            state: Current schema state (unused).

        Returns:
            The SQL statements that recreate the table.
        """
        spec = _tspec(
            {
                "fields": self.fields,
                "composite_pk": self.composite_pk,
                "composite_indexes": self.composite_indexes,
                "constraints": [c.to_spec() for c in self.constraints],
            }
        )
        return dialect.render_create_table(self.table, spec, safe=False)

    def apply_state(self, state: dict[str, Any]) -> None:
        """Remove the table from the schema state.

        Args:
            state: Mutable schema state to update in place.

        Returns:
            None
        """
        state["tables"].pop(self.table, None)

    def revert_state(self, state: dict[str, Any]) -> None:
        """Restore the table in the schema state.

        Args:
            state: Mutable schema state to update in place.

        Returns:
            None
        """
        tstate = _new_tstate(self.fields, self.composite_pk)
        tstate["composite_indexes"] = dict(self.composite_indexes)
        tstate["constraints"] = [c.to_spec() for c in self.constraints]
        state["tables"][self.table] = tstate

    def to_source(self) -> str:
        """Render this operation as Python source for a migration file.

        Returns:
            The source code constructing this operation.
        """
        args = [repr(self.table), f"fields={_fields_source(self.fields, 8)}"]
        if self.composite_pk:
            args.append(f"composite_pk={self.composite_pk!r}")
        if self.composite_indexes:
            args.append(f"composite_indexes={self.composite_indexes!r}")
        if self.constraints:
            cons_src = ", ".join(c.to_source() for c in self.constraints)
            args.append(f"constraints=[{cons_src}]")
        return _call(f"m.{type(self).__name__}", args)


class DeleteModelIfExists(DeleteModel):
    """Idempotent :class:`DeleteModel` (drop already guards with ``IF EXISTS``)."""


class _FieldColumnOp(_ReversibleOp):
    """Shared add/drop-column primitives for :class:`AddField`/:class:`RemoveField`."""

    def __init__(self, table: str, name: str, field: Field) -> None:
        """Store the column and the field describing it.

        Args:
            table: Name of the table to alter.
            name: Name of the column.
            field: The field describing the column (used to recreate on reverse).

        Returns:
            None
        """
        self.table = table
        self.name = name
        self.field = field

    def _do_sql(self, dialect: BaseDialect, safe: bool) -> list[str]:
        """Render the ``ADD COLUMN`` statements.

        Args:
            dialect: Active dialect used to render SQL.
            safe: Whether to emit the ``IF NOT EXISTS`` guard.

        Returns:
            The SQL statements that add the column.
        """
        return dialect.render_add_column(self.table, self.name, _column_spec(self.field), safe)

    def _undo_sql(self, dialect: BaseDialect, safe: bool) -> list[str]:
        """Render the ``DROP COLUMN`` statements.

        Args:
            dialect: Active dialect used to render SQL.
            safe: Whether to emit the ``IF EXISTS`` guard.

        Returns:
            The SQL statements that drop the column.
        """
        return dialect.render_drop_column(self.table, self.name, safe)

    def _do_state(self, state: dict[str, Any]) -> None:
        """Record the column in the schema state.

        Args:
            state: Mutable schema state to update in place.

        Returns:
            None
        """
        state["tables"][self.table]["fields"][self.name] = self.field

    def _undo_state(self, state: dict[str, Any]) -> None:
        """Remove the column from the schema state.

        Args:
            state: Mutable schema state to update in place.

        Returns:
            None
        """
        state["tables"][self.table]["fields"].pop(self.name, None)

    def to_source(self) -> str:
        """Render this operation as Python source for a migration file.

        Returns:
            The source code constructing this operation.
        """
        return _call(
            f"m.{type(self).__name__}",
            [repr(self.table), repr(self.name), _field_source(self.field)],
        )


class AddField(_FieldColumnOp):
    """Add a column to a table from a field object."""


class AddFieldIfNotExists(AddField):
    """Idempotent :class:`AddField`: emits an ``IF NOT EXISTS`` guard."""

    safe = True


class RemoveField(_FieldColumnOp):
    """Drop a column, keeping its field so the operation can be reversed."""

    _reverse = True


class RemoveFieldIfExists(RemoveField):
    """Idempotent :class:`RemoveField`: emits an ``IF EXISTS`` guard."""

    safe = True


class AlterField(Operation):
    """Change a column's type/nullability, carrying both the new and old field."""

    def __init__(self, table: str, name: str, field: Field, old: Field) -> None:
        """Store the column being altered and its before/after fields.

        Args:
            table: Name of the table to alter.
            name: Name of the column being altered.
            field: The field describing the column after the change.
            old: The field describing the column before the change.

        Returns:
            None
        """
        self.table = table
        self.name = name
        self.field = field
        self.old = old

    def _alter(
        self, dialect: BaseDialect, state: dict[str, Any], new: Field, old: Field
    ) -> list[str]:
        """Render the statements that change the column from ``old`` to ``new``.

        Builds the full post-change table spec from ``state`` so dialects that
        rebuild the table (SQLite) have the complete column set.

        Args:
            dialect: Active dialect used to render SQL.
            state: Current schema state (carries the table's other columns).
            new: The target field for the column.
            old: The current field for the column.

        Returns:
            The SQL statements applying the column change.
        """
        tstate = state["tables"][self.table]
        post_fields = dict(tstate["fields"])
        post_fields[self.name] = new
        post_spec = _tspec(
            {
                "fields": post_fields,
                "composite_pk": tstate.get("composite_pk"),
                "indexes": tstate.get("indexes"),
                "composite_indexes": tstate.get("composite_indexes"),
                "constraints": tstate.get("constraints"),
            }
        )
        return dialect.render_alter_column(
            self.table, self.name, _column_spec(old), _column_spec(new), post_spec
        )

    def forward_sql(self, dialect: BaseDialect, state: dict[str, Any]) -> list[str]:
        """Render the statements applying the change.

        Args:
            dialect: Active dialect used to render SQL.
            state: Current schema state (carries the pre-change columns).

        Returns:
            The SQL statements applying the change.
        """
        return self._alter(dialect, state, self.field, self.old)

    def backward_sql(self, dialect: BaseDialect, state: dict[str, Any]) -> list[str]:
        """Render the statements reverting the change.

        Args:
            dialect: Active dialect used to render SQL.
            state: Current schema state (carries the post-change columns).

        Returns:
            The SQL statements reverting the change.
        """
        return self._alter(dialect, state, self.old, self.field)

    def apply_state(self, state: dict[str, Any]) -> None:
        """Replace the column's field with the post-change field.

        Args:
            state: Mutable schema state to update in place.

        Returns:
            None
        """
        state["tables"][self.table]["fields"][self.name] = self.field

    def revert_state(self, state: dict[str, Any]) -> None:
        """Restore the column's pre-change field.

        Args:
            state: Mutable schema state to update in place.

        Returns:
            None
        """
        state["tables"][self.table]["fields"][self.name] = self.old

    def to_source(self) -> str:
        """Render this operation as Python source for a migration file.

        Returns:
            The source code constructing this operation.
        """
        return _call(
            "m.AlterField",
            [
                repr(self.table),
                repr(self.name),
                _field_source(self.field),
                f"old={_field_source(self.old)}",
            ],
        )


class _SingleColumnIndexOp(_ReversibleOp):
    """Shared create/drop-index primitives for :class:`AddIndex`/:class:`RemoveIndex`."""

    #: Whether the index is built/dropped ``CONCURRENTLY`` (PostgreSQL, non-atomic).
    concurrently = False
    #: Whether the index is ``UNIQUE`` (only meaningful on the create side).
    unique = False

    def __init__(self, table: str, column: str) -> None:
        """Store the table and indexed column.

        Args:
            table: Name of the table.
            column: Name of the indexed column.

        Returns:
            None
        """
        self.table = table
        self.column = column

    def _do_sql(self, dialect: BaseDialect, safe: bool) -> list[str]:
        """Render the ``CREATE INDEX`` statements.

        Args:
            dialect: Active dialect used to render SQL.
            safe: Whether to emit the ``IF NOT EXISTS`` guard.

        Returns:
            The SQL statements that create the index.
        """
        return dialect.render_create_index(
            self.table, self.column, safe=safe, unique=self.unique, concurrently=self.concurrently
        )

    def _undo_sql(self, dialect: BaseDialect, safe: bool) -> list[str]:
        """Render the ``DROP INDEX`` statements (always ``IF EXISTS``).

        Args:
            dialect: Active dialect used to render SQL.
            safe: Unused; the drop is inherently idempotent.

        Returns:
            The SQL statements that drop the index.
        """
        return dialect.render_drop_index(self.table, self.column, concurrently=self.concurrently)

    def _do_state(self, state: dict[str, Any]) -> None:
        """Record the index in the schema state.

        Args:
            state: Mutable schema state to update in place.

        Returns:
            None
        """
        idx = state["tables"][self.table].setdefault("indexes", [])
        if self.column not in idx:
            idx.append(self.column)

    def _undo_state(self, state: dict[str, Any]) -> None:
        """Remove the index from the schema state.

        Args:
            state: Mutable schema state to update in place.

        Returns:
            None
        """
        idx = state["tables"][self.table].get("indexes", [])
        if self.column in idx:
            idx.remove(self.column)

    def to_source(self) -> str:
        """Render this operation as Python source for a migration file.

        Returns:
            The source code constructing this operation.
        """
        return _call(f"m.{type(self).__name__}", [repr(self.table), repr(self.column)])


class AddIndex(_SingleColumnIndexOp):
    """Create an index on a single column of a table."""


class AddIndexIfNotExists(AddIndex):
    """Idempotent :class:`AddIndex`: emits an ``IF NOT EXISTS`` guard."""

    safe = True


class AddIndexConcurrently(AddIndex):
    """Build the index ``CONCURRENTLY`` (PostgreSQL; requires ``atomic = False``)."""

    safe = True
    concurrently = True


class AddUniqueIndexConcurrently(AddIndex):
    """Build a ``UNIQUE`` index ``CONCURRENTLY`` (requires ``atomic = False``)."""

    safe = True
    concurrently = True
    unique = True


class RemoveIndex(_SingleColumnIndexOp):
    """Drop an index from a single column of a table."""

    _reverse = True


class RemoveIndexIfExists(RemoveIndex):
    """Idempotent :class:`RemoveIndex` (drop already guards with ``IF EXISTS``)."""


class RemoveIndexConcurrently(RemoveIndex):
    """Drop the index ``CONCURRENTLY`` (PostgreSQL; requires ``atomic = False``)."""

    concurrently = True


class _CompositeIndexOp(_ReversibleOp):
    """Shared create/drop primitives for the multi-column index pair.

    ``_backward_safe`` is ``True`` so recreating the index on reverse keeps its
    ``IF NOT EXISTS`` guard (the historical behaviour of the create renderer's
    default).
    """

    _backward_safe = True

    def __init__(
        self,
        table: str,
        name: str,
        columns: list[str],
        condition: str | None = None,
        unique: bool = False,
        using: str | None = None,
        include: list[str] | None = None,
        opclass: str | None = None,
    ) -> None:
        """Store the index's table, name, columns and rendering options.

        Args:
            table: Name of the table.
            name: The index name.
            columns: The ordered columns the index covers.
            condition: Optional partial-index predicate (raw SQL ``WHERE``).
            unique: Whether the index enforces uniqueness.
            using: Optional access method (``USING <method>``; PostgreSQL-only).
            include: Optional non-key covering columns (``INCLUDE (...)``;
                PostgreSQL-only).
            opclass: Optional per-column operator class (PostgreSQL-only).

        Returns:
            None
        """
        self.table = table
        self.name = name
        self.columns = list(columns)
        self.condition = condition
        self.unique = unique
        self.using = using
        self.include = list(include) if include else None
        self.opclass = opclass

    def _do_sql(self, dialect: BaseDialect, safe: bool) -> list[str]:
        """Render the ``CREATE INDEX`` statements.

        Args:
            dialect: Active dialect used to render SQL.
            safe: Whether to emit the ``IF NOT EXISTS`` guard.

        Returns:
            The SQL statements that create the index.
        """
        return dialect.render_create_composite_index(
            self.table,
            self.name,
            self.columns,
            safe=safe,
            condition=self.condition,
            unique=self.unique,
            using=self.using,
            include=self.include,
            opclass=self.opclass,
        )

    def _undo_sql(self, dialect: BaseDialect, safe: bool) -> list[str]:
        """Render the ``DROP INDEX`` statements.

        Args:
            dialect: Active dialect used to render SQL.
            safe: Unused; the drop guards ``IF EXISTS`` itself.

        Returns:
            The SQL statements that drop the index.
        """
        return dialect.render_drop_composite_index(self.name)

    def _do_state(self, state: dict[str, Any]) -> None:
        """Record the composite index in the schema state.

        Args:
            state: Mutable schema state to update in place.

        Returns:
            None
        """
        idx = state["tables"][self.table].setdefault("composite_indexes", {})
        idx[self.name] = _index_spec(
            self.columns, self.condition, self.unique, self.using, self.include, self.opclass
        )

    def _undo_state(self, state: dict[str, Any]) -> None:
        """Remove the composite index from the schema state.

        Args:
            state: Mutable schema state to update in place.

        Returns:
            None
        """
        state["tables"][self.table].get("composite_indexes", {}).pop(self.name, None)

    def to_source(self) -> str:
        """Render this operation as Python source for a migration file.

        Returns:
            The source code constructing this operation.
        """
        args = [repr(self.table), repr(self.name), repr(self.columns)]
        args.extend(
            _index_option_source(
                self.condition, self.unique, self.using, self.include, self.opclass
            )
        )
        return _call(f"m.{type(self).__name__}", args)


class AddCompositeIndex(_CompositeIndexOp):
    """Create a multi-column index (from ``Meta.indexes``)."""


class AddCompositeIndexIfNotExists(AddCompositeIndex):
    """Idempotent :class:`AddCompositeIndex`: emits an ``IF NOT EXISTS`` guard."""

    safe = True


class RemoveCompositeIndex(_CompositeIndexOp):
    """Drop a multi-column index (keeping its definition so it can be reversed)."""

    _reverse = True


class RemoveCompositeIndexIfExists(RemoveCompositeIndex):
    """Idempotent :class:`RemoveCompositeIndex` (drop already guards ``IF EXISTS``)."""


# -- rename operations (hand-written) ---------------------------------------
class RenameModel(Operation):
    """Rename a table."""

    def __init__(self, old: str, new: str) -> None:
        """Store the current and new table names.

        Args:
            old: The current table name.
            new: The new table name.

        Returns:
            None
        """
        self.old = old
        self.new = new

    def forward_sql(self, dialect: BaseDialect, state: dict[str, Any]) -> list[str]:
        """Render the rename-table statements.

        Args:
            dialect: Active dialect used to render SQL.
            state: Current schema state (unused).

        Returns:
            The SQL statements that rename the table.
        """
        return dialect.render_rename_table(self.old, self.new)

    def backward_sql(self, dialect: BaseDialect, state: dict[str, Any]) -> list[str]:
        """Render the statements that rename the table back.

        Args:
            dialect: Active dialect used to render SQL.
            state: Current schema state (unused).

        Returns:
            The SQL statements that restore the original table name.
        """
        return dialect.render_rename_table(self.new, self.old)

    def apply_state(self, state: dict[str, Any]) -> None:
        """Move the table entry under its new name.

        Args:
            state: Mutable schema state to update in place.

        Returns:
            None
        """
        state["tables"][self.new] = state["tables"].pop(self.old)

    def revert_state(self, state: dict[str, Any]) -> None:
        """Move the table entry back under its original name.

        Args:
            state: Mutable schema state to update in place.

        Returns:
            None
        """
        state["tables"][self.old] = state["tables"].pop(self.new)

    def to_source(self) -> str:
        """Render this operation as Python source for a migration file.

        Returns:
            The source code constructing this operation.
        """
        return _call("m.RenameModel", [repr(self.old), repr(self.new)])


class RenameField(Operation):
    """Rename a column on a table."""

    def __init__(self, table: str, old: str, new: str) -> None:
        """Store the table and the column's current and new names.

        Args:
            table: The table owning the column.
            old: The current column name.
            new: The new column name.

        Returns:
            None
        """
        self.table = table
        self.old = old
        self.new = new

    def forward_sql(self, dialect: BaseDialect, state: dict[str, Any]) -> list[str]:
        """Render the rename-column statements.

        Args:
            dialect: Active dialect used to render SQL.
            state: Current schema state (unused).

        Returns:
            The SQL statements that rename the column.
        """
        return dialect.render_rename_column(self.table, self.old, self.new)

    def backward_sql(self, dialect: BaseDialect, state: dict[str, Any]) -> list[str]:
        """Render the statements that rename the column back.

        Args:
            dialect: Active dialect used to render SQL.
            state: Current schema state (unused).

        Returns:
            The SQL statements that restore the original column name.
        """
        return dialect.render_rename_column(self.table, self.new, self.old)

    def apply_state(self, state: dict[str, Any]) -> None:
        """Rename the column in the schema state.

        Args:
            state: Mutable schema state to update in place.

        Returns:
            None
        """
        _rename_in_table(state["tables"][self.table], self.old, self.new)

    def revert_state(self, state: dict[str, Any]) -> None:
        """Restore the column's original name in the schema state.

        Args:
            state: Mutable schema state to update in place.

        Returns:
            None
        """
        _rename_in_table(state["tables"][self.table], self.new, self.old)

    def to_source(self) -> str:
        """Render this operation as Python source for a migration file.

        Returns:
            The source code constructing this operation.
        """
        return _call("m.RenameField", [repr(self.table), repr(self.old), repr(self.new)])


class RenameIndex(Operation):
    """Rename an index (PostgreSQL in place; SQLite drops and recreates it)."""

    def __init__(
        self, table: str, column: str, old_name: str, new_name: str, unique: bool = False
    ) -> None:
        """Store the index's table, column and current/new names.

        Args:
            table: The table owning the index.
            column: The indexed column (used to recreate on rebuild dialects).
            old_name: The current index name.
            new_name: The new index name.
            unique: Whether the recreated index should be ``UNIQUE``.

        Returns:
            None
        """
        self.table = table
        self.column = column
        self.old_name = old_name
        self.new_name = new_name
        self.unique = unique

    def forward_sql(self, dialect: BaseDialect, state: dict[str, Any]) -> list[str]:
        """Render the rename-index statements.

        Args:
            dialect: Active dialect used to render SQL.
            state: Current schema state (unused).

        Returns:
            The SQL statements that rename the index.
        """
        return dialect.render_rename_index(
            self.table, self.column, self.old_name, self.new_name, unique=self.unique
        )

    def backward_sql(self, dialect: BaseDialect, state: dict[str, Any]) -> list[str]:
        """Render the statements that rename the index back.

        Args:
            dialect: Active dialect used to render SQL.
            state: Current schema state (unused).

        Returns:
            The SQL statements that restore the original index name.
        """
        return dialect.render_rename_index(
            self.table, self.column, self.new_name, self.old_name, unique=self.unique
        )

    def to_source(self) -> str:
        """Render this operation as Python source for a migration file.

        Returns:
            The source code constructing this operation.
        """
        args = [repr(self.table), repr(self.column), repr(self.old_name), repr(self.new_name)]
        if self.unique:
            args.append("unique=True")
        return _call("m.RenameIndex", args)


# -- constraint operations (hand-written; SQLite raises UnSupportedError) ----
class _ConstraintOp(_ReversibleOp):
    """Shared add/drop primitives for :class:`AddConstraint`/:class:`RemoveConstraint`.

    On dialects without ``ALTER TABLE ... ADD/DROP CONSTRAINT`` (SQLite) the
    operation falls back to a full table rebuild carrying the post-change
    constraint set, using the table state replayed from prior migrations.
    """

    def __init__(self, table: str, constraint: Constraint) -> None:
        """Store the table and the constraint definition.

        Args:
            table: The constrained table.
            constraint: The constraint definition (must be named to be
                reversible; kept so the reverse can recreate it).

        Returns:
            None
        """
        self.table = table
        self.constraint = constraint

    def forward_sql(self, dialect: BaseDialect, state: dict[str, Any]) -> list[str]:
        """Render the forward SQL, rebuilding the table on SQLite.

        Args:
            dialect: Active dialect used to render SQL.
            state: Current schema state (used for the rebuild fallback).

        Returns:
            The SQL statements that apply the operation.
        """
        if not dialect.alter_constraint_in_place:
            return self._rebuild_sql(dialect, state, adding=not self._reverse)
        return super().forward_sql(dialect, state)

    def backward_sql(self, dialect: BaseDialect, state: dict[str, Any]) -> list[str]:
        """Render the reverse SQL, rebuilding the table on SQLite.

        Args:
            dialect: Active dialect used to render SQL.
            state: Current schema state (used for the rebuild fallback).

        Returns:
            The SQL statements that revert the operation.
        """
        if not dialect.alter_constraint_in_place:
            return self._rebuild_sql(dialect, state, adding=self._reverse)
        return super().backward_sql(dialect, state)

    def _rebuild_sql(self, dialect: BaseDialect, state: dict[str, Any], adding: bool) -> list[str]:
        """Render a table rebuild whose constraint set includes/omits this one.

        Args:
            dialect: Active dialect used to render SQL.
            state: Current schema state (must contain the table).
            adding: Whether the rebuilt table carries the constraint.

        Raises:
            UnSupportedError: When the table is unknown to the migration state
                (e.g. it was created via ``RunSQL``), so no rebuild spec exists.

        Returns:
            The SQL statements rebuilding the table.
        """
        tstate = state["tables"].get(self.table)
        if tstate is None:
            raise UnSupportedError(
                f"{dialect.name} cannot ALTER constraints in place and table {self.table!r} "
                "is not tracked by the migration state; rebuild the table via RunSQL"
            )
        spec = self.constraint.to_spec()
        constraints = [c for c in tstate.get("constraints", []) if c.get("name") != spec["name"]]
        if adding:
            constraints.append(spec)
        table_spec = _tspec({**tstate, "constraints": constraints})
        return dialect.render_rebuild_table(self.table, table_spec)

    def _do_sql(self, dialect: BaseDialect, safe: bool) -> list[str]:
        """Render the add-constraint statements.

        Args:
            dialect: Active dialect used to render SQL.
            safe: Unused; constraints carry no ``IF [NOT] EXISTS`` guard.

        Returns:
            The SQL statements that add the constraint.
        """
        return dialect.render_add_constraint(self.table, self.constraint.to_spec())

    def _undo_sql(self, dialect: BaseDialect, safe: bool) -> list[str]:
        """Render the drop-constraint statements.

        Args:
            dialect: Active dialect used to render SQL.
            safe: Unused; constraints carry no ``IF [NOT] EXISTS`` guard.

        Returns:
            The SQL statements that drop the constraint.
        """
        return dialect.render_drop_constraint(self.table, cast(str, self.constraint.name))

    def _do_state(self, state: dict[str, Any]) -> None:
        """Record the constraint in the schema state.

        Args:
            state: Mutable schema state to update in place.

        Returns:
            None
        """
        state["tables"][self.table].setdefault("constraints", []).append(self.constraint.to_spec())

    def _undo_state(self, state: dict[str, Any]) -> None:
        """Remove the constraint from the schema state.

        Args:
            state: Mutable schema state to update in place.

        Returns:
            None
        """
        cons = state["tables"][self.table].get("constraints", [])
        state["tables"][self.table]["constraints"] = [
            c for c in cons if c.get("name") != self.constraint.name
        ]

    def to_source(self) -> str:
        """Render this operation as Python source for a migration file.

        Returns:
            The source code constructing this operation.
        """
        return _call(f"m.{type(self).__name__}", [repr(self.table), self.constraint.to_source()])


class AddConstraint(_ConstraintOp):
    """Add a unique or check constraint to a table."""


class RemoveConstraint(_ConstraintOp):
    """Drop a constraint, keeping its definition so it can be reversed."""

    _reverse = True


class RenameConstraint(Operation):
    """Rename a named constraint on a table."""

    def __init__(self, table: str, old: str, new: str) -> None:
        """Store the table and the constraint's current and new names.

        Args:
            table: The constrained table.
            old: The current constraint name.
            new: The new constraint name.

        Returns:
            None
        """
        self.table = table
        self.old = old
        self.new = new

    def forward_sql(self, dialect: BaseDialect, state: dict[str, Any]) -> list[str]:
        """Render the rename-constraint statements.

        Args:
            dialect: Active dialect used to render SQL.
            state: Current schema state (unused).

        Returns:
            The SQL statements that rename the constraint.
        """
        return dialect.render_rename_constraint(self.table, self.old, self.new)

    def backward_sql(self, dialect: BaseDialect, state: dict[str, Any]) -> list[str]:
        """Render the statements that rename the constraint back.

        Args:
            dialect: Active dialect used to render SQL.
            state: Current schema state (unused).

        Returns:
            The SQL statements that restore the original constraint name.
        """
        return dialect.render_rename_constraint(self.table, self.new, self.old)

    def apply_state(self, state: dict[str, Any]) -> None:
        """Rename the constraint in the schema state.

        Args:
            state: Mutable schema state to update in place.

        Returns:
            None
        """
        for c in state["tables"][self.table].get("constraints", []):
            if c.get("name") == self.old:
                c["name"] = self.new

    def revert_state(self, state: dict[str, Any]) -> None:
        """Restore the constraint's original name in the schema state.

        Args:
            state: Mutable schema state to update in place.

        Returns:
            None
        """
        for c in state["tables"][self.table].get("constraints", []):
            if c.get("name") == self.new:
                c["name"] = self.old

    def to_source(self) -> str:
        """Render this operation as Python source for a migration file.

        Returns:
            The source code constructing this operation.
        """
        return _call("m.RenameConstraint", [repr(self.table), repr(self.old), repr(self.new)])


class RunSQL(Operation):
    """Run arbitrary SQL (forward and, optionally, its reverse)."""

    def __init__(self, sql: str | list[str], reverse_sql: str | list[str] | None = None) -> None:
        """Normalise the forward and reverse SQL into lists.

        Args:
            sql: Forward SQL statement or list of statements.
            reverse_sql: Reverse SQL statement(s), if reversible.

        Returns:
            None
        """
        self.sql = [sql] if isinstance(sql, str) else list(sql)
        self.reverse_sql = (
            [reverse_sql] if isinstance(reverse_sql, str) else list(reverse_sql or [])
        )

    def forward_sql(self, dialect: BaseDialect, state: dict[str, Any]) -> list[str]:
        """Return the forward SQL statements verbatim.

        Args:
            dialect: Active dialect (unused; SQL is supplied literally).
            state: Current schema state (unused).

        Returns:
            The forward SQL statements.
        """
        return self.sql

    def backward_sql(self, dialect: BaseDialect, state: dict[str, Any]) -> list[str]:
        """Return the reverse SQL statements verbatim.

        Args:
            dialect: Active dialect (unused; SQL is supplied literally).
            state: Current schema state (unused).

        Returns:
            The reverse SQL statements.
        """
        return self.reverse_sql

    def to_source(self) -> str:
        """Render this operation as Python source for a migration file.

        Returns:
            The source code constructing this operation.
        """
        return _call("m.RunSQL", [_fmt(self.sql, 8), f"reverse_sql={_fmt(self.reverse_sql, 8)}"])


class RunPython(Operation):
    """Run async Python callables (hand-written migrations only)."""

    def __init__(
        self,
        forward: Callable[[], Awaitable[None]] | None,
        backward: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Store the forward and reverse async callables.

        Args:
            forward: Async callable run when applying the migration.
            backward: Async callable run when reverting the migration.

        Returns:
            None
        """
        self.forward = forward
        self.backward = backward

    async def run_forward(self) -> None:
        """Invoke the forward callable, if any.

        Returns:
            None
        """
        if self.forward:
            await self.forward()

    async def run_backward(self) -> None:
        """Invoke the reverse callable, if any.

        Returns:
            None
        """
        if self.backward:
            await self.backward()

    def forward_sql(self, dialect: BaseDialect, state: dict[str, Any]) -> list[str]:
        """Return no SQL; this operation runs Python instead.

        Args:
            dialect: Active dialect (unused).
            state: Current schema state (unused).

        Returns:
            An empty list.
        """
        return []

    def backward_sql(self, dialect: BaseDialect, state: dict[str, Any]) -> list[str]:
        """Return no SQL; this operation runs Python instead.

        Args:
            dialect: Active dialect (unused).
            state: Current schema state (unused).

        Returns:
            An empty list.
        """
        return []


# ---------------------------------------------------------------------------
# Schema state from models + diffing
# ---------------------------------------------------------------------------
def model_state(models: list[type[Model]] | None = None) -> dict[str, Any]:
    """Build the current schema state (tables + M2M join tables) from models.

    Pass ``models`` to scope to a subset (defaults to every registered model).

    Args:
        models: Models to inspect, or None for every registered model.

    Returns:
        The schema state mapping with a ``tables`` entry.
    """
    tables: dict[str, Any] = {}
    for model in models if models is not None else registry.all_models():
        meta = model._meta
        fields = {f.db_column: f for f in meta.field_list}
        tstate = _new_tstate(fields, None)
        tstate["composite_indexes"] = _meta_index_specs(meta)
        tstate["constraints"] = _meta_named_constraint_specs(meta)
        tstate["constraints"] += _meta_unique_together_specs(meta)
        tables[meta.table] = tstate

        for info in meta.m2m.values():
            target = info.finalize()
            if info.through in tables:  # pragma: no cover - defensive de-dup
                continue
            join_fields: dict[str, Field] = {
                info.backward_key: ForeignKeyFieldInstance(
                    model.__name__, on_delete=OnDelete.CASCADE
                ),
                info.forward_key: ForeignKeyFieldInstance(
                    target.__name__, on_delete=OnDelete.CASCADE
                ),
            }
            tables[info.through] = _new_tstate(join_fields, [info.backward_key, info.forward_key])
    return {"tables": tables}


def _table_deps(spec: dict[str, Any]) -> set[str]:
    """Return the set of tables a table depends on via foreign keys.

    Args:
        spec: Table spec whose foreign-key targets to collect.

    Returns:
        The set of referenced table names.
    """
    return {ref["table"] for ref in spec.get("fks", {}).values()}


def _topo_order(names: Iterable[str], specs: dict[str, Any]) -> list[str]:
    """Order tables so referenced tables come before referencing ones.

    Args:
        names: Table names to order.
        specs: Mapping of table name to table spec.

    Returns:
        The table names in dependency order.
    """
    names = list(names)
    ordered: list[str] = []
    seen: set[str] = set()

    def visit(name: str) -> None:
        """Depth-first visit a table and its dependencies.

        Args:
            name: Table name to visit.

        Returns:
            None
        """
        if name in seen or name not in names:
            return
        seen.add(name)
        for dep in _table_deps(specs[name]):
            if dep != name:
                visit(dep)
        ordered.append(name)

    for n in names:
        visit(n)
    return ordered


def _alterable(old: dict[str, Any], new: dict[str, Any]) -> bool:
    """Report whether two column specs differ in a way ``AlterField`` handles.

    Args:
        old: The column spec before the change.
        new: The column spec after the change.

    Returns:
        ``True`` if the column's type, nullability, uniqueness, database
        default or foreign-key target/action changed.
    """
    keys = ("kind", "type_params", "null", "unique", "default", "fk")
    return any(old.get(k) != new.get(k) for k in keys)


def diff_states(old: dict[str, Any], new: dict[str, Any]) -> list[Operation]:
    """Compute the operations that transform one schema state into another.

    Generated operations are the **idempotent** analogs (``*IfNotExists`` /
    ``*IfExists``), and column type/nullability changes are emitted as
    :class:`AlterField`.

    Args:
        old: Previous schema state.
        new: Target schema state.

    Returns:
        The ordered operations to migrate from ``old`` to ``new``.
    """
    ops: list[Operation] = []
    old_t, new_t = old["tables"], new["tables"]
    new_specs = {t: _tspec(ts) for t, ts in new_t.items()}
    old_specs = {t: _tspec(ts) for t, ts in old_t.items()}

    for table in _topo_order([t for t in new_t if t not in old_t], new_specs):
        ts = new_t[table]
        ops.append(
            CreateModelIfNotExists(
                table,
                ts["fields"],
                ts.get("composite_pk"),
                composite_indexes=ts.get("composite_indexes") or None,
                constraints=[_constraint_from_spec(c) for c in ts.get("constraints", [])] or None,
            )
        )

    for table in new_t:
        if table not in old_t:
            continue
        old_cols, new_cols = old_specs[table]["columns"], new_specs[table]["columns"]
        old_fields, new_fields = old_t[table]["fields"], new_t[table]["fields"]
        old_fks, new_fks = old_specs[table]["fks"], new_specs[table]["fks"]
        old_idx = set(old_specs[table]["indexes"])
        new_idx = set(new_specs[table]["indexes"])

        # A column that disappears while an identical one appears is treated as a
        # rename (RENAME COLUMN), not a destructive drop+add that would lose data.
        renames = _detect_renames(table, old_cols, new_cols, old_fks, new_fks)
        renamed_old = {old_name for old_name, _ in renames}
        renamed_new = {new_name for _, new_name in renames}

        # Order matters: drop indexes before the columns they reference, and add
        # columns before indexing them (SQLite rejects the reverse).
        for col in sorted(old_idx - new_idx):
            ops.append(RemoveIndexIfExists(table, col))
        for old_name, new_name in renames:
            ops.append(RenameField(table, old_name, new_name))
        for col in old_cols:
            if col not in new_cols and col not in renamed_old:
                ops.append(RemoveFieldIfExists(table, col, old_fields[col]))
        for col in new_cols:
            if col not in old_cols and col not in renamed_new:
                ops.append(AddFieldIfNotExists(table, col, new_fields[col]))
            elif col in old_cols and _alterable(old_cols[col], new_cols[col]):
                ops.append(AlterField(table, col, new_fields[col], old_fields[col]))
        for col in sorted(new_idx - old_idx):
            ops.append(AddIndexIfNotExists(table, col))

        # Composite indexes (Meta.indexes) and named constraints (Meta.constraints).
        ops.extend(_diff_composite_indexes(table, old_t[table], new_t[table]))
        ops.extend(_diff_constraints(table, old_t[table], new_t[table]))

    for table in reversed(_topo_order([t for t in old_t if t not in new_t], old_specs)):
        ts = old_t[table]
        ops.append(
            DeleteModelIfExists(
                table,
                ts["fields"],
                ts.get("composite_pk"),
                composite_indexes=ts.get("composite_indexes") or None,
                constraints=[_constraint_from_spec(c) for c in ts.get("constraints", [])] or None,
            )
        )

    return ops


def _detect_renames(
    table: str,
    old_cols: dict[str, Any],
    new_cols: dict[str, Any],
    old_fks: dict[str, Any],
    new_fks: dict[str, Any],
) -> list[tuple[str, str]]:
    """Pair a dropped column with an added one of identical definition.

    A pair is only formed when the column spec **and** foreign-key spec match
    exactly, so a column whose type also changed is not mistaken for a rename
    (it falls back to drop+add). Pairing is deliberately conservative: a
    dropped column is only matched when exactly one added column shares its
    definition *and* no other dropped column does — an ambiguous set (e.g. two
    same-typed columns renamed at once) is emitted as drop+add with a hint,
    because guessing wrong would silently swap data between columns.

    Args:
        table: The table being diffed (for the ambiguity hint).
        old_cols: Column specs before the change.
        new_cols: Column specs after the change.
        old_fks: Foreign-key specs before the change.
        new_fks: Foreign-key specs after the change.

    Returns:
        A list of ``(old_name, new_name)`` rename pairs.
    """

    def same(r: str, a: str) -> bool:
        """Report whether a dropped and an added column have identical specs.

        Args:
            r: The dropped column name.
            a: The added column name.

        Returns:
            ``True`` when the column and foreign-key specs match exactly.
        """
        return old_cols[r] == new_cols[a] and old_fks.get(r) == new_fks.get(a)

    removed = sorted(c for c in old_cols if c not in new_cols)
    added = sorted(c for c in new_cols if c not in old_cols)
    pairs: list[tuple[str, str]] = []
    ambiguous = False
    for r in removed:
        matches = [a for a in added if same(r, a)]
        if not matches:
            continue
        if len(matches) > 1 or any(r2 != r and same(r2, matches[0]) for r2 in removed):
            ambiguous = True
            continue
        pairs.append((r, matches[0]))
    if ambiguous:
        print(
            f"hint: table {table!r} drops and adds several columns with identical definitions; "
            "emitting drop+add. If these are renames, hand-write m.RenameField(...) operations "
            "to preserve the data.",
            file=sys.stderr,
        )
    return pairs


def _diff_composite_indexes(
    table: str, old_ts: dict[str, Any], new_ts: dict[str, Any]
) -> list[Operation]:
    """Diff ``Meta.indexes`` (composite indexes) between two table states.

    Args:
        table: The table name.
        old_ts: Previous table state.
        new_ts: Target table state.

    Returns:
        The composite-index add/remove operations (a column change for the same
        index name is emitted as a drop followed by a create).
    """
    ops: list[Operation] = []
    old_ci: dict[str, dict[str, Any]] = old_ts.get("composite_indexes") or {}
    new_ci: dict[str, dict[str, Any]] = new_ts.get("composite_indexes") or {}
    for name in sorted(old_ci):
        if name not in new_ci or old_ci[name] != new_ci[name]:
            spec = old_ci[name]
            ops.append(
                RemoveCompositeIndexIfExists(
                    table,
                    name,
                    spec["columns"],
                    condition=spec.get("condition"),
                    unique=spec.get("unique", False),
                    using=spec.get("using"),
                    include=spec.get("include"),
                    opclass=spec.get("opclass"),
                )
            )
    for name in sorted(new_ci):
        if name not in old_ci or old_ci[name] != new_ci[name]:
            spec = new_ci[name]
            ops.append(
                AddCompositeIndexIfNotExists(
                    table,
                    name,
                    spec["columns"],
                    condition=spec.get("condition"),
                    unique=spec.get("unique", False),
                    using=spec.get("using"),
                    include=spec.get("include"),
                    opclass=spec.get("opclass"),
                )
            )
    return ops


def _diff_constraints(
    table: str, old_ts: dict[str, Any], new_ts: dict[str, Any]
) -> list[Operation]:
    """Diff named ``Meta.constraints`` between two table states.

    Args:
        table: The table name.
        old_ts: Previous table state.
        new_ts: Target table state.

    Returns:
        The constraint add/remove operations (a changed spec for the same name
        is emitted as a drop followed by an add).
    """
    ops: list[Operation] = []
    old_c = {c["name"]: c for c in (old_ts.get("constraints") or [])}
    new_c = {c["name"]: c for c in (new_ts.get("constraints") or [])}
    for name in sorted(old_c):
        if name not in new_c or old_c[name] != new_c[name]:
            ops.append(RemoveConstraint(table, _constraint_from_spec(old_c[name])))
    for name in sorted(new_c):
        if name not in old_c or old_c[name] != new_c[name]:
            ops.append(AddConstraint(table, _constraint_from_spec(new_c[name])))
    return ops


def _table_recreate_warnings(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Warn when a diff drops a table and creates an identically-shaped one.

    That pattern is what a bare ``Meta.table`` rename diffs to, and applying it
    destroys the table's data (``DROP ... CASCADE`` on PostgreSQL). The pair is
    still written — the drop may be intentional — but with a prominent warning
    suggesting ``RenameModel``.

    Args:
        old: Previous schema state.
        new: Target schema state.

    Returns:
        One warning message per same-shape drop+create table pair.
    """

    def shape(tstate: dict[str, Any]) -> tuple[Any, ...]:
        """Reduce a table state to its comparable column/key shape.

        Args:
            tstate: The table state to reduce.

        Returns:
            A tuple of the spec parts that identify the table's structure.
        """
        spec = _tspec(tstate)
        return (spec["columns"], spec["pk"], spec["fks"], spec.get("composite_pk"))

    dropped = [t for t in old["tables"] if t not in new["tables"]]
    created = [t for t in new["tables"] if t not in old["tables"]]
    warnings = []
    for old_table in dropped:
        old_shape = shape(old["tables"][old_table])
        for new_table in created:
            if shape(new["tables"][new_table]) == old_shape:
                warnings.append(
                    f"table {old_table!r} is dropped while {new_table!r} is created with an "
                    "identical definition; applying this migration DESTROYS the table's data. "
                    f"If this is a Meta.table rename, replace the DeleteModel/CreateModel pair "
                    f"with m.RenameModel({old_table!r}, {new_table!r})."
                )
    return warnings


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------
class MigrationManager:
    """Discover, write, apply, and revert migration files for one app."""

    def __init__(
        self,
        directory: str = "migrations",
        app: str = "models",
        models: list[type[Model]] | None = None,
    ) -> None:
        """Configure the migrations directory, app label, and model scope.

        Args:
            directory: Path to the migrations directory.
            app: Application/label name recorded against migrations.
            models: Models to consider, or None for every registered model.

        Returns:
            None
        """
        self.directory = Path(directory)
        self.app = app
        self.models = models

    # -- file handling ----------------------------------------------------
    def _migration_files(self) -> list[Path]:
        """List migration files on disk in numeric order.

        Returns:
            The migration file paths sorted by their leading number.
        """
        if not self.directory.exists():
            return []
        files = [p for p in self.directory.iterdir() if _FILENAME_RE.match(p.name)]
        # The full name breaks number ties (e.g. two 0002_* files from a branch
        # merge), so their relative order is at least deterministic.
        return sorted(files, key=lambda p: (_file_number(p.name), p.name))

    @staticmethod
    def _load_module(path: Path) -> ModuleType:
        """Import a migration file as a module.

        Args:
            path: Path to the migration file to import.

        Returns:
            The imported migration module.
        """
        spec = importlib.util.spec_from_file_location(path.stem, path)
        if spec is None or spec.loader is None:  # pragma: no cover - defensive
            raise ImportError(f"Cannot load migration module: {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _load_all(self, files: list[Path] | None = None) -> list[tuple[str, type[Migration]]]:
        """Import every migration file in order and return its ``Migration``.

        Sanity-checks the set on load and warns (stderr) about duplicate
        numeric prefixes, unknown declared dependencies, and dependencies that
        sort after their dependant. These are warnings, not errors: existing
        directories legitimately contain such sets (e.g. two files sharing a
        number after a branch merge, long since applied), and refusing to load
        would block every command — including generating the fixing migration.
        Ties in the numeric order are broken by file name, so the run order is
        deterministic either way.

        Args:
            files: Pre-listed migration files to import, so a caller that already
                scanned the directory does not scan it again. Defaults to
                ``_migration_files()``.

        Returns:
            Pairs of migration name and its ``Migration`` class.
        """
        files = self._migration_files() if files is None else files
        by_number: dict[int, str] = {}
        for path in files:
            number = _file_number(path.name)
            if number in by_number:
                print(
                    f"WARNING: duplicate migration number {number:04d}: "
                    f"{by_number[number]!r} and {path.name!r} (they run in name order; "
                    "consider renumbering)",
                    file=sys.stderr,
                )
            by_number[number] = path.name
        loaded = [(p.stem, self._load_module(p).Migration) for p in files]
        position = {name: i for i, (name, _) in enumerate(loaded)}
        for i, (name, migration) in enumerate(loaded):
            for dep in migration.dependencies:
                if dep not in position:
                    print(
                        f"WARNING: migration {name!r} depends on unknown migration {dep!r}",
                        file=sys.stderr,
                    )
                elif position[dep] >= i:
                    print(
                        f"WARNING: migration {name!r} depends on {dep!r}, which runs after it "
                        "(migrations apply in numeric order)",
                        file=sys.stderr,
                    )
        return loaded

    def _next_number(self, files: list[Path] | None = None) -> int:
        """Compute the next migration number.

        Args:
            files: Pre-listed migration files (avoids a redundant directory
                scan). Defaults to ``_migration_files()``.

        Returns:
            One greater than the highest existing number, or 1 if none.
        """
        files = self._migration_files() if files is None else files
        if not files:
            return 1
        return _file_number(files[-1].name) + 1

    def _replay(
        self,
        applied: set[str] | None = None,
        loaded: list[tuple[str, type[Migration]]] | None = None,
    ) -> dict[str, Any]:
        """Rebuild schema state by replaying migrations' ``apply_state``.

        Args:
            applied: When given, replay only migrations whose name is in this
                set; otherwise replay every migration on disk.
            loaded: Pre-loaded ``(name, Migration)`` pairs to replay, so a caller
                that already imported the migrations (``upgrade``/``downgrade``)
                does not re-``exec`` every file. Defaults to ``_load_all()``.

        Returns:
            The replayed schema state.
        """
        state: dict[str, Any] = {"tables": {}}
        for name, migration in loaded if loaded is not None else self._load_all():
            if applied is None or name in applied:
                for op in migration.operations:
                    op.apply_state(state)
        return state

    # -- commands ---------------------------------------------------------
    def init(self) -> None:
        """Create the migrations directory and its ``__init__.py`` if missing.

        Returns:
            None
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        init_py = self.directory / "__init__.py"
        if not init_py.exists():
            init_py.write_text("")

    def make_migrations(
        self, name: str | None = None, empty: bool = False, allow_destructive: bool = False
    ) -> str | None:
        """Write a new migration from the model diff (or an empty one).

        Args:
            name: Optional label for the migration file.
            empty: When True, write a migration with no operations.
            allow_destructive: Permit a diff that drops **every** recorded
                table. Without it such a diff aborts, since it almost always
                means the model modules were not imported (an empty registry
                diffs to a drop of the whole schema).

        Raises:
            ConfigurationError: When the diff would drop every recorded table
                and ``allow_destructive`` is not set.

        Returns:
            The new migration filename, or None when there are no changes.
        """
        self.init()
        files = self._migration_files()  # single directory scan, reused below
        recorded = self._replay(loaded=self._load_all(files))
        target = model_state(self.models)
        operations = [] if empty else diff_states(recorded, target)
        if not operations and not empty:
            return None
        if not empty and not allow_destructive:
            kept = set(recorded["tables"]) & set(target["tables"])
            if recorded["tables"] and not kept:
                raise ConfigurationError(
                    f"refusing to write a migration that drops every recorded table "
                    f"({len(recorded['tables'])} tables). This usually means no models were "
                    "imported — pass --models <module> (or models=[...]). Use "
                    "--allow-destructive / allow_destructive=True to override."
                )
        warnings = [] if empty else _table_recreate_warnings(recorded, target)
        for warning in warnings:
            print(f"WARNING: {warning}", file=sys.stderr)
        number = self._next_number(files)
        label = name or ("initial" if not files else "auto")
        filename = f"{number:04d}_{label}.py"
        dependencies = [files[-1].stem] if files else []
        self._write_migration(filename, dependencies, operations, warnings)
        return filename

    def _write_migration(
        self,
        filename: str,
        dependencies: list[str],
        operations: list[Operation],
        warnings: list[str] | None = None,
    ) -> None:
        """Render and write a ``class Migration`` file to disk.

        Args:
            filename: Name of the migration file to create.
            dependencies: Names of migrations this one depends on.
            operations: Operations to serialise into the file.
            warnings: Autodetector warnings, written as prominent comments at
                the top of the file so a destructive diff is reviewed before it
                is applied.

        Returns:
            None
        """
        sources = [op.to_source() for op in operations]
        lines = []
        if any("db_defaults." in source for source in sources):
            lines.append("from yara_orm import db_defaults\n")
        lines += [
            "from yara_orm import fields\n",
            "from yara_orm import migrations as m\n",
        ]
        for warning in warnings or []:
            lines.append(f"\n# WARNING: {warning}")
        lines += [
            "\n\nclass Migration(m.Migration):\n",
            "    atomic = True\n",
            f"    dependencies = {dependencies!r}\n",
            "    operations = [\n" if operations else "    operations = []\n",
        ]
        for source in sources:
            lines.append(f"        {source},\n")
        if operations:
            lines.append("    ]\n")
        (self.directory / filename).write_text("".join(lines))

    @staticmethod
    @asynccontextmanager
    async def _maybe_txn(atomic: bool) -> AsyncIterator[None]:
        """Run the body in a transaction when ``atomic``; otherwise as-is.

        Args:
            atomic: Whether to wrap the body in a database transaction.

        Yields:
            None
        """
        if atomic:
            async with in_transaction():
                yield
        else:
            yield

    async def _ensure_table(self) -> None:
        """Create the migration-tracking table if it does not yet exist.

        Returns:
            None
        """
        dialect = get_dialect()
        spec = {
            "columns": {
                "id": {
                    "kind": "int",
                    "type_params": {},
                    "null": False,
                    "unique": False,
                    "pk": True,
                    "auto_increment": True,
                },
                "app": {
                    "kind": "varchar",
                    "type_params": {"max_length": 100},
                    "null": False,
                    "unique": False,
                    "pk": False,
                    "auto_increment": False,
                },
                "name": {
                    "kind": "varchar",
                    "type_params": {"max_length": 255},
                    "null": False,
                    "unique": False,
                    "pk": False,
                    "auto_increment": False,
                },
                "applied_at": {
                    "kind": "datetime",
                    "type_params": {},
                    "null": False,
                    "unique": False,
                    "pk": False,
                    "auto_increment": False,
                },
            },
            "pk": "id",
            "fks": {},
            "indexes": [],
        }
        engine = get_executor()
        for sql in dialect.render_create_table(MIGRATION_TABLE, spec, safe=True):
            await engine.execute(sql)

    async def _applied(self) -> list[str]:
        """Fetch the names of migrations already applied for this app.

        Returns:
            The applied migration names in application order.
        """
        await self._ensure_table()
        dialect = get_dialect()
        engine = get_executor()
        rows = await engine.fetch_rows(
            f"SELECT {dialect.quote('name')} FROM {dialect.quote(MIGRATION_TABLE)} "
            f"WHERE {dialect.quote('app')} = {dialect.placeholder(1)} "
            f"ORDER BY {dialect.quote('id')}",
            [self.app],
        )
        return [r[0] for r in rows]

    async def upgrade(self, target: str | None = None) -> list[str]:
        """Apply pending migrations up to an optional target, recording each.

        Args:
            target: Migration to stop after — a full name (``0002_auto``) or a
                numeric prefix (``0002``/``2``) — or None to apply all.

        Raises:
            KeyError: When ``target`` matches no known migration (nothing is
                applied in that case).

        Returns:
            The names of the migrations applied.
        """
        applied = await self._applied()
        dialect = get_dialect()
        loaded = self._load_all()  # imported once; reused for state replay + apply
        if target is not None:
            target = _resolve_target(target, [n for n, _ in loaded])
        state = self._replay(set(applied), loaded)
        table = dialect.quote(MIGRATION_TABLE)
        cols = ", ".join(dialect.quote(c) for c in ("app", "name", "applied_at"))
        holes = ", ".join(dialect.placeholder(i) for i in (1, 2, 3))
        insert_sql = f"INSERT INTO {table} ({cols}) VALUES ({holes})"
        done = []
        for name, migration in loaded:
            if name in applied:
                continue
            async with self._maybe_txn(migration.atomic):
                engine = get_executor()
                for op in migration.operations:
                    if isinstance(op, RunPython):
                        await op.run_forward()
                    await _apply_op_sql(engine, op.forward_sql(dialect, state), migration.atomic)
                    op.apply_state(state)
                await engine.execute(insert_sql, [self.app, name, datetime.now(timezone.utc)])
            done.append(name)
            if target and name == target:
                break
        return done

    async def downgrade(self, steps: int = 1, target: str | None = None) -> list[str]:
        """Revert applied migrations by step count or down to a target.

        Args:
            steps: Number of most-recent migrations to revert.
            target: Migration to revert down to (kept applied), taking
                precedence — a full name or a numeric prefix, as in ``upgrade``.

        Raises:
            KeyError: When ``target`` matches no known migration (nothing is
                reverted in that case).

        Returns:
            The names of the migrations reverted.
        """
        applied = await self._applied()
        loaded = self._load_all()  # imported once; reused for state replay + revert
        migrations = dict(loaded)
        state = self._replay(set(applied), loaded)
        if target is not None:
            target = _resolve_target(target, [n for n, _ in loaded])
            to_revert = [n for n in applied if _num(n) > _num(target)]
        else:
            to_revert = applied[-steps:] if steps > 0 else []
        reverted = []
        dialect = get_dialect()
        for name in reversed(to_revert):
            migration = migrations[name]
            async with self._maybe_txn(migration.atomic):
                engine = get_executor()
                for op in reversed(migration.operations):
                    if isinstance(op, RunPython):
                        await op.run_backward()
                    await _apply_op_sql(engine, op.backward_sql(dialect, state), migration.atomic)
                    op.revert_state(state)
                await engine.execute(
                    f"DELETE FROM {dialect.quote(MIGRATION_TABLE)} "
                    f"WHERE {dialect.quote('app')} = {dialect.placeholder(1)} "
                    f"AND {dialect.quote('name')} = {dialect.placeholder(2)}",
                    [self.app, name],
                )
            reverted.append(name)
        return reverted

    async def history(self) -> list[dict[str, Any]]:
        """List applied migrations with their application timestamps.

        Returns:
            Mappings with ``name`` and ``applied_at`` for each migration.
        """
        await self._ensure_table()
        dialect = get_dialect()
        engine = get_executor()
        rows = await engine.fetch_rows(
            f"SELECT {dialect.quote('name')}, {dialect.quote('applied_at')} "
            f"FROM {dialect.quote(MIGRATION_TABLE)} "
            f"WHERE {dialect.quote('app')} = {dialect.placeholder(1)} "
            f"ORDER BY {dialect.quote('id')}",
            [self.app],
        )
        return [{"name": r[0], "applied_at": r[1]} for r in rows]

    async def heads(self) -> list[dict[str, Any]]:
        """List every on-disk migration and whether it has been applied.

        Returns:
            Mappings with ``name`` and ``applied`` for each migration.
        """
        applied = set(await self._applied())
        return [{"name": name, "applied": name in applied} for name, _ in self._load_all()]

    def sqlmigrate(self, name: str, backward: bool = False) -> list[str]:
        """Render the SQL for one migration without executing it.

        Args:
            name: Name of the migration to render.
            backward: When True, render the reverse SQL instead.

        Returns:
            The SQL statements for the migration.
        """
        state: dict[str, Any] = {"tables": {}}
        target: type[Migration] | None = None
        for migration_name, migration in self._load_all():
            if migration_name == name:
                target = migration
                break
            for op in migration.operations:
                op.apply_state(state)
        if target is None:
            raise KeyError(f"Unknown migration: {name!r}")
        dialect = get_dialect()
        out: list[str] = []
        ops = target.operations
        if backward:
            for op in ops:
                op.apply_state(state)
            for op in reversed(ops):
                out.extend(op.backward_sql(dialect, state))
                op.revert_state(state)
        else:
            for op in ops:
                out.extend(op.forward_sql(dialect, state))
                op.apply_state(state)
        return out


async def _apply_op_sql(engine: BaseDBAsyncClient, sqls: list[str], atomic: bool) -> None:
    """Execute one operation's statements, honouring out-of-transaction pragmas.

    SQLite's table rebuild brackets its statements with ``PRAGMA foreign_keys``
    toggles (see :meth:`SqliteDialect.render_rebuild_table`), and that pragma is
    a silent no-op inside a transaction. A non-atomic migration therefore wraps
    such an operation in its own transaction (pinning one pooled connection, as
    the pragma is per-connection); the pinned runner then closes and reopens the
    transaction around each pragma so it actually takes effect.

    Args:
        engine: The executor to run statements on (pinned transaction wrapper
            for atomic migrations, pooled proxy otherwise).
        sqls: The operation's SQL statements.
        atomic: Whether the caller already runs inside a pinned transaction.

    Returns:
        None
    """
    if atomic or not any(sql in (PRAGMA_FK_OFF, PRAGMA_FK_ON) for sql in sqls):
        await _run_op_sql(engine, sqls, in_txn=atomic)
        return
    async with in_transaction():
        await _run_op_sql(get_executor(), sqls, in_txn=True)


async def _run_op_sql(engine: BaseDBAsyncClient, sqls: list[str], in_txn: bool) -> None:
    """Run statements, hoisting ``PRAGMA foreign_keys`` out of the transaction.

    Inside a pinned transaction the pragma is executed between a ``COMMIT`` and
    a fresh ``BEGIN`` on the same connection — the standard SQLite rebuild
    recipe (enforcement off outside the transaction, rebuild inside). On a
    failure after enforcement was switched off, the transaction is rolled back
    and enforcement restored before re-raising, so the pooled connection is
    never returned with foreign keys disabled.

    Args:
        engine: The executor to run statements on.
        sqls: The SQL statements to execute in order.
        in_txn: Whether ``engine`` is a pinned, open transaction.

    Returns:
        None
    """
    fk_off = False
    try:
        for sql in sqls:
            if sql in (PRAGMA_FK_OFF, PRAGMA_FK_ON) and in_txn:
                await engine.execute("COMMIT")
                await engine.execute(sql)
                await engine.execute("BEGIN")
            else:
                await engine.execute(sql)
            if sql in (PRAGMA_FK_OFF, PRAGMA_FK_ON):
                fk_off = sql == PRAGMA_FK_OFF
    except BaseException:
        if fk_off:
            if in_txn:
                await engine.execute("ROLLBACK")
                await engine.execute(PRAGMA_FK_ON)
                await engine.execute("BEGIN")
            else:
                await engine.execute(PRAGMA_FK_ON)
        raise


def _resolve_target(target: str, names: list[str]) -> str:
    """Resolve an upgrade/downgrade target to a known migration name.

    Accepts a full migration name (``0002_auto``) or a bare numeric prefix
    (``0002`` / ``2``), the same forms in both directions.

    Args:
        target: The requested target.
        names: The known migration names, in order.

    Raises:
        KeyError: When the target matches no known migration.

    Returns:
        The matching migration name.
    """
    if target in names:
        return target
    if target.isdigit():
        matches = [n for n in names if _num(n) == int(target)]
        if len(matches) == 1:
            return matches[0]
    available = ", ".join(names) if names else "(none)"
    raise KeyError(f"unknown migration target {target!r}; available: {available}")


def _num(name: str) -> int:
    """Extract the leading numeric prefix from a migration name.

    Args:
        name: Migration name like ``0001_initial``.

    Returns:
        The integer migration number.
    """
    return int(name.split("_", 1)[0])


def _file_number(filename: str) -> int:
    """Return the leading migration number of a ``NNNN_name.py`` file name.

    Args:
        filename: A migration file name matching ``_FILENAME_RE``.

    Returns:
        The integer migration number.
    """
    match = _FILENAME_RE.match(filename)
    if match is None:  # pragma: no cover - inputs are pre-filtered
        raise ValueError(f"Not a migration file name: {filename!r}")
    return int(match.group(1))

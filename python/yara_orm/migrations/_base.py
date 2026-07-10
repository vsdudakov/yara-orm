"""Migration helpers: source rendering, schema-state specs, Constraint classes."""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING, Any, cast

from .. import registry
from ..db_defaults import DatabaseDefault, Now, RandomHex, SqlDefault
from ..exceptions import ConfigurationError
from ..fields import ForeignKeyFieldInstance, OnDelete, registered_field_kind

if TYPE_CHECKING:
    from ..fields import Field
    from ..models import MetaInfo

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


def _field_flag_args(field: Field) -> list[str]:
    """Render the schema-relevant flag arguments shared by every field kind.

    Args:
        field: The field whose flags to render.

    Returns:
        The ``pk``/``null``/``unique``/``index``/``default`` keyword-argument
        source fragments that differ from their defaults (possibly empty).
    """
    args: list[str] = []
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
    return args


def _field_source(field: Field) -> str:
    """Render a field as a ``fields.XxxField(...)`` constructor call.

    Only schema-relevant arguments are emitted, so the rebuilt field produces the
    same column spec (Python-side defaults, validators and enum types are
    irrelevant to DDL and intentionally dropped; **database-side** defaults are
    part of the DDL and are emitted). Foreign keys whose target is resolvable are
    wrapped in :func:`resolved_fk` so the migration file is registry-independent.
    A registered custom kind renders via its ``source`` callable, or as
    ``fields.<ClassName>(<type_params as kwargs>, ...)`` by default.

    Args:
        field: The field to render as source.

    Raises:
        ConfigurationError: For a field kind that is neither built in nor
            registered via :func:`~yara_orm.fields.register_field_kind`.

    Returns:
        The constructor-call source reconstructing an equivalent field.
    """
    if isinstance(field, ForeignKeyFieldInstance):
        cls = "OneToOneField" if field.is_o2o else "ForeignKeyField"
        args = [repr(field.reference)]
        if field.on_delete != OnDelete.CASCADE:
            args.append(f"on_delete={field.on_delete!r}")
        if field.pk:
            # The field's own primary-key flag (e.g. an O2O used as the pk).
            # It lives inside the constructor call: the resolved_fk wrapper
            # below has its own ``pk=`` kwarg naming the TARGET model's pk
            # column, which is a different thing.
            args.append("pk=True")
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

    registration = registered_field_kind(field.field_kind)
    if registration is not None:
        if registration.source is not None:
            return registration.source(field)
        args = [f"{name}={value!r}" for name, value in field.type_params.items()]
        args.extend(_field_flag_args(field))
        return f"fields.{registration.field_cls.__name__}({', '.join(args)})"

    try:
        cls_name = _KIND_FIELD[field.field_kind]
    except KeyError:
        raise ConfigurationError(
            f"cannot render field kind {field.field_kind!r} into a migration; "
            "register custom kinds with yara_orm.register_field_kind(...)"
        ) from None
    args = []
    if field.field_kind == "varchar":
        args.append(f"max_length={field.type_params['max_length']}")
    elif field.field_kind == "decimal":
        args.append(f"max_digits={field.type_params['max_digits']}")
        args.append(f"decimal_places={field.type_params['decimal_places']}")
    args.extend(_field_flag_args(field))
    return f"fields.{cls_name}({', '.join(args)})"


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


__all__ = [
    "MIGRATION_TABLE",
    "_FILENAME_RE",
    "_WRAP",
    "_KIND_FIELD",
    "_fmt",
    "_call",
    "_default_source",
    "_default_spec",
    "resolved_fk",
    "_fk_target",
    "_field_flag_args",
    "_field_source",
    "_fields_source",
    "_column_spec",
    "_fk_spec",
    "_derived_indexes",
    "_index_spec",
    "_index_option_source",
    "_meta_index_specs",
    "_meta_named_constraint_specs",
    "_meta_unique_together_specs",
    "_MAX_IDENTIFIER",
    "_unique_together_name",
    "_constraint_from_spec",
    "_tspec",
    "_new_tstate",
    "_rename_in_table",
    "Constraint",
    "UniqueConstraint",
    "CheckConstraint",
]

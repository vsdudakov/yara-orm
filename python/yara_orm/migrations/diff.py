"""Schema-state extraction from models and the state-to-operations diff."""

from __future__ import annotations

import sys
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, cast

from .. import registry
from ..fields import ForeignKeyFieldInstance, OnDelete, registered_field_kind

if TYPE_CHECKING:
    from ..fields import Field
    from ..models import Model

from ._base import (
    _constraint_from_spec,
    _meta_index_specs,
    _meta_named_constraint_specs,
    _meta_unique_together_specs,
    _new_tstate,
    _tspec,
)
from .operations import (
    AddCompositeIndexIfNotExists,
    AddConstraint,
    AddField,
    AddFieldIfNotExists,
    AddIndexIfNotExists,
    AlterField,
    CreateExtension,
    CreateModel,
    CreateModelIfNotExists,
    DeleteModelIfExists,
    Operation,
    RemoveCompositeIndexIfExists,
    RemoveConstraint,
    RemoveFieldIfExists,
    RemoveIndexIfExists,
    RenameField,
)


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

    extensions = _required_extensions(ops)
    if extensions:
        return [cast(Operation, CreateExtension(n)) for n in sorted(extensions)] + ops
    return ops


def _required_extensions(ops: list[Operation]) -> set[str]:
    """Collect the extensions the diffed columns' registered kinds require.

    Only the operations that introduce or retype a column are inspected
    (``CreateModel``/``AddField``/``AlterField``): an extension is needed
    exactly when such an operation is present, which also keeps re-runs
    idempotent — no column change, no ``CreateExtension``.

    Args:
        ops: The diffed operations.

    Returns:
        The required extension names (possibly empty).
    """

    def required(field: Field) -> str | None:
        """Return the extension a field's registered kind requires, if any.

        Args:
            field: The field to inspect.

        Returns:
            The extension name, or ``None``.
        """
        registration = registered_field_kind(field.field_kind)
        return registration.requires_extension if registration else None

    extensions: set[str] = set()
    for op in ops:
        candidates: list[Field] = []
        if isinstance(op, CreateModel):
            candidates = list(op.fields.values())
        elif isinstance(op, (AddField, AlterField)):
            candidates = [op.field]
        for field in candidates:
            extension = required(field)
            if extension:
                extensions.add(extension)
    return extensions


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


__all__ = [
    "model_state",
    "_table_deps",
    "_topo_order",
    "_alterable",
    "diff_states",
    "_required_extensions",
    "_detect_renames",
    "_diff_composite_indexes",
    "_diff_constraints",
    "_table_recreate_warnings",
]

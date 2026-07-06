"""Migration operations: the Operation hierarchy rendered to DDL per dialect."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast

from ..exceptions import UnSupportedError

if TYPE_CHECKING:
    from ..dialects import BaseDialect
    from ..fields import Field

from ._base import (
    Constraint,
    _call,
    _column_spec,
    _field_source,
    _fields_source,
    _fmt,
    _index_option_source,
    _index_spec,
    _new_tstate,
    _rename_in_table,
    _tspec,
)


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
        return dialect.render_drop_composite_index(self.name, table=self.table)

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


class CreateExtension(Operation):
    """Create a PostgreSQL extension; a no-op on dialects without extensions.

    Emitted first in a generated migration whenever a diffed column's
    registered field kind declares ``requires_extension`` (see
    :func:`~yara_orm.fields.register_field_kind`). Unlike :class:`RunSQL` the
    statement renders per dialect, so the same migration applies cleanly on
    SQLite (where it emits nothing). The reverse is deliberately empty: other
    tables may rely on the extension, so it is never dropped automatically.
    """

    def __init__(self, name: str) -> None:
        """Store the extension name.

        Args:
            name: The extension to create (e.g. ``"vector"``/``"pg_trgm"``).

        Returns:
            None
        """
        self.name = name

    def forward_sql(self, dialect: BaseDialect, state: dict[str, Any]) -> list[str]:
        """Render ``CREATE EXTENSION IF NOT EXISTS`` on supporting dialects.

        Args:
            dialect: Active dialect used to render SQL.
            state: Current schema state (unused).

        Returns:
            The create-extension statement, or nothing on dialects without
            extension support (SQLite).
        """
        if not dialect.supports_extensions:
            return []
        return [f"CREATE EXTENSION IF NOT EXISTS {dialect.quote(self.name)}"]

    def backward_sql(self, dialect: BaseDialect, state: dict[str, Any]) -> list[str]:
        """Return no SQL: the extension is kept (other tables may use it).

        Args:
            dialect: Active dialect (unused).
            state: Current schema state (unused).

        Returns:
            An empty list.
        """
        return []

    def to_source(self) -> str:
        """Render this operation as Python source for a migration file.

        Returns:
            The source code constructing this operation.
        """
        return f"m.CreateExtension({self.name!r})"


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


__all__ = [
    "Migration",
    "Operation",
    "_ReversibleOp",
    "CreateModel",
    "CreateModelIfNotExists",
    "DeleteModel",
    "DeleteModelIfExists",
    "_FieldColumnOp",
    "AddField",
    "AddFieldIfNotExists",
    "RemoveField",
    "RemoveFieldIfExists",
    "AlterField",
    "_SingleColumnIndexOp",
    "AddIndex",
    "AddIndexIfNotExists",
    "AddIndexConcurrently",
    "AddUniqueIndexConcurrently",
    "RemoveIndex",
    "RemoveIndexIfExists",
    "RemoveIndexConcurrently",
    "_CompositeIndexOp",
    "AddCompositeIndex",
    "AddCompositeIndexIfNotExists",
    "RemoveCompositeIndex",
    "RemoveCompositeIndexIfExists",
    "RenameModel",
    "RenameField",
    "RenameIndex",
    "_ConstraintOp",
    "AddConstraint",
    "RemoveConstraint",
    "RenameConstraint",
    "RunSQL",
    "CreateExtension",
    "RunPython",
]

"""An operation-based migration system.

Migrations are operation-based and backend-portable: each migration file lists
``operations`` (and ``dependencies``); operations render to SQL per active
dialect at apply time, so the same migration runs on PostgreSQL or SQLite.

Workflow (see :class:`MigrationManager` and the ``python -m yara_orm`` CLI):

    makemigrations -> writes NNNN_name.py from the model diff
    upgrade        -> applies pending migrations, records them
    downgrade      -> reverts applied migrations
    history/heads  -> inspect applied vs on-disk
    sqlmigrate     -> print a migration's SQL without running it
"""

from __future__ import annotations

import importlib.util
import re
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import registry
from .connection import get_dialect, get_executor, in_transaction
from .fields import ForeignKeyField

if TYPE_CHECKING:
    from types import ModuleType

    from .dialects import BaseDialect
    from .fields import Field
    from .models import Model

MIGRATION_TABLE = "orm_migrations"
_FILENAME_RE = re.compile(r"^(\d+)_.*\.py$")

# Reprs longer than this are expanded one item per line in generated migrations.
# Kept above the widest single column spec so leaf specs stay on one line while
# the surrounding ``columns``/``fks`` mappings break out per column.
_WRAP = 200


def _fmt(value: Any, indent: int = 0) -> str:
    """Render a value as Python source, expanding long dicts/lists per line.

    Short values (and any scalar) render as their plain ``repr``; long dicts and
    lists break onto one item per line, indented under ``indent`` spaces, so the
    column maps in a generated migration read top-to-bottom instead of as one
    unreadable line.

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
    goes on its own line so the generated migration reads top-to-bottom. The
    indentation assumes the call sits at four spaces inside an ``operations``
    list, matching :meth:`Migrator._write_migration`.

    Args:
        name: Dotted name of the operation constructor (e.g. ``m.CreateTable``).
        args: Pre-rendered positional/keyword argument source fragments.

    Returns:
        The constructor call source (no trailing indentation).
    """
    oneline = f"{name}({', '.join(args)})"
    if len(oneline) <= _WRAP and "\n" not in oneline:
        return oneline
    body = ",\n".join(f"        {arg}" for arg in args)
    return f"{name}(\n{body},\n    )"


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------
class Operation:
    """Base class: render SQL both ways and evolve the in-memory schema state."""

    def forward_sql(self, dialect: BaseDialect) -> list[str]:
        """Render the SQL statements that apply this operation.

        Args:
            dialect: Active dialect used to render SQL.

        Returns:
            The SQL statements to run when applying the operation.
        """
        raise NotImplementedError

    def backward_sql(self, dialect: BaseDialect) -> list[str]:
        """Render the SQL statements that revert this operation.

        Args:
            dialect: Active dialect used to render SQL.

        Returns:
            The SQL statements to run when reverting the operation.
        """
        raise NotImplementedError

    def apply_state(self, state: dict[str, Any]) -> None:
        """Evolve the in-memory schema state to reflect this operation.

        Args:
            state: Mutable schema state to update in place.

        Returns:
            None
        """
        pass

    def to_source(self) -> str:
        """Render this operation as Python source for a migration file.

        Returns:
            The source code constructing this operation.
        """
        raise NotImplementedError


class CreateTable(Operation):
    """Create a table with its columns, primary key, foreign keys and indexes."""

    def __init__(
        self,
        table: str,
        columns: dict[str, Any],
        pk: str | None = None,
        fks: dict[str, Any] | None = None,
        indexes: list[str] | None = None,
        composite_pk: list[str] | None = None,
    ) -> None:
        """Store the specification of the table to create.

        Args:
            table: Name of the table to create.
            columns: Mapping of column name to column spec.
            pk: Name of the primary-key column, if any.
            fks: Mapping of column name to foreign-key spec.
            indexes: Column names to index.
            composite_pk: Column names forming a composite primary key.

        Returns:
            None
        """
        self.table = table
        self.columns = columns
        self.pk = pk
        self.fks = fks or {}
        self.indexes = indexes or []
        self.composite_pk = composite_pk

    def _spec(self) -> dict[str, Any]:
        """Assemble the table spec dict from the stored attributes.

        Returns:
            The table specification mapping.
        """
        spec = {"columns": self.columns, "pk": self.pk, "fks": self.fks, "indexes": self.indexes}
        if self.composite_pk:
            spec["composite_pk"] = self.composite_pk
        return spec

    def forward_sql(self, dialect: BaseDialect) -> list[str]:
        """Render the ``CREATE TABLE`` statements.

        Args:
            dialect: Active dialect used to render SQL.

        Returns:
            The SQL statements that create the table.
        """
        return dialect.render_create_table(self.table, self._spec(), safe=False)

    def backward_sql(self, dialect: BaseDialect) -> list[str]:
        """Render the ``DROP TABLE`` statements.

        Args:
            dialect: Active dialect used to render SQL.

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
        state["tables"][self.table] = self._spec()

    def to_source(self) -> str:
        """Render this operation as Python source for a migration file.

        Returns:
            The source code constructing this operation.
        """
        args = [
            repr(self.table),
            f"columns={_fmt(self.columns, 8)}",
            f"pk={self.pk!r}",
            f"fks={_fmt(self.fks, 8)}",
            f"indexes={_fmt(self.indexes, 8)}",
        ]
        if self.composite_pk:
            args.append(f"composite_pk={self.composite_pk!r}")
        return _call("m.CreateTable", args)


class DropTable(Operation):
    """Drop a table, keeping its spec so the operation can be reversed."""

    def __init__(self, table: str, spec: dict[str, Any]) -> None:
        """Store the name and spec of the table to drop.

        Args:
            table: Name of the table to drop.
            spec: Full table spec used to recreate it on reverse.

        Returns:
            None
        """
        self.table = table
        self.spec = spec

    def forward_sql(self, dialect: BaseDialect) -> list[str]:
        """Render the ``DROP TABLE`` statements.

        Args:
            dialect: Active dialect used to render SQL.

        Returns:
            The SQL statements that drop the table.
        """
        return dialect.render_drop_table(self.table)

    def backward_sql(self, dialect: BaseDialect) -> list[str]:
        """Render the ``CREATE TABLE`` statements that restore the table.

        Args:
            dialect: Active dialect used to render SQL.

        Returns:
            The SQL statements that recreate the table.
        """
        return dialect.render_create_table(self.table, self.spec, safe=False)

    def apply_state(self, state: dict[str, Any]) -> None:
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
        return _call("m.DropTable", [repr(self.table), f"spec={_fmt(self.spec, 8)}"])


class AddColumn(Operation):
    """Add a column to a table, optionally with a foreign-key spec."""

    def __init__(
        self, table: str, name: str, spec: dict[str, Any], fk: dict[str, Any] | None = None
    ) -> None:
        """Store the specification of the column to add.

        Args:
            table: Name of the table to alter.
            name: Name of the column to add.
            spec: Column spec describing the new column.
            fk: Optional foreign-key spec for the column.

        Returns:
            None
        """
        self.table = table
        self.name = name
        self.spec = spec
        self.fk = fk

    def forward_sql(self, dialect: BaseDialect) -> list[str]:
        """Render the ``ADD COLUMN`` statements.

        Args:
            dialect: Active dialect used to render SQL.

        Returns:
            The SQL statements that add the column.
        """
        return dialect.render_add_column(self.table, self.name, self.spec)

    def backward_sql(self, dialect: BaseDialect) -> list[str]:
        """Render the ``DROP COLUMN`` statements.

        Args:
            dialect: Active dialect used to render SQL.

        Returns:
            The SQL statements that drop the column.
        """
        return dialect.render_drop_column(self.table, self.name)

    def apply_state(self, state: dict[str, Any]) -> None:
        """Record the new column (and any FK) in the schema state.

        Args:
            state: Mutable schema state to update in place.

        Returns:
            None
        """
        table = state["tables"][self.table]
        table["columns"][self.name] = self.spec
        if self.fk:
            table.setdefault("fks", {})[self.name] = self.fk

    def to_source(self) -> str:
        """Render this operation as Python source for a migration file.

        Returns:
            The source code constructing this operation.
        """
        return _call(
            "m.AddColumn",
            [
                repr(self.table),
                repr(self.name),
                f"spec={_fmt(self.spec, 8)}",
                f"fk={_fmt(self.fk, 8)}",
            ],
        )


class DropColumn(Operation):
    """Drop a column, keeping its spec so the operation can be reversed."""

    def __init__(
        self, table: str, name: str, spec: dict[str, Any], fk: dict[str, Any] | None = None
    ) -> None:
        """Store the specification of the column to drop.

        Args:
            table: Name of the table to alter.
            name: Name of the column to drop.
            spec: Column spec used to recreate it on reverse.
            fk: Optional foreign-key spec for the column.

        Returns:
            None
        """
        self.table = table
        self.name = name
        self.spec = spec
        self.fk = fk

    def forward_sql(self, dialect: BaseDialect) -> list[str]:
        """Render the ``DROP COLUMN`` statements.

        Args:
            dialect: Active dialect used to render SQL.

        Returns:
            The SQL statements that drop the column.
        """
        return dialect.render_drop_column(self.table, self.name)

    def backward_sql(self, dialect: BaseDialect) -> list[str]:
        """Render the ``ADD COLUMN`` statements that restore the column.

        Args:
            dialect: Active dialect used to render SQL.

        Returns:
            The SQL statements that add the column back.
        """
        return dialect.render_add_column(self.table, self.name, self.spec)

    def apply_state(self, state: dict[str, Any]) -> None:
        """Remove the column (and any FK) from the schema state.

        Args:
            state: Mutable schema state to update in place.

        Returns:
            None
        """
        table = state["tables"][self.table]
        table["columns"].pop(self.name, None)
        table.get("fks", {}).pop(self.name, None)

    def to_source(self) -> str:
        """Render this operation as Python source for a migration file.

        Returns:
            The source code constructing this operation.
        """
        return _call(
            "m.DropColumn",
            [
                repr(self.table),
                repr(self.name),
                f"spec={_fmt(self.spec, 8)}",
                f"fk={_fmt(self.fk, 8)}",
            ],
        )


class CreateIndex(Operation):
    """Create an index on a single column of a table."""

    def __init__(self, table: str, column: str) -> None:
        """Store the table and column to index.

        Args:
            table: Name of the table to index.
            column: Name of the column to index.

        Returns:
            None
        """
        self.table = table
        self.column = column

    def forward_sql(self, dialect: BaseDialect) -> list[str]:
        """Render the ``CREATE INDEX`` statements.

        Args:
            dialect: Active dialect used to render SQL.

        Returns:
            The SQL statements that create the index.
        """
        return dialect.render_create_index(self.table, self.column, safe=False)

    def backward_sql(self, dialect: BaseDialect) -> list[str]:
        """Render the ``DROP INDEX`` statements.

        Args:
            dialect: Active dialect used to render SQL.

        Returns:
            The SQL statements that drop the index.
        """
        return dialect.render_drop_index(self.table, self.column)

    def apply_state(self, state: dict[str, Any]) -> None:
        """Record the index in the schema state.

        Args:
            state: Mutable schema state to update in place.

        Returns:
            None
        """
        idx = state["tables"][self.table].setdefault("indexes", [])
        if self.column not in idx:
            idx.append(self.column)

    def to_source(self) -> str:
        """Render this operation as Python source for a migration file.

        Returns:
            The source code constructing this operation.
        """
        return _call("m.CreateIndex", [repr(self.table), repr(self.column)])


class DropIndex(Operation):
    """Drop an index from a single column of a table."""

    def __init__(self, table: str, column: str) -> None:
        """Store the table and column whose index to drop.

        Args:
            table: Name of the table.
            column: Name of the indexed column.

        Returns:
            None
        """
        self.table = table
        self.column = column

    def forward_sql(self, dialect: BaseDialect) -> list[str]:
        """Render the ``DROP INDEX`` statements.

        Args:
            dialect: Active dialect used to render SQL.

        Returns:
            The SQL statements that drop the index.
        """
        return dialect.render_drop_index(self.table, self.column)

    def backward_sql(self, dialect: BaseDialect) -> list[str]:
        """Render the ``CREATE INDEX`` statements that restore the index.

        Args:
            dialect: Active dialect used to render SQL.

        Returns:
            The SQL statements that recreate the index.
        """
        return dialect.render_create_index(self.table, self.column, safe=False)

    def apply_state(self, state: dict[str, Any]) -> None:
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
        return _call("m.DropIndex", [repr(self.table), repr(self.column)])


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

    def forward_sql(self, dialect: BaseDialect) -> list[str]:
        """Return the forward SQL statements verbatim.

        Args:
            dialect: Active dialect (unused; SQL is supplied literally).

        Returns:
            The forward SQL statements.
        """
        return self.sql

    def backward_sql(self, dialect: BaseDialect) -> list[str]:
        """Return the reverse SQL statements verbatim.

        Args:
            dialect: Active dialect (unused; SQL is supplied literally).

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

    def forward_sql(self, dialect: BaseDialect) -> list[str]:
        """Return no SQL; this operation runs Python instead.

        Args:
            dialect: Active dialect (unused).

        Returns:
            An empty list.
        """
        return []

    def backward_sql(self, dialect: BaseDialect) -> list[str]:
        """Return no SQL; this operation runs Python instead.

        Args:
            dialect: Active dialect (unused).

        Returns:
            An empty list.
        """
        return []


# ---------------------------------------------------------------------------
# Schema state from models + diffing
# ---------------------------------------------------------------------------
def _column_spec(field: Field) -> dict[str, Any]:
    """Build a column spec dict from a model field.

    Args:
        field: Field whose spec to capture.

    Returns:
        The column specification mapping.
    """
    return {
        "kind": field.field_kind,
        "type_params": dict(field.type_params),
        "null": field.null,
        "unique": field.unique,
        "pk": field.pk,
        "auto_increment": field.auto_increment,
    }


def model_state(models: list[type[Model]] | None = None) -> dict[str, Any]:
    """Build the current schema state (tables + M2M join tables) from models.

    Pass ``models`` to scope to a subset (defaults to every registered model).

    Args:
        models: Models to inspect, or None for every registered model.

    Returns:
        The schema state mapping with a ``tables`` entry.
    """
    tables: dict = {}
    for model in models if models is not None else registry.all_models():
        meta = model._meta
        columns, fks, indexes = {}, {}, []
        for field in meta.field_list:
            if isinstance(field, ForeignKeyField):
                ref = registry.get_model(field.reference)
                pkf = ref._meta.pk_field
                columns[field.db_column] = {
                    "kind": pkf.field_kind,
                    "type_params": dict(pkf.type_params),
                    "null": field.null,
                    "unique": field.unique,
                    "pk": field.pk,
                    "auto_increment": False,
                }
                fks[field.db_column] = {
                    "table": ref._meta.table,
                    "pk": pkf.db_column,
                    "on_delete": field.on_delete,
                }
            else:
                columns[field.db_column] = _column_spec(field)
            if field.index and not field.unique and not field.pk:
                indexes.append(field.db_column)
        tables[meta.table] = {
            "columns": columns,
            "pk": meta.pk_field.db_column,
            "fks": fks,
            "indexes": indexes,
        }

        for info in meta.m2m.values():
            info.finalize()
            if info.through in tables:  # pragma: no cover - defensive de-dup
                continue
            target = info.resolve_target()
            owner_pk, target_pk = meta.pk_field, target._meta.pk_field
            tables[info.through] = {
                "columns": {
                    info.backward_key: _ref_col(owner_pk),
                    info.forward_key: _ref_col(target_pk),
                },
                "pk": None,
                "composite_pk": [info.backward_key, info.forward_key],
                "fks": {
                    info.backward_key: {
                        "table": meta.table,
                        "pk": owner_pk.db_column,
                        "on_delete": "CASCADE",
                    },
                    info.forward_key: {
                        "table": target._meta.table,
                        "pk": target_pk.db_column,
                        "on_delete": "CASCADE",
                    },
                },
                "indexes": [],
            }
    return {"tables": tables}


def _ref_col(pk_field: Field) -> dict[str, Any]:
    """Build a column spec for an M2M join column referencing a primary key.

    Args:
        pk_field: Primary-key field that the join column references.

    Returns:
        The column specification mapping.
    """
    return {
        "kind": pk_field.field_kind,
        "type_params": dict(pk_field.type_params),
        "null": False,
        "unique": False,
        "pk": False,
        "auto_increment": False,
    }


def _table_deps(tspec: dict[str, Any]) -> set[str]:
    """Return the set of tables a table depends on via foreign keys.

    Args:
        tspec: Table spec whose foreign-key targets to collect.

    Returns:
        The set of referenced table names.
    """
    return {ref["table"] for ref in tspec.get("fks", {}).values()}


def _topo_order(names: Iterable[str], tables: dict[str, Any]) -> list[str]:
    """Order tables so referenced tables come before referencing ones.

    Args:
        names: Table names to order.
        tables: Mapping of table name to table spec.

    Returns:
        The table names in dependency order.
    """
    names = list(names)
    ordered, seen = [], set()

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
        for dep in _table_deps(tables[name]):
            if dep != name:
                visit(dep)
        ordered.append(name)

    for n in names:
        visit(n)
    return ordered


def diff_states(old: dict[str, Any], new: dict[str, Any]) -> list[Operation]:
    """Compute the operations that transform one schema state into another.

    Args:
        old: Previous schema state.
        new: Target schema state.

    Returns:
        The ordered operations to migrate from ``old`` to ``new``.
    """
    ops: list[Operation] = []
    old_tables, new_tables = old["tables"], new["tables"]

    for table in _topo_order([t for t in new_tables if t not in old_tables], new_tables):
        spec = new_tables[table]
        ops.append(
            CreateTable(
                table,
                columns=spec["columns"],
                pk=spec.get("pk"),
                fks=spec.get("fks"),
                indexes=spec.get("indexes"),
                composite_pk=spec.get("composite_pk"),
            )
        )

    for table in new_tables:
        if table not in old_tables:
            continue
        old_cols = old_tables[table]["columns"]
        new_cols = new_tables[table]["columns"]
        new_fks = new_tables[table].get("fks", {})
        old_fks = old_tables[table].get("fks", {})
        old_idx = set(old_tables[table].get("indexes", []))
        new_idx = set(new_tables[table].get("indexes", []))
        # Order matters: drop indexes before the columns they reference, and
        # add columns before indexing them (SQLite rejects the reverse).
        for col in sorted(old_idx - new_idx):
            ops.append(DropIndex(table, col))
        for col in old_cols:
            if col not in new_cols:
                ops.append(DropColumn(table, col, old_cols[col], fk=old_fks.get(col)))
        for col in new_cols:
            if col not in old_cols:
                ops.append(AddColumn(table, col, new_cols[col], fk=new_fks.get(col)))
        for col in sorted(new_idx - old_idx):
            ops.append(CreateIndex(table, col))

    for table in reversed(_topo_order([t for t in old_tables if t not in new_tables], old_tables)):
        ops.append(DropTable(table, old_tables[table]))

    return ops


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
        return sorted(files, key=lambda p: _file_number(p.name))

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

    def _load_all(self) -> list[tuple[str, ModuleType]]:
        """Import every migration file in order.

        Returns:
            Pairs of migration name and imported module.
        """
        return [(p.stem, self._load_module(p)) for p in self._migration_files()]

    def _next_number(self) -> int:
        """Compute the next migration number.

        Returns:
            One greater than the highest existing number, or 1 if none.
        """
        files = self._migration_files()
        if not files:
            return 1
        return _file_number(files[-1].name) + 1

    def _recorded_state(self) -> dict[str, Any]:
        """Replay all on-disk migrations to rebuild the recorded schema state.

        Returns:
            The schema state implied by the existing migration files.
        """
        state = {"tables": {}}
        for _, module in self._load_all():
            for op in module.operations:
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

    def make_migrations(self, name: str | None = None, empty: bool = False) -> str | None:
        """Write a new migration from the model diff (or an empty one).

        Args:
            name: Optional label for the migration file.
            empty: When True, write a migration with no operations.

        Returns:
            The new migration filename, or None when there are no changes.
        """
        self.init()
        files = self._migration_files()
        operations = [] if empty else diff_states(self._recorded_state(), model_state(self.models))
        if not operations and not empty:
            return None
        number = self._next_number()
        label = name or ("initial" if not files else "auto")
        filename = f"{number:04d}_{label}.py"
        dependencies = [files[-1].stem] if files else []
        self._write_migration(filename, dependencies, operations)
        return filename

    def _write_migration(
        self, filename: str, dependencies: list[str], operations: list[Operation]
    ) -> None:
        """Render and write a migration file to disk.

        Args:
            filename: Name of the migration file to create.
            dependencies: Names of migrations this one depends on.
            operations: Operations to serialise into the file.

        Returns:
            None
        """
        lines = [
            "from yara_orm import migrations as m\n",
            f"\ndependencies = {dependencies!r}\n",
            "\noperations = [",
        ]
        for op in operations:
            lines.append(f"\n    {op.to_source()},")
        lines.append("\n]\n" if operations else "]\n")
        (self.directory / filename).write_text("".join(lines))

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
            target: Migration name to stop after, or None to apply all.

        Returns:
            The names of the migrations applied.
        """
        applied = await self._applied()
        dialect = get_dialect()
        done = []
        for name, module in self._load_all():
            if name in applied:
                continue
            async with in_transaction():
                engine = get_executor()
                for op in module.operations:
                    if isinstance(op, RunPython):
                        await op.run_forward()
                    for sql in op.forward_sql(dialect):
                        await engine.execute(sql)
                table = dialect.quote(MIGRATION_TABLE)
                cols = ", ".join(dialect.quote(c) for c in ("app", "name", "applied_at"))
                holes = ", ".join(dialect.placeholder(i) for i in (1, 2, 3))
                await engine.execute(
                    f"INSERT INTO {table} ({cols}) VALUES ({holes})",
                    [self.app, name, datetime.now(timezone.utc)],
                )
            done.append(name)
            if target and name == target:
                break
        return done

    async def downgrade(self, steps: int = 1, target: str | None = None) -> list[str]:
        """Revert applied migrations by step count or down to a target.

        Args:
            steps: Number of most-recent migrations to revert.
            target: Migration name to revert down to, taking precedence.

        Returns:
            The names of the migrations reverted.
        """
        applied = await self._applied()
        modules = dict(self._load_all())
        if target is not None:
            to_revert = [n for n in applied if _num(n) > _num(target)]
        else:
            to_revert = applied[-steps:] if steps > 0 else []
        reverted = []
        dialect = get_dialect()
        for name in reversed(to_revert):
            module = modules[name]
            async with in_transaction():
                engine = get_executor()
                for op in reversed(module.operations):
                    if isinstance(op, RunPython):
                        await op.run_backward()
                    for sql in op.backward_sql(dialect):
                        await engine.execute(sql)
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
        module = dict(self._load_all())[name]
        dialect = get_dialect()
        out: list[str] = []
        ops = reversed(module.operations) if backward else module.operations
        for op in ops:
            out.extend(op.backward_sql(dialect) if backward else op.forward_sql(dialect))
        return out


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

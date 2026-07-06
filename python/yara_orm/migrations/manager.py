"""MigrationManager: discover, write, apply and revert migration files."""

from __future__ import annotations

import importlib.util
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..connection import get_dialect, get_executor, in_transaction
from ..dialects import PRAGMA_FK_OFF, PRAGMA_FK_ON
from ..exceptions import ConfigurationError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from types import ModuleType

    from ..connection import BaseDBAsyncClient
    from ..models import Model

from ._base import _FILENAME_RE, MIGRATION_TABLE
from .diff import _table_recreate_warnings, diff_states, model_state
from .operations import Migration, Operation, RunPython


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
            # Only consider migrations up to and including the target, so a target
            # that is already applied is a no-op rather than sweeping in the
            # migrations that follow it (the `break` below never fires when the
            # target itself is skipped by the already-applied guard).
            target_idx = next(i for i, (n, _) in enumerate(loaded) if n == target)
            loaded = loaded[: target_idx + 1]
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
                # The tracking table's ``applied_at`` is a plain (tz-naive)
                # datetime column, and the ORM stores naive UTC there; binding an
                # aware value would shift or be rejected on backends that map
                # ``datetime`` to TIMESTAMP WITHOUT TIME ZONE. Strip the tzinfo so
                # the stored value matches the column (and ``history()`` reads).
                applied_at = datetime.now(timezone.utc).replace(tzinfo=None)
                await engine.execute(insert_sql, [self.app, name, applied_at])
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


__all__ = [
    "MigrationManager",
    "_resolve_target",
    "_num",
    "_file_number",
    "_apply_op_sql",
    "_run_op_sql",
]

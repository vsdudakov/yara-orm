---
title: Migrations
description: Database migrations for an async Python ORM — makemigrations, upgrade and downgrade with operation-based, backend-portable schemas rendered to PostgreSQL, MySQL or SQLite.
---

# Migrations

`yara_orm` ships a Django/Tortoise-style migration system for evolving your
database **schema** alongside your models. Migrations are **operation-based**
and **auto-generated**: `makemigrations` diffs your models against the recorded
state and writes a numbered migration file; `upgrade` and `downgrade` apply or
revert those files in this **async Python ORM**.

Crucially, migrations are **backend-portable**. A migration records *operations*
(create table, add column, …), not raw DDL. The same operations render to the
correct SQL for the active dialect **at apply time**, so one migration set runs
unchanged on **PostgreSQL**, **MySQL** or **SQLite**.

Each migration file declares a `class Migration(m.Migration)` with `operations`
(and optional `dependencies` / `atomic`). Operations are built from **live field
objects** — `CreateModel` lists `fields={col: Field}`, `AddField` / `AlterField`
carry a single `Field` — so a migration reads like your models.

!!! info "How state is tracked"
    The target schema is **replayed from the migration files** on disk
    (Django-style) — each file's `Migration.operations` are applied in order to
    rebuild the recorded schema state, which is then diffed against your current
    models. Applied migrations are recorded per app in an `orm_migrations` table,
    which is created automatically on first use. Files are sanity-checked at
    load time: duplicate numeric prefixes and `dependencies` that would run
    after their dependant print a warning (existing directories keep loading —
    number ties run in deterministic file-name order).

!!! warning "SQLite table rebuilds and atomicity"
    On SQLite, an `AlterField` (or a constraint change) rebuilds the table using
    SQLite's official recipe, which must toggle `PRAGMA foreign_keys` outside a
    transaction. An atomic migration containing a rebuild therefore **commits
    the operations preceding the rebuild** before running it — keep schema
    rebuilds in their own migration file when you need strict all-or-nothing
    behaviour around other operations.

!!! tip "Idempotent by default"
    `makemigrations` emits the **idempotent** analog of each operation
    (`CreateModelIfNotExists`, `AddFieldIfNotExists`, `RemoveFieldIfExists`,
    `AddIndexIfNotExists`, …). A column whose definition changed is emitted as
    `AlterField` automatically (PostgreSQL alters in place, MySQL restates the
    definition with `MODIFY COLUMN`; SQLite rebuilds the table, preserving rows,
    referencing rows, indexes and constraints).

    Atomic migrations (the default) are all-or-nothing: a failure rolls the
    whole file back, so re-running is always safe. For `atomic = False`
    migrations, re-running a half-applied file is safe only for the guarded
    `*IfExists` / `*IfNotExists` operations — `RenameField`, constraint changes
    and concurrent index builds are not guarded (SQLite and MySQL cannot guard
    add/drop column at all, and MySQL has no `CREATE INDEX IF NOT EXISTS`
    either).

!!! tip "What `makemigrations` auto-detects"
    Beyond create/drop table and add/drop/alter column, the diff also detects:

    - **Renames** — a field renamed with an unchanged type becomes a single
      `RenameField` (preserving the data) instead of a destructive drop + add.
      Detection is deliberately conservative: only an **unambiguous** pairing
      (one removed and one added column of identical definition) is treated as
      a rename; ambiguous sets fall back to drop + add with a printed hint to
      hand-write `RenameField` if a rename was intended.
    - **Column definition changes** — type, nullability, single-column
      `unique`, a database-side `default`, a foreign key's `on_delete`, and a
      referenced primary key's type change all emit `AlterField`.
    - **Composite indexes** — adding/removing a `Meta.indexes` entry (including
      partial `Index(..., condition=...)` indexes) emits
      `AddCompositeIndex` / `RemoveCompositeIndex`.
    - **Named constraints** — adding/removing a named `UniqueConstraint` /
      `CheckConstraint` in `Meta.constraints` emits `AddConstraint` /
      `RemoveConstraint`.
    - **Many-to-many fields** — adding/removing an M2M field creates/drops its
      join table.

    Generated foreign keys are stamped with their resolved target
    (`m.resolved_fk(...)` records the target table, primary-key column and
    type), so an old migration replays correctly even after the target model
    is deleted or its primary key changes.

## The CLI

Run the tool as a module:

```bash
python -m yara_orm <command> [options]
```

### Global options

These flags belong to the **top-level** parser, so they come **before** the
subcommand:

| Flag | Default | Purpose |
| --- | --- | --- |
| `--dir` | `migrations` | Migrations directory. |
| `--app` | `models` | App/label recorded against migrations in `orm_migrations`. |
| `--db` | _(none)_ | Database URL; opens a connection for commands that touch the DB. |
| `--models` | _(empty)_ | Comma-separated model modules to import (and resolve relations) before running. |
| `--allow-destructive` | off | Let `makemigrations` write a diff that would drop **every** recorded table (normally refused as a sign `--models` was forgotten). |

!!! warning "Order matters"
    `--dir`, `--app`, `--db`, and `--models` must appear **before** the
    subcommand. Subcommand flags (like `--name` or `--empty`) appear **after**
    it. For example:

    ```bash
    python -m yara_orm --models myapp.models makemigrations --name initial
    ```

### `init`

Create the migrations directory (and its `__init__.py`) if missing.

```bash
python -m yara_orm --dir migrations init
```

### `makemigrations`

Generate a migration from the model diff. Takes `--name` (a label for the file)
and `--empty` (write a migration with no operations, for hand-written data
migrations). With no detected changes, it prints `no changes detected`.

```bash
python -m yara_orm --models myapp.models makemigrations --name add_age
python -m yara_orm --models myapp.models makemigrations --empty --name backfill
```

### `upgrade [version]`

Apply pending migrations, recording each in `orm_migrations`. An optional
positional `version` stops after that migration; omit it to apply everything.
The target may be the full name (`0002_add_age`) or just the numeric prefix
(`0002`); an unknown target errors before anything is applied, listing the
available migrations.

```bash
python -m yara_orm --db postgres://localhost/app --models myapp.models upgrade
python -m yara_orm --db postgres://localhost/app --models myapp.models upgrade 0002_add_age
```

### `downgrade [version] [--steps N]`

Revert applied migrations. By default reverts `--steps 1` (the most recent
migration). An optional positional `version` reverts everything **after** that
migration and takes precedence over `--steps`. Like `upgrade`, the target may be
a full name or a numeric prefix, and an unknown target errors up front.

```bash
python -m yara_orm --db postgres://localhost/app --models myapp.models downgrade --steps 2
python -m yara_orm --db postgres://localhost/app --models myapp.models downgrade 0001_initial
```

### `history`

List applied migrations for the app, with their timestamps.

```bash
python -m yara_orm --db postgres://localhost/app --app myapp history
```

### `heads`

List every on-disk migration and whether it has been applied (`[x]` / `[ ]`).

```bash
python -m yara_orm --db postgres://localhost/app --models myapp.models heads
```

### `sqlmigrate <version> [--backward]`

Print a migration's SQL **without running it**. Pass `--backward` to render the
reverse SQL. The `version` argument is required here.

```bash
python -m yara_orm --models myapp.models sqlmigrate 0001_initial
python -m yara_orm --models myapp.models sqlmigrate 0001_initial --backward
```

## Backend-portable SQL preview

Because operations render per dialect, `sqlmigrate` shows the **same migration**
as different DDL depending on the `--db` URL. Choose a backend by pointing
`--db` at PostgreSQL, MySQL or SQLite:

=== "PostgreSQL"

    ```bash
    python -m yara_orm --db postgres://localhost/app --models myapp.models \
        sqlmigrate 0001_initial
    ```

=== "MySQL"

    ```bash
    python -m yara_orm --db mysql://root@localhost/app --models myapp.models \
        sqlmigrate 0001_initial
    ```

=== "SQLite"

    ```bash
    python -m yara_orm --db sqlite:///app.db --models myapp.models \
        sqlmigrate 0001_initial
    ```

The migration file is identical in both cases — only the rendered SQL differs.

## Programmatic API

The same workflow is available through `MigrationManager`, exported from
`yara_orm`:

```python
import asyncio

from yara_orm import MigrationManager, YaraOrm
from myapp.models import User, Post


async def main() -> None:
    await YaraOrm.init("sqlite:///app.db")

    manager = MigrationManager(
        directory="migrations",
        app="myapp",
        models=[User, Post],   # defaults to every registered model
    )

    manager.init()                                    # ensure the directory exists
    filename = manager.make_migrations(name="initial")  # -> "0001_initial.py" or None
    applied = await manager.upgrade()                 # -> ["0001_initial"]

    for row in await manager.history():               # [{"name", "applied_at"}, ...]
        print(row["name"], row["applied_at"])
    for row in await manager.heads():                 # [{"name", "applied"}, ...]
        print(row["name"], row["applied"])

    sql = manager.sqlmigrate("0001_initial")          # list[str], no execution
    await manager.downgrade(steps=1)                  # -> reverted names

    await YaraOrm.close()


asyncio.run(main())
```

| Method | Signature | Notes |
| --- | --- | --- |
| `init()` | `init() -> None` | Create the directory and `__init__.py`. |
| `make_migrations()` | `make_migrations(name=None, empty=False) -> str \| None` | Returns the new filename, or `None` when there are no changes. |
| `upgrade()` | `await upgrade(target=None) -> list[str]` | Applies pending migrations up to an optional target. |
| `downgrade()` | `await downgrade(steps=1, target=None) -> list[str]` | `target` reverts down to that migration and wins over `steps`. |
| `history()` | `await history() -> list[dict]` | Applied migrations with `applied_at`. |
| `heads()` | `await heads() -> list[dict]` | All on-disk migrations with an `applied` flag. |
| `sqlmigrate()` | `sqlmigrate(name, backward=False) -> list[str]` | Render SQL without executing it. |

!!! note "Async surface"
    `upgrade`, `downgrade`, `history`, and `heads` touch the database and are
    coroutines — `await` them. `init`, `make_migrations`, and `sqlmigrate` are
    synchronous (the last renders SQL but does not run it).

## Operations reference

A migration file is plain Python: a `class Migration(m.Migration)` with an
`operations` list (and optional `dependencies` / `atomic`), built from
`yara_orm.migrations` (imported in generated files as
`from yara_orm import migrations as m`, alongside `from yara_orm import fields`).

| Operation | Purpose |
| --- | --- |
| `CreateModel(table, fields, composite_pk=None, composite_indexes=None, constraints=None)` | Create a table from a `{column: Field}` set (columns, pk, foreign keys, indexes, named constraints). |
| `DeleteModel(table, fields, composite_pk=None)` | Drop a table (keeps its fields so it can be reversed). |
| `AddField(table, name, field)` | Add a column from a field object. |
| `RemoveField(table, name, field)` | Drop a column (keeps the field so it can be reversed). |
| `AlterField(table, name, field, old)` | Change a column's definition (PostgreSQL in place, MySQL in place via `MODIFY COLUMN`; SQLite rebuilds the table, keeping rows, FK-referencing rows, indexes and constraints). |
| `AddIndex(table, column)` | Create an index on a single column. |
| `RemoveIndex(table, column)` | Drop a single-column index. |
| `AddCompositeIndex(table, name, columns, condition=None)` | Create a multi-column (optionally partial) index. |
| `RemoveCompositeIndex(table, name, columns, condition=None)` | Drop a multi-column index. |
| `RunSQL(sql, reverse_sql=None)` | Run literal SQL forward and, optionally, its reverse. |
| `RunPython(forward, backward=None)` | Run async Python callables (hand-written migrations only). |
| `CreateExtension(name)` | `CREATE EXTENSION IF NOT EXISTS <name>` on PostgreSQL; a no-op on MySQL and SQLite (unlike `RunSQL`, it renders per dialect). Reverse is empty — the extension is never dropped. |

`makemigrations` generates the **idempotent** analogs of the schema operations —
`CreateModelIfNotExists`, `DeleteModelIfExists`, `AddFieldIfNotExists`,
`RemoveFieldIfExists`, `AddIndexIfNotExists`, `RemoveIndexIfExists`,
`AddCompositeIndexIfNotExists`, `RemoveCompositeIndexIfExists` — plus
`AlterField`, `RenameField` and constraint add/remove. For online index builds on PostgreSQL, hand-written migrations can
use `AddIndexConcurrently`, `AddUniqueIndexConcurrently` or
`RemoveIndexConcurrently` with `atomic = False` (those builds cannot run inside a
transaction). `RunSQL` and `RunPython` are for hand-written `--empty` migrations.

Custom field kinds registered with
[`register_field_kind()`](custom-fields.md) participate like built-ins: the
field is serialised as `fields.<ClassName>(...)` (or the registration's custom
`source`), a `type_params` change diffs to `AlterField`, and when the kind
declares `requires_extension`, `makemigrations` prepends the matching
`m.CreateExtension(...)` operations to any migration that creates or retypes
such a column (idempotent — a re-run with no column changes emits nothing).

A generated initial migration for a `User` and a related `Post` looks like:

```python
from yara_orm import fields
from yara_orm import migrations as m


class Migration(m.Migration):
    atomic = True
    dependencies = []
    operations = [
        m.CreateModelIfNotExists(
            "user",
            fields={
                "id": fields.IntField(pk=True),
                "name": fields.CharField(max_length=100),
            },
        ),
        m.CreateModelIfNotExists(
            "post",
            fields={
                "id": fields.IntField(pk=True),
                "title": fields.CharField(max_length=200, index=True),
                "author_id": fields.ForeignKeyField("User"),
            },
        ),
    ]
```

### Renames and constraints

`makemigrations` **auto-detects a column rename** (a removed column and an added
one with an identical definition) and emits a single `RenameField`, preserving
the data. Detection requires the pairing to be unambiguous — with several
same-definition candidates, or when the type *also* changed, it falls back to
drop + add (with a hint printed when a hand-written `RenameField` may be what
you want). Renaming `Meta.table` is likewise not auto-detected: the diff would
be a destructive drop + create pair, so the generated file carries a prominent
`# WARNING:` comment suggesting `RenameModel(old, new)` instead. Table and
index renames are hand-written:

| Operation | Purpose |
| --- | --- |
| `RenameModel(old, new)` | Rename a table. |
| `RenameField(table, old, new)` | Rename a column (auto-detected, or hand-written). |
| `RenameIndex(table, column, old_name, new_name, unique=False)` | Rename an index (PostgreSQL and MySQL in place; SQLite drops/recreates). |

Named constraints in `Meta.constraints` are diffed automatically (added/removed
constraints emit `AddConstraint` / `RemoveConstraint`). You can also manage them
by hand — build a constraint with `UniqueConstraint(fields=[...], name=...)` or
`CheckConstraint(check="...", name=...)` and use
`AddConstraint` / `RemoveConstraint` / `RenameConstraint`:

```python
from yara_orm import migrations as m


class Migration(m.Migration):
    dependencies = ["0001_initial"]
    operations = [
        m.RenameField("user", "name", "full_name"),
        m.AddConstraint(
            "user",
            m.UniqueConstraint(fields=["full_name"], name="uq_user_full_name"),
        ),
    ]
```

!!! note "Constraint changes across backends"
    `AddConstraint` / `RemoveConstraint` / `RenameConstraint` use
    `ALTER TABLE … CONSTRAINT` on **PostgreSQL** (in place). **MySQL** also adds
    and drops constraints in place, but has no `RENAME CONSTRAINT` —
    `RenameConstraint` raises `UnSupportedError` there (drop and re-add
    instead). **SQLite** has no
    such syntax, so on SQLite these rebuild the table with the new constraint
    set — data, indexes and other constraints preserved. (A table the migration
    system has no recorded state for — e.g. one created via `RunSQL` — still
    raises `UnSupportedError` on SQLite.) Give every constraint a `name` so the
    operation can be reversed on `downgrade`.

### Data migration with `--empty`

Start from a blank migration:

```bash
python -m yara_orm --models myapp.models makemigrations --empty --name backfill
```

Then fill in `operations`. With raw SQL via `RunSQL` (give `reverse_sql` to make
it reversible):

```python
from yara_orm import migrations as m


class Migration(m.Migration):
    dependencies = ["0001_initial"]
    operations = [
        m.RunSQL(
            "UPDATE users SET active = TRUE WHERE active IS NULL",
            reverse_sql="UPDATE users SET active = NULL WHERE active = TRUE",
        ),
    ]
```

Or with async Python via `RunPython` — handy for ORM-driven data changes
(`backward` is optional):

```python
from yara_orm import migrations as m
from myapp.models import User


async def seed() -> None:
    await User.objects.create(name="admin")


async def unseed() -> None:
    await User.objects.filter(name="admin").delete()


class Migration(m.Migration):
    dependencies = ["0001_initial"]
    operations = [
        m.RunPython(seed, unseed),
    ]
```

!!! tip "Reversibility"
    `RunPython` runs `backward` on `downgrade` and `RunSQL` runs `reverse_sql`.
    If you omit them, the operation simply does nothing when reverted — leave a
    note so reviewers know the step is one-way.

## Typical workflow

1. **Edit your models** — add a field, a model, or an index.
2. **Generate** the migration:
   ```bash
   python -m yara_orm --models myapp.models makemigrations --name add_age
   ```
3. **Review** the generated `NNNN_add_age.py` file and its `operations`.
   Optionally preview the SQL with `sqlmigrate`.
4. **Apply** it:
   ```bash
   python -m yara_orm --db postgres://localhost/app --models myapp.models upgrade
   ```
5. **Revert** if needed:
   ```bash
   python -m yara_orm --db postgres://localhost/app --models myapp.models downgrade --steps 1
   ```

## See also

- [Models & fields](models-and-fields.md)
- [Backends](../backends/index.md)

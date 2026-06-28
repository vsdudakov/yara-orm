---
title: Migrations
description: Database migrations for an async Python ORM — makemigrations, upgrade and downgrade with operation-based, backend-portable schemas rendered to PostgreSQL or SQLite.
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
unchanged on **PostgreSQL** or **SQLite**.

!!! info "How state is tracked"
    The target schema is **replayed from the migration files** on disk
    (Django-style) — each file's `operations` are applied in order to rebuild
    the recorded schema state, which is then diffed against your current models.
    Applied migrations are recorded per app in an `orm_migrations` table, which
    is created automatically on first use.

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

```bash
python -m yara_orm --db postgres://localhost/app --models myapp.models upgrade
python -m yara_orm --db postgres://localhost/app --models myapp.models upgrade 0002_add_age
```

### `downgrade [version] [--steps N]`

Revert applied migrations. By default reverts `--steps 1` (the most recent
migration). An optional positional `version` reverts everything **after** that
migration and takes precedence over `--steps`.

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
`--db` at PostgreSQL or SQLite:

=== "PostgreSQL"

    ```bash
    python -m yara_orm --db postgres://localhost/app --models myapp.models \
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

Migration files are plain Python: a `dependencies` list and an `operations`
list, built from `yara_orm.migrations` (imported in generated files as
`from yara_orm import migrations as m`).

| Operation | Purpose |
| --- | --- |
| `CreateTable` | Create a table with its columns, primary key, foreign keys, and indexes. |
| `DropTable` | Drop a table (keeps its spec so it can be reversed). |
| `AddColumn` | Add a column, optionally with a foreign-key spec. |
| `DropColumn` | Drop a column (keeps its spec so it can be reversed). |
| `CreateIndex` | Create an index on a column. |
| `DropIndex` | Drop an index on a column. |
| `RunSQL(sql, reverse_sql=None)` | Run literal SQL forward and, optionally, its reverse. |
| `RunPython(forward, backward=None)` | Run async Python callables (hand-written migrations only). |

`CreateTable`, `DropTable`, `AddColumn`, `DropColumn`, `CreateIndex`, and
`DropIndex` are generated automatically by `makemigrations`. `RunSQL` and
`RunPython` are for hand-written `--empty` migrations.

### Data migration with `--empty`

Start from a blank migration:

```bash
python -m yara_orm --models myapp.models makemigrations --empty --name backfill
```

Then fill in `operations`. With raw SQL via `RunSQL` (give `reverse_sql` to make
it reversible):

```python
from yara_orm import migrations as m

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

# Yara ORM

**A fast, async Python ORM with a Rust engine — [Tortoise](https://tortoise.github.io/)-style
models, querysets, relations and migrations for PostgreSQL, MySQL, MariaDB, SQLite,
Oracle and Microsoft SQL Server.**

[![CI](https://github.com/vsdudakov/yara-orm/actions/workflows/ci.yml/badge.svg)](https://github.com/vsdudakov/yara-orm/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/yara-orm.svg)](https://pypi.org/project/yara-orm/)
[![Python](https://img.shields.io/badge/python-3.9%E2%80%933.14-blue.svg)](https://github.com/vsdudakov/yara-orm)
[![Coverage](https://img.shields.io/badge/coverage-100%25-brightgreen.svg)](#development)
[![Docs](https://img.shields.io/badge/docs-vsdudakov.github.io%2Fyara--orm-blue.svg)](https://vsdudakov.github.io/yara-orm/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE.md)
[![Sponsor](https://img.shields.io/badge/sponsor-%E2%9D%A4-ec6cb9.svg?logo=github-sponsors)](https://github.com/sponsors/vsdudakov)

📖 **Documentation: [vsdudakov.github.io/yara-orm](https://vsdudakov.github.io/yara-orm/)**

✍️ **Deep dive: [How the GIL, PyO3 & asyncio cooperate](https://dev.to/vsdudakov/i-built-a-python-orm-with-a-rust-engine-heres-how-the-gil-pyo3-and-asyncio-actually-cooperate-4fkj)** — how the Rust engine bridges Python's event loop without the GIL collapsing it.

Yara ORM is a high-performance **async ORM for Python** that pairs the ergonomics
of a Django/Tortoise-style API — models, querysets, relations, aggregation and
migrations — with a hot path (connection pooling, parameter binding, row decoding)
written in compiled **Rust** (PyO3 + tokio). It is a drop-in-feel **alternative to
Tortoise ORM and async SQLAlchemy**: **2–9× faster** than popular pure-Python ORMs
on common operations, with first-class **PostgreSQL**, **MySQL**, **MariaDB** and
**SQLite** backends — plus **Oracle** and **Microsoft SQL Server** (both beta) —
full type hints, and **100% test coverage**.

```python
from yara_orm import Model, YaraOrm, fields

class User(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=120)

await YaraOrm.init("postgres://localhost/app")
await YaraOrm.generate_schemas()
await User.create(name="Ada")
print(await User.filter(name__icontains="ad").count())
```

---

## Highlights

- ⚡ **Rust engine** — pooling, binding and decoding in compiled code; the async
  bridge (PyO3 + tokio) keeps your event loop free.
- 🧩 **Familiar API** — Tortoise/Django-style models, lazy chainable querysets,
  `Q` objects, aggregation, `prefetch_related`, transactions, signals. Coming from
  Tortoise? Most code moves across unchanged — see
  [Migrating from Tortoise ORM](https://vsdudakov.github.io/yara-orm/guides/migrating-from-tortoise/).
- 🗄️ **Pluggable backends** — PostgreSQL, MySQL/MariaDB and SQLite, plus beta
  **Oracle** and **Microsoft SQL Server** backends, selected by
  URL; a new database is one Rust trait + one Python dialect.
- 🚚 **Migrations** — operation-based, auto-generated, backend-portable
  (`makemigrations` / `upgrade` / `downgrade`).
- 🧪 **Quality** — fully typed, linted (ruff + ty) and **100% test coverage**.

## Installation

```bash
pip install yara-orm
```

Prebuilt wheels are published for Linux, macOS and Windows on CPython 3.9–3.14,
so installation needs **no Rust toolchain**. (Installing the source
distribution on an unsupported platform compiles the engine and requires a Rust
toolchain — see [Development](#development).)

## Quick start

```python
import asyncio
from yara_orm import Model, YaraOrm, fields


class Tournament(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=100)
    created_at = fields.DatetimeField(auto_now_add=True)


class Event(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=100, index=True)
    tournament = fields.ForeignKeyField("Tournament", related_name="events")


async def main() -> None:
    await YaraOrm.init("postgres://localhost/app")   # or "mysql://…", "sqlite:///app.db"
    await YaraOrm.generate_schemas()

    cup = await Tournament.create(name="World Cup")
    await Event.create(name="Final", tournament=cup)

    # Lazy, chainable queries
    finals = await Event.filter(name__icontains="fin").order_by("-id")
    count = await Event.filter(tournament=cup).count()

    # Relations
    async for event in cup.events:
        print(event.name, "→", (await event.tournament).name)

    await YaraOrm.close()


asyncio.run(main())
```

## Querying

```python
# Lookups: exact, not, gt/gte/lt/lte, in, isnull, contains/icontains,
# startswith/endswith (+ case-insensitive `i` variants)
await User.filter(age__gte=18, name__icontains="a").order_by("-age").limit(10)

# Complex boolean filters with Q
from yara_orm import Q
await User.filter(Q(name="Ada") | Q(age__lt=30)).exclude(active=False)

# Aggregation + group by
from yara_orm import Count, Sum
await Author.annotate(books=Count("books")).filter(books__gte=1)
await Book.annotate(total=Sum("rating")).group_by("author_id").values("author_id", "total")

# Construction-free projections
await User.all().values("id", "name")
await User.all().values_list("name", flat=True)
```

Model methods: `create`, `bulk_create`, `get`, `get_or_none`, `filter`,
`exclude`, `all`, `annotate`, `prefetch_related`, `raw`; instances `save`,
`delete`, `fetch_related`.

## Relations

```python
class Author(Model):
    name = fields.CharField(max_length=100)

class Book(Model):
    title = fields.CharField(max_length=200)
    author = fields.ForeignKeyField("Author", related_name="books")
    tags = fields.ManyToManyField("Tag", related_name="books")

book = await Book.create(title="Compilers", author=author)
await book.tags.add(tag1, tag2)        # m2m add / remove / clear

await book.author                       # forward FK (awaitable)
async for b in author.books: ...        # reverse manager
await Author.all().prefetch_related("books")   # no N+1
```

`ForeignKeyField`, `OneToOneField`, `ManyToManyField`, recursive self-FK,
`related_name`, `Prefetch(rel, queryset=...)`.

## Transactions, signals & more

```python
from yara_orm import in_transaction, atomic, pre_save, connections

async with in_transaction():            # commit on success, rollback on error
    await Account.create(name="A")
    async with in_transaction():        # nesting opens a savepoint
        await Account.create(name="B")  # rolls back independently on error

@atomic(isolation="SERIALIZABLE")       # isolation levels (PostgreSQL/MySQL)
async def transfer(): ...

@pre_save(User)                          # lifecycle signals
async def on_save(sender, instance, using_db, update_fields): ...

await connections.get("default").execute("INSERT ...", [..])   # manual SQL
```

Also: **enum fields** (`IntEnumField`/`CharEnumField`), column/table
**comments** (`description=`, `Meta.table_description`), and **multi-database
routing** via a `Router` over multiple named connections.

## Backends

Backends are selected by the connection URL; the abstraction is a single Rust
trait (`Backend`) plus a Python `BaseDialect` subclass:

```python
await YaraOrm.init("postgres://user@localhost/db")     # PostgreSQL (tokio-postgres)
await YaraOrm.init("mysql://user:pass@localhost/db")   # MySQL/MariaDB (mysql_async)
await YaraOrm.init("sqlite:///path/to/app.db")          # SQLite (rusqlite)
await YaraOrm.init("oracle://user:pass@localhost:1521/FREEPDB1")  # Oracle 23ai — beta (oracle-rs)
await YaraOrm.init("mssql://user:pass@localhost:1433/db")  # SQL Server 2017+ — beta (tiberius)
```

The SQLite backend maps rich types (uuid/json/datetime/decimal) onto SQLite's
storage classes and reconstructs them on read from the declared column type, so
the model layer is identical across backends.

**Oracle** and **Microsoft SQL Server** (both beta) ride on the same
model/queryset API — both on pure-Rust drivers (`oracle-rs` TNS and `tiberius`
TDS, no OCI/ODBC/Instant Client, so the wheels stay self-contained). The shared
cross-backend suite runs against a live SQL Server 2022 and Oracle 23ai in CI.
See the [backends guide](https://vsdudakov.github.io/yara-orm/backends/) for their
type maps and the driver caveats that keep them out of the stable tier.

## Migrations

A Django/Tortoise-style, operation-based migration system. Migrations are
auto-generated from model changes and **backend-portable** — the same operations
render to PostgreSQL, MySQL, SQLite, Oracle or SQL Server DDL at apply time. Applied migrations are tracked
in an `orm_migrations` table.

```bash
# autodetect model changes -> migrations/0001_initial.py
python -m yara_orm --models myapp.models makemigrations --name initial

# preview SQL without running it (per the target dialect)
python -m yara_orm --db sqlite:///app.db --models myapp.models sqlmigrate 0001_initial

# apply / revert / inspect
python -m yara_orm --db postgres://localhost/app --models myapp.models upgrade
python -m yara_orm --db postgres://localhost/app --models myapp.models downgrade
python -m yara_orm --db postgres://localhost/app --models myapp.models history
```

Each migration is a `class Migration(m.Migration)` whose `operations` are built
from live field objects: `CreateModel`, `DeleteModel`, `AddField`, `RemoveField`,
`AlterField`, `AddIndex`, `RemoveIndex`, plus hand-written renames
(`RenameModel` / `RenameField` / `RenameIndex`), constraints (`AddConstraint` /
`RemoveConstraint` / `RenameConstraint` with `UniqueConstraint` / `CheckConstraint`)
and `RunSQL` / `RunPython` for data migrations. `makemigrations` emits the
idempotent analogs (`CreateModelIfNotExists`, `AddFieldIfNotExists`, …) and detects
`AlterField` automatically. The same commands are available programmatically via
`yara_orm.MigrationManager`.

## Performance

Median of 5 runs, Python 3.12, 5000 rows, against **eight other Python ORMs**
(Tortoise, SQLAlchemy, Pony, Django, Peewee, SQLObject, Ormar, Piccolo) — Yara
ORM is fastest or tied on every operation across PostgreSQL, MySQL, MariaDB and
SQLite, losing only to leaner in-process sync ORMs on single-row point reads. Times in
ms, lower is better. Full methodology, speedup tables and per-op notes in
[`benchmarks/`](benchmarks/) and the [performance docs](https://vsdudakov.github.io/yara-orm/performance/).

### PostgreSQL 18

![Yara ORM vs eight Python ORMs on PostgreSQL — latency per operation, log scale, lower is better](docs/assets/benchmark-postgres.png)

| operation     | yara-orm | tortoise | sqlalchemy | pony | django | peewee | sqlobject | ormar | piccolo |
|---------------|---------:|---------:|-----------:|-----:|-------:|-------:|----------:|------:|--------:|
| bulk_insert   | 14.7 | 24.2 | 78.0 | 222.8 | 40.6 | 51.7 | 526.3 | 229.8 | 99.2 |
| single_insert | 34.4 | 80.7 | 153.1 | 61.8 | 40.5 | 47.1 | 53.5 | 167.4 | 89.5 |
| fetch_all     | 3.6 | 17.0 | 29.4 | 34.5 | 9.1 | 11.9 | 26.6 | 56.7 | 4.3 |
| count         | 0.3 | 0.6 | 1.0 | 0.4 | 0.4 | 0.3 | 0.3 | 5.4 | 0.4 |
| group_by      | 0.7 | 1.0 | 1.6 | 2.4 | 1.0 | 0.8 | 0.6 | - | 1.0 |
| filter        | 2.3 | 9.1 | 8.1 | 17.9 | 5.3 | 6.7 | 9.1 | 42.2 | 2.6 |
| get_by_pk     | 65.1 | 196.3 | 292.6 | 85.3 | 115.7 | 114.1 | 23.8 | 333.1 | 196.1 |
| update        | 3.3 | 3.6 | 4.0 | 120.8 | 3.4 | 3.4 | 3.3 | 15.0 | 3.5 |
| delete        | 0.7 | 0.8 | 1.1 | 94.3 | 0.8 | 0.7 | 0.6 | 2.4 | 0.8 |

### MySQL 8.4

Same workload against MySQL (Tortoise over asyncmy, SQLAlchemy/Ormar over
aiomysql, the sync ORMs over pymysql; Piccolo has no MySQL backend):

![Yara ORM vs seven Python ORMs on MySQL — latency per operation, log scale, lower is better](docs/assets/benchmark-mysql.png)

| operation     | yara-orm | tortoise | sqlalchemy | pony | django | peewee | sqlobject | ormar |
|---------------|---------:|---------:|-----------:|-----:|-------:|-------:|----------:|------:|
| bulk_insert   | 49.8 | 50.9 | 600.9 | 443.8 | 89.2 | 88.7 | 1185.8 | 221.7 |
| single_insert | 605.4 | 816.9 | 1058.2 | 904.5 | 848.3 | 795.2 | 875.4 | 1183.9 |
| fetch_all     | 5.6 | 33.4 | 44.2 | 48.4 | 29.0 | 28.0 | 43.8 | 73.3 |
| count         | 0.5 | 0.9 | 1.2 | 0.8 | 1.0 | 1.0 | 0.8 | 4.6 |
| group_by      | 1.2 | 1.4 | 2.0 | 2.5 | 1.5 | 1.2 | 1.0 | - |
| filter        | 3.3 | 17.4 | 15.8 | 25.3 | 15.6 | 14.8 | 17.1 | 30.5 |
| get_by_pk     | 128.3 | 226.7 | 524.1 | 312.5 | 211.7 | 206.2 | 65.8 | 925.0 |
| update        | 7.0 | 7.4 | 8.2 | 236.3 | 7.2 | 10.1 | 6.9 | 8.8 |
| delete        | 5.2 | 4.8 | 5.4 | 210.0 | 6.4 | 5.0 | 5.1 | 7.2 |

(`single_insert` ~0.6–1.2 s is dominated by InnoDB's per-commit fsync — every ORM
pays it; `get_by_pk` and `single_insert` include the Docker-network round trip.)

### MariaDB 11

Every competitor connects through its MySQL driver; `yara-orm` auto-detects
MariaDB and uses its RETURNING dialect. Piccolo has no MySQL backend:

![Yara ORM vs seven Python ORMs on MariaDB — latency per operation, log scale, lower is better](docs/assets/benchmark-mariadb.png)

| operation     | yara-orm | tortoise | sqlalchemy | pony | django | peewee | sqlobject | ormar |
|---------------|---------:|---------:|-----------:|-----:|-------:|-------:|----------:|------:|
| bulk_insert   | 30.5 | 41.5 | 99.0 | 455.0 | 87.7 | 59.5 | 1257.2 | 191.2 |
| single_insert | 392.8 | 304.8 | 430.8 | 329.6 | 343.6 | 367.3 | 359.8 | 555.3 |
| fetch_all     | 5.5 | 34.0 | 42.5 | 47.9 | 28.4 | 30.4 | 44.9 | 72.8 |
| count         | 0.5 | 0.8 | 1.2 | 0.7 | 0.7 | 0.8 | 0.7 | 4.7 |
| group_by      | 1.3 | 1.3 | 2.1 | 2.2 | 1.5 | 1.1 | 0.9 | - |
| filter        | 3.2 | 17.8 | 16.0 | 24.5 | 15.5 | 14.7 | 16.9 | 31.9 |
| get_by_pk     | 123.3 | 228.7 | 534.1 | 310.7 | 214.5 | 206.0 | 66.0 | 916.1 |
| update        | 3.8 | 3.3 | 6.5 | 265.9 | 4.3 | 4.3 | 4.2 | 7.6 |
| delete        | 3.2 | 3.2 | 3.3 | 249.9 | 3.3 | 3.0 | 2.9 | 3.6 |

(MariaDB's `single_insert` ~390 ms is markedly faster than MySQL 8's — a lighter
default commit path.)

### SQLite

![Yara ORM vs eight Python ORMs on SQLite — latency per operation, log scale, lower is better](docs/assets/benchmark-sqlite.png)

| operation     | yara-orm | tortoise | sqlalchemy | pony | django | peewee | sqlobject | ormar | piccolo |
|---------------|---------:|---------:|-----------:|-----:|-------:|-------:|----------:|------:|--------:|
| bulk_insert   | 7.9 | 14.4 | 612.7 | 51.0 | 58.1 | 30.7 | 223.1 | 158.0 | 78.8 |
| single_insert | 32.6 | 29.3 | 240.0 | 128.3 | 139.0 | 114.7 | 139.9 | 323.2 | 259.1 |
| fetch_all     | 3.4 | 39.7 | 28.8 | 51.0 | 16.3 | 12.5 | 44.9 | 54.8 | 9.1 |
| count         | 0.1 | 0.3 | 0.7 | 0.2 | 0.2 | 0.1 | 0.1 | 1.7 | 0.5 |
| group_by      | 0.5 | 0.8 | 1.4 | 1.5 | 0.9 | 0.7 | 0.5 | - | 1.0 |
| filter        | 2.0 | 20.5 | 7.7 | 26.2 | 8.5 | 6.7 | 17.3 | 19.6 | 5.1 |
| get_by_pk     | 47.4 | 87.5 | 330.9 | 30.7 | 83.6 | 77.7 | 13.3 | 501.8 | 359.5 |
| update        | 0.6 | 0.5 | 1.8 | 43.1 | 1.3 | 1.2 | 1.2 | 1.6 | 1.4 |
| delete        | 0.4 | 0.4 | 1.2 | 36.3 | 0.9 | 0.7 | 0.8 | 1.3 | 1.2 |

Yara ORM wins everything throughput-shaped (fetch_all 2.7–16×, filter 2.5–13×,
bulk_insert 1.8–77×) and trails only the latency-bound point reads, where the
per-statement asyncio bridge costs tens of µs against in-process sync ORMs
(SQLObject and Pony on `get_by_pk`) — the opt-in `sqlite://...?sync_fast_path=1`
URL flag removes that bridge entirely (point queries ~7× faster).

Speed comes from the Rust hot path, **positional row decoding** (no per-row dict
or column-name allocation), **compiled-SQL + prepared-statement caching**, and
connection pooling (see
[Performance](https://vsdudakov.github.io/yara-orm/performance/)). Run it
yourself with `make bench` / `make bench-mysql` / `make bench-sqlite`.

## Architecture

```
┌─────────────────────────────────────────────┐
│ Python  (python/yara_orm) ................. │
│   Model / metaclass ....... schema + ORM API│
│   QuerySet ................ lazy SQL builder│
│   fields .................... abstract types│
│   dialects ................ per-DB SQL rules│
└───────────────┬─────────────────────────────┘
                │  sql + params  (PyO3 / asyncio bridge)
┌───────────────▼─────────────────────────────┐
│ Rust  (rust/src)  →  yara_orm._engine ..... │
│   Engine ...................... async facade│
│   Backend trait .............. pluggable DBs│
│     PgBackend ............... tokio-postgres│
│     MySqlBackend ................ mysql_async│
│     SqliteBackend ................. rusqlite│
│     OracleBackend ............... oracle-rs│
│     MsSqlBackend .................. tiberius│
│   Value .................. Py⇆Rust⇆SQL types│
└─────────────────────────────────────────────┘
```

- **Rust owns** pooling (deadpool), binding, type conversion and decoding.
- **Python owns** the model layer and SQL generation.
- **Adding a database** = a new `Backend` impl + scheme match in
  `rust/src/backend/mod.rs`, plus a `BaseDialect` subclass in
  `python/yara_orm/dialects.py`. The model layer never changes.

## Development

```bash
git clone https://github.com/vsdudakov/yara-orm
cd yara-orm
make dev        # create .venv313 and install dev tools (maturin, ruff, ty, pytest)
make build      # compile the Rust engine into the venv (maturin develop)
make lint       # ruff check + ruff format --check + ty
make test       # pytest against $DB (default postgres://localhost/orm_demo)
make cov        # tests with the 100% coverage gate
make bench      # cross-ORM benchmark (needs `make bench-setup` once; Python ≤ 3.12 for Pony)
make bench-mysql   # same comparison on MySQL
make bench-sqlite  # same comparison on SQLite
```

Requires a Rust toolchain (`rustup`), a local PostgreSQL for the Postgres tests
and a local MySQL for the MySQL tests; the SQLite tests are self-contained.

## Contributing

Issues and pull requests are welcome. Please run `make lint` and `make cov`
(both must be green — lint clean and 100% coverage) before opening a PR.

## Sponsor

Yara ORM is MIT-licensed and developed in the open. If it saves your project
time — or you'd like to support continued work on the Rust engine, backends and
docs — please consider [**sponsoring on GitHub**](https://github.com/sponsors/vsdudakov).
Every bit helps and is hugely appreciated. ❤️

## License

[MIT](LICENSE.md) © Yara ORM contributors

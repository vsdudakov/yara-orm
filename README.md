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
| bulk_insert   | 15.2 | 25.2 | 80.8 | 223.8 | 40.5 | 51.1 | 513.3 | 227.5 | 100.7 |
| single_insert | 34.4 | 84.1 | 158.7 | 60.8 | 42.9 | 46.6 | 53.3 | 169.1 | 92.3 |
| fetch_all     | 3.6 | 17.3 | 31.6 | 34.4 | 9.1 | 11.9 | 27.8 | 56.6 | 4.4 |
| count         | 0.3 | 0.5 | 1.0 | 0.4 | 0.5 | 0.3 | 0.3 | 6.0 | 0.4 |
| group_by      | 0.8 | 1.1 | 1.6 | 2.3 | 1.0 | 0.8 | 0.6 | - | 1.0 |
| filter        | 2.2 | 9.1 | 8.4 | 17.5 | 5.3 | 6.8 | 9.3 | 21.2 | 2.6 |
| get_by_pk     | 64.1 | 205.9 | 310.4 | 86.0 | 121.1 | 115.6 | 23.1 | 336.9 | 201.6 |
| update        | 3.5 | 3.9 | 4.1 | 121.1 | 3.5 | 3.4 | 3.3 | 12.8 | 3.6 |
| delete        | 0.7 | 0.9 | 1.1 | 95.5 | 0.8 | 0.7 | 0.6 | 2.2 | 0.8 |

### MySQL 8.4

Same workload against MySQL (Tortoise over asyncmy, SQLAlchemy/Ormar over
aiomysql, the sync ORMs over pymysql; Piccolo has no MySQL backend):

![Yara ORM vs seven Python ORMs on MySQL — latency per operation, log scale, lower is better](docs/assets/benchmark-mysql.png)

| operation     | yara-orm | tortoise | sqlalchemy | pony | django | peewee | sqlobject | ormar |
|---------------|---------:|---------:|-----------:|-----:|-------:|-------:|----------:|------:|
| bulk_insert   | 46.8 | 48.2 | 596.1 | 443.5 | 100.0 | 82.1 | 1076.6 | 212.3 |
| single_insert | 638.7 | 660.6 | 985.1 | 800.6 | 783.4 | 743.5 | 773.8 | 1062.2 |
| fetch_all     | 7.5 | 33.8 | 44.3 | 47.7 | 28.7 | 28.5 | 42.0 | 73.0 |
| count         | 0.5 | 0.8 | 1.2 | 0.9 | 0.9 | 0.8 | 0.7 | 4.2 |
| group_by      | 1.3 | 1.4 | 2.0 | 2.5 | 1.5 | 1.2 | 1.0 | - |
| filter        | 3.2 | 17.5 | 15.5 | 24.6 | 15.1 | 14.9 | 16.8 | 31.3 |
| get_by_pk     | 122.0 | 226.1 | 524.2 | 315.4 | 214.0 | 208.0 | 64.6 | 924.3 |
| update        | 6.8 | 7.4 | 9.9 | 232.4 | 7.7 | 9.5 | 7.4 | 11.7 |
| delete        | 4.9 | 4.8 | 5.6 | 207.0 | 5.8 | 4.8 | 6.6 | 6.1 |

(`single_insert` ~0.6–1.1 s is dominated by InnoDB's per-commit fsync — every ORM
pays it; `get_by_pk` and `single_insert` include the Docker-network round trip.)

### MariaDB 11

Every competitor connects through its MySQL driver; `yara-orm` auto-detects
MariaDB and uses its RETURNING dialect. Piccolo has no MySQL backend:

![Yara ORM vs seven Python ORMs on MariaDB — latency per operation, log scale, lower is better](docs/assets/benchmark-mariadb.png)

| operation     | yara-orm | tortoise | sqlalchemy | pony | django | peewee | sqlobject | ormar |
|---------------|---------:|---------:|-----------:|-----:|-------:|-------:|----------:|------:|
| bulk_insert   | 25.6 | 38.4 | 101.2 | 473.5 | 100.8 | 59.6 | 1247.3 | 208.4 |
| single_insert | 311.9 | 345.2 | 476.6 | 403.7 | 296.3 | 299.0 | 345.6 | 573.5 |
| fetch_all     | 5.8 | 36.1 | 43.2 | 48.1 | 32.1 | 31.0 | 42.7 | 71.6 |
| count         | 0.4 | 0.8 | 1.3 | 0.8 | 0.9 | 0.7 | 0.6 | 5.8 |
| group_by      | 1.3 | 1.3 | 2.1 | 2.2 | 1.8 | 1.3 | 0.9 | - |
| filter        | 3.3 | 17.7 | 16.2 | 24.8 | 15.4 | 14.6 | 17.3 | 50.0 |
| get_by_pk     | 120.8 | 224.8 | 541.4 | 310.9 | 217.8 | 210.7 | 64.8 | 914.8 |
| update        | 3.8 | 3.3 | 6.6 | 264.7 | 4.5 | 4.3 | 4.2 | 7.3 |
| delete        | 2.7 | 2.8 | 3.2 | 252.1 | 3.1 | 2.8 | 3.0 | 3.7 |

(`single_insert`, `update` and `delete` are near ties across every ORM here
because they're database-bound — single inserts are paced by MariaDB's
per-commit disk fsync, and `update`/`delete` are one server-side set statement
each — so there's no client-side marshaling for the Rust hot path to speed up.
MariaDB's `single_insert` ~310 ms is markedly faster than MySQL 8's — a lighter
default commit path.)

### SQLite

![Yara ORM vs eight Python ORMs on SQLite — latency per operation, log scale, lower is better](docs/assets/benchmark-sqlite.png)

SQLite is in-process, so these use its recommended `sync_fast_path=1` config
(statements run synchronously on the calling thread — no I/O to overlap):

| operation     | yara-orm | tortoise | sqlalchemy | pony | django | peewee | sqlobject | ormar | piccolo |
|---------------|---------:|---------:|-----------:|-----:|-------:|-------:|----------:|------:|--------:|
| bulk_insert   | 7.5 | 14.1 | 612.6 | 50.8 | 57.3 | 28.2 | 221.3 | 160.7 | 75.7 |
| single_insert | 14.6 | 30.1 | 234.4 | 111.7 | 124.8 | 106.9 | 132.1 | 315.5 | 247.3 |
| fetch_all     | 3.5 | 39.7 | 29.4 | 52.2 | 15.8 | 12.6 | 60.5 | 54.9 | 9.1 |
| count         | 0.0 | 0.3 | 0.7 | 0.2 | 0.2 | 0.1 | 0.1 | 1.6 | 0.5 |
| group_by      | 0.5 | 0.7 | 1.4 | 1.6 | 0.9 | 0.6 | 0.5 | - | 1.0 |
| filter        | 2.0 | 20.1 | 7.6 | 25.7 | 8.6 | 7.0 | 17.7 | 42.7 | 5.0 |
| get_by_pk     | 11.9 | 86.0 | 332.9 | 31.0 | 82.9 | 76.4 | 13.3 | 510.4 | 368.3 |
| update        | 0.5 | 0.6 | 1.9 | 42.8 | 1.2 | 1.2 | 1.2 | 1.7 | 1.6 |
| delete        | 0.3 | 0.4 | 1.2 | 35.3 | 0.8 | 0.7 | 0.8 | 1.2 | 1.1 |

With the fast path Yara ORM is fastest or tied on **every** operation
(SQLObject's hand-written raw SQL ties the sub-millisecond `group_by`) — including
the point reads it trailed on under the default async bridge (`get_by_pk` 1.1× vs
SQLObject, 2.6× vs Pony), while staying far ahead on throughput (bulk_insert
1.9–82×, fetch_all 2.6–17×, filter 2.5–21×). On the **default** async path the
per-statement bridge costs tens of µs on sequential point reads, so SQLObject's
lean sync active-record leads `get_by_pk` there; `sqlite://...?sync_fast_path=1`
removes that bridge (~7× faster point queries).

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

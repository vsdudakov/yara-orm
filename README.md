# Yara ORM

**A fast, async Python ORM with a [Tortoise](https://tortoise.github.io/)-style API and a Rust execution engine.**

[![CI](https://github.com/vsdudakov/yara-orm/actions/workflows/ci.yml/badge.svg)](https://github.com/vsdudakov/yara-orm/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/yara-orm.svg)](https://pypi.org/project/yara-orm/)
[![Python](https://img.shields.io/pypi/pyversions/yara-orm.svg)](https://pypi.org/project/yara-orm/)
[![Coverage](https://img.shields.io/badge/coverage-100%25-brightgreen.svg)](#development)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE.md)

Yara ORM gives you the ergonomics of an async Django/Tortoise-style ORM — models,
querysets, relations, migrations — while the hot path (connection pooling,
parameter binding, row decoding) runs in compiled Rust. It is **2–9× faster**
than popular pure-Python ORMs on common operations, ships with **PostgreSQL and
SQLite** backends, and is **100% test-covered**.

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
  `Q` objects, aggregation, `prefetch_related`, transactions, signals.
- 🗄️ **Pluggable backends** — PostgreSQL and SQLite today, selected by URL; a new
  database is one Rust trait + one Python dialect.
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
    await YaraOrm.init("postgres://localhost/app")   # or "sqlite:///app.db"
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

@atomic()
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
await YaraOrm.init("postgres://user@localhost/db")   # PostgreSQL (tokio-postgres)
await YaraOrm.init("sqlite:///path/to/app.db")        # SQLite (rusqlite)
```

The SQLite backend maps rich types (uuid/json/datetime/decimal) onto SQLite's
storage classes and reconstructs them on read from the declared column type, so
the model layer is identical across backends.

## Migrations

A Django/Tortoise-style, operation-based migration system. Migrations are
auto-generated from model changes and **backend-portable** — the same operations
render to PostgreSQL or SQLite DDL at apply time. Applied migrations are tracked
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

Operations: `CreateTable`, `DropTable`, `AddColumn`, `DropColumn`,
`CreateIndex`, `DropIndex`, plus hand-written `RunSQL` / `RunPython` for data
migrations. The same commands are available programmatically via
`yara_orm.MigrationManager`.

## Performance

Median of 5 runs, PostgreSQL 18, Python 3.12, 5000 rows — Yara ORM is fastest on
every operation measured. Cells show Yara ORM's time and each competitor's
slowdown factor (>1 means Yara ORM is faster). Full methodology in
[`benchmarks/`](benchmarks/).

| operation     | yara-orm | vs Tortoise | vs SQLAlchemy | vs Pony |
|---------------|---------:|------------:|--------------:|--------:|
| bulk_insert   | 11.5 ms  | 2.0×        | 5.9×          | 18.1×   |
| single_insert | 32.8 ms  | 2.4×        | 4.7×          |  1.8×   |
| fetch_all     |  3.5 ms  | 4.5×        | 3.5×          |  8.6×   |
| count         |  0.4 ms  | 1.5×        | 3.1×          |  1.3×   |
| filter        |  2.2 ms  | 3.9×        | 9.5×          |  7.2×   |
| get_by_pk     | 63.2 ms  | 3.1×        | 4.6×          |  1.3×   |
| update        |  3.3 ms  | 1.0×        | 1.3×          | 35.7×   |

Speed comes from the Rust hot path, **positional row decoding** (no per-row dict
or column-name allocation), **compiled-SQL + prepared-statement caching**, and
connection pooling. Run it yourself with `make bench`.

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
│     SqliteBackend ................. rusqlite│
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
make bench      # 4-way benchmark (needs `make bench-setup` once; Python ≤ 3.12 for Pony)
```

Requires a Rust toolchain (`rustup`) and a local PostgreSQL for the Postgres
tests; the SQLite tests are self-contained.

## Contributing

Issues and pull requests are welcome. Please run `make lint` and `make cov`
(both must be green — lint clean and 100% coverage) before opening a PR.

## License

[MIT](LICENSE.md) © Yara ORM contributors

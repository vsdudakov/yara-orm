---
title: Yara ORM — Fast async Python ORM with a Rust engine
description: Yara ORM is a fast, async Python ORM with a Rust engine. Tortoise-style models, querysets, relations and migrations for PostgreSQL and SQLite — 2–9× faster.
---

# Yara ORM

**A fast, async Python ORM with a Rust engine — [Tortoise](https://tortoise.github.io/)-style
models, querysets, relations and migrations for PostgreSQL and SQLite.**

[![CI](https://github.com/vsdudakov/yara-orm/actions/workflows/ci.yml/badge.svg)](https://github.com/vsdudakov/yara-orm/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/yara-orm.svg)](https://pypi.org/project/yara-orm/)
[![Python](https://img.shields.io/badge/python-3.9%E2%80%933.14-blue.svg)](https://pypi.org/project/yara-orm/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](https://github.com/vsdudakov/yara-orm/blob/main/LICENSE.md)

Yara ORM is a high-performance **async ORM for Python** that pairs the ergonomics of a
Django/Tortoise-style API — models, querysets, relations, aggregation and migrations —
with a hot path (connection pooling, parameter binding, row decoding) written in compiled
**Rust** (PyO3 + tokio). It is a drop-in-feel **alternative to Tortoise ORM and async
SQLAlchemy**: **2–9× faster** than popular pure-Python ORMs on common operations, with
first-class **PostgreSQL** and **SQLite** backends, full type hints, and **100% test
coverage**.

```python
from yara_orm import Model, YaraOrm, fields


class User(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=120)


await YaraOrm.init("postgres://localhost/app")   # or "sqlite:///app.db"
await YaraOrm.generate_schemas()

await User.create(name="Ada")
print(await User.filter(name__icontains="ad").count())
```

[Install Yara ORM :material-arrow-right:](getting-started/installation.md){ .md-button .md-button--primary }
[Quick start :material-arrow-right:](getting-started/quickstart.md){ .md-button }

## Why Yara ORM

- :zap: **Rust engine** — pooling, parameter binding and row decoding run in compiled
  code; the async bridge (PyO3 + tokio) keeps your event loop free.
- :jigsaw: **Familiar async API** — Tortoise/Django-style models, lazy chainable
  querysets, `Q` filters, aggregation, `prefetch_related`, transactions and signals.
- :file_cabinet: **Pluggable backends** — **PostgreSQL** and **SQLite** today, selected by
  URL; adding a database is one Rust trait plus one Python dialect.
- :truck: **Migrations** — operation-based, auto-generated and backend-portable
  (`makemigrations` / `upgrade` / `downgrade`).
- :test_tube: **Quality** — fully typed, linted (ruff + ty) and **100% test coverage**.
- :rocket: **Fast** — **2–9× faster** than Tortoise ORM, async SQLAlchemy and Pony on
  common operations. See [Performance](performance.md).

## Installation

```bash
pip install yara-orm
```

Prebuilt wheels are published for Linux, macOS and Windows on CPython 3.9–3.14, so
installation needs **no Rust toolchain**. See [Installation](getting-started/installation.md).

## Explore the docs

<div class="grid cards" markdown>

- :material-rocket-launch: **[Quick start](getting-started/quickstart.md)** — your first
  models, queries and relations in a few minutes.
- :material-table: **[Models & fields](guides/models-and-fields.md)** — define models,
  field types, enums, comments.
- :material-database-search: **[Querying](guides/querying.md)** — lazy querysets, lookups,
  `Q` objects, projections.
- :material-relation-many-to-many: **[Relations](guides/relations.md)** — FK, one-to-one,
  many-to-many and prefetch.
- :material-sigma: **[Aggregation](guides/aggregation.md)** — `Count`/`Sum`/`Avg`,
  `annotate` and `group_by`.
- :material-swap-horizontal: **[Migrations](guides/migrations.md)** — backend-portable,
  auto-generated schema migrations.

</div>

## How it compares

Yara ORM is built for teams who want the **developer experience of Tortoise ORM** or the
async query style of **SQLAlchemy 2.0**, but with materially lower per-query overhead
because binding and decoding happen in Rust rather than Python. If you are searching for a
**fast async Python ORM for PostgreSQL** (or a lightweight one for SQLite), Yara ORM is
designed to be a direct, fully-typed replacement.

## License

Yara ORM is released under the [MIT License](https://github.com/vsdudakov/yara-orm/blob/main/LICENSE.md).

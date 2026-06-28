---
title: Backends — PostgreSQL & SQLite
description: Yara ORM backends — connect the async Python ORM to PostgreSQL (tokio-postgres) or SQLite (rusqlite) by URL, with identical model code across both.
---

# Backends: PostgreSQL & SQLite

Yara ORM selects a database **backend by connection URL**. The same model and queryset
code runs unchanged across backends — only the URL you pass to `YaraOrm.init()` differs.

```python
from yara_orm import YaraOrm

await YaraOrm.init("postgres://user:pass@localhost/db")   # PostgreSQL (tokio-postgres)
await YaraOrm.init("sqlite:///path/to/app.db")             # SQLite (rusqlite)
```

## PostgreSQL

The PostgreSQL backend is built on **tokio-postgres** with a **deadpool** connection pool.

```python
await YaraOrm.init("postgres://user:password@host:5432/dbname")
```

- Async, pooled connections kept warm for steady-state latency.
- Prepared-statement caching (`prepare_cached`) per pooled connection.
- Case-insensitive lookups (`icontains`, `istartswith`, …) use SQL `ILIKE`.
- Column and table `description=` values become SQL `COMMENT`s.

!!! tip "URL schemes"
    Both `postgres://` and `postgresql://` style URLs are accepted, including
    `user:password@host:port/dbname` and standard query parameters.

## SQLite

The SQLite backend is built on **rusqlite** (bundled SQLite), bridged to async by hopping
to a blocking thread per call.

```python
await YaraOrm.init("sqlite:///app.db")     # file-backed
```

- Rich types (UUID, JSON, datetime, decimal) are mapped onto SQLite's storage classes and
  reconstructed on read from the declared column type — so your models behave identically.
- Case-insensitive lookups use `LIKE` (SQLite's `LIKE` is already case-insensitive for
  ASCII), since `ILIKE` is PostgreSQL-only. This is handled for you by the dialect.

!!! note "When to choose which"
    SQLite is ideal for tests, local development, embedded apps and small services;
    PostgreSQL for concurrent, production workloads. Because the model layer is identical,
    you can develop against SQLite and deploy on PostgreSQL.

## Mixing backends

Each named connection has its own backend, so a single app can talk to a PostgreSQL
database and a SQLite database at once. See [Multiple databases](../guides/multiple-databases.md).

```python
await YaraOrm.init("postgres://localhost/primary")     # default
await YaraOrm.add_connection("cache", "sqlite:///cache.db")
```

## Adding a new backend

The backend abstraction is intentionally a two-seam extension point:

1. A **Rust `Backend` trait** implementation (connection, execution, value conversion) plus
   a scheme match in `rust/src/backend/mod.rs`.
2. A **`BaseDialect` subclass** in `python/yara_orm/dialects.py` that renders SQL for the
   new database, registered via `register_dialect(name, DialectClass)`.

The model and queryset layers never change. See [Architecture](../architecture.md) for the
full picture.

## See also

- [Migrations](../guides/migrations.md) — backend-portable schema changes.
- [Performance](../performance.md) — PostgreSQL and SQLite benchmark results.

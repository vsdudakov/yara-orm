---
title: Architecture
description: How Yara ORM works — a Python model layer over a compiled Rust engine (PyO3 + tokio) with pluggable PostgreSQL and SQLite backends.
---

# Architecture

Yara ORM splits cleanly into a **Python model layer** and a **compiled Rust engine**. Python
owns the ergonomics — models, querysets, SQL generation — and hands finished SQL plus
parameters to Rust, which owns the performance-critical work: pooling, binding, type
conversion and row decoding.

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

## Responsibilities

- **Rust owns** pooling (deadpool), parameter binding, type conversion and row decoding.
  The async bridge (PyO3 + [pyo3-async-runtimes](https://github.com/PyO3/pyo3-async-runtimes)
  on tokio) lets Rust futures await without blocking your asyncio event loop.
- **Python owns** the model metaclass, descriptors for relations, the lazy queryset/SQL
  builder, and the per-dialect SQL rules.

## Key design points

- **`Value` enum** is the currency that crosses the boundary — a single tagged type that
  converts Python scalars ⇄ Rust ⇄ SQL parameters, checking the common scalar types first to
  minimize per-value work on large binds.
- **Positional row decoding** — SELECTs return column values by index with no per-row
  column-name allocation and no dict; Python fills model instances using a precomputed decode
  plan. This is a major part of why reads are fast (see [Performance](performance.md)).
- **Compiled-SQL + prepared-statement caching** — the SELECT column list, single-row INSERT
  and a fast-path `get()` are built once per model and reused; each pooled connection caches
  prepared statements.
- **Metaclass-driven models** — `ModelMeta` builds a `_meta: MetaInfo` for every model
  (table, fields, primary key, relations, m2m), and installs descriptors for forward/reverse
  relations and many-to-many managers.

## Adding a database

Backends plug in at exactly two seams, and the model layer never changes:

1. **Rust** — implement the `Backend` trait (connect, execute, fetch, value conversion) and
   add a scheme match in `rust/src/backend/mod.rs`.
2. **Python** — add a [`BaseDialect`](guides/migrations.md) subclass in
   `python/yara_orm/dialects.py` and register it with `register_dialect(name, DialectClass)`.

See [Backends](backends/index.md) for how PostgreSQL and SQLite use these seams.

## Distribution

Yara ORM is built with [maturin](https://www.maturin.rs/) in a mixed Python/Rust layout
(`python-source = "python"`, native module `yara_orm._engine`). Wheels are non-abi3 — one
wheel per CPython minor version — published for Linux, macOS and Windows. See
[Installation](getting-started/installation.md).

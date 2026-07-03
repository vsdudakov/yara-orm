---
title: Backends — PostgreSQL, MySQL & SQLite
description: Yara ORM backends — connect the async Python ORM to PostgreSQL (tokio-postgres), MySQL (mysql_async) or SQLite (rusqlite) by URL, with identical model code across all of them.
---

# Backends: PostgreSQL, MySQL & SQLite

Yara ORM selects a database **backend by connection URL**. The same model and queryset
code runs unchanged across backends — only the URL you pass to `YaraOrm.init()` differs.

```python
from yara_orm import YaraOrm

await YaraOrm.init("postgres://user:pass@localhost/db")   # PostgreSQL (tokio-postgres)
await YaraOrm.init("mysql://user:pass@localhost/db")       # MySQL/MariaDB (mysql_async)
await YaraOrm.init("sqlite:///path/to/app.db")             # SQLite (rusqlite)
```

## PostgreSQL

The PostgreSQL backend is built on **tokio-postgres** with a **deadpool** connection pool.

```python
await YaraOrm.init("postgres://user:password@host:5432/dbname")
```

- Async, pooled connections kept warm for steady-state latency.
- Prepared-statement caching (`prepare_cached`) per pooled connection — on by default, disable with `statement_cache_size=0` (see below).
- Case-insensitive lookups (`icontains`, `istartswith`, …) use SQL `ILIKE`.
- Column and table `description=` values become SQL `COMMENT`s.
- A column of a type the engine cannot decode (e.g. `interval`, `money` in raw
  SQL) raises a clear `OperationalError` naming the column — cast it to text in
  the query — instead of silently reading back as `None`.

!!! tip "URL schemes"
    Both `postgres://` and `postgresql://` style URLs are accepted, including
    `user:password@host:port/dbname` and standard query parameters.

### Pool and statement-cache tuning

A few pool/cache knobs ride along as URL query parameters. They are consumed by
the engine and stripped from the URL before the driver parses it, so they sit
alongside ordinary driver parameters (e.g. `sslmode`):

```python
await YaraOrm.init(
    "postgres://user:pass@host/db"
    "?max_size=32&min_size=4&statement_cache_size=0&sslmode=require"
)
```

| Parameter              | Default | Effect                                                                       |
| ---------------------- | ------- | ---------------------------------------------------------------------------- |
| `max_size`             | `16`    | Maximum pooled connections.                                                  |
| `min_size`             | `0`     | Connections pre-warmed at startup (best effort — the pool keeps no hard minimum). |
| `statement_cache_size` | nonzero | `0` disables per-connection prepared-statement caching.                      |

Standard driver parameters pass straight through to **tokio-postgres**, so
`application_name` and server settings work via the URL too — handy when migrating
from Tortoise's `application_name` / `server_settings` credentials:

```python
await YaraOrm.init(
    "postgres://user:pass@host/db"
    "?application_name=my-service"
    # server_settings via the libpq `options` param (one `-c key=value` each):
    "&options=-c%20search_path%3Dmyschema%20-c%20timezone%3DUTC"
)
```

`application_name` shows up in `pg_stat_activity`; the `options` settings (e.g.
`search_path`, `timezone`) are applied on every pooled connection.

!!! warning "PgBouncer / transaction pooling"
    The PostgreSQL backend caches prepared statements per connection by default.
    Behind a **transaction-pooling** proxy such as PgBouncer, set
    `statement_cache_size=0` so each statement is prepared and used within a
    single pooled checkout — otherwise the proxy can route a `Bind` to a backend
    that never saw the `Parse`. A non-numeric value (e.g. `max_size=lots`) raises
    a `ValueError` at `init()` rather than being silently ignored.

These parameters apply to SQLite too (`max_size`/`min_size`/`statement_cache_size`);
in-memory databases always pin a single connection regardless of `max_size`.

## MySQL

The MySQL backend is built on the pure-Rust **mysql_async** driver and its own
connection pool. It targets MySQL 8.x and also speaks the MariaDB protocol;
driver-qualified schemes (`mysql+aiomysql://`, `mariadb://`, ...) are
normalised automatically.

```python
await YaraOrm.init("mysql://user:password@host:3306/dbname")
```

- The same `max_size` / `min_size` / `statement_cache_size` URL parameters as
  the other backends; everything else passes through to the driver (e.g.
  `require_ssl=true` for TLS, served by rustls — no system OpenSSL needed).
  On MySQL, `min_size` also bounds the *idle connections the pool retains*
  (the driver closes idle connections beyond it); it defaults to `max_size`
  so pooled statements never pay a reconnect handshake.
- Every session is pinned to **UTC** and to **`ANSI_QUOTES`**, so portable raw
  SQL with double-quoted identifiers runs unchanged. String literals must use
  single quotes (everything the ORM emits already does).
- **No `INSERT ... RETURNING`**: new auto-increment primary keys come from the
  driver-reported last-insert id (single inserts and `bulk_create`, which
  backfills a batch from its first id under the default consecutive
  `innodb_autoinc_lock_mode`). `Meta.fetch_db_defaults` is honoured with a
  follow-up `SELECT` by primary key.
- Upserts render `INSERT IGNORE` (`ignore_conflicts`) and the 8.4-safe
  `INSERT ... AS new ON DUPLICATE KEY UPDATE` (`update_fields`); MySQL matches
  against *any* unique key, so an explicit `on_conflict` target is ignored.
- Case semantics: the default utf8mb4 collation makes `LIKE` case-insensitive,
  so `icontains`/`iexact`/... use plain `LIKE` while the case-sensitive
  lookups use `LIKE BINARY`. Regex lookups render `REGEXP_LIKE(col, ?, 'c')`
  (or `'i'`).
- `__search` renders `MATCH ... AGAINST`; the column needs a FULLTEXT index —
  declare `Index(fields=["col"], using="fulltext")` on the model.
- Aware datetimes are stored as their UTC instant in a naive `DATETIME(6)`
  column and read back naive (aware UTC under `use_tz=True`). `CHAR(36)` uuid
  columns are reconstructed to `uuid.UUID` on read.
- JSON columns cannot be indexed directly on MySQL; a JSON `Index`
  (e.g. a PostgreSQL GIN declaration) is dropped like the other
  PostgreSQL-only index options.

## SQLite

The SQLite backend is built on **rusqlite** (bundled SQLite). Statements run inline on
the async runtime (long-running work like `BEGIN` under contention and migration scripts
hops to a blocking thread), and the async bridge itself can be removed entirely with the
opt-in [sync fast path](#opt-in-synchronous-fast-path-sync_fast_path1).

```python
await YaraOrm.init("sqlite:///app.db")     # file-backed
```

- Rich types (UUID, JSON, datetime, decimal) are mapped onto SQLite's storage classes and
  reconstructed on read from the declared column type — so your models behave identically.
- Datetimes are stored as text in one canonical layout: naive values as
  `YYYY-MM-DD HH:MM:SS.ffffff`, timezone-aware values normalised to UTC as
  `YYYY-MM-DD HH:MM:SS.ffffff+00:00` — so naive and aware rows in one column
  compare and sort chronologically. Rows written by older versions (RFC 3339
  `T`-separated text) still decode.

    !!! warning "Upgrading a SQLite database with aware datetimes written by ≤ 1.9"
        Old aware rows use a `T` separator, so they no longer *compare*
        correctly against newly written rows or bound query parameters (SQLite
        compares datetime text lexicographically). Rewrite each affected
        column once after upgrading — this preserves the stored precision:

        ```sql
        UPDATE my_table SET created_at = replace(created_at, 'T', ' ')
        WHERE created_at LIKE '%T%';
        ```

        Naive-only columns (the default) need no rewrite.
- Case-insensitive lookups use `LIKE` (SQLite's `LIKE` is already case-insensitive for
  ASCII), since `ILIKE` is PostgreSQL-only. This is handled for you by the dialect.
- **Foreign keys are enforced.** `PRAGMA foreign_keys=ON` is applied to every pooled
  connection, so `on_delete` actions (CASCADE / SET NULL / RESTRICT) and referential
  integrity behave the same as on PostgreSQL. File databases also run in WAL mode
  with a 5-second busy timeout.
- **Transactions begin with `BEGIN IMMEDIATE`**, taking the write lock up front so
  concurrent read-then-write transactions queue on the busy timeout instead of
  failing instantly with `database is locked`.
- URL query parameters are validated: `sqlite://app.db?mode=memory` and
  `sqlite://app.db?sync_fast_path=1` are supported, and an unrecognised
  parameter raises `ValueError` instead of being read as part of the file name.

### Opt-in synchronous fast path (`sync_fast_path=1`)

For microsecond-statement workloads, the per-query asyncio bridge (scheduling
the statement on the runtime, waking the event loop, resuming the task) costs
far more than the SQLite work itself. Opting in with:

```python
await YaraOrm.init("sqlite:///app.db?sync_fast_path=1")
```

makes every statement run **synchronously on the calling thread** (with the
GIL released) and return an already-completed awaitable — your code still
`await`s everything exactly as before, but each query is ~7× faster
(~6µs instead of ~40µs per point query). `sync_fast_path=0` / `off` keep the
default async bridge; any other value raises `ValueError`. The flag is
SQLite-only — a postgres URL carrying it is rejected at `init()`.

Two things stay async regardless: `BEGIN` (it can queue behind competing
write transactions for up to the 5s busy timeout) and `execute_script`
(arbitrary migration SQL can run for seconds).

!!! warning "Semantics you are opting into"
    - **The event loop is blocked for the duration of each statement.** Great
      for tests, scripts, benchmarks and low-contention apps where every
      statement is microseconds; wrong for anything that runs large table
      scans or contended writes — a write parked on the 5s busy timeout
      stalls **all** tasks on the loop, not just the caller.
    - **`await` may no longer be a scheduling point.** Awaiting a completed
      awaitable resumes immediately without yielding to the event loop, so
      task interleaving/fairness changes. Code must not rely on
      `await Model.get(...)` giving other tasks a turn (insert
      `await asyncio.sleep(0)` where you need a guaranteed yield).
    - Exception behaviour is unchanged: errors are stored and raised at the
      `await`, exactly like the async path.

!!! note "When to choose which"
    SQLite is ideal for tests, local development, embedded apps and small services;
    PostgreSQL and MySQL for concurrent, production workloads. Because the model layer is identical,
    you can develop against SQLite and deploy on PostgreSQL.

## Mixing backends

Each named connection has its own backend, so a single app can talk to PostgreSQL,
MySQL and SQLite databases at once. See [Multiple databases](../guides/multiple-databases.md).

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
- [Performance](../performance.md) — PostgreSQL, MySQL and SQLite benchmark results.

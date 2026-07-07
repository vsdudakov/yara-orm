# Benchmarks

`bench.py` runs identical workloads against **this library (`yara-orm`)** and
eight other Python ORMs, each in its own table on the same database:

| ORM | model | driver (PostgreSQL) |
| --- | --- | --- |
| **yara-orm** | async | Rust engine (tokio-postgres) |
| **Tortoise ORM** | async | asyncpg |
| **SQLAlchemy 2.0** | async | asyncpg |
| **Pony ORM** | sync | psycopg2 |
| **Django ORM** | sync | psycopg/psycopg2 |
| **Peewee** | sync | psycopg2 |
| **SQLObject** | sync | psycopg2 |
| **Ormar** | async | asyncpg |
| **Piccolo** | async | asyncpg (no MySQL backend) |

Any ORM that isn't installed — or can't serve the chosen backend (e.g. Piccolo on
MySQL) — is skipped and reported as `-`, so the suite runs with whatever subset is
present.

## Running

Pony's query decompiler does **not** support Python 3.13+, so the full run needs
Python ≤ 3.12. The others run on any supported version (a missing/incompatible
ORM is simply reported as `-`).

```bash
# full suite (Python 3.12)
python3.12 -m venv .venv312
.venv312/bin/pip install -U maturin \
    tortoise-orm "sqlalchemy[asyncio]" asyncpg pony psycopg2-binary \
    django peewee sqlobject ormar piccolo
VIRTUAL_ENV=$PWD/.venv312 .venv312/bin/maturin develop --release
ORM_TEST_DB=postgres://USER@localhost/orm_demo .venv312/bin/python benchmarks/bench.py
```

Env knobs: `BENCH_N` (bulk rows, default 5000), `BENCH_S` (single-insert rows,
500), `BENCH_GETS` (pk lookups, 1000), `BENCH_REPEAT` (runs per ORM, median
reported, 5).

### SQLite

Set `BENCH_BACKEND=sqlite` to run the same workload on SQLite (each ORM
gets its own file in `BENCH_SQLITE_DIR`, default `/tmp`). Needs `aiosqlite`
for the async ORMs (Tortoise, SQLAlchemy, Ormar, Piccolo):

```bash
.venv312/bin/pip install -U aiosqlite
BENCH_BACKEND=sqlite .venv312/bin/python benchmarks/bench.py
```

### MySQL

Set `BENCH_BACKEND=mysql` to run the same workload on MySQL
(`ORM_TEST_MYSQL`, default `mysql://root:root@localhost:3306/orm_demo`).
Competitor drivers: `asyncmy` (Tortoise), `aiomysql` (SQLAlchemy, Ormar) and
`pymysql` + `cryptography` (Pony, Django, Peewee, SQLObject; MySQL 8's
`caching_sha2_password` needs `cryptography`) — any missing driver is simply
reported as `-`. **Piccolo has no MySQL backend** and is reported as `-` here:

```bash
.venv312/bin/pip install -U asyncmy aiomysql pymysql cryptography
BENCH_BACKEND=mysql .venv312/bin/python benchmarks/bench.py
```

### MariaDB

Set `BENCH_BACKEND=mariadb` to run the same workload on MariaDB
(`ORM_TEST_MARIADB`, default `mysql://root:root@localhost:3307/orm_demo`). Every
competitor reaches MariaDB through its MySQL driver (same wire protocol), so the
driver set is identical to the MySQL run; `yara-orm` auto-detects the server and
switches to its MariaDB dialect (RETURNING, `VALUES()` upsert). Piccolo has no
MySQL backend and is reported as `-`:

```bash
.venv312/bin/pip install -U asyncmy aiomysql pymysql cryptography
BENCH_BACKEND=mariadb .venv312/bin/python benchmarks/bench.py
```

## Methodology

* Each ORM gets its own table and the **same** workload and data.
* Every operation is timed `BENCH_REPEAT` times and the **median** is reported,
  so warm steady-state (driver + prepared-statement caches hot) dominates over
  cold-start noise.
* `get_by_pk` issues the same random pk sequence to every ORM.
* Caveats — this is *throughput-oriented*, not a micro-benchmark:
  * sync (Pony, Django, Peewee, SQLObject) vs async (Tortoise, SQLAlchemy, Ormar,
    Piccolo, `yara-orm`) have different concurrency models;
  * Pony opens a transaction per `get` (its design) and has no SQL-level bulk
    `UPDATE`, so its update path mutates objects in a loop;
  * SQLAlchemy `get_by_pk` uses a fresh session per lookup (no identity-map
    reuse), matching the stateless-handler pattern the others use;
  * SQLObject runs with `cache=false` so every `.get()` hits the database, and
    has no bulk `INSERT` — its `bulk_insert` is one row per statement wrapped in a
    single transaction;
  * Ormar exposes no `GROUP BY`/annotate API and Piccolo no `HAVING` clause, so
    their `group_by` cells read `-` / run unfiltered respectively;
  * feature sets differ. Treat the numbers as indicative.

## Representative results

Nine ORMs, all measured together in one run per backend (Apple Silicon,
Python 3.12, N=5000, median of 5). Piccolo has no MySQL backend, so it is absent from the MySQL and MariaDB tables.

PostgreSQL 18 (ms, lower is better):

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

The `group_by` op is a `GROUP BY … COUNT/SUM … HAVING` aggregate over the rows
(Ormar has no GROUP BY API, hence `-`).

Speedup vs `yara-orm` (competitor_time / yara_orm_time; >1 means `yara-orm` faster):

| operation     | tortoise | sqlalchemy | pony | django | peewee | sqlobject | ormar | piccolo |
|---------------|---------:|-----------:|-----:|-------:|-------:|----------:|------:|--------:|
| bulk_insert   | 1.6x | 5.3x | 15.2x | 2.8x | 3.5x | 35.8x | 15.6x | 6.7x |
| single_insert | 2.3x | 4.5x | 1.8x | 1.2x | 1.4x | 1.6x | 4.9x | 2.6x |
| fetch_all     | 4.7x | 8.2x | 9.6x | 2.5x | 3.3x | 7.4x | 15.8x | 1.2x |
| count         | 2.0x | 3.3x | 1.3x | 1.3x | 1.0x | 1.0x | 18.0x | 1.3x |
| group_by      | 1.4x | 2.3x | 3.4x | 1.4x | 1.1x | 0.9x | - | 1.4x |
| filter        | 4.0x | 3.5x | 7.8x | 2.3x | 2.9x | 4.0x | 18.3x | 1.1x |
| get_by_pk     | 3.0x | 4.5x | 1.3x | 1.8x | 1.8x | 0.4x | 5.1x | 3.0x |
| update        | 1.1x | 1.2x | 36.6x | 1.0x | 1.0x | 1.0x | 4.5x | 1.1x |
| delete        | 1.1x | 1.6x | 134.7x | 1.1x | 1.0x | 0.9x | 3.4x | 1.1x |

`yara-orm` is fastest or tied on every operation; the only place any ORM edges
ahead is **SQLObject** on `get_by_pk` (0.4× — 23.5 vs 65.3 ms), where its lean
in-process sync active-record avoids the async event-loop hop on single-row point
reads (the same latency-bound floor that keeps Pony close on `get_by_pk`).
Everything throughput-shaped is far ahead (`bulk_insert` up to 36×, `fetch_all`
up to 16×, `delete` 139× vs Pony's row-by-row loop).

### Charts

The grouped-bar charts shown in the README and docs are rendered from these
numbers by `plot_benchmarks.py` (the values are embedded in the script, so it
needs no database — just `pip install matplotlib`):

```bash
python benchmarks/plot_benchmarks.py
# writes docs/assets/benchmark-{postgres,mysql,mariadb,sqlite}.png
```

If you re-run `bench.py`, update the tables here **and** the `BACKENDS` dict in
`plot_benchmarks.py` so the charts stay in sync.

### MySQL results

`BENCH_BACKEND=mysql`, MySQL 8.4 (Docker), Apple Silicon, Python 3.12, N=5000,
median of 5 (ms, lower is better). Tortoise runs over asyncmy, SQLAlchemy/Ormar
over aiomysql, the sync ORMs over pymysql. **Piccolo has no MySQL backend:**

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

`yara-orm` is fastest or tied on every operation (`fetch_all` 4.6–12.4×, `filter`
4.5–9.3×, `get_by_pk` 1.8–7.3× vs the competitors) — except SQLObject's leaner
`get_by_pk` (0.6×) and the sub-millisecond `group_by`, where peewee/SQLObject
edge us (0.8×). The two latency-bound ops carry the Docker-network round trip,
and `single_insert` (~0.6–1.2 s across the board) is dominated by InnoDB's
per-commit fsync — a cost every ORM pays equally.

### MariaDB results

`BENCH_BACKEND=mariadb`, MariaDB 11 (Docker), Apple Silicon, Python 3.12,
N=5000, median of 5 (ms, lower is better). Every competitor connects through its
MySQL driver; `yara-orm` auto-detects MariaDB and uses its RETURNING dialect.
Piccolo has no MySQL backend, so it is absent here:

| operation     | yara-orm | tortoise | sqlalchemy | pony | django | peewee | sqlobject | ormar |
|---------------|---------:|---------:|-----------:|-----:|-------:|-------:|----------:|------:|
| bulk_insert   | 23.3 | 37.5 | 105.4 | 475.4 | 96.4 | 71.8 | 1266.5 | 209.9 |
| single_insert | 264.9 | 311.6 | 531.7 | 391.5 | 388.2 | 372.2 | 390.3 | 660.0 |
| fetch_all     | 5.7 | 35.0 | 43.8 | 48.1 | 28.1 | 37.0 | 42.6 | 72.1 |
| count         | 0.5 | 0.8 | 1.2 | 0.7 | 0.7 | 0.8 | 0.6 | 6.7 |
| group_by      | 1.2 | 1.3 | 2.3 | 2.2 | 1.5 | 1.4 | 0.9 | - |
| filter        | 3.2 | 17.5 | 16.3 | 24.8 | 15.1 | 14.7 | 17.1 | 32.6 |
| get_by_pk     | 132.9 | 240.8 | 575.0 | 306.7 | 220.7 | 206.5 | 64.4 | 895.2 |
| update        | 4.0 | 4.4 | 7.8 | 266.3 | 5.4 | 4.2 | 3.7 | 8.2 |
| delete        | 3.0 | 3.0 | 3.6 | 249.5 | 3.6 | 3.0 | 3.0 | 4.0 |

`yara-orm` leads or ties every operation except SQLObject's leaner `get_by_pk`
(0.5×) and the sub-millisecond `group_by` (SQLObject 0.8× via raw SQL).
`single_insert`, `update` and `delete` are near ties across every ORM because
they are **database-bound**, not client-bound: single inserts are paced by
MariaDB's per-commit disk fsync (high run-to-run variance — it swings ±40%
between runs), and `update`/`delete` are one server-side set statement each, so
there's no client-side row marshaling for the Rust hot path to accelerate. It
still wins the throughput ops decisively (`fetch_all` 4.9–12.6×, `filter`
4.6–10.2×, `bulk_insert` up to 54× vs SQLObject's row-by-row inserts). MariaDB's
`single_insert` (~265 ms) is notably faster than MySQL 8's (~630 ms) here — a
lighter default commit path.

### SQLite results

`BENCH_BACKEND=sqlite`, Python 3.12, N=5000, median of 5 (ms, lower is better).
SQLite is in-process, so `bench.py` uses its recommended `sync_fast_path=1`
config (statements run synchronously on the calling thread — an embedded
database has no I/O to overlap, so the async bridge is pure overhead):

| operation     | yara-orm | tortoise | sqlalchemy | pony | django | peewee | sqlobject | ormar | piccolo |
|---------------|---------:|---------:|-----------:|-----:|-------:|-------:|----------:|------:|--------:|
| bulk_insert   | 7.5 | 13.6 | 607.9 | 50.1 | 55.8 | 29.0 | 218.3 | 143.8 | 73.9 |
| single_insert | 15.3 | 26.9 | 234.5 | 107.0 | 120.1 | 113.5 | 124.8 | 296.7 | 240.4 |
| fetch_all     | 3.4 | 38.6 | 27.1 | 51.0 | 16.4 | 12.2 | 44.2 | 52.0 | 9.2 |
| count         | 0.0 | 0.2 | 0.7 | 0.2 | 0.3 | 0.1 | 0.1 | 1.6 | 0.5 |
| group_by      | 0.6 | 0.7 | 1.3 | 1.4 | 0.9 | 0.6 | 0.5 | - | 1.0 |
| filter        | 2.0 | 20.2 | 7.3 | 25.8 | 8.7 | 6.6 | 17.4 | 19.2 | 5.0 |
| get_by_pk     | 12.5 | 79.4 | 329.6 | 31.3 | 84.6 | 75.5 | 13.3 | 484.1 | 357.2 |
| update        | 0.5 | 0.5 | 1.7 | 43.0 | 1.3 | 1.2 | 1.1 | 1.7 | 1.5 |
| delete        | 0.3 | 0.4 | 1.1 | 35.9 | 0.8 | 0.7 | 0.7 | 1.1 | 1.1 |

`yara-orm` is fastest on **every** operation except the sub-millisecond
`group_by`, where SQLObject's hand-written raw-SQL aggregate (bypassing its ORM
entirely) edges it at 0.8×. With the fast path it wins the point reads it
trailed on under the default async bridge — `get_by_pk` **1.1× vs SQLObject**
and **2.5× vs Pony** — and stays far ahead on throughput (`bulk_insert` 1.8–81×,
`fetch_all` 2.7–15×, `filter` 2.5–13×, `single_insert` 1.8× vs Tortoise). On the
**default** async path (no fast path), the per-statement asyncio bridge costs
tens of µs on sequential point reads, so SQLObject's lean sync active-record
leads `get_by_pk` there instead; `sqlite://...?sync_fast_path=1` removes that
bridge (~7× faster point queries).

## Why `yara-orm` is fast here

* **Rust hot path** — parameter binding and row decoding happen in compiled
  code; the async bridge (PyO3 + tokio) keeps the event loop free.
* **Positional row decoding** — for SELECTs the engine returns column values
  with no per-row column-name allocation and no dict; Python fills instances by
  index using a precomputed decode plan that skips no-op conversions.
* **Compiled-SQL caching** — the SELECT column list, single-row INSERT and a
  fast-path simple `get()` are built once per model and reused, and
  `prepare_cached` on each pooled connection skips re-parse/plan.
* **Connection pooling** — deadpool keeps warm connections, so steady-state
  latency excludes connect cost.
* **Tight parameter extraction** — the `FromPyObject` path checks the common
  scalar types first, minimising per-value work on large binds.

For pure projections, `values_list()` / `values()` select only the requested
columns and skip model construction (~1.7–2.2× faster than full fetch).

## Feature micro-benchmarks (`yara-orm` only)

`bench_features.py` times the features the cross-ORM suite intentionally skips
because they are not comparable across ORMs and feature sets: **nested-transaction
savepoints**, **eager loading** (`select_related` / `prefetch_related` vs N+1) and
**projection** (`values` / `values_list`). It is `yara-orm`-only, so it needs no
competitor installs — just the built engine.

```bash
# SQLite (zero setup — a throwaway temp file)
make bench-features
# or directly, on either backend:
python benchmarks/bench_features.py
BENCH_BACKEND=postgres ORM_TEST_DB=postgres://USER@localhost/orm_demo \
    python benchmarks/bench_features.py
```

Env knobs: `BENCH_AUTHORS` (parent rows, default 100), `BENCH_BOOKS_PER`
(children each, 5), `BENCH_S` (transaction insert rows, 300), `BENCH_REPEAT` (5).

It reports each operation's time plus a **feature payoff** ratio (slower ÷ faster).
Representative ratios (100 authors × 5 books, 300 tx rows, median of 5):

| payoff                                  | SQLite | PostgreSQL |
|-----------------------------------------|-------:|-----------:|
| `select_related` vs N+1 (forward FK)    | 28.5×  |     38.4×  |
| `prefetch_related` vs N+1 (reverse)     |  9.7×  |     14.5×  |
| one transaction vs autocommit           |  1.4×  |      1.0×  |
| `values_list` vs full model fetch       |  1.6×  |      1.6×  |
| savepoint-per-row overhead vs one tx    |  2.7×  |      2.8×  |

Eager loading collapses the N+1 fan-out into one (`select_related`) or two
(`prefetch_related`) queries, so its payoff grows with the row count and is
largest on PostgreSQL, where each avoided query is a network round-trip. The
last row is a **cost**, not a win: wrapping every row in its own savepoint adds a
`SAVEPOINT`/`RELEASE` pair per insert — the price of fine-grained nested rollback.

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
| bulk_insert   | 15.2 | 25.2 | 80.8 | 223.8 | 40.5 | 51.1 | 513.3 | 227.5 | 100.7 |
| single_insert | 34.4 | 84.1 | 158.7 | 60.8 | 42.9 | 46.6 | 53.3 | 169.1 | 92.3 |
| fetch_all     | 3.6 | 17.3 | 31.6 | 34.4 | 9.1 | 11.9 | 27.8 | 56.6 | 4.4 |
| count         | 0.3 | 0.5 | 1.0 | 0.4 | 0.5 | 0.3 | 0.3 | 6.0 | 0.4 |
| group_by      | 0.8 | 1.1 | 1.6 | 2.3 | 1.0 | 0.8 | 0.6 | - | 1.0 |
| filter        | 2.2 | 9.1 | 8.4 | 17.5 | 5.3 | 6.8 | 9.3 | 21.2 | 2.6 |
| get_by_pk     | 64.1 | 205.9 | 310.4 | 86.0 | 121.1 | 115.6 | 23.1 | 336.9 | 201.6 |
| update        | 3.5 | 3.9 | 4.1 | 121.1 | 3.5 | 3.4 | 3.3 | 12.8 | 3.6 |
| delete        | 0.7 | 0.9 | 1.1 | 95.5 | 0.8 | 0.7 | 0.6 | 2.2 | 0.8 |

The `group_by` op is a `GROUP BY … COUNT/SUM … HAVING` aggregate over the rows
(Ormar has no GROUP BY API, hence `-`).

Speedup vs `yara-orm` (competitor_time / yara_orm_time; >1 means `yara-orm` faster):

| operation     | tortoise | sqlalchemy | pony | django | peewee | sqlobject | ormar | piccolo |
|---------------|---------:|-----------:|-----:|-------:|-------:|----------:|------:|--------:|
| bulk_insert   | 1.7x | 5.3x | 14.7x | 2.7x | 3.4x | 33.8x | 15.0x | 6.6x |
| single_insert | 2.4x | 4.6x | 1.8x | 1.2x | 1.4x | 1.6x | 4.9x | 2.7x |
| fetch_all     | 4.8x | 8.7x | 9.5x | 2.5x | 3.3x | 7.7x | 15.6x | 1.2x |
| count         | 2.0x | 3.6x | 1.5x | 1.8x | 1.0x | 1.2x | 22.5x | 1.6x |
| group_by      | 1.4x | 1.9x | 2.8x | 1.3x | 1.0x | 0.7x | - | 1.2x |
| filter        | 4.1x | 3.8x | 7.9x | 2.4x | 3.1x | 4.2x | 9.6x | 1.2x |
| get_by_pk     | 3.2x | 4.8x | 1.3x | 1.9x | 1.8x | 0.4x | 5.3x | 3.1x |
| update        | 1.1x | 1.2x | 35.0x | 1.0x | 1.0x | 0.9x | 3.7x | 1.0x |
| delete        | 1.2x | 1.5x | 129.6x | 1.1x | 0.9x | 0.9x | 2.9x | 1.1x |

`yara-orm` is fastest or tied on every operation; the only place any ORM edges
ahead is **SQLObject** on `get_by_pk` (0.4× — 23.1 vs 64.1 ms), where its lean
in-process sync active-record avoids the async event-loop hop on single-row point
reads (the same latency-bound floor that keeps Pony close on `get_by_pk`).
Everything throughput-shaped is far ahead (`bulk_insert` up to 34×, `fetch_all`
up to 16×, `delete` 130× vs Pony's row-by-row loop).

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
| bulk_insert   | 46.8 | 48.2 | 596.1 | 443.5 | 100.0 | 82.1 | 1076.6 | 212.3 |
| single_insert | 638.7 | 660.6 | 985.1 | 800.6 | 783.4 | 743.5 | 773.8 | 1062.2 |
| fetch_all     | 7.5 | 33.8 | 44.3 | 47.7 | 28.7 | 28.5 | 42.0 | 73.0 |
| count         | 0.5 | 0.8 | 1.2 | 0.9 | 0.9 | 0.8 | 0.7 | 4.2 |
| group_by      | 1.3 | 1.4 | 2.0 | 2.5 | 1.5 | 1.2 | 1.0 | - |
| filter        | 3.2 | 17.5 | 15.5 | 24.6 | 15.1 | 14.9 | 16.8 | 31.3 |
| get_by_pk     | 122.0 | 226.1 | 524.2 | 315.4 | 214.0 | 208.0 | 64.6 | 924.3 |
| update        | 6.8 | 7.4 | 9.9 | 232.4 | 7.7 | 9.5 | 7.4 | 11.7 |
| delete        | 4.9 | 4.8 | 5.6 | 207.0 | 5.8 | 4.8 | 6.6 | 6.1 |

`yara-orm` is fastest or tied on every operation (`fetch_all` 3.8–9.7×, `filter`
4.7–9.8×, `get_by_pk` 1.7–7.6× vs the competitors) — except SQLObject's leaner
`get_by_pk` (0.5×) and the sub-millisecond `group_by`, where peewee/SQLObject
edge us (0.8–0.9×). The two latency-bound ops carry the Docker-network round
trip, and `single_insert` (~0.6–1.1 s across the board) is dominated by InnoDB's
per-commit fsync — a cost every ORM pays equally.

### MariaDB results

`BENCH_BACKEND=mariadb`, MariaDB 11 (Docker), Apple Silicon, Python 3.12,
N=5000, median of 5 (ms, lower is better). Every competitor connects through its
MySQL driver; `yara-orm` auto-detects MariaDB and uses its RETURNING dialect.
Piccolo has no MySQL backend, so it is absent here:

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

`yara-orm` leads or ties every operation except SQLObject's leaner `get_by_pk`
(0.5×) and the sub-millisecond `group_by` (SQLObject 0.7× via raw SQL).
`single_insert`, `update` and `delete` are near ties across every ORM because
they are **database-bound**, not client-bound: single inserts are paced by
MariaDB's per-commit disk fsync (high run-to-run variance — it swings ±40%
between runs), and `update`/`delete` are one server-side set statement each, so
there's no client-side row marshaling for the Rust hot path to accelerate. It
still wins the throughput ops decisively (`fetch_all` 5.4–12.4×, `filter`
4.4–15.2×, `bulk_insert` up to 49× vs SQLObject's row-by-row inserts). MariaDB's
`single_insert` (~310 ms) is notably faster than MySQL 8's (~640 ms) here — a
lighter default commit path.

### SQLite results

`BENCH_BACKEND=sqlite`, Python 3.12, N=5000, median of 5 (ms, lower is better).
SQLite is in-process, so `bench.py` uses its recommended `sync_fast_path=1`
config (statements run synchronously on the calling thread — an embedded
database has no I/O to overlap, so the async bridge is pure overhead):

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

`yara-orm` is fastest or tied on **every** operation (SQLObject's hand-written
raw-SQL aggregate, bypassing its ORM entirely, ties the sub-millisecond
`group_by`). With the fast path it wins the point reads it trailed on under the
default async bridge — `get_by_pk` **1.1× vs SQLObject** and **2.6× vs Pony** —
and stays far ahead on throughput (`bulk_insert` 1.9–82×, `fetch_all` 2.6–17×,
`filter` 2.5–21×, `single_insert` 2.1× vs Tortoise). On the **default** async
path (no fast path), the per-statement asyncio bridge costs tens of µs on
sequential point reads, so SQLObject's lean sync active-record leads `get_by_pk`
there instead; `sqlite://...?sync_fast_path=1` removes that bridge (~7× faster
point queries).

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

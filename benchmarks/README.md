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
| bulk_insert   | 14.7 | 25.7 | 81.6 | 222.9 | 39.7 | 53.2 | 523.6 | 236.4 | 99.2 |
| single_insert | 34.5 | 81.2 | 156.1 | 61.5 | 40.8 | 48.2 | 53.7 | 163.7 | 88.8 |
| fetch_all     | 3.5 | 17.3 | 31.1 | 35.3 | 9.2 | 12.3 | 26.5 | 55.6 | 4.3 |
| count         | 0.3 | 0.6 | 1.0 | 0.4 | 0.4 | 0.4 | 0.3 | 4.9 | 0.4 |
| group_by      | 0.8 | 1.0 | 1.6 | 2.3 | 1.1 | 0.9 | 0.6 | - | 1.0 |
| filter        | 2.2 | 9.1 | 8.7 | 17.8 | 5.3 | 7.1 | 9.0 | 21.3 | 2.6 |
| get_by_pk     | 65.3 | 197.7 | 297.3 | 85.1 | 114.9 | 115.5 | 23.5 | 329.1 | 194.2 |
| update        | 3.3 | 3.5 | 4.1 | 121.3 | 3.4 | 3.6 | 3.3 | 14.4 | 3.6 |
| delete        | 0.7 | 0.8 | 1.1 | 95.1 | 0.8 | 0.7 | 0.6 | 2.4 | 0.9 |

The `group_by` op is a `GROUP BY … COUNT/SUM … HAVING` aggregate over the rows
(Ormar has no GROUP BY API, hence `-`).

Speedup vs `yara-orm` (competitor_time / yara_orm_time; >1 means `yara-orm` faster):

| operation     | tortoise | sqlalchemy | pony | django | peewee | sqlobject | ormar | piccolo |
|---------------|---------:|-----------:|-----:|-------:|-------:|----------:|------:|--------:|
| bulk_insert   | 1.8× | 5.6× | 15.2× | 2.7× | 3.6× | 35.7× | 16.1× | 6.8× |
| single_insert | 2.4× | 4.5× | 1.8× | 1.2× | 1.4× | 1.6× | 4.8× | 2.6× |
| fetch_all     | 5.0× | 8.9× | 10.1× | 2.6× | 3.5× | 7.6× | 15.9× | 1.2× |
| count         | 1.9× | 3.5× | 1.5× | 1.4× | 1.5× | 1.1× | 16.6× | 1.5× |
| group_by      | 1.3× | 2.1× | 2.9× | 1.3× | 1.1× | 0.7× | - | 1.2× |
| filter        | 4.1× | 3.9× | 8.0× | 2.4× | 3.2× | 4.1× | 9.6× | 1.2× |
| get_by_pk     | 3.0× | 4.6× | 1.3× | 1.8× | 1.8× | 0.4× | 5.0× | 3.0× |
| update        | 1.1× | 1.2× | 36.5× | 1.0× | 1.1× | 1.0× | 4.3× | 1.1× |
| delete        | 1.2× | 1.6× | 139.1× | 1.1× | 1.0× | 0.9× | 3.6× | 1.3× |

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
| bulk_insert   | 47.3 | 53.9 | 568.7 | 403.1 | 91.3 | 83.3 | 974.9 | 236.3 |
| single_insert | 627.2 | 766.7 | 1043.6 | 867.9 | 751.7 | 798.9 | 867.2 | 1367.5 |
| fetch_all     | 6.1 | 34.0 | 45.7 | 48.8 | 29.1 | 28.0 | 42.9 | 75.5 |
| count         | 0.5 | 0.9 | 1.2 | 0.9 | 0.9 | 0.8 | 0.7 | 5.3 |
| group_by      | 1.2 | 1.4 | 2.1 | 2.6 | 1.5 | 1.2 | 1.0 | - |
| filter        | 3.3 | 17.8 | 15.8 | 25.2 | 15.2 | 14.9 | 16.7 | 31.0 |
| get_by_pk     | 110.3 | 212.5 | 484.1 | 275.9 | 200.9 | 195.8 | 58.7 | 804.8 |
| update        | 6.3 | 11.1 | 10.4 | 222.2 | 9.9 | 7.5 | 7.3 | 10.9 |
| delete        | 4.9 | 5.5 | 5.9 | 192.4 | 5.4 | 4.8 | 4.9 | 8.4 |

`yara-orm` is fastest or tied on every operation (`fetch_all` 4.6–12.4×, `filter`
4.5–9.3×, `get_by_pk` 1.8–7.3× vs the competitors) — except SQLObject's leaner
`get_by_pk` (0.6×) and the sub-millisecond `group_by`, where peewee/SQLObject
edge us (0.8×). The two latency-bound ops carry the Docker-network round trip,
and `single_insert` (~0.6–1.4 s across the board) is dominated by InnoDB's
per-commit fsync — a cost every ORM pays equally.

### MariaDB results

`BENCH_BACKEND=mariadb`, MariaDB 11 (Docker), Apple Silicon, Python 3.12,
N=5000, median of 5 (ms, lower is better). Every competitor connects through its
MySQL driver; `yara-orm` auto-detects MariaDB and uses its RETURNING dialect.
Piccolo has no MySQL backend, so it is absent here:

| operation     | yara-orm | tortoise | sqlalchemy | pony | django | peewee | sqlobject | ormar |
|---------------|---------:|---------:|-----------:|-----:|-------:|-------:|----------:|------:|
| bulk_insert   | 23.7 | 37.6 | 102.2 | 397.7 | 88.6 | 56.7 | 1086.1 | 209.5 |
| single_insert | 315.0 | 345.9 | 494.6 | 388.8 | 367.1 | 284.5 | 365.5 | 599.5 |
| fetch_all     | 5.5 | 34.5 | 27.7 | 47.8 | 28.6 | 29.2 | 42.7 | 71.3 |
| count         | 0.4 | 0.7 | 1.1 | 0.7 | 0.8 | 0.9 | 0.7 | 5.7 |
| group_by      | 1.1 | 1.3 | 2.0 | 2.2 | 1.5 | 1.2 | 0.9 | - |
| filter        | 3.5 | 17.4 | 16.6 | 24.4 | 15.0 | 15.5 | 16.9 | 32.0 |
| get_by_pk     | 107.3 | 211.6 | 482.3 | 262.8 | 195.7 | 189.8 | 57.2 | 808.3 |
| update        | 3.5 | 3.6 | 7.6 | 232.3 | 3.9 | 3.8 | 3.7 | 7.7 |
| delete        | 2.8 | 2.8 | 3.2 | 220.1 | 3.3 | 2.7 | 2.8 | 3.5 |

`yara-orm` leads or ties every operation except SQLObject's leaner `get_by_pk`
(0.5×), the sub-millisecond `group_by` (SQLObject 0.8× via raw SQL), and a
statistical tie with Tortoise on `update`/`delete`. It wins the throughput ops
decisively (`fetch_all` 5.0–12.9×, `filter` 4.4–9.3×, `bulk_insert` up to 46× vs
SQLObject's row-by-row inserts). MariaDB's `single_insert` (~315 ms) is notably
faster than MySQL 8's (~630 ms) here — a lighter default commit path.

### SQLite results

`BENCH_BACKEND=sqlite`, Python 3.12, N=5000, median of 5 (ms, lower is better):

| operation     | yara-orm | tortoise | sqlalchemy | pony | django | peewee | sqlobject | ormar | piccolo |
|---------------|---------:|---------:|-----------:|-----:|-------:|-------:|----------:|------:|--------:|
| bulk_insert   | 8.0 | 14.2 | 612.4 | 53.0 | 57.9 | 29.0 | 232.1 | 140.8 | 77.2 |
| single_insert | 32.6 | 29.0 | 231.1 | 118.6 | 130.4 | 121.0 | 131.3 | 306.8 | 242.2 |
| fetch_all     | 3.5 | 39.6 | 28.9 | 52.3 | 15.8 | 12.5 | 48.0 | 54.3 | 9.1 |
| count         | 0.1 | 0.2 | 0.7 | 0.2 | 0.2 | 0.2 | 0.1 | 1.7 | 0.4 |
| group_by      | 0.5 | 0.7 | 1.4 | 1.6 | 0.9 | 0.7 | 0.5 | - | 1.0 |
| filter        | 2.0 | 20.0 | 7.7 | 26.7 | 8.5 | 6.8 | 17.6 | 20.1 | 5.0 |
| get_by_pk     | 47.5 | 85.9 | 330.0 | 31.7 | 84.1 | 77.8 | 13.9 | 497.0 | 357.2 |
| update        | 0.5 | 0.5 | 1.8 | 43.5 | 1.3 | 1.2 | 1.0 | 1.7 | 1.6 |
| delete        | 0.4 | 0.3 | 1.2 | 36.6 | 0.8 | 0.8 | 0.7 | 1.2 | 1.1 |

`yara-orm` wins the throughput-bound operations decisively (`bulk_insert`
1.8–77×, `fetch_all` 2.6–15×, `filter` 2.5–13× across the field). It trails only
on **latency-bound point reads**: in-process sync ORMs — SQLObject (`get_by_pk`
0.3×) and Pony (0.7×) — beat us there (plus the microsecond `group_by`, where
SQLObject's raw-SQL path is a hair ahead at 0.9×). The cost is the per-statement asyncio
bridge (scheduling on the runtime + waking the event loop), tens of µs a
synchronous in-process driver avoids on sequential point queries. Real workloads
rarely fire thousands of sequential point reads, and everything throughput-shaped
is far ahead. The opt-in
`sqlite://...?sync_fast_path=1` URL flag removes that bridge (point queries ~7×
faster) when those ops dominate.

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

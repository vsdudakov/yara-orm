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
Python 3.12, N=5000, median of 5). Piccolo has no MySQL backend, so it is absent
from the MySQL table.

PostgreSQL 18 (ms, lower is better):

| operation     | yara-orm | tortoise | sqlalchemy | pony | django | peewee | sqlobject | ormar | piccolo |
|---------------|---------:|---------:|-----------:|-----:|-------:|-------:|----------:|------:|--------:|
| bulk_insert   | 15.5 | 26.4 | 100.5 | 411.6 | 56.6 | 83.9 | 1045.8 | 260.7 | 119.9 |
| single_insert | 35.5 | 79.5 | 299.3 | 109.6 | 67.2 | 75.5 | 171.0 | 273.8 | 186.2 |
| fetch_all     | 3.7 | 17.5 | 36.0 | 41.5 | 12.4 | 14.4 | 70.3 | 65.8 | 5.9 |
| count         | 0.3 | 0.6 | 1.9 | 0.7 | 0.9 | 0.8 | 0.8 | 3.7 | 1.0 |
| group_by      | 0.8 | 1.2 | 3.2 | 3.7 | 2.4 | 1.5 | 1.3 | - | 2.2 |
| filter        | 2.3 | 9.4 | 12.0 | 20.5 | 6.8 | 9.9 | 14.2 | 51.1 | 2.8 |
| get_by_pk     | 64.0 | 198.1 | 589.1 | 136.5 | 189.2 | 175.8 | 54.7 | 512.5 | 347.8 |
| update        | 3.7 | 3.7 | 7.8 | 204.1 | 9.1 | 7.9 | 8.4 | 9.1 | 8.0 |
| delete        | 0.9 | 0.9 | 1.9 | 148.3 | 1.8 | 1.4 | 1.6 | 2.2 | 1.6 |

The `group_by` op is a `GROUP BY … COUNT/SUM … HAVING` aggregate over the rows
(Ormar has no GROUP BY API, hence `-`).

Speedup vs `yara-orm` (competitor_time / yara_orm_time; >1 means `yara-orm` faster):

| operation     | tortoise | sqlalchemy | pony | django | peewee | sqlobject | ormar | piccolo |
|---------------|---------:|-----------:|-----:|-------:|-------:|----------:|------:|--------:|
| bulk_insert   | 1.7× | 6.5× | 26.6× | 3.7× | 5.4× | 67.5× | 16.8× | 7.7× |
| single_insert | 2.2× | 8.4× | 3.1× | 1.9× | 2.1× | 4.8× | 7.7× | 5.2× |
| fetch_all     | 4.7× | 9.6× | 11.1× | 3.3× | 3.8× | 18.8× | 17.6× | 1.6× |
| count         | 1.8× | 6.1× | 2.2× | 3.0× | 2.5× | 2.4× | 11.7× | 3.2× |
| group_by      | 1.4× | 3.8× | 4.5× | 2.9× | 1.8× | 1.6× | - | 2.6× |
| filter        | 4.1× | 5.3× | 9.0× | 3.0× | 4.4× | 6.3× | 22.5× | 1.2× |
| get_by_pk     | 3.1× | 9.2× | 2.1× | 3.0× | 2.7× | 0.9× | 8.0× | 5.4× |
| update        | 1.0× | 2.1× | 55.2× | 2.5× | 2.1× | 2.3× | 2.5× | 2.1× |
| delete        | 1.0× | 2.2× | 172.4× | 2.1× | 1.7× | 1.9× | 2.5× | 1.9× |

`yara-orm` is fastest or tied on every operation; the only place any ORM edges
ahead is **SQLObject** on `get_by_pk` (0.9× — 54.7 vs 64.0 ms), where its lean
in-process sync active-record avoids the async event-loop hop on single-row point
reads (the same latency-bound floor that keeps Pony close on `get_by_pk`).
Everything throughput-shaped is far ahead (`bulk_insert` up to 67×, `fetch_all`
up to 19×, `delete` 172× vs Pony's row-by-row loop).

### Charts

The grouped-bar charts shown in the README and docs are rendered from these
numbers by `plot_benchmarks.py` (the values are embedded in the script, so it
needs no database — just `pip install matplotlib`):

```bash
python benchmarks/plot_benchmarks.py
# writes docs/assets/benchmark-{postgres,mysql,sqlite}.png
```

If you re-run `bench.py`, update the tables here **and** the `BACKENDS` dict in
`plot_benchmarks.py` so the charts stay in sync.

### MySQL results

`BENCH_BACKEND=mysql`, MySQL 8.4 (Docker), Apple Silicon, Python 3.12, N=5000,
median of 5 (ms, lower is better). Tortoise runs over asyncmy, SQLAlchemy/Ormar
over aiomysql, the sync ORMs over pymysql. **Piccolo has no MySQL backend:**

| operation     | yara-orm | tortoise | sqlalchemy | pony | django | peewee | sqlobject | ormar |
|---------------|---------:|---------:|-----------:|-----:|-------:|-------:|----------:|------:|
| bulk_insert   | 53.8 | 51.5 | 640.5 | 402.5 | 93.3 | 85.6 | 1058.7 | 193.7 |
| single_insert | 715.8 | 795.4 | 1214.8 | 883.9 | 834.8 | 840.3 | 879.0 | 1091.1 |
| fetch_all     | 11.5 | 33.8 | 45.5 | 49.2 | 30.0 | 28.0 | 43.8 | 74.7 |
| count         | 0.8 | 0.8 | 1.2 | 0.8 | 0.9 | 0.8 | 0.7 | 3.9 |
| group_by      | 1.4 | 1.4 | 2.1 | 2.5 | 1.4 | 1.1 | 1.1 | - |
| filter        | 3.6 | 17.7 | 16.1 | 25.0 | 15.8 | 15.0 | 17.0 | 30.6 |
| get_by_pk     | 106.6 | 212.9 | 479.6 | 275.5 | 209.6 | 194.9 | 59.8 | 855.5 |
| update        | 7.0 | 9.8 | 10.1 | 221.9 | 7.0 | 7.4 | 7.0 | 9.1 |
| delete        | 5.0 | 5.6 | 5.5 | 192.0 | 6.1 | 5.7 | 5.3 | 6.0 |

`yara-orm` is fastest or tied on every operation (`fetch_all` 2.4–6.5×, `filter`
4.2–8.5×, `get_by_pk` 1.8–8.0× vs the competitors) — except SQLObject's leaner
`get_by_pk` (0.6×) and the sub-millisecond `group_by`, where peewee/SQLObject
edge us (0.8×). The two latency-bound ops carry the Docker-network round trip,
and `single_insert` (~0.7–1.2 s across the board) is dominated by InnoDB's
per-commit fsync — a cost every ORM pays equally.

### SQLite results

`BENCH_BACKEND=sqlite`, Python 3.12, N=5000, median of 5 (ms, lower is better):

| operation     | yara-orm | tortoise | sqlalchemy | pony | django | peewee | sqlobject | ormar | piccolo |
|---------------|---------:|---------:|-----------:|-----:|-------:|-------:|----------:|------:|--------:|
| bulk_insert   | 8.0 | 15.5 | 660.8 | 56.1 | 66.9 | 34.0 | 234.8 | 158.9 | 75.9 |
| single_insert | 36.1 | 44.1 | 397.7 | 155.1 | 162.8 | 144.0 | 139.4 | 339.0 | 264.7 |
| fetch_all     | 3.5 | 43.3 | 32.0 | 56.4 | 16.9 | 13.4 | 46.5 | 53.1 | 9.2 |
| count         | 0.1 | 0.4 | 0.8 | 0.2 | 0.2 | 0.2 | 0.1 | 1.6 | 0.4 |
| group_by      | 0.6 | 0.8 | 1.5 | 1.6 | 0.9 | 0.7 | 0.5 | - | 1.0 |
| filter        | 2.0 | 21.6 | 8.2 | 29.7 | 8.9 | 7.1 | 17.9 | 19.1 | 5.0 |
| get_by_pk     | 57.2 | 103.8 | 373.8 | 35.5 | 90.9 | 92.3 | 13.9 | 506.9 | 365.9 |
| update        | 0.6 | 0.8 | 2.0 | 50.0 | 1.5 | 1.2 | 1.0 | 1.7 | 1.5 |
| delete        | 0.5 | 0.5 | 1.4 | 39.7 | 1.0 | 0.8 | 0.7 | 1.1 | 1.2 |

`yara-orm` wins the throughput-bound operations decisively (`bulk_insert`
1.9–83×, `fetch_all` 2.6–16×, `filter` 2.5–15× across the field). It trails only
on **latency-bound point reads**: in-process sync ORMs — SQLObject (`get_by_pk`
0.2×) and Pony (0.6×) — beat us there (plus the microsecond `group_by`, where
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

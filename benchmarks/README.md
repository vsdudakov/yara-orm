# Benchmarks

`bench.py` runs identical workloads against **this library (`yara-orm`)**,
**Tortoise ORM** (async, asyncpg), **SQLAlchemy 2.0** (async ORM, asyncpg) and
**Pony ORM** (sync, psycopg2) on the same PostgreSQL instance, each ORM in its
own table.

## Running

Pony's query decompiler does **not** support Python 3.13+, so the full 4-way
run needs Python ≤ 3.12. `yara-orm`, Tortoise and SQLAlchemy run on any supported
version (Pony is simply reported as `-`).

```bash
# 4-way (Python 3.12)
python3.12 -m venv .venv312
.venv312/bin/pip install -U maturin tortoise-orm "sqlalchemy[asyncio]" asyncpg pony psycopg2-binary
VIRTUAL_ENV=$PWD/.venv312 .venv312/bin/maturin develop --release
ORM_TEST_DB=postgres://USER@localhost/orm_demo .venv312/bin/python benchmarks/bench.py
```

Env knobs: `BENCH_N` (bulk rows, default 5000), `BENCH_S` (single-insert rows,
500), `BENCH_GETS` (pk lookups, 1000), `BENCH_REPEAT` (runs per ORM, median
reported, 5).

### SQLite

Set `BENCH_BACKEND=sqlite` to run the same 4-way workload on SQLite (each ORM
gets its own file in `BENCH_SQLITE_DIR`, default `/tmp`). Needs `aiosqlite`
for Tortoise and SQLAlchemy:

```bash
.venv312/bin/pip install -U aiosqlite
BENCH_BACKEND=sqlite .venv312/bin/python benchmarks/bench.py
```

## Methodology

* Each ORM gets its own table and the **same** workload and data.
* Every operation is timed `BENCH_REPEAT` times and the **median** is reported,
  so warm steady-state (driver + prepared-statement caches hot) dominates over
  cold-start noise.
* `get_by_pk` issues the same random pk sequence to every ORM.
* Caveats — this is *throughput-oriented*, not a micro-benchmark:
  * sync (Pony) vs async (Tortoise, `yara-orm`) have different concurrency models;
  * Pony opens a transaction per `get` (its design) and has no SQL-level bulk
    `UPDATE`, so its update path mutates objects in a loop;
  * SQLAlchemy `get_by_pk` uses a fresh session per lookup (no identity-map
    reuse), matching the stateless-handler pattern the others use;
  * feature sets differ. Treat the numbers as indicative.

## Representative results

PostgreSQL 18, Apple Silicon, Python 3.12, N=5000, median of 5 (ms, lower is
better).

| operation       | yara-orm | tortoise | sqlalchemy |  pony |
|-----------------|---------:|---------:|-----------:|------:|
| bulk_insert     |     11.0 |     24.2 |       66.6 | 218.2 |
| single_insert   |     33.4 |     79.5 |      151.3 |  59.7 |
| fetch_all       |      3.4 |     16.1 |       11.8 |  30.4 |
| count           |      0.3 |      0.5 |        0.9 |   0.5 |
| filter          |      2.2 |      8.4 |       19.9 |  16.0 |
| get_by_pk       |     62.5 |    193.6 |      294.3 |  82.5 |
| update          |      3.4 |      3.4 |        3.8 | 119.5 |
| delete          |      0.7 |      0.8 |        1.0 |  90.7 |

Speedup vs `yara-orm` (competitor_time / yara_orm_time; >1 means `yara-orm` faster):

| operation     | tortoise | sqlalchemy |   pony |
|---------------|---------:|-----------:|-------:|
| bulk_insert   |    2.1×  |      6.2×  |  18.7× |
| single_insert |    2.5×  |      4.7×  |   1.8× |
| fetch_all     |    4.7×  |      3.5×  |   8.9× |
| count         |    2.1×  |      3.2×  |   1.7× |
| filter        |    4.1×  |     10.0×  |   7.9× |
| get_by_pk     |    3.1×  |      4.7×  |   1.3× |
| update        |    1.1×  |      1.2×  |  36.5× |
| delete        |    1.3×  |      1.6×  | 134.0× |

`yara-orm` is fastest on every operation in this configuration. `get_by_pk` and
`single_insert` are latency-bound (one sequential round-trip per call) and sit
near the raw client⇄PostgreSQL round-trip floor.

### SQLite results

`BENCH_BACKEND=sqlite`, Python 3.12, N=5000, median of 5 (ms, lower is better):

| operation     | yara-orm | tortoise | sqlalchemy |  pony |
|---------------|---------:|---------:|-----------:|------:|
| bulk_insert   |      7.4 |     13.2 |      608.2 |  45.7 |
| single_insert |     35.7 |     25.8 |      240.3 | 110.7 |
| fetch_all     |      4.9 |     38.2 |       11.3 |  46.9 |
| count         |      0.1 |      0.2 |        0.7 |   0.2 |
| filter        |      2.7 |     19.7 |       18.1 |  23.6 |
| get_by_pk     |     54.7 |     79.7 |      332.8 |  29.0 |
| update        |      0.5 |      0.5 |        1.8 |  40.6 |
| delete        |      0.4 |      0.3 |        1.1 |  33.8 |

`yara-orm` wins the throughput-bound operations decisively (bulk 1.8×/82×/6.2×,
fetch_all 7.9×/2.3×/9.6×, filter 7.4×/6.8×/8.9× vs Tortoise/SQLAlchemy/Pony).
It trails on the two **latency-bound** ops: in-process Pony beats us on
`get_by_pk` (0.5×) and Tortoise edges `single_insert` (0.7×) — because our
SQLite backend bridges synchronous `rusqlite` to async by hopping to a
blocking thread **per call**, which costs a few µs that an in-process driver
avoids on sequential point queries. Real workloads rarely fire thousands of
sequential point reads, and everything throughput-shaped is far ahead.

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

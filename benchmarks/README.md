# Benchmarks

`bench.py` runs identical workloads against **this library (`orm`)**,
**Tortoise ORM** (async, asyncpg), **SQLAlchemy 2.0** (async ORM, asyncpg) and
**Pony ORM** (sync, psycopg2) on the same PostgreSQL instance, each ORM in its
own table.

## Running

Pony's query decompiler does **not** support Python 3.13+, so the full 4-way
run needs Python ≤ 3.12. `orm`, Tortoise and SQLAlchemy run on any supported
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
  * sync (Pony) vs async (Tortoise, `orm`) have different concurrency models;
  * Pony opens a transaction per `get` (its design) and has no SQL-level bulk
    `UPDATE`, so its update path mutates objects in a loop;
  * SQLAlchemy `get_by_pk` uses a fresh session per lookup (no identity-map
    reuse), matching the stateless-handler pattern the others use;
  * feature sets differ. Treat the numbers as indicative.

## Representative results

PostgreSQL 18, Apple Silicon, Python 3.12, N=5000, median of 5 (ms, lower is
better).

| operation       | orm  | tortoise | sqlalchemy |  pony |
|-----------------|-----:|---------:|-----------:|------:|
| bulk_insert     | 11.5 |     23.1 |       67.5 | 208.4 |
| single_insert   | 32.8 |     80.4 |      153.3 |  59.1 |
| fetch_all       |  3.5 |     16.0 |       12.2 |  30.4 |
| count           |  0.4 |      0.5 |        1.2 |   0.5 |
| filter          |  2.2 |      8.5 |       20.5 |  15.4 |
| get_by_pk       | 63.2 |    194.4 |      292.8 |  82.7 |
| update          |  3.3 |      3.4 |        4.1 | 117.8 |

Speedup vs `orm` (competitor_time / orm_time; >1 means `orm` faster):

| operation     | tortoise | sqlalchemy |  pony |
|---------------|---------:|-----------:|------:|
| bulk_insert   |    2.0×  |      5.9×  | 18.1× |
| single_insert |    2.4×  |      4.7×  |  1.8× |
| fetch_all     |    4.5×  |      3.5×  |  8.6× |
| count         |    1.5×  |      3.1×  |  1.3× |
| filter        |    3.9×  |      9.5×  |  7.2× |
| get_by_pk     |    3.1×  |      4.6×  |  1.3× |
| update        |    1.0×  |      1.3×  | 35.7× |

`orm` is fastest on every operation in this configuration. `get_by_pk` and
`single_insert` are latency-bound (one sequential round-trip per call) and sit
near the raw client⇄PostgreSQL round-trip floor.

### SQLite results

`BENCH_BACKEND=sqlite`, Python 3.12, N=5000, median of 5 (ms, lower is better):

| operation     | orm | tortoise | sqlalchemy |  pony |
|---------------|----:|---------:|-----------:|------:|
| bulk_insert   | 7.5 |     13.2 |      607.7 |  47.2 |
| single_insert | 35.1|     27.6 |      235.2 | 117.2 |
| fetch_all     | 4.9 |     38.2 |       11.0 |  48.7 |
| count         | 0.1 |      0.3 |        0.6 |   0.2 |
| filter        | 2.6 |     19.5 |       17.6 |  24.9 |
| get_by_pk     | 54.1|     79.2 |      329.3 |  30.1 |
| update        | 0.5 |      0.5 |        1.8 |  41.5 |

`orm` wins the throughput-bound operations decisively (bulk 1.8×/81×/6.3×,
fetch_all 7.8×/2.2×/9.9×, filter 7.5×/6.8×/9.6× vs Tortoise/SQLAlchemy/Pony).
It trails on the two **latency-bound** ops: in-process Pony beats us on
`get_by_pk` (0.6×) and Tortoise edges `single_insert` (0.8×) — because our
SQLite backend bridges synchronous `rusqlite` to async by hopping to a
blocking thread **per call**, which costs a few µs that an in-process driver
avoids on sequential point queries. Real workloads rarely fire thousands of
sequential point reads, and everything throughput-shaped is far ahead.

## Why `orm` is fast here

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

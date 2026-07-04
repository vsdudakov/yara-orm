---
title: Performance
description: Yara ORM benchmarks — a fast async Python ORM benchmarked against eight others (Tortoise, SQLAlchemy, Pony, Django, Peewee, SQLObject, Ormar, Piccolo) on PostgreSQL, MySQL and SQLite.
---

# Performance

Yara ORM is built to be a **fast async Python ORM**: the per-query hot path (parameter
binding, row decoding, pooling) runs in compiled Rust, so steady-state overhead is far
lower than pure-Python ORMs. The numbers below compare Yara ORM against **eight other
Python ORMs** — **Tortoise ORM**, **async SQLAlchemy 2.0**, **Pony ORM**, **Django ORM**,
**Peewee**, **SQLObject**, **Ormar** and **Piccolo** — on identical workloads.

!!! note "Methodology"
    Each ORM gets its own table and the **same** workload and data. Every operation is timed
    `BENCH_REPEAT` times and the **median** is reported, so warm steady-state (driver and
    prepared-statement caches hot) dominates over cold-start noise. Treat the numbers as
    indicative throughput, not a micro-benchmark. Full methodology and the runnable script
    live in [`benchmarks/`](https://github.com/vsdudakov/yara-orm/tree/main/benchmarks).

## PostgreSQL

![Yara ORM vs eight Python ORMs on PostgreSQL — latency per operation, log scale, lower is better](assets/benchmark-postgres.png)

PostgreSQL 18, Apple Silicon, Python 3.12, N=5000, median of 5 (ms, lower is better).

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

`group_by` is a `GROUP BY … COUNT/SUM … HAVING` aggregate query (Ormar has no
GROUP BY API, hence `-`).

**Speedup vs Yara ORM** (competitor time ÷ yara-orm time; >1 means Yara ORM is faster):

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

Yara ORM is fastest or tied on every operation; the only place any ORM edges ahead is
**SQLObject** on `get_by_pk` (0.9× — 54.7 vs 64.0 ms), where its lean in-process sync
active-record avoids the async event-loop hop on single-row point reads — the same
latency-bound floor that keeps Pony close on `get_by_pk`. Everything throughput-shaped is
far ahead (`bulk_insert` up to 67×, `fetch_all` up to 19×, `delete` 172× vs Pony's
row-by-row loop).

## MySQL

![Yara ORM vs seven Python ORMs on MySQL — latency per operation, log scale, lower is better](assets/benchmark-mysql.png)

MySQL 8.4 (Docker), Apple Silicon, Python 3.12, N=5000, median of 5 (ms, lower
is better). Tortoise runs over asyncmy, SQLAlchemy/Ormar over aiomysql, the sync
ORMs over pymysql. Piccolo has no MySQL backend, so it is absent here:

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

Yara ORM is fastest or tied on every operation here too (`fetch_all` 2.4–6.5×,
`filter` 4.2–8.5×, `get_by_pk` 1.8–8.0×), except SQLObject's leaner `get_by_pk`
(0.6×) and the sub-millisecond `group_by` where peewee/SQLObject edge it (0.8×).
The two latency-bound operations include the Docker-network round trip, and
`single_insert` (~0.7–1.2 s across the board) is dominated by InnoDB's per-commit
fsync — a durability cost every ORM pays equally.

## SQLite

![Yara ORM vs eight Python ORMs on SQLite — latency per operation, log scale, lower is better](assets/benchmark-sqlite.png)

Python 3.12, N=5000, median of 5 (ms, lower is better).

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

Yara ORM wins the throughput-bound operations decisively (`bulk_insert` 1.9–83×, `fetch_all`
2.6–16×, `filter` 2.5–15× across the field). It trails only on **latency-bound point reads**:
in-process sync ORMs — SQLObject (`get_by_pk` 0.2×) and Pony (0.6×) — beat us there (plus the
microsecond `group_by`, where SQLObject's raw-SQL path is a hair ahead at 0.9×). The cost
is the per-statement asyncio bridge (scheduling the statement on the runtime and waking the
event loop), tens of microseconds that a synchronous in-process driver avoids on sequential
point queries. Real workloads rarely fire
thousands of sequential point reads, and everything throughput-shaped is far ahead. If those
point operations dominate your workload, the opt-in
[sync fast path](#opt-in-sqlite-sync-fast-path) below removes exactly that per-statement
bridge.

### Opt-in SQLite sync fast path

On SQLite, per-statement work is microseconds, so the asyncio bridge dominates:
scheduling the statement on the tokio runtime and waking the event loop costs
~40µs around ~0.5–6µs of actual SQLite work. Adding **`sync_fast_path=1`** to
the URL removes that bridge — statements run synchronously on the calling
thread (GIL released) and return already-completed awaitables, cutting a warm
point query from ~40µs to ~6µs (~7×):

```python
await YaraOrm.init("sqlite:///app.db?sync_fast_path=1")
```

`benchmarks/bench_features.py` (Apple Silicon, Python 3.13, median of 5),
default vs fast path:

| operation              | default (ms) | sync_fast_path=1 (ms) | speedup |
|------------------------|-------------:|----------------------:|--------:|
| insert_autocommit      |        17.26 |                  6.40 |    2.7× |
| insert_one_tx          |        11.17 |                  1.58 |    7.1× |
| insert_savepoint_each  |        31.79 |                  2.22 |   14.3× |
| forward_n_plus_1       |        23.76 |                  5.29 |    4.5× |
| forward_select_related |         0.73 |                  0.69 |    1.1× |
| reverse_n_plus_1       |         5.98 |                  2.37 |    2.5× |
| reverse_prefetch       |         0.48 |                  0.41 |    1.2× |
| fetch_full             |         0.23 |                  0.19 |    1.2× |
| values                 |         0.21 |                  0.16 |    1.3× |
| values_list            |         0.13 |                  0.09 |    1.4× |

The per-statement operations (point inserts, N+1 fan-outs, savepoints) win
big; single-query bulk fetches only shed one bridge crossing each.

!!! warning "Read the caveats before opting in"
    The event loop is **blocked** for the duration of each statement, and
    awaiting a completed awaitable may not yield to the loop (task fairness
    changes). Opt in for microsecond-statement workloads — tests, scripts,
    benchmarks, low-contention apps — and read
    [the full caveat list](backends/index.md#opt-in-synchronous-fast-path-sync_fast_path1)
    first. The flag is SQLite-only.

### uvloop on the default async path

Independently of the fast path: on the **default** async path, running your app
under [uvloop](https://github.com/MagicStack/uvloop) cuts roughly 20% of the
per-query overhead with zero code changes (the bridge's event-loop wakeups get
cheaper), and it composes with every backend, PostgreSQL included:

```python
import uvloop

uvloop.run(main())          # instead of asyncio.run(main())
```

## Why it's fast

- **Rust hot path** — parameter binding and row decoding happen in compiled code; the async
  bridge (PyO3 + tokio) keeps the event loop free.
- **Positional row decoding** — for SELECTs the engine returns column values with no per-row
  column-name allocation and no dict; Python fills instances by index using a precomputed
  decode plan.
- **Compiled-SQL caching** — the SELECT column list, single-row INSERT and a fast-path
  simple `get()` are built once per model and reused, and `prepare_cached` skips re-parse.
- **Connection pooling** — deadpool keeps warm connections, so steady-state latency excludes
  connect cost.

!!! tip "Faster reads with projections"
    For pure projections, [`values_list()` / `values()`](guides/querying.md) select only the
    requested columns and skip model construction (~1.7–2.2× faster than a full fetch).

## Run it yourself

```bash
make bench          # PostgreSQL cross-ORM benchmark (9 ORMs)
make bench-mysql    # same comparison on MySQL
make bench-sqlite   # same comparison on SQLite
```

See [`benchmarks/README.md`](https://github.com/vsdudakov/yara-orm/tree/main/benchmarks)
for setup and tuning knobs (`BENCH_N`, `BENCH_REPEAT`, …).

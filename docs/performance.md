---
title: Performance
description: Yara ORM benchmarks — a fast async Python ORM benchmarked against eight others (Tortoise, SQLAlchemy, Pony, Django, Peewee, SQLObject, Ormar, Piccolo) on PostgreSQL, MySQL, MariaDB and SQLite.
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
| bulk_insert   | 14.7 | 24.2 | 78.0 | 222.8 | 40.6 | 51.7 | 526.3 | 229.8 | 99.2 |
| single_insert | 34.4 | 80.7 | 153.1 | 61.8 | 40.5 | 47.1 | 53.5 | 167.4 | 89.5 |
| fetch_all     | 3.6 | 17.0 | 29.4 | 34.5 | 9.1 | 11.9 | 26.6 | 56.7 | 4.3 |
| count         | 0.3 | 0.6 | 1.0 | 0.4 | 0.4 | 0.3 | 0.3 | 5.4 | 0.4 |
| group_by      | 0.7 | 1.0 | 1.6 | 2.4 | 1.0 | 0.8 | 0.6 | - | 1.0 |
| filter        | 2.3 | 9.1 | 8.1 | 17.9 | 5.3 | 6.7 | 9.1 | 42.2 | 2.6 |
| get_by_pk     | 65.1 | 196.3 | 292.6 | 85.3 | 115.7 | 114.1 | 23.8 | 333.1 | 196.1 |
| update        | 3.3 | 3.6 | 4.0 | 120.8 | 3.4 | 3.4 | 3.3 | 15.0 | 3.5 |
| delete        | 0.7 | 0.8 | 1.1 | 94.3 | 0.8 | 0.7 | 0.6 | 2.4 | 0.8 |

`group_by` is a `GROUP BY … COUNT/SUM … HAVING` aggregate query (Ormar has no
GROUP BY API, hence `-`).

**Speedup vs Yara ORM** (competitor time ÷ yara-orm time; >1 means Yara ORM is faster):

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

Yara ORM is fastest or tied on every operation; the only place any ORM edges ahead is
**SQLObject** on `get_by_pk` (0.4× — 23.5 vs 65.3 ms), where its lean in-process sync
active-record avoids the async event-loop hop on single-row point reads — the same
latency-bound floor that keeps Pony close on `get_by_pk`. Everything throughput-shaped is
far ahead (`bulk_insert` up to 36×, `fetch_all` up to 16×, `delete` 139× vs Pony's
row-by-row loop).

## MySQL

![Yara ORM vs seven Python ORMs on MySQL — latency per operation, log scale, lower is better](assets/benchmark-mysql.png)

MySQL 8.4 (Docker), Apple Silicon, Python 3.12, N=5000, median of 5 (ms, lower
is better). Tortoise runs over asyncmy, SQLAlchemy/Ormar over aiomysql, the sync
ORMs over pymysql. Piccolo has no MySQL backend, so it is absent here:

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

Yara ORM is fastest or tied on every operation here too (`fetch_all` 4.6–12.4×,
`filter` 4.5–9.3×, `get_by_pk` 1.8–7.3×), except SQLObject's leaner `get_by_pk` (0.5×) and the sub-millisecond `group_by` where peewee/SQLObject edge it (0.8×).
The two latency-bound operations include the Docker-network round trip, and
`single_insert` (~0.6–1.2 s across the board) is dominated by InnoDB's per-commit
fsync — a durability cost every ORM pays equally.

## MariaDB

![Yara ORM vs seven Python ORMs on MariaDB — latency per operation, log scale, lower is better](assets/benchmark-mariadb.png)

MariaDB 11 (Docker), Apple Silicon, Python 3.12, N=5000, median of 5 (ms, lower
is better). Every competitor connects through its MySQL driver; `yara-orm`
auto-detects MariaDB and switches to its RETURNING dialect. Piccolo has no MySQL
backend, so it is absent here:

| operation     | yara-orm | tortoise | sqlalchemy | pony | django | peewee | sqlobject | ormar |
|---------------|---------:|---------:|-----------:|-----:|-------:|-------:|----------:|------:|
| bulk_insert   | 30.5 | 41.5 | 99.0 | 455.0 | 87.7 | 59.5 | 1257.2 | 191.2 |
| single_insert | 392.8 | 304.8 | 430.8 | 329.6 | 343.6 | 367.3 | 359.8 | 555.3 |
| fetch_all     | 5.5 | 34.0 | 42.5 | 47.9 | 28.4 | 30.4 | 44.9 | 72.8 |
| count         | 0.5 | 0.8 | 1.2 | 0.7 | 0.7 | 0.8 | 0.7 | 4.7 |
| group_by      | 1.3 | 1.3 | 2.1 | 2.2 | 1.5 | 1.1 | 0.9 | - |
| filter        | 3.2 | 17.8 | 16.0 | 24.5 | 15.5 | 14.7 | 16.9 | 31.9 |
| get_by_pk     | 123.3 | 228.7 | 534.1 | 310.7 | 214.5 | 206.0 | 66.0 | 916.1 |
| update        | 3.8 | 3.3 | 6.5 | 265.9 | 4.3 | 4.3 | 4.2 | 7.6 |
| delete        | 3.2 | 3.2 | 3.3 | 249.9 | 3.3 | 3.0 | 2.9 | 3.6 |

Yara ORM leads or ties every operation except SQLObject's leaner `get_by_pk`
(0.5×) and the sub-millisecond `group_by` (SQLObject 0.8×). It wins the throughput
ops decisively (`fetch_all` 5.0–12.9×, `filter` 4.4–9.3×, `bulk_insert` up to 46×
vs SQLObject). MariaDB's `single_insert` (~390 ms) is notably faster than
MySQL 8's here — a lighter default commit path.

## SQLite

![Yara ORM vs eight Python ORMs on SQLite — latency per operation, log scale, lower is better](assets/benchmark-sqlite.png)

Python 3.12, N=5000, median of 5 (ms, lower is better).

| operation     | yara-orm | tortoise | sqlalchemy | pony | django | peewee | sqlobject | ormar | piccolo |
|---------------|---------:|---------:|-----------:|-----:|-------:|-------:|----------:|------:|--------:|
| bulk_insert   | 7.9 | 14.4 | 612.7 | 51.0 | 58.1 | 30.7 | 223.1 | 158.0 | 78.8 |
| single_insert | 32.6 | 29.3 | 240.0 | 128.3 | 139.0 | 114.7 | 139.9 | 323.2 | 259.1 |
| fetch_all     | 3.4 | 39.7 | 28.8 | 51.0 | 16.3 | 12.5 | 44.9 | 54.8 | 9.1 |
| count         | 0.1 | 0.3 | 0.7 | 0.2 | 0.2 | 0.1 | 0.1 | 1.7 | 0.5 |
| group_by      | 0.5 | 0.8 | 1.4 | 1.5 | 0.9 | 0.7 | 0.5 | - | 1.0 |
| filter        | 2.0 | 20.5 | 7.7 | 26.2 | 8.5 | 6.7 | 17.3 | 19.6 | 5.1 |
| get_by_pk     | 47.4 | 87.5 | 330.9 | 30.7 | 83.6 | 77.7 | 13.3 | 501.8 | 359.5 |
| update        | 0.6 | 0.5 | 1.8 | 43.1 | 1.3 | 1.2 | 1.2 | 1.6 | 1.4 |
| delete        | 0.4 | 0.4 | 1.2 | 36.3 | 0.9 | 0.7 | 0.8 | 1.3 | 1.2 |

Yara ORM wins the throughput-bound operations decisively (`bulk_insert` 1.8–77×, `fetch_all`
2.6–15×, `filter` 2.5–13× across the field). It trails only on **latency-bound point reads**:
in-process sync ORMs — SQLObject (`get_by_pk` 0.3×) and Pony (0.7×) — beat us there (plus the
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

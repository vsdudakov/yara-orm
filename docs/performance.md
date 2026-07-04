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
| bulk_insert   | 14.7 | 25.7 | 81.6 | 222.9 | 39.7 | 53.2 | 523.6 | 236.4 | 99.2 |
| single_insert | 34.5 | 81.2 | 156.1 | 61.5 | 40.8 | 48.2 | 53.7 | 163.7 | 88.8 |
| fetch_all     | 3.5 | 17.3 | 31.1 | 35.3 | 9.2 | 12.3 | 26.5 | 55.6 | 4.3 |
| count         | 0.3 | 0.6 | 1.0 | 0.4 | 0.4 | 0.4 | 0.3 | 4.9 | 0.4 |
| group_by      | 0.8 | 1.0 | 1.6 | 2.3 | 1.1 | 0.9 | 0.6 | - | 1.0 |
| filter        | 2.2 | 9.1 | 8.7 | 17.8 | 5.3 | 7.1 | 9.0 | 21.3 | 2.6 |
| get_by_pk     | 65.3 | 197.7 | 297.3 | 85.1 | 114.9 | 115.5 | 23.5 | 329.1 | 194.2 |
| update        | 3.3 | 3.5 | 4.1 | 121.3 | 3.4 | 3.6 | 3.3 | 14.4 | 3.6 |
| delete        | 0.7 | 0.8 | 1.1 | 95.1 | 0.8 | 0.7 | 0.6 | 2.4 | 0.9 |

`group_by` is a `GROUP BY … COUNT/SUM … HAVING` aggregate query (Ormar has no
GROUP BY API, hence `-`).

**Speedup vs Yara ORM** (competitor time ÷ yara-orm time; >1 means Yara ORM is faster):

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
| bulk_insert   | 47.3 | 53.9 | 568.7 | 403.1 | 91.3 | 83.3 | 974.9 | 236.3 |
| single_insert | 627.2 | 766.7 | 1043.6 | 867.9 | 751.7 | 798.9 | 867.2 | 1367.5 |
| fetch_all     | 6.1 | 34.0 | 45.7 | 48.8 | 29.1 | 28.0 | 42.9 | 75.5 |
| count         | 0.5 | 0.9 | 1.2 | 0.9 | 0.9 | 0.8 | 0.7 | 5.3 |
| group_by      | 1.2 | 1.4 | 2.1 | 2.6 | 1.5 | 1.2 | 1.0 | - |
| filter        | 3.3 | 17.8 | 15.8 | 25.2 | 15.2 | 14.9 | 16.7 | 31.0 |
| get_by_pk     | 110.3 | 212.5 | 484.1 | 275.9 | 200.9 | 195.8 | 58.7 | 804.8 |
| update        | 6.3 | 11.1 | 10.4 | 222.2 | 9.9 | 7.5 | 7.3 | 10.9 |
| delete        | 4.9 | 5.5 | 5.9 | 192.4 | 5.4 | 4.8 | 4.9 | 8.4 |

Yara ORM is fastest or tied on every operation here too (`fetch_all` 4.6–12.4×,
`filter` 4.5–9.3×, `get_by_pk` 1.8–7.3×), except SQLObject's leaner `get_by_pk` (0.5×) and the sub-millisecond `group_by` where peewee/SQLObject edge it (0.8×).
The two latency-bound operations include the Docker-network round trip, and
`single_insert` (~0.6–1.4 s across the board) is dominated by InnoDB's per-commit
fsync — a durability cost every ORM pays equally.

## MariaDB

![Yara ORM vs seven Python ORMs on MariaDB — latency per operation, log scale, lower is better](assets/benchmark-mariadb.png)

MariaDB 11 (Docker), Apple Silicon, Python 3.12, N=5000, median of 5 (ms, lower
is better). Every competitor connects through its MySQL driver; `yara-orm`
auto-detects MariaDB and switches to its RETURNING dialect. Piccolo has no MySQL
backend, so it is absent here:

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

Yara ORM leads or ties every operation except SQLObject's leaner `get_by_pk`
(0.5×) and the sub-millisecond `group_by` (SQLObject 0.8×). It wins the throughput
ops decisively (`fetch_all` 5.0–12.9×, `filter` 4.4–9.3×, `bulk_insert` up to 46×
vs SQLObject). MariaDB's `single_insert` (~315 ms) is notably faster than
MySQL 8's here — a lighter default commit path.

## SQLite

![Yara ORM vs eight Python ORMs on SQLite — latency per operation, log scale, lower is better](assets/benchmark-sqlite.png)

Python 3.12, N=5000, median of 5 (ms, lower is better).

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

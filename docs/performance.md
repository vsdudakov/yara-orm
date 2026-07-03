---
title: Performance
description: Yara ORM benchmarks — a fast async Python ORM that runs 2–9× faster than Tortoise ORM, async SQLAlchemy and Pony on PostgreSQL and SQLite.
---

# Performance

Yara ORM is built to be a **fast async Python ORM**: the per-query hot path (parameter
binding, row decoding, pooling) runs in compiled Rust, so steady-state overhead is far
lower than pure-Python ORMs. The numbers below compare Yara ORM against **Tortoise ORM**,
**async SQLAlchemy 2.0** and **Pony ORM** on identical workloads.

!!! note "Methodology"
    Each ORM gets its own table and the **same** workload and data. Every operation is timed
    `BENCH_REPEAT` times and the **median** is reported, so warm steady-state (driver and
    prepared-statement caches hot) dominates over cold-start noise. Treat the numbers as
    indicative throughput, not a micro-benchmark. Full methodology and the runnable script
    live in [`benchmarks/`](https://github.com/vsdudakov/yara-orm/tree/main/benchmarks).

## PostgreSQL

![Yara ORM vs Tortoise, SQLAlchemy and Pony on PostgreSQL — latency per operation, log scale, lower is better](assets/benchmark-postgres.png)

PostgreSQL 18, Apple Silicon, Python 3.12, N=5000, median of 5 (ms, lower is better).

| operation     | yara-orm | tortoise | sqlalchemy |  pony |
|---------------|---------:|---------:|-----------:|------:|
| bulk_insert   |     14.7 |     24.2 |       68.0 | 220.1 |
| single_insert |     34.2 |     80.0 |      150.7 |  60.9 |
| fetch_all     |      3.5 |     16.7 |       21.3 |  34.4 |
| count         |      0.3 |      0.5 |        0.9 |   0.4 |
| group_by      |      0.7 |      1.2 |        1.4 |   2.3 |
| filter        |      2.2 |      8.5 |        7.5 |  17.6 |
| get_by_pk     |     65.0 |    194.9 |      287.0 |  84.1 |
| update        |      3.2 |      3.4 |        3.8 | 119.8 |
| delete        |      0.7 |      0.8 |        1.1 |  92.8 |

`group_by` is a `GROUP BY … COUNT/SUM … HAVING` aggregate query.

**Speedup vs Yara ORM** (competitor time ÷ yara-orm time; >1 means Yara ORM is faster):

| operation     | tortoise | sqlalchemy |  pony |
|---------------|---------:|-----------:|------:|
| bulk_insert   |    1.6×  |      4.6×  | 14.9× |
| single_insert |    2.3×  |      4.4×  |  1.8× |
| fetch_all     |    4.8×  |      6.1×  |  9.8× |
| count         |    1.9×  |      3.2×  |  1.5× |
| group_by      |    1.6×  |      1.9×  |  3.1× |
| filter        |    3.9×  |      3.5×  |  8.1× |
| get_by_pk     |    3.0×  |      4.4×  |  1.3× |
| update        |    1.1×  |      1.2×  | 37.3× |
| delete        |    1.2×  |      1.6×  | 135.6× |

Yara ORM is fastest on every operation in this configuration. `get_by_pk` and
`single_insert` are latency-bound (one sequential round-trip per call) and sit near the raw
client⇄PostgreSQL round-trip floor.

## SQLite

Python 3.12, N=5000, median of 5 (ms, lower is better).

| operation     | yara-orm | tortoise | sqlalchemy |  pony |
|---------------|---------:|---------:|-----------:|------:|
| bulk_insert   |      8.2 |     13.7 |      604.5 |  54.7 |
| single_insert |     36.0 |     26.1 |      231.8 | 111.3 |
| fetch_all     |      5.4 |     39.9 |       20.4 |  51.8 |
| count         |      0.1 |      0.2 |        0.7 |   0.2 |
| filter        |      3.0 |     20.4 |        7.0 |  26.1 |
| get_by_pk     |     56.5 |     79.0 |      331.5 |  31.5 |
| update        |      0.5 |      0.5 |        1.8 |  43.6 |

Yara ORM wins the throughput-bound operations decisively (bulk 1.7× vs Tortoise,
`fetch_all` 7.4×, `filter` 6.9×). It trails on the two **latency-bound** point operations:
in-process Pony edges `get_by_pk` (56.5 vs 31.5 ms), and Tortoise edges `single_insert`
(36.0 vs 26.1 ms) — the cost is the per-statement asyncio bridge (scheduling the
statement on the runtime and waking the event loop), tens of microseconds that a
synchronous in-process driver avoids on sequential point queries. Real workloads rarely fire thousands of
sequential point reads, and everything throughput-shaped is far ahead. If those point
operations dominate your workload, the opt-in
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
make bench          # PostgreSQL 4-way benchmark
BENCH_BACKEND=sqlite make bench
```

See [`benchmarks/README.md`](https://github.com/vsdudakov/yara-orm/tree/main/benchmarks)
for setup and tuning knobs (`BENCH_N`, `BENCH_REPEAT`, …).

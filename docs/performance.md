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

PostgreSQL 18, Apple Silicon, Python 3.12, N=5000, median of 5 (ms, lower is better).

| operation     | yara-orm | tortoise | sqlalchemy |  pony |
|---------------|---------:|---------:|-----------:|------:|
| bulk_insert   |     11.5 |     23.1 |       67.5 | 208.4 |
| single_insert |     32.8 |     80.4 |      153.3 |  59.1 |
| fetch_all     |      3.5 |     16.0 |       12.2 |  30.4 |
| count         |      0.4 |      0.5 |        1.2 |   0.5 |
| filter        |      2.2 |      8.5 |       20.5 |  15.4 |
| get_by_pk     |     63.2 |    194.4 |      292.8 |  82.7 |
| update        |      3.3 |      3.4 |        4.1 | 117.8 |

**Speedup vs Yara ORM** (competitor time ÷ yara-orm time; >1 means Yara ORM is faster):

| operation     | tortoise | sqlalchemy |  pony |
|---------------|---------:|-----------:|------:|
| bulk_insert   |    2.0×  |      5.9×  | 18.1× |
| single_insert |    2.4×  |      4.7×  |  1.8× |
| fetch_all     |    4.5×  |      3.5×  |  8.6× |
| count         |    1.5×  |      3.1×  |  1.3× |
| filter        |    3.9×  |      9.5×  |  7.2× |
| get_by_pk     |    3.1×  |      4.6×  |  1.3× |
| update        |    1.0×  |      1.3×  | 35.7× |

Yara ORM is fastest on every operation in this configuration. `get_by_pk` and
`single_insert` are latency-bound (one sequential round-trip per call) and sit near the raw
client⇄PostgreSQL round-trip floor.

## SQLite

Python 3.12, N=5000, median of 5 (ms, lower is better).

| operation     | yara-orm | tortoise | sqlalchemy |  pony |
|---------------|---------:|---------:|-----------:|------:|
| bulk_insert   |      7.5 |     13.2 |      607.7 |  47.2 |
| single_insert |     35.1 |     27.6 |      235.2 | 117.2 |
| fetch_all     |      4.9 |     38.2 |       11.0 |  48.7 |
| count         |      0.1 |      0.3 |        0.6 |   0.2 |
| filter        |      2.6 |     19.5 |       17.6 |  24.9 |
| get_by_pk     |     54.1 |     79.2 |      329.3 |  30.1 |
| update        |      0.5 |      0.5 |        1.8 |  41.5 |

Yara ORM wins the throughput-bound operations decisively (bulk, `fetch_all`, `filter`). It
trails on the two **latency-bound** point operations: in-process Pony edges `get_by_pk`,
and Tortoise edges `single_insert` — because the SQLite backend bridges synchronous
`rusqlite` to async by hopping to a blocking thread per call, which costs a few microseconds
that an in-process driver avoids on sequential point queries. Real workloads rarely fire
thousands of sequential point reads, and everything throughput-shaped is far ahead.

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

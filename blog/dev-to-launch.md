---
title: "I built a Python ORM with a Rust engine — here's how the GIL, PyO3, and asyncio actually cooperate"
published: true
description: "A Rust database engine and Python's asyncio loop have to share one interpreter without the GIL collapsing it back into a single-threaded program. Here's exactly how that works in yara-orm: the GIL boundary in PyO3, and how a Tokio future becomes a Python await."
tags: python, rust, database, async
canonical_url: "https://dev.to/vsdudakov/i-built-a-python-orm-with-a-rust-engine-heres-how-the-gil-pyo3-and-asyncio-actually-cooperate-4fkj"
cover_image: ""
---

I like Tortoise ORM. Django-style models, async-first, clean. But I wanted more speed on read-heavy paths without reaching for SQLAlchemy, so I built [**yara-orm**](https://github.com/vsdudakov/yara-orm): a Tortoise-style async ORM where the model and query layer is Python, but the engine — connection pooling, parameter binding, and row decoding — is written in Rust (PyO3 over `tokio-postgres` and `rusqlite`).

The API is exactly what you'd expect:

```python
from yara_orm import Model, YaraOrm, fields, in_transaction

class Author(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=100)

class Book(Model):
    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=200)
    author = fields.ForeignKeyField("Author", related_name="books")

await YaraOrm.init("postgres://localhost/app")   # or sqlite://./app.db
await YaraOrm.generate_schemas()

ada = await Author.create(name="Ada")
hot = await Book.filter(author__name="Ada").order_by("-id").limit(10)

async with in_transaction():
    await Book.create(title="Atomic", author=ada)
```

But the API isn't the interesting part — Tortoise already nailed that. The interesting part is underneath: a Rust database engine and Python's asyncio loop have to share one interpreter, and if you get it wrong the GIL collapses the whole thing back into a single-threaded program. Here's exactly how that works.

## Two runtimes in one process

There are two schedulers running at once:

- **CPython's asyncio event loop** — single-threaded, on your main thread, where your `async def` code runs.
- **Tokio's multi-threaded runtime** — background worker threads that actually open sockets, send queries, and parse wire protocols.

The job is to let them cooperate so that the event loop never blocks on I/O, and the database I/O never blocks on the GIL. The GIL is the thing that makes that non-trivial.

## How the GIL shows up in Rust

In PyO3 you can't touch a Python object without *proof* that you hold the GIL. That proof is a token — `Python<'py>` — threaded through the API: every function that reads or creates Python objects takes one, and the `'py` lifetime ties every borrowed `Bound<'py, PyAny>` to it. It's a compile-time guarantee. No token, no access to the interpreter.

So the GIL boundary is explicit in the code, and only two places actually need it:

- **Binding parameters** (Python → Rust): pulling a Python `int` / `str` / `datetime` / `UUID` out and converting it to a Rust value *reads* Python objects, so it holds the GIL.
- **Decoding rows** (Rust → Python): constructing the `int` / `str` / `datetime` you get back *creates* Python objects, so it holds the GIL.

Everything *between* those two — acquiring a pooled connection, sending the query, waiting on the socket, parsing the wire protocol — touches no Python objects at all. So it runs with the **GIL released**. That's the entire point: while Postgres is doing work and bytes are in flight, the GIL is free and other Python tasks run.

To make that safe, the data crossing into the async world has to be **owned and `Send`**. yara-orm converts each parameter into a small Rust `Value` enum *under the GIL*, then hands an owned `Vec<Value>` to the database layer:

```rust
#[derive(Clone)]
enum Value { Null, Int(i64), Text(String), Uuid(Uuid), /* ... */ }
```

By the time real I/O starts there isn't a single `Py<...>` or `Bound<...>` in scope — nothing borrowed from the interpreter, nothing that needs the GIL — so Tokio is free to move the future across worker threads. This is also why you *can't* just hold a `PyObject` across an `.await`: a GIL-bound handle isn't `Send`, and the borrow checker stops you. The architecture is partly **forced** by PyO3's types, which is a feature, not a limitation.

## How a Rust future becomes a Python `await`

The model layer calls `await engine.fetch_rows(sql, params)`. On the Rust side `fetch_rows` doesn't block — it returns a Python awaitable, built with `pyo3-async-runtimes`:

```rust
fn fetch_rows<'p>(&self, py: Python<'p>, sql: String, params: Vec<Value>)
    -> PyResult<Bound<'p, PyAny>>
{
    let backend = self.backend.clone();
    future_into_py(py, async move {
        // runs on a Tokio worker thread, GIL released
        backend.fetch_all_values(&sql, &params).await.map_err(to_pyerr)
    })
}
```

`future_into_py` does three things:

1. Creates a Python `asyncio.Future` bound to the **currently running event loop** — which is why this has to be called from inside a running loop.
2. Spawns the Rust `async move { ... }` onto the **Tokio runtime**, which lives on its own background threads, completely separate from the asyncio loop thread.
3. When the Rust future finishes — on a Tokio worker thread — it schedules the result back onto the asyncio loop with `loop.call_soon_threadsafe(...)`, the *only* thread-safe way to poke the loop from another thread.

From Python it's an ordinary `await`: the coroutine suspends, the event loop keeps serving other tasks, and when the Tokio side resolves the future the loop wakes the coroutine with the rows. The decode step (Rust `Value` → Python objects) re-acquires the GIL for the few microseconds it takes to build the result, then releases it again.

So the two runtimes never block each other: **the asyncio thread is never blocked on I/O, and the database I/O never holds the GIL.** The GIL is held only during the cheap conversion at each end.

That last sentence is the whole performance story, and it tells you exactly where to optimize. The query builder runs **once** per query; the decoder runs **once per row**. A `SELECT` returning 5,000 rows runs your row-hydration code 5,000 times — that loop is where the time goes, on *every* ORM. So that's where the effort went:

- `uuid.UUID` and `decimal.Decimal` type objects are imported **once per interpreter**, not re-resolved per cell (UUID primary keys show up on basically every query).
- Postgres decoding dispatches on the column's type OID via a jump table, instead of walking a 16-deep chain of type comparisons per cell.
- SQLite upper-cases each column's declared type **once per result set** instead of per cell, and binds parameters by *move* rather than copying them twice.

None of these are glamorous. All of them compound across rows — and crucially, all of them are *inside* the short GIL-held window, where every microsecond is one the event loop can't use.

## A note on blocking drivers

`rusqlite` is synchronous — a blocking C library. Call it directly on a Tokio worker and you stall an async executor thread. So SQLite work runs on a dedicated blocking thread pool (the connection pool's `interact` / `spawn_blocking`): same decoupling principle, one layer down. Blocking work stays off *both* the asyncio loop and Tokio's async workers.

## The honest tradeoff

There's another way to build this: serialize the query to bytes (MessagePack or similar) in Python, hand the bytes to Rust, get bytes back, and never touch a Python object in Rust at all — so the GIL is never involved. yara-orm goes the direct-PyO3 route instead.

Holding the GIL during conversion is a real cost, and free-threaded Python (3.13+ `--disable-gil`) changes the calculus: if your workload is many OS threads decoding result sets in parallel, the GIL-free, bytes-on-the-wire design can pull ahead. For the typical single-event-loop async service, paying the GIL for a few microseconds of conversion and skipping a serialize/deserialize round trip is the better trade.

There's no free lunch — just a choice, and most "we rewrote it in Rust" posts don't tell you which one they made.

## Try it

```bash
pip install yara-orm
```

- Repo: https://github.com/vsdudakov/yara-orm
- Docs: https://vsdudakov.github.io/yara-orm/

If you've built a PyO3 bridge yourself: did you go IR-over-MessagePack or direct conversion — and would you make the same call again? Genuinely curious how others have drawn the GIL boundary.

# Yara ORM — launch post copy

Ready-to-paste copy for the v1.0 launch. Tune the personal/voice bits (marked
`[…]`) before posting. The performance numbers come from `benchmarks/README.md`
and `docs/performance.md` — keep them in sync if you re-run the benchmarks.

Links to reuse:
- Repo: https://github.com/vsdudakov/yara-orm
- Docs: https://vsdudakov.github.io/yara-orm/
- PyPI: https://pypi.org/project/yara-orm/
- Benchmarks: https://github.com/vsdudakov/yara-orm/tree/main/benchmarks
- Migration guide: https://vsdudakov.github.io/yara-orm/guides/migrating-from-tortoise/
- Benchmark chart (attach to Reddit/Twitter posts): `docs/assets/benchmark-postgres.png`
  (regenerate with `python benchmarks/plot_benchmarks.py`)

---

## Hacker News — "Show HN"

**Title** (HN strips marketing words; keep it factual, < 80 chars):

```
Show HN: Yara ORM – async Python ORM with a Rust engine, 2–9x faster
```

**First comment** (post immediately after submitting — HN expects the author to
explain context in a comment, not the body):

```
Author here. Yara ORM is an async ORM for Python with a Tortoise/Django-style
API — models, chainable querysets, Q objects, relations, aggregation, signals,
transactions and migrations — but the per-query hot path (connection pooling,
parameter binding, row decoding) is written in Rust (PyO3 + tokio) instead of
pure Python.

The reason it's faster isn't magic, it's where the work happens: SELECTs decode
rows positionally in compiled code with no per-row dict or column-name
allocation, the SQL for each model is compiled once and reused, and prepared
statements are cached on pooled connections. The async bridge keeps your event
loop free.

On PostgreSQL 18 (Python 3.12, 5000 rows, median of 5) it's fastest on every
operation I measured: bulk_insert 2.1x vs Tortoise / 6.2x vs SQLAlchemy,
fetch_all 4.7x / 3.5x, filter 4.1x / 10x. The two latency-bound point ops
(get_by_pk, single_insert) sit near the raw round-trip floor. Full table,
methodology and the runnable script are in the repo — I tried to be honest about
the caveats (sync vs async models, feature-set differences, SQLite's
thread-hop cost on sequential point reads).

It's a drop-in-feel alternative to Tortoise / async SQLAlchemy: if you're on
Tortoise, most code moves across with just the imports and the init() call
changed (migration guide linked below). PostgreSQL + SQLite today, prebuilt
wheels for CPython 3.9–3.14 on Linux/macOS/Windows so there's no Rust toolchain
needed to install. Fully typed, 100% test coverage, MIT.

Repo: https://github.com/vsdudakov/yara-orm
Docs: https://vsdudakov.github.io/yara-orm/
Benchmarks (method + script): https://github.com/vsdudakov/yara-orm/tree/main/benchmarks

Happy to answer questions about the Rust/Python boundary, the benchmark setup,
or what's still missing (e.g. [list known gaps honestly]).
```

**Timing:** weekday, ~8–10am US Eastern. Be present in the thread for the first
2–3 hours — HN heavily probes "Nx faster" claims, so have the methodology answer
ready: own table, same workload/data per ORM, median of 5 warm runs, link the
script.

---

## Reddit r/Python

Check the subreddit rules — standalone "I made this" posts are usually fine, but
there's also a weekly showcase thread. Flair: **Showcase**.

**Title:**

```
I built Yara ORM: an async Python ORM with a Rust engine — 2–9x faster than Tortoise, looking for feedback
```

**Body:**

```
I've been building **Yara ORM**, an async ORM for Python that keeps the
ergonomic, Tortoise/Django-style API people like but moves the hot path into a
compiled Rust engine (PyO3 + tokio). Just tagged v1.0.

**What it looks like** — the API is intentionally familiar:

    from yara_orm import Model, YaraOrm, fields

    class User(Model):
        id = fields.IntField(pk=True)
        name = fields.CharField(max_length=120)

    await YaraOrm.init("postgres://localhost/app")
    await YaraOrm.generate_schemas()
    await User.create(name="Ada")
    print(await User.filter(name__icontains="ad").count())

Lazy chainable querysets, `__` field lookups, `Q` objects, foreign keys / M2M,
`select_related` / `prefetch_related`, aggregation, signals, nested transactions
with savepoints, and operation-based migrations (`makemigrations` / `upgrade` /
`downgrade`).

**Why it's faster:** parameter binding and row decoding run in Rust; SELECTs
decode positionally with no per-row dict allocation; SQL is compiled once per
model and prepared statements are cached on pooled connections. On PostgreSQL
(5000 rows, median of 5) it's the fastest of the four ORMs I tested on every
operation — e.g. bulk_insert 2.1x and filter 4.1x vs Tortoise. Methodology and
the script are in the repo; I documented the caveats too.

**Practical bits:** PostgreSQL + SQLite, prebuilt wheels for CPython 3.9–3.14
(no Rust toolchain to install), fully typed, 100% test coverage, MIT. If you're
on Tortoise there's a migration guide — most code moves across unchanged.

- Repo: https://github.com/vsdudakov/yara-orm
- Docs: https://vsdudakov.github.io/yara-orm/

I'd genuinely like feedback on the API, the benchmark methodology, and what's
missing before people would use it in production. What would you want to see?
```

**Tone for r/Python:** humble, feedback-seeking, no hard sell. Reply to every
top comment. Expect (and welcome) benchmark scrutiny.

---

## Twitter / X / Bluesky / Mastodon thread

Post the benchmark chart image with tweet 1. Use #python (+ #rustlang on
Mastodon/Bluesky).

**1/**
```
Yara ORM v1.0 is out 🎉

An async Python ORM with a Tortoise-style API — but the hot path
(pooling, binding, row decoding) is written in Rust.

2–9× faster than popular pure-Python ORMs. PostgreSQL + SQLite, fully typed.

🔗 github.com/vsdudakov/yara-orm
```

**2/**
```
The API is the boring part on purpose — it should feel like Tortoise/Django:

    await YaraOrm.init("postgres://localhost/app")
    await User.create(name="Ada")
    await User.filter(name__icontains="ad").count()

Lazy querysets, Q objects, relations, prefetch, migrations.
```

**3/**
```
Why it's fast: SELECTs decode rows positionally in compiled code — no per-row
dict or column-name allocation. SQL compiled once per model, prepared statements
cached on pooled connections, event loop stays free.

bulk_insert 2.1×, filter 4.1× vs Tortoise.
```

**4/**
```
Coming from Tortoise? Most code moves across with just the imports + init()
changed. Migration guide:
vsdudakov.github.io/yara-orm/guides/migrating-from-tortoise/

Prebuilt wheels for CPython 3.9–3.14, no Rust toolchain needed. MIT. Feedback welcome 🙏
```

---

## Newsletter / aggregator submissions (one-liners)

- **Python Weekly / PyCoder's Weekly** (submit via their "suggest a link" forms):
  "Yara ORM — a new async Python ORM with a Rust engine (PyO3 + tokio): a
  Tortoise-style API that runs 2–9× faster on PostgreSQL & SQLite. v1.0, fully
  typed, MIT."
- **This Week in Rust** ("Call for Participation" / project spotlight): angle on
  the PyO3 + tokio engine and positional decode plan.
- **Awesome lists** — open PRs to: awesome-python (ORM section), awesome-asyncio,
  any "Python ORM comparison" repos.

---

## Reusable elevator descriptions

**One line:** A fast async Python ORM with a Rust engine — Tortoise-style API,
2–9× faster, PostgreSQL & SQLite.

**Two sentences:** Yara ORM pairs a familiar Tortoise/Django-style async API
(models, querysets, relations, migrations) with a hot path written in Rust
(PyO3 + tokio). It's a drop-in-feel alternative to Tortoise and async SQLAlchemy
that runs 2–9× faster on common operations, with prebuilt wheels for CPython
3.9–3.14 and 100% test coverage.

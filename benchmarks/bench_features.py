"""Feature micro-benchmarks for `yara-orm` only (no cross-ORM comparison).

These cover what the 4-way ``bench.py`` suite intentionally skips because it is
not comparable across ORMs and feature sets:

* **transactions** — autocommit vs one transaction vs a nested savepoint per
  row (the savepoint overhead from transactions-in-transactions);
* **eager loading** — N+1 access vs ``select_related`` (forward FK) and vs
  ``prefetch_related`` (reverse one-to-many);
* **projection** — full model construction vs ``values`` / ``values_list``.

Runs on SQLite by default (zero setup, a throwaway temp file). Set
``BENCH_BACKEND=postgres`` and ``ORM_TEST_DB=...`` to run on PostgreSQL.

Usage:
    python benchmarks/bench_features.py
    BENCH_BACKEND=postgres ORM_TEST_DB=postgres://localhost/orm_demo \
        python benchmarks/bench_features.py
Env: BENCH_AUTHORS, BENCH_BOOKS_PER, BENCH_S (tx insert rows), BENCH_REPEAT.
"""

from __future__ import annotations

import asyncio
import os
import statistics
import tempfile
import time

from yara_orm import Model, YaraOrm, fields, in_transaction
from yara_orm.connection import get_engine

AUTHORS = int(os.environ.get("BENCH_AUTHORS", "100"))
BOOKS_PER = int(os.environ.get("BENCH_BOOKS_PER", "5"))
S = int(os.environ.get("BENCH_S", "300"))
REPEAT = int(os.environ.get("BENCH_REPEAT", "5"))
BACKEND = os.environ.get("BENCH_BACKEND", "sqlite")
URL = os.environ.get("ORM_TEST_DB", "postgres://localhost/orm_demo")
SQLITE_DIR = os.environ.get("BENCH_SQLITE_DIR", "/tmp")

BOOKS = AUTHORS * BOOKS_PER


class FAuthor(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "f_author"


class FBook(Model):
    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=50)
    author = fields.ForeignKeyField("FAuthor", related_name="books")

    class Meta:
        table = "f_book"


class FNote(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)
    value = fields.IntField()

    class Meta:
        table = "f_note"


class _Stopwatch:
    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.elapsed = time.perf_counter() - self.t0


def clear_sql(table: str) -> str:
    if BACKEND == "sqlite":
        return f"DELETE FROM {table}"
    return f"TRUNCATE {table} RESTART IDENTITY CASCADE"


def drop_sql(table: str) -> str:
    if BACKEND == "sqlite":
        return f"DROP TABLE IF EXISTS {table}"
    return f"DROP TABLE IF EXISTS {table} CASCADE"


async def _seed() -> None:
    """Create the schema and a fixed authors-with-books dataset."""
    engine = get_engine()
    for table in ("f_book", "f_author", "f_note"):
        await engine.execute(drop_sql(table))
    await YaraOrm.generate_schemas()

    authors = await FAuthor.bulk_create([FAuthor(name=f"a{i}") for i in range(AUTHORS)])
    books = [FBook(title=f"b{a.id}_{j}", author=a) for a in authors for j in range(BOOKS_PER)]
    await FBook.bulk_create(books)


async def bench_once() -> dict:
    """Run every feature measurement once and return per-operation seconds."""
    res: dict = {}
    engine = get_engine()

    # -- transactions: insert S rows three ways ------------------------------
    await engine.execute(clear_sql("f_note"))
    with _Stopwatch() as sw:
        for i in range(S):
            await FNote.create(name=f"n{i}", value=i)
    res["insert_autocommit"] = sw.elapsed

    await engine.execute(clear_sql("f_note"))
    with _Stopwatch() as sw:
        async with in_transaction():
            for i in range(S):
                await FNote.create(name=f"n{i}", value=i)
    res["insert_one_tx"] = sw.elapsed

    await engine.execute(clear_sql("f_note"))
    with _Stopwatch() as sw:
        async with in_transaction():
            for i in range(S):
                async with in_transaction():  # a savepoint per row
                    await FNote.create(name=f"n{i}", value=i)
    res["insert_savepoint_each"] = sw.elapsed

    # -- forward FK: N+1 access vs select_related ----------------------------
    with _Stopwatch() as sw:
        for book in await FBook.all():
            _ = (await book.author).name  # one query per book
    res["forward_n_plus_1"] = sw.elapsed

    with _Stopwatch() as sw:
        for book in await FBook.all().select_related("author"):
            _ = book.author.name  # hydrated by the join, no query
    res["forward_select_related"] = sw.elapsed

    # -- reverse one-to-many: N+1 access vs prefetch_related -----------------
    with _Stopwatch() as sw:
        for author in await FAuthor.all():
            _ = len(await author.books)  # one query per author
    res["reverse_n_plus_1"] = sw.elapsed

    with _Stopwatch() as sw:
        for author in await FAuthor.all().prefetch_related("books"):
            _ = len(await author.books)  # served from the prefetch cache
    res["reverse_prefetch"] = sw.elapsed

    # -- projection: full model vs values / values_list ----------------------
    with _Stopwatch() as sw:
        _ = await FBook.all()
    res["fetch_full"] = sw.elapsed

    with _Stopwatch() as sw:
        _ = await FBook.all().values("id", "title")
    res["values"] = sw.elapsed

    with _Stopwatch() as sw:
        _ = await FBook.all().values_list("id", "title")
    res["values_list"] = sw.elapsed

    return res


#: (operation, work units, unit) for the reporting tables.
OPS = [
    ("insert_autocommit", S, "rows"),
    ("insert_one_tx", S, "rows"),
    ("insert_savepoint_each", S, "rows"),
    ("forward_n_plus_1", BOOKS, "rows"),
    ("forward_select_related", BOOKS, "rows"),
    ("reverse_n_plus_1", AUTHORS, "authors"),
    ("reverse_prefetch", AUTHORS, "authors"),
    ("fetch_full", BOOKS, "rows"),
    ("values", BOOKS, "rows"),
    ("values_list", BOOKS, "rows"),
]

#: (label, slow_op, fast_op) contrasts highlighting each feature's payoff.
CONTRASTS = [
    ("select_related vs N+1 (forward FK)", "forward_n_plus_1", "forward_select_related"),
    ("prefetch_related vs N+1 (reverse)", "reverse_n_plus_1", "reverse_prefetch"),
    ("one transaction vs autocommit", "insert_autocommit", "insert_one_tx"),
    ("savepoint-per-row overhead vs one tx", "insert_savepoint_each", "insert_one_tx"),
    ("values_list vs full model fetch", "fetch_full", "values_list"),
]


async def _run_repeats() -> dict:
    """Seed once, then time every operation REPEAT times; return the medians."""
    await _seed()
    runs = [await bench_once() for _ in range(REPEAT)]
    return {op: statistics.median(r[op] for r in runs) for op in runs[0]}


def main() -> None:
    if BACKEND == "sqlite":
        fd, path = tempfile.mkstemp(suffix=".db", dir=SQLITE_DIR)
        os.close(fd)
        os.remove(path)
        db_url = f"sqlite://{path}"
        target = f"sqlite ({path})"
    else:
        db_url = URL
        path = None
        target = URL

    print(
        f"BACKEND={BACKEND}  target={target}  authors={AUTHORS}  books={BOOKS}  "
        f"tx_rows={S}  REPEAT={REPEAT} (median)\n"
    )

    async def _go() -> dict:
        await YaraOrm.init(db_url)
        try:
            return await _run_repeats()
        finally:
            await YaraOrm.close()

    results = asyncio.run(_go())

    if path is not None:
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(path + suffix):
                os.remove(path + suffix)

    print("=== Time per operation (ms, lower is better) ===")
    print(f"{'operation':<26}{'time (ms)':>12}{'throughput':>22}")
    print("-" * 60)
    for op, count, unit in OPS:
        t = results[op]
        print(f"{op:<26}{t * 1000:>12.2f}{f'{count / t:,.0f} {unit}/s':>22}")

    print("\n=== Feature payoff (slower / faster; >1 means the feature wins) ===")
    for label, slow, fast in CONTRASTS:
        ratio = results[slow] / results[fast]
        print(f"{label:<40}{ratio:>6.1f}x")

    print(
        "\nNotes: yara-orm only — these are not cross-ORM comparable.\n"
        "  - select_related / prefetch collapse the N+1 fan-out into one or two\n"
        "    queries; the ratios scale with the row count.\n"
        "  - a single transaction amortises per-statement commit cost; a nested\n"
        "    savepoint per row shows the (small) SAVEPOINT/RELEASE overhead.\n"
        "  - values / values_list skip model construction (positional decode only)."
    )


if __name__ == "__main__":
    main()

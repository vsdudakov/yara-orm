"""Benchmark: this library (`yara-orm`) vs Tortoise ORM vs Pony ORM on PostgreSQL.

Identical workloads run against the same database, each ORM in its own table.
Times are wall-clock for the warm path (drivers/prepared-statement caches are
hit once before measuring). This is throughput-oriented and not a micro-
benchmark: sync (Pony) vs async (Tortoise, yara-orm) and differing feature sets mean
results are indicative, not absolute. Methodology is printed with the results.

Usage:
    ORM_TEST_DB=postgres://user@localhost/orm_demo python benchmarks/bench.py
Env: BENCH_N (bulk rows), BENCH_S (single-insert rows), BENCH_GETS (pk lookups).
"""

from __future__ import annotations

import asyncio
import os
import random
import statistics
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

URL = os.environ.get("ORM_TEST_DB", "postgres://sevad@localhost/orm_demo")
N = int(os.environ.get("BENCH_N", "5000"))
S = int(os.environ.get("BENCH_S", "500"))
GETS = int(os.environ.get("BENCH_GETS", "1000"))
REPEAT = int(os.environ.get("BENCH_REPEAT", "5"))

random.seed(1234)
PK_SEQUENCE = [random.randint(1, N) for _ in range(GETS)]
HALF = N // 2
NOW = datetime.now(timezone.utc)
NOW_NAIVE = datetime.now()


#: Which database to benchmark: "postgres" (default) or "sqlite".
BACKEND = os.environ.get("BENCH_BACKEND", "postgres")
SQLITE_DIR = os.environ.get("BENCH_SQLITE_DIR", "/tmp")


def pg_parts(url: str) -> dict:
    u = urlparse(url)
    return {
        "user": u.username or os.environ.get("USER"),
        "password": u.password,
        "host": u.hostname or "localhost",
        "port": u.port or 5432,
        "database": (u.path or "").lstrip("/"),
    }


def clear_sql(table: str) -> str:
    """Statement that empties a table (SQLite has no TRUNCATE)."""
    if BACKEND == "sqlite":
        return f"DELETE FROM {table}"
    return f"TRUNCATE {table} RESTART IDENTITY"


def drop_sql(table: str) -> str:
    if BACKEND == "sqlite":
        return f"DROP TABLE IF EXISTS {table}"
    return f"DROP TABLE IF EXISTS {table} CASCADE"


def ours_url() -> str:
    if BACKEND == "sqlite":
        return f"sqlite://{SQLITE_DIR}/bench_ours.db"
    return URL


def _pg_userinfo(p: dict) -> str:
    """Render ``user`` or ``user:password`` for a libpq-style URL.

    The password must survive into the competitor URLs: in CI PostgreSQL
    requires ``postgres:postgres`` auth, so dropping it (as an earlier
    ``{user}@{host}`` form did) makes Tortoise/SQLAlchemy fail with
    'password authentication failed' while yara-orm — which uses the full URL
    verbatim — connects fine, producing a bogus one-sided benchmark.
    """
    return f"{p['user']}:{p['password']}" if p["password"] else p["user"]


def tortoise_url() -> str:
    if BACKEND == "sqlite":
        return f"sqlite://{SQLITE_DIR}/bench_tortoise.db"
    p = pg_parts(URL)
    return f"asyncpg://{_pg_userinfo(p)}@{p['host']}:{p['port']}/{p['database']}"


def sqla_url() -> str:
    if BACKEND == "sqlite":
        return f"sqlite+aiosqlite:///{SQLITE_DIR}/bench_sqla.db"
    p = pg_parts(URL)
    return f"postgresql+asyncpg://{_pg_userinfo(p)}@{p['host']}:{p['port']}/{p['database']}"


class _Stopwatch:
    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.elapsed = time.perf_counter() - self.t0


# ---------------------------------------------------------------------------
# This library
# ---------------------------------------------------------------------------
async def run_ours() -> dict:
    from yara_orm import Model, YaraOrm, fields
    from yara_orm.connection import get_engine

    class BOurs(Model):
        id = fields.IntField(pk=True)
        name = fields.CharField(max_length=50)
        value = fields.IntField()
        created = fields.DatetimeField(auto_now_add=True)

        class Meta:
            table = "bench_ours"

    await YaraOrm.init(ours_url())
    engine = get_engine()
    await engine.execute(drop_sql("bench_ours"))
    await YaraOrm.generate_schemas()

    res: dict = {}

    objs = [BOurs(name=f"n{i}", value=i) for i in range(N)]
    with _Stopwatch() as sw:
        await BOurs.bulk_create(objs)
    res["bulk_insert"] = sw.elapsed

    with _Stopwatch() as sw:
        rows = await BOurs.all()
    res["fetch_all"] = sw.elapsed
    assert len(rows) == N

    with _Stopwatch() as sw:
        total = await BOurs.all().count()
    res["count"] = sw.elapsed
    assert total == N

    with _Stopwatch() as sw:
        rows = await BOurs.filter(value__gte=HALF)
    res["filter"] = sw.elapsed

    with _Stopwatch() as sw:
        for pk in PK_SEQUENCE:
            await BOurs.get(id=pk)
    res["get_by_pk"] = sw.elapsed

    with _Stopwatch() as sw:
        await BOurs.filter(value__lt=HALF).update(value=0)
    res["update"] = sw.elapsed

    with _Stopwatch() as sw:
        await BOurs.filter(value__gte=HALF).delete()
    res["delete"] = sw.elapsed

    await engine.execute(clear_sql("bench_ours"))
    with _Stopwatch() as sw:
        for i in range(S):
            await BOurs.create(name=f"s{i}", value=i)
    res["single_insert"] = sw.elapsed

    await YaraOrm.close()
    return res


# ---------------------------------------------------------------------------
# Tortoise ORM
# ---------------------------------------------------------------------------
# Tortoise discovers models by scanning a module's top-level classes, so the
# model must live at module scope (not inside the runner function).
try:
    from tortoise import fields as tfields
    from tortoise.models import Model as TModel

    class BTort(TModel):
        id = tfields.IntField(pk=True)
        name = tfields.CharField(max_length=50)
        value = tfields.IntField()
        created = tfields.DatetimeField(auto_now_add=True)

        class Meta:
            table = "bench_tortoise"

except ImportError:  # pragma: no cover
    BTort = None


async def run_tortoise() -> dict:
    from tortoise import Tortoise

    await Tortoise.init(db_url=tortoise_url(), modules={"models": [__name__]})
    conn = Tortoise.get_connection("default")
    await conn.execute_query(drop_sql("bench_tortoise"))
    await Tortoise.generate_schemas(safe=True)

    res: dict = {}

    objs = [BTort(name=f"n{i}", value=i) for i in range(N)]
    with _Stopwatch() as sw:
        await BTort.bulk_create(objs)
    res["bulk_insert"] = sw.elapsed

    with _Stopwatch() as sw:
        rows = await BTort.all()
    res["fetch_all"] = sw.elapsed
    assert len(rows) == N

    with _Stopwatch() as sw:
        total = await BTort.all().count()
    res["count"] = sw.elapsed
    assert total == N

    with _Stopwatch() as sw:
        rows = await BTort.filter(value__gte=HALF)
    res["filter"] = sw.elapsed

    with _Stopwatch() as sw:
        for pk in PK_SEQUENCE:
            await BTort.get(id=pk)
    res["get_by_pk"] = sw.elapsed

    with _Stopwatch() as sw:
        await BTort.filter(value__lt=HALF).update(value=0)
    res["update"] = sw.elapsed

    with _Stopwatch() as sw:
        await BTort.filter(value__gte=HALF).delete()
    res["delete"] = sw.elapsed

    await conn.execute_query(clear_sql("bench_tortoise"))
    with _Stopwatch() as sw:
        for i in range(S):
            await BTort.create(name=f"s{i}", value=i)
    res["single_insert"] = sw.elapsed

    await Tortoise.close_connections()
    return res


# ---------------------------------------------------------------------------
# Pony ORM (synchronous)
# ---------------------------------------------------------------------------
def run_pony() -> dict:
    from pony import orm as pony

    db = pony.Database()

    class BPony(db.Entity):
        _table_ = "bench_pony"
        id = pony.PrimaryKey(int, auto=True)
        name = pony.Required(str)
        value = pony.Required(int)
        created = pony.Optional(datetime)

    if BACKEND == "sqlite":
        db.bind(provider="sqlite", filename=f"{SQLITE_DIR}/bench_pony.db", create_db=True)
    else:
        parts = pg_parts(URL)
        bind_kwargs = {
            "provider": "postgres",
            "user": parts["user"],
            "host": parts["host"],
            "port": parts["port"],
            "database": parts["database"],
        }
        if parts["password"]:
            bind_kwargs["password"] = parts["password"]
        db.bind(**bind_kwargs)
    db.generate_mapping(create_tables=True)

    with pony.db_session:
        db.execute(clear_sql("bench_pony"))
        if BACKEND == "sqlite":
            # DELETE doesn't reset AUTOINCREMENT; clear the sequence so ids
            # restart at 1 each run (Postgres uses TRUNCATE ... RESTART IDENTITY).
            db.execute("DELETE FROM sqlite_sequence WHERE name = 'bench_pony'")

    res: dict = {}

    with _Stopwatch() as sw:
        with pony.db_session:
            for i in range(N):
                BPony(name=f"n{i}", value=i, created=NOW_NAIVE)
    res["bulk_insert"] = sw.elapsed

    with _Stopwatch() as sw:
        with pony.db_session:
            rows = pony.select(p for p in BPony)[:]
    res["fetch_all"] = sw.elapsed
    assert len(rows) == N

    with _Stopwatch() as sw:
        with pony.db_session:
            total = BPony.select().count()
    res["count"] = sw.elapsed
    assert total == N

    with _Stopwatch() as sw:
        with pony.db_session:
            rows = pony.select(p for p in BPony if p.value >= HALF)[:]
    res["filter"] = sw.elapsed

    with _Stopwatch() as sw:
        for pk in PK_SEQUENCE:
            with pony.db_session:
                _ = BPony[pk]
    res["get_by_pk"] = sw.elapsed

    # Pony has no SQL-level bulk update; the idiomatic path mutates objects.
    with _Stopwatch() as sw:
        with pony.db_session:
            for p in pony.select(p for p in BPony if p.value < HALF):
                p.value = 0
    res["update"] = sw.elapsed

    with _Stopwatch() as sw:
        with pony.db_session:
            pony.delete(p for p in BPony if p.value >= HALF)
    res["delete"] = sw.elapsed

    with pony.db_session:
        db.execute(clear_sql("bench_pony"))
    with _Stopwatch() as sw:
        for i in range(S):
            with pony.db_session:
                BPony(name=f"s{i}", value=i, created=NOW_NAIVE)
    res["single_insert"] = sw.elapsed

    db.disconnect()
    return res


# ---------------------------------------------------------------------------
# SQLAlchemy (async, asyncpg)
# ---------------------------------------------------------------------------
try:
    from sqlalchemy import DateTime, Integer, String, func, select
    from sqlalchemy import delete as sa_delete
    from sqlalchemy import update as sa_update
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

    class SABase(DeclarativeBase):
        pass

    class BAlc(SABase):
        __tablename__ = "bench_alchemy"
        id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
        name: Mapped[str] = mapped_column(String(50))
        value: Mapped[int] = mapped_column(Integer)
        created: Mapped[datetime] = mapped_column(DateTime(timezone=True))

except ImportError:  # pragma: no cover
    SABase = None
    BAlc = None


async def run_sqlalchemy() -> dict:
    engine = create_async_engine(sqla_url())
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.exec_driver_sql(drop_sql("bench_alchemy"))
        await conn.run_sync(SABase.metadata.create_all)

    res: dict = {}

    objs = [BAlc(name=f"n{i}", value=i, created=NOW) for i in range(N)]
    with _Stopwatch() as sw:
        async with Session() as s:
            s.add_all(objs)
            await s.commit()
    res["bulk_insert"] = sw.elapsed

    with _Stopwatch() as sw:
        async with Session() as s:
            rows = (await s.execute(select(BAlc))).scalars().all()
    res["fetch_all"] = sw.elapsed
    assert len(rows) == N

    with _Stopwatch() as sw:
        async with Session() as s:
            total = await s.scalar(select(func.count()).select_from(BAlc))
    res["count"] = sw.elapsed
    assert total == N

    with _Stopwatch() as sw:
        async with Session() as s:
            rows = (await s.execute(select(BAlc).where(BAlc.value >= HALF))).scalars().all()
    res["filter"] = sw.elapsed

    # Fresh session per lookup to force a database hit (no identity-map reuse).
    with _Stopwatch() as sw:
        for pk in PK_SEQUENCE:
            async with Session() as s:
                await s.get(BAlc, pk)
    res["get_by_pk"] = sw.elapsed

    with _Stopwatch() as sw:
        async with Session() as s:
            await s.execute(sa_update(BAlc).where(BAlc.value < HALF).values(value=0))
            await s.commit()
    res["update"] = sw.elapsed

    with _Stopwatch() as sw:
        async with Session() as s:
            await s.execute(sa_delete(BAlc).where(BAlc.value >= HALF))
            await s.commit()
    res["delete"] = sw.elapsed

    async with engine.begin() as conn:
        await conn.exec_driver_sql(clear_sql("bench_alchemy"))
    with _Stopwatch() as sw:
        for i in range(S):
            async with Session() as s:
                s.add(BAlc(name=f"s{i}", value=i, created=NOW))
                await s.commit()
    res["single_insert"] = sw.elapsed

    await engine.dispose()
    return res


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
OPS = [
    ("bulk_insert", N, "rows"),
    ("single_insert", S, "rows"),
    ("fetch_all", N, "rows"),
    ("count", N, "rows"),
    ("filter", N - HALF, "rows"),
    ("get_by_pk", GETS, "queries"),
    ("update", HALF, "rows"),
    ("delete", N - HALF, "rows"),
]


def fmt_ms(v):
    return "-" if v is None else f"{v * 1000:8.1f}"


def _median_runs(runner, is_async: bool) -> dict:
    """Run the suite REPEAT times and return the median time per operation."""
    runs = []
    for _ in range(REPEAT):
        runs.append(asyncio.run(runner()) if is_async else runner())
    return {op: statistics.median(r[op] for r in runs) for op in runs[0]}


def main():
    target = URL if BACKEND == "postgres" else f"sqlite ({SQLITE_DIR})"
    print(
        f"BACKEND={BACKEND}  target={target}  N={N}  S={S}  GETS={GETS}  REPEAT={REPEAT} (median)\n"
    )

    results = {}
    print("running: yara-orm (ours) ...")
    results["ours"] = _median_runs(run_ours, True)
    print("running: tortoise ...")
    try:
        results["tortoise"] = _median_runs(run_tortoise, True)
    except Exception as exc:  # noqa: BLE001
        print(f"  tortoise failed: {exc!r}")
        results["tortoise"] = {}
    print("running: sqlalchemy ...")
    try:
        results["sqlalchemy"] = _median_runs(run_sqlalchemy, True)
    except Exception as exc:  # noqa: BLE001
        print(f"  sqlalchemy failed: {exc!r}")
        results["sqlalchemy"] = {}
    print("running: pony ...")
    try:
        results["pony"] = _median_runs(run_pony, False)
    except Exception as exc:  # noqa: BLE001
        print(f"  pony failed: {exc!r}")
        results["pony"] = {}

    cols = ["ours", "tortoise", "sqlalchemy", "pony"]
    competitors = ["tortoise", "sqlalchemy", "pony"]

    print("\n=== Time per operation (ms, lower is better) ===")
    header = f"{'operation':<16}" + "".join(f"{c:>13}" for c in cols)
    print(header)
    print("-" * len(header))
    for op, _count, _unit in OPS:
        row = f"{op:<16}"
        for c in cols:
            row += f"{fmt_ms(results[c].get(op)):>13}"
        print(row)

    print("\n=== Throughput (ops/sec, higher is better) ===")
    print(f"{'operation':<16}" + "".join(f"{c:>13}" for c in cols))
    for op, count, _unit in OPS:
        row = f"{op:<16}"
        for c in cols:
            v = results[c].get(op)
            row += f"{('-' if not v else f'{count / v:12.0f}'):>13}"
        print(row)

    print("\n=== Speedup vs ours (competitor_time / ours_time; >1 means ours faster) ===")
    print(f"{'operation':<16}" + "".join(f"{c:>13}" for c in competitors))
    for op, _count, _unit in OPS:
        row = f"{op:<16}"
        ours_v = results["ours"].get(op)
        for c in competitors:
            cv = results[c].get(op)
            row += f"{('-' if not (ours_v and cv) else f'{cv / ours_v:11.1f}x'):>13}"
        print(row)

    print(
        "\nNotes: each ORM uses its own table; same workload; median of "
        f"{REPEAT} runs (warm)."
        "\n  - Tortoise & SQLAlchemy are async over asyncpg; Pony is sync over psycopg2."
        "\n  - SQLAlchemy get_by_pk uses a fresh session per lookup (no identity-map reuse)."
        "\n  - Pony opens a transaction per get and has no SQL-level bulk UPDATE,"
        "\n    so its update path mutates objects in a loop."
    )


if __name__ == "__main__":
    main()

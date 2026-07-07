"""Benchmark: this library (`yara-orm`) vs eight other Python ORMs.

Competitors: Tortoise, SQLAlchemy, Pony, Django, Peewee, SQLObject, Ormar, Piccolo.
Identical workloads run against the same database, each ORM in its own table.
Times are wall-clock for the warm path (drivers/prepared-statement caches are
hit once before measuring). This is throughput-oriented and not a micro-
benchmark: sync (Pony, Django, Peewee, SQLObject) vs async (Tortoise, SQLAlchemy,
Ormar, Piccolo, yara-orm) and differing feature sets mean results are indicative,
not absolute. Any ORM that isn't installed (or can't serve the chosen backend) is
skipped and shown as "-". Methodology is printed with the results.

Usage:
    ORM_TEST_DB=postgres://user@localhost/orm_demo python benchmarks/bench.py
    BENCH_BACKEND=sqlite python benchmarks/bench.py
    BENCH_BACKEND=mysql ORM_TEST_MYSQL=mysql://root:root@localhost:3306/orm_demo \
        python benchmarks/bench.py
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

#: Categories the rows are spread across for the aggregate benchmark
#: (``GROUP BY cat`` with ``COUNT``/``SUM`` and a ``HAVING`` threshold) and the
#: threshold itself — a group passes when it holds more than TH rows (all do
#: under the even spread, so the HAVING clause is still evaluated each run).
CATS = 20
TH = (N // CATS) // 2


#: Which database to benchmark: "postgres" (default), "mysql", "mariadb" or "sqlite".
BACKEND = os.environ.get("BENCH_BACKEND", "postgres")
SQLITE_DIR = os.environ.get("BENCH_SQLITE_DIR", "/tmp")
MYSQL_URL = os.environ.get("ORM_TEST_MYSQL", "mysql://root:root@localhost:3306/orm_demo")
#: MariaDB uses the same wire protocol/drivers as MySQL, so every competitor
#: connects to it through its MySQL path; only the server URL differs.
MARIADB_URL = os.environ.get("ORM_TEST_MARIADB", "mysql://root:root@localhost:3307/orm_demo")
#: Oracle only benchmarks yara-orm itself — none of the eight competitors ship an
#: Oracle backend, so ``BENCH_BACKEND=oracle`` runs the "ours" column alone.
ORACLE_URL = os.environ.get("ORM_TEST_ORACLE", "oracle://orm:orm@localhost:1521/FREEPDB1")
#: SQL Server, like Oracle, benchmarks yara-orm alone — the competitor ORMs need
#: an ODBC driver stack to reach it, so ``BENCH_BACKEND=mssql`` runs "ours" only.
MSSQL_URL = os.environ.get("ORM_TEST_MSSQL", "mssql://sa:yaraOrm_Pass1@localhost:1433/master")


def pg_parts(url: str, default_port: int = 5432) -> dict:
    u = urlparse(url)
    return {
        "user": u.username or os.environ.get("USER"),
        "password": u.password,
        "host": u.hostname or "localhost",
        "port": u.port or default_port,
        "database": (u.path or "").lstrip("/"),
    }


def mysql_family_url() -> str:
    """The active MySQL-family server URL (MariaDB or MySQL)."""
    return MARIADB_URL if BACKEND == "mariadb" else MYSQL_URL


def mysql_parts() -> dict:
    return pg_parts(mysql_family_url(), default_port=3306)


def clear_sql(table: str) -> str:
    """Statement that empties a table (SQLite has no TRUNCATE)."""
    if BACKEND == "sqlite":
        return f"DELETE FROM {table}"
    if BACKEND in ("mysql", "mariadb"):
        # MySQL's TRUNCATE resets AUTO_INCREMENT by itself (no RESTART IDENTITY).
        return f"TRUNCATE TABLE {table}"
    if BACKEND == "oracle":
        # Oracle folds unquoted names to upper-case; the ORM creates the table
        # quoted lower-case, so the name must be quoted to match. TRUNCATE takes
        # no RESTART IDENTITY (the IDENTITY sequence simply carries on).
        return f'TRUNCATE TABLE "{table}"'
    if BACKEND == "mssql":
        # SQL Server's TRUNCATE reseeds the IDENTITY column by itself (there is
        # no RESTART IDENTITY clause).
        return f"TRUNCATE TABLE {table}"
    return f"TRUNCATE {table} RESTART IDENTITY"


def drop_sql(table: str) -> str:
    # SQL Server (2016+) also honours DROP TABLE IF EXISTS and takes no CASCADE.
    if BACKEND in ("sqlite", "mysql", "mariadb", "mssql"):  # MySQL accepts no CASCADE here
        return f"DROP TABLE IF EXISTS {table}"
    if BACKEND == "oracle":  # quoted name; CASCADE CONSTRAINTS severs FKs
        return f'DROP TABLE IF EXISTS "{table}" CASCADE CONSTRAINTS'
    return f"DROP TABLE IF EXISTS {table} CASCADE"


def ours_url() -> str:
    if BACKEND == "sqlite":
        # SQLite is in-process (no I/O to overlap), so the recommended config
        # drives statements synchronously on the calling thread instead of the
        # async bridge — the right choice for an embedded DB and how yara-orm is
        # deployed on SQLite. The competitors' aiosqlite pays the async cost for
        # no benefit here.
        return f"sqlite://{SQLITE_DIR}/bench_ours.db?sync_fast_path=1"
    if BACKEND in ("mysql", "mariadb"):
        return mysql_family_url()
    if BACKEND == "oracle":
        return ORACLE_URL
    if BACKEND == "mssql":
        return MSSQL_URL
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
    if BACKEND in ("mysql", "mariadb"):
        p = mysql_parts()
        return f"mysql://{_pg_userinfo(p)}@{p['host']}:{p['port']}/{p['database']}"
    p = pg_parts(URL)
    return f"asyncpg://{_pg_userinfo(p)}@{p['host']}:{p['port']}/{p['database']}"


def sqla_url() -> str:
    if BACKEND == "sqlite":
        return f"sqlite+aiosqlite:///{SQLITE_DIR}/bench_sqla.db"
    if BACKEND in ("mysql", "mariadb"):
        p = mysql_parts()
        return f"mysql+aiomysql://{_pg_userinfo(p)}@{p['host']}:{p['port']}/{p['database']}"
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
    from yara_orm.aggregations import Count, Sum
    from yara_orm.connection import get_engine

    class BOurs(Model):
        id = fields.IntField(pk=True)
        name = fields.CharField(max_length=50)
        value = fields.IntField()
        cat = fields.IntField()  # low-cardinality group key for the aggregate op
        created = fields.DatetimeField(auto_now_add=True)

        class Meta:
            table = "bench_ours"

    await YaraOrm.init(ours_url())
    engine = get_engine()
    await engine.execute(drop_sql("bench_ours"))
    await YaraOrm.generate_schemas()

    res: dict = {}

    objs = [BOurs(name=f"n{i}", value=i, cat=i % CATS) for i in range(N)]
    with _Stopwatch() as sw:
        await BOurs.bulk_create(objs)
    res["bulk_insert"] = sw.elapsed

    with _Stopwatch() as sw:
        agg = await (
            BOurs.annotate(n=Count("id"), s=Sum("value"))
            .group_by("cat")
            .filter(n__gt=TH)
            .order_by("cat")
            .values("cat", "n", "s")
        )
    res["group_by"] = sw.elapsed
    assert len(agg) == CATS

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
            await BOurs.create(name=f"s{i}", value=i, cat=i % CATS)
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
    from tortoise.functions import Count as TCount
    from tortoise.functions import Sum as TSum
    from tortoise.models import Model as TModel

    class BTort(TModel):
        id = tfields.IntField(pk=True)
        name = tfields.CharField(max_length=50)
        value = tfields.IntField()
        cat = tfields.IntField()
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

    objs = [BTort(name=f"n{i}", value=i, cat=i % CATS) for i in range(N)]
    with _Stopwatch() as sw:
        await BTort.bulk_create(objs)
    res["bulk_insert"] = sw.elapsed

    with _Stopwatch() as sw:
        agg = await (
            BTort.annotate(n=TCount("id"), s=TSum("value"))
            .group_by("cat")
            .filter(n__gt=TH)
            .order_by("cat")
            .values("cat", "n", "s")
        )
    res["group_by"] = sw.elapsed
    assert len(agg) == CATS

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
            await BTort.create(name=f"s{i}", value=i, cat=i % CATS)
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
        cat = pony.Required(int)
        created = pony.Optional(datetime)

    # Drop any leftover table BEFORE pony maps/creates it, so a table from an
    # earlier run with a different schema (e.g. before the ``cat`` column) is
    # rebuilt — mirroring how the other ORMs drop their table at the top of a run.
    if BACKEND == "sqlite":
        import contextlib

        with contextlib.suppress(FileNotFoundError):
            os.remove(f"{SQLITE_DIR}/bench_pony.db")
        db.bind(provider="sqlite", filename=f"{SQLITE_DIR}/bench_pony.db", create_db=True)
    elif BACKEND in ("mysql", "mariadb"):
        import pymysql

        parts = mysql_parts()
        conn = pymysql.connect(
            host=parts["host"],
            port=parts["port"],
            user=parts["user"],
            password=parts["password"] or "",
            database=parts["database"],
            autocommit=True,
        )
        conn.cursor().execute(drop_sql("bench_pony"))
        conn.close()
        db.bind(
            provider="mysql",
            user=parts["user"],
            passwd=parts["password"] or "",
            host=parts["host"],
            port=parts["port"],
            db=parts["database"],
        )
    else:
        import psycopg2

        parts = pg_parts(URL)
        conn = psycopg2.connect(
            host=parts["host"],
            port=parts["port"],
            user=parts["user"],
            password=parts["password"],
            dbname=parts["database"],
        )
        conn.autocommit = True
        conn.cursor().execute(drop_sql("bench_pony"))
        conn.close()
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

    res: dict = {}

    with _Stopwatch() as sw:
        with pony.db_session:
            for i in range(N):
                BPony(name=f"n{i}", value=i, cat=i % CATS, created=NOW_NAIVE)
    res["bulk_insert"] = sw.elapsed

    try:
        with _Stopwatch() as sw:
            with pony.db_session:
                pony.select((p.cat, pony.count(p), pony.sum(p.value)) for p in BPony).filter(
                    lambda cat, cnt, total: cnt > TH
                ).order_by(1)[:]
        res["group_by"] = sw.elapsed
    except Exception:  # noqa: BLE001 - Pony's group-by/HAVING dialecting is finicky
        pass  # leave group_by unset -> reported as "-" without losing the other ops

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
                BPony(name=f"s{i}", value=i, cat=i % CATS, created=NOW_NAIVE)
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
        cat: Mapped[int] = mapped_column(Integer)
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

    objs = [BAlc(name=f"n{i}", value=i, cat=i % CATS, created=NOW) for i in range(N)]
    with _Stopwatch() as sw:
        async with Session() as s:
            s.add_all(objs)
            await s.commit()
    res["bulk_insert"] = sw.elapsed

    with _Stopwatch() as sw:
        async with Session() as s:
            stmt = (
                select(BAlc.cat, func.count(BAlc.id), func.sum(BAlc.value))
                .group_by(BAlc.cat)
                .having(func.count(BAlc.id) > TH)
                .order_by(BAlc.cat)
            )
            agg = (await s.execute(stmt)).all()
    res["group_by"] = sw.elapsed
    assert len(agg) == CATS

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
                s.add(BAlc(name=f"s{i}", value=i, cat=i % CATS, created=NOW))
                await s.commit()
    res["single_insert"] = sw.elapsed

    await engine.dispose()
    return res


# ---------------------------------------------------------------------------
# Django ORM (synchronous)
# ---------------------------------------------------------------------------
# Django needs its settings configured (and ``django.setup()`` run) *before* any
# model class is defined, so both happen at import time. The model declares an
# explicit ``app_label`` so it can live in this standalone script with no app in
# ``INSTALLED_APPS`` — the documented pattern for using the ORM outside a project.
def _django_db() -> dict:
    if BACKEND == "sqlite":
        return {"ENGINE": "django.db.backends.sqlite3", "NAME": f"{SQLITE_DIR}/bench_django.db"}
    if BACKEND in ("mysql", "mariadb"):
        import pymysql

        pymysql.install_as_MySQLdb()
        p = mysql_parts()
        return {
            "ENGINE": "django.db.backends.mysql",
            "NAME": p["database"],
            "USER": p["user"],
            "PASSWORD": p["password"] or "",
            "HOST": p["host"],
            "PORT": str(p["port"]),
        }
    p = pg_parts(URL)
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": p["database"],
        "USER": p["user"],
        "PASSWORD": p["password"] or "",
        "HOST": p["host"],
        "PORT": str(p["port"]),
    }


try:
    import django
    from django.conf import settings as _dj_settings

    if not _dj_settings.configured:
        _dj_settings.configure(INSTALLED_APPS=[], USE_TZ=True, DATABASES={"default": _django_db()})
        django.setup()

    from django.db import connection as _dj_connection
    from django.db import models as _dj_models
    from django.db.models import Count as DjCount
    from django.db.models import Sum as DjSum

    class BDjango(_dj_models.Model):
        name = _dj_models.CharField(max_length=50)
        value = _dj_models.IntegerField()
        cat = _dj_models.IntegerField()
        created = _dj_models.DateTimeField(auto_now_add=True)

        class Meta:
            app_label = "bench"
            db_table = "bench_django"

except ImportError:  # pragma: no cover
    BDjango = None


def run_django() -> dict:
    if BDjango is None:
        raise RuntimeError("django is not installed")

    with _dj_connection.cursor() as cur:
        cur.execute(drop_sql("bench_django"))
    with _dj_connection.schema_editor() as se:
        se.create_model(BDjango)

    res: dict = {}

    objs = [BDjango(name=f"n{i}", value=i, cat=i % CATS) for i in range(N)]
    with _Stopwatch() as sw:
        BDjango.objects.bulk_create(objs)
    res["bulk_insert"] = sw.elapsed

    with _Stopwatch() as sw:
        agg = list(
            BDjango.objects.values("cat")
            .annotate(n=DjCount("id"), s=DjSum("value"))
            .filter(n__gt=TH)
            .order_by("cat")
        )
    res["group_by"] = sw.elapsed
    assert len(agg) == CATS

    with _Stopwatch() as sw:
        rows = list(BDjango.objects.all())
    res["fetch_all"] = sw.elapsed
    assert len(rows) == N

    with _Stopwatch() as sw:
        total = BDjango.objects.count()
    res["count"] = sw.elapsed
    assert total == N

    with _Stopwatch() as sw:
        rows = list(BDjango.objects.filter(value__gte=HALF))
    res["filter"] = sw.elapsed

    with _Stopwatch() as sw:
        for pk in PK_SEQUENCE:
            BDjango.objects.get(id=pk)
    res["get_by_pk"] = sw.elapsed

    with _Stopwatch() as sw:
        BDjango.objects.filter(value__lt=HALF).update(value=0)
    res["update"] = sw.elapsed

    with _Stopwatch() as sw:
        BDjango.objects.filter(value__gte=HALF).delete()
    res["delete"] = sw.elapsed

    with _dj_connection.cursor() as cur:
        cur.execute(clear_sql("bench_django"))
    with _Stopwatch() as sw:
        for i in range(S):
            BDjango.objects.create(name=f"s{i}", value=i, cat=i % CATS)
    res["single_insert"] = sw.elapsed

    _dj_connection.close()
    return res


# ---------------------------------------------------------------------------
# Peewee (synchronous)
# ---------------------------------------------------------------------------
try:
    import peewee as _pw

    _pw_db = _pw.DatabaseProxy()  # bound to a concrete database inside the runner

    class BPeewee(_pw.Model):
        name = _pw.CharField(max_length=50)
        value = _pw.IntegerField()
        cat = _pw.IntegerField()
        created = _pw.DateTimeField(null=True)

        class Meta:
            database = _pw_db
            table_name = "bench_peewee"

except ImportError:  # pragma: no cover
    BPeewee = None


def run_peewee() -> dict:
    if BPeewee is None:
        raise RuntimeError("peewee is not installed")

    if BACKEND == "sqlite":
        db = _pw.SqliteDatabase(f"{SQLITE_DIR}/bench_peewee.db")
    elif BACKEND in ("mysql", "mariadb"):
        p = mysql_parts()
        db = _pw.MySQLDatabase(
            p["database"],
            user=p["user"],
            password=p["password"] or "",
            host=p["host"],
            port=p["port"],
        )
    else:
        p = pg_parts(URL)
        db = _pw.PostgresqlDatabase(
            p["database"], user=p["user"], password=p["password"], host=p["host"], port=p["port"]
        )
    _pw_db.initialize(db)
    db.connect(reuse_if_open=True)
    db.execute_sql(drop_sql("bench_peewee"))
    db.create_tables([BPeewee])

    res: dict = {}

    # SQLite caps bound parameters per statement; batch so multi-row INSERTs fit.
    batch = 200 if BACKEND == "sqlite" else 900
    objs = [BPeewee(name=f"n{i}", value=i, cat=i % CATS, created=NOW_NAIVE) for i in range(N)]
    with _Stopwatch() as sw:
        with db.atomic():
            BPeewee.bulk_create(objs, batch_size=batch)
    res["bulk_insert"] = sw.elapsed

    with _Stopwatch() as sw:
        q = (
            BPeewee.select(
                BPeewee.cat,
                _pw.fn.COUNT(BPeewee.id).alias("n"),
                _pw.fn.SUM(BPeewee.value).alias("s"),
            )
            .group_by(BPeewee.cat)
            .having(_pw.fn.COUNT(BPeewee.id) > TH)
            .order_by(BPeewee.cat)
        )
        agg = list(q.dicts())
    res["group_by"] = sw.elapsed
    assert len(agg) == CATS

    with _Stopwatch() as sw:
        rows = list(BPeewee.select())
    res["fetch_all"] = sw.elapsed
    assert len(rows) == N

    with _Stopwatch() as sw:
        total = BPeewee.select().count()
    res["count"] = sw.elapsed
    assert total == N

    with _Stopwatch() as sw:
        rows = list(BPeewee.select().where(BPeewee.value >= HALF))
    res["filter"] = sw.elapsed

    with _Stopwatch() as sw:
        for pk in PK_SEQUENCE:
            BPeewee.get_by_id(pk)
    res["get_by_pk"] = sw.elapsed

    with _Stopwatch() as sw:
        BPeewee.update(value=0).where(BPeewee.value < HALF).execute()
    res["update"] = sw.elapsed

    with _Stopwatch() as sw:
        BPeewee.delete().where(BPeewee.value >= HALF).execute()
    res["delete"] = sw.elapsed

    db.execute_sql(clear_sql("bench_peewee"))
    with _Stopwatch() as sw:
        for i in range(S):
            BPeewee.create(name=f"s{i}", value=i, cat=i % CATS, created=NOW_NAIVE)
    res["single_insert"] = sw.elapsed

    db.close()
    return res


# ---------------------------------------------------------------------------
# SQLObject (synchronous)
# ---------------------------------------------------------------------------
# SQLObject keeps an in-process identity map; ``?cache=false`` disables it so
# every ``.get()`` hits the database, matching how the other ORMs are measured.
def sqlobject_uri() -> str:
    if BACKEND == "sqlite":
        return f"sqlite://{SQLITE_DIR}/bench_sqlobject.db?cache=false"
    if BACKEND in ("mysql", "mariadb"):
        p = mysql_parts()
        return f"mysql://{_pg_userinfo(p)}@{p['host']}:{p['port']}/{p['database']}?driver=pymysql&cache=false"
    p = pg_parts(URL)
    return f"postgres://{_pg_userinfo(p)}@{p['host']}:{p['port']}/{p['database']}?cache=false"


try:
    from sqlobject import DateTimeCol, IntCol, SQLObject, StringCol, connectionForURI
    from sqlobject import sqlhub as _sqlhub

    class BSQLObject(SQLObject):
        class sqlmeta:
            table = "bench_sqlobject"

        name = StringCol(length=50)
        value = IntCol()
        cat = IntCol()
        created = DateTimeCol(default=None)

except ImportError:  # pragma: no cover
    BSQLObject = None


def run_sqlobject() -> dict:
    if BSQLObject is None:
        raise RuntimeError("sqlobject is not installed")

    conn = connectionForURI(sqlobject_uri())
    _sqlhub.processConnection = conn
    BSQLObject.dropTable(ifExists=True)
    BSQLObject.createTable(ifNotExists=True)

    res: dict = {}

    # SQLObject has no bulk INSERT; the idiomatic path creates one row at a time.
    # A single transaction around the loop mirrors the other ORMs' one commit.
    trans = conn.transaction()
    with _Stopwatch() as sw:
        for i in range(N):
            BSQLObject(name=f"n{i}", value=i, cat=i % CATS, created=NOW_NAIVE, connection=trans)
        trans.commit(close=True)
    res["bulk_insert"] = sw.elapsed

    try:
        with _Stopwatch() as sw:
            agg = conn.queryAll(
                "SELECT cat, COUNT(id), SUM(value) FROM bench_sqlobject "
                f"GROUP BY cat HAVING COUNT(id) > {TH} ORDER BY cat"
            )
        res["group_by"] = sw.elapsed
        assert len(agg) == CATS
    except Exception:  # noqa: BLE001 - SQLObject has no ORM-level GROUP BY/HAVING
        pass

    with _Stopwatch() as sw:
        rows = list(BSQLObject.select())
    res["fetch_all"] = sw.elapsed
    assert len(rows) == N

    with _Stopwatch() as sw:
        total = BSQLObject.select().count()
    res["count"] = sw.elapsed
    assert total == N

    with _Stopwatch() as sw:
        rows = list(BSQLObject.select(BSQLObject.q.value >= HALF))
    res["filter"] = sw.elapsed

    with _Stopwatch() as sw:
        for pk in PK_SEQUENCE:
            BSQLObject.get(pk)
    res["get_by_pk"] = sw.elapsed

    # No ORM-level bulk UPDATE; issue the set-based statement directly.
    with _Stopwatch() as sw:
        conn.query(f"UPDATE bench_sqlobject SET value = 0 WHERE value < {HALF}")
    res["update"] = sw.elapsed

    with _Stopwatch() as sw:
        BSQLObject.deleteMany(BSQLObject.q.value >= HALF)
    res["delete"] = sw.elapsed

    conn.query(clear_sql("bench_sqlobject"))
    with _Stopwatch() as sw:
        for i in range(S):
            BSQLObject(name=f"s{i}", value=i, cat=i % CATS, created=NOW_NAIVE)
    res["single_insert"] = sw.elapsed

    conn.close()
    return res


# ---------------------------------------------------------------------------
# Ormar (async, SQLAlchemy core + asyncpg/aiomysql/aiosqlite)
# ---------------------------------------------------------------------------
def ormar_url() -> str:
    if BACKEND == "sqlite":
        return f"sqlite+aiosqlite:///{SQLITE_DIR}/bench_ormar.db"
    if BACKEND in ("mysql", "mariadb"):
        p = mysql_parts()
        return f"mysql+aiomysql://{_pg_userinfo(p)}@{p['host']}:{p['port']}/{p['database']}"
    p = pg_parts(URL)
    return f"postgresql+asyncpg://{_pg_userinfo(p)}@{p['host']}:{p['port']}/{p['database']}"


try:
    import ormar
    import sqlalchemy as _ormar_sa
    from ormar.databases.connection import DatabaseConnection

    _ormar_meta = _ormar_sa.MetaData()
    _ormar_db = DatabaseConnection(ormar_url())
    _ormar_config = ormar.OrmarConfig(metadata=_ormar_meta, database=_ormar_db)

    class BOrmar(ormar.Model):
        ormar_config = _ormar_config.copy(tablename="bench_ormar")

        id: int = ormar.Integer(primary_key=True)
        name: str = ormar.String(max_length=50)
        value: int = ormar.Integer()
        cat: int = ormar.Integer()
        created: datetime = ormar.DateTime(timezone=True, nullable=True)

except ImportError:  # pragma: no cover
    BOrmar = None


async def run_ormar() -> dict:
    if BOrmar is None:
        raise RuntimeError("ormar is not installed")

    await _ormar_db.connect()
    async with _ormar_db.engine.begin() as conn:
        await conn.run_sync(_ormar_meta.drop_all)
        await conn.run_sync(_ormar_meta.create_all)

    res: dict = {}

    objs = [BOrmar(name=f"n{i}", value=i, cat=i % CATS, created=NOW) for i in range(N)]
    with _Stopwatch() as sw:
        await BOrmar.objects.bulk_create(objs)
    res["bulk_insert"] = sw.elapsed

    # Ormar exposes no annotate/GROUP BY; group_by is left unset (reported "-").

    with _Stopwatch() as sw:
        rows = await BOrmar.objects.all()
    res["fetch_all"] = sw.elapsed
    assert len(rows) == N

    with _Stopwatch() as sw:
        total = await BOrmar.objects.count()
    res["count"] = sw.elapsed
    assert total == N

    with _Stopwatch() as sw:
        rows = await BOrmar.objects.filter(value__gte=HALF).all()
    res["filter"] = sw.elapsed

    with _Stopwatch() as sw:
        for pk in PK_SEQUENCE:
            await BOrmar.objects.get(id=pk)
    res["get_by_pk"] = sw.elapsed

    with _Stopwatch() as sw:
        await BOrmar.objects.filter(value__lt=HALF).update(value=0)
    res["update"] = sw.elapsed

    with _Stopwatch() as sw:
        await BOrmar.objects.filter(value__gte=HALF).delete()
    res["delete"] = sw.elapsed

    async with _ormar_db.engine.begin() as conn:
        await conn.exec_driver_sql(clear_sql("bench_ormar"))
    with _Stopwatch() as sw:
        for i in range(S):
            await BOrmar.objects.create(name=f"s{i}", value=i, cat=i % CATS, created=NOW)
    res["single_insert"] = sw.elapsed

    await _ormar_db.disconnect()
    return res


# ---------------------------------------------------------------------------
# Piccolo (async; no MySQL backend)
# ---------------------------------------------------------------------------
try:
    from piccolo.columns import Integer as PInteger
    from piccolo.columns import Timestamptz as PTimestamptz
    from piccolo.columns import Varchar as PVarchar
    from piccolo.query.functions.aggregate import Count as PCount
    from piccolo.query.functions.aggregate import Sum as PSum
    from piccolo.table import Table as PTable

    def _piccolo_engine():
        if BACKEND == "sqlite":
            from piccolo.engine.sqlite import SQLiteEngine

            return SQLiteEngine(path=f"{SQLITE_DIR}/bench_piccolo.sqlite")
        if BACKEND == "postgres":
            from piccolo.engine.postgres import PostgresEngine

            p = pg_parts(URL)
            cfg = {
                "user": p["user"],
                "host": p["host"],
                "port": p["port"],
                "database": p["database"],
            }
            if p["password"]:
                cfg["password"] = p["password"]
            return PostgresEngine(config=cfg)
        return None  # Piccolo has no MySQL backend

    _piccolo_db = _piccolo_engine()

    if _piccolo_db is not None:

        class BPiccolo(PTable, tablename="bench_piccolo", db=_piccolo_db):
            name = PVarchar(length=50)
            value = PInteger()
            cat = PInteger()
            created = PTimestamptz(null=True)

    else:
        BPiccolo = None

except ImportError:  # pragma: no cover
    BPiccolo = None


async def run_piccolo() -> dict:
    if BPiccolo is None:
        raise RuntimeError("piccolo is not installed or has no backend for this database")

    # Piccolo opens a fresh connection per query unless its pool is started; the
    # other async ORMs all pool/reuse connections, so start one for a fair fight
    # (SQLite is file-based and has no real pool, so only PostgreSQL pools here).
    pooled = BACKEND == "postgres"
    if pooled:
        await _piccolo_db.start_connection_pool()
    try:
        return await _run_piccolo_ops()
    finally:
        if pooled:
            await _piccolo_db.close_connection_pool()


async def _run_piccolo_ops() -> dict:
    await BPiccolo.alter().drop_table(if_exists=True).run()
    await BPiccolo.create_table(if_not_exists=True).run()

    res: dict = {}

    objs = [BPiccolo(name=f"n{i}", value=i, cat=i % CATS, created=NOW) for i in range(N)]
    with _Stopwatch() as sw:
        await BPiccolo.insert(*objs).run()
    res["bulk_insert"] = sw.elapsed

    # Piccolo supports GROUP BY + aggregates but no HAVING clause here; the even
    # row spread means every group passes anyway, so CATS groups still come back.
    with _Stopwatch() as sw:
        agg = await (
            BPiccolo.select(
                BPiccolo.cat, PCount().as_alias("n"), PSum(BPiccolo.value).as_alias("s")
            )
            .group_by(BPiccolo.cat)
            .order_by(BPiccolo.cat)
        )
    res["group_by"] = sw.elapsed
    assert len(agg) == CATS

    with _Stopwatch() as sw:
        rows = await BPiccolo.select()
    res["fetch_all"] = sw.elapsed
    assert len(rows) == N

    with _Stopwatch() as sw:
        total = await BPiccolo.count()
    res["count"] = sw.elapsed
    assert total == N

    with _Stopwatch() as sw:
        rows = await BPiccolo.select().where(BPiccolo.value >= HALF)
    res["filter"] = sw.elapsed

    with _Stopwatch() as sw:
        for pk in PK_SEQUENCE:
            await BPiccolo.objects().where(BPiccolo.id == pk).first().run()
    res["get_by_pk"] = sw.elapsed

    with _Stopwatch() as sw:
        await BPiccolo.update({BPiccolo.value: 0}).where(BPiccolo.value < HALF).run()
    res["update"] = sw.elapsed

    with _Stopwatch() as sw:
        await BPiccolo.delete().where(BPiccolo.value >= HALF).run()
    res["delete"] = sw.elapsed

    await BPiccolo.delete(force=True).run()
    with _Stopwatch() as sw:
        for i in range(S):
            await BPiccolo(name=f"s{i}", value=i, cat=i % CATS, created=NOW).save().run()
    res["single_insert"] = sw.elapsed

    return res


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
OPS = [
    ("bulk_insert", N, "rows"),
    ("single_insert", S, "rows"),
    ("fetch_all", N, "rows"),
    ("count", N, "rows"),
    ("group_by", CATS, "groups"),
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
    if BACKEND == "postgres":
        target = URL
    elif BACKEND in ("mysql", "mariadb"):
        target = mysql_family_url()
    elif BACKEND == "oracle":
        target = ORACLE_URL
    elif BACKEND == "mssql":
        target = MSSQL_URL
    else:
        target = f"sqlite ({SQLITE_DIR})"
    print(
        f"BACKEND={BACKEND}  target={target}  N={N}  S={S}  GETS={GETS}  REPEAT={REPEAT} (median)\n"
    )

    # (name, runner, is_async). "ours" runs first and unguarded; a failure there
    # means the benchmark itself is broken and should surface loudly. None of the
    # eight competitors ships an Oracle backend, so the Oracle run measures
    # yara-orm alone (its own numbers across operations, no comparison column).
    runners = [
        ("tortoise", run_tortoise, True),
        ("sqlalchemy", run_sqlalchemy, True),
        ("pony", run_pony, False),
        ("django", run_django, False),
        ("peewee", run_peewee, False),
        ("sqlobject", run_sqlobject, False),
        ("ormar", run_ormar, True),
        ("piccolo", run_piccolo, True),
    ]
    if BACKEND in ("oracle", "mssql"):
        runners = []

    results = {}
    print("running: yara-orm (ours) ...")
    results["ours"] = _median_runs(run_ours, True)
    for name, runner, is_async in runners:
        print(f"running: {name} ...")
        try:
            results[name] = _median_runs(runner, is_async)
        except Exception as exc:  # noqa: BLE001 - a missing/unsupported ORM shouldn't abort the suite
            print(f"  {name} failed: {exc!r}")
            results[name] = {}

    cols = ["ours", *(r[0] for r in runners)]
    competitors = [r[0] for r in runners]

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
        "\n  - Async (asyncpg/aiomysql/aiosqlite): yara-orm, Tortoise, SQLAlchemy, Ormar, Piccolo."
        "\n  - Sync: Pony, Django, Peewee, SQLObject."
        "\n  - SQLAlchemy get_by_pk uses a fresh session per lookup (no identity-map reuse);"
        "\n    SQLObject runs with cache=false so every .get() hits the database."
        "\n  - Pony opens a transaction per get and has no SQL-level bulk UPDATE,"
        "\n    so its update path mutates objects in a loop."
        "\n  - Ormar has no GROUP BY/annotate API and Piccolo no HAVING clause, so their"
        "\n    group_by cells are '-' / unfiltered respectively; Piccolo has no MySQL backend."
        "\n  - SQLObject has no bulk INSERT (one row per statement, wrapped in one transaction)."
    )


if __name__ == "__main__":
    main()

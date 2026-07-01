"""Hot-path profiler: attribute read/write time to each stage.

Splits the model read path into Rust cell-materialization
(``engine.fetch_rows``) vs Python hydration (``_from_db_rows``), and measures the
values_list, select_related and write (``to_db`` / ``_json_safe``) paths, so an
optimization can be aimed and re-measured. Warm (medians), matching bench.py.

Usage:
    ORM_TEST_DB=postgres://localhost/orm_demo python benchmarks/profile_hotpath.py
Env: PROF_N (rows), PROF_REPEAT (timed reps).
"""

from __future__ import annotations

import asyncio
import os
import statistics
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import IntEnum
from uuid import uuid4

from yara_orm import Model, YaraOrm, fields
from yara_orm.connection import get_dialect, get_executor

URL = os.environ.get("ORM_TEST_DB", "postgres://localhost/orm_demo")
N = int(os.environ.get("PROF_N", "5000"))
REPEAT = int(os.environ.get("PROF_REPEAT", "7"))


class Colour(IntEnum):
    RED = 1
    GREEN = 2


class Country(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=40)

    class Meta:
        table = "prof_country"


class Row(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=40)
    created = fields.DatetimeField()
    price = fields.DecimalField(max_digits=12, decimal_places=2)
    ref = fields.UUIDField()
    payload = fields.JSONField()
    colour = fields.IntEnumField(Colour)
    span = fields.TimeDeltaField()
    country = fields.ForeignKeyField("Country", related_name="rows")

    class Meta:
        table = "prof_row"


async def _median(label: str, fn, results: dict[str, float]) -> None:
    times = []
    for _ in range(REPEAT):
        t0 = time.perf_counter()
        await fn()
        times.append((time.perf_counter() - t0) * 1000)
    results[label] = statistics.median(times)


def _sync_median(label: str, fn, results: dict[str, float]) -> None:
    times = []
    for _ in range(REPEAT):
        t0 = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t0) * 1000)
    results[label] = statistics.median(times)


async def main() -> None:
    await YaraOrm.init(URL)
    for m in (Row, Country):
        await m.raw(f"DROP TABLE IF EXISTS {m._meta.table} CASCADE")
    await YaraOrm.generate_schemas()

    c = await Country.create(name="Wonderland")
    now = datetime.now(timezone.utc)
    await Row.bulk_create(
        [
            Row(
                name=f"row-{i}",
                created=now,
                price=Decimal("12.34"),
                ref=uuid4(),
                payload={"i": i, "tags": ["a", "b"], "nested": {"x": 1}},
                colour=Colour.RED if i % 2 else Colour.GREEN,
                span=timedelta(seconds=i),
                country=c,
            )
            for i in range(N)
        ]
    )

    dialect = get_dialect(Row)
    engine = get_executor(Row)
    qs = Row.all()
    sql, params, _ = qs._plain_select_sql(dialect)

    results: dict[str, float] = {}

    # -- read: split materialization vs hydration --------------------------
    async def fetch_only():
        await engine.fetch_rows(sql, params)

    await _median("fetch_rows (Rust decode+materialize)", fetch_only, results)

    rows = await engine.fetch_rows(sql, params)

    def hydrate_only():
        Row._from_db_rows(rows)

    _sync_median("_from_db_rows (Python hydrate)", hydrate_only, results)

    async def full_fetch():
        await Row.all()

    await _median("full Model.all() (fetch+hydrate)", full_fetch, results)

    async def values_list():
        await Row.all().values_list("id", "name", "price")

    await _median("values_list (no model build)", values_list, results)

    async def select_related():
        await Row.all().select_related("country")

    await _median("select_related('country')", select_related, results)

    # -- write: to_db / _json_safe bind cost --------------------------------
    json_field = Row._meta.get_field("payload")
    sample_payload = {"i": 1, "tags": ["a", "b"], "nested": {"x": 1, "u": uuid4()}}

    def json_safe_bind():
        for _ in range(N):
            json_field.to_db(sample_payload)

    _sync_median(f"JSONField.to_db x{N} (_json_safe walk)", json_safe_bind, results)

    price_field = Row._meta.get_field("price")

    def decimal_bind():
        for _ in range(N):
            price_field.to_db(Decimal("12.34"))

    _sync_median(f"DecimalField.to_db x{N}", decimal_bind, results)

    await YaraOrm.close()

    print(f"\nPROF  target={URL}  N={N}  REPEAT={REPEAT} (median ms)\n")
    width = max(len(k) for k in results)
    for label, ms in results.items():
        print(f"  {label:<{width}}  {ms:8.2f} ms")
    # Read-path split summary.
    f = results["fetch_rows (Rust decode+materialize)"]
    h = results["_from_db_rows (Python hydrate)"]
    total = f + h
    print(
        f"\n  read split: Rust materialize {f / total:.0%} | "
        f"Python hydrate {h / total:.0%}  (of fetch+hydrate)"
    )


if __name__ == "__main__":
    asyncio.run(main())

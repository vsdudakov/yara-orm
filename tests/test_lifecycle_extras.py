"""Lifecycle/tooling parity: get_schema_sql(), run_async(), and pool/cache
URL parameters (max_size / min_size / statement_cache_size)."""

import pytest

from yara_orm import Model, YaraOrm, fields, run_async


class LcThing(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20)

    class Meta:
        table = "lc_thing"


class LcTag(Model):
    id = fields.IntField(pk=True)
    things = fields.ManyToManyField("LcThing", related_name="tags", through="lc_thing_tag")

    class Meta:
        table = "lc_tag"


MODELS = [LcThing, LcTag]


@pytest.mark.asyncio
async def test_get_schema_sql(db):
    """
    GIVEN initialised models
    WHEN get_schema_sql() is called
    THEN it returns the CREATE TABLE / join-table DDL without executing it
    """
    lo, hi = (
        ("[", "]") if db == "mssql" else (("`", "`") if db in ("mysql", "mariadb") else ('"', '"'))
    )
    sql = YaraOrm.get_schema_sql()
    assert "CREATE TABLE" in sql
    assert f"{lo}lc_thing{hi}" in sql
    assert "lc_thing_tag" in sql  # m2m join table included
    assert sql.rstrip().endswith(";")


@pytest.mark.asyncio
async def test_get_schema_sql_subset(db):
    """
    GIVEN a subset of models
    WHEN get_schema_sql(models=[...]) is called
    THEN only those tables appear
    """
    lo, hi = (
        ("[", "]") if db == "mssql" else (("`", "`") if db in ("mysql", "mariadb") else ('"', '"'))
    )
    sql = YaraOrm.get_schema_sql(models=[LcThing])
    assert f"{lo}lc_thing{hi}" in sql
    assert f"{lo}lc_tag{hi}" not in sql


def test_run_async_runs_and_closes():
    """
    GIVEN a coroutine that initialises and uses the ORM
    WHEN run_async drives it
    THEN it completes and closes every connection afterwards
    """
    seen = {}

    async def main() -> None:
        await YaraOrm.init("sqlite://:memory:")
        await YaraOrm.generate_schemas(models=[LcThing])
        await LcThing.create(name="hi")
        seen["name"] = (await LcThing.first()).name

    run_async(main())
    assert seen["name"] == "hi"

    # Connections were closed by run_async's finally block.
    from yara_orm.connection import _CONNECTIONS

    assert _CONNECTIONS == {}


def test_run_async_closes_on_error():
    """
    GIVEN a coroutine that raises
    WHEN run_async drives it
    THEN the error propagates and connections are still closed
    """

    async def boom() -> None:
        await YaraOrm.init("sqlite://:memory:")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        run_async(boom())

    from yara_orm.connection import _CONNECTIONS

    assert _CONNECTIONS == {}


@pytest.mark.asyncio
async def test_pool_and_cache_url_params_accepted():
    """
    GIVEN a connection URL carrying pool/cache parameters
    WHEN the ORM connects and runs a query
    THEN the parameters are accepted (stripped before the driver) and work
    """
    await YaraOrm.init("sqlite://:memory:?max_size=4&statement_cache_size=0")
    try:
        await YaraOrm.generate_schemas(models=[LcThing])
        await LcThing.create(name="pooled")
        assert (await LcThing.first()).name == "pooled"
    finally:
        await YaraOrm.close()

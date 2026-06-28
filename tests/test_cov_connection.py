"""Coverage: connection routing, named connections, transactions, errors."""

import os
import tempfile

import pytest

from yara_orm import ConfigurationError, Model, YaraOrm, connections, fields, in_transaction
from yara_orm.dialects import BaseDialect, get_dialect, register_dialect


class CvStar(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "cov_star"


class CvPlanet(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "cov_planet"


class _Router:
    def db_for_read(self, model):
        return "second" if model.__name__ == "CvPlanet" else "default"

    def db_for_write(self, model):
        return self.db_for_read(model)


def test_get_engine_and_dialect_before_init():
    """
    GIVEN the ORM has not been initialised
    WHEN get_engine / get_dialect are called
    THEN both raise ConfigurationError
    """
    from yara_orm.connection import get_dialect as conn_get_dialect
    from yara_orm.connection import get_engine

    with pytest.raises(ConfigurationError):
        get_engine()
    with pytest.raises(ConfigurationError):
        conn_get_dialect()


@pytest.mark.asyncio
async def test_unsupported_url_rejected():
    """
    GIVEN an unsupported database URL scheme
    WHEN YaraOrm.init is called
    THEN the engine rejects it with a ValueError
    """
    with pytest.raises(ValueError):
        await YaraOrm.init("mysql://localhost/nope")


def test_dialect_registry():
    """
    GIVEN the dialect registry
    WHEN resolving an unknown name and registering a custom dialect
    THEN unknown names raise and registered ones resolve
    """
    with pytest.raises(ConfigurationError):
        get_dialect("nosuch")

    class MyDialect(BaseDialect):
        name = "mydb"

    register_dialect("mydb", MyDialect)
    assert isinstance(get_dialect("mydb"), MyDialect)


@pytest.mark.asyncio
async def test_router_directs_models_between_sqlite_files():
    """
    GIVEN two SQLite connections and a router
    WHEN models are created and read
    THEN each model routes to its configured connection
    """
    paths = []
    for _ in range(2):
        fd, p = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(p)
        paths.append(p)
    await YaraOrm.init(f"sqlite://{paths[0]}", router=_Router())
    await YaraOrm.add_connection("second", f"sqlite://{paths[1]}")
    try:
        await YaraOrm.generate_schemas()
        await CvStar.create(name="Sun")
        await CvPlanet.create(name="Earth")

        # Planets live only in the second database.
        default_planet = await connections.get("default").fetch_rows(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='cov_planet'"
        )
        assert default_planet[0][0] == 0
        assert (await connections.get("second").fetch_rows("SELECT count(*) FROM cov_planet"))[0][
            0
        ] == 1
        assert await CvPlanet.all().count() == 1
        assert await CvStar.all().count() == 1
    finally:
        await YaraOrm.close()
        for p in paths:
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(p + suffix):
                    os.remove(p + suffix)


@pytest.mark.asyncio
async def test_set_router_and_transaction_fetch_all(sqlite_db):
    """
    GIVEN an initialised ORM
    WHEN set_router is toggled and a transaction runs manual SQL
    THEN routing is configurable and the transaction wrapper serves fetch_all
    """
    YaraOrm.set_router(None)
    await CvStar.create(name="x")
    async with in_transaction():
        conn = connections.get("default")
        await conn.execute("INSERT INTO cov_star (name) VALUES ($1)", ["y"])
        rows = await conn.fetch_all("SELECT name FROM cov_star ORDER BY name")
        assert [r["name"] for r in rows] == ["x", "y"]
        assert (await conn.fetch_row("SELECT count(*) FROM cov_star"))[0] == 2


def test_column_type_unknown_kind():
    """
    GIVEN a field with an unmapped kind
    WHEN the dialect renders its column type
    THEN a ConfigurationError is raised
    """
    from yara_orm.dialects import PostgresDialect

    field = fields.Field()
    field.field_kind = "bogus"
    field.db_column = "x"
    with pytest.raises(ConfigurationError):
        PostgresDialect().column_type(field)

"""Two databases with a Router directing models to different connections."""

import os

import pytest

from yara_orm import Model, YaraOrm, connections, fields

DB1 = os.environ.get("ORM_TEST_DB", "postgres://sevad@localhost/orm_demo")
DB2 = os.environ.get("ORM_TEST_DB2", "postgres://sevad@localhost/orm_demo2")


class Star(Model):
    name = fields.CharField(max_length=50)

    class Meta:
        table = "x_star"


class Planet(Model):
    name = fields.CharField(max_length=50)

    class Meta:
        table = "x_planet"


class Router:
    """Send Planet to the 'second' connection; everything else to default."""

    def db_for_read(self, model):
        return "second" if model.__name__ == "Planet" else "default"

    def db_for_write(self, model):
        return self.db_for_read(model)


@pytest.mark.asyncio
async def test_router_directs_models_to_databases():
    """
    GIVEN two connections and a Router routing Planet to the second database
    WHEN Star and Planet rows are created through the ORM
    THEN each model's rows land in (and are read from) the routed database
    """
    await YaraOrm.init(DB1, router=Router())
    try:
        await YaraOrm.add_connection("second", DB2)
    except Exception:  # pragma: no cover - second DB unavailable
        await YaraOrm.close()
        pytest.skip("second test database not available")

    try:
        # Clean both databases.
        await connections.get("default").execute("DROP TABLE IF EXISTS x_star CASCADE")
        await connections.get("default").execute("DROP TABLE IF EXISTS x_planet CASCADE")
        await connections.get("second").execute("DROP TABLE IF EXISTS x_planet CASCADE")
        await connections.get("second").execute("DROP TABLE IF EXISTS x_star CASCADE")
        await YaraOrm.generate_schemas()

        await Star.create(name="Sun")
        await Planet.create(name="Earth")
        await Planet.create(name="Mars")

        # Planets live in the second database, not the default one.
        default_has_planet = await connections.get("default").fetch_rows(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = 'x_planet'"
        )
        assert default_has_planet[0][0] == 0

        second_planets = await connections.get("second").fetch_rows("SELECT count(*) FROM x_planet")
        assert second_planets[0][0] == 2

        # Reads route correctly through the ORM too.
        assert await Planet.all().count() == 2
        assert await Star.all().count() == 1
        assert sorted(p.name for p in await Planet.all()) == ["Earth", "Mars"]
    finally:
        await YaraOrm.close()

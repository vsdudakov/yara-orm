"""Migration system: makemigrations / upgrade / downgrade / history / sqlmigrate."""

import os
import tempfile

import pytest
import pytest_asyncio

from yara_orm import MigrationManager, Model, YaraOrm, fields, migrations
from yara_orm.connection import get_engine


class MgUser(Model):
    name = fields.CharField(max_length=100)

    class Meta:
        table = "mg_user"


class MgPost(Model):
    title = fields.CharField(max_length=200, index=True)
    author = fields.ForeignKeyField("MgUser", related_name="posts")

    class Meta:
        table = "mg_post"


MODELS = [MgUser, MgPost]


def _attach_field(model, name, field):
    """Simulate a model gaining a field (for diff-detection tests)."""
    field.model_field_name = name
    field.db_column = name
    meta = model._meta
    meta.fields[name] = field
    meta.field_list.append(field)
    meta.decoders.append((name, None if field.read_identity else field.to_python))
    meta._compiled_for = None


def _detach_field(model, name):
    meta = model._meta
    field = meta.fields.pop(name)
    meta.field_list.remove(field)
    meta.decoders = [(n, d) for (n, d) in meta.decoders if n != name]
    meta._compiled_for = None


async def _drop_all():
    engine = get_engine()
    for table in ("mg_post", "mg_user", "orm_migrations"):
        await engine.execute(f"DROP TABLE IF EXISTS {table} CASCADE")


def _manager(tmp):
    return MigrationManager(directory=str(tmp), app="mgtest", models=MODELS)


@pytest.mark.asyncio
async def test_makemigrations_initial_and_upgrade(orm, tmp_path):
    """
    GIVEN two models and no prior migrations
    WHEN makemigrations then upgrade run
    THEN an initial migration is created, tables are built and rows persist
    """
    await _drop_all()
    mgr = _manager(tmp_path)

    filename = mgr.make_migrations(name="initial")
    assert filename == "0001_initial.py"

    applied = await mgr.upgrade()
    assert applied == ["0001_initial"]

    user = await MgUser.create(name="Ada")
    await MgPost.create(title="Hello", author=user)
    assert await MgPost.all().count() == 1

    history = await mgr.history()
    assert [h["name"] for h in history] == ["0001_initial"]


@pytest.mark.asyncio
async def test_no_changes_returns_none(orm, tmp_path):
    """
    GIVEN an up-to-date migration set
    WHEN makemigrations runs again with no model changes
    THEN it reports no changes (returns None)
    """
    await _drop_all()
    mgr = _manager(tmp_path)
    mgr.make_migrations(name="initial")
    assert mgr.make_migrations() is None


@pytest.mark.asyncio
async def test_add_column_migration_and_downgrade(orm, tmp_path):
    """
    GIVEN an applied initial schema
    WHEN a new field is added to a model, then makemigrations + upgrade run
    THEN the column is added and usable; downgrade removes it again
    """
    await _drop_all()
    mgr = _manager(tmp_path)
    mgr.make_migrations(name="initial")
    await mgr.upgrade()

    _attach_field(MgUser, "age", fields.IntField(null=True))
    try:
        filename = mgr.make_migrations(name="add_age")
        assert filename == "0002_add_age.py"
        module = mgr._load_module(tmp_path / filename)
        assert any(isinstance(op, migrations.AddColumn) for op in module.operations)

        applied = await mgr.upgrade()
        assert applied == ["0002_add_age"]

        u = await MgUser.create(name="Bob", age=42)
        assert (await MgUser.get(id=u.id)).age == 42

        reverted = await mgr.downgrade(steps=1)
        assert reverted == ["0002_add_age"]
    finally:
        _detach_field(MgUser, "age")

    # Column is gone after downgrade.
    engine = get_engine()
    rows = await engine.fetch_rows(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name = 'mg_user' AND column_name = 'age'"
    )
    assert rows[0][0] == 0


@pytest.mark.asyncio
async def test_sqlmigrate_and_heads(orm, tmp_path):
    """
    GIVEN a generated migration
    WHEN sqlmigrate and heads are queried
    THEN the SQL is rendered without executing and head status is reported
    """
    await _drop_all()
    mgr = _manager(tmp_path)
    mgr.make_migrations(name="initial")

    sql = mgr.sqlmigrate("0001_initial")
    assert any("CREATE TABLE" in s and "mg_user" in s for s in sql)

    heads = await mgr.heads()
    assert heads == [{"name": "0001_initial", "applied": False}]
    await mgr.upgrade()
    heads = await mgr.heads()
    assert heads[0]["applied"] is True


@pytest_asyncio.fixture
async def sqlite_orm():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    await YaraOrm.init(f"sqlite://{path}")
    try:
        yield
    finally:
        await YaraOrm.close()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(path + suffix):
                os.remove(path + suffix)


@pytest.mark.asyncio
async def test_migrations_on_sqlite(sqlite_orm, tmp_path):
    """
    GIVEN the SQLite backend
    WHEN the same migration operations are applied and reverted
    THEN they render to SQLite DDL and run end-to-end
    """
    mgr = _manager(tmp_path)
    mgr.make_migrations(name="initial")
    assert await mgr.upgrade() == ["0001_initial"]

    user = await MgUser.create(name="Grace")
    await MgPost.create(title="On SQLite", author=user)
    assert await MgPost.all().count() == 1

    assert await mgr.downgrade(steps=1) == ["0001_initial"]
    assert await mgr.heads() == [{"name": "0001_initial", "applied": False}]

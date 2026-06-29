"""Migration system: operation rendering, makemigrations / upgrade / downgrade /
history / sqlmigrate, and the manager workflow."""

import os
import tempfile

import pytest
import pytest_asyncio

from yara_orm import MigrationManager, Model, YaraOrm, fields, migrations
from yara_orm import migrations as m
from yara_orm.connection import get_engine
from yara_orm.dialects import PostgresDialect, SqliteDialect

INT = {
    "kind": "int",
    "type_params": {},
    "null": False,
    "unique": False,
    "pk": False,
    "auto_increment": False,
}
PK = {
    "kind": "int",
    "type_params": {},
    "null": False,
    "unique": False,
    "pk": True,
    "auto_increment": True,
}
TABLE_SPEC = {"columns": {"id": PK, "n": INT}, "pk": "id", "fks": {}, "indexes": ["n"]}


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


# -- operation rendering / state --------------------------------------------
@pytest.mark.parametrize("dialect", [PostgresDialect(), SqliteDialect()])
def test_operation_rendering(dialect):
    """
    GIVEN one of every migration operation
    WHEN rendered forward and backward against a dialect
    THEN each produces SQL strings (covering both dialect type maps)
    """
    fk = {"table": "other", "pk": "id", "on_delete": "CASCADE"}
    ops = [
        m.CreateTable("t", columns={"id": PK, "n": INT}, pk="id", indexes=["n"]),
        m.CreateTable("j", columns={"a": INT, "b": INT}, composite_pk=["a", "b"]),
        m.DropTable("t", spec=TABLE_SPEC),
        m.AddColumn("t", "c", spec=INT, fk=fk),
        m.DropColumn("t", "c", spec=INT),
        m.CreateIndex("t", "n"),
        m.DropIndex("t", "n"),
        m.RunSQL("SELECT 1", reverse_sql="SELECT 2"),
        m.RunSQL(["A", "B"]),
        m.RunPython(None),
    ]
    for op in ops:
        assert all(isinstance(s, str) for s in op.forward_sql(dialect))
        assert all(isinstance(s, str) for s in op.backward_sql(dialect))


def test_operation_to_source_and_apply_state():
    """
    GIVEN migration operations
    WHEN serialised to source and applied to a schema state
    THEN to_source is valid text and the state evolves correctly
    """
    ops = [
        m.CreateTable("t", columns={"id": PK}, pk="id"),
        m.AddColumn("t", "c", spec=INT, fk={"table": "x", "pk": "id"}),
        m.CreateIndex("t", "c"),
        m.DropIndex("t", "c"),
        m.DropColumn("t", "c", spec=INT, fk={"table": "x", "pk": "id"}),
        m.DropTable("t", spec={"columns": {"id": PK}, "pk": "id"}),
        m.RunSQL("X"),
    ]
    for op in ops:
        assert isinstance(op.to_source(), str)

    state = {"tables": {}}
    m.CreateTable("t", columns={"id": PK}, pk="id").apply_state(state)
    m.AddColumn("t", "c", spec=INT, fk={"table": "x", "pk": "id"}).apply_state(state)
    assert "c" in state["tables"]["t"]["columns"]
    m.CreateIndex("t", "c").apply_state(state)
    m.CreateIndex("t", "c").apply_state(state)  # idempotent branch
    m.DropIndex("t", "c").apply_state(state)
    m.DropIndex("t", "missing").apply_state(state)  # absent index -> no-op
    m.DropColumn("t", "c", spec=INT).apply_state(state)
    assert "c" not in state["tables"]["t"]["columns"]
    m.DropTable("t", spec={}).apply_state(state)
    assert "t" not in state["tables"]
    m.RunSQL("X").apply_state(state)  # no-op


@pytest.mark.asyncio
async def test_run_python_callbacks():
    """
    GIVEN a RunPython operation with forward/backward callables
    WHEN run forward and backward (and with None callbacks)
    THEN the callables execute and None callbacks are a no-op
    """
    calls = []

    async def fwd():
        calls.append("f")

    async def bwd():
        calls.append("b")

    op = m.RunPython(fwd, bwd)
    await op.run_forward()
    await op.run_backward()
    assert calls == ["f", "b"]
    await m.RunPython(None).run_forward()
    await m.RunPython(None).run_backward()


def test_file_number_rejects_bad_name():
    """
    GIVEN a non-migration file name
    WHEN _file_number parses it
    THEN it raises ValueError
    """
    with pytest.raises(ValueError):
        m._file_number("not_a_migration.txt")


# -- manager workflow -------------------------------------------------------
class CvMigA(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "cov_mig_a"


class CvMigB(Model):
    id = fields.IntField(pk=True)
    label = fields.CharField(max_length=50)

    class Meta:
        table = "cov_mig_b"


@pytest.mark.asyncio
async def test_manager_workflow_drop_table_empty_and_sql(sqlite_empty, tmp_path):
    """
    GIVEN two models with an applied initial migration
    WHEN a model is removed, an empty migration is made, and SQL is previewed
    THEN drop/recreate, empty generation, sqlmigrate (both ways) and heads work
    """
    mgr = MigrationManager(directory=str(tmp_path), app="cov", models=[CvMigA, CvMigB])
    assert mgr.make_migrations(name="initial") == "0001_initial.py"
    await mgr.upgrade()

    # sqlmigrate both directions.
    assert any("CREATE TABLE" in s for s in mgr.sqlmigrate("0001_initial"))
    assert any("DROP TABLE" in s for s in mgr.sqlmigrate("0001_initial", backward=True))

    # An explicitly empty migration is written with no operations.
    empty = mgr.make_migrations(name="manual", empty=True)
    assert empty == "0002_manual.py"
    await mgr.upgrade()

    # Removing CvMigB from the model set yields a DropTable migration.
    mgr2 = MigrationManager(directory=str(tmp_path), app="cov", models=[CvMigA])
    name = mgr2.make_migrations(name="drop_b")
    module = mgr2._load_module(tmp_path / name)
    assert any(isinstance(op, m.DropTable) for op in module.operations)
    await mgr2.upgrade()

    heads = await mgr2.heads()
    assert all(h["applied"] for h in heads)
    hist = await mgr2.history()
    assert [h["name"] for h in hist][0] == "0001_initial"

    # Downgrade the drop, recreating CvMigB.
    assert await mgr2.downgrade(steps=1) == [name.removesuffix(".py")]


@pytest.mark.asyncio
async def test_manager_no_directory_and_run_sql_file(sqlite_empty, tmp_path):
    """
    GIVEN a hand-written migration using RunSQL
    WHEN it is applied and reverted
    THEN the raw SQL runs in both directions
    """
    mgr = MigrationManager(directory=str(tmp_path), app="cov2", models=[CvMigA])
    # No files yet -> _migration_files returns [] for a missing dir.
    empty_mgr = MigrationManager(directory=str(tmp_path / "missing"), app="cov3")
    assert empty_mgr._migration_files() == []

    mgr.make_migrations(name="initial")
    await mgr.upgrade()
    (tmp_path / "0002_data.py").write_text(
        "from yara_orm import migrations as m\n\n"
        "dependencies = ['0001_initial']\n\n"
        "operations = [\n"
        "    m.RunSQL(\"INSERT INTO cov_mig_a (name) VALUES ('seeded')\",\n"
        "             reverse_sql=\"DELETE FROM cov_mig_a WHERE name='seeded'\"),\n"
        "]\n"
    )
    await mgr.upgrade()
    assert await CvMigA.filter(name="seeded").exists() is True
    await mgr.downgrade(steps=1)
    assert await CvMigA.filter(name="seeded").exists() is False


class CvMigUser(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "cov_mig_user"


class CvMigGroup(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)
    members = fields.ManyToManyField("CvMigUser", related_name="groups", through="cov_mig_members")

    class Meta:
        table = "cov_mig_group"


def _attach(model, name, field):
    """Simulate a model gaining a field (for diff-detection tests).

    Args:
        model: The model class to mutate.
        name: The new field's attribute/column name.
        field: The field instance to attach.

    Returns:
        None
    """
    field.model_field_name = name
    field.db_column = name
    meta = model._meta
    meta.fields[name] = field
    meta.field_list.append(field)
    meta.decoders.append((name, None if field.read_identity else field.to_python))
    meta._compiled_for = None


def _detach(model, name):
    """Remove a field previously attached with :func:`_attach`.

    Args:
        model: The model class to mutate.
        name: The field name to remove.

    Returns:
        None
    """
    meta = model._meta
    field = meta.fields.pop(name)
    meta.field_list.remove(field)
    meta.decoders = [(n, d) for (n, d) in meta.decoders if n != name]
    meta._compiled_for = None


@pytest.mark.asyncio
async def test_m2m_state_downgrade_target_and_drop_column(sqlite_empty, tmp_path):
    """
    GIVEN models including a many-to-many relation
    WHEN migrations build the join table, drop a column, and downgrade to a target
    THEN m2m state, drop-column diff and target downgrade all work
    """
    models = [CvMigUser, CvMigGroup]
    mgr = MigrationManager(directory=str(tmp_path), app="cm", models=models)
    mgr.make_migrations(name="initial")
    await mgr.upgrade()
    # The join table was created from the m2m schema state.
    rows = await CvMigGroup.all()  # table exists
    assert rows == []

    # Drop a column: add then remove an indexed field, generating
    # AddColumn/CreateIndex then DropColumn/DropIndex diffs.
    _attach(CvMigUser, "nick", fields.CharField(max_length=20, null=True, index=True))
    try:
        mgr.make_migrations(name="add_nick")
        await mgr.upgrade()
        _detach(CvMigUser, "nick")
        drop_name = mgr.make_migrations(name="drop_nick")
        module = mgr._load_module(tmp_path / drop_name)
        assert any(isinstance(op, m.DropColumn) for op in module.operations)
        await mgr.upgrade()
    finally:
        if "nick" in CvMigUser._meta.fields:
            _detach(CvMigUser, "nick")

    # Downgrade everything applied after 0001 back to the initial migration.
    reverted = await mgr.downgrade(target="0001_initial")
    assert reverted  # reverted the later migrations, kept the initial one
    heads = {h["name"]: h["applied"] for h in await mgr.heads()}
    assert heads["0001_initial"] is True

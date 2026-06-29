"""Migration system: class-based operations, makemigrations / upgrade /
downgrade / history / sqlmigrate, AlterField detection, idempotent and
concurrent operations, and the manager workflow."""

import os
import tempfile

import pytest
import pytest_asyncio

from yara_orm import MigrationManager, Model, YaraOrm, fields, migrations
from yara_orm import migrations as m
from yara_orm.connection import get_engine
from yara_orm.dialects import PostgresDialect, SqliteDialect


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


def _replace_field(model, name, field):
    """Swap an existing field for a new one, preserving its position."""
    _detach_field(model, name)
    _attach_field(model, name, field)


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
    GIVEN an up-to-date migration set (makemigrations replays into the same state)
    WHEN makemigrations runs again with no model changes
    THEN it reports no changes (returns None) — the idempotence invariant
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
        assert any(isinstance(op, migrations.AddField) for op in module.Migration.operations)

        applied = await mgr.upgrade()
        assert applied == ["0002_add_age"]

        u = await MgUser.create(name="Bob", age=42)
        assert (await MgUser.get(id=u.id)).age == 42

        reverted = await mgr.downgrade(steps=1)
        assert reverted == ["0002_add_age"]
    finally:
        _detach_field(MgUser, "age")

    engine = get_engine()
    rows = await engine.fetch_rows(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name = 'mg_user' AND column_name = 'age'"
    )
    assert rows[0][0] == 0


@pytest.mark.asyncio
async def test_alter_field_migration_postgres(orm, tmp_path):
    """
    GIVEN an applied column of one type
    WHEN the field's type changes and makemigrations + upgrade run
    THEN an AlterField is generated and the column type is altered in place
    """
    await _drop_all()
    mgr = _manager(tmp_path)
    _attach_field(MgUser, "bio", fields.CharField(max_length=50, null=True))
    try:
        mgr.make_migrations(name="initial")
        await mgr.upgrade()

        _replace_field(MgUser, "bio", fields.TextField(null=True))
        filename = mgr.make_migrations(name="widen_bio")
        module = mgr._load_module(tmp_path / filename)
        assert any(isinstance(op, migrations.AlterField) for op in module.Migration.operations)
        await mgr.upgrade()

        engine = get_engine()
        rows = await engine.fetch_rows(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'mg_user' AND column_name = 'bio'"
        )
        assert rows[0][0] == "text"

        # Reverse restores the original varchar type.
        await mgr.downgrade(steps=1)
        rows = await engine.fetch_rows(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'mg_user' AND column_name = 'bio'"
        )
        assert rows[0][0] == "character varying"
    finally:
        _detach_field(MgUser, "bio")


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


@pytest.mark.asyncio
async def test_alter_field_on_sqlite_rebuilds_table(sqlite_orm, tmp_path):
    """
    GIVEN an applied column on SQLite
    WHEN the field's type changes and the migration is applied
    THEN SQLite rebuilds the table and the data survives the change
    """
    mgr = _manager(tmp_path)
    _attach_field(MgUser, "score", fields.IntField(null=True))
    try:
        mgr.make_migrations(name="initial")
        await mgr.upgrade()
        await MgUser.create(name="Edsger", score=7)

        _replace_field(MgUser, "score", fields.BigIntField(null=True))
        mgr.make_migrations(name="widen_score")
        await mgr.upgrade()

        # The row survived the table rebuild.
        assert (await MgUser.get(name="Edsger")).score == 7
        assert await mgr.downgrade(steps=1) == ["0002_widen_score"]
    finally:
        _detach_field(MgUser, "score")


@pytest.mark.asyncio
async def test_non_atomic_concurrent_index_migration(sqlite_orm, tmp_path):
    """
    GIVEN a hand-written non-atomic migration using a concurrent index op
    WHEN it is applied and reverted
    THEN the index is created and dropped outside a transaction
    """
    mgr = _manager(tmp_path)
    mgr.make_migrations(name="initial")
    await mgr.upgrade()

    (tmp_path / "0002_idx.py").write_text(
        "from yara_orm import migrations as m\n\n\n"
        "class Migration(m.Migration):\n"
        "    atomic = False\n"
        "    dependencies = ['0001_initial']\n"
        "    operations = [\n"
        "        m.AddIndexConcurrently('mg_user', 'name'),\n"
        "    ]\n"
    )
    assert await mgr.upgrade() == ["0002_idx"]
    # sqlmigrate shows the non-atomic op rendered for SQLite (no CONCURRENTLY).
    assert any("CREATE INDEX" in s for s in mgr.sqlmigrate("0002_idx"))
    assert await mgr.downgrade(steps=1) == ["0002_idx"]


# -- operation rendering / state --------------------------------------------
def _scalar_state():
    """A schema state with one table carrying a pk and a scalar column."""
    return {
        "tables": {
            "t": {
                "fields": {"id": fields.IntField(pk=True), "n": fields.IntField()},
                "composite_pk": None,
                "indexes": [],
            }
        }
    }


@pytest.mark.parametrize("dialect", [PostgresDialect(), SqliteDialect()])
def test_operation_rendering(dialect):
    """
    GIVEN one of every migration operation
    WHEN rendered forward and backward against a dialect
    THEN each produces SQL strings (covering both dialect type maps)
    """
    state = _scalar_state()
    ops = [
        m.CreateModelIfNotExists(
            "t", fields={"id": fields.IntField(pk=True), "n": fields.IntField(index=True)}
        ),
        m.CreateModel(
            "j",
            fields={
                "a_id": fields.ForeignKeyField("MgUser"),
                "b_id": fields.ForeignKeyField("MgUser"),
            },
            composite_pk=["a_id", "b_id"],
        ),
        m.DeleteModelIfExists("t", fields={"id": fields.IntField(pk=True)}),
        m.AddFieldIfNotExists("t", "c", fields.ForeignKeyField("MgUser")),
        m.RemoveFieldIfExists("t", "c", fields.IntField()),
        m.AlterField("t", "n", fields.BigIntField(null=True), old=fields.IntField()),
        m.AddIndex("t", "n"),
        m.AddIndexConcurrently("t", "n"),
        m.AddUniqueIndexConcurrently("t", "n"),
        m.RemoveIndex("t", "n"),
        m.RemoveIndexConcurrently("t", "n"),
        m.RunSQL("SELECT 1", reverse_sql="SELECT 2"),
        m.RunSQL(["A", "B"]),
        m.RunPython(None),
    ]
    for op in ops:
        assert all(isinstance(s, str) for s in op.forward_sql(dialect, state))
        assert all(isinstance(s, str) for s in op.backward_sql(dialect, state))


def test_operation_apply_and_revert_state():
    """
    GIVEN migration operations
    WHEN applied to and reverted from a schema state
    THEN the state evolves and unwinds correctly in both directions
    """
    state = {"tables": {}}
    m.CreateModel("t", fields={"id": fields.IntField(pk=True)}).apply_state(state)
    assert "t" in state["tables"]

    m.AddField("t", "c", fields.IntField()).apply_state(state)
    assert "c" in state["tables"]["t"]["fields"]

    m.AddIndex("t", "c").apply_state(state)
    m.AddIndex("t", "c").apply_state(state)  # idempotent branch
    assert state["tables"]["t"]["indexes"] == ["c"]
    m.RemoveIndex("t", "c").revert_state(state)  # already present -> exit branch
    assert state["tables"]["t"]["indexes"] == ["c"]
    m.RemoveIndex("t", "c").apply_state(state)
    m.RemoveIndex("t", "missing").apply_state(state)  # absent index -> no-op
    assert state["tables"]["t"]["indexes"] == []

    alter = m.AlterField("t", "c", fields.BigIntField(), old=fields.IntField())
    alter.apply_state(state)
    assert isinstance(state["tables"]["t"]["fields"]["c"], fields.BigIntField)
    alter.revert_state(state)
    assert isinstance(state["tables"]["t"]["fields"]["c"], fields.IntField)

    m.RemoveField("t", "c", fields.IntField()).apply_state(state)
    assert "c" not in state["tables"]["t"]["fields"]
    m.RemoveField("t", "c", fields.IntField()).revert_state(state)
    assert "c" in state["tables"]["t"]["fields"]

    # Reverse-side state evolution.
    m.AddIndex("t", "c").revert_state(state)  # remove when present
    m.RemoveIndex("t", "c").revert_state(state)  # add back
    assert state["tables"]["t"]["indexes"] == ["c"]

    m.DeleteModel("t", fields={"id": fields.IntField(pk=True)}).apply_state(state)
    assert "t" not in state["tables"]
    m.DeleteModel("t", fields={"id": fields.IntField(pk=True)}).revert_state(state)
    assert "t" in state["tables"]

    m.CreateModel("t", fields={"id": fields.IntField(pk=True)}).revert_state(state)
    assert "t" not in state["tables"]

    # No-op state hooks.
    m.RunSQL("X").apply_state(state)
    m.RunPython(None).revert_state(state)


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


def test_sqlmigrate_unknown_name_raises(tmp_path):
    """
    GIVEN a manager with no matching migration
    WHEN sqlmigrate is asked for an unknown name
    THEN it raises KeyError
    """
    mgr = MigrationManager(directory=str(tmp_path), app="x", models=MODELS)
    mgr.make_migrations(name="initial")
    with pytest.raises(KeyError):
        mgr.sqlmigrate("9999_nope")


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

    assert any("CREATE TABLE" in s for s in mgr.sqlmigrate("0001_initial"))
    assert any("DROP TABLE" in s for s in mgr.sqlmigrate("0001_initial", backward=True))

    empty = mgr.make_migrations(name="manual", empty=True)
    assert empty == "0002_manual.py"
    await mgr.upgrade()

    mgr2 = MigrationManager(directory=str(tmp_path), app="cov", models=[CvMigA])
    name = mgr2.make_migrations(name="drop_b")
    module = mgr2._load_module(tmp_path / name)
    assert any(isinstance(op, m.DeleteModel) for op in module.Migration.operations)
    await mgr2.upgrade()

    heads = await mgr2.heads()
    assert all(h["applied"] for h in heads)
    hist = await mgr2.history()
    assert [h["name"] for h in hist][0] == "0001_initial"

    assert await mgr2.downgrade(steps=1) == [name.removesuffix(".py")]


@pytest.mark.asyncio
async def test_manager_no_directory_and_run_sql_file(sqlite_empty, tmp_path):
    """
    GIVEN a hand-written migration using RunSQL
    WHEN it is applied and reverted
    THEN the raw SQL runs in both directions
    """
    mgr = MigrationManager(directory=str(tmp_path), app="cov2", models=[CvMigA])
    empty_mgr = MigrationManager(directory=str(tmp_path / "missing"), app="cov3")
    assert empty_mgr._migration_files() == []

    mgr.make_migrations(name="initial")
    await mgr.upgrade()
    (tmp_path / "0002_data.py").write_text(
        "from yara_orm import migrations as m\n\n\n"
        "class Migration(m.Migration):\n"
        "    dependencies = ['0001_initial']\n"
        "    operations = [\n"
        "        m.RunSQL(\"INSERT INTO cov_mig_a (name) VALUES ('seeded')\",\n"
        "                 reverse_sql=\"DELETE FROM cov_mig_a WHERE name='seeded'\"),\n"
        "    ]\n"
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
    assert await CvMigGroup.all() == []  # join table exists

    _attach_field(CvMigUser, "nick", fields.CharField(max_length=20, null=True, index=True))
    try:
        mgr.make_migrations(name="add_nick")
        await mgr.upgrade()
        _detach_field(CvMigUser, "nick")
        drop_name = mgr.make_migrations(name="drop_nick")
        module = mgr._load_module(tmp_path / drop_name)
        ops = module.Migration.operations
        assert any(isinstance(op, m.RemoveField) for op in ops)
        assert any(isinstance(op, m.RemoveIndex) for op in ops)
        await mgr.upgrade()
    finally:
        if "nick" in CvMigUser._meta.fields:
            _detach_field(CvMigUser, "nick")

    reverted = await mgr.downgrade(target="0001_initial")
    assert reverted
    heads = {h["name"]: h["applied"] for h in await mgr.heads()}
    assert heads["0001_initial"] is True

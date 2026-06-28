"""Coverage: migration operations, rendering, and manager workflow."""

import pytest

from yara_orm import MigrationManager, Model, fields
from yara_orm import migrations as m
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
    field.model_field_name = name
    field.db_column = name
    meta = model._meta
    meta.fields[name] = field
    meta.field_list.append(field)
    meta.decoders.append((name, None if field.read_identity else field.to_python))
    meta._compiled_for = None


def _detach(model, name):
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

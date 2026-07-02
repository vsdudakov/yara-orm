"""Corner-case migration tests: diff detection edges (nullability / max_length /
add+drop index in one diff), full upgrade->downgrade state round-trips,
idempotent re-upgrade, empty migrations, RunSQL list forms, and the
constraint-op rejection on SQLite.

These complement test_migrations.py / test_migration_generation.py /
test_migration_source.py by hammering the *gaps* rather than the happy path.
Pure diff/state/source tests need no backend; the end-to-end ones use a
throwaway SQLite database.
"""

import os
import tempfile

import pytest
import pytest_asyncio

from yara_orm import MigrationManager, Model, YaraOrm, fields
from yara_orm import migrations as m
from yara_orm.connection import get_engine
from yara_orm.exceptions import UnSupportedError
from yara_orm.migrations import diff_states, model_state


# --- model-mutation helpers (mirror those in test_migrations.py) -------------
def _attach_field(model, name, field):
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
    _detach_field(model, name)
    _attach_field(model, name, field)


def _op_names(ops):
    return [type(o).__name__ for o in ops]


@pytest_asyncio.fixture
async def sqlite_orm():
    """A fresh throwaway SQLite database with no tables created."""
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


class MtMigThing(Model):
    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=50)
    size = fields.IntField(default=0)

    class Meta:
        table = "mt_mig_thing"


def _state():
    return model_state([MtMigThing])


# --- diff-detection corner cases --------------------------------------------
def test_nullability_change_emits_alterfield():
    """
    GIVEN a column whose only change is null=False -> null=True
    WHEN the state diff is computed
    THEN a single AlterField is emitted (nullability is an alterable change)
    """
    before = _state()
    _replace_field(MtMigThing, "size", fields.IntField(default=0, null=True))
    try:
        ops = diff_states(before, _state())
        assert _op_names(ops) == ["AlterField"]
        assert ops[0].name == "size"
    finally:
        _replace_field(MtMigThing, "size", fields.IntField(default=0))


def test_max_length_change_emits_alterfield():
    """
    GIVEN a CharField whose max_length widens (varchar(50) -> varchar(200))
    WHEN the state diff is computed
    THEN it is an AlterField (the column type_params changed)
    """
    before = _state()
    _replace_field(MtMigThing, "title", fields.CharField(max_length=200))
    try:
        ops = diff_states(before, _state())
        assert _op_names(ops) == ["AlterField"]
    finally:
        _replace_field(MtMigThing, "title", fields.CharField(max_length=50))


def test_alterfield_reverse_diff_restores_prior_type():
    """
    GIVEN a widened column and the reverse diff (new -> old)
    WHEN the reverse diff is computed
    THEN it emits an AlterField carrying the original field type back
    """
    before = _state()
    _replace_field(MtMigThing, "size", fields.BigIntField(default=0))
    try:
        after = _state()
        reverse = diff_states(after, before)
        assert _op_names(reverse) == ["AlterField"]
        assert isinstance(reverse[0].field, fields.IntField)
    finally:
        _replace_field(MtMigThing, "size", fields.IntField(default=0))


def test_add_one_index_and_drop_another_in_single_diff():
    """
    GIVEN one field losing index=True while another field gains it, in one step
    WHEN the state diff is computed
    THEN both a RemoveIndexIfExists and an AddIndexIfNotExists are emitted
    """
    before_field = fields.CharField(max_length=50, index=True)
    _replace_field(MtMigThing, "title", before_field)
    before = _state()
    _replace_field(MtMigThing, "title", fields.CharField(max_length=50))  # drop title idx
    _replace_field(MtMigThing, "size", fields.IntField(default=0, index=True))  # add size idx
    try:
        ops = set(_op_names(diff_states(before, _state())))
        assert {"AddIndexIfNotExists", "RemoveIndexIfExists"} <= ops
    finally:
        _replace_field(MtMigThing, "title", fields.CharField(max_length=50))
        _replace_field(MtMigThing, "size", fields.IntField(default=0))


def test_identical_state_diffs_to_nothing():
    """
    GIVEN a model diffed against a byte-identical replay of its own state
    WHEN diff_states runs
    THEN no operations are produced (the no-change invariant)
    """
    target = _state()
    recorded = {"tables": {}}
    for op in diff_states({"tables": {}}, target):
        op.apply_state(recorded)
    assert diff_states(recorded, _state()) == []


# --- pure state round-trips (forward apply then reverse) ---------------------
def test_rename_model_and_field_combined_state_roundtrip():
    """
    GIVEN a rename-field followed by a rename-model applied to a state
    WHEN both are reverted in reverse order
    THEN the state returns exactly to where it started
    """
    state = {"tables": {}}
    m.CreateModel("t", fields={"id": fields.IntField(pk=True), "n": fields.IntField()}).apply_state(
        state
    )
    snapshot = set(state["tables"]["t"]["fields"])

    rf = m.RenameField("t", "n", "num")
    rm = m.RenameModel("t", "t2")
    rf.apply_state(state)
    rm.apply_state(state)
    assert "t2" in state["tables"] and "num" in state["tables"]["t2"]["fields"]

    rm.revert_state(state)
    rf.revert_state(state)
    assert "t" in state["tables"]
    assert set(state["tables"]["t"]["fields"]) == snapshot


def test_composite_pk_create_delete_state_roundtrip():
    """
    GIVEN a CreateModel with a composite pk and its inverse DeleteModel
    WHEN create is applied then reverted
    THEN the join table appears and disappears, pk preserved through the trip
    """
    state = {"tables": {}}
    op = m.CreateModel(
        "j",
        fields={"a_id": fields.ForeignKeyField("A"), "b_id": fields.ForeignKeyField("B")},
        composite_pk=["a_id", "b_id"],
    )
    op.apply_state(state)
    assert state["tables"]["j"]["composite_pk"] == ["a_id", "b_id"]
    op.revert_state(state)
    assert "j" not in state["tables"]


# --- end-to-end on SQLite ---------------------------------------------------
class MtCovA(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "mt_cov_a"


def _mgr(tmp_path, models=(MtCovA,)):
    return MigrationManager(directory=str(tmp_path), app="mtcov", models=list(models))


@pytest.mark.asyncio
async def test_second_upgrade_is_noop(sqlite_orm, tmp_path):
    """
    GIVEN an already-applied migration set
    WHEN upgrade() is called again with nothing pending
    THEN it applies no further migrations (returns an empty list)
    """
    mgr = _mgr(tmp_path)
    mgr.make_migrations(name="initial")
    assert await mgr.upgrade() == ["0001_initial"]
    assert await mgr.upgrade() == []


@pytest.mark.asyncio
async def test_upgrade_then_full_downgrade_returns_to_prior_state(sqlite_orm, tmp_path):
    """
    GIVEN an applied initial schema plus an add-column migration
    WHEN both are applied then fully downgraded
    THEN the added column is gone and the base table is back to its prior shape
    """
    mgr = _mgr(tmp_path)
    mgr.make_migrations(name="initial")
    await mgr.upgrade()

    _attach_field(MtCovA, "extra", fields.IntField(null=True))
    try:
        mgr.make_migrations(name="add_extra")
        await mgr.upgrade()
        await MtCovA.create(name="x", extra=5)
    finally:
        _detach_field(MtCovA, "extra")

    # Downgrade both steps; the extra column must be gone.
    assert await mgr.downgrade(steps=1) == ["0002_add_extra"]
    engine = get_engine()
    rows = await engine.fetch_rows("PRAGMA table_info(mt_cov_a)")
    cols = {r[1] for r in rows}
    assert "extra" not in cols
    assert {"id", "name"} <= cols

    # And the base migration reverses cleanly too (table dropped).
    assert await mgr.downgrade(steps=1) == ["0001_initial"]
    rows = await engine.fetch_rows(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='mt_cov_a'"
    )
    assert rows == []


@pytest.mark.asyncio
async def test_empty_migration_has_no_operations_and_applies(sqlite_orm, tmp_path):
    """
    GIVEN an empty (hand-editable) migration
    WHEN it is generated, applied and reverted
    THEN it carries zero operations and is a no-op in both directions
    """
    mgr = _mgr(tmp_path)
    mgr.make_migrations(name="initial")
    await mgr.upgrade()

    empty = mgr.make_migrations(name="manual", empty=True)
    assert empty == "0002_manual.py"
    module = mgr._load_module(tmp_path / empty)
    assert module.Migration.operations == []

    assert await mgr.upgrade() == ["0002_manual"]
    assert await mgr.downgrade(steps=1) == ["0002_manual"]


@pytest.mark.asyncio
async def test_runsql_list_forward_and_reverse_execute(sqlite_orm, tmp_path):
    """
    GIVEN a RunSQL op built from *lists* of forward and reverse statements
    WHEN the migration is applied and reverted
    THEN each forward statement runs, and downgrade runs each reverse statement
    """
    mgr = _mgr(tmp_path)
    mgr.make_migrations(name="initial")
    await mgr.upgrade()

    (tmp_path / "0002_seed.py").write_text(
        "from yara_orm import migrations as m\n\n\n"
        "class Migration(m.Migration):\n"
        "    dependencies = ['0001_initial']\n"
        "    operations = [\n"
        "        m.RunSQL(\n"
        "            [\"INSERT INTO mt_cov_a (name) VALUES ('a')\",\n"
        "             \"INSERT INTO mt_cov_a (name) VALUES ('b')\"],\n"
        "            reverse_sql=[\"DELETE FROM mt_cov_a WHERE name='a'\",\n"
        "                         \"DELETE FROM mt_cov_a WHERE name='b'\"],\n"
        "        ),\n"
        "    ]\n"
    )
    await mgr.upgrade()
    assert await MtCovA.all().count() == 2

    await mgr.downgrade(steps=1)
    assert await MtCovA.all().count() == 0


@pytest.mark.asyncio
async def test_add_constraint_migration_rebuilds_on_sqlite(sqlite_orm, tmp_path):
    """
    GIVEN a migration adding a table-level constraint
    WHEN it is applied on SQLite (no ALTER ... ADD CONSTRAINT; the operation
         routes through the table rebuild instead)
    THEN the constraint is enforced, existing rows survive, and downgrade
         removes it again
    """
    from yara_orm import IntegrityError

    mgr = _mgr(tmp_path)
    mgr.make_migrations(name="initial")
    await mgr.upgrade()
    await MtCovA.create(name="kept")

    (tmp_path / "0002_con.py").write_text(
        "from yara_orm import migrations as m\n\n\n"
        "class Migration(m.Migration):\n"
        "    dependencies = ['0001_initial']\n"
        "    operations = [\n"
        "        m.AddConstraint('mt_cov_a', m.UniqueConstraint("
        "fields=['name'], name='uq_mt_cov_a_name')),\n"
        "    ]\n"
    )
    await mgr.upgrade()
    assert await MtCovA.filter(name="kept").exists() is True  # rows survive
    with pytest.raises(IntegrityError):
        await MtCovA.create(name="kept")  # the unique constraint is enforced

    await mgr.downgrade(steps=1)
    await MtCovA.create(name="kept")  # duplicate allowed again
    assert await MtCovA.filter(name="kept").count() == 2


@pytest.mark.asyncio
async def test_add_constraint_on_untracked_table_rejected_on_sqlite(sqlite_orm, tmp_path):
    """
    GIVEN a migration adding a constraint to a table the migration state does
          not track (created via RunSQL, so no rebuild spec exists)
    WHEN it is applied on SQLite
    THEN the upgrade raises UnSupportedError
    """
    mgr = _mgr(tmp_path)
    mgr.make_migrations(name="initial")
    await mgr.upgrade()

    (tmp_path / "0002_con.py").write_text(
        "from yara_orm import migrations as m\n\n\n"
        "class Migration(m.Migration):\n"
        "    dependencies = ['0001_initial']\n"
        "    operations = [\n"
        "        m.RunSQL('CREATE TABLE raw_t (a INTEGER)', "
        "reverse_sql='DROP TABLE raw_t'),\n"
        "        m.AddConstraint('raw_t', m.UniqueConstraint("
        "fields=['a'], name='uq_raw_t_a')),\n"
        "    ]\n"
    )
    with pytest.raises(UnSupportedError):
        await mgr.upgrade()


@pytest.mark.asyncio
async def test_history_and_heads_reflect_multiple_migrations(sqlite_orm, tmp_path):
    """
    GIVEN three migrations applied in order
    WHEN history and heads are queried
    THEN history lists them oldest-first and every one reports as applied
    """
    mgr = _mgr(tmp_path)
    mgr.make_migrations(name="initial")
    await mgr.upgrade()
    for i, name in enumerate(("two", "three"), start=2):
        (tmp_path / f"000{i}_{name}.py").write_text(
            "from yara_orm import migrations as m\n\n\n"
            "class Migration(m.Migration):\n"
            f"    dependencies = ['000{i - 1}_" + ("initial" if i == 2 else "two") + "']\n"
            "    operations = [m.RunPython(None)]\n"
        )
    assert await mgr.upgrade() == ["0002_two", "0003_three"]

    hist = [h["name"] for h in await mgr.history()]
    assert hist == ["0001_initial", "0002_two", "0003_three"]

    heads = await mgr.heads()
    assert {h["name"] for h in heads} == {"0001_initial", "0002_two", "0003_three"}
    assert all(h["applied"] for h in heads)


@pytest.mark.asyncio
async def test_makemigrations_none_after_replay(sqlite_orm, tmp_path):
    """
    GIVEN a generated initial migration that fully captures the model state
    WHEN makemigrations runs a second time with no model change
    THEN it reports no changes (returns None)
    """
    mgr = _mgr(tmp_path)
    mgr.make_migrations(name="initial")
    assert mgr.make_migrations() is None

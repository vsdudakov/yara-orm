"""Regression tests for the migration-audit findings.

Covers: database-side defaults in migration DDL, the SQLite table rebuild
(foreign-key cascade safety, final index names, composite index / constraint
preservation), registry-independent foreign-key specs, unique / on_delete
diffing, conservative rename detection, the empty-registry makemigrations
guard, SQLite constraint changes via rebuild, migration-set validation
(duplicate numbers, dependency order), upgrade/downgrade target validation,
unique_together name collisions, and the table-recreate (Meta.table rename)
warning.
"""

import pytest

from yara_orm import Index, IntegrityError, MigrationManager, Model, connections, fields
from yara_orm import migrations as m
from yara_orm.connection import get_engine
from yara_orm.db_defaults import DatabaseDefault, Now, RandomHex, SqlDefault
from yara_orm.dialects import PRAGMA_FK_OFF, PRAGMA_FK_ON, PostgresDialect, SqliteDialect
from yara_orm.exceptions import ConfigurationError
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


async def _table_indexes(table):
    rows = await get_engine().fetch_rows(
        f"SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='{table}'"
    )
    return {r[0] for r in rows}


# --- finding 1: database-side defaults ---------------------------------------
class AuEvent(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)
    created = fields.DatetimeField(default=Now())

    class Meta:
        table = "au_event"


@pytest.mark.asyncio
async def test_db_default_survives_makemigrations_and_upgrade(sqlite_empty, tmp_path):
    """
    GIVEN a model with a database-side default (DatetimeField(default=Now()))
    WHEN makemigrations writes the migration and upgrade applies it
    THEN the generated file records the default, the applied column carries a
         DEFAULT clause (create() works without the value), and a second
         makemigrations reports no changes (no silent drift)
    """
    mgr = MigrationManager(directory=str(tmp_path), app="au1", models=[AuEvent])
    filename = mgr.make_migrations(name="initial")
    source = (tmp_path / filename).read_text()
    assert "from yara_orm import db_defaults" in source
    assert "default=db_defaults.Now()" in source

    await mgr.upgrade()
    # The database fills the column: an insert omitting it must succeed.
    await get_engine().execute("INSERT INTO au_event (name) VALUES ('e1')")
    row = await get_engine().fetch_rows("SELECT created FROM au_event WHERE name='e1'")
    assert row[0][0] is not None

    # Replaying the generated migration reproduces the exact model state.
    assert mgr.make_migrations() is None


def test_db_default_change_diffs_to_alterfield():
    """
    GIVEN a column whose only change is gaining/losing a database default
    WHEN the state diff is computed each way
    THEN an AlterField is emitted (default drift is detected)
    """
    before = model_state([AuEvent])
    _replace_field(AuEvent, "created", fields.DatetimeField(null=True))
    try:
        after = model_state([AuEvent])
        assert _op_names(diff_states(before, after)) == ["AlterField"]
        assert _op_names(diff_states(after, before)) == ["AlterField"]
    finally:
        _replace_field(AuEvent, "created", fields.DatetimeField(default=Now()))


class AuPkMove(Model):
    id = fields.IntField(pk=True)
    code = fields.IntField(default=0)

    class Meta:
        table = "au_pk_move"


def test_pk_move_diffs_demotion_before_promotion():
    """
    GIVEN the primary key moving from one column to another
    WHEN the state diff is computed in either direction
    THEN the demoting AlterField precedes the promoting one, so the rendered
         DROP PRIMARY KEY runs before ADD PRIMARY KEY ("multiple primary
         keys" would abort the migration otherwise)
    """
    before = model_state([AuPkMove])
    _replace_field(AuPkMove, "id", fields.IntField(default=0))
    _replace_field(AuPkMove, "code", fields.IntField(pk=True))
    try:
        after = model_state([AuPkMove])
        forward = [op for op in diff_states(before, after) if isinstance(op, m.AlterField)]
        assert [(op.name, bool(op.field.pk)) for op in forward] == [
            ("id", False),  # demotion first
            ("code", True),
        ]
        backward = [op for op in diff_states(after, before) if isinstance(op, m.AlterField)]
        assert [(op.name, bool(op.field.pk)) for op in backward] == [
            ("code", False),  # demotion first in this direction too
            ("id", True),
        ]
    finally:
        _replace_field(AuPkMove, "id", fields.IntField(pk=True))
        _replace_field(AuPkMove, "code", fields.IntField(default=0))


def test_db_default_variants_render_and_round_trip():
    """
    GIVEN fields carrying each built-in database default
    WHEN rendered to migration source and to column DDL on both dialects
    THEN the source reconstructs the default and the DDL carries DEFAULT
    """
    src = m._field_source(fields.CharField(max_length=64, default=RandomHex(size=8)))
    assert src == "fields.CharField(max_length=64, default=db_defaults.RandomHex(size=8))"
    src = m._field_source(fields.IntField(default=SqlDefault("0")))
    assert src == "fields.IntField(default=db_defaults.SqlDefault('0'))"

    spec = m._column_spec(fields.DatetimeField(default=Now()))
    assert spec["default"] == {"kind": "now"}
    for dialect in (PostgresDialect(), SqliteDialect()):
        ddl = dialect.render_column_def("created", spec)
        assert "DEFAULT (CURRENT_TIMESTAMP)" in ddl

    # Python-side defaults stay out of the DDL (deliberate exclusion).
    assert m._column_spec(fields.IntField(default=0))["default"] is None
    assert "default" not in m._field_source(fields.IntField(default=0))


# --- findings 2-4: the SQLite table rebuild -----------------------------------
class AuParent(Model):
    id = fields.IntField(pk=True)
    label = fields.CharField(max_length=30)
    rank = fields.IntField(default=0, index=True)

    class Meta:
        table = "au_parent"


class AuChild(Model):
    id = fields.IntField(pk=True)
    parent = fields.ForeignKeyField("AuParent", related_name="au_children")

    class Meta:
        table = "au_child"


@pytest.mark.asyncio
async def test_alter_parent_column_does_not_cascade_delete_children(sqlite_empty, tmp_path):
    """
    GIVEN a parent row with CASCADE children on SQLite
    WHEN a parent column is altered (table rebuild drops the parent table)
    THEN the child rows survive: the rebuild toggles PRAGMA foreign_keys
         outside the transaction so the drop fires no ON DELETE CASCADE
    """
    mgr = MigrationManager(directory=str(tmp_path), app="au2", models=[AuParent, AuChild])
    mgr.make_migrations(name="initial")
    await mgr.upgrade()
    parent = await AuParent.create(label="p", rank=1)
    await AuChild.create(parent=parent)
    await AuChild.create(parent=parent)

    _replace_field(AuParent, "label", fields.CharField(max_length=200))
    try:
        mgr.make_migrations(name="widen_label")
        await mgr.upgrade()
    finally:
        _replace_field(AuParent, "label", fields.CharField(max_length=30))

    assert await AuChild.all().count() == 2  # would be 0 with the cascade bug
    assert (await AuParent.get(id=parent.id)).label == "p"
    # And referential integrity is enforced again after the rebuild.
    with pytest.raises(IntegrityError):
        await get_engine().execute("INSERT INTO au_child (parent_id) VALUES (99999)")


@pytest.mark.asyncio
async def test_second_alter_field_on_same_table_and_final_index_names(sqlite_empty, tmp_path):
    """
    GIVEN a table with an indexed column that is rebuilt by an AlterField
    WHEN a second AlterField rebuilds the same table, then the index is removed
    THEN both rebuilds apply (no 'idx__new_... already exists'), indexes keep
         their final names, and RemoveIndex actually drops the index
    """
    mgr = MigrationManager(directory=str(tmp_path), app="au3", models=[AuParent, AuChild])
    mgr.make_migrations(name="initial")
    await mgr.upgrade()
    await AuParent.create(label="p", rank=3)

    _replace_field(AuParent, "label", fields.TextField())
    try:
        mgr.make_migrations(name="widen1")
        await mgr.upgrade()
        names = await _table_indexes("au_parent")
        assert "idx_au_parent_rank" in names  # final name, not idx__new_au_parent_rank
        assert not any(n.startswith("idx__new_") for n in names)

        # A second rebuild of the same table must not collide on index names.
        _replace_field(AuParent, "rank", fields.BigIntField(default=0, index=True))
        mgr.make_migrations(name="widen2")
        await mgr.upgrade()
        assert (await AuParent.get(label="p")).rank == 3

        # RemoveIndex computes idx_<table>_<col> and now really drops it.
        _replace_field(AuParent, "rank", fields.BigIntField(default=0))
        mgr.make_migrations(name="drop_idx")
        await mgr.upgrade()
        assert "idx_au_parent_rank" not in await _table_indexes("au_parent")
    finally:
        _replace_field(AuParent, "label", fields.CharField(max_length=30))
        _replace_field(AuParent, "rank", fields.IntField(default=0, index=True))


class AuComposite(Model):
    id = fields.IntField(pk=True)
    room = fields.CharField(max_length=10)
    hour = fields.IntField()
    note = fields.CharField(max_length=20, null=True)

    class Meta:
        table = "au_composite"
        indexes = [Index(fields=["room", "hour"], name="idx_au_comp_room_hour")]
        unique_together = ("room", "hour")


@pytest.mark.asyncio
async def test_rebuild_preserves_composite_indexes_and_constraints(sqlite_empty, tmp_path):
    """
    GIVEN a table with Meta.indexes and unique_together applied on SQLite
    WHEN an unrelated column is altered (table rebuild)
    THEN the composite index and the UNIQUE constraint both survive the rebuild
    """
    mgr = MigrationManager(directory=str(tmp_path), app="au4", models=[AuComposite])
    mgr.make_migrations(name="initial")
    await mgr.upgrade()
    await AuComposite.create(room="A", hour=9)

    _replace_field(AuComposite, "note", fields.TextField(null=True))
    try:
        mgr.make_migrations(name="widen_note")
        await mgr.upgrade()
    finally:
        _replace_field(AuComposite, "note", fields.CharField(max_length=20, null=True))

    assert "idx_au_comp_room_hour" in await _table_indexes("au_composite")
    await AuComposite.create(room="B", hour=9)  # distinct group still fine
    with pytest.raises(IntegrityError):
        await AuComposite.create(room="A", hour=9)  # unique_together still enforced


@pytest.mark.asyncio
async def test_delete_model_downgrade_restores_indexes_and_constraints(sqlite_empty, tmp_path):
    """
    GIVEN an applied model with Meta.indexes and unique_together, then dropped
    WHEN the drop migration is reverted
    THEN the recreated table carries the composite index and the constraint
    """
    mgr = MigrationManager(directory=str(tmp_path), app="au5", models=[AuComposite])
    mgr.make_migrations(name="initial")
    await mgr.upgrade()

    mgr2 = MigrationManager(directory=str(tmp_path), app="au5", models=[])
    name = mgr2.make_migrations(name="drop_all", allow_destructive=True)
    source = (tmp_path / name).read_text()
    assert "composite_indexes=" in source  # DeleteModel keeps them for reverse
    assert "m.UniqueConstraint" in source
    await mgr2.upgrade()

    await mgr2.downgrade(steps=1)
    assert "idx_au_comp_room_hour" in await _table_indexes("au_composite")
    await AuComposite.create(room="A", hour=9)
    with pytest.raises(IntegrityError):
        await AuComposite.create(room="A", hour=9)


def test_sqlite_rebuild_brackets_statements_with_fk_pragmas():
    """
    GIVEN the SQLite rebuild renderer
    WHEN a rebuild is rendered
    THEN it is bracketed by the foreign_keys pragmas and creates secondary
         indexes after the RENAME, under the final table name
    """
    spec = {
        "columns": {
            "id": {"kind": "int", "type_params": {}, "null": False, "pk": True},
            "n": {"kind": "int", "type_params": {}, "null": False},
        },
        "pk": "id",
        "fks": {},
        "indexes": ["n"],
    }
    out = SqliteDialect().render_rebuild_table("t", spec)
    assert out[0] == "PRAGMA foreign_keys=OFF"
    assert out[-1] == "PRAGMA foreign_keys=ON"
    rename_at = next(i for i, s in enumerate(out) if "RENAME TO" in s)
    index_at = next(i for i, s in enumerate(out) if "CREATE INDEX" in s)
    assert index_at > rename_at
    assert '"idx_t_n"' in out[index_at]
    assert "idx__new_" not in "\n".join(out)


# --- finding 5: registry-independent foreign-key specs ------------------------
def test_generated_fk_records_target_and_replays_without_registry(tmp_path):
    """
    GIVEN a generated migration for a model with a foreign key
    WHEN the migration file is rewritten to reference a model that no longer
         exists (simulating the target model's deletion) and replayed
    THEN makemigrations still diffs cleanly: the recorded target (resolved_fk)
         makes the state self-contained, instead of crashing with KeyError
    """
    mgr = MigrationManager(directory=str(tmp_path), app="au6", models=[AuParent, AuChild])
    filename = mgr.make_migrations(name="initial")
    source = (tmp_path / filename).read_text()
    assert "m.resolved_fk(" in source
    assert "table='au_parent'" in source and "pk='id'" in source and "kind='int'" in source

    # Simulate the referenced model having been deleted from the codebase.
    (tmp_path / filename).write_text(source.replace("'AuParent'", "'AuGhostModel'"))
    # The FK column replays from its recorded target: no registry lookup, so no
    # KeyError crash and no phantom column diffs.
    ops = diff_states(mgr._replay(), model_state([AuParent, AuChild]))
    assert not any(isinstance(op, m.AddField | m.RemoveField) for op in ops)


def test_old_style_fk_migration_with_missing_target_raises_clear_error(tmp_path):
    """
    GIVEN an old-format migration (bare ForeignKeyField, no recorded target)
          whose target model is not registered
    WHEN the recorded state is diffed
    THEN a KeyError with an actionable message is raised instead of a bare
         'Unknown model' crash
    """
    (tmp_path / "0001_initial.py").write_text(
        "from yara_orm import fields\n"
        "from yara_orm import migrations as m\n\n\n"
        "class Migration(m.Migration):\n"
        "    dependencies = []\n"
        "    operations = [\n"
        "        m.CreateModelIfNotExists('au_orphan', fields={\n"
        "            'id': fields.IntField(pk=True),\n"
        "            'ref_id': fields.ForeignKeyField('AuNoSuchModel'),\n"
        "        }),\n"
        "    ]\n"
    )
    mgr = MigrationManager(directory=str(tmp_path), app="au7", models=[AuParent])
    with pytest.raises(KeyError, match="no longer registered"):
        mgr.make_migrations(name="next")


def test_target_pk_type_change_diffs_referencing_fk_column():
    """
    GIVEN a recorded FK column whose target pk type differs from the live one
    WHEN the states are diffed
    THEN an AlterField is emitted for the referencing column
    """
    old_fk = m.resolved_fk(fields.ForeignKeyField("AuX"), table="au_x", pk="id", kind="int")
    new_fk = m.resolved_fk(fields.ForeignKeyField("AuX"), table="au_x", pk="id", kind="bigint")
    old = {"tables": {"t": {"fields": {"x_id": old_fk}, "composite_pk": None, "indexes": []}}}
    new = {"tables": {"t": {"fields": {"x_id": new_fk}, "composite_pk": None, "indexes": []}}}
    ops = diff_states(old, new)
    assert _op_names(ops) == ["AlterField"]
    assert ops[0].name == "x_id"


# --- finding 6: unique / on_delete changes are diffed --------------------------
@pytest.mark.asyncio
async def test_unique_toggle_diffs_and_enforces_on_sqlite(sqlite_empty, tmp_path):
    """
    GIVEN a column that gains unique=True (its only change)
    WHEN makemigrations + upgrade run on SQLite
    THEN an AlterField is generated and uniqueness is enforced afterwards
    """
    mgr = MigrationManager(directory=str(tmp_path), app="au8", models=[AuParent, AuChild])
    mgr.make_migrations(name="initial")
    await mgr.upgrade()
    await AuParent.create(label="taken", rank=0)

    _replace_field(AuParent, "label", fields.CharField(max_length=30, unique=True))
    try:
        filename = mgr.make_migrations(name="unique_label")
        assert filename is not None  # previously diffed to no changes
        module = mgr._load_module(tmp_path / filename)
        assert any(isinstance(op, m.AlterField) for op in module.Migration.operations)
        await mgr.upgrade()

        await AuParent.create(label="free", rank=0)
        with pytest.raises(IntegrityError):
            await AuParent.create(label="taken", rank=0)
    finally:
        _replace_field(AuParent, "label", fields.CharField(max_length=30))


def test_unique_toggle_renders_constraint_ddl_on_postgres():
    """
    GIVEN a unique-only column change
    WHEN rendered on PostgreSQL (in-place ALTER)
    THEN ADD/DROP of the default-named unique constraint is emitted
    """
    pg = PostgresDialect()
    old = {"kind": "int", "type_params": {}, "null": False, "unique": False}
    new = {**old, "unique": True}
    tspec = {"columns": {"n": new}, "pk": None, "fks": {}, "indexes": []}
    assert pg.render_alter_column("t", "n", old, new, tspec) == [
        'ALTER TABLE "t" ADD CONSTRAINT "t_n_key" UNIQUE ("n")'
    ]
    assert pg.render_alter_column("t", "n", new, old, tspec) == [
        'ALTER TABLE "t" DROP CONSTRAINT IF EXISTS "t_n_key"'
    ]


def test_on_delete_change_diffs_to_alterfield():
    """
    GIVEN an FK whose only change is its ON DELETE action
    WHEN the states are diffed
    THEN an AlterField is emitted (previously never detected)
    """
    cascade = m.resolved_fk(fields.ForeignKeyField("AuX"), table="au_x", pk="id", kind="int")
    restrict = m.resolved_fk(
        fields.ForeignKeyField("AuX", on_delete=fields.OnDelete.RESTRICT),
        table="au_x",
        pk="id",
        kind="int",
    )
    old = {"tables": {"t": {"fields": {"x_id": cascade}, "composite_pk": None, "indexes": []}}}
    new = {"tables": {"t": {"fields": {"x_id": restrict}, "composite_pk": None, "indexes": []}}}
    ops = diff_states(old, new)
    assert _op_names(ops) == ["AlterField"]
    # PostgreSQL re-points the FK constraint in place.
    sql = "\n".join(ops[0].forward_sql(PostgresDialect(), old))
    assert 'DROP CONSTRAINT IF EXISTS "t_x_id_fkey"' in sql
    assert "ON DELETE RESTRICT" in sql


# --- finding 7: conservative rename detection ----------------------------------
class AuRen(Model):
    id = fields.IntField(pk=True)
    a = fields.CharField(max_length=10)
    b = fields.IntField()

    class Meta:
        table = "au_ren"


def test_unambiguous_simultaneous_renames_still_detected():
    """
    GIVEN two columns of *different* types renamed at once (each spec unique)
    WHEN the diff runs
    THEN both renames are detected (only ambiguity falls back to drop+add)
    """
    before = model_state([AuRen])
    _detach_field(AuRen, "a")
    _detach_field(AuRen, "b")
    _attach_field(AuRen, "x", fields.CharField(max_length=10))
    _attach_field(AuRen, "y", fields.IntField())
    try:
        ops = diff_states(before, model_state([AuRen]))
        renamed = {(o.old, o.new) for o in ops if isinstance(o, m.RenameField)}
        assert renamed == {("a", "x"), ("b", "y")}
    finally:
        _detach_field(AuRen, "x")
        _detach_field(AuRen, "y")
        _attach_field(AuRen, "a", fields.CharField(max_length=10))
        _attach_field(AuRen, "b", fields.IntField())


def test_ambiguous_rename_prints_hint_and_emits_drop_add(capsys):
    """
    GIVEN two same-typed columns dropped while two same-typed ones are added
    WHEN the diff runs
    THEN no rename is guessed, drop+add is emitted, and a hint is printed
    """
    before = model_state([AuRen])
    _detach_field(AuRen, "a")
    _attach_field(AuRen, "a2", fields.CharField(max_length=10))
    _attach_field(AuRen, "c", fields.CharField(max_length=10))
    try:
        after = model_state([AuRen])
        # before: {a}; after: {a2, c} -> one drop, two identical adds: ambiguous.
        ops = set(_op_names(diff_states(before, after)))
        assert "RenameField" not in ops
        assert {"RemoveFieldIfExists", "AddFieldIfNotExists"} <= ops
        assert "hand-write m.RenameField" in capsys.readouterr().err
    finally:
        _detach_field(AuRen, "a2")
        _detach_field(AuRen, "c")
        _attach_field(AuRen, "a", fields.CharField(max_length=10))


# --- finding 8: empty-registry destructive guard --------------------------------
@pytest.mark.asyncio
async def test_makemigrations_refuses_to_drop_every_table(sqlite_empty, tmp_path):
    """
    GIVEN a recorded schema and a manager scoped to zero models (as the CLI's
          default --models "" produces)
    WHEN makemigrations runs
    THEN it aborts with a clear error instead of writing a drop-everything
         migration; allow_destructive=True overrides
    """
    mgr = MigrationManager(directory=str(tmp_path), app="au9", models=[AuParent, AuChild])
    mgr.make_migrations(name="initial")

    empty_mgr = MigrationManager(directory=str(tmp_path), app="au9", models=[])
    with pytest.raises(ConfigurationError, match="--models"):
        empty_mgr.make_migrations(name="oops")
    assert not (tmp_path / "0002_oops.py").exists()

    name = empty_mgr.make_migrations(name="wipe", allow_destructive=True)
    assert name == "0002_wipe.py"
    assert "DeleteModelIfExists" in (tmp_path / name).read_text()


# --- finding 9: constraints — rename follow + SQLite rebuild --------------------
def test_rename_field_does_not_rediff_composite_indexes_or_constraints():
    """
    GIVEN a table state with a named composite index and a named constraint
    WHEN a column they cover is renamed via RenameField state evolution
    THEN a diff against the renamed model emits no index/constraint churn
    """
    state = {"tables": {}}
    m.CreateModel(
        "au_rn",
        fields={"id": fields.IntField(pk=True), "a": fields.IntField(), "b": fields.IntField()},
        composite_indexes={
            "idx_au_rn_ab": {
                "columns": ["a", "b"],
                "condition": None,
                "unique": False,
                "using": None,
                "include": None,
                "opclass": None,
            }
        },
        constraints=[m.UniqueConstraint(fields=["a", "b"], name="uq_au_rn_ab")],
    ).apply_state(state)
    m.RenameField("au_rn", "a", "a2").apply_state(state)

    tstate = state["tables"]["au_rn"]
    assert tstate["composite_indexes"]["idx_au_rn_ab"]["columns"] == ["a2", "b"]
    assert tstate["constraints"][0]["fields"] == ["a2", "b"]

    target = {"tables": {}}
    m.CreateModel(
        "au_rn",
        fields={"id": fields.IntField(pk=True), "a2": fields.IntField(), "b": fields.IntField()},
        composite_indexes={
            "idx_au_rn_ab": {
                "columns": ["a2", "b"],
                "condition": None,
                "unique": False,
                "using": None,
                "include": None,
                "opclass": None,
            }
        },
        constraints=[m.UniqueConstraint(fields=["a2", "b"], name="uq_au_rn_ab")],
    ).apply_state(target)
    assert diff_states(state, target) == []


@pytest.mark.asyncio
async def test_autogenerated_constraint_change_applies_on_sqlite(sqlite_empty, tmp_path):
    """
    GIVEN a model that gains a unique_together group after its initial migration
    WHEN makemigrations + upgrade run on SQLite
    THEN the AddConstraint applies via the table rebuild (previously it raised
         UnSupportedError at upgrade time) and is enforced; downgrade removes it
    """
    mgr = MigrationManager(directory=str(tmp_path), app="au10", models=[AuRen])
    mgr.make_migrations(name="initial")
    await mgr.upgrade()
    await AuRen.create(a="x", b=1)

    AuRen._meta.unique_together = [("a", "b")]
    try:
        filename = mgr.make_migrations(name="add_uniq")
        assert "AddConstraint" in (tmp_path / filename).read_text()
        await mgr.upgrade()
        assert (await AuRen.get(a="x")).b == 1  # rows survive the rebuild
        with pytest.raises(IntegrityError):
            await AuRen.create(a="x", b=1)

        await mgr.downgrade(steps=1)
        await AuRen.create(a="x", b=1)  # allowed again
    finally:
        AuRen._meta.unique_together = []


# --- finding 10: migration-set validation ---------------------------------------
def _write_noop(tmp_path, filename, dependencies):
    (tmp_path / filename).write_text(
        "from yara_orm import migrations as m\n\n\n"
        "class Migration(m.Migration):\n"
        f"    dependencies = {dependencies!r}\n"
        "    operations = []\n"
    )


def test_duplicate_migration_numbers_warn_and_order_deterministically(tmp_path, capsys):
    """
    GIVEN two migration files sharing a numeric prefix (e.g. a branch merge,
          possibly already applied on existing deployments)
    WHEN the migration set is loaded
    THEN it loads with a stderr warning naming both files, ordered
         deterministically by file name (never a hard error that would block
         every command on an existing project)
    """
    _write_noop(tmp_path, "0001_bbb.py", [])
    _write_noop(tmp_path, "0001_aaa.py", [])
    mgr = MigrationManager(directory=str(tmp_path), app="au11", models=[AuRen])
    loaded = mgr._load_all()
    assert [name for name, _ in loaded] == ["0001_aaa", "0001_bbb"]
    assert "duplicate migration number" in capsys.readouterr().err


def test_dependency_problems_warn_but_load(tmp_path, capsys):
    """
    GIVEN a migration depending on one that sorts after it, or on a missing one
    WHEN the migration set is loaded
    THEN each case loads with a stderr warning (existing directories with
         stale/typo'd dependency lists must not become unloadable)
    """
    _write_noop(tmp_path, "0001_first.py", ["0002_second"])
    _write_noop(tmp_path, "0002_second.py", [])
    mgr = MigrationManager(directory=str(tmp_path), app="au12", models=[AuRen])
    assert len(mgr._load_all()) == 2
    assert "which runs after it" in capsys.readouterr().err

    _write_noop(tmp_path, "0001_first.py", [])
    _write_noop(tmp_path, "0002_second.py", ["0001_missing"])
    assert len(mgr._load_all()) == 2
    assert "unknown migration" in capsys.readouterr().err


# --- finding 11: upgrade/downgrade target validation ------------------------------
@pytest.mark.asyncio
async def test_upgrade_unknown_target_applies_nothing(sqlite_empty, tmp_path):
    """
    GIVEN pending migrations and a target that matches none of them
    WHEN upgrade runs
    THEN it raises (listing the available names) and applies nothing
    """
    mgr = MigrationManager(directory=str(tmp_path), app="au13", models=[AuRen])
    mgr.make_migrations(name="initial")
    with pytest.raises(KeyError, match="0001_initial"):
        await mgr.upgrade(target="9999_nope")
    assert [h["applied"] for h in await mgr.heads()] == [False]


@pytest.mark.asyncio
async def test_upgrade_and_downgrade_accept_numeric_prefix_targets(sqlite_empty, tmp_path):
    """
    GIVEN two migrations
    WHEN upgrade/downgrade are given bare numeric-prefix targets
    THEN both directions resolve them consistently to the full names
    """
    mgr = MigrationManager(directory=str(tmp_path), app="au14", models=[AuRen])
    mgr.make_migrations(name="initial")
    _write_noop(tmp_path, "0002_noop.py", ["0001_initial"])

    assert await mgr.upgrade(target="1") == ["0001_initial"]
    assert await mgr.upgrade() == ["0002_noop"]
    assert await mgr.downgrade(target="0001") == ["0002_noop"]
    with pytest.raises(KeyError, match="unknown migration target"):
        await mgr.downgrade(target="0042")


# --- finding 13: unique_together constraint names ---------------------------------
class AuCollide(Model):
    id = fields.IntField(pk=True)
    a = fields.CharField(max_length=5)
    b_c = fields.CharField(max_length=5)
    a_b = fields.CharField(max_length=5)
    c = fields.CharField(max_length=5)

    class Meta:
        table = "au_collide"
        unique_together = (("a", "b_c"), ("a_b", "c"))


class AuLongName(Model):
    id = fields.IntField(pk=True)
    the_first_rather_long_column_name = fields.CharField(max_length=5)
    the_second_rather_long_column_name = fields.CharField(max_length=5)

    class Meta:
        table = "au_long_name_table"
        unique_together = (
            ("the_first_rather_long_column_name", "the_second_rather_long_column_name"),
        )


def test_colliding_unique_together_groups_get_distinct_names():
    """
    GIVEN two unique_together groups whose underscore-joins are identical
    WHEN their constraint specs are built
    THEN the generated names are distinct (hash-disambiguated) and stable
    """
    specs = model_state([AuCollide])["tables"]["au_collide"]["constraints"]
    names = [s["name"] for s in specs]
    assert len(set(names)) == 2
    assert all(len(n) <= 63 for n in names)
    # Deterministic across runs (the diff idempotence invariant relies on it).
    again = [s["name"] for s in model_state([AuCollide])["tables"]["au_collide"]["constraints"]]
    assert names == again


def test_common_case_unique_together_name_is_unchanged_and_long_names_fit():
    """
    GIVEN a normal unique_together group and one whose joined name exceeds 63
    WHEN their constraint specs are built
    THEN the common case keeps the historical uniq_<table>_<cols> name while
         the long one is clamped under PostgreSQL's identifier limit
    """
    slot = model_state([AuComposite])["tables"]["au_composite"]["constraints"]
    assert slot[0]["name"] == "uniq_au_composite_room_hour"  # stable legacy name

    long = model_state([AuLongName])["tables"]["au_long_name_table"]["constraints"]
    assert len(long[0]["name"]) <= 63
    assert long[0]["name"].startswith("uniq_au_long_name_table_")


# --- finding 14: Meta.table rename warning -----------------------------------------
class AuRenameMe(Model):
    id = fields.IntField(pk=True)
    payload = fields.CharField(max_length=10)

    class Meta:
        table = "au_rename_old"


class AuRenamed(Model):
    id = fields.IntField(pk=True)
    payload = fields.CharField(max_length=10)

    class Meta:
        table = "au_rename_new"


def test_table_recreate_pair_writes_prominent_warning(tmp_path, capsys):
    """
    GIVEN a diff that drops one table and creates an identically-shaped one
          (the shape of a bare Meta.table rename)
    WHEN makemigrations writes the migration
    THEN the file carries a WARNING comment suggesting RenameModel and the
         warning is also printed to stderr
    """
    mgr = MigrationManager(directory=str(tmp_path), app="au15", models=[AuRenameMe])
    mgr.make_migrations(name="initial")

    mgr2 = MigrationManager(directory=str(tmp_path), app="au15", models=[AuRenamed])
    filename = mgr2.make_migrations(name="rename", allow_destructive=True)
    source = (tmp_path / filename).read_text()
    assert "# WARNING:" in source
    assert "m.RenameModel('au_rename_old', 'au_rename_new')" in source
    assert "DESTROYS" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Coverage: unserialisable custom DatabaseDefault subclasses are rejected
# ---------------------------------------------------------------------------


def test_custom_database_default_subclass_is_rejected():
    """
    GIVEN a custom DatabaseDefault subclass migrations cannot serialise
    WHEN its migration source and spec are rendered
    THEN both helpers raise ConfigurationError pointing at SqlDefault
    """

    class _WeirdDefault(DatabaseDefault):
        pass

    with pytest.raises(ConfigurationError, match="SqlDefault"):
        m._default_source(_WeirdDefault())
    with pytest.raises(ConfigurationError, match="SqlDefault"):
        m._default_spec(_WeirdDefault())


# ---------------------------------------------------------------------------
# Coverage: FK to a varchar pk records the target's type_params
# ---------------------------------------------------------------------------


class AuCharPk(Model):
    code = fields.CharField(pk=True, max_length=24)

    class Meta:
        table = "au_char_pk"


class AuCharChild(Model):
    id = fields.IntField(pk=True)
    parent = fields.ForeignKeyField("AuCharPk")

    class Meta:
        table = "au_char_child"


def test_fk_to_varchar_pk_records_type_params_in_source():
    """
    GIVEN a foreign key whose target pk is a varchar with a max_length
    WHEN the field renders as migration source
    THEN resolved_fk records the target's type_params alongside table/pk/kind
    """
    fk = next(f for f in AuCharChild._meta.field_list if getattr(f, "reference", None))
    src = m._field_source(fk)
    assert "m.resolved_fk(" in src
    assert "kind='varchar'" in src
    assert "type_params={'max_length': 24}" in src


# ---------------------------------------------------------------------------
# Coverage: column renames follow INCLUDE lists and skip check constraints
# ---------------------------------------------------------------------------


def test_rename_in_table_follows_include_and_skips_check_constraints():
    """
    GIVEN a table state with a covering composite index and mixed constraints
    WHEN a column referenced by both is renamed
    THEN the index columns and INCLUDE list plus unique-constraint fields
         follow the rename while raw-SQL check constraints stay untouched
    """
    tstate = {
        "fields": {"a": fields.IntField(), "c": fields.IntField()},
        "indexes": [],
        "composite_indexes": {"ix": {"columns": ["a"], "include": ["a", "c"]}},
        "constraints": [
            {"kind": "check", "name": "ck", "check": "a > 0"},
            {"kind": "unique", "name": "uq", "fields": ["a"]},
        ],
    }
    m._rename_in_table(tstate, "a", "b")
    assert list(tstate["fields"]) == ["b", "c"]
    assert tstate["composite_indexes"]["ix"]["columns"] == ["b"]
    assert tstate["composite_indexes"]["ix"]["include"] == ["b", "c"]
    assert tstate["constraints"][0]["check"] == "a > 0"  # raw SQL untouched
    assert tstate["constraints"][1]["fields"] == ["b"]


# ---------------------------------------------------------------------------
# Coverage: recreate warning only fires for identically-shaped tables
# ---------------------------------------------------------------------------


class AuRenamedWider(Model):
    id = fields.IntField(pk=True)
    payload = fields.CharField(max_length=10)
    extra = fields.IntField(default=0)

    class Meta:
        table = "au_rename_wider"


def test_table_recreate_warning_skips_differently_shaped_tables():
    """
    GIVEN a diff dropping one table while creating a differently-shaped one
    WHEN the recreate warnings are computed
    THEN no warning is emitted (this is not the shape of a Meta.table rename)
    """
    old = model_state([AuRenameMe])
    new = model_state([AuRenamedWider])
    assert m._table_recreate_warnings(old, new) == []


# ---------------------------------------------------------------------------
# Coverage: pragma-bracketed ops outside an atomic migration self-wrap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_atomic_pragma_op_wraps_itself_in_a_transaction(sqlite_empty):
    """
    GIVEN a non-atomic migration operation bracketed with FK pragmas
    WHEN it is applied outside any transaction
    THEN it wraps itself in its own pinned transaction and completes
    """
    await m._apply_op_sql(
        get_engine(),
        [
            PRAGMA_FK_OFF,
            "CREATE TABLE au_pragma_scratch (id INTEGER PRIMARY KEY)",
            PRAGMA_FK_ON,
        ],
        atomic=False,
    )
    rows = await connections.get("default").fetch_rows("SELECT COUNT(*) FROM au_pragma_scratch")
    assert rows[0][0] == 0


# ---------------------------------------------------------------------------
# Coverage: a failed rebuild restores foreign-key enforcement
# ---------------------------------------------------------------------------


class _FkRecorder:
    """Records executed SQL; raises on the marker statement."""

    def __init__(self):
        self.calls = []

    async def execute(self, sql, params=None):
        self.calls.append(sql)
        if sql == "BOOM":
            raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_failed_rebuild_restores_fk_enforcement_inside_transaction():
    """
    GIVEN a rebuild that fails after FK enforcement was switched off
    WHEN the statements ran on a pinned transaction
    THEN the transaction rolls back, enforcement is restored and a fresh
         BEGIN reopens the transaction before the error propagates
    """
    eng = _FkRecorder()
    with pytest.raises(RuntimeError, match="boom"):
        await m._run_op_sql(eng, [PRAGMA_FK_OFF, "BOOM"], in_txn=True)
    assert eng.calls[-3:] == ["ROLLBACK", PRAGMA_FK_ON, "BEGIN"]


@pytest.mark.asyncio
async def test_failed_rebuild_restores_fk_enforcement_in_autocommit():
    """
    GIVEN a rebuild that fails after FK enforcement was switched off
    WHEN the statements ran in autocommit (no pinned transaction)
    THEN enforcement is restored before the error propagates
    """
    eng = _FkRecorder()
    with pytest.raises(RuntimeError, match="boom"):
        await m._run_op_sql(eng, [PRAGMA_FK_OFF, "BOOM"], in_txn=False)
    assert eng.calls[-1] == PRAGMA_FK_ON


@pytest.mark.asyncio
async def test_failed_op_without_fk_toggle_reraises_without_restore():
    """
    GIVEN an operation that fails before any FK pragma was executed
    WHEN the statements run
    THEN the error propagates as-is with no enforcement-restore statements
    """
    eng = _FkRecorder()
    with pytest.raises(RuntimeError, match="boom"):
        await m._run_op_sql(eng, ["BOOM"], in_txn=False)
    assert eng.calls == ["BOOM"]

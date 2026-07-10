"""Auto-generated migration *operations* from a model-state diff.

Covers what ``makemigrations`` should emit when a model changes: a column rename
(RenameField, not a destructive drop+add), single-column field indexes,
``Meta.indexes`` composite indexes, named ``Meta.constraints``, an ``AlterField``
type change, and adding/removing a ManyToMany field (which creates/drops the
join table). The diff is pure (no DB), so these run without a backend; a couple
of end-to-end SQLite tests confirm the generated ops actually execute.
"""

import os
import tempfile

import pytest
import pytest_asyncio

from yara_orm import Index, MigrationManager, Model, YaraOrm, fields
from yara_orm import migrations as m
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


class GenWidget(Model):
    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=50)
    size = fields.IntField(default=0)

    class Meta:
        table = "gen_widget"


def _state():
    return model_state([GenWidget])


# --- rename detection --------------------------------------------------------
def test_rename_column_emits_renamefield_not_drop_add():
    """
    GIVEN a model whose column is renamed (same type)
    WHEN the state diff is computed
    THEN a single RenameField is emitted, not a destructive remove+add
    """
    before = _state()
    _detach_field(GenWidget, "title")
    _attach_field(GenWidget, "headline", fields.CharField(max_length=50))
    try:
        ops = diff_states(before, _state())
        assert _op_names(ops) == ["RenameField"]
        assert (ops[0].old, ops[0].new) == ("title", "headline")
    finally:
        _detach_field(GenWidget, "headline")
        _attach_field(GenWidget, "title", fields.CharField(max_length=50))


def test_rename_with_type_change_falls_back_to_drop_add():
    """
    GIVEN a renamed column whose type *also* changed
    WHEN the diff runs
    THEN it is NOT treated as a rename (no safe column match) but drop+add
    """
    before = _state()
    _detach_field(GenWidget, "title")
    _attach_field(GenWidget, "headline", fields.IntField(null=True))
    try:
        ops = set(_op_names(diff_states(before, _state())))
        assert "RenameField" not in ops
        assert {"RemoveFieldIfExists", "AddFieldIfNotExists"} <= ops
    finally:
        _detach_field(GenWidget, "headline")
        _attach_field(GenWidget, "title", fields.CharField(max_length=50))


# --- single-column field index ----------------------------------------------
def test_add_and_remove_field_index():
    """
    GIVEN a field that gains then loses index=True
    WHEN the diff runs each way
    THEN AddIndexIfNotExists / RemoveIndexIfExists are emitted
    """
    before = _state()
    _replace_field(GenWidget, "size", fields.IntField(default=0, index=True))
    try:
        after = _state()
        assert _op_names(diff_states(before, after)) == ["AddIndexIfNotExists"]
        assert _op_names(diff_states(after, before)) == ["RemoveIndexIfExists"]
    finally:
        _replace_field(GenWidget, "size", fields.IntField(default=0))


# --- Meta.indexes (composite) ------------------------------------------------
def test_add_and_remove_meta_composite_index():
    """
    GIVEN a model that gains/loses a Meta.indexes composite index
    WHEN the diff runs each way
    THEN AddCompositeIndexIfNotExists / RemoveCompositeIndexIfExists are emitted
    """
    before = _state()
    GenWidget._meta.indexes = [Index(fields=["title", "size"])]
    try:
        after = _state()
        add = diff_states(before, after)
        assert _op_names(add) == ["AddCompositeIndexIfNotExists"]
        assert add[0].columns == ["title", "size"]
        assert _op_names(diff_states(after, before)) == ["RemoveCompositeIndexIfExists"]
    finally:
        GenWidget._meta.indexes = []


# --- partial (conditional) index --------------------------------------------
def test_add_and_remove_partial_index():
    """
    GIVEN a model that gains/loses a partial Index (with a WHERE condition)
    WHEN the diff runs each way
    THEN the add/remove ops carry the condition so it round-trips
    """
    before = _state()
    GenWidget._meta.indexes = [
        Index(fields=["size"], name="idx_big_widgets", condition="size > 100")
    ]
    try:
        after = _state()
        add = diff_states(before, after)
        assert _op_names(add) == ["AddCompositeIndexIfNotExists"]
        assert add[0].name == "idx_big_widgets"
        assert add[0].condition == "size > 100"
        remove = diff_states(after, before)
        assert _op_names(remove) == ["RemoveCompositeIndexIfExists"]
        assert remove[0].condition == "size > 100"
    finally:
        GenWidget._meta.indexes = []


def test_partial_index_renders_where_clause():
    """
    GIVEN an AddCompositeIndex carrying a partial condition
    WHEN it is rendered to SQL on each dialect
    THEN the statement includes a trailing WHERE clause
    """
    from yara_orm.dialects import PostgresDialect, SqliteDialect

    op = m.AddCompositeIndex("t", "idx_p", ["a"], condition="a > 0")
    for dialect in (PostgresDialect(), SqliteDialect()):
        [sql] = op.forward_sql(dialect, {})
        assert sql.endswith('("a") WHERE a > 0')


# --- Meta.constraints (named unique) ----------------------------------------
def test_add_and_remove_named_unique_constraint():
    """
    GIVEN a model that gains/loses a named UniqueConstraint in Meta.constraints
    WHEN the diff runs each way
    THEN AddConstraint / RemoveConstraint are emitted with that constraint
    """
    before = _state()
    GenWidget._meta.constraints = [
        m.UniqueConstraint(fields=["title", "size"], name="uq_gen_widget_ts")
    ]
    try:
        after = _state()
        add = diff_states(before, after)
        assert _op_names(add) == ["AddConstraint"]
        assert add[0].constraint.name == "uq_gen_widget_ts"
        assert _op_names(diff_states(after, before)) == ["RemoveConstraint"]
    finally:
        GenWidget._meta.constraints = []


# --- AlterField type change --------------------------------------------------
def test_alter_field_type_change():
    """
    GIVEN a field whose column type changes
    WHEN the diff runs
    THEN an AlterField operation is emitted
    """
    before = _state()
    _replace_field(GenWidget, "size", fields.BigIntField(default=0))
    try:
        assert _op_names(diff_states(before, _state())) == ["AlterField"]
    finally:
        _replace_field(GenWidget, "size", fields.IntField(default=0))


# --- ManyToMany add/remove -> join table create/drop -------------------------
class GenLeft(Model):
    id = fields.IntField(pk=True)
    rights = fields.ManyToManyField("GenRight", through="gen_left_right")

    class Meta:
        table = "gen_left"


class GenRight(Model):
    id = fields.IntField(pk=True)

    class Meta:
        table = "gen_right"


def test_add_m2m_creates_join_table_remove_drops_it():
    """
    GIVEN a model whose ManyToMany field defines a join table
    WHEN that join table is absent from the prior state (i.e. the M2M was added)
    THEN the diff creates it via CreateModel, and removing it drops the table
    """
    after = model_state([GenLeft, GenRight])
    assert "gen_left_right" in after["tables"]
    # The prior state had no join table — exactly the shape after adding an M2M.
    before = {"tables": {t: s for t, s in after["tables"].items() if t != "gen_left_right"}}

    assert _op_names(diff_states(before, after)) == ["CreateModelIfNotExists"]
    assert _op_names(diff_states(after, before)) == ["DeleteModelIfExists"]


# --- end-to-end on SQLite: makemigrations + upgrade --------------------------
class E2EThing(Model):
    id = fields.IntField(pk=True)
    label = fields.CharField(max_length=30)

    class Meta:
        table = "e2e_thing"


E2E_MODELS = [E2EThing]


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
async def test_makemigrations_rename_executes_on_sqlite(sqlite_orm, tmp_path):
    """
    GIVEN an applied table on SQLite
    WHEN a column is renamed and makemigrations + upgrade run
    THEN a RenameField migration is generated and the data survives the rename
    """
    mgr = MigrationManager(directory=str(tmp_path), app="e2e", models=E2E_MODELS)
    mgr.make_migrations(name="initial")
    await mgr.upgrade()
    await E2EThing.create(label="keep-me")

    _detach_field(E2EThing, "label")
    _attach_field(E2EThing, "caption", fields.CharField(max_length=30))
    try:
        filename = mgr.make_migrations(name="rename_label")
        source = (tmp_path / filename).read_text()
        assert "RenameField" in source
        assert "RemoveField" not in source and "AddField" not in source
        await mgr.upgrade()
        # Data preserved through the rename (would be lost by a drop+add).
        rows = await E2EThing.all().values_list("caption", flat=True)
        assert rows == ["keep-me"]
    finally:
        _detach_field(E2EThing, "caption")
        _attach_field(E2EThing, "label", fields.CharField(max_length=30))


@pytest.mark.asyncio
async def test_makemigrations_composite_index_executes_on_sqlite(sqlite_orm, tmp_path):
    """
    GIVEN an applied table on SQLite
    WHEN a Meta.indexes composite index is added and the migration runs
    THEN an AddCompositeIndex migration is generated and applies cleanly
    """
    mgr = MigrationManager(directory=str(tmp_path), app="e2e2", models=E2E_MODELS)
    _attach_field(E2EThing, "rank", fields.IntField(default=0))
    try:
        mgr.make_migrations(name="initial")
        await mgr.upgrade()

        E2EThing._meta.indexes = [Index(fields=["label", "rank"])]
        filename = mgr.make_migrations(name="add_index")
        assert "AddCompositeIndex" in (tmp_path / filename).read_text()
        await mgr.upgrade()
        # The index exists in SQLite's catalogue.
        from yara_orm.connection import get_engine

        rows = await get_engine().fetch_rows(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='e2e_thing'"
        )
        names = {r[0] for r in rows}
        assert "idx_e2e_thing_label_rank" in names
    finally:
        E2EThing._meta.indexes = []
        _detach_field(E2EThing, "rank")


@pytest.mark.asyncio
async def test_partial_index_migration_roundtrip_on_sqlite(sqlite_orm, tmp_path):
    """
    GIVEN an applied table on SQLite
    WHEN a partial Index is added then removed across migrations, each applied
         and reverted
    THEN every composite-index operation (add/remove, forward/backward, source
         with the condition) round-trips and the catalogue ends up consistent
    """
    from yara_orm.connection import get_engine

    mgr = MigrationManager(directory=str(tmp_path), app="e2ep", models=E2E_MODELS)
    _attach_field(E2EThing, "score", fields.IntField(default=0))

    async def index_names():
        rows = await get_engine().fetch_rows(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='e2e_thing'"
        )
        return {r[0] for r in rows}

    try:
        mgr.make_migrations(name="initial")
        await mgr.upgrade()

        # Add a partial index -> AddCompositeIndex (forward + to_source w/ condition).
        E2EThing._meta.indexes = [
            Index(fields=["score"], name="idx_hi_score", condition="score > 10")
        ]
        add_file = mgr.make_migrations(name="add_partial")
        assert "condition='score > 10'" in (tmp_path / add_file).read_text()
        await mgr.upgrade()
        assert "idx_hi_score" in await index_names()

        # Revert the add (backward_sql -> drop), then re-apply.
        await mgr.downgrade(steps=1)
        assert "idx_hi_score" not in await index_names()
        await mgr.upgrade()
        assert "idx_hi_score" in await index_names()

        # Remove it from the model -> RemoveCompositeIndex (forward drop).
        E2EThing._meta.indexes = []
        rm_file = mgr.make_migrations(name="drop_partial")
        assert "RemoveCompositeIndex" in (tmp_path / rm_file).read_text()
        await mgr.upgrade()
        assert "idx_hi_score" not in await index_names()

        # Revert the remove (backward_sql -> recreate the partial index).
        await mgr.downgrade(steps=1)
        assert "idx_hi_score" in await index_names()
    finally:
        E2EThing._meta.indexes = []
        _detach_field(E2EThing, "score")


# --- index over a relation name + multi-rename + Meta normalization forms -----
class GenParent(Model):
    id = fields.IntField(pk=True)

    class Meta:
        table = "gen_parent"


class GenChild(Model):
    id = fields.IntField(pk=True)
    a = fields.CharField(max_length=10)
    b = fields.CharField(max_length=10)
    parent = fields.ForeignKeyField("GenParent", related_name="children")

    class Meta:
        table = "gen_child"


def test_meta_index_over_relation_name():
    """
    GIVEN a Meta.indexes entry naming a foreign-key relation
    WHEN the migration state is built
    THEN the index resolves to the relation's backing column
    """
    before = model_state([GenParent, GenChild])
    GenChild._meta.indexes = [Index(fields=["parent"], name="idx_child_parent")]
    try:
        ci = model_state([GenParent, GenChild])["tables"]["gen_child"]["composite_indexes"]
        assert ci["idx_child_parent"]["columns"] == ["parent_id"]
        assert _op_names(diff_states(before, model_state([GenParent, GenChild]))) == [
            "AddCompositeIndexIfNotExists"
        ]
    finally:
        GenChild._meta.indexes = []


def test_simultaneous_column_renames_fall_back_to_drop_add():
    """
    GIVEN two columns of identical type renamed at once (ambiguous pairing)
    WHEN the diff runs
    THEN no RenameField is guessed (a wrong pair would swap data between
         columns); the diff conservatively emits drop+add instead
    """
    before = model_state([GenParent, GenChild])
    _detach_field(GenChild, "a")
    _detach_field(GenChild, "b")
    _attach_field(GenChild, "x", fields.CharField(max_length=10))
    _attach_field(GenChild, "y", fields.CharField(max_length=10))
    try:
        ops = set(_op_names(diff_states(before, model_state([GenParent, GenChild]))))
        assert "RenameField" not in ops
        assert {"RemoveFieldIfExists", "AddFieldIfNotExists"} <= ops
    finally:
        _detach_field(GenChild, "x")
        _detach_field(GenChild, "y")
        _attach_field(GenChild, "a", fields.CharField(max_length=10))
        _attach_field(GenChild, "b", fields.CharField(max_length=10))


def test_meta_indexes_normalization_forms():
    """
    GIVEN the accepted Meta.indexes shapes (bare Index, single string group)
          and a multi-group unique_together
    WHEN a model is defined with each
    THEN they normalise to the expected index/constraint sets
    """

    class NormA(Model):
        id = fields.IntField(pk=True)
        a = fields.CharField(max_length=10)
        b = fields.CharField(max_length=10)

        class Meta:
            table = "norm_a"
            indexes = Index(fields=["a"], name="idx_norm_a")  # bare Index
            unique_together = (("a", "b"), ("b",))  # several groups

    class NormB(Model):
        id = fields.IntField(pk=True)
        a = fields.CharField(max_length=10)
        b = fields.CharField(max_length=10)

        class Meta:
            table = "norm_b"
            indexes = ("a", "b")  # single group of plain field names

    assert [ix.fields for ix in NormA._meta.indexes] == [["a"]]
    assert NormA._meta.unique_together == [("a", "b"), ("b",)]
    assert [ix.fields for ix in NormB._meta.indexes] == [["a", "b"]]


def test_unnamed_meta_constraint_is_not_diffed():
    """
    GIVEN an unnamed Meta.constraints entry
    WHEN the migration state is built
    THEN it is omitted (it cannot be dropped by name, so it is not diffed)
    """
    before = model_state([GenParent, GenChild])
    GenChild._meta.constraints = [m.UniqueConstraint(fields=["a", "b"])]  # no name
    try:
        after = model_state([GenParent, GenChild])
        assert after["tables"]["gen_child"]["constraints"] == []
        assert diff_states(before, after) == []
    finally:
        GenChild._meta.constraints = []


def test_remove_plain_composite_index_to_source_has_no_condition():
    """
    GIVEN a RemoveCompositeIndex for an index with no partial condition
    WHEN it is rendered to migration source
    THEN no ``condition=`` argument is emitted
    """
    src = m.RemoveCompositeIndex("t", "idx_t_a_b", ["a", "b"]).to_source()
    assert "condition=" not in src
    assert "idx_t_a_b" in src


def test_reversible_op_pairs_preserve_forward_backward_and_guards():
    """
    GIVEN the Add/Remove operation pairs built on the shared _ReversibleOp base
    WHEN their forward/backward SQL is rendered on PostgreSQL
    THEN each Remove is the exact inverse of its Add, and the IF [NOT] EXISTS
         guards land on the same side they did before the refactor
    """
    from yara_orm.dialects import PostgresDialect

    pg = PostgresDialect()
    st: dict = {"tables": {}}

    # A Remove op's forward is its Add counterpart's backward (true inverse).
    add_f = m.AddField("t", "c", fields.IntField())
    rem_f = m.RemoveField("t", "c", fields.IntField())
    assert rem_f.forward_sql(pg, st) == add_f.backward_sql(pg, st)
    assert rem_f.backward_sql(pg, st) == add_f.forward_sql(pg, st)

    # The forward guard follows the op's own action: RemoveFieldIfExists guards
    # the DROP, but recreating on reverse (ADD) is unguarded.
    rem_if = m.RemoveFieldIfExists("t", "c", fields.IntField())
    assert "DROP COLUMN IF EXISTS" in rem_if.forward_sql(pg, st)[0]
    assert "IF NOT EXISTS" not in rem_if.backward_sql(pg, st)[0]

    # The subtle historical asymmetry: a composite index recreated on reverse
    # keeps IF NOT EXISTS, but a single-column index recreated on reverse does not.
    assert "IF NOT EXISTS" in m.RemoveCompositeIndex("t", "i", ["a"]).backward_sql(pg, st)[0]
    assert "IF NOT EXISTS" not in m.RemoveIndex("t", "c").backward_sql(pg, st)[0]


def test_remove_composite_index_if_exists_round_trips_to_its_own_class():
    """
    GIVEN a RemoveCompositeIndexIfExists operation (as diff_states emits)
    WHEN it is rendered to migration source
    THEN it serializes back as its own class, not the base RemoveCompositeIndex
    """
    src = m.RemoveCompositeIndexIfExists("t", "idx_t_a_b", ["a", "b"]).to_source()
    assert src.startswith("m.RemoveCompositeIndexIfExists(")


def test_index_opclass_renders_on_postgres_and_drops_on_sqlite():
    """
    GIVEN a composite index carrying a per-column operator class
    WHEN it is rendered on each dialect
    THEN PostgreSQL appends the opclass and SQLite omits it
    """
    from yara_orm.dialects import PostgresDialect, SqliteDialect

    op = m.AddCompositeIndex("t", "idx_trgm", ["a"], using="gin", opclass="gin_trgm_ops")
    [pg_sql] = op.forward_sql(PostgresDialect(), {})
    assert '("a" gin_trgm_ops)' in pg_sql
    [lite_sql] = op.forward_sql(SqliteDialect(), {})
    assert '("a")' in lite_sql
    assert "gin_trgm_ops" not in lite_sql


def test_index_opclass_round_trips_through_migration_source():
    """
    GIVEN a composite index op with an operator class
    WHEN it is rendered to migration source
    THEN the opclass keyword is emitted so re-runs preserve it
    """
    op = m.AddCompositeIndex("t", "idx_trgm", ["a"], using="gin", opclass="gin_trgm_ops")
    assert "opclass='gin_trgm_ops'" in op.to_source()


def test_meta_index_opclass_emitted_in_create_table_sql():
    """
    GIVEN a model declaring Index(opclass=...) in Meta.indexes
    WHEN its table DDL is generated on PostgreSQL
    THEN the index statement carries the operator class
    """
    from yara_orm.dialects import PostgresDialect

    # Abstract so the model is not registered globally (its index needs the
    # pg_trgm extension, which other tests' generate_schemas() does not install).
    class GenDoc(Model):
        id = fields.IntField(pk=True)
        body = fields.TextField()

        class Meta:
            abstract = True
            table = "gen_doc"
            indexes = [Index(fields=["body"], using="gin", opclass="gin_trgm_ops")]

    sql = "\n".join(PostgresDialect().create_table_sql(GenDoc._meta))
    assert "gin_trgm_ops" in sql


def test_create_model_if_not_exists_is_rerun_safe_per_dialect():
    """
    GIVEN a CreateModelIfNotExists operation with an indexed column
    WHEN its forward SQL renders on MySQL, SQL Server, PostgreSQL and SQLite
    THEN every emitted statement carries its own existence guard (inline fold
         on MySQL, sys.indexes check on SQL Server, native IF NOT EXISTS on
         PostgreSQL/SQLite)
    """
    from yara_orm.dialects import MySQLDialect, PostgresDialect, SqliteDialect, SqlServerDialect

    op = m.CreateModelIfNotExists(
        "t",
        fields={
            "id": fields.IntField(pk=True),
            "tag": fields.CharField(max_length=20, index=True),
        },
    )
    mysql_sql = op.forward_sql(MySQLDialect(), {})
    assert len(mysql_sql) == 1
    assert mysql_sql[0].startswith("CREATE TABLE IF NOT EXISTS `t`")
    assert "INDEX `idx_t_tag` (`tag`)" in mysql_sql[0]

    mssql_sql = op.forward_sql(SqlServerDialect(), {})
    assert mssql_sql[0].startswith("IF OBJECT_ID(")
    assert all(
        stmt.startswith("IF NOT EXISTS (SELECT 1 FROM sys.indexes") for stmt in mssql_sql[1:]
    )

    for dialect in (PostgresDialect(), SqliteDialect()):
        sqls = op.forward_sql(dialect, {})
        assert sqls[0].startswith("CREATE TABLE IF NOT EXISTS")
        assert all(stmt.startswith("CREATE INDEX IF NOT EXISTS") for stmt in sqls[1:])
        assert len(sqls) == 2  # native guards keep the separate index statement

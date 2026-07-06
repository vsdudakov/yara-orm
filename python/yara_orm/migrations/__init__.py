"""A class-based, operation-driven migration system.

Each migration file defines a ``class Migration(m.Migration)`` carrying
``operations`` (and ``dependencies`` / ``atomic``). Operations are built from
**live field objects** -- ``CreateModel`` lists ``fields={col: Field}``,
``AddField``/``AlterField`` carry a single ``Field`` -- and render to SQL per
active dialect at apply time, so the same migration runs on PostgreSQL or
SQLite.

Workflow (see :class:`MigrationManager` and the ``python -m yara_orm`` CLI):

    makemigrations -> writes NNNN_name.py from the model diff
    upgrade        -> applies pending migrations, records them
    downgrade      -> reverts applied migrations
    history/heads  -> inspect applied vs on-disk
    sqlmigrate     -> print a migration's SQL without running it

Generated migrations emit the **idempotent** analog of each operation
(``CreateModelIfNotExists``, ``AddFieldIfNotExists``, ...). An ``atomic``
migration (the default) is all-or-nothing: a failure rolls the whole migration
back, so it can simply be re-run. Re-applying a half-applied **non-atomic**
migration is only safe for the guarded operations (``CreateModelIfNotExists``,
``DeleteModelIfExists``, the ``*IfNotExists``/``*IfExists`` index and field
ops on PostgreSQL); ``RenameField``/``RenameModel``, constraint ops, and — on
SQLite, which has no ``IF [NOT] EXISTS`` for ``ADD``/``DROP COLUMN`` — the
field ops are *not* re-run safe. On SQLite a column or constraint change
rebuilds the table, and the rebuild must toggle ``PRAGMA foreign_keys``
outside a transaction, so an atomic migration containing a rebuild commits the
operations preceding it before the rebuild runs (the rebuild itself is still
transactional). ``AlterField`` is detected automatically when a column's type,
nullability, uniqueness, database default or foreign-key target changes. The
``*Concurrently`` index operations are for hand-written, ``atomic = False``
migrations (PostgreSQL builds those indexes outside a transaction).
"""

from ._base import (
    _FILENAME_RE,
    _KIND_FIELD,
    _MAX_IDENTIFIER,
    _WRAP,
    MIGRATION_TABLE,
    CheckConstraint,
    Constraint,
    UniqueConstraint,
    _call,
    _column_spec,
    _constraint_from_spec,
    _default_source,
    _default_spec,
    _derived_indexes,
    _field_flag_args,
    _field_source,
    _fields_source,
    _fk_spec,
    _fk_target,
    _fmt,
    _index_option_source,
    _index_spec,
    _meta_index_specs,
    _meta_named_constraint_specs,
    _meta_unique_together_specs,
    _new_tstate,
    _rename_in_table,
    _tspec,
    _unique_together_name,
    resolved_fk,
)
from .diff import (
    _alterable,
    _detect_renames,
    _diff_composite_indexes,
    _diff_constraints,
    _required_extensions,
    _table_deps,
    _table_recreate_warnings,
    _topo_order,
    diff_states,
    model_state,
)
from .manager import (
    MigrationManager,
    _apply_op_sql,
    _file_number,
    _num,
    _resolve_target,
    _run_op_sql,
)
from .operations import (
    AddCompositeIndex,
    AddCompositeIndexIfNotExists,
    AddConstraint,
    AddField,
    AddFieldIfNotExists,
    AddIndex,
    AddIndexConcurrently,
    AddIndexIfNotExists,
    AddUniqueIndexConcurrently,
    AlterField,
    CreateExtension,
    CreateModel,
    CreateModelIfNotExists,
    DeleteModel,
    DeleteModelIfExists,
    Migration,
    Operation,
    RemoveCompositeIndex,
    RemoveCompositeIndexIfExists,
    RemoveConstraint,
    RemoveField,
    RemoveFieldIfExists,
    RemoveIndex,
    RemoveIndexConcurrently,
    RemoveIndexIfExists,
    RenameConstraint,
    RenameField,
    RenameIndex,
    RenameModel,
    RunPython,
    RunSQL,
    _CompositeIndexOp,
    _ConstraintOp,
    _FieldColumnOp,
    _ReversibleOp,
    _SingleColumnIndexOp,
)

__all__ = [
    "MIGRATION_TABLE",
    "_FILENAME_RE",
    "_WRAP",
    "_KIND_FIELD",
    "_fmt",
    "_call",
    "_default_source",
    "_default_spec",
    "resolved_fk",
    "_fk_target",
    "_field_flag_args",
    "_field_source",
    "_fields_source",
    "_column_spec",
    "_fk_spec",
    "_derived_indexes",
    "_index_spec",
    "_index_option_source",
    "_meta_index_specs",
    "_meta_named_constraint_specs",
    "_meta_unique_together_specs",
    "_MAX_IDENTIFIER",
    "_unique_together_name",
    "_constraint_from_spec",
    "_tspec",
    "_new_tstate",
    "_rename_in_table",
    "Constraint",
    "UniqueConstraint",
    "CheckConstraint",
    "Migration",
    "Operation",
    "_ReversibleOp",
    "CreateModel",
    "CreateModelIfNotExists",
    "DeleteModel",
    "DeleteModelIfExists",
    "_FieldColumnOp",
    "AddField",
    "AddFieldIfNotExists",
    "RemoveField",
    "RemoveFieldIfExists",
    "AlterField",
    "_SingleColumnIndexOp",
    "AddIndex",
    "AddIndexIfNotExists",
    "AddIndexConcurrently",
    "AddUniqueIndexConcurrently",
    "RemoveIndex",
    "RemoveIndexIfExists",
    "RemoveIndexConcurrently",
    "_CompositeIndexOp",
    "AddCompositeIndex",
    "AddCompositeIndexIfNotExists",
    "RemoveCompositeIndex",
    "RemoveCompositeIndexIfExists",
    "RenameModel",
    "RenameField",
    "RenameIndex",
    "_ConstraintOp",
    "AddConstraint",
    "RemoveConstraint",
    "RenameConstraint",
    "RunSQL",
    "CreateExtension",
    "RunPython",
    "model_state",
    "_table_deps",
    "_topo_order",
    "_alterable",
    "diff_states",
    "_required_extensions",
    "_detect_renames",
    "_diff_composite_indexes",
    "_diff_constraints",
    "_table_recreate_warnings",
    "MigrationManager",
    "_resolve_target",
    "_num",
    "_file_number",
    "_apply_op_sql",
    "_run_op_sql",
]

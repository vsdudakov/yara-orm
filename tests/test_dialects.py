"""Dialect DDL renderers: rename, constraint, alter-column and create-table
edge cases that the migration operations build on.

PostgreSQL alters in place; SQLite rebuilds or rejects, so each renderer is
exercised on both dialects to lock in the capability-flag behaviour.
"""

import datetime as _dt

import pytest

from yara_orm.dialects import (
    BaseDialect,
    OracleDialect,
    PostgresDialect,
    SqliteDialect,
    _json_from_db,
    _time_from_db,
)
from yara_orm.exceptions import UnSupportedError

PG = PostgresDialect()
LITE = SqliteDialect()
BASE = BaseDialect()
ORA = OracleDialect()


@pytest.mark.parametrize(
    "render",
    [
        lambda: BASE.date_part_sql("year", '"c"'),
        lambda: BASE.truncate_date_sql('"c"'),
        lambda: BASE.json_extract_sql('"c"', ["k"]),
        lambda: BASE.json_contains_sql('"c"', "$1"),
    ],
)
def test_base_dialect_rejects_backend_specific_query_sql(render):
    """
    GIVEN the backend-agnostic BaseDialect (no concrete backend)
    WHEN a backend-specific query renderer (date part / date trunc / JSON) runs
    THEN it raises UnSupportedError so a new dialect must override it
    """
    with pytest.raises(UnSupportedError):
        render()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"using": "btree); DROP TABLE t; --"},
        {"opclass": "gin_trgm_ops); DROP TABLE t; --"},
        {"opclass": "a b"},
    ],
)
def test_composite_index_rejects_unsafe_using_and_opclass(kwargs):
    """
    GIVEN a composite index with a crafted USING method or operator class
    WHEN it is rendered (these tokens are spliced into DDL, not bound)
    THEN it raises ValueError instead of emitting the injected SQL
    """
    with pytest.raises(ValueError):
        PG.render_create_composite_index("t", "idx", ["a"], **kwargs)


def test_composite_index_accepts_known_using_and_plain_opclass():
    """
    GIVEN a composite index with a valid access method and operator class
    WHEN it is rendered on PostgreSQL
    THEN both are emitted unchanged
    """
    [sql] = PG.render_create_composite_index("t", "idx", ["a"], using="gin", opclass="gin_trgm_ops")
    assert "USING gin" in sql
    assert "gin_trgm_ops" in sql


def test_base_dialect_json_extract_with_no_keys_returns_column():
    """
    GIVEN the BaseDialect JSON extractor with an empty key path
    WHEN rendered
    THEN it returns the column unchanged (no backend syntax needed)
    """
    assert BaseDialect().json_extract_sql('"c"', []) == '"c"'


INT = {
    "kind": "int",
    "type_params": {},
    "null": False,
    "unique": False,
    "pk": False,
    "auto_increment": False,
}


def test_rename_table_and_column():
    """
    GIVEN a table and column rename
    WHEN rendered
    THEN ALTER TABLE ... RENAME statements are produced
    """
    assert PG.render_rename_table("a", "b") == ['ALTER TABLE "a" RENAME TO "b"']
    assert PG.render_rename_column("t", "old", "new") == [
        'ALTER TABLE "t" RENAME COLUMN "old" TO "new"'
    ]


def test_rename_index_in_place_vs_rebuild():
    """
    GIVEN an index rename
    WHEN rendered on PostgreSQL and SQLite
    THEN PostgreSQL renames in place while SQLite drops and recreates it
    """
    pg = PG.render_rename_index("t", "c", "idx_old", "idx_new")
    assert pg == ['ALTER INDEX IF EXISTS "idx_old" RENAME TO "idx_new"']

    lite = LITE.render_rename_index("t", "c", "idx_old", "idx_new", unique=True)
    assert any("DROP INDEX" in s and "idx_old" in s for s in lite)
    assert any("CREATE UNIQUE INDEX" in s and "idx_new" in s for s in lite)


def test_constraints_in_place_on_postgres():
    """
    GIVEN unique and check constraints
    WHEN added/dropped/renamed on PostgreSQL
    THEN ALTER TABLE constraint DDL is produced
    """
    uniq = {"kind": "unique", "name": "uq_t", "fields": ["a", "b"]}
    check = {"kind": "check", "name": "ck_t", "check": "a > 0"}
    assert PG.render_add_constraint("t", uniq) == [
        'ALTER TABLE "t" ADD CONSTRAINT "uq_t" UNIQUE ("a", "b")'
    ]
    assert PG.render_add_constraint("t", check) == [
        'ALTER TABLE "t" ADD CONSTRAINT "ck_t" CHECK (a > 0)'
    ]
    assert PG.render_drop_constraint("t", "uq_t") == [
        'ALTER TABLE "t" DROP CONSTRAINT IF EXISTS "uq_t"'
    ]
    assert PG.render_rename_constraint("t", "uq_t", "uq_t2") == [
        'ALTER TABLE "t" RENAME CONSTRAINT "uq_t" TO "uq_t2"'
    ]


@pytest.mark.parametrize(
    "render",
    [
        lambda: LITE.render_add_constraint("t", {"kind": "unique", "fields": ["a"]}),
        lambda: LITE.render_drop_constraint("t", "uq_t"),
        lambda: LITE.render_rename_constraint("t", "uq_t", "uq_t2"),
    ],
)
def test_sqlite_rejects_in_place_constraint_changes(render):
    """
    GIVEN SQLite (no ALTER TABLE constraint syntax)
    WHEN a constraint add/drop/rename is rendered
    THEN it raises a clear UnSupportedError
    """
    with pytest.raises(UnSupportedError):
        render()


def test_create_table_with_inline_constraints():
    """
    GIVEN a table spec carrying inline unique and check constraints
    WHEN rendered
    THEN both constraint clauses appear in the CREATE TABLE
    """
    tspec = {
        "columns": {"a": INT, "b": INT},
        "pk": "a",
        "fks": {},
        "indexes": [],
        "constraints": [
            {"kind": "unique", "fields": ["a", "b"]},
            {"kind": "check", "name": "ck", "check": "a > 0"},
        ],
    }
    sql = PG.render_create_table("t", tspec, safe=False)[0]
    assert 'UNIQUE ("a", "b")' in sql
    assert 'CONSTRAINT "ck" CHECK (a > 0)' in sql


def test_create_table_without_primary_key():
    """
    GIVEN a table spec with no primary key
    WHEN rendered on PostgreSQL
    THEN no PRIMARY KEY clause is emitted
    """
    tspec = {"columns": {"x": INT}, "pk": None, "fks": {}, "indexes": []}
    assert "PRIMARY KEY" not in PG.render_create_table("t", tspec, safe=False)[0]


def test_alter_column_nullability_only():
    """
    GIVEN a column change that only flips nullability (same type)
    WHEN rendered on PostgreSQL
    THEN only the SET/DROP NOT NULL statement is produced
    """
    old = dict(INT)
    new = {**INT, "null": True}
    tspec = {"columns": {"n": new}, "pk": None, "fks": {}, "indexes": []}
    out = PG.render_alter_column("t", "n", old, new, tspec)
    assert out == ['ALTER TABLE "t" ALTER COLUMN "n" DROP NOT NULL']


def test_alter_column_default_set_and_drop():
    """
    GIVEN a column change that only adds or removes a database default
    WHEN rendered on PostgreSQL
    THEN a SET DEFAULT / DROP DEFAULT statement is produced
    """
    plain = dict(INT)
    defaulted = {**INT, "default": {"kind": "sql", "sql": "7"}}
    tspec = {"columns": {"n": defaulted}, "pk": None, "fks": {}, "indexes": []}
    assert PG.render_alter_column("t", "n", plain, defaulted, tspec) == [
        'ALTER TABLE "t" ALTER COLUMN "n" SET DEFAULT (7)'
    ]
    tspec = {"columns": {"n": plain}, "pk": None, "fks": {}, "indexes": []}
    assert PG.render_alter_column("t", "n", defaulted, plain, tspec) == [
        'ALTER TABLE "t" ALTER COLUMN "n" DROP DEFAULT'
    ]


def test_alter_column_fk_drop_without_readd():
    """
    GIVEN a column change that removes its foreign-key reference
    WHEN rendered on PostgreSQL
    THEN only the DROP CONSTRAINT is emitted (no re-added FOREIGN KEY)
    """
    with_fk = {**INT, "fk": {"table": "u", "pk": "id", "on_delete": "CASCADE"}}
    without_fk = dict(INT)
    tspec = {"columns": {"n": without_fk}, "pk": None, "fks": {}, "indexes": []}
    out = PG.render_alter_column("t", "n", with_fk, without_fk, tspec)
    assert out == ['ALTER TABLE "t" DROP CONSTRAINT IF EXISTS "t_n_fkey"']


# ---------------------------------------------------------------------------
# Oracle dialect — DDL renderers and decode helpers.
#
# These are pure string builders (no database), mirroring the PostgreSQL/SQLite
# cases above so the Oracle-specific ``MODIFY``/``ALTER INDEX``/``JSON_VALUE``
# spellings are exercised without needing an Oracle server.
# ---------------------------------------------------------------------------


def test_oracle_alter_column_add_type_default_unique_fk():
    """
    GIVEN a column that changes type and gains a default, a unique constraint
    and a foreign key
    WHEN rendered on Oracle
    THEN a MODIFY plus ADD CONSTRAINT statements are produced
    """
    old = dict(INT)
    new = {
        **INT,
        "kind": "bigint",
        "default": {"kind": "sql", "sql": "7"},
        "unique": True,
        "fk": {"table": "u", "pk": "id", "on_delete": "CASCADE"},
    }
    tspec = {"columns": {"n": new}, "pk": None, "fks": {}, "indexes": []}
    out = ORA.render_alter_column("t", "n", old, new, tspec)
    joined = "\n".join(out)
    assert 'MODIFY ("n"' in joined  # type change
    assert "DEFAULT 7" in joined  # default set
    assert 'ADD CONSTRAINT "t_n_key" UNIQUE ("n")' in joined  # unique add
    assert 'ADD CONSTRAINT "t_n_fkey"' in joined  # fk add


def test_oracle_alter_column_null_drop_default_unique_fk():
    """
    GIVEN a column that only flips nullability and drops its default, unique
    constraint and foreign key
    WHEN rendered on Oracle
    THEN MODIFY NULL / DEFAULT NULL and DROP CONSTRAINT statements are produced
    """
    old = {
        **INT,
        "kind": "bigint",
        "default": {"kind": "sql", "sql": "7"},
        "unique": True,
        "fk": {"table": "u", "pk": "id", "on_delete": "CASCADE"},
    }
    new = {**INT, "kind": "bigint", "null": True}
    tspec = {"columns": {"n": new}, "pk": None, "fks": {}, "indexes": []}
    out = ORA.render_alter_column("t", "n", old, new, tspec)
    joined = "\n".join(out)
    assert 'MODIFY ("n" NULL)' in joined  # nullability only (no type change)
    assert 'DEFAULT NULL' in joined  # default drop
    assert 'DROP CONSTRAINT "t_n_key"' in joined  # unique drop
    assert 'DROP CONSTRAINT "t_n_fkey"' in joined  # fk drop


def test_oracle_alter_column_default_only_leaves_others_untouched():
    """
    GIVEN a column change that only adds a default (type, nullability, unique and
    fk unchanged)
    WHEN rendered on Oracle
    THEN a single MODIFY DEFAULT statement is produced (no MODIFY type, no
    constraint DDL) — exercising the "unchanged" branch of each other clause
    """
    old = dict(INT)
    new = {**INT, "default": {"kind": "sql", "sql": "5"}}
    tspec = {"columns": {"n": new}, "pk": None, "fks": {}, "indexes": []}
    assert ORA.render_alter_column("t", "n", old, new, tspec) == [
        'ALTER TABLE "t" MODIFY ("n" DEFAULT 5)'
    ]


def test_oracle_alter_column_unique_only_leaves_default_untouched():
    """
    GIVEN a column change that only adds a unique constraint
    WHEN rendered on Oracle
    THEN a single ADD CONSTRAINT UNIQUE is produced — exercising the "default
    unchanged" branch
    """
    old = dict(INT)
    new = {**INT, "unique": True}
    tspec = {"columns": {"n": new}, "pk": None, "fks": {}, "indexes": []}
    assert ORA.render_alter_column("t", "n", old, new, tspec) == [
        'ALTER TABLE "t" ADD CONSTRAINT "t_n_key" UNIQUE ("n")'
    ]


def test_oracle_index_renderers():
    """
    GIVEN single- and multi-column index create/drop/rename
    WHEN rendered on Oracle
    THEN Oracle CREATE INDEX / DROP INDEX / ALTER INDEX statements are produced
    """
    assert ORA.render_create_index("t", "c", safe=False, unique=True) == [
        'CREATE UNIQUE INDEX "idx_t_c" ON "t" ("c")'
    ]
    assert ORA.render_drop_index("t", "c") == ['DROP INDEX IF EXISTS "idx_t_c"']
    assert ORA.render_create_composite_index("t", "ix", ["a", "b"], safe=False) == [
        'CREATE INDEX "ix" ON "t" ("a", "b")'
    ]
    assert ORA.render_drop_composite_index("ix") == ['DROP INDEX IF EXISTS "ix"']
    assert ORA.render_rename_index("t", "c", "ix_old", "ix_new") == [
        'ALTER INDEX "ix_old" RENAME TO "ix_new"'
    ]


def test_oracle_constraint_renderers():
    """
    GIVEN a constraint drop and rename
    WHEN rendered on Oracle
    THEN ALTER TABLE DROP/RENAME CONSTRAINT statements are produced
    """
    assert ORA.render_drop_constraint("t", "uq") == [
        'ALTER TABLE "t" DROP CONSTRAINT "uq"'
    ]
    assert ORA.render_rename_constraint("t", "uq", "uq2") == [
        'ALTER TABLE "t" RENAME CONSTRAINT "uq" TO "uq2"'
    ]


def test_oracle_date_part_and_json_extract():
    """
    GIVEN a TO_CHAR-backed date part, an unsupported part, and a JSON path
    WHEN rendered on Oracle
    THEN TO_NUMBER(TO_CHAR(...)) / JSON_VALUE(...) are produced and unsupported
    parts raise
    """
    assert ORA.date_part_sql("quarter", '"c"') == "TO_NUMBER(TO_CHAR(\"c\", 'Q'))"
    with pytest.raises(UnSupportedError):
        ORA.date_part_sql("century", '"c"')
    assert ORA.json_extract_sql('"c"', ["a", "b"]) == "JSON_VALUE(\"c\", '$.\"a\".\"b\"')"
    assert ORA.json_extract_sql('"c"', []) == '"c"'  # no keys -> bare column


def test_oracle_decode_helpers_parse_text():
    """
    GIVEN JSON and time columns Oracle hands back as text
    WHEN decoded
    THEN the string is parsed into the Python value
    """
    assert _json_from_db('{"a": 1}') == {"a": 1}
    assert _time_from_db("12:30:00") == _dt.time(12, 30, 0)
    # Already-decoded values pass through unchanged.
    assert _json_from_db({"a": 1}) == {"a": 1}
    assert _time_from_db(_dt.time(1, 2, 3)) == _dt.time(1, 2, 3)


def test_uuid_field_to_python_none_and_text():
    """
    GIVEN a UUID field decoding a NULL and a text uuid (Oracle stores uuids as
    VARCHAR2 and hands them back as strings)
    WHEN to_python runs
    THEN None passes through and text is reconstructed into a UUID
    """
    import uuid as _uuid

    from yara_orm.fields import UUIDField

    field = UUIDField()
    assert field.to_python(None) is None
    u = _uuid.uuid4()
    assert field.to_python(str(u)) == u

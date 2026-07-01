"""Dialect DDL renderers: rename, constraint, alter-column and create-table
edge cases that the migration operations build on.

PostgreSQL alters in place; SQLite rebuilds or rejects, so each renderer is
exercised on both dialects to lock in the capability-flag behaviour.
"""

import pytest

from yara_orm.dialects import BaseDialect, PostgresDialect, SqliteDialect
from yara_orm.exceptions import UnSupportedError

PG = PostgresDialect()
LITE = SqliteDialect()
BASE = BaseDialect()


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

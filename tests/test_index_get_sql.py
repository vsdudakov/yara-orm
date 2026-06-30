"""``Index.get_sql()`` — render an index's CREATE INDEX DDL for introspection.

Parity with Tortoise's ``Index.get_sql``; the field names resolve against the
model's columns and the active dialect's rules (PostgreSQL-only options are
dropped on SQLite).

The ``Index`` objects are built inline (not declared in ``Meta.indexes``) so the
test model stays creatable by a global ``generate_schemas()`` regardless of
which PostgreSQL extensions (e.g. ``pg_trgm``) are installed.
"""

import pytest

from yara_orm import Index, Model, fields
from yara_orm.dialects import PostgresDialect, SqliteDialect


class IxDoc(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)
    status = fields.CharField(max_length=20)

    class Meta:
        table = "ix_doc"


def test_get_sql_partial_index_postgres():
    """
    GIVEN a partial index
    WHEN get_sql renders for PostgreSQL
    THEN it emits the WHERE predicate and an IF NOT EXISTS guard
    """
    index = Index(fields=["status"], condition="status = 'active'")
    sql = index.get_sql(IxDoc, PostgresDialect())
    assert sql == (
        'CREATE INDEX IF NOT EXISTS "idx_ix_doc_status" '
        'ON "ix_doc" ("status") WHERE status = \'active\''
    )


def test_get_sql_drops_postgres_only_options_on_sqlite():
    """
    GIVEN an index using a GIN method and an operator class
    WHEN get_sql renders for SQLite
    THEN the PostgreSQL-only USING/opclass are dropped
    """
    index = Index(fields=["name"], using="gin", opclass="gin_trgm_ops")
    sql = index.get_sql(IxDoc, SqliteDialect(), safe=False)
    assert sql == 'CREATE INDEX "idx_ix_doc_name" ON "ix_doc" ("name")'


def test_get_sql_unique_multicolumn():
    """
    GIVEN a named unique multi-column index
    WHEN get_sql renders
    THEN it emits CREATE UNIQUE INDEX with both columns and the given name
    """
    index = Index(fields=["name", "status"], unique=True, name="uq_name_status")
    sql = index.get_sql(IxDoc, PostgresDialect(), safe=False)
    assert sql == 'CREATE UNIQUE INDEX "uq_name_status" ON "ix_doc" ("name", "status")'


def test_get_sql_postgres_using_and_opclass():
    """
    GIVEN an index with a USING method and an operator class
    WHEN get_sql renders for PostgreSQL
    THEN both are emitted
    """
    index = Index(fields=["name"], using="gin", opclass="gin_trgm_ops")
    sql = index.get_sql(IxDoc, PostgresDialect(), safe=False)
    assert sql == 'CREATE INDEX "idx_ix_doc_name" ON "ix_doc" USING gin ("name" gin_trgm_ops)'


def test_get_sql_include_covering_columns():
    """
    GIVEN an index with non-key covering columns
    WHEN get_sql renders for PostgreSQL
    THEN they are emitted as INCLUDE (...)
    """
    index = Index(fields=["status"], include=["name"], name="ix_cover")
    sql = index.get_sql(IxDoc, PostgresDialect(), safe=False)
    assert sql == 'CREATE INDEX "ix_cover" ON "ix_doc" ("status") INCLUDE ("name")'


@pytest.mark.asyncio
async def test_get_sql_defaults_to_connection_dialect(orm):
    """
    GIVEN an initialised connection (no tables needed)
    WHEN get_sql is called without an explicit dialect
    THEN it renders against the connection's dialect
    """
    sql = Index(fields=["status"]).get_sql(IxDoc)
    assert "CREATE INDEX" in sql
    assert '"ix_doc"' in sql
    assert '"status"' in sql

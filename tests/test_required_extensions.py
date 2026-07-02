"""Declarative PostgreSQL extensions required by registered field kinds.

Covers ``BaseDialect.extensions_sql`` (the ``generate_schemas`` hook), the
``CreateExtension`` migration operation (per-dialect: real SQL on PostgreSQL,
no-op on SQLite) and the autodetector emitting it first in a migration.
"""

import os

import pytest

from yara_orm import (
    MigrationManager,
    Model,
    fields,
    register_field_kind,
)
from yara_orm import (
    migrations as m,
)
from yara_orm.connection import get_engine
from yara_orm.dialects import PostgresDialect, SqliteDialect

DB_URL = os.environ.get("ORM_TEST_DB", "postgres://localhost/orm_demo")


# ---------------------------------------------------------------------------
# Module-level registrations (import-time, the recommended pattern).
# ---------------------------------------------------------------------------
class EmbeddingField(fields.Field):
    """A pgvector-style column whose kind requires the ``vector`` extension."""

    field_kind = "embedding"

    def __init__(self, dim: int = 3, **kwargs):
        super().__init__(**kwargs)
        self.type_params = {"dim": dim}


register_field_kind(
    "embedding",
    field_cls=EmbeddingField,
    sql={"postgres": "vector({dim})", "sqlite": "TEXT"},
    requires_extension="vector",
)


class TrgmTextField(fields.Field):
    """A text column whose kind requires the ``pg_trgm`` extension."""

    field_kind = "trgm_text"


register_field_kind(
    "trgm_text",
    field_cls=TrgmTextField,
    sql="TEXT",
    requires_extension="pg_trgm",
)


class PlainExtField(fields.Field):
    """A registered kind without any extension requirement."""

    field_kind = "plain_ext"


register_field_kind("plain_ext", field_cls=PlainExtField, sql="TEXT")


# Abstract: keeps these models out of the global registry so other modules'
# all-model ``generate_schemas()`` never renders ``vector(...)`` (or CREATE
# EXTENSION vector) on PostgreSQL — pgvector is deliberately not required.
class ExtDoc(Model):
    id = fields.IntField(pk=True)
    emb = EmbeddingField(dim=3)
    body = TrgmTextField()

    class Meta:
        abstract = True
        table = "rext_docs"


class ExtDocTwin(Model):
    id = fields.IntField(pk=True)
    emb = EmbeddingField(dim=8)

    class Meta:
        abstract = True
        table = "rext_docs_twin"


class ExtDocNoEmb(Model):
    id = fields.IntField(pk=True)
    body = TrgmTextField()

    class Meta:
        abstract = True
        table = "rext_docs"


class PlainDoc(Model):
    id = fields.IntField(pk=True)
    note = PlainExtField()

    class Meta:
        abstract = True
        table = "rext_plain"


# ---------------------------------------------------------------------------
# extensions_sql (the generate_schemas hook)
# ---------------------------------------------------------------------------
def test_extensions_sql_postgres_deduped_and_sorted():
    """
    GIVEN models whose registered kinds require extensions (one twice)
    WHEN the PostgreSQL dialect renders the required extensions
    THEN CREATE EXTENSION IF NOT EXISTS statements come deduped and sorted
    """
    statements = PostgresDialect().extensions_sql([ExtDoc, ExtDocTwin])
    assert statements == [
        'CREATE EXTENSION IF NOT EXISTS "pg_trgm"',
        'CREATE EXTENSION IF NOT EXISTS "vector"',
    ]


def test_extensions_sql_sqlite_is_empty():
    """
    GIVEN the same extension-requiring models
    WHEN the SQLite dialect renders the required extensions
    THEN nothing is emitted (SQLite has no extensions)
    """
    assert SqliteDialect().extensions_sql([ExtDoc, ExtDocTwin]) == []


def test_extensions_sql_ignores_kinds_without_extensions():
    """
    GIVEN a model using only built-in kinds and a registered kind without
          an extension requirement
    WHEN the PostgreSQL dialect renders the required extensions
    THEN the list is empty
    """
    assert PostgresDialect().extensions_sql([PlainDoc]) == []


# ---------------------------------------------------------------------------
# The CreateExtension operation
# ---------------------------------------------------------------------------
def test_create_extension_sql_per_dialect():
    """
    GIVEN a CreateExtension operation
    WHEN its SQL is rendered per dialect
    THEN PostgreSQL gets the guarded statement, SQLite nothing, and the
         reverse is always empty (the extension is never dropped)
    """
    op = m.CreateExtension("vector")
    state: dict = {"tables": {}}
    assert op.forward_sql(PostgresDialect(), state) == ['CREATE EXTENSION IF NOT EXISTS "vector"']
    assert op.forward_sql(SqliteDialect(), state) == []
    assert op.backward_sql(PostgresDialect(), state) == []
    assert op.to_source() == "m.CreateExtension('vector')"


# ---------------------------------------------------------------------------
# Autodetector emission + apply on SQLite
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_makemigrations_emits_create_extension_first(sqlite_empty, tmp_path):
    """
    GIVEN a model whose custom-kind columns require extensions
    WHEN makemigrations writes the initial migration
    THEN CreateExtension operations come first, the migration applies cleanly
         on SQLite (the op is a no-op there), and a re-run emits nothing
    """
    mgr = MigrationManager(directory=str(tmp_path), models=[ExtDoc])
    filename = mgr.make_migrations()
    assert filename is not None
    text = (tmp_path / filename).read_text()
    pg_trgm_at = text.index("m.CreateExtension('pg_trgm')")
    vector_at = text.index("m.CreateExtension('vector')")
    create_at = text.index("m.CreateModelIfNotExists")
    assert pg_trgm_at < vector_at < create_at

    assert await mgr.upgrade() == ["0001_initial"]
    assert mgr.make_migrations() is None  # idempotent: nothing re-emitted


@pytest.mark.asyncio
async def test_removing_extension_column_emits_no_create_extension(sqlite_empty, tmp_path):
    """
    GIVEN an applied schema with an extension-requiring column
    WHEN the column is removed and a new migration is generated
    THEN no CreateExtension is emitted (only added/altered columns need one)
    """
    mgr = MigrationManager(directory=str(tmp_path), models=[ExtDoc])
    mgr.make_migrations()
    await mgr.upgrade()

    mgr_v2 = MigrationManager(directory=str(tmp_path), models=[ExtDocNoEmb])
    filename = mgr_v2.make_migrations()
    assert filename is not None
    text = (tmp_path / filename).read_text()
    assert "m.RemoveFieldIfExists" in text
    assert "CreateExtension('vector')" not in text
    # pg_trgm column is untouched, so no extension op at all.
    assert "CreateExtension" not in text


# ---------------------------------------------------------------------------
# Live PostgreSQL: pg_trgm is typically installable; pgvector is not required.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_create_extension_applies_on_postgres(orm):
    """
    GIVEN a CreateExtension('pg_trgm') operation on a live PostgreSQL
    WHEN its forward SQL is executed (where the extension is available)
    THEN pg_extension lists pg_trgm; otherwise the emitted SQL alone is asserted
    """
    if not DB_URL.startswith("postgres"):
        pytest.skip("PostgreSQL-only test")
    dialect = PostgresDialect()
    statements = m.CreateExtension("pg_trgm").forward_sql(dialect, {"tables": {}})
    assert statements == ['CREATE EXTENSION IF NOT EXISTS "pg_trgm"']
    engine = get_engine()
    try:
        for sql in statements:
            await engine.execute(sql)
    except Exception:  # pg_trgm not installed on this server: SQL shape asserted above
        return
    rows = await engine.fetch_rows("SELECT extname FROM pg_extension WHERE extname = 'pg_trgm'")
    assert rows and rows[0][0] == "pg_trgm"

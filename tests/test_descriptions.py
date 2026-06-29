"""Comments: field `description` and Meta.table_description become SQL COMMENTs."""

import pytest

from yara_orm import Model, YaraOrm, fields
from yara_orm.connection import get_engine


class Described(Model):
    name = fields.CharField(max_length=50, description="the display name")
    note = fields.TextField(null=True)

    class Meta:
        table = "d_described"
        table_description = "a fully described table"


async def _reset():
    engine = get_engine()
    await engine.execute("DROP TABLE IF EXISTS d_described CASCADE")
    await YaraOrm.generate_schemas()


# Postgres-only: SQLite has no COMMENT support nor a way to query object comments.
@pytest.mark.asyncio
async def test_table_comment(orm):
    """
    GIVEN a model whose Meta declares a table_description
    WHEN the schema is generated
    THEN the table carries that comment in PostgreSQL
    """
    await _reset()
    engine = get_engine()
    rows = await engine.fetch_rows("SELECT obj_description('d_described'::regclass, 'pg_class')")
    assert rows[0][0] == "a fully described table"


# Postgres-only: SQLite has no COMMENT support nor a way to query object comments.
@pytest.mark.asyncio
async def test_column_comment(orm):
    """
    GIVEN a field declared with description=
    WHEN the schema is generated
    THEN the column carries that comment in PostgreSQL
    """
    await _reset()
    engine = get_engine()
    rows = await engine.fetch_rows(
        """
        SELECT col_description('d_described'::regclass, (
            SELECT ordinal_position FROM information_schema.columns
            WHERE table_name = 'd_described' AND column_name = 'name'))
        """
    )
    assert rows[0][0] == "the display name"

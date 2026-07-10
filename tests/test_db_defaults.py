"""Database-side default expressions (Now, RandomHex, SqlDefault)."""

import datetime as dt

import pytest

from yara_orm import DatabaseDefault, Model, Now, RandomHex, SqlDefault, db_defaults, fields
from yara_orm.dialects import PostgresDialect, SqliteDialect


class DdDoc(Model):
    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=20)
    created = fields.DatetimeField(default=Now())
    token = fields.CharField(max_length=64, default=RandomHex(8))
    flag = fields.IntField(default=SqlDefault("7"))

    class Meta:
        table = "dd_doc"


class RvfDoc(Model):
    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=50)
    expires = fields.DatetimeField(null=True, default=db_defaults.Now())

    class Meta:
        table = "rvf_doc"


MODELS = [DdDoc, RvfDoc]


def test_to_sql_per_dialect():
    """
    GIVEN the database-default expressions
    WHEN rendered against each dialect
    THEN Now/SqlDefault are portable and RandomHex differs per backend
    """
    pg, lite = PostgresDialect(), SqliteDialect()
    assert Now().to_sql(pg) == "CURRENT_TIMESTAMP"
    assert SqlDefault("7").to_sql(lite) == "7"
    assert "randomblob(4)" in RandomHex(4).to_sql(lite)
    assert "md5" in RandomHex(4).to_sql(pg)
    with pytest.raises(NotImplementedError):
        DatabaseDefault().to_sql(lite)


@pytest.mark.asyncio
async def test_database_defaults_filled_on_insert(db):
    """
    GIVEN columns with database-side defaults
    WHEN a row is created without supplying them
    THEN the database fills each value (timestamp, random hex, literal)
    """
    doc = await DdDoc.create(title="hi")
    fresh = await DdDoc.get(id=doc.id)
    assert isinstance(fresh.created, dt.datetime)
    # A non-empty hex string (length differs per backend: SQLite honours the
    # byte count, PostgreSQL uses a 32-char md5).
    assert fresh.token and all(c in "0123456789abcdef" for c in fresh.token)
    assert fresh.flag == 7


@pytest.mark.asyncio
async def test_database_default_via_bulk_create(db):
    """
    GIVEN a model with database-side defaults
    WHEN rows are bulk-created
    THEN the defaults are applied to every inserted row
    """
    await DdDoc.bulk_create([DdDoc(title=f"b{i}") for i in range(3)])
    docs = await DdDoc.all()
    assert len(docs) == 3
    assert all(d.flag == 7 and d.created is not None for d in docs)


@pytest.mark.asyncio
async def test_database_default_with_explicit_pk(db):
    """
    GIVEN a row created with an explicit primary key
    WHEN it omits its database-default columns
    THEN the explicit-pk insert path still lets the database fill them
    """
    doc = await DdDoc.create(id=99, title="explicit")
    assert doc.id == 99
    fresh = await DdDoc.get(id=99)
    assert fresh.created is not None and fresh.flag == 7


# ---------------------------------------------------------------------------
# explicit None on a nullable db-default column must persist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explicit_none_on_db_default_column_persists(db):
    """
    GIVEN a fetched row whose nullable db-default column holds a DB value
    WHEN the attribute is explicitly set to None and the instance fully saved
    THEN the column is written and reads back as NULL
    """
    created = await RvfDoc.create(title="d")
    doc = await RvfDoc.get(id=created.id)
    assert doc.expires is not None
    doc.expires = None
    await doc.save()
    assert (await RvfDoc.get(id=created.id)).expires is None


@pytest.mark.asyncio
async def test_unfetched_db_default_still_protected_on_full_save(db):
    """
    GIVEN a create() that did not fetch the DB-supplied default
    WHEN the instance is fully saved without touching the column
    THEN the database keeps its generated value (not overwritten with None)
    """
    doc = await RvfDoc.create(title="d")
    assert doc.expires is None  # not fetched (fetch_db_defaults off)
    doc.title = "renamed"
    await doc.save()
    refreshed = await RvfDoc.get(id=doc.id)
    assert refreshed.title == "renamed"
    assert refreshed.expires is not None

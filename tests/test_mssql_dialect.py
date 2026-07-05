"""Unit tests for the SQL Server dialect (no live server required).

These exercise the T-SQL SQL-generation divergences so the paths stay covered
even when the DB-backed suite runs without a SQL Server instance.
"""

import pytest

from yara_orm.db_defaults import Now, RandomHex
from yara_orm.dialects import BaseDialect, SqlServerDialect, get_dialect
from yara_orm.exceptions import UnSupportedError


def test_mssql_is_registered():
    """
    GIVEN the dialect registry
    WHEN "mssql" is resolved
    THEN it yields a SqlServerDialect
    """
    dialect = get_dialect("mssql")
    assert isinstance(dialect, SqlServerDialect)
    assert isinstance(dialect, BaseDialect)
    assert dialect.name == "mssql"


def test_capability_flags():
    """
    GIVEN SQL Server's feature set
    WHEN its capability flags are read
    THEN RETURNING and FOR UPDATE are off; multi-row insert is on
    """
    d = SqlServerDialect()
    assert d.supports_insert_returning is False
    assert d.supports_for_update is False
    assert d.supports_multirow_insert is True
    assert d.group_by_functional_dependency is False


def test_placeholder_and_quote():
    """
    GIVEN T-SQL syntax
    WHEN placeholders and identifiers are rendered
    THEN they use @PN binds and [bracket] quoting (with ] doubled)
    """
    d = SqlServerDialect()
    assert d.placeholder(1) == "@P1"
    assert d.placeholder(12) == "@P12"
    assert d.quote("col") == "[col]"
    assert d.quote("weird]name") == "[weird]]name]"


def test_concat_and_cast_text():
    """
    GIVEN string helpers
    WHEN rendered for SQL Server
    THEN CONCAT joins operands and CAST targets NVARCHAR
    """
    d = SqlServerDialect()
    assert d.concat_sql(["a", "b", "c"]) == "CONCAT(a, b, c)"
    assert d.cast_text("[x]") == "CAST([x] AS NVARCHAR(4000))"


def test_limit_offset_uses_offset_fetch():
    """
    GIVEN slicing
    WHEN rendered for SQL Server
    THEN it emits OFFSET ... ROWS FETCH NEXT ... ROWS ONLY
    """
    d = SqlServerDialect()
    assert d.limit_offset_sql(None, None) == ""
    assert d.limit_offset_sql(10, None) == " OFFSET 0 ROWS FETCH NEXT 10 ROWS ONLY"
    assert d.limit_offset_sql(None, 5) == " OFFSET 5 ROWS"
    assert d.limit_offset_sql(10, 5) == " OFFSET 5 ROWS FETCH NEXT 10 ROWS ONLY"


def test_offset_order_fallback():
    """
    GIVEN OFFSET/FETCH pagination (only legal after ORDER BY)
    WHEN a paginated query imposes no ordering of its own
    THEN the dialect supplies a placeholder ORDER BY (SELECT NULL)
    """
    assert SqlServerDialect().offset_order_fallback() == " ORDER BY (SELECT NULL)"
    # The portable default needs no such placeholder.
    assert BaseDialect().offset_order_fallback() == ""


def test_like_pattern_case_sensitivity():
    """
    GIVEN case-sensitive vs case-insensitive pattern lookups
    WHEN rendered for SQL Server
    THEN case-sensitive forces a binary COLLATE; case-insensitive is plain LIKE
    """
    d = SqlServerDialect()
    ci = d.like_pattern_sql(True, "[c]", "@P1")
    cs = d.like_pattern_sql(False, "[c]", "@P1")
    assert "COLLATE" not in ci
    assert "COLLATE Latin1_General_BIN2" in cs


def test_date_part_and_truncate_and_json():
    """
    GIVEN date/JSON query helpers
    WHEN rendered for SQL Server
    THEN they use DATEPART, CAST(... AS DATE) and JSON_VALUE
    """
    d = SqlServerDialect()
    assert d.date_part_sql("year", "[c]") == "DATEPART(year, [c])"
    assert d.date_part_sql("week", "[c]") == "DATEPART(iso_week, [c])"
    assert d.truncate_date_sql("[c]") == "CAST([c] AS DATE)"
    assert d.json_extract_sql("[c]", ["a", "b"]) == 'JSON_VALUE([c], \'$."a"."b"\')'
    assert d.json_extract_sql("[c]", []) == "[c]"
    with pytest.raises(UnSupportedError):
        d.date_part_sql("nanosecond", "[c]")


def test_regex_is_unsupported():
    """
    GIVEN a regex lookup
    WHEN rendered for SQL Server (which has no REGEXP)
    THEN it raises UnSupportedError
    """
    with pytest.raises(UnSupportedError):
        SqlServerDialect().regex_sql("regex", "[c]", "@P1")


def test_render_upsert_is_a_merge():
    """
    GIVEN a bulk upsert
    WHEN rendered for SQL Server
    THEN it produces a MERGE against a VALUES row source
    """
    d = SqlServerDialect()
    merge = d.render_upsert("[t]", ["a", "b"], 2, [], ["b"], ["a"])
    assert merge.startswith("MERGE INTO [t] AS d USING (VALUES (@P1, @P2), (@P3, @P4))")
    assert "ON (d.[a] = s.[a])" in merge
    assert "WHEN MATCHED THEN UPDATE SET d.[b] = s.[b]" in merge
    assert "WHEN NOT MATCHED THEN INSERT ([a], [b]) VALUES (s.[a], s.[b])" in merge
    assert merge.endswith(";")
    # Ignore-only (no update columns): no MATCHED branch.
    ignore = d.render_upsert("[t]", ["a", "b"], 1, [], [], ["a"])
    assert "WHEN MATCHED" not in ignore
    with pytest.raises(UnSupportedError):
        d.on_conflict_sql(["a"], ["b"])


def test_database_defaults_use_tsql_spelling():
    """
    GIVEN database-side defaults
    WHEN rendered for SQL Server
    THEN they use SYSDATETIME() and CRYPT_GEN_RANDOM, not the SQLite fallback
    """
    d = SqlServerDialect()
    assert Now().to_sql(d) == "SYSDATETIME()"
    assert RandomHex(8).to_sql(d) == "LOWER(CONVERT(VARCHAR(16), CRYPT_GEN_RANDOM(8), 2))"


def test_render_drop_table():
    """
    GIVEN a migration drop
    WHEN rendered for SQL Server
    THEN it uses DROP TABLE IF EXISTS with bracket quoting
    """
    assert SqlServerDialect().render_drop_table("t") == ["DROP TABLE IF EXISTS [t]"]

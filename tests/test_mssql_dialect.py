"""Unit tests for the SQL Server dialect (no live server required).

These exercise the T-SQL SQL-generation divergences so the paths stay covered
even when the DB-backed suite runs without a SQL Server instance.
"""

import pytest

from yara_orm import Model, fields
from yara_orm.db_defaults import Now, RandomHex
from yara_orm.dialects import BaseDialect, SqlServerDialect, get_dialect
from yara_orm.exceptions import UnSupportedError


class MsDlUser(Model):
    id = fields.IntField(pk=True)

    class Meta:
        table = "ms_dl_user"


class MsDlBook(Model):
    id = fields.IntField(pk=True)
    # A single-path cascade — legal on SQL Server, so CASCADE is preserved.
    author = fields.ForeignKeyField("MsDlUser", related_name="books")

    class Meta:
        table = "ms_dl_book"


class MsDlTicket(Model):
    id = fields.IntField(pk=True)
    # Two FKs to the same table (a second cascade path) plus a self-reference:
    # SQL Server rejects both with error 1785, so each must fall back to NO ACTION.
    created_by = fields.ForeignKeyField("MsDlUser", related_name="created")
    updated_by = fields.ForeignKeyField("MsDlUser", related_name="updated", null=True)
    parent = fields.ForeignKeyField("MsDlTicket", related_name="children", null=True)

    class Meta:
        table = "ms_dl_ticket"


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
    assert d.json_extract_sql("[c]", ["a", "b"]) == 'JSON_VALUE([c], N\'$."a"."b"\')'
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


def test_create_table_guards_with_object_id_and_drops_comments():
    """
    GIVEN a table create on SQL Server (no CREATE TABLE IF NOT EXISTS / COMMENT ON)
    WHEN create_table_sql renders it
    THEN safe=True prefixes an IF OBJECT_ID guard and no COMMENT ON is emitted
    """
    d = SqlServerDialect()
    guarded = d.create_table_sql(MsDlBook._meta, safe=True)[0]
    assert guarded.startswith("IF OBJECT_ID(N'[ms_dl_book]', 'U') IS NULL\nCREATE TABLE")
    unguarded = d.create_table_sql(MsDlBook._meta, safe=False)[0]
    assert unguarded.startswith("CREATE TABLE [ms_dl_book]")
    assert "COMMENT ON" not in guarded
    assert d._comment_sql(MsDlBook._meta) == []


def test_create_table_downgrades_only_illegal_cascades():
    """
    GIVEN multi-path and self-referential FKs (SQL Server error 1785)
    WHEN create_table_sql renders the table
    THEN those FKs fall back to NO ACTION while a single-path cascade is kept
    """
    d = SqlServerDialect()
    ticket = d.create_table_sql(MsDlTicket._meta, safe=False)[0]
    # Two FKs share ms_dl_user (a second cascade path) -> both NO ACTION.
    assert ticket.count("REFERENCES [ms_dl_user] ([id]) ON DELETE NO ACTION") == 2
    # The self-referential FK -> NO ACTION.
    assert "REFERENCES [ms_dl_ticket] ([id]) ON DELETE NO ACTION" in ticket
    assert "CASCADE" not in ticket
    # A lone single-path cascade stays CASCADE (legal on SQL Server).
    book = d.create_table_sql(MsDlBook._meta, safe=False)[0]
    assert "REFERENCES [ms_dl_user] ([id]) ON DELETE CASCADE" in book


def test_render_drop_table():
    """
    GIVEN a migration drop
    WHEN rendered for SQL Server
    THEN it uses DROP TABLE IF EXISTS with bracket quoting
    """
    assert SqlServerDialect().render_drop_table("t") == ["DROP TABLE IF EXISTS [t]"]


class MsDlDoc(Model):
    id = fields.IntField(pk=True)
    # A MAX-typed (NVARCHAR(MAX)) column: SQL Server cannot index it (error 1919),
    # so the inline index request is dropped from the CREATE TABLE.
    body = fields.JSONField(index=True)
    # A plain indexable column keeps its inline INDEX.
    slug = fields.CharField(max_length=50, index=True)

    class Meta:
        table = "ms_dl_doc"


class MsDlNick(Model):
    id = fields.IntField(pk=True)
    # A *nullable* UNIQUE column: SQL Server treats NULLs as equal, so the inline
    # UNIQUE is dropped and a filtered unique index over non-NULL rows is emitted.
    nick = fields.CharField(max_length=50, unique=True, null=True)

    class Meta:
        table = "ms_dl_nick"


class MsDlPost(Model):
    id = fields.IntField(pk=True)
    tags = fields.ManyToManyField("MsDlUser", related_name="tagged_posts")

    class Meta:
        table = "ms_dl_post"


def test_create_table_skips_max_column_indexes():
    """
    GIVEN a model with an indexed MAX-typed column and an indexed plain column
    WHEN create_table_sql renders it
    THEN the MAX column's INDEX is dropped (error 1919) but the plain one stays
    """
    sql = SqlServerDialect().create_table_sql(MsDlDoc._meta, safe=False)[0]
    assert "INDEX [idx_ms_dl_doc_slug] ([slug])" in sql
    # The MAX-typed column is still declared, but no INDEX clause covers it.
    assert "[body] NVARCHAR(MAX)" in sql
    assert "idx_ms_dl_doc_body" not in sql


def test_create_table_nullable_unique_uses_filtered_index():
    """
    GIVEN a nullable UNIQUE column
    WHEN create_table_sql renders the table
    THEN the inline UNIQUE is dropped and a filtered unique index is appended,
         guarded by sys.indexes only when safe=True
    """
    d = SqlServerDialect()
    guarded = d.create_table_sql(MsDlNick._meta, safe=True)
    create, idx = guarded[0], guarded[1]
    assert "UNIQUE" not in create  # inline UNIQUE deferred to the filtered index
    assert idx.startswith("IF NOT EXISTS (SELECT 1 FROM sys.indexes")
    assert "CREATE UNIQUE INDEX [uq_ms_dl_nick_nick] ON [ms_dl_nick]" in idx
    assert "WHERE [nick] IS NOT NULL" in idx
    # safe=False drops the existence guard.
    unguarded = d.create_table_sql(MsDlNick._meta, safe=False)[1]
    assert "IF NOT EXISTS" not in unguarded
    assert unguarded.startswith("CREATE UNIQUE INDEX [uq_ms_dl_nick_nick]")


def test_create_m2m_table_guard_toggles_with_safe():
    """
    GIVEN a many-to-many join table
    WHEN create_m2m_table_sql renders it
    THEN safe=True prefixes an IF OBJECT_ID guard and safe=False omits it
    """
    d = SqlServerDialect()
    info = MsDlPost._meta.m2m["tags"]
    info.finalize()
    guarded = d.create_m2m_table_sql(info, safe=True)[0]
    assert guarded.startswith("IF OBJECT_ID(N'")
    unguarded = d.create_m2m_table_sql(info, safe=False)[0]
    assert unguarded.startswith("CREATE TABLE")


def test_render_create_table_spec_paths():
    """
    GIVEN a migration table spec with no pk, FKs, constraints and indexes
    WHEN render_create_table renders it for SQL Server
    THEN the pk clause is skipped, illegal-cascade FKs fall back to NO ACTION,
         constraints/indexes render and safe=False omits the OBJECT_ID guard
    """
    d = SqlServerDialect()
    tspec = {
        "columns": {
            "a": {
                "kind": "int",
                "type_params": {},
                "null": False,
                "unique": False,
                "pk": False,
                "auto_increment": False,
            },
            "b": {
                "kind": "int",
                "type_params": {},
                "null": True,
                "unique": False,
                "pk": False,
                "auto_increment": False,
            },
            "c": {
                "kind": "int",
                "type_params": {},
                "null": True,
                "unique": False,
                "pk": False,
                "auto_increment": False,
            },
        },
        "pk": None,
        "fks": {
            # Self-referential FK -> NO ACTION (error 1785).
            "a": {"table": "t", "pk": "a", "on_delete": "CASCADE"},
            # RESTRICT -> NO ACTION (SQL Server has no RESTRICT keyword).
            "b": {"table": "other", "pk": "id", "on_delete": "RESTRICT"},
            # A lone single-path cascade stays CASCADE (legal on SQL Server).
            "c": {"table": "solo", "pk": "id", "on_delete": "CASCADE"},
        },
        "constraints": [{"kind": "check", "name": "ck", "check": "a > 0"}],
        "indexes": ["b"],
        "composite_indexes": {"ix_ab": {"columns": ["a", "b"], "unique": True}},
    }
    out = d.render_create_table("t", tspec, safe=False)
    create = out[0]
    assert "PRIMARY KEY" not in create
    assert create.startswith("CREATE TABLE [t]")  # safe=False -> no OBJECT_ID guard
    assert "REFERENCES [t] ([a]) ON DELETE NO ACTION" in create
    assert "REFERENCES [other] ([id]) ON DELETE NO ACTION" in create
    assert "REFERENCES [solo] ([id]) ON DELETE CASCADE" in create
    assert "CONSTRAINT [ck] CHECK (a > 0)" in create
    assert "CREATE INDEX [idx_t_b] ON [t] ([b])" in out
    assert "CREATE UNIQUE INDEX [ix_ab] ON [t] ([a], [b])" in out


def test_render_create_index_variants():
    """
    GIVEN single- and multi-column index renderers
    WHEN rendered for SQL Server with safe=False
    THEN UNIQUE and filtered (WHERE) variants render without any guard clause
         (safe=True prefixes a sys.indexes existence check instead)
    """
    d = SqlServerDialect()
    assert d.render_create_index("t", "c", safe=False, unique=True) == [
        "CREATE UNIQUE INDEX [idx_t_c] ON [t] ([c])"
    ]
    assert d.render_create_index("t", "c", safe=False, name="myidx") == [
        "CREATE INDEX [myidx] ON [t] ([c])"
    ]
    assert d.render_create_composite_index(
        "t", "ix", ["a", "b"], safe=False, condition="[a] > 0"
    ) == ["CREATE INDEX [ix] ON [t] ([a], [b]) WHERE [a] > 0"]
    assert d.render_create_composite_index("t", "ix", ["a"], safe=False, unique=True) == [
        "CREATE UNIQUE INDEX [ix] ON [t] ([a])"
    ]


def test_escape_like_value_escapes_bracket():
    """
    GIVEN a LIKE lookup value containing a SQL Server character-class bracket
    WHEN the SQL Server dialect escapes it
    THEN '[' is backslash-escaped (matched literally under ESCAPE '\\'), while
         the base dialect leaves it untouched

    Regression: '[' is a T-SQL LIKE metacharacter, so an unescaped value like
    'a[bc]' silently became a character class and broadened the match.
    """
    mssql = SqlServerDialect()
    base = BaseDialect()
    assert mssql.escape_like_value("a[bc]") == "a\\[bc]"
    assert base.escape_like_value("a[bc]") == "a[bc]"
    # The shared metacharacters are still escaped on both.
    assert mssql.escape_like_value("50%_x") == "50\\%\\_x"


# ---------------------------------------------------------------------------
# Guarded creates are re-run safe: each CREATE INDEX takes a sys.indexes guard
# ---------------------------------------------------------------------------
MS = SqlServerDialect()

_TSPEC = {
    "columns": {
        "id": {
            "kind": "int",
            "type_params": {},
            "null": False,
            "unique": False,
            "pk": True,
            "auto_increment": True,
        },
        "tag": {
            "kind": "varchar",
            "type_params": {"max_length": 20},
            "null": False,
            "unique": False,
            "pk": False,
            "auto_increment": False,
        },
        "rank": {
            "kind": "int",
            "type_params": {},
            "null": True,
            "unique": False,
            "pk": False,
            "auto_increment": False,
        },
    },
    "pk": "id",
    "fks": {},
    "indexes": ["tag"],
    "composite_indexes": {"ix_tag_rank": {"columns": ["tag", "rank"], "unique": True}},
}


def test_mssql_migration_create_table_guards_indexes():
    """
    GIVEN a guarded migration table spec with indexes
    WHEN render_create_table renders it on SQL Server
    THEN each CREATE INDEX is prefixed with a sys.indexes existence check
         (re-running against an existing schema would otherwise abort with
         error 1913), while safe=False keeps the bare statements
    """
    out = MS.render_create_table("t", _TSPEC, safe=True)
    assert out[0].startswith("IF OBJECT_ID(N'[t]', 'U') IS NULL")
    assert out[1] == (
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'idx_t_tag' "
        "AND object_id = OBJECT_ID(N'[t]'))\n"
        "CREATE INDEX [idx_t_tag] ON [t] ([tag])"
    )
    assert out[2] == (
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'ix_tag_rank' "
        "AND object_id = OBJECT_ID(N'[t]'))\n"
        "CREATE UNIQUE INDEX [ix_tag_rank] ON [t] ([tag], [rank])"
    )
    unguarded = MS.render_create_table("t", _TSPEC, safe=False)
    assert unguarded[1] == "CREATE INDEX [idx_t_tag] ON [t] ([tag])"
    assert unguarded[2] == "CREATE UNIQUE INDEX [ix_tag_rank] ON [t] ([tag], [rank])"


def test_mssql_standalone_index_renderers_take_the_guard():
    """
    GIVEN the standalone index renderers (AddIndexIfNotExists and friends)
    WHEN they render on SQL Server with safe=True
    THEN the sys.indexes guard prefixes the statement
    """
    [sql] = MS.render_create_index("t", "c", safe=True)
    assert sql.startswith("IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'idx_t_c' ")
    assert sql.endswith("CREATE INDEX [idx_t_c] ON [t] ([c])")
    [sql] = MS.render_create_composite_index("t", "ix", ["a", "b"], safe=True, unique=True)
    assert sql.startswith("IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'ix' ")
    assert sql.endswith("CREATE UNIQUE INDEX [ix] ON [t] ([a], [b])")

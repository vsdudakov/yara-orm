"""MySQL dialect SQL rendering — pure unit tests, no server required.

Locks in every MySQL-specific rendering decision: backtick quoting, ``?``
placeholders, the type map (incl. AUTO_INCREMENT primary keys), table-level
FOREIGN KEY clauses, inline index folding (MySQL has no ``CREATE INDEX IF NOT
EXISTS``), the 8.4-safe ``AS new ON DUPLICATE KEY UPDATE`` upsert and
``INSERT IGNORE``, the LIKE/LIKE BINARY case-sensitivity split, the capability
flags, migration-DDL overrides and the ``db_defaults`` expressions.
"""

import datetime as dt
import uuid

import pytest

from yara_orm import Index, Model, YaraOrm, fields
from yara_orm import timezone as tz
from yara_orm.db_defaults import Now, RandomHex, SqlDefault
from yara_orm.dialects import MySQLDialect, get_dialect
from yara_orm.exceptions import UnSupportedError
from yara_orm.migrations import CheckConstraint

MY = MySQLDialect()


class MyDlBook(Model):
    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=100, description="the title")
    body = fields.TextField(null=True)
    rating = fields.FloatField(default=0.0)
    tag = fields.CharField(max_length=20, index=True)

    class Meta:
        table = "my_dl_book"
        table_description = "books"
        indexes = (("title", "tag"),)
        unique_together = (("title", "rating"),)
        constraints = (CheckConstraint(check="rating >= 0", name="chk_rating"),)


class MyDlPage(Model):
    id = fields.BigIntField(pk=True)
    book = fields.ForeignKeyField("MyDlBook", related_name="pages")
    number = fields.SmallIntField()
    meta = fields.JSONField(null=True)

    class Meta:
        # No Meta.indexes here: the FK/JSON index rendering is exercised by
        # installing Index objects temporarily in the test, so a global
        # ``generate_schemas()`` from unrelated suites never emits them.
        table = "my_dl_page"


# -- registry / identifiers ----------------------------------------------------


def test_dialect_is_registered_under_mysql():
    """
    GIVEN the dialect registry
    WHEN "mysql" is resolved
    THEN a MySQLDialect instance comes back
    """
    assert isinstance(get_dialect("mysql"), MySQLDialect)


def test_quote_uses_backticks_and_doubles_embedded_backticks():
    """
    GIVEN identifiers, one containing a backtick
    WHEN quoted for MySQL
    THEN they are backtick-wrapped with embedded backticks doubled
    """
    assert MY.quote("users") == "`users`"
    assert MY.quote("we`ird") == "`we``ird`"


def test_placeholder_is_unnumbered_question_mark():
    """
    GIVEN any parameter positions
    WHEN placeholders render
    THEN every position renders the same bare "?"
    """
    assert MY.placeholder(1) == "?"
    assert MY.placeholder(42) == "?"


# -- capability flags ----------------------------------------------------------


def test_capability_flags_match_mysql_8():
    """
    GIVEN the MySQL dialect
    WHEN its capability flags are read
    THEN they reflect MySQL 8 syntax (no RETURNING, no IF EXISTS column guards,
         LIMIT required before OFFSET with the max-rows sentinel, no
         PostgreSQL-only index options)
    """
    assert MY.supports_insert_returning is False
    assert MY.insert_ignore_verb == "INSERT IGNORE"
    assert MY.insert_default_values_sql("id") == "() VALUES ()"
    assert MY.offset_requires_limit is True
    assert MY.no_limit == 18446744073709551615
    assert MY.column_if_exists is False
    assert MY.index_concurrently is False
    assert MY.index_using is False
    assert MY.index_include is False
    assert MY.index_opclass is False
    assert MY.supports_extensions is False
    assert MY.supports_for_update is True
    assert MY.random_function == "RAND()"
    assert MY.extensions_sql([MyDlBook]) == []


def test_like_operators_invert_the_collation_defaults():
    """
    GIVEN utf8mb4's case-insensitive default collation
    WHEN the LIKE spellings are read
    THEN icontains-family lookups use plain LIKE and case-sensitive lookups
         use LIKE BINARY, with the backslash ESCAPE literal doubled for
         MySQL's string-literal parsing
    """
    assert MY.ilike == "LIKE"
    assert MY.like == "LIKE BINARY"
    assert MY.like_escape == " ESCAPE '\\\\'"


# -- type map -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        (fields.SmallIntField(), "SMALLINT"),
        (fields.IntField(), "INT"),
        (fields.BigIntField(), "BIGINT"),
        (fields.CharField(max_length=50), "VARCHAR(50)"),
        (fields.TextField(), "LONGTEXT"),
        (fields.BooleanField(), "TINYINT(1)"),
        (fields.FloatField(), "DOUBLE"),
        (fields.DecimalField(max_digits=10, decimal_places=2), "DECIMAL(10, 2)"),
        (fields.DatetimeField(), "DATETIME(6)"),
        (fields.DateField(), "DATE"),
        (fields.TimeField(), "TIME(6)"),
        (fields.TimeDeltaField(), "BIGINT"),
        (fields.UUIDField(), "CHAR(36)"),
        (fields.JSONField(), "JSON"),
        (fields.BinaryField(), "LONGBLOB"),
    ],
)
def test_type_map_covers_every_field_kind(field, expected):
    """
    GIVEN each built-in field kind
    WHEN its column type renders on MySQL
    THEN the documented MySQL type comes out
    """
    field.model_field_name = "c"
    assert MY.column_type(field) == expected


def test_auto_increment_pk_types():
    """
    GIVEN auto-increment integer primary keys of each width
    WHEN the serial type renders
    THEN AUTO_INCREMENT is appended to the integer type
    """
    assert MY.serial_map == {
        "smallint": "SMALLINT AUTO_INCREMENT",
        "int": "INT AUTO_INCREMENT",
        "bigint": "BIGINT AUTO_INCREMENT",
    }
    pk = fields.IntField(pk=True)
    pk.model_field_name = "id"
    assert MY.column_type(pk) == "INT AUTO_INCREMENT"


# -- CREATE TABLE ---------------------------------------------------------------


def test_create_table_folds_indexes_inline_and_renders_comments():
    """
    GIVEN a model with an index=True column, a composite Meta index,
          unique_together, a CHECK constraint and descriptions
    WHEN its CREATE TABLE renders on MySQL
    THEN indexes appear as inline INDEX lines (MySQL has no CREATE INDEX IF NOT
         EXISTS), constraints stay table-level, the column comment rides the
         column and the table comment is a separate ALTER TABLE
    """
    statements = MY.create_table_sql(MyDlBook._meta)
    assert len(statements) == 2
    create = statements[0]
    assert create.startswith("CREATE TABLE IF NOT EXISTS `my_dl_book`")
    assert "`id` INT AUTO_INCREMENT" in create
    assert "PRIMARY KEY (`id`)" in create
    assert "`title` VARCHAR(100) NOT NULL COMMENT 'the title'" in create
    assert "UNIQUE (`title`, `rating`)" in create
    assert "CONSTRAINT `chk_rating` CHECK (rating >= 0)" in create
    assert "INDEX `idx_my_dl_book_tag` (`tag`)" in create
    assert "INDEX `idx_my_dl_book_title_tag` (`title`, `tag`)" in create
    assert "CREATE INDEX" not in create
    assert statements[1] == "ALTER TABLE `my_dl_book` COMMENT = 'books'"


def test_create_table_emits_table_level_foreign_keys():
    """
    GIVEN a model with a foreign key
    WHEN its CREATE TABLE renders on MySQL
    THEN the constraint is a table-level FOREIGN KEY clause (MySQL silently
         ignores column-level inline REFERENCES) targeting the parent pk type
    """
    MyDlPage._meta.indexes = [
        Index(fields=["book"], name="idx_page_book"),
        Index(fields=["meta"]),
    ]
    try:
        create = MY.create_table_sql(MyDlPage._meta, safe=False)[0]
    finally:
        MyDlPage._meta.indexes = []
    assert create.startswith("CREATE TABLE `my_dl_page`")
    assert "`book_id` INT NOT NULL" in create
    assert "FOREIGN KEY (`book_id`) REFERENCES `my_dl_book` (`id`) ON DELETE CASCADE" in create
    # An index through the relation name resolves to the FK column; the JSON
    # index is dropped (MySQL cannot index a JSON column directly).
    assert "INDEX `idx_page_book` (`book_id`)" in create
    assert "`meta`)" not in create


def test_index_get_sql_drops_guard_and_partial_condition():
    """
    GIVEN a unique index and a partial index
    WHEN Index.get_sql renders on MySQL
    THEN there is no IF NOT EXISTS guard and the (unsupported) WHERE predicate
         is dropped
    """
    unique = Index(fields=["title"], unique=True, name="uq_title")
    assert unique.get_sql(MyDlBook, MY) == (
        "CREATE UNIQUE INDEX `uq_title` ON `my_dl_book` (`title`)"
    )
    partial = Index(fields=["tag"], condition="tag <> ''")
    assert partial.get_sql(MyDlBook, MY) == (
        "CREATE INDEX `idx_my_dl_book_tag` ON `my_dl_book` (`tag`)"
    )


# -- upserts ---------------------------------------------------------------------


def test_on_conflict_update_uses_the_84_safe_alias_form():
    """
    GIVEN an upsert with update columns
    WHEN the conflict clause renders
    THEN it uses INSERT ... AS new ON DUPLICATE KEY UPDATE col = new.col
         (the VALUES() function is removed in MySQL 8.4)
    """
    clause = MY.on_conflict_sql(["id"], ["name", "score"])
    assert clause == (
        " AS `new` ON DUPLICATE KEY UPDATE `name` = `new`.`name`, `score` = `new`.`score`"
    )


def test_on_conflict_ignore_is_spelled_on_the_insert_verb():
    """
    GIVEN an ignore-conflicts insert (no update columns)
    WHEN the conflict clause renders
    THEN it is empty — skipping is spelled INSERT IGNORE via insert_ignore_verb
    """
    assert MY.on_conflict_sql([], []) == ""
    assert MY.on_conflict_sql(["id"], []) == ""


# -- query lookups -----------------------------------------------------------------


def test_query_expression_renderers():
    """
    GIVEN the per-dialect query expression hooks
    WHEN they render on MySQL
    THEN EXTRACT/CAST/JSON_EXTRACT/JSON_CONTAINS/CAST AS CHAR come out
    """
    assert MY.date_part_sql("year", "`t`.`c`") == "EXTRACT(YEAR FROM `t`.`c`)"
    assert MY.date_part_sql("microsecond", "`c`") == "EXTRACT(MICROSECOND FROM `c`)"
    assert MY.truncate_date_sql("`c`") == "CAST(`c` AS DATE)"
    assert MY.json_extract_sql("`c`", []) == "`c`"
    assert MY.json_extract_sql("`c`", ["a", "b"]) == (
        'JSON_UNQUOTE(JSON_EXTRACT(`c`, \'$."a"."b"\'))'
    )
    # Non-ASCII member names need the quoted-leg form.
    assert MY.json_extract_sql("`c`", ["ключ"]) == ("JSON_UNQUOTE(JSON_EXTRACT(`c`, '$.\"ключ\"'))")
    assert MY.json_contains_sql("`c`", "?") == "JSON_CONTAINS(`c`, ?)"
    assert MY.cast_text("`c`") == "CAST(`c` AS CHAR)"


def test_regex_lookups_render_regexp_like_with_case_flags():
    """
    GIVEN regex lookups on MySQL (whose infix REGEXP follows the collation and
          rejects BINARY under ICU)
    WHEN they render
    THEN REGEXP_LIKE carries the 'c' (sensitive) or 'i' (insensitive) flag
    """
    assert MY.regex_sql("regex", "`c`", "?") == "REGEXP_LIKE(`c`, ?, 'c')"
    assert MY.regex_sql("posix_regex", "`c`", "?") == "REGEXP_LIKE(`c`, ?, 'c')"
    assert MY.regex_sql("iregex", "`c`", "?") == "REGEXP_LIKE(`c`, ?, 'i')"
    assert MY.regex_sql("iposix_regex", "`c`", "?") == "REGEXP_LIKE(`c`, ?, 'i')"


def test_search_renders_match_against():
    """
    GIVEN the __search lookup on MySQL
    WHEN it renders
    THEN it emits MATCH ... AGAINST (natural language mode) and the dialect
         advertises search support
    """
    assert MY.supports_search is True
    assert MY.search_sql("`t`.`body`", "?") == "MATCH (`t`.`body`) AGAINST (?)"


def test_fulltext_index_renders_inline_and_standalone():
    """
    GIVEN an Index declared with using="fulltext"
    WHEN it renders on MySQL
    THEN both the standalone statement and the CREATE TABLE line spell
         FULLTEXT INDEX (what MATCH ... AGAINST requires)
    """
    ft = Index(fields=["title"], using="fulltext", name="ft_title")
    assert ft.get_sql(MyDlBook, MY) == (
        "CREATE FULLTEXT INDEX `ft_title` ON `my_dl_book` (`title`)"
    )
    MyDlPage._meta.indexes = [Index(fields=["number"], using="fulltext", name="ft_num")]
    try:
        create = MY.create_table_sql(MyDlPage._meta, safe=False)[0]
    finally:
        MyDlPage._meta.indexes = []
    assert "FULLTEXT INDEX `ft_num` (`number`)" in create


# -- row decoding -------------------------------------------------------------------


def test_read_decoder_reconstructs_uuids_from_char36_text():
    """
    GIVEN a UUIDField (CHAR(36) on MySQL, returned as str)
    WHEN the dialect's read decoder runs
    THEN the string is parsed back to uuid.UUID; UUIDs and None pass through
    """
    field = fields.UUIDField()
    field.model_field_name = "ref"
    decoder = MY.read_decoder(field)
    value = uuid.uuid4()
    assert decoder(str(value)) == value
    assert decoder(value) is value
    assert decoder(None) is None


def test_read_decoder_relabels_datetimes_utc_only_under_use_tz():
    """
    GIVEN a DatetimeField (DATETIME(6) on MySQL, always naive)
    WHEN the read decoder runs with use_tz on and off
    THEN the naive UTC value gains tzinfo=UTC only while use_tz is enabled
    """
    field = fields.DatetimeField()
    field.model_field_name = "created"
    decoder = MY.read_decoder(field)
    naive = dt.datetime(2024, 1, 2, 3, 4, 5, 123456)
    assert decoder(naive) == naive  # use_tz off: unchanged
    tz._set_config(use_tz=True)
    try:
        aware = decoder(naive)
        assert aware.tzinfo == dt.timezone.utc
        assert aware.replace(tzinfo=None) == naive
        # An already-aware value (defensive) passes through unchanged.
        assert decoder(aware) is aware
    finally:
        tz._set_config(use_tz=False)


def test_read_decoder_keeps_the_field_contract_for_other_kinds():
    """
    GIVEN identity-read and to_python-read fields
    WHEN the read decoder resolves on MySQL
    THEN identity fields get no converter and converting fields keep to_python
    """
    plain = fields.CharField(max_length=10)
    plain.model_field_name = "c"
    assert MY.read_decoder(plain) is None
    td = fields.TimeDeltaField()
    td.model_field_name = "d"
    decoder = MY.read_decoder(td)
    assert decoder(1_500_000) == dt.timedelta(seconds=1, microseconds=500_000)


def test_read_decoder_resolves_fk_columns_to_the_target_pk_kind():
    """
    GIVEN a foreign key column (its own kind is the "fk" pseudo-kind)
    WHEN the read decoder resolves
    THEN it adopts the referenced pk's kind (int here: identity, no converter)
    """
    fk = MyDlPage._meta.fields["book_id"]
    assert fk.field_kind == "fk"
    assert MY.read_decoder(fk) is None


def test_read_decoder_composes_with_a_fields_own_to_python():
    """
    GIVEN a uuid-kind field that also has its own to_python conversion
    WHEN the MySQL read decoder resolves
    THEN the dialect's uuid reconstruction runs first and the field's converter
         wraps its result
    """

    class WrappedUUIDField(fields.UUIDField):
        read_identity = False

        def to_python(self, value):
            return ("wrapped", value)

    field = WrappedUUIDField()
    field.model_field_name = "ref"
    decoder = MY.read_decoder(field)
    value = uuid.uuid4()
    assert decoder(str(value)) == ("wrapped", value)


def test_url_schemes_normalise_to_mysql():
    """
    GIVEN driver-qualified and alias MySQL URL schemes
    WHEN YaraOrm normalises them
    THEN each rewrites to mysql://; scheme-less strings pass through untouched
    """
    assert YaraOrm._normalize_url("mysql://u:p@h:3306/db") == "mysql://u:p@h:3306/db"
    assert YaraOrm._normalize_url("mysql+aiomysql://h/db") == "mysql://h/db"
    assert YaraOrm._normalize_url("mariadb://h/db") == "mysql://h/db"
    assert YaraOrm._normalize_url("asyncmy://h/db") == "mysql://h/db"
    assert YaraOrm._normalize_url("sqlite:/tmp/x.db") == "sqlite:/tmp/x.db"


# -- migration DDL overrides -----------------------------------------------------


def test_render_drop_table_has_no_cascade():
    """
    GIVEN a drop-table render
    WHEN it renders on MySQL
    THEN there is no CASCADE keyword (MySQL does not accept it)
    """
    assert MY.render_drop_table("t") == ["DROP TABLE IF EXISTS `t`"]


def test_render_alter_column_uses_modify_column():
    """
    GIVEN a type + nullability change
    WHEN it renders on MySQL
    THEN one MODIFY COLUMN restates the full definition
    """
    old = {"kind": "int", "type_params": {}, "null": False}
    new = {"kind": "bigint", "type_params": {}, "null": True}
    assert MY.render_alter_column("t", "c", old, new, {}) == [
        "ALTER TABLE `t` MODIFY COLUMN `c` BIGINT NULL"
    ]


def test_render_alter_column_pk_demotion_strips_auto_increment_first():
    """
    GIVEN an AUTO_INCREMENT primary-key column being demoted
    WHEN the pk toggle renders on MySQL
    THEN a MODIFY COLUMN drops AUTO_INCREMENT before DROP PRIMARY KEY
         (MySQL errno 1075 rejects dropping the pk of a live auto column),
         while a plain pk demotion/promotion renders the bare toggle
    """
    auto_pk = {"kind": "int", "type_params": {}, "null": False, "pk": True, "auto_increment": True}
    plain = {"kind": "int", "type_params": {}, "null": False, "pk": False, "auto_increment": False}
    assert MY.render_alter_column("t", "c", auto_pk, plain, {}) == [
        "ALTER TABLE `t` MODIFY COLUMN `c` INT NOT NULL",
        "ALTER TABLE `t` DROP PRIMARY KEY",
    ]
    manual_pk = {**auto_pk, "auto_increment": False}
    assert MY.render_alter_column("t", "c", manual_pk, plain, {}) == [
        "ALTER TABLE `t` DROP PRIMARY KEY"
    ]
    assert MY.render_alter_column("t", "c", plain, manual_pk, {}) == [
        "ALTER TABLE `t` ADD PRIMARY KEY (`c`)"
    ]


def test_render_alter_column_toggles_default_unique_and_fk():
    """
    GIVEN default/unique/fk spec changes
    WHEN they render on MySQL
    THEN defaults use SET/DROP DEFAULT, unique drops via DROP INDEX and the
         foreign key uses DROP FOREIGN KEY / ADD CONSTRAINT (no IF EXISTS)
    """
    base = {"kind": "int", "type_params": {}, "null": True}
    with_default = {**base, "default": {"kind": "sql", "sql": "7"}}
    assert MY.render_alter_column("t", "c", base, with_default, {}) == [
        "ALTER TABLE `t` ALTER COLUMN `c` SET DEFAULT (7)"
    ]
    assert MY.render_alter_column("t", "c", with_default, base, {}) == [
        "ALTER TABLE `t` ALTER COLUMN `c` DROP DEFAULT"
    ]
    assert MY.render_alter_column("t", "c", base, {**base, "unique": True}, {}) == [
        "ALTER TABLE `t` ADD CONSTRAINT `t_c_key` UNIQUE (`c`)"
    ]
    assert MY.render_alter_column("t", "c", {**base, "unique": True}, base, {}) == [
        "ALTER TABLE `t` DROP INDEX `t_c_key`"
    ]
    fk = {"table": "p", "pk": "id", "on_delete": "SET NULL"}
    assert MY.render_alter_column("t", "c", base, {**base, "fk": fk}, {}) == [
        "ALTER TABLE `t` ADD CONSTRAINT `t_c_fkey` FOREIGN KEY (`c`) "
        "REFERENCES `p` (`id`) ON DELETE SET NULL"
    ]
    assert MY.render_alter_column("t", "c", {**base, "fk": fk}, base, {}) == [
        "ALTER TABLE `t` DROP FOREIGN KEY `t_c_fkey`"
    ]


def test_index_ddl_uses_mysql_spellings():
    """
    GIVEN index create/drop/rename renders
    WHEN they render on MySQL
    THEN CREATE INDEX has no IF NOT EXISTS, drops go through ALTER TABLE and
         renames use RENAME INDEX
    """
    assert MY.render_create_index("t", "c") == ["CREATE INDEX `idx_t_c` ON `t` (`c`)"]
    assert MY.render_create_index("t", "c", unique=True, name="u") == [
        "CREATE UNIQUE INDEX `u` ON `t` (`c`)"
    ]
    assert MY.render_drop_index("t", "c") == ["ALTER TABLE `t` DROP INDEX `idx_t_c`"]
    assert MY.render_create_composite_index(
        "t", "i", ["a", "b"], condition="a > 0", using="gin"
    ) == ["CREATE INDEX `i` ON `t` (`a`, `b`)"]
    assert MY.render_rename_index("t", "c", "old", "new") == [
        "ALTER TABLE `t` RENAME INDEX `old` TO `new`"
    ]


def test_drop_composite_index_needs_the_owning_table():
    """
    GIVEN a named-index drop (as the DropIndex migration op renders it)
    WHEN the owning table is supplied versus omitted
    THEN MySQL renders ALTER TABLE ... DROP INDEX, and raises without a table
         (its DROP INDEX has no by-name form)
    """
    assert MY.render_drop_composite_index("i", table="t") == ["ALTER TABLE `t` DROP INDEX `i`"]
    with pytest.raises(UnSupportedError):
        MY.render_drop_composite_index("i")


def test_unsupported_ddl_raises_clearly():
    """
    GIVEN DDL MySQL cannot express (rename constraint)
    WHEN it renders
    THEN UnSupportedError is raised instead of emitting invalid SQL
    """
    with pytest.raises(UnSupportedError):
        MY.render_rename_constraint("t", "a", "b")


def test_render_drop_constraint_has_no_if_exists():
    """
    GIVEN a named-constraint drop
    WHEN it renders on MySQL
    THEN the statement carries no IF EXISTS (MySQL does not support it)
    """
    assert MY.render_drop_constraint("t", "chk") == ["ALTER TABLE `t` DROP CONSTRAINT `chk`"]


# -- db_defaults --------------------------------------------------------------------


def test_db_defaults_render_mysql_expressions():
    """
    GIVEN the three database-default kinds
    WHEN they render on MySQL
    THEN Now keeps microseconds, RandomHex uses random_bytes with the same hex
         width as the other backends, and SqlDefault passes through verbatim
    """
    assert Now().to_sql(MY) == "CURRENT_TIMESTAMP(6)"
    assert RandomHex(16).to_sql(MY) == "lower(hex(random_bytes(16)))"
    assert RandomHex(4).to_sql(MY) == "lower(hex(random_bytes(4)))"
    assert SqlDefault("42").to_sql(MY) == "42"


# -- compile-time insert behaviour ---------------------------------------------------


def test_compile_renders_insert_without_returning():
    """
    GIVEN a model compiled for MySQL
    WHEN its cached insert SQL is built
    THEN there is no RETURNING clause (the pk arrives via last-insert id)
    """
    MyDlBook._meta.compile(MY)
    try:
        assert "RETURNING" not in MyDlBook._meta.insert_sql
        assert MyDlBook._meta.insert_sql.startswith("INSERT INTO `my_dl_book`")
        assert "VALUES (?, ?, ?, ?)" in MyDlBook._meta.insert_sql
    finally:
        # Leave no MySQL-compiled state behind for suites sharing the model.
        MyDlBook._meta._compiled_for = None


def test_compile_builds_a_refresh_select_for_fetch_db_defaults():
    """
    GIVEN a model with Meta.fetch_db_defaults and a database-default column
    WHEN it compiles for MySQL (which has no INSERT ... RETURNING)
    THEN a follow-up SELECT-by-pk statement is cached to read the
         database-filled defaults back after the insert
    """

    class MyDlStamped(Model):
        id = fields.IntField(pk=True)
        created = fields.DatetimeField(db_default=Now())

        class Meta:
            table = "my_dl_stamped"
            fetch_db_defaults = True

    MyDlStamped._meta.compile(MY)
    try:
        assert "RETURNING" not in MyDlStamped._meta.insert_sql
        assert MyDlStamped._meta.insert_refresh_sql == (
            "SELECT `created` FROM `my_dl_stamped` WHERE `id` = ?"
        )
        assert [f.model_field_name for f in MyDlStamped._meta.insert_refresh_fields] == ["created"]
    finally:
        MyDlStamped._meta._compiled_for = None

"""Unit tests for the MariaDB dialect (no live server required).

These exercise the MariaDB-specific SQL divergences from MySQL so the paths are
covered even when the DB-backed suite runs only against PostgreSQL/MySQL.
"""

import uuid

import pytest

from yara_orm import fields
from yara_orm.db_defaults import Now, RandomHex
from yara_orm.dialects import MariaDbDialect, MySQLDialect, get_dialect
from yara_orm.exceptions import ConfigurationError
from yara_orm.fields import FieldKindRegistration


def test_mariadb_is_registered_and_mysql_family():
    """
    GIVEN the dialect registry
    WHEN "mariadb" is resolved
    THEN it yields a MariaDbDialect that subclasses MySQLDialect
    """
    dialect = get_dialect("mariadb")
    assert isinstance(dialect, MariaDbDialect)
    assert isinstance(dialect, MySQLDialect)
    assert dialect.name == "mariadb"


def test_capability_flags_differ_from_mysql():
    """
    GIVEN MariaDB's feature set
    WHEN its capability flags are read
    THEN it supports RETURNING but not FOR UPDATE OF (unlike MySQL 8)
    """
    dialect = MariaDbDialect()
    assert dialect.supports_insert_returning is True
    assert dialect.supports_for_update_of is False
    assert MySQLDialect().supports_insert_returning is False
    assert MySQLDialect().supports_for_update_of is True


@pytest.mark.parametrize(
    ("op", "flag"),
    [
        ("regex", "(?-i)"),
        ("posix_regex", "(?-i)"),
        ("iregex", "(?i)"),
        ("iposix_regex", "(?i)"),
    ],
)
def test_regex_sql_uses_pcre_inline_flags(op, flag):
    """
    GIVEN a regex lookup
    WHEN MariaDB renders it
    THEN it uses the REGEXP operator with a PCRE case flag (no REGEXP_LIKE)
    """
    sql = MariaDbDialect().regex_sql(op, "`t`.`c`", "?")
    assert sql == f"`t`.`c` REGEXP CONCAT('{flag}', ?)"
    assert "REGEXP_LIKE" not in sql


def test_on_conflict_uses_values_function_not_row_alias():
    """
    GIVEN a bulk upsert
    WHEN MariaDB renders the ON DUPLICATE KEY clause
    THEN it uses the classic VALUES(col) form, not MySQL 8's `AS new` alias
    """
    dialect = MariaDbDialect()
    clause = dialect.on_conflict_sql([], ["a", "b"])
    assert clause == " ON DUPLICATE KEY UPDATE `a` = VALUES(`a`), `b` = VALUES(`b`)"
    assert "AS `new`" not in clause
    # No update columns -> conflict skipping is spelled on the INSERT verb.
    assert dialect.on_conflict_sql([], []) == ""


def test_read_decoder_parses_json_text():
    """
    GIVEN MariaDB returning a JSON column as LONGTEXT
    WHEN the value is decoded
    THEN the raw text is parsed to a Python object
    """
    decode = MariaDbDialect().read_decoder(fields.JSONField())
    assert decode('{"a": 1}') == {"a": 1}
    assert decode(b"[1, 2]") == [1, 2]
    # An already-parsed value passes through unchanged.
    assert decode({"x": 2}) == {"x": 2}


def test_read_decoder_json_applies_decoder_hook():
    """
    GIVEN a JSONField with a decoder hook
    WHEN MariaDB decodes the LONGTEXT value
    THEN the hook runs on the parsed object
    """
    field = fields.JSONField(decoder=lambda v: {**v, "seen": True})
    decode = MariaDbDialect().read_decoder(field)
    assert decode('{"a": 1}') == {"a": 1, "seen": True}


def test_read_decoder_defers_to_mysql_for_uuid():
    """
    GIVEN a non-JSON column
    WHEN MariaDB decodes it
    THEN it defers to MySQL's decoder (CHAR(36) uuid -> uuid.UUID)
    """
    decode = MariaDbDialect().read_decoder(fields.UUIDField())
    value = "9084f2c6-1a01-4186-be2e-a58cc211188a"
    assert decode(value) == uuid.UUID(value)


def test_database_default_sql_matches_mysql():
    """
    GIVEN the database-side defaults
    WHEN rendered for MariaDB
    THEN they use the MySQL-family spelling (sub-second CURRENT_TIMESTAMP,
         random_bytes hex), not the SQLite fallback
    """
    dialect = MariaDbDialect()
    assert Now().to_sql(dialect) == "CURRENT_TIMESTAMP(6)"
    assert RandomHex(8).to_sql(dialect) == "lower(hex(random_bytes(8)))"


def test_custom_kind_sql_template_falls_back_to_mysql():
    """
    GIVEN a custom field kind registered only for "mysql"
    WHEN its SQL template is resolved for "mariadb"
    THEN the MySQL template is reused (MariaDB shares MySQL column types)
    """
    reg = FieldKindRegistration(
        "money",
        fields.CharField,
        {"mysql": "DECIMAL(10,2)", "postgres": "NUMERIC(10,2)"},
        None,
        None,
    )
    assert reg.sql_template("mariadb") == "DECIMAL(10,2)"
    # With no MySQL template there is nothing to fall back to.
    reg_no_mysql = FieldKindRegistration("x", fields.CharField, {"sqlite": "TEXT"}, None, None)
    with pytest.raises(ConfigurationError):
        reg_no_mysql.sql_template("mariadb")

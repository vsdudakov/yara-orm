"""SQL dialects.

Each dialect owns every database-specific decision: identifier quoting,
parameter placeholders and the mapping from a field's abstract *kind* to a
concrete column type. Supporting a new database means adding a subclass and
registering it -- the model and queryset layers never change.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .exceptions import ConfigurationError
from .fields import ForeignKeyField
from .registry import get_model

if TYPE_CHECKING:
    from .fields import Field
    from .models import MetaInfo
    from .relations import M2MInfo


class BaseDialect:
    """Base class rendering backend-agnostic SQL for a database dialect."""

    name = "base"

    #: kind -> SQL type template (``str.format`` with ``type_params``).
    type_map: dict[str, str] = {}
    #: kind -> auto-increment SQL type template.
    serial_map: dict[str, str] = {}
    #: Operator used for case-insensitive ``LIKE`` lookups.
    ilike = "ILIKE"

    # -- identifiers & placeholders --------------------------------------
    def quote(self, identifier: str) -> str:
        """Quote a SQL identifier, escaping embedded quote characters.

        Args:
            identifier: The identifier (table or column name) to quote.

        Returns:
            The double-quoted, escaped identifier.
        """
        return '"{}"'.format(identifier.replace('"', '""'))

    def placeholder(self, index: int) -> str:
        """Render a bound-parameter placeholder for the given position.

        Args:
            index: The 1-based parameter position.

        Returns:
            The dialect-specific placeholder string.
        """
        raise NotImplementedError

    # -- type rendering ---------------------------------------------------
    def column_type(self, field: Field) -> str:
        """Resolve the concrete SQL column type for a field.

        Args:
            field: The field whose column type is rendered.

        Returns:
            The SQL type string for the field.
        """
        kind = field.field_kind
        if isinstance(field, ForeignKeyField):
            ref = get_model(field.reference)
            pk = ref._meta.pk_field
            # Reference the scalar type of the target pk, never its serial form.
            return self.type_map[pk.field_kind].format(**pk.type_params)

        if field.auto_increment and kind in self.serial_map:
            return self.serial_map[kind]

        try:
            template = self.type_map[kind]
        except KeyError as exc:
            raise ConfigurationError(
                f"Dialect {self.name!r} has no type mapping for kind {kind!r}"
            ) from exc
        return template.format(**field.type_params)

    # -- DDL --------------------------------------------------------------
    def column_sql(self, field: Field) -> str:
        """Render a column definition for a ``CREATE TABLE`` statement.

        Args:
            field: The field to render as a column definition.

        Returns:
            The column definition SQL fragment.
        """
        parts = [self.quote(field.db_column), self.column_type(field)]
        if field.pk:
            # Primary key implies NOT NULL; the PK constraint is added separately.
            pass
        elif field.null:
            parts.append("NULL")
        else:
            parts.append("NOT NULL")
        if field.unique and not field.pk:
            parts.append("UNIQUE")
        return " ".join(parts)

    def create_table_sql(self, meta: MetaInfo, safe: bool = True) -> list[str]:
        """Render statements to create a model's table, indexes and comments.

        Args:
            meta: The model metadata describing the table.
            safe: Whether to emit ``IF NOT EXISTS`` guards.

        Returns:
            The list of SQL statements creating the table.
        """
        lines = [self.column_sql(f) for f in meta.fields.values()]
        lines.append(f"PRIMARY KEY ({self.quote(meta.pk_field.db_column)})")

        for field in meta.fields.values():
            if isinstance(field, ForeignKeyField):
                ref = get_model(field.reference)
                col = self.quote(field.db_column)
                ref_tbl = self.quote(ref._meta.table)
                ref_pk = self.quote(ref._meta.pk_field.db_column)
                lines.append(
                    f"FOREIGN KEY ({col}) REFERENCES {ref_tbl} ({ref_pk}) "
                    f"ON DELETE {field.on_delete}"
                )

        ine = "IF NOT EXISTS " if safe else ""
        body = ",\n  ".join(lines)
        statements = [f"CREATE TABLE {ine}{self.quote(meta.table)} (\n  {body}\n)"]

        # Secondary indexes for index=True (non-unique) columns.
        for field in meta.fields.values():
            if field.index and not field.unique and not field.pk:
                idx_name = f"idx_{meta.table}_{field.db_column}"
                statements.append(
                    f"CREATE INDEX {ine}{self.quote(idx_name)} "
                    f"ON {self.quote(meta.table)} ({self.quote(field.db_column)})"
                )

        # Comments from `description=` and a model's `Meta.table_description`.
        statements.extend(self._comment_sql(meta))
        return statements

    def _comment_sql(self, meta: MetaInfo) -> list[str]:
        """Render ``COMMENT ON`` statements for a table and its columns.

        Args:
            meta: The model metadata carrying descriptions.

        Returns:
            The list of comment statements (possibly empty).
        """
        out = []
        if meta.description:
            out.append(
                f"COMMENT ON TABLE {self.quote(meta.table)} IS {self._literal(meta.description)}"
            )
        for field in meta.fields.values():
            if field.description:
                col = f"{self.quote(meta.table)}.{self.quote(field.db_column)}"
                out.append(f"COMMENT ON COLUMN {col} IS {self._literal(field.description)}")
        return out

    @staticmethod
    def _literal(text: str) -> str:
        """Render a SQL string literal, escaping single quotes.

        Args:
            text: The text to quote as a literal.

        Returns:
            The escaped, single-quoted literal.
        """
        return "'" + text.replace("'", "''") + "'"

    def create_m2m_table_sql(self, info: M2MInfo, safe: bool = True) -> list[str]:
        """DDL for a many-to-many join table (two FK columns + composite pk).

        Args:
            info: The many-to-many relation metadata.
            safe: Whether to emit ``IF NOT EXISTS`` guards.

        Returns:
            The list of SQL statements creating the join table.
        """
        owner = info.owner
        target = info.resolve_target()
        ine = "IF NOT EXISTS " if safe else ""
        near = self.quote(info.backward_key)
        far = self.quote(info.forward_key)
        owner_type = self._scalar_pk_type(owner._meta.pk_field)
        target_type = self._scalar_pk_type(target._meta.pk_field)
        owner_tbl = self.quote(owner._meta.table)
        owner_pk = self.quote(owner._meta.pk_field.db_column)
        target_tbl = self.quote(target._meta.table)
        target_pk = self.quote(target._meta.pk_field.db_column)
        lines = [
            f"{near} {owner_type} NOT NULL",
            f"{far} {target_type} NOT NULL",
            f"PRIMARY KEY ({near}, {far})",
            f"FOREIGN KEY ({near}) REFERENCES {owner_tbl} ({owner_pk}) ON DELETE CASCADE",
            f"FOREIGN KEY ({far}) REFERENCES {target_tbl} ({target_pk}) ON DELETE CASCADE",
        ]
        body = ",\n  ".join(lines)
        return [f"CREATE TABLE {ine}{self.quote(info.through)} (\n  {body}\n)"]

    def _scalar_pk_type(self, pk_field: Field) -> str:
        """Non-serial column type matching a referenced primary key.

        Args:
            pk_field: The referenced primary key field.

        Returns:
            The scalar SQL type matching the primary key.
        """
        return self.type_map[pk_field.field_kind].format(**pk_field.type_params)

    # -- migration rendering (spec-based, model-independent) -------------
    # Migrations carry self-contained column/table specs (plain dicts) so the
    # generated SQL never depends on the live model definitions.
    def _spec_type(self, spec: dict[str, Any]) -> str:
        """Resolve the SQL column type from a migration column spec.

        Args:
            spec: The migration column spec.

        Returns:
            The SQL type string for the spec.
        """
        kind = spec["kind"]
        if spec.get("auto_increment") and kind in self.serial_map:
            return self.serial_map[kind]
        return self.type_map[kind].format(**spec.get("type_params", {}))

    def render_column_def(self, name: str, spec: dict[str, Any]) -> str:
        """Render a column definition from a migration column spec.

        Args:
            name: The column name.
            spec: The migration column spec.

        Returns:
            The column definition SQL fragment.
        """
        parts = [self.quote(name), self._spec_type(spec)]
        if spec.get("pk"):
            pass
        elif spec.get("null"):
            parts.append("NULL")
        else:
            parts.append("NOT NULL")
        if spec.get("unique") and not spec.get("pk"):
            parts.append("UNIQUE")
        return " ".join(parts)

    def _pk_clause(self, tspec: dict[str, Any]) -> str:
        """Render the ``PRIMARY KEY`` clause from a migration table spec.

        Args:
            tspec: The migration table spec.

        Returns:
            The primary key clause, or an empty string when there is none.
        """
        cols = tspec.get("composite_pk") or ([tspec["pk"]] if tspec.get("pk") else [])
        if not cols:
            return ""
        return "PRIMARY KEY ({})".format(", ".join(self.quote(c) for c in cols))

    def render_create_table(
        self, table: str, tspec: dict[str, Any], safe: bool = True
    ) -> list[str]:
        """Render statements to create a table from a migration table spec.

        Args:
            table: The table name.
            tspec: The migration table spec.
            safe: Whether to emit ``IF NOT EXISTS`` guards.

        Returns:
            The list of SQL statements creating the table.
        """
        lines = [self.render_column_def(n, s) for n, s in tspec["columns"].items()]
        pk = self._pk_clause(tspec)
        if pk:
            lines.append(pk)
        for col, ref in tspec.get("fks", {}).items():
            lines.append(
                "FOREIGN KEY ({c}) REFERENCES {t} ({p}) ON DELETE {od}".format(
                    c=self.quote(col),
                    t=self.quote(ref["table"]),
                    p=self.quote(ref["pk"]),
                    od=ref.get("on_delete", "CASCADE"),
                )
            )
        ine = "IF NOT EXISTS " if safe else ""
        body = ",\n  ".join(lines)
        out = [f"CREATE TABLE {ine}{self.quote(table)} (\n  {body}\n)"]
        for col in tspec.get("indexes", []):
            out.extend(self.render_create_index(table, col, safe))
        return out

    def render_drop_table(self, table: str) -> list[str]:
        """Render a statement to drop a table.

        Args:
            table: The table name.

        Returns:
            The list with the drop-table statement.
        """
        return [f"DROP TABLE IF EXISTS {self.quote(table)} CASCADE"]

    def render_add_column(self, table: str, name: str, spec: dict[str, Any]) -> list[str]:
        """Render a statement to add a column to a table.

        Args:
            table: The table name.
            name: The column name.
            spec: The migration column spec.

        Returns:
            The list with the add-column statement.
        """
        return [f"ALTER TABLE {self.quote(table)} ADD COLUMN {self.render_column_def(name, spec)}"]

    def render_drop_column(self, table: str, name: str) -> list[str]:
        """Render a statement to drop a column from a table.

        Args:
            table: The table name.
            name: The column name.

        Returns:
            The list with the drop-column statement.
        """
        return [f"ALTER TABLE {self.quote(table)} DROP COLUMN {self.quote(name)}"]

    def render_create_index(self, table: str, column: str, safe: bool = True) -> list[str]:
        """Render a statement to create an index on a column.

        Args:
            table: The table name.
            column: The column to index.
            safe: Whether to emit ``IF NOT EXISTS`` guards.

        Returns:
            The list with the create-index statement.
        """
        ine = "IF NOT EXISTS " if safe else ""
        return [
            "CREATE INDEX {ine}{name} ON {t} ({c})".format(
                ine=ine,
                name=self.quote(f"idx_{table}_{column}"),
                t=self.quote(table),
                c=self.quote(column),
            )
        ]

    def render_drop_index(self, table: str, column: str) -> list[str]:
        """Render a statement to drop a column's index.

        Args:
            table: The table name.
            column: The indexed column.

        Returns:
            The list with the drop-index statement.
        """
        return [f"DROP INDEX IF EXISTS {self.quote(f'idx_{table}_{column}')}"]


class PostgresDialect(BaseDialect):
    """Dialect rendering SQL for PostgreSQL."""

    name = "postgres"

    type_map = {
        "smallint": "SMALLINT",
        "int": "INTEGER",
        "bigint": "BIGINT",
        "varchar": "VARCHAR({max_length})",
        "text": "TEXT",
        "bool": "BOOL",
        "float": "DOUBLE PRECISION",
        "decimal": "NUMERIC({max_digits}, {decimal_places})",
        "datetime": "TIMESTAMPTZ",
        "date": "DATE",
        "time": "TIME",
        "timedelta": "BIGINT",
        "uuid": "UUID",
        "json": "JSONB",
        "bytes": "BYTEA",
    }
    serial_map = {
        "smallint": "SMALLSERIAL",
        "int": "SERIAL",
        "bigint": "BIGSERIAL",
    }

    def placeholder(self, index: int) -> str:
        """Render a PostgreSQL ``$n`` bound-parameter placeholder.

        Args:
            index: The 1-based parameter position.

        Returns:
            The ``$index`` placeholder string.
        """
        return f"${index}"


class SqliteDialect(BaseDialect):
    """Dialect rendering SQL for SQLite."""

    name = "sqlite"
    # SQLite has no ILIKE; its LIKE is case-insensitive for ASCII text.
    ilike = "LIKE"

    type_map = {
        "smallint": "INTEGER",
        "int": "INTEGER",
        "bigint": "INTEGER",
        "varchar": "VARCHAR({max_length})",
        "text": "TEXT",
        "bool": "BOOLEAN",
        "float": "REAL",
        # TEXT affinity (via VARCHAR) keeps decimals stored as the exact string
        # we bind; NUMERIC affinity would coerce them to lossy REAL on insert.
        # DecimalField.to_python reconstructs the Decimal on read.
        "decimal": "VARCHAR({max_digits})",
        "datetime": "TIMESTAMP",
        "date": "DATE",
        "time": "TIME",
        "timedelta": "BIGINT",
        "uuid": "UUID",
        "json": "JSON",
        "bytes": "BLOB",
    }
    # SQLite auto-increment pks are declared inline (see column_sql), so there
    # is no separate serial type.
    serial_map: dict[str, str] = {}

    def placeholder(self, index: int) -> str:
        """Render a SQLite ``?n`` bound-parameter placeholder.

        Args:
            index: The 1-based parameter position.

        Returns:
            The ``?index`` placeholder string.
        """
        return f"?{index}"

    def _is_auto_pk(self, field: Field) -> bool:
        """Report whether a field is an auto-increment integer primary key.

        Args:
            field: The field to inspect.

        Returns:
            ``True`` if the field is an auto-increment integer primary key.
        """
        return (
            field.pk and field.auto_increment and field.field_kind in ("int", "bigint", "smallint")
        )

    def column_sql(self, field: Field) -> str:
        """Render a column definition, inlining auto-increment primary keys.

        Args:
            field: The field to render as a column definition.

        Returns:
            The column definition SQL fragment.
        """
        if self._is_auto_pk(field):
            # rowid alias: must be inline INTEGER PRIMARY KEY to autoincrement.
            return f"{self.quote(field.db_column)} INTEGER PRIMARY KEY AUTOINCREMENT"
        return super().column_sql(field)

    def _comment_sql(self, meta: MetaInfo) -> list[str]:
        """Render comment statements; SQLite supports none.

        Args:
            meta: The model metadata (unused).

        Returns:
            An empty list.
        """
        # SQLite has no COMMENT statement.
        return []

    # -- migration rendering overrides ----------------------------------
    def render_drop_table(self, table: str) -> list[str]:
        """Render a statement to drop a table without ``CASCADE``.

        Args:
            table: The table name.

        Returns:
            The list with the drop-table statement.
        """
        return [f"DROP TABLE IF EXISTS {self.quote(table)}"]

    def render_column_def(self, name: str, spec: dict[str, Any]) -> str:
        """Render a column definition, inlining auto-increment primary keys.

        Args:
            name: The column name.
            spec: The migration column spec.

        Returns:
            The column definition SQL fragment.
        """
        if self._is_auto_pk_spec(spec):
            return f"{self.quote(name)} INTEGER PRIMARY KEY AUTOINCREMENT"
        return super().render_column_def(name, spec)

    @staticmethod
    def _is_auto_pk_spec(spec: dict[str, Any]) -> bool:
        """Report whether a column spec is an auto-increment integer pk.

        Args:
            spec: The migration column spec.

        Returns:
            ``True`` if the spec is an auto-increment integer primary key.
        """
        return bool(
            spec.get("pk")
            and spec.get("auto_increment")
            and spec.get("kind") in ("int", "bigint", "smallint")
        )

    def _pk_clause(self, tspec: dict[str, Any]) -> str:
        """Render the ``PRIMARY KEY`` clause, skipping inline auto-increment pks.

        Args:
            tspec: The migration table spec.

        Returns:
            The primary key clause, or an empty string when declared inline.
        """
        pk = tspec.get("pk")
        if pk and self._is_auto_pk_spec(tspec["columns"].get(pk, {})):
            return ""  # declared inline on the column
        return super()._pk_clause(tspec)

    def create_table_sql(self, meta: MetaInfo, safe: bool = True) -> list[str]:
        """Render statements to create a model's table and its indexes.

        Args:
            meta: The model metadata describing the table.
            safe: Whether to emit ``IF NOT EXISTS`` guards.

        Returns:
            The list of SQL statements creating the table.
        """
        pk = meta.pk_field
        lines = [self.column_sql(f) for f in meta.fields.values()]
        if not self._is_auto_pk(pk):
            lines.append(f"PRIMARY KEY ({self.quote(pk.db_column)})")

        for field in meta.fields.values():
            if isinstance(field, ForeignKeyField):
                ref = get_model(field.reference)
                col = self.quote(field.db_column)
                ref_tbl = self.quote(ref._meta.table)
                ref_pk = self.quote(ref._meta.pk_field.db_column)
                lines.append(
                    f"FOREIGN KEY ({col}) REFERENCES {ref_tbl} ({ref_pk}) "
                    f"ON DELETE {field.on_delete}"
                )

        ine = "IF NOT EXISTS " if safe else ""
        body = ",\n  ".join(lines)
        statements = [f"CREATE TABLE {ine}{self.quote(meta.table)} (\n  {body}\n)"]
        for field in meta.fields.values():
            if field.index and not field.unique and not field.pk:
                idx_name = f"idx_{meta.table}_{field.db_column}"
                tbl = self.quote(meta.table)
                statements.append(
                    f"CREATE INDEX {ine}{self.quote(idx_name)} "
                    f"ON {tbl} ({self.quote(field.db_column)})"
                )
        return statements


_DIALECTS: dict[str, type[BaseDialect]] = {
    "postgres": PostgresDialect,
    "sqlite": SqliteDialect,
}


def get_dialect(name: str) -> BaseDialect:
    """Instantiate the dialect registered under a name.

    Args:
        name: The registered dialect name.

    Returns:
        A new instance of the matching dialect.
    """
    try:
        return _DIALECTS[name]()
    except KeyError as exc:
        raise ConfigurationError(f"No dialect registered for {name!r}") from exc


def register_dialect(name: str, dialect_cls: type[BaseDialect]) -> None:
    """Register a dialect class under a name.

    Args:
        name: The name to register the dialect under.
        dialect_cls: The dialect class to register.

    Returns:
        None
    """
    _DIALECTS[name] = dialect_cls

"""SQL dialects.

Each dialect owns every database-specific decision: identifier quoting,
parameter placeholders and the mapping from a field's abstract *kind* to a
concrete column type. Supporting a new database means adding a subclass and
registering it -- the model and queryset layers never change.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from .db_defaults import DatabaseDefault
from .exceptions import ConfigurationError, UnSupportedError
from .fields import ForeignKeyField
from .registry import get_model

if TYPE_CHECKING:
    from .fields import Field
    from .models import Index, MetaInfo
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
    #: Lookup name -> SQL operator for regular-expression matches. Empty when
    #: the backend has no regex operator (the lookup then raises).
    regex_ops: dict[str, str] = {}
    #: Whether the backend supports the ``__search`` full-text lookup.
    supports_search = False
    #: Statement prefix that returns a query plan for ``QuerySet.explain``.
    explain_prefix = "EXPLAIN "
    #: SQL expression used for random ordering (``order_by("?")``). PostgreSQL
    #: and SQLite both spell it ``RANDOM()``.
    random_function = "RANDOM()"
    #: Date/time part -> the literal used inside ``EXTRACT(<part> FROM col)``.
    _extract_parts = {
        "year": "YEAR",
        "quarter": "QUARTER",
        "month": "MONTH",
        "week": "WEEK",
        "day": "DAY",
        "hour": "HOUR",
        "minute": "MINUTE",
        "second": "SECOND",
        "microsecond": "MICROSECONDS",
    }
    #: Whether ``ADD COLUMN`` / ``DROP COLUMN`` accept an ``IF [NOT] EXISTS``
    #: guard (PostgreSQL does; SQLite has no such syntax).
    column_if_exists = True
    #: Whether index DDL accepts the ``CONCURRENTLY`` keyword (PostgreSQL only).
    index_concurrently = True
    #: Whether a column's type/null can be changed in place with ``ALTER COLUMN``
    #: (PostgreSQL); SQLite requires a full table rebuild instead.
    alter_column_in_place = True
    #: Whether an index can be renamed in place with ``ALTER INDEX`` (PostgreSQL);
    #: otherwise it is dropped and recreated under the new name.
    rename_index_in_place = True
    #: Whether named constraints can be added/dropped/renamed with ``ALTER TABLE``
    #: (PostgreSQL); SQLite has no such syntax.
    alter_constraint_in_place = True
    #: Whether ``CREATE INDEX`` accepts an access method (``USING gin``/``gist``/
    #: ``btree``); PostgreSQL does, SQLite has no such syntax.
    index_using = True
    #: Whether ``CREATE INDEX`` accepts non-key covering columns
    #: (``INCLUDE (...)``); PostgreSQL does, SQLite has no such syntax.
    index_include = True
    #: Whether ``CREATE INDEX`` accepts a per-column operator class
    #: (``col gin_trgm_ops``); PostgreSQL does, SQLite has no such syntax.
    index_opclass = True

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

    # -- query lookups ----------------------------------------------------
    def date_part_sql(self, part: str, col: str) -> str:
        """Render an expression extracting a date/time part from a column.

        Args:
            part: One of ``year``/``month``/``day``/``hour``/``minute``/``second``.
            col: The already-qualified column reference.

        Returns:
            A SQL expression yielding the integer part (e.g. ``EXTRACT(...)``).
        """
        return f"EXTRACT({self._extract_parts[part]} FROM {col})"

    def truncate_date_sql(self, col: str) -> str:
        """Render an expression truncating a datetime column to a date.

        Args:
            col: The already-qualified column reference.

        Returns:
            A SQL expression yielding the date part (for the ``__date`` lookup).
        """
        return f"CAST({col} AS DATE)"

    def cast_text(self, col: str) -> str:
        """Render an expression casting a column to text.

        Used so a ``LIKE``/``ILIKE`` lookup works against a non-text column
        (e.g. ``uuid``), which the database would otherwise reject for lack of a
        ``uuid ~~ text`` operator.

        Args:
            col: The already-qualified column reference.

        Returns:
            A SQL expression yielding the column as text.
        """
        return f"CAST({col} AS TEXT)"

    def on_conflict_sql(self, conflict_columns: list[str], update_columns: list[str]) -> str:
        """Render an ``ON CONFLICT`` clause for ``bulk_create`` upserts.

        ``ON CONFLICT [(cols)] DO NOTHING`` when ``update_columns`` is empty,
        otherwise ``ON CONFLICT (cols) DO UPDATE SET col = EXCLUDED.col``. The
        syntax is shared by PostgreSQL and SQLite.

        Args:
            conflict_columns: The columns forming the conflict target (may be
                empty for a bare ``DO NOTHING``).
            update_columns: The columns to overwrite on conflict; empty means
                ignore the conflicting row.

        Returns:
            The ``ON CONFLICT`` clause (leading space included).
        """
        target = (
            f" ({', '.join(self.quote(c) for c in conflict_columns)})" if conflict_columns else ""
        )
        if not update_columns:
            return f" ON CONFLICT{target} DO NOTHING"
        sets = ", ".join(f"{self.quote(c)} = EXCLUDED.{self.quote(c)}" for c in update_columns)
        return f" ON CONFLICT{target} DO UPDATE SET {sets}"

    def search_sql(self, col: str, placeholder: str) -> str:
        """Render a full-text ``__search`` condition.

        Args:
            col: The already-qualified column reference.
            placeholder: The bound-parameter placeholder for the search query.

        Raises:
            UnSupportedError: On backends without full-text search.

        Returns:
            A boolean SQL expression matching ``col`` against the query.
        """
        raise UnSupportedError(f"{self.name} does not support the __search lookup")

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
        if isinstance(field.default, DatabaseDefault):
            # Parenthesise: SQLite requires expression defaults in parens, and
            # PostgreSQL accepts them, so one form works on both backends.
            parts.append(f"DEFAULT ({field.default.to_sql(self)})")
        return " ".join(parts)

    def _group_columns(self, meta: MetaInfo, names: Sequence[str]) -> list[str]:
        """Resolve a group of field/relation names to quoted column names.

        Args:
            meta: The model metadata.
            names: Field names (or forward relation names) in the group.

        Returns:
            The quoted column names.
        """
        cols = []
        for fname in names:
            if fname in meta.relations:
                column = meta.get_field(meta.relations[fname].source_attr).db_column
            else:
                column = meta.get_field(fname).db_column
            cols.append(self.quote(column))
        return cols

    def _unique_together_lines(self, meta: MetaInfo) -> list[str]:
        """Render ``UNIQUE (...)`` constraint lines for ``Meta.unique_together``.

        Args:
            meta: The model metadata.

        Returns:
            One ``UNIQUE (...)`` clause per group (possibly empty).
        """
        return [f"UNIQUE ({', '.join(self._group_columns(meta, g))})" for g in meta.unique_together]

    def _composite_index_sql(
        self,
        table: str,
        name: str,
        columns_sql: str,
        *,
        ine: str = "",
        unique: bool = False,
        using: str | None = None,
        include_sql: str | None = None,
        condition: str | None = None,
    ) -> str:
        """Render a single (optionally unique/partial/covering) ``CREATE INDEX``.

        ``USING`` and ``INCLUDE`` are PostgreSQL-only and are silently dropped on
        dialects whose ``index_using``/``index_include`` flags are unset, keeping
        the emitted SQL valid on SQLite.

        Args:
            table: The table to index.
            name: The index name.
            columns_sql: The already-quoted, comma-joined key columns.
            ine: The ``IF NOT EXISTS`` guard fragment (or empty).
            unique: Whether to render ``CREATE UNIQUE INDEX``.
            using: Optional access method rendered as ``USING <method>``.
            include_sql: Optional already-quoted, comma-joined covering columns
                rendered as ``INCLUDE (...)``.
            condition: Optional partial-index predicate rendered as ``WHERE``.

        Returns:
            The single ``CREATE INDEX`` statement.
        """
        uniq = "UNIQUE " if unique else ""
        method = f" USING {using}" if using and self.index_using else ""
        incl = f" INCLUDE ({include_sql})" if include_sql and self.index_include else ""
        where = f" WHERE {condition}" if condition else ""
        return (
            f"CREATE {uniq}INDEX {ine}{self.quote(name)} "
            f"ON {self.quote(table)}{method} ({columns_sql}){incl}{where}"
        )

    def render_index(self, meta: MetaInfo, index: Index, safe: bool = True) -> str:
        """Render the single ``CREATE INDEX`` statement for one :class:`Index`.

        Resolves the index's field names to the model's columns and applies its
        unique/partial/``USING``/``INCLUDE``/opclass options (the PostgreSQL-only
        ones are dropped on dialects that lack them). Powers ``Index.get_sql()``
        and the table-creation index emission.

        Args:
            meta: The owning model's metadata.
            index: The index to render.
            safe: Whether to emit an ``IF NOT EXISTS`` guard.

        Returns:
            The ``CREATE INDEX`` statement.
        """
        ine = "IF NOT EXISTS " if safe else ""
        include_sql = ", ".join(self._group_columns(meta, index.include)) if index.include else None
        columns = self._group_columns(meta, index.fields)
        if index.opclass and self.index_opclass:
            columns = [f"{col} {index.opclass}" for col in columns]
        return self._composite_index_sql(
            meta.table,
            index.resolve_name(meta.table),
            ", ".join(columns),
            ine=ine,
            unique=index.unique,
            using=index.using,
            include_sql=include_sql,
            condition=index.condition,
        )

    def _composite_index_statements(self, meta: MetaInfo, ine: str) -> list[str]:
        """Render ``CREATE INDEX`` statements for ``Meta.indexes``.

        Args:
            meta: The model metadata.
            ine: The ``IF NOT EXISTS`` guard fragment (or empty).

        Returns:
            One ``CREATE INDEX`` statement per group (possibly empty).
        """
        return [self.render_index(meta, index, safe=bool(ine)) for index in meta.indexes]

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
            if isinstance(field, ForeignKeyField) and field.db_constraint:
                ref = get_model(field.reference)
                col = self.quote(field.db_column)
                ref_tbl = self.quote(ref._meta.table)
                ref_pk = self.quote(ref._meta.pk_field.db_column)
                lines.append(
                    f"FOREIGN KEY ({col}) REFERENCES {ref_tbl} ({ref_pk}) "
                    f"ON DELETE {field.on_delete}"
                )
        lines.extend(self._unique_together_lines(meta))
        for constraint in meta.constraints:
            lines.append(self._constraint_clause(constraint.to_spec()))

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
        statements.extend(self._composite_index_statements(meta, ine))

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
        for constraint in tspec.get("constraints", []):
            lines.append(self._constraint_clause(constraint))
        ine = "IF NOT EXISTS " if safe else ""
        body = ",\n  ".join(lines)
        out = [f"CREATE TABLE {ine}{self.quote(table)} (\n  {body}\n)"]
        for col in tspec.get("indexes", []):
            out.extend(self.render_create_index(table, col, safe))
        for name, spec in tspec.get("composite_indexes", {}).items():
            out.extend(
                self.render_create_composite_index(
                    table,
                    name,
                    spec["columns"],
                    safe=safe,
                    condition=spec.get("condition"),
                    unique=spec.get("unique", False),
                    using=spec.get("using"),
                    include=spec.get("include"),
                    opclass=spec.get("opclass"),
                )
            )
        return out

    def render_drop_table(self, table: str) -> list[str]:
        """Render a statement to drop a table.

        Args:
            table: The table name.

        Returns:
            The list with the drop-table statement.
        """
        return [f"DROP TABLE IF EXISTS {self.quote(table)} CASCADE"]

    def render_add_column(
        self, table: str, name: str, spec: dict[str, Any], safe: bool = False
    ) -> list[str]:
        """Render a statement to add a column to a table.

        Args:
            table: The table name.
            name: The column name.
            spec: The migration column spec.
            safe: Whether to emit an ``IF NOT EXISTS`` guard (honoured only on
                dialects whose ``column_if_exists`` is set).

        Returns:
            The list with the add-column statement.
        """
        ine = "IF NOT EXISTS " if safe and self.column_if_exists else ""
        return [
            f"ALTER TABLE {self.quote(table)} ADD COLUMN {ine}{self.render_column_def(name, spec)}"
        ]

    def render_drop_column(self, table: str, name: str, safe: bool = False) -> list[str]:
        """Render a statement to drop a column from a table.

        Args:
            table: The table name.
            name: The column name.
            safe: Whether to emit an ``IF EXISTS`` guard (honoured only on
                dialects whose ``column_if_exists`` is set).

        Returns:
            The list with the drop-column statement.
        """
        ie = "IF EXISTS " if safe and self.column_if_exists else ""
        return [f"ALTER TABLE {self.quote(table)} DROP COLUMN {ie}{self.quote(name)}"]

    def render_alter_column(
        self,
        table: str,
        name: str,
        old: dict[str, Any],
        new: dict[str, Any],
        table_spec: dict[str, Any],
    ) -> list[str]:
        """Render statements that change a column's type and nullability.

        On dialects that can alter a column in place (PostgreSQL) this emits the
        targeted ``ALTER COLUMN`` statements; otherwise it rebuilds the table
        from ``table_spec`` (the SQLite-safe create/copy/drop/rename dance).

        Args:
            table: The table name.
            name: The column being altered.
            old: The column spec before the change.
            new: The column spec after the change.
            table_spec: The full table spec reflecting the post-change columns.

        Returns:
            The list of SQL statements applying the column change.
        """
        if not self.alter_column_in_place:
            return self._rebuild_table(table, table_spec, old, new, name)
        t = self.quote(table)
        col = self.quote(name)
        out: list[str] = []
        if old.get("kind") != new.get("kind") or old.get("type_params") != new.get("type_params"):
            out.append(
                f"ALTER TABLE {t} ALTER COLUMN {col} TYPE {self._spec_type(new)} "
                f"USING {col}::{self._spec_type(new)}"
            )
        if bool(old.get("null")) != bool(new.get("null")):
            action = "DROP NOT NULL" if new.get("null") else "SET NOT NULL"
            out.append(f"ALTER TABLE {t} ALTER COLUMN {col} {action}")
        return out

    def _rebuild_table(
        self,
        table: str,
        table_spec: dict[str, Any],
        old: dict[str, Any],
        new: dict[str, Any],
        name: str,
    ) -> list[str]:
        """Rebuild a table to apply a change a dialect cannot do in place.

        Copies the rows into a freshly created table (carrying ``table_spec``)
        and swaps it in, the standard SQLite approach to altering a column.

        Args:
            table: The table name.
            table_spec: The full table spec the rebuilt table should match.
            old: The column spec before the change (unused; kept for symmetry).
            new: The column spec after the change (unused; kept for symmetry).
            name: The column being altered (carried over in the copy).

        Returns:
            The list of SQL statements rebuilding the table.
        """
        tmp = f"_new_{table}"
        cols = ", ".join(self.quote(c) for c in table_spec["columns"])
        out = self.render_create_table(tmp, table_spec, safe=False)
        out.append(f"INSERT INTO {self.quote(tmp)} ({cols}) SELECT {cols} FROM {self.quote(table)}")
        out.append(f"DROP TABLE {self.quote(table)}")
        out.append(f"ALTER TABLE {self.quote(tmp)} RENAME TO {self.quote(table)}")
        return out

    def render_create_index(
        self,
        table: str,
        column: str,
        safe: bool = True,
        unique: bool = False,
        concurrently: bool = False,
        name: str | None = None,
    ) -> list[str]:
        """Render a statement to create an index on a column.

        Args:
            table: The table name.
            column: The column to index.
            safe: Whether to emit ``IF NOT EXISTS`` guards.
            unique: Whether to create a ``UNIQUE`` index.
            concurrently: Whether to build the index ``CONCURRENTLY`` (honoured
                only on dialects whose ``index_concurrently`` is set).
            name: Explicit index name; defaults to ``idx_<table>_<column>``.

        Returns:
            The list with the create-index statement.
        """
        uniq = "UNIQUE " if unique else ""
        conc = "CONCURRENTLY " if concurrently and self.index_concurrently else ""
        ine = "IF NOT EXISTS " if safe else ""
        return [
            "CREATE {u}INDEX {conc}{ine}{name} ON {t} ({c})".format(
                u=uniq,
                conc=conc,
                ine=ine,
                name=self.quote(name or f"idx_{table}_{column}"),
                t=self.quote(table),
                c=self.quote(column),
            )
        ]

    def render_drop_index(
        self, table: str, column: str, concurrently: bool = False, name: str | None = None
    ) -> list[str]:
        """Render a statement to drop a column's index.

        Args:
            table: The table name.
            column: The indexed column.
            concurrently: Whether to drop the index ``CONCURRENTLY`` (honoured
                only on dialects whose ``index_concurrently`` is set).
            name: Explicit index name; defaults to ``idx_<table>_<column>``.

        Returns:
            The list with the drop-index statement.
        """
        conc = "CONCURRENTLY " if concurrently and self.index_concurrently else ""
        return [f"DROP INDEX {conc}IF EXISTS {self.quote(name or f'idx_{table}_{column}')}"]

    def render_create_composite_index(
        self,
        table: str,
        name: str,
        columns: list[str],
        safe: bool = True,
        condition: str | None = None,
        unique: bool = False,
        using: str | None = None,
        include: list[str] | None = None,
        opclass: str | None = None,
    ) -> list[str]:
        """Render a statement creating a multi-column index.

        Args:
            table: The table to index.
            columns: The ordered columns covered by the index.
            name: The index name.
            safe: Whether to emit an ``IF NOT EXISTS`` guard.
            condition: Optional partial-index predicate, rendered as a trailing
                ``WHERE`` clause; ``None`` for a full index.
            unique: Whether to render ``CREATE UNIQUE INDEX``.
            using: Optional access method rendered as ``USING <method>``
                (PostgreSQL-only; dropped on SQLite).
            include: Optional non-key covering columns rendered as
                ``INCLUDE (...)`` (PostgreSQL-only; dropped on SQLite).
            opclass: Optional operator class appended to every key column
                (PostgreSQL-only; dropped on SQLite).

        Returns:
            The list with the create-index statement.
        """
        ine = "IF NOT EXISTS " if safe else ""
        include_sql = ", ".join(self.quote(c) for c in include) if include else None
        key_cols = [self.quote(c) for c in columns]
        if opclass and self.index_opclass:
            key_cols = [f"{c} {opclass}" for c in key_cols]
        return [
            self._composite_index_sql(
                table,
                name,
                ", ".join(key_cols),
                ine=ine,
                unique=unique,
                using=using,
                include_sql=include_sql,
                condition=condition,
            )
        ]

    def render_drop_composite_index(self, name: str) -> list[str]:
        """Render a statement dropping a named index.

        Args:
            name: The index name.

        Returns:
            The list with the drop-index statement.
        """
        return [f"DROP INDEX IF EXISTS {self.quote(name)}"]

    # -- rename / constraint rendering -----------------------------------
    def render_rename_table(self, old: str, new: str) -> list[str]:
        """Render a statement to rename a table.

        Args:
            old: The current table name.
            new: The new table name.

        Returns:
            The list with the rename-table statement.
        """
        return [f"ALTER TABLE {self.quote(old)} RENAME TO {self.quote(new)}"]

    def render_rename_column(self, table: str, old: str, new: str) -> list[str]:
        """Render a statement to rename a column.

        Args:
            table: The table name.
            old: The current column name.
            new: The new column name.

        Returns:
            The list with the rename-column statement.
        """
        return [
            f"ALTER TABLE {self.quote(table)} RENAME COLUMN {self.quote(old)} TO {self.quote(new)}"
        ]

    def render_rename_index(
        self,
        table: str,
        column: str,
        old_name: str,
        new_name: str,
        unique: bool = False,
    ) -> list[str]:
        """Render statements that rename an index.

        On dialects that can rename an index in place (PostgreSQL) this emits a
        single ``ALTER INDEX`` statement; otherwise the index is dropped and
        recreated under the new name.

        Args:
            table: The table owning the index.
            column: The indexed column (used to recreate on rebuild dialects).
            old_name: The current index name.
            new_name: The new index name.
            unique: Whether the recreated index should be ``UNIQUE``.

        Returns:
            The list of SQL statements renaming the index.
        """
        if self.rename_index_in_place:
            return [
                f"ALTER INDEX IF EXISTS {self.quote(old_name)} RENAME TO {self.quote(new_name)}"
            ]
        out = self.render_drop_index(table, column, name=old_name)
        out.extend(self.render_create_index(table, column, unique=unique, name=new_name))
        return out

    def _constraint_clause(self, constraint: dict[str, Any]) -> str:
        """Render the inline DDL clause for a table constraint.

        Args:
            constraint: A constraint spec with ``kind`` (``unique``/``check``),
                an optional ``name``, and either ``fields`` or ``check``.

        Returns:
            The constraint clause SQL fragment.
        """
        named = f"CONSTRAINT {self.quote(constraint['name'])} " if constraint.get("name") else ""
        if constraint["kind"] == "check":
            return f"{named}CHECK ({constraint['check']})"
        cols = ", ".join(self.quote(c) for c in constraint["fields"])
        return f"{named}UNIQUE ({cols})"

    def render_add_constraint(self, table: str, constraint: dict[str, Any]) -> list[str]:
        """Render a statement that adds a table constraint.

        Args:
            table: The table name.
            constraint: The constraint spec to add.

        Raises:
            UnSupportedError: If the dialect cannot alter constraints in place.

        Returns:
            The list with the add-constraint statement.
        """
        if not self.alter_constraint_in_place:
            raise UnSupportedError(
                f"{self.name} cannot ADD a constraint in place; use a unique index or RunSQL"
            )
        return [f"ALTER TABLE {self.quote(table)} ADD {self._constraint_clause(constraint)}"]

    def render_drop_constraint(self, table: str, name: str) -> list[str]:
        """Render a statement that drops a named table constraint.

        Args:
            table: The table name.
            name: The constraint name to drop.

        Raises:
            UnSupportedError: If the dialect cannot alter constraints in place.

        Returns:
            The list with the drop-constraint statement.
        """
        if not self.alter_constraint_in_place:
            raise UnSupportedError(
                f"{self.name} cannot DROP a constraint in place; rebuild the table via RunSQL"
            )
        return [f"ALTER TABLE {self.quote(table)} DROP CONSTRAINT IF EXISTS {self.quote(name)}"]

    def render_rename_constraint(self, table: str, old: str, new: str) -> list[str]:
        """Render a statement that renames a table constraint.

        Args:
            table: The table name.
            old: The current constraint name.
            new: The new constraint name.

        Raises:
            UnSupportedError: If the dialect cannot alter constraints in place.

        Returns:
            The list with the rename-constraint statement.
        """
        if not self.alter_constraint_in_place:
            raise UnSupportedError(f"{self.name} cannot RENAME a constraint in place")
        return [
            f"ALTER TABLE {self.quote(table)} "
            f"RENAME CONSTRAINT {self.quote(old)} TO {self.quote(new)}"
        ]


class PostgresDialect(BaseDialect):
    """Dialect rendering SQL for PostgreSQL."""

    name = "postgres"
    # `~` / `~*` are PostgreSQL's POSIX regex match operators (case-sensitive
    # and case-insensitive); `__search` uses full-text via to_tsvector/tsquery.
    regex_ops = {"regex": "~", "iregex": "~*", "posix_regex": "~", "iposix_regex": "~*"}
    supports_search = True

    def search_sql(self, col: str, placeholder: str) -> str:
        """Render a PostgreSQL full-text match using ``to_tsvector``/``plainto_tsquery``.

        Args:
            col: The already-qualified column reference.
            placeholder: The bound-parameter placeholder for the search query.

        Returns:
            A ``to_tsvector(...) @@ plainto_tsquery(...)`` boolean expression.
        """
        return f"to_tsvector('english', {col}) @@ plainto_tsquery('english', {placeholder})"

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
    # SQLite's human-readable plan comes from EXPLAIN QUERY PLAN.
    explain_prefix = "EXPLAIN QUERY PLAN "
    # date/time part -> strftime format specifier (results CAST to INTEGER).
    _strftime_parts = {
        "year": "%Y",
        "month": "%m",
        "week": "%W",
        "day": "%d",
        "hour": "%H",
        "minute": "%M",
        "second": "%S",
    }

    def date_part_sql(self, part: str, col: str) -> str:
        """Render a date/time part extraction using ``strftime``.

        Args:
            part: A supported date/time part name.
            col: The already-qualified column reference.

        Raises:
            UnSupportedError: For ``microsecond`` (SQLite's stored timestamp
                resolution makes it unreliable).

        Returns:
            A SQL integer expression yielding the requested part.
        """
        if part == "quarter":
            # SQLite has no quarter; derive it from the month (1-12 -> 1-4).
            return f"((CAST(strftime('%m', {col}) AS INTEGER) + 2) / 3)"
        if part == "microsecond":
            raise UnSupportedError("SQLite does not support the __microsecond lookup")
        return f"CAST(strftime('{self._strftime_parts[part]}', {col}) AS INTEGER)"

    def truncate_date_sql(self, col: str) -> str:
        """Render a date truncation using SQLite's ``date()`` function.

        Args:
            col: The already-qualified column reference.

        Returns:
            ``date(col)`` for the ``__date`` lookup.
        """
        return f"date({col})"

    # SQLite has no ``IF [NOT] EXISTS`` on ADD/DROP COLUMN, no ``CONCURRENTLY``,
    # no in-place ``ALTER COLUMN`` (a column change needs a table rebuild), no
    # ``ALTER INDEX ... RENAME``, and no ``ALTER TABLE ... CONSTRAINT`` syntax.
    column_if_exists = False
    index_concurrently = False
    alter_column_in_place = False
    rename_index_in_place = False
    alter_constraint_in_place = False
    index_using = False
    index_include = False
    index_opclass = False

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
            if isinstance(field, ForeignKeyField) and field.db_constraint:
                ref = get_model(field.reference)
                col = self.quote(field.db_column)
                ref_tbl = self.quote(ref._meta.table)
                ref_pk = self.quote(ref._meta.pk_field.db_column)
                lines.append(
                    f"FOREIGN KEY ({col}) REFERENCES {ref_tbl} ({ref_pk}) "
                    f"ON DELETE {field.on_delete}"
                )
        lines.extend(self._unique_together_lines(meta))
        for constraint in meta.constraints:
            lines.append(self._constraint_clause(constraint.to_spec()))

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
        statements.extend(self._composite_index_statements(meta, ine))
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

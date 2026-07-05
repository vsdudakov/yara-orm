"""SQL dialects.

Each dialect owns every database-specific decision: identifier quoting,
parameter placeholders and the mapping from a field's abstract *kind* to a
concrete column type. Supporting a new database means adding a subclass and
registering it -- the model and queryset layers never change.
"""

from __future__ import annotations

import datetime as _dt
import json as _json
import re
import uuid as _uuid
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from .db_defaults import DatabaseDefault, Now, RandomHex, SqlDefault
from .exceptions import ConfigurationError, UnSupportedError
from .fields import ForeignKeyFieldInstance, registered_field_kind
from .registry import get_model

if TYPE_CHECKING:
    from .fields import Field
    from .models import Index, MetaInfo, Model
    from .relations import M2MInfo


#: Memoised identifier -> quoted identifier, shared by every dialect (both quote
#: identically). Bounded by the set of table/column names in the schema.
_QUOTE_CACHE: dict[str, str] = {}

#: Index access methods accepted for ``USING <method>`` (a closed keyword set)
#: and the safe shape of an operator-class identifier. Both are spliced into
#: index DDL verbatim (they cannot be bound), so they are validated to keep
#: arbitrary SQL out of ``CREATE INDEX``.
_INDEX_METHODS = frozenset({"btree", "hash", "gist", "gin", "spgist", "brin", "fulltext"})
_OPCLASS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")

#: Statements bracketing SQLite's table rebuild. ``PRAGMA foreign_keys`` is a
#: silent no-op inside a transaction, so the migration runner recognises these
#: exact strings and executes them *outside* the migration transaction (the
#: standard SQLite rebuild recipe: enforcement off, rebuild, enforcement on).
PRAGMA_FK_OFF = "PRAGMA foreign_keys=OFF"
PRAGMA_FK_ON = "PRAGMA foreign_keys=ON"


def _validate_index_using(using: str | None) -> None:
    """Reject an index access method outside the known set.

    Args:
        using: The ``USING`` method, or None.

    Raises:
        ValueError: When ``using`` is not a recognised access method.
    """
    if using is not None and using.lower() not in _INDEX_METHODS:
        raise ValueError(
            f"invalid index method {using!r}; expected one of {sorted(_INDEX_METHODS)}"
        )


def _validate_index_opclass(opclass: str | None) -> None:
    """Reject an operator class that is not a plain (optionally qualified) identifier.

    Args:
        opclass: The operator-class name, or None.

    Raises:
        ValueError: When ``opclass`` is not a safe identifier.
    """
    if opclass is not None and not _OPCLASS_RE.match(opclass):
        raise ValueError(f"invalid index opclass {opclass!r}: expected a plain identifier")


class BaseDialect:
    """Base class rendering backend-agnostic SQL for a database dialect."""

    name = "base"

    #: kind -> SQL type template (``str.format`` with ``type_params``).
    type_map: dict[str, str] = {}
    #: kind -> auto-increment SQL type template.
    serial_map: dict[str, str] = {}
    #: Operator used for case-insensitive ``LIKE`` lookups.
    ilike = "ILIKE"
    #: Operator used for case-*sensitive* ``LIKE`` lookups. MySQL's default
    #: utf8mb4 collation makes plain ``LIKE`` case-insensitive, so it overrides
    #: this with ``LIKE BINARY`` (the inverse of SQLite's ``ilike`` situation).
    like = "LIKE"
    #: Clause declaring backslash as the LIKE escape character, appended to
    #: every pattern lookup (the pattern builder backslash-escapes ``%``/``_``).
    #: Renders ``ESCAPE '\'``; MySQL overrides it because backslash is an
    #: escape inside its string literals and must be doubled there.
    like_escape = " ESCAPE '\\'"
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
    #: Whether ``OFFSET`` requires a preceding ``LIMIT`` (SQLite does; a bare
    #: ``OFFSET`` is a syntax error there, so an offset-only slice needs
    #: ``LIMIT -1``). PostgreSQL accepts ``OFFSET`` alone.
    offset_requires_limit = False
    #: The LIMIT value meaning "no limit", used when ``offset_requires_limit``
    #: forces a LIMIT onto an offset-only slice (SQLite's sentinel is ``-1``;
    #: MySQL's documented spelling is the maximum row count).
    no_limit = -1
    #: Whether ``INSERT ... RETURNING`` is available (PostgreSQL, SQLite >=
    #: 3.35). When False the model layer compiles inserts without RETURNING and
    #: reads the new auto-increment pk from the driver-reported last-insert id.
    supports_insert_returning = True
    #: The INSERT verb used when conflicting rows should be silently skipped.
    #: Backends that spell "ignore duplicates" on the verb (MySQL's
    #: ``INSERT IGNORE``) override this; the others keep plain ``INSERT`` and
    #: render an ``ON CONFLICT ... DO NOTHING`` clause instead.
    insert_ignore_verb = "INSERT"
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
    #: Whether the backend supports ``CREATE EXTENSION`` (PostgreSQL only).
    supports_extensions = False
    #: Whether ``SELECT ... FOR UPDATE`` row locks are supported
    #: (PostgreSQL/MySQL); on SQLite the whole database locks instead, so the
    #: clause is dropped.
    supports_for_update = False
    #: Whether ``FOR UPDATE OF <table>`` (locking only specific tables of a
    #: join) is accepted. MariaDB has no such syntax — it locks every table in
    #: the statement — so its dialect drops the ``OF`` target.
    supports_for_update_of = True
    #: Whether an UPDATE/DELETE that subqueries its *own* table must wrap that
    #: subquery in a derived table. MySQL rejects the direct form ("You can't
    #: specify target table ... for update in FROM clause", error 1093).
    modifying_subquery_needs_wrap = False
    #: Whether aggregates accept a ``FILTER (WHERE ...)`` clause (PostgreSQL,
    #: SQLite 3.30+). MySQL lacks it; the compiler falls back to the
    #: equivalent ``AGG(CASE WHEN ... THEN col END)`` there.
    supports_aggregate_filter = True
    #: Whether a single ``INSERT`` can carry multiple ``VALUES (...)`` rows.
    #: Oracle cannot (its multi-row insert is ``INSERT ALL`` / a MERGE source),
    #: so ``bulk_create`` falls back to one statement per row there.
    supports_multirow_insert = True
    #: Whether grouping by a table's primary key lets the other selected columns
    #: of that table appear unaggregated (functional dependency — PostgreSQL's
    #: rule, and SQLite/MySQL allow bare columns too). Oracle enforces the strict
    #: SQL rule, so every selected non-aggregate column must be grouped.
    group_by_functional_dependency = True

    # -- identifiers & placeholders --------------------------------------
    def quote(self, identifier: str) -> str:
        """Quote a SQL identifier, escaping embedded quote characters.

        The result depends only on the identifier (both current dialects
        double-quote), so it is memoised in a shared cache: the SQL compilers
        re-quote the same fixed set of table/column names thousands of times per
        query build, and the identifier set is small and bounded.

        Args:
            identifier: The identifier (table or column name) to quote.

        Returns:
            The double-quoted, escaped identifier.
        """
        quoted = _QUOTE_CACHE.get(identifier)
        if quoted is None:
            quoted = _QUOTE_CACHE[identifier] = '"{}"'.format(identifier.replace('"', '""'))
        return quoted

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

        Backend-specific; concrete dialects override this.

        Args:
            part: One of ``year``/``month``/``day``/``hour``/``minute``/``second``.
            col: The already-qualified column reference.

        Raises:
            UnSupportedError: On a dialect that does not implement it.

        Returns:
            A SQL expression yielding the integer part.
        """
        raise UnSupportedError(f"{self.name} does not support date-part extraction")

    def truncate_date_sql(self, col: str) -> str:
        """Render an expression truncating a datetime column to a date.

        Backend-specific; concrete dialects override this.

        Args:
            col: The already-qualified column reference.

        Raises:
            UnSupportedError: On a dialect that does not implement it.

        Returns:
            A SQL expression yielding the date part (for the ``__date`` lookup).
        """
        raise UnSupportedError(f"{self.name} does not support the __date lookup")

    def json_extract_sql(self, col: str, keys: list[str]) -> str:
        """Render extraction of a JSON key path from a column, as text.

        Backend-specific; concrete dialects override this. With no keys the
        column itself is returned.

        Args:
            col: The already-qualified JSON column reference.
            keys: The object keys to traverse (outermost first).

        Raises:
            UnSupportedError: On a dialect that does not implement it.

        Returns:
            A SQL expression yielding the addressed value as text.
        """
        if not keys:
            return col
        raise UnSupportedError(f"{self.name} does not support JSON key-path lookups")

    def json_contains_sql(self, col: str, placeholder: str) -> str:
        """Render a JSON containment test (``__contains`` on a ``JSONField``).

        Backend-specific; concrete dialects override this.

        Args:
            col: The already-qualified JSON column reference.
            placeholder: The bound-parameter placeholder for the JSON value.

        Raises:
            UnSupportedError: On a dialect that does not implement it.

        Returns:
            A boolean SQL expression testing containment.
        """
        raise UnSupportedError(f"{self.name} does not support JSON containment")

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

    # -- row decoding -------------------------------------------------------
    def read_decoder(self, field: Field) -> Callable[[Any], Any] | None:
        """Return the per-value converter hydration applies to ``field``.

        The default honours the field's own contract: identity for
        ``read_identity`` fields (the engine already returns the native type),
        ``to_python`` otherwise. A dialect whose driver cannot express a type
        natively (MySQL returns ``CHAR(36)`` uuid columns as ``str``) overrides
        this to reconstruct the Python value on read.

        Args:
            field: The field whose column is being decoded.

        Returns:
            A one-argument converter, or None to assign the value directly.
        """
        return None if field.read_identity else field.to_python

    def concat_sql(self, parts: list[str]) -> str:
        """Render string concatenation of already-rendered SQL operands.

        PostgreSQL and SQLite use the portable ``||`` operator; MySQL treats
        ``||`` as logical OR by default, so its dialect renders ``CONCAT``.

        Args:
            parts: The rendered SQL operand expressions.

        Returns:
            The concatenation SQL expression.
        """
        return "(" + " || ".join(parts) + ")"

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

    def _values_placeholders(self, nrows: int, ncols: int, start: int = 1) -> str:
        """Render ``(?, ?), (?, ?), ...`` placeholder groups for a bulk INSERT.

        Args:
            nrows: Number of value rows.
            ncols: Number of columns per row.
            start: The 1-based index of the first placeholder.

        Returns:
            The comma-separated parenthesised placeholder groups.
        """
        rows = []
        idx = start
        for _ in range(nrows):
            holes = ", ".join(self.placeholder(idx + j) for j in range(ncols))
            rows.append(f"({holes})")
            idx += ncols
        return ", ".join(rows)

    def render_upsert(
        self,
        table: str,
        columns: Sequence[str],
        nrows: int,
        conflict_columns: Sequence[str],
        update_columns: Sequence[str],
        pk_columns: Sequence[str],
    ) -> str:
        """Render a multi-row INSERT that skips or updates conflicting rows.

        The default is an ``INSERT ... VALUES ...`` with the dialect's
        ``ON CONFLICT`` clause (or ``INSERT IGNORE`` verb on MySQL). Oracle,
        which has neither, renders an equivalent ``MERGE`` (see its override).

        Args:
            table: The already-quoted target table.
            columns: The unquoted column names, in insert (and bind) order.
            nrows: Number of value rows.
            conflict_columns: The unquoted conflict-target columns (may be
                empty for a bare "skip any duplicate").
            update_columns: The unquoted columns to overwrite on conflict
                (empty means ignore the conflicting row).
            pk_columns: The unquoted primary-key columns, a conflict-target
                fallback for dialects (Oracle) that require one.

        Returns:
            The complete upsert statement.
        """
        cols_sql = ", ".join(self.quote(c) for c in columns)
        values = self._values_placeholders(nrows, len(columns))
        verb = self.insert_ignore_verb if not update_columns else "INSERT"
        conflict = self.on_conflict_sql(list(conflict_columns), list(update_columns))
        return f"{verb} INTO {table} ({cols_sql}) VALUES {values}{conflict}"

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

    def regex_sql(self, op: str, col: str, placeholder: str) -> str:
        """Render a regular-expression lookup condition.

        The default renders the infix operator registered in
        :attr:`regex_ops` (``~``/``~*`` on PostgreSQL); dialects whose regex
        match is a function call (MySQL's ``REGEXP_LIKE``) override this.

        Args:
            op: The lookup name (``regex``/``iregex``/``posix_regex``/
                ``iposix_regex``).
            col: The already-qualified column reference.
            placeholder: The bound-parameter placeholder for the pattern.

        Raises:
            UnSupportedError: On backends without a regex operator.

        Returns:
            A boolean SQL expression matching ``col`` against the pattern.
        """
        regex_op = self.regex_ops.get(op)
        if regex_op is None:
            raise UnSupportedError(f"{self.name} does not support the __{op} lookup")
        return f"{col} {regex_op} {placeholder}"

    def like_pattern_sql(self, case_insensitive: bool, col: str, placeholder: str) -> str:
        """Render a ``LIKE``/``ILIKE`` pattern condition with its ESCAPE clause.

        The default spells the case-sensitive/insensitive match with the
        dialect's :attr:`like`/:attr:`ilike` operator. Oracle has no ``ILIKE``
        operator, so its dialect folds both operands with ``UPPER()`` instead.

        Args:
            case_insensitive: Whether the lookup ignores case (``i*`` lookups).
            col: The already-qualified (already text-cast) column reference.
            placeholder: The bound-parameter placeholder for the pattern.

        Returns:
            A boolean SQL expression matching ``col`` against the pattern.
        """
        op = self.ilike if case_insensitive else self.like
        return f"{col} {op} {placeholder}{self.like_escape}"

    def limit_offset_sql(self, limit: int | None, offset: int | None) -> str:
        """Render the trailing row-limit / row-offset clause.

        The default is the portable ``LIMIT n OFFSET m`` (supplying the
        dialect's "no limit" sentinel when an offset-only slice needs a leading
        ``LIMIT``). Oracle has no ``LIMIT``; its dialect renders the SQL-standard
        ``OFFSET m ROWS FETCH NEXT n ROWS ONLY`` instead.

        Args:
            limit: The maximum row count, or None.
            offset: The number of leading rows to skip, or None.

        Returns:
            The clause fragment (leading space included), or ``""``.
        """
        tail = ""
        if limit is None and offset is not None and self.offset_requires_limit:
            limit = self.no_limit
        if limit is not None:
            tail += f" LIMIT {int(limit)}"
        if offset is not None:
            tail += f" OFFSET {int(offset)}"
        return tail

    def insert_default_values_sql(self, pk_column: str) -> str:
        """Render the statement tail that inserts a row of column defaults.

        Rendered as ``INSERT INTO t <this>`` for a model with no writable
        columns (only an auto-increment pk). The default is ``DEFAULT VALUES``;
        MySQL has no such syntax, and Oracle spells it ``(pk) VALUES (DEFAULT)``.

        Args:
            pk_column: The primary-key column name (used by dialects that must
                name a column, e.g. Oracle).

        Returns:
            The statement tail following the table name.
        """
        return "DEFAULT VALUES"

    def insert_returning_clause(self, fields: Sequence[Field]) -> str:
        """Render the ``RETURNING`` clause appended to an INSERT.

        The default is PostgreSQL's result-set form (``RETURNING a, b``); Oracle
        spells it with OUT binds (``RETURNING a, b INTO :ret_0, :ret_1``), which
        its backend routes through the driver's OUT-bind path.

        Args:
            fields: The fields whose columns are returned (pk first).

        Returns:
            The clause fragment with a leading space.
        """
        return " RETURNING " + ", ".join(self.quote(f.db_column) for f in fields)

    # -- type rendering ---------------------------------------------------
    def _kind_template(self, kind: str) -> str:
        """Resolve the SQL type template for a kind, consulting the registry.

        Built-in kinds come from :attr:`type_map`; anything else falls back to
        the :func:`~yara_orm.fields.register_field_kind` registry.

        Args:
            kind: The abstract field kind.

        Raises:
            ConfigurationError: When the kind is neither built in nor
                registered (or is registered without SQL for this dialect).

        Returns:
            The ``str.format`` template for the kind's SQL type.
        """
        template = self.type_map.get(kind)
        if template is not None:
            return template
        registration = registered_field_kind(kind)
        if registration is None:
            raise ConfigurationError(
                f"Dialect {self.name!r} has no type mapping for kind {kind!r}; "
                "register custom kinds with yara_orm.register_field_kind(...)"
            )
        return registration.sql_template(self.name)

    def _render_type(self, kind: str, type_params: dict[str, Any]) -> str:
        """Render the concrete SQL type for a kind from its type parameters.

        Args:
            kind: The abstract field kind.
            type_params: The field's type parameters filled into the template.

        Raises:
            ConfigurationError: When the template's placeholders do not match
                the type parameters.

        Returns:
            The SQL type string.
        """
        template = self._kind_template(kind)
        try:
            return template.format(**type_params)
        except (KeyError, IndexError) as exc:
            raise ConfigurationError(
                f"cannot render the SQL type for kind {kind!r}: template {template!r} "
                f"does not match type_params {type_params!r} ({exc!r})"
            ) from exc

    def column_type(self, field: Field) -> str:
        """Resolve the concrete SQL column type for a field.

        Args:
            field: The field whose column type is rendered.

        Returns:
            The SQL type string for the field.
        """
        kind = field.field_kind
        if isinstance(field, ForeignKeyFieldInstance):
            ref = get_model(field.reference)
            pk = ref._meta.pk_field
            # Reference the scalar type of the target pk, never its serial form.
            return self._render_type(pk.field_kind, pk.type_params)

        if field.auto_increment and kind in self.serial_map:
            return self.serial_map[kind]

        return self._render_type(kind, field.type_params)

    # -- required extensions ----------------------------------------------
    def extensions_sql(self, models: list[type[Model]]) -> list[str]:
        """Render ``CREATE EXTENSION`` statements the models' fields require.

        Collects the ``requires_extension`` declarations of every registered
        field kind used by the models (deduplicated, sorted). Empty on dialects
        without extension support (SQLite).

        Args:
            models: The models whose fields are scanned.

        Returns:
            The ``CREATE EXTENSION IF NOT EXISTS`` statements (possibly empty).
        """
        if not self.supports_extensions:
            return []
        extensions: set[str] = set()
        for model in models:
            for field in model._meta.fields.values():
                registration = registered_field_kind(field.field_kind)
                if registration is not None and registration.requires_extension:
                    extensions.add(registration.requires_extension)
        return [f"CREATE EXTENSION IF NOT EXISTS {self.quote(ext)}" for ext in sorted(extensions)]

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
        _validate_index_using(using)
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
        _validate_index_opclass(index.opclass)
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
        pk_line = self._pk_line(meta)
        if pk_line:
            lines.append(pk_line)

        for field in meta.fields.values():
            if isinstance(field, ForeignKeyFieldInstance) and field.db_constraint:
                ref = get_model(field.reference)
                col = self.quote(field.db_column)
                ref_tbl = self.quote(ref._meta.table)
                ref_pk = self.quote(ref._meta.pk_field.db_column)
                lines.append(self._fk_clause(col, ref_tbl, ref_pk, field.on_delete))
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

    def _pk_line(self, meta: MetaInfo) -> str | None:
        """Return the table-level ``PRIMARY KEY (...)`` clause, or None.

        A dialect that renders the primary key inline on the column (SQLite's
        ``INTEGER PRIMARY KEY``) returns None to omit the separate clause.

        Args:
            meta: The model metadata describing the table.

        Returns:
            The ``PRIMARY KEY (...)`` clause, or None to omit it.
        """
        return f"PRIMARY KEY ({self.quote(meta.pk_field.db_column)})"

    def _fk_clause(self, col: str, ref_tbl: str, ref_pk: str, on_delete: str) -> str:
        """Render a table-level ``FOREIGN KEY`` clause with its ``ON DELETE``.

        The default emits the action verbatim. Oracle accepts only ``CASCADE``
        and ``SET NULL`` there, so its dialect drops an unsupported
        ``RESTRICT``/``NO ACTION`` (which is Oracle's default behaviour anyway).

        Args:
            col: The already-quoted referencing column.
            ref_tbl: The already-quoted referenced table.
            ref_pk: The already-quoted referenced primary key column.
            on_delete: The ``ON DELETE`` action.

        Returns:
            The ``FOREIGN KEY (...) REFERENCES ... ON DELETE ...`` clause.
        """
        return f"FOREIGN KEY ({col}) REFERENCES {ref_tbl} ({ref_pk}) ON DELETE {on_delete}"

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
        return self._render_type(pk_field.field_kind, pk_field.type_params)

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
        return self._render_type(kind, spec.get("type_params", {}))

    def _render_default(self, default: dict[str, Any]) -> str:
        """Render the SQL expression for a migration default spec.

        The spec is the serialised form of a ``db_defaults`` object (built by
        the migration spec-builder), reconstructed here so the expression
        renders for the active dialect.

        Args:
            default: The default spec (``kind`` plus its parameters).

        Returns:
            The SQL default expression.
        """
        kind = default["kind"]
        if kind == "now":
            return Now().to_sql(self)
        if kind == "random_hex":
            return RandomHex(default["size"]).to_sql(self)
        return SqlDefault(default["sql"]).to_sql(self)

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
        if spec.get("default"):
            # Parenthesised for the same reason as ``column_sql``: SQLite needs
            # expression defaults in parens, and PostgreSQL accepts them.
            parts.append(f"DEFAULT ({self._render_default(spec['default'])})")
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
            return self.render_rebuild_table(table, table_spec)
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
        if old.get("default") != new.get("default"):
            if new.get("default"):
                expr = self._render_default(new["default"])
                out.append(f"ALTER TABLE {t} ALTER COLUMN {col} SET DEFAULT ({expr})")
            else:
                out.append(f"ALTER TABLE {t} ALTER COLUMN {col} DROP DEFAULT")
        if bool(old.get("unique")) != bool(new.get("unique")) and not new.get("pk"):
            # The inline column UNIQUE renders as PostgreSQL's default-named
            # constraint (<table>_<column>_key), so toggle that constraint.
            uq = self.quote(f"{table}_{name}_key")
            if new.get("unique"):
                out.append(f"ALTER TABLE {t} ADD CONSTRAINT {uq} UNIQUE ({col})")
            else:
                out.append(f"ALTER TABLE {t} DROP CONSTRAINT IF EXISTS {uq}")
        if old.get("fk") != new.get("fk"):
            # Same story for the FOREIGN KEY constraint (<table>_<column>_fkey):
            # drop and re-add it to change the target or ON DELETE action.
            fk = self.quote(f"{table}_{name}_fkey")
            out.append(f"ALTER TABLE {t} DROP CONSTRAINT IF EXISTS {fk}")
            ref = new.get("fk")
            if ref:
                out.append(
                    f"ALTER TABLE {t} ADD CONSTRAINT {fk} FOREIGN KEY ({col}) "
                    f"REFERENCES {self.quote(ref['table'])} ({self.quote(ref['pk'])}) "
                    f"ON DELETE {ref.get('on_delete', 'CASCADE')}"
                )
        return out

    def render_rebuild_table(self, table: str, table_spec: dict[str, Any]) -> list[str]:
        """Rebuild a table to apply a change a dialect cannot do in place.

        Copies the rows into a freshly created table (carrying ``table_spec``)
        and swaps it in, the standard SQLite approach to altering a column or
        changing a constraint. Secondary indexes are created **after** the
        swap, under the final table name: ``RENAME TO`` does not rename
        indexes, so creating them on the temp table would leave them named
        ``idx__new_<table>_...`` (breaking later index drops and colliding on
        the next rebuild).

        Args:
            table: The table name.
            table_spec: The full table spec the rebuilt table should match.

        Returns:
            The list of SQL statements rebuilding the table.
        """
        tmp = f"_new_{table}"
        bare = dict(table_spec)
        indexes = bare.pop("indexes", None) or []
        composite = bare.pop("composite_indexes", None) or {}
        bare["indexes"] = []
        cols = ", ".join(self.quote(c) for c in table_spec["columns"])
        out = self.render_create_table(tmp, bare, safe=False)
        out.append(f"INSERT INTO {self.quote(tmp)} ({cols}) SELECT {cols} FROM {self.quote(table)}")
        out.append(f"DROP TABLE {self.quote(table)}")
        out.append(f"ALTER TABLE {self.quote(tmp)} RENAME TO {self.quote(table)}")
        for col in indexes:
            out.extend(self.render_create_index(table, col, safe=False))
        for idx_name, spec in composite.items():
            out.extend(
                self.render_create_composite_index(
                    table,
                    idx_name,
                    spec["columns"],
                    safe=False,
                    condition=spec.get("condition"),
                    unique=spec.get("unique", False),
                    using=spec.get("using"),
                    include=spec.get("include"),
                    opclass=spec.get("opclass"),
                )
            )
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
        _validate_index_opclass(opclass)
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

    def render_drop_composite_index(self, name: str, table: str | None = None) -> list[str]:
        """Render a statement dropping a named index.

        Args:
            name: The index name.
            table: The owning table; unused here (PostgreSQL/SQLite drop by
                name), required by dialects whose ``DROP INDEX`` needs it
                (MySQL).

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
    supports_extensions = True
    supports_for_update = True

    def date_part_sql(self, part: str, col: str) -> str:
        """Render ``EXTRACT(<part> FROM col)``.

        Args:
            part: One of ``year``/``month``/``day``/``hour``/``minute``/``second``.
            col: The already-qualified column reference.

        Returns:
            A SQL expression yielding the integer part.
        """
        return f"EXTRACT({self._extract_parts[part]} FROM {col})"

    def truncate_date_sql(self, col: str) -> str:
        """Render ``CAST(col AS DATE)`` (for the ``__date`` lookup).

        Args:
            col: The already-qualified column reference.

        Returns:
            A SQL expression yielding the date part.
        """
        return f"CAST({col} AS DATE)"

    def json_extract_sql(self, col: str, keys: list[str]) -> str:
        """Render a JSON key path as text: chain ``->`` then ``->>`` for the last.

        The result is text (comparable with a bound string). With no keys the
        column itself is returned.

        Args:
            col: The already-qualified JSON column reference.
            keys: The object keys to traverse (outermost first).

        Returns:
            A SQL expression yielding the addressed value as text.
        """
        if not keys:
            return col
        parts = [col]
        last = len(keys) - 1
        for i, key in enumerate(keys):
            parts.append(f"{'->>' if i == last else '->'} {self._literal(key)}")
        return " ".join(parts)

    def json_contains_sql(self, col: str, placeholder: str) -> str:
        """Render a JSON containment test with the ``@>`` operator.

        The bound value is a JSON string cast to ``jsonb``, so an object subset,
        an array element, or an array-of-objects subset all match.

        Args:
            col: The already-qualified JSON column reference.
            placeholder: The bound-parameter placeholder for the JSON value.

        Returns:
            A boolean SQL expression testing containment.
        """
        return f"{col} @> {placeholder}::jsonb"

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

    def json_extract_sql(self, col: str, keys: list[str]) -> str:
        """Render a JSON key-path extraction using SQLite's ``json_extract``.

        Args:
            col: The already-qualified JSON column reference.
            keys: The object keys to traverse (outermost first).

        Returns:
            ``json_extract(col, '$.a.b')`` (the column itself with no keys).
        """
        if not keys:
            return col
        path = "$." + ".".join(keys)
        return f"json_extract({col}, {self._literal(path)})"

    def json_contains_sql(self, col: str, placeholder: str) -> str:
        """SQLite has no JSON containment operator; reject ``__contains`` on JSON.

        Args:
            col: The already-qualified JSON column reference (unused).
            placeholder: The bound-parameter placeholder (unused).

        Raises:
            UnSupportedError: Always — use PostgreSQL for JSON ``@>`` containment.
        """
        raise UnSupportedError("SQLite does not support JSON __contains (@>)")

    # SQLite has no ``IF [NOT] EXISTS`` on ADD/DROP COLUMN, no ``CONCURRENTLY``,
    # no in-place ``ALTER COLUMN`` (a column change needs a table rebuild), no
    # ``ALTER INDEX ... RENAME``, and no ``ALTER TABLE ... CONSTRAINT`` syntax.
    offset_requires_limit = True
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
    def render_rebuild_table(self, table: str, table_spec: dict[str, Any]) -> list[str]:
        """Rebuild a table with foreign-key enforcement toggled off around it.

        The rebuild drops the original table, and with ``PRAGMA foreign_keys``
        on (set per connection by the backend) that drop performs an implicit
        ``DELETE`` which fires ``ON DELETE CASCADE`` on referencing tables —
        silently wiping child rows. The pragma statements bracket the rebuild;
        they are no-ops inside a transaction, so the migration runner executes
        them outside it (see the ``PRAGMA_FK_OFF``/``PRAGMA_FK_ON`` contract).

        Args:
            table: The table name.
            table_spec: The full table spec the rebuilt table should match.

        Returns:
            The list of SQL statements rebuilding the table.
        """
        return [PRAGMA_FK_OFF, *super().render_rebuild_table(table, table_spec), PRAGMA_FK_ON]

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

    def _pk_line(self, meta: MetaInfo) -> str | None:
        """Omit the separate ``PRIMARY KEY`` clause for an inline auto pk.

        SQLite renders an auto-increment primary key inline as ``INTEGER PRIMARY
        KEY``; any other primary key still needs the table-level clause.

        Args:
            meta: The model metadata describing the table.

        Returns:
            The ``PRIMARY KEY (...)`` clause, or None for an inline auto pk.
        """
        if self._is_auto_pk(meta.pk_field):
            return None
        return super()._pk_line(meta)


def _uuid_from_db(value: Any) -> Any:
    """Reconstruct a ``uuid.UUID`` from the text a CHAR(36) column returns.

    Args:
        value: The raw column value (str on MySQL, already a UUID elsewhere).

    Returns:
        The value as a ``uuid.UUID`` (None passes through).
    """
    if value is None or isinstance(value, _uuid.UUID):
        return value
    return _uuid.UUID(str(value))


def _datetime_from_db(value: Any) -> Any:
    """Re-attach UTC to a naive datetime read from MySQL when ``use_tz`` is on.

    MySQL's DATETIME has no timezone; aware values are stored as UTC-naive, so
    under ``use_tz`` the stored instant is re-labelled UTC on read (matching
    what PostgreSQL/SQLite return for aware columns). With ``use_tz`` off the
    naive value passes through.

    Args:
        value: The raw column value.

    Returns:
        The datetime, aware UTC under ``use_tz``.
    """
    from . import timezone as _tz

    if isinstance(value, _dt.datetime) and value.tzinfo is None and _tz.get_use_tz():
        return value.replace(tzinfo=_dt.timezone.utc)
    return value


class MySQLDialect(BaseDialect):
    """Dialect rendering SQL for MySQL 8.x (and compatible servers).

    Key departures from the base dialect: backtick identifier quoting,
    unnumbered ``?`` placeholders, no ``INSERT ... RETURNING`` (the new pk
    arrives via the driver's last-insert id), upserts spelled
    ``INSERT IGNORE`` / ``INSERT ... AS new ON DUPLICATE KEY UPDATE`` (the
    8.4-safe alias form — the ``VALUES()`` function is removed), and
    case-sensitivity handled per lookup: the default utf8mb4 collation makes
    plain ``LIKE`` case-insensitive, so case-sensitive pattern lookups use
    ``LIKE BINARY``.
    """

    name = "mysql"
    # Regex lookups are supported (via REGEXP_LIKE with a case flag; see
    # ``regex_sql``); the keys here advertise which lookups resolve.
    regex_ops = {
        "regex": "REGEXP",
        "iregex": "REGEXP",
        "posix_regex": "REGEXP",
        "iposix_regex": "REGEXP",
    }
    # ``__search`` renders MATCH ... AGAINST; the column needs a FULLTEXT
    # index (declare ``Index(fields=[...], using="fulltext")``).
    supports_search = True
    # utf8mb4's default *_ai_ci collation is case-insensitive, so plain LIKE
    # covers icontains/istartswith/iexact; BINARY restores case sensitivity.
    ilike = "LIKE"
    like = "LIKE BINARY"
    # ``ESCAPE '\\'``: MySQL string literals treat backslash as an escape, so
    # the single-backslash escape character is spelled with two backslashes.
    like_escape = " ESCAPE '\\\\'"
    supports_insert_returning = False
    insert_ignore_verb = "INSERT IGNORE"
    random_function = "RAND()"
    supports_for_update = True
    modifying_subquery_needs_wrap = True
    supports_aggregate_filter = False
    # MySQL requires LIMIT before OFFSET; its documented "no limit" sentinel is
    # the maximum row count (LIMIT -1 is a syntax error here).
    offset_requires_limit = True
    no_limit = 18446744073709551615
    # MySQL 8 has no ADD/DROP COLUMN IF [NOT] EXISTS, no CONCURRENTLY, and its
    # index DDL differs enough (no CREATE INDEX IF NOT EXISTS, USING in another
    # position, DROP INDEX needs the table) that the PostgreSQL-only options
    # are dropped and index creation is folded into CREATE TABLE.
    column_if_exists = False
    index_concurrently = False
    index_using = False
    index_include = False
    index_opclass = False
    # EXTRACT(...) works, but the microseconds part is spelled MICROSECOND.
    _extract_parts = {**BaseDialect._extract_parts, "microsecond": "MICROSECOND"}

    type_map = {
        "smallint": "SMALLINT",
        "int": "INT",
        "bigint": "BIGINT",
        "varchar": "VARCHAR({max_length})",
        "text": "LONGTEXT",
        "bool": "TINYINT(1)",
        "float": "DOUBLE",
        "decimal": "DECIMAL({max_digits}, {decimal_places})",
        # DATETIME(6)/TIME(6) keep microseconds (bare DATETIME truncates).
        "datetime": "DATETIME(6)",
        "date": "DATE",
        "time": "TIME(6)",
        "timedelta": "BIGINT",
        "uuid": "CHAR(36)",
        "json": "JSON",
        "bytes": "LONGBLOB",
    }
    serial_map = {
        "smallint": "SMALLINT AUTO_INCREMENT",
        "int": "INT AUTO_INCREMENT",
        "bigint": "BIGINT AUTO_INCREMENT",
    }

    def insert_default_values_sql(self, pk_column: str) -> str:
        """Render MySQL's empty-column-list defaults insert (no ``DEFAULT VALUES``).

        Args:
            pk_column: The primary-key column name (unused).

        Returns:
            ``"() VALUES ()"``.
        """
        return "() VALUES ()"

    def placeholder(self, index: int) -> str:
        """Render MySQL's unnumbered ``?`` placeholder.

        Every compile path appends parameters in SQL text order, so the
        position alone identifies the parameter.

        Args:
            index: The 1-based parameter position (unused).

        Returns:
            ``"?"``.
        """
        return "?"

    def quote(self, identifier: str) -> str:
        """Quote an identifier with backticks, doubling embedded backticks.

        Not memoised in the shared cache: that cache holds the double-quoted
        form the other dialects share.

        Args:
            identifier: The identifier (table or column name) to quote.

        Returns:
            The backtick-quoted, escaped identifier.
        """
        return "`{}`".format(identifier.replace("`", "``"))

    # -- row decoding -------------------------------------------------------
    def read_decoder(self, field: Field) -> Callable[[Any], Any] | None:
        """Reconstruct types MySQL's wire protocol cannot express natively.

        ``CHAR(36)`` uuid columns come back as ``str`` (parsed back to
        ``uuid.UUID``), and ``DATETIME(6)`` is always naive (re-labelled UTC
        under ``use_tz``, matching the aware values other backends return). An
        FK column adopts the referenced primary key's kind.

        Args:
            field: The field whose column is being decoded.

        Returns:
            A one-argument converter, or None to assign the value directly.
        """
        base = super().read_decoder(field)
        kind = field.field_kind
        if isinstance(field, ForeignKeyFieldInstance):
            kind = get_model(field.reference)._meta.pk_field.field_kind
        extra: Callable[[Any], Any] | None = None
        if kind == "uuid":
            extra = _uuid_from_db
        elif kind == "datetime":
            extra = _datetime_from_db
        if extra is None:
            return base
        if base is None:
            return extra
        return lambda value, _extra=extra, _base=base: _base(_extra(value))

    # -- query lookups ------------------------------------------------------
    def date_part_sql(self, part: str, col: str) -> str:
        """Render ``EXTRACT(<part> FROM col)`` (MySQL spells all parts).

        Args:
            part: A supported date/time part name.
            col: The already-qualified column reference.

        Returns:
            A SQL expression yielding the integer part.
        """
        return f"EXTRACT({self._extract_parts[part]} FROM {col})"

    def truncate_date_sql(self, col: str) -> str:
        """Render ``CAST(col AS DATE)`` (for the ``__date`` lookup).

        Args:
            col: The already-qualified column reference.

        Returns:
            A SQL expression yielding the date part.
        """
        return f"CAST({col} AS DATE)"

    def json_extract_sql(self, col: str, keys: list[str]) -> str:
        """Render a JSON key path as unquoted text via ``JSON_EXTRACT``.

        Every path leg is double-quoted: MySQL rejects bare non-ASCII (and
        otherwise special) member names in a JSON path, and quoting is always
        valid. Backslashes are doubled once for the JSON-path escaping and once
        more so they survive MySQL's backslash-escaped string literals.

        Args:
            col: The already-qualified JSON column reference.
            keys: The object keys to traverse (outermost first).

        Returns:
            ``JSON_UNQUOTE(JSON_EXTRACT(col, '$."a"."b"'))`` (the column
            itself with no keys).
        """
        if not keys:
            return col
        legs = ".".join('"' + key.replace("\\", "\\\\").replace('"', '\\"') + '"' for key in keys)
        path = ("$." + legs).replace("\\", "\\\\")
        return f"JSON_UNQUOTE(JSON_EXTRACT({col}, {self._literal(path)}))"

    def json_contains_sql(self, col: str, placeholder: str) -> str:
        """Render a JSON containment test with ``JSON_CONTAINS``.

        The bound value is JSON text; MySQL parses string arguments to JSON
        functions. Semantics track PostgreSQL's ``@>`` for objects and arrays.

        Args:
            col: The already-qualified JSON column reference.
            placeholder: The bound-parameter placeholder for the JSON value.

        Returns:
            A boolean SQL expression testing containment.
        """
        return f"JSON_CONTAINS({col}, {placeholder})"

    def cast_text(self, col: str) -> str:
        """Render a text cast; MySQL spells the target type ``CHAR``.

        Args:
            col: The already-qualified column reference.

        Returns:
            ``CAST(col AS CHAR)``.
        """
        return f"CAST({col} AS CHAR)"

    def concat_sql(self, parts: list[str]) -> str:
        """Render concatenation as ``CONCAT(...)`` (``||`` is logical OR here).

        Args:
            parts: The rendered SQL operand expressions.

        Returns:
            The ``CONCAT(...)`` expression.
        """
        return "CONCAT(" + ", ".join(parts) + ")"

    def regex_sql(self, op: str, col: str, placeholder: str) -> str:
        """Render a regex lookup through ``REGEXP_LIKE`` with a case flag.

        The infix ``REGEXP`` operator follows the column collation (typically
        case-insensitive), and ``REGEXP BINARY`` is rejected by MySQL 8's ICU
        engine — so case sensitivity is spelled with the function's match
        flag instead: ``'c'`` (sensitive) for ``regex``/``posix_regex``,
        ``'i'`` for the ``i``-variants.

        Args:
            op: The lookup name.
            col: The already-qualified column reference.
            placeholder: The bound-parameter placeholder for the pattern.

        Returns:
            A ``REGEXP_LIKE(col, ?, flag)`` expression.
        """
        flag = "i" if op in ("iregex", "iposix_regex") else "c"
        return f"REGEXP_LIKE({col}, {placeholder}, '{flag}')"

    def search_sql(self, col: str, placeholder: str) -> str:
        """Render a MySQL full-text ``__search`` via ``MATCH ... AGAINST``.

        The column must carry a FULLTEXT index — declare
        ``Index(fields=["col"], using="fulltext")`` on the model (rendered
        inline as ``FULLTEXT INDEX`` in the CREATE TABLE); MySQL errors
        otherwise.

        Args:
            col: The already-qualified column reference.
            placeholder: The bound-parameter placeholder for the search query.

        Returns:
            A ``MATCH (col) AGAINST (?)`` boolean expression (natural
            language mode, MySQL's default).
        """
        return f"MATCH ({col}) AGAINST ({placeholder})"

    def on_conflict_sql(self, conflict_columns: list[str], update_columns: list[str]) -> str:
        """Render the MySQL upsert clause for ``bulk_create``.

        With ``update_columns`` this is the 8.4-safe alias form
        ``AS new ON DUPLICATE KEY UPDATE col = new.col`` (the ``VALUES()``
        function is removed in 8.4). MySQL always matches against *any* unique
        key, so an explicit conflict target cannot be honoured and is ignored.
        Without update columns, conflict skipping is spelled on the INSERT verb
        (``INSERT IGNORE`` via :attr:`insert_ignore_verb`), so the clause is
        empty.

        Args:
            conflict_columns: Ignored — MySQL has no conflict-target syntax.
            update_columns: The columns to overwrite on a duplicate key.

        Returns:
            The upsert clause (leading space included), or ``""``.
        """
        if not update_columns:
            return ""
        alias = self.quote("new")
        sets = ", ".join(f"{self.quote(c)} = {alias}.{self.quote(c)}" for c in update_columns)
        return f" AS {alias} ON DUPLICATE KEY UPDATE {sets}"

    # -- DDL ------------------------------------------------------------------
    def column_sql(self, field: Field) -> str:
        """Render a column definition, adding the inline ``COMMENT`` clause.

        MySQL has no ``COMMENT ON COLUMN`` statement; column descriptions ride
        on the column definition instead.

        Args:
            field: The field to render as a column definition.

        Returns:
            The column definition SQL fragment.
        """
        sql = super().column_sql(field)
        if field.description:
            sql += f" COMMENT {self._literal(field.description)}"
        return sql

    def create_table_sql(self, meta: MetaInfo, safe: bool = True) -> list[str]:
        """Render the ``CREATE TABLE`` statement with indexes folded inline.

        MySQL has no ``CREATE INDEX IF NOT EXISTS``, so separate index
        statements would break idempotent ``generate_schemas`` re-runs; inline
        ``INDEX``/``UNIQUE INDEX`` lines ride the table's own ``IF NOT
        EXISTS``. Partial-index conditions (unsupported on MySQL) are dropped,
        like the other PostgreSQL-only index options.

        Args:
            meta: The model metadata describing the table.
            safe: Whether to emit an ``IF NOT EXISTS`` guard.

        Returns:
            The list of SQL statements creating the table.
        """
        lines = [self.column_sql(f) for f in meta.fields.values()]
        pk_line = self._pk_line(meta)
        if pk_line:  # pragma: no branch - always table-level on MySQL
            lines.append(pk_line)
        # Foreign keys MUST be table-level clauses: MySQL silently ignores a
        # column-level inline REFERENCES.
        for field in meta.fields.values():
            if isinstance(field, ForeignKeyFieldInstance) and field.db_constraint:
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
        for field in meta.fields.values():
            if field.index and not field.unique and not field.pk and field.field_kind != "json":
                idx_name = self.quote(f"idx_{meta.table}_{field.db_column}")
                lines.append(f"INDEX {idx_name} ({self.quote(field.db_column)})")
        for index in meta.indexes:
            # MySQL cannot index a JSON column directly (only via generated
            # columns on a path), so JSON indexes — typically PostgreSQL GIN
            # declarations — are dropped like the other pg-only index options.
            if any(self._index_field_kind(meta, name) == "json" for name in index.fields):
                continue
            cols = ", ".join(self._group_columns(meta, index.fields))
            lines.append(
                f"{self._index_prefix(index)}INDEX "
                f"{self.quote(index.resolve_name(meta.table))} ({cols})"
            )

        ine = "IF NOT EXISTS " if safe else ""
        body = ",\n  ".join(lines)
        statements = [f"CREATE TABLE {ine}{self.quote(meta.table)} (\n  {body}\n)"]
        statements.extend(self._comment_sql(meta))
        return statements

    @staticmethod
    def _index_field_kind(meta: MetaInfo, name: str) -> str:
        """Resolve the field kind behind an index member (relations included).

        Args:
            meta: The model metadata.
            name: A field name (or forward relation name) from an index.

        Returns:
            The member's abstract field kind.
        """
        if name in meta.relations:
            return meta.get_field(meta.relations[name].source_attr).field_kind
        return meta.get_field(name).field_kind

    def _comment_sql(self, meta: MetaInfo) -> list[str]:
        """Render the table comment; column comments are inline (``column_sql``).

        Args:
            meta: The model metadata carrying descriptions.

        Returns:
            The ``ALTER TABLE ... COMMENT`` statement, or an empty list.
        """
        if not meta.description:
            return []
        return [f"ALTER TABLE {self.quote(meta.table)} COMMENT = {self._literal(meta.description)}"]

    @staticmethod
    def _index_prefix(index: Index) -> str:
        """The keyword preceding ``INDEX`` for one :class:`Index` declaration.

        ``using="fulltext"`` renders MySQL's ``FULLTEXT INDEX`` (what
        ``MATCH ... AGAINST`` searches require); otherwise ``UNIQUE`` when set.

        Args:
            index: The index being rendered.

        Returns:
            ``"FULLTEXT "``, ``"UNIQUE "`` or ``""``.
        """
        _validate_index_using(index.using)
        if index.using == "fulltext":
            return "FULLTEXT "
        return "UNIQUE " if index.unique else ""

    def render_index(self, meta: MetaInfo, index: Index, safe: bool = True) -> str:
        """Render one ``CREATE INDEX`` statement for :meth:`Index.get_sql`.

        MySQL has no ``CREATE INDEX IF NOT EXISTS``, partial indexes, or the
        PostgreSQL index options, so only name/columns/unique — plus the
        MySQL-specific ``using="fulltext"`` — survive.

        Args:
            meta: The owning model's metadata.
            index: The index to render.
            safe: Ignored — the statement is never guarded on MySQL.

        Returns:
            The ``CREATE INDEX`` statement.
        """
        cols = ", ".join(self._group_columns(meta, index.fields))
        return (
            f"CREATE {self._index_prefix(index)}INDEX "
            f"{self.quote(index.resolve_name(meta.table))} "
            f"ON {self.quote(meta.table)} ({cols})"
        )

    # -- migration rendering overrides ---------------------------------------
    def render_drop_table(self, table: str) -> list[str]:
        """Render a drop-table statement (MySQL has no ``CASCADE``).

        Args:
            table: The table name.

        Returns:
            The list with the drop-table statement.
        """
        return [f"DROP TABLE IF EXISTS {self.quote(table)}"]

    def render_alter_column(
        self,
        table: str,
        name: str,
        old: dict[str, Any],
        new: dict[str, Any],
        table_spec: dict[str, Any],
    ) -> list[str]:
        """Render column changes with MySQL's ``MODIFY COLUMN`` spelling.

        Type and nullability changes restate the full definition in one
        ``MODIFY COLUMN``; defaults use ``ALTER COLUMN SET/DROP DEFAULT``;
        the inline-UNIQUE and FK constraints toggle via ``ADD CONSTRAINT`` /
        ``DROP INDEX`` / ``DROP FOREIGN KEY`` (MySQL has no ``DROP CONSTRAINT
        IF EXISTS``).

        Args:
            table: The table name.
            name: The column being altered.
            old: The column spec before the change.
            new: The column spec after the change.
            table_spec: The full table spec (unused; MySQL alters in place).

        Returns:
            The list of SQL statements applying the column change.
        """
        t = self.quote(table)
        col = self.quote(name)
        out: list[str] = []
        type_changed = old.get("kind") != new.get("kind") or old.get("type_params") != new.get(
            "type_params"
        )
        if type_changed or bool(old.get("null")) != bool(new.get("null")):
            nullable = "NULL" if new.get("null") else "NOT NULL"
            out.append(f"ALTER TABLE {t} MODIFY COLUMN {col} {self._spec_type(new)} {nullable}")
        if old.get("default") != new.get("default"):
            if new.get("default"):
                expr = self._render_default(new["default"])
                out.append(f"ALTER TABLE {t} ALTER COLUMN {col} SET DEFAULT ({expr})")
            else:
                out.append(f"ALTER TABLE {t} ALTER COLUMN {col} DROP DEFAULT")
        if bool(old.get("unique")) != bool(new.get("unique")) and not new.get("pk"):
            uq = self.quote(f"{table}_{name}_key")
            if new.get("unique"):
                out.append(f"ALTER TABLE {t} ADD CONSTRAINT {uq} UNIQUE ({col})")
            else:
                # A UNIQUE constraint is an index in MySQL; DROP INDEX removes it.
                out.append(f"ALTER TABLE {t} DROP INDEX {uq}")
        if old.get("fk") != new.get("fk"):
            fk = self.quote(f"{table}_{name}_fkey")
            if old.get("fk"):
                out.append(f"ALTER TABLE {t} DROP FOREIGN KEY {fk}")
            ref = new.get("fk")
            if ref:
                out.append(
                    f"ALTER TABLE {t} ADD CONSTRAINT {fk} FOREIGN KEY ({col}) "
                    f"REFERENCES {self.quote(ref['table'])} ({self.quote(ref['pk'])}) "
                    f"ON DELETE {ref.get('on_delete', 'CASCADE')}"
                )
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
        """Render a create-index statement (no ``IF NOT EXISTS`` on MySQL).

        Args:
            table: The table name.
            column: The column to index.
            safe: Ignored — MySQL has no ``CREATE INDEX IF NOT EXISTS``, so the
                statement is never guarded (re-running it raises 1061).
            unique: Whether to create a ``UNIQUE`` index.
            concurrently: Ignored (no ``CONCURRENTLY`` on MySQL).
            name: Explicit index name; defaults to ``idx_<table>_<column>``.

        Returns:
            The list with the create-index statement.
        """
        uniq = "UNIQUE " if unique else ""
        return [
            f"CREATE {uniq}INDEX {self.quote(name or f'idx_{table}_{column}')} "
            f"ON {self.quote(table)} ({self.quote(column)})"
        ]

    def render_drop_index(
        self, table: str, column: str, concurrently: bool = False, name: str | None = None
    ) -> list[str]:
        """Render a drop-index statement (MySQL's needs the owning table).

        Args:
            table: The table name.
            column: The indexed column.
            concurrently: Ignored (no ``CONCURRENTLY`` on MySQL).
            name: Explicit index name; defaults to ``idx_<table>_<column>``.

        Returns:
            The list with the drop-index statement.
        """
        idx = self.quote(name or f"idx_{table}_{column}")
        return [f"ALTER TABLE {self.quote(table)} DROP INDEX {idx}"]

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
        """Render a multi-column create-index statement without the guard.

        The PostgreSQL-only options (``condition``/``using``/``include``/
        ``opclass``) are dropped, matching the capability flags.

        Args:
            table: The table to index.
            name: The index name.
            columns: The ordered columns covered by the index.
            safe: Ignored — no ``CREATE INDEX IF NOT EXISTS`` on MySQL.
            condition: Ignored (no partial indexes).
            unique: Whether to render ``CREATE UNIQUE INDEX``.
            using: Ignored.
            include: Ignored.
            opclass: Ignored.

        Returns:
            The list with the create-index statement.
        """
        uniq = "UNIQUE " if unique else ""
        cols = ", ".join(self.quote(c) for c in columns)
        return [f"CREATE {uniq}INDEX {self.quote(name)} ON {self.quote(table)} ({cols})"]

    def render_drop_composite_index(self, name: str, table: str | None = None) -> list[str]:
        """Render a named-index drop; MySQL's form needs the owning table.

        Args:
            name: The index name.
            table: The owning table (the migration ops carry it).

        Raises:
            UnSupportedError: When no table is given — MySQL cannot drop an
                index by bare name.

        Returns:
            The list with the drop-index statement.
        """
        if table is None:
            raise UnSupportedError(
                "mysql cannot DROP INDEX by name alone (the owning table is required); "
                "use RunSQL with ALTER TABLE ... DROP INDEX"
            )
        return [f"ALTER TABLE {self.quote(table)} DROP INDEX {self.quote(name)}"]

    def render_rename_index(
        self,
        table: str,
        column: str,
        old_name: str,
        new_name: str,
        unique: bool = False,
    ) -> list[str]:
        """Render an in-place index rename (``ALTER TABLE ... RENAME INDEX``).

        Args:
            table: The table owning the index.
            column: The indexed column (unused; MySQL renames in place).
            old_name: The current index name.
            new_name: The new index name.
            unique: Unused (the index definition is unchanged).

        Returns:
            The list with the rename-index statement.
        """
        return [
            f"ALTER TABLE {self.quote(table)} "
            f"RENAME INDEX {self.quote(old_name)} TO {self.quote(new_name)}"
        ]

    def render_drop_constraint(self, table: str, name: str) -> list[str]:
        """Render a drop-constraint statement (no ``IF EXISTS`` on MySQL).

        Args:
            table: The table name.
            name: The constraint name to drop.

        Returns:
            The list with the drop-constraint statement.
        """
        return [f"ALTER TABLE {self.quote(table)} DROP CONSTRAINT {self.quote(name)}"]

    def render_rename_constraint(self, table: str, old: str, new: str) -> list[str]:
        """MySQL has no ``RENAME CONSTRAINT``.

        Args:
            table: The table name.
            old: The current constraint name.
            new: The new constraint name.

        Raises:
            UnSupportedError: Always — drop and re-add the constraint instead.
        """
        raise UnSupportedError("mysql cannot RENAME a constraint; drop and re-add it")


class MariaDbDialect(MySQLDialect):
    """MariaDB dialect: MySQL wire/SQL compatible, but with ``RETURNING``.

    MariaDB shares MySQL's protocol, driver and SQL, so this reuses the whole
    :class:`MySQLDialect` behaviour and changes only one thing: MariaDB 10.5+
    supports ``INSERT ... RETURNING``, so the model layer emits it (instead of
    the MySQL last_insert_id follow-up) and reads generated primary keys and
    ``Meta.fetch_db_defaults`` columns straight back from the insert.

    The engine selects this dialect automatically: the MySQL backend probes
    ``SELECT VERSION()`` at connect and reports ``"mariadb"`` for a MariaDB
    server at 10.5 or newer (older MariaDB, which lacks ``RETURNING``, stays on
    the ``"mysql"`` dialect). Both ``mariadb://`` and ``mysql://`` URLs work.
    """

    name = "mariadb"
    supports_insert_returning = True
    #: MariaDB has no ``FOR UPDATE OF <table>``; it locks the whole statement.
    supports_for_update_of = False

    def regex_sql(self, op: str, col: str, placeholder: str) -> str:
        """Render a regex lookup via MariaDB's PCRE ``REGEXP`` operator.

        MariaDB has no ``REGEXP_LIKE`` function (MySQL 8 only); its ``REGEXP``
        follows the operand collation for case (usually case-insensitive), so an
        inline PCRE flag pins the case semantics regardless of collation:
        ``(?-i)`` (case-sensitive) for ``regex``/``posix_regex``, ``(?i)`` for
        the ``i``-variants.

        Args:
            op: The lookup name.
            col: The already-qualified column reference.
            placeholder: The bound-parameter placeholder for the pattern.

        Returns:
            A ``col REGEXP CONCAT('(?flag)', ?)`` expression.
        """
        flag = "(?i)" if op in ("iregex", "iposix_regex") else "(?-i)"
        return f"{col} REGEXP CONCAT('{flag}', {placeholder})"

    def read_decoder(self, field: Field) -> Callable[[Any], Any] | None:
        """Decode MariaDB values, parsing ``JSON`` columns from text.

        MariaDB implements ``JSON`` as an alias for ``LONGTEXT`` (no native JSON
        type flag on the wire, unlike MySQL 8), so the engine returns the raw
        JSON text; parse it here so a ``JSONField`` hydrates to the same Python
        object it does on every other backend. The field's own ``to_python``
        still runs, so any ``decoder`` hook applies to the parsed value. All
        other kinds (uuid/datetime/identity) defer to :class:`MySQLDialect`.

        Args:
            field: The field whose column is being decoded.

        Returns:
            A one-argument converter, or None to assign the value directly.
        """
        if field.field_kind == "json":
            to_python = field.to_python
            return lambda value, _tp=to_python: _tp(
                _json.loads(value) if isinstance(value, (str, bytes, bytearray)) else value
            )
        return super().read_decoder(field)

    def on_conflict_sql(self, conflict_columns: list[str], update_columns: list[str]) -> str:
        """Render the MariaDB upsert clause for ``bulk_create``.

        MariaDB never adopted MySQL 8.0.19's ``... AS new`` row-alias syntax;
        it keeps the classic ``VALUES(col)`` function, so the upsert reads
        ``ON DUPLICATE KEY UPDATE col = VALUES(col)``. Without update columns,
        conflict skipping is spelled on the INSERT verb (``INSERT IGNORE``), so
        the clause is empty.

        Args:
            conflict_columns: Ignored — MySQL/MariaDB have no conflict target.
            update_columns: The columns to overwrite on a duplicate key.

        Returns:
            The upsert clause (leading space included), or ``""``.
        """
        if not update_columns:
            return ""
        sets = ", ".join(f"{self.quote(c)} = VALUES({self.quote(c)})" for c in update_columns)
        return f" ON DUPLICATE KEY UPDATE {sets}"


def _json_from_db(value: Any) -> Any:
    """Parse the JSON text an Oracle CLOB column returns into Python.

    Args:
        value: The raw column value (str from Oracle, already parsed elsewhere).

    Returns:
        The decoded JSON value (None passes through).
    """
    if isinstance(value, str):
        return _json.loads(value)
    return value


def _time_from_db(value: Any) -> Any:
    """Reconstruct a ``time`` from the ``HH:MM:SS.ffffff`` text Oracle stores.

    Args:
        value: The raw column value (str from Oracle, already a time elsewhere).

    Returns:
        The value as a ``datetime.time`` (None passes through).
    """
    if isinstance(value, str):
        return _dt.time.fromisoformat(value)
    return value


def _float_from_db(value: Any) -> Any:
    """Coerce a NUMBER-backed ``FLOAT`` column (a ``Decimal``) to a Python ``float``.

    Args:
        value: The raw column value (a ``Decimal`` from Oracle, or None).

    Returns:
        The value as a ``float`` (None passes through).
    """
    return None if value is None else float(value)


def _int_from_db(value: Any) -> Any:
    """Coerce an integer column to a Python ``int``.

    A row-decoded ``NUMBER`` already arrives as ``int``, but a ``RETURNING``
    OUT bind is declared ``VARCHAR2`` and hands the pk back as text (e.g.
    ``"1"``); integer fields are ``read_identity`` on every other backend, so
    the model layer would otherwise assign that string verbatim. ``int(...)``
    is a no-op on a value that is already an ``int``.

    Args:
        value: The raw column value (an ``int`` or numeric string, or None).

    Returns:
        The value as an ``int`` (None passes through).
    """
    return None if value is None else int(value)


class OracleDialect(BaseDialect):
    """Dialect rendering SQL for Oracle Database (23ai and compatible).

    Key departures from the base dialect: ``:N`` bind placeholders; the
    ``NUMBER``/``VARCHAR2``/``CLOB``/``BLOB`` type family (with ``NUMBER(1)``
    booleans and ``VARCHAR2(36)`` uuids reconstructed on read); IDENTITY
    columns for auto-increment; ``RETURNING ... INTO`` OUT binds instead of the
    PostgreSQL result-set form; the SQL-standard ``OFFSET ... ROWS FETCH NEXT
    ... ROWS ONLY`` in place of ``LIMIT``; ``UPPER()`` folding for the
    case-insensitive pattern lookups (Oracle has no ``ILIKE``); ``REGEXP_LIKE``
    for regex; and ``DBMS_RANDOM.VALUE`` for random ordering.
    """

    name = "oracle"

    # Regex lookups render through REGEXP_LIKE with a case flag (see
    # ``regex_sql``); the keys advertise which lookups resolve.
    regex_ops = {
        "regex": "REGEXP_LIKE",
        "iregex": "REGEXP_LIKE",
        "posix_regex": "REGEXP_LIKE",
        "iposix_regex": "REGEXP_LIKE",
    }
    supports_insert_returning = True
    # Oracle has no INSERT IGNORE / ON CONFLICT; bulk-create conflict handling
    # would need MERGE (not yet implemented — see ``on_conflict_sql``).
    insert_ignore_verb = "INSERT"
    random_function = "DBMS_RANDOM.VALUE"
    supports_for_update = True
    # Oracle rejects modifying a table that also appears in a subquery's FROM
    # ("ORA-01732"-class); wrap the subquery, like MySQL.
    modifying_subquery_needs_wrap = True
    # No aggregate FILTER clause; the compiler falls back to CASE WHEN.
    supports_aggregate_filter = False
    # Oracle has no multi-row ``VALUES (...), (...)`` INSERT; bulk_create inserts
    # one row per statement (the MERGE upsert path is multi-row via UNION ALL).
    supports_multirow_insert = False
    # Oracle enforces the strict GROUP BY rule (no functional-dependency
    # shortcut): every selected non-aggregate column must be grouped.
    group_by_functional_dependency = False
    # ``OFFSET/FETCH`` is rendered directly (see ``limit_offset_sql``), so the
    # base "no limit" machinery is unused.
    offset_requires_limit = False
    # The PostgreSQL-only index options have no Oracle spelling.
    index_concurrently = False
    index_using = False
    index_include = False
    index_opclass = False
    supports_extensions = False
    # Oracle 23ai supports ADD/DROP COLUMN IF [NOT] EXISTS and in-place column,
    # index and constraint changes (MODIFY / ALTER INDEX / RENAME CONSTRAINT).
    column_if_exists = True
    alter_column_in_place = True
    rename_index_in_place = True
    alter_constraint_in_place = True
    # Oracle has no ILIKE operator; the case-insensitive lookups fold both
    # operands with UPPER() in ``like_pattern_sql``. ``ilike``/``like`` remain
    # plain LIKE for the (rare) subquery-valued pattern path.
    ilike = "LIKE"
    like = "LIKE"
    # Oracle string literals do not process backslash escapes, so a single
    # backslash is the escape character (same rendered form as the base).
    like_escape = " ESCAPE '\\'"

    type_map = {
        "smallint": "NUMBER(5)",
        "int": "NUMBER(10)",
        "bigint": "NUMBER(19)",
        "varchar": "VARCHAR2({max_length})",
        "text": "CLOB",
        "bool": "NUMBER(1)",
        # NUMBER-based FLOAT rather than BINARY_DOUBLE: the driver (0.1.x) hands
        # BINARY_DOUBLE/BINARY_FLOAT back as undecoded raw bytes, whereas a
        # NUMBER decodes cleanly (and ``read_decoder`` coerces it to ``float``).
        "float": "FLOAT",
        "decimal": "NUMBER({max_digits}, {decimal_places})",
        "datetime": "TIMESTAMP(6)",
        "date": "DATE",
        # 'HH:MM:SS.ffffff' text (Oracle has no time-of-day type).
        "time": "VARCHAR2(15)",
        "timedelta": "NUMBER(19)",
        "uuid": "VARCHAR2(36)",
        "json": "CLOB",
        "bytes": "BLOB",
    }
    # ``GENERATED BY DEFAULT ON NULL AS IDENTITY``: the database assigns the pk
    # when the INSERT omits it (or binds NULL), while still allowing an explicit
    # value — exactly the ORM's contract.
    serial_map = {
        "smallint": "NUMBER(5) GENERATED BY DEFAULT ON NULL AS IDENTITY",
        "int": "NUMBER(10) GENERATED BY DEFAULT ON NULL AS IDENTITY",
        "bigint": "NUMBER(19) GENERATED BY DEFAULT ON NULL AS IDENTITY",
    }
    # EXTRACT covers the calendar/clock parts; quarter/week/microsecond go
    # through TO_CHAR (see ``date_part_sql``).
    _extract_parts = {
        "year": "YEAR",
        "month": "MONTH",
        "day": "DAY",
        "hour": "HOUR",
        "minute": "MINUTE",
        "second": "SECOND",
    }
    _tochar_parts = {"quarter": "Q", "week": "IW", "microsecond": "FF6"}

    def insert_default_values_sql(self, pk_column: str) -> str:
        """Render Oracle's defaults insert (``(pk) VALUES (DEFAULT)``; no ``DEFAULT VALUES``).

        Args:
            pk_column: The primary-key column name.

        Returns:
            ``'("pk") VALUES (DEFAULT)'``.
        """
        return f"({self.quote(pk_column)}) VALUES (DEFAULT)"

    def placeholder(self, index: int) -> str:
        """Render Oracle's numbered ``:N`` bind placeholder.

        Args:
            index: The 1-based parameter position.

        Returns:
            ``":<index>"``.
        """
        return f":{index}"

    # -- row decoding -------------------------------------------------------
    def read_decoder(self, field: Field) -> Callable[[Any], Any] | None:
        """Reconstruct types Oracle's columns store as text/number.

        ``VARCHAR2(36)`` uuids, ``CLOB`` json and ``VARCHAR2`` times come back
        as strings; ``TIMESTAMP`` is naive (re-labelled UTC under ``use_tz``).
        An FK column adopts the referenced primary key's kind.

        Args:
            field: The field whose column is being decoded.

        Returns:
            A one-argument converter, or None to assign the value directly.
        """
        base = super().read_decoder(field)
        kind = field.field_kind
        if isinstance(field, ForeignKeyFieldInstance):
            kind = get_model(field.reference)._meta.pk_field.field_kind
        extra: Callable[[Any], Any] | None = {
            "uuid": _uuid_from_db,
            "datetime": _datetime_from_db,
            "json": _json_from_db,
            "time": _time_from_db,
            "float": _float_from_db,
            "smallint": _int_from_db,
            "int": _int_from_db,
            "bigint": _int_from_db,
        }.get(kind)
        if extra is None:
            return base
        if base is None:
            return extra
        return lambda value, _extra=extra, _base=base: _base(_extra(value))

    # -- query lookups ------------------------------------------------------
    def like_pattern_sql(self, case_insensitive: bool, col: str, placeholder: str) -> str:
        """Render a pattern lookup, folding both operands with UPPER for case-insensitivity.

        Args:
            case_insensitive: Whether the lookup ignores case.
            col: The already-qualified (already text-cast) column reference.
            placeholder: The bound-parameter placeholder for the pattern.

        Returns:
            A boolean SQL expression matching ``col`` against the pattern.
        """
        if case_insensitive:
            return f"UPPER({col}) LIKE UPPER({placeholder}){self.like_escape}"
        return f"{col} LIKE {placeholder}{self.like_escape}"

    def limit_offset_sql(self, limit: int | None, offset: int | None) -> str:
        """Render Oracle's ``OFFSET m ROWS FETCH NEXT n ROWS ONLY``.

        Args:
            limit: The maximum row count, or None.
            offset: The number of leading rows to skip, or None.

        Returns:
            The clause fragment (leading space included), or ``""``.
        """
        tail = ""
        if offset is not None:
            tail += f" OFFSET {int(offset)} ROWS"
        if limit is not None:
            tail += f" FETCH NEXT {int(limit)} ROWS ONLY"
        return tail

    def insert_returning_clause(self, fields: Sequence[Field]) -> str:
        """Render ``RETURNING cols INTO :ret_N`` OUT binds.

        The backend detects the ``INTO`` binds and runs the insert through the
        driver's OUT-bind path, handing the values back as a synthetic row.

        Args:
            fields: The fields whose columns are returned (pk first).

        Returns:
            The clause fragment with a leading space.
        """
        cols = ", ".join(self.quote(f.db_column) for f in fields)
        outs = ", ".join(f":ret_{i}" for i in range(len(fields)))
        return f" RETURNING {cols} INTO {outs}"

    def date_part_sql(self, part: str, col: str) -> str:
        """Render a date/time part via ``EXTRACT`` (``TO_CHAR`` for quarter/week/microsecond).

        Args:
            part: A supported date/time part name.
            col: The already-qualified column reference.

        Returns:
            A SQL expression yielding the integer part.
        """
        if part in self._extract_parts:
            return f"EXTRACT({self._extract_parts[part]} FROM {col})"
        fmt = self._tochar_parts.get(part)
        if fmt is not None:
            return f"TO_NUMBER(TO_CHAR({col}, '{fmt}'))"
        raise UnSupportedError(f"oracle does not support the __{part} lookup")

    def truncate_date_sql(self, col: str) -> str:
        """Render ``TRUNC(col)`` (drops the time-of-day for the ``__date`` lookup).

        Args:
            col: The already-qualified column reference.

        Returns:
            A SQL expression yielding the date part.
        """
        return f"TRUNC({col})"

    def json_extract_sql(self, col: str, keys: list[str]) -> str:
        """Render a JSON key path as text via ``JSON_VALUE``.

        Args:
            col: The already-qualified JSON (CLOB) column reference.
            keys: The object keys to traverse (outermost first).

        Returns:
            ``JSON_VALUE(col, '$."a"."b"')`` (the column itself with no keys).
        """
        if not keys:
            return col
        legs = "".join('."' + key.replace('"', '\\"') + '"' for key in keys)
        return f"JSON_VALUE({col}, {self._literal('$' + legs)})"

    def cast_text(self, col: str) -> str:
        """Render a text cast; Oracle spells the target ``VARCHAR2(4000)``.

        Args:
            col: The already-qualified column reference.

        Returns:
            ``CAST(col AS VARCHAR2(4000))``.
        """
        return f"CAST({col} AS VARCHAR2(4000))"

    def regex_sql(self, op: str, col: str, placeholder: str) -> str:
        """Render a regex lookup through ``REGEXP_LIKE`` with a case flag.

        Args:
            op: The lookup name.
            col: The already-qualified column reference.
            placeholder: The bound-parameter placeholder for the pattern.

        Returns:
            A ``REGEXP_LIKE(col, ?, flag)`` expression.
        """
        flag = "i" if op in ("iregex", "iposix_regex") else "c"
        return f"REGEXP_LIKE({col}, {placeholder}, '{flag}')"

    def on_conflict_sql(self, conflict_columns: list[str], update_columns: list[str]) -> str:
        """Reject the ``ON CONFLICT`` suffix; Oracle renders a ``MERGE`` via ``render_upsert``.

        Args:
            conflict_columns: The conflict-target columns.
            update_columns: The columns to overwrite on conflict.

        Raises:
            UnSupportedError: Always — the ``MERGE`` path supersedes this hook.
        """
        raise UnSupportedError(  # pragma: no cover - superseded by render_upsert
            "oracle renders conflict handling as MERGE via render_upsert, not an ON CONFLICT suffix"
        )

    def render_upsert(
        self,
        table: str,
        columns: Sequence[str],
        nrows: int,
        conflict_columns: Sequence[str],
        update_columns: Sequence[str],
        pk_columns: Sequence[str],
    ) -> str:
        """Render a conflict-skipping/updating bulk insert as an Oracle ``MERGE``.

        Oracle has no ``INSERT ... ON CONFLICT`` / ``INSERT IGNORE``; a ``MERGE``
        against a ``SELECT ... FROM dual`` row source is the equivalent that also
        avoids raising a duplicate-key error (which the driver surfaces as a
        connection close). ``WHEN NOT MATCHED THEN INSERT`` covers the ignore
        case; ``WHEN MATCHED THEN UPDATE`` adds the upsert. When no conflict
        target is given, the primary key is used (a ``MERGE`` requires one).

        Args:
            table: The already-quoted target table.
            columns: The unquoted column names, in insert (and bind) order.
            nrows: Number of value rows.
            conflict_columns: The unquoted conflict-target columns.
            update_columns: The unquoted columns to overwrite on conflict.
            pk_columns: The unquoted primary-key columns (target fallback).

        Returns:
            The complete ``MERGE`` statement.
        """
        # A MERGE needs a conflict key that is present in the inserted columns
        # (its ON clause references the row source). An auto-increment pk is not
        # inserted, so fall back to any inserted pk columns; if none remain the
        # target is unknown (a bare "ignore any duplicate" cannot be expressed).
        targets = [c for c in (list(conflict_columns) or list(pk_columns)) if c in columns]
        if not targets:  # pragma: no cover - exercised only by oracle-skipped tests
            raise UnSupportedError(
                "oracle bulk upsert needs a conflict target present in the inserted "
                "columns; pass on_conflict=[...] (a bare ignore_conflicts on an "
                "auto-increment table has no MERGE key)"
            )
        alias = [f"c{j}" for j in range(len(columns))]
        pos = {c: j for j, c in enumerate(columns)}
        rows = []
        idx = 1
        for _ in range(nrows):
            sel = ", ".join(f"{self.placeholder(idx + j)} {alias[j]}" for j in range(len(columns)))
            rows.append(f"SELECT {sel} FROM dual")
            idx += len(columns)
        src = " UNION ALL ".join(rows)
        on = " AND ".join(f"d.{self.quote(c)} = s.{alias[pos[c]]}" for c in targets)
        merge = f"MERGE INTO {table} d USING ({src}) s ON ({on}) "
        # Oracle forbids updating a column referenced in the ON clause.
        updates = [c for c in update_columns if c not in targets]
        if updates:
            sets = ", ".join(f"d.{self.quote(c)} = s.{alias[pos[c]]}" for c in updates)
            merge += f"WHEN MATCHED THEN UPDATE SET {sets} "
        insert_cols = ", ".join(self.quote(c) for c in columns)
        insert_vals = ", ".join(f"s.{a}" for a in alias)
        merge += f"WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals})"
        return merge

    # -- DDL ------------------------------------------------------------------
    def column_sql(self, field: Field) -> str:
        """Render a column definition with ``DEFAULT`` before the null/unique constraints.

        Oracle accepts either order, but the driver (0.1.x) desyncs its protocol
        stream on the ``NOT NULL DEFAULT (...)`` ordering when the statement is
        not the first on a connection — closing the connection mid-batch. The
        equivalent ``DEFAULT (...) NOT NULL`` ordering parses cleanly, so it is
        emitted here.

        Args:
            field: The field to render as a column definition.

        Returns:
            The column definition SQL fragment.
        """
        parts = [self.quote(field.db_column), self.column_type(field)]
        if isinstance(field.default, DatabaseDefault):
            parts.append(f"DEFAULT ({field.default.to_sql(self)})")
        if field.pk:
            pass
        elif field.null:
            parts.append("NULL")
        else:
            parts.append("NOT NULL")
        if field.unique and not field.pk:
            parts.append("UNIQUE")
        return " ".join(parts)

    def _fk_clause(self, col: str, ref_tbl: str, ref_pk: str, on_delete: str) -> str:
        """Render an FK clause, dropping an ``ON DELETE RESTRICT``/``NO ACTION`` Oracle rejects.

        Args:
            col: The already-quoted referencing column.
            ref_tbl: The already-quoted referenced table.
            ref_pk: The already-quoted referenced primary key column.
            on_delete: The ``ON DELETE`` action.

        Returns:
            The ``FOREIGN KEY`` clause.
        """
        tail = "" if on_delete in ("RESTRICT", "NO ACTION") else f" ON DELETE {on_delete}"
        return f"FOREIGN KEY ({col}) REFERENCES {ref_tbl} ({ref_pk}){tail}"

    # -- migration rendering overrides ---------------------------------------
    def render_drop_table(self, table: str) -> list[str]:
        """Render a drop-table statement (``CASCADE CONSTRAINTS`` to sever FKs).

        Args:
            table: The table name.

        Returns:
            The list with the drop-table statement.
        """
        return [f"DROP TABLE IF EXISTS {self.quote(table)} CASCADE CONSTRAINTS"]

    def render_alter_column(
        self,
        table: str,
        name: str,
        old: dict[str, Any],
        new: dict[str, Any],
        table_spec: dict[str, Any],
    ) -> list[str]:
        """Render column changes with Oracle's ``MODIFY`` spelling.

        Args:
            table: The table name.
            name: The column being altered.
            old: The column spec before the change.
            new: The column spec after the change.
            table_spec: The full table spec (unused; Oracle alters in place).

        Returns:
            The list of SQL statements applying the column change.
        """
        t = self.quote(table)
        col = self.quote(name)
        out: list[str] = []
        type_changed = old.get("kind") != new.get("kind") or old.get("type_params") != new.get(
            "type_params"
        )
        null_changed = bool(old.get("null")) != bool(new.get("null"))
        if type_changed or null_changed:
            parts = [self._spec_type(new)] if type_changed else []
            if null_changed:
                parts.append("NULL" if new.get("null") else "NOT NULL")
            out.append(f"ALTER TABLE {t} MODIFY ({col} {' '.join(parts)})")
        if old.get("default") != new.get("default"):
            if new.get("default"):
                out.append(
                    f"ALTER TABLE {t} MODIFY ({col} DEFAULT {self._render_default(new['default'])})"
                )
            else:
                out.append(f"ALTER TABLE {t} MODIFY ({col} DEFAULT NULL)")
        if bool(old.get("unique")) != bool(new.get("unique")) and not new.get("pk"):
            uq = self.quote(f"{table}_{name}_key")
            if new.get("unique"):
                out.append(f"ALTER TABLE {t} ADD CONSTRAINT {uq} UNIQUE ({col})")
            else:
                out.append(f"ALTER TABLE {t} DROP CONSTRAINT {uq}")
        if old.get("fk") != new.get("fk"):
            fk = self.quote(f"{table}_{name}_fkey")
            if old.get("fk"):
                out.append(f"ALTER TABLE {t} DROP CONSTRAINT {fk}")
            ref = new.get("fk")
            if ref:
                clause = self._fk_clause(
                    col,
                    self.quote(ref["table"]),
                    self.quote(ref["pk"]),
                    ref.get("on_delete", "CASCADE"),
                )
                out.append(f"ALTER TABLE {t} ADD CONSTRAINT {fk} {clause}")
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
        """Render a create-index statement.

        Args:
            table: The table name.
            column: The column to index.
            safe: Whether to emit an ``IF NOT EXISTS`` guard.
            unique: Whether to create a ``UNIQUE`` index.
            concurrently: Ignored (no ``CONCURRENTLY`` on Oracle).
            name: Explicit index name; defaults to ``idx_<table>_<column>``.

        Returns:
            The list with the create-index statement.
        """
        uniq = "UNIQUE " if unique else ""
        ine = "IF NOT EXISTS " if safe else ""
        return [
            f"CREATE {uniq}INDEX {ine}{self.quote(name or f'idx_{table}_{column}')} "
            f"ON {self.quote(table)} ({self.quote(column)})"
        ]

    def render_drop_index(
        self, table: str, column: str, concurrently: bool = False, name: str | None = None
    ) -> list[str]:
        """Render a drop-index statement (Oracle drops by bare index name).

        Args:
            table: The table owning the index (unused; not needed by Oracle).
            column: The indexed column.
            concurrently: Ignored (no ``CONCURRENTLY`` on Oracle).
            name: Explicit index name; defaults to ``idx_<table>_<column>``.

        Returns:
            The list with the drop-index statement.
        """
        idx = self.quote(name or f"idx_{table}_{column}")
        return [f"DROP INDEX IF EXISTS {idx}"]

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
        """Render a multi-column create-index statement.

        The PostgreSQL-only options (``condition``/``using``/``include``/
        ``opclass``) are dropped, matching the capability flags.

        Args:
            table: The table to index.
            name: The index name.
            columns: The ordered columns covered by the index.
            safe: Whether to emit an ``IF NOT EXISTS`` guard.
            condition: Ignored (no partial indexes).
            unique: Whether to render ``CREATE UNIQUE INDEX``.
            using: Ignored.
            include: Ignored.
            opclass: Ignored.

        Returns:
            The list with the create-index statement.
        """
        uniq = "UNIQUE " if unique else ""
        ine = "IF NOT EXISTS " if safe else ""
        cols = ", ".join(self.quote(c) for c in columns)
        return [f"CREATE {uniq}INDEX {ine}{self.quote(name)} ON {self.quote(table)} ({cols})"]

    def render_drop_composite_index(self, name: str, table: str | None = None) -> list[str]:
        """Render a named-index drop (Oracle drops by bare name).

        Args:
            name: The index name.
            table: The owning table (unused).

        Returns:
            The list with the drop-index statement.
        """
        return [f"DROP INDEX IF EXISTS {self.quote(name)}"]

    def render_rename_index(
        self,
        table: str,
        column: str,
        old_name: str,
        new_name: str,
        unique: bool = False,
    ) -> list[str]:
        """Render an in-place index rename (``ALTER INDEX ... RENAME TO``).

        Args:
            table: The table owning the index (unused).
            column: The indexed column (unused).
            old_name: The current index name.
            new_name: The new index name.
            unique: Unused (the index definition is unchanged).

        Returns:
            The list with the rename-index statement.
        """
        return [f"ALTER INDEX {self.quote(old_name)} RENAME TO {self.quote(new_name)}"]

    def render_drop_constraint(self, table: str, name: str) -> list[str]:
        """Render a drop-constraint statement.

        Args:
            table: The table name.
            name: The constraint name to drop.

        Returns:
            The list with the drop-constraint statement.
        """
        return [f"ALTER TABLE {self.quote(table)} DROP CONSTRAINT {self.quote(name)}"]

    def render_rename_constraint(self, table: str, old: str, new: str) -> list[str]:
        """Render a constraint rename (``ALTER TABLE ... RENAME CONSTRAINT``).

        Args:
            table: The table name.
            old: The current constraint name.
            new: The new constraint name.

        Returns:
            The list with the rename-constraint statement.
        """
        return [
            f"ALTER TABLE {self.quote(table)} "
            f"RENAME CONSTRAINT {self.quote(old)} TO {self.quote(new)}"
        ]


class SqlServerDialect(BaseDialect):
    """Dialect rendering T-SQL for Microsoft SQL Server (2017+ / Azure SQL).

    Key departures from the base dialect: ``@PN`` bind placeholders and
    ``[bracket]`` identifier quoting; the ``NVARCHAR``/``BIGINT``/``DATETIME2``/
    ``UNIQUEIDENTIFIER``/``BIT`` type family (``BIT`` booleans, native ``GUID``
    uuids); ``IDENTITY(1,1)`` auto-increment whose generated value is read back
    via ``SCOPE_IDENTITY()`` — the backend batches it, since T-SQL has no
    ``RETURNING`` and ``OUTPUT`` cannot be a statement suffix the model appends;
    ``OFFSET ... FETCH NEXT`` paging (which requires an ``ORDER BY``); ``MERGE``
    for bulk upserts; ``CONCAT`` string concatenation; and case-insensitive
    ``LIKE`` by default (a binary ``COLLATE`` folds the case-sensitive lookups).
    SQL Server has no ``REGEXP`` operator, so regex lookups raise
    ``UnSupportedError``.
    """

    name = "mssql"
    # T-SQL has no `RETURNING`; the backend reads a generated IDENTITY back with
    # a batched `SELECT SCOPE_IDENTITY()`, and `Meta.fetch_db_defaults` columns
    # come back via the follow-up SELECT (the same path MySQL uses).
    supports_insert_returning = False
    insert_ignore_verb = "INSERT"  # no INSERT IGNORE; conflicts go through MERGE
    insert_default_values = "DEFAULT VALUES"
    random_function = "NEWID()"
    # SQL Server has no `SELECT ... FOR UPDATE` suffix (it uses table lock hints),
    # so the lock clause is dropped, as on SQLite.
    supports_for_update = False
    modifying_subquery_needs_wrap = True
    supports_aggregate_filter = False
    supports_multirow_insert = True  # multi-row VALUES (up to 1000 rows)
    group_by_functional_dependency = False
    offset_requires_limit = False
    index_concurrently = False
    index_using = False
    index_include = False
    index_opclass = False
    supports_extensions = False
    column_if_exists = False
    # Default collations are case-insensitive; `like_pattern_sql` adds a binary
    # COLLATE for the case-sensitive lookups. Plain LIKE for the subquery path.
    ilike = "LIKE"
    like = "LIKE"
    like_escape = " ESCAPE '\\'"
    #: SQL Server has no regular-expression operator (regex lookups raise).
    regex_ops: dict[str, str] = {}

    type_map = {
        "smallint": "SMALLINT",
        "int": "INT",
        "bigint": "BIGINT",
        "varchar": "NVARCHAR({max_length})",
        "text": "NVARCHAR(MAX)",
        "bool": "BIT",
        "float": "FLOAT(53)",
        "decimal": "DECIMAL({max_digits}, {decimal_places})",
        "datetime": "DATETIME2(6)",
        "date": "DATE",
        "time": "TIME(6)",
        "timedelta": "BIGINT",
        "uuid": "UNIQUEIDENTIFIER",
        "json": "NVARCHAR(MAX)",
        "bytes": "VARBINARY(MAX)",
    }
    #: ``IDENTITY(1,1)``: the database assigns the pk when the INSERT omits it.
    serial_map = {
        "smallint": "SMALLINT IDENTITY(1,1)",
        "int": "INT IDENTITY(1,1)",
        "bigint": "BIGINT IDENTITY(1,1)",
    }
    _datepart = {
        "year": "year",
        "month": "month",
        "day": "day",
        "hour": "hour",
        "minute": "minute",
        "second": "second",
        "quarter": "quarter",
        "week": "iso_week",
        "microsecond": "microsecond",
    }

    def placeholder(self, index: int) -> str:
        """Render T-SQL's ``@PN`` bind placeholder.

        Args:
            index: The 1-based parameter position.

        Returns:
            ``"@P<index>"``.
        """
        return f"@P{index}"

    def quote(self, identifier: str) -> str:
        """Quote an identifier with square brackets (``]`` doubled).

        Args:
            identifier: The identifier to quote.

        Returns:
            The bracket-quoted identifier.
        """
        return f"[{identifier.replace(']', ']]')}]"

    def concat_sql(self, parts: list[str]) -> str:
        """Concatenate operands with T-SQL's ``CONCAT`` (``||`` is not string concat).

        Args:
            parts: The rendered SQL operand expressions.

        Returns:
            The concatenation SQL expression.
        """
        return "CONCAT(" + ", ".join(parts) + ")"

    def cast_text(self, col: str) -> str:
        """Render a text cast (``CAST(col AS NVARCHAR(4000))``).

        Args:
            col: The already-qualified column reference.

        Returns:
            The cast expression.
        """
        return f"CAST({col} AS NVARCHAR(4000))"

    # -- row decoding -------------------------------------------------------
    def read_decoder(self, field: Field) -> Callable[[Any], Any] | None:
        """Reconstruct types SQL Server returns as text/naive.

        ``NVARCHAR(MAX)`` json comes back as a string; ``DATETIME2`` is naive
        (re-labelled UTC under ``use_tz``). ``UNIQUEIDENTIFIER`` and ``BIT`` are
        already native (``uuid.UUID`` / ``bool``). An FK column adopts the
        referenced primary key's kind.

        Args:
            field: The field whose column is being decoded.

        Returns:
            A one-argument converter, or None to assign the value directly.
        """
        base = super().read_decoder(field)
        kind = field.field_kind
        if isinstance(field, ForeignKeyFieldInstance):
            kind = get_model(field.reference)._meta.pk_field.field_kind
        extra: Callable[[Any], Any] | None = {
            "datetime": _datetime_from_db,
            "json": _json_from_db,
        }.get(kind)
        if extra is None:
            return base
        if base is None:
            return extra
        return lambda value, _extra=extra, _base=base: _base(_extra(value))

    # -- query lookups ------------------------------------------------------
    def like_pattern_sql(self, case_insensitive: bool, col: str, placeholder: str) -> str:
        """Render a pattern lookup; a binary COLLATE makes it case-sensitive.

        SQL Server's default collations are case-insensitive, so ``ILIKE``-style
        matching is plain ``LIKE``; the case-sensitive lookups force a binary
        collation on the comparison.

        Args:
            case_insensitive: Whether the lookup ignores case.
            col: The already-qualified (already text-cast) column reference.
            placeholder: The bound-parameter placeholder for the pattern.

        Returns:
            A boolean SQL expression matching ``col`` against the pattern.
        """
        if case_insensitive:
            return f"{col} LIKE {placeholder}{self.like_escape}"
        return f"{col} LIKE {placeholder} COLLATE Latin1_General_BIN2{self.like_escape}"

    def limit_offset_sql(self, limit: int | None, offset: int | None) -> str:
        """Render ``OFFSET m ROWS FETCH NEXT n ROWS ONLY``.

        SQL Server requires an ``ORDER BY`` for ``OFFSET/FETCH``; the queryset
        supplies one whenever it paginates.

        Args:
            limit: The maximum row count, or None.
            offset: The number of leading rows to skip, or None.

        Returns:
            The clause fragment (leading space included), or ``""``.
        """
        if limit is None and offset is None:
            return ""
        tail = f" OFFSET {int(offset) if offset is not None else 0} ROWS"
        if limit is not None:
            tail += f" FETCH NEXT {int(limit)} ROWS ONLY"
        return tail

    def date_part_sql(self, part: str, col: str) -> str:
        """Render a date/time part via ``DATEPART``.

        Args:
            part: A supported date/time part name.
            col: The already-qualified column reference.

        Returns:
            A SQL expression yielding the integer part.
        """
        spelled = self._datepart.get(part)
        if spelled is None:
            raise UnSupportedError(f"mssql does not support the __{part} lookup")
        return f"DATEPART({spelled}, {col})"

    def truncate_date_sql(self, col: str) -> str:
        """Render ``CAST(col AS DATE)`` (for the ``__date`` lookup).

        Args:
            col: The already-qualified column reference.

        Returns:
            A SQL expression yielding the date part.
        """
        return f"CAST({col} AS DATE)"

    def json_extract_sql(self, col: str, keys: list[str]) -> str:
        """Render a JSON key path as text via ``JSON_VALUE`` (SQL Server 2016+).

        Args:
            col: The already-qualified JSON (NVARCHAR) column reference.
            keys: The object keys to traverse (outermost first).

        Returns:
            ``JSON_VALUE(col, '$."a"."b"')`` (the column itself with no keys).
        """
        if not keys:
            return col
        legs = "".join('."' + key.replace('"', '\\"') + '"' for key in keys)
        return f"JSON_VALUE({col}, {self._literal('$' + legs)})"

    def regex_sql(self, op: str, col: str, placeholder: str) -> str:
        """SQL Server has no regular-expression operator.

        Args:
            op: The lookup name.
            col: The already-qualified column reference.
            placeholder: The bound-parameter placeholder.

        Raises:
            UnSupportedError: Always.
        """
        raise UnSupportedError("mssql has no regular-expression operator")

    # -- bulk upsert (MERGE) -------------------------------------------------
    def on_conflict_sql(self, conflict_columns: list[str], update_columns: list[str]) -> str:
        """Reject the ``ON CONFLICT`` suffix; SQL Server renders a ``MERGE``.

        Args:
            conflict_columns: The conflict-target columns.
            update_columns: The columns to overwrite on conflict.

        Raises:
            UnSupportedError: Always — the ``MERGE`` path supersedes this hook.
        """
        raise UnSupportedError(  # pragma: no cover - superseded by render_upsert
            "mssql renders conflict handling as MERGE via render_upsert, not an ON CONFLICT suffix"
        )

    def render_upsert(
        self,
        table: str,
        columns: Sequence[str],
        nrows: int,
        conflict_columns: Sequence[str],
        update_columns: Sequence[str],
        pk_columns: Sequence[str],
    ) -> str:
        """Render a conflict-skipping/updating bulk insert as a T-SQL ``MERGE``.

        SQL Server has no ``INSERT ... ON CONFLICT`` / ``INSERT IGNORE``; a
        ``MERGE`` against a ``VALUES`` row source is the equivalent.
        ``WHEN NOT MATCHED THEN INSERT`` covers the ignore case;
        ``WHEN MATCHED THEN UPDATE`` adds the upsert.

        Args:
            table: The already-quoted target table.
            columns: The unquoted column names, in insert (and bind) order.
            nrows: Number of value rows.
            conflict_columns: The unquoted conflict-target columns.
            update_columns: The unquoted columns to overwrite on conflict.
            pk_columns: The unquoted primary-key columns (target fallback).

        Returns:
            The complete ``MERGE`` statement (terminated with ``;``).
        """
        targets = [c for c in (list(conflict_columns) or list(pk_columns)) if c in columns]
        if not targets:  # pragma: no cover - exercised only by mssql-skipped tests
            raise UnSupportedError(
                "mssql bulk upsert needs a conflict target present in the inserted "
                "columns; pass on_conflict=[...]"
            )
        ncols = len(columns)
        idx = 1
        value_rows = []
        for _ in range(nrows):
            holes = ", ".join(self.placeholder(idx + j) for j in range(ncols))
            value_rows.append(f"({holes})")
            idx += ncols
        src_cols = ", ".join(self.quote(c) for c in columns)
        on = " AND ".join(f"d.{self.quote(c)} = s.{self.quote(c)}" for c in targets)
        merge = (
            f"MERGE INTO {table} AS d USING (VALUES {', '.join(value_rows)}) "
            f"AS s ({src_cols}) ON ({on}) "
        )
        updates = [c for c in update_columns if c not in targets]
        if updates:
            sets = ", ".join(f"d.{self.quote(c)} = s.{self.quote(c)}" for c in updates)
            merge += f"WHEN MATCHED THEN UPDATE SET {sets} "
        insert_cols = ", ".join(self.quote(c) for c in columns)
        insert_vals = ", ".join(f"s.{self.quote(c)}" for c in columns)
        merge += f"WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals});"
        return merge

    # -- migration rendering -------------------------------------------------
    def render_drop_table(self, table: str) -> list[str]:
        """Render a drop-table statement (``DROP TABLE IF EXISTS``, 2016+).

        Args:
            table: The table name.

        Returns:
            The list with the drop-table statement.
        """
        return [f"DROP TABLE IF EXISTS {self.quote(table)}"]


_DIALECTS: dict[str, type[BaseDialect]] = {
    "postgres": PostgresDialect,
    "sqlite": SqliteDialect,
    "mysql": MySQLDialect,
    "mariadb": MariaDbDialect,
    "oracle": OracleDialect,
    "mssql": SqlServerDialect,
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

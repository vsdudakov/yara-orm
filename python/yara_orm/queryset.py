"""Lazy, chainable query construction.

A :class:`QuerySet` records filters (incl. ``Q`` trees), ordering, limits,
annotations and prefetches, touching the database only when awaited or when a
terminal coroutine (``get``, ``count`` ...) runs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import registry
from .connection import get_dialect, get_executor
from .exceptions import DoesNotExist, FieldError, MultipleObjectsReturned
from .expressions import Expression
from .functions import Function

if TYPE_CHECKING:
    from collections.abc import Generator

    from .aggregations import Aggregate
    from .dialects import BaseDialect
    from .fields import Field
    from .models import Model

# op -> (sql operator, pattern builder or None). A pattern builder turns the
# value into a LIKE/ILIKE pattern (bound as plain text); None binds via to_db.
_OPERATORS = {
    "exact": ("=", None),
    "not": ("!=", None),
    "gt": (">", None),
    "gte": (">=", None),
    "lt": ("<", None),
    "lte": ("<=", None),
    "contains": ("LIKE", lambda v: f"%{v}%"),
    "icontains": ("ILIKE", lambda v: f"%{v}%"),
    "startswith": ("LIKE", lambda v: f"{v}%"),
    "istartswith": ("ILIKE", lambda v: f"{v}%"),
    "endswith": ("LIKE", lambda v: f"%{v}"),
    "iendswith": ("ILIKE", lambda v: f"%{v}"),
}


def _split_lookup(key: str) -> tuple[str, str]:
    """Split a filter key into its field path and lookup operator.

    Args:
        key: A filter key such as ``"age__gte"`` or ``"name"``.

    Returns:
        A ``(field_path, operator)`` tuple; the operator defaults to
        ``"exact"`` when the key carries no recognized lookup suffix.
    """
    if "__" in key:
        head, _, tail = key.rpartition("__")
        if tail in _OPERATORS or tail in ("in", "isnull"):
            return head, tail
    return key, "exact"


class Q:
    """A tree of filter conditions combinable with ``&``, ``|`` and ``~``."""

    def __init__(
        self,
        *children: Q,
        _connector: str = "AND",
        _negated: bool = False,
        **filters: Any,
    ) -> None:
        """Initialize a filter node from child nodes and keyword lookups.

        Args:
            *children: Nested ``Q`` nodes combined under this node's connector.
            _connector: Boolean connector joining the children, ``"AND"`` or
                ``"OR"``.
            _negated: Whether the resulting condition is logically negated.
            **filters: Field lookups applied directly at this node.

        Returns:
            None
        """
        self.children = list(children)
        self.filters = filters
        self.connector = _connector
        self.negated = _negated

    def __and__(self, other: Q) -> Q:
        """Combine this node with another using a boolean ``AND``.

        Args:
            other: The right-hand ``Q`` node to combine with.

        Returns:
            A new ``Q`` node joining both operands with ``AND``.
        """
        return Q(self, other, _connector="AND")

    def __or__(self, other: Q) -> Q:
        """Combine this node with another using a boolean ``OR``.

        Args:
            other: The right-hand ``Q`` node to combine with.

        Returns:
            A new ``Q`` node joining both operands with ``OR``.
        """
        return Q(self, other, _connector="OR")

    def __invert__(self) -> Q:
        """Return a negated copy of this node.

        Returns:
            A new ``Q`` node with the same children, filters and connector but
            inverted negation flag.
        """
        return Q(
            *self.children,
            _connector=self.connector,
            _negated=not self.negated,
            **self.filters,
        )


def _is_model(value: Any) -> bool:
    """Report whether a value looks like a model instance.

    Args:
        value: Any object to inspect.

    Returns:
        ``True`` if the value exposes both ``_meta`` and ``pk`` attributes.
    """
    return hasattr(value, "_meta") and hasattr(value, "pk")


class QuerySet:
    """Lazy, chainable builder that compiles and executes SQL queries."""

    def __init__(self, model: type[Model]) -> None:
        """Initialize an empty query set bound to a model.

        Args:
            model: The model class whose table this query set targets.

        Returns:
            None
        """
        self.model = model
        self._conditions: list[Q] = []  # AND-combined at the top level
        self._having: list[tuple[str, str, Any]] = []  # (annotation, op, value)
        self._order: list[tuple[str, bool]] = []
        self._limit: int | None = None
        self._offset: int | None = None
        self._annotations: dict = {}
        self._group_by: list[str] = []
        self._prefetch: list = []
        self._distinct: bool = False
        self._for_update: bool = False

    # -- cloning / chaining ----------------------------------------------
    def _clone(self) -> QuerySet:
        """Create a shallow copy of this query set's mutable state.

        Returns:
            A new ``QuerySet`` with independent copies of the conditions,
            ordering, annotations and other chainable state.
        """
        qs = QuerySet(self.model)
        qs._conditions = list(self._conditions)
        qs._having = list(self._having)
        qs._order = list(self._order)
        qs._limit = self._limit
        qs._offset = self._offset
        qs._annotations = dict(self._annotations)
        qs._group_by = list(self._group_by)
        qs._prefetch = list(self._prefetch)
        qs._distinct = self._distinct
        qs._for_update = self._for_update
        return qs

    def filter(self, *args: Q, **kwargs: Any) -> QuerySet:
        """Return a new query set narrowed by the given conditions.

        Args:
            *args: ``Q`` nodes ANDed into the query's conditions.
            **kwargs: Field lookups; lookups over annotations become ``HAVING``
                clauses while the rest become ``WHERE`` conditions.

        Returns:
            A cloned ``QuerySet`` with the added conditions.
        """
        qs = self._clone()
        qs._conditions.extend(a for a in args if isinstance(a, Q))
        where_kw = {}
        for key, value in kwargs.items():
            base, op = _split_lookup(key)
            if base in qs._annotations:
                qs._having.append((base, op, value))
            else:
                where_kw[key] = value
        if where_kw:
            qs._conditions.append(Q(**where_kw))
        return qs

    def exclude(self, *args: Q, **kwargs: Any) -> QuerySet:
        """Return a new query set excluding rows matching the conditions.

        Args:
            *args: ``Q`` nodes whose combined condition is negated.
            **kwargs: Field lookups whose combined condition is negated.

        Returns:
            A cloned ``QuerySet`` with the negated condition appended.
        """
        qs = self._clone()
        qs._conditions.append(~Q(*args, **kwargs))
        return qs

    def annotate(self, **annotations: Any) -> QuerySet:
        """Return a new query set with extra aggregate annotations.

        Args:
            **annotations: Mapping of output name to aggregate expression.

        Returns:
            A cloned ``QuerySet`` carrying the additional annotations.
        """
        qs = self._clone()
        qs._annotations.update(annotations)
        return qs

    def group_by(self, *fields: str) -> QuerySet:
        """Return a new query set grouped by the given fields.

        Args:
            *fields: Field names to group the result rows by.

        Returns:
            A cloned ``QuerySet`` with the grouping fields appended.
        """
        qs = self._clone()
        qs._group_by.extend(fields)
        return qs

    def prefetch_related(self, *specs: Any) -> QuerySet:
        """Return a new query set that prefetches the given relations.

        Args:
            *specs: Prefetch specifications describing relations to load.

        Returns:
            A cloned ``QuerySet`` with the prefetch specs appended.
        """
        qs = self._clone()
        qs._prefetch.extend(specs)
        return qs

    def order_by(self, *fields: str) -> QuerySet:
        """Return a new query set ordered by the given fields.

        Args:
            *fields: Field names, optionally prefixed with ``-`` for
                descending order; annotation names are also accepted.

        Returns:
            A cloned ``QuerySet`` with the ordering applied.
        """
        qs = self._clone()
        for spec in fields:
            descending = spec.startswith("-")
            name = spec[1:] if descending else spec
            if name not in self._annotations:
                self.model._meta.get_field(name)
            qs._order.append((name, descending))
        return qs

    def limit(self, value: int) -> QuerySet:
        """Return a new query set with a row limit applied.

        Args:
            value: Maximum number of rows to return.

        Returns:
            A cloned ``QuerySet`` with the limit set.
        """
        qs = self._clone()
        qs._limit = int(value)
        return qs

    def offset(self, value: int) -> QuerySet:
        """Return a new query set with a row offset applied.

        Args:
            value: Number of leading rows to skip.

        Returns:
            A cloned ``QuerySet`` with the offset set.
        """
        qs = self._clone()
        qs._offset = int(value)
        return qs

    def distinct(self) -> QuerySet:
        """Return a new query set that selects only distinct rows.

        Returns:
            A cloned ``QuerySet`` rendering ``SELECT DISTINCT``.
        """
        qs = self._clone()
        qs._distinct = True
        return qs

    def select_for_update(self) -> QuerySet:
        """Return a new query set that locks matched rows (``FOR UPDATE``).

        The lock is emitted on backends that support it (PostgreSQL) and is a
        no-op on SQLite; it only takes effect inside a transaction.

        Returns:
            A cloned ``QuerySet`` that locks the selected rows.
        """
        qs = self._clone()
        qs._for_update = True
        return qs

    def __getitem__(self, item: slice | int) -> QuerySet | Any:
        """Apply offset/limit via ``qs[start:stop]`` or fetch ``qs[i]``.

        Args:
            item: A slice (returns a narrowed query set) or an integer index
                (returns an awaitable resolving to that single row).

        Returns:
            A cloned ``QuerySet`` for a slice, or an awaitable for an index.
        """
        if isinstance(item, slice):
            if item.step not in (None, 1):
                raise ValueError("QuerySet slicing does not support a step")
            if (item.start is not None and item.start < 0) or (
                item.stop is not None and item.stop < 0
            ):
                raise ValueError("negative indexing is not supported")
            qs = self._clone()
            start = item.start or 0
            qs._offset = start or None
            qs._limit = (item.stop - start) if item.stop is not None else None
            return qs
        if isinstance(item, int):
            if item < 0:
                raise ValueError("negative indexing is not supported")

            async def _get_index() -> Model:
                qs = self._clone()
                qs._offset, qs._limit = item, 1
                rows = await qs._fetch()
                if not rows:
                    raise IndexError("QuerySet index out of range")
                return rows[0]

            return _get_index()
        raise TypeError("QuerySet indices must be integers or slices")

    # -- WHERE (Q tree) compilation --------------------------------------
    def _qualified(self, dialect: BaseDialect, field: Field) -> str:
        """Build a table-qualified, quoted column reference.

        Args:
            dialect: The SQL dialect providing identifier quoting.
            field: The field whose column should be referenced.

        Returns:
            A ``"table"."column"`` style qualified column reference.
        """
        return f"{dialect.quote(self.model._meta.table)}.{dialect.quote(field.db_column)}"

    def _compile_lookup(
        self,
        key: str,
        value: Any,
        dialect: BaseDialect,
        idx: int,
    ) -> tuple[str, list[Any], int]:
        """Compile a single field lookup into a SQL condition.

        Args:
            key: The filter key, e.g. ``"age__gte"``.
            value: The value to compare against.
            dialect: The SQL dialect providing quoting and placeholders.
            idx: The next available bind-parameter index.

        Returns:
            A ``(sql, params, next_index)`` tuple holding the condition SQL,
            its bound parameters and the updated parameter index.
        """
        # Deferred: breaks the queryset <-> relations import cycle.
        from .relations import M2MDescriptor

        meta = self.model._meta
        base, op = _split_lookup(key)

        descriptor = getattr(self.model, base, None)
        if base in meta.m2m or isinstance(descriptor, M2MDescriptor):
            return self._compile_m2m_lookup(base, op, value, dialect, idx)

        if base in meta.relations:
            field = meta.get_field(meta.relations[base].source_attr)
            if _is_model(value):
                value = value.pk
        else:
            field = meta.get_field(base)
        col = self._qualified(dialect, field)

        if op == "isnull":
            return (f"{col} IS NULL" if value else f"{col} IS NOT NULL"), [], idx
        if op == "in":
            if not value:
                return "1 = 0", [], idx
            holes, params = [], []
            for item in value:
                holes.append(dialect.placeholder(idx))
                params.append(field.to_db(item.pk if _is_model(item) else item))
                idx += 1
            return f"{col} IN ({', '.join(holes)})", params, idx

        sql_op, pattern = _OPERATORS[op]
        if sql_op == "ILIKE":
            # ILIKE is PostgreSQL-only; SQLite's LIKE is already case-insensitive.
            sql_op = dialect.ilike
        if isinstance(value, Expression):
            # Compare the column against another column expression (e.g. F).
            expr_params: list[Any] = []
            expr_sql, idx = value.resolve(
                lambda n: self._qualified(dialect, meta.get_field(n)), dialect, expr_params, idx
            )
            return f"{col} {sql_op} {expr_sql}", expr_params, idx
        placeholder = dialect.placeholder(idx)
        idx += 1
        bound = pattern(value) if pattern is not None else field.to_db(value)
        return f"{col} {sql_op} {placeholder}", [bound], idx

    def _compile_m2m_lookup(
        self,
        base: str,
        op: str,
        value: Any,
        dialect: BaseDialect,
        idx: int,
    ) -> tuple[str, list[Any], int]:
        """Compile a many-to-many relation lookup into a SQL subquery.

        Args:
            base: The many-to-many relation name being filtered.
            op: The lookup operator, e.g. ``"in"`` or ``"not"``.
            value: A target instance/pk or an iterable of them.
            dialect: The SQL dialect providing quoting and placeholders.
            idx: The next available bind-parameter index.

        Returns:
            A ``(sql, params, next_index)`` tuple holding the membership
            condition SQL, its bound parameters and the updated index.
        """
        descriptor = getattr(self.model, base)
        info = descriptor.info
        info.finalize()
        if descriptor.reverse:
            near, far = info.forward_key, info.backward_key
        else:
            near, far = info.backward_key, info.forward_key
        q = dialect.quote
        meta = self.model._meta
        table = q(meta.table)
        pk = q(meta.pk_field.db_column)
        through = q(info.through)
        if op == "in":
            vals = [v.pk if _is_model(v) else v for v in value]
            holes, params = [], []
            for v in vals:
                holes.append(dialect.placeholder(idx))
                params.append(v)
                idx += 1
            inner = (
                f"SELECT {through}.{q(near)} FROM {through} "
                f"WHERE {through}.{q(far)} IN ({', '.join(holes)})"
            )
            return f"{table}.{pk} IN ({inner})", params, idx
        membership = "NOT IN" if op == "not" else "IN"
        target = value.pk if _is_model(value) else value
        placeholder = dialect.placeholder(idx)
        idx += 1
        inner = (
            f"SELECT {through}.{q(near)} FROM {through} WHERE {through}.{q(far)} = {placeholder}"
        )
        return f"{table}.{pk} {membership} ({inner})", [target], idx

    def _compile_q(
        self,
        node: Q,
        dialect: BaseDialect,
        idx: int,
    ) -> tuple[str, list[Any], int]:
        """Recursively compile a ``Q`` node into a SQL boolean expression.

        Args:
            node: The ``Q`` node to compile.
            dialect: The SQL dialect providing quoting and placeholders.
            idx: The next available bind-parameter index.

        Returns:
            A ``(sql, params, next_index)`` tuple holding the combined
            expression SQL, its bound parameters and the updated index.
        """
        parts, params = [], []
        for key, value in node.filters.items():
            clause, p, idx = self._compile_lookup(key, value, dialect, idx)
            parts.append(clause)
            params.extend(p)
        for child in node.children:
            sub, p, idx = self._compile_q(child, dialect, idx)
            if sub:
                parts.append(f"({sub})")
                params.extend(p)
        if not parts:
            return "", [], idx
        joined = f" {node.connector} ".join(parts)
        if node.negated:
            joined = f"NOT ({joined})"
        return joined, params, idx

    def _compile_conditions(
        self,
        dialect: BaseDialect,
        start: int = 1,
    ) -> tuple[str, list[Any], int]:
        """Compile the top-level conditions into a ``WHERE`` clause.

        Args:
            dialect: The SQL dialect providing quoting and placeholders.
            start: The first bind-parameter index to use.

        Returns:
            A ``(sql, params, next_index)`` tuple holding the ``WHERE`` clause
            (empty when there are no conditions), its bound parameters and the
            updated index.
        """
        parts, params, idx = [], [], start
        for node in self._conditions:
            sub, p, idx = self._compile_q(node, dialect, idx)
            if sub:
                parts.append(sub)
                params.extend(p)
        where = (" WHERE " + " AND ".join(parts)) if parts else ""
        return where, params, idx

    def _order_sql(self, dialect: BaseDialect) -> str:
        """Build the ``ORDER BY`` clause for the configured ordering.

        Args:
            dialect: The SQL dialect providing identifier quoting.

        Returns:
            The ``ORDER BY`` clause, or an empty string when no ordering is set.
            An explicit ``order_by`` takes precedence over the model's
            ``Meta.ordering`` default.
        """
        order = self._order or self.model._meta.ordering
        if not order:
            return ""
        parts = []
        for name, descending in order:
            if name in self._annotations:
                ref = dialect.quote(name)
            else:
                ref = dialect.quote(self.model._meta.get_field(name).db_column)
            parts.append(ref + (" DESC" if descending else " ASC"))
        return " ORDER BY " + ", ".join(parts)

    def _tail_sql(self) -> str:
        """Build the trailing ``LIMIT`` / ``OFFSET`` clause.

        Returns:
            The ``LIMIT``/``OFFSET`` fragment, or an empty string when neither
            is set.
        """
        tail = ""
        if self._limit is not None:
            tail += f" LIMIT {int(self._limit)}"
        if self._offset is not None:
            tail += f" OFFSET {int(self._offset)}"
        return tail

    def _distinct_prefix(self, prefix: str) -> str:
        """Inject ``DISTINCT`` into a ``SELECT`` prefix when requested.

        Args:
            prefix: A ``SELECT ... FROM ...`` prefix string.

        Returns:
            The prefix with ``DISTINCT`` applied, or unchanged when not set.
        """
        return prefix.replace("SELECT ", "SELECT DISTINCT ", 1) if self._distinct else prefix

    def _lock_sql(self, dialect: BaseDialect) -> str:
        """Build the row-locking clause for ``select_for_update``.

        Args:
            dialect: The active SQL dialect.

        Returns:
            ``" FOR UPDATE"`` on backends that support it, else an empty string.
        """
        return " FOR UPDATE" if self._for_update and dialect.name == "postgres" else ""

    # -- aggregation helpers ---------------------------------------------
    def _add_relation_join(
        self,
        rel: str,
        dialect: BaseDialect,
        joins: dict[str, str],
    ) -> Any:
        """Register the join(s) needed to aggregate across a relation.

        Args:
            rel: The relation name to join through.
            dialect: The SQL dialect providing identifier quoting.
            joins: Mapping of join key to join SQL, mutated in place.

        Returns:
            The ``_meta`` object of the joined target model.
        """
        # Deferred: breaks the queryset <-> relations import cycle.
        from .relations import M2MDescriptor, ReverseFKDescriptor, model_name

        q = dialect.quote
        meta = self.model._meta
        table = q(meta.table)
        pk = q(meta.pk_field.db_column)

        if rel in meta.relations:
            info = meta.relations[rel]
            tmeta = info.resolve_target()._meta
            joins[rel] = (
                f" LEFT JOIN {q(tmeta.table)} ON {table}."
                f"{q(meta.get_field(info.source_attr).db_column)} = "
                f"{q(tmeta.table)}.{q(tmeta.pk_field.db_column)}"
            )
            return tmeta

        descriptor = getattr(self.model, rel, None)
        if isinstance(descriptor, ReverseFKDescriptor):
            source = registry.get_model(model_name(descriptor.source_reference))
            smeta = source._meta
            joins[rel] = (
                f" LEFT JOIN {q(smeta.table)} ON {q(smeta.table)}."
                f"{q(descriptor.source_attr)} = {table}.{pk}"
            )
            return smeta
        if isinstance(descriptor, M2MDescriptor):
            info = descriptor.info
            info.finalize()
            if descriptor.reverse:
                near, far = info.forward_key, info.backward_key
                tmeta = info.owner._meta
            else:
                near, far = info.backward_key, info.forward_key
                tmeta = info.resolve_target()._meta
            joins[rel + "#t"] = (
                f" LEFT JOIN {q(info.through)} ON {q(info.through)}.{q(near)} = {table}.{pk}"
            )
            joins[rel] = (
                f" LEFT JOIN {q(tmeta.table)} ON {q(tmeta.table)}."
                f"{q(tmeta.pk_field.db_column)} = {q(info.through)}.{q(far)}"
            )
            return tmeta
        raise FieldError(f"Cannot aggregate over unknown relation {rel!r}")

    def _aggregate_expr(
        self,
        agg: Aggregate | Function,
        dialect: BaseDialect,
        joins: dict[str, str],
    ) -> str:
        """Compile an aggregate into a SQL function expression.

        Args:
            agg: The aggregate describing the function and target field.
            dialect: The SQL dialect providing identifier quoting.
            joins: Mapping of join key to join SQL, mutated in place when the
                aggregate spans a relation.

        Returns:
            The SQL aggregate expression, e.g. ``COUNT(DISTINCT "t"."id")``.
        """

        def resolve(name: str) -> str:
            return self._resolve_column(name, dialect, joins)

        if isinstance(agg, Function):
            return agg.render(resolve)
        distinct = "DISTINCT " if getattr(agg, "distinct", False) else ""
        return f"{agg.function}({distinct}{resolve(agg.field)})"

    def _resolve_column(self, field: str, dialect: BaseDialect, joins: dict[str, str]) -> str:
        """Resolve a field name to its qualified column, adding joins as needed.

        Args:
            field: A local column, ``pk``, ``rel__col`` path, or relation name.
            dialect: The SQL dialect providing identifier quoting.
            joins: Mapping of join key to join SQL, mutated in place when the
                field spans a relation.

        Returns:
            The qualified ``"table"."column"`` reference.
        """
        q = dialect.quote
        meta = self.model._meta
        table = q(meta.table)
        if "__" in field:
            rel, col = field.split("__", 1)
            tmeta = self._add_relation_join(rel, dialect, joins)
            return f"{q(tmeta.table)}.{q(tmeta.get_field(col).db_column)}"
        if field in meta.fields or field == "pk":
            return f"{table}.{q(meta.get_field(field).db_column)}"
        tmeta = self._add_relation_join(field, dialect, joins)
        return f"{q(tmeta.table)}.{q(tmeta.pk_field.db_column)}"

    def _compile_having(
        self,
        dialect: BaseDialect,
        idx: int,
        joins: dict[str, str],
    ) -> tuple[str, list[Any], int]:
        """Compile annotation filters into a ``HAVING`` clause.

        Args:
            dialect: The SQL dialect providing quoting and placeholders.
            idx: The next available bind-parameter index.
            joins: Mapping of join key to join SQL, mutated in place when an
                annotation spans a relation.

        Returns:
            A ``(sql, params, next_index)`` tuple holding the ``HAVING`` clause
            (empty when there are no annotation filters), its bound parameters
            and the updated index.
        """
        clauses, params = [], []
        for name, op, value in self._having:
            expr = self._aggregate_expr(self._annotations[name], dialect, joins)
            sql_op, _ = _OPERATORS[op]
            clauses.append(f"{expr} {sql_op} {dialect.placeholder(idx)}")
            params.append(value)
            idx += 1
        having = (" HAVING " + " AND ".join(clauses)) if clauses else ""
        return having, params, idx

    # -- execution --------------------------------------------------------
    async def _fetch(self) -> list[Model]:
        """Execute the query and build model instances from the rows.

        Returns:
            A list of model instances, with any requested relations prefetched.
        """
        if self._annotations:
            return await self._fetch_annotated()
        dialect = get_dialect(self.model)
        engine = get_executor(self.model)
        meta = self.model._meta
        meta.compile(dialect)
        where, params, _ = self._compile_conditions(dialect)
        prefix = self._distinct_prefix(meta.select_prefix)
        sql = (
            f"{prefix}{where}{self._order_sql(dialect)}{self._tail_sql()}{self._lock_sql(dialect)}"
        )
        rows = await engine.fetch_rows(sql, params)
        build = self.model._from_db_row
        instances = [build(row) for row in rows]
        if self._prefetch:
            # Deferred: breaks the queryset <-> prefetch import cycle.
            from .prefetch import prefetch_instances

            await prefetch_instances(instances, self._prefetch)
        return instances

    async def _fetch_annotated(self) -> list[Model]:
        """Execute an annotated query and attach annotations to instances.

        Returns:
            A list of model instances with each annotation value set as an
            attribute, with any requested relations prefetched.
        """
        dialect = get_dialect(self.model)
        engine = get_executor(self.model)
        meta = self.model._meta
        meta.compile(dialect)
        q = dialect.quote
        table = q(meta.table)
        joins: dict = {}
        select = [f"{table}.{q(f.db_column)}" for f in meta.field_list]
        annotation_names = list(self._annotations.keys())
        for name in annotation_names:
            expr = self._aggregate_expr(self._annotations[name], dialect, joins)
            select.append(f"{expr} AS {q(name)}")
        where, params, idx = self._compile_conditions(dialect)
        having, hparams, idx = self._compile_having(dialect, idx, joins)
        params.extend(hparams)
        group = f" GROUP BY {table}.{q(meta.pk_field.db_column)}"
        sql = (
            f"SELECT {', '.join(select)} FROM {table}"
            f"{''.join(joins.values())}{where}{group}{having}"
            f"{self._order_sql(dialect)}{self._tail_sql()}"
        )
        rows = await engine.fetch_rows(sql, params)
        ncols = len(meta.field_list)
        instances = []
        for row in rows:
            obj = self.model._from_db_row(row[:ncols])
            for offset, name in enumerate(annotation_names):
                setattr(obj, name, row[ncols + offset])
            instances.append(obj)
        if self._prefetch:
            # Deferred: breaks the queryset <-> prefetch import cycle.
            from .prefetch import prefetch_instances

            await prefetch_instances(instances, self._prefetch)
        return instances

    def __await__(self) -> Generator[Any, None, list[Model]]:
        """Make the query set awaitable, executing it on ``await``.

        Returns:
            A generator yielding the awaited list of model instances.
        """
        return self._fetch().__await__()

    async def _fetch_columns(self, field_names: tuple[str, ...]) -> list[Any]:
        """Fetch raw rows for the given columns without building models.

        Args:
            field_names: The field names whose columns to select.

        Returns:
            The raw database rows for the selected columns.
        """
        dialect = get_dialect(self.model)
        engine = get_executor(self.model)
        meta = self.model._meta
        meta.compile(dialect)
        fields = [meta.get_field(n) for n in field_names]
        cols = ", ".join(dialect.quote(f.db_column) for f in fields)
        where, params, _ = self._compile_conditions(dialect)
        table = dialect.quote(meta.table)
        distinct = "DISTINCT " if self._distinct else ""
        sql = (
            f"SELECT {distinct}{cols} FROM {table}{where}"
            f"{self._order_sql(dialect)}{self._tail_sql()}"
        )
        return await engine.fetch_rows(sql, params)

    async def _values_grouped(
        self,
        fields: tuple[str, ...],
        as_dict: bool,
    ) -> list[Any]:
        """Execute a grouped/annotated query and return plain rows.

        Args:
            fields: Requested field/annotation names, or empty for all.
            as_dict: When ``True`` return dict rows, otherwise tuple rows.

        Returns:
            A list of dict rows when ``as_dict`` is ``True``, otherwise a list
            of tuple rows.
        """
        dialect = get_dialect(self.model)
        engine = get_executor(self.model)
        meta = self.model._meta
        meta.compile(dialect)
        q = dialect.quote
        table = q(meta.table)
        joins: dict = {}
        select, names, group_cols = [], [], []
        requested = list(fields) if fields else None

        for f in self._group_by:
            col = f"{table}.{q(meta.get_field(f).db_column)}"
            select.append(col)
            names.append(f)
            group_cols.append(col)
        if requested:
            for f in requested:
                if f in self._annotations or f in names:
                    continue
                col = f"{table}.{q(meta.get_field(f).db_column)}"
                select.append(col)
                names.append(f)
                group_cols.append(col)
        for name, agg in self._annotations.items():
            if requested and name not in requested:
                continue
            select.append(f"{self._aggregate_expr(agg, dialect, joins)} AS {q(name)}")
            names.append(name)

        where, params, idx = self._compile_conditions(dialect)
        having, hparams, idx = self._compile_having(dialect, idx, joins)
        params.extend(hparams)
        group = (" GROUP BY " + ", ".join(group_cols)) if group_cols else ""
        sql = (
            f"SELECT {', '.join(select)} FROM {table}"
            f"{''.join(joins.values())}{where}{group}{having}"
            f"{self._order_sql(dialect)}{self._tail_sql()}"
        )
        rows = await engine.fetch_rows(sql, params)
        if as_dict:
            return [dict(zip(names, row)) for row in rows]
        return [tuple(row) for row in rows]

    async def values_list(
        self,
        *fields: str,
        flat: bool = False,
    ) -> list[tuple[Any, ...]] | list[Any]:
        """Return rows as tuples (or scalars when ``flat=True``); no model build.

        Args:
            *fields: Field names to select; defaults to all model fields.
            flat: When ``True`` return scalar values for a single field.

        Returns:
            A list of tuples, or a list of scalars when ``flat`` is ``True``.
        """
        if self._annotations or self._group_by:
            return await self._values_grouped(fields, as_dict=False)
        names = fields or tuple(self.model._meta.fields.keys())
        if flat:
            if len(names) != 1:
                raise FieldError("flat=True requires exactly one field")
            rows = await self._fetch_columns(names)
            return [r[0] for r in rows]
        rows = await self._fetch_columns(names)
        return [tuple(r) for r in rows]

    async def values(self, *fields: str) -> list[dict[str, Any]]:
        """Return rows as dicts of the requested columns; no model build.

        Args:
            *fields: Field names to select; defaults to all model fields.

        Returns:
            A list of dicts mapping each requested field name to its value.
        """
        if self._annotations or self._group_by:
            return await self._values_grouped(fields, as_dict=True)
        names = fields or tuple(self.model._meta.fields.keys())
        rows = await self._fetch_columns(names)
        return [dict(zip(names, r)) for r in rows]

    async def get(self, **kwargs: Any) -> Model:
        """Fetch the single object matching the given lookups.

        Args:
            **kwargs: Optional field lookups further narrowing the query.

        Returns:
            The single matching model instance.
        """
        qs = self.filter(**kwargs).limit(2) if kwargs else self.limit(2)
        rows = await qs._fetch()
        if not rows:
            raise DoesNotExist(f"{self.model.__name__} matching query does not exist")
        if len(rows) > 1:
            raise MultipleObjectsReturned(f"Multiple {self.model.__name__} objects returned")
        return rows[0]

    async def first(self) -> Model | None:
        """Fetch the first matching object, if any.

        Returns:
            The first matching model instance, or ``None`` when there are no
            matches.
        """
        rows = await self.limit(1)._fetch()
        return rows[0] if rows else None

    async def last(self) -> Model | None:
        """Fetch the last matching object under the current ordering.

        With no explicit ``order_by`` the ordering defaults to descending
        primary key; otherwise the configured ordering is reversed.

        Returns:
            The last matching model instance, or ``None`` when there are none.
        """
        qs = self._clone()
        if qs._order:
            qs._order = [(name, not desc) for name, desc in qs._order]
        else:
            qs._order = [(self.model._meta.pk_field.model_field_name, True)]
        rows = await qs.limit(1)._fetch()
        return rows[0] if rows else None

    async def earliest(self, *fields: str) -> Model | None:
        """Fetch the first object ordered ascending by ``fields``.

        Args:
            *fields: Field names to order by; defaults to the primary key.

        Returns:
            The earliest matching instance, or ``None`` when there are none.
        """
        order = fields or (self.model._meta.pk_field.model_field_name,)
        return await self.order_by(*order).first()

    async def latest(self, *fields: str) -> Model | None:
        """Fetch the first object ordered descending by ``fields``.

        Args:
            *fields: Field names to order by descending; defaults to the
                primary key.

        Returns:
            The latest matching instance, or ``None`` when there are none.
        """
        order = fields or (self.model._meta.pk_field.model_field_name,)
        flipped = [name[1:] if name.startswith("-") else f"-{name}" for name in order]
        return await self.order_by(*flipped).first()

    async def count(self) -> int:
        """Count the rows matching the current conditions.

        Returns:
            The number of matching rows.
        """
        dialect = get_dialect(self.model)
        engine = get_executor(self.model)
        where, params, _ = self._compile_conditions(dialect)
        table = dialect.quote(self.model._meta.table)
        row = await engine.fetch_row(f"SELECT COUNT(*) FROM {table}{where}", params)
        return int(row[0]) if row else 0

    async def exists(self) -> bool:
        """Report whether any row matches the current conditions.

        Returns:
            ``True`` if at least one matching row exists.
        """
        dialect = get_dialect(self.model)
        engine = get_executor(self.model)
        where, params, _ = self._compile_conditions(dialect)
        table = dialect.quote(self.model._meta.table)
        rows = await engine.fetch_rows(f"SELECT 1 FROM {table}{where} LIMIT 1", params)
        return bool(rows)

    async def delete(self) -> int:
        """Delete all rows matching the current conditions.

        Returns:
            The number of rows deleted.
        """
        dialect = get_dialect(self.model)
        engine = get_executor(self.model, write=True)
        where, params, _ = self._compile_conditions(dialect)
        table = dialect.quote(self.model._meta.table)
        return await engine.execute(f"DELETE FROM {table}{where}", params)

    async def update(self, **kwargs: Any) -> int:
        """Update matching rows with the given field values.

        Args:
            **kwargs: Field names mapped to their new values; relation names
                accept either a model instance or its primary key.

        Returns:
            The number of rows updated.
        """
        dialect = get_dialect(self.model)
        engine = get_executor(self.model, write=True)
        meta = self.model._meta
        assignments: list[str] = []
        params: list = []
        idx = 1
        for name, value in kwargs.items():
            if name in meta.relations:
                field = meta.get_field(meta.relations[name].source_attr)
                if _is_model(value):
                    value = value.pk
            else:
                field = meta.get_field(name)
            if isinstance(value, Expression):
                # Assign a column expression, e.g. ``update(qty=F("qty") + 1)``.
                expr_sql, idx = value.resolve(
                    lambda n: dialect.quote(meta.get_field(n).db_column), dialect, params, idx
                )
                assignments.append(f"{dialect.quote(field.db_column)} = {expr_sql}")
            else:
                assignments.append(f"{dialect.quote(field.db_column)} = {dialect.placeholder(idx)}")
                params.append(field.to_db(value))
                idx += 1
        where, where_params, _ = self._compile_conditions(dialect, start=idx)
        params.extend(where_params)
        table = dialect.quote(meta.table)
        sql = f"UPDATE {table} SET {', '.join(assignments)}{where}"
        return await engine.execute(sql, params)

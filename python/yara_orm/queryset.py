"""Lazy, chainable query construction.

A :class:`QuerySet` records filters (incl. ``Q`` trees), ordering, limits,
annotations and prefetches, touching the database only when awaited or when a
terminal coroutine (``get``, ``count`` ...) runs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Generic, TypeVar

from . import registry
from .connection import get_dialect, get_executor
from .exceptions import FieldError, UnSupportedError
from .expressions import Expression
from .functions import Function

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Generator

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
    # Case-insensitive exact match: ILIKE with no wildcards in the bound value.
    "iexact": ("ILIKE", lambda v: f"{v}"),
}

# Date/time part lookups, e.g. ``created_at__year=2024`` (rendered per dialect).
_DATE_PARTS = frozenset(
    {"year", "quarter", "month", "week", "day", "hour", "minute", "second", "microsecond"}
)
# Regex lookups, e.g. ``name__regex=r"^A"`` (rendered per dialect operator). The
# ``posix_regex``/``iposix_regex`` are accepted spellings; they are aliases.
_REGEX_OPS = frozenset({"regex", "iregex", "posix_regex", "iposix_regex"})
# Lookups handled by dedicated branches rather than the ``_OPERATORS`` table.
_SPECIAL_OPS = frozenset({"in", "not_in", "isnull", "not_isnull", "range", "search", "date"})
# Every recognized trailing lookup suffix.
_LOOKUPS = frozenset(_OPERATORS) | _DATE_PARTS | _REGEX_OPS | _SPECIAL_OPS


def _split_lookup(key: str) -> tuple[str, str]:
    """Split a filter key into its field path and lookup operator.

    Args:
        key: A filter key such as ``"age__gte"`` or ``"author__name__icontains"``.

    Returns:
        A ``(field_path, operator)`` tuple; the operator defaults to
        ``"exact"`` when the key carries no recognized lookup suffix. The field
        path may itself span relations (``"author__name"``).
    """
    if "__" in key:
        head, _, tail = key.rpartition("__")
        if tail in _LOOKUPS:
            return head, tail
    return key, "exact"


class Q:
    """A tree of filter conditions combinable with ``&``, ``|`` and ``~``."""

    #: Connector constants; ``self.connector`` holds one of these.
    AND = "AND"
    OR = "OR"

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
        self._select_related: list[str] = []
        self._distinct: bool = False
        self._for_update: bool = False
        self._for_update_nowait: bool = False
        self._for_update_skip_locked: bool = False
        self._for_update_of: tuple[str, ...] = ()
        self._only: tuple[str, ...] | None = None
        self._defer: frozenset[str] = frozenset()
        self._using: str | Any | None = None

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
        qs._select_related = list(self._select_related)
        qs._distinct = self._distinct
        qs._for_update = self._for_update
        qs._for_update_nowait = self._for_update_nowait
        qs._for_update_skip_locked = self._for_update_skip_locked
        qs._for_update_of = self._for_update_of
        qs._only = self._only
        qs._defer = self._defer
        qs._using = self._using
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

    def all(self) -> QuerySet:
        """Return a clone of this query set (a no-op chain terminator).

        Code often ends a chain with ``.all()`` (e.g.
        ``qs.filter(...).all()``); yara query sets are already awaitable, so this
        just returns a clone to keep those chains working unchanged.

        Returns:
            A cloned ``QuerySet`` equivalent to this one.
        """
        return self._clone()

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

    def select_related(self, *relations: str) -> QuerySet:
        """Return a new query set that eager-loads forward FK/O2O relations.

        Each named relation is joined and hydrated in the same query, so the
        related instance is available synchronously (``obj.rel.field``) without
        a follow-up query. Only forward foreign keys and one-to-one relations
        are supported; use ``prefetch_related`` for reverse and m2m relations.

        Args:
            *relations: Forward relation names to join and load.

        Returns:
            A cloned ``QuerySet`` with the relations selected.
        """
        qs = self._clone()
        qs._select_related.extend(relations)
        return qs

    def order_by(self, *fields: str) -> QuerySet:
        """Return a new query set ordered by the given fields.

        Args:
            *fields: Field names, optionally prefixed with ``-`` for
                descending order; annotation names are also accepted. The
                special token ``"?"`` orders rows randomly (``RANDOM()``).

        Returns:
            A cloned ``QuerySet`` with the ordering applied.
        """
        qs = self._clone()
        for spec in fields:
            if spec == "?":
                # Random ordering; no column to validate or qualify.
                qs._order.append(("?", False))
                continue
            descending = spec.startswith("-")
            name = spec[1:] if descending else spec
            if name not in self._annotations and "__" not in name:
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

    def select_for_update(
        self,
        nowait: bool = False,
        skip_locked: bool = False,
        of: tuple[str, ...] = (),
    ) -> QuerySet:
        """Return a new query set that locks matched rows (``FOR UPDATE``).

        The lock is emitted on backends that support it (PostgreSQL) and is a
        no-op on SQLite; it only takes effect inside a transaction.

        Args:
            nowait: Emit ``NOWAIT`` so a contended lock errors instead of waiting.
            skip_locked: Emit ``SKIP LOCKED`` to skip already-locked rows
                (ignored when ``nowait`` is set).
            of: Table/relation names to lock (``FOR UPDATE OF ...``).

        Returns:
            A cloned ``QuerySet`` that locks the selected rows.
        """
        qs = self._clone()
        qs._for_update = True
        qs._for_update_nowait = nowait
        qs._for_update_skip_locked = skip_locked
        qs._for_update_of = tuple(of)
        return qs

    def using_db(self, connection_name: str | Any) -> QuerySet:
        """Return a new query set that executes on a given connection.

        Args:
            connection_name: Either the registered connection name to run
                statements on, or a connection/executor object (a transaction
                wrapper or engine proxy) to run them on directly. An active
                transaction still takes precedence.

        Returns:
            A cloned ``QuerySet`` bound to the connection.
        """
        qs = self._clone()
        qs._using = connection_name
        return qs

    def _using_name(self) -> str | None:
        """Return the connection name used for dialect resolution.

        A ``using_db`` connection passed as an object has no name to look up, so
        the dialect falls back to the active transaction or the default
        connection (the object itself is still used as the executor).

        Returns:
            The connection name string, or None when none/an object is bound.
        """
        return self._using if isinstance(self._using, str) else None

    def only(self, *fields: str) -> QuerySet:
        """Return a new query set selecting only the named columns.

        Instances come back partially populated; the primary key is always
        included. Reading a field that was not selected raises ``FieldError``.

        Args:
            *fields: Field names to load.

        Returns:
            A cloned ``QuerySet`` restricted to the named columns.
        """
        meta = self.model._meta
        for name in fields:
            meta.get_field(name)
        pk_name = meta.pk_field.model_field_name
        names = tuple(dict.fromkeys((pk_name, *fields)))  # pk first, de-duplicated
        qs = self._clone()
        qs._only = names
        qs._defer = frozenset()
        return qs

    def defer(self, *fields: str) -> QuerySet:
        """Return a new query set that omits the named columns.

        Instances come back without the deferred fields loaded; the primary key
        is never deferred. Reading a deferred field raises ``FieldError``.

        Args:
            *fields: Field names to omit from the SELECT.

        Returns:
            A cloned ``QuerySet`` omitting the named columns.
        """
        meta = self.model._meta
        for name in fields:
            meta.get_field(name)
        pk_name = meta.pk_field.model_field_name
        qs = self._clone()
        qs._defer = frozenset(f for f in fields if f != pk_name)
        qs._only = None
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

        # A multi-segment path (``author__name``) traverses one or more
        # relations; compile it as a correlated membership subquery.
        if "__" in base:
            return self._compile_relation_lookup(base, op, value, dialect, idx)

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
        return self._compile_field_op(col, field, op, value, dialect, idx)

    def _compile_field_op(
        self,
        col: str,
        field: Field | None,
        op: str,
        value: Any,
        dialect: BaseDialect,
        idx: int,
    ) -> tuple[str, list[Any], int]:
        """Compile a single ``column <op> value`` condition.

        Args:
            col: The already-qualified column reference (or a rendered
                expression, e.g. an aggregate for a ``HAVING`` comparison).
            field: The field backing the column (for value coercion), or None
                when ``col`` is an expression with no backing field.
            op: The lookup operator (e.g. ``gte``, ``in``, ``year``, ``regex``).
            value: The comparison value.
            dialect: The SQL dialect providing quoting and placeholders.
            idx: The next available bind-parameter index.

        Returns:
            A ``(sql, params, next_index)`` tuple.
        """
        if op == "isnull":
            return (f"{col} IS NULL" if value else f"{col} IS NOT NULL"), [], idx
        if op == "not_isnull":
            return (f"{col} IS NOT NULL" if value else f"{col} IS NULL"), [], idx
        if hasattr(value, "as_sql"):
            # A Subquery / RawSQL / Case used as the comparison value.
            vparams: list[Any] = []
            vsql, idx = value.as_sql(self, dialect, {}, vparams, idx)
            if op in ("in", "not_in"):
                membership = "NOT IN" if op == "not_in" else "IN"
                return f"{col} {membership} {vsql}", vparams, idx
            sql_op = dialect.ilike if _OPERATORS[op][0] == "ILIKE" else _OPERATORS[op][0]
            return f"{col} {sql_op} {vsql}", vparams, idx

        # When ``col`` is a bare expression (e.g. a HAVING aggregate) there is no
        # backing field to coerce through, so bind the value as-is.
        def coerce(v: Any) -> Any:
            return field.to_db(v) if field is not None else v

        if op in ("in", "not_in"):
            membership = "NOT IN" if op == "not_in" else "IN"
            if not value:
                # ``x IN ()`` is always false; ``x NOT IN ()`` always true.
                return ("1 = 1" if op == "not_in" else "1 = 0"), [], idx
            holes, params = [], []
            for item in value:
                holes.append(dialect.placeholder(idx))
                params.append(coerce(item.pk if _is_model(item) else item))
                idx += 1
            return f"{col} {membership} ({', '.join(holes)})", params, idx
        if op == "range":
            lo, hi = value
            p1, p2 = dialect.placeholder(idx), dialect.placeholder(idx + 1)
            return f"{col} BETWEEN {p1} AND {p2}", [coerce(lo), coerce(hi)], idx + 2
        if op in _DATE_PARTS:
            return (
                f"{dialect.date_part_sql(op, col)} = {dialect.placeholder(idx)}",
                [value],
                idx + 1,
            )
        if op in _REGEX_OPS:
            regex_op = dialect.regex_ops.get(op)
            if regex_op is None:
                raise UnSupportedError(f"{dialect.name} does not support the __{op} lookup")
            return f"{col} {regex_op} {dialect.placeholder(idx)}", [value], idx + 1
        if op == "date":
            return (
                f"{dialect.truncate_date_sql(col)} = {dialect.placeholder(idx)}",
                [coerce(value)],
                idx + 1,
            )
        if op == "search":
            return dialect.search_sql(col, dialect.placeholder(idx)), [value], idx + 1

        sql_op, pattern = _OPERATORS[op]
        if sql_op == "ILIKE":
            # ILIKE is PostgreSQL-only; SQLite's LIKE is already case-insensitive.
            sql_op = dialect.ilike
        if isinstance(value, Expression):
            # Compare the column against another column expression (e.g. F).
            meta = self.model._meta
            expr_params: list[Any] = []
            expr_sql, idx = value.resolve(
                lambda n: self._qualified(dialect, meta.get_field(n)), dialect, expr_params, idx
            )
            return f"{col} {sql_op} {expr_sql}", expr_params, idx
        placeholder = dialect.placeholder(idx)
        idx += 1
        bound = pattern(value) if pattern is not None else coerce(value)
        return f"{col} {sql_op} {placeholder}", [bound], idx

    def _compile_relation_lookup(
        self,
        base: str,
        op: str,
        value: Any,
        dialect: BaseDialect,
        idx: int,
    ) -> tuple[str, list[Any], int]:
        """Compile a relation-spanning lookup (``rel__...__field``) to a subquery.

        Resolves the leading relation segment, then compiles the remainder of
        the path against the related model in a fresh queryset, embedding it as
        an uncorrelated membership subquery. Nesting recurses, so paths of any
        depth and self-relations work without join-induced row duplication.

        Args:
            base: The field path with at least one relation segment.
            op: The trailing lookup operator applied to the final field.
            value: The comparison value.
            dialect: The SQL dialect providing quoting and placeholders.
            idx: The next available bind-parameter index.

        Returns:
            A ``(sql, params, next_index)`` tuple holding the membership
            condition SQL, its bound parameters and the updated index.
        """
        # Deferred: breaks the queryset <-> relations import cycle.
        from .relations import M2MDescriptor, ReverseFKDescriptor, model_name

        seg, _, rest = base.partition("__")
        inner_key = rest if op == "exact" else f"{rest}__{op}"
        meta = self.model._meta
        q = dialect.quote
        table = q(meta.table)
        pk = q(meta.pk_field.db_column)

        if seg in meta.relations:
            # Forward FK: base.<fk> IN (SELECT target.pk FROM target WHERE inner)
            info = meta.relations[seg]
            target = info.resolve_target()
            tmeta = target._meta
            fk_col = f"{table}.{q(meta.get_field(info.source_attr).db_column)}"
            inner, params, idx = QuerySet(target)._compile_lookup(inner_key, value, dialect, idx)
            sub = (
                f"SELECT {q(tmeta.table)}.{q(tmeta.pk_field.db_column)} "
                f"FROM {q(tmeta.table)} WHERE {inner}"
            )
            return f"{fk_col} IN ({sub})", params, idx

        descriptor = getattr(self.model, seg, None)
        if isinstance(descriptor, ReverseFKDescriptor):
            # Reverse FK: base.pk IN (SELECT child.<fk> FROM child WHERE inner)
            source = registry.get_model(model_name(descriptor.source_reference))
            smeta = source._meta
            inner, params, idx = QuerySet(source)._compile_lookup(inner_key, value, dialect, idx)
            sub = (
                f"SELECT {q(smeta.table)}.{q(descriptor.source_attr)} "
                f"FROM {q(smeta.table)} WHERE {inner}"
            )
            return f"{table}.{pk} IN ({sub})", params, idx

        if isinstance(descriptor, M2MDescriptor):
            # M2M: base.pk IN (SELECT through.near FROM through
            #                  WHERE through.far IN (SELECT target.pk ... inner))
            info = descriptor.info
            info.finalize()
            if descriptor.reverse:
                near, far = info.forward_key, info.backward_key
                target = info.owner
            else:
                near, far = info.backward_key, info.forward_key
                target = info.resolve_target()
            tmeta = target._meta
            inner, params, idx = QuerySet(target)._compile_lookup(inner_key, value, dialect, idx)
            target_pks = (
                f"SELECT {q(tmeta.table)}.{q(tmeta.pk_field.db_column)} "
                f"FROM {q(tmeta.table)} WHERE {inner}"
            )
            sub = (
                f"SELECT {q(info.through)}.{q(near)} FROM {q(info.through)} "
                f"WHERE {q(info.through)}.{q(far)} IN ({target_pks})"
            )
            return f"{table}.{pk} IN ({sub})", params, idx

        raise FieldError(f"Cannot filter across unknown relation {seg!r} on {self.model.__name__}")

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
            target_model = info.owner
        else:
            near, far = info.backward_key, info.forward_key
            target_model = info.resolve_target()
        q = dialect.quote
        meta = self.model._meta
        table = q(meta.table)
        pk = q(meta.pk_field.db_column)
        through = q(info.through)
        far_pk = target_model._meta.pk_field
        if op == "in":
            vals = [v.pk if _is_model(v) else v for v in value]
            if not vals:
                # ``rel__in=[]`` matches no rows (an empty membership set);
                # emit a constant false rather than the invalid ``IN ()``.
                return "1 = 0", [], idx
            holes, params = [], []
            for v in vals:
                holes.append(dialect.placeholder(idx))
                params.append(far_pk.to_db(v))
                idx += 1
            inner = (
                f"SELECT {through}.{q(near)} FROM {through} "
                f"WHERE {through}.{q(far)} IN ({', '.join(holes)})"
            )
            return f"{table}.{pk} IN ({inner})", params, idx
        membership = "NOT IN" if op == "not" else "IN"
        target = far_pk.to_db(value.pk if _is_model(value) else value)
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
        meta = self.model._meta
        table = dialect.quote(meta.table)
        parts = []
        for name, descending in order:
            if name == "?":
                # Random ordering — RANDOM() is accepted by PostgreSQL and SQLite.
                parts.append(dialect.random_function)
                continue
            if name in self._annotations:
                ref = dialect.quote(name)
            elif "__" in name:
                # Order across a forward relation via a correlated subquery, so
                # no JOIN has to be threaded into the FROM clause.
                ref = self._relation_order_ref(name, dialect)
            else:
                # Qualify with the base table so ordering stays unambiguous when
                # select_related joins a table that shares column names.
                ref = f"{table}.{dialect.quote(meta.get_field(name).db_column)}"
            parts.append(ref + (" DESC" if descending else " ASC"))
        return " ORDER BY " + ", ".join(parts)

    def _relation_order_ref(self, name: str, dialect: BaseDialect) -> str:
        """Render an ``order_by`` term that walks a forward-relation path.

        Builds a correlated scalar subquery ``(SELECT <col> FROM <target> ...
        WHERE <target.pk> = <base.fk>)`` for a forward foreign-key chain such as
        ``contact__last_call_created_at`` or ``a__b__name``. This keeps ordering
        on a related column self-contained, so every select path supports it
        without adding a join. Reverse-FK / M2M paths are not orderable this way.

        Args:
            name: The ``rel__...__col`` forward-relation path.
            dialect: The SQL dialect providing identifier quoting.

        Raises:
            FieldError: When a segment is not a forward relation (e.g. a reverse
                or many-to-many relation), which has no single orderable value.

        Returns:
            The correlated subquery SQL to use as the ``ORDER BY`` term.
        """
        q = dialect.quote
        meta = self.model._meta
        segments = name.split("__")
        info = meta.relations.get(segments[0])
        if info is None:
            raise FieldError(
                f"Cannot order by relation path {name!r}: {segments[0]!r} is not a forward relation"
            )
        base_fk = q(meta.get_field(info.source_attr).db_column)
        cur_meta = info.resolve_target()._meta
        from_table = q(cur_meta.table)
        correlation = f"{from_table}.{q(cur_meta.pk_field.db_column)} = {q(meta.table)}.{base_fk}"
        joins = ""
        for seg in segments[1:-1]:
            info = cur_meta.relations.get(seg)
            if info is None:
                raise FieldError(
                    f"Cannot order by relation path {name!r}: {seg!r} is not a forward relation"
                )
            next_meta = info.resolve_target()._meta
            src = q(cur_meta.get_field(info.source_attr).db_column)
            joins += (
                f" JOIN {q(next_meta.table)} ON {q(cur_meta.table)}.{src} = "
                f"{q(next_meta.table)}.{q(next_meta.pk_field.db_column)}"
            )
            cur_meta = next_meta
        final_col = f"{q(cur_meta.table)}.{q(cur_meta.get_field(segments[-1]).db_column)}"
        return f"(SELECT {final_col} FROM {from_table}{joins} WHERE {correlation})"  # noqa: S608

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
            The ``FOR UPDATE [OF ...] [NOWAIT|SKIP LOCKED]`` clause on backends
            that support it, else an empty string.
        """
        if not (self._for_update and dialect.name == "postgres"):
            return ""
        parts = ["FOR UPDATE"]
        if self._for_update_of:
            parts.append("OF " + ", ".join(dialect.quote(n) for n in self._for_update_of))
        if self._for_update_nowait:
            parts.append("NOWAIT")
        elif self._for_update_skip_locked:
            parts.append("SKIP LOCKED")
        return " " + " ".join(parts)

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
        agg: Any,
        dialect: BaseDialect,
        joins: dict[str, str],
        params: list[Any],
        idx: int,
    ) -> tuple[str, int]:
        """Compile an annotation expression to SQL, binding any literals.

        Aggregates and scalar functions bind nothing (``idx`` is returned
        unchanged); param-producing expressions (``Case``, ``RawSQL``) expose an
        ``as_sql`` hook that appends to ``params`` and advances ``idx``.

        Args:
            agg: The annotation expression (Aggregate, Function, Case, RawSQL).
            dialect: The SQL dialect providing identifier quoting.
            joins: Mapping of join key to join SQL, mutated in place when the
                expression spans a relation.
            params: Bound-parameter list, extended in place.
            idx: The next available bind-parameter index.

        Returns:
            A ``(sql, next_index)`` tuple.
        """
        if hasattr(agg, "as_sql"):
            return agg.as_sql(self, dialect, joins, params, idx)

        def resolve(name: str) -> str:
            return self._resolve_column(name, dialect, joins)

        if isinstance(agg, Expression):
            # F / arithmetic projected as an annotation, e.g. annotate(x=F("a")+1).
            return agg.resolve(resolve, dialect, params, idx)
        if isinstance(agg, Function):
            return agg.render_params(resolve, dialect, params, idx)
        # An aggregate (Count/Sum/...) over a column name, a column expression
        # (F / arithmetic) or a Case; with an optional FILTER (WHERE ...) Q.
        inner = agg.field
        if isinstance(inner, Expression):
            inner_sql, idx = inner.resolve(resolve, dialect, params, idx)
        elif hasattr(inner, "as_sql"):
            inner_sql, idx = inner.as_sql(self, dialect, joins, params, idx)
        else:
            inner_sql = resolve(inner)
        distinct = "DISTINCT " if getattr(agg, "distinct", False) else ""
        sql = f"{agg.function}({distinct}{inner_sql})"
        filt = getattr(agg, "filter", None)
        if filt is not None:
            # Reuse the WHERE compiler so the filter's Q binds correctly and
            # shares the surrounding statement's placeholder numbering.
            fsql, fparams, idx = self._compile_q(filt, dialect, idx)
            params.extend(fparams)
            if fsql:
                sql = f"{sql} FILTER (WHERE {fsql})"
        return sql, idx

    def _resolve_column(self, field: str, dialect: BaseDialect, joins: dict[str, str]) -> str:
        """Resolve a field name to its qualified column, adding joins as needed.

        Supports multi-level forward-relation paths (``author__country__name``):
        each non-final segment that names a forward relation is chain-joined, and
        the final segment is the target column (or, if it too is a relation, its
        primary key). A single reverse-FK/M2M hop (``rel__col``) is still handled
        via the aggregate join helper.

        Args:
            field: A local column, ``pk``, a relation name, or a (possibly
                multi-level) ``rel__...__col`` path.
            dialect: The SQL dialect providing identifier quoting.
            joins: Mapping of join key to join SQL, mutated in place when the
                field spans a relation.

        Returns:
            The qualified ``"table"."column"`` reference.
        """
        q = dialect.quote
        meta = self.model._meta
        table = q(meta.table)
        if "__" not in field:
            if field in meta.fields or field == "pk":
                return f"{table}.{q(meta.get_field(field).db_column)}"
            tmeta = self._add_relation_join(field, dialect, joins)
            return f"{q(tmeta.table)}.{q(tmeta.pk_field.db_column)}"

        segments = field.split("__")
        cur_meta = meta
        cur_table = table
        chain = ""
        for i, seg in enumerate(segments):
            last = i == len(segments) - 1
            info = cur_meta.relations.get(seg)
            if info is not None:
                # Forward relation: chain a LEFT JOIN to its target table.
                chain = f"{chain}__{seg}" if chain else seg
                tmeta = info.resolve_target()._meta
                src = q(cur_meta.get_field(info.source_attr).db_column)
                joins[chain] = (
                    f" LEFT JOIN {q(tmeta.table)} ON {cur_table}.{src} = "
                    f"{q(tmeta.table)}.{q(tmeta.pk_field.db_column)}"
                )
                cur_meta, cur_table = tmeta, q(tmeta.table)
                if last:
                    return f"{cur_table}.{q(cur_meta.pk_field.db_column)}"
            elif last:
                return f"{cur_table}.{q(cur_meta.get_field(seg).db_column)}"
            elif cur_meta is meta and len(segments) == 2:
                # A single reverse-FK / M2M hop, e.g. ``tags__name``.
                tmeta = self._add_relation_join(seg, dialect, joins)
                return f"{q(tmeta.table)}.{q(tmeta.get_field(segments[1]).db_column)}"
            else:
                raise FieldError(f"Cannot traverse relation path {field!r}")
        raise FieldError(f"Cannot traverse relation path {field!r}")  # pragma: no cover

    def _compile_filter_dict(
        self, conditions: dict[str, Any], dialect: BaseDialect, idx: int
    ) -> tuple[str, list[Any], int]:
        """Compile a dict of field lookups into a SQL boolean expression.

        Used by ``Case``/``When`` to render an arm's conditions.

        Args:
            conditions: Field lookups (as passed to ``filter``).
            dialect: The SQL dialect providing quoting and placeholders.
            idx: The next available bind-parameter index.

        Returns:
            A ``(sql, params, next_index)`` tuple.
        """
        return self._compile_q(Q(**conditions), dialect, idx)

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
            # Render the annotation expression, then reuse the same comparison
            # compiler as WHERE so every lookup (in/range/isnull/icontains/date
            # parts/...) works against an aggregate, with dialect-correct
            # operators and bound (never spliced) values.
            expr, idx = self._aggregate_expr(self._annotations[name], dialect, joins, params, idx)
            clause, p, idx = self._compile_field_op(expr, None, op, value, dialect, idx)
            clauses.append(clause)
            params.extend(p)
        having = (" HAVING " + " AND ".join(clauses)) if clauses else ""
        return having, params, idx

    # -- execution --------------------------------------------------------
    def _selected_fields(self) -> list[Field]:
        """Return the fields to SELECT under ``only()`` / ``defer()``.

        Returns:
            The selected ``Field`` objects (all fields when neither is set).
        """
        meta = self.model._meta
        if self._only is not None:
            return [meta.get_field(n) for n in self._only]
        if self._defer:
            return [f for f in meta.field_list if f.model_field_name not in self._defer]
        return meta.field_list

    def _plain_select_sql(
        self, dialect: BaseDialect, start: int = 1
    ) -> tuple[str, list[Any], list[Field] | None]:
        """Build the plain (no annotate/select_related) SELECT and its params.

        Shared by ``_fetch``, ``sql()``, ``explain()`` and ``Subquery`` so they
        always render the identical statement.

        Args:
            dialect: The SQL dialect providing quoting and placeholders.
            start: The first bind-parameter index (``Subquery`` continues an
                outer query's numbering).

        Returns:
            A ``(sql, params, fields)`` tuple; ``fields`` lists the selected
            columns under ``only()``/``defer()`` (``None`` for a full row).
        """
        meta = self.model._meta
        meta.compile(dialect)
        where, params, _ = self._compile_conditions(dialect, start=start)
        tail = f"{self._order_sql(dialect)}{self._tail_sql()}{self._lock_sql(dialect)}"
        if self._only is not None or self._defer:
            sel = self._selected_fields()
            cols = ", ".join(dialect.quote(f.db_column) for f in sel)
            prefix = self._distinct_prefix(f"SELECT {cols} FROM {dialect.quote(meta.table)}")
            return f"{prefix}{where}{tail}", params, sel
        prefix = self._distinct_prefix(meta.select_prefix)
        return f"{prefix}{where}{tail}", params, None

    async def _fetch(self) -> list[Model]:
        """Execute the query and build model instances from the rows.

        Returns:
            A list of model instances, with any requested relations prefetched.
        """
        if self._annotations or self._select_related:
            if (self._only is not None or self._defer) and self._annotations:
                raise FieldError("only()/defer() cannot be combined with annotate()")
            if self._annotations:
                return await self._fetch_annotated()
            return await self._fetch_select_related()
        dialect = get_dialect(self.model, using=self._using_name())
        engine = get_executor(self.model, using=self._using)
        sql, params, sel = self._plain_select_sql(dialect)
        rows = await engine.fetch_rows(sql, params)
        if sel is not None:
            instances = [self.model._from_db_row_fields(row, sel) for row in rows]
        else:
            instances = self.model._from_db_rows(rows)
        if self._prefetch:
            # Deferred: breaks the queryset <-> prefetch import cycle.
            from .prefetch import prefetch_instances

            await prefetch_instances(instances, self._prefetch)
        return instances

    def _select_related_plan(
        self, dialect: BaseDialect
    ) -> tuple[str, list[Any], list[str], dict[str, dict[str, Any]], int]:
        """Build the ``select_related`` SELECT and its row-decoding plan.

        Shared by :meth:`_fetch_select_related` and
        :meth:`get_parameterized_sql` so both render the identical statement.

        Args:
            dialect: The SQL dialect providing quoting and placeholders.

        Returns:
            A ``(sql, params, order, nodes, ncols)`` tuple: the SELECT and its
            bound params, the relation paths in join order, the per-relation
            decode nodes and the base model's column count.
        """
        meta = self.model._meta
        meta.compile(dialect)
        q = dialect.quote
        table = q(meta.table)
        # The base columns honour only()/defer() (the related tables are still
        # loaded in full); the JOINs reference the FK columns directly, so
        # restricting the SELECT does not affect them.
        base_fields = self._selected_fields()
        select = [f"{table}.{q(f.db_column)}" for f in base_fields]
        joins: list[str] = []
        # One node per (possibly nested) relation path, in join order. Each
        # records its parent path (None = base), the last segment, target model
        # and the column slice it occupies in the SELECT.
        nodes: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        offset = len(base_fields)

        def ensure_path(path: str) -> dict[str, Any]:
            """Add the LEFT JOIN(s) for ``path``, recursing through its parents.

            Args:
                path: The (possibly nested) forward-relation path to join.

            Returns:
                The decode node registered for ``path``.
            """
            nonlocal offset
            if path in nodes:
                return nodes[path]
            if "__" in path:
                parent_path, _, seg = path.rpartition("__")
                parent_meta = ensure_path(parent_path)["target"]._meta
                left_alias = q(parent_path)
                parent: str | None = parent_path
            else:
                parent_meta, left_alias, parent, seg = meta, table, None, path
            if seg not in parent_meta.relations:
                raise FieldError(
                    f"select_related: {path!r} is not a forward relation of {self.model.__name__}"
                )
            info = parent_meta.relations[seg]
            target = info.resolve_target()
            tmeta = target._meta
            tmeta.compile(dialect)
            alias = q(path)
            fk_col = q(parent_meta.get_field(info.source_attr).db_column)
            joins.append(
                f" LEFT JOIN {q(tmeta.table)} AS {alias} "
                f"ON {left_alias}.{fk_col} = {alias}.{q(tmeta.pk_field.db_column)}"
            )
            select.extend(f"{alias}.{q(f.db_column)}" for f in tmeta.field_list)
            node = {
                "parent": parent,
                "seg": seg,
                "target": target,
                "offset": offset,
                "width": len(tmeta.field_list),
            }
            offset += node["width"]
            nodes[path] = node
            order.append(path)
            return node

        for rel in self._select_related:
            ensure_path(rel)
        where, params, _ = self._compile_conditions(dialect)
        sql = (
            f"SELECT {', '.join(select)} FROM {table}{''.join(joins)}{where}"
            f"{self._order_sql(dialect)}{self._tail_sql()}"
        )
        return sql, params, order, nodes, len(base_fields)

    async def _fetch_select_related(self) -> list[Model]:
        """Execute a query that joins and hydrates forward FK/O2O relations.

        Each selected relation is LEFT JOINed (aliased by relation name, so
        self-joins and repeated targets are unambiguous) and its columns are
        decoded into a related instance cached under the instance's prefetch
        slot — making the relation available synchronously.

        Returns:
            The model instances with each selected relation cached.
        """
        dialect = get_dialect(self.model, using=self._using_name())
        engine = get_executor(self.model, using=self._using)
        sql, params, order, nodes, ncols = self._select_related_plan(dialect)
        base_sel = self._selected_fields() if (self._only is not None or self._defer) else None
        rows = await engine.fetch_rows(sql, params)
        instances = []
        for row in rows:
            obj = (
                self.model._from_db_row_fields(row[:ncols], base_sel)
                if base_sel is not None
                else self.model._from_db_row(row[:ncols])
            )
            built: dict[str | None, Any] = {None: obj}
            obj.__dict__.setdefault("_prefetch", {})
            for path in order:
                node = nodes[path]
                parent_inst = built[node["parent"]]
                if parent_inst is None:
                    built[path] = None
                    continue
                chunk = row[node["offset"] : node["offset"] + node["width"]]
                child = (
                    node["target"]._from_db_row(chunk)
                    if any(v is not None for v in chunk)
                    else None
                )
                parent_inst.__dict__.setdefault("_prefetch", {})[node["seg"]] = child
                built[path] = child
            instances.append(obj)
        if self._prefetch:
            # Deferred: breaks the queryset <-> prefetch import cycle.
            from .prefetch import prefetch_instances

            await prefetch_instances(instances, self._prefetch)
        return instances

    def _annotated_select_sql(self, dialect: BaseDialect) -> tuple[str, list[Any]]:
        """Build the annotated SELECT (grouped by pk) and its bound params.

        Shared by :meth:`_fetch_annotated` and :meth:`get_parameterized_sql` so
        both render the identical statement.

        Args:
            dialect: The SQL dialect providing quoting and placeholders.

        Returns:
            A ``(sql, params)`` tuple.
        """
        meta = self.model._meta
        meta.compile(dialect)
        q = dialect.quote
        table = q(meta.table)
        joins: dict = {}
        select = [f"{table}.{q(f.db_column)}" for f in meta.field_list]
        select_params: list[Any] = []
        idx = 1
        for name in self._annotations:
            expr, idx = self._aggregate_expr(
                self._annotations[name], dialect, joins, select_params, idx
            )
            select.append(f"{expr} AS {q(name)}")
        where, wparams, idx = self._compile_conditions(dialect, start=idx)
        having, hparams, idx = self._compile_having(dialect, idx, joins)
        params = select_params + wparams + hparams
        group = f" GROUP BY {table}.{q(meta.pk_field.db_column)}"
        sql = (
            f"SELECT {', '.join(select)} FROM {table}"
            f"{''.join(joins.values())}{where}{group}{having}"
            f"{self._order_sql(dialect)}{self._tail_sql()}"
        )
        return sql, params

    async def _fetch_annotated(self) -> list[Model]:
        """Execute an annotated query and attach annotations to instances.

        Returns:
            A list of model instances with each annotation value set as an
            attribute, with any requested relations prefetched.
        """
        dialect = get_dialect(self.model, using=self._using_name())
        engine = get_executor(self.model, using=self._using)
        meta = self.model._meta
        sql, params = self._annotated_select_sql(dialect)
        annotation_names = list(self._annotations.keys())
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

    async def __aiter__(self) -> AsyncGenerator[Model, None]:
        """Iterate the matching instances with ``async for``.

        Executes the query once and yields each instance, so
        ``async for obj in Model.filter(...)`` works like ``for obj in await ...``.

        Yields:
            Each matching model instance.
        """
        for obj in await self._fetch():
            yield obj

    async def _fetch_columns(self, field_paths: tuple[str, ...]) -> list[Any]:
        """Fetch raw rows for the given column paths without building models.

        Each path may traverse a relation (``"author__name"``); the needed
        ``LEFT JOIN`` is added automatically via :meth:`_resolve_column`.

        Args:
            field_paths: The field names/paths whose columns to select.

        Returns:
            The raw database rows for the selected columns.
        """
        dialect = get_dialect(self.model, using=self._using_name())
        engine = get_executor(self.model, using=self._using)
        meta = self.model._meta
        meta.compile(dialect)
        joins: dict[str, str] = {}
        cols = ", ".join(self._resolve_column(p, dialect, joins) for p in field_paths)
        where, params, _ = self._compile_conditions(dialect)
        table = dialect.quote(meta.table)
        distinct = "DISTINCT " if self._distinct else ""
        sql = (
            f"SELECT {distinct}{cols} FROM {table}{''.join(joins.values())}{where}"
            f"{self._order_sql(dialect)}{self._tail_sql()}"
        )
        return await engine.fetch_rows(sql, params)

    def _grouped_select_sql(
        self, dialect: BaseDialect, fields: tuple[str, ...] = ()
    ) -> tuple[str, list[Any], list[str]]:
        """Build the grouped/annotated SELECT, its params and column names.

        Shared by :meth:`_values_grouped` and :meth:`get_parameterized_sql`. An
        empty ``fields`` selects every group-by column and annotation (the shape
        a wrapping ``SELECT COUNT(*) FROM (...)`` needs).

        Args:
            dialect: The SQL dialect providing quoting and placeholders.
            fields: Requested field/annotation names, or empty for all.

        Returns:
            A ``(sql, params, names)`` tuple; ``names`` are the output column
            names in select order.
        """
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
        select_params: list[Any] = []
        idx = 1
        for name, agg in self._annotations.items():
            if requested and name not in requested:
                continue
            expr, idx = self._aggregate_expr(agg, dialect, joins, select_params, idx)
            select.append(f"{expr} AS {q(name)}")
            names.append(name)

        where, wparams, idx = self._compile_conditions(dialect, start=idx)
        having, hparams, idx = self._compile_having(dialect, idx, joins)
        params = select_params + wparams + hparams
        group = (" GROUP BY " + ", ".join(group_cols)) if group_cols else ""
        sql = (
            f"SELECT {', '.join(select)} FROM {table}"
            f"{''.join(joins.values())}{where}{group}{having}"
            f"{self._order_sql(dialect)}{self._tail_sql()}"
        )
        return sql, params, names

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
        dialect = get_dialect(self.model, using=self._using_name())
        engine = get_executor(self.model, using=self._using)
        sql, params, names = self._grouped_select_sql(dialect, fields)
        rows = await engine.fetch_rows(sql, params)
        if as_dict:
            return [dict(zip(names, row)) for row in rows]
        return [tuple(row) for row in rows]

    def values_list(self, *fields: str, flat: bool = False) -> _ValuesQuery:
        """Return rows as tuples (or scalars when ``flat=True``); no model build.

        The result is awaitable (``await qs.values_list(...)`` → list), async
        iterable (``async for row in qs.values_list(...)``) and supports
        ``.first()`` for a single row.

        Args:
            *fields: Field names/paths to select; a path may traverse a
                relation (``"author__name"``). Defaults to all model fields.
            flat: When ``True`` return scalar values for a single field.

        Returns:
            A ``_ValuesQuery`` resolving to a list of tuples, or scalars when
            ``flat`` is ``True``.
        """
        return _ValuesQuery(lambda: self._values_list_impl(fields, flat))

    async def _values_list_impl(
        self,
        fields: tuple[str, ...],
        flat: bool,
    ) -> list[tuple[Any, ...]] | list[Any]:
        """Execute the ``values_list`` query and return its rows.

        Args:
            fields: Field names/paths to select; empty for all model fields.
            flat: When ``True`` return scalar values for a single field.

        Returns:
            A list of tuples, or a list of scalars when ``flat`` is ``True``.
        """
        if self._annotations or self._group_by:
            if not fields:
                return await self._values_grouped(fields, as_dict=False)
            # The grouped SELECT also carries the group-by columns, so project
            # down to exactly the requested fields (by name) before tupling.
            dict_rows = await self._values_grouped(fields, as_dict=True)
            if flat:
                if len(fields) != 1:
                    raise FieldError("flat=True requires exactly one field")
                return [r[fields[0]] for r in dict_rows]
            return [tuple(r[f] for f in fields) for r in dict_rows]
        paths = fields or tuple(self.model._meta.fields.keys())
        if flat:
            if len(paths) != 1:
                raise FieldError("flat=True requires exactly one field")
            rows = await self._fetch_columns(paths)
            return [r[0] for r in rows]
        rows = await self._fetch_columns(paths)
        return [tuple(r) for r in rows]

    def values(self, *fields: str, **aliases: str) -> _ValuesQuery:
        """Return rows as dicts of the requested columns; no model build.

        The result is awaitable (``await qs.values(...)`` → list), async iterable
        (``async for row in qs.values(...)``) and supports ``.first()``.

        Args:
            *fields: Field names/paths to select; the dict key is the path
                itself (a path may traverse a relation, ``"author__name"``).
            **aliases: ``output_name=field_path`` pairs, so a traversed column
                can be given a clean key (``author_name="author__name"``).

        Returns:
            A ``_ValuesQuery`` resolving to a list of dicts mapping each
            requested name to its value.
        """
        return _ValuesQuery(lambda: self._values_impl(fields, aliases))

    async def _values_impl(
        self, fields: tuple[str, ...], aliases: dict[str, str]
    ) -> list[dict[str, Any]]:
        """Execute the ``values`` query and return its dict rows.

        Args:
            fields: Field names/paths to select; empty for all model fields.
            aliases: ``output_name -> field_path`` pairs.

        Returns:
            A list of dicts mapping each requested name to its value.
        """
        if self._annotations or self._group_by:
            rows = await self._values_grouped(fields, as_dict=True)
            # The grouped SELECT also carries the group-by columns; when specific
            # fields were requested, return only those (by name).
            if fields:
                return [{f: r[f] for f in fields} for r in rows]
            return rows
        if not fields and not aliases:
            names = paths = tuple(self.model._meta.fields.keys())
        else:
            names = tuple(fields) + tuple(aliases.keys())
            paths = tuple(fields) + tuple(aliases.values())
        rows = await self._fetch_columns(paths)
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
            raise self.model.DoesNotExist(f"{self.model.__name__} matching query does not exist")
        if len(rows) > 1:
            raise self.model.MultipleObjectsReturned(
                f"Multiple {self.model.__name__} objects returned"
            )
        return rows[0]

    async def get_or_none(self, **kwargs: Any) -> Model | None:
        """Fetch the single object matching the lookups, or ``None``.

        Args:
            **kwargs: Optional field lookups further narrowing the query.

        Returns:
            The single matching instance, or ``None`` when there is no match.
        """
        qs = self.filter(**kwargs).limit(2) if kwargs else self.limit(2)
        rows = await qs._fetch()
        if len(rows) > 1:
            raise self.model.MultipleObjectsReturned(
                f"Multiple {self.model.__name__} objects returned"
            )
        return rows[0] if rows else None

    async def get_or_create(
        self, defaults: dict[str, Any] | None = None, **kwargs: Any
    ) -> tuple[Model, bool]:
        """Fetch the row matching this query plus ``kwargs``, or create it.

        Args:
            defaults: Extra field values used only when creating the row.
            **kwargs: Lookups identifying the row and reused on creation.

        Returns:
            A ``(instance, created)`` tuple; ``created`` is ``True`` on insert.
        """
        obj = await self.get_or_none(**kwargs)
        if obj is not None:
            return obj, False
        return await self.model.create(**{**kwargs, **(defaults or {})}), True

    async def update_or_create(
        self, defaults: dict[str, Any] | None = None, **kwargs: Any
    ) -> tuple[Model, bool]:
        """Update the row matching this query plus ``kwargs``, or create it.

        Args:
            defaults: Field values to set (on update) or add (on create).
            **kwargs: Lookups identifying the row and reused on creation.

        Returns:
            A ``(instance, created)`` tuple; ``created`` is ``True`` on insert.
        """
        defaults = defaults or {}
        obj = await self.get_or_none(**kwargs)
        if obj is None:
            return await self.model.create(**{**kwargs, **defaults}), True
        if defaults:
            obj.update_from_dict(defaults)
            await obj.save()
        return obj, False

    def sql(self) -> str:
        """Return the SELECT statement this query set would execute.

        Returns:
            The SQL string (with dialect placeholders for bound parameters).

        Raises:
            UnSupportedError: With ``annotate()`` / ``select_related()`` set.
        """
        if self._annotations or self._select_related:
            raise UnSupportedError("sql() is not supported with annotate()/select_related()")
        sql, _ = self.get_parameterized_sql()
        return sql

    def get_parameterized_sql(self) -> tuple[str, list[Any]]:
        """Return the exact SELECT this query set runs, with its bind params.

        Unlike :meth:`sql` (SQL only, and rejecting annotated queries), this
        exposes the full ``(sql, params)`` pair for every query shape — plain,
        ``only()``/``defer()``, ``select_related``, ``annotate`` and grouped
        ``group_by(...).values(...)`` — built from the same compile path the
        query set executes. Callers can wrap or inspect it (e.g.
        ``SELECT COUNT(*) FROM (<sql>) x``) without reaching into private
        compilers.

        Returns:
            A ``(sql, params)`` tuple; ``params`` are the bound values in
            placeholder order.
        """
        dialect = get_dialect(self.model, using=self._using_name())
        if self._group_by:
            sql, params, _ = self._grouped_select_sql(dialect)
            return sql, params
        if self._annotations:
            return self._annotated_select_sql(dialect)
        if self._select_related:
            sql, params, _, _, _ = self._select_related_plan(dialect)
            return sql, params
        sql, params, _ = self._plain_select_sql(dialect)
        return sql, params

    async def explain(self) -> str:
        """Return the database's query plan for this query set.

        Returns:
            The plan rows joined into a single string.

        Raises:
            UnSupportedError: With ``annotate()`` / ``select_related()`` set.
        """
        if self._annotations or self._select_related:
            raise UnSupportedError("explain() is not supported with annotate()/select_related()")
        dialect = get_dialect(self.model, using=self._using_name())
        engine = get_executor(self.model, using=self._using)
        sql, params, _ = self._plain_select_sql(dialect)
        rows = await engine.fetch_rows(f"{dialect.explain_prefix}{sql}", params)
        return "\n".join(" ".join(str(c) for c in row) for row in rows)

    def first(self) -> QuerySetSingle[Model | None]:
        """Return a chainable single-row result for the first matching object.

        Awaiting it resolves to the first instance or ``None``; chaining
        ``only`` / ``values`` / ``values_list`` / ``select_related`` /
        ``prefetch_related`` narrows that single row first.

        Returns:
            A ``QuerySetSingle`` resolving to the first instance, or ``None``.
        """
        return QuerySetSingle(self, _resolve_first)

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
        dialect = get_dialect(self.model, using=self._using_name())
        engine = get_executor(self.model, using=self._using)
        where, params, _ = self._compile_conditions(dialect)
        table = dialect.quote(self.model._meta.table)
        row = await engine.fetch_row(f"SELECT COUNT(*) FROM {table}{where}", params)
        return int(row[0]) if row else 0

    async def exists(self) -> bool:
        """Report whether any row matches the current conditions.

        Returns:
            ``True`` if at least one matching row exists.
        """
        dialect = get_dialect(self.model, using=self._using_name())
        engine = get_executor(self.model, using=self._using)
        where, params, _ = self._compile_conditions(dialect)
        table = dialect.quote(self.model._meta.table)
        rows = await engine.fetch_rows(f"SELECT 1 FROM {table}{where} LIMIT 1", params)
        return bool(rows)

    async def delete(self) -> int:
        """Delete all rows matching the current conditions.

        Returns:
            The number of rows deleted.
        """
        dialect = get_dialect(self.model, using=self._using_name())
        engine = get_executor(self.model, write=True, using=self._using)
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
        dialect = get_dialect(self.model, using=self._using_name())
        engine = get_executor(self.model, write=True, using=self._using)
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
            elif isinstance(value, Function):
                # Assign a function expression, e.g. ``update(at=Coalesce("at", now))``.
                expr_sql, idx = value.render_params(
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


async def _resolve_first(queryset: QuerySet) -> Model | None:
    """Fetch the first row of ``queryset`` (the awaited form of ``first()``).

    Args:
        queryset: The query set to take the first row of.

    Returns:
        The first matching instance, or ``None`` when there are no matches.
    """
    rows = await queryset.limit(1)._fetch()
    return rows[0] if rows else None


async def _resolve_get(queryset: QuerySet) -> Model:
    """Fetch the single row of ``queryset`` (the awaited form of ``get()``).

    Args:
        queryset: The query set narrowed to a single row.

    Returns:
        The single matching instance.

    Raises:
        DoesNotExist: When no row matches.
        MultipleObjectsReturned: When more than one row matches.
    """
    return await queryset.get()


_SingleT = TypeVar("_SingleT")


class _ValuesQuery:
    """Awaitable, async-iterable result of ``values()`` / ``values_list()``.

    ``await`` resolves to the full list of rows; ``async for`` streams them; and
    ``first()`` returns the first row (or ``None``). Each terminal re-runs the
    underlying query.
    """

    def __init__(self, run: Callable[[], Awaitable[list[Any]]]) -> None:
        """Wrap the zero-arg coroutine factory that runs the projection.

        Args:
            run: A callable returning the awaitable that fetches the rows.

        Returns:
            None
        """
        self._run = run

    def __await__(self) -> Generator[Any, None, list[Any]]:
        """Resolve to the full list of projected rows.

        Returns:
            The list of rows.
        """
        return self._run().__await__()

    async def __aiter__(self) -> AsyncGenerator[Any, None]:
        """Stream the projected rows with ``async for``.

        Yields:
            Each projected row.
        """
        for row in await self._run():
            yield row

    async def first(self) -> Any:
        """Return the first projected row, or ``None`` when there are none.

        Returns:
            The first row, or ``None``.
        """
        rows = await self._run()
        return rows[0] if rows else None


class QuerySetSingle(Generic[_SingleT]):
    """Awaitable, chainable single-row result.

    Returned by ``Model.get(...)`` and ``QuerySet.first()`` so callers can either
    ``await Model.get(id=x)`` or chain
    ``await Model.get(id=x).prefetch_related(...).only(...)``. Awaiting a ``get``
    result raises ``DoesNotExist`` / ``MultipleObjectsReturned`` as usual; a
    ``first()`` result resolves to ``None`` when there is no match.
    """

    def __init__(
        self,
        queryset: QuerySet,
        resolver: Callable[[QuerySet], Awaitable[_SingleT]],
        fast: Callable[[], Awaitable[_SingleT]] | None = None,
    ) -> None:
        """Wrap the query set whose single row will be awaited.

        Args:
            queryset: The query set already narrowed to the target row.
            resolver: Coroutine resolving the wrapped query set to a single
                instance. ``Model.get`` passes one that raises on a missing
                row; ``first()`` passes one that returns ``None`` instead.
            fast: Optional zero-arg coroutine factory resolving the row when
                nothing is chained, preserving ``Model.get``'s fast path; it is
                dropped once a chaining method (``only``/``select_related``/…)
                is applied.

        Returns:
            None
        """
        self._queryset = queryset
        self._fast = fast
        self._resolver = resolver

    def _chain(self, queryset: QuerySet) -> QuerySetSingle[_SingleT]:
        """Wrap ``queryset`` keeping this result's resolver (drops the fast path).

        Args:
            queryset: The further-narrowed query set to wrap.

        Returns:
            A new ``QuerySetSingle`` over ``queryset``.
        """
        return QuerySetSingle(queryset, resolver=self._resolver)

    def prefetch_related(self, *specs: Any) -> QuerySetSingle[_SingleT]:
        """Return a single-row result that also prefetches the given relations.

        Args:
            *specs: Prefetch specifications describing relations to load.

        Returns:
            A new ``QuerySetSingle`` carrying the prefetch specs.
        """
        return self._chain(self._queryset.prefetch_related(*specs))

    def select_related(self, *relations: str) -> QuerySetSingle[_SingleT]:
        """Return a single-row result that also eager-loads forward relations.

        Args:
            *relations: Forward relation names to join and load.

        Returns:
            A new ``QuerySetSingle`` carrying the selected relations.
        """
        return self._chain(self._queryset.select_related(*relations))

    def using_db(self, connection: Any) -> QuerySetSingle[_SingleT]:
        """Return a single-row result bound to the given connection.

        Args:
            connection: The connection name (or object) to run the query on.

        Returns:
            A new ``QuerySetSingle`` bound to the connection.
        """
        return self._chain(self._queryset.using_db(connection))

    def only(self, *fields: str) -> QuerySetSingle[_SingleT]:
        """Return a single-row result restricted to the given columns.

        Args:
            *fields: Field names to load (as in ``first().only(...)``).

        Returns:
            A new ``QuerySetSingle`` projecting only ``fields``.
        """
        return self._chain(self._queryset.only(*fields))

    async def values(self, *fields: str, **aliases: str) -> dict[str, Any] | None:
        """Resolve the single row as a dict of the requested columns.

        Args:
            *fields: Field names/paths to select (as in ``first().values``).
            **aliases: ``output_name=field_path`` pairs.

        Returns:
            The row as a dict, or ``None`` when there is no match.
        """
        rows = await self._queryset.limit(1).values(*fields, **aliases)
        return rows[0] if rows else None

    async def values_list(self, *fields: str, flat: bool = False) -> tuple[Any, ...] | Any | None:
        """Resolve the single row as a tuple (or scalar when ``flat=True``).

        Args:
            *fields: Field names/paths to select.
            flat: When ``True`` return the single field's scalar value.

        Returns:
            The row as a tuple/scalar, or ``None`` when there is no match.
        """
        rows = await self._queryset.limit(1).values_list(*fields, flat=flat)
        return rows[0] if rows else None

    def __await__(self) -> Generator[Any, None, _SingleT]:
        """Resolve to the single matching instance (or ``None`` for ``first()``).

        Uses the fast path when nothing is chained; otherwise the configured
        resolver (``first()``), falling back to ``get()`` semantics.

        Returns:
            The single matching model instance, or ``None`` for ``first()``.
        """
        if self._fast is not None:
            return self._fast().__await__()
        return self._resolver(self._queryset).__await__()

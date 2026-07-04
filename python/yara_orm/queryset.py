"""Lazy, chainable query construction.

A :class:`QuerySet` records filters (incl. ``Q`` trees), ordering, limits,
annotations and prefetches, touching the database only when awaited or when a
terminal coroutine (``get``, ``count`` ...) runs.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from . import registry
from .aggregations import Aggregate
from .connection import get_dialect, get_executor
from .exceptions import FieldError, UnSupportedError
from .expressions import Expression
from .functions import Function

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Generator

    from .connection import BaseDBAsyncClient
    from .dialects import BaseDialect
    from .fields import Field
    from .models import MetaInfo, Model
    from .prefetch import Prefetch
    from .relations import RelationInfo

#: The model class a queryset yields; every chain method preserves it, so
#: ``await Call.filter(...)`` reveals ``list[Call]`` (not a bare ``Model``).
ModelT = TypeVar("ModelT", bound="Model")


def _like_escape(value: Any) -> str:
    r"""Escape LIKE/ILIKE metacharacters in a user-supplied value.

    Pattern lookups (``contains``/``startswith``/``iexact``/...) wrap the raw
    value in wildcards, so any ``%``/``_`` inside it must match literally —
    otherwise user input silently acts as a wildcard. Paired with an
    ``ESCAPE '\\'`` clause on the rendered comparison.

    Args:
        value: The raw lookup value.

    Returns:
        The value with ``\\``, ``%`` and ``_`` backslash-escaped.
    """
    return str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# op -> (sql operator, pattern builder or None). A pattern builder turns the
# value into a LIKE/ILIKE pattern (bound as plain text, wildcards escaped);
# None binds via to_db.
_OPERATORS = {
    "exact": ("=", None),
    "not": ("!=", None),
    "gt": (">", None),
    "gte": (">=", None),
    "lt": ("<", None),
    "lte": ("<=", None),
    "contains": ("LIKE", lambda v: f"%{_like_escape(v)}%"),
    "icontains": ("ILIKE", lambda v: f"%{_like_escape(v)}%"),
    "startswith": ("LIKE", lambda v: f"{_like_escape(v)}%"),
    "istartswith": ("ILIKE", lambda v: f"{_like_escape(v)}%"),
    "endswith": ("LIKE", lambda v: f"%{_like_escape(v)}"),
    "iendswith": ("ILIKE", lambda v: f"%{_like_escape(v)}"),
    # Case-insensitive exact match: ILIKE with no wildcards in the bound value.
    "iexact": ("ILIKE", _like_escape),
}

# Date/time part lookups, e.g. ``created_at__year=2024`` (rendered per dialect).
_DATE_PARTS = frozenset(
    {"year", "quarter", "month", "week", "day", "hour", "minute", "second", "microsecond"}
)
# Regex lookups, e.g. ``name__regex=r"^A"`` (rendered per dialect operator). The
# ``posix_regex``/``iposix_regex`` are accepted spellings; they are aliases.
_REGEX_OPS = frozenset({"regex", "iregex", "posix_regex", "iposix_regex"})
# Field kinds whose column is already textual, so a LIKE/ILIKE pattern lookup
# binds against it directly; any other kind is CAST to text first.
_TEXT_KINDS = frozenset({"varchar", "text"})
# Lookups handled by dedicated branches rather than the ``_OPERATORS`` table.
_SPECIAL_OPS = frozenset({"in", "not_in", "isnull", "not_isnull", "range", "search", "date"})
# Every recognized trailing lookup suffix.
_LOOKUPS = frozenset(_OPERATORS) | _DATE_PARTS | _REGEX_OPS | _SPECIAL_OPS


_relations_mod: Any = None


def _rel() -> Any:
    """Return the ``relations`` module, imported lazily and memoised.

    ``relations`` imports ``queryset`` at module load, so ``queryset`` cannot
    import it at top level. By the time any query compiles at runtime the module
    is fully loaded; this accessor pays the import machinery once instead of on
    every lookup/join/relation compilation (the compile hot path re-ran a
    ``from .relations import ...`` statement per condition).

    Returns:
        The imported ``relations`` module.
    """
    global _relations_mod
    if _relations_mod is None:
        from . import relations

        _relations_mod = relations
    return _relations_mod


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


class QuerySet(Generic[ModelT]):
    """Lazy, chainable builder that compiles and executes SQL queries.

    Generic over the model it yields: chain methods return
    ``QuerySet[ModelT]`` and terminals resolve to ``ModelT`` /
    ``list[ModelT]``, so results are typed as the concrete model.
    """

    def __init__(self, model: type[ModelT]) -> None:
        """Initialize an empty query set bound to a model.

        Args:
            model: The model class whose table this query set targets.

        Returns:
            None
        """
        self.model = model
        self._conditions: list[Q] = []  # AND-combined at the top level
        # Each entry is one filter()/exclude() call over annotations: a group of
        # (annotation, op, value) lookups ANDed together, negated as a whole
        # when it came from exclude() (NOT (a AND b), per De Morgan).
        self._having: list[tuple[list[tuple[str, str, Any]], bool]] = []
        self._order: list[tuple[str, bool]] = []
        self._limit: int | None = None
        self._offset: int | None = None
        self._annotations: dict[str, Any] = {}
        self._group_by: list[str] = []
        self._prefetch: list[str | Prefetch] = []
        self._select_related: list[str] = []
        self._distinct: bool = False
        self._for_update: bool = False
        self._for_update_nowait: bool = False
        self._for_update_skip_locked: bool = False
        self._for_update_of: tuple[str, ...] = ()
        self._only: tuple[str, ...] | None = None
        # The columns ``only()`` was explicitly given (without the auto-added
        # pk), so a Subquery can project exactly what the caller named.
        self._only_explicit: tuple[str, ...] | None = None
        self._defer: frozenset[str] = frozenset()
        # Per-relation column projections from ``only()``/``defer()`` paths
        # (``contact__properties``): a joined relation loads only/all-but these
        # columns and hydrates a partial related instance. Keyed by relation path.
        self._only_related: dict[str, tuple[str, ...]] = {}
        self._defer_related: dict[str, frozenset[str]] = {}
        self._using: str | BaseDBAsyncClient | None = None

    # -- cloning / chaining ----------------------------------------------
    def _clone(self) -> QuerySet[ModelT]:
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
        qs._only_explicit = self._only_explicit
        qs._defer = self._defer
        qs._only_related = dict(self._only_related)
        qs._defer_related = dict(self._defer_related)
        qs._using = self._using
        return qs

    def filter(self, *args: Q, **kwargs: Any) -> QuerySet[ModelT]:
        """Return a new query set narrowed by the given conditions.

        Args:
            *args: ``Q`` nodes ANDed into the query's conditions.
            **kwargs: Field lookups; lookups over annotations become ``HAVING``
                clauses while the rest become ``WHERE`` conditions.

        Returns:
            A cloned ``QuerySet`` with the added conditions.

        Raises:
            TypeError: When a positional argument is not a ``Q`` node.
        """
        for arg in args:
            if not isinstance(arg, Q):
                raise TypeError(
                    f"filter() positional arguments must be Q objects, got {type(arg).__name__}"
                )
        qs = self._clone()
        qs._conditions.extend(args)
        where_kw = {}
        for key, value in kwargs.items():
            base, op = _split_lookup(key)
            if base in qs._annotations:
                qs._having.append(([(base, op, value)], False))
            else:
                where_kw[key] = value
        if where_kw:
            qs._conditions.append(Q(**where_kw))
        return qs

    def exclude(self, *args: Q, **kwargs: Any) -> QuerySet[ModelT]:
        """Return a new query set excluding rows matching the conditions.

        Args:
            *args: ``Q`` nodes whose combined condition is negated.
            **kwargs: Field lookups whose combined condition is negated;
                lookups over annotations become negated ``HAVING`` clauses.

        Returns:
            A cloned ``QuerySet`` with the negated condition appended.

        Raises:
            TypeError: When a positional argument is not a ``Q`` node.
            FieldError: When annotation lookups are mixed with other conditions
                in the same call (the negation cannot be split soundly).
        """
        for arg in args:
            if not isinstance(arg, Q):
                raise TypeError(
                    f"exclude() positional arguments must be Q objects, got {type(arg).__name__}"
                )
        qs = self._clone()
        where_kw = {}
        having = []
        for key, value in kwargs.items():
            base, op = _split_lookup(key)
            if base in qs._annotations:
                having.append((base, op, value))
            else:
                where_kw[key] = value
        if having and (args or where_kw):
            # NOT(column AND annotation) cannot be split into a WHERE part and a
            # HAVING part without changing meaning (De Morgan), so reject it.
            raise FieldError(
                "exclude() cannot mix annotation lookups with other conditions in one call"
            )
        if having:
            # One exclude() call negates the conjunction of its lookups:
            # exclude(a, b) compiles to NOT (a AND b), not NOT a AND NOT b.
            qs._having.append((having, True))
        else:
            qs._conditions.append(~Q(*args, **where_kw))
        return qs

    def annotate(self, **annotations: Any) -> QuerySet[ModelT]:
        """Return a new query set with extra aggregate annotations.

        Annotations combine with ``select_related()`` (a single joined SELECT)
        and with ``only()``/``defer()`` (the annotation expressions are appended
        to the narrowed projection); each annotation value is set as an
        attribute on the resulting instances.

        Args:
            **annotations: Mapping of output name to aggregate expression.

        Returns:
            A cloned ``QuerySet`` carrying the additional annotations.
        """
        qs = self._clone()
        qs._annotations.update(annotations)
        return qs

    def group_by(self, *fields: str) -> QuerySet[ModelT]:
        """Return a new query set grouped by the given fields.

        Args:
            *fields: Field names to group the result rows by.

        Returns:
            A cloned ``QuerySet`` with the grouping fields appended.
        """
        qs = self._clone()
        qs._group_by.extend(fields)
        return qs

    def all(self) -> QuerySet[ModelT]:
        """Return a clone of this query set (a no-op chain terminator).

        Code often ends a chain with ``.all()`` (e.g.
        ``qs.filter(...).all()``); yara query sets are already awaitable, so this
        just returns a clone to keep those chains working unchanged.

        Returns:
            A cloned ``QuerySet`` equivalent to this one.
        """
        return self._clone()

    def prefetch_related(self, *specs: str | Prefetch) -> QuerySet[ModelT]:
        """Return a new query set that prefetches the given relations.

        Args:
            *specs: Prefetch specifications describing relations to load.

        Returns:
            A cloned ``QuerySet`` with the prefetch specs appended.
        """
        qs = self._clone()
        qs._prefetch.extend(specs)
        return qs

    def select_related(self, *relations: str) -> QuerySet[ModelT]:
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

    def order_by(self, *fields: str) -> QuerySet[ModelT]:
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

    def limit(self, value: int) -> QuerySet[ModelT]:
        """Return a new query set with a row limit applied.

        Args:
            value: Maximum number of rows to return.

        Returns:
            A cloned ``QuerySet`` with the limit set.
        """
        qs = self._clone()
        qs._limit = int(value)
        return qs

    def offset(self, value: int) -> QuerySet[ModelT]:
        """Return a new query set with a row offset applied.

        Args:
            value: Number of leading rows to skip.

        Returns:
            A cloned ``QuerySet`` with the offset set.
        """
        qs = self._clone()
        qs._offset = int(value)
        return qs

    def distinct(self) -> QuerySet[ModelT]:
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
    ) -> QuerySet[ModelT]:
        """Return a new query set that locks matched rows (``FOR UPDATE``).

        The lock is emitted on backends that support it (PostgreSQL, MySQL)
        and is a no-op on SQLite; it only takes effect inside a transaction.

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

    def using_db(self, connection_name: str | BaseDBAsyncClient) -> QuerySet[ModelT]:
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

    def _using_name(self) -> str | BaseDBAsyncClient | None:
        """Return the ``using`` value for dialect resolution.

        Both a connection name and a connection/executor object are meaningful
        to ``get_dialect``: statements execute on the bound object, so SQL must
        render for *its* dialect (a transaction wrapper carries the dialect of
        the connection it is pinned to).

        Returns:
            The bound connection name/object, or None when unbound.
        """
        return self._using

    def _resolve_related_field_path(self, path: str) -> tuple[str, str]:
        """Split a ``rel__...__col`` path into its relation path and column.

        Validates that every leading segment is a forward relation and that the
        final segment names a column on the resolved target model.

        Args:
            path: A dotted-underscore path traversing one or more forward
                relations to a column (``"contact__properties"``).

        Returns:
            A ``(relation_path, column_name)`` tuple.

        Raises:
            FieldError: When a leading segment is not a forward relation or the
                final segment is not a column on the target.
        """
        *rel_segs, col = path.split("__")
        meta = self.model._meta
        for seg in rel_segs:
            if seg not in meta.relations:
                raise FieldError(
                    f"only()/defer(): {path!r} is not a forward-relation path of "
                    f"{self.model.__name__}"
                )
            meta = meta.relations[seg].resolve_target()._meta
        meta.get_field(col)  # validates the final column exists on the target
        return "__".join(rel_segs), col

    def only(self, *fields: str) -> QuerySet[ModelT]:
        """Return a new query set selecting only the named columns.

        Instances come back partially populated; the primary key is always
        included. Reading a field that was not selected raises ``FieldError``.

        A ``rel__col`` path (``only("contact__properties")``) restricts a joined
        relation instead: the relation is loaded (like ``select_related``) but
        only its named column(s) are projected and the related instance is
        hydrated partially. Naming only related paths restricts the base model
        to its primary key.

        Args:
            *fields: Field names to load; a ``rel__col`` path restricts a
                forward relation's columns.

        Returns:
            A cloned ``QuerySet`` restricted to the named columns.
        """
        meta = self.model._meta
        base: list[str] = []
        related: dict[str, list[str]] = {}
        for name in fields:
            if "__" in name:
                relpath, col = self._resolve_related_field_path(name)
                related.setdefault(relpath, []).append(col)
            else:
                meta.get_field(name)
                base.append(name)
        pk_name = meta.pk_field.model_field_name
        names = tuple(dict.fromkeys((pk_name, *base)))  # pk first, de-duplicated
        qs = self._clone()
        qs._only = names
        qs._only_explicit = tuple(dict.fromkeys(base))
        qs._defer = frozenset()
        qs._only_related = {rp: tuple(dict.fromkeys(cols)) for rp, cols in related.items()}
        qs._defer_related = {}
        return qs

    def defer(self, *fields: str) -> QuerySet[ModelT]:
        """Return a new query set that omits the named columns.

        Instances come back without the deferred fields loaded; the primary key
        is never deferred. Reading a deferred field raises ``FieldError``.

        A ``rel__col`` path (``defer("contact__properties")``) defers a joined
        relation's column instead: the relation is loaded (like
        ``select_related``) with every column except the named one(s).

        Args:
            *fields: Field names to omit; a ``rel__col`` path defers a forward
                relation's column.

        Returns:
            A cloned ``QuerySet`` omitting the named columns.
        """
        meta = self.model._meta
        base: list[str] = []
        related: dict[str, list[str]] = {}
        for name in fields:
            if "__" in name:
                relpath, col = self._resolve_related_field_path(name)
                related.setdefault(relpath, []).append(col)
            else:
                meta.get_field(name)
                base.append(name)
        pk_name = meta.pk_field.model_field_name
        qs = self._clone()
        qs._defer = frozenset(f for f in base if f != pk_name)
        qs._only = None
        qs._only_explicit = None
        qs._defer_related = {rp: frozenset(cols) for rp, cols in related.items()}
        qs._only_related = {}
        return qs

    def __getitem__(self, item: slice | int) -> QuerySet[ModelT] | Any:
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
            # Compose relative to any window already set, so ``qs[2:5][1:2]``
            # and ``qs.offset(5)[:3]`` slice the current results rather than
            # restarting from the table. An empty/inverted window clamps to
            # ``LIMIT 0`` (never a negative limit).
            qs._offset = ((self._offset or 0) + start) or None
            window = None if item.stop is None else max(item.stop - start, 0)
            if self._limit is not None:
                remaining = max(self._limit - start, 0)
                window = remaining if window is None else min(window, remaining)
            qs._limit = window
            return qs
        if isinstance(item, int):
            if item < 0:
                raise ValueError("negative indexing is not supported")

            async def _get_index() -> ModelT:
                # Index through the slice path so it composes with an existing
                # offset/limit (``qs[10:][3]`` fetches absolute row 13).
                rows = await self[item : item + 1]._fetch()
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

    @staticmethod
    def _forward_join(
        cur_table: str, cur_meta: MetaInfo, info: RelationInfo, dialect: BaseDialect, alias: str
    ) -> tuple[MetaInfo, str]:
        """Build the ``LEFT JOIN`` from ``cur_table`` across a forward relation.

        Shared by the join-based relation resolvers (``_resolve_column``,
        ``_add_relation_join``) so the forward-FK join shape is defined once.

        Args:
            cur_table: The already-quoted table/alias the join starts from.
            cur_meta: The metadata of the model owning ``info``.
            info: The forward ``RelationInfo`` to traverse.
            dialect: The SQL dialect providing identifier quoting.
            alias: The already-quoted alias for the joined table. Aliasing per
                relation path keeps two paths to the same table — or a self
                relation — from colliding as duplicate unaliased joins.

        Returns:
            A ``(target_meta, join_sql)`` tuple; the joined table's columns
            must be referenced through ``alias``.
        """
        q = dialect.quote
        tmeta = info.resolve_target()._meta
        src = q(cur_meta.get_field(info.source_attr).db_column)
        join = (
            f" LEFT JOIN {q(tmeta.table)} AS {alias} "
            f"ON {cur_table}.{src} = {alias}.{q(tmeta.pk_field.db_column)}"
        )
        return tmeta, join

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
        # Lazily imported (breaks the queryset <-> relations import cycle);
        # memoised so this is not a per-lookup import statement.
        rel = _rel()
        M2MDescriptor, ReverseFKDescriptor = rel.M2MDescriptor, rel.ReverseFKDescriptor

        meta = self.model._meta
        base, op = _split_lookup(key)

        # A multi-segment path is either a JSON-column key path
        # (``data__key``/``data__a__b`` on a JSONField), a JSON ``__filter`` dict,
        # or a relation traversal (``author__name``) compiled as a subquery.
        if "__" in base:
            first = meta.fields.get(base.split("__", 1)[0])
            if first is not None and first.field_kind == "json":
                # ``data__filter={"a__b__op": v, ...}`` — nested JSON-path
                # conditions ANDed together (Tortoise JSON ``__filter``).
                if op == "exact" and base.endswith("__filter") and isinstance(value, dict):
                    return self._compile_json_filter(base[: -len("__filter")], value, dialect, idx)
                return self._compile_json_path(base, op, value, dialect, idx)
            return self._compile_relation_lookup(base, op, value, dialect, idx)

        descriptor = getattr(self.model, base, None)
        if base in meta.m2m or isinstance(descriptor, M2MDescriptor):
            return self._compile_m2m_lookup(base, op, value, dialect, idx)

        # A bare reverse-FK related_name with an ``isnull`` test asks whether a
        # related row exists (``Portfolio.filter(alerts__isnull=True)`` = "has no
        # alerts"); compile it as a correlated [NOT] EXISTS.
        if isinstance(descriptor, ReverseFKDescriptor) and op in ("isnull", "not_isnull"):
            return self._compile_reverse_exists(base, op, value, dialect, idx)

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
        if value is None and op in ("exact", "not"):
            # ``field=None`` / ``field__not=None`` mean NULL identity, not a bind:
            # ``col = NULL`` / ``col != NULL`` are always UNKNOWN and match no rows,
            # so compile them to ``IS NULL`` / ``IS NOT NULL`` (Django/Tortoise
            # semantics). Without this, ``get_or_create(field=None)`` never matches
            # and re-inserts a duplicate NULL row on every call.
            return (f"{col} IS NULL" if op == "exact" else f"{col} IS NOT NULL"), [], idx
        if op == "contains" and field is not None and field.field_kind == "json":
            # JSON ``__contains`` is structural containment (``@>``), not a text
            # LIKE: it matches an object subset, an array element, or an
            # array-of-objects subset. The value is bound as JSON text and cast.
            return (
                dialect.json_contains_sql(col, dialect.placeholder(idx)),
                [json.dumps(value)],
                idx + 1,
            )
        if hasattr(value, "as_sql"):
            # A Subquery / RawSQL / Case used as the comparison value.
            vparams: list[Any] = []
            vsql, idx = value.as_sql(self, dialect, {}, vparams, idx)
            if op in ("in", "not_in"):
                membership = "NOT IN" if op == "not_in" else "IN"
                return f"{col} {membership} {vsql}", vparams, idx
            sql_op = _OPERATORS[op][0]
            if sql_op == "ILIKE":
                sql_op = dialect.ilike
            elif sql_op == "LIKE":
                sql_op = dialect.like
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
            clause = f"{col} {membership} ({', '.join(holes)})"
            if op == "not_in":
                # ``NOT IN`` drops NULL rows (``NULL NOT IN (...)`` is UNKNOWN);
                # keep them, matching Tortoise, so a nullable column's NULL rows
                # are not silently excluded from a negative filter.
                clause = f"({clause} OR {col} IS NULL)"
            return clause, params, idx
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
            # Rendered by the dialect: an infix operator on PostgreSQL, a
            # REGEXP_LIKE(...) call on MySQL; raises where unsupported (SQLite).
            return dialect.regex_sql(op, col, dialect.placeholder(idx)), [value], idx + 1
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
            # ILIKE is PostgreSQL-only; SQLite's and MySQL's LIKE are already
            # case-insensitive.
            sql_op = dialect.ilike
        elif sql_op == "LIKE":
            # Case-*sensitive* pattern lookups: MySQL's collation makes plain
            # LIKE case-insensitive, so its dialect spells this LIKE BINARY.
            sql_op = dialect.like
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
        if pattern is not None:
            # LIKE/ILIKE need a text operand: a non-text column (uuid/int/...)
            # is cast to text first, mirroring how Tortoise compiled these so an
            # ``__icontains`` on e.g. a UUID column doesn't raise 'operator does
            # not exist: uuid ~~* text'.
            if field is not None and field.field_kind not in _TEXT_KINDS:
                col = dialect.cast_text(col)
            # The pattern builder backslash-escapes %/_ in the value; the
            # dialect's ESCAPE clause makes every backend honour that escaping
            # (MySQL spells the literal differently).
            return f"{col} {sql_op} {placeholder}{dialect.like_escape}", [pattern(value)], idx
        clause = f"{col} {sql_op} {placeholder}"
        if op == "not":
            # ``!=`` drops NULL rows (``NULL != x`` is UNKNOWN); keep them,
            # matching Tortoise, so a nullable column's NULL rows survive a
            # ``__not`` filter (a NULL default is not silently excluded).
            clause = f"({clause} OR {col} IS NULL)"
        return clause, [coerce(value)], idx

    def _compile_reverse_exists(
        self,
        base: str,
        op: str,
        value: Any,
        dialect: BaseDialect,
        idx: int,
    ) -> tuple[str, list[Any], int]:
        """Compile a reverse-FK ``isnull`` test into a correlated EXISTS.

        Args:
            base: The reverse-relation ``related_name`` on this model.
            op: ``"isnull"`` or ``"not_isnull"``.
            value: The truthiness selecting presence vs absence of a related row.
            dialect: The SQL dialect providing quoting and placeholders.
            idx: The next available bind-parameter index (unchanged; no binds).

        Returns:
            A ``(sql, params, next_index)`` tuple.
        """
        descriptor = getattr(self.model, base)
        # References are qualified (module.Name); get_model handles both forms.
        source = registry.get_model(descriptor.source_reference)
        smeta = source._meta
        q = dialect.quote
        meta = self.model._meta
        outer = f"{q(meta.table)}.{q(meta.pk_field.db_column)}"
        child = q(smeta.table)
        sub = f"SELECT 1 FROM {child} WHERE {child}.{q(descriptor.source_attr)} = {outer}"
        # isnull=True / not_isnull=False -> no related row -> NOT EXISTS.
        absent = (op == "isnull") == bool(value)
        keyword = "NOT EXISTS" if absent else "EXISTS"
        return f"{keyword} ({sub})", [], idx

    def _compile_json_path(
        self,
        base: str,
        op: str,
        value: Any,
        dialect: BaseDialect,
        idx: int,
    ) -> tuple[str, list[Any], int]:
        """Compile a JSON key-path lookup (``data__key``/``data__a__b``).

        The leading segment names a ``JSONField`` and the remaining segments are
        object keys extracted (as text) via the dialect's JSON operator, then
        compared with the usual field operators (``exact``/``contains``/
        ``isnull``/…).

        Args:
            base: The ``jsonfield__key...`` path.
            op: The trailing lookup operator.
            value: The comparison value.
            dialect: The SQL dialect providing quoting and placeholders.
            idx: The next available bind-parameter index.

        Returns:
            A ``(sql, params, next_index)`` tuple.
        """
        segments = base.split("__")
        field = self.model._meta.get_field(segments[0])
        col = self._qualified(dialect, field)
        expr = dialect.json_extract_sql(col, segments[1:])
        # The extracted value has no backing scalar field, so bind the value
        # as-is (text comparison) — pass ``field=None``.
        return self._compile_field_op(expr, None, op, value, dialect, idx)

    def _compile_json_filter(
        self,
        json_base: str,
        conditions: dict[str, Any],
        dialect: BaseDialect,
        idx: int,
    ) -> tuple[str, list[Any], int]:
        """Compile a JSON ``__filter`` dict into ANDed key-path conditions.

        Each ``"key__nested__op": value`` entry becomes a JSON key-path lookup on
        the column (``json_base->...->>'key' <op> value``); the entries are ANDed
        (Tortoise's JSON ``__filter``). E.g. ``audit_log_meta__filter={
        "status__not": "resolved", "task_name__icontains": part}``.

        Args:
            json_base: The JSON column path the filter applies to.
            conditions: ``path__op -> value`` entries to AND together.
            dialect: The SQL dialect providing quoting and placeholders.
            idx: The next available bind-parameter index.

        Returns:
            A ``(sql, params, next_index)`` tuple.
        """
        clauses, params = [], []
        for key, value in conditions.items():
            sql, p, idx = self._compile_lookup(f"{json_base}__{key}", value, dialect, idx)
            clauses.append(f"({sql})")
            params.extend(p)
        if not clauses:
            return "1 = 1", [], idx  # an empty filter matches everything
        return " AND ".join(clauses), params, idx

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
        # Lazily imported (breaks the queryset <-> relations import cycle).
        rel = _rel()
        M2MDescriptor, ReverseFKDescriptor = rel.M2MDescriptor, rel.ReverseFKDescriptor

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
            source = registry.get_model(descriptor.source_reference)
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
        if op in ("isnull", "not_isnull"):
            # ``tags__isnull=True`` asks "has no linked row": membership in the
            # through table decides presence; the value picks the polarity.
            absent = (op == "isnull") == bool(value)
            membership = "NOT IN" if absent else "IN"
            inner = f"SELECT {through}.{q(near)} FROM {through}"
            return f"{table}.{pk} {membership} ({inner})", [], idx
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
                # Each top-level condition is AND-joined with the others, so it
                # must be parenthesised: an OR-group (``Q(a) | Q(b)``) or a
                # chained ``.filter()`` compiles to ``a OR b`` and, left bare,
                # the tighter-binding AND would swallow the following conditions
                # into its right branch (``a OR (b AND c)``) — corrupting the
                # WHERE. Mirror the wrapping already done for child nodes.
                parts.append(f"({sub})")
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

    def _grouped_order_sql(self, dialect: BaseDialect, joins: dict[str, str]) -> str:
        """Build ``ORDER BY`` for a grouped/annotated SELECT, reusing its joins.

        A grouped query already LEFT JOINs any forward-relation group-by column,
        so ordering references that joined column (via ``_resolve_column``, which
        shares the ``joins`` dict) instead of a correlated subquery. The subquery
        form would reference the ungrouped foreign-key column and be rejected
        under ``GROUP BY`` on PostgreSQL (42803).

        Args:
            dialect: The SQL dialect providing quoting.
            joins: The grouped query's join map, extended in place if an ordering
                relation path needs a join not already present.

        Returns:
            The ``ORDER BY`` clause, or an empty string when no ordering is set.
        """
        order = self._order or self.model._meta.ordering
        if not order:
            return ""
        meta = self.model._meta
        table = dialect.quote(meta.table)
        parts = []
        for name, descending in order:
            if name == "?":
                parts.append(dialect.random_function)
                continue
            if name in self._annotations:
                ref = dialect.quote(name)
            elif "__" in name:
                ref = self._resolve_column(name, dialect, joins)
            else:
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
        # Alias every table *inside* the subquery. For a self-relation the target
        # table equals the base table, so an unaliased ``target.pk = base.fk``
        # would bind both sides to the inner row and stop correlating to the outer
        # row (arbitrary ordering); a distinct alias keeps the correlation to the
        # outer ``meta.table``.
        cur_alias = "_ord0"
        from_table = f"{q(cur_meta.table)} AS {q(cur_alias)}"
        correlation = f"{q(cur_alias)}.{q(cur_meta.pk_field.db_column)} = {q(meta.table)}.{base_fk}"
        joins = ""
        for depth, seg in enumerate(segments[1:-1], start=1):
            info = cur_meta.relations.get(seg)
            if info is None:
                raise FieldError(
                    f"Cannot order by relation path {name!r}: {seg!r} is not a forward relation"
                )
            next_meta = info.resolve_target()._meta
            next_alias = f"_ord{depth}"
            src = q(cur_meta.get_field(info.source_attr).db_column)
            joins += (
                f" JOIN {q(next_meta.table)} AS {q(next_alias)} ON {q(cur_alias)}.{src} = "
                f"{q(next_alias)}.{q(next_meta.pk_field.db_column)}"
            )
            cur_meta, cur_alias = next_meta, next_alias
        final_col = f"{q(cur_alias)}.{q(cur_meta.get_field(segments[-1]).db_column)}"
        return f"(SELECT {final_col} FROM {from_table}{joins} WHERE {correlation})"  # noqa: S608

    def _tail_sql(self, dialect: BaseDialect) -> str:
        """Build the trailing ``LIMIT`` / ``OFFSET`` clause.

        Args:
            dialect: The active dialect (some require a ``LIMIT`` before
                ``OFFSET``).

        Returns:
            The ``LIMIT``/``OFFSET`` fragment, or an empty string when neither
            is set.
        """
        tail = ""
        limit = self._limit
        if limit is None and self._offset is not None and dialect.offset_requires_limit:
            # SQLite and MySQL reject ``OFFSET`` without a preceding ``LIMIT``;
            # each dialect supplies its own "no limit" sentinel (-1 on SQLite,
            # the max row count on MySQL), so an offset-only slice (``qs[3:]``)
            # stays valid. PostgreSQL accepts a bare ``OFFSET`` and skips this.
            limit = dialect.no_limit
        if limit is not None:
            tail += f" LIMIT {int(limit)}"
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
        if not (self._for_update and dialect.supports_for_update):
            return ""
        parts = ["FOR UPDATE"]
        if self._for_update_of and dialect.supports_for_update_of:
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
    ) -> tuple[MetaInfo, str]:
        """Register the join(s) needed to aggregate across a relation.

        Args:
            rel: The relation name to join through.
            dialect: The SQL dialect providing identifier quoting.
            joins: Mapping of join key to join SQL, mutated in place.

        Returns:
            A ``(target_meta, alias)`` tuple; ``alias`` is the already-quoted
            alias the joined table's columns must be referenced through.
        """
        # Lazily imported (breaks the queryset <-> relations import cycle).
        _relmod = _rel()
        M2MDescriptor, ReverseFKDescriptor = _relmod.M2MDescriptor, _relmod.ReverseFKDescriptor

        q = dialect.quote
        meta = self.model._meta
        table = q(meta.table)
        pk = q(meta.pk_field.db_column)
        alias = q(rel)

        if rel in meta.relations:
            tmeta, joins[rel] = self._forward_join(table, meta, meta.relations[rel], dialect, alias)
            return tmeta, alias

        descriptor = getattr(self.model, rel, None)
        if isinstance(descriptor, ReverseFKDescriptor):
            source = registry.get_model(descriptor.source_reference)
            smeta = source._meta
            joins[rel] = (
                f" LEFT JOIN {q(smeta.table)} AS {alias} ON {alias}."
                f"{q(descriptor.source_attr)} = {table}.{pk}"
            )
            return smeta, alias
        if isinstance(descriptor, M2MDescriptor):
            info = descriptor.info
            info.finalize()
            if descriptor.reverse:
                near, far = info.forward_key, info.backward_key
                tmeta = info.owner._meta
            else:
                near, far = info.backward_key, info.forward_key
                tmeta = info.resolve_target()._meta
            through_alias = q(rel + "#t")
            joins[rel + "#t"] = (
                f" LEFT JOIN {q(info.through)} AS {through_alias} "
                f"ON {through_alias}.{q(near)} = {table}.{pk}"
            )
            joins[rel] = (
                f" LEFT JOIN {q(tmeta.table)} AS {alias} ON {alias}."
                f"{q(tmeta.pk_field.db_column)} = {through_alias}.{q(far)}"
            )
            return tmeta, alias
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
        filt = getattr(agg, "filter", None)
        # Dialects without FILTER (MySQL) get the equivalent
        # ``AGG(CASE WHEN <q> THEN <col> END)`` — aggregates ignore NULLs. The
        # filter is compiled *first* there because it precedes the column in
        # the SQL text, and MySQL's `?` placeholders bind strictly by position.
        case_form = filt is not None and not dialect.supports_aggregate_filter
        case_sql = ""
        if case_form:
            case_sql, fparams, idx = self._compile_q(filt, dialect, idx)
            params.extend(fparams)
        if isinstance(inner, Expression):
            inner_sql, idx = inner.resolve(resolve, dialect, params, idx)
        elif hasattr(inner, "as_sql"):
            inner_sql, idx = inner.as_sql(self, dialect, joins, params, idx)
        else:
            inner_sql = resolve(inner)
        distinct = "DISTINCT " if getattr(agg, "distinct", False) else ""
        if case_form and case_sql:
            counted = "1" if inner_sql == "*" else inner_sql
            return f"{agg.function}({distinct}CASE WHEN {case_sql} THEN {counted} END)", idx
        sql = f"{agg.function}({distinct}{inner_sql})"
        if filt is not None and not case_form:
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
            tmeta, alias = self._add_relation_join(field, dialect, joins)
            return f"{alias}.{q(tmeta.pk_field.db_column)}"

        segments = field.split("__")
        cur_meta = meta
        cur_table = table
        chain = ""
        for i, seg in enumerate(segments):
            last = i == len(segments) - 1
            info = cur_meta.relations.get(seg)
            if info is not None:
                # Forward relation: chain a LEFT JOIN to its target table,
                # aliased by the relation path so two paths hitting the same
                # table (or a self relation) stay distinct joins.
                chain = f"{chain}__{seg}" if chain else seg
                alias = q(chain)
                tmeta, joins[chain] = self._forward_join(cur_table, cur_meta, info, dialect, alias)
                cur_meta, cur_table = tmeta, alias
                if last:
                    return f"{cur_table}.{q(cur_meta.pk_field.db_column)}"
            elif last:
                return f"{cur_table}.{q(cur_meta.get_field(seg).db_column)}"
            elif cur_meta is meta and len(segments) == 2:
                # A single reverse-FK / M2M hop, e.g. ``tags__name``.
                tmeta, alias = self._add_relation_join(seg, dialect, joins)
                return f"{alias}.{q(tmeta.get_field(segments[1]).db_column)}"
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
        for group, negated in self._having:
            # Render each annotation expression, then reuse the same comparison
            # compiler as WHERE so every lookup (in/range/isnull/icontains/date
            # parts/...) works against an aggregate, with dialect-correct
            # operators and bound (never spliced) values.
            parts = []
            for name, op, value in group:
                expr, idx = self._aggregate_expr(
                    self._annotations[name], dialect, joins, params, idx
                )
                clause, p, idx = self._compile_field_op(expr, None, op, value, dialect, idx)
                parts.append(clause)
                params.extend(p)
            clause = " AND ".join(parts)
            if negated:
                # An ``exclude()`` group: negate the conjunction as a whole.
                clause = f"NOT ({clause})"
            elif len(parts) > 1:
                clause = f"({clause})"
            clauses.append(clause)
        having = (" HAVING " + " AND ".join(clauses)) if clauses else ""
        return having, params, idx

    # -- execution --------------------------------------------------------
    def _has_aggregate_annotations(self) -> bool:
        """Report whether any annotation is an aggregate expression.

        Decides whether an annotated ``select_related`` query needs a
        ``GROUP BY``. Only real :class:`~yara_orm.aggregations.Aggregate`
        instances count: a ``RawSQL`` fragment is opaque, so a raw aggregate
        (as opposed to e.g. a window function) is not detected — use the
        aggregate classes (``Count``/``Sum``/...) for grouped shapes.

        Returns:
            ``True`` when at least one annotation is an ``Aggregate``.
        """
        return any(isinstance(agg, Aggregate) for agg in self._annotations.values())

    def _check_annotation_collisions(self) -> None:
        """Reject annotation names shadowing model fields under only()/defer().

        With a narrowed projection, an annotation reusing a column name would
        either overwrite the loaded value or silently "un-defer" the column
        with a non-column value, so the combination is ambiguous and raises.

        Raises:
            FieldError: When ``only()``/``defer()`` is active and an annotation
                name equals a model field name.
        """
        if self._only is None and not self._defer:
            return
        meta = self.model._meta
        for name in self._annotations:
            if name in meta.fields:
                raise FieldError(
                    f"annotate() name {name!r} collides with a field of "
                    f"{self.model.__name__}; rename the annotation when "
                    "using only()/defer()"
                )

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
        tail = f"{self._order_sql(dialect)}{self._tail_sql(dialect)}{self._lock_sql(dialect)}"
        if self._only is not None or self._defer:
            sel = self._selected_fields()
            cols = ", ".join(dialect.quote(f.db_column) for f in sel)
            prefix = self._distinct_prefix(f"SELECT {cols} FROM {dialect.quote(meta.table)}")
            return f"{prefix}{where}{tail}", params, sel
        prefix = self._distinct_prefix(meta.select_prefix)
        return f"{prefix}{where}{tail}", params, None

    def _projection_select_sql(
        self, field_paths: tuple[str, ...], dialect: BaseDialect, start: int = 1
    ) -> tuple[str, list[Any], None]:
        """Build a SELECT over the given column paths, for use as a subquery.

        Mirrors :meth:`_fetch_columns` but renders without executing and
        continues an outer query's placeholder numbering, so a ``values_list``
        projection can be embedded in a :class:`~yara_orm.Subquery` (e.g.
        ``id__in=Subquery(qs.values_list("col", flat=True))``).

        Args:
            field_paths: The field names/paths to select (one for an ``IN``
                membership subquery).
            dialect: The SQL dialect providing quoting and placeholders.
            start: The first bind-parameter index (the outer query's next slot).

        Returns:
            A ``(sql, params, None)`` tuple matching :meth:`_plain_select_sql`.

        Raises:
            FieldError: When the projection is grouped/annotated, which has no
                single-column form to embed as a scalar/membership subquery.
        """
        if self._annotations or self._group_by:
            raise FieldError(
                "a grouped/annotated values()/values_list() cannot be used as a Subquery"
            )
        meta = self.model._meta
        meta.compile(dialect)
        joins: dict[str, str] = {}
        resolved = [self._resolve_column(p, dialect, joins) for p in field_paths]
        cols = ", ".join(resolved)
        where, params, _ = self._compile_conditions(dialect, start=start)
        if len(resolved) == 1:
            # A single-column projection feeds an IN / NOT IN membership test
            # (``id__in=Subquery(qs.values_list("col", flat=True))``). Excluding
            # NULLs of that column keeps NOT IN correct — a NULL in the set makes
            # ``x NOT IN (...)`` evaluate to UNKNOWN, dropping every row, so
            # ``exclude(col__in=Subquery(...))`` would otherwise return nothing —
            # without changing IN results (a NULL never matches anyway).
            guard = f"{resolved[0]} IS NOT NULL"
            where = f"{where} AND {guard}" if where else f" WHERE {guard}"
        table = dialect.quote(meta.table)
        distinct = "DISTINCT " if self._distinct else ""
        sql = (
            f"SELECT {distinct}{cols} FROM {table}{''.join(joins.values())}{where}"
            f"{self._order_sql(dialect)}{self._tail_sql(dialect)}{self._lock_sql(dialect)}"
        )
        return sql, params, None

    async def _fetch(self) -> list[ModelT]:
        """Execute the query and build model instances from the rows.

        Returns:
            A list of model instances, with any requested relations prefetched.
        """
        if self._select_related or self._only_related or self._defer_related:
            # Handles annotations too: the join plan's columns are followed by
            # the annotation expressions in one SELECT.
            return await self._fetch_select_related()
        if self._annotations:
            return await self._fetch_annotated()
        dialect = get_dialect(self.model, using=self._using_name())
        engine = get_executor(self.model, using=self._using)
        sql, params, sel = self._plain_select_sql(dialect)
        rows = await engine.fetch_rows(sql, params)
        if sel is not None:
            instances = self.model._from_db_rows_fields(rows, sel)
        else:
            instances = self.model._from_db_rows(rows)
        if self._prefetch:
            # Deferred: breaks the queryset <-> prefetch import cycle.
            from .prefetch import prefetch_instances

            await prefetch_instances(instances, self._prefetch)
        return instances

    def _related_projected_fields(self, path: str, tmeta: MetaInfo) -> tuple[list[Field], bool]:
        """Resolve the columns a joined relation projects, honouring only/defer.

        Args:
            path: The relation path being joined.
            tmeta: The target model's metadata.

        Returns:
            A ``(fields, partial)`` tuple: the projected ``Field`` objects in
            SELECT order, and whether the projection is a restricted subset (so
            the related instance is hydrated partially).
        """
        pk_name = tmeta.pk_field.model_field_name
        if path in self._only_related:
            names = dict.fromkeys((pk_name, *self._only_related[path]))  # pk first
            return [tmeta.get_field(n) for n in names], True
        if path in self._defer_related:
            omit = self._defer_related[path]
            kept = [
                f
                for f in tmeta.field_list
                if f.model_field_name == pk_name or f.model_field_name not in omit
            ]
            return kept, True
        return tmeta.field_list, False

    def _select_related_plan(
        self, dialect: BaseDialect
    ) -> tuple[str, list[Any], list[str], dict[str, dict[str, Any]], int]:
        """Build the ``select_related`` SELECT and its row-decoding plan.

        Shared by :meth:`_fetch_select_related` and
        :meth:`get_parameterized_sql` so both render the identical statement.
        Annotations combine with the join plan: their expressions are appended
        after the relation columns, and aggregate annotations (or a ``HAVING``
        filter) add a ``GROUP BY`` over the base pk plus each joined relation's
        pk; non-aggregate annotations (window functions, ``F`` arithmetic) add
        no grouping.

        Args:
            dialect: The SQL dialect providing quoting and placeholders.

        Returns:
            A ``(sql, params, order, nodes, ncols)`` tuple: the SELECT and its
            bound params, the relation paths in join order, the per-relation
            decode nodes and the base model's column count (annotation values
            trail the relation columns).

        Raises:
            UnSupportedError: With ``select_for_update()`` and ``annotate()``
                both set. The lock is rejected on *every* annotated shape:
                grouped/aggregate results cannot be locked, and even the
                ungrouped shape may carry a window function (e.g. via
                ``RawSQL``), which PostgreSQL also refuses to lock.
            FieldError: When an annotation name collides with a model field
                under an ``only()``/``defer()`` projection.
        """
        if self._annotations:
            if self._for_update:
                raise UnSupportedError(
                    "select_for_update() cannot be combined with annotate(): "
                    "FOR UPDATE is not allowed with GROUP BY/aggregates"
                )
            self._check_annotation_collisions()
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
            proj_fields, partial = self._related_projected_fields(path, tmeta)
            select.extend(f"{alias}.{q(f.db_column)}" for f in proj_fields)
            node = {
                "parent": parent,
                "seg": seg,
                "target": target,
                "offset": offset,
                "width": len(proj_fields),
                "fields": proj_fields,
                "partial": partial,
            }
            offset += node["width"]
            nodes[path] = node
            order.append(path)
            return node

        # Join every requested relation: explicit select_related paths plus any
        # relation referenced by an only()/defer() ``rel__col`` projection.
        for rel in (*self._select_related, *self._only_related, *self._defer_related):
            ensure_path(rel)
        # Annotation expressions follow the relation columns; their joins are
        # collected separately and de-duplicated against the plan's joins (an
        # annotation over an already-select_related relation would otherwise
        # emit the identical LEFT JOIN twice, colliding on the alias).
        select_params: list[Any] = []
        ann_joins: dict[str, str] = {}
        idx = 1
        for name, agg in self._annotations.items():
            expr, idx = self._aggregate_expr(agg, dialect, ann_joins, select_params, idx)
            select.append(f"{expr} AS {q(name)}")
        where, wparams, idx = self._compile_conditions(dialect, start=idx)
        having, hparams, _ = self._compile_having(dialect, idx, ann_joins)
        params = select_params + wparams + hparams
        extra_joins = "".join(j for key, j in ann_joins.items() if key not in nodes)
        group = ""
        if self._annotations and (self._having or self._has_aggregate_annotations()):
            # Aggregates (or a HAVING filter) need grouping. Group by the base
            # pk plus each joined relation's pk: the pk of a grouped table makes
            # its remaining selected columns functionally dependent (PostgreSQL's
            # rule; SQLite allows bare columns), so the wide join projection
            # stays selectable while a reverse-relation aggregate still
            # collapses to one row per base row.
            group_refs = [f"{table}.{q(meta.pk_field.db_column)}"]
            group_refs += [
                f"{q(path)}.{q(nodes[path]['target']._meta.pk_field.db_column)}" for path in order
            ]
            group = " GROUP BY " + ", ".join(group_refs)
        lock = self._lock_sql(dialect)
        if lock and joins and not self._for_update_of and dialect.supports_for_update_of:
            # PostgreSQL rejects FOR UPDATE on the nullable side of a LEFT
            # JOIN, so with joined relations lock only the base table's rows.
            # MariaDB has no FOR UPDATE OF (and doesn't need it — plain
            # FOR UPDATE over a join locks the matched rows), so it is skipped.
            lock = lock.replace("FOR UPDATE", f"FOR UPDATE OF {table}", 1)
        sql = (
            f"SELECT {', '.join(select)} FROM {table}{''.join(joins)}{extra_joins}{where}"
            f"{group}{having}{self._order_sql(dialect)}{self._tail_sql(dialect)}{lock}"
        )
        return sql, params, order, nodes, len(base_fields)

    async def _fetch_select_related(self) -> list[ModelT]:
        """Execute a query that joins and hydrates forward FK/O2O relations.

        Each selected relation is LEFT JOINed (aliased by relation name, so
        self-joins and repeated targets are unambiguous) and its columns are
        decoded into a related instance cached under the instance's prefetch
        slot — making the relation available synchronously. Annotation values
        trail the relation columns and are set as instance attributes.

        Returns:
            The model instances with each selected relation cached (and any
            annotation values attached).
        """
        dialect = get_dialect(self.model, using=self._using_name())
        engine = get_executor(self.model, using=self._using)
        sql, params, order, nodes, ncols = self._select_related_plan(dialect)
        base_sel = self._selected_fields() if (self._only is not None or self._defer) else None
        ann_base = ncols + sum(nodes[path]["width"] for path in order)
        # Trailing annotation columns: (column index, attribute name) pairs.
        ann_plan = [(ann_base + i, name) for i, name in enumerate(self._annotations)]
        # Per-node hydration plan, resolved once instead of per row: the column
        # slice, the hydrating classmethod and (for a partial only()/defer()
        # projection) the field subset it decodes with.
        plan: list[tuple[str, str | None, str, int, int, Any, list[Any] | None]] = []
        for path in order:
            node = nodes[path]
            start = node["offset"]
            partial = node["partial"]
            target = node["target"]
            plan.append(
                (
                    path,
                    node["parent"],
                    node["seg"],
                    start,
                    start + node["width"],
                    target._from_db_row_fields if partial else target._from_db_row,
                    node["fields"] if partial else None,
                )
            )
        rows = await engine.fetch_rows(sql, params)
        model = self.model
        from_row = model._from_db_row
        from_row_fields = model._from_db_row_fields
        instances = []
        if len(plan) == 1 and plan[0][1] is None:
            # Common case — a single direct relation: skip the per-row ``built``
            # path map and assign the prefetch slot in one dict display.
            _, _, seg, start, end, hydrate, sel = plan[0]
            for row in rows:
                obj = (
                    from_row_fields(row[:ncols], base_sel)
                    if base_sel is not None
                    else from_row(row[:ncols])
                )
                chunk = row[start:end]
                if not any(v is not None for v in chunk):
                    child = None
                elif sel is not None:
                    child = hydrate(chunk, sel)
                else:
                    child = hydrate(chunk)
                obj.__dict__["_prefetch"] = {seg: child}
                for col, name in ann_plan:
                    setattr(obj, name, row[col])
                instances.append(obj)
        else:
            for row in rows:
                obj = (
                    from_row_fields(row[:ncols], base_sel)
                    if base_sel is not None
                    else from_row(row[:ncols])
                )
                prefetch: dict[str, Model | None] = {}
                obj.__dict__["_prefetch"] = prefetch
                built: dict[str | None, Model | None] = {None: obj}
                for path, parent, seg, start, end, hydrate, sel in plan:
                    parent_inst = built[parent]
                    if parent_inst is None:
                        built[path] = None
                        continue
                    chunk = row[start:end]
                    if not any(v is not None for v in chunk):
                        child = None
                    elif sel is not None:
                        child = hydrate(chunk, sel)
                    else:
                        child = hydrate(chunk)
                    if parent is None:
                        prefetch[seg] = child
                    else:
                        pd = parent_inst.__dict__.get("_prefetch")
                        if pd is None:
                            pd = parent_inst.__dict__["_prefetch"] = {}
                        pd[seg] = child
                    built[path] = child
                for col, name in ann_plan:
                    setattr(obj, name, row[col])
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

        The base columns honour ``only()``/``defer()`` (the pk is always
        included, so the ``GROUP BY`` stays valid).

        Args:
            dialect: The SQL dialect providing quoting and placeholders.

        Returns:
            A ``(sql, params)`` tuple.

        Raises:
            UnSupportedError: With ``select_for_update()`` set; ``FOR UPDATE``
                is not allowed with GROUP BY/aggregates.
            FieldError: When an annotation name collides with a model field
                under an ``only()``/``defer()`` projection.
        """
        if self._for_update:
            raise UnSupportedError(
                "select_for_update() cannot be combined with annotate(): "
                "FOR UPDATE is not allowed with GROUP BY/aggregates"
            )
        self._check_annotation_collisions()
        meta = self.model._meta
        meta.compile(dialect)
        q = dialect.quote
        table = q(meta.table)
        joins: dict[str, str] = {}
        select = [f"{table}.{q(f.db_column)}" for f in self._selected_fields()]
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
            f"{self._order_sql(dialect)}{self._tail_sql(dialect)}"
        )
        return sql, params

    async def _fetch_annotated(self) -> list[ModelT]:
        """Execute an annotated query and attach annotations to instances.

        Under ``only()``/``defer()`` the base instance hydrates partially (the
        unselected columns stay deferred and raise on access).

        Returns:
            A list of model instances with each annotation value set as an
            attribute, with any requested relations prefetched.
        """
        dialect = get_dialect(self.model, using=self._using_name())
        engine = get_executor(self.model, using=self._using)
        sql, params = self._annotated_select_sql(dialect)
        base_sel = self._selected_fields() if (self._only is not None or self._defer) else None
        annotation_names = list(self._annotations.keys())
        rows = await engine.fetch_rows(sql, params)
        ncols = len(base_sel) if base_sel is not None else len(self.model._meta.field_list)
        instances = []
        for row in rows:
            obj = (
                self.model._from_db_row_fields(row[:ncols], base_sel)
                if base_sel is not None
                else self.model._from_db_row(row[:ncols])
            )
            for offset, name in enumerate(annotation_names):
                setattr(obj, name, row[ncols + offset])
            instances.append(obj)
        if self._prefetch:
            # Deferred: breaks the queryset <-> prefetch import cycle.
            from .prefetch import prefetch_instances

            await prefetch_instances(instances, self._prefetch)
        return instances

    def __await__(self) -> Generator[Any, None, list[ModelT]]:
        """Make the query set awaitable, executing it on ``await``.

        Returns:
            A generator yielding the awaited list of model instances.
        """
        return self._fetch().__await__()

    async def __aiter__(self) -> AsyncGenerator[ModelT, None]:
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
        lock = self._lock_sql(dialect)
        if lock and joins and not self._for_update_of and dialect.supports_for_update_of:
            # PostgreSQL rejects FOR UPDATE on the nullable side of a LEFT
            # JOIN, so with joined relations lock only the base table's rows.
            # MariaDB has no FOR UPDATE OF (and doesn't need it — plain
            # FOR UPDATE over a join locks the matched rows), so it is skipped.
            lock = lock.replace("FOR UPDATE", f"FOR UPDATE OF {table}", 1)
        sql = (
            f"SELECT {distinct}{cols} FROM {table}{''.join(joins.values())}{where}"
            f"{self._order_sql(dialect)}{self._tail_sql(dialect)}{lock}"
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

        Raises:
            UnSupportedError: With ``select_for_update()`` set; ``FOR UPDATE``
                is not allowed with GROUP BY/aggregates.
        """
        if self._for_update:
            raise UnSupportedError(
                "select_for_update() cannot be combined with annotate()/group_by(): "
                "FOR UPDATE is not allowed with GROUP BY/aggregates"
            )
        meta = self.model._meta
        meta.compile(dialect)
        q = dialect.quote
        table = q(meta.table)
        joins: dict[str, str] = {}
        select, names, group_cols = [], [], []
        requested = list(fields) if fields else None

        if requested is None and not self._group_by:
            # Pure ``annotate(...).values()`` with no field restriction: project
            # every base column alongside the annotations and GROUP BY the pk, so
            # the base model's fields are returned (not just the annotations) —
            # matching the annotated model fetch. The pk group makes the base
            # columns functionally dependent, so they need no explicit grouping.
            for f in meta.field_list:
                select.append(f"{table}.{q(f.db_column)}")
                names.append(f.model_field_name)
            group_cols.append(f"{table}.{q(meta.pk_field.db_column)}")
        else:
            # ``_resolve_column`` handles both own columns and forward-relation
            # paths (``bearworks_disposition__user_defined``), adding the needed
            # LEFT JOIN, so group_by()/values() can reference related columns.
            for f in self._group_by:
                col = self._resolve_column(f, dialect, joins)
                select.append(col)
                names.append(f)
                group_cols.append(col)
            if requested:
                for f in requested:
                    if f in self._annotations or f in names:
                        continue
                    col = self._resolve_column(f, dialect, joins)
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
        # Build ORDER BY before the FROM joins are interpolated below, so any
        # relation path it resolves can register its (shared) join in time.
        order = self._grouped_order_sql(dialect, joins)
        params = select_params + wparams + hparams
        group = (" GROUP BY " + ", ".join(group_cols)) if group_cols else ""
        sql = (
            f"SELECT {', '.join(select)} FROM {table}"
            f"{''.join(joins.values())}{where}{group}{having}{order}{self._tail_sql(dialect)}"
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
        paths = fields or tuple(self.model._meta.fields.keys())
        return _ValuesQuery(lambda: self._values_list_impl(fields, flat), self, paths)

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
        if not fields and not aliases:
            paths: tuple[str, ...] = tuple(self.model._meta.fields.keys())
        else:
            paths = tuple(fields) + tuple(aliases.values())
        return _ValuesQuery(lambda: self._values_impl(fields, aliases), self, paths)

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
            # Select the requested fields plus any alias source paths (which may
            # traverse a relation), then remap to the requested output names.
            sources = fields + tuple(aliases.values())
            rows = await self._values_grouped(sources, as_dict=True)
            if not sources:
                return rows
            out: list[dict[str, Any]] = []
            for r in rows:
                d = {f: r[f] for f in fields}
                for out_name, src in aliases.items():
                    d[out_name] = r[src]
                out.append(d)
            return out
        if not fields and not aliases:
            names = paths = tuple(self.model._meta.fields.keys())
        else:
            names = tuple(fields) + tuple(aliases.keys())
            paths = tuple(fields) + tuple(aliases.values())
        rows = await self._fetch_columns(paths)
        return [dict(zip(names, r)) for r in rows]

    async def get(self, **kwargs: Any) -> ModelT:
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

    async def get_or_none(self, **kwargs: Any) -> ModelT | None:
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
    ) -> tuple[ModelT, bool]:
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
    ) -> tuple[ModelT, bool]:
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
        if self._select_related or self._only_related or self._defer_related:
            # The join plan also carries any annotations (combined shape).
            sql, params, _, _, _ = self._select_related_plan(dialect)
            return sql, params
        if self._annotations:
            return self._annotated_select_sql(dialect)
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

    def first(self) -> QuerySetSingle[ModelT | None]:
        """Return a chainable single-row result for the first matching object.

        Awaiting it resolves to the first instance or ``None``; chaining
        ``only`` / ``values`` / ``values_list`` / ``select_related`` /
        ``prefetch_related`` narrows that single row first.

        Returns:
            A ``QuerySetSingle`` resolving to the first instance, or ``None``.
        """
        return QuerySetSingle(self, _resolve_first)

    async def last(self) -> ModelT | None:
        """Fetch the last matching object under the effective ordering.

        The effective ordering — explicit ``order_by``, else the model's
        ``Meta.ordering``, else ascending primary key — is reversed and the
        first row taken.

        Returns:
            The last matching model instance, or ``None`` when there are none.

        Raises:
            TypeError: When the query set is already sliced (limit/offset);
                reversing a slice is ambiguous (matching Django).
        """
        if self._limit is not None or self._offset is not None:
            raise TypeError("Cannot reverse a query once a slice has been taken")
        qs = self._clone()
        order = (
            qs._order
            or list(self.model._meta.ordering)
            or [(self.model._meta.pk_field.model_field_name, False)]
        )
        qs._order = [(name, not desc) for name, desc in order]
        rows = await qs.limit(1)._fetch()
        return rows[0] if rows else None

    async def earliest(self, *fields: str) -> ModelT | None:
        """Fetch the first object ordered ascending by ``fields``.

        Args:
            *fields: Field names to order by; defaults to the primary key.

        Returns:
            The earliest matching instance, or ``None`` when there are none.
        """
        order = fields or (self.model._meta.pk_field.model_field_name,)
        return await self.order_by(*order).first()

    async def latest(self, *fields: str) -> ModelT | None:
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

    def _needs_wrapped_terminal(self) -> bool:
        """Report whether count()/exists() must wrap the full SELECT.

        A HAVING clause, an explicit grouping or a limit/offset slice all change
        which result rows exist, so counting the base table's rows would be
        wrong; the full select is wrapped as a subquery instead.

        Returns:
            ``True`` when the wrapped form is required.
        """
        return bool(
            self._having or self._group_by or self._limit is not None or self._offset is not None
        )

    async def count(self) -> int:
        """Count the rows (or groups, when grouped) matching this query set.

        Returns:
            The number of matching rows, honouring annotation filters
            (``HAVING``), ``group_by`` (groups are counted) and slicing.
        """
        dialect = get_dialect(self.model, using=self._using_name())
        engine = get_executor(self.model, using=self._using)
        if self._needs_wrapped_terminal():
            inner, params = self._wrapped_terminal_inner()
            sub = dialect.quote("sub")
            row = await engine.fetch_row(f"SELECT COUNT(*) FROM ({inner}) AS {sub}", params)
            return int(row[0]) if row else 0
        where, params, _ = self._compile_conditions(dialect)
        table = dialect.quote(self.model._meta.table)
        row = await engine.fetch_row(f"SELECT COUNT(*) FROM {table}{where}", params)
        return int(row[0]) if row else 0

    def _wrapped_terminal_inner(self) -> tuple[str, list[Any]]:
        """Compile the subquery ``count()``/``exists()`` wrap when needed.

        The eager-loading options are dropped from the inner query first: their
        1:1 LEFT JOIN columns never change row multiplicity, but they can
        collide by name inside a derived table (both tables select ``id``),
        which MySQL rejects outright — and the extra columns are dead weight
        for counting on every backend.

        Returns:
            The ``(sql, params)`` pair of the inner query.
        """
        inner_qs = self
        if self._select_related or self._only_related or self._defer_related:
            inner_qs = self._clone()
            inner_qs._select_related = []
            inner_qs._only_related = {}
            inner_qs._defer_related = {}
        return inner_qs.get_parameterized_sql()

    async def exists(self) -> bool:
        """Report whether any row matches the current conditions.

        Returns:
            ``True`` if at least one matching row (or group, when grouped)
            exists, honouring annotation filters and slicing.
        """
        dialect = get_dialect(self.model, using=self._using_name())
        engine = get_executor(self.model, using=self._using)
        if self._needs_wrapped_terminal():
            inner, params = self._wrapped_terminal_inner()
            sub = dialect.quote("sub")
            rows = await engine.fetch_rows(f"SELECT 1 FROM ({inner}) AS {sub} LIMIT 1", params)
            return bool(rows)
        where, params, _ = self._compile_conditions(dialect)
        table = dialect.quote(self.model._meta.table)
        rows = await engine.fetch_rows(f"SELECT 1 FROM {table}{where} LIMIT 1", params)
        return bool(rows)

    def _pk_having_subselect(self, dialect: BaseDialect, start: int = 1) -> tuple[str, list[Any]]:
        """Build the ``SELECT pk ... GROUP BY pk HAVING ...`` restriction.

        ``DELETE``/``UPDATE`` statements cannot carry a ``HAVING`` clause, so
        annotation filters are applied by restricting the statement to the
        primary keys of the groups that survive them.

        Args:
            dialect: The SQL dialect providing quoting and placeholders.
            start: The first bind-parameter index (continues the outer
                statement's numbering).

        Returns:
            A ``(sql, params)`` tuple for the subselect.
        """
        meta = self.model._meta
        meta.compile(dialect)
        q = dialect.quote
        table = q(meta.table)
        pk = f"{table}.{q(meta.pk_field.db_column)}"
        joins: dict[str, str] = {}
        where, wparams, idx = self._compile_conditions(dialect, start=start)
        having, hparams, _ = self._compile_having(dialect, idx, joins)
        sql = f"SELECT {pk} FROM {table}{''.join(joins.values())}{where} GROUP BY {pk}{having}"
        return sql, wparams + hparams

    @staticmethod
    def _wrap_modifying_subquery(dialect: BaseDialect, sub: str) -> str:
        """Wrap a self-referencing UPDATE/DELETE subquery when the dialect needs it.

        MySQL cannot subquery the statement's own target table directly
        (error 1093); routing the subselect through a derived table
        materialises it first, which the server accepts.

        Args:
            dialect: The active SQL dialect.
            sub: The compiled subselect SQL.

        Returns:
            The subselect, wrapped in a derived table when required.
        """
        if not dialect.modifying_subquery_needs_wrap:
            return sub
        return f"SELECT * FROM ({sub}) AS {dialect.quote('_yara_having')}"

    async def delete(self) -> int:
        """Delete all rows matching the current conditions.

        Returns:
            The number of rows deleted.

        Raises:
            TypeError: When the query set is sliced (limit/offset); a sliced
                delete is ambiguous (matching Django).
        """
        if self._limit is not None or self._offset is not None:
            raise TypeError("Cannot use limit/offset with delete()")
        dialect = get_dialect(self.model, using=self._using_name())
        engine = get_executor(self.model, write=True, using=self._using)
        meta = self.model._meta
        table = dialect.quote(meta.table)
        if self._having:
            # Annotation filters compile to HAVING (which DELETE cannot carry);
            # without this restriction they were dropped — a full-table wipe.
            sub, params = self._pk_having_subselect(dialect)
            sub = self._wrap_modifying_subquery(dialect, sub)
            pk = f"{table}.{dialect.quote(meta.pk_field.db_column)}"
            return await engine.execute(f"DELETE FROM {table} WHERE {pk} IN ({sub})", params)
        where, params, _ = self._compile_conditions(dialect)
        return await engine.execute(f"DELETE FROM {table}{where}", params)

    async def update(self, **kwargs: Any) -> int:
        """Update matching rows with the given field values.

        Args:
            **kwargs: Field names mapped to their new values; relation names
                accept either a model instance or its primary key.

        Returns:
            The number of rows updated.

        Raises:
            TypeError: When the query set is sliced (limit/offset); a sliced
                update is ambiguous (matching Django).
        """
        if self._limit is not None or self._offset is not None:
            raise TypeError("Cannot use limit/offset with update()")
        dialect = get_dialect(self.model, using=self._using_name())
        engine = get_executor(self.model, write=True, using=self._using)
        meta = self.model._meta
        assignments: list[str] = []
        params: list[Any] = []
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
        table = dialect.quote(meta.table)
        if self._having:
            # Annotation filters compile to HAVING (which UPDATE cannot carry);
            # restrict to the pks of the groups that survive them instead.
            sub, sub_params = self._pk_having_subselect(dialect, start=idx)
            sub = self._wrap_modifying_subquery(dialect, sub)
            params.extend(sub_params)
            pk = f"{table}.{dialect.quote(meta.pk_field.db_column)}"
            where = f" WHERE {pk} IN ({sub})"
        else:
            where, where_params, _ = self._compile_conditions(dialect, start=idx)
            params.extend(where_params)
        sql = f"UPDATE {table} SET {', '.join(assignments)}{where}"
        return await engine.execute(sql, params)


async def _resolve_first(queryset: QuerySet[ModelT]) -> ModelT | None:
    """Fetch the first row of ``queryset`` (the awaited form of ``first()``).

    Args:
        queryset: The query set to take the first row of.

    Returns:
        The first matching instance, or ``None`` when there are no matches.
    """
    rows = await queryset.limit(1)._fetch()
    return rows[0] if rows else None


async def _resolve_get(queryset: QuerySet[ModelT]) -> ModelT:
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

    def __init__(
        self,
        run: Callable[[], Awaitable[list[Any]]],
        queryset: QuerySet[Any] | None = None,
        paths: tuple[str, ...] | None = None,
    ) -> None:
        """Wrap the zero-arg coroutine factory that runs the projection.

        Args:
            run: A callable returning the awaitable that fetches the rows.
            queryset: The source query set, retained so the projection can also
                render itself as a subquery (``Subquery(qs.values_list(...))``).
            paths: The projected column paths, used when rendering the subquery.

        Returns:
            None
        """
        self._run = run
        self._queryset = queryset
        self._paths = paths

    def _plain_select_sql(
        self, dialect: BaseDialect, start: int = 1
    ) -> tuple[str, list[Any], None]:
        """Render this projection as a SELECT, for embedding as a subquery.

        Lets a lazy ``values()`` / ``values_list()`` stand in for a queryset
        inside :class:`~yara_orm.Subquery` (the ``id__in=Subquery(...)``
        pattern), selecting exactly the projected column(s).

        Args:
            dialect: The SQL dialect providing quoting and placeholders.
            start: The first bind-parameter index (the outer query's next slot).

        Returns:
            A ``(sql, params, None)`` tuple matching ``QuerySet._plain_select_sql``.

        Raises:
            TypeError: When this result did not capture its source query set.
        """
        if self._queryset is None or self._paths is None:
            raise TypeError("this values()/values_list() result cannot be used as a Subquery")
        return self._queryset._projection_select_sql(self._paths, dialect, start)

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
        queryset: QuerySet[Any] | Callable[[], QuerySet[Any]],
        resolver: Callable[[QuerySet[Any]], Awaitable[_SingleT]],
        fast: Callable[[], Awaitable[_SingleT]] | None = None,
    ) -> None:
        """Wrap the query set whose single row will be awaited.

        Args:
            queryset: The query set already narrowed to the target row, or a
                zero-arg factory building it. ``Model.get`` passes a factory:
                the query set is only needed when the caller chains (or the
                fast path bails out), so the common ``await Model.get(...)``
                never pays for building it.
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
        self._queryset_source = queryset
        self._fast = fast
        self._resolver = resolver

    @property
    def _queryset(self) -> QuerySet[Any]:
        """The wrapped query set, built on first use when given as a factory.

        Returns:
            The query set narrowed to the target row.
        """
        qs = self._queryset_source
        if not isinstance(qs, QuerySet):
            qs = self._queryset_source = qs()
        return qs

    def _chain(self, queryset: QuerySet[Any]) -> QuerySetSingle[_SingleT]:
        """Wrap ``queryset`` keeping this result's resolver (drops the fast path).

        Args:
            queryset: The further-narrowed query set to wrap.

        Returns:
            A new ``QuerySetSingle`` over ``queryset``.
        """
        return QuerySetSingle(queryset, resolver=self._resolver)

    def prefetch_related(self, *specs: str | Prefetch) -> QuerySetSingle[_SingleT]:
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

    def using_db(self, connection: str | BaseDBAsyncClient) -> QuerySetSingle[_SingleT]:
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

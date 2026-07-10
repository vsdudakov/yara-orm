"""Model base class and metaclass."""

from __future__ import annotations

import copy
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar, cast

from . import registry, signals
from . import timezone as _tz
from .connection import get_dialect, get_executor
from .db_defaults import DatabaseDefault
from .exceptions import DoesNotExist, FieldError, MultipleObjectsReturned
from .fields import (
    DatetimeField,
    Field,
    ForeignKeyFieldInstance,
    IntField,
    ManyToManyFieldInstance,
)
from .manager import Manager
from .prefetch import prefetch_instances
from .queryset import QuerySet, QuerySetSingle, _resolve_get
from .relations import (
    ForwardRelationDescriptor,
    M2MDescriptor,
    M2MInfo,
    RelationInfo,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Sequence

    from typing_extensions import Self

    from .connection import BaseDBAsyncClient
    from .dialects import BaseDialect
    from .migrations import Constraint
    from .prefetch import Prefetch
    from .queryset import Q

#: A concrete model type flowing through helpers that take instances (e.g.
#: ``fetch_for_list``), so the caller's model type is preserved.
_ModelT = TypeVar("_ModelT", bound="Model")

#: Every attribute :meth:`MetaInfo.compile` (and the ``_build_decode_plan`` it
#: calls) produces for one dialect. A compiled plan is stable per (model,
#: dialect), so these are snapshotted per dialect name and restored wholesale
#: when a model alternates dialects, instead of recompiling from scratch. This
#: list MUST stay complete: a missing attribute would keep another dialect's
#: value after a restore. ``_partial_update_cache`` is intentionally absent — it
#: is already keyed by dialect name and shared across all of them.
_COMPILED_PLAN_ATTRS = (
    "_read_decoders",
    "decoders",
    "decoder_names",
    "active_decoders",
    "auto_now_fields",
    "validated_fields",
    "coerced_fields",
    "db_default_fields",
    "_simple_lookup_cache",
    "_partial_plan_cache",
    "columns_sql",
    "select_prefix",
    "insert_fields",
    "insert_returning_fields",
    "insert_refresh_fields",
    "insert_refresh_sql",
    "insert_sql",
    "update_field_list",
    "update_sql",
    "delete_sql",
)


class MetaInfo:
    """Resolved metadata for a model: table name, fields and primary key.

    Also caches per-dialect compiled SQL fragments (column list, SELECT prefix,
    single-row INSERT) and a row-decode plan, so hot paths avoid rebuilding
    strings and skip no-op value conversions.
    """

    def __init__(
        self,
        table: str,
        fields: dict[str, Field],
        pk_field: Field,
        relations: dict[str, RelationInfo] | None = None,
        m2m: dict[str, M2MInfo] | None = None,
        description: str | None = None,
        abstract: bool = False,
        ordering: list[tuple[str, bool]] | None = None,
        unique_together: list[tuple[str, ...]] | None = None,
        indexes: list[Index] | None = None,
        constraints: list[Constraint] | None = None,
    ) -> None:
        """Store resolved table metadata and precompute the row-decode plan.

        Args:
            table: Database table name for the model.
            fields: Mapping of model field name to its ``Field`` instance.
            pk_field: The field acting as the primary key.
            relations: Forward relation metadata keyed by relation name.
            m2m: Many-to-many relation metadata keyed by relation name.
            description: Optional human-readable table description.
            abstract: Whether the model is an abstract base (no table of its
                own; only contributes fields to concrete subclasses).
            ordering: Default ordering as ``(field_name, descending)`` tuples,
                applied to queries that set no explicit ``order_by``.
            unique_together: Groups of field names forming composite UNIQUE
                constraints.
            indexes: Groups of field names forming composite indexes.
            constraints: Declarative table constraints (``UniqueConstraint`` /
                ``CheckConstraint``) from ``Meta.constraints``.

        Returns:
            None
        """
        self.abstract = abstract
        self.ordering = ordering or []
        self.unique_together = unique_together or []
        self.indexes = indexes or []
        #: Declarative table constraints (UniqueConstraint/CheckConstraint)
        #: from ``Meta.constraints``; emitted by ``generate_schemas``.
        self.constraints = constraints or []
        #: Additional ``Meta`` options, populated by the metaclass. ``schema``,
        #: ``app`` and ``fetch_db_defaults`` are recorded for introspection;
        #: ``default_connection`` also routes the model's statements.
        self.schema: str | None = None
        self.app: str | None = None
        self.default_connection: str | None = None
        self.fetch_db_defaults: bool = False
        #: When ``"store"``, ``Model.__init__`` keeps unknown kwargs as plain
        #: instance attributes (a lenient mode) instead of raising; left
        #: ``None`` (the default) yara stays strict and rejects them.
        self.extra_kwargs: str | None = None
        #: The model's manager; rebound to the declared/default one by the
        #: metaclass once the class object exists. ``Any``-parameterised: the
        #: bound model is only known per concrete class (``Model.filter``
        #: re-types the result as ``QuerySet[Self]``).
        self.manager: Manager[Any] = Manager()
        self.table = table
        self.fields = fields
        self.pk_field = pk_field
        self.field_list = list(fields.values())
        # Keep the caller's dict object: the metaclass fills m2m in after
        # constructing this MetaInfo, so `or {}` would silently drop entries.
        self.relations = relations if relations is not None else {}
        self.m2m = m2m if m2m is not None else {}
        self.description = description
        # Decode plan: (attr_name, converter-or-None). None => assign directly.
        # The converters start from the fields' own contract; ``compile``
        # rebuilds them through ``dialect.read_decoder`` so a backend whose
        # driver cannot express a type natively (MySQL's CHAR(36) uuids) can
        # add its reconstruction step.
        self._read_decoders: dict[str, Any] = {
            f.model_field_name: (None if f.read_identity else f.to_python) for f in self.field_list
        }
        self.decoders = [
            (f.model_field_name, self._read_decoders[f.model_field_name]) for f in self.field_list
        ]
        self._build_decode_plan()
        self._compiled_for: str | None = None
        # Memoised partial-UPDATE statements for ``save(update_fields=...)``,
        # keyed by ``(dialect, ordered db columns)``. A given column set always
        # maps to the same SQL, so entries never need invalidating.
        self._partial_update_cache: dict[tuple[str, tuple[str, ...]], str] = {}
        # Memoised partial read-hydration plans for ``only()``/``defer()``,
        # keyed by the ordered selected field names, so a partial SELECT reuses
        # the same ``(names, active_decoders)`` split as the full-row fast path.
        self._partial_plan_cache: dict[tuple[str, ...], tuple[list[str], list[tuple]]] = {}
        # Per-dialect compiled plans (see ``_COMPILED_PLAN_ATTRS``), keyed by
        # dialect name. Lets a model that alternates dialects restore a prior
        # dialect's plan instead of recompiling it every switch.
        self._compiled_plans: dict[str, dict[str, Any]] = {}

    def partial_decode_plan(self, fields: list[Field]) -> tuple[list[str], list[tuple]]:
        """Return the cached ``(names, active_decoders)`` plan for a subset.

        Mirrors :meth:`_build_decode_plan` for an ``only()``/``defer()`` column
        subset: the attr names (assigned in one ``dict.update``) and the index/
        name/converter triples for the columns that need a Python decoder.

        Args:
            fields: The selected fields, in SELECT column order.

        Returns:
            A ``(names, active_decoders)`` tuple.
        """
        key = tuple(f.model_field_name for f in fields)
        plan = self._partial_plan_cache.get(key)
        if plan is None:
            names = [f.model_field_name for f in fields]
            # Same dialect-aware converters as the full-row plan (the cache is
            # cleared when ``compile`` switches dialects).
            active = []
            for i, f in enumerate(fields):
                if f.model_field_name in self._read_decoders:
                    decoder = self._read_decoders[f.model_field_name]
                else:  # pragma: no cover - fields outside the model's own set
                    decoder = None if f.read_identity else f.to_python
                if decoder is not None:
                    active.append((i, f.model_field_name, decoder))
            plan = (names, active)
            self._partial_plan_cache[key] = plan
        return plan

    def _build_decode_plan(self) -> None:
        """Precompute the fast-path row-hydration plan from ``self.decoders``.

        Splits the decode plan into the column names (assigned in one C-level
        ``dict.update``) and the subset of columns that actually need a Python
        converter, so :meth:`Model._from_db_row` skips a per-field branch and a
        per-field Python call for the common all-identity case.

        Returns:
            None
        """
        self.decoder_names = [name for name, _ in self.decoders]
        self.active_decoders = [
            (i, name, decode)
            for i, (name, decode) in enumerate(self.decoders)
            if decode is not None
        ]
        # Save-path plans: only the fields that actually need work, so save()
        # skips a full-field scan + isinstance when a model has no auto_now or
        # validated columns (the common case).
        self.auto_now_fields = [
            (f.model_field_name, f)
            for f in self.field_list
            if isinstance(f, DatetimeField) and (f.auto_now or f.auto_now_add)
        ]
        self.validated_fields = [f for f in self.field_list if f.validators]
        # Construction plan (``Model.__init__``): only the fields whose
        # ``to_python_value`` is actually overridden need the per-value coercion
        # call. The base implementation is identity, so for an all-plain model
        # (no datetime/date/time-style loose-input coercion) construction assigns
        # every attribute directly instead of paying a virtual call per column.
        self.coerced_fields = frozenset(
            f.model_field_name
            for f in self.field_list
            if type(f).to_python_value is not Field.to_python_value
        )
        # Columns whose value the *database* supplies (``default=Now()`` etc.).
        # The write paths treat these specially: omitted from an INSERT unless
        # explicitly set, and never overwritten with a never-fetched ``None``.
        self.db_default_fields = [
            f for f in self.field_list if isinstance(f.default, DatabaseDefault)
        ]
        # Memoised SELECTs for the ``Model.get``/``get_or_none`` fast path
        # (:meth:`Model._simple_equality_rows`), keyed by (dialect name, kwarg
        # names in call order, which values are NULL, limit) — each maps to a
        # fixed statement plus the ``to_db`` binder per non-NULL value. Rebuilt
        # here alongside the decode plan, so a field-set change that forces a
        # recompile (``_compiled_for`` reset) also drops the stale SQL.
        self._simple_lookup_cache: dict[
            tuple[str, tuple[str, ...], tuple[bool, ...], int],
            tuple[str, list[Callable[[Any], Any]]],
        ] = {}

    @property
    def db_table(self) -> str:
        """Alias for :attr:`table` (the database table name)."""
        return self.table

    @db_table.setter
    def db_table(self, value: str) -> None:
        """Set the database table name through the alias.

        Args:
            value: The new table name.

        Returns:
            None
        """
        self.table = value

    @property
    def fields_map(self) -> dict[str, Field]:
        """Alias exposing all fields (incl. relations) by name."""
        return {**self.fields, **{name: info.field for name, info in self.relations.items()}}

    @property
    def db_fields(self) -> set[str]:
        """Set of database column names for this model."""
        return {f.db_column for f in self.field_list}

    @property
    def fields_db_projection(self) -> dict[str, Field]:
        """Mapping of db column name to its field."""
        return {f.db_column: f for f in self.field_list}

    def get_field(self, name: str) -> Field:
        """Return the field for ``name``, treating ``"pk"`` as the primary key.

        Args:
            name: Model field name, or the alias ``"pk"``.

        Returns:
            The matching ``Field`` instance.
        """
        if name == "pk":
            return self.pk_field
        try:
            return self.fields[name]
        except KeyError as exc:
            raise FieldError(f"Field {name!r} does not exist on this model") from exc

    def resolve_writable_field(self, name: str) -> Field:
        """Resolve a field or forward-relation name to its writable column field.

        A forward-relation name (``author``) maps to the local foreign-key column
        field (``author_id``); any other name resolves as a plain field. The
        write paths (partial ``save``, ``bulk_create``, ``bulk_update``) share
        this so a relation name in ``update_fields``/column lists targets the
        underlying FK column.

        Args:
            name: A model field name or a forward-relation name.

        Returns:
            The ``Field`` whose ``db_column`` the write should target.
        """
        info = self.relations.get(name)
        return self.get_field(info.source_attr if info is not None else name)

    def compile(self, dialect: BaseDialect) -> None:
        """Build and cache dialect-specific SQL once (idempotent per dialect).

        Args:
            dialect: The SQL dialect used to quote names and build placeholders.

        Returns:
            None
        """
        if self._compiled_for == dialect.name:
            return
        if self._compiled_for is None:
            # ``_compiled_for = None`` is the "field set changed, recompile"
            # signal (construction, migrations). That invalidates *every* cached
            # plan, not just the current dialect's, so drop them all and rebuild.
            self._compiled_plans.clear()
        else:
            # A genuine dialect switch: the field set is unchanged, so a plan
            # built for ``dialect`` earlier is still exact. Restore it verbatim
            # rather than recompiling — no re-quoting columns or regenerating
            # placeholders. The restored cache dicts are the same objects captured
            # at build time, so entries accumulated since (partial plans,
            # simple-lookup SQL) survive.
            cached = self._compiled_plans.get(dialect.name)
            if cached is not None:
                self.__dict__.update(cached)
                self._compiled_for = dialect.name
                return
        # The field set may have changed since construction (migrations); refresh
        # the hydration plan so it stays in sync with the current columns. The
        # converters come from the dialect (``read_decoder``) so a backend can
        # add its own reconstruction step (MySQL: CHAR(36) -> uuid.UUID); the
        # partial-plan cache is dropped alongside, since its entries embed them.
        self._read_decoders = {f.model_field_name: dialect.read_decoder(f) for f in self.field_list}
        self.decoders = [
            (f.model_field_name, self._read_decoders[f.model_field_name]) for f in self.field_list
        ]
        # Fresh dict (not ``.clear()``): any prior dialect's plan snapshot holds a
        # reference to its own cache, which clearing in place would corrupt.
        self._partial_plan_cache = {}
        self._build_decode_plan()
        q = dialect.quote
        self.columns_sql = ", ".join(q(f.db_column) for f in self.field_list)
        self.select_prefix = f"SELECT {self.columns_sql} FROM {q(self.table)}"

        # Single-row INSERT for the common case of an unset auto-increment pk.
        # Database-default columns are omitted so the database supplies them.
        self.insert_fields = [
            f
            for f in self.field_list
            if not (f is self.pk_field and f.auto_increment)
            and not isinstance(f.default, DatabaseDefault)
        ]
        # With ``Meta.fetch_db_defaults`` the INSERT also returns the
        # database-supplied default columns, so the instance reflects the
        # persisted row (both PostgreSQL and SQLite >= 3.35 support RETURNING).
        self.insert_returning_fields = [self.pk_field]
        if self.fetch_db_defaults:
            self.insert_returning_fields += [
                f for f in self.db_default_fields if f is not self.pk_field
            ]
        if dialect.supports_insert_returning:
            ret = dialect.insert_returning_clause(self.insert_returning_fields)
            self.insert_refresh_fields: list[Field] = []
            self.insert_refresh_sql: str | None = None
        else:
            # No RETURNING (MySQL): the backend hands the new auto-increment pk
            # back as a synthetic single-value row, which covers exactly the
            # ``[pk]`` returning list. ``Meta.fetch_db_defaults`` columns are
            # read back with a follow-up SELECT by pk instead.
            ret = ""
            self.insert_refresh_fields = self.insert_returning_fields[1:]
            if self.insert_refresh_fields:
                cols = ", ".join(q(f.db_column) for f in self.insert_refresh_fields)
                self.insert_refresh_sql = (
                    f"SELECT {cols} FROM {q(self.table)} "  # noqa: S608 - quoted identifiers
                    f"WHERE {q(self.pk_field.db_column)} = {dialect.placeholder(1)}"
                )
            else:
                self.insert_refresh_sql = None
        if self.insert_fields:
            cols = ", ".join(q(f.db_column) for f in self.insert_fields)
            holes = ", ".join(
                dialect.insert_placeholder(f, i + 1) for i, f in enumerate(self.insert_fields)
            )
            self.insert_sql = f"INSERT INTO {q(self.table)} ({cols}) VALUES ({holes}){ret}"
        else:
            default_values = dialect.insert_default_values_sql(self.pk_field.db_column)
            self.insert_sql = f"INSERT INTO {q(self.table)} {default_values}{ret}"

        # Single-instance UPDATE (all non-pk columns) and DELETE, both keyed by
        # the primary key. These are static per (model, dialect) — exactly like
        # the INSERT above — so ``save()`` on an existing row and ``delete()``
        # bind params against a cached statement instead of rebuilding the SQL,
        # quoting columns and generating placeholders on every call.
        self.update_field_list = [f for f in self.field_list if f is not self.pk_field]
        pk_col = q(self.pk_field.db_column)
        if self.update_field_list:
            assignments = ", ".join(
                f"{q(f.db_column)} = {dialect.placeholder(i + 1)}"
                for i, f in enumerate(self.update_field_list)
            )
            pk_hole = dialect.placeholder(len(self.update_field_list) + 1)
            self.update_sql = f"UPDATE {q(self.table)} SET {assignments} WHERE {pk_col} = {pk_hole}"
        else:
            # A pk-only model has nothing to UPDATE; callers skip the statement.
            self.update_sql = None
        self.delete_sql = f"DELETE FROM {q(self.table)} WHERE {pk_col} = {dialect.placeholder(1)}"
        self._compiled_for = dialect.name
        # Snapshot this dialect's plan so a later switch back restores it in O(1)
        # instead of rebuilding. The dicts are captured by reference, so caches
        # populated later (partial plans, simple-lookup SQL) stay visible here.
        self._compiled_plans[dialect.name] = {k: getattr(self, k) for k in _COMPILED_PLAN_ATTRS}

    def partial_update_sql(self, dialect: BaseDialect, fields: list[Field]) -> str:
        """Return a cached ``UPDATE`` statement writing only ``fields`` by pk.

        Powers ``save(update_fields=...)``: the SET clause covers just the named
        columns instead of every non-pk column. Memoised per column set.

        Args:
            dialect: The SQL dialect used to quote names and build placeholders.
            fields: The non-pk fields to assign, in bind order.

        Returns:
            An ``UPDATE ... SET ... WHERE pk = ?`` statement string.
        """
        key = (dialect.name, tuple(f.db_column for f in fields))
        sql = self._partial_update_cache.get(key)
        if sql is None:
            q = dialect.quote
            assignments = ", ".join(
                f"{q(f.db_column)} = {dialect.placeholder(i + 1)}" for i, f in enumerate(fields)
            )
            pk_hole = dialect.placeholder(len(fields) + 1)
            sql = (
                f"UPDATE {q(self.table)} SET {assignments} "
                f"WHERE {q(self.pk_field.db_column)} = {pk_hole}"
            )
            self._partial_update_cache[key] = sql
        return sql


def _normalize_field_groups(value: Any) -> list[tuple[str, ...]]:
    """Normalise a ``unique_together`` / ``indexes`` Meta option to field groups.

    Accepts a single group ``("a", "b")`` or several ``(("a", "b"), ("c",))``.

    Args:
        value: The raw Meta option value, or None.

    Returns:
        A list of field-name tuples (empty when ``value`` is falsy).
    """
    if not value:
        return []
    items = list(value)
    if items and isinstance(items[0], str):
        return [tuple(items)]
    return [tuple(group) for group in items]


class Index:
    """A secondary index declared in ``Meta.indexes``.

    Beyond a plain column group it can carry an explicit ``name``, a partial
    ``condition`` (a raw SQL predicate, rendered as ``WHERE <condition>``), a
    ``unique`` flag (``CREATE UNIQUE INDEX``), an access ``using`` method
    (``USING gin``/``gist``/``btree``) and ``include`` covering columns
    (``INCLUDE (...)``). Both PostgreSQL and SQLite support unique and partial
    indexes; ``using``/``include`` are PostgreSQL-only and are silently omitted
    on SQLite (which has no such syntax).

    Example::

        class Meta:
            indexes = [
                Index(fields=["status"], condition="status = 'active'"),
                Index(fields=["tags"], using="gin"),
                Index(fields=["owner_id"], unique=True, include=["email"]),
                Index(fields=["name"], using="gin", opclass="gin_trgm_ops"),
            ]
    """

    def __init__(
        self,
        *,
        fields: list[str],
        name: str | None = None,
        condition: str | None = None,
        unique: bool = False,
        using: str | None = None,
        include: list[str] | None = None,
        opclass: str | None = None,
    ) -> None:
        """Store the indexed fields and optional name/partial condition/options.

        Args:
            fields: Field (or forward-relation) names the index covers.
            name: Explicit index name; defaults to ``idx_<table>_<fields>``.
            condition: Optional partial-index predicate (raw SQL); ``None`` for a
                full index.
            unique: Whether to render ``CREATE UNIQUE INDEX`` (enforcing
                uniqueness over the covered columns).
            using: Optional access method (e.g. ``"gin"``/``"gist"``/``"btree"``)
                rendered as ``USING <method>``; PostgreSQL-only.
            include: Optional non-key covering columns rendered as
                ``INCLUDE (...)``; PostgreSQL-only.
            opclass: Optional operator class applied to every key column
                (e.g. ``"gin_trgm_ops"`` for trigram search or
                ``"jsonb_path_ops"`` for JSONB containment); PostgreSQL-only.

        Returns:
            None
        """
        self.fields = list(fields)
        self.name = name
        self.condition = condition
        self.unique = unique
        self.using = using
        self.include = list(include) if include else None
        self.opclass = opclass

    def resolve_name(self, table: str) -> str:
        """Return this index's name, deriving a default from ``table`` if unset.

        Args:
            table: The owning table name.

        Returns:
            The explicit name, or ``idx_<table>_<field1>_<field2>...``.
        """
        return self.name or (f"idx_{table}_" + "_".join(self.fields))

    def get_sql(
        self, model: type[Model], dialect: BaseDialect | None = None, safe: bool = True
    ) -> str:
        """Render the ``CREATE INDEX`` statement for this index on ``model``.

        A convenience for introspecting the DDL an index produces. The field
        names are resolved against ``model``'s columns and the active dialect's
        rules (PostgreSQL-only options are dropped on SQLite).

        Args:
            model: The model class the index is declared on.
            dialect: The dialect to render for; defaults to ``model``'s active
                connection dialect.
            safe: Whether to include an ``IF NOT EXISTS`` guard.

        Returns:
            The ``CREATE INDEX`` statement.
        """
        if dialect is None:
            dialect = get_dialect(model)
        meta = model._meta
        meta.compile(dialect)
        return dialect.render_index(meta, self, safe=safe)


def _normalize_indexes(value: Any) -> list[Index]:
    """Normalise ``Meta.indexes`` to a list of :class:`Index` objects.

    Accepts the legacy forms (a single ``("a", "b")`` group or several groups)
    as well as explicit :class:`Index` instances (which may carry a partial
    ``condition``), so plain and conditional indexes can be mixed.

    Args:
        value: The raw ``Meta.indexes`` value, or None.

    Returns:
        A list of ``Index`` objects (empty when ``value`` is falsy).
    """
    if not value:
        return []
    if isinstance(value, Index):
        return [value]
    items = list(value)
    # A single group of plain field names, e.g. ``indexes = ("a", "b")``.
    if items and all(isinstance(i, str) for i in items):
        return [Index(fields=list(items))]
    out: list[Index] = []
    for item in items:
        out.append(item if isinstance(item, Index) else Index(fields=list(item)))
    return out


def _setattr_unmark_db_default(self: Model, name: str, value: Any, /) -> None:
    """Set the attribute, un-marking a pending database-default column.

    An explicit assignment — including ``None`` — makes the in-memory value
    authoritative again, so the next full ``save()`` writes it instead of
    skipping the column as never-fetched.

    Installed as ``__setattr__`` by the metaclass *only* on models that
    declare a ``DatabaseDefault`` column; every other model keeps the plain
    ``object.__setattr__`` (an override is ~20x slower and fires on every
    attribute write, including each field assignment in ``__init__``).

    Args:
        self: The model instance being mutated.
        name: The attribute name being assigned.
        value: The value to assign.

    Returns:
        None
    """
    unfetched = self.__dict__.get("_unfetched_db_defaults")
    if unfetched:
        unfetched.discard(name)
    object.__setattr__(self, name, value)


class ModelMeta(type):
    """Metaclass that collects fields and installs relation descriptors."""

    def __new__(
        mcls,
        name: str,
        bases: tuple[type, ...],
        namespace: dict[str, Any],
    ) -> type:
        """Build a model class, wiring up its ``MetaInfo`` and descriptors.

        Args:
            name: Name of the class being created.
            bases: Base classes of the new class.
            namespace: Class body namespace, including declared fields.

        Returns:
            The newly constructed model class.
        """
        parents = [b for b in bases if isinstance(b, ModelMeta)]
        if not parents:
            # This is the base ``Model`` itself; nothing to wire up.
            return super().__new__(mcls, name, bases, namespace)

        fields: dict[str, Field] = {}
        fk_decls: dict[str, ForeignKeyFieldInstance] = {}
        m2m_decls: dict[str, ManyToManyFieldInstance] = {}
        # Relation/m2m declarations inherited from abstract bases. The base's
        # fields (incl. each FK's ``<name>_id`` column) are merged below, but its
        # FK/M2M *relations* must be re-collected here: without this a concrete
        # subclass of an abstract base keeps the column yet loses the relation
        # accessor, so ``create(rel=...)`` and ``await obj.rel`` break.
        inherited_fk: dict[str, ForeignKeyFieldInstance] = {}
        inherited_m2m: dict[str, ManyToManyFieldInstance] = {}
        # Names merged in from a base class, so a pk this class declares itself
        # can be told apart from one it merely inherited (see the pk resolution
        # below: an own pk supersedes an inherited auto-injected ``id``).
        inherited_field_names: set[str] = set()
        for parent in parents:
            parent_meta: MetaInfo | None = getattr(parent, "_meta", None)
            if parent_meta is not None:
                inherited_field_names.update(parent_meta.fields)
                fields.update(parent_meta.fields)
                for rel_name, info in parent_meta.relations.items():
                    inherited_fk[rel_name] = info.field
                for rel_name, m2m_info in parent_meta.m2m.items():
                    inherited_m2m[rel_name] = m2m_info.field
        for key, value in namespace.items():
            if isinstance(value, ManyToManyFieldInstance):
                m2m_decls[key] = value
            elif isinstance(value, ForeignKeyFieldInstance):
                fk_decls[key] = value
            elif isinstance(value, Field):
                fields[key] = value

        # Foreign keys synthesise a concrete "<name>_id" backing column.
        for rel_name, fk in fk_decls.items():
            source = fk.source_field or f"{rel_name}_id"
            fk.model_field_name = source
            fk.db_column = source
            fields[source] = fk

        for fname, field in fields.items():
            if not field.model_field_name:
                field.model_field_name = fname
            if not field.db_column:
                field.db_column = fname

        meta_cls = namespace.get("Meta")
        # `abstract` is intentionally read from the class's own Meta only (not
        # inherited): a concrete subclass of an abstract base is itself concrete
        # unless it redeclares `abstract = True`.
        abstract = bool(getattr(meta_cls, "abstract", False))

        # A pk this class declares itself always wins over one inherited from a
        # base. Without this, an inherited auto-injected ``id`` (synthesised
        # below for a base — including an abstract one — that declared no pk)
        # would shadow a subclass naming its pk differently
        # (``uuid = UUIDField(pk=True)``): ``next(f for f if f.pk)`` returns the
        # inherited ``id`` first, silently demoting the real pk to a plain
        # column and leaving a spurious ``id`` serial on the table.
        own_pk = next(
            (f for n, f in fields.items() if f.pk and n not in inherited_field_names),
            None,
        )
        if own_pk is not None:
            for stale in [n for n, f in fields.items() if f._auto_pk and f is not own_pk]:
                del fields[stale]
            pk_field: Field | None = own_pk
        else:
            pk_field = next((f for f in fields.values() if f.pk), None)
        if pk_field is None:
            pk_field = IntField(pk=True)
            pk_field.model_field_name = "id"
            pk_field.db_column = "id"
            pk_field._auto_pk = True  # synthesised default, not user-declared
            fields = {"id": pk_field, **fields}
        table = getattr(meta_cls, "table", None) or name.lower()
        description = getattr(meta_cls, "table_description", None) or getattr(
            meta_cls, "description", None
        )

        # `ordering` is a list of field specs (``"name"`` / ``"-name"``) applied
        # as the default ORDER BY when a query sets none of its own.
        ordering: list[tuple[str, bool]] = []
        for spec in getattr(meta_cls, "ordering", None) or ():
            descending = spec.startswith("-")
            fname = spec[1:] if descending else spec
            if fname != "pk" and fname not in fields:
                raise FieldError(f"Meta.ordering refers to unknown field {fname!r} on {name}")
            ordering.append((fname, descending))

        unique_together = _normalize_field_groups(getattr(meta_cls, "unique_together", None))
        indexes = _normalize_indexes(getattr(meta_cls, "indexes", None))
        constraints: list[Constraint] = list(getattr(meta_cls, "constraints", None) or ())

        relations = {}
        for rel_name, fk in fk_decls.items():
            relations[rel_name] = RelationInfo(
                rel_name, fk, fk.model_field_name, fk.reference, fk.is_o2o
            )
        # Relations inherited from abstract bases (not redeclared on this class).
        for rel_name, fk in inherited_fk.items():
            if rel_name not in relations:
                relations[rel_name] = RelationInfo(
                    rel_name, fk, fk.model_field_name, fk.reference, fk.is_o2o
                )
        m2m = {}

        cls = cast("type[Model]", super().__new__(mcls, name, bases, namespace))
        cls._meta = MetaInfo(
            table=table,
            fields=fields,
            pk_field=pk_field,
            relations=relations,
            m2m=m2m,
            description=description,
            abstract=abstract,
            ordering=ordering,
            unique_together=unique_together,
            indexes=indexes,
            constraints=constraints,
        )

        # Only a model with database-default columns needs the ``__setattr__``
        # override that un-marks never-fetched columns on explicit assignment;
        # installing it per class keeps plain models on ``object.__setattr__``.
        # ``db_default_fields`` includes inherited fields (merged above), so a
        # subclass of a db-default model gets the override too. A class body
        # declaring its own ``__setattr__`` wins, as it would have via the MRO.
        if cls._meta.db_default_fields and "__setattr__" not in namespace:
            cls.__setattr__ = _setattr_unmark_db_default  # ty: ignore[invalid-assignment]

        # Additional Meta options are recorded (not silently dropped) so they
        # are introspectable via `_meta` / `describe()`. `default_connection`
        # also routes the model's statements (see connection._route); `schema`,
        # `app` and `fetch_db_defaults` are stored for tooling/forward-compat.
        cls._meta.schema = getattr(meta_cls, "schema", None)
        cls._meta.app = getattr(meta_cls, "app", None)
        cls._meta.default_connection = getattr(meta_cls, "default_connection", None)
        cls._meta.fetch_db_defaults = bool(getattr(meta_cls, "fetch_db_defaults", False))
        # ``extra_kwargs`` is inherited: a model that declares its own ``Meta``
        # (without restating the option) still picks it up from a base class, so
        # setting it once on a shared base applies to every subclass.
        extra_kwargs = getattr(meta_cls, "extra_kwargs", None) if meta_cls is not None else None
        if extra_kwargs is None:
            for parent in parents:
                parent_meta = getattr(parent, "_meta", None)
                if parent_meta is not None and parent_meta.extra_kwargs is not None:
                    extra_kwargs = parent_meta.extra_kwargs
                    break
        cls._meta.extra_kwargs = extra_kwargs

        # Per-model exception subclasses so callers can `except User.DoesNotExist`.
        # They subclass the global exceptions, so `except DoesNotExist` still works.
        cls.DoesNotExist = type(
            "DoesNotExist", (DoesNotExist,), {"__qualname__": f"{name}.DoesNotExist"}
        )
        cls.MultipleObjectsReturned = type(
            "MultipleObjectsReturned",
            (MultipleObjectsReturned,),
            {"__qualname__": f"{name}.MultipleObjectsReturned"},
        )

        # Bind the model's manager (a declared ``Meta.manager``, one inherited
        # from a base's Meta — so a soft-delete manager on an abstract base
        # scopes every subclass — or the default). The instance is *copied* per
        # class: rebinding a manager shared between two models' Meta in place
        # would silently point the first model's queries at the second.
        manager = getattr(meta_cls, "manager", None)
        if manager is None:
            for parent in parents:
                parent_meta = getattr(parent, "_meta", None)
                # A plain ``Manager`` carries no behaviour worth inheriting; only
                # a custom subclass propagates (mirroring ``extra_kwargs``).
                if parent_meta is not None and type(parent_meta.manager) is not Manager:
                    manager = parent_meta.manager
                    break
        manager = copy.copy(manager) if manager is not None else Manager()
        manager._model = cls
        cls._meta.manager = manager

        # Install forward accessors and m2m managers as class descriptors.
        for rel_name, info in relations.items():
            setattr(cls, rel_name, ForwardRelationDescriptor(info))
        # Own m2m declarations plus those inherited from abstract bases.
        for rel_name, mm in {**inherited_m2m, **m2m_decls}.items():
            info = M2MInfo(rel_name, mm, cls, mm.reference)
            m2m[rel_name] = info
            setattr(cls, rel_name, M2MDescriptor(info, pk_field.model_field_name, name=rel_name))

        # Abstract bases contribute their fields to subclasses but have no table
        # of their own, so they stay out of the registry (schema generation,
        # migrations and relation resolution all iterate the registry).
        if not abstract:
            registry.register(cls)
        return cls


class Model(metaclass=ModelMeta):
    """Base class for ORM models with persistence and query entry points."""

    _meta: ClassVar[MetaInfo]  # populated by the metaclass
    #: Per-model exception subclasses, installed by the metaclass.
    DoesNotExist: ClassVar[type[DoesNotExist]]
    MultipleObjectsReturned: ClassVar[type[MultipleObjectsReturned]]

    def __init__(self, **kwargs: Any) -> None:
        """Initialise field values from keyword arguments.

        Args:
            **kwargs: Field values, relation objects/ids, or db-column aliases.
                Unset fields fall back to their declared defaults.

        Returns:
            None
        """
        # Field values land in ``__dict__`` directly: fields are non-data
        # descriptors (instance ``__dict__`` wins attribute lookup), no
        # ``_unfetched_db_defaults`` mark can exist during ``__init__`` (it is
        # set only after an INSERT), and a per-write ``setattr`` — potentially
        # through the db-default ``__setattr__`` override — costs ~20x more.
        d = self.__dict__
        d["_in_db"] = False
        meta = self._meta
        # Relation objects (e.g. tournament=<Tournament>) resolve to their id.
        rel_overrides = {}
        for rel_name, info in meta.relations.items():
            if rel_name in kwargs:
                rel_overrides[rel_name] = (info, kwargs.pop(rel_name))
        for rel_name in meta.m2m:
            if rel_name in kwargs:
                raise TypeError(
                    f"Cannot set m2m field {rel_name!r} at construction; use "
                    f"`await obj.{rel_name}.add(...)` after saving"
                )

        coerced = meta.coerced_fields
        for name, field in meta.fields.items():
            if name in kwargs:
                value = kwargs.pop(name)
            elif field.db_column != name and field.db_column in kwargs:
                value = kwargs.pop(field.db_column)
            else:
                value = field.get_default()
            # Normalise loose input (e.g. an ISO string for a date column) to the
            # field's canonical Python type, so the in-memory attribute matches a
            # fetched row (``create(created_at="...").created_at`` is a datetime).
            # Only fields that override ``to_python_value`` need the call; the
            # rest (the majority) assign directly at C speed.
            d[name] = field.to_python_value(value) if name in coerced else value

        for rel_name, (info, value) in rel_overrides.items():
            if value is None:
                self.__dict__[info.source_attr] = None
            elif isinstance(value, Model):
                if value.pk is None:
                    raise ValueError(
                        f'Cannot assign "{value!r}" to {type(self).__name__}.{rel_name}: '
                        f"the instance isn't saved in the database yet; save it first"
                    )
                self.__dict__[info.source_attr] = value.pk
                self.__dict__.setdefault("_prefetch", {})[rel_name] = value
            else:
                self.__dict__[info.source_attr] = value

        if kwargs:
            if meta.extra_kwargs == "store":
                # Keep unknown kwargs as plain attributes (factories and
                # serializers rely on it); opt in via ``Meta.extra_kwargs``.
                for key, value in kwargs.items():
                    self.__dict__[key] = value
            else:
                raise TypeError(f"Unexpected keyword arguments: {sorted(kwargs)}")

    def __await__(self) -> Generator[Any, Any, Self]:
        """Make instances awaitable, yielding ``self``.

        Some code (and test factories) does ``await Model.create(...)``
        where ``create`` already returns an instance; awaiting the instance
        again must be a harmless no-op that returns it.

        Returns:
            This instance.
        """
        yield from ()
        return self

    def __class_getitem__(cls, _item: Any) -> type:
        """Make model classes subscriptable for annotations (no-op).

        Args:
            _item: The (ignored) type argument.

        Returns:
            The model class itself.
        """
        return cls

    @property
    def _saved_in_db(self) -> bool:
        """Alias for :attr:`_in_db` (True once persisted)."""
        return self._in_db

    @_saved_in_db.setter
    def _saved_in_db(self, value: bool) -> None:
        """Set the persisted flag through the alias.

        Args:
            value: Whether the instance is considered saved in the database.

        Returns:
            None
        """
        self._in_db = value

    async def fetch_related(self, *names: str) -> Self:
        """Populate the named relations on this instance (one query each).

        Args:
            *names: Relation names to fetch and attach to this instance.

        Returns:
            This instance, with the requested relations populated.
        """
        await prefetch_instances([self], names)
        return self

    # -- identity ---------------------------------------------------------
    @property
    def pk(self) -> Any:
        """Return the primary key value of this instance.

        Returns:
            The current value of the primary key field.
        """
        return getattr(self, self._meta.pk_field.model_field_name)

    def __eq__(self, other: object) -> bool:
        """Compare by model type and primary key (value identity semantics).

        Two instances of the same model with the same (non-``None``) primary key
        are equal, so a freshly fetched row compares equal to one already held
        and ``obj in [<same row>]`` works. Instances without a primary key
        (unsaved) are equal only to themselves.

        Args:
            other: The object to compare against.

        Returns:
            ``True`` when ``other`` is the same model with an equal primary key.
        """
        if self is other:
            return True
        if type(self) is not type(other):
            return NotImplemented
        own_pk = self.pk
        return own_pk is not None and own_pk == other.pk

    def __hash__(self) -> int:
        """Hash by model type and primary key, consistent with :meth:`__eq__`.

        Raises:
            TypeError: For an unsaved instance (``pk`` is ``None``) — its hash
                would change once ``save()`` assigns the primary key, making it
                unfindable in any set/dict it was already placed in.

        Returns:
            A hash over ``(type, pk)``.
        """
        pk = self.pk
        if pk is None:
            raise TypeError("Model instances without a primary key are unhashable")
        return hash((type(self), pk))

    # NOTE: models with database-default columns get a ``__setattr__`` override
    # (:func:`_setattr_unmark_db_default`) installed by the metaclass; every
    # other model keeps ``object.__setattr__`` for plain attribute-write speed.

    async def _refresh_inserted_defaults(
        self,
        executor: BaseDBAsyncClient,
        refresh_sql: str,
        refresh_fields: Sequence[Field],
    ) -> None:
        """Read database-supplied default columns back after an insert.

        The ``Meta.fetch_db_defaults`` refresh for dialects without
        ``INSERT ... RETURNING`` (MySQL): a follow-up ``SELECT`` by the new
        primary key assigns the database-filled values onto the instance,
        matching what a RETURNING clause hands back elsewhere. The statement and
        fields are passed in (not read from ``meta``) so a concurrent recompile
        for another dialect cannot swap them out across the ``await``.

        Args:
            executor: The executor the INSERT ran on (so an open transaction
                sees its own row).
            refresh_sql: The cached ``SELECT`` reading the defaults back by pk.
            refresh_fields: The fields the refresh row carries, in order.

        Returns:
            None
        """
        row = await executor.fetch_row(refresh_sql, [self._meta.pk_field.to_db(self.pk)])
        for field, value in zip(refresh_fields, row):
            setattr(self, field.model_field_name, field.to_python(value))

    def _mark_unfetched_db_defaults(self) -> None:
        """Record which database-default columns hold a never-fetched ``None``.

        Called right after an INSERT: any ``DatabaseDefault`` column that is
        still ``None`` in memory was filled by the database but not returned
        (``Meta.fetch_db_defaults`` off), so a later full ``save()`` must not
        overwrite it. Explicit assignments clear the mark (``__setattr__``).

        Returns:
            None
        """
        unset = {
            f.model_field_name
            for f in self._meta.db_default_fields
            if getattr(self, f.model_field_name, None) is None
        }
        if unset:
            self.__dict__["_unfetched_db_defaults"] = unset

    @classmethod
    def _from_db_row(cls, values: list[Any]) -> Self:
        """Build an instance from positional column values (fast read path).

        Column order matches ``_meta.field_list`` because the SELECT is compiled
        from the same field list. Direct ``__dict__`` writes and the precomputed
        decode plan keep this allocation-light.

        Args:
            values: Raw column values in ``_meta.field_list`` order.

        Returns:
            A new instance marked as already persisted.
        """
        meta = cls._meta
        obj = cls.__new__(cls)
        d = obj.__dict__
        d["_in_db"] = True
        # Assign every column at C speed, then convert only the few columns that
        # need a Python decoder (datetime/decimal/enum/...); most models are
        # all-identity, so the loop below is empty and this is a single update.
        d.update(zip(meta.decoder_names, values))
        for i, name, decode in meta.active_decoders:
            value = values[i]
            if value is not None:
                d[name] = decode(value)
        return obj

    @classmethod
    def _from_db_rows(cls, rows: list[list[Any]]) -> list[Self]:
        """Build instances for many rows, hoisting the per-row invariants once.

        The batch counterpart of :meth:`_from_db_row` for the common
        ``SELECT * -> list[Model]`` path: the decode plan and ``__new__`` are
        resolved a single time instead of once per row.

        Args:
            rows: Raw column-value lists in ``_meta.field_list`` order.

        Returns:
            The hydrated instances.
        """
        meta = cls._meta
        names = meta.decoder_names
        active = meta.active_decoders
        new = cls.__new__
        out: list[Self] = []
        for values in rows:
            obj = new(cls)
            d = obj.__dict__
            d["_in_db"] = True
            d.update(zip(names, values))
            for i, name, decode in active:
                value = values[i]
                if value is not None:
                    d[name] = decode(value)
            out.append(obj)
        return out

    @classmethod
    def _from_db_row_fields(cls, values: list[Any], fields: list[Field]) -> Self:
        """Build a partially-populated instance from a subset of columns.

        Powers ``only()`` / ``defer()``: only ``fields`` are set, so reading any
        other column raises ``FieldError`` (via the field descriptor) rather
        than returning a stale or wrong value. Uses the cached partial decode
        plan (:meth:`MetaInfo.partial_decode_plan`) so the per-field
        read-identity branch is resolved once, not per row.

        Args:
            values: Raw column values in ``fields`` order.
            fields: The selected fields, matching the SELECT column order.

        Returns:
            A new, partially-populated instance marked as already persisted.
        """
        names, active = cls._meta.partial_decode_plan(fields)
        obj = cls.__new__(cls)
        d = obj.__dict__
        d["_in_db"] = True
        d.update(zip(names, values))
        for i, name, decode in active:
            value = values[i]
            if value is not None:
                d[name] = decode(value)
        return obj

    @classmethod
    def _from_db_rows_fields(cls, rows: list[list[Any]], fields: list[Field]) -> list[Self]:
        """Build partially-populated instances for many rows (batch fast path).

        The ``only()``/``defer()`` counterpart of :meth:`_from_db_rows`: the
        cached decode plan and ``__new__`` are resolved once for the batch.

        Args:
            rows: Raw column-value lists in ``fields`` order.
            fields: The selected fields, matching the SELECT column order.

        Returns:
            The hydrated, partially-populated instances.
        """
        names, active = cls._meta.partial_decode_plan(fields)
        new = cls.__new__
        out: list[Self] = []
        for values in rows:
            obj = new(cls)
            d = obj.__dict__
            d["_in_db"] = True
            d.update(zip(names, values))
            for i, name, decode in active:
                value = values[i]
                if value is not None:
                    d[name] = decode(value)
            out.append(obj)
        return out

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        """Return a debugging representation showing the type and primary key.

        Returns:
            A string of the form ``<TypeName pk=...>``.
        """
        return f"<{type(self).__name__} pk={self.pk!r}>"

    # -- persistence ------------------------------------------------------
    def _apply_auto_now(self, only: set[str] | None = None) -> None:
        """Set ``auto_now``/``auto_now_add`` datetime fields to the current time.

        Args:
            only: When given, restrict updates to these field names. Used by
                ``save(update_fields=...)`` so an ``auto_now`` column is bumped
                only if it is among the fields being persisted (and the
                in-memory value never diverges from the row on disk).

        Returns:
            None
        """
        auto_now_fields = self._meta.auto_now_fields
        if not auto_now_fields:
            return
        # Honour ``use_tz``: aware UTC when enabled, naive UTC otherwise, so
        # auto_now columns match manually-set values and never mix aware/naive.
        now = _tz.now()
        in_db = self._in_db
        for name, field in auto_now_fields:
            if field.auto_now or (field.auto_now_add and not in_db):
                if field.auto_now_add and in_db:
                    continue
                if only is not None and name not in only:
                    continue
                setattr(self, name, now)

    async def save(
        self,
        update_fields: list[str] | None = None,
        using_db: str | BaseDBAsyncClient | None = None,
    ) -> Self:
        """Persist this instance, emitting pre/post-save signals if registered.

        Args:
            update_fields: When updating an existing row, restrict the write to
                these field names (relation names map to their FK column); an
                empty list is a no-op and unknown names raise ``FieldError``.
                ``auto_now`` columns are bumped only if named. Ignored when the
                instance is being inserted (a new row needs all its columns).
                The list is also forwarded to the save signals.
            using_db: Optional connection name/object to run on.

        Returns:
            This instance.
        """
        cls = type(self)
        created = not self._in_db
        self._run_validators()
        has_signals = signals._has_handlers(cls)
        executor = get_executor(cls, write=True, using=using_db)
        if has_signals:
            await signals.emit_pre_save(cls, self, executor, update_fields)
        await self._perform_save(executor, update_fields, using_db)
        if has_signals:
            await signals.emit_post_save(cls, self, created, executor, update_fields)
        return self

    def _run_validators(self) -> None:
        """Run each field's validators against this instance's current values.

        Returns:
            None
        """
        for field in self._meta.validated_fields:
            value = getattr(self, field.model_field_name, None)
            if value is None:
                continue
            for validator in field.validators:
                validator(value)

    def _bind_values(self, fields: Iterable[Field]) -> list[Any]:
        """Bind this instance's values for ``fields`` in order via ``to_db``.

        The insert/update binding step shared by the save paths: read each
        field's attribute (missing → ``None``) and coerce it through the field's
        ``to_db``.

        Args:
            fields: The fields to bind, in placeholder order.

        Returns:
            The bound parameter values.
        """
        return [f.to_db(getattr(self, f.model_field_name, None)) for f in fields]

    def _apply_returning(
        self,
        row: Sequence[Any],
        returning_fields: Sequence[Field],
        read_decoders: dict[str, Any],
    ) -> None:
        """Write an INSERT's RETURNING row back onto this instance.

        The database-assigned columns (serial pk, ``RETURNING`` defaults) are
        decoded through the model's read decoders and assigned. The plan is
        passed in (not read from ``meta``) so it stays consistent across the
        caller's ``await`` even if another coroutine recompiles the shared
        metadata for a different dialect meanwhile.

        Args:
            row: The RETURNING row, in ``returning_fields`` order.
            returning_fields: The fields the RETURNING row carries, in order.
            read_decoders: The per-field read decoders for the active dialect.

        Returns:
            None
        """
        for field, value in zip(returning_fields, row):
            decode = read_decoders.get(field.model_field_name)
            setattr(self, field.model_field_name, decode(value) if decode else value)

    async def _perform_save(
        self,
        executor: BaseDBAsyncClient,
        update_fields: list[str] | None = None,
        using_db: str | BaseDBAsyncClient | None = None,
    ) -> None:
        """Run the INSERT or UPDATE statement that persists this instance.

        Applies auto-now timestamps, then dispatches to :meth:`_insert` for a
        new row, :meth:`_update_partial` when ``update_fields`` names a subset,
        or :meth:`_update_full` for a full update of an existing row.

        Args:
            executor: The write-capable database executor to run SQL against.
            update_fields: Optional subset of fields to write on an UPDATE; see
                :meth:`save`. Ignored for INSERTs.
            using_db: Optional connection name/object the statement runs on, so
                the SQL is rendered for that connection's dialect.

        Returns:
            None
        """
        if self._in_db and update_fields is not None:
            self._apply_auto_now(only=set(update_fields))
        else:
            self._apply_auto_now()
        dialect = get_dialect(type(self), using=using_db)
        meta = self._meta
        meta.compile(dialect)
        if not self._in_db:
            await self._insert(executor, dialect, meta)
        elif update_fields is not None:
            await self._update_partial(executor, dialect, meta, update_fields)
        elif meta.update_sql is not None:
            await self._update_full(executor, dialect, meta)

    async def _insert(
        self, executor: BaseDBAsyncClient, dialect: BaseDialect, meta: MetaInfo
    ) -> None:
        """Insert this not-yet-persisted instance and read back generated values.

        Uses the cached single INSERT when the pk is an unset auto-increment and
        no database-default column carries an explicit value; otherwise builds
        the statement from the columns actually set. Generated columns come back
        via ``RETURNING`` (or a synthetic id row / a follow-up refresh on
        backends without RETURNING).

        Args:
            executor: The write-capable executor.
            dialect: The dialect the statement renders for.
            meta: The model metadata.

        Returns:
            None
        """
        pk_field = meta.pk_field
        table = dialect.quote(meta.table)
        pk_attr = pk_field.model_field_name
        pk_unset = pk_field.auto_increment and getattr(self, pk_attr, None) is None
        # Snapshot the dialect-specific compiled plan synchronously here: the
        # caller ran ``meta.compile(dialect)`` immediately before this with no
        # await between, so these are consistent for ``dialect``. Holding them in
        # locals (rather than re-reading ``meta.*`` after the awaits below) keeps
        # a concurrent save of the same model on a *different* dialect — which
        # recompiles and overwrites the shared ``meta`` attributes — from
        # decoding this row against the wrong plan.
        insert_sql = meta.insert_sql
        insert_fields = meta.insert_fields
        returning_fields = meta.insert_returning_fields
        read_decoders = meta._read_decoders
        refresh_sql = meta.insert_refresh_sql
        refresh_fields = meta.insert_refresh_fields
        # An explicit value on a database-default column must reach the INSERT
        # (the cached statement omits those columns), so the fast path only
        # applies while every such column is unset.
        if pk_unset and all(
            getattr(self, f.model_field_name, None) is None for f in meta.db_default_fields
        ):
            # Fast path: reuse the cached INSERT statement, bind params only.
            row = await executor.fetch_row(insert_sql, self._bind_values(insert_fields))
            self._apply_returning(row, returning_fields, read_decoders)
            self._in_db = True
            if refresh_sql:
                # fetch_db_defaults without RETURNING (MySQL): read the
                # database-filled defaults back by the new pk.
                await self._refresh_inserted_defaults(executor, refresh_sql, refresh_fields)
            self._mark_unfetched_db_defaults()
            return

        columns = []
        placeholders = []
        params = []
        idx = 1
        for name, field in meta.fields.items():
            value = getattr(self, name, None)
            # Omit an unset database-default column so the DB supplies it, and an
            # unset auto-increment pk so the DB assigns it.
            if value is None and (
                isinstance(field.default, DatabaseDefault)
                or (field is pk_field and field.auto_increment)
            ):
                continue
            columns.append(dialect.quote(field.db_column))
            placeholders.append(dialect.insert_placeholder(field, idx))
            params.append(field.to_db(value))
            idx += 1

        sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(placeholders)})"
        if dialect.supports_insert_returning:
            returning = dialect.insert_returning_clause(returning_fields)
            row = await executor.fetch_row(f"{sql}{returning}", params)
            self._apply_returning(row, returning_fields, read_decoders)
        elif pk_unset:
            # No RETURNING (MySQL): the backend returns the auto-increment id as
            # a synthetic single-value row.
            row = await executor.fetch_row(sql, params)
            setattr(self, pk_attr, pk_field.to_python(row[0]))
        else:
            # The pk was supplied by the caller; nothing to read back. An
            # explicit value for an auto-increment pk needs the dialect's
            # identity-insert wrapper (a no-op except on SQL Server).
            if pk_field.auto_increment:
                sql = dialect.identity_insert_sql(table, sql)
            await executor.execute(sql, params)
        self._in_db = True
        if not dialect.supports_insert_returning and refresh_sql:
            await self._refresh_inserted_defaults(executor, refresh_sql, refresh_fields)
        self._mark_unfetched_db_defaults()

    async def _update_partial(
        self,
        executor: BaseDBAsyncClient,
        dialect: BaseDialect,
        meta: MetaInfo,
        update_fields: list[str],
    ) -> None:
        """Write only ``update_fields`` on an existing row.

        Relation names map to their FK column; the pk is the WHERE key, never an
        assignment. Empty (or pk-only) ``update_fields`` persists nothing.

        Args:
            executor: The write-capable executor.
            dialect: The dialect the statement renders for.
            meta: The model metadata.
            update_fields: The field/relation names to write.

        Returns:
            None
        """
        pk_field = meta.pk_field
        resolved: list[Field] = []
        seen: set[str] = set()
        for name in update_fields:
            field = meta.resolve_writable_field(name)  # raises if unknown
            if field is pk_field or field.db_column in seen:
                continue
            seen.add(field.db_column)
            resolved.append(field)
        if not resolved:
            return
        params = self._bind_values(resolved)
        params.append(pk_field.to_db(self.pk))
        await executor.execute(meta.partial_update_sql(dialect, resolved), params)

    async def _update_full(
        self, executor: BaseDBAsyncClient, dialect: BaseDialect, meta: MetaInfo
    ) -> None:
        """Write every updatable column of an existing row via the cached UPDATE.

        A never-fetched database-default column is excluded so its DB-supplied
        value is not overwritten with the ``None`` the instance holds only as a
        placeholder (an explicit assignment clears that mark and is written).

        Args:
            executor: The write-capable executor.
            dialect: The dialect the statement renders for.
            meta: The model metadata (``update_sql`` guaranteed set by the caller).

        Returns:
            None
        """
        pk_field = meta.pk_field
        fields = meta.update_field_list
        sql = meta.update_sql
        assert sql is not None  # noqa: S101 - dispatched only when update_sql is set
        unfetched = self.__dict__.get("_unfetched_db_defaults")
        if unfetched:
            fields = [f for f in fields if f.model_field_name not in unfetched]
            if not fields:
                return
            sql = meta.partial_update_sql(dialect, fields)
        params = self._bind_values(fields)
        params.append(pk_field.to_db(self.pk))
        await executor.execute(sql, params)

    async def delete(self, using_db: str | BaseDBAsyncClient | None = None) -> None:
        """Delete this instance's row, emitting pre/post-delete signals.

        Args:
            using_db: Optional connection name/object to run on.

        Returns:
            None
        """
        cls = type(self)
        dialect = get_dialect(cls, using=using_db)
        executor = get_executor(cls, write=True, using=using_db)
        meta = self._meta
        has_signals = signals._has_handlers(cls)
        if has_signals:
            await signals.emit_pre_delete(cls, self, executor)
        meta.compile(dialect)
        # On a dialect whose m2m join-table FKs do not cascade (SQL Server pins
        # them to NO ACTION — see ``SqlServerDialect.create_m2m_table_sql``),
        # the join rows referencing this row must be removed first or the row
        # delete raises an FK-conflict IntegrityError. Every other dialect's
        # join tables carry ON DELETE CASCADE and skip this. The getattr lets a
        # dialect opt in/out via a capability attribute without requiring one.
        if not getattr(dialect, "m2m_on_delete_cascades", dialect.name != "mssql"):
            await self._delete_m2m_rows(executor, dialect)
        await executor.execute(meta.delete_sql, [meta.pk_field.to_db(self.pk)])
        self._in_db = False
        if has_signals:
            await signals.emit_post_delete(cls, self, executor)

    async def _delete_m2m_rows(self, executor: BaseDBAsyncClient, dialect: BaseDialect) -> None:
        """Delete every m2m join row referencing this instance.

        Covers both sides: relations this model owns (the join row's backward
        key holds this pk) and relations that target it (the forward key); a
        self-referential relation clears both columns. Only called on dialects
        whose join-table FKs do not cascade, so the registry scan for reverse
        relations is off every other backend's hot path.

        Args:
            executor: The write-capable executor the row delete will run on.
            dialect: The dialect the statements render for.

        Returns:
            None
        """
        cls = type(self)
        meta = self._meta
        pk_param = meta.pk_field.to_db(self.pk)
        targets: list[tuple[str, str]] = []
        for info in meta.m2m.values():
            info.finalize()
            targets.append((info.through, info.backward_key))
        for model in registry.all_models():
            for info in model._meta.m2m.values():
                if info.resolve_target() is cls:
                    info.finalize()
                    targets.append((info.through, info.forward_key))
        seen: set[tuple[str, str]] = set()
        for through, key in targets:
            if (through, key) in seen:
                continue
            seen.add((through, key))
            sql = (
                f"DELETE FROM {dialect.quote(through)} "
                f"WHERE {dialect.quote(key)} = {dialect.placeholder(1)}"
            )
            await executor.execute(sql, [pk_param])

    def clone(self, pk: Any = None) -> Self:
        """Return an unsaved copy of this instance, ready to insert as a new row.

        Copies every loaded field except the primary key, so the next ``save()``
        inserts a fresh row. Pass ``pk`` to assign an explicit primary key.

        Args:
            pk: Optional primary key for the clone; left unset (auto-assigned on
                save) when ``None``.

        Returns:
            A new, not-yet-persisted instance.
        """
        cls = type(self)
        clone = cls.__new__(cls)
        d = clone.__dict__
        d["_in_db"] = False
        pk_name = self._meta.pk_field.model_field_name
        for field in self._meta.field_list:
            name = field.model_field_name
            if name != pk_name and name in self.__dict__:
                d[name] = self.__dict__[name]
        d[pk_name] = pk
        return clone

    @classmethod
    def describe(cls) -> dict[str, Any]:
        """Return a structured description of this model's schema.

        Returns:
            A mapping of the model name, table, primary key, data fields,
            relations and ``Meta`` options — handy for introspection and tools.
        """
        meta = cls._meta

        def field_desc(field: Field) -> dict[str, Any]:
            # Only report JSON-friendly scalar defaults; callables/DatabaseDefault
            # objects are described as None.
            default = field.default
            if not isinstance(default, (int, str, bool, float)):
                default = None
            return {
                "name": field.model_field_name,
                "db_column": field.db_column,
                "field_type": type(field).__name__,
                "kind": field.field_kind,
                "pk": field.pk,
                "null": field.null,
                "unique": field.unique,
                "index": field.index,
                "default": default,
                "description": field.description,
            }

        return {
            "name": cls.__name__,
            "table": meta.table,
            "abstract": meta.abstract,
            "description": meta.description,
            "pk_field": meta.pk_field.model_field_name,
            "data_fields": [field_desc(f) for f in meta.field_list],
            "fk_fields": sorted(meta.relations),
            "m2m_fields": sorted(meta.m2m),
            "unique_together": [list(g) for g in meta.unique_together],
            "indexes": [list(ix.fields) for ix in meta.indexes],
            "ordering": [("-" if desc else "") + n for n, desc in meta.ordering],
        }

    @classmethod
    def construct(cls, _from_db: bool = False, **kwargs: Any) -> Self:
        """Build a detached instance directly, skipping validation and defaults.

        A fast, low-ceremony constructor: the given values are written straight
        onto the instance with no relation resolution, default filling or
        validation. Use it when you already have trusted field values.

        Args:
            _from_db: Mark the instance as already persisted (so the next
                ``save()`` issues an UPDATE rather than an INSERT).
            **kwargs: Field values to set directly.

        Returns:
            A new, lightly-constructed instance.
        """
        obj = cls.__new__(cls)
        obj.__dict__["_in_db"] = _from_db
        obj.__dict__.update(kwargs)
        return obj

    @classmethod
    async def fetch_for_list(
        cls, instances: Sequence[_ModelT], *relations: str | Prefetch
    ) -> list[_ModelT]:
        """Prefetch ``relations`` across a list of instances (one query each).

        Args:
            instances: The instances to populate.
            *relations: Relation names or :class:`~yara_orm.Prefetch` specs.

        Returns:
            The same ``instances`` list, with the relations cached on each.
        """
        instances = list(instances)
        if instances:
            await prefetch_instances(instances, relations)
        return instances

    # -- query entry points ----------------------------------------------
    @classmethod
    def all(cls) -> QuerySet[Self]:
        """Return a query set over all rows of this model.

        Returns:
            A new ``QuerySet`` bound to this model.
        """
        return cls._meta.manager.get_queryset()

    @classmethod
    def filter(
        cls, *args: Q, using_db: str | BaseDBAsyncClient | None = None, **kwargs: Any
    ) -> QuerySet[Self]:
        """Return a query set filtered by the given conditions.

        Args:
            *args: Positional filter expressions (e.g. ``Q`` objects).
            using_db: Optional connection name/object to run on.
            **kwargs: Field lookups to filter by.

        Returns:
            A new ``QuerySet`` with the filters applied.
        """
        qs = cls._meta.manager.get_queryset().filter(*args, **kwargs)
        return qs.using_db(using_db) if using_db is not None else qs

    @classmethod
    def exclude(
        cls, *args: Q, using_db: str | BaseDBAsyncClient | None = None, **kwargs: Any
    ) -> QuerySet[Self]:
        """Return a query set excluding rows matching the given conditions.

        Args:
            *args: Positional filter expressions (e.g. ``Q`` objects).
            using_db: Optional connection name/object to run on.
            **kwargs: Field lookups to exclude by.

        Returns:
            A new ``QuerySet`` with the exclusions applied.
        """
        qs = cls._meta.manager.get_queryset().exclude(*args, **kwargs)
        return qs.using_db(using_db) if using_db is not None else qs

    @classmethod
    def annotate(cls, **annotations: Any) -> QuerySet[Self]:
        """Return a query set with the given computed annotations.

        Args:
            **annotations: Annotation expressions keyed by output name.

        Returns:
            A new ``QuerySet`` carrying the annotations.
        """
        return cls._meta.manager.get_queryset().annotate(**annotations)

    @classmethod
    def prefetch_related(cls, *specs: str | Prefetch) -> QuerySet[Self]:
        """Return a query set that prefetches the given relations.

        Args:
            *specs: Relation names or prefetch specifications to load.

        Returns:
            A new ``QuerySet`` configured to prefetch the relations.
        """
        return cls._meta.manager.get_queryset().prefetch_related(*specs)

    @classmethod
    def select_related(cls, *relations: str) -> QuerySet[Self]:
        """Return a query set that eager-loads forward FK/O2O relations by join.

        Args:
            *relations: Forward relation names to join and load in one query.

        Returns:
            A new ``QuerySet`` configured to select the relations.
        """
        return cls._meta.manager.get_queryset().select_related(*relations)

    @classmethod
    async def first(cls) -> Self | None:
        """Return the first row (by default ordering), or ``None``.

        Returns:
            The first matching instance, or ``None`` when the table is empty.
        """
        return await cls._meta.manager.get_queryset().first()

    @classmethod
    async def last(cls) -> Self | None:
        """Return the last row (by default ordering), or ``None``.

        Returns:
            The last matching instance, or ``None`` when the table is empty.
        """
        return await cls._meta.manager.get_queryset().last()

    @classmethod
    async def earliest(cls, *fields: str) -> Self | None:
        """Return the earliest row ordered ascending by ``fields``.

        Args:
            *fields: Field names to order by; defaults to the primary key.

        Returns:
            The earliest instance, or ``None`` when there are none.
        """
        return await cls._meta.manager.get_queryset().earliest(*fields)

    @classmethod
    async def latest(cls, *fields: str) -> Self | None:
        """Return the latest row ordered descending by ``fields``.

        Args:
            *fields: Field names to order by; defaults to the primary key.

        Returns:
            The latest instance, or ``None`` when there are none.
        """
        return await cls._meta.manager.get_queryset().latest(*fields)

    @classmethod
    async def exists(cls, **kwargs: Any) -> bool:
        """Report whether any row matches the given lookups.

        Args:
            **kwargs: Optional field lookups to test for.

        Returns:
            ``True`` if at least one matching row exists.
        """
        qs = cls._meta.manager.get_queryset()
        return await (qs.filter(**kwargs) if kwargs else qs).exists()

    @classmethod
    def distinct(cls) -> QuerySet[Self]:
        """Return a query set selecting only distinct rows.

        Returns:
            A new ``QuerySet`` rendering ``SELECT DISTINCT``.
        """
        return cls._meta.manager.get_queryset().distinct()

    @classmethod
    def select_for_update(
        cls,
        nowait: bool = False,
        skip_locked: bool = False,
        of: tuple[str, ...] = (),
    ) -> QuerySet[Self]:
        """Return a query set that locks matched rows (``FOR UPDATE``).

        Args:
            nowait: Emit ``NOWAIT`` so a contended lock errors instead of waiting.
            skip_locked: Emit ``SKIP LOCKED`` to skip already-locked rows.
            of: Table/relation names to lock (``FOR UPDATE OF ...``).

        Returns:
            A new ``QuerySet`` locking the selected rows.
        """
        return cls._meta.manager.get_queryset().select_for_update(
            nowait=nowait, skip_locked=skip_locked, of=of
        )

    @classmethod
    async def values(cls, *fields: str, **aliases: str) -> list[dict[str, Any]]:
        """Return all rows as dicts of the requested columns.

        Args:
            *fields: Field names/paths to select; defaults to all model fields.
            **aliases: ``output_name=field_path`` pairs for traversed columns.

        Returns:
            A list of dicts mapping each requested name to its value.
        """
        return await cls._meta.manager.get_queryset().values(*fields, **aliases)

    @classmethod
    async def values_list(cls, *fields: str, flat: bool = False) -> list[Any]:
        """Return all rows as tuples (or scalars when ``flat=True``).

        Args:
            *fields: Field names to select; defaults to all model fields.
            flat: When ``True`` return scalar values for a single field.

        Returns:
            A list of tuples, or a list of scalars when ``flat`` is ``True``.
        """
        return await cls._meta.manager.get_queryset().values_list(*fields, flat=flat)

    @classmethod
    async def raw(cls, sql: str, params: list[Any] | None = None) -> list[Self]:
        """Run raw SQL returning this model's instances (positional columns).

        Args:
            sql: The SQL query to execute; columns must be in field-list order.
            params: Optional bind parameters for the query.

        Returns:
            A list of model instances built from the returned rows.
        """
        executor = get_executor(cls)
        rows = await executor.fetch_rows(sql, params or [])
        return cls._from_db_rows(rows)

    @classmethod
    async def _simple_equality_rows(
        cls, kwargs: dict[str, Any], limit: int
    ) -> list[list[Any]] | None:
        """Direct SELECT for plain field-equality lookups, bypassing QuerySet.

        Returns positional rows or ``None`` when the lookup is not a simple
        equality on concrete columns (operator, relation name, ...), so the
        caller falls back to the full query builder.

        Args:
            kwargs: Field-equality lookups keyed by field name (or ``"pk"``).
            limit: Maximum number of rows to fetch.

        Returns:
            Positional rows for the lookup, or ``None`` if it is not a simple
            equality lookup.
        """
        meta = cls._meta
        # A custom manager may scope the base queryset, so skip the fast path
        # (which selects straight from the table) and use the full builder.
        if type(meta.manager) is not Manager:
            return None
        if not kwargs or any(
            ("__" in key) or (key != "pk" and key not in meta.fields) for key in kwargs
        ):
            return None
        dialect = get_dialect(cls)
        engine = get_executor(cls)
        meta.compile(dialect)
        # The rendered SELECT depends only on the kwarg names (in call order),
        # which of the values are NULL, the dialect and the limit — memoise it
        # (with the per-value ``to_db`` binders) so repeated lookups of the
        # same shape skip re-quoting/re-rendering the identical statement.
        cache_key = (dialect.name, tuple(kwargs), tuple(v is None for v in kwargs.values()), limit)
        cached = meta._simple_lookup_cache.get(cache_key)
        if cached is None:
            clauses = []
            converters: list[Callable[[Any], Any]] = []
            idx = 1
            for key, value in kwargs.items():
                field = meta.get_field(key)
                if value is None:
                    # ``field=None`` is NULL identity, not ``col = NULL`` (always
                    # UNKNOWN); mirror the QuerySet builder so get()/get_or_create
                    # match existing NULL rows instead of inserting duplicates.
                    clauses.append(f"{dialect.quote(field.db_column)} IS NULL")
                    continue
                clauses.append(f"{dialect.quote(field.db_column)} = {dialect.placeholder(idx)}")
                converters.append(field.to_db)
                idx += 1
            # Honour ``Meta.ordering`` like the QuerySet fallback path does, so a
            # multi-match lookup (``get_or_none``) picks the same deterministic row
            # on either path instead of an arbitrary one.
            order = ""
            if meta.ordering:
                parts = [
                    f"{dialect.quote(meta.get_field(name).db_column)} {'DESC' if desc else 'ASC'}"
                    for name, desc in meta.ordering
                ]
                order = f" ORDER BY {', '.join(parts)}"
            if not order and limit is not None:
                # SQL Server's OFFSET/FETCH needs a preceding ORDER BY; borrow the
                # dialect's placeholder ordering when the lookup imposes none.
                order = dialect.offset_order_fallback()
            tail = dialect.limit_offset_sql(limit, None)
            sql = f"{meta.select_prefix} WHERE {' AND '.join(clauses)}{order}{tail}"
            cached = meta._simple_lookup_cache[cache_key] = (sql, converters)
        sql, converters = cached
        params = [
            to_db(value)
            for to_db, value in zip(converters, (v for v in kwargs.values() if v is not None))
        ]
        return await engine.fetch_rows(sql, params)

    @classmethod
    def get(
        cls, *, using_db: str | BaseDBAsyncClient | None = None, **kwargs: Any
    ) -> QuerySetSingle[Self]:
        """Return a chainable, awaitable single-row result for the lookups.

        ``await Model.get(id=x)`` resolves to the instance (via a fast path);
        ``await Model.get(id=x).prefetch_related(...)`` chains as a single-row
        result and applies the prefetch/select before resolving.

        Args:
            using_db: Optional connection name/object to run on.
            **kwargs: Field lookups identifying exactly one row.

        Returns:
            A ``QuerySetSingle`` resolving to the matching instance.
        """
        if using_db is not None:
            return QuerySetSingle(cls.filter(**kwargs).using_db(using_db), _resolve_get)
        # The queryset is passed as a factory: the fast path resolves without
        # ever building it, so its cost is paid only when the caller chains.
        return QuerySetSingle(
            lambda: cls.filter(**kwargs), _resolve_get, fast=lambda: cls._get_one(kwargs)
        )

    @classmethod
    async def _get_one(cls, kwargs: dict[str, Any]) -> Self:
        """Fast single-row fetch used when no prefetch/select is chained.

        Args:
            kwargs: Field lookups identifying exactly one row.

        Returns:
            The matching model instance.
        """
        rows = await cls._simple_equality_rows(kwargs, limit=2)
        if rows is None:
            return await cls._meta.manager.get_queryset().get(**kwargs)
        if not rows:
            raise cls.DoesNotExist(f"{cls.__name__} matching query does not exist")
        if len(rows) > 1:
            raise cls.MultipleObjectsReturned(f"Multiple {cls.__name__} objects returned")
        return cls._from_db_row(rows[0])

    @classmethod
    async def get_or_none(cls, **kwargs: Any) -> Self | None:
        """Fetch the instance matching the lookups, or ``None`` if absent.

        Args:
            **kwargs: Field lookups identifying at most one row.

        Returns:
            The matching model instance, or ``None`` when no row matches.
        """
        rows = await cls._simple_equality_rows(kwargs, limit=1)
        if rows is None:
            return await cls._meta.manager.get_queryset().filter(**kwargs).first()
        return cls._from_db_row(rows[0]) if rows else None

    @classmethod
    async def create(
        cls, *, using_db: str | BaseDBAsyncClient | None = None, **kwargs: Any
    ) -> Self:
        """Construct an instance from the given values and save it.

        Args:
            using_db: Optional connection name/object to run on.
            **kwargs: Field values to initialise the new instance with.

        Returns:
            The newly created, persisted instance.
        """
        obj = cls(**kwargs)
        await obj.save(using_db=using_db)
        return obj

    @classmethod
    async def get_or_create(
        cls,
        defaults: dict[str, Any] | None = None,
        using_db: str | BaseDBAsyncClient | None = None,
        **kwargs: Any,
    ) -> tuple[Self, bool]:
        """Fetch the row matching ``kwargs`` or create it.

        Args:
            defaults: Extra field values used only when creating the row.
            using_db: Optional connection name/object to run on.
            **kwargs: Lookups identifying the row and reused on creation.

        Returns:
            A ``(instance, created)`` tuple; ``created`` is ``True`` when a new
            row was inserted.
        """
        try:
            return await cls.get(using_db=using_db, **kwargs), False
        except DoesNotExist:
            return await cls.create(using_db=using_db, **{**kwargs, **(defaults or {})}), True

    @classmethod
    async def update_or_create(
        cls,
        defaults: dict[str, Any] | None = None,
        using_db: str | BaseDBAsyncClient | None = None,
        **kwargs: Any,
    ) -> tuple[Self, bool]:
        """Update the row matching ``kwargs`` with ``defaults``, or create it.

        Args:
            defaults: Field values to set (on update) or add (on create).
            using_db: Optional connection name/object to run on.
            **kwargs: Lookups identifying the row and reused on creation.

        Returns:
            A ``(instance, created)`` tuple; ``created`` is ``True`` when a new
            row was inserted.
        """
        defaults = defaults or {}
        try:
            obj = await cls.get(using_db=using_db, **kwargs)
        except DoesNotExist:
            return await cls.create(using_db=using_db, **{**kwargs, **defaults}), True
        if defaults:
            obj.update_from_dict(defaults)
            await obj.save(using_db=using_db)
        return obj, False

    @classmethod
    def _natural_key(cls, values: dict[str, Any] | Model, key_fields: tuple[str, ...]) -> tuple:
        """Build a comparable key tuple from a record dict or an instance.

        Both sides are normalised through ``field.to_db`` (the canonical
        loose-input coercion lever), so a record carrying ``"42"`` or an ISO
        date string matches the typed attribute of a fetched instance instead
        of silently missing and duplicating the row.

        Args:
            values: A record mapping or a model instance.
            key_fields: The field names forming the natural key.

        Returns:
            The normalised key tuple.
        """
        meta = cls._meta
        out = []
        for name in key_fields:
            if isinstance(values, dict):
                value = values[name]  # ty: ignore[invalid-argument-type]
            else:
                value = getattr(values, name)
            field = meta.fields.get(name)
            out.append(field.to_db(value) if field is not None else value)
        return tuple(out)

    @classmethod
    async def _existing_by_key(
        cls, records: list[dict[str, Any]], key_fields: tuple[str, ...]
    ) -> dict[tuple, Self]:
        """Return ``{key_tuple: instance}`` for records that already exist.

        Fetches candidates in a single query (the first key field's ``__in``) and
        matches the full key tuple in memory, so a composite key still costs one
        round-trip. Keys are normalised via :meth:`_natural_key`.

        Args:
            records: The records whose keys to look up.
            key_fields: The field names forming the natural key.

        Returns:
            A mapping of normalised key tuple to the existing instance.
        """
        first = key_fields[0]
        # ``Any``: the values feed a ``filter(**{...})`` unpacking, whose dict
        # value type must stay assignable to every keyword parameter.
        values: Any = list({rec[first] for rec in records})
        wanted = {cls._natural_key(rec, key_fields) for rec in records}
        out: dict[tuple, Self] = {}
        for obj in await cls.filter(**{f"{first}__in": values}):
            key = cls._natural_key(obj, key_fields)
            if key in wanted:
                out[key] = obj
        return out

    @classmethod
    async def bulk_get_or_create(
        cls,
        records: Iterable[dict[str, Any]],
        key_fields: Iterable[str],
        defaults: dict[str, Any] | None = None,
        batch_size: int = 500,
    ) -> list[tuple[Self, bool]]:
        """Fetch or create many rows in as few queries as possible.

        Existing rows are matched by ``key_fields`` in one query; the missing ones
        are inserted with a single ``bulk_create``. A key repeated within the
        batch resolves to the same instance (created once).

        Args:
            records: One mapping of field values per row (the key values plus any
                values used only when creating).
            key_fields: The field names identifying an existing row.
            defaults: Extra field values applied only to newly created rows.
            batch_size: Maximum rows per INSERT statement for the created rows.

        Returns:
            A ``(instance, created)`` tuple per input record, in input order.
        """
        records = list(records)
        keys = tuple(key_fields)
        if not keys:
            raise ValueError("bulk_get_or_create requires at least one key field")
        if not records:
            return []
        defaults = defaults or {}
        existing = await cls._existing_by_key(records, keys)
        results: list[tuple[Self, bool]] = []
        to_create: list[Self] = []
        for rec in records:
            key = cls._natural_key(rec, keys)
            found = existing.get(key)
            if found is not None:
                results.append((found, False))
            else:
                obj = cls(**{**rec, **defaults})
                existing[key] = obj  # dedupe repeats of a new key within the batch
                to_create.append(obj)
                results.append((obj, True))
        if to_create:
            await cls.bulk_create(to_create, batch_size=batch_size)
        return results

    @classmethod
    async def bulk_update_or_create(
        cls,
        records: Iterable[dict[str, Any]],
        key_fields: Iterable[str],
        update_fields: Iterable[str] | None = None,
        batch_size: int = 500,
    ) -> list[tuple[Self, bool]]:
        """Update existing rows (matched by ``key_fields``) or create missing ones.

        Existing rows are fetched in one query and updated with a single
        ``bulk_update``; missing rows are inserted with one ``bulk_create``.

        Args:
            records: One mapping of field values per row.
            key_fields: The field names identifying an existing row.
            update_fields: Field names to overwrite on existing rows; defaults to
                every non-key field present in the records.
            batch_size: Maximum rows per statement for the created/updated rows.

        Returns:
            A ``(instance, created)`` tuple per input record, in input order.
        """
        records = list(records)
        keys = tuple(key_fields)
        if not keys:
            raise ValueError("bulk_update_or_create requires at least one key field")
        if not records:
            return []
        if update_fields is not None:
            updates = list(update_fields)
        else:
            # Every non-key field present in *any* record (per the docstring);
            # records may be heterogeneous, so the first one is not enough.
            seen_fields: dict[str, None] = {}
            for rec in records:
                for f in rec:
                    if f not in keys:
                        seen_fields[f] = None
            updates = list(seen_fields)
        existing = await cls._existing_by_key(records, keys)
        pending: dict[tuple, Self] = {}  # new keys created in this batch
        results: list[tuple[Self, bool]] = []
        to_create: list[Self] = []
        to_update: dict[int, Self] = {}  # id(obj) -> obj, deduped
        for rec in records:
            key = cls._natural_key(rec, keys)
            found = existing.get(key)
            if found is not None:
                found.update_from_dict({f: rec[f] for f in updates if f in rec})
                to_update[id(found)] = found
                results.append((found, False))
            elif key in pending:
                results.append((pending[key], False))  # duplicate of an in-batch create
            else:
                obj = cls(**rec)
                pending[key] = obj
                to_create.append(obj)
                results.append((obj, True))
        if to_create:
            await cls.bulk_create(to_create, batch_size=batch_size)
        if to_update:
            # bulk_update no-ops when ``updates`` is empty (all fields are keys).
            await cls.bulk_update(list(to_update.values()), fields=updates, batch_size=batch_size)
        return results

    @classmethod
    async def in_bulk(cls, id_list: Iterable[Any], field_name: str = "pk") -> dict[Any, Self]:
        """Fetch instances keyed by ``field_name`` for the given values.

        Args:
            id_list: The values to look up.
            field_name: The field to match and key the result by (default pk).

        Returns:
            A dict mapping each present key to its instance.
        """
        # ``Any``: the ids feed a ``filter(**{...})`` unpacking, whose dict
        # value type must stay assignable to every keyword parameter.
        ids: Any = list(id_list)
        if not ids:
            return {}
        objects = await cls.filter(**{f"{field_name}__in": ids})
        key = cls._meta.pk_field.model_field_name if field_name == "pk" else field_name
        return {getattr(obj, key): obj for obj in objects}

    @classmethod
    async def bulk_create(
        cls,
        objects: Iterable[Self],
        batch_size: int = 500,
        ignore_conflicts: bool = False,
        update_fields: Iterable[str] | None = None,
        on_conflict: Iterable[str] | None = None,
    ) -> list[Self]:
        """Insert many instances using one multi-row INSERT per batch.

        Each batch is a single prepared statement (parsed once, cached on the
        connection), so the whole batch is one round-trip. The per-batch
        placeholder string is built once and reused.

        With no conflict handling, generated primary keys are written back onto
        the instances. When ``ignore_conflicts`` or ``update_fields`` is set an
        ``ON CONFLICT`` clause is emitted and primary keys are **not** written
        back (the database may insert, skip or update each row).

        MySQL caveats: without ``INSERT ... RETURNING``, generated primary
        keys are backfilled arithmetically from the batch's first
        auto-increment id — correct under the default
        ``innodb_autoinc_lock_mode`` consecutive allocation, but not with a
        non-default interleaved mode. Conflict handling matches against *any*
        unique key (``INSERT IGNORE`` / ``ON DUPLICATE KEY UPDATE``): an
        explicit ``on_conflict`` target cannot be narrowed there.

        Args:
            objects: The instances to insert.
            batch_size: Maximum number of rows per INSERT statement.
            ignore_conflicts: Emit ``ON CONFLICT DO NOTHING`` to skip rows that
                violate a unique constraint.
            update_fields: Field names to overwrite on conflict (upsert via
                ``ON CONFLICT ... DO UPDATE``); mutually exclusive intent with
                ``ignore_conflicts`` (update wins if both are given).
            on_conflict: Field names forming the conflict target; defaults to
                the primary key when ``update_fields`` is set.

        Returns:
            The list of instances (primary keys populated only when no conflict
            handling was requested).
        """
        objects = list(objects)
        if not objects:
            return objects

        dialect = get_dialect(cls)
        engine = get_executor(cls, write=True)
        meta = cls._meta
        pk_field = meta.pk_field
        table = dialect.quote(meta.table)

        def column_of(name: str) -> str:
            """Resolve a field/relation name to its database column."""
            return meta.resolve_writable_field(name).db_column

        upsert = ignore_conflicts or update_fields is not None
        update_cols: list[str] = []
        conflict_cols: list[str] = []
        if upsert:
            update_cols = [column_of(n) for n in (update_fields or ())]
            if on_conflict is not None:
                conflict_cols = [column_of(n) for n in on_conflict]
            # ``DO UPDATE`` is invalid without a conflict target, so when the
            # caller names update_fields but gives no target — ``on_conflict``
            # omitted *or* an empty list — fall back to the primary key. (A bare
            # ``DO NOTHING`` with no update_cols is fine targetless.)
            if update_cols and not conflict_cols:
                conflict_cols = [pk_field.db_column]
            # ``not on_conflict``: an empty list means "no explicit target"
            # exactly like None does, so it must take the same substitution —
            # otherwise the pk fallback above leaves MERGE matching on the
            # (uninserted) auto pk column.
            if dialect.upsert_requires_conflict_target and not on_conflict:
                # SQL Server's MERGE must name real match columns present in the
                # inserted set; INSERT IGNORE / ON CONFLICT DO NOTHING catch any
                # unique violation implicitly, and default to the (uninserted)
                # auto pk. Substitute the model's unique columns as the target.
                auto_pk = pk_field.db_column if pk_field.auto_increment else None
                if not conflict_cols or conflict_cols == [auto_pk]:
                    unique_cols = [
                        f.db_column
                        for f in meta.fields.values()
                        if f.unique and not (f is pk_field and f.auto_increment)
                    ]
                    if unique_cols:
                        conflict_cols = unique_cols

        # An explicit value on an auto-increment pk must reach the INSERT —
        # ``save()`` honours one (via the dialect's identity-insert wrapper), so
        # ``bulk_create`` must not silently discard the supplied ids and rewrite
        # them with fresh serials. Mirror save()'s per-instance "pk unset" test;
        # a mixed batch is rejected rather than split, since splitting would
        # silently reorder the inserts. Conflict handling keeps its documented
        # behaviour (the pk column stays out of the statement).
        pk_attr = pk_field.model_field_name
        explicit_pks = False
        if pk_field.auto_increment and not upsert:
            with_pk = sum(1 for obj in objects if getattr(obj, pk_attr, None) is not None)
            if with_pk == len(objects):
                explicit_pks = True
            elif with_pk:
                raise ValueError(
                    f"bulk_create() got a mix of instances with and without an "
                    f"explicit {pk_attr!r}; set the primary key on all instances "
                    f"or on none"
                )

        # SQL Server also lacks a RETURNING suffix, but it cannot reuse MySQL's
        # first-id arithmetic below: SQL Server does not guarantee consecutive
        # identity values for one multi-row INSERT under concurrency, so
        # ``first + offset`` could silently assign *other rows'* ids. Render
        # T-SQL's own returning form instead — an ``OUTPUT INSERTED.<pk>``
        # clause between the column list and VALUES — and read the real
        # generated ids back. Caveat: OUTPUT without INTO fails on tables with
        # triggers (the usual ORM trade-off).
        output_inserted = (
            dialect.name == "mssql" and pk_field.auto_increment and not upsert and not explicit_pks
        )

        base_fields = [
            f
            for f in meta.fields.values()
            if not (f is pk_field and f.auto_increment and not explicit_pks)
            and not isinstance(f.default, DatabaseDefault)
        ]
        db_default_fields = [
            f
            for f in meta.fields.values()
            if isinstance(f.default, DatabaseDefault) and not (f is pk_field and f.auto_increment)
        ]

        # A database-default column is omitted so the DB fills it — but an
        # object carrying an explicit value must have it inserted, not silently
        # replaced by the default. Group rows by which of those columns they
        # set, so each group's INSERT lists exactly the supplied columns.
        if db_default_fields:
            groups: dict[tuple[str, ...], list[Self]] = {}
            for obj in objects:
                sig = tuple(
                    f.model_field_name
                    for f in db_default_fields
                    if getattr(obj, f.model_field_name, None) is not None
                )
                groups.setdefault(sig, []).append(obj)
        else:
            groups = {(): objects}

        # ``auto_now``/``auto_now_add`` columns share one timestamp for the whole
        # call (one ``now()`` instead of one per field per object) — all rows of
        # a batch are inserted together, so a single creation instant is also
        # the more faithful value.
        auto_now_fields = meta.auto_now_fields
        now = _tz.now() if auto_now_fields else None

        for sig, group in groups.items():
            insert_fields = base_fields + [meta.fields[name] for name in sig]
            ncols = len(insert_fields)
            if not ncols:
                # Only an auto-increment pk: DEFAULT VALUES is single-row, so
                # insert each object on its own (a degenerate but valid case).
                for obj in group:
                    await obj.save()
                continue
            # Keep batches under the dialect's bind-parameter ceiling
            # (65535 on PostgreSQL, 2100 on SQL Server).
            size = min(batch_size, max(1, dialect.max_bind_params // ncols))
            # Oracle has no multi-row ``VALUES (...), (...)`` INSERT: a plain
            # bulk insert falls back to one single-row statement per object
            # (its RETURNING pk backfill needs a single-row DML anyway). The
            # upsert path renders a MERGE, which is inherently multi-row.
            if not upsert and not dialect.supports_multirow_insert:
                size = 1
            columns = ", ".join(dialect.quote(f.db_column) for f in insert_fields)

            def values_clause(nrows: int, ncols: int = ncols) -> str:
                """Build the ``VALUES`` placeholder groups for ``nrows`` rows.

                Args:
                    nrows: Number of rows in the batch.
                    ncols: Number of columns per row.

                Returns:
                    The comma-separated parenthesised placeholder groups.
                """
                rows = []
                idx = 1
                for _ in range(nrows):
                    holes = ", ".join(dialect.placeholder(idx + j) for j in range(ncols))
                    rows.append(f"({holes})")
                    idx += ncols
                return ", ".join(rows)

            column_names = [f.db_column for f in insert_fields]

            def build_sql(
                nrows: int, columns: str = columns, column_names: list = column_names
            ) -> str:
                """Build the multi-row INSERT statement for ``nrows`` rows.

                Args:
                    nrows: Number of rows the statement should insert.
                    columns: The rendered column list for this group.
                    column_names: The unquoted column names (for the dialect's
                        upsert renderer).

                Returns:
                    The complete INSERT SQL string (with conflict/RETURNING).
                """
                if upsert:
                    # Conflict handling has no RETURNING (skipped rows could not
                    # be matched back to objects); the dialect renders the whole
                    # statement (ON CONFLICT suffix, or a MERGE on Oracle).
                    return dialect.render_upsert(
                        table, column_names, nrows, conflict_cols, update_cols, [pk_field.db_column]
                    )
                if explicit_pks:
                    # Caller-supplied ids for a serial/IDENTITY column: no
                    # RETURNING (the pks are already known) and the dialect's
                    # identity-insert wrapper, exactly as in ``save()`` (a
                    # no-op except on SQL Server).
                    return dialect.identity_insert_sql(
                        table, f"INSERT INTO {table} ({columns}) VALUES {values_clause(nrows)}"
                    )
                if output_inserted:
                    # T-SQL's returning clause sits mid-statement, so it cannot
                    # ride the suffix path below.
                    return (
                        f"INSERT INTO {table} ({columns}) "
                        f"OUTPUT INSERTED.{dialect.quote(pk_field.db_column)} "
                        f"VALUES {values_clause(nrows)}"
                    )
                # RETURNING is omitted on dialects without it (MySQL reports the
                # batch's first auto-increment id instead; see the backfill
                # below); the dialect renders its own clause (Oracle uses
                # ``RETURNING ... INTO`` OUT binds).
                ret = (
                    dialect.insert_returning_clause([pk_field])
                    if dialect.supports_insert_returning
                    else ""
                )
                return f"INSERT INTO {table} ({columns}) VALUES {values_clause(nrows)}{ret}"

            # Pre-build the statement shared by every full-size batch, and
            # resolve the per-column binder/attribute pairs once per group
            # instead of two attribute lookups per column per row.
            full_sql = build_sql(size) if len(group) >= size else None
            bind_plan = [(f.to_db, f.model_field_name) for f in insert_fields]

            for start in range(0, len(group), size):
                batch = group[start : start + size]
                # ``full_sql`` is always built when a full-size batch exists
                # (len(batch) == size implies len(group) >= size).
                sql = cast("str", full_sql) if len(batch) == size else build_sql(len(batch))
                params: list = []
                append = params.append
                for obj in batch:
                    if auto_now_fields:
                        # Inline ``_apply_auto_now`` with the shared ``now`` and
                        # direct ``__dict__`` writes; discard any unfetched-mark
                        # first (mirroring the ``__setattr__`` override).
                        d = obj.__dict__
                        unfetched = d.get("_unfetched_db_defaults")
                        in_db = obj._in_db
                        for fname, f in auto_now_fields:
                            # Same rule as ``_apply_auto_now``: an already-
                            # persisted row never gets its add-stamp rewritten.
                            if not (f.auto_now_add and in_db):
                                if unfetched:
                                    unfetched.discard(fname)
                                d[fname] = now
                    for to_db, attr in bind_plan:
                        append(to_db(getattr(obj, attr, None)))
                if upsert:
                    await engine.execute(sql, params)
                    continue
                if explicit_pks:
                    # The objects already carry their (verified-set) ids;
                    # nothing to read back and no backfill to apply.
                    await engine.execute(sql, params)
                    for obj in batch:
                        obj._in_db = True
                        obj._mark_unfetched_db_defaults()
                elif dialect.supports_insert_returning:
                    returned = await engine.fetch_rows(sql, params)
                    for obj, row in zip(batch, returned):
                        setattr(obj, pk_field.model_field_name, pk_field.to_python(row[0]))
                        obj._in_db = True
                        obj._mark_unfetched_db_defaults()
                elif output_inserted:
                    # OUTPUT rows are not guaranteed to arrive in VALUES order;
                    # identity values within one statement *are* assigned in
                    # row order (ascending), so sort the ids to restore batch
                    # order before zipping.
                    returned = await engine.fetch_rows(sql, params)
                    for obj, new_id in zip(batch, sorted(row[0] for row in returned)):
                        setattr(obj, pk_field.model_field_name, pk_field.to_python(new_id))
                        obj._in_db = True
                        obj._mark_unfetched_db_defaults()
                elif pk_field.auto_increment:
                    # No RETURNING (MySQL): a multi-row INSERT reports the
                    # *first* generated id. With the default
                    # innodb_autoinc_lock_mode (consecutive allocation for a
                    # simple INSERT), the batch's ids are first..first+n-1 in
                    # row order, so they are backfilled arithmetically — the
                    # same assumption Django and SQLAlchemy make on MySQL.
                    row = await engine.fetch_row(sql, params)
                    first = int(row[0])
                    for offset, obj in enumerate(batch):
                        setattr(
                            obj,
                            pk_field.model_field_name,
                            pk_field.to_python(first + offset),
                        )
                        obj._in_db = True
                        obj._mark_unfetched_db_defaults()
                else:
                    # Client-supplied pks (uuid, natural keys): nothing to read
                    # back — the objects already carry their primary keys.
                    await engine.execute(sql, params)
                    for obj in batch:
                        obj._in_db = True
                        obj._mark_unfetched_db_defaults()
        return objects

    @classmethod
    async def bulk_update(
        cls, objects: Iterable[Self], fields: Iterable[str], batch_size: int = 500
    ) -> int:
        """Update the given ``fields`` of many instances in batched statements.

        Each batch issues a single ``UPDATE ... SET col = CASE pk ... END``
        statement, so a batch is one round-trip rather than one per row.

        Args:
            objects: The instances to update (each must have a primary key).
            fields: Names of the fields to write back.
            batch_size: Maximum number of rows per ``UPDATE`` statement.

        Returns:
            The total number of rows updated.
        """
        objects = list(objects)
        field_names = list(fields)
        if not objects or not field_names:
            return 0
        dialect = get_dialect(cls)
        engine = get_executor(cls, write=True)
        meta = cls._meta
        # ``auto_now`` (updated_at-style) columns bump on every update: set each
        # object's value to now and include the column in the SET list even when
        # the caller did not list it. ``auto_now_add`` columns are left alone.
        auto_now_names = [name for name, f in meta.auto_now_fields if f.auto_now]
        if auto_now_names:
            for obj in objects:
                obj._apply_auto_now()
            for name in auto_now_names:
                if name not in field_names:
                    field_names.append(name)
        pk_field = meta.pk_field
        q = dialect.quote
        table = q(meta.table)
        # (writable field, read attribute) — resolved once here, not per object.
        # For a relation the value comes from the FK backing column
        # (``<name>_id``), never the relation accessor: the accessor returns a
        # ``ForwardRelation`` awaitable whenever only the id was set (not a model
        # instance), which would otherwise be bound verbatim. The descriptor
        # stores the pk in that column on model assignment too, so reading it
        # covers both ``author=obj`` and ``author_id=1``.
        targets = [
            (
                meta.resolve_writable_field(name),
                meta.relations[name].source_attr if name in meta.relations else name,
            )
            for name in field_names
        ]
        total = 0
        for start in range(0, len(objects), batch_size):
            batch = objects[start : start + batch_size]
            params: list[Any] = []
            idx = 1
            set_parts = []
            for field, read_attr in targets:
                whens = []
                for obj in batch:
                    # Read the backing column straight from ``__dict__`` and
                    # raise if it is absent. A missing column means the instance
                    # was narrowed with only()/defer() and never loaded it; an
                    # FK source column has no class-level descriptor, so a
                    # ``getattr`` default would silently bind NULL here and wipe
                    # the foreign key instead of erroring like a deferred regular
                    # column does.
                    if read_attr not in obj.__dict__:
                        raise FieldError(
                            f"cannot bulk_update {field.model_field_name!r}: the "
                            f"column {read_attr!r} is not loaded on an instance "
                            f"(deferred via only()/defer()); re-fetch it first"
                        )
                    value = obj.__dict__[read_attr]
                    whens.append(
                        f"WHEN {dialect.placeholder(idx)} THEN {dialect.placeholder(idx + 1)}"
                    )
                    params.extend([pk_field.to_db(obj.pk), field.to_db(value)])
                    idx += 2
                # ``ELSE <column>`` anchors the CASE result type to the column,
                # so PostgreSQL unifies the untyped placeholders to it instead
                # of defaulting them to text (it never fires: WHERE limits the
                # statement to the batch's primary keys).
                col = q(field.db_column)
                set_parts.append(
                    f"{col} = CASE {q(pk_field.db_column)} {' '.join(whens)} ELSE {col} END"
                )
            holes = []
            for obj in batch:
                holes.append(dialect.placeholder(idx))
                params.append(pk_field.to_db(obj.pk))
                idx += 1
            sql = (
                f"UPDATE {table} SET {', '.join(set_parts)} "
                f"WHERE {q(pk_field.db_column)} IN ({', '.join(holes)})"
            )
            total += await engine.execute(sql, params)
        return total

    async def refresh_from_db(
        self, fields: Iterable[str] | None = None, using_db: str | BaseDBAsyncClient | None = None
    ) -> Self:
        """Reload this instance's column values from the database.

        Args:
            fields: Optional field names to reload; ``None``
                reloads every field.
            using_db: Optional connection name/object to run on.

        Returns:
            ``self``, with the requested fields refreshed from its persisted row.
        """
        meta = self._meta
        fresh = await type(self).get(pk=self.pk, using_db=using_db)
        targets = (
            [meta.get_field(name) for name in fields] if fields is not None else meta.field_list
        )
        for field in targets:
            setattr(self, field.model_field_name, getattr(fresh, field.model_field_name))
        return self

    def update_from_dict(self, data: dict[str, Any]) -> Self:
        """Set attributes from ``data`` in place (without saving).

        Unknown keys raise unless ``Meta.extra_kwargs == "store"``, in which case
        they are kept as plain attributes (a lenient mode).

        Args:
            data: Mapping of field or relation name to its new value.

        Returns:
            ``self``, for chaining (call ``save()`` to persist).
        """
        meta = self._meta
        for key, value in data.items():
            if key in meta.fields or key in meta.relations:
                setattr(self, key, value)
            elif meta.extra_kwargs == "store":
                self.__dict__[key] = value
            else:
                raise FieldError(f"{type(self).__name__} has no field {key!r}")
        return self

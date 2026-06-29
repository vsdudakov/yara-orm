"""Model base class and metaclass."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, ClassVar, cast

from . import registry, signals
from . import timezone as _tz
from .connection import get_dialect, get_executor
from .db_defaults import DatabaseDefault
from .exceptions import DoesNotExist, FieldError, MultipleObjectsReturned
from .fields import (
    DatetimeField,
    Field,
    ForeignKeyField,
    IntField,
    ManyToManyField,
)
from .manager import Manager
from .prefetch import prefetch_instances
from .queryset import QuerySet
from .relations import (
    ForwardRelationDescriptor,
    M2MDescriptor,
    M2MInfo,
    RelationInfo,
)

if TYPE_CHECKING:
    from .dialects import BaseDialect


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
        constraints: list[Any] | None = None,
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
        #: The model's manager; rebound to the declared/default one by the
        #: metaclass once the class object exists.
        self.manager: Manager = Manager()
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
        self.decoders = [
            (f.model_field_name, None if f.read_identity else f.to_python) for f in self.field_list
        ]
        self._build_decode_plan()
        self._compiled_for: str | None = None
        # Memoised partial-UPDATE statements for ``save(update_fields=...)``,
        # keyed by ``(dialect, ordered db columns)``. A given column set always
        # maps to the same SQL, so entries never need invalidating.
        self._partial_update_cache: dict[tuple[str, tuple[str, ...]], str] = {}

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

    def compile(self, dialect: BaseDialect) -> None:
        """Build and cache dialect-specific SQL once (idempotent per dialect).

        Args:
            dialect: The SQL dialect used to quote names and build placeholders.

        Returns:
            None
        """
        if self._compiled_for == dialect.name:
            return
        # The field set may have changed since construction (migrations); refresh
        # the hydration plan so it stays in sync with the current columns.
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
        ret = q(self.pk_field.db_column)
        if self.insert_fields:
            cols = ", ".join(q(f.db_column) for f in self.insert_fields)
            holes = ", ".join(dialect.placeholder(i + 1) for i in range(len(self.insert_fields)))
            self.insert_sql = (
                f"INSERT INTO {q(self.table)} ({cols}) VALUES ({holes}) RETURNING {ret}"
            )
        else:
            self.insert_sql = f"INSERT INTO {q(self.table)} DEFAULT VALUES RETURNING {ret}"

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

    Beyond a plain column group it can carry an explicit ``name`` and a partial
    ``condition`` (a raw SQL predicate), rendering ``CREATE INDEX ... WHERE
    <condition>``. Both PostgreSQL and SQLite support partial indexes.

    Example::

        class Meta:
            indexes = [Index(fields=["status"], condition="status = 'active'")]
    """

    def __init__(
        self,
        *,
        fields: list[str],
        name: str | None = None,
        condition: str | None = None,
    ) -> None:
        """Store the indexed fields and optional name/partial condition.

        Args:
            fields: Field (or forward-relation) names the index covers.
            name: Explicit index name; defaults to ``idx_<table>_<fields>``.
            condition: Optional partial-index predicate (raw SQL); ``None`` for a
                full index.

        Returns:
            None
        """
        self.fields = list(fields)
        self.name = name
        self.condition = condition

    def resolve_name(self, table: str) -> str:
        """Return this index's name, deriving a default from ``table`` if unset.

        Args:
            table: The owning table name.

        Returns:
            The explicit name, or ``idx_<table>_<field1>_<field2>...``.
        """
        return self.name or (f"idx_{table}_" + "_".join(self.fields))


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
        fk_decls: dict[str, ForeignKeyField] = {}
        m2m_decls: dict[str, ManyToManyField] = {}
        for parent in parents:
            parent_meta: MetaInfo | None = getattr(parent, "_meta", None)
            if parent_meta is not None:
                fields.update(parent_meta.fields)
        for key, value in namespace.items():
            if isinstance(value, ManyToManyField):
                m2m_decls[key] = value
            elif isinstance(value, ForeignKeyField):
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

        pk_field = next((f for f in fields.values() if f.pk), None)
        if pk_field is None:
            pk_field = IntField(pk=True)
            pk_field.model_field_name = "id"
            pk_field.db_column = "id"
            fields = {"id": pk_field, **fields}

        meta_cls = namespace.get("Meta")
        # `abstract` is intentionally read from the class's own Meta only (not
        # inherited): a concrete subclass of an abstract base is itself concrete
        # unless it redeclares `abstract = True`.
        abstract = bool(getattr(meta_cls, "abstract", False))
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
        constraints = list(getattr(meta_cls, "constraints", None) or ())

        relations = {}
        for rel_name, fk in fk_decls.items():
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

        # Additional Meta options are recorded (not silently dropped) so they
        # are introspectable via `_meta` / `describe()`. `default_connection`
        # also routes the model's statements (see connection._route); `schema`,
        # `app` and `fetch_db_defaults` are stored for tooling/forward-compat.
        cls._meta.schema = getattr(meta_cls, "schema", None)
        cls._meta.app = getattr(meta_cls, "app", None)
        cls._meta.default_connection = getattr(meta_cls, "default_connection", None)
        cls._meta.fetch_db_defaults = bool(getattr(meta_cls, "fetch_db_defaults", False))

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

        # Bind the model's manager (a declared ``Meta.manager`` or the default).
        manager = getattr(meta_cls, "manager", None) or Manager()
        manager._model = cls
        cls._meta.manager = manager

        # Install forward accessors and m2m managers as class descriptors.
        for rel_name, info in relations.items():
            setattr(cls, rel_name, ForwardRelationDescriptor(info))
        for rel_name, mm in m2m_decls.items():
            info = M2MInfo(rel_name, mm, cls, mm.reference)
            m2m[rel_name] = info
            setattr(cls, rel_name, M2MDescriptor(info, pk_field.model_field_name))

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
        self._in_db = False
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

        for name, field in meta.fields.items():
            if name in kwargs:
                setattr(self, name, kwargs.pop(name))
            elif field.db_column != name and field.db_column in kwargs:
                setattr(self, name, kwargs.pop(field.db_column))
            else:
                setattr(self, name, field.get_default())

        for rel_name, (info, value) in rel_overrides.items():
            if value is None:
                self.__dict__[info.source_attr] = None
            elif isinstance(value, Model):
                self.__dict__[info.source_attr] = value.pk
                self.__dict__.setdefault("_prefetch", {})[rel_name] = value
            else:
                self.__dict__[info.source_attr] = value

        if kwargs:
            raise TypeError(f"Unexpected keyword arguments: {sorted(kwargs)}")

    async def fetch_related(self, *names: str) -> Model:
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

    @classmethod
    def _from_db_row(cls, values: list[Any]) -> Model:
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
    def _from_db_rows(cls, rows: list[list[Any]]) -> list[Model]:
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
        out: list[Model] = []
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
    def _from_db_row_fields(cls, values: list[Any], fields: list[Field]) -> Model:
        """Build a partially-populated instance from a subset of columns.

        Powers ``only()`` / ``defer()``: only ``fields`` are set, so reading any
        other column raises ``FieldError`` (via the field descriptor) rather
        than returning a stale or wrong value.

        Args:
            values: Raw column values in ``fields`` order.
            fields: The selected fields, matching the SELECT column order.

        Returns:
            A new, partially-populated instance marked as already persisted.
        """
        obj = cls.__new__(cls)
        d = obj.__dict__
        d["_in_db"] = True
        for field, value in zip(fields, values):
            decode = None if field.read_identity else field.to_python
            d[field.model_field_name] = (
                value if (decode is None or value is None) else decode(value)
            )
        return obj

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

    async def save(self, update_fields: list[str] | None = None) -> Model:
        """Persist this instance, emitting pre/post-save signals if registered.

        Args:
            update_fields: When updating an existing row, restrict the write to
                these field names (relation names map to their FK column); an
                empty list is a no-op and unknown names raise ``FieldError``.
                ``auto_now`` columns are bumped only if named. Ignored when the
                instance is being inserted (a new row needs all its columns).
                The list is also forwarded to the save signals.

        Returns:
            This instance.
        """
        cls = type(self)
        created = not self._in_db
        self._run_validators()
        has_signals = signals._has_handlers(cls)
        executor = get_executor(cls, write=True)
        if has_signals:
            await signals.emit_pre_save(cls, self, executor, update_fields)
        await self._perform_save(executor, update_fields)
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

    async def _perform_save(self, executor: Any, update_fields: list[str] | None = None) -> None:
        """Run the INSERT or UPDATE statement that persists this instance.

        Args:
            executor: The write-capable database executor to run SQL against.
            update_fields: Optional subset of fields to write on an UPDATE; see
                :meth:`save`. Ignored for INSERTs.

        Returns:
            None
        """
        if self._in_db and update_fields is not None:
            self._apply_auto_now(only=set(update_fields))
        else:
            self._apply_auto_now()
        dialect = get_dialect(type(self))
        meta = self._meta
        meta.compile(dialect)
        pk_field = meta.pk_field
        table = dialect.quote(meta.table)

        if not self._in_db:
            pk_attr = pk_field.model_field_name
            pk_unset = pk_field.auto_increment and getattr(self, pk_attr, None) is None
            if pk_unset:
                # Fast path: reuse the cached INSERT statement, bind params only.
                params = [
                    f.to_db(getattr(self, f.model_field_name, None)) for f in meta.insert_fields
                ]
                row = await executor.fetch_row(meta.insert_sql, params)
                setattr(self, pk_attr, pk_field.to_python(row[0]))
                self._in_db = True
                return

            columns = []
            placeholders = []
            params = []
            idx = 1
            for name, field in meta.fields.items():
                value = getattr(self, name, None)
                # Omit an unset database-default column so the DB supplies it.
                if value is None and isinstance(field.default, DatabaseDefault):
                    continue
                columns.append(dialect.quote(field.db_column))
                placeholders.append(dialect.placeholder(idx))
                params.append(field.to_db(value))
                idx += 1

            returning = dialect.quote(pk_field.db_column)
            sql = (
                f"INSERT INTO {table} ({', '.join(columns)}) "
                f"VALUES ({', '.join(placeholders)}) RETURNING {returning}"
            )
            row = await executor.fetch_row(sql, params)
            setattr(self, pk_field.model_field_name, pk_field.to_python(row[0]))
            self._in_db = True
        elif update_fields is not None:
            # Partial update: write only the named fields (relation names map to
            # their FK column). The pk is the WHERE key, never an assignment.
            resolved: list[Field] = []
            seen: set[str] = set()
            for name in update_fields:
                if name in meta.relations:
                    field = meta.get_field(meta.relations[name].source_attr)
                else:
                    field = meta.get_field(name)  # raises FieldError if unknown
                if field is pk_field or field.db_column in seen:
                    continue
                seen.add(field.db_column)
                resolved.append(field)
            if not resolved:
                # Empty update_fields (or only the pk): nothing to persist.
                return
            params = [f.to_db(getattr(self, f.model_field_name, None)) for f in resolved]
            params.append(pk_field.to_db(self.pk))
            await executor.execute(meta.partial_update_sql(dialect, resolved), params)
        elif meta.update_sql is not None:
            # Reuse the cached UPDATE statement; only the bound values change.
            params = [
                f.to_db(getattr(self, f.model_field_name, None)) for f in meta.update_field_list
            ]
            params.append(pk_field.to_db(self.pk))
            await executor.execute(meta.update_sql, params)

    async def delete(self) -> None:
        """Delete this instance's row, emitting pre/post-delete signals.

        Returns:
            None
        """
        cls = type(self)
        dialect = get_dialect(cls)
        executor = get_executor(cls, write=True)
        meta = self._meta
        has_signals = signals._has_handlers(cls)
        if has_signals:
            await signals.emit_pre_delete(cls, self, executor)
        meta.compile(dialect)
        await executor.execute(meta.delete_sql, [meta.pk_field.to_db(self.pk)])
        self._in_db = False
        if has_signals:
            await signals.emit_post_delete(cls, self, executor)

    def clone(self, pk: Any = None) -> Model:
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
    def construct(cls, _from_db: bool = False, **kwargs: Any) -> Model:
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
    async def fetch_for_list(cls, instances: list[Model], *relations: Any) -> list[Model]:
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
    def all(cls) -> QuerySet:
        """Return a query set over all rows of this model.

        Returns:
            A new ``QuerySet`` bound to this model.
        """
        return cls._meta.manager.get_queryset()

    @classmethod
    def filter(cls, *args: Any, **kwargs: Any) -> QuerySet:
        """Return a query set filtered by the given conditions.

        Args:
            *args: Positional filter expressions (e.g. ``Q`` objects).
            **kwargs: Field lookups to filter by.

        Returns:
            A new ``QuerySet`` with the filters applied.
        """
        return cls._meta.manager.get_queryset().filter(*args, **kwargs)

    @classmethod
    def exclude(cls, *args: Any, **kwargs: Any) -> QuerySet:
        """Return a query set excluding rows matching the given conditions.

        Args:
            *args: Positional filter expressions (e.g. ``Q`` objects).
            **kwargs: Field lookups to exclude by.

        Returns:
            A new ``QuerySet`` with the exclusions applied.
        """
        return cls._meta.manager.get_queryset().exclude(*args, **kwargs)

    @classmethod
    def annotate(cls, **annotations: Any) -> QuerySet:
        """Return a query set with the given computed annotations.

        Args:
            **annotations: Annotation expressions keyed by output name.

        Returns:
            A new ``QuerySet`` carrying the annotations.
        """
        return cls._meta.manager.get_queryset().annotate(**annotations)

    @classmethod
    def prefetch_related(cls, *specs: Any) -> QuerySet:
        """Return a query set that prefetches the given relations.

        Args:
            *specs: Relation names or prefetch specifications to load.

        Returns:
            A new ``QuerySet`` configured to prefetch the relations.
        """
        return cls._meta.manager.get_queryset().prefetch_related(*specs)

    @classmethod
    def select_related(cls, *relations: str) -> QuerySet:
        """Return a query set that eager-loads forward FK/O2O relations by join.

        Args:
            *relations: Forward relation names to join and load in one query.

        Returns:
            A new ``QuerySet`` configured to select the relations.
        """
        return cls._meta.manager.get_queryset().select_related(*relations)

    @classmethod
    async def first(cls) -> Model | None:
        """Return the first row (by default ordering), or ``None``.

        Returns:
            The first matching instance, or ``None`` when the table is empty.
        """
        return await cls._meta.manager.get_queryset().first()

    @classmethod
    async def last(cls) -> Model | None:
        """Return the last row (by default ordering), or ``None``.

        Returns:
            The last matching instance, or ``None`` when the table is empty.
        """
        return await cls._meta.manager.get_queryset().last()

    @classmethod
    async def earliest(cls, *fields: str) -> Model | None:
        """Return the earliest row ordered ascending by ``fields``.

        Args:
            *fields: Field names to order by; defaults to the primary key.

        Returns:
            The earliest instance, or ``None`` when there are none.
        """
        return await cls._meta.manager.get_queryset().earliest(*fields)

    @classmethod
    async def latest(cls, *fields: str) -> Model | None:
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
    def distinct(cls) -> QuerySet:
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
    ) -> QuerySet:
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
    async def raw(cls, sql: str, params: list[Any] | None = None) -> list[Model]:
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
        clauses = []
        params = []
        idx = 1
        for key, value in kwargs.items():
            field = meta.get_field(key)
            clauses.append(f"{dialect.quote(field.db_column)} = {dialect.placeholder(idx)}")
            params.append(field.to_db(value))
            idx += 1
        sql = f"{meta.select_prefix} WHERE {' AND '.join(clauses)} LIMIT {limit}"
        return await engine.fetch_rows(sql, params)

    @classmethod
    async def get(cls, **kwargs: Any) -> Model:
        """Fetch the single instance matching the lookups.

        Args:
            **kwargs: Field lookups identifying exactly one row.

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
    async def get_or_none(cls, **kwargs: Any) -> Model | None:
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
    async def create(cls, **kwargs: Any) -> Model:
        """Construct an instance from the given values and save it.

        Args:
            **kwargs: Field values to initialise the new instance with.

        Returns:
            The newly created, persisted instance.
        """
        obj = cls(**kwargs)
        await obj.save()
        return obj

    @classmethod
    async def get_or_create(
        cls, defaults: dict[str, Any] | None = None, **kwargs: Any
    ) -> tuple[Model, bool]:
        """Fetch the row matching ``kwargs`` or create it.

        Args:
            defaults: Extra field values used only when creating the row.
            **kwargs: Lookups identifying the row and reused on creation.

        Returns:
            A ``(instance, created)`` tuple; ``created`` is ``True`` when a new
            row was inserted.
        """
        try:
            return await cls.get(**kwargs), False
        except DoesNotExist:
            return await cls.create(**{**kwargs, **(defaults or {})}), True

    @classmethod
    async def update_or_create(
        cls, defaults: dict[str, Any] | None = None, **kwargs: Any
    ) -> tuple[Model, bool]:
        """Update the row matching ``kwargs`` with ``defaults``, or create it.

        Args:
            defaults: Field values to set (on update) or add (on create).
            **kwargs: Lookups identifying the row and reused on creation.

        Returns:
            A ``(instance, created)`` tuple; ``created`` is ``True`` when a new
            row was inserted.
        """
        defaults = defaults or {}
        try:
            obj = await cls.get(**kwargs)
        except DoesNotExist:
            return await cls.create(**{**kwargs, **defaults}), True
        if defaults:
            obj.update_from_dict(defaults)
            await obj.save()
        return obj, False

    @classmethod
    async def in_bulk(cls, id_list: Iterable[Any], field_name: str = "pk") -> dict[Any, Model]:
        """Fetch instances keyed by ``field_name`` for the given values.

        Args:
            id_list: The values to look up.
            field_name: The field to match and key the result by (default pk).

        Returns:
            A dict mapping each present key to its instance.
        """
        ids = list(id_list)
        if not ids:
            return {}
        objects = await cls.filter(**{f"{field_name}__in": ids})
        key = cls._meta.pk_field.model_field_name if field_name == "pk" else field_name
        return {getattr(obj, key): obj for obj in objects}

    @classmethod
    async def bulk_create(
        cls,
        objects: Iterable[Model],
        batch_size: int = 500,
        ignore_conflicts: bool = False,
        update_fields: Iterable[str] | None = None,
        on_conflict: Iterable[str] | None = None,
    ) -> list[Model]:
        """Insert many instances using one multi-row INSERT per batch.

        Each batch is a single prepared statement (parsed once, cached on the
        connection), so the whole batch is one round-trip. The per-batch
        placeholder string is built once and reused.

        With no conflict handling, generated primary keys are written back onto
        the instances. When ``ignore_conflicts`` or ``update_fields`` is set an
        ``ON CONFLICT`` clause is emitted and primary keys are **not** written
        back (the database may insert, skip or update each row).

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
        returning = dialect.quote(pk_field.db_column)

        def column_of(name: str) -> str:
            """Resolve a field/relation name to its database column."""
            if name in meta.relations:
                return meta.get_field(meta.relations[name].source_attr).db_column
            return meta.get_field(name).db_column

        upsert = ignore_conflicts or update_fields is not None
        conflict_sql = ""
        if upsert:
            update_cols = [column_of(n) for n in (update_fields or ())]
            if on_conflict is not None:
                conflict_cols = [column_of(n) for n in on_conflict]
            elif update_cols:
                conflict_cols = [pk_field.db_column]
            else:
                conflict_cols = []
            conflict_sql = dialect.on_conflict_sql(conflict_cols, update_cols)

        insert_fields = [
            f
            for f in meta.fields.values()
            if not (f is pk_field and f.auto_increment)
            and not isinstance(f.default, DatabaseDefault)
        ]
        ncols = len(insert_fields)
        if not ncols:
            # Only an auto-increment pk: DEFAULT VALUES is single-row, so insert
            # each object on its own (a degenerate but valid case).
            for obj in objects:
                await obj.save()
            return objects
        # Keep batches under PostgreSQL's 65535 bind-parameter ceiling.
        batch_size = min(batch_size, max(1, 65535 // ncols))
        columns = ", ".join(dialect.quote(f.db_column) for f in insert_fields)

        def values_clause(nrows: int) -> str:
            """Build the ``VALUES`` placeholder groups for ``nrows`` rows.

            Args:
                nrows: Number of rows in the batch.

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

        def build_sql(nrows: int) -> str:
            """Build the multi-row INSERT statement for ``nrows`` rows.

            Args:
                nrows: Number of rows the statement should insert.

            Returns:
                The complete INSERT SQL string (with conflict and/or RETURNING).
            """
            # RETURNING is omitted under ON CONFLICT: skipped rows would not be
            # returned, so the row order could not be matched back to objects.
            ret = "" if upsert else f" RETURNING {returning}"
            return (
                f"INSERT INTO {table} ({columns}) VALUES {values_clause(nrows)}{conflict_sql}{ret}"
            )

        # Pre-build the statement shared by every full-size batch.
        full_sql = build_sql(batch_size) if len(objects) >= batch_size else None

        for start in range(0, len(objects), batch_size):
            batch = objects[start : start + batch_size]
            sql = full_sql if len(batch) == batch_size else build_sql(len(batch))
            params: list = []
            for obj in batch:
                obj._apply_auto_now()
                for field in insert_fields:
                    params.append(field.to_db(getattr(obj, field.model_field_name, None)))
            if upsert:
                await engine.execute(sql, params)
                continue
            returned = await engine.fetch_rows(sql, params)
            for obj, row in zip(batch, returned):
                setattr(obj, pk_field.model_field_name, pk_field.to_python(row[0]))
                obj._in_db = True
        return objects

    @classmethod
    async def bulk_update(
        cls, objects: Iterable[Model], fields: Iterable[str], batch_size: int = 500
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
        pk_field = meta.pk_field
        q = dialect.quote
        table = q(meta.table)
        targets = [
            (name, meta.get_field(meta.relations[name].source_attr))
            if name in meta.relations
            else (name, meta.get_field(name))
            for name in field_names
        ]
        total = 0
        for start in range(0, len(objects), batch_size):
            batch = objects[start : start + batch_size]
            params: list[Any] = []
            idx = 1
            set_parts = []
            for name, field in targets:
                whens = []
                for obj in batch:
                    value = getattr(obj, name)
                    if name in meta.relations and hasattr(value, "pk"):
                        value = value.pk
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

    async def refresh_from_db(self) -> Model:
        """Reload this instance's column values from the database.

        Returns:
            ``self``, with every field refreshed from its persisted row.
        """
        fresh = await type(self).get(pk=self.pk)
        for field in self._meta.field_list:
            setattr(self, field.model_field_name, getattr(fresh, field.model_field_name))
        return self

    def update_from_dict(self, data: dict[str, Any]) -> Model:
        """Set attributes from ``data`` in place (without saving).

        Args:
            data: Mapping of field or relation name to its new value.

        Returns:
            ``self``, for chaining (call ``save()`` to persist).
        """
        meta = self._meta
        for key, value in data.items():
            if key in meta.fields or key in meta.relations:
                setattr(self, key, value)
            else:
                raise FieldError(f"{type(self).__name__} has no field {key!r}")
        return self

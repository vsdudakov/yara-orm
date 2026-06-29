"""Model base class and metaclass."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, ClassVar, cast

from . import registry, signals
from .connection import get_dialect, get_executor
from .db_defaults import DatabaseDefault
from .exceptions import DoesNotExist, FieldError, MultipleObjectsReturned
from .manager import Manager
from .fields import (
    DatetimeField,
    Field,
    ForeignKeyField,
    IntField,
    ManyToManyField,
)
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
        indexes: list[tuple[str, ...]] | None = None,
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

        Returns:
            None
        """
        self.abstract = abstract
        self.ordering = ordering or []
        self.unique_together = unique_together or []
        self.indexes = indexes or []
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
        self._compiled_for: str | None = None

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
        self._compiled_for = dialect.name


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
        indexes = _normalize_field_groups(getattr(meta_cls, "indexes", None))

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
        obj = cls.__new__(cls)
        d = obj.__dict__
        d["_in_db"] = True
        for (name, decode), value in zip(cls._meta.decoders, values):
            d[name] = value if (decode is None or value is None) else decode(value)
        return obj

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        """Return a debugging representation showing the type and primary key.

        Returns:
            A string of the form ``<TypeName pk=...>``.
        """
        return f"<{type(self).__name__} pk={self.pk!r}>"

    # -- persistence ------------------------------------------------------
    def _apply_auto_now(self) -> None:
        """Set ``auto_now``/``auto_now_add`` datetime fields to the current time.

        Returns:
            None
        """
        now = datetime.now(timezone.utc)
        for name, field in self._meta.fields.items():
            if isinstance(field, DatetimeField):
                if field.auto_now or (field.auto_now_add and not self._in_db):
                    if field.auto_now_add and self._in_db:
                        continue
                    setattr(self, name, now)

    async def save(self, update_fields: list[str] | None = None) -> Model:
        """Persist this instance, emitting pre/post-save signals if registered.

        Args:
            update_fields: Optional list of field names passed through to the
                save signals; provided for signal handlers' information.

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
        await self._perform_save(executor)
        if has_signals:
            await signals.emit_post_save(cls, self, created, executor, update_fields)
        return self

    def _run_validators(self) -> None:
        """Run each field's validators against this instance's current values.

        Returns:
            None
        """
        for field in self._meta.field_list:
            if not field.validators:
                continue
            value = getattr(self, field.model_field_name, None)
            if value is None:
                continue
            for validator in field.validators:
                validator(value)

    async def _perform_save(self, executor: Any) -> None:
        """Run the INSERT or UPDATE statement that persists this instance.

        Args:
            executor: The write-capable database executor to run SQL against.

        Returns:
            None
        """
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
        else:
            assignments = []
            params = []
            idx = 1
            for name, field in meta.fields.items():
                if field is pk_field:
                    continue
                assignments.append(f"{dialect.quote(field.db_column)} = {dialect.placeholder(idx)}")
                params.append(field.to_db(getattr(self, name, None)))
                idx += 1
            params.append(pk_field.to_db(self.pk))
            sql = (
                f"UPDATE {table} SET {', '.join(assignments)} "
                f"WHERE {dialect.quote(pk_field.db_column)} = {dialect.placeholder(idx)}"
            )
            await executor.execute(sql, params)

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
        sql = (
            f"DELETE FROM {dialect.quote(meta.table)} "
            f"WHERE {dialect.quote(meta.pk_field.db_column)} = {dialect.placeholder(1)}"
        )
        await executor.execute(sql, [meta.pk_field.to_db(self.pk)])
        self._in_db = False
        if has_signals:
            await signals.emit_post_delete(cls, self, executor)

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
        return [cls._from_db_row(row) for row in rows]

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
            raise DoesNotExist(f"{cls.__name__} matching query does not exist")
        if len(rows) > 1:
            raise MultipleObjectsReturned(f"Multiple {cls.__name__} objects returned")
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
    async def bulk_create(cls, objects: Iterable[Model], batch_size: int = 500) -> list[Model]:
        """Insert many instances using one multi-row INSERT per batch.

        Each batch is a single prepared statement (parsed once, cached on the
        connection), so the whole batch is one round-trip. The per-batch
        placeholder string is built once and reused. Generated primary keys are
        written back onto the instances.

        Args:
            objects: The instances to insert.
            batch_size: Maximum number of rows per INSERT statement.

        Returns:
            The list of inserted instances with their primary keys populated.
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
                The complete INSERT SQL string with a RETURNING clause.
            """
            return (
                f"INSERT INTO {table} ({columns}) VALUES {values_clause(nrows)} "
                f"RETURNING {returning}"
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

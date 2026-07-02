"""Relation metadata, descriptors and managers.

Forward FK/O2O access is an awaitable accessor (``await obj.tournament``).
Reverse FK and M2M access is a manager that is awaitable (to a list),
chainable (``.filter`` / ``.order_by``) and async-iterable. Prefetched results
are cached on the instance under ``_prefetch`` and served without a query.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Generic, TypeVar, Union, cast

from . import registry
from .connection import get_dialect, get_executor
from .queryset import QuerySet

if TYPE_CHECKING:
    from collections.abc import Awaitable, Generator

    from .dialects import BaseDialect
    from .fields import ForeignKeyFieldInstance, ManyToManyFieldInstance
    from .models import Model
    from .queryset import Q

#: The related model a relation annotation is parameterised over, e.g.
#: ``books: ReverseRelation["Book"]``.
MODEL = TypeVar("MODEL", bound="Model")


# ---------------------------------------------------------------------------
# Relation descriptors (metadata)
# ---------------------------------------------------------------------------
class RelationInfo:
    """Describes a forward FK/O2O relation on the owning model."""

    def __init__(
        self,
        name: str,
        field: ForeignKeyFieldInstance,
        source_attr: str,
        reference: str,
        is_o2o: bool,
    ) -> None:
        """Store the metadata describing a forward FK/O2O relation.

        Args:
            name: The attribute name of the relation on the owning model.
            field: The foreign-key field backing the relation.
            source_attr: The local column holding the foreign key value.
            reference: The reference to the target model.
            is_o2o: Whether the relation is a one-to-one relation.

        Returns:
            None
        """
        self.name = name
        self.field = field
        self.source_attr = source_attr  # e.g. "tournament_id"
        self.reference = reference
        self.is_o2o = is_o2o
        self._target: type[Model] | None = None

    def resolve_target(self) -> type[Model]:
        """Resolve (and memoise) the target model class for this relation.

        Called on every forward-relation join compilation and relation access;
        the reference is fixed once models are registered, so the resolved class
        is cached after the first lookup.

        Returns:
            The model class referenced by this relation.
        """
        target = self._target
        if target is None:
            target = self._target = registry.get_model(self.reference)
        return target


class M2MInfo:
    """Describes a many-to-many relation and its join table."""

    def __init__(
        self,
        name: str,
        field: ManyToManyFieldInstance,
        owner: type[Model],
        reference: str,
    ) -> None:
        """Store the metadata describing a many-to-many relation.

        Args:
            name: The attribute name of the relation on the owning model.
            field: The many-to-many field backing the relation.
            owner: The model class that owns the relation.
            reference: The reference to the target model.

        Returns:
            None
        """
        self.name = name
        self.field = field
        self.owner = owner
        self.reference = reference
        # Resolved to concrete names by ``finalize`` when both models are known.
        self.through: str = field.through or ""
        self.forward_key: str = field.forward_key or ""  # -> target pk
        self.backward_key: str = field.backward_key or ""  # -> owner pk
        self._target: type[Model] | None = None
        self._finalized = False

    def resolve_target(self) -> type[Model]:
        """Resolve (and memoise) the target model class for this relation.

        Returns:
            The model class referenced by this relation.
        """
        target = self._target
        if target is None:
            target = self._target = registry.get_model(self.reference)
        return target

    def finalize(self) -> type[Model]:
        """Fill in defaulted table / key names once both models are known.

        Idempotent and cheap to re-call: ``M2MManager`` finalises on every
        ``obj.rel`` access, so once the defaults are filled the name-building is
        skipped.

        Returns:
            The resolved target model class.
        """
        target = self.resolve_target()
        if self._finalized:
            return target
        owner_name = self.owner.__name__.lower()
        target_name = target.__name__.lower()
        if not self.through:
            self.through = f"{owner_name}_{target_name}"
        if not self.backward_key:
            self.backward_key = f"{owner_name}_id"
        if not self.forward_key:
            self.forward_key = f"{target_name}_id"
        self._finalized = True
        return target


# ---------------------------------------------------------------------------
# Forward FK / O2O accessor
# ---------------------------------------------------------------------------
class ForwardRelationDescriptor:
    """Descriptor exposing a forward FK/O2O relation as an awaitable accessor."""

    def __init__(self, info: RelationInfo) -> None:
        """Store the relation metadata backing this descriptor.

        Args:
            info: The relation metadata describing the forward relation.

        Returns:
            None
        """
        self.info = info

    def __get__(
        self, instance: Model | None, owner: type[Model] | None
    ) -> ForwardRelationDescriptor | ForwardRelation | Model | None:
        """Return the descriptor, the prefetched instance, or an awaitable.

        Args:
            instance: The model instance the attribute is accessed on, or None.
            owner: The model class owning the descriptor.

        Returns:
            This descriptor when accessed on the class; the related instance (or
            ``None``) directly when it has been prefetched or assigned — so
            ``obj.rel.field`` and ``if obj.rel`` work after ``prefetch_related``;
            otherwise a ``ForwardRelation`` awaitable that lazily loads it.
        """
        if instance is None:
            return self
        cache = instance.__dict__.get("_prefetch")
        if cache and self.info.name in cache:
            cached = cache[self.info.name]
            if cached is None:
                # A prefetch resolved this relation to None (no row, or its
                # custom queryset excluded it); serve that result.
                return None
            # Serve a cached object only while it matches the current FK value;
            # a direct write to the ``<name>_id`` attribute (which bypasses
            # this descriptor) leaves a stale entry — drop it and reload. A
            # deferred FK column (``only()``/``defer()``) has no local value to
            # compare, so the cache is trusted as-is.
            fk = instance.__dict__.get(self.info.source_attr, _MISSING)
            if fk is _MISSING or cached.pk == fk:
                return cached
            del cache[self.info.name]
        return ForwardRelation(instance, self.info)

    def __set__(self, instance: Model, value: Model | Any) -> None:
        """Assign the related instance or raw foreign key value.

        Args:
            instance: The model instance the attribute is set on.
            value: A related model instance, a raw key value, or None.

        Raises:
            ValueError: When ``value`` is an unsaved model instance (its primary
                key is ``None``) — storing it would silently persist a NULL
                foreign key.

        Returns:
            None
        """
        # Deferred: breaks the relations <-> models import cycle.
        from .models import Model

        if isinstance(value, Model):
            if value.pk is None:
                raise ValueError(
                    f'Cannot assign "{value!r}" to {type(instance).__name__}.'
                    f"{self.info.name}: the instance isn't saved in the database "
                    f"yet; save it first"
                )
            instance.__dict__[self.info.source_attr] = value.pk
            instance.__dict__.setdefault("_prefetch", {})[self.info.name] = value
        else:
            # ``None`` or a raw key value: drop any stale cached related object
            # so the accessor reflects the new key instead of the old instance.
            instance.__dict__[self.info.source_attr] = value
            cache = instance.__dict__.get("_prefetch")
            if cache is not None:
                cache.pop(self.info.name, None)


class ForwardRelation(Generic[MODEL]):
    """Awaitable resolving to the related instance (cached if prefetched)."""

    def __init__(self, instance: Model, info: RelationInfo) -> None:
        """Bind the relation to a specific model instance.

        Args:
            instance: The model instance owning the relation.
            info: The relation metadata describing the forward relation.

        Returns:
            None
        """
        self.instance = instance
        self.info = info

    def __await__(self) -> Generator[Any, None, MODEL | None]:
        """Await resolution of the related instance.

        Returns:
            A generator yielding the related instance, or None if unset.
        """
        return self._resolve().__await__()

    async def _resolve(self) -> MODEL | None:
        """Load the related instance from the database and cache it.

        Only reached for an un-cached relation — the descriptor serves a
        prefetched/assigned relation directly, without an awaitable.

        Returns:
            The related model instance, or None if there is no foreign key.
        """
        fk = self.instance.__dict__.get(self.info.source_attr)
        if fk is None:
            return None
        target = self.info.resolve_target()
        obj = await target.get_or_none(pk=fk)
        # A miss (dangling key) is not cached: caching ``None`` would keep
        # serving it even after the row appears or the key changes.
        if obj is not None:
            self.instance.__dict__.setdefault("_prefetch", {})[self.info.name] = obj
        # The registry resolves the target dynamically; the annotation on the
        # declaring model (ForeignKeyRelation[X]) is the static source of truth.
        return cast("MODEL | None", obj)


# ---------------------------------------------------------------------------
# Reverse FK / O2O manager
# ---------------------------------------------------------------------------
class ReverseFKDescriptor:
    """Installed on the *target* model under ``related_name``."""

    def __init__(self, name: str, source_reference: str, source_attr: str, is_o2o: bool) -> None:
        """Store the metadata describing a reverse FK/O2O relation.

        Args:
            name: The attribute name of the reverse relation.
            source_reference: The reference to the source model.
            source_attr: The source column holding the foreign key value.
            is_o2o: Whether the relation is a one-to-one relation.

        Returns:
            None
        """
        self.name = name
        self.source_reference = source_reference
        self.source_attr = source_attr
        self.is_o2o = is_o2o
        self._source: type[Model] | None = None

    def _resolve_source(self) -> type[Model]:
        """Resolve (and memoise) the source model of this reverse relation.

        ``__get__`` runs on every ``obj.related_set`` access; the source model
        is fixed once registered, so it is looked up once and cached.

        Returns:
            The source model class.
        """
        # The registry installs reverse descriptors with the qualified
        # ``module.ClassName`` reference, which resolves exactly (no bare-name
        # guessing between identically named models in different modules).
        source = self._source
        if source is None:
            source = self._source = registry.get_model(self.source_reference)
        return source

    def __get__(
        self, instance: Model | None, owner: type[Model] | None
    ) -> ReverseFKDescriptor | ReverseOneToOne | RelatedManager:
        """Return the descriptor itself or a reverse relation accessor.

        Args:
            instance: The model instance the attribute is accessed on, or None.
            owner: The model class owning the descriptor.

        Returns:
            This descriptor when accessed on the class, a ``ReverseOneToOne``
            for one-to-one relations, otherwise a ``RelatedManager``.
        """
        if instance is None:
            return self
        source = self._resolve_source()
        cached = (instance.__dict__.get("_prefetch") or {}).get(self.name, _MISSING)
        if self.is_o2o:
            return ReverseOneToOne(source, self.source_attr, instance.pk, cached)
        return RelatedManager(source, {self.source_attr: instance.pk}, cached)


_MISSING = object()


class ReverseOneToOne:
    """Awaitable resolving the reverse side of a one-to-one relation."""

    def __init__(
        self,
        model: type[Model],
        source_attr: str,
        pk: Any,
        cached: Any = _MISSING,
    ) -> None:
        """Bind the reverse one-to-one relation to a target lookup.

        Args:
            model: The source model class to query.
            source_attr: The source column holding the foreign key value.
            pk: The primary key value to match against ``source_attr``.
            cached: A prefetched result, or ``_MISSING`` if none.

        Returns:
            None
        """
        self.model = model
        self.source_attr = source_attr
        self.pk = pk
        self.cached = cached

    def __await__(self) -> Generator[Any, None, Model | None]:
        """Await resolution of the reverse one-to-one instance.

        Returns:
            A generator yielding the related instance, or None if absent.
        """
        return self._resolve().__await__()

    async def _resolve(self) -> Model | None:
        """Resolve the related instance, using the cached value if present.

        Returns:
            The related model instance, or None if none matches.
        """
        if self.cached is not _MISSING:
            return self.cached
        return await self.model.get_or_none(**{self.source_attr: self.pk})


class _ChainableManager(Generic[MODEL]):
    """Shared awaitable/chainable surface for the relation managers.

    Subclasses provide ``_qs()`` (the base queryset over the related rows) and
    ``_as_list()`` (resolve to a list, honouring any prefetch cache); this base
    supplies the queryset-proxy surface so both managers await to a list,
    async-iterate, and chain (``.filter``/``.order_by``/``.limit``/…) like a
    queryset without re-declaring each method.
    """

    def _qs(self) -> QuerySet[MODEL]:  # pragma: no cover - abstract
        """Return the base queryset over the related rows."""
        raise NotImplementedError

    async def _as_list(self) -> list[MODEL]:  # pragma: no cover - abstract
        """Resolve the related rows to a list, serving the prefetch cache."""
        raise NotImplementedError

    def __await__(self) -> Generator[Any, None, list[MODEL]]:
        """Await the related rows, serving the cache when prefetched.

        Returns:
            A generator yielding the list of related instances.
        """
        return self._as_list().__await__()

    def __aiter__(self) -> _AsyncList[MODEL]:
        """Iterate asynchronously over the related rows.

        Returns:
            An async iterator over the related instances.
        """
        return _AsyncList(self._as_list())

    def all(self) -> QuerySet[MODEL]:
        """Return a chainable queryset for all related rows.

        Returns:
            A queryset filtered to the related rows.
        """
        return self._qs()

    def filter(self, *args: Q, **kwargs: Any) -> QuerySet[MODEL]:
        """Return the related queryset further filtered by the given criteria.

        Args:
            *args: ``Q`` nodes ANDed into the query.
            **kwargs: Additional filter keyword arguments.

        Returns:
            A queryset over the related rows narrowed by the extra criteria.
        """
        return self._qs().filter(*args, **kwargs)

    def order_by(self, *fields: str) -> QuerySet[MODEL]:
        """Return the related queryset ordered by the given fields.

        Args:
            *fields: Field names to order the related rows by.

        Returns:
            A queryset over the related rows ordered by ``fields``.
        """
        return self._qs().order_by(*fields)

    def __getattr__(self, name: str) -> Any:
        """Proxy queryset methods onto the related queryset.

        Lets a manager chain like a queryset (``rel.limit(10).select_related(…)``
        / ``.exclude`` / ``.values`` / ``.annotate`` / …) without re-declaring
        each method. Only consulted for attributes the manager does not define.

        Args:
            name: The attribute being looked up.

        Returns:
            The corresponding attribute of a freshly filtered queryset.

        Raises:
            AttributeError: For private names or names the queryset lacks.
        """
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._qs(), name)


class RelatedManager(_ChainableManager[MODEL]):
    """Reverse-FK manager: awaitable to a list, chainable, async-iterable."""

    def __init__(
        self,
        model: type[MODEL],
        filters: dict[str, Any],
        cached: Any = _MISSING,
    ) -> None:
        """Bind the manager to a source model and its filter criteria.

        Args:
            model: The source model class to query.
            filters: The filter keyword arguments selecting related rows.
            cached: A prefetched list of results, or ``_MISSING`` if none.

        Returns:
            None
        """
        self.model = model
        self._filters = filters
        self._cached = cached

    def _qs(self) -> QuerySet[MODEL]:
        """Build a filtered queryset for the related rows.

        Returns:
            A queryset filtered by the manager's criteria.
        """
        return QuerySet(self.model).filter(**self._filters)

    async def _as_list(self) -> list[MODEL]:
        """Resolve the related rows, serving the prefetch cache when present.

        Returns:
            The list of related instances.
        """
        if self._cached is not _MISSING:
            return self._cached
        return await self._qs()._fetch()

    async def create(self, **kwargs: Any) -> MODEL:
        """Create a related instance bound to the manager's criteria.

        Args:
            **kwargs: Field values for the new instance.

        Returns:
            The newly created related model instance.
        """
        kwargs.update(self._filters)
        return await self.model.create(**kwargs)


class _AsyncList(Generic[MODEL]):
    """Turn an awaitable-to-list into an async iterator."""

    def __init__(self, awaitable: Awaitable[list[MODEL]]) -> None:
        """Store the awaitable that resolves to the list of items.

        Args:
            awaitable: An awaitable resolving to the list to iterate over.

        Returns:
            None
        """
        self._awaitable = awaitable
        self._items = None
        self._i = 0

    def __aiter__(self) -> _AsyncList[MODEL]:
        """Return the async iterator itself.

        Returns:
            This iterator instance.
        """
        return self

    async def __anext__(self) -> MODEL:
        """Return the next item, awaiting the list on first access.

        Returns:
            The next element from the resolved list.

        Raises:
            StopAsyncIteration: When the list is exhausted.
        """
        if self._items is None:
            self._items = await _ensure(self._awaitable)
        if self._i >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._i]
        self._i += 1
        return item


async def _ensure(awaitable: Awaitable[list[MODEL]]) -> list[MODEL]:
    """Await and return the resolved list.

    Args:
        awaitable: An awaitable resolving to a list.

    Returns:
        The list produced by the awaitable.
    """
    return await awaitable


# ---------------------------------------------------------------------------
# Many-to-many manager
# ---------------------------------------------------------------------------
class M2MDescriptor:
    """Descriptor exposing a many-to-many relation as a manager."""

    def __init__(self, info: M2MInfo, owner_pk_attr: str, reverse: bool = False) -> None:
        """Store the metadata backing this many-to-many descriptor.

        Args:
            info: The many-to-many relation metadata.
            owner_pk_attr: The owning model's primary key attribute name.
            reverse: Whether this descriptor is the reverse side of the relation.

        Returns:
            None
        """
        self.info = info
        self.owner_pk_attr = owner_pk_attr
        self.reverse = reverse

    def __get__(
        self, instance: Model | None, owner: type[Model] | None
    ) -> M2MDescriptor | M2MManager:
        """Return the descriptor itself or a bound many-to-many manager.

        Args:
            instance: The model instance the attribute is accessed on, or None.
            owner: The model class owning the descriptor.

        Returns:
            This descriptor when accessed on the class, otherwise an
            ``M2MManager`` bound to the instance.
        """
        if instance is None:
            return self
        return M2MManager(self.info, instance, self.reverse)


class _M2MMembershipSubquery:
    """Renders ``(SELECT far FROM through WHERE near = $n)`` for an m2m manager.

    Used as the value of a ``pk__in`` filter so :meth:`M2MManager.all` returns a
    chainable :class:`~yara_orm.QuerySet` over the target model constrained to
    the rows linked to one owning instance — without depending on a reverse
    relation name being declared.
    """

    def __init__(self, through: str, near: str, far: str, owner_pk: Any) -> None:
        """Store the join-table identifiers and the owning instance's pk.

        Args:
            through: The (unquoted) join-table name.
            near: The (unquoted) join column referencing the owning instance.
            far: The (unquoted) join column referencing the target row.
            owner_pk: The owning instance's primary-key value to bind.

        Returns:
            None
        """
        self.through = through
        self.near = near
        self.far = far
        self.owner_pk = owner_pk

    def as_sql(
        self,
        queryset: QuerySet,
        dialect: BaseDialect,
        joins: dict[str, str],
        params: list[Any],
        idx: int,
    ) -> tuple[str, int]:
        """Render the membership subquery, binding the owning instance's pk.

        Args:
            queryset: The owning queryset (unused; the subquery is self-contained).
            dialect: The active SQL dialect.
            joins: Join map (unused).
            params: Bound-parameter list, extended in place.
            idx: The next available bind-parameter index.

        Returns:
            A ``(sql, next_index)`` tuple.
        """
        q = dialect.quote
        through = q(self.through)
        params.append(self.owner_pk)
        sql = (
            f"(SELECT {through}.{q(self.far)} FROM {through} "
            f"WHERE {through}.{q(self.near)} = {dialect.placeholder(idx)})"
        )
        return sql, idx + 1


class M2MManager(_ChainableManager[MODEL]):
    """Many-to-many manager: awaitable to a list, async-iterable, mutable."""

    def __init__(self, info: M2MInfo, instance: Model, reverse: bool) -> None:
        """Bind the manager to an instance and resolve the join key roles.

        Args:
            info: The many-to-many relation metadata.
            instance: The model instance owning the relation.
            reverse: Whether this manager is the reverse side of the relation.

        Returns:
            None
        """
        info.finalize()
        self.info = info
        self.instance = instance
        # On the reverse side, the owner/target keys swap roles.
        if reverse:
            self.near_key = info.forward_key
            self.far_key = info.backward_key
            target = info.owner
        else:
            self.near_key = info.backward_key
            self.far_key = info.forward_key
            target = info.resolve_target()
        # The target class is registry-resolved; ``ManyToManyRelation[X]`` on
        # the declaring model is the static source of truth for ``MODEL``.
        self.target = cast("type[MODEL]", target)
        self.name = info.name

    def _sql(self, dialect: BaseDialect) -> dict[str, str]:
        """Return cached static SQL pieces for this side of the relation.

        The join-table SELECT/DELETE and the quoted join-table identifiers are
        constant per ``(dialect, direction)``; they are rendered once and reused
        on every ``add``/``remove``/``clear``/fetch instead of being rebuilt (and
        re-quoted) on each call.

        Args:
            dialect: The active SQL dialect.

        Returns:
            A mapping with the rendered ``fetch``/``clear`` statements and the
            quoted ``through``/``near``/``far`` identifiers.
        """
        cache = self.info.__dict__.setdefault("_sql_cache", {})
        key = (dialect.name, self.near_key, self.far_key)
        entry = cache.get(key)
        if entry is None:
            q = dialect.quote
            ph = dialect.placeholder
            through, near, far = q(self.info.through), q(self.near_key), q(self.far_key)
            meta = self.target._meta
            meta.compile(dialect)
            ttbl = q(meta.table)
            cols = ", ".join(f"{ttbl}.{q(f.db_column)}" for f in meta.field_list)
            entry = {
                "through": through,
                "near": near,
                "far": far,
                "fetch": (
                    f"SELECT {cols} FROM {ttbl} JOIN {through} "
                    f"ON {ttbl}.{q(meta.pk_field.db_column)} = {through}.{far} "
                    f"WHERE {through}.{near} = {ph(1)}"
                ),
                "clear": f"DELETE FROM {through} WHERE {near} = {ph(1)}",
            }
            cache[key] = entry
        return entry

    async def _as_list(self) -> list[MODEL]:
        """Fetch related rows through the join table, using the cache if set.

        Returns:
            The list of related model instances.
        """
        cache = self.instance.__dict__.get("_prefetch")
        if cache and self.name in cache:
            return cache[self.name]
        owner = type(self.instance)
        dialect = get_dialect(owner)
        engine = get_executor(owner, write=False)
        rows = await engine.fetch_rows(self._sql(dialect)["fetch"], [self.instance.pk])
        return self.target._from_db_rows(rows)

    def _qs(self) -> QuerySet[MODEL]:
        """Build a queryset over the target rows linked to this instance.

        Returns:
            A ``QuerySet`` on the target model constrained, through the join
            table, to the rows related to the owning instance.
        """
        sub = _M2MMembershipSubquery(
            self.info.through, self.near_key, self.far_key, self.instance.pk
        )
        return QuerySet(self.target).filter(pk__in=sub)

    async def add(self, *objects: Model | Any) -> None:
        """Add related objects to the join table.

        Args:
            *objects: Related instances or raw primary key values to add.

        Returns:
            None
        """
        if not objects:
            return
        owner = type(self.instance)
        dialect = get_dialect(owner)
        engine = get_executor(owner, write=True)
        parts = self._sql(dialect)
        ph = dialect.placeholder
        near = self.instance.pk
        fars = [obj.pk if hasattr(obj, "pk") else obj for obj in objects]
        # One multi-row INSERT instead of N round-trips.
        values = ", ".join(f"({ph(2 * i + 1)}, {ph(2 * i + 2)})" for i in range(len(fars)))
        params: list[Any] = []
        for far in fars:
            params.append(near)
            params.append(far)
        sql = (
            f"INSERT INTO {parts['through']} ({parts['near']}, {parts['far']}) "
            f"VALUES {values} ON CONFLICT DO NOTHING"
        )
        await engine.execute(sql, params)

    async def remove(self, *objects: Model | Any) -> None:
        """Remove related objects from the join table.

        Args:
            *objects: Related instances or raw primary key values to remove.

        Returns:
            None
        """
        if not objects:
            return
        owner = type(self.instance)
        dialect = get_dialect(owner)
        engine = get_executor(owner, write=True)
        parts = self._sql(dialect)
        fars = [obj.pk if hasattr(obj, "pk") else obj for obj in objects]
        holes = ", ".join(dialect.placeholder(i + 2) for i in range(len(fars)))
        sql = (
            f"DELETE FROM {parts['through']} WHERE {parts['near']} = {dialect.placeholder(1)} "
            f"AND {parts['far']} IN ({holes})"
        )
        await engine.execute(sql, [self.instance.pk, *fars])

    async def clear(self) -> None:
        """Remove all related objects from the join table.

        Returns:
            None
        """
        owner = type(self.instance)
        dialect = get_dialect(owner)
        engine = get_executor(owner, write=True)
        await engine.execute(self._sql(dialect)["clear"], [self.instance.pk])


# ---------------------------------------------------------------------------
# Typing aliases (Tortoise-compatible spellings)
# ---------------------------------------------------------------------------
# Annotate relation attributes with the related model so IDEs and type
# checkers know what an access resolves to. The field *constructors* return
# ``Any`` to the type checker, so the annotation is the source of truth:
#
#     class Book(Model):
#         author: fields.ForeignKeyRelation[Author] = fields.ForeignKeyField(
#             "Author", related_name="books"
#         )
#         editor: fields.ForeignKeyNullableRelation[Author] = fields.ForeignKeyField(
#             "Author", null=True, related_name="edited_books"
#         )
#         tags: fields.ManyToManyRelation[Tag] = fields.ManyToManyField("Tag")
#
#     class Author(Model):
#         books: fields.ReverseRelation["Book"]  # annotation only; installed
#                                                # by the FK's related_name
#
#: A forward FK/O2O access: the related instance when prefetched/assigned,
#: otherwise an awaitable resolving to it.
ForeignKeyRelation = Union[MODEL, ForwardRelation[MODEL]]
#: Like :data:`ForeignKeyRelation` for a nullable FK — the access (and the
#: awaited result) may also be ``None``.
ForeignKeyNullableRelation = Union[MODEL, ForwardRelation[MODEL], None]
#: One-to-one relations share the forward-FK access shape.
OneToOneRelation = ForeignKeyRelation
OneToOneNullableRelation = ForeignKeyNullableRelation
#: The reverse-FK accessor a ``related_name`` installs on the target model:
#: awaitable to a list, chainable, async-iterable.
ReverseRelation = RelatedManager
#: The many-to-many accessor: like a reverse relation, plus add/remove/clear.
ManyToManyRelation = M2MManager

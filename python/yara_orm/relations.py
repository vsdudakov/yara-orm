"""Relation metadata, descriptors and managers.

Forward FK/O2O access is an awaitable accessor (``await obj.tournament``).
Reverse FK and M2M access is a manager that is awaitable (to a list),
chainable (``.filter`` / ``.order_by``) and async-iterable. Prefetched results
are cached on the instance under ``_prefetch`` and served without a query.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import registry
from .connection import get_dialect, get_engine
from .queryset import QuerySet

if TYPE_CHECKING:
    from collections.abc import Awaitable, Generator

    from .fields import ForeignKeyField, ManyToManyField
    from .models import Model


def model_name(reference: str) -> str:
    """Normalise a model reference, accepting ``"app.Model"`` or ``"Model"``.

    Args:
        reference: A model reference such as ``"app.Model"`` or ``"Model"``.

    Returns:
        The bare model name without any leading app label.
    """
    return reference.rsplit(".", 1)[-1]


# ---------------------------------------------------------------------------
# Relation descriptors (metadata)
# ---------------------------------------------------------------------------
class RelationInfo:
    """Describes a forward FK/O2O relation on the owning model."""

    def __init__(
        self,
        name: str,
        field: ForeignKeyField,
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

    def resolve_target(self) -> type[Model]:
        """Resolve the target model class for this relation.

        Returns:
            The model class referenced by this relation.
        """
        return registry.get_model(model_name(self.reference))


class M2MInfo:
    """Describes a many-to-many relation and its join table."""

    def __init__(
        self,
        name: str,
        field: ManyToManyField,
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

    def resolve_target(self) -> type[Model]:
        """Resolve the target model class for this relation.

        Returns:
            The model class referenced by this relation.
        """
        return registry.get_model(model_name(self.reference))

    def finalize(self) -> type[Model]:
        """Fill in defaulted table / key names once both models are known.

        Returns:
            The resolved target model class.
        """
        target = self.resolve_target()
        owner_name = self.owner.__name__.lower()
        target_name = target.__name__.lower()
        if not self.through:
            self.through = f"{owner_name}_{target_name}"
        if not self.backward_key:
            self.backward_key = f"{owner_name}_id"
        if not self.forward_key:
            self.forward_key = f"{target_name}_id"
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
            ``obj.rel.field`` and ``if obj.rel`` work after ``prefetch_related``,
            matching Tortoise; otherwise a ``ForwardRelation`` awaitable that
            lazily loads it.
        """
        if instance is None:
            return self
        cache = instance.__dict__.get("_prefetch")
        if cache and self.info.name in cache:
            return cache[self.info.name]
        return ForwardRelation(instance, self.info)

    def __set__(self, instance: Model, value: Model | Any) -> None:
        """Assign the related instance or raw foreign key value.

        Args:
            instance: The model instance the attribute is set on.
            value: A related model instance, a raw key value, or None.

        Returns:
            None
        """
        # Deferred: breaks the relations <-> models import cycle.
        from .models import Model

        if value is None:
            instance.__dict__[self.info.source_attr] = None
        elif isinstance(value, Model):
            instance.__dict__[self.info.source_attr] = value.pk
            instance.__dict__.setdefault("_prefetch", {})[self.info.name] = value
        else:
            instance.__dict__[self.info.source_attr] = value


class ForwardRelation:
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

    def __await__(self) -> Generator[Any, None, Model | None]:
        """Await resolution of the related instance.

        Returns:
            A generator yielding the related instance, or None if unset.
        """
        return self._resolve().__await__()

    async def _resolve(self) -> Model | None:
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
        self.instance.__dict__.setdefault("_prefetch", {})[self.info.name] = obj
        return obj


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
        source = registry.get_model(model_name(self.source_reference))
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


class RelatedManager:
    """Reverse-FK manager: awaitable to a list, chainable, async-iterable."""

    def __init__(
        self,
        model: type[Model],
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

    def _qs(self) -> QuerySet:
        """Build a filtered queryset for the related rows.

        Returns:
            A queryset filtered by the manager's criteria.
        """
        return QuerySet(self.model).filter(**self._filters)

    async def _as_list(self) -> list[Model]:
        """Resolve the related rows, serving the prefetch cache when present.

        Returns:
            The list of related instances.
        """
        if self._cached is not _MISSING:
            return self._cached
        return await self._qs()._fetch()

    def __await__(self) -> Generator[Any, None, list[Model]]:
        """Await the related rows, serving the cache when prefetched.

        Returns:
            A generator yielding the list of related instances.
        """
        return self._as_list().__await__()

    def __aiter__(self) -> _AsyncList:
        """Iterate asynchronously over the related rows.

        Returns:
            An async iterator over the related instances.
        """
        return _AsyncList(self._as_list())

    def all(self) -> QuerySet:
        """Return a queryset for all related rows.

        Returns:
            A queryset filtered by the manager's criteria.
        """
        return self._qs()

    def filter(self, **kwargs: Any) -> QuerySet:
        """Return a queryset further filtered by the given criteria.

        Args:
            **kwargs: Additional filter keyword arguments.

        Returns:
            A queryset narrowed by both the manager and extra criteria.
        """
        return self._qs().filter(**kwargs)

    def order_by(self, *fields: str) -> QuerySet:
        """Return a queryset ordered by the given fields.

        Args:
            *fields: Field names to order the related rows by.

        Returns:
            A queryset ordered by the given fields.
        """
        return self._qs().order_by(*fields)

    async def create(self, **kwargs: Any) -> Model:
        """Create a related instance bound to the manager's criteria.

        Args:
            **kwargs: Field values for the new instance.

        Returns:
            The newly created related model instance.
        """
        kwargs.update(self._filters)
        return await self.model.create(**kwargs)


class _AsyncList:
    """Turn an awaitable-to-list into an async iterator."""

    def __init__(self, awaitable: Awaitable[list[Any]]) -> None:
        """Store the awaitable that resolves to the list of items.

        Args:
            awaitable: An awaitable resolving to the list to iterate over.

        Returns:
            None
        """
        self._awaitable = awaitable
        self._items = None
        self._i = 0

    def __aiter__(self) -> _AsyncList:
        """Return the async iterator itself.

        Returns:
            This iterator instance.
        """
        return self

    async def __anext__(self) -> Any:
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


async def _ensure(awaitable: Awaitable[list[Any]]) -> list[Any]:
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


class M2MManager:
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
            self.target = info.owner
        else:
            self.near_key = info.backward_key
            self.far_key = info.forward_key
            self.target = info.resolve_target()
        self.name = info.name

    async def _fetch(self) -> list[Model]:
        """Fetch related rows through the join table, using the cache if set.

        Returns:
            The list of related model instances.
        """
        cache = self.instance.__dict__.get("_prefetch")
        if cache and self.name in cache:
            return cache[self.name]
        dialect = get_dialect()
        engine = get_engine()
        meta = self.target._meta
        meta.compile(dialect)
        q = dialect.quote
        ttbl = q(meta.table)
        cols = ", ".join(f"{ttbl}.{q(f.db_column)}" for f in meta.field_list)
        sql = (
            f"SELECT {cols} FROM {ttbl} "
            f"JOIN {q(self.info.through)} ON {ttbl}.{q(meta.pk_field.db_column)} = "
            f"{q(self.info.through)}.{q(self.far_key)} "
            f"WHERE {q(self.info.through)}.{q(self.near_key)} = {dialect.placeholder(1)}"
        )
        rows = await engine.fetch_rows(sql, [self.instance.pk])
        return [self.target._from_db_row(r) for r in rows]

    def __await__(self) -> Generator[Any, None, list[Model]]:
        """Await the related rows through the join table.

        Returns:
            A generator yielding the list of related instances.
        """
        return self._fetch().__await__()

    def __aiter__(self) -> _AsyncList:
        """Iterate asynchronously over the related rows.

        Returns:
            An async iterator over the related instances.
        """
        return _AsyncList(self._fetch())

    async def add(self, *objects: Model | Any) -> None:
        """Add related objects to the join table.

        Args:
            *objects: Related instances or raw primary key values to add.

        Returns:
            None
        """
        if not objects:
            return
        dialect = get_dialect()
        engine = get_engine()
        q = dialect.quote
        near = self.instance.pk
        for obj in objects:
            far = obj.pk if hasattr(obj, "pk") else obj
            sql = (
                f"INSERT INTO {q(self.info.through)} "
                f"({q(self.near_key)}, {q(self.far_key)}) "
                f"VALUES ({dialect.placeholder(1)}, {dialect.placeholder(2)}) "
                f"ON CONFLICT DO NOTHING"
            )
            await engine.execute(sql, [near, far])

    async def remove(self, *objects: Model | Any) -> None:
        """Remove related objects from the join table.

        Args:
            *objects: Related instances or raw primary key values to remove.

        Returns:
            None
        """
        if not objects:
            return
        dialect = get_dialect()
        engine = get_engine()
        q = dialect.quote
        fars = [obj.pk if hasattr(obj, "pk") else obj for obj in objects]
        holes = ", ".join(dialect.placeholder(i + 2) for i in range(len(fars)))
        sql = (
            f"DELETE FROM {q(self.info.through)} "
            f"WHERE {q(self.near_key)} = {dialect.placeholder(1)} "
            f"AND {q(self.far_key)} IN ({holes})"
        )
        await engine.execute(sql, [self.instance.pk, *fars])

    async def clear(self) -> None:
        """Remove all related objects from the join table.

        Returns:
            None
        """
        dialect = get_dialect()
        engine = get_engine()
        q = dialect.quote
        sql = (
            f"DELETE FROM {q(self.info.through)} "
            f"WHERE {q(self.near_key)} = {dialect.placeholder(1)}"
        )
        await engine.execute(sql, [self.instance.pk])

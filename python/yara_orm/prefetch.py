"""Eager loading: ``prefetch_related`` / ``fetch_related``.

Populates each instance's ``_prefetch`` cache so subsequent relation access
returns without a query, using a single query per relation (no N+1).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from . import registry
from .connection import get_dialect, get_engine
from .relations import M2MDescriptor, ReverseFKDescriptor, model_name

if TYPE_CHECKING:
    from .models import Model
    from .queryset import QuerySet


class Prefetch:
    """Customise a prefetch with a constrained queryset."""

    def __init__(self, relation: str, queryset: QuerySet, to_attr: str | None = None) -> None:
        """Bind a relation name to the queryset used to load it.

        Args:
            relation: Name of the relation to prefetch.
            queryset: Queryset used to fetch the related objects.
            to_attr: When given, store the prefetched result on this instance
                attribute instead of populating the relation accessor.

        Returns:
            None
        """
        self.relation = relation
        self.queryset = queryset
        self.to_attr = to_attr


async def prefetch_instances(instances: list[Model], specs: Sequence[str | Prefetch]) -> None:
    """Populate the ``_prefetch`` cache of each instance for every spec.

    Args:
        instances: Instances whose relations should be prefetched.
        specs: Relation names or :class:`Prefetch` objects to load.

    Returns:
        None
    """
    if not instances:
        return
    for spec in specs:
        if isinstance(spec, Prefetch):
            await _prefetch_one(instances, spec.relation, spec.queryset, spec.to_attr)
        else:
            await _prefetch_one(instances, spec, None, None)


def _assign(instances: list[Model], name: str, to_attr: str | None, values: dict) -> None:
    """Store each instance's prefetched result, by relation name or ``to_attr``.

    Args:
        instances: The instances to assign onto.
        name: The relation name (used as the ``_prefetch`` cache key).
        to_attr: When set, store on this plain attribute instead of the cache.
        values: Mapping of instance to its prefetched value.

    Returns:
        None
    """
    for inst in instances:
        value = values[inst]
        if to_attr is not None:
            inst.__dict__[to_attr] = value
        else:
            inst.__dict__.setdefault("_prefetch", {})[name] = value


async def _prefetch_one(
    instances: list[Model], name: str, custom_qs: QuerySet | None, to_attr: str | None = None
) -> None:
    """Prefetch a single forward FK/O2O, reverse FK/O2O, or M2M relation.

    Args:
        instances: Instances whose relation should be loaded.
        name: Name of the relation to prefetch.
        custom_qs: Optional queryset to constrain the related lookup.
        to_attr: When set, store the result on this attribute instead of the
            relation accessor.

    Returns:
        None
    """
    model = type(instances[0])
    meta = model._meta

    # Forward FK / O2O.
    if name in meta.relations:
        info = meta.relations[name]
        target = info.resolve_target()
        ids = {
            getattr(i, info.source_attr)
            for i in instances
            if getattr(i, info.source_attr) is not None
        }
        objs = await target.filter(pk__in=list(ids)) if ids else []
        by_id = {o.pk: o for o in objs}
        _assign(
            instances,
            name,
            to_attr,
            {i: by_id.get(getattr(i, info.source_attr)) for i in instances},
        )
        return

    descriptor = getattr(model, name, None)

    # Reverse FK / O2O.
    if isinstance(descriptor, ReverseFKDescriptor):
        source = registry.get_model(model_name(descriptor.source_reference))
        pks = [i.pk for i in instances]
        qs = custom_qs if custom_qs is not None else source.all()
        children = await qs.filter(**{f"{descriptor.source_attr}__in": pks})
        grouped: dict = {}
        for child in children:
            grouped.setdefault(getattr(child, descriptor.source_attr), []).append(child)
        values = {}
        for inst in instances:
            group = grouped.get(inst.pk, [])
            values[inst] = (group[0] if group else None) if descriptor.is_o2o else group
        _assign(instances, name, to_attr, values)
        return

    # Many-to-many (forward or reverse); the descriptor is always installed on
    # the class for m2m relations, so an isinstance check covers both cases.
    if isinstance(descriptor, M2MDescriptor):
        await _prefetch_m2m(instances, name, descriptor, to_attr)
        return

    raise ValueError(f"Cannot prefetch unknown relation {name!r} on {model.__name__}")


async def _prefetch_m2m(
    instances: list[Model], name: str, descriptor: M2MDescriptor, to_attr: str | None = None
) -> None:
    """Prefetch a many-to-many relation with a single join query.

    Args:
        instances: Instances whose M2M relation should be loaded.
        name: Name of the M2M relation to prefetch.
        descriptor: Descriptor describing the M2M relation.
        to_attr: When set, store the result on this attribute instead of the
            relation accessor.

    Returns:
        None
    """
    info = descriptor.info
    info.finalize()
    if descriptor.reverse:
        near_key, far_key = info.forward_key, info.backward_key
        target = info.owner
    else:
        near_key, far_key = info.backward_key, info.forward_key
        target = info.resolve_target()

    dialect = get_dialect()
    engine = get_engine()
    tmeta = target._meta
    tmeta.compile(dialect)
    q = dialect.quote
    ttbl = q(tmeta.table)
    through = q(info.through)
    pks = [i.pk for i in instances]
    holes = ", ".join(dialect.placeholder(i + 1) for i in range(len(pks)))
    cols = ", ".join(f"{ttbl}.{q(f.db_column)}" for f in tmeta.field_list)
    sql = (
        f"SELECT {through}.{q(near_key)}, {cols} FROM {ttbl} "
        f"JOIN {through} ON {ttbl}.{q(tmeta.pk_field.db_column)} = {through}.{q(far_key)} "
        f"WHERE {through}.{q(near_key)} IN ({holes})"
    )
    rows = await engine.fetch_rows(sql, pks)
    grouped: dict = {}
    for row in rows:
        owner_id = row[0]
        grouped.setdefault(owner_id, []).append(target._from_db_row(row[1:]))
    _assign(instances, name, to_attr, {i: grouped.get(i.pk, []) for i in instances})

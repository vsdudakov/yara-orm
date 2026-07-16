"""Eager loading: ``prefetch_related`` / ``fetch_related``.

Populates each instance's ``_prefetch`` cache so subsequent relation access
returns without a query, using a single query per relation (no N+1).
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any

from . import registry
from .connection import _active_tx, get_dialect, get_executor
from .relations import M2MDescriptor, ReverseFKDescriptor

if TYPE_CHECKING:
    from .models import Model
    from .queryset import QuerySet


class Prefetch:
    """Customise a prefetch with a constrained queryset."""

    def __init__(self, relation: str, queryset: QuerySet[Any], to_attr: str | None = None) -> None:
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


async def prefetch_instances(instances: Sequence[Model], specs: Sequence[str | Prefetch]) -> None:
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
            await _prefetch_path(instances, spec.relation, spec.queryset, spec.to_attr)
        else:
            await _prefetch_path(instances, spec, None, None)


def _gather_related(instances: Sequence[Model], name: str) -> list[Model]:
    """Collect the loaded related instances cached under ``name`` across a batch.

    Args:
        instances: The instances whose ``_prefetch`` cache to read.
        name: The relation name the prior hop stored under.

    Returns:
        A flat list of the related instances (single values and lists merged,
        ``None`` skipped).
    """
    out: list[Model] = []
    for inst in instances:
        value = inst.__dict__.get("_prefetch", {}).get(name)
        if value is None:
            continue
        if isinstance(value, list):
            out.extend(value)
        else:
            out.append(value)
    return out


async def _prefetch_path(
    instances: Sequence[Model], path: str, custom_qs: QuerySet[Any] | None, to_attr: str | None
) -> None:
    """Prefetch a possibly multi-hop relation path (``"author__publisher"``).

    Intermediate hops load plainly; the optional queryset and ``to_attr`` apply
    to the final hop.

    Args:
        instances: The instances to start from.
        path: The relation path, hops separated by ``__``.
        custom_qs: Optional queryset constraining the final hop.
        to_attr: Optional custom attribute for the final hop's result.

    Returns:
        None
    """
    head, sep, rest = path.partition("__")
    if not sep:
        await _prefetch_one(instances, head, custom_qs, to_attr)
        return
    await _prefetch_one(instances, head, None, None)
    children = _gather_related(instances, head)
    if children:
        await _prefetch_path(children, rest, custom_qs, to_attr)


def _assign(
    instances: Sequence[Model], name: str, to_attr: str | None, values: dict[Model, object]
) -> None:
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


def _is_sliced(qs: QuerySet[Any]) -> bool:
    """Report whether a custom ``Prefetch`` queryset carries a LIMIT/OFFSET.

    A slice on the Prefetch queryset applies *per owner* (e.g. "the 3 newest
    tags per book"), not globally, so every relation branch must special-case
    it the same way.

    Args:
        qs: The custom Prefetch queryset.

    Returns:
        True when the queryset is sliced.
    """
    return qs._limit is not None or qs._offset is not None


def _clone_shared(obj: Model) -> Model:
    """Copy a related instance shared by several owners.

    Each owner must get an independent instance (like the join path's per-row
    hydration), including for mutable column values: a plain ``copy.copy``
    would leave a JSON dict / array list shared, so mutating one owner's
    prefetched object would leak into another's.

    Args:
        obj: The instance to copy for an additional owner.

    Returns:
        A copy safe to hand to a different owner.
    """
    clone = copy.copy(obj)
    d = clone.__dict__
    for f in type(obj)._meta.field_list:
        value = d.get(f.model_field_name)
        if isinstance(value, (dict, list, set, bytearray)):
            d[f.model_field_name] = copy.deepcopy(value)
    return clone


async def _per_owner(
    owner_ids: Sequence[Any], run: Callable[[Any], Awaitable[list[Any]]]
) -> list[list[Any]]:
    """Run a per-owner sliced prefetch query for every owner.

    The queries are independent, so they run concurrently (one round-trip's
    latency instead of N) — except inside ``in_transaction()``, where sibling
    tasks sharing the pinned connection are unsupported and the queries run
    sequentially instead.

    Args:
        owner_ids: The owner pks to query for.
        run: Coroutine function loading one owner's related rows.

    Returns:
        The per-owner results, aligned with ``owner_ids``.
    """
    if _active_tx.get() is not None:
        return [await run(owner_id) for owner_id in owner_ids]
    return list(await asyncio.gather(*(run(owner_id) for owner_id in owner_ids)))


async def _prefetch_one(
    instances: Sequence[Model],
    name: str,
    custom_qs: QuerySet[Any] | None,
    to_attr: str | None = None,
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
        # Honour a custom ``Prefetch(..., queryset=...)`` exactly like the
        # reverse/M2M branches: its filters/ordering constrain the lookup.
        base = custom_qs if custom_qs is not None else target.all()
        if custom_qs is not None and _is_sliced(custom_qs):
            # A slice applies per owner, and a forward FK has at most one
            # target per owner: any offset (or a zero limit) empties every
            # owner's window, and otherwise the slice is a no-op — so strip
            # it rather than let it cap the batched ``pk__in`` globally.
            if (custom_qs._offset or 0) > 0 or custom_qs._limit == 0:
                _assign(instances, name, to_attr, dict.fromkeys(instances))
                return
            base = custom_qs._clone()
            base._limit = None
            base._offset = None
        objs = await base.filter(pk__in=list(ids)) if ids else []
        by_id = {o.pk: o for o in objs}
        _assign(
            instances,
            name,
            to_attr,
            {i: by_id.get(getattr(i, info.source_attr)) for i in instances},
        )
        return

    descriptor = getattr(model, name, None)

    # Reverse FK / O2O. The descriptor carries the qualified ``module.Name``
    # reference, so the lookup is exact (no bare-name guessing).
    if isinstance(descriptor, ReverseFKDescriptor):
        source = registry.get_model(descriptor.source_reference)
        pks = [i.pk for i in instances]
        qs = custom_qs if custom_qs is not None else source.all()
        grouped: dict = {}
        if custom_qs is not None and _is_sliced(custom_qs):
            # A slice applies per owner ("the 3 newest books per author"): a
            # single batched query would apply it globally and starve every
            # owner outside the first window, so query each owner separately.
            async def run_one(owner_pk: Any) -> list[Any]:
                return list(await custom_qs.filter(**{descriptor.source_attr: owner_pk}))

            unique_pks = list(dict.fromkeys(pks))
            for owner_pk, group in zip(unique_pks, await _per_owner(unique_pks, run_one)):
                grouped[owner_pk] = group
        else:
            children = await qs.filter(**{f"{descriptor.source_attr}__in": pks})
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
        await _prefetch_m2m(instances, name, descriptor, to_attr, custom_qs)
        return

    raise ValueError(f"Cannot prefetch unknown relation {name!r} on {model.__name__}")


async def _prefetch_m2m(
    instances: Sequence[Model],
    name: str,
    descriptor: M2MDescriptor,
    to_attr: str | None = None,
    custom_qs: QuerySet[Any] | None = None,
) -> None:
    """Prefetch a many-to-many relation with a single join query.

    Args:
        instances: Instances whose M2M relation should be loaded.
        name: Name of the M2M relation to prefetch.
        descriptor: Descriptor describing the M2M relation.
        to_attr: When set, store the result on this attribute instead of the
            relation accessor.
        custom_qs: A constrained ``Prefetch(..., queryset=...)`` whose
            filters/ordering must shape the loaded rows; ``None`` loads the
            whole relation with a single join query.

    Returns:
        None
    """
    if not instances:  # pragma: no cover - prefetch is only dispatched for non-empty sets
        return
    info = descriptor.info
    info.finalize()
    if descriptor.reverse:
        near_key, far_key = info.forward_key, info.backward_key
        target = info.owner
    else:
        near_key, far_key = info.backward_key, info.forward_key
        target = info.resolve_target()

    # Route through the active transaction / the owning model's connection so a
    # prefetch inside `in_transaction()` sees the transaction's uncommitted rows.
    owner = type(instances[0])
    dialect = get_dialect(owner)
    engine = get_executor(owner, write=False)
    tmeta = target._meta
    tmeta.compile(dialect)
    q = dialect.quote
    ttbl = q(tmeta.table)
    through = q(info.through)
    pks = [i.pk for i in instances]
    holes = ", ".join(dialect.placeholder(i + 1) for i in range(len(pks)))

    if custom_qs is not None:
        # A constrained queryset can carry arbitrary WHERE/ORDER BY (and more)
        # that a raw join cannot express, so load in two steps: read the
        # owner<->target links from the through table, then fetch the targets
        # through the queryset so its filters/ordering apply. The custom order
        # is preserved per owner by distributing the ordered rows into groups.
        link_sql = (
            f"SELECT {q(near_key)}, {q(far_key)} FROM {through} WHERE {q(near_key)} IN ({holes})"
        )
        links = await engine.fetch_rows(link_sql, pks)
        # ``fetch_rows`` returns raw driver values, but the link values are
        # matched against hydrated pks below (``i.pk`` / ``obj.pk``), so they
        # need the same dialect decoding hydration applies (e.g. a CHAR(36)
        # uuid pk comes back as ``str`` on MySQL while ``obj.pk`` is ``UUID``).
        near_dec = dialect.read_decoder(owner._meta.pk_field)
        far_dec = dialect.read_decoder(tmeta.pk_field)
        owners_by_far: dict = {}
        for near, far in links:
            if near_dec is not None:
                near = near_dec(near)
            if far_dec is not None:
                far = far_dec(far)
            owners_by_far.setdefault(far, []).append(near)
        grouped: dict = {i.pk: [] for i in instances}
        if _is_sliced(custom_qs):
            # A LIMIT/OFFSET/slice on the Prefetch queryset must apply *per
            # owner* (e.g. "the 3 newest tags per book"), so run the queryset
            # once per owner. A single `pk__in` over every owner's targets would
            # apply the slice globally and starve owners outside the first window.
            far_by_owner: dict = {}
            for far, owner_ids in owners_by_far.items():
                for owner_id in owner_ids:
                    far_by_owner.setdefault(owner_id, []).append(far)

            async def run_one(owner_id: Any) -> list[Any]:
                return list(await custom_qs.filter(pk__in=far_by_owner[owner_id]))

            owner_ids = list(far_by_owner)
            for owner_id, group in zip(owner_ids, await _per_owner(owner_ids, run_one)):
                grouped[owner_id] = group
        else:
            # No slice: one batched query, then distribute the ordered rows into
            # per-owner groups. A target shared by several owners gets a distinct
            # instance per owner (like the join path's per-row `_from_db_row`), so
            # mutating one owner's prefetched object cannot leak into another's.
            far_pks = list(owners_by_far)
            objs = await custom_qs.filter(pk__in=far_pks) if far_pks else []
            for obj in objs:
                for n, owner_id in enumerate(owners_by_far.get(obj.pk, ())):
                    grouped[owner_id].append(obj if n == 0 else _clone_shared(obj))
        _assign(instances, name, to_attr, {i: grouped.get(i.pk, []) for i in instances})
        return

    cols = ", ".join(dialect.select_column(f, f"{ttbl}.{q(f.db_column)}") for f in tmeta.field_list)
    sql = (
        f"SELECT {through}.{q(near_key)}, {cols} FROM {ttbl} "
        f"JOIN {through} ON {ttbl}.{q(tmeta.pk_field.db_column)} = {through}.{q(far_key)} "
        f"WHERE {through}.{q(near_key)} IN ({holes})"
    )
    rows = await engine.fetch_rows(sql, pks)
    # The near column is raw from the driver but keys a lookup by the hydrated
    # ``i.pk`` below, so decode it the way hydration would (see the custom-qs
    # branch above).
    near_dec = dialect.read_decoder(owner._meta.pk_field)
    grouped = {}
    for row in rows:
        owner_id = row[0] if near_dec is None else near_dec(row[0])
        grouped.setdefault(owner_id, []).append(target._from_db_row(row[1:]))
    _assign(instances, name, to_attr, {i: grouped.get(i.pk, []) for i in instances})

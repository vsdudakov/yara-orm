"""Global model registry.

Models register themselves here at class-creation time so that schema
generation and foreign-key resolution can find them by name without import
cycles.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Model

#: Qualified "module.ClassName" -> model class, so identically named models in
#: different modules don't overwrite each other (every table still gets built).
_MODELS: dict[str, type[Model]] = {}

#: Memoised bare-name -> model resolutions, so a relation lookup does not rescan
#: every registered model on each call. Invalidated whenever the model set
#: changes (a new model can alter a bare name's resolution).
_RESOLVE_CACHE: dict[str, type[Model]] = {}


def register(model: type[Model]) -> None:
    """Register a model under its qualified ``module.ClassName`` key.

    Args:
        model: The model class to register.

    Returns:
        None
    """
    _MODELS[f"{model.__module__}.{model.__name__}"] = model
    _RESOLVE_CACHE.clear()


def get_model(name: str) -> type[Model]:
    """Resolve a model reference: exact ``module.Name`` or a bare class name."""
    model = _MODELS.get(name)
    if model is not None:
        return model
    cached = _RESOLVE_CACHE.get(name)
    if cached is not None:
        return cached
    short = name.rsplit(".", 1)[-1]
    matches = [m for m in _MODELS.values() if m.__name__ == short]
    if not matches:
        raise KeyError(f"Unknown model referenced: {name!r}")
    # A single match is exact; an ambiguous bare name resolves to the most
    # recently defined model across modules.
    result = matches[0] if len(matches) == 1 else matches[-1]
    _RESOLVE_CACHE[name] = result
    return result


def all_models() -> list[type[Model]]:
    """Return every registered model class.

    Returns:
        The list of registered model classes.
    """
    return list(_MODELS.values())


def clear() -> None:
    """Drop all registered models and reset relation resolution.

    Returns:
        None
    """
    _MODELS.clear()
    _RESOLVE_CACHE.clear()
    _RESOLVED.clear()


_RESOLVED = {"done": False}


def resolve_relations() -> None:
    """Install reverse FK/O2O/M2M accessors on target models.

    Idempotent: safe to call repeatedly (on every ``YaraOrm.init`` / schema build).
    """
    # Deferred: breaks the registry <-> relations import cycle.
    from .relations import M2MDescriptor, ReverseFKDescriptor

    for model in list(_MODELS.values()):
        meta = model._meta
        # Forward FK/O2O: install reverse manager on the target.
        for info in meta.relations.values():
            if not info.field.related_name:
                continue
            target = info.resolve_target()
            if not hasattr(target, info.field.related_name):
                setattr(
                    target,
                    info.field.related_name,
                    ReverseFKDescriptor(
                        info.field.related_name,
                        model.__name__,
                        info.source_attr,
                        info.is_o2o,
                    ),
                )
        # M2M: finalise keys and install reverse manager on the target.
        for info in meta.m2m.values():
            target = info.finalize()
            if info.field.related_name and not hasattr(target, info.field.related_name):
                setattr(
                    target,
                    info.field.related_name,
                    M2MDescriptor(info, target._meta.pk_field.model_field_name, reverse=True),
                )
    _RESOLVED["done"] = True

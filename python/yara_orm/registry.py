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


def register(model: type[Model]) -> None:
    _MODELS[f"{model.__module__}.{model.__name__}"] = model


def get_model(name: str) -> type[Model]:
    """Resolve a model reference: exact ``module.Name`` or a bare class name."""
    if name in _MODELS:
        return _MODELS[name]
    short = name.rsplit(".", 1)[-1]
    matches = [m for m in _MODELS.values() if m.__name__ == short]
    if len(matches) == 1:
        return matches[0]
    if matches:
        # Ambiguous bare name across modules; the most recently defined wins.
        return matches[-1]
    raise KeyError(f"Unknown model referenced: {name!r}")


def all_models() -> list[type[Model]]:
    return list(_MODELS.values())


def clear() -> None:
    _MODELS.clear()
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

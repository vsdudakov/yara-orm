"""Global model registry.

Models register themselves here at class-creation time so that schema
generation and foreign-key resolution can find them by name without import
cycles.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .exceptions import ConfigurationError

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


def _check_related_name(related_name: str, target: type[Model], source: str) -> None:
    """Reject a ``related_name`` that collides with a real attribute on the target.

    A reverse accessor is installed on the target under ``related_name``; if that
    name is already a column, forward relation or m2m field on the target, the
    reverse accessor would be silently dropped (the existing attribute wins),
    giving a wrong/absent reverse relation with no error. Surface it instead.

    Args:
        related_name: The reverse-accessor name to install.
        target: The model the reverse accessor is installed on.
        source: The source model name (for the error message).

    Raises:
        ConfigurationError: When ``related_name`` clashes with a declared
            column, forward relation or m2m field on ``target``.
    """
    tmeta = target._meta
    if related_name in tmeta.fields or related_name in tmeta.relations or related_name in tmeta.m2m:
        raise ConfigurationError(
            f"related_name {related_name!r} on {source} conflicts with an existing "
            f"field/relation named {related_name!r} on {target.__name__}"
        )


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
            related_name = info.field.related_name
            if not related_name:
                continue
            target = info.resolve_target()
            _check_related_name(related_name, target, model.__name__)
            if not hasattr(target, related_name):
                setattr(
                    target,
                    related_name,
                    ReverseFKDescriptor(
                        related_name,
                        model.__name__,
                        info.source_attr,
                        info.is_o2o,
                    ),
                )
        # M2M: finalise keys and install reverse manager on the target.
        for info in meta.m2m.values():
            target = info.finalize()
            related_name = info.field.related_name
            if not related_name:
                continue
            _check_related_name(related_name, target, model.__name__)
            if not hasattr(target, related_name):
                setattr(
                    target,
                    related_name,
                    M2MDescriptor(info, target._meta.pk_field.model_field_name, reverse=True),
                )
    _RESOLVED["done"] = True

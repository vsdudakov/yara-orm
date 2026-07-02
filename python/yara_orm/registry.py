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
    matches = [(key, m) for key, m in _MODELS.items() if m.__name__ == short]
    if len(matches) > 1 and "." in name:
        # A partially-qualified reference ("app.models.Tag" against the key
        # "myproj.app.models.Tag") disambiguates same-named models when exactly
        # one candidate's registration key ends with it.
        suffixed = [(key, m) for key, m in matches if key.endswith(f".{name}")]
        if len(suffixed) == 1:
            matches = suffixed
    if not matches:
        raise KeyError(f"Unknown model referenced: {name!r}")
    # A single match resolves exactly; an ambiguous bare name is an error —
    # guessing (e.g. "most recently defined wins") silently wires relations to
    # the wrong model.
    if len(matches) > 1:
        candidates = ", ".join(sorted(key for key, _ in matches))
        raise ConfigurationError(
            f"Ambiguous model reference {name!r}: matches {candidates}. "
            f"Use the qualified 'module.ClassName' form."
        )
    result = matches[0][1]
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


def _substitute_class(related_name: str, model: type[Model]) -> str:
    """Expand the Django-style ``%(class)s`` placeholder in a ``related_name``.

    Lets an abstract base declare ``related_name="%(class)s_items"`` so each
    concrete subclass installs a distinct reverse accessor instead of
    colliding on the inherited name.

    Args:
        related_name: The declared reverse-accessor name.
        model: The source model owning the relation.

    Returns:
        The name with ``%(class)s`` replaced by the lowercased class name.
    """
    return related_name.replace("%(class)s", model.__name__.lower())


def _duplicate_related_name_error(
    related_name: str, target: type[Model], source: str, other: str
) -> ConfigurationError:
    """Build the error for two relations claiming one reverse name on a target.

    Args:
        related_name: The colliding reverse-accessor name.
        target: The model both relations point at.
        source: The qualified name of the model now claiming the name.
        other: A description of the attribute already holding the name.

    Returns:
        The ``ConfigurationError`` to raise.
    """
    return ConfigurationError(
        f"related_name {related_name!r} on {source} is already used by {other} on "
        f"{target.__name__}; give each relation a distinct related_name (an abstract "
        f"base can use '%(class)s' — e.g. related_name='%(class)s_set' — so every "
        f"concrete subclass derives its own)"
    )


def resolve_relations() -> None:
    """Install reverse FK/O2O/M2M accessors on target models.

    Idempotent: safe to call repeatedly (on every ``YaraOrm.init`` / schema build).
    A ``related_name`` already claimed on the target by a *different* relation
    raises ``ConfigurationError`` instead of silently keeping the first winner.
    """
    # Deferred: breaks the registry <-> relations import cycle.
    from .relations import M2MDescriptor, ReverseFKDescriptor

    for model in list(_MODELS.values()):
        meta = model._meta
        source_ref = f"{model.__module__}.{model.__name__}"
        # Forward FK/O2O: install reverse manager on the target.
        for info in meta.relations.values():
            related_name = info.field.related_name
            if not related_name:
                continue
            related_name = _substitute_class(related_name, model)
            target = info.resolve_target()
            _check_related_name(related_name, target, model.__name__)
            existing = getattr(target, related_name, None)
            if isinstance(existing, ReverseFKDescriptor):
                if (
                    existing.source_reference == source_ref
                    and existing.source_attr == info.source_attr
                ):
                    continue  # already installed by a previous resolve pass
                raise _duplicate_related_name_error(
                    related_name, target, source_ref, existing.source_reference
                )
            if existing is not None:
                raise _duplicate_related_name_error(
                    related_name, target, source_ref, f"attribute {related_name!r}"
                )
            setattr(
                target,
                related_name,
                ReverseFKDescriptor(
                    related_name,
                    source_ref,
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
            related_name = _substitute_class(related_name, model)
            _check_related_name(related_name, target, model.__name__)
            existing = getattr(target, related_name, None)
            if isinstance(existing, M2MDescriptor):
                # Compare by identity of the *relation* (owning model + attr),
                # not the info object: re-registering a module (reload, test
                # re-definition) builds a fresh info for the same relation and
                # must stay idempotent, like the ReverseFK branch above.
                ex = existing.info
                ex_owner = f"{ex.owner.__module__}.{ex.owner.__name__}"
                if existing.reverse and ex_owner == source_ref and ex.name == info.name:
                    if ex is not info:
                        # Point the descriptor at the fresh info so it tracks
                        # the re-registered model class.
                        setattr(
                            target,
                            related_name,
                            M2MDescriptor(
                                info, target._meta.pk_field.model_field_name, reverse=True
                            ),
                        )
                    continue  # already installed by a previous resolve pass
                raise _duplicate_related_name_error(
                    related_name, target, source_ref, f"m2m relation {existing.info.name!r}"
                )
            if existing is not None:
                raise _duplicate_related_name_error(
                    related_name, target, source_ref, f"attribute {related_name!r}"
                )
            setattr(
                target,
                related_name,
                M2MDescriptor(info, target._meta.pk_field.model_field_name, reverse=True),
            )
    _RESOLVED["done"] = True

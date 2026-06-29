"""Model lifecycle signals: pre/post save and delete.

Register a handler with the decorator for a model; handlers are coroutines that
receive the sender model, the instance, and the operation's context.

    @post_save(User)
    async def on_user(sender, instance, created, using_db, update_fields):
        ...
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .models import Model


class Signals(Enum):
    """The model lifecycle signals."""

    pre_save = "pre_save"
    post_save = "post_save"
    pre_delete = "pre_delete"
    post_delete = "post_delete"


_HANDLERS: dict[str, dict[type, list]] = {
    "pre_save": {},
    "post_save": {},
    "pre_delete": {},
    "post_delete": {},
}

#: Models that have at least one handler on any signal. Lets the (very hot)
#: save/delete path skip the four-dict scan in :func:`_has_handlers` with a
#: single set membership test.
_MODELS_WITH_HANDLERS: set[type] = set()


def _decorator(
    kind: str, models: tuple[type[Model], ...]
) -> Callable[[Callable[..., Awaitable[None]]], Callable[..., Awaitable[None]]]:
    """Build a decorator that registers a handler for ``kind`` and ``models``.

    Args:
        kind: Signal name, one of the keys in ``_HANDLERS``.
        models: One or more model classes the handler is registered for.

    Returns:
        A decorator that registers and returns the wrapped handler.
    """

    def register(func: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
        """Register ``func`` for every sender model and return it unchanged.

        Args:
            func: The async handler to register.

        Returns:
            The same handler that was passed in.
        """
        for model in models:
            _HANDLERS[kind].setdefault(model, []).append(func)
            _MODELS_WITH_HANDLERS.add(model)
        return func

    return register


def pre_save(
    *models: type[Model],
) -> Callable[[Callable[..., Awaitable[None]]], Callable[..., Awaitable[None]]]:
    """Return a decorator registering a pre-save handler for one or more models.

    Args:
        *models: Model class(es) to attach the handler to.

    Returns:
        A decorator registering the wrapped handler.
    """
    return _decorator("pre_save", models)


def post_save(
    *models: type[Model],
) -> Callable[[Callable[..., Awaitable[None]]], Callable[..., Awaitable[None]]]:
    """Return a decorator registering a post-save handler for one or more models.

    Args:
        *models: Model class(es) to attach the handler to.

    Returns:
        A decorator registering the wrapped handler.
    """
    return _decorator("post_save", models)


def pre_delete(
    *models: type[Model],
) -> Callable[[Callable[..., Awaitable[None]]], Callable[..., Awaitable[None]]]:
    """Return a decorator registering a pre-delete handler for one or more models.

    Args:
        *models: Model class(es) to attach the handler to.

    Returns:
        A decorator registering the wrapped handler.
    """
    return _decorator("pre_delete", models)


def post_delete(
    *models: type[Model],
) -> Callable[[Callable[..., Awaitable[None]]], Callable[..., Awaitable[None]]]:
    """Return a decorator registering a post-delete handler for one or more models.

    Args:
        *models: Model class(es) to attach the handler to.

    Returns:
        A decorator registering the wrapped handler.
    """
    return _decorator("post_delete", models)


async def emit_pre_save(
    model: type[Model], instance: Model, using_db: Any, update_fields: list[str] | None
) -> None:
    """Invoke all pre-save handlers registered for ``model``.

    Args:
        model: Model class whose handlers to invoke.
        instance: The model instance being saved.
        using_db: The database/executor used for the operation.
        update_fields: Fields being updated, or None for a full save.

    Returns:
        None
    """
    for func in _HANDLERS["pre_save"].get(model, ()):
        await func(model, instance, using_db, update_fields)


async def emit_post_save(
    model: type[Model],
    instance: Model,
    created: bool,
    using_db: Any,
    update_fields: list[str] | None,
) -> None:
    """Invoke all post-save handlers registered for ``model``.

    Args:
        model: Model class whose handlers to invoke.
        instance: The model instance that was saved.
        created: Whether the instance was newly created.
        using_db: The database/executor used for the operation.
        update_fields: Fields that were updated, or None for a full save.

    Returns:
        None
    """
    for func in _HANDLERS["post_save"].get(model, ()):
        await func(model, instance, created, using_db, update_fields)


async def emit_pre_delete(model: type[Model], instance: Model, using_db: Any) -> None:
    """Invoke all pre-delete handlers registered for ``model``.

    Args:
        model: Model class whose handlers to invoke.
        instance: The model instance being deleted.
        using_db: The database/executor used for the operation.

    Returns:
        None
    """
    for func in _HANDLERS["pre_delete"].get(model, ()):
        await func(model, instance, using_db)


async def emit_post_delete(model: type[Model], instance: Model, using_db: Any) -> None:
    """Invoke all post-delete handlers registered for ``model``.

    Args:
        model: Model class whose handlers to invoke.
        instance: The model instance that was deleted.
        using_db: The database/executor used for the operation.

    Returns:
        None
    """
    for func in _HANDLERS["post_delete"].get(model, ()):
        await func(model, instance, using_db)


def _has_handlers(model: type[Model]) -> bool:
    """Report whether any signal has a handler registered for ``model``.

    Args:
        model: Model class to check.

    Returns:
        True if at least one handler is registered, otherwise False.
    """
    return model in _MODELS_WITH_HANDLERS

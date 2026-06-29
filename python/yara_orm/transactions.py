"""The ``atomic`` decorator, built on ``in_transaction``."""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import Any

from .connection import in_transaction


def atomic(
    connection_name: str = "default",
    isolation: str | None = None,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Wrap a coroutine so it runs inside a transaction (mirrors ``@atomic()``).

    Args:
        connection_name: Name of the connection to run the transaction on.
        isolation: SQL isolation level for the transaction (see
            :class:`~yara_orm.connection.IsolationLevel`), or None for the
            database default.

    Returns:
        A decorator wrapping a coroutine to run inside a transaction.
    """

    def decorator(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        """Wrap ``func`` so each call runs inside a transaction.

        Args:
            func: The coroutine function to wrap.

        Returns:
            The transaction-wrapped coroutine function.
        """

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            """Run the wrapped coroutine within a transaction.

            Args:
                *args: Positional arguments forwarded to the coroutine.
                **kwargs: Keyword arguments forwarded to the coroutine.

            Returns:
                The wrapped coroutine's return value.
            """
            async with in_transaction(connection_name, isolation=isolation):
                return await func(*args, **kwargs)

        return wrapper

    return decorator

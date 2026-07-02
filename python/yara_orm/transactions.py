"""The ``atomic`` decorator, built on ``in_transaction``."""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from .connection import in_transaction

if TYPE_CHECKING:
    from typing import ParamSpec, TypeVar

    P = ParamSpec("P")
    T = TypeVar("T")


def atomic(
    connection_name: str = "default",
    isolation: str | None = None,
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    """Wrap a coroutine so it runs inside a transaction (mirrors ``@atomic()``).

    Args:
        connection_name: Name of the connection to run the transaction on.
        isolation: SQL isolation level for the transaction (see
            :class:`~yara_orm.connection.IsolationLevel`), or None for the
            database default.

    Returns:
        A decorator wrapping a coroutine to run inside a transaction.
    """

    def decorator(func: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        """Wrap ``func`` so each call runs inside a transaction.

        Args:
            func: The coroutine function to wrap.

        Returns:
            The transaction-wrapped coroutine function.
        """

        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
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

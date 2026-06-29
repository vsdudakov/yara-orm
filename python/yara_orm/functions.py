"""Scalar SQL functions usable as ``annotate`` expressions.

Each function renders itself to SQL through a caller-supplied column resolver
(which maps a field name to its qualified column), so functions compose with the
queryset's relation-join handling. Output is portable across the supported
dialects: ``Concat`` uses the ``||`` operator (PostgreSQL and SQLite both accept
it) rather than a ``CONCAT`` call.
"""

from __future__ import annotations

from typing import Any, Callable

# Maps a field name to its qualified SQL column reference.
ColumnResolver = Callable[[str], str]


class Function:
    """Base class for scalar SQL functions over one or more columns."""

    def render(self, resolve: ColumnResolver) -> str:
        """Render this function to a SQL expression.

        Args:
            resolve: Maps a field name to its qualified SQL column reference.

        Returns:
            The SQL expression text.
        """
        raise NotImplementedError

    def render_params(
        self, resolve: ColumnResolver, dialect: Any, params: list[Any], idx: int
    ) -> tuple[str, int]:
        """Render to SQL, binding any embedded literals as parameters.

        The default delegates to :meth:`render` for functions that bind nothing.
        Functions carrying a user value (e.g. :class:`Coalesce`) override this to
        append to ``params`` rather than splice the value into the SQL text.

        Args:
            resolve: Maps a field name to its qualified SQL column reference.
            dialect: The active dialect (provides ``placeholder``).
            params: Bound-parameter list, extended in place.
            idx: The next available 1-based bind-parameter index.

        Returns:
            A ``(sql, next_index)`` tuple.
        """
        return self.render(resolve), idx


class _Unary(Function):
    """A single-column function rendered as ``NAME(column)``."""

    function = ""

    def __init__(self, field: str) -> None:
        """Store the target field name.

        Args:
            field: The field the function is applied to.

        Returns:
            None
        """
        self.field = field

    def render(self, resolve: ColumnResolver) -> str:
        """Render ``NAME(column)``.

        Args:
            resolve: Maps a field name to its qualified SQL column reference.

        Returns:
            The SQL expression text.
        """
        return f"{self.function}({resolve(self.field)})"


class Lower(_Unary):
    """Lower-case a text column."""

    function = "LOWER"


class Upper(_Unary):
    """Upper-case a text column."""

    function = "UPPER"


class Length(_Unary):
    """Length of a text column."""

    function = "LENGTH"


class Trim(_Unary):
    """Strip surrounding whitespace from a text column."""

    function = "TRIM"


class Concat(Function):
    """Concatenate two or more columns via the portable ``||`` operator."""

    def __init__(self, *fields: str) -> None:
        """Store the field names to concatenate.

        Args:
            *fields: Two or more field names.

        Returns:
            None
        """
        self.fields = fields

    def render(self, resolve: ColumnResolver) -> str:
        """Render ``(a || b || ...)``.

        Args:
            resolve: Maps a field name to its qualified SQL column reference.

        Returns:
            The SQL expression text.
        """
        return "(" + " || ".join(resolve(f) for f in self.fields) + ")"


class Coalesce(Function):
    """Return the first non-NULL of a column and a fallback literal."""

    def __init__(self, field: str, default: Any) -> None:
        """Store the column and its fallback value.

        Args:
            field: The field to read.
            default: The fallback literal (string or number) used when NULL.

        Returns:
            None
        """
        self.field = field
        self.default = default

    def render_params(
        self, resolve: ColumnResolver, dialect: Any, params: list[Any], idx: int
    ) -> tuple[str, int]:
        """Render ``COALESCE(column, ?)``, binding the fallback as a parameter.

        Args:
            resolve: Maps a field name to its qualified SQL column reference.
            dialect: The active dialect (provides ``placeholder``).
            params: Bound-parameter list, extended in place.
            idx: The next available 1-based bind-parameter index.

        Returns:
            A ``(sql, next_index)`` tuple.
        """
        params.append(self.default)
        return f"COALESCE({resolve(self.field)}, {dialect.placeholder(idx)})", idx + 1


class Random(Function):
    """A random value in ``[0, 1)`` via ``RANDOM()`` (PostgreSQL and SQLite).

    Takes no column; useful for random ordering, e.g.
    ``Model.annotate(r=Random()).order_by("r")``.
    """

    def render(self, resolve: ColumnResolver) -> str:
        """Render ``RANDOM()``.

        Args:
            resolve: Column resolver (unused; the function takes no column).

        Returns:
            The SQL expression text.
        """
        return "RANDOM()"


__all__ = ["Function", "Lower", "Upper", "Length", "Trim", "Concat", "Coalesce", "Random"]

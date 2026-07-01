"""Scalar SQL functions usable as ``annotate`` expressions.

Each function renders itself to SQL through a caller-supplied column resolver
(which maps a field name to its qualified column), so functions compose with the
queryset's relation-join handling. Output is portable across the supported
dialects: ``Concat`` uses the ``||`` operator (PostgreSQL and SQLite both accept
it) rather than a ``CONCAT`` call.
"""

from __future__ import annotations

from typing import Any, Callable

from .expressions import Expression

# Maps a field name to its qualified SQL column reference.
ColumnResolver = Callable[[str], str]


def _render_operand(
    operand: Any,
    resolve: ColumnResolver,
    dialect: Any,
    params: list[Any],
    idx: int,
    *,
    str_is_column: bool,
) -> tuple[str, int]:
    """Render a function operand to SQL.

    An :class:`~yara_orm.expressions.F` (or any :class:`Expression`) and a nested
    :class:`Function` render themselves, so functions compose with ``F`` and with
    one another (e.g. ``Coalesce(F("at"), now)``). A plain value is a column name
    when ``str_is_column`` is set (the usual operand), otherwise bound as a
    parameter — a literal fallback such as :class:`Coalesce`'s default.

    Args:
        operand: A field name/literal, an ``Expression``/``F``, or a ``Function``.
        resolve: Maps a field name to its qualified SQL column reference.
        dialect: The active dialect (provides ``placeholder``).
        params: Bound-parameter list, extended in place.
        idx: The next available 1-based bind-parameter index.
        str_is_column: Whether a plain value names a column (else bind it).

    Returns:
        A ``(sql, next_index)`` tuple.
    """
    if isinstance(operand, Expression):
        return operand.resolve(resolve, dialect, params, idx)
    if isinstance(operand, Function):
        return operand.render_params(resolve, dialect, params, idx)
    if str_is_column:
        return resolve(operand), idx
    params.append(operand)
    return dialect.placeholder(idx), idx + 1


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

    def render_params(
        self, resolve: ColumnResolver, dialect: Any, params: list[Any], idx: int
    ) -> tuple[str, int]:
        """Render ``NAME(operand)``, accepting an ``F``/expression operand.

        Args:
            resolve: Maps a field name to its qualified SQL column reference.
            dialect: The active dialect (provides ``placeholder``).
            params: Bound-parameter list, extended in place.
            idx: The next available 1-based bind-parameter index.

        Returns:
            A ``(sql, next_index)`` tuple.
        """
        if isinstance(self.field, (Expression, Function)):
            inner, idx = _render_operand(
                self.field, resolve, dialect, params, idx, str_is_column=True
            )
            return f"{self.function}({inner})", idx
        return self.render(resolve), idx


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

    def render_params(
        self, resolve: ColumnResolver, dialect: Any, params: list[Any], idx: int
    ) -> tuple[str, int]:
        """Render ``(a || b || ...)``, accepting ``F``/expression operands.

        Args:
            resolve: Maps a field name to its qualified SQL column reference.
            dialect: The active dialect (provides ``placeholder``).
            params: Bound-parameter list, extended in place.
            idx: The next available 1-based bind-parameter index.

        Returns:
            A ``(sql, next_index)`` tuple.
        """
        if not any(isinstance(f, (Expression, Function)) for f in self.fields):
            return self.render(resolve), idx
        parts = []
        for field in self.fields:
            sql, idx = _render_operand(field, resolve, dialect, params, idx, str_is_column=True)
            parts.append(sql)
        return "(" + " || ".join(parts) + ")", idx


class Coalesce(Function):
    """Return the first non-NULL of a column and a fallback value."""

    def __init__(self, field: Any, default: Any) -> None:
        """Store the column and its fallback value.

        Args:
            field: The field to read — a field name, an ``F``/expression, or a
                nested function.
            default: The fallback used when the field is NULL — a literal
                (string/number/datetime), or an ``F``/expression/function.

        Returns:
            None
        """
        self.field = field
        self.default = default

    def render_params(
        self, resolve: ColumnResolver, dialect: Any, params: list[Any], idx: int
    ) -> tuple[str, int]:
        """Render ``COALESCE(column, ?)``, binding the fallback as a parameter.

        The column operand accepts an ``F``/expression (so
        ``Coalesce(F("at"), now)`` works as an ``update()`` value), and the
        fallback binds as a parameter unless it is itself an expression/function.

        Args:
            resolve: Maps a field name to its qualified SQL column reference.
            dialect: The active dialect (provides ``placeholder``).
            params: Bound-parameter list, extended in place.
            idx: The next available 1-based bind-parameter index.

        Returns:
            A ``(sql, next_index)`` tuple.
        """
        field_sql, idx = _render_operand(
            self.field, resolve, dialect, params, idx, str_is_column=True
        )
        default_sql, idx = _render_operand(
            self.default, resolve, dialect, params, idx, str_is_column=False
        )
        return f"COALESCE({field_sql}, {default_sql})", idx


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

"""Query expressions that reference columns rather than bound values.

``F`` names a column so it can take part in a comparison or an arithmetic
update (``F("qty") + 1``). Expressions resolve themselves to SQL through a
caller-supplied column renderer, so the same tree works both in a ``WHERE``
clause (table-qualified columns) and in an ``UPDATE ... SET`` (bare columns).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .dialects import BaseDialect

# A column renderer turns a field name into its SQL column reference.
ColumnRenderer = Callable[[str], str]


class Expression:
    """Base class for column-referencing expressions usable in arithmetic."""

    def resolve(
        self,
        render_column: ColumnRenderer,
        dialect: BaseDialect,
        params: list[Any],
        idx: int,
    ) -> tuple[str, int]:
        """Render this expression to SQL, binding any literal operands.

        Args:
            render_column: Maps a field name to its SQL column reference.
            dialect: The active SQL dialect (for placeholders).
            params: Bound-parameter list, extended in place with literals.
            idx: The next available bind-parameter index.

        Returns:
            A ``(sql, next_index)`` tuple.
        """
        raise NotImplementedError

    def __add__(self, other: Any) -> CombinedExpression:
        """Return ``self + other`` as a combined expression.

        Args:
            other: The right-hand operand (expression or literal).

        Returns:
            The combined expression.
        """
        return CombinedExpression(self, "+", other)

    def __sub__(self, other: Any) -> CombinedExpression:
        """Return ``self - other`` as a combined expression.

        Args:
            other: The right-hand operand (expression or literal).

        Returns:
            The combined expression.
        """
        return CombinedExpression(self, "-", other)

    def __mul__(self, other: Any) -> CombinedExpression:
        """Return ``self * other`` as a combined expression.

        Args:
            other: The right-hand operand (expression or literal).

        Returns:
            The combined expression.
        """
        return CombinedExpression(self, "*", other)

    def __truediv__(self, other: Any) -> CombinedExpression:
        """Return ``self / other`` as a combined expression.

        Args:
            other: The right-hand operand (expression or literal).

        Returns:
            The combined expression.
        """
        return CombinedExpression(self, "/", other)

    def __radd__(self, other: Any) -> CombinedExpression:
        """Return ``other + self`` for a literal left operand.

        Args:
            other: The left-hand literal operand.

        Returns:
            The combined expression.
        """
        return CombinedExpression(other, "+", self)

    def __rsub__(self, other: Any) -> CombinedExpression:
        """Return ``other - self`` for a literal left operand.

        Args:
            other: The left-hand literal operand.

        Returns:
            The combined expression.
        """
        return CombinedExpression(other, "-", self)

    def __rmul__(self, other: Any) -> CombinedExpression:
        """Return ``other * self`` for a literal left operand.

        Args:
            other: The left-hand literal operand.

        Returns:
            The combined expression.
        """
        return CombinedExpression(other, "*", self)


class F(Expression):
    """A reference to a model column by field name."""

    def __init__(self, name: str) -> None:
        """Store the referenced field name.

        Args:
            name: The model field name to reference.

        Returns:
            None
        """
        self.name = name

    def resolve(
        self,
        render_column: ColumnRenderer,
        dialect: BaseDialect,
        params: list[Any],
        idx: int,
    ) -> tuple[str, int]:
        """Render the field as its SQL column reference.

        Args:
            render_column: Maps a field name to its SQL column reference.
            dialect: The active SQL dialect (unused; no literal to bind).
            params: Bound-parameter list (unchanged).
            idx: The next available bind-parameter index.

        Returns:
            A ``(column_sql, idx)`` tuple.
        """
        return render_column(self.name), idx


class CombinedExpression(Expression):
    """An arithmetic combination of two operands (expressions or literals)."""

    def __init__(self, left: Any, op: str, right: Any) -> None:
        """Store the two operands and the operator joining them.

        Args:
            left: The left operand (expression or literal).
            op: One of ``+``, ``-``, ``*``, ``/``.
            right: The right operand (expression or literal).

        Returns:
            None
        """
        self.left = left
        self.op = op
        self.right = right

    def resolve(
        self,
        render_column: ColumnRenderer,
        dialect: BaseDialect,
        params: list[Any],
        idx: int,
    ) -> tuple[str, int]:
        """Render both operands and join them with the operator.

        Args:
            render_column: Maps a field name to its SQL column reference.
            dialect: The active SQL dialect (for placeholders).
            params: Bound-parameter list, extended in place with literals.
            idx: The next available bind-parameter index.

        Returns:
            A ``(sql, next_index)`` tuple wrapped in parentheses.
        """
        left_sql, idx = _resolve_operand(self.left, render_column, dialect, params, idx)
        right_sql, idx = _resolve_operand(self.right, render_column, dialect, params, idx)
        return f"({left_sql} {self.op} {right_sql})", idx


def _resolve_operand(
    operand: Any,
    render_column: ColumnRenderer,
    dialect: BaseDialect,
    params: list[Any],
    idx: int,
) -> tuple[str, int]:
    """Render one operand: an expression resolves itself, a literal is bound.

    Args:
        operand: An :class:`Expression` or a plain literal value.
        render_column: Maps a field name to its SQL column reference.
        dialect: The active SQL dialect (for placeholders).
        params: Bound-parameter list, extended in place with literals.
        idx: The next available bind-parameter index.

    Returns:
        A ``(sql, next_index)`` tuple.
    """
    if isinstance(operand, Expression):
        return operand.resolve(render_column, dialect, params, idx)
    params.append(operand)
    return dialect.placeholder(idx), idx + 1

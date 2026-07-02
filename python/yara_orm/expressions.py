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

    def __rtruediv__(self, other: Any) -> CombinedExpression:
        """Return ``other / self`` for a literal left operand.

        Args:
            other: The left-hand literal operand.

        Returns:
            The combined expression.
        """
        return CombinedExpression(other, "/", self)


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


class Array(list):  # noqa: FURB189 - a thin marker subclass, not a full list reimpl
    """A sequence bound as a PostgreSQL array parameter rather than JSON.

    A bare ``list`` binds as a JSON value (so a ``JSONField`` round-trips), so
    wrap a sequence in ``Array`` to bind it as a real array — e.g.
    ``await conn.execute_query("... WHERE id = ANY($1)", [Array(ids)])`` or
    ``Model.filter(...).update(tags=Array([...]))`` against an array column.
    The engine reads array columns back as plain Python lists.
    """


class Value(Expression):
    """A literal value usable where an expression is expected (compat shim).

    Some ORMs wrap literals as ``Value(0)`` in ``Case(default=...)`` and ``F``
    arithmetic. yara already binds bare literals there, but ``Value`` keeps such
    code importing and working unchanged: it renders as a single bound parameter.
    """

    def __init__(self, value: Any) -> None:
        """Store the literal value to bind.

        Args:
            value: The literal value to bind when rendered.

        Returns:
            None
        """
        self.value = value

    def resolve(
        self,
        render_column: ColumnRenderer,
        dialect: BaseDialect,
        params: list[Any],
        idx: int,
    ) -> tuple[str, int]:
        """Bind the literal value and render its placeholder.

        Args:
            render_column: Maps a field name to its column reference (unused).
            dialect: The active SQL dialect (for placeholders).
            params: Bound-parameter list, extended in place with the value.
            idx: The next available bind-parameter index.

        Returns:
            A ``(placeholder_sql, next_index)`` tuple.
        """
        params.append(self.value)
        return dialect.placeholder(idx), idx + 1


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


def _render_value(
    value: Any,
    queryset: Any,
    dialect: BaseDialect,
    joins: dict[str, str],
    params: list[Any],
    idx: int,
) -> tuple[str, int]:
    """Render a ``THEN``/``ELSE`` value: an F as a column, else a bound literal.

    Args:
        value: An :class:`Expression` (column) or a plain literal value.
        queryset: The owning queryset (for column resolution).
        dialect: The active SQL dialect (for placeholders).
        joins: Join map, mutated in place when a column spans a relation.
        params: Bound-parameter list, extended in place with literals.
        idx: The next available bind-parameter index.

    Returns:
        A ``(sql, next_index)`` tuple.
    """
    render_column = lambda name: queryset._resolve_column(name, dialect, joins)  # noqa: E731
    return _resolve_operand(value, render_column, dialect, params, idx)


class When:
    """One ``WHEN <conditions> THEN <value>`` arm of a :class:`Case`."""

    def __init__(self, then: Any, **conditions: Any) -> None:
        """Store the arm's filter conditions and result value.

        Args:
            then: The value (literal or ``F``) produced when the arm matches.
            **conditions: Field lookups (like ``filter``) gating this arm.

        Returns:
            None

        Raises:
            ValueError: When no conditions are given (the arm would render as
                the invalid ``WHEN  THEN ...``).
        """
        if not conditions:
            raise ValueError("When() requires at least one condition")
        self.then = then
        self.conditions = conditions


class Case:
    """A SQL ``CASE`` expression built from :class:`When` arms and a default."""

    def __init__(self, *whens: When, default: Any = None) -> None:
        """Store the ``WHEN`` arms and the optional ``ELSE`` default.

        Args:
            *whens: The ordered :class:`When` arms.
            default: The ``ELSE`` value (omitted when ``None``).

        Returns:
            None
        """
        self.whens = whens
        self.default = default

    def as_sql(
        self,
        queryset: Any,
        dialect: BaseDialect,
        joins: dict[str, str],
        params: list[Any],
        idx: int,
    ) -> tuple[str, int]:
        """Render the ``CASE`` expression, binding conditions and values.

        Args:
            queryset: The owning queryset (for condition/column compilation).
            dialect: The active SQL dialect.
            joins: Join map, mutated in place when a value spans a relation.
            params: Bound-parameter list, extended in place.
            idx: The next available bind-parameter index.

        Returns:
            A ``(sql, next_index)`` tuple.
        """
        parts = ["CASE"]
        for when in self.whens:
            cond_sql, cond_params, idx = queryset._compile_filter_dict(
                when.conditions, dialect, idx
            )
            params.extend(cond_params)
            then_sql, idx = _render_value(when.then, queryset, dialect, joins, params, idx)
            parts.append(f"WHEN {cond_sql} THEN {then_sql}")
        if self.default is not None:
            default_sql, idx = _render_value(self.default, queryset, dialect, joins, params, idx)
            parts.append(f"ELSE {default_sql}")
        parts.append("END")
        return "(" + " ".join(parts) + ")", idx


class Subquery:
    """A nested ``SELECT`` embedded in an annotation or filter.

    Wraps a :class:`~yara_orm.QuerySet`; render it where a scalar is expected by
    restricting the inner query to one column (``.only(...)`` / ``.values_list``)
    so it reads as ``(SELECT col FROM ... WHERE ...)``.
    """

    def __init__(self, queryset: Any) -> None:
        """Store the inner query set.

        Args:
            queryset: The :class:`QuerySet` to embed as a subquery.

        Returns:
            None
        """
        self.queryset = queryset

    def as_sql(
        self,
        queryset: Any,
        dialect: BaseDialect,
        joins: dict[str, str],
        params: list[Any],
        idx: int,
    ) -> tuple[str, int]:
        """Render the wrapped query as a parenthesised subquery.

        Bound parameters of the inner query continue the outer query's
        placeholder numbering, so it composes inside a larger statement.

        Args:
            queryset: The owning queryset (unused).
            dialect: The active SQL dialect.
            joins: Join map (unused; the subquery carries its own FROM).
            params: Bound-parameter list, extended in place.
            idx: The next available bind-parameter index.

        Returns:
            A ``(sql, next_index)`` tuple.
        """
        if not hasattr(self.queryset, "_plain_select_sql"):
            # Callers sometimes pass an *awaited* projection
            # (``Subquery(qs.values_list("id", flat=True))``); yara's
            # values_list() is terminal, so guide the caller to a lazy queryset
            # instead of failing with an opaque 'coroutine' AttributeError.
            raise TypeError(
                f"Subquery() expects a QuerySet, got {type(self.queryset).__name__}; "
                "pass a lazy queryset such as Model.filter(...).only('col') rather "
                "than an awaited values()/values_list()."
            )
        inner = self.queryset
        explicit = getattr(inner, "_only_explicit", None)
        if explicit and inner._only != explicit:
            # ``only()`` force-includes the pk so instances can hydrate, but a
            # subquery must project exactly the requested column(s) — otherwise
            # ``Subquery(qs.only("email"))`` renders a two-column SELECT.
            inner = inner._clone()
            inner._only = explicit
        sub_sql, sub_params, _ = inner._plain_select_sql(dialect, start=idx)
        params.extend(sub_params)
        return f"({sub_sql})", idx + len(sub_params)


class RawSQL:
    """A raw SQL fragment spliced into an annotation.

    Prefer the parameterised form: write ``?`` markers in ``sql`` and pass their
    values as ``params`` — each ``?`` becomes a bound placeholder, so untrusted
    values never touch the SQL text. Values interpolated into ``sql`` directly
    (no ``params``) are the caller's responsibility and must already be trusted.
    """

    def __init__(self, sql: str, params: list[Any] | None = None) -> None:
        """Store the raw SQL fragment and any positional bind values.

        Args:
            sql: The SQL expression text. Each ``?`` is a positional placeholder
                filled from ``params`` (bound, not interpolated).
            params: Values to bind for the ``?`` markers, in order.

        Returns:
            None
        """
        self.sql = sql
        self.params = list(params) if params else []

    def as_sql(
        self,
        queryset: Any,
        dialect: BaseDialect,
        joins: dict[str, str],
        params: list[Any],
        idx: int,
    ) -> tuple[str, int]:
        """Render the fragment, binding ``?`` markers as parameters.

        Args:
            queryset: The owning queryset (unused).
            dialect: The active SQL dialect (for placeholder rendering).
            joins: Join map (unused).
            params: Bound-parameter list, extended in place with ``self.params``.
            idx: The next available bind-parameter index.

        Returns:
            A ``(sql, next_index)`` tuple.

        Raises:
            ValueError: When the ``?`` marker count does not match ``params``.
        """
        if not self.params:
            return self.sql, idx
        segments = self.sql.split("?")
        expected = len(segments) - 1
        if expected != len(self.params):
            raise ValueError(
                f"RawSQL has {expected} '?' placeholder(s) but {len(self.params)} param(s)"
            )
        out = [segments[0]]
        for segment, value in zip(segments[1:], self.params):
            out.append(dialect.placeholder(idx))
            params.append(value)
            idx += 1
            out.append(segment)
        return "".join(out), idx

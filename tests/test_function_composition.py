"""Composition of scalar functions with ``F`` / nested functions.

Functions accept a field name, an ``F``/expression, or a nested function as an
operand, and ``Coalesce``'s fallback likewise accepts a literal, an ``F`` or a
function. These render-level unit tests pin every operand branch.
"""

from yara_orm import F
from yara_orm.functions import Coalesce, Concat, Function, Lower, Trim


class _Dialect:
    """Minimal stand-in exposing the placeholder and concatenation renderers."""

    def placeholder(self, index: int) -> str:
        return f"${index}"

    def concat_sql(self, parts: list) -> str:
        return "(" + " || ".join(parts) + ")"


def _resolve(name: str) -> str:
    return f'"{name}"'


def _render(fn):
    params: list = []
    sql, idx = fn.render_params(_resolve, _Dialect(), params, 1)
    return sql, params, idx


def test_custom_function_render_params_defaults_to_render():
    """
    GIVEN a custom Function subclass that only implements render()
    WHEN render_params runs (the compile entry point)
    THEN the base class delegates to render() and binds nothing
    """

    class Version(Function):
        def render(self, resolve):
            return "VERSION()"

    sql, params, idx = _render(Version())
    assert sql == "VERSION()"
    assert params == []
    assert idx == 1


def test_unary_string_operand_uses_render():
    """
    GIVEN a unary function over a column name
    WHEN rendered
    THEN it emits NAME("col") with no bound params
    """
    sql, params, idx = _render(Lower("name"))
    assert sql == 'LOWER("name")'
    assert params == []
    assert idx == 1


def test_unary_nested_function_operand():
    """
    GIVEN a unary function wrapping another function
    WHEN rendered
    THEN the inner function renders as the operand
    """
    sql, params, _ = _render(Lower(Trim("name")))
    assert sql == 'LOWER(TRIM("name"))'
    assert params == []


def test_unary_f_operand():
    """
    GIVEN a unary function over an F expression
    WHEN rendered
    THEN the column reference is used as the operand
    """
    sql, params, _ = _render(Lower(F("name")))
    assert sql == 'LOWER("name")'
    assert params == []


def test_coalesce_string_column_literal_default():
    """
    GIVEN Coalesce over a column name with a literal default
    WHEN rendered
    THEN the column is referenced and the default is bound
    """
    sql, params, idx = _render(Coalesce("at", "x"))
    assert sql == 'COALESCE("at", $1)'
    assert params == ["x"]
    assert idx == 2


def test_coalesce_f_column():
    """
    GIVEN Coalesce whose first operand is an F expression
    WHEN rendered
    THEN the column reference is used and the default is bound
    """
    sql, params, _ = _render(Coalesce(F("at"), "x"))
    assert sql == 'COALESCE("at", $1)'
    assert params == ["x"]


def test_coalesce_function_column():
    """
    GIVEN Coalesce whose first operand is a nested function
    WHEN rendered
    THEN the nested function renders as the column operand
    """
    sql, params, _ = _render(Coalesce(Lower("at"), "x"))
    assert sql == 'COALESCE(LOWER("at"), $1)'
    assert params == ["x"]


def test_coalesce_f_default():
    """
    GIVEN Coalesce whose default is an F expression
    WHEN rendered
    THEN the default renders as a column reference, not a bound param
    """
    sql, params, idx = _render(Coalesce("a", F("b")))
    assert sql == 'COALESCE("a", "b")'
    assert params == []
    assert idx == 1


def test_coalesce_function_default():
    """
    GIVEN Coalesce whose default is a nested function
    WHEN rendered
    THEN the default renders as that function
    """
    sql, params, _ = _render(Coalesce("a", Lower("b")))
    assert sql == 'COALESCE("a", LOWER("b"))'
    assert params == []


def test_concat_string_operands_use_render():
    """
    GIVEN Concat over plain column names
    WHEN rendered
    THEN it emits the || chain with no bound params
    """
    sql, params, _ = _render(Concat("a", "b"))
    assert sql == '("a" || "b")'
    assert params == []


def test_concat_with_function_operand():
    """
    GIVEN Concat mixing a function and a column name
    WHEN rendered
    THEN the function renders inline alongside the column
    """
    sql, params, _ = _render(Concat(Lower("a"), "b"))
    assert sql == '(LOWER("a") || "b")'
    assert params == []

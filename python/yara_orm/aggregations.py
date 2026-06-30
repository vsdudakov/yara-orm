"""Aggregate expressions for ``annotate`` / ``group_by``.

An aggregate names a target which is either a local column or a relation
(reverse FK or M2M); the queryset compiler turns the latter into a JOIN. The
target may also be a column expression (``F``/arithmetic) or a ``Case``, and an
optional ``_filter`` ``Q`` renders a ``FILTER (WHERE ...)`` clause (PostgreSQL).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .expressions import Case, Expression
    from .queryset import Q


class Aggregate:
    """Base class for aggregate expressions over a column or relation."""

    function = ""

    def __init__(
        self,
        field: str | Expression | Case,
        *,
        distinct: bool = False,
        _filter: Q | None = None,
    ) -> None:
        """Initialise the aggregate.

        Args:
            field: The target to aggregate: a column/relation name, a column
                expression (``F`` / arithmetic) or a ``Case`` rendered inline.
            distinct: Whether to aggregate over distinct values only
                (keyword-only, so a stray positional cannot silently become it).
            _filter: Optional ``Q`` restricting which rows feed the aggregate,
                rendered as ``... FILTER (WHERE <q>)`` (PostgreSQL).

        Returns:
            None
        """
        self.field = field
        self.distinct = distinct
        self.filter = _filter

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        """Return a debug representation of the aggregate.

        Returns:
            A string showing the class name and target field.
        """
        return f"{type(self).__name__}({self.field!r})"


class Count(Aggregate):
    """Aggregate that counts matching rows."""

    function = "COUNT"


class Sum(Aggregate):
    """Aggregate that sums the target column."""

    function = "SUM"


class Avg(Aggregate):
    """Aggregate that averages the target column."""

    function = "AVG"


class Min(Aggregate):
    """Aggregate that selects the minimum value of the target column."""

    function = "MIN"


class Max(Aggregate):
    """Aggregate that selects the maximum value of the target column."""

    function = "MAX"

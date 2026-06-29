"""Aggregate expressions for ``annotate`` / ``group_by``.

An aggregate names a target which is either a local column or a relation
(reverse FK or M2M); the queryset compiler turns the latter into a JOIN.
"""

from __future__ import annotations


class Aggregate:
    """Base class for aggregate expressions over a column or relation."""

    function = ""

    def __init__(self, field: str, *, distinct: bool = False) -> None:
        """Initialise the aggregate.

        Args:
            field: Name of the target column or relation to aggregate.
            distinct: Whether to aggregate over distinct values only
                (keyword-only, so a stray positional cannot silently become it).

        Returns:
            None
        """
        self.field = field
        self.distinct = distinct

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

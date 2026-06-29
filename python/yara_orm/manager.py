"""Model managers: the object that produces a model's base queryset.

Set ``class Meta: manager = MyManager()`` on a model and override
:meth:`Manager.get_queryset` to scope every query (e.g. hide soft-deleted
rows). ``Model.all()`` / ``filter()`` / ``exclude()`` and friends route through
the model's manager.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Model
    from .queryset import QuerySet


class Manager:
    """Produces the base queryset for a model.

    Subclass and override :meth:`get_queryset` to apply a default scope.
    """

    def __init__(self, model: type[Model] | None = None) -> None:
        """Store the bound model (the metaclass binds it for declared managers).

        Args:
            model: The model class this manager serves, or None until bound.

        Returns:
            None
        """
        self._model = model

    def get_queryset(self) -> QuerySet:
        """Return the base queryset for the bound model.

        Returns:
            A new ``QuerySet`` over the model.
        """
        # Deferred: breaks the manager <-> queryset import cycle.
        from .queryset import QuerySet

        model = self._model
        assert model is not None, "Manager is not bound to a model"
        return QuerySet(model)


__all__ = ["Manager"]

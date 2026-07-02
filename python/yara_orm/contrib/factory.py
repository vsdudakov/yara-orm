"""`factory_boy`_ integration for yara-orm models.

:class:`YaraModelFactory` makes factory_boy's declaration surface —
``Sequence``, ``Faker``, ``LazyAttribute``, ``SubFactory``,
``@factory.post_generation``, ``PostGenerationMethodCall``, ``Trait``, params —
work with yara-orm's async persistence::

    import factory
    from yara_orm.contrib.factory import YaraModelFactory

    class AuthorFactory(YaraModelFactory):
        class Meta:
            model = Author

        name = factory.Faker("name")

    class BookFactory(YaraModelFactory):
        class Meta:
            model = Book

        title = factory.Sequence(lambda n: f"book-{n}")
        author = factory.SubFactory(AuthorFactory)

    book = await BookFactory.create(title="Dune")   # persisted, FK chain too
    books = await BookFactory.create_batch(5)       # sequential inserts
    draft = AuthorFactory.build()                   # unsaved instance, no DB

How it works: declaration resolution stays synchronous (sequences, fakers,
lazy attributes), but ``create()`` returns a *coroutine* that persists the
instance. A ``SubFactory`` of another :class:`YaraModelFactory` therefore
resolves to a coroutine in the keyword arguments; the create step awaits every
awaitable value (including inside lists and tuples) before calling
``Model.create``, and post-generation hooks run *inside* that coroutine, after
the instance is persisted — so hooks always receive a saved model instance and
may return an awaitable (for example ``instance.tags.add(*extracted)``) which
the factory awaits for them.

``create_batch`` runs its creations **sequentially**, not with
``asyncio.gather``: sub-factory coroutines and SQLite's single-writer locking
make concurrent inserts from one declaration set hazardous, and sequential
inserts keep ``Sequence`` counters and shared ``SubFactory`` instances
deterministic.

factory_boy is an optional dependency: install it with ``pip install
factory-boy`` or ``pip install "yara-orm[factory]"``.

.. _factory_boy: https://factoryboy.readthedocs.io/
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any, TypeVar

try:
    import factory as factory_boy
    from factory import builder as factory_builder
    from factory import enums as factory_enums
except ImportError as exc:  # factory-boy not installed
    raise ImportError(
        "yara_orm.contrib.factory requires the optional 'factory_boy' package. "
        'Install it with `pip install factory-boy` or `pip install "yara-orm[factory]"`.'
    ) from exc

from yara_orm.models import Model

if TYPE_CHECKING:
    from collections.abc import Coroutine

__all__ = ["YaraModelFactory"]

TModel = TypeVar("TModel", bound="Model")


async def _resolve_value(value: Any) -> Any:
    """Await the awaitables a value may carry, recursing into lists/tuples.

    Sub-factory declarations on a :class:`YaraModelFactory` resolve to
    coroutines; this awaits them (sequentially) so the model only ever sees
    concrete values.

    Args:
        value: A keyword-argument or post-generation value.

    Returns:
        ``value`` awaited when it is awaitable; a list/tuple with its elements
        resolved (container type preserved); otherwise ``value`` unchanged.
    """
    if isinstance(value, Model):
        # Model instances are awaitable no-ops (``await obj`` returns ``obj``);
        # treat them as plain values.
        return value
    if inspect.isawaitable(value):
        return await value
    if isinstance(value, (list, tuple)):
        return type(value)([await _resolve_value(item) for item in value])
    return value


def _find_awaitable(value: Any) -> Any:
    """Find the first awaitable inside a value, recursing into lists/tuples.

    Args:
        value: A keyword-argument value passed to the build strategy.

    Returns:
        The first awaitable found, or ``None`` when the value is awaitable-free.
    """
    if isinstance(value, Model):
        # Awaitable no-op, not a pending coroutine — fine for a sync build.
        return None
    if inspect.isawaitable(value):
        return value
    if isinstance(value, (list, tuple)):
        for item in value:
            found = _find_awaitable(item)
            if found is not None:
                return found
    return None


class _AsyncCreateStepBuilder(factory_builder.StepBuilder):
    """A ``StepBuilder`` that defers persistence and post-generation to a coroutine.

    factory_boy's stock builder is fully synchronous: it instantiates the model
    and immediately runs post-generation declarations against whatever
    ``_create`` returned. With an async ``_create`` that would hand hooks a
    *coroutine* instead of a model. This builder keeps declaration resolution
    synchronous but performs instantiation *and* the post-generation phase
    inside a single coroutine, so hooks always receive the persisted instance.

    Sub-factories recurse through this same builder class (``recurse`` uses
    ``self.__class__``), so a nested :class:`YaraModelFactory` chain yields
    nested coroutines that the create step awaits depth-first.
    """

    def build(
        self,
        parent_step: factory_builder.BuildStep | None = None,
        force_sequence: int | None = None,
    ) -> Coroutine[Any, Any, Any]:
        """Resolve declarations now and return a coroutine that persists the row.

        Mirrors ``StepBuilder.build`` up to the instantiation point, then packs
        instantiation and post-generation into the returned coroutine.

        Args:
            parent_step: The parent build step when recursing from a
                ``SubFactory``, or ``None`` at the root.
            force_sequence: A sequence counter forced by the caller (used by
                ``SubFactory`` subclasses with ``FORCE_SEQUENCE``).

        Returns:
            A coroutine resolving to the persisted model instance.
        """
        pre, post = factory_builder.parse_declarations(
            self.extras,
            base_pre=self.factory_meta.pre_declarations,
            base_post=self.factory_meta.post_declarations,
        )

        if force_sequence is not None:
            sequence = force_sequence
        elif self.force_init_sequence is not None:
            sequence = self.force_init_sequence
        else:
            sequence = self.factory_meta.next_sequence()

        step = factory_builder.BuildStep(builder=self, sequence=sequence, parent_step=parent_step)
        step.resolve(pre)
        args, kwargs = self.factory_meta.prepare_arguments(step.attributes)
        return self._finalize(step, post, args, kwargs)

    async def _finalize(
        self,
        step: factory_builder.BuildStep,
        post: factory_builder.DeclarationSet,
        args: tuple,
        kwargs: dict[str, Any],
    ) -> Any:
        """Persist the instance, then run post-generation against it.

        Args:
            step: The resolved build step.
            post: The post-generation declaration set to evaluate.
            args: Positional arguments prepared by the factory options.
            kwargs: Keyword arguments prepared by the factory options (these
                may contain coroutines from nested sub-factories; the
                factory's ``_create`` awaits them).

        Returns:
            The persisted model instance.
        """
        instance = self.factory_meta.instantiate(step=step, args=args, kwargs=kwargs)
        if inspect.isawaitable(instance):
            instance = await instance

        results: dict[str, Any] = {}
        for name in post.sorted():
            declaration = post[name]
            # Values handed to a post-generation declaration (the extracted
            # value and its extra params) may be coroutines from sub-factories
            # (e.g. ``create(tags=[TagFactory.create()])``) — resolve them so
            # hooks receive concrete instances.
            context = {
                key: await _resolve_value(value) for key, value in declaration.context.items()
            }
            result = declaration.declaration.evaluate_post(
                instance=instance, step=step, overrides=context
            )
            # A hook returning an awaitable (``instance.tags.add(...)``) gets
            # it awaited here, after the instance is persisted.
            results[name] = await _resolve_value(result)

        hook_result = self.factory_meta.factory._after_postgeneration(
            instance, create=True, results=results
        )
        if inspect.isawaitable(hook_result):
            await hook_result
        return instance


class YaraModelFactory(factory_boy.Factory[TModel]):
    """Async-aware :class:`factory.Factory` for yara-orm models.

    Subclass it, point ``Meta.model`` at a yara-orm :class:`~yara_orm.Model`
    and declare attributes with the usual factory_boy declarations. ``create``
    / ``create_batch`` are awaitable and persist rows; ``build`` /
    ``build_batch`` stay synchronous and return unsaved instances.

    Supported: ``Sequence``, ``Faker``, ``LazyAttribute``/``LazyFunction``,
    ``SelfAttribute``, params/``Trait``, ``SubFactory`` chains of other
    ``YaraModelFactory`` classes (awaited depth-first before the parent row is
    inserted), ``@factory.post_generation`` hooks (run after the instance is
    persisted; an awaitable return value is awaited),
    ``PostGenerationMethodCall``, and async ``_after_postgeneration``
    overrides. Not supported: ``Meta.inline_args`` (yara-orm models are
    keyword-only) and sub-factory coroutines under the *build* strategy — see
    :meth:`_build`.
    """

    class Meta:
        """Marks the base factory abstract; subclasses set ``model``."""

        abstract = True

    @classmethod
    def _generate(cls, strategy: str, params: dict[str, Any]) -> Any:
        """Route the create strategy through the async builder.

        ``build``/``stub`` (and abstract-factory errors) fall through to
        factory_boy's synchronous machinery.

        Args:
            strategy: One of factory_boy's build/create/stub strategies.
            params: Declaration overrides passed by the caller.

        Returns:
            A coroutine resolving to the persisted instance for the create
            strategy; whatever factory_boy returns otherwise.
        """
        if strategy != factory_enums.CREATE_STRATEGY or cls._meta.abstract:
            return super()._generate(strategy, params)
        return _AsyncCreateStepBuilder(cls._meta, params, strategy).build()

    @classmethod
    def _create(cls, model_class: type[TModel], *args: Any, **kwargs: Any) -> Any:
        """Return a coroutine that persists an instance of ``model_class``.

        Args:
            model_class: The model class from ``Meta.model``.
            *args: Unsupported for yara-orm models (kept for API parity).
            **kwargs: Resolved declarations; values may be coroutines from
                nested sub-factories and are awaited before the insert.

        Returns:
            A coroutine resolving to the persisted instance.
        """
        return cls._create_async(model_class, *args, **kwargs)

    @classmethod
    async def _create_async(cls, model_class: type[TModel], *args: Any, **kwargs: Any) -> TModel:
        """Await awaitable keyword values, then insert the row.

        Args:
            model_class: The model class from ``Meta.model``.
            *args: Unsupported for yara-orm models (kept for API parity).
            **kwargs: Field values; awaitables (also inside lists/tuples) are
                resolved sequentially before ``Model.create`` runs.

        Returns:
            The persisted model instance.
        """
        resolved = {key: await _resolve_value(value) for key, value in kwargs.items()}
        # ``Model.create`` returns ``Self``, so the concrete class flows through.
        return await model_class.create(*args, **resolved)

    @classmethod
    def _build(cls, model_class: type[TModel], *args: Any, **kwargs: Any) -> TModel:
        """Construct an unsaved instance synchronously.

        The build strategy propagates to sub-factories, so a ``SubFactory`` of
        another :class:`YaraModelFactory` yields an unsaved related instance —
        no coroutines are involved. Note that yara-orm refuses to assign an
        *unsaved* instance to a foreign key, so building a factory whose
        ``SubFactory`` feeds an FK raises ``ValueError`` from the model unless
        you override the value with a saved instance or a raw id
        (``BookFactory.build(author=saved_author)``). An awaitable can only
        get here when passed explicitly (e.g.
        ``BookFactory.build(author=AuthorFactory.create())``), which the
        synchronous build API cannot await — that raises ``TypeError``.

        Args:
            model_class: The model class from ``Meta.model``.
            *args: Unsupported for yara-orm models (kept for API parity).
            **kwargs: Field values for the unsaved instance.

        Returns:
            An unsaved model instance.

        Raises:
            TypeError: If any keyword value is (or contains) an awaitable.
        """
        for name, value in kwargs.items():
            awaitable = _find_awaitable(value)
            if awaitable is not None:
                if inspect.iscoroutine(awaitable):
                    awaitable.close()  # silence "coroutine was never awaited"
                raise TypeError(
                    f"{cls.__name__}.build() received an awaitable for {name!r}. "
                    "build() is synchronous and cannot await it — use "
                    f"`await {cls.__name__}.create(...)` instead, or resolve the "
                    "value first (e.g. pass an already-persisted instance)."
                )
        return model_class(*args, **kwargs)

    @classmethod
    # The awaitable return type deliberately diverges from factory_boy's sync
    # signature — that *is* the integration.
    def create(cls, **kwargs: Any) -> Coroutine[Any, Any, TModel]:  # ty: ignore[invalid-method-override]
        """Create and persist an instance: ``obj = await MyFactory.create()``.

        Args:
            **kwargs: Declaration overrides and extra field values.

        Returns:
            A coroutine resolving to the persisted model instance.
        """
        return cls._generate(factory_enums.CREATE_STRATEGY, kwargs)

    @classmethod
    # Awaitable on purpose, like ``create`` above.
    async def create_batch(cls, size: int, **kwargs: Any) -> list[TModel]:  # ty: ignore[invalid-method-override]
        """Create and persist ``size`` instances, one after another.

        Creations run sequentially (not ``asyncio.gather``): shared sub-factory
        awaitables and SQLite write locks make concurrent inserts hazardous.
        An awaitable passed explicitly in ``kwargs`` is awaited once up front,
        so every instance in the batch shares the resolved value.

        Args:
            size: Number of instances to create.
            **kwargs: Declaration overrides applied to every instance.

        Returns:
            The persisted instances, in creation order.
        """
        shared = {key: await _resolve_value(value) for key, value in kwargs.items()}
        return [await cls.create(**shared) for _ in range(size)]

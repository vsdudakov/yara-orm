"""yara_orm.contrib.factory: the factory_boy integration — async create /
create_batch, SubFactory chains, sync build, post-generation hooks and the
optional-dependency import guard."""

import importlib
import sys

import factory
import pytest
from factory import enums, errors

from yara_orm import Model, fields
from yara_orm.contrib.factory import (
    YaraModelFactory,
    _AsyncCreateStepBuilder,
    _find_awaitable,
    _resolve_value,
)


class CfPublisher(Model):
    name = fields.CharField(max_length=100)

    class Meta:
        table = "cf_publisher"


class CfAuthor(Model):
    name = fields.CharField(max_length=100)
    publisher = fields.ForeignKeyField("CfPublisher", related_name="authors")

    class Meta:
        table = "cf_author"


class CfTag(Model):
    name = fields.CharField(max_length=50)

    class Meta:
        table = "cf_tag"


class CfBook(Model):
    title = fields.CharField(max_length=100)
    author = fields.ForeignKeyField("CfAuthor", related_name="books")
    tags = fields.ManyToManyField("CfTag", related_name="books", through="cf_book_tag")

    class Meta:
        table = "cf_book"


MODELS = [CfPublisher, CfAuthor, CfTag, CfBook]


class PublisherFactory(YaraModelFactory):
    class Meta:
        model = CfPublisher

    name = factory.Sequence(lambda n: f"publisher-{n}")


class AuthorFactory(YaraModelFactory):
    class Meta:
        model = CfAuthor

    name = factory.Faker("name")
    publisher = factory.SubFactory(PublisherFactory)


class TagFactory(YaraModelFactory):
    class Meta:
        model = CfTag

    name = factory.Sequence(lambda n: f"tag-{n}")


class BookFactory(YaraModelFactory):
    class Meta:
        model = CfBook

    title = factory.Sequence(lambda n: f"book-{n}")
    author = factory.SubFactory(AuthorFactory)

    @factory.post_generation
    def tags(obj, create, extracted, **kwargs):
        if create and extracted:
            return obj.tags.add(*extracted)


class HookedPublisherFactory(YaraModelFactory):
    class Meta:
        model = CfPublisher

    name = factory.Sequence(lambda n: f"hooked-{n}")

    @factory.post_generation
    def mark(obj, create, extracted, **kwargs):
        obj.hook_create = create
        obj.hook_was_persisted = obj._in_db


class TouchedPublisherFactory(YaraModelFactory):
    class Meta:
        model = CfPublisher

    name = "untouched"

    @classmethod
    async def _after_postgeneration(cls, instance, create, results=None):
        instance.name = "touched"
        await instance.save()


class _Payload:
    def __init__(self, text):
        self.text = text


class PayloadFactory(factory.Factory):
    class Meta:
        model = _Payload

    text = "hello"


# ---------------------------------------------------------------------------
# create() / create_batch()
# ---------------------------------------------------------------------------
async def test_create_persists_nested_subfactory_chain(db):
    """GIVEN a factory with a two-level SubFactory chain (book -> author -> publisher)
    WHEN awaiting BookFactory.create()
    THEN the book and both related rows are persisted, linked and fetchable."""
    book = await BookFactory.create()

    fetched = await CfBook.get(id=book.pk)
    author = await fetched.author
    publisher = await author.publisher
    assert fetched.title == book.title
    assert author.pk is not None
    assert publisher.name.startswith("publisher-")
    assert await CfPublisher.filter(id=publisher.pk).count() == 1


async def test_create_applies_kwarg_overrides(db):
    """GIVEN a factory declaring a Sequence for the title
    WHEN awaiting create(title="Dune")
    THEN the explicit value wins over the declaration and is persisted."""
    book = await BookFactory.create(title="Dune")

    assert book.title == "Dune"
    assert await CfBook.filter(title="Dune").count() == 1


async def test_create_batch_creates_distinct_rows_sequentially(db):
    """GIVEN a factory with a Sequence-driven name
    WHEN awaiting create_batch(3)
    THEN three distinct persisted rows come back in creation order."""
    publishers = await PublisherFactory.create_batch(3)

    assert len(publishers) == 3
    assert len({p.pk for p in publishers}) == 3
    names = [p.name for p in publishers]
    assert names == sorted(names, key=lambda n: int(n.rsplit("-", 1)[1]))


async def test_create_batch_shares_an_awaitable_kwarg(db):
    """GIVEN an unawaited AuthorFactory.create() coroutine passed to create_batch
    WHEN awaiting create_batch(2, author=<coroutine>)
    THEN it is awaited once and both books share the same author row."""
    books = await BookFactory.create_batch(2, author=AuthorFactory.create())

    assert books[0].author_id == books[1].author_id
    assert await CfBook.filter(author_id=books[0].author_id).count() == 2


async def test_create_accepts_awaitable_kwarg(db):
    """GIVEN an unawaited sub-factory coroutine passed explicitly as a kwarg
    WHEN awaiting BookFactory.create(author=AuthorFactory.create())
    THEN the coroutine is awaited and the persisted author backs the book."""
    book = await BookFactory.create(author=AuthorFactory.create())

    author = await book.author
    assert author._in_db


async def test_create_accepts_model_instance_kwarg(db):
    """GIVEN an already-persisted author (model instances are awaitable no-ops)
    WHEN passing it as a kwarg to BookFactory.create()
    THEN it is used as-is rather than being awaited away."""
    author = await AuthorFactory.create()

    book = await BookFactory.create(author=author)

    assert book.author_id == author.pk


async def test_create_honours_forced_sequence_kwarg(db):
    """GIVEN factory_boy's reserved __sequence override
    WHEN awaiting PublisherFactory.create(__sequence=7)
    THEN the Sequence declaration renders with the forced counter."""
    publisher = await PublisherFactory.create(__sequence=7)

    assert publisher.name == "publisher-7"


async def test_builder_honours_force_sequence(db):
    """GIVEN the async step builder invoked directly (as SubFactory FORCE_SEQUENCE does)
    WHEN building with force_sequence=99
    THEN the forced counter takes precedence over the factory's own sequence."""
    builder = _AsyncCreateStepBuilder(PublisherFactory._meta, {}, enums.CREATE_STRATEGY)

    publisher = await builder.build(force_sequence=99)

    assert publisher.name == "publisher-99"


async def test_abstract_factory_create_raises():
    """GIVEN a YaraModelFactory subclass without a Meta.model
    WHEN calling create()
    THEN factory_boy's abstract-factory error surfaces synchronously."""

    class AbstractFactory(YaraModelFactory):
        pass

    with pytest.raises(errors.FactoryError, match="abstract"):
        AbstractFactory.create()


# ---------------------------------------------------------------------------
# post-generation
# ---------------------------------------------------------------------------
async def test_post_generation_hook_runs_on_persisted_instance(db):
    """GIVEN a synchronous @factory.post_generation hook
    WHEN awaiting create()
    THEN the hook runs after persistence with create=True."""
    publisher = await HookedPublisherFactory.create()

    assert publisher.hook_create is True
    assert publisher.hook_was_persisted is True


async def test_post_generation_awaits_m2m_add(db):
    """GIVEN a post_generation hook returning obj.tags.add(*extracted) and a mix of
    an unawaited TagFactory coroutine and a persisted tag in the extracted list
    WHEN awaiting BookFactory.create(tags=[...])
    THEN list members are resolved and the returned add() coroutine is awaited."""
    persisted_tag = await TagFactory.create(name="existing")

    book = await BookFactory.create(tags=[TagFactory.create(), persisted_tag])

    tag_names = {tag.name for tag in await book.tags}
    assert "existing" in tag_names
    assert len(tag_names) == 2


async def test_async_after_postgeneration_is_awaited(db):
    """GIVEN a factory overriding _after_postgeneration as a coroutine function
    WHEN awaiting create()
    THEN the override is awaited and its DB write is visible."""
    publisher = await TouchedPublisherFactory.create()

    assert publisher.name == "touched"
    assert (await CfPublisher.get(id=publisher.pk)).name == "touched"


# ---------------------------------------------------------------------------
# build() — synchronous, no DB
# ---------------------------------------------------------------------------
def test_build_returns_unsaved_instance():
    """GIVEN a factory for an FK-free model
    WHEN calling build() (no event loop, no database)
    THEN an unsaved instance with resolved declarations comes back."""
    publisher = PublisherFactory.build()

    assert isinstance(publisher, CfPublisher)
    assert publisher._in_db is False
    assert publisher.pk is None
    assert publisher.name.startswith("publisher-")


def test_build_batch_returns_unsaved_instances():
    """GIVEN a factory for an FK-free model
    WHEN calling build_batch(2)
    THEN two distinct unsaved instances come back."""
    publishers = PublisherFactory.build_batch(2)

    assert len(publishers) == 2
    assert all(not p._in_db for p in publishers)
    assert publishers[0].name != publishers[1].name


async def test_build_accepts_saved_fk_instance(db):
    """GIVEN a persisted author (awaitable no-op, allowed by the FK setter)
    WHEN calling BookFactory.build(author=author)
    THEN an unsaved book referencing the saved author comes back."""
    author = await AuthorFactory.create()

    book = BookFactory.build(author=author)

    assert book._in_db is False
    assert book.author_id == author.pk


def test_build_rejects_awaitable_kwarg():
    """GIVEN an unawaited create() coroutine passed to the sync build API
    WHEN calling BookFactory.build(author=<coroutine>)
    THEN a TypeError points the user at create() instead (the coroutine is
    closed so no 'never awaited' warning escapes)."""
    with pytest.raises(TypeError, match=r"await BookFactory\.create"):
        BookFactory.build(author=PublisherFactory.create())


def test_build_rejects_awaitable_inside_list_kwarg():
    """GIVEN a list kwarg containing an unawaited coroutine
    WHEN calling build()
    THEN the awaitable is found inside the container and rejected."""

    async def pending():
        return None

    with pytest.raises(TypeError, match="received an awaitable for 'name'"):
        PublisherFactory.build(name=[pending()])


def test_build_rejects_non_coroutine_awaitable():
    """GIVEN a non-coroutine awaitable (a bare __await__ object)
    WHEN calling build()
    THEN it is rejected too (nothing to close, still un-awaitable here)."""

    class Waitable:
        def __await__(self):
            yield from ()
            return None

    with pytest.raises(TypeError, match="received an awaitable"):
        PublisherFactory.build(name=Waitable())


# ---------------------------------------------------------------------------
# helpers and mixed plain factories
# ---------------------------------------------------------------------------
async def test_plain_factory_through_async_builder():
    """GIVEN a plain (non-Yara) factory.Factory routed through the async builder,
    as happens when a YaraModelFactory declares a SubFactory of one
    WHEN awaiting the builder's coroutine
    THEN the synchronously-built object passes through un-awaited."""
    coro = _AsyncCreateStepBuilder(PayloadFactory._meta, {}, enums.CREATE_STRATEGY).build()

    payload = await coro

    assert isinstance(payload, _Payload)
    assert payload.text == "hello"


async def test_resolve_value_preserves_tuples():
    """GIVEN a tuple containing an awaitable
    WHEN resolving it
    THEN the awaitable is awaited and the container stays a tuple."""

    async def value():
        return 42

    assert await _resolve_value((value(), "x")) == (42, "x")


def test_find_awaitable_ignores_plain_containers():
    """GIVEN a list of plain values
    WHEN scanning it for awaitables
    THEN nothing is found."""
    assert _find_awaitable(["a", 1]) is None


def test_import_error_guard(monkeypatch):
    """GIVEN an environment where factory_boy is not importable
    WHEN importing yara_orm.contrib.factory fresh
    THEN a clear ImportError names the pip install commands."""
    monkeypatch.setitem(sys.modules, "factory", None)
    monkeypatch.delitem(sys.modules, "yara_orm.contrib.factory")

    with pytest.raises(ImportError, match=r"factory-boy.*yara-orm\[factory\]"):
        importlib.import_module("yara_orm.contrib.factory")

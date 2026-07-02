---
title: Testing with factories
description: Official factory_boy integration for the async Python ORM — YaraModelFactory gives you awaitable create()/create_batch(), SubFactory chains and post-generation hooks against real database rows.
---

# Testing with factories

`yara_orm.contrib.factory` is the official [factory_boy](https://factoryboy.readthedocs.io/) integration. `YaraModelFactory` keeps factory_boy's whole declaration surface — `Sequence`, `Faker`, `LazyAttribute`, `SubFactory`, `@factory.post_generation`, traits and params — and makes persistence async: `create()` and `create_batch()` return awaitables, so your test fixtures never need `asyncio.run()` or `nest_asyncio` hacks.

factory_boy is an **optional dependency**:

```bash
pip install "yara-orm[factory]"     # or: pip install factory-boy
```

## Declaring factories

```python
import factory
from yara_orm.contrib.factory import YaraModelFactory

class PublisherFactory(YaraModelFactory):
    class Meta:
        model = Publisher

    name = factory.Sequence(lambda n: f"publisher-{n}")

class AuthorFactory(YaraModelFactory):
    class Meta:
        model = Author

    name = factory.Faker("name")
    publisher = factory.SubFactory(PublisherFactory)

class BookFactory(YaraModelFactory):
    class Meta:
        model = Book

    title = factory.Sequence(lambda n: f"book-{n}")
    author = factory.SubFactory(AuthorFactory)
```

## Creating instances

```python
book = await BookFactory.create()                # persisted Book
book = await BookFactory.create(title="Dune")    # overrides win
books = await BookFactory.create_batch(5)        # list[Book]
```

`create()` resolves all declarations synchronously (sequences, fakers, lazy attributes) and returns a coroutine that performs the inserts. A `SubFactory` chain of `YaraModelFactory` classes is awaited depth-first, so `await BookFactory.create()` persists the publisher, then the author, then the book — each row's foreign key pointing at a real saved row.

`create_batch(n)` runs its creations **sequentially**, not with `asyncio.gather`. That is deliberate: sub-factory coroutines shared between instances and SQLite's single-writer locking make concurrent inserts from one declaration set hazardous, and sequential inserts keep `Sequence` counters deterministic.

Keyword values may themselves be awaitables — an unawaited `create()` coroutine or an already-persisted instance both work:

```python
book = await BookFactory.create(author=AuthorFactory.create())   # awaited for you
book = await BookFactory.create(author=existing_author)          # used as-is

# In a batch, an awaitable kwarg is awaited once and shared by every instance:
books = await BookFactory.create_batch(3, author=AuthorFactory.create())
```

## Building unsaved instances

`build()` stays synchronous and touches no database:

```python
draft = AuthorFactory.build()        # unsaved Author, pk is None
drafts = AuthorFactory.build_batch(3)
```

Two caveats, both raising a clear error:

- yara-orm refuses to assign an **unsaved** instance to a foreign key, so building a factory whose `SubFactory` feeds an FK raises `ValueError` unless you override the value with a saved instance or raw id: `BookFactory.build(author=saved_author)`.
- The sync build API cannot await, so passing an awaitable (e.g. `BookFactory.build(author=AuthorFactory.create())`) raises `TypeError` telling you to use `create()` instead.

## Post-generation hooks

Hooks run **after the instance is persisted**, inside the awaited coroutine — they always receive a saved model with `create=True`. A hook that returns an awaitable gets it awaited for you, which makes many-to-many setup a one-liner:

```python
class BookFactory(YaraModelFactory):
    class Meta:
        model = Book

    title = factory.Sequence(lambda n: f"book-{n}")
    author = factory.SubFactory(AuthorFactory)

    @factory.post_generation
    def tags(obj, create, extracted, **kwargs):
        if create and extracted:
            return obj.tags.add(*extracted)   # coroutine — awaited by the factory

book = await BookFactory.create(tags=[TagFactory.create(), existing_tag])
```

Values handed to a hook (the extracted value and its `name__param` extras) are resolved first, including inside lists and tuples — the unawaited `TagFactory.create()` above arrives as a persisted `Tag`. An async `_after_postgeneration` override is awaited too.

## With the pytest ecosystem

Factories are plain classes, so they compose with `pytest-asyncio` fixtures directly:

```python
async def test_reading_list(db):
    books = await BookFactory.create_batch(3)
    assert await Book.all().count() == 3
```

## What is (and is not) supported

| Feature | Status |
| --- | --- |
| `Sequence`, `Faker`, `LazyAttribute`/`LazyFunction`, `SelfAttribute`, params, `Trait` | Supported (resolved synchronously, as usual) |
| `SubFactory` of another `YaraModelFactory` (any depth) | Supported — awaited depth-first before the parent insert |
| `SubFactory` of a plain `factory.Factory` | Supported — built synchronously, passed through |
| `create()` / `create_batch(n)` | Awaitable; batch inserts run sequentially |
| `build()` / `build_batch(n)` | Synchronous, unsaved instances; FK sub-factories need a saved override |
| `@factory.post_generation`, `PostGenerationMethodCall` | Run after persistence; awaitable results are awaited |
| Async `_after_postgeneration` override | Supported — awaited with the resolved results |
| `stub()` | factory_boy default (no persistence involved) |
| `Meta.inline_args` | Not supported — yara-orm models are keyword-only |

Extra keyword arguments pass straight through to `Model.create()`, so `using_db="replica"` routes an instance to another configured connection — see [Multiple databases](multiple-databases.md).

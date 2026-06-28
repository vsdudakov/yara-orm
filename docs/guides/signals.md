---
title: Signals
description: Lifecycle signals in an async Python ORM — register pre_save, post_save, pre_delete and post_delete hooks to run async logic around model save and delete.
---

# Signals

Signals are lifecycle hooks that let you run code around a model's persistence operations. `yara_orm` ships four signals — `pre_save`, `post_save`, `pre_delete` and `post_delete` — so you can react to inserts, updates and deletes without scattering logic across your call sites. Because this is an async Python ORM, every handler is a coroutine that is awaited inline during the `save()` or `delete()` it belongs to.

Each signal is a decorator imported from `yara_orm` that takes the **model class** it should fire for:

```python
from yara_orm import pre_save, post_save, pre_delete, post_delete
```

```python
@pre_save(Author)
async def on_author_save(sender, instance, using_db, update_fields):
    ...
```

## Handler signatures

The signatures differ per signal. Get them exactly right — handlers are called positionally.

| Signal | Async handler signature |
| --- | --- |
| `pre_save` | `async def handler(sender, instance, using_db, update_fields)` |
| `post_save` | `async def handler(sender, instance, created, using_db, update_fields)` |
| `pre_delete` | `async def handler(sender, instance, using_db)` |
| `post_delete` | `async def handler(sender, instance, using_db)` |

### Parameters

- **`sender`** — the model class the signal was registered for (the same class you passed to the decorator).
- **`instance`** — the model instance being saved or deleted. In `pre_save` you may still mutate it before it is written; changes are persisted.
- **`created`** — *(post_save only)* a `bool` that is `True` when the save was an INSERT (a brand-new row) and `False` when it was an UPDATE.
- **`using_db`** — the database executor used for the operation. It is the same executor that runs the underlying SQL, so you can issue queries on the same connection.
- **`update_fields`** — `list[str] | None`. The list of field names passed to `save(update_fields=...)`, or `None` for a full save. It is informational, passed straight through to your handler.

!!! note "Handlers are async and awaited"
    Every handler is a coroutine and is `await`ed as part of the `save()`/`delete()` call. `pre_*` handlers run **before** the SQL statement; `post_*` handlers run **after** it. You can register multiple handlers for the same signal on the same model — they run in registration order.

## Mutating in `pre_save`

A `pre_save` handler can change the instance before it is written. For example, deriving a slug:

```python
from yara_orm import pre_save


@pre_save(Author)
async def fill_slug(sender, instance, using_db, update_fields):
    instance.slug = instance.name.lower()
```

The mutation happens before the INSERT/UPDATE, so the new value is what gets stored.

## Reacting to create vs. update with `post_save`

The `created` flag lets a single `post_save` handler branch between first insert and subsequent updates:

```python
from yara_orm import post_save


@post_save(Book)
async def on_book_saved(sender, instance, created, using_db, update_fields):
    if created:
        # Runs once, when the row is first inserted.
        await send_new_book_notification(instance)
    else:
        # Runs on every later update.
        await reindex_book(instance)
```

```python
book = await Book.create(title="Dune")   # post_save fires with created=True
book.title = "Dune (Special Edition)"
await book.save()                         # post_save fires with created=False
```

## When signals fire

Signals fire for **instance-level** operations:

- `instance.save()` emits `pre_save` then `post_save`.
- `Model.create(...)` calls `save()` internally, so it emits the save signals too (with `created=True`).
- `instance.delete()` emits `pre_delete` then `post_delete`.

!!! warning "Bulk queryset operations bypass signals"
    QuerySet-level bulk methods run a single SQL statement and do **not** load or instantiate rows, so they emit **no** signals:

    - `Model.filter(...).update(...)`
    - `Model.filter(...).delete()`
    - `Model.bulk_create(...)`

    If you need per-row hooks to run, iterate and call `instance.save()` / `instance.delete()` on each object instead of using the bulk path.

!!! tip "Registration is global"
    Decorating a handler registers it for the process. Register your handlers at import time (e.g. in a module that is imported during app startup) so they are in place before any `save()`/`delete()` runs.

## See also

- [Models & fields](models-and-fields.md)
- [Transactions](transactions.md)

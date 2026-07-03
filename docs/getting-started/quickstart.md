---
title: Quick start
description: Build your first app with Yara ORM, the async Python ORM with a Rust engine — define models, run queries and traverse relations on PostgreSQL, MySQL or SQLite.
---

# Quick start

This guide takes you from an empty file to a working async app: define models, create the
schema, run queries and traverse relations. It works the same on **PostgreSQL**, **MySQL**
and **SQLite** — only the connection URL changes.

## 1. Install

```bash
pip install yara-orm
```

See [Installation](installation.md) for platform details.

## 2. Define models

```python
from yara_orm import Model, fields


class Author(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=120, index=True)
    created_at = fields.DatetimeField(auto_now_add=True)


class Book(Model):
    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=200)
    rating = fields.DecimalField(max_digits=3, decimal_places=1, default=0)
    author = fields.ForeignKeyField("Author", related_name="books")
```

## 3. Connect and create the schema

Pick a backend by URL — the rest of your code is identical.

=== "PostgreSQL"

    ```python
    from yara_orm import YaraOrm

    await YaraOrm.init("postgres://user:pass@localhost/app")
    await YaraOrm.generate_schemas()
    ```

=== "MySQL"

    ```python
    from yara_orm import YaraOrm

    await YaraOrm.init("mysql://user:pass@localhost/app")
    await YaraOrm.generate_schemas()
    ```

=== "SQLite"

    ```python
    from yara_orm import YaraOrm

    await YaraOrm.init("sqlite:///app.db")
    await YaraOrm.generate_schemas()
    ```

!!! note
    `generate_schemas()` is convenient for getting started and for tests. For evolving a
    real schema over time, use [Migrations](../guides/migrations.md).

## 4. Create and query

```python
# Insert
ada = await Author.create(name="Ada Lovelace")
await Book.create(title="Notes on the Analytical Engine", rating=5, author=ada)

# Lazy, chainable queries — they run when awaited
books = await Book.filter(rating__gte=4).order_by("-rating").limit(10)
how_many = await Book.filter(author=ada).count()

# Fetch one (raises DoesNotExist if missing)
ada = await Author.get(name="Ada Lovelace")
maybe = await Author.get_or_none(name="Nobody")   # -> None
```

## 5. Traverse relations

```python
# Forward foreign key (awaitable)
book = await Book.get(title="Notes on the Analytical Engine")
author = await book.author
print(author.name)

# Reverse manager (from related_name="books")
async for book in ada.books:
    print(book.title)

# Avoid N+1 with prefetch
for author in await Author.all().prefetch_related("books"):
    print(author.name, len(await author.books))
```

## 6. Put it together

```python
import asyncio
from yara_orm import Model, YaraOrm, fields


class Author(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=120, index=True)


class Book(Model):
    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=200)
    author = fields.ForeignKeyField("Author", related_name="books")


async def main() -> None:
    await YaraOrm.init("sqlite:///app.db")
    await YaraOrm.generate_schemas()

    ada = await Author.create(name="Ada Lovelace")
    await Book.create(title="Notes", author=ada)

    async for book in ada.books:
        print(book.title, "→", (await book.author).name)

    await YaraOrm.close()


asyncio.run(main())
```

!!! tip "`run_async` — one-call lifecycle for scripts"
    `run_async(main())` drives the event loop and guarantees `YaraOrm.close()`
    runs afterwards (even on error), so a script's `main()` need not close
    connections itself:

    ```python
    from yara_orm import run_async

    run_async(main())            # replaces asyncio.run(main()) + manual close
    ```

!!! tip "Preview the schema with `get_schema_sql`"
    `YaraOrm.get_schema_sql()` returns the `CREATE TABLE` DDL as a string
    without touching the database — handy for inspection or dumping it to a file:

    ```python
    print(YaraOrm.get_schema_sql())            # all registered models
    print(YaraOrm.get_schema_sql(models=[Author]))
    ```

## Next steps

- [Models & fields](../guides/models-and-fields.md) — every field type and option.
- [Querying](../guides/querying.md) — lookups, `Q` objects and projections.
- [Relations](../guides/relations.md) — foreign keys, many-to-many and prefetch.
- [Migrations](../guides/migrations.md) — evolve your schema safely.

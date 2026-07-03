---
title: Migrating from Tortoise ORM
description: A step-by-step guide to moving an async Python app from Tortoise ORM to Yara ORM — models, querysets, relations, transactions and migrations map across almost unchanged, with a Rust engine 2–9× faster underneath.
---

# Migrating from Tortoise ORM

Yara ORM was designed to feel like [Tortoise ORM](https://tortoise.github.io/): the same
Django-style models, the same lazy chainable querysets, the same `__` field lookups and
`Q` objects. Most application code moves across with only the import lines and the
initialisation call changed — and runs **2–9× faster** on the [Rust engine](../performance.md)
underneath.

This guide walks through what changes, mapped one concept at a time. If a Tortoise feature
isn't mentioned here, the odds are good it works the same way — check the matching
[guide](querying.md) for the exact surface.

## At a glance

| Concept | Tortoise ORM | Yara ORM |
| --- | --- | --- |
| Import | `from tortoise import fields, models` | `from yara_orm import fields, Model` |
| Base class | `class User(models.Model)` | `class User(Model)` |
| Init | `Tortoise.init(db_url=..., modules=...)` | `YaraOrm.init("postgres://…")` (or `init(config=...)` with a Tortoise config dict) |
| Create schema | `Tortoise.generate_schemas()` | `YaraOrm.generate_schemas()` (auto-orders FK dependencies) |
| Shut down | `Tortoise.close_connections()` | `YaraOrm.close()` (`close_connections()` kept as an alias) |
| Field lookups | `name__icontains=…` | `name__icontains=…` (identical) |
| `Q` objects | `from tortoise.expressions import Q` | `from yara_orm import Q` |
| `F` expressions | `from tortoise.expressions import F` | `from yara_orm import F` |
| Transaction | `from tortoise.transactions import in_transaction` | `from yara_orm import in_transaction` |
| Eager loading | `select_related` / `prefetch_related` | `select_related` / `prefetch_related` (identical) |

The headline difference is initialisation: Yara ORM does **not** take a `modules` list.
Models register themselves on definition, so you just hand `init()` a connection URL.

## 1. Models and fields

Field definitions are nearly identical — change the base class and the import.

=== "Tortoise"

    ```python
    from tortoise import fields
    from tortoise.models import Model

    class Author(Model):
        id = fields.IntField(pk=True)
        name = fields.CharField(max_length=120, index=True)
        created_at = fields.DatetimeField(auto_now_add=True)

    class Book(Model):
        id = fields.IntField(pk=True)
        title = fields.CharField(max_length=200)
        author = fields.ForeignKeyField("models.Author", related_name="books")
        tags = fields.ManyToManyField("models.Tag", related_name="books")
    ```

=== "Yara ORM"

    ```python
    from yara_orm import Model, fields

    class Author(Model):
        id = fields.IntField(pk=True)
        name = fields.CharField(max_length=120, index=True)
        created_at = fields.DatetimeField(auto_now_add=True)

    class Book(Model):
        id = fields.IntField(pk=True)
        title = fields.CharField(max_length=200)
        author = fields.ForeignKeyField("Author", related_name="books")
        tags = fields.ManyToManyField("Tag", related_name="books")
    ```

Two things to note:

- **`Model` is imported directly** from `yara_orm`, not from a `models` submodule.
- **Relation targets are bare model names** (`"Author"`), not the dotted
  `"models.Author"` Tortoise uses — there is no app/module registry to qualify.

See [Models & fields](models-and-fields.md) for the full field catalogue and `Meta` options.

## 2. Initialisation and shutdown

This is the biggest change. Tortoise needs a `modules` mapping so it can discover your
models; Yara ORM discovers them automatically and only wants a URL.

=== "Tortoise"

    ```python
    from tortoise import Tortoise

    await Tortoise.init(
        db_url="postgres://user:pass@localhost/app",
        modules={"models": ["myapp.models"]},
    )
    await Tortoise.generate_schemas()
    # …
    await Tortoise.close_connections()
    ```

=== "Yara ORM"

    ```python
    from yara_orm import YaraOrm

    await YaraOrm.init("postgres://user:pass@localhost/app")
    await YaraOrm.generate_schemas()
    # …
    await YaraOrm.close()
    ```

The same code switches to MySQL or SQLite by changing only the URL
(`mysql://user:pass@localhost/app`, `sqlite:///app.db`) — Tortoise's three
backends are all covered, and driver-qualified Tortoise URLs such as
`mysql+aiomysql://` are normalised automatically. See
[Backends](../backends/index.md).

## 3. Querying

Querysets are the part that moves across **unchanged**. Lazy, chainable, awaited to run —
the same mental model as Tortoise.

```python
# Identical on both ORMs:
books = await Book.filter(rating__gte=4).order_by("-rating").limit(10)
count = await Book.filter(author=ada).count()
ada   = await Author.get(name="Ada Lovelace")
maybe = await Author.get_or_none(name="Nobody")
await Book.exclude(title__startswith="Draft")
```

Field lookups (`__gte`, `__icontains`, `__in`, `__isnull`, `__startswith`, …) are the same
spellings. The one import to redirect is `Q` (and `F` for column expressions):

```python
from yara_orm import Q, F

await Book.filter(Q(rating__gte=4) | Q(title__icontains="sea"))
await Account.filter(id=src).update(balance=F("balance") - amount)
```

For read-heavy paths, Yara ORM's projections — `values()` and `values_list()` — skip model
construction and run noticeably faster; see
[Querying → Projections](querying.md#projections-values-and-values_list).

## 4. Relations

Forward foreign keys are awaitable and reverse managers iterate, just like Tortoise:

```python
book = await Book.get(title="Notes")
author = await book.author              # forward FK — awaitable

async for book in ada.books:            # reverse manager — iterable
    print(book.title)

# Avoid N+1 with eager loading (same names as Tortoise):
for author in await Author.all().prefetch_related("books"):
    ...
await Book.all().select_related("author")
```

`select_related` collapses forward-FK joins into one query and `prefetch_related` batches
reverse/M2M relations into a second query — the [benchmarks](../performance.md) show this
paying off **10–38×** versus naive N+1 access. See [Relations](relations.md).

## 5. Transactions

Tortoise's `in_transaction` and `@atomic` both have direct equivalents — only the import
path changes.

=== "Tortoise"

    ```python
    from tortoise.transactions import in_transaction, atomic

    async with in_transaction():
        ...

    @atomic()
    async def f(): ...
    ```

=== "Yara ORM"

    ```python
    from yara_orm import in_transaction, atomic

    async with in_transaction():
        ...

    @atomic()
    async def f(): ...
    ```

Nesting an `in_transaction`/`@atomic` block establishes a **savepoint** on the same
connection (independent inner rollback), and you can request an
[isolation level](transactions.md#isolation-levels) with
`in_transaction(isolation=IsolationLevel.SERIALIZABLE)`. See [Transactions](transactions.md).

## 6. Migrations

Both ORMs offer operation-based, auto-generated migrations. If you currently use
[Aerich](https://github.com/tortoise/aerich) with Tortoise, the workflow maps directly onto
Yara ORM's built-in commands — `makemigrations`, `upgrade`, `downgrade` — with no third-party
tool to install. See [Migrations](migrations.md) for the full command set, including rename
and constraint operations.

!!! tip "Switching an existing database"
    Migrations describe *schema changes*, not data. When pointing Yara ORM at a database
    that Tortoise already created, generate an initial migration and reconcile it against the
    live schema before applying further changes, rather than running `generate_schemas()`
    against populated tables.

## 7. Compatibility helpers

A range of Tortoise spellings are accepted directly so large codebases migrate with
fewer edits:

**Fields & models**

- **`UUIDField(primary_key=True)`** applies the `uuid4` default (same as `pk=True`),
  and a **foreign key set from a string** id (`obj.parent_id = str(uuid)`) is coerced
  to the target primary key's type when bound.
- **`JSONField(encoder=..., decoder=...)`** value-transform hooks (e.g. to keep
  oversized integers JS-safe). With no `encoder`, exotic Python values stored in a
  JSON column (**UUID, `Decimal`, `datetime`/`date`/`time`, `set`, `Enum`**) are
  coerced to JSON-native forms rather than raising, matching a Tortoise + orjson setup.
- **`BooleanField` coerces non-bool writes** with `bool(value)` (so `1`/`0`/`"yes"`
  round-trip), and **`Meta.extra_kwargs = "store"` is inherited** from a base/abstract
  `Meta` by subclasses that declare their own `Meta`.
- **`Index(..., opclass="gin_trgm_ops")`** applies a per-column operator class (e.g.
  `gin_trgm_ops`, `jsonb_path_ops`) on PostgreSQL — dropped on MySQL and SQLite — replacing
  Tortoise's `contrib.postgres.indexes.GinIndex(opclass=...)`.
- **`_meta.db_table` is assignable** (`Model._meta.db_table = "..."`) alongside its
  read access.
- **`ManyToManyField(through_fields=(fwd, bwd))`** is accepted (alias of
  `forward_key=`/`backward_key=`); bare **`fields.SET_NULL` / `fields.CASCADE` …**
  on-delete constants exist alongside `fields.OnDelete.*`; **`blank=` / `max_length=`**
  on length-less fields are accepted and ignored.
- **Relation type hints** (`ForeignKeyRelation`, `ReverseRelation`,
  `ManyToManyRelation`, …) are importable from `yara_orm.fields` and are real
  generics — `books: fields.ReverseRelation["Book"]` types exactly as in
  Tortoise (see [Typing your relations](relations.md#typing-your-relations)).
  As in Tortoise, the field factories return relation-typed values and the
  underlying classes are `ForeignKeyFieldInstance` / `OneToOneFieldInstance` /
  `ManyToManyFieldInstance` for `isinstance` checks.
- A foreign key declared on an **`abstract = True` base** is inherited by concrete
  subclasses (relation accessor included).
- `_meta` exposes the Tortoise aliases **`db_table`**, **`fields_map`**,
  **`db_fields`**, **`fields_db_projection`**, and fields carry **`has_db_field`**.
- Opt into Tortoise's lenient constructor with **`Meta.extra_kwargs = "store"`** to
  keep unknown `__init__` kwargs as attributes (yara is strict by default).

**Querysets & expressions**

- **`Model.get(...)` and `QuerySet.first()` are chainable single-row results.**
  `await Model.get(id=x).prefetch_related(...)` works, as does a plain
  `await Model.get(id=x)`; `first()` awaits to the instance or `None`. Both accept
  `.only(...)`, `.values(...)` and `.values_list(...)` so
  `await qs.first().values("a", "b")` returns a single dict (or `None`) — Tortoise's
  `QuerySetSingle`. **`QuerySet.all()`** is a no-op terminator. **`Value`** literal
  expressions and **`Q.AND` / `Q.OR`** constants exist. **`Aggregate`** is importable
  from `yara_orm` (Tortoise's `tortoise.functions.Aggregate`).
- **Model instances compare by `(type, pk)`** — a refetched row equals one already
  held, and `obj in [<same row>]` / set membership work (`__eq__`/`__hash__`).
- Aggregates accept an **expression or `Case`** and an optional **`_filter=Q(...)`**
  (`Count("x", _filter=Q(...))` → `... FILTER (WHERE ...)`). **`QuerySet.using_db()`**
  accepts a connection name *or* object.
- **`order_by()` traverses a forward relation** — `order_by("author__name")` (and
  multi-hop `order_by("author__country__name")`) sort by the related column.

- **Pool & connection params via the URL** — `application_name` and server
  settings carry through: `?application_name=svc&options=-c%20search_path%3Dmyschema`
  (plus `max_size`/`min_size`/`statement_cache_size`), replacing Tortoise's
  `credentials` `application_name` / `server_settings`.

**Manual SQL & lifecycle** — `connections.get()` / the `in_transaction()` connection
expose **`execute_query()`** (`(rowcount, rows)`), **`execute_query_dict()`**,
**`fetch_one()`** and **`execute_script()`** (multi-statement). Database errors surface
as **`OperationalError`**. Register **`register_query_hook(fn)`** for SQLCommenter /
tracing on every statement. See [Manual SQL](manual-sql.md).

## What to double-check

- **Dotted relation targets** — rewrite `"models.Author"` to `"Author"`.
- **The `modules` argument** — drop it; `init()` takes a URL (or `config=`).
- **Import paths for `Q`, `F`, `in_transaction`, `atomic`** — all move to `yara_orm`.
- **`order_by("rel__col")`** works for **forward** relations; ordering by a
  **reverse / many-to-many** relation raises (it has no single orderable value).
- **Signals and manual SQL** — supported; see [Signals](signals.md) and
  [Manual SQL](manual-sql.md) for the exact call shapes if you relied on Tortoise's.

Once those are done, the bulk of your query and model code should run as-is — on a
[much faster engine](../performance.md).

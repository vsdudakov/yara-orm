---
title: API reference
description: Yara ORM API reference — the public classes and functions of the async Python ORM: YaraOrm, Model, fields, QuerySet, Q, aggregations, signals and migrations.
---

# API reference

A concise reference to everything exported from `yara_orm`. For task-oriented explanations,
see the [guides](guides/models-and-fields.md).

```python
from yara_orm import (
    YaraOrm, Tortoise, Model, QuerySet, Q, fields, migrations,
    Count, Sum, Avg, Min, Max, Prefetch,
    connections, in_transaction, atomic,
    pre_save, post_save, pre_delete, post_delete,
    BaseDialect, PostgresDialect, SqliteDialect, register_dialect,
    ORMError, ConfigurationError, DoesNotExist,
    MultipleObjectsReturned, IntegrityError, FieldError,
)
```

## YaraOrm

Entry point for connections and schema. `Tortoise` is an alias for `YaraOrm`.

| Member | Signature | Purpose |
|--------|-----------|---------|
| `init` | `await YaraOrm.init(db_url, router=None)` | Connect the default database and resolve relations. |
| `add_connection` | `await YaraOrm.add_connection(name, db_url)` | Register an additional named connection. |
| `set_router` | `YaraOrm.set_router(router)` | Set the per-model read/write router. |
| `generate_schemas` | `await YaraOrm.generate_schemas(safe=True)` | Create tables and join tables. |
| `close` | `await YaraOrm.close()` | Close all connections and reset state. |

Related: `connections.get(name="default")` returns the active executor;
`in_transaction(connection_name="default")` is an async context manager. See
[Transactions](guides/transactions.md) and [Multiple databases](guides/multiple-databases.md).

## Model

Base class for models. See [Models & fields](guides/models-and-fields.md).

| Member | Signature | Purpose |
|--------|-----------|---------|
| `create` | `await Model.create(**kwargs)` | Construct and save a new instance. |
| `bulk_create` | `await Model.bulk_create(objects, batch_size=500)` | Multi-row insert. |
| `get` | `await Model.get(**kwargs)` | Single row; raises `DoesNotExist` / `MultipleObjectsReturned`. |
| `get_or_none` | `await Model.get_or_none(**kwargs)` | Single row or `None`. |
| `all` | `Model.all()` | QuerySet over all rows. |
| `filter` / `exclude` | `Model.filter(*q, **lookups)` | Narrow / negate a QuerySet. |
| `annotate` | `Model.annotate(**aggregates)` | Add computed columns. |
| `prefetch_related` | `Model.prefetch_related(*specs)` | Prefetch relations (no N+1). |
| `raw` | `await Model.raw(sql, params=None)` | Raw SQL → model instances. |
| `save` | `await instance.save(update_fields=None)` | Persist (emits save signals). |
| `delete` | `await instance.delete()` | Delete the row (emits delete signals). |
| `fetch_related` | `await instance.fetch_related(*names)` | Populate relations on the instance. |
| `pk` | `instance.pk` | Primary key value. |

The inner `Meta` class supports `table` and `table_description` / `description`.

## fields

See the full table in [Models & fields](guides/models-and-fields.md). Field classes:
`SmallIntField`, `IntField`, `BigIntField`, `FloatField`, `DecimalField`, `CharField`,
`TextField`, `BinaryField`, `BooleanField`, `DatetimeField`, `DateField`, `TimeField`,
`UUIDField`, `JSONField`, `IntEnumField`, `CharEnumField`, `ForeignKeyField`,
`OneToOneField`, `ManyToManyField`.

Common kwargs: `pk`, `null`, `default`, `unique`, `index`, `db_column`, `description`.

## QuerySet & Q

Lazy and chainable; runs when awaited or on a terminal method. See [Querying](guides/querying.md).

**Chainable:** `filter`, `exclude`, `annotate`, `group_by`, `prefetch_related`, `order_by`,
`limit`, `offset`.

**Terminal (async):** `await qs` → `list[Model]`, `get`, `first`, `count`, `exists`,
`values`, `values_list`, `delete`, `update`.

**`Q`** combines lookups with `&` (AND), `|` (OR), `~` (NOT).

**Lookups:** `exact` (default), `not`, `gt`, `gte`, `lt`, `lte`, `in`, `isnull`, `contains`,
`icontains`, `startswith`, `istartswith`, `endswith`, `iendswith`.

## Aggregations

`Count`, `Sum`, `Avg`, `Min`, `Max` — each constructed as `Agg(field, distinct=False)`. See
[Aggregation & grouping](guides/aggregation.md).

## Relations

`Prefetch(relation, queryset=...)` customizes a prefetch. Forward FK access is awaitable;
reverse and many-to-many managers support `await`, `async for`, `.add/.remove/.clear`
(m2m), `.filter`, `.order_by`. See [Relations](guides/relations.md).

## Signals

Decorators `pre_save`, `post_save`, `pre_delete`, `post_delete` — each takes the model class.
Handlers are async. See [Signals](guides/signals.md) for exact signatures.

## Transactions

`in_transaction(connection_name="default")` (async context manager) and
`atomic(connection_name="default")` (decorator). See [Transactions](guides/transactions.md).

## Migrations

`migrations.MigrationManager(directory="migrations", app="models", models=None)` plus the
operation classes `CreateTable`, `DropTable`, `AddColumn`, `DropColumn`, `CreateIndex`,
`DropIndex`, `RunSQL`, `RunPython`. CLI: `python -m yara_orm …`. See
[Migrations](guides/migrations.md).

## Dialects

`BaseDialect`, `PostgresDialect`, `SqliteDialect`, and `register_dialect(name, cls)` for
adding a backend. See [Backends](backends/index.md) and [Architecture](architecture.md).

## Exceptions

`ORMError` (base), `ConfigurationError`, `DoesNotExist`, `MultipleObjectsReturned`,
`IntegrityError`, `FieldError`.

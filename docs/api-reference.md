---
title: API reference
description: Yara ORM API reference — the public classes and functions of the async Python ORM: YaraOrm, Model, fields, QuerySet, Q, aggregations, signals and migrations.
---

# API reference

A concise reference to everything exported from `yara_orm`. For task-oriented explanations,
see the [guides](guides/models-and-fields.md).

```python
from yara_orm import (
    YaraOrm, Tortoise, Model, QuerySet, Q, F, fields, migrations,
    Count, Sum, Avg, Min, Max,
    Lower, Upper, Length, Trim, Concat, Coalesce,
    Case, When, RawSQL, Prefetch, Manager,
    Now, RandomHex, SqlDefault, DatabaseDefault,
    connections, in_transaction, atomic,
    pre_save, post_save, pre_delete, post_delete,
    Signals, validators, timezone,
    BaseDialect, PostgresDialect, SqliteDialect, register_dialect,
    ORMError, BaseORMException, ConfigurationError, OperationalError,
    DBConnectionError, TransactionManagementError, NotExistOrMultiple,
    DoesNotExist, ObjectDoesNotExistError, MultipleObjectsReturned,
    IntegrityError, FieldError, ParamsError, ValidationError,
    NoValuesFetched, IncompleteInstanceError, UnSupportedError,
)
```

`F` is a column reference for filters and arithmetic updates; `Lower`/`Upper`/`Length`/`Trim`/`Concat`/`Coalesce` are scalar functions and `Case`/`When`/`RawSQL` are conditional/raw expressions for `annotate()` — see [Querying](guides/querying.md) and [Aggregation](guides/aggregation.md).

## YaraOrm

Entry point for connections and schema. `Tortoise` is an alias for `YaraOrm`.

| Member | Signature | Purpose |
|--------|-----------|---------|
| `init` | `await YaraOrm.init(db_url, router=None)` | Connect the default database and resolve relations. |
| `add_connection` | `await YaraOrm.add_connection(name, db_url)` | Register an additional named connection. |
| `set_router` | `YaraOrm.set_router(router)` | Set the per-model read/write router. |
| `generate_schemas` | `await YaraOrm.generate_schemas(safe=True, models=None)` | Create tables and join tables (optionally scoped to `models`). |
| `close` | `await YaraOrm.close()` | Close all connections and reset state. |

Related: `connections.get(name="default")` returns the active executor;
`in_transaction(connection_name="default")` is an async context manager. See
[Transactions](guides/transactions.md) and [Multiple databases](guides/multiple-databases.md).

## Model

Base class for models. See [Models & fields](guides/models-and-fields.md).

| Member | Signature | Purpose |
|--------|-----------|---------|
| `create` | `await Model.create(**kwargs)` | Construct and save a new instance. |
| `get_or_create` | `await Model.get_or_create(defaults=None, **kwargs)` | Fetch or create → `(instance, created)`. |
| `update_or_create` | `await Model.update_or_create(defaults=None, **kwargs)` | Update or create → `(instance, created)`. |
| `bulk_create` | `await Model.bulk_create(objects, batch_size=500)` | Multi-row insert. |
| `bulk_update` | `await Model.bulk_update(objects, fields, batch_size=500)` | Batched multi-row update. |
| `in_bulk` | `await Model.in_bulk(ids, field_name="pk")` | Fetch many rows as a `{key: instance}` dict. |
| `get` | `await Model.get(**kwargs)` | Single row; raises `DoesNotExist` / `MultipleObjectsReturned`. |
| `get_or_none` | `await Model.get_or_none(**kwargs)` | Single row or `None`. |
| `all` | `Model.all()` | QuerySet over all rows. |
| `filter` / `exclude` | `Model.filter(*q, **lookups)` | Narrow / negate a QuerySet. |
| `annotate` | `Model.annotate(**aggregates)` | Add computed columns. |
| `prefetch_related` | `Model.prefetch_related(*specs)` | Prefetch reverse/m2m relations (no N+1). |
| `select_related` | `Model.select_related(*relations)` | Join-load forward FK/O2O relations in one query. |
| `raw` | `await Model.raw(sql, params=None)` | Raw SQL → model instances. |
| `save` | `await instance.save(update_fields=None)` | Persist (emits save signals). |
| `delete` | `await instance.delete()` | Delete the row (emits delete signals). |
| `refresh_from_db` | `await instance.refresh_from_db()` | Reload column values from the row. |
| `update_from_dict` | `instance.update_from_dict(data)` | Set fields in place (no DB write). |
| `fetch_related` | `await instance.fetch_related(*names)` | Populate relations on the instance. |
| `pk` | `instance.pk` | Primary key value. |

The inner `Meta` class supports `table`, `table_description` / `description`,
`abstract` (mark as a base model with no table; not inherited by subclasses),
`ordering` (default `ORDER BY` field list, e.g. `["-created_at"]`),
`unique_together` / `indexes` (composite constraints/indexes over field groups),
and `manager` (a `Manager` instance scoping the base queryset).

## fields

See the full table in [Models & fields](guides/models-and-fields.md). Field classes:
`SmallIntField`, `IntField`, `BigIntField`, `FloatField`, `DecimalField`, `CharField`,
`TextField`, `BinaryField`, `BooleanField`, `DatetimeField`, `DateField`, `TimeField`, `TimeDeltaField`,
`UUIDField`, `JSONField`, `IntEnumField`, `CharEnumField`, `ForeignKeyField`,
`OneToOneField`, `ManyToManyField`.

Common kwargs: `pk`, `null`, `default`, `unique`, `index`, `db_column`, `description`,
`validators`.

## validators

`yara_orm.validators` — attach via `validators=[...]`; runs on `save()`, raising
`ValidationError`. Classes: `Validator` (base), `MinValueValidator`, `MaxValueValidator`,
`MinLengthValidator`, `MaxLengthValidator`, `RegexValidator`. Functions:
`validate_ipv4_address`, `validate_ipv6_address`, `validate_ipv46_address`.

## timezone

`yara_orm.timezone` — helpers over `datetime` / `zoneinfo`: `now`, `is_aware`,
`is_naive`, `make_aware`, `make_naive`, `localtime`, `parse_timezone`,
`get_timezone`, `get_use_tz`, `get_default_timezone`.

## Database defaults & managers

Database-side column defaults (pass as a field `default`): `Now()`,
`RandomHex(size)`, `SqlDefault(sql)` (base `DatabaseDefault`). The database fills
the value on insert. `Manager` is the base queryset provider; subclass it and set
`Meta.manager` to scope every query. See [Models & fields](guides/models-and-fields.md).

## QuerySet & Q

Lazy and chainable; runs when awaited or on a terminal method. See [Querying](guides/querying.md).

**Chainable:** `filter`, `exclude`, `annotate`, `group_by`, `prefetch_related`,
`select_related`, `order_by`, `limit`, `offset`, `distinct`, `select_for_update`,
slicing (`qs[start:stop]`).

**Terminal (async):** `await qs` → `list[Model]`, `get`, `first`, `last`, `earliest`,
`latest`, `count`, `exists`, `values`, `values_list`, `delete`, `update`.

**`Q`** combines lookups with `&` (AND), `|` (OR), `~` (NOT).

**`F`** references a column for filters and arithmetic updates, e.g. `update(n=F("n") + 1)`
or `filter(a__gt=F("b"))`.

**Lookups:** `exact` (default), `not`, `gt`, `gte`, `lt`, `lte`, `in`, `isnull`, `contains`,
`icontains`, `startswith`, `istartswith`, `endswith`, `iendswith`.

## Aggregations & functions

`Count`, `Sum`, `Avg`, `Min`, `Max` — each constructed as `Agg(field, distinct=False)`.
Scalar functions `Lower`, `Upper`, `Length`, `Trim`, `Concat`, `Coalesce`, plus `Case`/`When`
and `RawSQL`, are also usable as `annotate()` expressions. See
[Aggregation & grouping](guides/aggregation.md).

## Relations

`Prefetch(relation, queryset=...)` customizes a prefetch. Forward FK access is awaitable;
reverse and many-to-many managers support `await`, `async for`, `.add/.remove/.clear`
(m2m), `.filter`, `.order_by`. See [Relations](guides/relations.md).

## Signals

Decorators `pre_save`, `post_save`, `pre_delete`, `post_delete` — each takes the model class.
Handlers are async. The `Signals` enum names the four lifecycle signals. See
[Signals](guides/signals.md) for exact signatures.

## Transactions

`in_transaction(connection_name="default", isolation=None)` (async context manager) and
`atomic(connection_name="default", isolation=None)` (decorator). Nested blocks open
savepoints automatically; `isolation` takes an `IsolationLevel` constant (PostgreSQL honours
all four, SQLite is serializable-only). See [Transactions](guides/transactions.md).

## Migrations

`migrations.MigrationManager(directory="migrations", app="models", models=None)` plus the
`migrations.Migration` base class (carrying `operations`, `dependencies`, `atomic`) and the
operation classes `CreateModel`, `DeleteModel`, `AddField`, `RemoveField`, `AlterField`,
`AddIndex`, `RemoveIndex`, `RenameModel`, `RenameField`, `RenameIndex`, `AddConstraint`,
`RemoveConstraint`, `RenameConstraint`, `RunSQL`, `RunPython` (constraints built with
`UniqueConstraint` / `CheckConstraint`). Generated migrations use the idempotent analogs
(`CreateModelIfNotExists`, `AddFieldIfNotExists`, …); `AddIndexConcurrently`,
`AddUniqueIndexConcurrently` and `RemoveIndexConcurrently` are for hand-written non-atomic
migrations. Constraint operations are PostgreSQL-only (SQLite raises `UnSupportedError`).
CLI: `python -m yara_orm …`. See [Migrations](guides/migrations.md).

## Dialects

`BaseDialect`, `PostgresDialect`, `SqliteDialect`, and `register_dialect(name, cls)` for
adding a backend. See [Backends](backends/index.md) and [Architecture](architecture.md).

## Exceptions

`ORMError` is the base (also exported as `BaseORMException`):
`ConfigurationError`, `OperationalError` (→ `DBConnectionError`,
`TransactionManagementError`, `IntegrityError`, `NotExistOrMultiple` → `DoesNotExist`
(alias `ObjectDoesNotExistError`) / `MultipleObjectsReturned`, `NoValuesFetched`),
`FieldError` (→ `ParamsError`, `ValidationError`), `IncompleteInstanceError`,
`UnSupportedError`.

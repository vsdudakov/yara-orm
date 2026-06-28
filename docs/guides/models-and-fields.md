---
title: Models & Fields
description: Define async Python ORM models and fields with yara_orm — typed columns, primary keys, enums, defaults and comments mapped to PostgreSQL and SQLite.
---

# Models & fields

`yara_orm` is an async Python ORM with a Rust engine. You describe your schema as
plain Python classes: subclass `Model`, declare typed fields as class attributes,
and the ORM maps each one onto a column for your database (PostgreSQL or SQLite).
Field declarations read like type hints, so models stay concise and self-documenting.

## Defining a model

A model is a subclass of `Model` whose class attributes are `Field` instances. A
metaclass collects those fields at class-creation time and builds a `_meta`
descriptor holding the resolved table name, field map and primary key.

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
    tags = fields.ManyToManyField("Tag", related_name="books")

class Tag(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50, unique=True)
```

!!! note "Automatic primary key"
    If you declare no field with `pk=True`, the metaclass inserts an
    auto-increment `id = IntField(pk=True)` for you. Declaring `id` explicitly,
    as above, simply makes that behaviour visible.

## The `Meta` inner class

An optional `Meta` inner class customises table-level metadata.

```python
class Author(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=120)

    class Meta:
        table = "authors"                       # default: "author" (lowercase model name)
        table_description = "Catalogue authors"  # emitted as a table COMMENT
```

| `Meta` attribute    | Purpose                                                              |
| ------------------- | ------------------------------------------------------------------- |
| `table`             | Custom table name. Defaults to the lowercase model class name.      |
| `table_description` | Human-readable table comment. Aliased as `description`.             |
| `description`       | Alternative spelling of `table_description` (used if the latter is unset). |

## Field reference

Every field maps to one column. Numeric and `UUID` primary keys auto-increment or
default automatically; see [Primary keys](#primary-keys) below.

| Field                | Key parameters                                          | Stores                          |
| -------------------- | ------------------------------------------------------- | ------------------------------- |
| `SmallIntField`      | —                                                       | small integer                   |
| `IntField`           | —                                                       | integer                         |
| `BigIntField`        | —                                                       | 64-bit integer                  |
| `FloatField`         | —                                                       | floating-point number           |
| `DecimalField`       | `max_digits=12`, `decimal_places=2`                     | `decimal.Decimal`               |
| `CharField`          | `max_length=255`                                        | variable-length string          |
| `TextField`          | —                                                       | unbounded text                  |
| `BinaryField`        | —                                                       | `bytes`                         |
| `BooleanField`       | —                                                       | `bool`                          |
| `DatetimeField`      | `auto_now=False`, `auto_now_add=False`                  | `datetime.datetime`             |
| `DateField`          | —                                                       | `datetime.date`                 |
| `TimeField`          | —                                                       | `datetime.time`                 |
| `UUIDField`          | `pk` defaults `default` to `uuid4`                      | `uuid.UUID`                     |
| `JSONField`          | —                                                       | JSON-serialisable value         |
| `IntEnumField`       | `enum_type`                                             | `IntEnum` member (as integer)   |
| `CharEnumField`      | `enum_type`, `max_length=255`                           | string `Enum` member            |
| `ForeignKeyField`    | `reference`, `related_name`, `on_delete`, `source_field`| foreign key (`<name>_id` column)|
| `OneToOneField`      | `reference` (unique FK)                                 | one-to-one foreign key          |
| `ManyToManyField`    | `reference`, `related_name`, `through`                  | join-table relation (no column) |

!!! tip "Auto timestamps"
    Set `auto_now_add=True` to stamp the row once at creation, or `auto_now=True`
    to refresh the value on every `save()`. In the canonical `Author` model,
    `created_at` uses `auto_now_add=True`.

## Common keyword arguments

Every concrete field accepts the same column options:

| Keyword       | Default | Meaning                                                        |
| ------------- | ------- | -------------------------------------------------------------- |
| `pk`          | `False` | Mark the column as the primary key.                            |
| `null`        | `False` | Allow `NULL` values.                                           |
| `default`     | `None`  | Default value, or a callable producing one.                    |
| `unique`      | `False` | Add a unique constraint.                                       |
| `index`       | `False` | Create an index on the column.                                 |
| `db_column`   | `None`  | Explicit column name (defaults to the attribute name).         |
| `description` | `None`  | Column comment (emitted as a SQL `COMMENT`).                   |

```python
import uuid

class Session(Model):
    token = fields.CharField(max_length=64, unique=True, index=True)
    ref = fields.UUIDField(default=uuid.uuid4)         # callable default
    attempts = fields.IntField(default=0)              # value default
    label = fields.CharField(max_length=80, db_column="display_label")
    note = fields.TextField(null=True)
```

!!! note "Callable defaults"
    A `default` that is callable is invoked per row at insert time, so
    `default=uuid.uuid4` produces a fresh value for each instance rather than one
    shared value.

## Enum fields

`IntEnumField` stores an `IntEnum` as its integer value; `CharEnumField` stores a
string `Enum` as its `.value`. Both read back as live enum members, and you can
filter by member directly.

```python
from enum import Enum, IntEnum

class Service(IntEnum):
    PYTHON = 1
    RUST = 2

class Currency(str, Enum):
    HUF = "HUF"
    USD = "USD"

class Account(Model):
    service = fields.IntEnumField(Service)
    currency = fields.CharEnumField(Currency, max_length=3, default=Currency.HUF)

acc = await Account.create(service=Service.RUST)
reloaded = await Account.get(id=acc.id)
assert reloaded.service is Service.RUST          # reads back as the enum member

huf = await Account.filter(currency=Currency.HUF)  # filter by member
```

## Column & table comments

Use `description=` on a field for a column comment and `Meta.table_description`
for a table comment.

```python
class Described(Model):
    name = fields.CharField(max_length=50, description="the display name")
    note = fields.TextField(null=True)

    class Meta:
        table = "d_described"
        table_description = "a fully described table"
```

!!! note "PostgreSQL comments"
    On PostgreSQL these become real SQL `COMMENT` statements: the table comment is
    readable via `obj_description(...)` and the column comment via
    `col_description(...)`. They document your schema directly in the database.

## Primary keys

- `IntField(pk=True)` (also `SmallIntField` and `BigIntField`) creates an
  auto-increment primary key; the database assigns the value on insert.
- `UUIDField(pk=True)` defaults its `default` to `uuid.uuid4`, so a fresh UUID is
  generated for each row unless you pass an explicit `default`.

```python
import uuid

class Event(Model):
    id = fields.UUIDField(pk=True)   # default=uuid.uuid4 applied automatically
    name = fields.CharField(max_length=100)
```

## Relation fields

`ForeignKeyField`, `OneToOneField` and `ManyToManyField` declare relationships
between models. A foreign key synthesises a concrete `<name>_id` backing column
and installs forward and reverse accessors; a many-to-many field adds no column
and instead manages a join table. Their full behaviour — accessors, `related_name`,
`on_delete` and prefetching — is covered in the Relations guide.

!!! tip "See the Relations guide"
    For `await book.author`, reverse managers and `await book.tags.add(...)`, see
    [Relations](relations.md).

## See also

- [Querying](querying.md)
- [Relations](relations.md)
- [Migrations](migrations.md)

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
| `abstract`          | Mark the model as an abstract base — no table of its own; see below. |
| `ordering`          | Default `ORDER BY` for queries that set no explicit `order_by`; see below. |
| `unique_together`   | Composite `UNIQUE` constraint(s) over groups of field names.         |
| `indexes`           | Composite index(es) over groups of field names.                     |
| `constraints`       | Declarative `UniqueConstraint` / `CheckConstraint` objects; see below. |
| `manager`           | A custom `Manager` scoping every query; see [Custom managers](#custom-managers). |
| `fetch_db_defaults` | Read database-computed defaults back onto the instance on insert; see [Database-side defaults](#database-side-defaults). |

```python
class Booking(Model):
    room = fields.CharField(max_length=10)
    slot = fields.IntField()
    day = fields.CharField(max_length=10)

    class Meta:
        unique_together = ("room", "slot")    # one group; or (("room", "slot"), ...)
        indexes = (("day", "slot"),)          # composite index
```

A field name in `unique_together` / `indexes` may be a foreign-key relation
name; it resolves to that relation's backing column.

#### Partial (conditional) indexes

For a custom name or a **partial index** (an index with a `WHERE` predicate),
put an `Index` object in `Meta.indexes` instead of a plain group. Plain groups
and `Index` objects can be mixed:

```python
from yara_orm import Index

class Job(Model):
    id = fields.IntField(pk=True)
    status = fields.CharField(max_length=20)
    priority = fields.IntField()

    class Meta:
        indexes = [
            ("status", "priority"),                       # plain composite index
            Index(
                fields=["priority"],
                name="idx_active_priority",
                condition="status = 'active'",            # -> CREATE INDEX ... WHERE status = 'active'
            ),
        ]
```

Partial indexes are supported on both PostgreSQL and SQLite, and the condition
round-trips through [migrations](migrations.md).

To introspect the DDL an `Index` produces, call
`index.get_sql(model, dialect=None, safe=True)`. It renders the `CREATE INDEX`
statement for the index on `model`; `dialect` defaults to the model's active
connection dialect, and `safe=True` adds an `IF NOT EXISTS` guard:

```python
idx = Index(fields=["priority"], name="idx_active_priority", condition="status = 'active'")
print(idx.get_sql(Job))          # "CREATE INDEX IF NOT EXISTS idx_active_priority ON ..."
```

### Declarative constraints

`Meta.constraints` takes `UniqueConstraint` / `CheckConstraint` objects, emitted
in the `CREATE TABLE` by `generate_schemas()`:

```python
from yara_orm import CheckConstraint, UniqueConstraint

class Account(Model):
    id = fields.IntField(pk=True)
    email = fields.CharField(max_length=200)
    balance = fields.IntField(default=0)

    class Meta:
        constraints = [
            UniqueConstraint(fields=["email"], name="uq_account_email"),
            CheckConstraint(check="balance >= 0", name="ck_account_balance"),
        ]
```

### Introspection & helpers

- `Model.describe()` returns a structured dict of the model's schema (table, primary key, fields, relations, `Meta` options) — handy for tooling.
- `instance.clone()` returns an unsaved copy with no primary key, ready to `save()` as a new row (pass `clone(pk=...)` to set one).
- All query-set terminals are also model classmethods: `await Book.first()`, `await Book.exists(...)`, `await Book.values_list("title", flat=True)`, etc.
- Instances hash by primary key, so an **unsaved** instance (pk still `None`)
  is unhashable — putting one in a `set`/`dict` raises `TypeError` rather than
  silently going stale when `save()` assigns the pk.

### Default ordering

Set `ordering` to a list of field names to apply a default `ORDER BY` to every
query on the model that does not call `order_by` itself. Prefix a name with `-`
for descending order; `pk` is accepted as an alias for the primary key. Each name
is validated against the model's fields at class-creation time.

```python
class Message(Model):
    id = fields.IntField(pk=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "messages"
        ordering = ["-created_at"]      # newest first by default

await Message.all()                      # ORDER BY created_at DESC
await Message.all().order_by("id")       # explicit order_by overrides the default
```

An explicit `order_by()` always takes precedence over `Meta.ordering`.

### Abstract base models

Set `abstract = True` to define a reusable base whose fields are inherited by
subclasses but which has no table of its own. An abstract model is left out of
the registry, so schema generation and migrations skip it. `abstract` is read
from each class's own `Meta` only — it is **not** inherited, so a subclass is
concrete unless it redeclares `abstract = True`.

```python
import uuid

class TimestampedModel(Model):
    id = fields.UUIDField(pk=True, default=uuid.uuid4)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        abstract = True

class Article(TimestampedModel):     # concrete: gets an "article" table with
    title = fields.CharField()       # id, created_at, updated_at and title
```

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
| `TimeDeltaField`     | —                                                       | `datetime.timedelta`            |
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

!!! note "ISO-8601 string input is coerced"
    `DateField`, `DatetimeField` and `TimeField` accept ISO-8601 **string** input
    (a trailing `Z` is accepted) and coerce it to a real `date` / `datetime` /
    `time`. So after `await Event.create(created_at="2026-07-01T12:00:00+00:00")`
    the instance attribute is a genuine `datetime` object — `created_at.isoformat()`
    works without a reload.

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
| `validators`  | `None`  | List of validators run against the value on `save()`.          |

!!! note "Tortoise parameter spellings accepted"
    The modern Tortoise names are accepted as aliases: `primary_key` (→ `pk`),
    `db_index` (→ `index`), `source_field` (→ `db_column`), `db_default`
    (→ `default`), and `to` (→ `reference`) on `ForeignKeyField` /
    `ManyToManyField`.

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
    shared value. A **mutable literal** default (`default={}` or `default=[]` on a
    `JSONField`, say) is copied per instance, so mutating one instance's value
    never leaks into another.

!!! note "`BooleanField` string input is semantic"
    Strings are coerced by meaning, not truthiness: `"true"/"t"/"1"/"yes"/"y"/"on"`
    → `True`, `"false"/"f"/"0"/"no"/"n"/"off"` → `False` (case-insensitive), and
    an unrecognised string raises `ValueError` instead of silently binding `True`.

## Validators

Attach validators to a field with `validators=[...]`. They run on `save()` and
raise `ValidationError` for an invalid value (a `None` value on a nullable field
is skipped). The validators live in `yara_orm.validators`:

```python
from yara_orm import fields
from yara_orm.validators import MinValueValidator, MaxLengthValidator, RegexValidator


class Account(Model):
    handle = fields.CharField(max_length=30, validators=[RegexValidator(r"^[a-z0-9_]+$")])
    age = fields.IntField(validators=[MinValueValidator(0)])
    bio = fields.CharField(max_length=200, null=True, validators=[MaxLengthValidator(200)])
```

Available validators: `MinValueValidator`, `MaxValueValidator`, `MinLengthValidator`,
`MaxLengthValidator`, `RegexValidator`, and the IP-address functions
`validate_ipv4_address` / `validate_ipv6_address` / `validate_ipv46_address`.
Write your own by subclassing `Validator` and implementing `__call__` to raise
`ValidationError` on failure.

## Database-side defaults

Pass `Now()`, `RandomHex(size)`, or `SqlDefault(sql)` as a field's `default` to
emit a SQL `DEFAULT` clause in the column DDL. The column is omitted from the
`INSERT`, so the **database** computes the value (not Python):

```python
from yara_orm import fields, Now, RandomHex, SqlDefault


class Session(Model):
    created = fields.DatetimeField(default=Now())          # DEFAULT CURRENT_TIMESTAMP
    token = fields.CharField(max_length=64, default=RandomHex(16))
    state = fields.IntField(default=SqlDefault("0"))
```

`Now()` and `SqlDefault(...)` are portable; `RandomHex` renders per backend
(SQLite honours the byte count, PostgreSQL uses a 32-char `md5`). By default the
value is filled on insert and left off the in-memory instance — set
`Meta.fetch_db_defaults = True` to have `create()` / `save()` read the computed
values back onto the instance via `INSERT ... RETURNING` (both backends):

```python
class Session(Model):
    created = fields.DatetimeField(default=Now())

    class Meta:
        fetch_db_defaults = True

s = await Session.create()
s.created                    # the database-computed timestamp, no reload needed
```

Passing an **explicit value** for a db-default column stores that value instead
of the default. And a full `save()` never writes `NULL` over a db-default column
whose value was simply never fetched — the column is skipped unless you fetched
it, set it, or name it in `update_fields`.

Database-side defaults round-trip through [migrations](migrations.md): the
`DEFAULT` clause is emitted in migration DDL and default changes are
autodetected.

## Custom managers

Set `Meta.manager` to a `Manager` subclass to scope every query (`all`,
`filter`, `get`, …) — for example to hide soft-deleted rows:

```python
from yara_orm import Manager


class ActiveManager(Manager):
    def get_queryset(self):
        return super().get_queryset().filter(deleted=False)


class Article(Model):
    deleted = fields.BooleanField(default=False)

    class Meta:
        manager = ActiveManager()
```

A custom manager declared on an [abstract base](#abstract-base-models) is
inherited by its concrete subclasses (each class gets its own bound copy), so a
soft-delete scope written once applies to every subclass.

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

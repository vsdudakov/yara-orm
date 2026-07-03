---
title: Working with JSON
description: Store and query JSON in yara_orm — JSONField declaration, automatic value coercion (UUID/Decimal/datetime/bytes/enum), key-path lookups (data__key, data__a__b), full-text search, and encoder/decoder hooks on PostgreSQL, MySQL and SQLite.
---

# Working with JSON

`JSONField` stores a JSON document per row — a dict, a list, or any nested
mixture of JSON-native values. On PostgreSQL it maps to `JSONB`; on MySQL to the
native `JSON` type; on SQLite it is
stored as JSON text. The engine serialises and parses the JSON itself, so you
work with ordinary Python `dict`/`list` values on both sides.

```python
from yara_orm import Model, fields

class Event(Model):
    id = fields.IntField(pk=True)
    payload = fields.JSONField(null=True)

await Event.create(payload={"kind": "signup", "tags": ["a", "b"], "meta": {"ip": "1.2.3.4"}})
row = await Event.get(id=1)
row.payload           # {'kind': 'signup', 'tags': ['a', 'b'], 'meta': {'ip': '1.2.3.4'}}
row.payload["tags"]   # ['a', 'b']
```

## Storing values

Beyond the JSON-native types (`dict`, `list`, `str`, `int`, `float`, `bool`,
`None`), the engine coerces the stdlib types apps most often drop into a JSON
document to their JSON form, in a single native pass at bind time:

| Python value            | Stored as                    |
| ----------------------- | ---------------------------- |
| `uuid.UUID`             | its string form              |
| `decimal.Decimal`       | its string form              |
| `datetime` / `date` / `time` | `.isoformat()` string   |
| `bytes` / `bytearray`   | base64 string                |
| `set` / `frozenset` / `tuple` | JSON array             |
| `enum.Enum` member      | its `.value` (recursively)   |

```python
import datetime as dt, uuid
from decimal import Decimal

await Event.create(payload={
    "ref": uuid.uuid4(),          # -> "5f0e...-..." (string)
    "price": Decimal("12.34"),    # -> "12.34"
    "at": dt.datetime(2026, 7, 1, 12, 0),  # -> "2026-07-01T12:00:00"
    "blob": b"\x00\x01",          # -> base64 string
    "tags": {"x", "y"},           # -> ["x", "y"]
})
```

!!! warning "Coercion is one-way"
    These conversions run on **write** only. A `Decimal` you store reads back as
    the string `"12.34"`, not a `Decimal`; a `UUID` reads back as its string.
    JSON has no native type for them, so reconstruct on your side if you need the
    original type (or use a dedicated column — `DecimalField`, `UUIDField`, … —
    when you want a typed round-trip).

A value with no JSON representation (an arbitrary object) raises a `TypeError` at
save time rather than storing corrupt data:

```python
await Event.create(payload={"bad": object()})
# TypeError: value of type object is not JSON serialisable
```

## Querying by key path

Filter on a key inside the document with a `__`-separated path. The leading
segment is the `JSONField`; the rest are object keys, extracted per dialect
(PostgreSQL `->`/`->>`, MySQL `JSON_UNQUOTE(JSON_EXTRACT(...))`, SQLite `json_extract`):

```python
await Event.filter(payload__kind="signup")          # top-level key
await Event.filter(payload__meta__ip="1.2.3.4")     # nested path
```

Any of the usual text lookups apply to the extracted value:

```python
await Event.filter(payload__kind__contains="sign")      # LIKE
await Event.filter(payload__kind__icontains="SIGN")     # ILIKE (case-insensitive)
await Event.filter(payload__kind__startswith="sign")
await Event.filter(payload__missing__isnull=True)       # key absent or JSON null
```

!!! important "Key-path values compare as text"
    A key path extracts its value **as text**, so compare it against **string**
    values:

    ```python
    await Event.filter(payload__count="5")     # ✅  matches {"count": 5}
    await Event.filter(payload__count=5)       # ❌  operator does not exist: text = bigint
    ```

    For numeric ordering, boolean logic, or range filters on a JSON value, either
    store it in a typed column instead, or drop to [manual SQL](manual-sql.md)
    with an explicit cast (`(payload->>'count')::int > 10`). Note `__isnull=True`
    matches both an **absent** key and an explicit JSON `null`.

## Full-text search across the whole document

A **case-insensitive / pattern** lookup on the `JSONField` itself (no key path)
searches the serialised JSON — the column is cast to text automatically, so
`ILIKE` works against `JSONB`:

```python
await Event.filter(payload__icontains="signup")   # CAST(payload AS TEXT) ILIKE '%signup%'
```

## Containment: `__contains` (PostgreSQL and MySQL)

`__contains` on the `JSONField` itself is **structural containment** (`@>`), not
a text search — it matches an object subset, an array element, or an
array-of-objects subset:

```python
await Event.filter(payload__contains={"kind": "signup"})    # object subset
await Event.filter(payload__contains={"tags": ["a"]})       # array-element subset
# array of objects (e.g. a related JSON column)
await Call.filter(contact__tags__contains=[{"name": "vip"}])
```

!!! note
    `@>` is PostgreSQL-only; MySQL renders the same semantics with
    `JSON_CONTAINS(...)`. `__contains` on a JSON column raises `UnSupportedError`
    on SQLite. (A key-path `payload__key__contains="x"` is still a text `LIKE` on
    the extracted value.)

## JSON-path filtering: `__filter`

`__filter` takes a dict of `path__op: value` entries and ANDs them, each resolved
as a key-path condition on the column (Tortoise's JSON `__filter`):

```python
await Task.filter(hubspot_task_data__filter={
    "properties__hs_task_subject__in": ["Call", "Email"],
})
await Log.filter(audit_log_meta__filter={
    "status__not": "resolved",
    "task_name__icontains": "sync",
})
```

Each entry supports the usual lookups (`exact`, `in`, `not`, `icontains`, …); the
values compare as text, as with any key path.

## Updating

JSON values are replaced wholesale — read, modify in Python, write back:

```python
row = await Event.get(id=1)
data = dict(row.payload)
data["seen"] = True
await Event.filter(id=row.id).update(payload=data)
```

There is no partial (`jsonb_set`-style) update through the queryset API; use
[manual SQL](manual-sql.md) for in-place key updates.

## Encoder / decoder hooks

`encoder` and `decoder` are optional **value-transform** hooks — not full
serialisers (the engine still handles JSON itself). `encoder` runs on the Python
value before it is stored; `decoder` runs on the value read back:

```python
class Doc(Model):
    id = fields.IntField(pk=True)
    body = fields.JSONField(
        encoder=lambda v: {**v, "v": 2},        # tag every stored document
        decoder=lambda v: {k: x for k, x in v.items() if k != "v"},
    )
```

Use them for value migrations (e.g. making oversized integers JS-safe) rather
than for (de)serialisation. An `encoder` that returns a JSON **string** is parsed
back to a native value before storage, so it never double-encodes the column.

## Raw SQL and arrays

When you pass a bare Python `list` as a **raw-SQL** parameter it binds as a
PostgreSQL **array** (asyncpg-style), so `WHERE col = ANY($1)` works with a plain
list. This is independent of `JSONField` — to bind a JSON value in a raw query,
pass a `dict` or a JSON string. See [Manual SQL](manual-sql.md) for details.

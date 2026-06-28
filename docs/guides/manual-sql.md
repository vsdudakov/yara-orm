---
title: Manual SQL
description: Run raw SQL in an async Python ORM — use Model.raw and connections.get for parameterized queries with bind parameters on PostgreSQL and SQLite.
---

# Manual SQL

When the query builder is not enough, `yara_orm` lets you drop down to raw SQL while
staying fully async. You can hydrate hand-written `SELECT`s straight into model
instances with `Model.raw`, or reach the active executor through `connections.get`
for low-level `execute` and `fetch_*` calls. Every entry point takes **parameterized
queries**, so values are bound by the driver rather than spliced into the SQL string.

```python
from yara_orm import Model, connections, fields
```

## `Model.raw` — rows as model instances

`await Model.raw(sql, params=None)` runs a query and returns a `list[Model]`
instances built from each row.

!!! important
    The `SELECT` must return columns in the model's **field-list order**. Rows are
    consumed positionally, so prefer listing columns explicitly (or `SELECT *` only
    when the table column order matches the model's fields).

```python
class Thing(Model):
    name = fields.CharField(max_length=50)

    class Meta:
        table = "m_thing"


# Returns [Thing(...), ...] — real model instances, not raw rows.
things = await Thing.raw(
    "SELECT * FROM m_thing WHERE name = $1",
    ["alpha"],
)
assert things[0].name == "alpha"
```

`params` defaults to `None` (treated as no parameters). Pass a list to bind values.

## Low-level access with `connections.get`

For SQL that does not map to a model — `INSERT`, `UPDATE`, `DELETE`, aggregates,
DDL — use the executor returned by `connections.get(name="default")`. It is the
**active executor**: an open transaction when one is in scope, otherwise the named
connection's pool.

```python
conn = connections.get("default")
```

The executor exposes four async methods:

| Method | Returns |
| --- | --- |
| `await conn.execute(sql, params)` | The driver's execute result — for a single statement, the number of affected rows. |
| `await conn.fetch_all(sql, params)` | All result rows as **dict-like rows** keyed by column name. |
| `await conn.fetch_rows(sql, params)` | All result rows as **positional rows** (what `Model.raw` consumes internally). |
| `await conn.fetch_row(sql, params)` | A single row, or `None` when the query matches nothing. |

```python
conn = connections.get("default")

# execute() reports affected rows.
affected = await conn.execute(
    "INSERT INTO m_thing (name) VALUES ($1)",
    ["x"],
)
assert affected == 1

# fetch_all() returns dict rows you can index by column name.
rows = await conn.fetch_all("SELECT name FROM m_thing ORDER BY name")
names = [r["name"] for r in rows]
```

## Parameter placeholders per backend

Placeholder syntax is **dialect-specific**. PostgreSQL uses `$1, $2, ...`; SQLite
uses `?1, ?2, ...`. Use the form that matches the backend you connected to.

=== "PostgreSQL"

    ```python
    conn = connections.get("default")
    rows = await conn.fetch_all(
        "SELECT name FROM m_thing WHERE name = $1 OR name = $2",
        ["alpha", "beta"],
    )
    ```

=== "SQLite"

    ```python
    conn = connections.get("default")
    rows = await conn.fetch_all(
        "SELECT name FROM m_thing WHERE name = ?1 OR name = ?2",
        ["alpha", "beta"],
    )
    ```

!!! warning "Always bind values via `params`"
    Never build SQL by interpolating values into the string (f-strings, `+`,
    `.format()`). Pass every value through the `params` list and reference it with a
    placeholder so the driver binds it. String interpolation opens you to **SQL
    injection** and breaks on quoting, `NULL`, and type coercion.

## Manual SQL inside a transaction

When you are inside an `in_transaction()` block, `connections.get` and `Model.raw`
both route through the **active transaction automatically** — there is nothing extra
to wire up. Your raw statements share the same transaction as the surrounding
ORM calls and are committed or rolled back together.

```python
from yara_orm import in_transaction

async with in_transaction():
    conn = connections.get("default")
    await conn.execute("INSERT INTO m_thing (name) VALUES ($1)", ["x"])
    await Thing.create(name="y")  # same transaction
```

See [Transactions](transactions.md) for the full lifecycle and rollback semantics.

## See also

- [Querying](querying.md)
- [Transactions](transactions.md)

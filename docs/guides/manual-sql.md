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
| `await conn.fetch_one(sql, params)` | A single **dict** row, or `None` when nothing matches. |

For projects migrating from Tortoise ORM, the executor also accepts Tortoise's
spellings: `await conn.execute_query(sql, params)` returns a `(rowcount, rows)`
tuple (rows as dicts), `await conn.execute_query_dict(sql, params)` returns the rows
as dicts, and `await conn.execute_script(script)` runs a **multi-statement** script
(it splits on `;`, leaving dollar-quoted `DO $$ … $$` blocks, string literals and
comments intact). A database error surfaces as `OperationalError` from these methods,
so existing `except OperationalError` handlers keep working.

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

## Binding lists as PostgreSQL arrays

A bare Python `list` (or `tuple`) passed as a raw-SQL param binds as a PostgreSQL
**array** (asyncpg-style), coercing element types as needed (`UUID`, `Decimal`,
`date`, …). Reference it with `ANY($n)` or `unnest($n::type[])`:

```python
conn = connections.get("default")

# The bare list [1, 2, 3] binds as an int[] array
rows = await conn.execute_query(
    "SELECT * FROM m_thing WHERE id = ANY($1)",
    [[1, 2, 3]],
)

# unnest a bound array
rows = await conn.execute_query(
    "SELECT * FROM unnest($1::int[]) AS id",
    [[1, 2, 3]],
)
```

!!! warning "A bare list is an array, not JSON"
    This is a change from older behaviour: a bare list is now an **array** bind, so
    to bind a JSON value in a raw query pass a `dict` or a JSON string instead — a
    Python list is not treated as JSON here.

### Forcing array binding with `Array`

Wrap a sequence in `Array` to mark it explicitly as an array parameter — useful
for clarity or when the value could otherwise be ambiguous. Array columns read
back as plain Python lists.

```python
from yara_orm import Array

ids = [1, 2, 3]
rows = await conn.execute_query(
    "SELECT * FROM m_thing WHERE id = ANY($1)",
    [Array(ids)],
)
```

## Positional and named row access

Rows returned by `execute_query` / `fetch_all` support **both** positional and
named indexing (`asyncpg.Record` parity), so you can read a column by ordinal or
by name off the same row:

```python
rows = await conn.fetch_all("SELECT id, name FROM m_thing ORDER BY id")
row = rows[0]
row[0]         # positional access -> the id
row["name"]    # named access -> the name
```

## Inspecting a query's exact SQL and params

`QuerySet.get_parameterized_sql()` returns the `(sql, params)` pair for **any**
query shape — plain, `only()`/`defer()`, `select_related`, `annotate`, and grouped
`values()` — built from the same compile path the query set executes. It is the
public way to inspect the exact statement and bind values without reaching into
internals, complementing [`.sql()` / `.explain()`](querying.md#inspecting-a-query-sql-explain):

```python
sql, params = Book.filter(rating__gte=4).get_parameterized_sql()

# Works for grouped/annotated projections too
sql, params = (
    Author.annotate(n=Count("books")).group_by("id").values("id", "n")
).get_parameterized_sql()
```

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

## Observing queries with hooks

Register a hook to observe every SQL statement before it runs — useful for query
logging, tracing, or [SQLCommenter](https://google.github.io/sqlcommenter/)-style
annotation. Each hook is called as `hook(sql, params)`; the return value is ignored.

```python
from yara_orm import register_query_hook, clear_query_hooks

statements: list[str] = []
register_query_hook(lambda sql, params: statements.append(sql))

await Thing.all()                       # model queries fire the hook too
await connections.get().execute("SELECT 1")

clear_query_hooks()                     # back to zero overhead
```

While at least one hook is registered, both model and manual statements route through
a proxy that invokes the hooks; with none registered there is **no overhead** — the
hot path uses the raw engine.

## See also

- [Querying](querying.md)
- [Transactions](transactions.md)

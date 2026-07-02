---
title: Multiple databases
description: Connect to multiple databases and route reads/writes in an async Python ORM, sending queries across PostgreSQL and SQLite read-replica connections.
---

# Multiple databases

`yara_orm` can talk to more than one database at once. You register a set of named
connections, then hand the async Python ORM a small **router** object that picks a
connection per model and per operation. This is how you split reads onto a
**read replica**, keep certain models in their own database, or mix backends
(for example a PostgreSQL primary alongside a SQLite cache). Routing is fully
transparent: your model and queryset code stays the same while the engine resolves
the right connection underneath.

## Registering connections

The first connection is created by `init` and is always named `"default"`. Every
additional database is registered by name with `add_connection`.

```python
from yara_orm import YaraOrm

# The default connection (the primary).
await YaraOrm.init("postgres://user:pass@primary/app")

# A named read replica.
await YaraOrm.add_connection("replica", "postgres://user:pass@replica/app")
```

You can pass the router straight to `init` (shown below) or attach it later with
`set_router`.

!!! note "The default is special"
    `"default"` is the fallback for any model the router does not place, and it is
    the connection used by transactions unless told otherwise. There is always
    exactly one default connection.

## The router

A router is a plain class. The engine calls two methods on it, passing the model
class for the query:

- `db_for_read(model)` — name of the connection to read from.
- `db_for_write(model)` — name of the connection to write to.

Each method returns a connection name, or a falsy value (`None`, `""`) to fall
back to `"default"`. That is the entire interface — no base class to inherit, no
registration step.

```python
class ReplicaRouter:
    """Writes go to the primary; reads go to the replica."""

    def db_for_write(self, model):
        return "default"

    def db_for_read(self, model):
        return "replica"
```

Wire it up at init time:

```python
from yara_orm import YaraOrm

await YaraOrm.init("postgres://user:pass@primary/app", router=ReplicaRouter())
await YaraOrm.add_connection("replica", "postgres://user:pass@replica/app")
```

Now `await User.all()` reads from `"replica"`, while `await User.create(...)`
writes to `"default"` — no change to call sites.

### Routing a specific model elsewhere

Because the model class is passed in, you can route per model. Return a falsy
value to let everything else fall back to the default:

```python
class AnalyticsRouter:
    """Keep Event in its own database; everything else stays on default."""

    def db_for_read(self, model):
        if model.__name__ == "Event":
            return "analytics"
        return None  # falls back to "default"

    def db_for_write(self, model):
        return self.db_for_read(model)
```

!!! tip "Reuse one method"
    When reads and writes share a destination, have `db_for_write` delegate to
    `db_for_read` (as above) so there is a single source of truth.

## Replacing the router later

You do not have to decide the routing policy at startup. Set or swap the active
router at any time with `set_router`:

```python
from yara_orm import YaraOrm

YaraOrm.set_router(AnalyticsRouter())
```

Calling `set_router(None)` removes routing entirely, sending every query to the
default connection.

## Routing and transactions

Inside `in_transaction()` the active transaction is **pinned per connection
name**: every statement that resolves (via the router or `using_db`) to the
transaction's connection runs on it, sharing a single unit of work. Statements
whose model routes to a **different** named connection are *not* absorbed —
they keep using their own connection's pool (or its own transaction, if you
opened one).

```python
from yara_orm import in_transaction

async with in_transaction():
    # Both models route to "default", so both statements share the transaction.
    await User.create(name="Ada")
    await Account.create(owner="Ada")

async with in_transaction():                # on "default"
    await User.create(name="Ada")           # joins the transaction
    await Event.create(kind="signup")       # routed to "analytics": runs on its
                                            # own connection, outside this transaction
```

!!! warning "Cross-database transactions"
    A transaction spans a single connection. Models that the router places on a
    different database are not part of that connection's transaction — wrap them
    in their own `in_transaction("name")` block (nested blocks on different
    names are independent sibling transactions), and plan write boundaries
    around which database each model lives in. See
    [Transactions](transactions.md) for the full lifecycle.

## Mixed backends

Each connection resolves its own SQL dialect from its URL, so backends are
independent per connection. A PostgreSQL primary can sit beside a SQLite
connection with no extra configuration:

```python
from yara_orm import YaraOrm

await YaraOrm.init("postgres://user:pass@primary/app")
await YaraOrm.add_connection("cache", "sqlite://./cache.db")


class CacheRouter:
    def db_for_read(self, model):
        return "cache" if model.__name__ == "Snapshot" else "default"

    def db_for_write(self, model):
        return self.db_for_read(model)


YaraOrm.set_router(CacheRouter())
```

The ORM generates each model's schema on its routed write connection, so
`Snapshot` tables are created in SQLite while the rest land in PostgreSQL. See
[Backends](../backends/index.md) for the supported databases and URL formats.

## Manual SQL on a specific connection

Use `connections.get(name)` to grab a particular connection and run raw SQL
against it. It exposes `execute`, `fetch_row`, `fetch_rows`, and `fetch_all`:

```python
from yara_orm import connections

# Count rows directly on the replica.
rows = await connections.get("replica").fetch_rows("SELECT count(*) FROM users")
print(rows[0][0])

# Default connection when no name is given.
await connections.get().execute("VACUUM ANALYZE")
```

An unregistered (or misspelled) name raises `ConfigurationError` rather than
silently falling back to the default connection.

!!! note
    Inside a transaction, `connections.get(name)` for the transaction's own
    connection name returns the active transaction, so your manual SQL joins the
    same unit of work; other names return their own independent connections.

## See also

- [Transactions](transactions.md)
- [Backends](../backends/index.md)

---
title: Transactions
description: Database transactions in an async Python ORM — wrap writes in atomic blocks that commit on success and roll back on errors, on PostgreSQL & SQLite.
---

# Transactions

A **transaction** groups several statements into one **atomic** unit of work: either every change is **committed** together, or none of them are. `yara-orm` exposes transactions as first-class building blocks of the **async Python ORM**, so a block of model and queryset calls either persists as a whole or **rolls back** cleanly when something goes wrong.

There are two ways to open a transaction, both imported from `yara_orm`: the `in_transaction` async context manager and the `@atomic` decorator. Both work identically on the **PostgreSQL** and **SQLite** backends.

## `in_transaction` context manager

`async with in_transaction(connection_name="default"):` begins a transaction on the named connection. Every model and queryset statement executed inside the block routes through that single pinned connection. The transaction **commits** on a clean exit of the block and **rolls back** if the block raises.

```python
from yara_orm import in_transaction


async def transfer(source_id: int, dest_id: int, amount: int) -> None:
    async with in_transaction():
        await Account.filter(id=source_id).update(balance=F("balance") - amount)
        await Account.filter(id=dest_id).update(balance=F("balance") + amount)
```

Both updates happen as one atomic operation: if the process is interrupted between the two writes, neither is persisted. The two statements share the same transaction even though neither is told which connection to use.

!!! warning "Exceptions trigger a rollback"
    If the body of an `in_transaction` block raises, the transaction is rolled back and **nothing inside it is persisted** — then the exception propagates out of the block. Only a clean exit commits. Do not swallow exceptions inside the block if you expect the writes to survive.

## `@atomic` decorator

The `@atomic(connection_name="default")` decorator wraps an async function so its entire body runs inside a transaction — it is a thin convenience over `in_transaction`. Import `atomic` from `yara_orm`.

```python
from yara_orm import atomic


@atomic()
async def register_user(name: str, email: str) -> User:
    user = await User.create(name=name, email=email)
    await Profile.create(user_id=user.id)
    return user
```

Each call to `register_user` opens its own transaction; the return value of the wrapped coroutine is passed straight through. If the body raises, the transaction is rolled back before the exception propagates.

## Rollback on exception

When an exception escapes a transaction block, every write made inside it is undone:

```python
from yara_orm import in_transaction

# Nothing below is persisted: the RuntimeError rolls the whole block back.
try:
    async with in_transaction():
        await Account.create(name="A", balance=100)
        raise RuntimeError("boom")
except RuntimeError:
    pass

assert await Account.all().count() == 0
```

The `create` call ran, but because the block raised before exiting cleanly, the transaction is rolled back and the row never reaches the database.

## Active transaction is pinned automatically

While a transaction is active it is pinned in a context variable, so statements automatically use it — you never pass a connection to individual queries. Reads inside the transaction also see the uncommitted writes made earlier in the same block:

```python
async with in_transaction():
    await Account.create(name="Z", balance=5)
    found = await Account.get(name="Z")  # sees the uncommitted write
    assert found.balance == 5
```

This pinning is why nested or deeply-called code "just works": any model or queryset call made while the block is open — directly or in a helper function it calls — runs on the active transaction without extra wiring.

## Choosing a connection

In a multi-database setup, pass the connection name to run the transaction somewhere other than the default. Both APIs accept it positionally:

```python
async with in_transaction("replica"):
    ...

@atomic("other_db")
async def sync() -> None:
    ...
```

The named connection must already be registered. See [Multiple databases](multiple-databases.md) for setting up additional connections and routing.

## Backend support

Transactions are backed by the database's native transaction support and behave the same whether the ORM is initialised against **PostgreSQL** or **SQLite** — the same `in_transaction` and `@atomic` code runs unchanged across both.

## See also

- [Manual SQL](manual-sql.md)
- [Multiple databases](multiple-databases.md)

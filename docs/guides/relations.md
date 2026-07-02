---
title: Relations
description: Model ForeignKey, OneToOne and ManyToMany relations and prefetch related rows in an async Python ORM, avoiding N+1 queries with batched eager loading.
---

# Relations

Relations connect your models to one another. `yara_orm` gives you a foreign key
for one-to-many links, a one-to-one for exclusive pairs, and a many-to-many
realised through a join table. Forward and reverse access is fully async, and
the `prefetch_related` helper batches related rows so you can traverse relations
in an async Python ORM without falling into the N+1 query trap.

All relation fields are declared with the `fields` module, and the `Prefetch`
helper is imported straight from the package:

```python
from yara_orm import Model, Prefetch, fields
```

The examples below use these canonical models:

```python
from yara_orm import Model, fields


class Author(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=120)


class Book(Model):
    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=200)
    author = fields.ForeignKeyField("Author", related_name="books")
    tags = fields.ManyToManyField("Tag", related_name="books")


class Tag(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50, unique=True)
```

## Foreign keys

```python
fields.ForeignKeyField(
    reference,
    related_name=None,
    on_delete=OnDelete.CASCADE,
    source_field=None,
)
```

- `reference` — the target model, given as a name (`"Author"`) or a dotted path
  (`"app.Author"`). A bare name must be unambiguous — if two registered models
  share it, resolution raises `ConfigurationError` and you must use the
  module-qualified form.
- `related_name` — the name of the reverse accessor installed on the target
  model (here, `Author.books`). Two relations claiming the same name on one
  target raise `ConfigurationError`. On an abstract base, use the `%(class)s`
  placeholder so each concrete subclass gets its own reverse name
  (`related_name="%(class)s_items"`).
- `on_delete` — the referential action applied when the referenced row is
  deleted (see [`OnDelete`](#on-delete-actions)).
- `source_field` — the target field that is referenced; defaults to the target's
  primary key.

Although you declare the field under the relation name (`author`), the metaclass
synthesises a concrete `<name>_id` backing column. For `Book.author` that is the
`author_id` column, which actually stores the foreign key value.

### Forward access

When the relation has **not** been loaded, accessing it returns an awaitable
descriptor that resolves to the related instance (or `None` when the foreign key
is unset):

```python
book = await Book.get(id=1)
author = await book.author          # -> Author instance, or None
```

Once the relation is **known** — because you assigned it, created the row with
it, or loaded it via `prefetch_related` — the attribute is the related instance
directly (or `None`), served synchronously without awaiting, matching Tortoise:

```python
books = await Book.all().prefetch_related("author")
books[0].author.name                # synchronous attribute access
if books[0].author:                 # truthiness reflects a NULL FK correctly
    ...
```

Assign a related instance directly when creating or updating a row. You may pass
the model instance itself:

```python
author = await Author.create(name="Ada")
book = await Book.create(title="Foundations", author=author)

book.author.name                    # "Ada" — cached on assignment, synchronous
```

Assigning an instance sets the `author_id` backing column to the instance's
primary key and caches the instance, so `book.author` returns it without a
query. Use `await book.author` only when the relation has not been cached
(e.g. on a freshly fetched row).

The cache always tracks the foreign key: assigning `None`, a raw primary key,
or writing `book.author_id` directly invalidates any previously cached
instance, so `book.author` never serves a stale object. Assigning an
**unsaved** instance (its pk is still `None`) raises `ValueError` — save the
related row first.

### Reverse manager

The `related_name` installs a manager on the target model. It is awaitable (to a
list), async-iterable, and chainable:

```python
author = await Author.get(id=1)

books = await author.books                 # -> list[Book]

async for book in author.books:            # async iteration
    print(book.title)

# Chainable queryset methods:
await author.books.all()                   # all related books
await author.books.filter(title="Foundations")
await author.books.order_by("-id")
await author.books.filter(rating__gte=5).count()

# Create a related row already bound to this author:
new_book = await author.books.create(title="Second Foundation")
```

The manager proxies the **full queryset API** scoped to the parent — `.all()`,
`.filter()`, `.exclude()`, `.order_by()`, `.limit()`, `.select_related()`,
`.values()`, `.annotate()`, `.count()`, and so on — each returning a queryset you
can chain further before awaiting. `.create(**kwargs)` sets the foreign key for
you.

```python
# Reverse FK: page and join in one chained expression
await author.books.limit(10).select_related("author")
await author.books.exclude(title__startswith="Draft").order_by("-id").values("title")
```

### On-delete actions

`OnDelete` enumerates the referential actions emitted in the DDL:

| Value | `ON DELETE` clause |
| --- | --- |
| `OnDelete.CASCADE` | `CASCADE` (the default) |
| `OnDelete.RESTRICT` | `RESTRICT` |
| `OnDelete.SET_NULL` | `SET NULL` |
| `OnDelete.SET_DEFAULT` | `SET DEFAULT` |
| `OnDelete.NO_ACTION` | `NO ACTION` |

```python
from yara_orm import fields
from yara_orm.fields import OnDelete


class Book(Model):
    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=200)
    author = fields.ForeignKeyField(
        "Author",
        related_name="books",
        on_delete=OnDelete.SET_NULL,
        null=True,
    )
```

## One-to-one

`OneToOneField(reference, ...)` is a foreign key that enforces uniqueness, so the
reverse side yields a single instance instead of a list. It accepts the same
arguments as `ForeignKeyField` and defaults `unique=True`.

```python
class Profile(Model):
    id = fields.IntField(pk=True)
    bio = fields.TextField()
    author = fields.OneToOneField("Author", related_name="profile")
```

The forward side awaits to one instance, and the reverse accessor
(`related_name`) also awaits to a single instance (or `None`):

```python
author = await Author.create(name="Ada")
await Profile.create(bio="Pioneer of computing", author=author)

profile = await author.profile          # -> single Profile, or None
back = await profile.author             # -> the Author
```

## Many-to-many

```python
fields.ManyToManyField(
    reference,
    related_name=None,
    through=None,
    forward_key=None,
    backward_key=None,
)
```

A many-to-many field adds **no column** to the owning table. Instead a
through/join table is auto-created to hold the pairings:

- `through` — the join table name; synthesised as `<owner>_<target>` when
  omitted.
- `forward_key` — the join-table column referencing the target model; defaults
  to `<target>_id`.
- `backward_key` — the join-table column referencing the owning model; defaults
  to `<owner>_id`.

The relation exposes a manager that is awaitable (to a list), async-iterable, and
mutable:

```python
book = await Book.create(title="Foundations", author=author)
sci = await Tag.create(name="sci-fi")
classic = await Tag.create(name="classic")

await book.tags.add(sci, classic)       # link rows in the join table
tags = await book.tags                   # -> list[Tag]

async for tag in book.tags:              # async iteration
    print(tag.name)

await book.tags.remove(classic)          # unlink specific rows
await book.tags.clear()                  # unlink everything

# Querying methods mirror the reverse manager — the full chainable queryset API:
await book.tags.all()
await book.tags.filter(name="sci-fi")
await book.tags.order_by("name")
await book.tags.limit(10).exclude(name="draft").values("name")
```

Both the reverse-FK and many-to-many managers proxy the queryset API, so
`.all()`, `.filter()`, `.exclude()`, `.order_by()`, `.limit()`,
`.select_related()`, `.values()` and `.annotate()` all chain off the related
manager the same way they do off a model queryset.

`add()` and `remove()` accept model instances or raw primary key values. `add()`
inserts join rows idempotently (`ON CONFLICT DO NOTHING`), and `clear()` removes
all pairings for this instance. The reverse side (`Tag.books`) works the same
way.

## Recursive (self-referential) relations

A foreign key can point at its own model to build a hierarchy. Pass the model's
own name as the reference and make the column nullable for root rows:

```python
class Employee(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=120)
    manager = fields.ForeignKeyField("Employee", related_name="reports", null=True)
```

The forward `manager` awaits to the parent (or `None` at the top), and the
reverse `reports` manager lists the direct children:

```python
boss = await Employee.create(name="Boss")
worker = await Employee.create(name="Worker", manager=boss)

assert (await worker.manager).id == boss.id
reports = await boss.reports            # -> list[Employee]
```

## Avoiding N+1 with prefetch

Traversing a relation per row issues one query per instance — the classic N+1
problem. `prefetch_related` solves it by loading every related row for a batch in
a single query per relation, populating each instance's prefetch cache so later
relation access returns without touching the database.

```python
authors = await Author.all().prefetch_related("books")
for author in authors:
    for book in await author.books:     # served from cache, no query
        print(author.name, book.title)
```

To prefetch onto a single instance you already have, use `fetch_related`, which
accepts one or more relation names:

```python
book = await Book.get(id=1)
await book.fetch_related("author", "tags")

author = book.author                    # cached forward FK -> synchronous
tags = await book.tags                  # cached m2m manager -> awaited
```

To prefetch relations across a **list** of already-loaded instances in one batch,
use the `Model.fetch_for_list(instances, *relations)` classmethod. It caches each
named relation on every instance (one query per relation) and returns the same
list:

```python
books = await Book.all()
await Book.fetch_for_list(books, "author", "tags")

for book in books:
    print(book.author.name)             # cached forward FK -> synchronous
    print(await book.tags)              # cached m2m manager -> awaited
```

Both work across forward foreign keys, one-to-one relations, reverse managers,
and many-to-many relations. After caching, a **forward FK / one-to-one** is the
instance itself (accessed synchronously); reverse managers and many-to-many
relations stay awaitable and serve their cached rows.

!!! tip "Reach for `prefetch_related` to kill N+1"
    Whenever you loop over a list of rows and touch a relation on each one,
    prefetch it. `Author.all().prefetch_related("books")` runs two queries total
    — one for the authors, one for every author's books — instead of one extra
    query per author. The related rows are cached on each instance, so awaiting
    the relation inside the loop is free.

### `select_related` for forward relations

For **forward** foreign keys and one-to-one relations, `select_related` loads the
related row in the *same* query with a `JOIN` (no second query at all), and caches
it so you access it synchronously:

```python
for book in await Book.all().select_related("author"):
    print(book.title, book.author.name)   # one query total; synchronous access
```

Each relation is joined under an alias of its own name, so self-referential joins
work too:

```python
employees = await Employee.select_related("manager").order_by("name")
for e in employees:
    print(e.name, e.manager.name if e.manager else "—")
```

!!! note "`select_related` vs `prefetch_related`"
    Use `select_related` for forward FK / one-to-one relations — it's a single
    joined query. Use `prefetch_related` for reverse managers and many-to-many
    relations, where a separate batched query per relation is the right shape.

!!! tip "Spanning multiple relations"
    Both accept `__`-separated paths to traverse more than one hop:
    `Book.all().select_related("author__country")` joins book→author→country in
    one query, and `Country.all().prefetch_related("authors__books")` batches each
    level. Intermediate hops load automatically.

### Customising a prefetch with `Prefetch`

For finer control, pass a `Prefetch(relation, queryset=...)` object to filter or
order the related rows that get loaded:

```python
from yara_orm import Prefetch

authors = await Author.all().prefetch_related(
    Prefetch("books", queryset=Book.filter(rating__gte=4))
)

for author in authors:
    top_books = await author.books      # only books with rating >= 4
```

The supplied queryset constrains the lookup, while the batching guarantee still
holds: one query loads the filtered related rows for the whole batch. This works
for every relation kind — forward FK / one-to-one included, where a related row
excluded by the queryset simply leaves the cached attribute as `None`.

Pass `to_attr` to store the result on a custom attribute instead of the relation
accessor — useful for loading the *same* relation more than once with different
filters:

```python
authors = await Author.all().prefetch_related(
    Prefetch("books", queryset=Book.filter(rating__gte=4), to_attr="top_books")
)
for author in authors:
    print(author.top_books)             # a plain list on the instance
```

## Typing your relations

Relation attributes can carry Tortoise-style typing annotations so IDEs and
type checkers know exactly what an access resolves to. The field factories
(`ForeignKeyField` / `OneToOneField` / `ManyToManyField`) are typed to return
the relation, so the declared annotation is the attribute's static type:

```python
from yara_orm import Model, fields


class Author(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)

    # Annotation-only: the accessors are installed by the other side's
    # related_name. Use a string forward reference for not-yet-defined models.
    books: fields.ReverseRelation["Book"]
    liked_books: fields.ManyToManyRelation["Book"]


class Book(Model):
    id = fields.IntField(pk=True)
    author: fields.ForeignKeyRelation[Author] = fields.ForeignKeyField(
        "Author", related_name="books"
    )
    editor: fields.ForeignKeyNullableRelation[Author] = fields.ForeignKeyField(
        "Author", null=True, related_name="edited_books"
    )
    fans: fields.ManyToManyRelation[Author] = fields.ManyToManyField(
        "Author", related_name="liked_books"
    )
```

With these in place a checker sees `book.author` as
`Author | ForwardRelation[Author]`, `await author.books` as `list[Book]`, and
`book.fans` as an `M2MManager[Author]`. The full alias family — importable
from `yara_orm.fields` or `yara_orm.relations`:

| Alias | Annotates | Access resolves to |
| --- | --- | --- |
| `ForeignKeyRelation[X]` | forward FK | `X` (prefetched) or awaitable of `X` |
| `ForeignKeyNullableRelation[X]` | nullable forward FK | as above, or `None` |
| `OneToOneRelation[X]` / `OneToOneNullableRelation[X]` | one-to-one | as the FK forms |
| `ReverseRelation[X]` | reverse FK accessor | manager awaitable to `list[X]` |
| `ManyToManyRelation[X]` | M2M accessor | manager awaitable to `list[X]`, plus `add`/`remove`/`clear` |

Annotations are optional — undeclared relations behave identically and simply
type as `Any`. For `isinstance` checks against the field objects themselves,
use the `ForeignKeyFieldInstance` / `OneToOneFieldInstance` /
`ManyToManyFieldInstance` classes (the factories return instances of these).

## See also

- [Querying](querying.md)
- [Models & fields](models-and-fields.md)

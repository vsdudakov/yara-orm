---
title: Querying
description: Query an async Python ORM with yara_orm — build lazy chainable querysets, filter with field lookups and Q objects, order and paginate on PostgreSQL or SQLite.
---

# Querying

`yara_orm` is an async Python ORM with a Rust engine and a Tortoise-style API. You read and write data through **querysets**: lazy, chainable builders that compose `WHERE`, `ORDER BY`, `LIMIT` and friends into SQL and only touch the database when you `await` them. This guide covers filters, field lookups, `Q` objects, ordering, pagination, the terminal methods, and the CRUD basics — all async, all from `yara_orm`.

The examples reuse the canonical `Author` / `Book` models (see [Models](models-and-fields.md)):

```python
from yara_orm import Model, fields

class Author(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=120, index=True)

class Book(Model):
    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=200)
    rating = fields.DecimalField(max_digits=3, decimal_places=1, default=0)
    author = fields.ForeignKeyField("Author", related_name="books")
```

## Lazy, chainable querysets

A `QuerySet` records filters (including `Q` trees), ordering, limits and offsets without running anything. No SQL is sent until you **await** the queryset or call a terminal coroutine such as `.get()`, `.count()`, `.delete()` or `.update()`.

```python
qs = Book.filter(rating__gte=4).order_by("-rating").limit(10)  # nothing runs yet
books = await qs                                               # now it executes -> list[Book]
```

Every chaining method returns a **new** queryset, so builders are safe to reuse and extend:

```python
top = Book.filter(rating__gte=4)
await top.count()                       # one query
await top.order_by("title").limit(5)    # a separate query, `top` is untouched
```

!!! tip "Awaiting a queryset returns a list"
    `await Book.filter(...)` resolves to `list[Book]`. There is no separate `.all()` you must call before awaiting — `Model.all()` is just a convenient empty queryset.

## Entry points

Start a queryset from the model class:

| Entry point | Meaning |
| --- | --- |
| `Model.all()` | Every row (an unfiltered queryset). |
| `Model.filter(*Q, **lookups)` | Rows matching the conditions. |
| `Model.exclude(*Q, **lookups)` | Rows **not** matching the conditions. |

```python
await Author.all()
await Book.filter(author=author, rating__gte=3)
await Book.exclude(title__startswith="Draft")
```

## Field lookups with `__`

Append a double-underscore suffix to a field name to choose how it is compared. Without a suffix the lookup is an exact match (`field=value` is the same as `field__exact=value`).

| Lookup | SQL | Example |
| --- | --- | --- |
| `exact` (default) | `=` | `Book.filter(title="Dune")` |
| `iexact` | case-insensitive `=` | `Author.filter(name__iexact="ada")` |
| `not` | `!= … OR IS NULL` | `Book.filter(rating__not=0)` |
| `gt` | `>` | `Book.filter(rating__gt=4)` |
| `gte` | `>=` | `Book.filter(rating__gte=4)` |
| `lt` | `<` | `Book.filter(rating__lt=2)` |
| `lte` | `<=` | `Book.filter(rating__lte=2)` |
| `in` | `IN (...)` | `Author.filter(name__in=["Ada", "Bob"])` |
| `not_in` | `NOT IN (...) OR IS NULL` | `Author.filter(name__not_in=["Ada"])` |
| `range` | `BETWEEN a AND b` | `Book.filter(rating__range=(3, 5))` |
| `isnull` | `IS NULL` / `IS NOT NULL` | `Author.filter(name__isnull=True)` |
| `not_isnull` | `IS NOT NULL` / `IS NULL` | `Author.filter(name__not_isnull=True)` |
| `date` | truncate datetime to date | `Book.filter(created__date=d)` |
| `contains` | `LIKE '%v%'` | `Book.filter(title__contains="sea")` |
| `icontains` | `ILIKE '%v%'` | `Book.filter(title__icontains="sea")` |
| `startswith` | `LIKE 'v%'` | `Book.filter(title__startswith="The")` |
| `istartswith` | `ILIKE 'v%'` | `Book.filter(title__istartswith="the")` |
| `endswith` | `LIKE '%v'` | `Book.filter(title__endswith="II")` |
| `iendswith` | `ILIKE '%v'` | `Book.filter(title__iendswith="ii")` |
| `year`/`quarter`/`month`/`week`/`day`/`hour`/`minute`/`second`/`microsecond` | extracted date part `=` | `Book.filter(published__year=2024)` |
| `regex` / `iregex` (aliases `posix_regex` / `iposix_regex`) | POSIX regex (PostgreSQL) | `Book.filter(title__regex="^The")` |
| `search` | full-text (PostgreSQL) | `Book.filter(title__search="ocean")` |

A few categories worth calling out:

```python
# Comparisons
await Book.filter(rating__gte=4, rating__lt=5)

# Membership and NULL checks
await Author.filter(name__in=["Ada", "Grace", "Linus"])
await Author.filter(name__isnull=False)

# Text matching
await Book.filter(title__startswith="The")
await Book.filter(title__icontains="ocean")
```

!!! note "`__not` / `__not_in` keep NULL rows"
    A negative filter also matches `NULL` rows (Tortoise semantics):
    `__not` compiles to `(col != v OR col IS NULL)` and `__not_in` to
    `(col NOT IN (...) OR col IS NULL)`. Plain SQL `!=` / `NOT IN` would drop
    `NULL`s (a `NULL != v` is unknown), so a nullable column's `NULL` rows are
    kept rather than silently vanishing from the result.

!!! tip "Values are coerced to the column type"
    Filter values are coerced through the field before binding — e.g. an integer
    column filtered with strings (`id__in={"1", "2"}`, a `str(ext_id)` from an
    external system) binds as `int`, and an ISO date string binds as a `date`.

!!! note "Case-insensitive lookups across dialects"
    The `i*` lookups (`icontains`, `istartswith`, `iendswith`) use `ILIKE` on PostgreSQL. On SQLite they fall back to `LIKE`, which is already case-insensitive for ASCII — so the behaviour is consistent: a case-insensitive match on either backend.

!!! note "Pattern lookups treat the value literally"
    The value you pass to `contains` / `startswith` / `endswith` (and their `i*`
    variants) is matched **literally**: `%`, `_` and `\` in the value are escaped
    before the `LIKE` pattern is built, so `title__contains="100%"` matches the
    three characters `100%`, not "100 followed by anything". `iexact` is likewise
    a true case-insensitive *equality* — wildcards in the value do not act as
    wildcards.

!!! note "`__contains` on a JSON column is containment"
    For a text column `__contains` is a `LIKE '%v%'` substring match (the table
    above). For a [`JSONField`](json-fields.md) it is structural containment
    (`@>`) instead — see [Working with JSON](json-fields.md#containment-__contains-postgresql).

!!! warning "PostgreSQL-only lookups"
    `regex`, `iregex` (and their `posix_regex`/`iposix_regex` aliases) and `search` are implemented for PostgreSQL only; on SQLite they raise `UnSupportedError`. The `microsecond` date part is likewise PostgreSQL-only.

## Spanning relations in filters

A lookup key can traverse relations with the `__` separator — a foreign key, a reverse FK or a many-to-many, to any depth:

```python
# Forward FK: books whose author's name matches
await Book.filter(author__name__icontains="ada")

# Multiple hops: book -> author -> country
await Book.filter(author__country__name="UK")

# Reverse FK: authors who have a 5-star book
await Author.filter(books__rating__gte=5)

# Many-to-many, either direction
await Book.filter(tags__name="python")
await Tag.filter(books__title="Dune")
```

Each relation hop compiles to a membership subquery, so spanning works at any depth (and across self-relations) without duplicating rows.

An `isnull` lookup on a **reverse relation or many-to-many** filters on the
*existence* of related rows rather than a column value — it compiles to a
membership test on the relation:

```python
# Authors with NO related books at all -> NOT EXISTS (...)
await Author.filter(books__isnull=True)

# Authors that have at least one related book -> EXISTS (...)
await Author.filter(books__isnull=False)

# Books with no tags at all (M2M)
await Book.filter(tags__isnull=True)
```

This complements the reverse field traversal above: `Author.filter(books__rating__gte=5)`
matches on a related column, while `books__isnull` matches on whether any related
row exists.

### Subquery as a filter value

Pass a lazy single-column projection wrapped in `Subquery` as the right-hand side
of a lookup to correlate against another query without materialising it:

```python
from yara_orm import Subquery

# Books whose author appears in a set computed by another query
await Book.filter(
    author_id__in=Subquery(
        Author.filter(name__startswith="A").values_list("id", flat=True)
    )
)

# The same inside exclude()
await Book.exclude(
    author_id__in=Subquery(
        Author.filter(name__startswith="A").values_list("id", flat=True)
    )
)

# A scalar subquery compared for equality
await Book.filter(author_id=Subquery(Author.filter(name="Ada").values_list("id", flat=True)))
```

A single-column `only()` projection works too — `Subquery(qs.only("email"))`
projects exactly the named column (the usual implicit primary key is not
added inside a subquery).

!!! note "`exclude(col__in=Subquery(...))` is NULL-safe"
    A single-column `values_list` subquery drops `NULL`s, so `exclude(col__in=Subquery(...))`
    excludes exactly the intended rows rather than collapsing to no matches on a
    `NOT IN` over a set containing `NULL`.

!!! warning "Pass the lazy queryset, not an awaited result"
    Wrap the queryset itself — `Subquery(qs.values_list(...))`. Passing an
    already-awaited (non-lazy) projection raises a guiding `TypeError`.

## Loading a subset of columns: `only()` / `defer()`

Fetch model instances carrying only some columns. The primary key is always loaded; reading a column that was not fetched raises `FieldError` (re-fetch without deferring it to read it).

```python
authors = await Author.all().only("name")        # SELECT id, name
authors = await Author.all().defer("bio")         # everything except bio
```

Both accept `__`-separated **related-field paths**, in which case they compose
with `select_related()` — the relation is joined and hydrated as a *partial*
related instance:

```python
# Join `author` and hydrate it with just its `name` column
await Book.all().only("title", "author__name")

# Load the related `author` with every column except `name`
await Book.all().defer("author__name")

# Nested paths span multiple hops
await Book.all().only("author__country__code")
```

Naming **only** related paths (no base column) restricts the base model to its
primary key, loading the joined relation partially. This is why `only()` /
`defer()` work with `select_related()`, but they **cannot** be combined with
`annotate()`. For plain dict/tuple projections that skip model construction
entirely, prefer [`values()` / `values_list()`](#projections-values-and-values_list).

## Inspecting a query: `sql()` / `explain()`

```python
print(Book.filter(rating__gte=4).sql())      # the SELECT statement
print(await Book.filter(rating__gte=4).explain())  # the database query plan
```

## `Q` objects for AND / OR / NOT

Keyword lookups passed to `filter()` are combined with `AND`. For richer boolean logic, build `Q` objects and combine them with `&` (AND), `|` (OR) and `~` (NOT), then pass them **positionally**:

```python
from yara_orm import Q

# rating is 1 or 3, AND the title is not "Gamma"
await Book.filter((Q(rating=1) | Q(rating=3)) & ~Q(title="Gamma")).order_by("title")
```

Positional `Q` args and keyword lookups can be mixed — they are ANDed together.
A positional argument that is not a `Q` object raises `TypeError` (it would
otherwise be silently ignored):

```python
# (rating >= 1) AND title IN (...)
await Book.filter(Q(rating__gte=1), title__in=["Alpha", "Beta"])
```

`exclude()` negates the whole condition, so `exclude(...)` is the same as `filter(~Q(...))`:

```python
await Book.exclude(title__startswith="Draft")
```

## Ordering

Pass field names to `.order_by()`. Prefix a name with `-` for descending order; later fields break ties:

```python
await Book.all().order_by("-rating", "title")   # highest rating first, then A→Z by title
```

Pass the special token `"?"` to order rows randomly (rendered as `RANDOM()`):

```python
await Book.all().order_by("?").limit(1)   # one random book
```

A model can declare a default ordering via [`Meta.ordering`](models-and-fields.md#default-ordering), applied to any query that does not call `.order_by()` itself; an explicit `.order_by()` always overrides it.

## Pagination

Use `.limit(n)` and `.offset(n)` to page through results (typically alongside `.order_by()` for stable paging):

```python
page_size = 20
page = 3
await Book.all().order_by("id").limit(page_size).offset(page * page_size)
```

Slicing is shorthand for `offset`/`limit`. A slice returns a queryset (still lazy); an integer index returns an awaitable for that single row:

```python
page = await Book.all().order_by("id")[40:60]   # offset 40, limit 20 -> queryset
third = await Book.all().order_by("id")[2]       # a single Book (IndexError if missing)
```

Slices and indexes **compose relative to the current window**: slicing an
already-sliced queryset narrows it further, exactly like slicing a list.
`qs[10:][3]` is absolute row 13, `qs[2:5][1:2]` is absolute row 3, and an
empty or inverted window (`qs[5:3]`) matches no rows.

Use `.distinct()` to drop duplicate rows from the result:

```python
await Book.all().distinct().values_list("rating", flat=True)
```

## Terminal methods

These run the query. All are coroutines (`await` them), except awaiting the queryset itself.

| Call | Returns | Notes |
| --- | --- | --- |
| `await qs` | `list[Model]` | Awaiting the queryset fetches all matching rows. |
| `await qs.get(**kwargs)` | `Model` | Exactly one row. Raises `DoesNotExist` if none, `MultipleObjectsReturned` if more than one. |
| `await qs.first()` | `Model \| None` | First row, or `None` when empty. |
| `await qs.last()` | `Model \| None` | Last row: the effective ordering (explicit `order_by()`, else `Meta.ordering`, else pk) reversed. Raises `TypeError` on a sliced queryset. |
| `await qs.earliest(*fields)` | `Model \| None` | First row ordered ascending by `fields` (pk by default). |
| `await qs.latest(*fields)` | `Model \| None` | First row ordered descending by `fields` (pk by default). |
| `await qs.count()` | `int` | Number of matching rows (`SELECT COUNT(*)`). Honours `group_by()` (counts groups), annotation filters and slices. |
| `await qs.exists()` | `bool` | `True` if at least one row matches (within the slice, if any). |
| `await qs.delete()` | `int` | Deletes matching rows, returns the count. Raises `TypeError` on a sliced queryset. |
| `await qs.update(**kwargs)` | `int` | Updates matching rows, returns the count. Raises `TypeError` on a sliced queryset. |

`delete()` and `update()` honour **annotation filters** — a condition on an
`annotate()` alias restricts the statement through a grouped subquery on the
primary key:

```python
# Delete only authors with more than five books
await Author.annotate(n=Count("books")).filter(n__gt=5).delete()
```

`.select_for_update()` adds `FOR UPDATE` to lock the selected rows for the duration of the surrounding transaction on PostgreSQL; it is a no-op on SQLite. It applies to plain fetches, `select_related()` joins and `values()` / `values_list()` projections alike; combining it with `annotate()` or `group_by()` raises `UnSupportedError` (PostgreSQL forbids locking an aggregated result).

```python
async with in_transaction():
    rows = await Book.filter(rating__lt=1).select_for_update()
    ...  # rows are locked until the transaction commits
```

```python
from yara_orm import DoesNotExist, MultipleObjectsReturned

ada = await Author.get(name="Ada")               # raises if 0 or >1 match
maybe = await Book.filter(rating__gte=5).first()  # Book | None

await Book.filter(rating__lt=1).count()
await Author.filter(name="Ghost").exists()

# Bulk write straight from the queryset (no instances loaded)
updated = await Author.filter(name="Linus").update(name="Linus T.")
removed = await Book.filter(rating=0).delete()
```

!!! warning "Bulk `update()` / `delete()` skip instance hooks"
    `QuerySet.update()` and `QuerySet.delete()` issue a single `UPDATE` / `DELETE` statement. They do not load model instances, so per-instance save/delete signals are not fired. Use instance methods when you need that behaviour.

## CRUD basics

Create, mutate and remove rows through the model and its instances:

```python
# Create and persist in one call
author = await Author.create(name="Grace")
book = await Book.create(title="On Computing", rating=4.5, author=author)

# Mutate then save; update_fields limits the UPDATE to specific columns
book.rating = 5
await book.save(update_fields=["rating"])

# Delete a single loaded instance
await book.delete()
```

!!! note "`update_fields` semantics"
    `save(update_fields=[...])` writes **only** the named columns of an existing
    row — handy for narrow updates on wide tables and to avoid clobbering columns
    other code may have changed. A relation name (e.g. `"author"`) maps to its
    foreign-key column, an `auto_now` timestamp is refreshed only if you name it,
    an empty list is a no-op, and an unknown name raises `FieldError`. It is
    ignored when the instance is being **inserted** (a new row needs every
    column).

Fetch-or-`None` and batched inserts:

```python
# Returns the matching instance or None (never raises)
existing = await Author.get_or_none(name="Grace")

# Insert many rows efficiently, in batches
books = [Book(title=f"Vol {i}", author=author) for i in range(1500)]
created = await Book.bulk_create(books, batch_size=500)
```

!!! tip "`get` vs `get_or_none`"
    Use `Model.get(**kwargs)` when a missing row is an error (it raises `DoesNotExist` / `MultipleObjectsReturned`). Use `Model.get_or_none(**kwargs)` when "not found" is a normal outcome you want to branch on.

### Get-or-create and bulk helpers

| Call | Returns | Meaning |
| --- | --- | --- |
| `await Model.get_or_create(defaults=None, **kwargs)` | `(instance, created)` | Fetch the row matching `kwargs`, or create it (merging `defaults`). |
| `await Model.update_or_create(defaults=None, **kwargs)` | `(instance, created)` | Update the match with `defaults`, or create it. |
| `await Model.in_bulk(ids, field_name="pk")` | `dict[key, Model]` | Fetch many rows, keyed by `field_name`. |
| `await Model.bulk_update(objects, fields, batch_size=500)` | `int` | Write the named `fields` of many instances in batched statements. |
| `await Model.bulk_get_or_create(records, key_fields, defaults=None, batch_size=500)` | `list[(instance, created)]` | The batched `get_or_create`: one fetch + one insert for the whole batch. |
| `await Model.bulk_update_or_create(records, key_fields, update_fields=None, batch_size=500)` | `list[(instance, created)]` | The batched `update_or_create`: one fetch + one update + one insert. |

```python
author, created = await Author.get_or_create(name="Ada", defaults={"rating": 5})
author, created = await Author.update_or_create(name="Ada", defaults={"rating": 4})

by_id = await Book.in_bulk([1, 2, 3])            # {1: <Book>, 2: <Book>, 3: <Book>}

for book in books:
    book.rating = 5
await Book.bulk_update(books, ["rating"])         # one UPDATE per batch
```

### Batched get-or-create / update-or-create

The per-row loop of `get_or_create` / `update_or_create` costs one round trip
per record. The bulk variants take **plain dicts** and resolve the whole batch
in a fixed number of queries — one `SELECT` matching existing rows by
`key_fields`, then one `bulk_create` for the misses (and, for
`bulk_update_or_create`, one `bulk_update` for the hits):

```python
results = await Author.bulk_get_or_create(
    [
        {"name": "Ada", "rating": 5},
        {"name": "Grace", "rating": 4},
        {"name": "Ada"},               # repeated key: same instance, created once
    ],
    key_fields=["name"],
    defaults={"active": True},          # applied only to newly created rows
)
for author, created in results:         # one (instance, created) per input record,
    ...                                 # in input order

results = await Stat.bulk_update_or_create(
    [{"key": "a", "hits": 10}, {"key": "b", "hits": 3}],
    key_fields=["key"],
    update_fields=["hits"],             # default: every non-key field present
)                                       # in any record
```

Semantics worth knowing:

- **`key_fields` is the natural key** (one or more columns) used to match
  existing rows; it must be non-empty. Both sides of the match are normalised
  through `to_db`, so loosely typed key values (`"42"` for an int column, a
  UUID string, an ISO date string) match the stored row instead of silently
  inserting a duplicate.
- **Duplicate keys within one batch collapse**: repeats of an existing key get
  the same fetched instance; repeats of a new key get the single instance
  created for it (later records of a duplicate key report `created=False` in
  `bulk_update_or_create`).
- `bulk_get_or_create` **never modifies existing rows** — `defaults` apply
  only to the rows it creates. Use `bulk_update_or_create` to overwrite
  `update_fields` on the matched rows.
- These ride on `bulk_create`/`bulk_update`, so the same caveats apply:
  **signals and field validators are bypassed**, and matching + insert are
  separate statements (not `ON CONFLICT`) — a row inserted concurrently
  between them can still raise `IntegrityError` under a unique constraint.
  For a single-statement upsert, use
  [`bulk_create(..., on_conflict=...)`](#upserts-with-bulk_create).

Instance helpers for in-place edits:

```python
book.update_from_dict({"title": "New", "rating": 4})   # set fields, no DB write
await book.save()

await book.refresh_from_db()                            # reload column values from the row
```

## Projections: `values()` and `values_list()`

When you only need a few columns, project them directly. Both methods skip model construction, so they are faster for pure reads.

`.values(*fields)` returns a list of dicts:

```python
rows = await Author.all().values("name", "rating")
# [{"name": "Ada", "rating": Decimal("4.5")}, ...]
```

`.values_list(*fields, flat=False)` returns a list of tuples — or a list of scalars when `flat=True` (which requires exactly one field):

```python
pairs = await Book.all().order_by("title").values_list("id", "title")
# [(1, "Alpha"), (2, "Beta"), ...]

titles = await Book.all().order_by("title").values_list("title", flat=True)
# ["Alpha", "Beta", ...]
```

Called with no field names, both default to every field on the model.

A field name may **traverse one or more forward relations** with `__`, selecting
a related-model column; the joins are chained automatically. `values()` also
accepts keyword aliases so the dict key is clean:

```python
await Book.all().values("title", "author__name")
# [{"title": "Dune", "author__name": "Herbert"}, ...]

# Multi-level: chain across several relations.
await Book.all().values("title", "author__publisher__country__name")
# [{"title": "Dune", "author__publisher__country__name": "US"}, ...]

await Book.all().values("title", author_name="author__name")
# [{"title": "Dune", "author_name": "Herbert"}, ...]

await Book.all().values_list("author__name", flat=True)   # ["Herbert", ...]
```

## Upserts with `bulk_create`

`bulk_create` accepts conflict-handling arguments that emit an `ON CONFLICT`
clause (PostgreSQL and SQLite):

```python
# Skip rows that collide with an existing unique value:
await Stat.bulk_create([Stat(key="a"), Stat(key="b")], ignore_conflicts=True)

# Upsert: update the named fields when the conflict target already exists.
await Stat.bulk_create(
    [Stat(key="a", hits=99)],
    update_fields=["hits"],
    on_conflict=["key"],          # a unique column; defaults to the pk
)
```

When conflict handling is requested, primary keys are **not** written back onto
the instances (the database may insert, skip or update each row).

## `F` expressions

`F` references a column instead of a Python value, so you can compare or update one column against another — or compute against a column — entirely in SQL (no read-modify-write round trip):

```python
from yara_orm import F

# Arithmetic update: bump every book's rating by 1, atomically
await Book.all().update(rating=F("rating") + 1)

# Assign one column from another
await Book.all().update(rating=F("rating"))

# Compare two columns in a filter
await Book.filter(rating__gt=F("rating"))
```

`F` supports `+`, `-`, `*` and `/`, with the column on either side (`F("a") + 1`, `10 - F("a")`).

Beyond bare `F` arithmetic, an `update()` value may be any SQL expression — a
scalar function, a `Case`, or an `Array` — so the new value is computed in the
database:

```python
from yara_orm import Case, When, Array
from yara_orm.functions import Coalesce

# Fill in a column only where it is currently NULL (function over F + a fallback)
await Book.all().update(rating=Coalesce(F("rating"), 0))

# Set a column conditionally per row
await Book.all().update(
    rating=Case(When(rating__gte=4, then=5), default=0)
)

# Bind a Python sequence to a PostgreSQL array column
await Book.all().update(labels=Array([1, 2, 3]))   # `labels` is a PG array column
```

## See also

- [Relations](relations.md)
- [Aggregation & grouping](aggregation.md)
- [Models](models-and-fields.md)

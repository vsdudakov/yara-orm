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
| `not` | `!=` | `Book.filter(rating__not=0)` |
| `gt` | `>` | `Book.filter(rating__gt=4)` |
| `gte` | `>=` | `Book.filter(rating__gte=4)` |
| `lt` | `<` | `Book.filter(rating__lt=2)` |
| `lte` | `<=` | `Book.filter(rating__lte=2)` |
| `in` | `IN (...)` | `Author.filter(name__in=["Ada", "Bob"])` |
| `isnull` | `IS NULL` / `IS NOT NULL` | `Author.filter(name__isnull=True)` |
| `contains` | `LIKE '%v%'` | `Book.filter(title__contains="sea")` |
| `icontains` | `ILIKE '%v%'` | `Book.filter(title__icontains="sea")` |
| `startswith` | `LIKE 'v%'` | `Book.filter(title__startswith="The")` |
| `istartswith` | `ILIKE 'v%'` | `Book.filter(title__istartswith="the")` |
| `endswith` | `LIKE '%v'` | `Book.filter(title__endswith="II")` |
| `iendswith` | `ILIKE '%v'` | `Book.filter(title__iendswith="ii")` |

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

!!! note "Case-insensitive lookups across dialects"
    The `i*` lookups (`icontains`, `istartswith`, `iendswith`) use `ILIKE` on PostgreSQL. On SQLite they fall back to `LIKE`, which is already case-insensitive for ASCII — so the behaviour is consistent: a case-insensitive match on either backend.

## `Q` objects for AND / OR / NOT

Keyword lookups passed to `filter()` are combined with `AND`. For richer boolean logic, build `Q` objects and combine them with `&` (AND), `|` (OR) and `~` (NOT), then pass them **positionally**:

```python
from yara_orm import Q

# rating is 1 or 3, AND the title is not "Gamma"
await Book.filter((Q(rating=1) | Q(rating=3)) & ~Q(title="Gamma")).order_by("title")
```

Positional `Q` args and keyword lookups can be mixed — they are ANDed together:

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

## Pagination

Use `.limit(n)` and `.offset(n)` to page through results (typically alongside `.order_by()` for stable paging):

```python
page_size = 20
page = 3
await Book.all().order_by("id").limit(page_size).offset(page * page_size)
```

## Terminal methods

These run the query. All are coroutines (`await` them), except awaiting the queryset itself.

| Call | Returns | Notes |
| --- | --- | --- |
| `await qs` | `list[Model]` | Awaiting the queryset fetches all matching rows. |
| `await qs.get(**kwargs)` | `Model` | Exactly one row. Raises `DoesNotExist` if none, `MultipleObjectsReturned` if more than one. |
| `await qs.first()` | `Model \| None` | First row, or `None` when empty. |
| `await qs.count()` | `int` | Number of matching rows (`SELECT COUNT(*)`). |
| `await qs.exists()` | `bool` | `True` if at least one row matches. |
| `await qs.delete()` | `int` | Deletes matching rows, returns the count. |
| `await qs.update(**kwargs)` | `int` | Updates matching rows, returns the count. |

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

## See also

- [Relations](relations.md)
- [Aggregation & grouping](aggregation.md)
- [Models](models-and-fields.md)

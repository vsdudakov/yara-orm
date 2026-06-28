---
title: Aggregation & grouping
description: Aggregate and group_by in an async Python ORM — use Count, Sum, Avg, Min, Max annotations to compute counts, totals and averages over rows and relations.
---

# Aggregation & grouping

Aggregation lets you compute values across many rows instead of fetching them one by one. With `yara_orm` you build aggregates such as `Count`, `Sum`, `Avg`, `Min` and `Max`, attach them to a query as **annotations**, and optionally **group by** one or more columns. Everything stays lazy and chainable until you `await` it, keeping aggregation idiomatic in an async Python ORM.

The models used throughout this guide:

```python
from yara_orm import Model, fields


class Author(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=120)


class Book(Model):
    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=200)
    rating = fields.DecimalField(max_digits=3, decimal_places=1, default=0)
    author = fields.ForeignKeyField("Author", related_name="books")
```

## Aggregate functions

The aggregate expressions are imported directly from `yara_orm`:

```python
from yara_orm import Count, Sum, Avg, Min, Max
```

| Aggregate | SQL    | Typical target                          |
| --------- | ------ | --------------------------------------- |
| `Count`   | COUNT  | a relation (reverse FK / M2M) or column |
| `Sum`     | SUM    | a numeric column                        |
| `Avg`     | AVG    | a numeric column                        |
| `Min`     | MIN    | any orderable column                    |
| `Max`     | MAX    | any orderable column                    |

Every aggregate shares the same constructor:

```python
Aggregate(field, distinct=False)
```

- `field` — the name of a column (e.g. `"rating"`) or a relation (e.g. `"books"`, a reverse foreign key). When the target is a relation, the query compiler adds the necessary `JOIN` for you.
- `distinct` — when `True`, the aggregate counts/aggregates distinct values only, compiling to `COUNT(DISTINCT ...)`.

```python
# Count the distinct books linked to each author
Count("books", distinct=True)
```

## Annotating a query

`.annotate(**annotations)` adds computed columns to a query. Each keyword becomes the output name; each value is an aggregate expression.

```python
# Count over a relation (reverse FK): how many books each author has
qs = Author.annotate(book_count=Count("books"))

# Aggregate over a column: the average rating of all books
Book.annotate(avg_rating=Avg("rating"))
```

You can attach several annotations at once:

```python
Book.annotate(
    avg=Avg("rating"),
    lo=Min("rating"),
    hi=Max("rating"),
)
```

### Reading annotated results

Awaiting an annotated queryset returns ordinary **model instances**, with each annotation set as an attribute named after its keyword:

```python
for author in await Author.annotate(book_count=Count("books")):
    print(author.name, author.book_count)
```

!!! tip "Projecting with `.values()`"
    When you only need the computed numbers (not full model instances), project with `.values(...)` to get plain dicts, or `.values_list(...)` for tuples:

    ```python
    rows = await Author.annotate(book_count=Count("books")).values("name", "book_count")
    # [{"name": "Ada", "book_count": 2}, {"name": "Bob", "book_count": 1}, ...]
    ```

## Grouping with `group_by`

`.group_by(*fields)` groups the result rows by the given columns. Combine it with an annotation and a projection to produce one aggregated row per group:

```python
rows = (
    await Book.annotate(total=Sum("rating"))
    .group_by("author_id")
    .values("author_id", "total")
)
# [{"author_id": 1, "total": Decimal("8.0")}, {"author_id": 2, "total": Decimal("4.0")}]
```

!!! note "Aggregating over the whole table"
    Calling `.group_by()` with no arguments collapses every row into a single group, which is handy for table-wide statistics:

    ```python
    [row] = (
        await Book.annotate(avg=Avg("rating"), lo=Min("rating"), hi=Max("rating"))
        .group_by()
        .values("avg", "lo", "hi")
    )
    ```

## Filtering annotations → HAVING

`yara_orm` decides between `WHERE` and `HAVING` by what you filter on:

- Filtering by a **normal field** adds a `WHERE` condition (applied before grouping).
- Filtering by an **annotation name** adds a `HAVING` condition (applied after the aggregate is computed).

```python
# HAVING COUNT(...) >= 1 — keep only authors that have at least one book
Author.annotate(books=Count("books")).filter(books__gte=1)
```

Because `books` is an annotation, the `books__gte=1` lookup compiles to `HAVING`. If you had filtered on a column such as `name__startswith="A"`, that would compile to `WHERE` instead. The two can be mixed freely in a single `.filter(...)` call.

## Putting it together

A realistic report: group authors, count their books, average the ratings, keep only authors with at least one book, and order by the busiest author first.

```python
from yara_orm import Count, Avg

authors = (
    await Author.annotate(
        book_count=Count("books"),
        avg_rating=Avg("books__rating"),
    )
    .filter(book_count__gte=1)        # HAVING COUNT(books) >= 1
    .order_by("-book_count")          # busiest authors first
)

for author in authors:
    print(author.name, author.book_count, author.avg_rating)
```

Here `Avg("books__rating")` reaches across the `books` relation to the related `rating` column using the `relation__column` path, `filter(book_count__gte=1)` becomes a `HAVING` clause, and `order_by("-book_count")` sorts by the annotation in descending order.

## See also

- [Querying](querying.md)
- [Relations](relations.md)

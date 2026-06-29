# Changelog

All notable changes to **yara-orm** are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Relation-spanning filters.** `filter()` / `exclude()` now traverse
  relations with the `__` syntax — `Book.filter(author__name__icontains="ad")`,
  multi-level `Book.filter(author__country__name="UK")`, reverse FKs
  (`Author.filter(books__rating__gte=5)`) and many-to-many in both directions.
  Compiled as correlated membership subqueries, so any depth and self-relations
  work without join-induced row duplication.
- **More field lookups:** `iexact`, `not_in`, `range`, the date/time parts
  `year`/`month`/`day`/`hour`/`minute`/`second`, and (PostgreSQL) `regex` /
  `iregex` / `search` full-text. The regex/search lookups raise
  `UnSupportedError` on SQLite.
- **`only()` / `defer()`** — fetch a subset of columns and return partially
  populated instances; reading a column that was not loaded raises `FieldError`.
- **`QuerySet.get_or_none()` / `get_or_create()` / `update_or_create()`** —
  previously only on the model class, now also chainable on a query set.
- **`QuerySet.select_for_update(nowait=, skip_locked=, of=)`** — row-lock
  modifiers (PostgreSQL; a no-op on SQLite).
- **`QuerySet.using_db(name)`** — run a query set on a named connection.
- **`QuerySet.sql()` / `QuerySet.explain()`** — inspect the compiled SQL and the
  database's query plan.
- **`Subquery`** — embed a query set as a nested `SELECT` in an annotation.
- **`save(update_fields=[...])` now performs a partial-column update.**
  Previously the argument was forwarded only to the save signals; it now
  restricts the `UPDATE` to the named columns of an existing row. Relation names
  map to their foreign-key column, an `auto_now` timestamp is bumped only if
  named, an empty list is a no-op, and an unknown name raises `FieldError`. The
  argument is ignored on insert (a new row needs every column).

### Changed

- **Cached single-instance `UPDATE`/`DELETE` SQL.** `save()` on an existing row
  and `delete()` now bind parameters against a statement compiled once per
  model/dialect (matching the existing `INSERT`/`SELECT` caching) instead of
  rebuilding the SQL string on every call.

## [1.0.0] - 2026-06-29

First stable release. yara-orm reaches effectively full Tortoise-style API
parity — models, querysets, relations, aggregation, signals, validators,
migrations and transactions — backed by the Rust engine, green on PostgreSQL
and SQLite with 100% test coverage.

### Added

- **Migrations — class-based, field-object system.** Each migration file is a
  `class Migration(m.Migration)` whose `operations` are built from live field
  objects (`CreateModel(fields={col: Field})`, `AddField`/`AlterField`, …).
  - Core ops: `CreateModel`, `DeleteModel`, `AddField`, `RemoveField`,
    `AlterField`, `AddIndex`, `RemoveIndex`, `RunSQL`, `RunPython`.
  - Idempotent analogs emitted by `makemigrations`
    (`CreateModelIfNotExists`, `AddFieldIfNotExists`, …) and automatic
    `AlterField` detection on column type/nullability changes.
  - Concurrent index ops (`AddIndexConcurrently`, `AddUniqueIndexConcurrently`,
    `RemoveIndexConcurrently`) for non-atomic migrations.
  - Rename ops (`RenameModel`, `RenameField`, `RenameIndex`).
  - Constraints: `UniqueConstraint` / `CheckConstraint` with `AddConstraint` /
    `RemoveConstraint` / `RenameConstraint` (PostgreSQL in place; SQLite raises
    a clear `UnSupportedError`).
- **Transactions — nesting and isolation.** Nested `in_transaction` / `@atomic`
  blocks open **savepoints** (inner rollback without aborting the outer
  transaction); `isolation=` accepts the four standard `IsolationLevel`s
  (PostgreSQL honours all, SQLite is serializable-only).
- **Eager loading** — `select_related` for forward FK / one-to-one relations,
  and synchronous serving of prefetched forward FK / O2O.
- **Query expressions** — `Case` / `When` and `RawSQL` annotations.
- **Fields & validation** — `validators=`, `TimeDeltaField`, `IntEnumField` /
  `CharEnumField`, and database-side default expressions.
- **Models & metadata** — `Meta.unique_together` / `Meta.indexes`,
  `Meta.abstract`, custom managers, timezone helpers, the `Signals` enum with
  lifecycle signals, and column/table comments.
- **Benchmarks** — a `delete` operation in the 4-way suite and a new
  yara-orm-only feature micro-benchmark (`bench_features.py`) covering
  savepoints, eager loading vs N+1, and projection.

### Changed

- Migration files moved from module-level `operations`/`dependencies` to the
  `class Migration` format; operations now carry field objects rather than
  plain spec dicts.
- Google-style docstrings enforced across the package; 100% branch coverage
  gated in CI.

### Fixed

- Exact `Decimal` binding (no float round-trip), typed `IntegrityError`, and
  timezone-aware datetime handling.
- Pony import in the benchmark suite (the Pony column had been silently
  dropped).

## [0.1.1] - 2026-06-29

### Added

- `Meta.ordering` for default queryset ordering.
- Configurable connection-pool size and per-connection statement-cache (via URL
  parameters).
- Expanded documentation.

### Changed

- CI/release wheel matrix housekeeping (dropped the Intel macOS runner; grouped
  GitHub Actions dependency bumps).

## [0.1.0] - 2026-06-28

Initial public release: an async Python ORM with a Rust (PyO3 + tokio) engine.

### Added

- Declarative models with a metaclass-driven schema, abstract field types and
  per-dialect SQL rendering for **PostgreSQL** and **SQLite**.
- Lazy `QuerySet` query builder: filtering, ordering, aggregation, `values` /
  `values_list` projections and bulk create/update/delete.
- Relations — foreign keys, one-to-one and many-to-many with reverse accessors
  and `prefetch_related`.
- Transactions (`in_transaction`, `@atomic`), manual SQL, multiple databases
  with a per-model router, and an operation-based migration CLI
  (`python -m yara_orm`).

[1.0.0]: https://github.com/vsdudakov/yara-orm/releases/tag/v1.0.0
[0.1.1]: https://github.com/vsdudakov/yara-orm/releases/tag/v0.1.1
[0.1.0]: https://github.com/vsdudakov/yara-orm/releases/tag/v0.1.0

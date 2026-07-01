# Changelog

All notable changes to **yara-orm** are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **Mixing a UUID param with an array param in one statement** no longer corrupts
  the binary encoding (`22P03`). Each param's known type is declared and
  array/JSON/NULL params get OID 0 (server-inferred) — instead of dropping every
  declaration when one param is an array, which made a `::uuid`-cast text param
  be re-inferred as `uuid` and mis-encoded. A string element inside a `::uuid[]`
  array is also parsed to uuid at bind.

### Changed

- **`JSONField` value coercion moved into the Rust engine** — the
  UUID/Decimal/datetime/bytes/set/enum → JSON conversion now happens in a single
  native pass at bind time (previously a Python `_json_safe` pre-walk ran on every
  JSON write). A value with no JSON form now raises `TypeError` at save time
  rather than a path-named `FieldError` from `to_db`.

### Performance

- **`DecimalField` reads skip a redundant `Decimal(str(...))` round-trip** on
  PostgreSQL (the engine already returns a native `Decimal`); SQLite still
  reconstructs from text.
- **`only()`/`defer()` partial reads reuse a cached decode plan** and a batch
  hydration path, instead of recomputing the per-field plan for every row.

## [1.7.0] - 2026-07-01

### Added

- **Filter across reverse FK relations by `related_name`** — a bare reverse
  relation with `__isnull` compiles to a correlated `[NOT] EXISTS`
  (`Portfolio.filter(alerts__isnull=True)` = "has no alerts"), and field
  traversal (`alerts__status="open"`) works.
- **JSON key-path lookups on `JSONField`** — `data__key`, nested `data__a__b`,
  and with operators (`data__key__contains=...`, `data__missing__isnull=True`).
  Rendered per dialect (PostgreSQL `->`/`->>`, SQLite `json_extract`).

### Changed

- **A bare `list`/`tuple` raw-SQL parameter now binds as a PostgreSQL array**
  (asyncpg-style), so `execute_query("... WHERE id = ANY($1)", [ids])` and
  `unnest($1::int[])` work with plain lists and coerce element types
  (UUID/Decimal/date/…). Previously a bare list bound as JSON in raw queries; to
  bind JSON in a raw query, pass a dict or a JSON string. The ORM path
  (`JSONField`) is unaffected — its lists still round-trip as JSON.

## [1.6.0] - 2026-07-01

### Added

- **Reverse-FK and M2M managers chain like a queryset** — `M2MManager` gains
  `.all()` / `.filter()` / `.order_by()` (and proxies `.limit()`,
  `.select_related()`, `.exclude()`, `.values()`, … ), and `RelatedManager`
  proxies the same queryset methods. So
  `await portfolio.subscribers.all().select_related("organisation")` and
  `await portfolio.companies.limit(10).select_related("company")` work.

### Fixed

- **Date/Datetime/Time fields keep the proper Python type on the instance** —
  `create(created_at="2026-07-01T…")` now leaves a `datetime` (not the raw
  string) on the object, so `obj.created_at.isoformat()` works after create.
- **`JSONField` coerces `bytes` (base64) and raises a clear, path-named error**
  for a leaf with no JSON form, instead of an opaque "value is not JSON
  serialisable" at bind time.
- **`exclude(col__in=Subquery(values_list))` is null-safe** — a single-column
  membership subquery filters NULLs of its column, so a nullable column with
  NULL rows no longer defeats the exclusion via the `NOT IN (… NULL …)` pitfall
  (and positive `IN` results are unchanged).

## [1.5.0] - 2026-07-01

### Added

- **`only()` / `defer()` accept related-field paths** — `only("contact__properties")`
  joins the relation and hydrates a *partial* related instance projecting just the
  named column(s); `defer("contact__properties")` loads the relation with every
  column but those. Nested paths (`contact__country__code`) work too. Naming only
  related paths restricts the base model to its primary key.
- **Scalar functions compose with `F` and nested functions** — `Lower(F("name"))`,
  `Coalesce(F("at"), now)`, `Coalesce(Lower("a"), "x")` and `Concat(Lower("a"), "b")`
  now resolve, and `Coalesce`'s fallback accepts an `F`/function as well as a literal.
- **`Index.get_sql(model, dialect=None, safe=True)`** renders an index's
  `CREATE INDEX` DDL for introspection (Tortoise parity); the dialect defaults to
  the model's connection dialect.

### Fixed

- **`DateField` / `DatetimeField` / `TimeField` coerce ISO-8601 string input** to
  `date`/`datetime`/`time` before binding, instead of binding text to the typed
  column (PostgreSQL `42804`). A trailing `Z` is accepted.
- **`Subquery()` accepts a single-column `values_list()` / `values()` projection** —
  `id__in=Subquery(qs.values_list("col", flat=True))` now renders as a membership
  subquery instead of raising.
- **`LIKE` / `ILIKE` lookups on non-text columns cast the column to text** — an
  `__icontains` / `__startswith` / … against a `uuid` or `JSONField` column emits
  `CAST(col AS TEXT) LIKE $1` instead of failing with `operator does not exist:
  uuid ~~* text` / `jsonb ~~ text` (`42883`).
- **`annotate(...).values()` / `.values_list()` keep the base model columns** — a
  pure-`annotate` projection with no explicit field list now returns the base
  fields alongside the annotation (grouped by pk), instead of only the annotation.

## [1.4.0] - 2026-06-30

### Removed

- **The `Tortoise` alias for `YaraOrm`** is removed; import `YaraOrm` directly.

### Added

- **`using_db=` keyword** on `create`, `save`, `delete`, `get`, `filter`,
  `exclude`, `get_or_create`, `update_or_create` and `refresh_from_db` to target a
  connection (alongside the existing chained `.using_db(...)`).
- **`refresh_from_db(fields=..., using_db=...)`** — reload a subset of fields on a
  chosen connection.
- **Function expressions as `update()` values** — e.g.
  `update(at=Coalesce("at", now))`.
- **`async for` over a queryset** — `async for obj in Model.filter(...)`.
- **`values()` / `values_list()` are awaitable, async-iterable and `.first()`-able** —
  `async for row in qs.values(...)` and `await qs.values(...).first()` now work.
- **Driver-qualified postgres URL schemes** (`psycopg://`, `asyncpg://`,
  `postgresql+asyncpg://`) are normalised to `postgres://`.
- **Positional access on raw `execute_query`/`fetch_all` rows** (`row[0]` and
  `row["col"]`), mirroring `asyncpg.Record`.
- **`Array(...)` binds a sequence as a PostgreSQL array** (a bare `list` still
  binds as JSON, so `JSONField` is unchanged) — e.g.
  `execute_query("... WHERE id = ANY($1)", [Array(ids)])`. Array columns read
  back as plain Python lists.
- **`only()`/`defer()` compose with `select_related()`** — the base row loads
  just the requested/non-deferred base columns while each selected relation
  still loads in full.

### Fixed

- **`JSONField` encoder returning a string** is parsed back to a native value
  instead of corrupting a `jsonb` column.
- **`update_from_dict` honours `Meta.extra_kwargs = "store"`**, keeping unknown
  keys instead of raising.
- **`values_list(flat=True)` on the annotated/grouped path** returns scalars (and
  the grouped `values_list` now respects the requested projection).
- **`Subquery()` handed a non-queryset** raises a clear `TypeError` instead of an
  opaque attribute error.
- **Bind parameters declare their PostgreSQL type from the Python value** (like asyncpg),
  so the server no longer mis-infers them from context: an uncast `execute_query("SELECT $1", [5])`
  returns the real type (not a crash), a `float` compared to an `int` column stays `float8`
  (`filter(int_col__lte=1.5)`), and the annotated/grouped paths bind filter params correctly.
- **`values()` on a grouped query returns only the requested fields** (consistent with
  `values_list()`), instead of also including the group-by columns.

## [1.3.0] - 2026-06-30

### More Tortoise-migration compatibility

A second sweep of fixes from migrating two large Tortoise codebases (callbear and
wiserfunding). See `MIGRATION_GAPS.md` for the full catalogue and originating evidence.

#### Fixed (correctness)

- **`BooleanField` coerces non-bool writes** with `bool(value)` (Tortoise
  semantics), so a truthy/falsy non-bool (e.g. `1`, `0`, `"yes"`) round-trips
  instead of reaching the engine as a type the boolean column rejects.
- **`JSONField` tolerates exotic Python values** — UUID, `Decimal`, `datetime`/
  `date`/`time`, `set`/`frozenset` and `Enum` are coerced to JSON-native forms
  before serialisation (matching a Tortoise + orjson `default=` setup) instead of
  raising "value is not JSON serialisable".
- **`Meta.extra_kwargs` is inherited** from a base `Meta`, so setting
  `extra_kwargs = "store"` once on a shared/abstract base applies to every
  subclass that declares its own `Meta`.

#### Added

- **Chainable `first()` / `QuerySetSingle` projections.** `QuerySet.first()`
  now returns a chainable single-row result (awaits to the instance or `None`),
  and both `first()` and `Model.get(...)` accept `.only(...)`, `.values(...)` and
  `.values_list(...)` — `await qs.first().values("a")` returns a single dict (or
  `None`), matching Tortoise's `QuerySetSingle`.
- **Model identity:** `__eq__`/`__hash__` compare by `(type, pk)`, so a refetched
  row equals one already held and `obj in [<same row>]` / set membership work.
- **Per-column index operator classes** — `Index(..., opclass="gin_trgm_ops")`
  (and `jsonb_path_ops`, etc.) render as `(<col> <opclass>)` on PostgreSQL
  (dropped on SQLite), through both `generate_schemas` and migrations.
- **`MetaInfo.db_table` setter** so `Model._meta.db_table = "..."` renames the
  table through the Tortoise alias.
- **`Aggregate` is a top-level export** (`from yara_orm import Aggregate`),
  matching Tortoise's `from tortoise.functions import Aggregate`.

## [1.2.0] - 2026-06-30

### Tortoise-migration compatibility

A sweep of compatibility fixes so existing Tortoise ORM projects migrate onto
yara-orm with far fewer shims (see `MIGRATION_GAPS.md` for the full catalogue and
the originating evidence).

#### Fixed (correctness)

- **`UUIDField(primary_key=True)` no longer inserts a NULL id.** The `uuid4`
  default is now applied for the Tortoise `primary_key=` spelling, not only `pk=`.
- **Foreign-key values coerce to the target primary key's type when bound.**
  `ForeignKeyField`/`OneToOneField` now convert a `str` (e.g. `str(instance.id)`)
  to the referenced pk type (e.g. `UUID`) instead of raising a binary-format
  error; non-string and int-pk values pass through unchanged.
- **`Meta.unique_together` is emitted by the migration autogenerator.**
  Previously honored only by `generate_schemas`, so migrations silently dropped
  the UNIQUE constraint; the two schema paths now agree and round-trip idempotently.
- **Foreign-key relations declared on an abstract base are inherited by concrete
  subclasses.** The backing `<name>_id` column was inherited but the relation
  accessor was lost, so `create(rel=...)` failed and `await obj.rel` broke.
- **`generate_schemas()` topologically sorts models by foreign-key dependency,**
  so a referencing table is created after its target regardless of input order.
- **Database errors on the manual-SQL path surface as `OperationalError`**
  instead of a bare `RuntimeError`, so `except OperationalError` handlers keep working.

#### Added

- **`JSONField(encoder=..., decoder=...)`** value-transform hooks (applied on
  write/read) for custom JSON handling such as JS-safe large integers.
- **Tortoise-compatible manual-SQL methods** on the connection (`connections.get()`
  / `in_transaction()` connection): `execute_query()` → `(rowcount, rows)`,
  `execute_query_dict()` → `list[dict]`, `fetch_one()`, and `execute_script()`
  (runs multi-statement scripts via a dollar-quote/string/comment-aware splitter).
- **`register_query_hook()` / `clear_query_hooks()`** — opt-in pre-execute query
  hooks (SQLCommenter/tracing/logging); zero overhead while none are registered.
- **`YaraOrm.init(config=...)`** accepts a Tortoise-style config dict, plus
  `YaraOrm.get_connection()` / `close_connections()` lifecycle aliases.
- **Chainable `Model.get(...)`** returns an awaitable `QuerySetSingle` supporting
  `.prefetch_related()` / `.select_related()`, while preserving the fast path for
  plain `await Model.get(...)`. **`QuerySet.all()`** no-op terminator added.
- **`QuerySet.get_parameterized_sql()`** returns `(sql, params)` for any query
  (including grouped/annotated `values()`), so callers no longer reach into private
  internals to wrap a query in `SELECT COUNT(*) FROM (...)`.
- **Filtered & conditional aggregates** — `Count("x", _filter=Q(...))` renders
  `... FILTER (WHERE ...)`, and aggregates accept an expression/`Case`
  (`Sum(Case(...))`). **`QuerySet.using_db()`** accepts a connection object as well
  as a name.
- **`order_by()` across a forward relation** — `order_by("author__name")` and
  multi-hop `order_by("author__country__name")` sort by the related column (via a
  correlated subquery); reverse/M2M paths raise.
- **`BaseDBAsyncClient`** is exported as a runtime-checkable executor `Protocol`
  for typing `using_db` / connection handles.
- **Custom index options on `Meta.indexes`** — `Index(unique=..., using=..., include=...)`
  renders `CREATE [UNIQUE] INDEX ... [USING <method>] (...) [INCLUDE (...)] [WHERE ...]`
  in both `generate_schemas` and migrations (idempotent); SQLite keeps `UNIQUE` /
  partial `WHERE` and omits `USING` / `INCLUDE`.
- **`application_name` / server settings via the connection URL** — documented and
  tested: `?application_name=svc&options=-c search_path=myschema` (libpq `options`),
  alongside the existing `max_size`/`min_size`/`statement_cache_size` pool params.
- **`Value` literal expression**, **`Q.AND` / `Q.OR`** connector constants, and
  **relation typing-hint placeholders** (`ForeignKeyRelation`, `ReverseRelation`,
  `ManyToManyRelation`, …) re-exposed on `yara_orm.fields`.
- **`Meta.extra_kwargs = "store"`** opt-in to keep unknown `__init__` kwargs as
  plain attributes (Tortoise behaviour); yara stays strict by default.
- **Model instances are awaitable** (`await instance` → the instance), and
  **`_meta` Tortoise aliases** (`db_table`, `fields_map`, `db_fields`,
  `fields_db_projection`), **`Field.has_db_field`**, bare **`fields.SET_NULL` /
  `fields.CASCADE` …** constants, subscriptable field/model classes, a
  `ManyToManyField(through_fields=...)` alias, accepted-and-ignored `blank` /
  `max_length` field kwargs, and the `_saved_in_db` alias for `_in_db`.

## [1.1.0] - 2026-06-30

### Performance

- **Decode/bind hot paths.** The `uuid.UUID` / `decimal.Decimal` type objects are
  cached once per interpreter (were re-imported per cell/bind); PostgreSQL result
  decoding dispatches on the type OID (jump table) instead of a ~16-deep type
  comparison chain; SQLite upper-cases each column's declared type once per
  result set rather than per cell and binds parameters by move instead of a
  double copy. ~6–7% higher SQLite `fetch_all`/`bulk_insert`/`filter` throughput.
- **`ManyToManyField.add(*objs)` issues a single multi-row `INSERT`** instead of
  one round-trip per object; the static join-table SQL for
  `add`/`remove`/`clear`/fetch is rendered once and reused.
- **Lighter row hydration and save path.** Rows are hydrated in a batch with a
  C-level bulk assign for non-decoded columns; `save()` skips full-field scans
  when a model has no `auto_now`/validated columns; signal dispatch and bare-name
  model resolution use a set/cache fast path.

### Fixed

- **SQLite foreign keys are now enforced.** `PRAGMA foreign_keys=ON` is applied
  to every pooled connection, so `ForeignKeyField(on_delete=...)` actions and
  referential checks actually run on SQLite (previously they were silently
  ignored). The WAL/synchronous PRAGMAs are likewise applied to every
  connection, not just the pre-warmed ones.
- **M2M operations honor the active transaction.** `obj.rel.add/remove/clear`,
  awaiting an M2M relation, and M2M `prefetch_related` now run on the active
  `in_transaction()` connection (and respect the model's router /
  `Meta.default_connection`) instead of a separate autocommit connection — so
  they are atomic, roll back with the block, and can read their own writes.
- **No silent integer corruption.** Binding an out-of-range value to a
  `SMALLINT`/`INTEGER` column on PostgreSQL now raises instead of wrapping to a
  wrong number; integers compared against a `NUMERIC`/`FLOAT` expression (e.g.
  `created__year=2024`) bind in the right type instead of returning no rows.
- **Result decode errors are no longer masked as NULL.** A failed decode of a
  known column type (e.g. a `NUMERIC` beyond the supported range) raises rather
  than silently returning `None`.
- **`order_by("?")`** for random ordering (renders `RANDOM()`).
- **Multi-level relation traversal in `values()` / `values_list()`** —
  `Book.values("author__publisher__country__name")` chains the joins (previously
  only a single relation hop worked).
- **`auto_now` / `auto_now_add` honor `use_tz`.** They now match manually-set
  datetimes (aware when `use_tz=True`, naive UTC otherwise) instead of always
  being aware UTC.
- **`RandomHex(size=...)` honors `size` on PostgreSQL** (the width matches the
  SQLite branch instead of always being a 32-char md5).
- **Transactions honor the connection name.** `in_transaction("name")` /
  `@atomic("name")` previously always ran on the default connection; they now
  open on the named connection.
- **Aggregate `distinct` is keyword-only.** `Sum("x", 0)` (a stray positional)
  raised no error and silently set `distinct`; it now raises `TypeError`. Use
  `Sum("x", distinct=True)`.

### Added

- **`makemigrations` detects column renames.** A renamed field with an unchanged
  type now generates a `RenameField` (preserving the data) instead of a
  destructive drop + add.
- **`Meta.indexes` and named `Meta.constraints` are diffed by migrations.**
  Adding or removing a composite index or a named `UniqueConstraint` /
  `CheckConstraint` generates the corresponding migration operation.
- **Partial (conditional) indexes** via the new `Index` declaration:
  `Meta.indexes = [Index(fields=["status"], condition="status = 'active'")]`
  renders `CREATE INDEX ... WHERE ...` on PostgreSQL and SQLite, and round-trips
  through migrations. Plain column groups (`("a", "b")`) still work alongside it.

- **Modern Tortoise field parameter names** as aliases: `primary_key` (`pk`),
  `db_index` (`index`), `source_field` (`db_column`), `db_default` (`default`),
  and FK/M2M `to` (`reference`).
- **`use_tz` / `timezone` arguments on `YaraOrm.init`** — actually wire the
  timezone config (previously only settable via a private helper).
- **`F` in `annotate()`** — project a column or arithmetic expression
  (`annotate(x=F("a") + 1)`).
- **`Subquery` / `RawSQL` as filter values** — `filter(pk=Subquery(...))`,
  `filter(pk__in=Subquery(...))`.
- **Multi-level `select_related` and `prefetch_related`** —
  `select_related("author__country")`, `prefetch_related("authors__books")`.
- **More lookups:** `not_isnull`, `posix_regex`/`iposix_regex` (aliases for
  `regex`/`iregex`), the `quarter`/`week`/`microsecond` date parts and the
  `date` truncation lookup.
- **Multi-sender signals** — `@post_save(ModelA, ModelB)`.
- **Per-model `DoesNotExist` / `MultipleObjectsReturned`** subclasses (still
  catchable via the global exceptions).
- **`Model.construct()`** (fast detached instance) and **`Model.fetch_for_list()`**
  (prefetch across a list).
- **`Meta` options recorded** (`schema`, `app`, `fetch_db_defaults`,
  `default_connection`) instead of silently dropped; `default_connection` also
  routes the model's statements to a named connection.

### Added (earlier this cycle)

- **`YaraOrm.get_schema_sql(safe=, models=)`** — return the schema DDL as a
  string without executing it (the read-only counterpart of
  `generate_schemas`), for previewing or dumping a schema.
- **`run_async(coro)`** — a lifecycle helper for scripts that runs a coroutine
  and guarantees `YaraOrm.close()` runs afterwards, even on error.
- **Documented connection-URL pool/cache parameters** — `max_size`, `min_size`
  and `statement_cache_size` (set `statement_cache_size=0` for PgBouncer
  transaction pooling). These were already honored by the engine; they are now
  documented and covered by tests.
- **`bulk_create` upsert.** New `ignore_conflicts`, `update_fields` and
  `on_conflict` arguments emit an `ON CONFLICT` clause (`DO NOTHING` or
  `DO UPDATE`) on PostgreSQL and SQLite. Primary keys are not written back when
  conflict handling is requested.
- **Relation traversal in `values()` / `values_list()`.** Select related-model
  columns with `__`, e.g. `Book.values("title", "author__name")`; `values()`
  also takes keyword aliases (`values(author_name="author__name")`).
- **`Prefetch(to_attr=...)`** — store a prefetched result on a custom instance
  attribute instead of the relation accessor.
- **Model-level query shortcuts.** `first()`, `last()`, `earliest()`,
  `latest()`, `exists()`, `distinct()`, `select_for_update()`, `values()` and
  `values_list()` are now classmethods on the model (previously query-set only),
  so `await Book.first()` works without `Book.all()`.
- **`Model.clone()`** — return an unsaved copy ready to insert as a new row
  (optionally with an explicit `pk`).
- **`Model.describe()`** — a structured description of the model's schema
  (table, primary key, fields, relations and `Meta` options).
- **`Meta.constraints`** — declare `UniqueConstraint` / `CheckConstraint` on the
  model; `generate_schemas()` emits them in the `CREATE TABLE`.
- **`ForeignKeyField(db_constraint=False)`** — keep the FK column without
  emitting a database `FOREIGN KEY` constraint.
- **`Random()`** function — `RANDOM()` for random ordering
  (`annotate(r=Random()).order_by("r")`).
- **`NumericValidator` and `CommaSeparatedIntegerListValidator`.**
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

[1.1.0]: https://github.com/vsdudakov/yara-orm/releases/tag/v1.1.0
[1.0.0]: https://github.com/vsdudakov/yara-orm/releases/tag/v1.0.0
[0.1.1]: https://github.com/vsdudakov/yara-orm/releases/tag/v0.1.1
[0.1.0]: https://github.com/vsdudakov/yara-orm/releases/tag/v0.1.0

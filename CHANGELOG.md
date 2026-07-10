# Changelog

All notable changes to **yara-orm** are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.14.4] - 2026-07-11

### Changed

- **`ManyToManyField(through_fields=...)` now reads Django's
  `(owner_column, target_column)` order.** Declarations written against the
  previous `(forward_key, backward_key)` reading must swap the two elements,
  or switch to the explicit `forward_key=` / `backward_key=` kwargs (whose
  meaning is unchanged).
- **Related-manager bulk writes are unscoped.** `parent.children.update(...)`
  / `.delete()` now reach every related row even when the model's custom
  `Meta.manager` (e.g. soft-delete) hides some from reads — lazy relation
  *reads* keep the manager scope, matching prefetch.

### Fixed

- **Oracle: zero-fraction datetime binds.** The driver's 11-byte TIMESTAMP
  bind with a zero fractional part compared unequal to the server's canonical
  7-byte storage, so `col >= :midnight` silently dropped exact-midnight rows;
  such values now bind as DATE (full second precision).
- **Bare `date` values on every datetime write path.** `DatetimeField.to_db`
  widens a bare `date` to a (tz-aware under `use_tz`) midnight datetime, so
  plain-attribute `save()`, `QuerySet.update()` and `bulk_update()` no longer
  store date-only text; the `__date` lookup narrows its comparison value to a
  date itself. Assignment/construction coercion (1.14.3-era fix) is unchanged.
- **m2m join-row cleanup is atomic.** On dialects whose join-table FKs do not
  cascade (SQL Server), `Model.delete()` and `QuerySet.delete()` run the
  join-row deletes and the row delete in one transaction — a failed row
  delete no longer silently strips the surviving rows' m2m links.
- **SQL Server `bulk_create`.** Generated ids return via
  `OUTPUT ... INTO` a table variable — works on tables with enabled triggers
  (a bare OUTPUT clause raises server error 334) — and are assigned in
  arrival order (sorting misassigned ids under a negative IDENTITY
  increment).
- **`bulk_get_or_create` / `bulk_update_or_create` accept mixed pks.** A
  create list mixing records with and without an explicit auto-increment id
  is partitioned and inserted in two batches instead of tripping
  `bulk_create`'s mixed-batch `ValueError`.
- **PostgreSQL `execute_many` type unification.** Differing numeric mixes
  now widen to NUMERIC (INT8→FLOAT8 silently rounded integers above 2^53);
  naive/aware datetime mixes unify to TIMESTAMPTZ and str/UUID mixes to UUID
  instead of rejecting the batch.
- Plus the fifteen correctness fixes from the full-codebase review that
  opened this release's PR: ZoneInfo-aware datetime binds, per-batch
  parameter-type unification, the sync-fast-path pool deadlock,
  `get_or_create`/`update_or_create` connection binding and root-filter
  inheritance, decode-plan snapshots on all read paths, order_by-referenced
  annotations in grouped projections, and lazy relation reads built through
  `Meta.manager`.

## [1.14.3] - 2026-07-08

### Fixed

- **Custom-`Prefetch` correctness and performance.** The custom-queryset M2M
  prefetch path decodes through-table link values the way hydration does, so
  pk types the driver returns in a non-native form (e.g. a `CHAR(36)` UUID pk
  on MySQL/MariaDB) group correctly instead of silently producing empty
  prefetched lists; the plain M2M join path decodes its owner-id column the
  same way. A target shared by several owners now deep-copies mutable column
  values (JSON dicts/lists), so mutating one owner's prefetched instance can
  no longer leak into another's. Per-owner `LIMIT`/`OFFSET` semantics now
  apply to **every** relation kind (reverse FK and forward FK, not just M2M),
  and the per-owner queries run concurrently outside a transaction instead of
  serially (no more N+1 latency for sliced prefetches).
- **Non-finite floats in JSON documents are rejected.** Binding
  `NaN`/`±Infinity` inside a `JSONField` value now raises a clear
  `ValueError` (like `json.dumps(..., allow_nan=False)` and PostgreSQL's
  `jsonb`) instead of silently storing the strings `"inf"`/`"NaN"` (1.14.2)
  or `null` (≤ 1.14.1), both of which changed the member's type under readers
  and filters with no decode path back.
- **SQL Server connection URLs.** An explicit `trust_cert=true` is honoured
  even with `encrypt=strict`, so "encryption required but trust the
  (self-signed) server cert" stays expressible; `encrypt=strict` alone still
  defaults to full certificate validation. Percent-decoding of URL
  credentials is no longer lossy: a `%`-sequence that does not decode to
  valid UTF-8 (a raw password merely containing `%`) passes through unchanged
  instead of being rewritten to U+FFFD.
- **Primary-key migrations.** Moving the pk between columns now emits the
  demoting `AlterField` before the promoting one, so `DROP PRIMARY KEY` runs
  before `ADD PRIMARY KEY` ("multiple primary keys" previously aborted the
  migration when the promoted column diffed first). On MySQL/MariaDB,
  demoting an `AUTO_INCREMENT` pk first restates the column via
  `MODIFY COLUMN` to strip the flag (MySQL errno 1075 rejects dropping the
  pk of a live auto column).
- **`bulk_create(update_fields=..., on_conflict=[])` on SQL Server.** An
  explicit empty conflict target now takes the same unique-column
  substitution as an omitted one, instead of rendering a MERGE that matches
  on the uninserted auto pk column and failing.

## [1.14.2] - 2026-07-07

### Fixed

- **Prefetch correctness.** A custom `Prefetch` queryset's filters/ordering are
  now honoured for M2M prefetch instead of loading every related row, and a
  `LIMIT`/`OFFSET`/slice on a custom M2M `Prefetch` applies **per owner**
  instead of once globally (owners outside the first window were previously
  starved). Reverse-M2M managers read the prefetch cache under the reverse
  accessor name (`related_name`), so reverse-M2M prefetch uses its cache
  instead of silently re-querying per instance (N+1).
- **Dialect / backend fixes.** Block-comment nesting when splitting
  multi-statement scripts is now PostgreSQL-only (other dialects end the
  comment at the first `*/`), guarded by a new `nests_block_comments` flag;
  MySQL/MariaDB escape backslashes in string literals so a backslash in a
  `COMMENT` can't splice the DDL; MSSQL percent-decodes URL credentials and
  honours `?encrypt=strict` for full TLS validation; SQLite rolls back on
  `COMMIT` failure so a dirty connection isn't recycled; PostgreSQL clamps
  `max_size` to `>= 1` so `?max_size=0` no longer panics on prewarm.
- **Query / field fixes.** A relation whose terminal field is named like a
  lookup operator (e.g. `slot__date`) traverses to that field instead of
  misparsing the token; `FloatField` coerces numeric-string input to `float`
  before binding; non-finite floats (`NaN`/`Infinity`) inside JSON parameters
  are preserved as their textual form across both JSON binders instead of
  raising or coercing to null.
- **Migrations / models.** Migration diffing detects primary-key changes and
  renders `ADD`/`DROP PRIMARY KEY` per dialect (SQLite rebuild; SQL Server
  rejects an unnameable drop); `bulk_create(update_fields=..., on_conflict=[])`
  falls back to the pk target; the compiled insert plan is snapshotted before
  awaiting so a concurrent cross-dialect save can't decode a row against the
  wrong plan.

### Changed

- **Hot-path optimizations.** The compiled statement plan is cached per dialect
  so a model that alternates dialects restores a prior plan in O(1) instead of
  recompiling; the queryset compile path skips the relation-target probe for
  plain scalar fields; the custom-M2M batched prefetch path drops a redundant
  per-row generator.
- **SQLite benchmarks use `sync_fast_path`.** The benchmark SQLite suite now
  runs with `?sync_fast_path=1` (the recommended config for an in-process
  database — statements run synchronously on the calling thread, removing the
  per-statement event-loop hop). With it, yara-orm is fastest on every SQLite
  operation except the sub-millisecond `group_by`; SQLite and MariaDB tables
  and charts are refreshed.

## [1.14.1] - 2026-07-06

### Fixed

- **`values()` / `values_list()` now decode column values to their field's
  Python type.** Previously these projections returned whatever the driver
  handed back, skipping the per-field read decoder that instance hydration
  applies — so on SQLite a `DecimalField` came back as `str` (and, on backends
  that don't express a type natively, a `UUIDField`/`DatetimeField` was
  likewise under-converted), while `obj.field` on a hydrated instance returned
  a `Decimal`. Arithmetic on a `values_list(...)` decimal therefore raised
  `TypeError: can't multiply sequence by non-int of type 'str'`. Each projected
  column (including values that traverse a relation, e.g. `ledger__balance`) is
  now decoded through the same dialect read decoder as instance attributes, so
  a `values()`/`values_list()` value has the same type as `obj.field`.

## [1.14.0] - 2026-07-06

### Added

- **MariaDB support — first-class backend.**
  `await YaraOrm.init("mariadb://user:pass@host:3306/db")` (or a plain
  `mysql://` URL pointing at a MariaDB server) now runs end to end against
  MariaDB 10.5+. The MySQL backend detects the server via `SELECT VERSION()` at
  connect and switches to a dedicated `MariaDbDialect` that uses MariaDB's
  `INSERT ... RETURNING` (primary-key and database-default backfill straight
  from the insert), its classic `ON DUPLICATE KEY UPDATE col = VALUES(col)`
  upsert (MariaDB has no MySQL 8 `AS new` row alias), PCRE `REGEXP` lookups
  (no `REGEXP_LIKE`), JSON decoding from `LONGTEXT`, and MariaDB's CHECK-
  constraint error code. `FOR UPDATE OF <table>` (unsupported by MariaDB) is
  dropped to a plain `FOR UPDATE`. MariaDB is part of the test matrix and CI
  alongside PostgreSQL, MySQL and SQLite.
- **Cross-ORM benchmark expanded to nine ORMs.** `benchmarks/bench.py` now
  compares yara-orm against Django, Peewee, SQLObject, Ormar and Piccolo in
  addition to Tortoise, SQLAlchemy and Pony, across PostgreSQL, MySQL, MariaDB
  and SQLite (`BENCH_BACKEND=mariadb`).
- **Oracle backend (beta).**
  `await YaraOrm.init("oracle://user:pass@host:1521/FREEPDB1")` connects to
  Oracle Database 23ai. Built on the **pure-Rust `oracle-rs`** driver (a native
  TNS implementation — no OCI / ODPI-C / Instant Client, so manylinux wheels
  stay self-contained) with a custom `deadpool` pool honouring
  `max_size`/`min_size`/`statement_cache_size` and `require_ssl=true` (rustls).
  Sessions are pinned to UTC; auto-increment pks use IDENTITY columns read back
  through a `RETURNING ... INTO` OUT bind; the type map is the
  `NUMBER`/`VARCHAR2`/`TIMESTAMP`/`CLOB`/`BLOB` family (`NUMBER(1)` booleans,
  `VARCHAR2(36)` uuids). Slicing uses `OFFSET ... FETCH NEXT`, case-insensitive
  lookups fold with `UPPER()`, regex uses `REGEXP_LIKE`, upserts and m2m links
  render `MERGE`, and `GROUP BY` lists every selected column (Oracle's strict
  rule). Constraint violations raise `IntegrityError` with the `ORA-` code, and
  result sets of any size are returned. The shared cross-backend suite runs on
  Oracle via `ORM_TEST_ORACLE`.

  yara-orm pins a fork of `oracle-rs` 0.1.7 that fixes three wire-protocol bugs
  (each proposed upstream): negotiate `END_OF_RESPONSE` + read multi-packet
  responses (`stiang/oracle-rs#14`), terminate long-form bind data with a
  zero-length chunk (`#15`), and frame marker packets with a 4-byte length in
  large-SDU mode (`#16`). It is **beta** for the remaining gaps (documented,
  their tests skipped): values above the server's max `VARCHAR2`/`RAW` size need
  `CLOB`/`LONG` binding (not yet implemented); custom per-transaction isolation
  levels are unavailable; `__search` and JSON `__contains` are unimplemented;
  and `bulk_create` inserts one row per statement. See `docs/backends` for the
  full list.
- **Microsoft SQL Server backend (beta).**
  `await YaraOrm.init("mssql://user:pass@host:1433/db")` (or a `sqlserver://`
  URL) connects to SQL Server 2017+ / Azure SQL. Built on the **pure-Rust
  `tiberius`** TDS driver — no ODBC / native client / Instant Client, so
  manylinux wheels stay self-contained — pooled through a custom `deadpool`
  manager honouring `max_size`/`min_size`/`statement_cache_size` and
  `require_ssl=true` (rustls). The `SqlServerDialect` renders T-SQL: `[bracket]`
  identifier quoting and `@PN` binds; the
  `NVARCHAR`/`BIGINT`/`DATETIME2`/`UNIQUEIDENTIFIER`/`BIT` type family (`BIT`
  booleans, native `GUID` uuids); `IDENTITY(1,1)` auto-increment read back with a
  batched `SELECT SCOPE_IDENTITY()` (T-SQL has no `RETURNING`); `OFFSET ... FETCH
  NEXT` paging; `MERGE` for upserts and m2m links; `CONCAT` concatenation;
  case-insensitive `LIKE` by default (a binary `COLLATE` folds the
  case-sensitive lookups). The shared cross-backend suite runs against a live
  **SQL Server 2022** in CI (`ORM_TEST_MSSQL`) with no backend-specific skips.
  It is **beta**: regex lookups (no `REGEXP` operator) and `SELECT ... FOR
  UPDATE` are unsupported, and altering a column's `DEFAULT` in a migration
  raises (defaults are auto-named constraints). All other migration operations
  render to T-SQL (`AddField`/`AlterField` run against a live server in the
  suite). See `docs/backends` for the full list.

### Changed

- Dialect gains a few capability hooks used by the Oracle backend and shared by
  all backends: `insert_returning_clause`, `limit_offset_sql`,
  `like_pattern_sql`, `render_upsert`, `supports_multirow_insert` and
  `group_by_functional_dependency`. `UUIDField.to_python` now reconstructs a
  `uuid.UUID` from text (backends that return uuid columns as strings).
- **Query failures now raise `OperationalError` uniformly.** A statement error
  (bad SQL, deadlock, serialization failure) raised a bare `RuntimeError` on the
  default hot path — only the transaction/hook path translated it — so the
  exception a caller saw depended on unrelated registration state. All query
  errors now route through the ORM hierarchy (`OperationalError`) at the engine
  source, so `except OperationalError` and retry-on-deadlock work everywhere.

### Fixed

- **`YaraOrm.close()` no longer leaks pools when one fails.** A failing
  `engine.close()` used to abort the loop, leaking every remaining pool and
  stranding the module globals half-reset. Closes now run concurrently, the
  globals always reset, and the first error is re-raised after cleanup.
- **`upgrade(target=...)` no longer overshoots.** Targeting an already-applied
  migration (or a middle one) applied the migrations *after* it, because the
  already-applied target was skipped before the stop condition fired. The loop
  now only considers migrations up to and including the target.
- **Oracle & SQL Server migration DDL for column changes.** `AddField`,
  `AlterField` and renames rendered PostgreSQL/ANSI SQL that Oracle and SQL
  Server reject (`ADD COLUMN`, `ALTER COLUMN ... TYPE`, ANSI `RENAME`). They now
  render each dialect's spelling — Oracle `ADD (...)`/`MODIFY`, T-SQL
  `ADD`/`ALTER COLUMN`/`sp_rename`/`DROP INDEX ... ON`. Create-table foreign
  keys route through the per-dialect `ON DELETE` rules too (Oracle drops an
  unsupported `RESTRICT`). `AddField`/`AlterField` now run against live Oracle
  and SQL Server in the cross-backend suite.
- **Oracle row decoding is panic-safe.** A malformed row from the young
  `oracle-rs` driver (more values than declared columns) indexed out of bounds
  and would abort the process; values are now zipped with their columns.
- **SQL Server rolls back a stale transaction on connection reuse.** A pooled
  connection returned after a failed commit/rollback could carry an open
  transaction into the next checkout; recycling now issues
  `IF @@TRANCOUNT > 0 ROLLBACK` first. Connect-error messages on the Oracle and
  SQL Server backends are password-redacted, matching PostgreSQL/MySQL.
- **`values()/values_list().first()` fetches a single row.** It materialised the
  whole result set and discarded all but the first; it now applies `LIMIT 1`.
- **`INSERT ... RETURNING` backfill decodes through the dialect read decoder.**
  Values read back from a RETURNING insert now pass through the same decoder as
  a SELECT, so a database-generated UUID primary key stored in a `CHAR(36)`/text
  column hydrates as `uuid.UUID` rather than a raw string. Surfaced by MariaDB's
  RETURNING support; applies to any backend whose RETURNING returns a
  non-native column type.
- **Abstract base no longer hijacks a subclass's primary key.** An abstract base
  that declared no pk had an `id` serial auto-injected, which a concrete subclass
  inherited — so a subclass naming its pk differently (`ref = UUIDField(pk=True)`)
  had that pk silently demoted to a plain column plus a spurious `id`. A pk the
  class declares itself now supersedes and drops the inherited auto-`id`.
- **`bulk_update` on a relation field is correct and safe.** It read the relation
  accessor (an unresolved `ForwardRelation` awaitable when only `<name>_id` was
  set) and bound it verbatim; it now reads the FK backing column. A target column
  that was never loaded (`only()`/`defer()`) now raises `FieldError` instead of
  silently writing `NULL` — an FK source column has no descriptor, so the old
  `getattr` default swallowed the miss and wiped the foreign key.
- **`__not_in` against a `Subquery` keeps NULL rows**, matching the literal-list
  form (`NULL NOT IN (...)` is UNKNOWN, so a bare `NOT IN` dropped a nullable
  column's NULL rows).
- **SQL Server `LIKE` escapes `[`.** `[` is a T-SQL character-class metacharacter,
  so a `__contains`/`__startswith` value such as `a[bc]` silently broadened the
  match; it is now escaped (other backends are unaffected).
- **SQL Server resets the session isolation level on connection reuse.**
  `SET TRANSACTION ISOLATION LEVEL` is session-persistent in T-SQL, so a
  `SERIALIZABLE` transaction leaked its level onto later checkouts of the pooled
  connection; recycle now resets to `READ COMMITTED`.
- **MySQL no longer fabricates a stale synthetic primary key.** A fetched
  UPDATE/DELETE could return a bogus `[id]` row from a connection-level
  `last_insert_id` left by an earlier INSERT; only the statement's own id is used.
- **Migration `applied_at` is stored as naive UTC**, matching its (tz-naive)
  tracking column, instead of an aware datetime that some backends shift or reject.
- **Cleaner binding errors from the engine.** A NaN/±Inf (or out-of-`Decimal`-range)
  float bound to a `NUMERIC` column is rejected instead of misencoding float bytes;
  a JSON integer above `i64::MAX` decodes exactly (through `u64`); and Oracle
  surfaces a decode error for an unhandled driver value shape rather than storing
  its debug string.

### Performance

- **Faster model construction** — `Model.__init__` routes only the fields whose
  `to_python_value` is overridden through the coercion call (≈1.18× faster
  construction on the `create`/`bulk_create` path). Raw dict queries intern each
  result set's column names once instead of once per row.
- **Benchmark suite refreshed** — the cross-ORM numbers, tables and charts were
  re-measured against the current engine (PostgreSQL 18 / MySQL 8 / MariaDB 11 /
  SQLite, Apple Silicon, N=5000, median of 5).

## [1.13.1] - 2026-07-04

### Performance

- **PostgreSQL: TLS connector is built once per process, not per `init()`.**
  tokio-postgres defaults to `sslmode=prefer`, so every connection took the TLS
  path — and each `YaraOrm.init()` rebuilt the rustls connector, re-parsing the
  entire OS trust store (`rustls_native_certs::load_native_certs`, ~100–150 CA
  certs, tens of milliseconds) every time. The connector (an `Arc<ClientConfig>`)
  is now cached in a `OnceLock` and cloned per connection, so the trust store is
  parsed once. Repeated `init()` on the TLS path now matches the plaintext
  baseline (measured: first init ~157ms → subsequent ~33ms, same as
  `sslmode=disable`).


## [1.13.0] - 2026-07-03

### Added

- **MySQL backend.**
  `await YaraOrm.init("mysql://user:pass@host:3306/db")` now works end to end
  against MySQL 8.x (the driver also speaks MariaDB; `mysql+aiomysql://`-style
  scheme aliases are normalised). Built on the pure-Rust `mysql_async` driver
  and its own connection pool — `max_size`/`min_size`/`statement_cache_size`
  URL parameters are honoured like on the other backends, and every session is
  pinned to UTC with `ANSI_QUOTES` enabled so portable double-quoted raw SQL
  runs unchanged. The pool retains idle connections up to `max_size` (an
  explicit `min_size` lowers the retained count) and skips the driver's
  per-check-in session reset, so pooled statements never pay reconnect or
  reset round trips — ~7x faster point queries than the driver defaults,
  putting yara-orm ahead of Tortoise/SQLAlchemy/Pony on every benchmark
  operation on MySQL as well (`make bench-mysql`; `bench.py` and
  `bench_features.py` now take `BENCH_BACKEND=mysql`).
  - **No `RETURNING` needed:** inserts compile without it on MySQL; the new
    auto-increment pk is read from the driver-reported last-insert id (single
    inserts and `bulk_create`, which backfills a batch arithmetically from its
    first id under the default consecutive `innodb_autoinc_lock_mode`).
    `Meta.fetch_db_defaults` is honoured with a follow-up `SELECT` by pk.
  - **Dialect:** backtick quoting, `?` placeholders, `DATETIME(6)`/`TIME(6)`,
    `TINYINT(1)` booleans, `CHAR(36)` uuids (reconstructed to `uuid.UUID` on
    read), `JSON`, `LONGTEXT`/`LONGBLOB`, table-level `FOREIGN KEY` clauses,
    indexes folded into `CREATE TABLE` (MySQL has no
    `CREATE INDEX IF NOT EXISTS`), `INSERT IGNORE` and the 8.4-safe
    `INSERT ... AS new ON DUPLICATE KEY UPDATE` upsert forms.
  - **Case semantics:** `icontains`/`istartswith`/`iexact` use MySQL's
    collation-insensitive `LIKE`; case-sensitive pattern lookups use
    `LIKE BINARY`; pattern escaping works despite MySQL's backslash-escaped
    string literals.
  - **Aware datetimes** are stored as their UTC instant in the naive
    `DATETIME(6)` column and decode naive (aware UTC under `use_tz=True`).
  - Cross-backend parity fixes that came with it (all backends benefit):
    `Concat`/`Random()` and aggregate `_filter=Q(...)` now render per dialect,
    `count()`/`exists()` on wrapped shapes no longer drag eager-load columns
    into the derived table, JSON path lookups quote their key legs on MySQL
    (non-ASCII keys work), `update()`/`delete()` with annotation filters work
    around MySQL's self-referencing subquery restriction, and
    `select_for_update()` is now driven by a dialect capability
    (PostgreSQL + MySQL emit it, SQLite stays a no-op).
  - **Migrations** work on MySQL: the manager's bookkeeping, `upgrade`/
    `downgrade` and the operation DDL all render per dialect (`DropIndex` now
    passes the owning table through, since MySQL's `DROP INDEX` needs it).
  - **Regex and full-text lookups:** `__regex`/`__iregex` render
    `REGEXP_LIKE(col, ?, 'c'|'i')` (MySQL 8's ICU engine rejects
    `REGEXP BINARY`); `__search` renders `MATCH ... AGAINST` — declare the
    required FULLTEXT index as `Index(fields=[...], using="fulltext")`
    (rendered inline as `FULLTEXT INDEX` in the CREATE TABLE).
  - **TLS** via the driver's rustls stack (ring provider — wheels stay free of
    system OpenSSL): opt in with `mysql://...?require_ssl=true`
    (plus the driver's `verify_ca`/`verify_identity`/`built_in_roots` params).
  - The test matrix default is now `sqlite,postgres,mysql`
    (`ORM_TEST_BACKENDS` still overrides; each server-backed leg skips itself
    when its server is unreachable — `ORM_TEST_DB` / `ORM_TEST_MYSQL`,
    defaulting to `postgres://localhost/orm_demo` and
    `mysql://root:root@localhost:3306/orm_demo`), and CI runs a MySQL 8
    service alongside PostgreSQL.
  - Remaining MySQL-specific gaps: JSON-column indexes (need generated
    columns; JSON `Index` declarations are dropped like other pg-only index
    options) and PostgreSQL array parameters (stored as JSON text, matching
    SQLite).

## [1.12.0] - 2026-07-03

### Added

- **Opt-in SQLite synchronous fast path** — `sqlite:///app.db?sync_fast_path=1`.
  Statement calls (queries, transaction statements, commit/rollback,
  savepoints, `execute_many`) run the SQLite work synchronously on the calling
  thread with the GIL released and return an already-completed awaitable, so
  `await` resumes immediately instead of round-tripping the event loop —
  ~7× faster warm point queries (~6µs vs ~40µs) and 2.7–14× on
  per-statement benchmark ops. Code changes: none — everything is still
  `await`ed; errors still raise at the `await`. `sync_fast_path=0`/`off` keep
  the default async bridge; other values raise `ValueError`, and the flag is
  rejected on postgres URLs. **Caveats (deliberate trade-offs):** the event
  loop is blocked for the duration of each statement (opt in only for
  microsecond-statement workloads — tests, scripts, benchmarks,
  low-contention apps; a long scan or a write parked on the 5s busy timeout
  stalls *all* tasks), and awaiting a completed awaitable may not yield to
  the event loop, changing task interleaving (don't rely on `await` as a
  scheduling point). `begin()` and `execute_script` always stay async
  (`BEGIN IMMEDIATE` can park on the busy timeout; scripts can run long).

- **Fully-typed model attributes and generic querysets.** Scalar fields are
  now generic over their Python value type with `null=True` folding `None`
  in, so `call.to_number` reveals `str`, `call.duration` (from
  `IntField(null=True)`) reveals `int | None`, enum fields reveal their enum
  class, class-level access reveals the field object (`Call.to_number` →
  `CharField[str]`), and assigning a wrongly-typed value is a type error. No
  annotations needed — the declaration is the source of truth
  (`JSONField` stays `Any`; annotate `data: fields.JSONField[...] = ...` to
  narrow it). `QuerySet` is generic over its model and `Model` query entry
  points return `Self`-parameterised types, so `Call.filter(...)` is a
  `QuerySet[Call]`, `await Call.filter(...)` is `list[Call]`,
  `await Call.filter(...).first()` is `Call | None`, and
  `get`/`create`/`get_or_create`/`bulk_*` resolve to the concrete model.
  Relation managers' `all()`/`filter()`/`order_by()` now return
  `QuerySet[Target]`. All of this is type-checking only: fields remain
  runtime non-data descriptors (typed `__set__` and `__get__` overloads live
  under `if TYPE_CHECKING:`), so attribute access and row hydration are
  byte-for-byte the runtime paths they were. One observable change:
  subscripting a field class (`JSONField[dict]`) now returns a
  `types.GenericAlias` (real `Generic` machinery) instead of the class
  itself; annotations keep evaluating fine.

- **`yara_orm.contrib.factory` — official factory_boy integration.**
  `YaraModelFactory` keeps factory_boy's full declaration surface and makes
  persistence async: `await MyFactory.create(**overrides)` and
  `await MyFactory.create_batch(n)` return persisted instances (`SubFactory`
  chains of other `YaraModelFactory` classes are awaited depth-first; batch
  creations run sequentially), `build()` stays synchronous for unsaved
  instances, and `@factory.post_generation` hooks run after the row is
  persisted — a hook returning an awaitable (e.g. `obj.tags.add(*extracted)`)
  is awaited for you. factory-boy is an optional dependency: install with
  `pip install "yara-orm[factory]"`. See the new
  [Testing with factories](https://vsdudakov.github.io/yara-orm/guides/testing-factories/)
  guide.

- **`select_related()`, `only()`/`defer()` and `annotate()` now combine on one
  queryset** (previously the combination raised `FieldError`, forcing a
  fallback to `prefetch_related` and full-row projections). A single SELECT
  carries the join plan's columns, the narrowed base projection and the
  annotation expressions: non-aggregate annotations (window `RawSQL`,
  `F` arithmetic) add no `GROUP BY`, while aggregate annotations (or a
  `HAVING` filter) group by the base pk plus each joined relation's pk, so
  reverse-relation aggregates don't inflate row counts. Related instances
  hydrate as before, annotation values are set as attributes, and unselected
  columns stay deferred. Annotation names may not shadow a model field when
  `only()`/`defer()` is active (`FieldError`), and `select_for_update()`
  still raises `UnSupportedError` on every annotated shape.
- **Public custom-field-kind registry.** `register_field_kind(kind, *,
  field_cls, sql, source=None, requires_extension=None)` (and
  `unregister_field_kind(kind)`) replaces the three private monkey-patches
  downstream apps needed to teach yara-orm a custom column type (e.g. a
  pgvector `VectorField`). One call wires the kind into the dialects (`sql`
  type template, single or per-dialect, filled from the field's
  `type_params`), the migration writer (fields serialise as
  `fields.<ClassName>(...)` or via a custom `source` callable, and
  `fields.<ClassName>` resolves so generated files import cleanly) and the
  autodetector (a `type_params` change diffs to `AlterField`). Registration
  is validated (no shadowing built-in kinds, `field_cls` must be a `Field`
  subclass with the matching `field_kind`, template placeholders must match
  `type_params` at render time) and idempotent (re-registering the same
  class is a no-op). See the new [Custom fields](docs/guides/custom-fields.md)
  guide.
- **Declarative required PostgreSQL extensions.** A kind registered with
  `requires_extension="vector"` makes the extension part of the schema:
  `BaseDialect.extensions_sql(models)` returns the deduped, sorted
  `CREATE EXTENSION IF NOT EXISTS` statements (empty on SQLite) for
  `generate_schemas`, and `makemigrations` prepends the new
  `m.CreateExtension(name)` operation — rendered per dialect, so it applies
  the guarded `CREATE EXTENSION` on PostgreSQL and is a clean no-op on
  SQLite — first in any migration that creates or retypes such a column.
  Its reverse is empty (extensions are never dropped automatically).
- **Query annotators for SQL attribution.** `register_query_annotator(fn)`
  (decorator-friendly) and `clear_query_annotators()`: each annotator is a
  zero-arg callable returning a short string (or `None`/`""` to skip); the
  non-empty results join with `,` in registration order into a single
  `/* ... */` comment prepended to every statement on the Python query path
  (model queries, manual SQL, transactions, `execute_script`), so
  `pg_stat_statements` / APM tools can attribute queries per request. Values
  are sanitised (control characters and comment delimiters stripped) so they
  cannot break out of the comment; query hooks observe the final SQL,
  comment included; zero overhead while no annotator is registered. Note:
  the PostgreSQL statement cache is keyed on SQL text — prefer
  low-cardinality values or `statement_cache_size=0`.
- `YaraOrm.generate_schemas` now executes a dialect's
  `extensions_sql(models)` statements (e.g. `CREATE EXTENSION IF NOT
  EXISTS`) first, per write connection, before creating tables.

### Performance

- **`bulk_create` stamps `auto_now`/`auto_now_add` columns once per call**
  (one `timezone.now()` shared by the whole batch, written via direct
  `__dict__` assignment) and resolves each column's binder/attribute pair
  once per statement instead of twice per column per row. Semantic note: all
  rows inserted by one `bulk_create` call now share an *identical* timestamp
  (previously each row got its own `now()`, microseconds apart); single
  `save()` keeps its per-call timestamp. ~28% faster on a 5000-row batch.
- **`Model.__setattr__` is no longer overridden on every model.** The
  override existed only to un-mark never-fetched database-default columns on
  explicit assignment; it is now installed by the metaclass only on model
  classes that declare (or inherit) a `DatabaseDefault` column, and
  `Model.__init__` writes field values into `__dict__` directly. Plain
  models keep `object.__setattr__`, cutting `Model(...)` construction time
  roughly in half; db-default semantics (explicit `None` persists) are
  unchanged.
- **`Model.get`/`get_or_none` fast path got faster**: the chainable
  `QuerySetSingle` wrapper now builds its fallback queryset lazily (only
  when the caller actually chains `.select_related()` etc. or the fast path
  bails out), and the simple-equality SELECT (including its `IS NULL` shape
  and `Meta.ordering` clause) is memoised per (dialect, lookup-name,
  NULL-mask, limit) on the model's meta instead of being re-rendered per
  call.
- **`select_related` row hydration precomputes its per-relation plan**
  (column slice, hydrator, parent/attribute) once per query instead of
  re-reading the node dicts per row, with a dedicated single-relation fast
  path; annotation columns and `only()`/`defer()` partial hydration behave
  exactly as before.
- **SQLite statements no longer hop to a blocking thread.** Every pooled and
  in-transaction statement (`execute`, `fetch_*`, `execute_many`, savepoints,
  COMMIT/ROLLBACK) now runs inline on the async runtime instead of paying a
  `spawn_blocking` round trip per statement — measured at ~17% of per-query
  time, +11% on autocommit inserts and +10–16% on N+1-style workloads.
  `BEGIN IMMEDIATE` (which can queue on `busy_timeout` for up to 5s behind
  concurrent write transactions) and `execute_script` (arbitrary migration
  scripts) stay on the blocking pool so a parked statement cannot stall
  unrelated queries. Parameters and SQL are now borrowed instead of cloned
  per statement.
- **SQLite result decoding plans each column once per result set.** The
  declared-type substring scans (up to six per cell) are replaced by a
  per-column decode tag computed once per statement, cell text is borrowed
  instead of copied per typed decode attempt, and datetime TEXT parsing
  dispatches on the value's shape (offset suffix, `T` vs space separator) so
  well-formed values parse on the first try instead of walking a trial
  chain. All previously accepted datetime layouts — canonical aware
  (`YYYY-MM-DD HH:MM:SS.ffffff+00:00`), legacy RFC 3339 rows, and naive
  space/`T` forms — still decode identically (covered by new Rust unit tests
  plus a corpus differential check against the old decoder).
- **Dict-row column names are created once per result set**: `fetch_all`'s
  list-of-dicts conversion now interns each column name as a single shared
  Python string instead of allocating a fresh key per cell per row, and the
  Rust-side row representation shares one `Arc<str>` per column name across
  rows. Positional `fetch_rows` is untouched.
- **Release profile: fat LTO + a single codegen unit** (+7–12% on decode-heavy
  fetch benchmarks; release builds take longer).

## [1.11.0] - 2026-07-02

### Added

- **Typed relations (Tortoise-style).** The relation annotations
  `ForeignKeyRelation[X]` / `ForeignKeyNullableRelation[X]` /
  `OneToOneRelation[X]` / `OneToOneNullableRelation[X]` /
  `ReverseRelation[X]` / `ManyToManyRelation[X]` are now real generics:
  a checker sees `book.author` as `Author | ForwardRelation[Author]`,
  `await author.books` as `list[Book]`, and `book.fans` as
  `M2MManager[Author]` (previously they were subscriptable no-ops). The
  relation field factories are typed to return the relation, so
  `author: fields.ForeignKeyRelation[Author] = fields.ForeignKeyField("Author")`
  type-checks, and unannotated declarations keep working.

### Changed

- `ForeignKeyField` / `OneToOneField` / `ManyToManyField` are now factory
  *functions* (Tortoise's exact structure); the classes they construct are
  `ForeignKeyFieldInstance` / `OneToOneFieldInstance` /
  `ManyToManyFieldInstance`. Declarations are untouched; only
  `isinstance(f, fields.ForeignKeyField)`-style checks (and subclassing)
  must switch to the `*Instance` names.
- **Precise annotations across the public API.** ``Any`` was replaced with
  the real type wherever one exists: ``using_db`` params are
  ``str | BaseDBAsyncClient | None``, executors are the ``BaseDBAsyncClient``
  protocol (now satisfied statically by the engine, transaction wrapper and
  proxy alike), a ``Router`` protocol types ``init(router=)``/``set_router``,
  ``transactions.atomic`` is fully ``ParamSpec``-typed, signal registries,
  query hooks, dialects and relation/join internals carry concrete types, and
  concrete fields' ``to_db``/``to_python`` declare their real returns.
  Annotation-only — no runtime behaviour change.

## [1.10.0] - 2026-07-02

A full-codebase correctness audit (five independent review passes over the query
compiler, migrations, model layer, connection/transaction machinery and the Rust
engine) followed by a fix of every confirmed finding. 147 new regression tests.

### Fixed — data loss

- **Annotation-filtered `delete()` / `update()` no longer wipe the table.**
  `annotate(n=Count(...)).filter(n__gt=5).delete()` compiled its HAVING
  condition to nothing and emitted `DELETE FROM t` with **no WHERE clause**.
  Both terminals now restrict through `pk IN (SELECT pk ... GROUP BY pk
  HAVING ...)`; sliced `delete()`/`update()` raise `TypeError` (Django parity).
- **Migrations preserve database-side defaults.** `makemigrations` dropped
  `default=Now()`-style DB defaults from column DDL entirely — a migrated table
  failed its first insert with a NOT NULL violation and the drift was invisible
  to a follow-up diff. Defaults are now recorded in migration files
  (`default=db_defaults.Now()`), rendered as `DEFAULT` clauses, and default
  changes autodetect to `AlterField`.
- **SQLite `AlterField` rebuilds no longer cascade-delete child rows.** The
  table rebuild dropped the old table with `foreign_keys=ON`, firing
  `ON DELETE CASCADE` into every child table. The rebuild is now bracketed by
  the FK-pragma sandwich from SQLite's documented recipe; child rows survive
  and FK enforcement resumes afterwards. Rebuilds also keep composite indexes,
  named constraints and `unique_together`, and recreate single-column indexes
  under their final names (a second alter on the same table previously failed
  on a leftover `idx__new_*` index).
- **A full `save()` after `create()` no longer NULLs DB-default columns.**
  Unfetched `DatabaseDefault` columns are excluded from a full-row UPDATE, and
  `Meta.fetch_db_defaults = True` is now implemented: `create()`/`save()`
  refresh DB-generated values onto the instance via `INSERT ... RETURNING`
  (both backends). Explicitly supplied values for DB-default columns are also
  actually inserted (they were silently dropped in favour of the default).
- **Writes inside a transaction land on the right database.** A nested
  `in_transaction("other")` and any model routed to another connection were
  silently absorbed by the open transaction's connection. Transactions are now
  tracked per connection name: same-name nesting still uses savepoints, a
  different name opens an independent transaction, and the router/`using`
  resolution is honoured inside transactions.
- **Cancelling a transaction mid-`commit`/`rollback`/`begin` (e.g.
  `asyncio.wait_for` timeouts) can no longer return a connection to the pool
  with an open transaction**, where the next request would silently join and
  lose its writes. Connections re-enter the pool only after a clean
  COMMIT/ROLLBACK; on any cancellation window the connection is rolled back in
  the background or destroyed.
- **Naive and aware datetimes compare correctly on SQLite.** Aware values were
  stored as RFC 3339 (`T` separator) next to naive space-separated text, so
  range filters and ordering across the two forms were lexicographically wrong.
  Aware datetimes now store as `YYYY-MM-DD HH:MM:SS.ffffff+00:00` UTC text;
  existing RFC 3339 rows still decode. **Upgrade note:** a SQLite database
  holding aware datetimes written by ≤ 1.9 should rewrite them once so old and
  new rows compare correctly —
  `UPDATE t SET col = replace(col, 'T', ' ') WHERE col LIKE '%T%'`
  per affected column (naive-only columns, the default, need nothing).

### Fixed — wrong results

- **`count()` / `exists()` honour `group_by`, annotation filters and slices**
  (previously they counted the unfiltered, unsliced table).
- **Slices compose relative to the existing window** — `qs.offset(5)[:3]`
  returns rows 5–7 (was 0–2), `qs[10:][3]` row 13, and an inverted slice is
  empty (was: unbounded on SQLite via `LIMIT -2`).
- **Two FKs to the same table (or a self-FK) in `values()` / `group_by()` /
  aggregates** now join under per-path aliases instead of erroring (42712) or
  silently reading the wrong side.
- **M2M `__isnull`** compiles to a real membership test (it previously bound
  `True` as a target pk — "objects tagged with tag 1" on SQLite).
- **`select_for_update()` is no longer silently dropped** on
  `select_related()` / `values()` shapes, and raises on `annotate`/`group_by`
  shapes PostgreSQL cannot lock.
- **`iexact`/`contains`/`startswith`/`endswith` escape `%`, `_` and `\`** in
  the user value (with an `ESCAPE` clause), so user input can no longer smuggle
  LIKE wildcards — `filter(email__iexact="a_min@x.com")` is exact again.
- **`last()` follows `Meta.ordering`** (it reversed pk order regardless);
  `first()`/`last()` now return opposite ends of the same ordering.
- **A stale FK cache no longer serves the old object** after `book.author =
  None` / `book.author = other_pk` / a direct `author_id` write; a cached miss
  is also not pinned forever.
- **`Prefetch(relation, queryset=...)` constrains forward FK/O2O prefetches**
  (the custom queryset was ignored on the forward branch).
- **`get_or_none()` with multiple matches is deterministic** — the fast path
  applies `Meta.ordering` like the queryset path.
- **`bulk_get_or_create` / `bulk_update_or_create` match keys typed loosely**
  (`"42"` vs `42`, UUID strings) instead of silently inserting duplicates, and
  the default `update_fields` is the union across all records, not the first.
- **Unknown PostgreSQL column types raise a clear error** naming the OID and
  column (previously every such cell silently decoded to `None`); `bytes`
  inside a bound array on SQLite base64-encode instead of becoming JSON `null`.

### Fixed — silent misconfiguration now raises

- `connections.get("typo")` raises `ConfigurationError` instead of silently
  using the default connection.
- A duplicate `related_name` on one target raises at startup (the second
  model's reverse accessor was silently never installed); `related_name`
  supports Django-style `%(class)s` for abstract bases.
- An ambiguous bare model name (two `Order` classes in different modules)
  raises listing the candidates instead of picking the most recently defined.
- Assigning an unsaved instance to a FK raises `ValueError` (it silently
  stored NULL).
- `filter()`/`exclude()` reject non-`Q` positional arguments (they were
  silently discarded — `filter(expr)` was a no-op returning all rows).
- `makemigrations` refuses to write a migration that would drop **every**
  table (the empty-`--models` footgun); `--allow-destructive` overrides.
- `upgrade`/`downgrade` validate the target migration before applying anything
  (an unknown target used to apply *all* remaining migrations) and both accept
  numeric prefixes; duplicate migration numbers and unsatisfiable declared
  dependencies warn at load time (ties in numeric order now break
  deterministically by file name).
- Unsaved model instances are unhashable (Django parity — a saved pk changed
  the hash and corrupted sets/dicts); `BooleanField` coerces `"false"`/`"0"`
  to `False` and rejects unrecognised strings (any non-empty string bound as
  `True`); `NumericValidator` rejects `nan`/`inf`.

### Added

- `exclude()` can target annotations (negated HAVING), symmetric with
  `filter()`.
- Migrations: FK targets are stamped into generated files (`m.resolved_fk`),
  so replaying/diffing no longer needs the referenced model importable, and
  target-pk-type changes propagate to referencing columns; single-column
  `unique` toggles and `on_delete` changes are autodetected;
  `AddConstraint`/`RemoveConstraint` apply on SQLite via a table rebuild.
- SQLite transactions use `BEGIN IMMEDIATE` (+ a 5 s busy timeout), so
  concurrent read-then-write transactions queue instead of failing instantly
  with `database is locked`; `sqlite://` URLs accept `?mode=memory` and reject
  unknown parameters instead of treating them as part of the filename.
- `execute_script()` runs the whole script on a single pinned connection
  (statements were previously spread across pooled connections, splitting
  session state and explicit BEGIN/COMMIT); each statement still runs in
  autocommit, so `VACUUM`/`PRAGMA`-style statements keep working, and a
  transaction the script leaves open is rolled back before the connection
  returns to the pool. `execute_many()` is transactional (all-or-nothing).
- Transaction-control failures raise `OperationalError` /
  `TransactionManagementError` (not bare `RuntimeError`), connect failures
  raise `DBConnectionError`, and out-of-order savepoint release from
  concurrent tasks sharing one transaction is detected with a clear error.

### Changed

- `select_related()` combined with `annotate()` raises `FieldError` (the
  eager joins were silently dropped; use `prefetch_related()`).
- An `exclude()` mixing annotation and column lookups in one call raises
  `FieldError` (a sound De Morgan split isn't possible).
- Rename autodetection is conservative: only an unambiguous single drop+add
  pair of identical spec becomes `RenameField`; ambiguous sets emit drop+add
  plus a hint. A same-shape drop+create table pair gets a prominent warning
  suggesting `RenameModel` instead of silently destroying data.
- `Subquery(qs.only("col"))` projects exactly the named column (the auto-pk
  no longer widens the subquery); `When(...)` with no condition raises;
  `100 / F("x")` works (`__rtruediv__`).

## [1.9.0] - 2026-07-01

### Security

- **TLS for PostgreSQL is honoured from `sslmode`.** Connections now negotiate
  TLS through a native connector chosen by the URL's `sslmode`: `require` /
  `verify-ca` / `verify-full` actually encrypt and verify the server certificate
  against the OS trust store, `disable` opts out, and `prefer` (the default)
  tries TLS and falls back to plaintext only when the server offers no SSL.
  **Behaviour change:** a `require`-mode connection to a server without SSL now
  fails instead of silently downgrading to plaintext. TLS uses pure-Rust rustls
  (ring), so wheels need no system OpenSSL at build or run time on any platform.
- **`RawSQL` can be parameterised** — `RawSQL("expr ?", [value])` binds each `?`
  marker as a parameter, so untrusted values no longer have to be interpolated
  into the SQL text. The no-argument form is unchanged (verbatim, caller-trusted).
- **`ForeignKeyField.on_delete` is validated** against the `OnDelete` actions
  (it is spliced into DDL, not bound); an unknown action raises `ValueError`.
  Values are normalised (`"set null"` → `"SET NULL"`).
- **Index `using` / `opclass` are validated** — a known access method and a plain
  (optionally schema-qualified) identifier — before being spliced into
  `CREATE INDEX`.
- **Connection credentials are redacted** from config/connection error messages,
  so a driver error surfaced to Python cannot leak the password.

### Fixed

- **`Q` OR-groups keep their parentheses.** `filter(Q(a) | Q(b), c=v)` and a
  chained `.filter(Q(a) | Q(b))` now compile to `(a OR b) AND c` instead of
  `a OR (b AND c)`, so keyword filters are no longer swallowed into one OR branch
  (a WHERE-precedence corruption).
- **Text columns accept a non-string scalar** — a `CharField`/`TextField`
  filtered or populated with a non-bool `int` binds as text, avoiding
  `operator does not exist: character varying = bigint`.
- **`SmallIntField` reads back as `int`** (it had no `to_python`).
- **`RemoveCompositeIndexIfExists` round-trips to its own class** in generated
  migration source (it previously serialised as the base `RemoveCompositeIndex`).
- **`CommaSeparatedIntegerListValidator` rejects multi-dash tokens** such as
  `"--5"`.
- **`connections.get()` fallback no longer double-wraps the engine**, so query
  hooks fire once (not twice) for raw SQL on that path.

### Changed

- Internal refactors with no API change: single-sourced relation-name resolution,
  integer/temporal field base classes, unified expression/function operand
  rendering, a shared chainable relation-manager base, and a `_ReversibleOp` base
  that collapses the migration Add/Remove operation pairs.
- Performance: memoised the per-lookup `relations` import and `dialect.quote()`,
  cached relation-target resolution, and removed redundant migration-file
  re-reads and directory scans. Benchmarks remain fastest-in-class across insert,
  read, filter, get, update and delete versus Tortoise, SQLAlchemy and Pony.

## [1.8.0] - 2026-07-01

### Added

- **JSON `__contains` containment** (`@>`) on a `JSONField` — matches an object
  subset, an array element, or an array-of-objects subset
  (`Model.filter(tags__contains=[{"name": "vip"}])`). PostgreSQL only.
- **JSON `__filter` lookup** — `col__filter={"path__op": value, ...}` applies each
  entry as a JSON key-path condition on the column and ANDs them (Tortoise's JSON
  `__filter`), e.g. `audit_log_meta__filter={"status__not": "resolved"}`.
- **`group_by()` / `values()` / `values_list()` accept forward-relation paths** —
  `group_by("author__country")`, `values(country="author__country")` — the
  related table is joined automatically.

### Fixed

- **`__not` / `__not_in` keep NULL rows** (Tortoise semantics) — they compile to
  `(col != v OR col IS NULL)` / `(col NOT IN (...) OR col IS NULL)` so a nullable
  column's `NULL` rows are not silently dropped from a negative filter.
- **Integer columns accept string values** — `filter(id__in={"1", "2"})` /
  `filter(id="3")` coerce the strings to `int` before binding, avoiding
  'operator does not exist: integer = text' (42883).
- **`bulk_update` bumps `auto_now` columns** — an `updated_at`-style column is set
  to now and written even when not listed in `fields` (bulk_create already
  applied `auto_now`/`auto_now_add`).
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

//! SQLite backend built on rusqlite + deadpool-sqlite.
//!
//! rusqlite is synchronous. Ordinary statements run *inline* on the tokio
//! worker (via deadpool-sync's `Object::lock`): a pooled SQLite statement
//! completes in microseconds, and the `interact()` alternative costs a full
//! `spawn_blocking` round trip per statement (measured at ~17% of total query
//! time). The blocking thread (`interact`) is kept only for work that can
//! plausibly park on SQLite's locks or run long — see the notes on
//! `begin_tx` (BEGIN IMMEDIATE waits on `busy_timeout`, up to 5s) and
//! `execute_script` (arbitrary migration scripts) — where stalling a tokio
//! worker would stall unrelated queries.

use std::sync::Arc;

use async_trait::async_trait;
use deadpool_sqlite::{Config, Hook, HookError, Object, Pool, Runtime};
use rusqlite::Connection;

use crate::backend::pool::extract_pool_params;
use crate::backend::{Backend, TxConn, TxState};
use crate::error::EngineError;
use crate::value::{decode_sqlite, sqlite_decode_plan, Row, SqliteDecode, Value};

/// Default pool size for file-backed databases when the URL omits `max_size`.
const DEFAULT_MAX_SIZE: usize = 8;

fn map_interact<E: std::fmt::Display>(e: E) -> EngineError {
    EngineError::Query(format!("sqlite interact: {e}"))
}

/// Map a rusqlite error, promoting constraint violations (UNIQUE, FOREIGN KEY,
/// NOT NULL, CHECK) to [`EngineError::Integrity`] so they reach Python as
/// `IntegrityError` instead of a generic runtime error.
fn map_sqlite(e: rusqlite::Error) -> EngineError {
    if let rusqlite::Error::SqliteFailure(err, msg) = &e {
        if err.code == rusqlite::ErrorCode::ConstraintViolation {
            return EngineError::Integrity(msg.clone().unwrap_or_else(|| e.to_string()));
        }
    }
    EngineError::Query(e.to_string())
}

/// Prepare `sql` (cached or not) and hand the statement to `f`. Caching is
/// skipped when the URL carried `statement_cache_size=0`.
fn with_stmt<T>(
    conn: &Connection,
    sql: &str,
    cache: bool,
    f: impl FnOnce(&mut rusqlite::Statement) -> Result<T, EngineError>,
) -> Result<T, EngineError> {
    if cache {
        let mut stmt = conn.prepare_cached(sql).map_err(map_interact)?;
        f(&mut stmt)
    } else {
        let mut stmt = conn.prepare(sql).map_err(map_interact)?;
        f(&mut stmt)
    }
}

/// Options parsed from a sqlite URL by [`sqlite_path`].
#[derive(Debug, PartialEq, Eq)]
struct SqliteUrl {
    /// The database file path, or `":memory:"`.
    path: String,
    /// Whether the URL opted into the synchronous fast path
    /// (`sync_fast_path=1`); see [`SqliteBackend::sync_capable`].
    sync_fast_path: bool,
}

/// Resolve the database path (and sqlite-only options) from a sqlite URL,
/// honouring its query string.
///
/// The pool parameters (`max_size`/...) were already stripped by
/// `extract_pool_params`; whatever query string remains is parsed here rather
/// than being treated as part of the file name (`sqlite://data.db?cache=shared`
/// must not open a literal file named `data.db?cache=shared`). `mode=memory`
/// selects an in-memory database and `sync_fast_path=1` opts into the
/// synchronous engine fast path (`0`/`off` keep the default async bridge); any
/// other parameter — or a bad `sync_fast_path` value — is rejected so a typo
/// cannot silently corrupt the path or silently drop the opt-in.
fn sqlite_path(url: &str) -> Result<SqliteUrl, EngineError> {
    let raw = url
        .strip_prefix("sqlite://")
        .or_else(|| url.strip_prefix("sqlite:"))
        .unwrap_or(url);
    let (path, query) = match raw.split_once('?') {
        Some((p, q)) => (p, Some(q)),
        None => (raw, None),
    };
    let mut memory = false;
    let mut sync_fast_path = false;
    if let Some(q) = query {
        for pair in q.split('&').filter(|p| !p.is_empty()) {
            let (key, val) = pair.split_once('=').unwrap_or((pair, ""));
            if key == "mode" && val == "memory" {
                memory = true;
            } else if key == "sync_fast_path" {
                sync_fast_path = match val {
                    "1" => true,
                    "0" | "off" => false,
                    _ => {
                        return Err(EngineError::Config(format!(
                            "invalid sync_fast_path value {val:?} (supported: 1, 0, off)"
                        )))
                    }
                };
            } else {
                return Err(EngineError::Config(format!(
                    "unsupported sqlite URL parameter {pair:?} (supported: mode=memory, \
                     sync_fast_path, max_size, min_size, statement_cache_size)"
                )));
            }
        }
    }
    let path = if memory || path.is_empty() {
        ":memory:".to_string()
    } else {
        path.to_string()
    };
    Ok(SqliteUrl {
        path,
        sync_fast_path,
    })
}

// --- synchronous primitives ------------------------------------------------
// Run inline on the tokio worker (see the module docs and `lock_conn` for why
// that is safe); `sql` and `params` are borrowed from the caller instead of
// being cloned into a `'static` closure for `interact`.

fn sql_execute(
    conn: &Connection,
    sql: &str,
    params: &[Value],
    cache: bool,
) -> Result<u64, EngineError> {
    with_stmt(conn, sql, cache, |stmt| {
        let n = stmt
            .execute(rusqlite::params_from_iter(params))
            .map_err(map_sqlite)?;
        Ok(n as u64)
    })
}

/// Per-result-set column metadata: shared column name + decode plan.
type ColumnMeta = Vec<(Arc<str>, SqliteDecode)>;

fn column_meta(stmt: &rusqlite::Statement) -> ColumnMeta {
    // Resolve each column's decode plan from its declared type once per result
    // set; `decode_sqlite` then dispatches on the tag per cell without any
    // substring scans. Names are `Arc<str>` so `to_named` shares one
    // allocation across all rows.
    stmt.columns()
        .iter()
        .map(|c| {
            (
                Arc::from(c.name()),
                sqlite_decode_plan(c.decl_type().unwrap_or("")),
            )
        })
        .collect()
}

fn sql_fetch_rows(
    conn: &Connection,
    sql: &str,
    params: &[Value],
    with_names: bool,
    cache: bool,
) -> Result<(ColumnMeta, Vec<Vec<Value>>), EngineError> {
    with_stmt(conn, sql, cache, |stmt| {
        let meta = column_meta(stmt);
        let mut rows = stmt
            .query(rusqlite::params_from_iter(params))
            .map_err(map_sqlite)?;
        let mut out = Vec::new();
        while let Some(row) = rows.next().map_err(map_sqlite)? {
            let mut values = Vec::with_capacity(meta.len());
            for (idx, (_, plan)) in meta.iter().enumerate() {
                let vr = row.get_ref(idx).map_err(map_interact)?;
                values.push(decode_sqlite(*plan, vr));
            }
            out.push(values);
        }
        let meta = if with_names { meta } else { Vec::new() };
        Ok((meta, out))
    })
}

/// Run the whole batch inside one transaction so a mid-batch failure applies
/// nothing. The transaction begins/ends within this one synchronous call (no
/// await points), so a cancelled Python future cannot leave it half-open.
fn sql_execute_many(
    conn: &Connection,
    sql: &str,
    rows: &[Vec<Value>],
    cache: bool,
) -> Result<Vec<Row>, EngineError> {
    conn.execute_batch("BEGIN IMMEDIATE").map_err(map_sqlite)?;
    match sql_execute_many_inner(conn, sql, rows, cache) {
        Ok(out) => match conn.execute_batch("COMMIT") {
            Ok(()) => Ok(out),
            // A failed COMMIT (e.g. SQLITE_BUSY) can leave the write transaction
            // open; roll it back so a mid-transaction connection is not recycled
            // into the pool where a later borrower would hit "cannot start a
            // transaction within a transaction" or silently join the stale tx.
            Err(e) => {
                let _ = conn.execute_batch("ROLLBACK");
                Err(map_sqlite(e))
            }
        },
        Err(e) => {
            let _ = conn.execute_batch("ROLLBACK");
            Err(e)
        }
    }
}

fn sql_execute_many_inner(
    conn: &Connection,
    sql: &str,
    rows: &[Vec<Value>],
    cache: bool,
) -> Result<Vec<Row>, EngineError> {
    with_stmt(conn, sql, cache, |stmt| {
        let meta = column_meta(stmt);
        let mut out = Vec::with_capacity(rows.len());
        for row_params in rows {
            let mut qrows = stmt
                .query(rusqlite::params_from_iter(row_params))
                .map_err(map_sqlite)?;
            if let Some(row) = qrows.next().map_err(map_sqlite)? {
                let mut r = Vec::with_capacity(meta.len());
                for (idx, (name, plan)) in meta.iter().enumerate() {
                    let vr = row.get_ref(idx).map_err(map_interact)?;
                    r.push((name.clone(), decode_sqlite(*plan, vr)));
                }
                out.push(r);
            } else {
                out.push(Vec::new());
            }
        }
        Ok(out)
    })
}

fn to_named(meta: &[(Arc<str>, SqliteDecode)], rows: Vec<Vec<Value>>) -> Vec<Row> {
    rows.into_iter()
        .map(|vals| {
            meta.iter()
                .map(|(n, _)| n.clone())
                .zip(vals)
                .collect::<Row>()
        })
        .collect()
}

// --- backend ---------------------------------------------------------------

pub struct SqliteBackend {
    pool: Pool,
    /// When false (URL `statement_cache_size=0`), prepared statements are not
    /// cached per connection. Kept for parity with the Postgres backend; SQLite
    /// has no connection proxy, so this is mainly a knob for predictable memory.
    cache_statements: bool,
    /// URL `sync_fast_path=1`: the engine may drive this backend's statement
    /// futures to completion synchronously on the calling Python thread.
    sync_fast_path: bool,
}

impl SqliteBackend {
    pub async fn connect(url: &str) -> Result<Self, EngineError> {
        let (clean_url, params) = extract_pool_params(url)?;
        let parsed = sqlite_path(&clean_url)?;
        let (path, sync_fast_path) = (parsed.path, parsed.sync_fast_path);
        let in_memory = path == ":memory:";
        let cfg = Config::new(path);
        // In-memory databases are per-connection, so they must pin a single one;
        // file databases honour `max_size` (default 8).
        let max_size = if in_memory {
            1
        } else {
            params.max_size.unwrap_or(DEFAULT_MAX_SIZE)
        };

        // PRAGMAs that must hold on *every* connection, not just the pre-warmed
        // ones. `foreign_keys=ON` is essential: SQLite ignores FOREIGN KEY
        // constraints (and ON DELETE actions) unless it is set per connection,
        // and the setting does not survive into connections the pool creates
        // lazily — so it is applied from a post_create hook. File databases also
        // get WAL + relaxed sync for throughput; :memory: supports neither WAL
        // nor multiple connections, so it only gets foreign_keys.
        // `busy_timeout` makes a connection wait (instead of failing instantly
        // with SQLITE_BUSY) when another connection holds a conflicting lock —
        // required for concurrent writers on a file database, and what makes
        // BEGIN IMMEDIATE block rather than error under write contention.
        let pragma = if in_memory {
            "PRAGMA foreign_keys=ON;"
        } else {
            "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA foreign_keys=ON; \
             PRAGMA busy_timeout=5000;"
        };
        let pool = cfg
            .builder(Runtime::Tokio1)
            .map_err(|e| EngineError::Config(e.to_string()))?
            .max_size(max_size)
            .post_create(Hook::async_fn(move |obj, _| {
                Box::pin(async move {
                    obj.interact(move |conn| conn.execute_batch(pragma))
                        .await
                        .map_err(|e| HookError::message(e.to_string()))?
                        .map_err(HookError::Backend)?;
                    Ok(())
                })
            }))
            .build()
            .map_err(|e| EngineError::Config(e.to_string()))?;

        // Pre-warm at least one connection so we fail fast on an unreachable
        // database, and up to `min_size` so early queries skip connection setup.
        // The post_create hook above has already applied the PRAGMAs.
        let warm = params.min_size.unwrap_or(0).max(1).min(max_size);
        let mut held = Vec::with_capacity(warm);
        for _ in 0..warm {
            held.push(
                pool.get()
                    .await
                    .map_err(|e| EngineError::Connection(e.to_string()))?,
            );
        }
        drop(held); // return the warmed connections to the pool as idle

        Ok(Self {
            pool,
            cache_statements: params.cache_statements,
            sync_fast_path,
        })
    }

    async fn obj(&self) -> Result<Object, EngineError> {
        self.pool
            .get()
            .await
            .map_err(|e| EngineError::Connection(e.to_string()))
    }
}

/// Lock a pooled connection for *inline* use on the tokio worker, instead of
/// shipping a closure to the blocking pool with `interact()`.
///
/// This is safe here because the mutex is effectively uncontended: the pool
/// hands each `Object` to one task at a time, so the only other lockers are a
/// finished `interact()` (already released) — the lock itself never parks.
/// What can stall inline is SQLite: a write statement may wait on
/// `busy_timeout` while another connection holds the write lock. That wait is
/// normally far below a millisecond for the autocommit statements run this
/// way (WAL writers hold the lock only for the statement itself), and the
/// per-statement `spawn_blocking` round trip it replaces measured ~17% of
/// total query time. Work that *plausibly* parks for longer stays on
/// `interact()`: `begin_tx`'s BEGIN IMMEDIATE (queues behind whole
/// transactions, up to the full 5s `busy_timeout`) and `execute_script`
/// (arbitrary long migration scripts).
///
/// Poisoning (a panic inside a previous `interact`) surfaces as a query error.
fn lock_conn(obj: &Object) -> Result<impl std::ops::Deref<Target = Connection> + '_, EngineError> {
    obj.lock().map_err(map_interact)
}

#[async_trait]
impl Backend for SqliteBackend {
    // Statement methods run inline on the tokio worker (see `lock_conn`): no
    // per-statement `spawn_blocking` hop, and `sql`/`params` are borrowed
    // instead of cloned into a `'static` closure.

    async fn execute(&self, sql: &str, params: &[Value]) -> Result<u64, EngineError> {
        let obj = self.obj().await?;
        let conn = lock_conn(&obj)?;
        sql_execute(&conn, sql, params, self.cache_statements)
    }

    async fn fetch_all(&self, sql: &str, params: &[Value]) -> Result<Vec<Row>, EngineError> {
        let obj = self.obj().await?;
        let conn = lock_conn(&obj)?;
        let (meta, rows) = sql_fetch_rows(&conn, sql, params, true, self.cache_statements)?;
        Ok(to_named(&meta, rows))
    }

    async fn fetch_all_values(
        &self,
        sql: &str,
        params: &[Value],
    ) -> Result<Vec<Vec<Value>>, EngineError> {
        let obj = self.obj().await?;
        let conn = lock_conn(&obj)?;
        let (_, rows) = sql_fetch_rows(&conn, sql, params, false, self.cache_statements)?;
        Ok(rows)
    }

    async fn execute_many(&self, sql: &str, rows: &[Vec<Value>]) -> Result<Vec<Row>, EngineError> {
        // Inline like the other statement methods: the batch's own BEGIN
        // IMMEDIATE contends for the write lock exactly like an autocommit
        // INSERT does, and the whole batch runs without await points so a
        // cancelled Python future cannot abandon it half-applied.
        let obj = self.obj().await?;
        let conn = lock_conn(&obj)?;
        sql_execute_many(&conn, sql, rows, self.cache_statements)
    }

    async fn execute_script(&self, statements: &[String]) -> Result<(), EngineError> {
        let obj = self.obj().await?;
        let statements = statements.to_vec();
        // Deliberately stays on `interact()` (the blocking thread pool):
        // scripts are arbitrary user SQL (migrations, bulk DDL) that can run
        // for seconds — inline they would stall a tokio worker and every task
        // scheduled on it.
        obj.interact(move |conn| {
            let mut result = Ok(());
            for statement in &statements {
                // Each statement runs in autocommit (no wrapping transaction),
                // so PRAGMAs take effect and explicit BEGIN/COMMIT inside the
                // script hold together on this one connection.
                if let Err(e) = conn.execute_batch(statement) {
                    result = Err(map_sqlite(e));
                    break;
                }
            }
            // Safety net: never hand a mid-transaction connection back to the
            // pool when the script failed (or forgot COMMIT).
            if !conn.is_autocommit() {
                let _ = conn.execute_batch("ROLLBACK");
            }
            result
        })
        .await
        .map_err(map_interact)?
    }

    fn dialect(&self) -> &'static str {
        "sqlite"
    }

    fn sync_capable(&self) -> bool {
        // Opt-in via `sqlite://...?sync_fast_path=1`. Statement futures on this
        // backend complete in microseconds (pool checkout + an in-process
        // rusqlite call, no real I/O awaits), so the engine may legally drive
        // them with `block_on` from the Python caller thread. `begin_tx` is
        // exempt — the engine always keeps it async, because BEGIN IMMEDIATE
        // can park on `busy_timeout` for up to 5s.
        self.sync_fast_path
    }

    async fn close(&self) {
        self.pool.close();
    }

    async fn begin_tx(&self, _isolation: Option<&str>) -> Result<Box<dyn TxConn>, EngineError> {
        // SQLite transactions are serializable; the Python layer rejects any
        // other requested level, so the isolation hint is ignored here.
        let obj = self.obj().await?;
        // Arm the drop guard *before* BEGIN so a cancellation mid-BEGIN cannot
        // recycle a possibly-in-transaction connection.
        let tx = SqliteTx {
            obj: Some(obj),
            cache_statements: self.cache_statements,
            state: TxState::Active,
        };
        // BEGIN IMMEDIATE takes the write (RESERVED) lock up front, so
        // concurrent read-then-write transactions queue on `busy_timeout`
        // instead of failing instantly with an unretryable
        // SQLITE_BUSY_SNAPSHOT at their first write. The trade-off — writers
        // serialize from BEGIN rather than from their first write — is the
        // right one for an ORM's in_transaction(), which usually writes.
        //
        // This one deliberately stays on `interact()` (unlike the statement
        // methods, which run inline): queuing behind *whole transactions* here
        // can park for the full 5s `busy_timeout`, and that must happen on the
        // blocking pool, not a tokio worker.
        tx.obj()
            .interact(|conn| conn.execute_batch("BEGIN IMMEDIATE"))
            .await
            .map_err(map_interact)?
            .map_err(map_sqlite)?;
        Ok(Box::new(tx))
    }
}

// --- transaction -----------------------------------------------------------


/// A pinned-connection SQLite transaction.
///
/// Like the PostgreSQL twin, the connection only returns to the pool after a
/// clean COMMIT/ROLLBACK; dropped in any other state (cancelled task, abandoned
/// transaction) the guard rolls the transaction back on the background runtime
/// and detaches + closes the connection if even that fails, so a
/// mid-transaction connection is never recycled.
struct SqliteTx {
    obj: Option<Object>,
    cache_statements: bool,
    state: TxState,
}

impl SqliteTx {
    fn obj(&self) -> &Object {
        self.obj
            .as_ref()
            .expect("SqliteTx connection is present until drop")
    }

    /// Run a COMMIT/ROLLBACK inline and settle the drop-guard state. Inline is
    /// safe for these: the transaction has held the write lock since BEGIN
    /// IMMEDIATE, so neither statement waits on other connections (a WAL
    /// commit appends to the log; it does not wait for readers).
    fn control(&mut self, sql: &'static str) -> Result<(), EngineError> {
        let result =
            lock_conn(self.obj()).and_then(|conn| conn.execute_batch(sql).map_err(map_sqlite));
        self.state = if result.is_ok() {
            TxState::Finished
        } else {
            TxState::Broken
        };
        result
    }
}

impl Drop for SqliteTx {
    fn drop(&mut self) {
        let Some(obj) = self.obj.take() else {
            return;
        };
        match self.state {
            // Clean end: dropping the Object recycles the connection normally.
            TxState::Finished => drop(obj),
            // A COMMIT/ROLLBACK failed outright: state unknown, so take the
            // connection out of the pool and close it.
            TxState::Broken => drop(Object::take(obj)),
            // Dropped mid-transaction: roll back on the background runtime.
            // "no transaction is active" means a cancelled COMMIT actually
            // completed — the connection is clean and may recycle. Any other
            // failure closes the connection instead of recycling it dirty.
            TxState::Active => {
                pyo3_async_runtimes::tokio::get_runtime().spawn(async move {
                    let rolled_back = obj.interact(|conn| conn.execute_batch("ROLLBACK")).await;
                    let clean = match &rolled_back {
                        Ok(Ok(())) => true,
                        Ok(Err(e)) => e.to_string().contains("no transaction is active"),
                        Err(_) => false,
                    };
                    if !clean {
                        drop(Object::take(obj));
                    }
                });
            }
        }
    }
}

#[async_trait]
impl TxConn for SqliteTx {
    // All statement and savepoint methods run inline (see `lock_conn`): the
    // transaction acquired the write lock at BEGIN IMMEDIATE, so nothing here
    // waits on other connections — inline they only cost the statement itself,
    // not a `spawn_blocking` round trip each.

    async fn execute(&self, sql: &str, params: &[Value]) -> Result<u64, EngineError> {
        let conn = lock_conn(self.obj())?;
        sql_execute(&conn, sql, params, self.cache_statements)
    }

    async fn fetch_all(&self, sql: &str, params: &[Value]) -> Result<Vec<Row>, EngineError> {
        let conn = lock_conn(self.obj())?;
        let (meta, rows) = sql_fetch_rows(&conn, sql, params, true, self.cache_statements)?;
        Ok(to_named(&meta, rows))
    }

    async fn fetch_all_values(
        &self,
        sql: &str,
        params: &[Value],
    ) -> Result<Vec<Vec<Value>>, EngineError> {
        let conn = lock_conn(self.obj())?;
        let (_, rows) = sql_fetch_rows(&conn, sql, params, false, self.cache_statements)?;
        Ok(rows)
    }

    async fn commit(mut self: Box<Self>) -> Result<(), EngineError> {
        self.control("COMMIT")
    }

    async fn rollback(mut self: Box<Self>) -> Result<(), EngineError> {
        self.control("ROLLBACK")
    }

    async fn savepoint(&self, name: &str) -> Result<(), EngineError> {
        let sql = format!("SAVEPOINT {name}");
        let conn = lock_conn(self.obj())?;
        conn.execute_batch(&sql).map_err(map_sqlite)
    }

    async fn release(&self, name: &str) -> Result<(), EngineError> {
        let sql = format!("RELEASE SAVEPOINT {name}");
        let conn = lock_conn(self.obj())?;
        conn.execute_batch(&sql).map_err(map_sqlite)
    }

    async fn rollback_to(&self, name: &str) -> Result<(), EngineError> {
        let sql = format!("ROLLBACK TO SAVEPOINT {name}");
        let conn = lock_conn(self.obj())?;
        conn.execute_batch(&sql).map_err(map_sqlite)
    }
}

#[cfg(test)]
mod tests {
    use super::sqlite_path;
    use crate::error::EngineError;

    /// The resolved path of a URL that parses cleanly.
    fn path(url: &str) -> String {
        sqlite_path(url).unwrap().path
    }

    #[test]
    fn plain_paths_and_memory_defaults() {
        assert_eq!(path("sqlite://./data.db"), "./data.db");
        assert_eq!(path("sqlite:/tmp/x.db"), "/tmp/x.db");
        assert_eq!(path("sqlite://"), ":memory:");
        assert_eq!(path("sqlite://:memory:"), ":memory:");
        // No query string: the fast path stays off.
        assert!(!sqlite_path("sqlite://./data.db").unwrap().sync_fast_path);
    }

    #[test]
    fn mode_memory_is_honoured() {
        assert_eq!(path("sqlite://x.db?mode=memory"), ":memory:");
    }

    #[test]
    fn unknown_query_params_error_instead_of_corrupting_the_path() {
        let err = sqlite_path("sqlite://data.db?cache=shared").unwrap_err();
        assert!(matches!(err, EngineError::Config(_)));
        assert!(err.to_string().contains("cache=shared"));
        // The error names the newly supported parameter so users discover it.
        assert!(err.to_string().contains("sync_fast_path"));
    }

    #[test]
    fn sync_fast_path_accepts_1_0_and_off() {
        let parsed = sqlite_path("sqlite://data.db?sync_fast_path=1").unwrap();
        assert_eq!(parsed.path, "data.db");
        assert!(parsed.sync_fast_path);
        assert!(
            !sqlite_path("sqlite://data.db?sync_fast_path=0")
                .unwrap()
                .sync_fast_path
        );
        assert!(
            !sqlite_path("sqlite://data.db?sync_fast_path=off")
                .unwrap()
                .sync_fast_path
        );
    }

    #[test]
    fn sync_fast_path_combines_with_mode_memory() {
        let parsed = sqlite_path("sqlite://x.db?mode=memory&sync_fast_path=1").unwrap();
        assert_eq!(parsed.path, ":memory:");
        assert!(parsed.sync_fast_path);
    }

    #[test]
    fn sync_fast_path_rejects_other_values() {
        for bad in ["true", "yes", "on", "2", ""] {
            let err = sqlite_path(&format!("sqlite://data.db?sync_fast_path={bad}")).unwrap_err();
            assert!(matches!(err, EngineError::Config(_)), "value {bad:?}");
            assert!(err.to_string().contains("sync_fast_path"), "value {bad:?}");
        }
    }
}

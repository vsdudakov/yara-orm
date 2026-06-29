//! SQLite backend built on rusqlite + deadpool-sqlite.
//!
//! rusqlite is synchronous, so each operation runs on deadpool's blocking
//! thread via `Object::interact`, keeping the async `Backend` contract.

use async_trait::async_trait;
use deadpool_sqlite::{Config, Hook, HookError, Object, Pool, Runtime};
use rusqlite::Connection;

use crate::backend::pool::extract_pool_params;
use crate::backend::{Backend, TxConn};
use crate::error::EngineError;
use crate::value::{decode_sqlite, value_into_sqlite, Row, Value};

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

fn sqlite_path(url: &str) -> String {
    let path = url
        .strip_prefix("sqlite://")
        .or_else(|| url.strip_prefix("sqlite:"))
        .unwrap_or(url);
    if path.is_empty() {
        ":memory:".to_string()
    } else {
        path.to_string()
    }
}

// --- synchronous primitives, run inside `interact` -------------------------

fn sql_execute(
    conn: &Connection,
    sql: &str,
    params: Vec<Value>,
    cache: bool,
) -> Result<u64, EngineError> {
    let bound: Vec<rusqlite::types::Value> = params.into_iter().map(value_into_sqlite).collect();
    with_stmt(conn, sql, cache, |stmt| {
        let n = stmt
            .execute(rusqlite::params_from_iter(bound))
            .map_err(map_sqlite)?;
        Ok(n as u64)
    })
}

fn column_meta(stmt: &rusqlite::Statement) -> Vec<(String, String)> {
    // Upper-case the declared type once per column here; `decode_sqlite` then
    // matches against it per cell without re-allocating an uppercased string.
    stmt.columns()
        .iter()
        .map(|c| {
            (
                c.name().to_string(),
                c.decl_type().unwrap_or("").to_ascii_uppercase(),
            )
        })
        .collect()
}

fn sql_fetch_rows(
    conn: &Connection,
    sql: &str,
    params: Vec<Value>,
    with_names: bool,
    cache: bool,
) -> Result<(Vec<(String, String)>, Vec<Vec<Value>>), EngineError> {
    let bound: Vec<rusqlite::types::Value> = params.into_iter().map(value_into_sqlite).collect();
    with_stmt(conn, sql, cache, |stmt| {
        let meta = column_meta(stmt);
        let mut rows = stmt
            .query(rusqlite::params_from_iter(bound))
            .map_err(map_sqlite)?;
        let mut out = Vec::new();
        while let Some(row) = rows.next().map_err(map_sqlite)? {
            let mut values = Vec::with_capacity(meta.len());
            for (idx, (_, decl)) in meta.iter().enumerate() {
                let vr = row.get_ref(idx).map_err(map_interact)?;
                values.push(decode_sqlite(decl, vr));
            }
            out.push(values);
        }
        let meta = if with_names { meta } else { Vec::new() };
        Ok((meta, out))
    })
}

fn sql_execute_many(
    conn: &Connection,
    sql: &str,
    rows: Vec<Vec<Value>>,
    cache: bool,
) -> Result<Vec<Row>, EngineError> {
    with_stmt(conn, sql, cache, |stmt| {
        let meta = column_meta(stmt);
        let mut out = Vec::with_capacity(rows.len());
        for row_params in rows {
            let bound: Vec<rusqlite::types::Value> =
                row_params.into_iter().map(value_into_sqlite).collect();
            let mut qrows = stmt
                .query(rusqlite::params_from_iter(bound))
                .map_err(map_sqlite)?;
            if let Some(row) = qrows.next().map_err(map_sqlite)? {
                let mut r = Vec::with_capacity(meta.len());
                for (idx, (name, decl)) in meta.iter().enumerate() {
                    let vr = row.get_ref(idx).map_err(map_interact)?;
                    r.push((name.clone(), decode_sqlite(decl, vr)));
                }
                out.push(r);
            } else {
                out.push(Vec::new());
            }
        }
        Ok(out)
    })
}

fn to_named(meta: &[(String, String)], rows: Vec<Vec<Value>>) -> Vec<Row> {
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
}

impl SqliteBackend {
    pub async fn connect(url: &str) -> Result<Self, EngineError> {
        let (clean_url, params) = extract_pool_params(url)?;
        let path = sqlite_path(&clean_url);
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
        let pragma = if in_memory {
            "PRAGMA foreign_keys=ON;"
        } else {
            "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA foreign_keys=ON;"
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
        })
    }

    async fn obj(&self) -> Result<Object, EngineError> {
        self.pool
            .get()
            .await
            .map_err(|e| EngineError::Connection(e.to_string()))
    }
}

#[async_trait]
impl Backend for SqliteBackend {
    async fn execute(&self, sql: &str, params: &[Value]) -> Result<u64, EngineError> {
        let obj = self.obj().await?;
        let (sql, params, cache) = (sql.to_string(), params.to_vec(), self.cache_statements);
        obj.interact(move |conn| sql_execute(conn, &sql, params, cache))
            .await
            .map_err(map_interact)?
    }

    async fn fetch_all(&self, sql: &str, params: &[Value]) -> Result<Vec<Row>, EngineError> {
        let obj = self.obj().await?;
        let (sql, params, cache) = (sql.to_string(), params.to_vec(), self.cache_statements);
        let (meta, rows) = obj
            .interact(move |conn| sql_fetch_rows(conn, &sql, params, true, cache))
            .await
            .map_err(map_interact)??;
        Ok(to_named(&meta, rows))
    }

    async fn fetch_all_values(
        &self,
        sql: &str,
        params: &[Value],
    ) -> Result<Vec<Vec<Value>>, EngineError> {
        let obj = self.obj().await?;
        let (sql, params, cache) = (sql.to_string(), params.to_vec(), self.cache_statements);
        let (_, rows) = obj
            .interact(move |conn| sql_fetch_rows(conn, &sql, params, false, cache))
            .await
            .map_err(map_interact)??;
        Ok(rows)
    }

    async fn execute_many(
        &self,
        sql: &str,
        rows: &[Vec<Value>],
    ) -> Result<Vec<Row>, EngineError> {
        let obj = self.obj().await?;
        let (sql, rows, cache) = (sql.to_string(), rows.to_vec(), self.cache_statements);
        obj.interact(move |conn| sql_execute_many(conn, &sql, rows, cache))
            .await
            .map_err(map_interact)?
    }

    fn dialect(&self) -> &'static str {
        "sqlite"
    }

    async fn close(&self) {
        self.pool.close();
    }

    async fn begin_tx(&self, _isolation: Option<&str>) -> Result<Box<dyn TxConn>, EngineError> {
        // SQLite transactions are serializable; the Python layer rejects any
        // other requested level, so the isolation hint is ignored here.
        let obj = self.obj().await?;
        obj.interact(|conn| conn.execute_batch("BEGIN"))
            .await
            .map_err(map_interact)?
            .map_err(map_interact)?;
        Ok(Box::new(SqliteTx {
            obj,
            cache_statements: self.cache_statements,
        }))
    }
}

// --- transaction -----------------------------------------------------------

struct SqliteTx {
    obj: Object,
    cache_statements: bool,
}

#[async_trait]
impl TxConn for SqliteTx {
    async fn execute(&self, sql: &str, params: &[Value]) -> Result<u64, EngineError> {
        let (sql, params, cache) = (sql.to_string(), params.to_vec(), self.cache_statements);
        self.obj
            .interact(move |conn| sql_execute(conn, &sql, params, cache))
            .await
            .map_err(map_interact)?
    }

    async fn fetch_all(&self, sql: &str, params: &[Value]) -> Result<Vec<Row>, EngineError> {
        let (sql, params, cache) = (sql.to_string(), params.to_vec(), self.cache_statements);
        let (meta, rows) = self
            .obj
            .interact(move |conn| sql_fetch_rows(conn, &sql, params, true, cache))
            .await
            .map_err(map_interact)??;
        Ok(to_named(&meta, rows))
    }

    async fn fetch_all_values(
        &self,
        sql: &str,
        params: &[Value],
    ) -> Result<Vec<Vec<Value>>, EngineError> {
        let (sql, params, cache) = (sql.to_string(), params.to_vec(), self.cache_statements);
        let (_, rows) = self
            .obj
            .interact(move |conn| sql_fetch_rows(conn, &sql, params, false, cache))
            .await
            .map_err(map_interact)??;
        Ok(rows)
    }

    async fn commit(self: Box<Self>) -> Result<(), EngineError> {
        self.obj
            .interact(|conn| conn.execute_batch("COMMIT"))
            .await
            .map_err(map_interact)?
            .map_err(map_interact)
    }

    async fn rollback(self: Box<Self>) -> Result<(), EngineError> {
        self.obj
            .interact(|conn| conn.execute_batch("ROLLBACK"))
            .await
            .map_err(map_interact)?
            .map_err(map_interact)
    }

    async fn savepoint(&self, name: &str) -> Result<(), EngineError> {
        let sql = format!("SAVEPOINT {name}");
        self.obj
            .interact(move |conn| conn.execute_batch(&sql))
            .await
            .map_err(map_interact)?
            .map_err(map_interact)
    }

    async fn release(&self, name: &str) -> Result<(), EngineError> {
        let sql = format!("RELEASE SAVEPOINT {name}");
        self.obj
            .interact(move |conn| conn.execute_batch(&sql))
            .await
            .map_err(map_interact)?
            .map_err(map_interact)
    }

    async fn rollback_to(&self, name: &str) -> Result<(), EngineError> {
        let sql = format!("ROLLBACK TO SAVEPOINT {name}");
        self.obj
            .interact(move |conn| conn.execute_batch(&sql))
            .await
            .map_err(map_interact)?
            .map_err(map_interact)
    }
}

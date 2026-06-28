//! Backend abstraction.
//!
//! Every supported database implements [`Backend`]. The Python-facing `Engine`
//! holds a `dyn Backend`, so adding MySQL or SQLite later is purely a matter of
//! providing another implementation and wiring it into [`connect`].

use async_trait::async_trait;

use crate::error::EngineError;
use crate::value::{Row, Value};

pub mod pool;
pub mod postgres;
pub mod sqlite;

#[async_trait]
pub trait Backend: Send + Sync {
    /// Run a statement that does not return rows; yields the affected row count.
    async fn execute(&self, sql: &str, params: &[Value]) -> Result<u64, EngineError>;

    /// Run a query and return every result row.
    async fn fetch_all(&self, sql: &str, params: &[Value]) -> Result<Vec<Row>, EngineError>;

    /// Like [`fetch_all`] but returns positional values (no column names),
    /// avoiding per-row name allocation and dict construction.
    async fn fetch_all_values(
        &self,
        sql: &str,
        params: &[Value],
    ) -> Result<Vec<Vec<Value>>, EngineError>;

    /// Run the same statement once per parameter set, pipelined on a single
    /// connection. Returns the first result row of each execution (empty when a
    /// statement returns nothing), preserving input order. This is the fast
    /// path for bulk inserts with `RETURNING`.
    async fn execute_many(
        &self,
        sql: &str,
        rows: &[Vec<Value>],
    ) -> Result<Vec<Row>, EngineError>;

    /// Short dialect identifier (e.g. `"postgres"`); the Python layer uses this
    /// to pick a SQL dialect for query generation.
    fn dialect(&self) -> &'static str;

    /// Release pooled connections.
    async fn close(&self);

    /// Begin a transaction on a pinned connection (BEGIN already issued).
    async fn begin_tx(&self) -> Result<Box<dyn TxConn>, EngineError>;
}

/// A transaction bound to a single connection. Statements run on that
/// connection until `commit`/`rollback` consume it and return it to the pool.
#[async_trait]
pub trait TxConn: Send + Sync {
    async fn execute(&self, sql: &str, params: &[Value]) -> Result<u64, EngineError>;
    async fn fetch_all(&self, sql: &str, params: &[Value]) -> Result<Vec<Row>, EngineError>;
    async fn fetch_all_values(
        &self,
        sql: &str,
        params: &[Value],
    ) -> Result<Vec<Vec<Value>>, EngineError>;
    async fn commit(self: Box<Self>) -> Result<(), EngineError>;
    async fn rollback(self: Box<Self>) -> Result<(), EngineError>;
}

/// Build the appropriate backend for a connection URL.
///
/// This is the single extension point: match a new scheme here and return the
/// matching backend implementation.
pub async fn connect(url: &str) -> Result<Box<dyn Backend>, EngineError> {
    if url.starts_with("postgres://") || url.starts_with("postgresql://") {
        let backend = postgres::PgBackend::connect(url).await?;
        Ok(Box::new(backend))
    } else if url.starts_with("sqlite:") {
        let backend = sqlite::SqliteBackend::connect(url).await?;
        Ok(Box::new(backend))
    } else {
        Err(EngineError::UnsupportedUrl(url.to_string()))
    }
}

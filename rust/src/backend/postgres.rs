//! PostgreSQL backend built on tokio-postgres + deadpool connection pooling.

use async_trait::async_trait;
use deadpool_postgres::{Manager, ManagerConfig, Pool, RecyclingMethod};
use tokio_postgres::types::ToSql;
use tokio_postgres::NoTls;

use crate::backend::{Backend, TxConn};
use crate::error::EngineError;
use crate::value::{decode_pg_row, decode_pg_row_values, Row, Value};

pub struct PgBackend {
    pool: Pool,
}

impl PgBackend {
    pub async fn connect(url: &str) -> Result<Self, EngineError> {
        let pg_config: tokio_postgres::Config = url
            .parse()
            .map_err(|e: tokio_postgres::Error| EngineError::Config(e.to_string()))?;

        let mgr_config = ManagerConfig {
            recycling_method: RecyclingMethod::Fast,
        };
        let mgr = Manager::from_config(pg_config, NoTls, mgr_config);
        let pool = Pool::builder(mgr)
            .max_size(16)
            .build()
            .map_err(|e| EngineError::Config(e.to_string()))?;

        // Fail fast if the database is unreachable / credentials are wrong.
        let client = pool
            .get()
            .await
            .map_err(|e| EngineError::Connection(e.to_string()))?;
        client
            .simple_query("SELECT 1")
            .await
            .map_err(|e| EngineError::Connection(e.to_string()))?;

        Ok(Self { pool })
    }
}

fn as_sql_params(params: &[Value]) -> Vec<&(dyn ToSql + Sync)> {
    params.iter().map(|v| v as &(dyn ToSql + Sync)).collect()
}

#[async_trait]
impl Backend for PgBackend {
    async fn execute(&self, sql: &str, params: &[Value]) -> Result<u64, EngineError> {
        let client = self
            .pool
            .get()
            .await
            .map_err(|e| EngineError::Connection(e.to_string()))?;
        // Cache the prepared statement on the connection so repeated calls with
        // the same SQL skip the parse/plan round-trip.
        let stmt = client.prepare_cached(sql).await?;
        let bound = as_sql_params(params);
        let affected = client.execute(&stmt, &bound).await?;
        Ok(affected)
    }

    async fn fetch_all(&self, sql: &str, params: &[Value]) -> Result<Vec<Row>, EngineError> {
        let client = self
            .pool
            .get()
            .await
            .map_err(|e| EngineError::Connection(e.to_string()))?;
        let stmt = client.prepare_cached(sql).await?;
        let bound = as_sql_params(params);
        let rows = client.query(&stmt, &bound).await?;
        Ok(rows.iter().map(decode_pg_row).collect())
    }

    async fn fetch_all_values(
        &self,
        sql: &str,
        params: &[Value],
    ) -> Result<Vec<Vec<Value>>, EngineError> {
        let client = self
            .pool
            .get()
            .await
            .map_err(|e| EngineError::Connection(e.to_string()))?;
        let stmt = client.prepare_cached(sql).await?;
        let bound = as_sql_params(params);
        let rows = client.query(&stmt, &bound).await?;
        Ok(rows.iter().map(decode_pg_row_values).collect())
    }

    async fn execute_many(
        &self,
        sql: &str,
        rows: &[Vec<Value>],
    ) -> Result<Vec<Row>, EngineError> {
        let client = self
            .pool
            .get()
            .await
            .map_err(|e| EngineError::Connection(e.to_string()))?;
        let stmt = client.prepare_cached(sql).await?;

        // Bind every row up front so the borrows live across the pipelined await.
        let bounds: Vec<Vec<&(dyn ToSql + Sync)>> = rows.iter().map(|r| as_sql_params(r)).collect();

        // Fire all queries on one connection; tokio-postgres pipelines them, so
        // the server processes the batch with a single network round-trip-ish
        // flush instead of one per row.
        let futures = bounds.iter().map(|bound| client.query(&stmt, bound));
        let results = futures_util::future::try_join_all(futures).await?;

        Ok(results
            .into_iter()
            .map(|rows| rows.first().map(decode_pg_row).unwrap_or_default())
            .collect())
    }

    fn dialect(&self) -> &'static str {
        "postgres"
    }

    async fn close(&self) {
        self.pool.close();
    }

    async fn begin_tx(&self) -> Result<Box<dyn TxConn>, EngineError> {
        let client = self
            .pool
            .get()
            .await
            .map_err(|e| EngineError::Connection(e.to_string()))?;
        client.batch_execute("BEGIN").await?;
        Ok(Box::new(PgTx { client }))
    }
}

/// A pinned-connection PostgreSQL transaction.
struct PgTx {
    client: deadpool_postgres::Object,
}

#[async_trait]
impl TxConn for PgTx {
    async fn execute(&self, sql: &str, params: &[Value]) -> Result<u64, EngineError> {
        let stmt = self.client.prepare_cached(sql).await?;
        let bound = as_sql_params(params);
        Ok(self.client.execute(&stmt, &bound).await?)
    }

    async fn fetch_all(&self, sql: &str, params: &[Value]) -> Result<Vec<Row>, EngineError> {
        let stmt = self.client.prepare_cached(sql).await?;
        let bound = as_sql_params(params);
        let rows = self.client.query(&stmt, &bound).await?;
        Ok(rows.iter().map(decode_pg_row).collect())
    }

    async fn fetch_all_values(
        &self,
        sql: &str,
        params: &[Value],
    ) -> Result<Vec<Vec<Value>>, EngineError> {
        let stmt = self.client.prepare_cached(sql).await?;
        let bound = as_sql_params(params);
        let rows = self.client.query(&stmt, &bound).await?;
        Ok(rows.iter().map(decode_pg_row_values).collect())
    }

    async fn commit(self: Box<Self>) -> Result<(), EngineError> {
        self.client.batch_execute("COMMIT").await?;
        Ok(())
    }

    async fn rollback(self: Box<Self>) -> Result<(), EngineError> {
        self.client.batch_execute("ROLLBACK").await?;
        Ok(())
    }
}

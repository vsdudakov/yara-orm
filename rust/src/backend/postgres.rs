//! PostgreSQL backend built on tokio-postgres + deadpool connection pooling.

use async_trait::async_trait;
use deadpool_postgres::{Hook, HookError, Manager, ManagerConfig, Object, Pool, RecyclingMethod};
use tokio_postgres::types::ToSql;
use tokio_postgres::{NoTls, Statement};

use crate::backend::pool::extract_pool_params;
use crate::backend::{Backend, TxConn};
use crate::error::EngineError;
use crate::value::{decode_pg_row, decode_pg_row_values, Row, Value};

/// Default pool size when the URL does not specify `max_size`.
const DEFAULT_MAX_SIZE: usize = 16;

pub struct PgBackend {
    pool: Pool,
    /// When false (URL `statement_cache_size=0`), prepared statements are not
    /// cached per connection — required behind a transaction-pooling proxy
    /// such as PgBouncer, which would otherwise see stale prepared statements.
    cache_statements: bool,
}

/// Prepare `sql`, honouring the backend's statement-cache setting.
async fn prepare(client: &Object, sql: &str, cache: bool) -> Result<Statement, EngineError> {
    Ok(if cache {
        client.prepare_cached(sql).await?
    } else {
        client.prepare(sql).await?
    })
}

impl PgBackend {
    pub async fn connect(url: &str) -> Result<Self, EngineError> {
        let (clean_url, params) = extract_pool_params(url)?;
        let pg_config: tokio_postgres::Config = clean_url
            .parse()
            .map_err(|e: tokio_postgres::Error| EngineError::Config(e.to_string()))?;

        let mgr_config = ManagerConfig {
            recycling_method: RecyclingMethod::Fast,
        };
        let mgr = Manager::from_config(pg_config, NoTls, mgr_config);
        let max_size = params.max_size.unwrap_or(DEFAULT_MAX_SIZE);
        let pool = Pool::builder(mgr)
            .max_size(max_size)
            // Pin every connection to UTC. The engine stores and returns all
            // timestamps in UTC, so a non-UTC server `TimeZone` would otherwise
            // make `timestamptz` extraction (EXTRACT(HOUR FROM ...)) and
            // CURRENT_TIMESTAMP depend on the server's locale. Applied per
            // connection so lazily-created ones are covered too.
            .post_create(Hook::async_fn(|client, _| {
                Box::pin(async move {
                    client
                        .batch_execute("SET TIME ZONE 'UTC'")
                        .await
                        .map_err(HookError::Backend)?;
                    Ok(())
                })
            }))
            .build()
            .map_err(|e| EngineError::Config(e.to_string()))?;

        // Pre-warm connections: always at least one (so we fail fast on an
        // unreachable database / bad credentials), and up to `min_size` so the
        // first real queries don't each pay connection latency. The pool keeps
        // no hard minimum, so this is a best-effort prime of idle connections.
        let warm = params.min_size.unwrap_or(0).max(1).min(max_size);
        let mut held = Vec::with_capacity(warm);
        for _ in 0..warm {
            held.push(
                pool.get()
                    .await
                    .map_err(|e| EngineError::Connection(e.to_string()))?,
            );
        }
        held[0]
            .simple_query("SELECT 1")
            .await
            .map_err(|e| EngineError::Connection(e.to_string()))?;
        drop(held); // return the warmed connections to the pool as idle

        Ok(Self {
            pool,
            cache_statements: params.cache_statements,
        })
    }

    async fn get(&self) -> Result<Object, EngineError> {
        self.pool
            .get()
            .await
            .map_err(|e| EngineError::Connection(e.to_string()))
    }
}

fn as_sql_params(params: &[Value]) -> Vec<&(dyn ToSql + Sync)> {
    params.iter().map(|v| v as &(dyn ToSql + Sync)).collect()
}

#[async_trait]
impl Backend for PgBackend {
    async fn execute(&self, sql: &str, params: &[Value]) -> Result<u64, EngineError> {
        let client = self.get().await?;
        let stmt = prepare(&client, sql, self.cache_statements).await?;
        let bound = as_sql_params(params);
        let affected = client.execute(&stmt, &bound).await?;
        Ok(affected)
    }

    async fn fetch_all(&self, sql: &str, params: &[Value]) -> Result<Vec<Row>, EngineError> {
        let client = self.get().await?;
        let stmt = prepare(&client, sql, self.cache_statements).await?;
        let bound = as_sql_params(params);
        let rows = client.query(&stmt, &bound).await?;
        rows.iter().map(decode_pg_row).collect()
    }

    async fn fetch_all_values(
        &self,
        sql: &str,
        params: &[Value],
    ) -> Result<Vec<Vec<Value>>, EngineError> {
        let client = self.get().await?;
        let stmt = prepare(&client, sql, self.cache_statements).await?;
        let bound = as_sql_params(params);
        let rows = client.query(&stmt, &bound).await?;
        rows.iter().map(decode_pg_row_values).collect()
    }

    async fn execute_many(
        &self,
        sql: &str,
        rows: &[Vec<Value>],
    ) -> Result<Vec<Row>, EngineError> {
        let client = self.get().await?;
        let stmt = prepare(&client, sql, self.cache_statements).await?;

        // Bind every row up front so the borrows live across the pipelined await.
        let bounds: Vec<Vec<&(dyn ToSql + Sync)>> = rows.iter().map(|r| as_sql_params(r)).collect();

        // Fire all queries on one connection; tokio-postgres pipelines them, so
        // the server processes the batch with a single network round-trip-ish
        // flush instead of one per row.
        let futures = bounds.iter().map(|bound| client.query(&stmt, bound));
        let results = futures_util::future::try_join_all(futures).await?;

        results
            .into_iter()
            .map(|rows| match rows.first() {
                Some(r) => decode_pg_row(r),
                None => Ok(Row::new()),
            })
            .collect()
    }

    fn dialect(&self) -> &'static str {
        "postgres"
    }

    async fn close(&self) {
        self.pool.close();
    }

    async fn begin_tx(&self, isolation: Option<&str>) -> Result<Box<dyn TxConn>, EngineError> {
        let client = self.get().await?;
        // Apply the isolation level atomically at BEGIN (it must precede any
        // query in the transaction). The level is validated by the Python layer.
        let begin = match isolation {
            Some(level) => format!("BEGIN ISOLATION LEVEL {level}"),
            None => "BEGIN".to_string(),
        };
        client.batch_execute(&begin).await?;
        Ok(Box::new(PgTx {
            client,
            cache_statements: self.cache_statements,
        }))
    }
}

/// A pinned-connection PostgreSQL transaction.
struct PgTx {
    client: Object,
    cache_statements: bool,
}

#[async_trait]
impl TxConn for PgTx {
    async fn execute(&self, sql: &str, params: &[Value]) -> Result<u64, EngineError> {
        let stmt = prepare(&self.client, sql, self.cache_statements).await?;
        let bound = as_sql_params(params);
        Ok(self.client.execute(&stmt, &bound).await?)
    }

    async fn fetch_all(&self, sql: &str, params: &[Value]) -> Result<Vec<Row>, EngineError> {
        let stmt = prepare(&self.client, sql, self.cache_statements).await?;
        let bound = as_sql_params(params);
        let rows = self.client.query(&stmt, &bound).await?;
        rows.iter().map(decode_pg_row).collect()
    }

    async fn fetch_all_values(
        &self,
        sql: &str,
        params: &[Value],
    ) -> Result<Vec<Vec<Value>>, EngineError> {
        let stmt = prepare(&self.client, sql, self.cache_statements).await?;
        let bound = as_sql_params(params);
        let rows = self.client.query(&stmt, &bound).await?;
        rows.iter().map(decode_pg_row_values).collect()
    }

    async fn commit(self: Box<Self>) -> Result<(), EngineError> {
        self.client.batch_execute("COMMIT").await?;
        Ok(())
    }

    async fn rollback(self: Box<Self>) -> Result<(), EngineError> {
        self.client.batch_execute("ROLLBACK").await?;
        Ok(())
    }

    async fn savepoint(&self, name: &str) -> Result<(), EngineError> {
        self.client.batch_execute(&format!("SAVEPOINT {name}")).await?;
        Ok(())
    }

    async fn release(&self, name: &str) -> Result<(), EngineError> {
        self.client
            .batch_execute(&format!("RELEASE SAVEPOINT {name}"))
            .await?;
        Ok(())
    }

    async fn rollback_to(&self, name: &str) -> Result<(), EngineError> {
        self.client
            .batch_execute(&format!("ROLLBACK TO SAVEPOINT {name}"))
            .await?;
        Ok(())
    }
}

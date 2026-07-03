//! PostgreSQL backend built on tokio-postgres + deadpool connection pooling.

use std::sync::Arc;

use async_trait::async_trait;
use deadpool_postgres::{Hook, HookError, Manager, ManagerConfig, Object, Pool, RecyclingMethod};
use tokio_postgres::config::SslMode;
use tokio_postgres::types::{Kind, ToSql, Type};
use tokio_postgres::{NoTls, Statement};
use tokio_postgres_rustls::MakeRustlsConnect;

use crate::backend::pool::extract_pool_params;
use crate::backend::{Backend, TxConn};
use crate::error::EngineError;
use crate::value::{decode_pg_row, decode_pg_row_values, Row, Value};

/// Default pool size when the URL does not specify `max_size`.
const DEFAULT_MAX_SIZE: usize = 16;

/// Build a rustls TLS connector: server certs are verified against the OS trust
/// store, using the pure-Rust `ring` crypto provider (no system OpenSSL).
fn make_tls_connector() -> Result<MakeRustlsConnect, EngineError> {
    let mut roots = rustls::RootCertStore::empty();
    for cert in rustls_native_certs::load_native_certs().certs {
        let _ = roots.add(cert); // ignore individual malformed OS certs
    }
    let config = rustls::ClientConfig::builder_with_provider(Arc::new(
        rustls::crypto::ring::default_provider(),
    ))
    .with_safe_default_protocol_versions()
    .map_err(|e| EngineError::Config(e.to_string()))?
    .with_root_certificates(roots)
    .with_no_client_auth();
    Ok(MakeRustlsConnect::new(config))
}

/// The password embedded in a `scheme://user:password@host...` URL, if present.
pub(crate) fn url_password(url: &str) -> Option<&str> {
    let authority = url.split_once("://")?.1.split(['/', '?']).next()?;
    let userinfo = authority.rsplit_once('@')?.0;
    match userinfo.split_once(':') {
        Some((_, pass)) if !pass.is_empty() => Some(pass),
        _ => None,
    }
}

/// Redact the connection URL's password (if any) from an error message, so a
/// driver/config error surfaced to Python cannot leak the credential.
/// Shared with the MySQL backend (URL layout is identical for this purpose).
pub(crate) fn redact(msg: String, url: &str) -> String {
    match url_password(url) {
        Some(pw) => msg.replace(pw, "***"),
        None => msg,
    }
}

pub struct PgBackend {
    pool: Pool,
    /// When false (URL `statement_cache_size=0`), prepared statements are not
    /// cached per connection — required behind a transaction-pooling proxy
    /// such as PgBouncer, which would otherwise see stale prepared statements.
    cache_statements: bool,
}

/// The unspecified parameter type (OID 0): tells the server to infer *this*
/// parameter's type from context, per the Parse-message protocol. Used for
/// values with no definite type here (arrays, JSON, NULL).
fn unspecified_type() -> Type {
    Type::new(String::new(), 0, Kind::Simple, String::new())
}

/// Prepare `sql`, declaring each parameter's type from its Python value (so the
/// server doesn't mis-infer it from context — e.g. a `float` compared to an
/// `int` column). A value with no definite type here (array/JSON/NULL) is given
/// OID 0 so the server infers *that one* from context, without dropping the
/// declared types of the others — otherwise a `::uuid`-cast text param mixed
/// with an array would be re-inferred as `uuid` and mis-encoded (22P03).
async fn prepare_for(
    client: &Object,
    sql: &str,
    params: &[Value],
    cache: bool,
) -> Result<Statement, EngineError> {
    let types: Vec<Type> = params
        .iter()
        .map(|v| v.pg_type().unwrap_or_else(unspecified_type))
        .collect();
    Ok(if cache {
        client.prepare_typed_cached(sql, &types).await?
    } else {
        client.prepare_typed(sql, &types).await?
    })
}

impl PgBackend {
    pub async fn connect(url: &str) -> Result<Self, EngineError> {
        let (clean_url, params) = extract_pool_params(url)?;
        let pg_config: tokio_postgres::Config = clean_url
            .parse()
            .map_err(|e: tokio_postgres::Error| EngineError::Config(redact(e.to_string(), url)))?;

        let mgr_config = ManagerConfig {
            recycling_method: RecyclingMethod::Fast,
        };
        // Honour `sslmode` with a real (rustls) TLS connector: `require`/
        // `verify-ca`/`verify-full` actually encrypt and verify the cert against
        // the OS trust store instead of silently running in plaintext. `disable`
        // opts out; `prefer` (the libpq default) attempts TLS and falls back to
        // plaintext when the server has no SSL.
        let mgr = if pg_config.get_ssl_mode() == SslMode::Disable {
            Manager::from_config(pg_config, NoTls, mgr_config)
        } else {
            Manager::from_config(pg_config, make_tls_connector()?, mgr_config)
        };
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
            .map_err(|e| EngineError::Config(redact(e.to_string(), url)))?;

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
                    .map_err(|e| EngineError::Connection(redact(e.to_string(), url)))?,
            );
        }
        held[0]
            .simple_query("SELECT 1")
            .await
            .map_err(|e| EngineError::Connection(redact(e.to_string(), url)))?;
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
        let stmt = prepare_for(&client, sql, params, self.cache_statements).await?;
        let bound = as_sql_params(params);
        let affected = client.execute(&stmt, &bound).await?;
        Ok(affected)
    }

    async fn fetch_all(&self, sql: &str, params: &[Value]) -> Result<Vec<Row>, EngineError> {
        let client = self.get().await?;
        let stmt = prepare_for(&client, sql, params, self.cache_statements).await?;
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
        let stmt = prepare_for(&client, sql, params, self.cache_statements).await?;
        let bound = as_sql_params(params);
        let rows = client.query(&stmt, &bound).await?;
        rows.iter().map(decode_pg_row_values).collect()
    }

    async fn execute_many(&self, sql: &str, rows: &[Vec<Value>]) -> Result<Vec<Row>, EngineError> {
        let Some(first) = rows.first() else {
            return Ok(Vec::new());
        };
        // Run the batch inside one transaction so a mid-batch failure applies
        // nothing (all-or-nothing), and the guard's drop-safety keeps a
        // cancelled batch from recycling a mid-transaction connection.
        let tx = PgTx::begin(self.get().await?, None, self.cache_statements).await?;

        let result: Result<Vec<Row>, EngineError> = async {
            // Declare parameter types from the first row's values, matching the
            // single-statement paths (`prepare_for`), so the server doesn't
            // mis-infer e.g. a float param compared to an int column.
            let stmt = prepare_for(tx.client(), sql, first, self.cache_statements).await?;

            // Bind every row up front so the borrows live across the pipelined
            // await.
            let bounds: Vec<Vec<&(dyn ToSql + Sync)>> =
                rows.iter().map(|r| as_sql_params(r)).collect();

            // Fire all queries on one connection; tokio-postgres pipelines
            // them, so the server processes the batch with a single network
            // round-trip-ish flush instead of one per row.
            let futures = bounds.iter().map(|bound| tx.client().query(&stmt, bound));
            let results = futures_util::future::try_join_all(futures).await?;

            results
                .into_iter()
                .map(|rows| match rows.first() {
                    Some(r) => decode_pg_row(r),
                    None => Ok(Row::new()),
                })
                .collect()
        }
        .await;

        match result {
            Ok(out) => {
                Box::new(tx).commit().await?;
                Ok(out)
            }
            Err(e) => {
                let _ = Box::new(tx).rollback().await;
                Err(e)
            }
        }
    }

    async fn execute_script(&self, statements: &[String]) -> Result<(), EngineError> {
        let client = self.get().await?;
        let mut result = Ok(());
        for statement in statements {
            // The simple protocol accepts any statement (VACUUM, CREATE INDEX
            // CONCURRENTLY, ...) and runs it in autocommit — no wrapping
            // transaction, matching per-statement `execute` semantics.
            if let Err(e) = client.batch_execute(statement).await {
                result = Err(EngineError::from(e));
                break;
            }
        }
        // Safety net: a script that opened a transaction and failed (or forgot
        // COMMIT) must not hand a mid-transaction connection back to the pool.
        // Outside a transaction this is a harmless no-op warning.
        let _ = client.batch_execute("ROLLBACK").await;
        result
    }

    fn dialect(&self) -> &'static str {
        "postgres"
    }

    async fn close(&self) {
        self.pool.close();
    }

    async fn begin_tx(&self, isolation: Option<&str>) -> Result<Box<dyn TxConn>, EngineError> {
        let tx = PgTx::begin(self.get().await?, isolation, self.cache_statements).await?;
        Ok(Box::new(tx))
    }
}

/// Lifecycle of a pinned-connection transaction, driving the drop guard.
#[derive(Clone, Copy, PartialEq, Eq)]
enum TxState {
    /// A transaction is (or may be) open on the connection.
    Active,
    /// COMMIT/ROLLBACK completed cleanly; the connection is safe to recycle.
    Finished,
    /// A control statement failed; the connection state is unknown.
    Broken,
}

/// A pinned-connection PostgreSQL transaction.
///
/// The connection is *guarded*: it only returns to the pool after a clean
/// COMMIT/ROLLBACK. If the transaction is dropped in any other state — e.g.
/// the driving asyncio task was cancelled between taking the transaction and
/// the COMMIT completing — the drop guard tries a best-effort ROLLBACK on the
/// background runtime, and detaches + closes the connection when even that
/// fails. Deadpool's `RecyclingMethod::Fast` performs no reset, so returning a
/// mid-transaction connection would silently corrupt the next consumer.
struct PgTx {
    client: Option<Object>,
    cache_statements: bool,
    state: TxState,
}

impl PgTx {
    /// Acquire-and-BEGIN, with the drop guard armed *before* BEGIN is sent so a
    /// cancellation mid-BEGIN can never recycle a possibly-in-transaction
    /// connection.
    async fn begin(
        client: Object,
        isolation: Option<&str>,
        cache_statements: bool,
    ) -> Result<Self, EngineError> {
        // Apply the isolation level atomically at BEGIN (it must precede any
        // query in the transaction). The level is validated by the Python layer.
        let begin = match isolation {
            Some(level) => format!("BEGIN ISOLATION LEVEL {level}"),
            None => "BEGIN".to_string(),
        };
        let tx = PgTx {
            client: Some(client),
            cache_statements,
            state: TxState::Active,
        };
        tx.client().batch_execute(&begin).await?;
        Ok(tx)
    }

    fn client(&self) -> &Object {
        self.client
            .as_ref()
            .expect("PgTx client is present until drop")
    }

    async fn control(mut self: Box<Self>, sql: &str) -> Result<(), EngineError> {
        match self.client().batch_execute(sql).await {
            Ok(()) => {
                self.state = TxState::Finished;
                Ok(())
            }
            Err(e) => {
                self.state = TxState::Broken;
                Err(e.into())
            }
        }
    }
}

impl Drop for PgTx {
    fn drop(&mut self) {
        let Some(client) = self.client.take() else {
            return;
        };
        match self.state {
            // Clean end: dropping the Object recycles the connection normally.
            TxState::Finished => drop(client),
            // A COMMIT/ROLLBACK failed outright: the session state is unknown,
            // so take the connection out of the pool and close it (the pool
            // replaces it lazily).
            TxState::Broken => drop(Object::take(client)),
            // Dropped mid-transaction (cancellation windows around BEGIN /
            // COMMIT / ROLLBACK, or an abandoned transaction object): try a
            // ROLLBACK on the background runtime — ROLLBACK outside a
            // transaction is a harmless no-op warning, so this also covers
            // "the cancelled COMMIT actually completed server-side". Only if
            // the ROLLBACK itself fails is the connection closed.
            TxState::Active => {
                pyo3_async_runtimes::tokio::get_runtime().spawn(async move {
                    if client.batch_execute("ROLLBACK").await.is_err() {
                        drop(Object::take(client));
                    }
                });
            }
        }
    }
}

#[async_trait]
impl TxConn for PgTx {
    async fn execute(&self, sql: &str, params: &[Value]) -> Result<u64, EngineError> {
        let stmt = prepare_for(self.client(), sql, params, self.cache_statements).await?;
        let bound = as_sql_params(params);
        Ok(self.client().execute(&stmt, &bound).await?)
    }

    async fn fetch_all(&self, sql: &str, params: &[Value]) -> Result<Vec<Row>, EngineError> {
        let stmt = prepare_for(self.client(), sql, params, self.cache_statements).await?;
        let bound = as_sql_params(params);
        let rows = self.client().query(&stmt, &bound).await?;
        rows.iter().map(decode_pg_row).collect()
    }

    async fn fetch_all_values(
        &self,
        sql: &str,
        params: &[Value],
    ) -> Result<Vec<Vec<Value>>, EngineError> {
        let stmt = prepare_for(self.client(), sql, params, self.cache_statements).await?;
        let bound = as_sql_params(params);
        let rows = self.client().query(&stmt, &bound).await?;
        rows.iter().map(decode_pg_row_values).collect()
    }

    async fn commit(self: Box<Self>) -> Result<(), EngineError> {
        self.control("COMMIT").await
    }

    async fn rollback(self: Box<Self>) -> Result<(), EngineError> {
        self.control("ROLLBACK").await
    }

    async fn savepoint(&self, name: &str) -> Result<(), EngineError> {
        self.client()
            .batch_execute(&format!("SAVEPOINT {name}"))
            .await?;
        Ok(())
    }

    async fn release(&self, name: &str) -> Result<(), EngineError> {
        self.client()
            .batch_execute(&format!("RELEASE SAVEPOINT {name}"))
            .await?;
        Ok(())
    }

    async fn rollback_to(&self, name: &str) -> Result<(), EngineError> {
        self.client()
            .batch_execute(&format!("ROLLBACK TO SAVEPOINT {name}"))
            .await?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::{redact, url_password};

    #[test]
    fn url_password_is_extracted_only_when_present() {
        assert_eq!(url_password("postgres://u:pw@h/db"), Some("pw"));
        assert_eq!(
            url_password("postgres://u:pw@h:5432/db?sslmode=require"),
            Some("pw")
        );
        // Password may itself contain a colon; the userinfo splits on the first.
        assert_eq!(url_password("postgres://u:a:b@h/db"), Some("a:b"));
        assert_eq!(url_password("postgres://u@h/db"), None); // user, no password
        assert_eq!(url_password("postgres://h/db"), None); // no userinfo
    }

    #[test]
    fn redact_hides_the_password_in_messages() {
        let url = "postgres://u:sup3rsecret@h/db";
        let msg = "auth failed for postgres://u:sup3rsecret@h/db".to_string();
        let out = redact(msg, url);
        assert!(!out.contains("sup3rsecret"));
        assert!(out.contains("***"));
        // Nothing to redact when the URL carries no password.
        assert_eq!(redact("boom".to_string(), "postgres://h/db"), "boom");
    }
}

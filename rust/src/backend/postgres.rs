//! PostgreSQL backend built on tokio-postgres + deadpool connection pooling.

use std::sync::{Arc, OnceLock};

use async_trait::async_trait;
use deadpool_postgres::{Hook, HookError, Manager, ManagerConfig, Object, Pool, RecyclingMethod};
use tokio_postgres::config::SslMode;
use tokio_postgres::types::{Kind, ToSql, Type};
use tokio_postgres::{NoTls, Statement};
use tokio_postgres_rustls::MakeRustlsConnect;

use crate::backend::pool::extract_pool_params;
use crate::backend::{Backend, TxConn, TxState};
use crate::error::EngineError;
use crate::value::{decode_pg_row, decode_pg_row_values, decode_pg_rows, Row, Value};

/// Default pool size when the URL does not specify `max_size`.
const DEFAULT_MAX_SIZE: usize = 16;

/// The process-wide TLS connector, built once on first use.
///
/// Constructing it parses the entire OS trust store
/// (`rustls_native_certs::load_native_certs`), which reads and decodes ~100–150
/// system CA certificates — tens of milliseconds. It holds only an
/// `Arc<ClientConfig>`, so it is cached here and cloned (a refcount bump) for
/// every connection instead of being rebuilt per `connect()` — otherwise each
/// `YaraOrm.init()` re-parsed the whole trust store (measured ~95ms of the
/// ~100ms per-init cost, since tokio-postgres defaults to `sslmode=prefer` and
/// so takes the TLS path even for a plaintext localhost URL).
static TLS_CONNECTOR: OnceLock<MakeRustlsConnect> = OnceLock::new();

/// Return the shared rustls TLS connector, building it once (see
/// [`TLS_CONNECTOR`]) and cloning it thereafter.
fn make_tls_connector() -> Result<MakeRustlsConnect, EngineError> {
    if let Some(connector) = TLS_CONNECTOR.get() {
        return Ok(connector.clone());
    }
    // Build outside the lock; on a first-connect race the loser's connector is
    // simply dropped and every caller clones the single cached one.
    let connector = build_tls_connector()?;
    Ok(TLS_CONNECTOR.get_or_init(|| connector).clone())
}

/// Build a rustls TLS connector: server certs are verified against the OS trust
/// store, using the pure-Rust `ring` crypto provider (no system OpenSSL).
fn build_tls_connector() -> Result<MakeRustlsConnect, EngineError> {
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
    prepare_typed(client, sql, &types, cache).await
}

/// Prepare `sql` with explicitly declared parameter types (cached or not).
async fn prepare_typed(
    client: &Object,
    sql: &str,
    types: &[Type],
    cache: bool,
) -> Result<Statement, EngineError> {
    Ok(if cache {
        client.prepare_typed_cached(sql, types).await?
    } else {
        client.prepare_typed(sql, types).await?
    })
}

/// The common declared type two differing scalar parameter types can both be
/// encoded into without corruption. Any differing numeric pair unifies to
/// NUMERIC — `Value::to_sql` encodes both Int and (finite) Float into NUMERIC
/// exactly, whereas widening INT8 to FLOAT8 would silently round integers
/// above 2^53. Timestamp/TimestampTz share the identical 8-byte wire format
/// (TIMESTAMPTZ keeps the aware rows' instant semantics), and TEXT unifies
/// with UUID via the str-to-uuid coercion arm. Any other pair has no safe
/// common encoding.
fn widen_pg_type(a: &Type, b: &Type) -> Option<Type> {
    match (a, b) {
        (&Type::TIMESTAMP, &Type::TIMESTAMPTZ) | (&Type::TIMESTAMPTZ, &Type::TIMESTAMP) => {
            return Some(Type::TIMESTAMPTZ)
        }
        (&Type::TEXT, &Type::UUID) | (&Type::UUID, &Type::TEXT) => return Some(Type::UUID),
        _ => {}
    }
    let numeric = |t: &Type| matches!(*t, Type::INT8 | Type::FLOAT8 | Type::NUMERIC);
    if numeric(a) && numeric(b) {
        return Some(Type::NUMERIC);
    }
    None
}

/// Declared parameter types for a batch, unified across *all* rows.
///
/// `execute_many` prepares its statement once, so deriving the types from the
/// first row alone (as the single-statement paths do per call) would make a
/// later row of a different value type encode into the wrong declared type —
/// e.g. rows `[[1], [2.5]]` would write row 2's f64 bytes into an
/// INT8-declared parameter, silently storing a huge bogus integer. Numeric
/// mixes unify to NUMERIC (exact for ints and finite floats), naive/aware
/// datetime mixes to TIMESTAMPTZ, and TEXT+UUID to UUID (matching
/// `Value::to_sql`'s coercion arms); values with no definite type (NULL/JSON/
/// array) don't vote; any other mix is rejected up front with a clear error.
fn unified_param_types(rows: &[Vec<Value>]) -> Result<Vec<Type>, EngineError> {
    let Some(first) = rows.first() else {
        return Ok(Vec::new());
    };
    let mut types: Vec<Option<Type>> = first.iter().map(Value::pg_type).collect();
    for row in &rows[1..] {
        // Row arity mismatches are the server's to reject at bind time; unify
        // only the parameters the prepared statement will declare.
        for (idx, value) in row.iter().enumerate().take(types.len()) {
            let Some(next) = value.pg_type() else {
                continue; // NULL/JSON/array: stays inferred from context
            };
            match &mut types[idx] {
                slot @ None => *slot = Some(next),
                Some(current) if *current == next => {}
                Some(current) => match widen_pg_type(current, &next) {
                    Some(widened) => *current = widened,
                    None => {
                        return Err(EngineError::Query(format!(
                            "execute_many parameter {} mixes incompatible types across rows \
                             ({} vs {}); bind a consistent type per column",
                            idx + 1,
                            current.name(),
                            next.name(),
                        )))
                    }
                },
            }
        }
    }
    Ok(types
        .into_iter()
        .map(|t| t.unwrap_or_else(unspecified_type))
        .collect())
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
        // Clamp to at least 1 (like the mysql/mssql/oracle backends): a
        // `?max_size=0` URL would otherwise build a zero-capacity pool, leaving
        // `held` empty and panicking on the `held[0]` pre-warm probe below.
        let max_size = params.max_size.unwrap_or(DEFAULT_MAX_SIZE).max(1);
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
        decode_pg_rows(&rows)
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
        if rows.is_empty() {
            return Ok(Vec::new());
        }
        // Declare parameter types unified across all rows (see
        // `unified_param_types`): the statement is prepared once, so a later
        // row's wider value type (Int in row 1, Float in row 2) must widen
        // the declared type instead of being mis-encoded into the first
        // row's.
        let types = unified_param_types(rows)?;
        // Run the batch inside one transaction so a mid-batch failure applies
        // nothing (all-or-nothing), and the guard's drop-safety keeps a
        // cancelled batch from recycling a mid-transaction connection.
        let tx = PgTx::begin(self.get().await?, None, self.cache_statements).await?;

        let result: Result<Vec<Row>, EngineError> = async {
            let stmt = prepare_typed(tx.client(), sql, &types, self.cache_statements).await?;

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
        decode_pg_rows(&rows)
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
    use super::{redact, unified_param_types, url_password, Type, Value};

    #[test]
    fn unified_types_widen_numeric_mixes_across_rows() {
        // Int in row 1, Float in row 2: the single declared type must unify to
        // NUMERIC — declaring INT8 (the old first-row behaviour) made row 2's
        // f64 bytes decode server-side as a huge bogus integer, and FLOAT8
        // would silently round integers above 2^53 (e.g. snowflake ids).
        let rows = vec![vec![Value::Int(1)], vec![Value::Float(2.5)]];
        assert_eq!(unified_param_types(&rows).unwrap(), vec![Type::NUMERIC]);
        // Widening is order-independent.
        let rows = vec![vec![Value::Float(2.5)], vec![Value::Int(1)]];
        assert_eq!(unified_param_types(&rows).unwrap(), vec![Type::NUMERIC]);
        // Anything numeric mixed with a Decimal widens to NUMERIC (exact).
        let rows = vec![
            vec![Value::Int(1)],
            vec![Value::Decimal(rust_decimal::Decimal::new(25, 1))],
            vec![Value::Float(0.5)],
        ];
        assert_eq!(unified_param_types(&rows).unwrap(), vec![Type::NUMERIC]);
    }

    #[test]
    fn unified_types_allow_datetime_and_uuid_text_mixes() {
        // Naive + aware datetimes share the 8-byte wire format; TIMESTAMPTZ
        // keeps the aware rows' instant semantics.
        let naive = chrono::NaiveDate::from_ymd_opt(2024, 5, 3)
            .unwrap()
            .and_hms_opt(0, 0, 0)
            .unwrap();
        let aware = chrono::DateTime::from_naive_utc_and_offset(naive, chrono::Utc);
        let rows = vec![
            vec![Value::Timestamp(naive)],
            vec![Value::TimestampTz(aware)],
        ];
        assert_eq!(unified_param_types(&rows).unwrap(), vec![Type::TIMESTAMPTZ]);
        let rows = vec![
            vec![Value::TimestampTz(aware)],
            vec![Value::Timestamp(naive)],
        ];
        assert_eq!(unified_param_types(&rows).unwrap(), vec![Type::TIMESTAMPTZ]);
        // A str mixed into a uuid column rides the str-to-uuid coercion arm.
        let u = uuid::Uuid::nil();
        let rows = vec![
            vec![Value::Uuid(u)],
            vec![Value::Text(u.to_string().into())],
        ];
        assert_eq!(unified_param_types(&rows).unwrap(), vec![Type::UUID]);
    }

    #[test]
    fn unified_types_keep_uniform_columns_and_skip_null_votes() {
        // Uniform columns keep their type; a NULL carries no type and must not
        // reset a column to "inferred", nor stop a later row from setting it.
        let rows = vec![
            vec![Value::Null, Value::Text("a".into())],
            vec![Value::Int(7), Value::Text("b".into())],
            vec![Value::Null, Value::Text("c".into())],
        ];
        let types = unified_param_types(&rows).unwrap();
        assert_eq!(types[0], Type::INT8);
        assert_eq!(types[1], Type::TEXT);
        // An all-NULL column stays server-inferred (OID 0).
        let rows = vec![vec![Value::Null], vec![Value::Null]];
        assert_eq!(unified_param_types(&rows).unwrap()[0].oid(), 0);
        // An empty batch declares nothing.
        assert!(unified_param_types(&[]).unwrap().is_empty());
    }

    #[test]
    fn unified_types_reject_mixes_with_no_safe_common_encoding() {
        // Text + Int has no common declared type both rows can encode into;
        // erroring up front beats writing mismatched raw bytes.
        let rows = vec![vec![Value::Text("a".into())], vec![Value::Int(1)]];
        let err = unified_param_types(&rows).unwrap_err().to_string();
        assert!(err.contains("parameter 1"), "{err}");
        assert!(err.contains("text") && err.contains("int8"), "{err}");
    }

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

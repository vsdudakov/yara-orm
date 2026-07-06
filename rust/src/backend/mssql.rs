//! Microsoft SQL Server backend built on tiberius (a pure-Rust TDS driver — no
//! ODBC / native client / Instant Client, so wheels stay self-contained),
//! pooled through a custom `deadpool` manager.
//!
//! # `OUTPUT` instead of `RETURNING`
//!
//! T-SQL spells the returning clause `INSERT ... OUTPUT INSERTED.[id] VALUES
//! (...)`, which produces a real result set. The [`SqlServerDialect`](crate)
//! renders it, and this backend runs such statements through the ordinary fetch
//! path, so the model layer's `fetch_row` contract holds unchanged (as on
//! PostgreSQL's native `RETURNING`).
//!
//! # Placeholders & types
//!
//! Parameters are `@P1`, `@P2`, ... (tiberius' positional style). Aware
//! datetimes are stored UTC-naive in `DATETIME2` (SQL Server's tz-aware
//! `DATETIMEOFFSET` is avoided for the same reason MySQL/SQLite avoid theirs);
//! the Python layer re-attaches UTC on read.
//!
//! # Transactions
//!
//! `BEGIN TRANSACTION` / `COMMIT` / `ROLLBACK`; savepoints use
//! `SAVE TRANSACTION name` and `ROLLBACK TRANSACTION name`. T-SQL has no
//! "release savepoint" (savepoints merge into the outer transaction on commit),
//! so `release` is a no-op.

use std::sync::Arc;

use async_trait::async_trait;
use chrono::{DateTime, NaiveDate, NaiveDateTime, NaiveTime, Utc};
use deadpool::managed::{Manager, Metrics, Object, Pool, RecycleResult};
use rust_decimal::Decimal;
use tiberius::{AuthMethod, ColumnData, Config, FromSql, IntoSql, ToSql};
use tokio::net::TcpStream;
use tokio_util::compat::{Compat, TokioAsyncWriteCompatExt};

use crate::backend::pool::extract_pool_params;
use crate::backend::postgres::redact;
use crate::backend::{Backend, TxConn};
use crate::error::EngineError;
use crate::value::{value_to_json, Row, Value};

/// Default pool size when the URL does not specify `max_size` (matches the
/// PostgreSQL/MySQL backends' default).
const DEFAULT_MAX_SIZE: usize = 16;

/// SQL Server error numbers that signal an integrity-constraint violation,
/// mapped to [`EngineError::Integrity`] so they reach Python as
/// `IntegrityError`: 2627 PK/unique constraint, 2601 duplicate unique index,
/// 547 FK / CHECK violation, 515 NOT NULL (NULL into a non-null column).
const INTEGRITY_CODES: &[u32] = &[2627, 2601, 547, 515];

type MssqlConn = tiberius::Client<Compat<TcpStream>>;

/// Map a tiberius error, promoting constraint violations to `Integrity`.
fn map_tds(e: tiberius::error::Error) -> EngineError {
    if let tiberius::error::Error::Server(ref token) = e {
        if INTEGRITY_CODES.contains(&token.code()) {
            return EngineError::Integrity(token.message().to_string());
        }
        return EngineError::Query(format!("{} (SQL Server error {})", token.message(), token.code()));
    }
    EngineError::Query(e.to_string())
}

// ---------------------------------------------------------------------------
// Parameter encoding: crate Value -> tiberius ColumnData (via ToSql)
// ---------------------------------------------------------------------------

/// Wraps a `&Value` so it can be passed as a tiberius bind parameter. Standard
/// scalar types delegate to tiberius' own `ToSql`; types SQL Server lacks
/// (uuid text is native `GUID`, json/array as text) are converted here.
struct Param<'a>(&'a Value);

impl ToSql for Param<'_> {
    fn to_sql(&self) -> ColumnData<'_> {
        match self.0 {
            // A typeless NULL: NVARCHAR NULL converts implicitly to any target.
            Value::Null => ColumnData::String(None),
            Value::Bool(b) => ColumnData::Bit(Some(*b)),
            Value::Int(i) => ColumnData::I64(Some(*i)),
            Value::Float(f) => ColumnData::F64(Some(*f)),
            Value::Text(s) => ColumnData::String(Some(s.as_str().into())),
            Value::Bytes(b) => ColumnData::Binary(Some(b.as_slice().into())),
            Value::Uuid(u) => ColumnData::Guid(Some(*u)),
            Value::Decimal(d) => d.to_sql(),
            Value::Timestamp(dt) => dt.to_sql(),
            // No tz-aware storage: canonicalise to UTC and store naive. The
            // naive value is a local temporary, so bind it *owned* via IntoSql.
            Value::TimestampTz(dt) => dt.naive_utc().into_sql(),
            Value::Date(d) => d.to_sql(),
            Value::Time(t) => t.to_sql(),
            // JSON is stored as NVARCHAR (SQL Server has JSON *functions*, not a
            // native JSON type); arrays likewise, mirroring MySQL/SQLite.
            Value::Json(j) => ColumnData::String(Some(j.to_string().into())),
            Value::Array(items) => {
                let arr = serde_json::Value::Array(items.iter().map(value_to_json).collect());
                ColumnData::String(Some(arr.to_string().into()))
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Result decoding: tiberius ColumnData -> crate Value
// ---------------------------------------------------------------------------

fn cd_to_value(cd: ColumnData<'static>) -> Result<Value, EngineError> {
    Ok(match &cd {
        ColumnData::U8(o) => o.map_or(Value::Null, |v| Value::Int(i64::from(v))),
        ColumnData::I16(o) => o.map_or(Value::Null, |v| Value::Int(i64::from(v))),
        ColumnData::I32(o) => o.map_or(Value::Null, |v| Value::Int(i64::from(v))),
        ColumnData::I64(o) => o.map_or(Value::Null, Value::Int),
        ColumnData::F32(o) => o.map_or(Value::Null, |v| Value::Float(f64::from(v))),
        ColumnData::F64(o) => o.map_or(Value::Null, Value::Float),
        ColumnData::Bit(o) => o.map_or(Value::Null, Value::Bool),
        ColumnData::String(o) => o.as_ref().map_or(Value::Null, |s| Value::Text(s.to_string())),
        ColumnData::Guid(o) => o.map_or(Value::Null, Value::Uuid),
        ColumnData::Binary(o) => o.as_ref().map_or(Value::Null, |b| Value::Bytes(b.to_vec())),
        ColumnData::Numeric(o) => match o {
            None => Value::Null,
            Some(_) => Decimal::from_sql(&cd)
                .map_err(map_tds)?
                .map_or(Value::Null, Value::Decimal),
        },
        ColumnData::DateTime(_) | ColumnData::SmallDateTime(_) | ColumnData::DateTime2(_) => {
            NaiveDateTime::from_sql(&cd)
                .map_err(map_tds)?
                .map_or(Value::Null, Value::Timestamp)
        }
        ColumnData::DateTimeOffset(_) => DateTime::<Utc>::from_sql(&cd)
            .map_err(map_tds)?
            .map_or(Value::Null, Value::TimestampTz),
        ColumnData::Date(_) => NaiveDate::from_sql(&cd)
            .map_err(map_tds)?
            .map_or(Value::Null, Value::Date),
        ColumnData::Time(_) => NaiveTime::from_sql(&cd)
            .map_err(map_tds)?
            .map_or(Value::Null, Value::Time),
        ColumnData::Xml(o) => o.as_ref().map_or(Value::Null, |x| Value::Text(x.to_string())),
    })
}

/// Column names + positional rows of one query.
type Fetched = (Vec<Arc<str>>, Vec<Vec<Value>>);

/// Whether a statement is an `INSERT`, seen past any leading whitespace and
/// `/* ... */` block comment (the query annotators prepend such a comment, which
/// would otherwise hide the `INSERT` keyword from a naive prefix check).
fn stmt_is_insert(sql: &str) -> bool {
    let mut s = sql.trim_start();
    while let Some(rest) = s.strip_prefix("/*") {
        match rest.find("*/") {
            Some(end) => s = rest[end + 2..].trim_start(),
            None => return false,
        }
    }
    s.get(..6).is_some_and(|p| p.eq_ignore_ascii_case("insert"))
}

async fn run_fetch(conn: &mut MssqlConn, sql: &str, params: &[Value]) -> Result<Fetched, EngineError> {
    let wrappers: Vec<Param> = params.iter().map(Param).collect();
    let refs: Vec<&dyn ToSql> = wrappers.iter().map(|p| p as &dyn ToSql).collect();
    // The model layer calls the fetch path on an auto-increment INSERT to read
    // the new pk back (T-SQL has no `RETURNING`, and `OUTPUT` cannot be a suffix
    // the model appends). Batch a `SELECT` so the identity is read on the same
    // connection immediately after the insert. `SCOPE_IDENTITY()` is the *last*
    // id, but the model's bulk backfill expects the *first* (MySQL's
    // `LAST_INSERT_ID` semantics), so subtract `@@ROWCOUNT - 1`: a single-row
    // insert yields the id itself, and a multi-row insert (contiguous within one
    // statement) yields the first of the range. Cast to BIGINT so it decodes as
    // an integer (uuid/explicit-pk inserts never reach this path — they carry
    // their own pk and go through `execute`).
    let is_insert = stmt_is_insert(sql);
    let batched;
    let query = if is_insert {
        batched = format!("{sql}; SELECT CAST(SCOPE_IDENTITY() - @@ROWCOUNT + 1 AS BIGINT) AS id");
        batched.as_str()
    } else {
        sql
    };
    let stream = conn.query(query, &refs).await.map_err(map_tds)?;
    let rows = stream.into_first_result().await.map_err(map_tds)?;
    let mut names: Vec<Arc<str>> = Vec::new();
    let mut out: Vec<Vec<Value>> = Vec::with_capacity(rows.len());
    for (i, row) in rows.into_iter().enumerate() {
        if i == 0 {
            names = row.columns().iter().map(|c| Arc::from(c.name())).collect();
        }
        let mut vals = Vec::with_capacity(names.len());
        for cd in row.into_iter() {
            vals.push(cd_to_value(cd)?);
        }
        out.push(vals);
    }
    Ok((names, out))
}

async fn run_execute(conn: &mut MssqlConn, sql: &str, params: &[Value]) -> Result<u64, EngineError> {
    let wrappers: Vec<Param> = params.iter().map(Param).collect();
    let refs: Vec<&dyn ToSql> = wrappers.iter().map(|p| p as &dyn ToSql).collect();
    let result = conn.execute(sql, &refs).await.map_err(map_tds)?;
    Ok(result.rows_affected().iter().sum())
}

fn to_named(names: &[Arc<str>], rows: Vec<Vec<Value>>) -> Vec<Row> {
    rows.into_iter()
        .map(|vals| names.iter().cloned().zip(vals).collect::<Row>())
        .collect()
}

// ---------------------------------------------------------------------------
// Connection config + pool
// ---------------------------------------------------------------------------

/// Build a tiberius [`Config`] from an `mssql://user:pass@host:port/db?...` URL.
fn config_from_url(url: &str) -> Result<Config, EngineError> {
    let u = url::Url::parse(url).map_err(|e| EngineError::Config(redact(e.to_string(), url)))?;
    let mut config = Config::new();
    config.host(u.host_str().unwrap_or("localhost"));
    config.port(u.port().unwrap_or(1433));
    let database = u.path().trim_start_matches('/');
    if !database.is_empty() {
        config.database(database);
    }
    let user = if u.username().is_empty() { "sa" } else { u.username() };
    let pass = u.password().unwrap_or("");
    config.authentication(AuthMethod::sql_server(user, pass));
    // TLS is negotiated; the server's certificate is trusted by default (a
    // dev/self-signed cert is the common case). `?encrypt=strict` opts into
    // full certificate validation; `?trust_cert=false` also enforces it.
    let params: std::collections::HashMap<_, _> = u.query_pairs().collect();
    let trust = params
        .get("trust_cert")
        .map(|v| v != "false")
        .unwrap_or(true);
    if trust {
        config.trust_cert();
    }
    Ok(config)
}

async fn connect_client(config: Config) -> Result<MssqlConn, EngineError> {
    let tcp = TcpStream::connect(config.get_addr())
        .await
        .map_err(|e| EngineError::Connection(e.to_string()))?;
    tcp.set_nodelay(true)
        .map_err(|e| EngineError::Connection(e.to_string()))?;
    tiberius::Client::connect(config, tcp.compat_write())
        .await
        .map_err(map_tds)
}

struct MssqlManager {
    config: Config,
}

impl Manager for MssqlManager {
    type Type = MssqlConn;
    type Error = EngineError;

    async fn create(&self) -> Result<MssqlConn, EngineError> {
        connect_client(self.config.clone()).await
    }

    async fn recycle(&self, conn: &mut MssqlConn, _: &Metrics) -> RecycleResult<EngineError> {
        // Roll back any transaction an aborted checkout left open before reuse,
        // then a cheap liveness check. Without the rollback a recycled
        // connection could run the next checkout's statements inside a stale
        // transaction whose work is silently lost on eventual disconnect.
        //
        // Also reset the isolation level to the SQL Server default: unlike
        // MySQL's `SET TRANSACTION` (next-tx only) and Oracle's (current-tx
        // only), T-SQL's `SET TRANSACTION ISOLATION LEVEL` is a *session*
        // setting that persists after the transaction that set it commits. A
        // prior `begin_tx(Some("SERIALIZABLE"))` would otherwise silently pin
        // every later checkout of this physical connection to SERIALIZABLE.
        // Both statements ride the same batch, so this adds no round trip.
        conn.simple_query(
            "IF @@TRANCOUNT > 0 ROLLBACK; \
             SET TRANSACTION ISOLATION LEVEL READ COMMITTED; SELECT 1",
        )
        .await
        .map_err(map_tds)?
        .into_first_result()
        .await
        .map_err(map_tds)?;
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// Backend
// ---------------------------------------------------------------------------

pub struct MssqlBackend {
    pool: Pool<MssqlManager>,
}

impl MssqlBackend {
    pub async fn connect(url: &str) -> Result<Self, EngineError> {
        let (clean_url, params) = extract_pool_params(url)?;
        let config = config_from_url(&clean_url)?;
        let max = params.max_size.unwrap_or(DEFAULT_MAX_SIZE).max(1);
        let pool = Pool::builder(MssqlManager { config })
            .max_size(max)
            .build()
            .map_err(|e| EngineError::Config(e.to_string()))?;
        // Fail fast on an unreachable server / bad credentials.
        let conn = pool
            .get()
            .await
            .map_err(|e| EngineError::Connection(redact(e.to_string(), url)))?;
        drop(conn);
        Ok(Self { pool })
    }

    async fn conn(&self) -> Result<Object<MssqlManager>, EngineError> {
        self.pool
            .get()
            .await
            .map_err(|e| EngineError::Connection(e.to_string()))
    }
}

#[async_trait]
impl Backend for MssqlBackend {
    async fn execute(&self, sql: &str, params: &[Value]) -> Result<u64, EngineError> {
        let mut conn = self.conn().await?;
        run_execute(&mut conn, sql, params).await
    }

    async fn fetch_all(&self, sql: &str, params: &[Value]) -> Result<Vec<Row>, EngineError> {
        let mut conn = self.conn().await?;
        let (names, rows) = run_fetch(&mut conn, sql, params).await?;
        Ok(to_named(&names, rows))
    }

    async fn fetch_all_values(
        &self,
        sql: &str,
        params: &[Value],
    ) -> Result<Vec<Vec<Value>>, EngineError> {
        let mut conn = self.conn().await?;
        let (_, rows) = run_fetch(&mut conn, sql, params).await?;
        Ok(rows)
    }

    async fn execute_many(&self, sql: &str, rows: &[Vec<Value>]) -> Result<Vec<Row>, EngineError> {
        if rows.is_empty() {
            return Ok(Vec::new());
        }
        // One transaction for the whole batch (all-or-nothing).
        let tx = MssqlTx::begin(self.conn().await?, None).await?;
        let result: Result<Vec<Row>, EngineError> = async {
            let mut guard = tx.conn.lock().await;
            let conn = guard.as_mut().expect("MssqlTx conn present until drop");
            let mut out = Vec::with_capacity(rows.len());
            for row_params in rows {
                let (names, fetched) = run_fetch(conn, sql, row_params).await?;
                out.push(match fetched.into_iter().next() {
                    Some(vals) => names.iter().cloned().zip(vals).collect(),
                    None => Row::new(),
                });
            }
            Ok(out)
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
        let mut conn = self.conn().await?;
        let mut result = Ok(());
        for statement in statements {
            if let Err(e) = conn
                .simple_query(statement.as_str())
                .await
                .map_err(map_tds)
                .and_then(|_| Ok(()))
            {
                result = Err(e);
                break;
            }
        }
        // Safety net: a script that left a transaction open must not hand a
        // mid-transaction connection back to the pool.
        let _ = conn.simple_query("IF @@TRANCOUNT > 0 ROLLBACK").await;
        result
    }

    fn dialect(&self) -> &'static str {
        "mssql"
    }

    async fn close(&self) {
        self.pool.close();
    }

    async fn begin_tx(&self, isolation: Option<&str>) -> Result<Box<dyn TxConn>, EngineError> {
        let tx = MssqlTx::begin(self.conn().await?, isolation).await?;
        Ok(Box::new(tx))
    }
}

// ---------------------------------------------------------------------------
// Transaction
// ---------------------------------------------------------------------------

#[derive(Clone, Copy)]
enum TxState {
    Active,
    Finished,
    Broken,
}

struct MssqlTx {
    conn: tokio::sync::Mutex<Option<Object<MssqlManager>>>,
    state: std::sync::Mutex<TxState>,
}

impl MssqlTx {
    async fn begin(
        conn: Object<MssqlManager>,
        isolation: Option<&str>,
    ) -> Result<Self, EngineError> {
        let tx = MssqlTx {
            conn: tokio::sync::Mutex::new(Some(conn)),
            state: std::sync::Mutex::new(TxState::Active),
        };
        {
            let mut guard = tx.conn.lock().await;
            let conn = guard.as_mut().expect("MssqlTx conn present until drop");
            if let Some(level) = isolation {
                // Applies to the session's next transaction; set before BEGIN.
                conn.simple_query(format!("SET TRANSACTION ISOLATION LEVEL {level}"))
                    .await
                    .map_err(map_tds)?;
            }
            conn.simple_query("BEGIN TRANSACTION")
                .await
                .map_err(map_tds)?;
        }
        Ok(tx)
    }

    fn set_state(&self, state: TxState) {
        *self.state.lock().expect("tx state lock never poisoned") = state;
    }

    async fn control(self: Box<Self>, sql: &str) -> Result<(), EngineError> {
        let mut guard = self.conn.lock().await;
        let conn = guard.as_mut().expect("MssqlTx conn present until drop");
        let result = conn.simple_query(sql).await.map(|_| ()).map_err(map_tds);
        drop(guard);
        self.set_state(if result.is_ok() {
            TxState::Finished
        } else {
            TxState::Broken
        });
        result
    }
}

impl Drop for MssqlTx {
    fn drop(&mut self) {
        let Some(conn) = self.conn.get_mut().take() else {
            return;
        };
        let state = *self.state.lock().expect("tx state lock never poisoned");
        match state {
            TxState::Finished => drop(conn),
            TxState::Broken => drop(conn),
            // Dropped mid-transaction: roll back on the background runtime.
            TxState::Active => {
                pyo3_async_runtimes::tokio::get_runtime().spawn(async move {
                    let mut conn = conn;
                    let _ = conn.simple_query("IF @@TRANCOUNT > 0 ROLLBACK").await;
                });
            }
        }
    }
}

#[async_trait]
impl TxConn for MssqlTx {
    async fn execute(&self, sql: &str, params: &[Value]) -> Result<u64, EngineError> {
        let mut guard = self.conn.lock().await;
        let conn = guard.as_mut().expect("MssqlTx conn present until drop");
        run_execute(conn, sql, params).await
    }

    async fn fetch_all(&self, sql: &str, params: &[Value]) -> Result<Vec<Row>, EngineError> {
        let mut guard = self.conn.lock().await;
        let conn = guard.as_mut().expect("MssqlTx conn present until drop");
        let (names, rows) = run_fetch(conn, sql, params).await?;
        Ok(to_named(&names, rows))
    }

    async fn fetch_all_values(
        &self,
        sql: &str,
        params: &[Value],
    ) -> Result<Vec<Vec<Value>>, EngineError> {
        let mut guard = self.conn.lock().await;
        let conn = guard.as_mut().expect("MssqlTx conn present until drop");
        let (_, rows) = run_fetch(conn, sql, params).await?;
        Ok(rows)
    }

    async fn commit(self: Box<Self>) -> Result<(), EngineError> {
        self.control("COMMIT").await
    }

    async fn rollback(self: Box<Self>) -> Result<(), EngineError> {
        self.control("IF @@TRANCOUNT > 0 ROLLBACK").await
    }

    async fn savepoint(&self, name: &str) -> Result<(), EngineError> {
        let mut guard = self.conn.lock().await;
        let conn = guard.as_mut().expect("MssqlTx conn present until drop");
        conn.simple_query(format!("SAVE TRANSACTION {name}"))
            .await
            .map(|_| ())
            .map_err(map_tds)
    }

    async fn release(&self, _name: &str) -> Result<(), EngineError> {
        // T-SQL savepoints merge into the outer transaction on commit; there is
        // no explicit release.
        Ok(())
    }

    async fn rollback_to(&self, name: &str) -> Result<(), EngineError> {
        let mut guard = self.conn.lock().await;
        let conn = guard.as_mut().expect("MssqlTx conn present until drop");
        conn.simple_query(format!("ROLLBACK TRANSACTION {name}"))
            .await
            .map(|_| ())
            .map_err(map_tds)
    }
}

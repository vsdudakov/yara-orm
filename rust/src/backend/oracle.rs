//! Oracle backend built on oracle-rs (a pure-Rust TNS driver — no OCI / ODPI-C
//! / Instant Client), pooled through a custom `deadpool` manager.
//!
//! # `RETURNING ... INTO`
//!
//! Oracle spells the RETURNING clause with OUT binds
//! (`INSERT ... RETURNING "id" INTO :ret_0`) rather than the PostgreSQL
//! result-set form. The [`OracleDialect`](crate) therefore renders the clause
//! with `INTO :ret_N` placeholders, and this backend detects such statements
//! (via the driver's own parser) and routes them through `execute_plsql`,
//! packaging the OUT-bind values back as a single synthetic row so the model
//! layer's `fetch_row` contract holds unchanged. The OUT binds are declared as
//! `VARCHAR2` and Oracle converts each returned column to its (ISO-formatted)
//! text, which the model layer's `field.to_python` re-coerces to the field
//! type — the same reconstruction the MySQL backend relies on for `CHAR(36)`
//! uuids.
//!
//! # Autocommit
//!
//! The driver never autocommits DML. On the pooled (non-transaction) path this
//! backend issues an explicit `COMMIT` after each write; inside an explicit
//! transaction the [`OracleTx`] guard owns commit/rollback. The pool's
//! `recycle` rolls back any transaction a connection carries back, so an
//! abandoned or cancelled statement can never leak uncommitted work.
//!
//! # Timezones
//!
//! Every session pins `TIME_ZONE` to UTC and the `NLS_*` formats to ISO-8601,
//! so `CURRENT_TIMESTAMP` defaults, date-part extraction and the RETURNING
//! text conversions are stable and locale-independent. Aware datetimes are
//! stored UTC-naive in `TIMESTAMP(6)` (Oracle's tz-aware type is avoided for
//! the same reason MySQL/SQLite avoid theirs); the Python layer re-attaches
//! UTC on read.

use std::sync::Arc;

use async_trait::async_trait;
use chrono::{DateTime, Datelike, NaiveDate, NaiveDateTime, Timelike, Utc};
use deadpool::managed::{Manager, Metrics, Object, Pool, RecycleError, RecycleResult};
use oracle_rs::types::{OracleDate, OracleNumber, OracleTimestamp};
use oracle_rs::{
    BindParam, ColumnInfo, Config, Connection, LobValue, OracleType, Statement, StatementType,
    Value as OraValue,
};

use crate::backend::pool::extract_pool_params;
use crate::backend::postgres::redact;
use crate::backend::{Backend, TxConn};
use crate::error::EngineError;
use crate::value::{value_to_json, Row, Value};

/// Default pool size when the URL does not specify `max_size` (matches the
/// PostgreSQL/MySQL backends' default).
const DEFAULT_MAX_SIZE: usize = 16;

/// Buffer size (bytes) for a RETURNING OUT bind. Comfortably covers a NUMBER's
/// text form, a uuid, and an ISO timestamp; larger returned columns are not a
/// supported RETURNING target.
const RETURN_BIND_BUFFER: u32 = 4000;

/// How many times a pooled connection may be reused before it is discarded and
/// reopened. The driver (0.1.x) desyncs its protocol stream after a few hundred
/// statements on one connection, after which the server drops it (observed
/// around checkout ~196 once the per-checkout recycle rollback is counted).
/// Retiring a connection well before that keeps high-volume workloads (per-row
/// `bulk_create`) reliable, at the cost of an occasional reconnect.
const REUSE_LIMIT: usize = 80;

/// ORA error codes that signal an integrity-constraint violation, mapped to
/// [`EngineError::Integrity`] so they reach Python as `IntegrityError`:
/// 1 unique key, 1400 insert NULL into NOT NULL, 1407 update to NULL,
/// 2290 CHECK, 2291 FK parent missing, 2292 FK child still referenced.
const INTEGRITY_CODES: &[u32] = &[1, 1400, 1407, 2290, 2291, 2292];

/// Pin the session to UTC so `CURRENT_TIMESTAMP`/`SYSTIMESTAMP` database
/// defaults are stored in the same UTC the engine uses everywhere else. Run
/// once per physical connection by [`OracleManager::create`].
///
/// Only `TIME_ZONE` is set here: the driver (0.1.x) mis-parses the server's
/// response to `ALTER SESSION SET NLS_*` (a bare one errors, a combined one
/// hangs), so the NLS formats are left at their session defaults. SELECTed
/// temporal values decode from the wire's binary form (NLS-independent), and
/// the RETURNING pk is an integer whose text form needs no NLS either — so this
/// suffices for correctness; see the module docs on the RETURNING path.
const SESSION_SETUP: &str = "ALTER SESSION SET TIME_ZONE='+00:00'";

/// Map an oracle-rs error, promoting constraint violations to `Integrity`.
/// Everything else stays `Query`, which the Python layer surfaces as
/// `OperationalError`.
fn map_ora(e: oracle_rs::Error) -> EngineError {
    match &e {
        oracle_rs::Error::OracleError { code, message }
        | oracle_rs::Error::ServerError { code, message } => {
            if INTEGRITY_CODES.contains(code) {
                EngineError::Integrity(message.clone())
            } else {
                EngineError::Query(format!("{message} (ORA-{code:05})"))
            }
        }
        _ => EngineError::Query(e.to_string()),
    }
}

// ---------------------------------------------------------------------------
// URL parsing
// ---------------------------------------------------------------------------

/// Parsed pieces of an `oracle://user:pass@host:port/service` URL.
struct OracleUrl {
    host: String,
    port: u16,
    service: String,
    username: String,
    password: String,
}

/// Parse an `oracle://user:pass@host[:port]/service` URL. The pool/cache and
/// `require_ssl` query parameters must already be stripped.
fn parse_oracle_url(url: &str) -> Result<OracleUrl, EngineError> {
    let rest = url
        .strip_prefix("oracle://")
        .ok_or_else(|| EngineError::Config(redact(format!("not an oracle URL: {url}"), url)))?;
    let (authority, service) = rest
        .split_once('/')
        .ok_or_else(|| EngineError::Config("oracle URL is missing the /service name".into()))?;
    // A residual query string (e.g. an unrecognised driver param) never belongs
    // in the service name.
    let service = service.split('?').next().unwrap_or(service);
    if service.is_empty() {
        return Err(EngineError::Config(
            "oracle URL is missing the /service name".into(),
        ));
    }
    let (userinfo, hostport) = match authority.rsplit_once('@') {
        Some((u, h)) => (u, h),
        None => ("", authority),
    };
    let (username, password) = match userinfo.split_once(':') {
        Some((u, p)) => (u, p),
        None => (userinfo, ""),
    };
    let (host, port) = match hostport.rsplit_once(':') {
        Some((h, p)) => (
            h,
            p.parse::<u16>()
                .map_err(|_| EngineError::Config(format!("invalid port in oracle URL: {p:?}")))?,
        ),
        None => (hostport, 1521),
    };
    if host.is_empty() {
        return Err(EngineError::Config("oracle URL is missing the host".into()));
    }
    Ok(OracleUrl {
        host: host.to_string(),
        port,
        service: service.to_string(),
        username: username.to_string(),
        password: password.to_string(),
    })
}

// ---------------------------------------------------------------------------
// Parameter encoding: crate Value -> oracle Value
// ---------------------------------------------------------------------------

/// Encode one bind parameter. Types Oracle lacks a scalar for are sent as their
/// canonical text (uuid, json, array-as-json, time-of-day); aware datetimes are
/// stored UTC-naive.
fn to_ora(v: &Value) -> OraValue {
    match v {
        Value::Null => OraValue::Null,
        // BOOLEAN is stored in a NUMBER(1) column; bind as 0/1.
        Value::Bool(b) => OraValue::Integer(i64::from(*b)),
        Value::Int(i) => OraValue::Integer(*i),
        Value::Float(f) => OraValue::Float(*f),
        Value::Text(s) => OraValue::String(s.clone()),
        Value::Bytes(b) => OraValue::Bytes(b.clone()),
        Value::Json(j) => OraValue::String(j.to_string()),
        // Oracle has no array type; store as a JSON text array (like SQLite/MySQL).
        Value::Array(items) => OraValue::String(
            serde_json::Value::Array(items.iter().map(value_to_json).collect()).to_string(),
        ),
        Value::Uuid(u) => OraValue::String(u.to_string()),
        // Text form keeps NUMBER exact (the server parses it server-side).
        Value::Decimal(d) => OraValue::Number(OracleNumber::new(d.to_string())),
        Value::Timestamp(dt) => OraValue::Timestamp(naive_to_ots(dt)),
        // No tz-aware storage: canonicalise to UTC and store naive.
        Value::TimestampTz(dt) => OraValue::Timestamp(naive_to_ots(&dt.naive_utc())),
        Value::Date(d) => {
            OraValue::Date(OracleDate::date(d.year(), d.month() as u8, d.day() as u8))
        }
        // Oracle has no TIME type; store the time-of-day as ISO text.
        Value::Time(t) => OraValue::String(t.format("%H:%M:%S%.6f").to_string()),
    }
}

fn naive_to_ots(dt: &NaiveDateTime) -> OracleTimestamp {
    OracleTimestamp::new(
        dt.year(),
        dt.month() as u8,
        dt.day() as u8,
        dt.hour() as u8,
        dt.minute() as u8,
        dt.second() as u8,
        dt.nanosecond() / 1_000,
    )
}

fn to_ora_params(params: &[Value]) -> Vec<OraValue> {
    params.iter().map(to_ora).collect()
}

// ---------------------------------------------------------------------------
// Result decoding: oracle cell -> crate Value
// ---------------------------------------------------------------------------

/// The numeric text carried by a cell of a NUMBER column. The driver decodes
/// NUMBER values as `String` (their canonical text); the typed variants are
/// handled too for robustness.
fn number_text(v: &OraValue) -> Option<String> {
    match v {
        OraValue::String(s) => Some(s.clone()),
        OraValue::Number(n) => Some(n.as_str().to_string()),
        OraValue::Integer(i) => Some(i.to_string()),
        OraValue::Float(f) => Some(f.to_string()),
        _ => None,
    }
}

/// Decode a NUMBER cell using the column metadata. A `NUMBER(1)` column is the
/// ORM's BooleanField spelling; an integral NUMBER that fits `i64` decodes to
/// `Int`; anything else stays exact as a `Decimal`.
fn decode_number(col: &ColumnInfo, v: &OraValue) -> Value {
    let Some(text) = number_text(v) else {
        return Value::Null;
    };
    let text = text.trim();
    if col.precision == 1 && col.scale == 0 {
        return Value::Bool(text != "0");
    }
    if col.scale <= 0 {
        if let Ok(i) = text.parse::<i64>() {
            return Value::Int(i);
        }
    }
    match rust_decimal::Decimal::from_str_exact(text) {
        Ok(d) => Value::Decimal(d),
        Err(_) => Value::Text(text.to_string()),
    }
}

/// Decode a TIMESTAMP cell. A tz-bearing value normalises to UTC; a naive one
/// stays naive (the Python layer re-attaches UTC for tz-aware fields).
fn decode_timestamp(ts: &OracleTimestamp) -> Value {
    let Some(date) = NaiveDate::from_ymd_opt(ts.year, u32::from(ts.month), u32::from(ts.day))
    else {
        return Value::Null;
    };
    let Some(ndt) = date.and_hms_micro_opt(
        u32::from(ts.hour),
        u32::from(ts.minute),
        u32::from(ts.second),
        ts.microsecond,
    ) else {
        return Value::Null;
    };
    if ts.has_timezone() {
        let offset_secs = i64::from(ts.tz_hour_offset) * 3600 + i64::from(ts.tz_minute_offset) * 60;
        let utc = ndt - chrono::Duration::seconds(offset_secs);
        Value::TimestampTz(DateTime::<Utc>::from_naive_utc_and_offset(utc, Utc))
    } else {
        Value::Timestamp(ndt)
    }
}

/// Whether a column holds binary (BLOB/RAW/BFILE) rather than character data.
fn is_binary_column(col: &ColumnInfo) -> bool {
    matches!(
        col.oracle_type,
        OracleType::Blob | OracleType::Bfile | OracleType::Raw | OracleType::LongRaw
    )
}

/// Decode a LOB cell, reading a locator's content when the driver did not
/// prefetch it inline. CLOB/NCLOB decode to `Text`, BLOB/BFILE to `Bytes`.
async fn decode_lob(
    conn: &Connection,
    col: &ColumnInfo,
    lob: LobValue,
) -> Result<Value, EngineError> {
    let binary = is_binary_column(col);
    Ok(match lob {
        LobValue::Null => Value::Null,
        LobValue::Empty => {
            if binary {
                Value::Bytes(Vec::new())
            } else {
                Value::Text(String::new())
            }
        }
        LobValue::Inline(bytes) => {
            if binary {
                Value::Bytes(bytes.to_vec())
            } else {
                Value::Text(String::from_utf8_lossy(&bytes).into_owned())
            }
        }
        LobValue::Locator(loc) => {
            if binary {
                Value::Bytes(conn.read_blob(&loc).await.map_err(map_ora)?.to_vec())
            } else {
                Value::Text(conn.read_clob(&loc).await.map_err(map_ora)?)
            }
        }
    })
}

/// Decode one result cell, dispatching on the column's declared Oracle type
/// (the driver hands most scalars back as `String`, so the value shape alone is
/// not enough). LOB locators need the connection to fetch their content, so the
/// whole decode path is async.
async fn decode_cell(
    conn: &Connection,
    col: &ColumnInfo,
    v: OraValue,
) -> Result<Value, EngineError> {
    if matches!(v, OraValue::Null) {
        return Ok(Value::Null);
    }
    Ok(match col.oracle_type {
        OracleType::Number | OracleType::BinaryInteger => decode_number(col, &v),
        OracleType::BinaryDouble | OracleType::BinaryFloat => match v {
            OraValue::Float(f) => Value::Float(f),
            other => number_text(&other)
                .and_then(|s| s.trim().parse::<f64>().ok())
                .map(Value::Float)
                .unwrap_or(Value::Null),
        },
        OracleType::Date => match v {
            OraValue::Date(od) => {
                NaiveDate::from_ymd_opt(od.year, u32::from(od.month), u32::from(od.day))
                    .map(Value::Date)
                    .unwrap_or(Value::Null)
            }
            OraValue::Timestamp(ref ts) => decode_timestamp(ts),
            _ => decode_by_shape(v),
        },
        OracleType::Timestamp | OracleType::TimestampTz | OracleType::TimestampLtz => match v {
            OraValue::Timestamp(ref ts) => decode_timestamp(ts),
            OraValue::Date(od) => decode_timestamp(&OracleTimestamp::from(od)),
            _ => decode_by_shape(v),
        },
        OracleType::Clob | OracleType::Blob | OracleType::Bfile | OracleType::LongRaw => match v {
            OraValue::Lob(lob) => decode_lob(conn, col, lob).await?,
            _ => decode_by_shape(v),
        },
        OracleType::Boolean => match v {
            OraValue::Boolean(b) => Value::Bool(b),
            _ => decode_number(col, &v),
        },
        _ => decode_by_shape(v),
    })
}

/// Fallback decode by the driver value's own shape, for types not pinned by the
/// column-type dispatch above.
fn decode_by_shape(v: OraValue) -> Value {
    match v {
        OraValue::Null => Value::Null,
        OraValue::String(s) => Value::Text(s),
        OraValue::Bytes(b) => Value::Bytes(b),
        OraValue::Integer(i) => Value::Int(i),
        OraValue::Float(f) => Value::Float(f),
        OraValue::Boolean(b) => Value::Bool(b),
        OraValue::Json(j) => Value::Json(j),
        OraValue::Timestamp(ref ts) => decode_timestamp(ts),
        other => Value::Text(format!("{other:?}")),
    }
}

/// Decode a RETURNING OUT-bind value. These are declared `VARCHAR2`, so Oracle
/// hands each column back as text (which `field.to_python` re-coerces).
fn decode_out_value(v: OraValue) -> Value {
    match v {
        OraValue::Null => Value::Null,
        OraValue::String(s) => Value::Text(s),
        OraValue::Integer(i) => Value::Int(i),
        OraValue::Number(ref n) => Value::Text(n.as_str().to_string()),
        OraValue::Bytes(b) => Value::Bytes(b),
        other => Value::Text(format!("{other:?}")),
    }
}

// ---------------------------------------------------------------------------
// Statement runners (shared by the pooled backend and transactions)
// ---------------------------------------------------------------------------

/// Column names + positional rows of one statement execution.
type Fetched = (Vec<Arc<str>>, Vec<Vec<Value>>);

/// Strip leading whitespace and SQL comments (`/* ... */`, `-- ...`) from a
/// statement, so statement-type / RETURNING detection is not thrown off by a
/// query-annotator comment prefix (the driver's parser does not skip a leading
/// comment and mis-classifies the statement as `Unknown`). Only used to probe
/// the shape; the original SQL (comment included) is still what runs.
fn strip_leading_comments(sql: &str) -> &str {
    let mut s = sql.trim_start();
    loop {
        if let Some(rest) = s.strip_prefix("/*") {
            match rest.find("*/") {
                Some(end) => s = rest[end + 2..].trim_start(),
                None => return s,
            }
        } else if let Some(rest) = s.strip_prefix("--") {
            match rest.find('\n') {
                Some(nl) => s = rest[nl + 1..].trim_start(),
                None => return "",
            }
        } else {
            return s;
        }
    }
}

/// Run a fetch-style statement, committing writes when `autocommit` is set (the
/// pooled path; inside a transaction the caller owns commit). A RETURNING
/// insert routes through `execute_plsql` and returns its OUT binds as one row;
/// a query returns its decoded rows; any other statement runs and returns no
/// rows.
async fn run_fetch(
    conn: &Connection,
    sql: &str,
    params: &[Value],
    autocommit: bool,
) -> Result<Fetched, EngineError> {
    // The driver mis-handles a leading comment (statement-type detection and
    // even `query`/`execute` fail on it). A query-annotator comment prefix is
    // already observed by the hook layer before it reaches the backend, so it
    // is stripped here for both shape detection and execution.
    let sql = strip_leading_comments(sql);
    let statement = Statement::new(sql);
    if statement.is_returning() {
        return run_returning(conn, &statement, sql, params, autocommit).await;
    }
    if statement.statement_type() == StatementType::Query {
        let result = conn
            .query(sql, &to_ora_params(params))
            .await
            .map_err(map_ora)?;
        let names: Vec<Arc<str>> = result
            .columns
            .iter()
            .map(|c| Arc::from(c.name.as_str()))
            .collect();
        let cols = &result.columns;
        let mut rows = Vec::with_capacity(result.rows.len());
        for row in result.rows {
            let vals = row.into_values();
            let mut out = Vec::with_capacity(vals.len());
            // Zip values with column metadata so a driver row with more values
            // than declared columns truncates safely instead of index-panicking
            // (the desync-prone driver could return a malformed row).
            for (col, v) in cols.iter().zip(vals) {
                out.push(decode_cell(conn, col, v).await?);
            }
            rows.push(out);
        }
        Ok((names, rows))
    } else {
        conn.execute(sql, &to_ora_params(params))
            .await
            .map_err(map_ora)?;
        if autocommit {
            conn.commit().await.map_err(map_ora)?;
        }
        Ok((Vec::new(), Vec::new()))
    }
}

/// Execute an `INSERT ... RETURNING cols INTO :ret_N` statement, binding the
/// input parameters and one `VARCHAR2` OUT bind per return column, then package
/// the OUT values as a single synthetic row (OUT binds decode in return-column
/// order).
async fn run_returning(
    conn: &Connection,
    statement: &Statement,
    sql: &str,
    params: &[Value],
    autocommit: bool,
) -> Result<Fetched, EngineError> {
    let mut binds: Vec<BindParam> = Vec::with_capacity(statement.bind_info().len());
    let mut names: Vec<Arc<str>> = Vec::new();
    let mut input_idx = 0;
    for info in statement.bind_info() {
        if info.is_return_bind {
            binds.push(BindParam::output(OracleType::Varchar, RETURN_BIND_BUFFER));
            names.push(Arc::from(info.name.as_str()));
        } else {
            let value = params.get(input_idx).ok_or_else(|| {
                EngineError::Query("returning insert has fewer params than input binds".into())
            })?;
            binds.push(BindParam::input(to_ora(value)));
            input_idx += 1;
        }
    }
    // The driver's SQL-level `RETURNING ... INTO` closes the connection; the
    // same statement inside an anonymous PL/SQL block runs correctly, handing
    // the OUT-bind values back through `PlsqlResult`. A leading annotator
    // comment is stripped so the block body starts at the INSERT.
    let block = format!("BEGIN {}; END;", strip_leading_comments(sql));
    let result = conn.execute_plsql(&block, &binds).await.map_err(map_ora)?;
    if autocommit {
        conn.commit().await.map_err(map_ora)?;
    }
    let vals: Vec<Value> = result
        .out_values
        .into_iter()
        .map(decode_out_value)
        .collect();
    Ok((names, vec![vals]))
}

/// Run a statement for its affected-row count, committing on the pooled path.
async fn run_execute(
    conn: &Connection,
    sql: &str,
    params: &[Value],
    autocommit: bool,
) -> Result<u64, EngineError> {
    // Strip a leading query-annotator comment (the driver mis-handles it; the
    // hook layer has already observed it — see `run_fetch`).
    let sql = strip_leading_comments(sql);
    let result = conn
        .execute(sql, &to_ora_params(params))
        .await
        .map_err(map_ora)?;
    if autocommit {
        conn.commit().await.map_err(map_ora)?;
    }
    Ok(result.rows_affected)
}

fn to_named(names: &[Arc<str>], rows: Vec<Vec<Value>>) -> Vec<Row> {
    rows.into_iter()
        .map(|vals| names.iter().cloned().zip(vals).collect::<Row>())
        .collect()
}

// ---------------------------------------------------------------------------
// Pool manager
// ---------------------------------------------------------------------------

/// A `deadpool` manager that opens Oracle connections and applies the session
/// setup once per physical connection (see [`SESSION_SETUP`]).
struct OracleManager {
    config: Config,
}

impl Manager for OracleManager {
    type Type = Connection;
    type Error = oracle_rs::Error;

    async fn create(&self) -> Result<Connection, oracle_rs::Error> {
        let conn = Connection::connect_with_config(self.config.clone()).await?;
        conn.execute(SESSION_SETUP, &[]).await?;
        Ok(conn)
    }

    async fn recycle(
        &self,
        conn: &mut Connection,
        metrics: &Metrics,
    ) -> RecycleResult<oracle_rs::Error> {
        if conn.is_closed() {
            return Err(RecycleError::message("connection closed"));
        }
        // Retire the connection before it reaches the driver's desync threshold
        // (see REUSE_LIMIT); deadpool then opens a fresh one for the caller.
        if metrics.recycle_count >= REUSE_LIMIT {
            return Err(RecycleError::message("oracle connection reuse cap reached"));
        }
        // Roll back any work a check-in connection still carries — the safety
        // net for an abandoned/cancelled autocommit statement or explicit
        // transaction. The engine's own commit paths have already persisted
        // anything meant to survive.
        conn.rollback().await.map_err(RecycleError::Backend)?;
        Ok(())
    }
}

type OraPool = Pool<OracleManager>;
type OraConn = Object<OracleManager>;

// ---------------------------------------------------------------------------
// Backend
// ---------------------------------------------------------------------------

pub struct OracleBackend {
    pool: OraPool,
}

impl OracleBackend {
    pub async fn connect(url: &str) -> Result<Self, EngineError> {
        // `require_ssl` opts into TLS; detect it before the pool-param stripper
        // drops the rest of the query string.
        let require_ssl = url_has_flag(url, "require_ssl");
        let (clean_url, params) = extract_pool_params(url)?;
        let parsed = parse_oracle_url(&clean_url)?;

        let mut config = Config::new(
            parsed.host,
            parsed.port,
            parsed.service,
            parsed.username,
            parsed.password,
        );
        if require_ssl {
            config = config
                .with_tls()
                .map_err(|e| EngineError::Config(e.to_string()))?;
        }
        if !params.cache_statements {
            // URL `statement_cache_size=0`: disable the driver's prepared-
            // statement cache (parity with the other backends).
            config = config.stmtcachesize(0);
        }

        let max = params.max_size.unwrap_or(DEFAULT_MAX_SIZE).max(1);
        let pool = Pool::builder(OracleManager { config })
            .max_size(max)
            .runtime(deadpool::Runtime::Tokio1)
            .build()
            .map_err(|e| EngineError::Config(e.to_string()))?;

        // Pre-warm connections: always at least one (fail fast on an
        // unreachable server / bad credentials), up to the URL's `min_size`.
        let warm = params.min_size.unwrap_or(0).clamp(1, max);
        let mut held = Vec::with_capacity(warm);
        for _ in 0..warm {
            held.push(
                pool.get()
                    .await
                    .map_err(|e| EngineError::Connection(redact(e.to_string(), url)))?,
            );
        }
        drop(held);

        Ok(Self { pool })
    }

    async fn conn(&self) -> Result<OraConn, EngineError> {
        self.pool
            .get()
            .await
            .map_err(|e| EngineError::Connection(e.to_string()))
    }
}

/// Whether the URL's query string carries a bare/`=true` flag named `key`.
fn url_has_flag(url: &str, key: &str) -> bool {
    match url.split_once('?') {
        Some((_, query)) => query.split('&').any(|pair| {
            let (k, v) = pair.split_once('=').unwrap_or((pair, ""));
            k == key && (v.is_empty() || v.eq_ignore_ascii_case("true"))
        }),
        None => false,
    }
}

#[async_trait]
impl Backend for OracleBackend {
    async fn execute(&self, sql: &str, params: &[Value]) -> Result<u64, EngineError> {
        let conn = self.conn().await?;
        run_execute(&conn, sql, params, true).await
    }

    async fn fetch_all(&self, sql: &str, params: &[Value]) -> Result<Vec<Row>, EngineError> {
        let conn = self.conn().await?;
        let (names, rows) = run_fetch(&conn, sql, params, true).await?;
        Ok(to_named(&names, rows))
    }

    async fn fetch_all_values(
        &self,
        sql: &str,
        params: &[Value],
    ) -> Result<Vec<Vec<Value>>, EngineError> {
        let conn = self.conn().await?;
        let (_, rows) = run_fetch(&conn, sql, params, true).await?;
        Ok(rows)
    }

    async fn execute_many(&self, sql: &str, rows: &[Vec<Value>]) -> Result<Vec<Row>, EngineError> {
        if rows.is_empty() {
            return Ok(Vec::new());
        }
        // One transaction for the whole batch (all-or-nothing); the recycle
        // rollback covers a mid-batch cancellation.
        let conn = self.conn().await?;
        let mut out = Vec::with_capacity(rows.len());
        for row_params in rows {
            match run_fetch(&conn, sql, row_params, false).await {
                Ok((names, fetched)) => out.push(match fetched.into_iter().next() {
                    Some(vals) => names.iter().cloned().zip(vals).collect(),
                    None => Row::new(),
                }),
                Err(e) => {
                    let _ = conn.rollback().await;
                    return Err(e);
                }
            }
        }
        conn.commit().await.map_err(map_ora)?;
        Ok(out)
    }

    async fn execute_script(&self, statements: &[String]) -> Result<(), EngineError> {
        let conn = self.conn().await?;
        let mut result = Ok(());
        for statement in statements {
            if let Err(e) = conn.execute(strip_leading_comments(statement), &[]).await {
                result = Err(map_ora(e));
                break;
            }
        }
        // Persist any DML the script ran (DDL autocommits on its own); on error
        // discard the partial work.
        if result.is_ok() {
            if let Err(e) = conn.commit().await {
                result = Err(map_ora(e));
            }
        } else {
            let _ = conn.rollback().await;
        }
        result
    }

    fn dialect(&self) -> &'static str {
        "oracle"
    }

    async fn close(&self) {
        self.pool.close();
    }

    async fn begin_tx(&self, isolation: Option<&str>) -> Result<Box<dyn TxConn>, EngineError> {
        let tx = OracleTx::begin(self.conn().await?, isolation).await?;
        Ok(Box::new(tx))
    }
}

// ---------------------------------------------------------------------------
// Transactions
// ---------------------------------------------------------------------------

/// A pinned-connection Oracle transaction. Oracle starts a transaction
/// implicitly at the first DML, so `begin` only applies an optional isolation
/// level; the pooled connection carries the open transaction until
/// `commit`/`rollback` (or, on drop, the pool's `recycle` rollback).
struct OracleTx {
    conn: OraConn,
}

impl OracleTx {
    async fn begin(conn: OraConn, isolation: Option<&str>) -> Result<Self, EngineError> {
        if let Some(level) = isolation {
            // `SET TRANSACTION ISOLATION LEVEL` must be the first statement of
            // the transaction; validated by the Python layer.
            conn.execute(&format!("SET TRANSACTION ISOLATION LEVEL {level}"), &[])
                .await
                .map_err(map_ora)?;
        }
        Ok(OracleTx { conn })
    }
}

#[async_trait]
impl TxConn for OracleTx {
    async fn execute(&self, sql: &str, params: &[Value]) -> Result<u64, EngineError> {
        run_execute(&self.conn, sql, params, false).await
    }

    async fn fetch_all(&self, sql: &str, params: &[Value]) -> Result<Vec<Row>, EngineError> {
        let (names, rows) = run_fetch(&self.conn, sql, params, false).await?;
        Ok(to_named(&names, rows))
    }

    async fn fetch_all_values(
        &self,
        sql: &str,
        params: &[Value],
    ) -> Result<Vec<Vec<Value>>, EngineError> {
        let (_, rows) = run_fetch(&self.conn, sql, params, false).await?;
        Ok(rows)
    }

    async fn commit(self: Box<Self>) -> Result<(), EngineError> {
        self.conn.commit().await.map_err(map_ora)
    }

    async fn rollback(self: Box<Self>) -> Result<(), EngineError> {
        self.conn.rollback().await.map_err(map_ora)
    }

    async fn savepoint(&self, name: &str) -> Result<(), EngineError> {
        self.conn.savepoint(name).await.map_err(map_ora)
    }

    async fn release(&self, _name: &str) -> Result<(), EngineError> {
        // Oracle has no `RELEASE SAVEPOINT`; savepoints are released implicitly.
        Ok(())
    }

    async fn rollback_to(&self, name: &str) -> Result<(), EngineError> {
        self.conn.rollback_to_savepoint(name).await.map_err(map_ora)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn col(oracle_type: OracleType, precision: i16, scale: i16) -> ColumnInfo {
        ColumnInfo {
            name: "C".to_string(),
            oracle_type,
            data_size: 0,
            buffer_size: 0,
            precision,
            scale,
            nullable: true,
            csfrm: 0,
            type_schema: None,
            type_name: None,
            domain_schema: None,
            domain_name: None,
            is_json: false,
            is_oson: false,
            vector_dimensions: None,
            vector_format: None,
            element_type: None,
        }
    }

    #[test]
    fn url_parses_full_and_default_port() {
        // GIVEN a full oracle URL WHEN parsed THEN every piece is extracted.
        let u = parse_oracle_url("oracle://orm:secret@db.example:1600/FREEPDB1").unwrap();
        assert_eq!(u.host, "db.example");
        assert_eq!(u.port, 1600);
        assert_eq!(u.service, "FREEPDB1");
        assert_eq!(u.username, "orm");
        assert_eq!(u.password, "secret");
        // WHEN no port is given THEN it defaults to 1521; a trailing query is
        // dropped from the service name.
        let d = parse_oracle_url("oracle://orm:orm@localhost/FREEPDB1?foo=bar").unwrap();
        assert_eq!(d.port, 1521);
        assert_eq!(d.service, "FREEPDB1");
    }

    #[test]
    fn url_rejects_missing_pieces() {
        // GIVEN URLs missing the scheme/service/host WHEN parsed THEN a config
        // error results.
        assert!(parse_oracle_url("mysql://x/y").is_err());
        assert!(parse_oracle_url("oracle://orm:orm@localhost").is_err());
        assert!(parse_oracle_url("oracle:///FREEPDB1").is_err());
    }

    #[test]
    fn require_ssl_flag_is_detected_by_key() {
        assert!(url_has_flag(
            "oracle://h/db?require_ssl=true",
            "require_ssl"
        ));
        assert!(url_has_flag("oracle://h/db?a=1&require_ssl", "require_ssl"));
        assert!(!url_has_flag(
            "oracle://h/db?require_ssl=false",
            "require_ssl"
        ));
        assert!(!url_has_flag("oracle://h/db", "require_ssl"));
    }

    #[test]
    fn params_encode_scalars_and_temporal_values() {
        // GIVEN the crate's scalar values WHEN encoded for Oracle THEN each maps
        // to the matching driver value (bool -> 0/1, uuid/decimal -> text/number).
        assert!(matches!(to_ora(&Value::Null), OraValue::Null));
        assert!(matches!(to_ora(&Value::Bool(true)), OraValue::Integer(1)));
        assert!(matches!(to_ora(&Value::Bool(false)), OraValue::Integer(0)));
        assert!(matches!(to_ora(&Value::Int(-7)), OraValue::Integer(-7)));
        match to_ora(&Value::Uuid(
            uuid::Uuid::parse_str("6a3e93b6-16f6-4d9b-9c07-4d5a3f5d2a10").unwrap(),
        )) {
            OraValue::String(s) => assert_eq!(s, "6a3e93b6-16f6-4d9b-9c07-4d5a3f5d2a10"),
            other => panic!("expected String, got {other:?}"),
        }
        match to_ora(&Value::Decimal(
            rust_decimal::Decimal::from_str_exact("12.340").unwrap(),
        )) {
            OraValue::Number(n) => assert_eq!(n.as_str(), "12.340"),
            other => panic!("expected Number, got {other:?}"),
        }
        match to_ora(&Value::Time(
            chrono::NaiveTime::from_hms_micro_opt(3, 4, 5, 123_456).unwrap(),
        )) {
            OraValue::String(s) => assert_eq!(s, "03:04:05.123456"),
            other => panic!("expected String, got {other:?}"),
        }
    }

    #[test]
    fn aware_datetimes_store_as_utc_naive() {
        // GIVEN an aware datetime at +02:00 WHEN encoded THEN the wire value is
        // the UTC-naive equivalent, microseconds preserved.
        let dt = DateTime::parse_from_rfc3339("2024-01-02T03:04:05.123456+02:00")
            .unwrap()
            .with_timezone(&Utc);
        match to_ora(&Value::TimestampTz(dt)) {
            OraValue::Timestamp(ts) => {
                assert_eq!(
                    (ts.year, ts.month, ts.day, ts.hour, ts.minute),
                    (2024, 1, 2, 1, 4)
                );
                assert_eq!(ts.microsecond, 123_456);
            }
            other => panic!("expected Timestamp, got {other:?}"),
        }
    }

    #[test]
    fn number_decode_uses_column_metadata() {
        // The driver hands NUMBER values back as their text form.
        let s = |v: &str| OraValue::String(v.to_string());
        // GIVEN a NUMBER(1) column WHEN decoding THEN it is a bool.
        assert!(matches!(
            decode_number(&col(OracleType::Number, 1, 0), &s("1")),
            Value::Bool(true)
        ));
        assert!(matches!(
            decode_number(&col(OracleType::Number, 1, 0), &s("0")),
            Value::Bool(false)
        ));
        // An integral NUMBER that fits i64 decodes to Int (from text or a typed
        // OracleNumber alike).
        assert!(matches!(
            decode_number(&col(OracleType::Number, 10, 0), &s("42")),
            Value::Int(42)
        ));
        assert!(matches!(
            decode_number(
                &col(OracleType::Number, 10, 0),
                &OraValue::Number(OracleNumber::new("42"))
            ),
            Value::Int(42)
        ));
        // A fractional NUMBER stays exact as a Decimal.
        match decode_number(&col(OracleType::Number, 8, 2), &s("12.34")) {
            Value::Decimal(d) => assert_eq!(d.to_string(), "12.34"),
            other => panic!("expected Decimal, got {other:?}"),
        }
    }

    #[test]
    fn timestamp_decode_handles_naive_and_aware() {
        // GIVEN a naive Oracle timestamp WHEN decoded THEN it stays naive.
        let naive = OracleTimestamp::new(2024, 1, 2, 3, 4, 5, 123_456);
        match decode_timestamp(&naive) {
            Value::Timestamp(dt) => assert_eq!(dt.to_string(), "2024-01-02 03:04:05.123456"),
            other => panic!("expected Timestamp, got {other:?}"),
        }
        // GIVEN a +02:00 timestamp WHEN decoded THEN it normalises to UTC.
        let aware = OracleTimestamp::with_timezone(2024, 1, 2, 3, 4, 5, 0, 2, 0);
        match decode_timestamp(&aware) {
            Value::TimestampTz(dt) => assert_eq!(dt.to_rfc3339(), "2024-01-02T01:04:05+00:00"),
            other => panic!("expected TimestampTz, got {other:?}"),
        }
    }

    #[test]
    fn strips_leading_comments_for_shape_detection() {
        // GIVEN a query-annotator comment prefix WHEN stripped THEN the real
        // statement remains, so RETURNING/type detection is not thrown off.
        assert_eq!(
            strip_leading_comments("/* app=x */ INSERT INTO t VALUES (1)"),
            "INSERT INTO t VALUES (1)"
        );
        assert_eq!(strip_leading_comments("  -- note\n  SELECT 1"), "SELECT 1");
        assert_eq!(strip_leading_comments("SELECT 1"), "SELECT 1");
        // An unterminated block comment leaves the remainder untouched.
        assert_eq!(strip_leading_comments("/* open"), "/* open");
    }

    #[test]
    fn out_values_decode_as_text() {
        // GIVEN RETURNING OUT binds (declared VARCHAR2) WHEN decoded THEN each is
        // text the model layer re-coerces.
        assert!(matches!(decode_out_value(OraValue::Null), Value::Null));
        match decode_out_value(OraValue::String("42".into())) {
            Value::Text(s) => assert_eq!(s, "42"),
            other => panic!("expected Text, got {other:?}"),
        }
    }

    #[test]
    fn integrity_codes_map_to_integrity() {
        // GIVEN the documented ORA constraint codes WHEN mapped THEN they become
        // Integrity; a lock-timeout stays a Query error.
        for code in [1u32, 1400, 1407, 2290, 2291, 2292] {
            let err = oracle_rs::Error::OracleError {
                code,
                message: "boom".into(),
            };
            assert!(
                matches!(map_ora(err), EngineError::Integrity(_)),
                "code {code}"
            );
        }
        let busy = oracle_rs::Error::OracleError {
            code: 30006,
            message: "resource busy".into(),
        };
        match map_ora(busy) {
            EngineError::Query(msg) => assert!(msg.contains("ORA-30006")),
            other => panic!("expected Query, got {other:?}"),
        }
    }
}

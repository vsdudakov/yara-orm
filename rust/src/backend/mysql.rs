//! MySQL backend built on mysql_async (pure-Rust driver with its own pool).
//!
//! # No `RETURNING`
//!
//! MySQL has no `INSERT ... RETURNING`. The model layer compiles inserts
//! without a RETURNING clause on this dialect and still calls `fetch_row` on
//! them; to keep that contract, a statement that yields **no result set** but
//! *did* generate an auto-increment id returns a single synthetic row
//! `[last_insert_id]` (see [`run_fetch`]). Statements that generate no id
//! (UPDATE/DELETE/explicit-pk INSERT) return no rows, exactly like PostgreSQL.
//!
//! # Timezones
//!
//! Aware datetimes are stored as UTC-naive `DATETIME(6)` values (MySQL has no
//! timezone-aware type); they decode back as naive. Every connection pins its
//! session `time_zone` to UTC (via `setup`, re-applied after pool resets) so
//! `CURRENT_TIMESTAMP(6)` defaults and date-part extraction are stable.

use std::sync::Arc;

use async_trait::async_trait;
use chrono::{Datelike, NaiveDate, NaiveDateTime, NaiveTime, Timelike};
use mysql_async::consts::{ColumnFlags, ColumnType};
use mysql_async::prelude::Queryable;
use mysql_async::{
    Column, Conn, Opts, OptsBuilder, Params, Pool, PoolConstraints, PoolOpts, Row as MyRow,
    Value as MyValue,
};

use crate::backend::pool::extract_pool_params;
use crate::backend::postgres::redact;
use crate::backend::{Backend, TxConn};
use crate::error::EngineError;
use crate::value::{value_to_json, Row, Value};

/// Default pool size when the URL does not specify `max_size` (matches the
/// PostgreSQL backend's default).
const DEFAULT_MAX_SIZE: usize = 16;

/// MySQL error codes that signal an integrity-constraint violation, mapped to
/// [`EngineError::Integrity`] so they reach Python as `IntegrityError`:
/// 1062/1586 duplicate key, 1452 FK insert/update, 1451 FK delete (row still
/// referenced), 1216/1217 legacy FK codes, 1048/1364 NOT NULL (explicit NULL /
/// omitted column under strict mode), 3819 CHECK.
const INTEGRITY_CODES: &[u16] = &[1062, 1586, 1452, 1451, 1216, 1217, 1048, 1364, 3819];

/// Map a mysql_async error, promoting constraint violations to `Integrity`.
/// Deadlocks (1213) and lock-wait timeouts (1205) stay `Query`, which the
/// Python layer surfaces as `OperationalError`.
fn map_mysql(e: mysql_async::Error) -> EngineError {
    if let mysql_async::Error::Server(ref err) = e {
        if INTEGRITY_CODES.contains(&err.code) {
            return EngineError::Integrity(err.message.clone());
        }
        return EngineError::Query(format!("{} (MySQL error {})", err.message, err.code));
    }
    EngineError::Query(e.to_string())
}

// ---------------------------------------------------------------------------
// Parameter encoding: crate Value -> mysql Value
// ---------------------------------------------------------------------------

/// Encode one bind parameter. Types MySQL lacks are sent as their canonical
/// text (uuid/decimal/json), arrays as a JSON array (mirroring SQLite), and
/// aware datetimes as UTC-naive `DATETIME` values.
fn to_my_value(v: &Value) -> MyValue {
    match v {
        Value::Null => MyValue::NULL,
        // MySQL's BOOLEAN is TINYINT(1); bind as 0/1.
        Value::Bool(b) => MyValue::Int(i64::from(*b)),
        Value::Int(i) => MyValue::Int(*i),
        Value::Float(f) => MyValue::Double(*f),
        Value::Text(s) => MyValue::Bytes(s.clone().into_bytes()),
        Value::Bytes(b) => MyValue::Bytes(b.clone()),
        Value::Json(j) => MyValue::Bytes(j.to_string().into_bytes()),
        // MySQL has no array type; store as a JSON text array (like SQLite).
        Value::Array(items) => MyValue::Bytes(
            serde_json::Value::Array(items.iter().map(value_to_json).collect())
                .to_string()
                .into_bytes(),
        ),
        Value::Uuid(u) => MyValue::Bytes(u.to_string().into_bytes()),
        // Text form keeps DECIMAL exact (the server parses it server-side).
        Value::Decimal(d) => MyValue::Bytes(d.to_string().into_bytes()),
        Value::Timestamp(dt) => naive_to_my(dt),
        // MySQL has no tz-aware type: canonicalise to UTC and store naive
        // (the same decision SQLite made; the Python layer's use_tz handling
        // re-attaches UTC on read).
        Value::TimestampTz(dt) => naive_to_my(&dt.naive_utc()),
        Value::Date(d) => MyValue::Date(
            u16::try_from(d.year()).unwrap_or(0),
            d.month() as u8,
            d.day() as u8,
            0,
            0,
            0,
            0,
        ),
        Value::Time(t) => MyValue::Time(
            false,
            0,
            t.hour() as u8,
            t.minute() as u8,
            t.second() as u8,
            t.nanosecond() / 1_000,
        ),
    }
}

fn naive_to_my(dt: &NaiveDateTime) -> MyValue {
    MyValue::Date(
        u16::try_from(dt.year()).unwrap_or(0),
        dt.month() as u8,
        dt.day() as u8,
        dt.hour() as u8,
        dt.minute() as u8,
        dt.second() as u8,
        dt.nanosecond() / 1_000,
    )
}

fn to_params(params: &[Value]) -> Params {
    if params.is_empty() {
        Params::Empty
    } else {
        Params::Positional(params.iter().map(to_my_value).collect())
    }
}

// ---------------------------------------------------------------------------
// Result decoding: mysql cell -> crate Value
// ---------------------------------------------------------------------------

/// Per-column decode plan, computed once per result set from the column
/// metadata. It mostly disambiguates `Bytes` payloads (text vs blob vs decimal
/// vs JSON) and the `TINYINT(1)` boolean convention.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum MyDecode {
    /// `TINYINT(1)` (signed, display width 1): MySQL's BOOLEAN spelling.
    Bool,
    /// DECIMAL/NEWDECIMAL: `Bytes` carries the exact decimal text.
    Decimal,
    /// JSON column: `Bytes` carries the JSON document text.
    Json,
    /// DATE column (the driver still ships a full `Date` value).
    Date,
    /// Textual column (any non-binary character set): `Bytes` -> `Text`.
    Text,
    /// Binary column (character set 63): `Bytes` stays `Bytes`.
    Blob,
    /// Anything else: decode by the driver value's own shape.
    Raw,
}

/// The binary character-set id: a `Bytes` cell in this charset is real binary
/// data (BLOB/VARBINARY), any other charset means text.
const BINARY_CHARSET: u16 = 63;

pub(crate) fn plan_for(col: &Column) -> MyDecode {
    match col.column_type() {
        // The tinyint(1) display width survives in the wire metadata precisely
        // so clients can restore BOOLEAN semantics; an UNSIGNED or wider TINY
        // stays an integer.
        ColumnType::MYSQL_TYPE_TINY => {
            if col.column_length() == 1 && !col.flags().contains(ColumnFlags::UNSIGNED_FLAG) {
                MyDecode::Bool
            } else {
                MyDecode::Raw
            }
        }
        ColumnType::MYSQL_TYPE_DECIMAL | ColumnType::MYSQL_TYPE_NEWDECIMAL => MyDecode::Decimal,
        ColumnType::MYSQL_TYPE_JSON => MyDecode::Json,
        ColumnType::MYSQL_TYPE_DATE | ColumnType::MYSQL_TYPE_NEWDATE => MyDecode::Date,
        ColumnType::MYSQL_TYPE_TINY_BLOB
        | ColumnType::MYSQL_TYPE_MEDIUM_BLOB
        | ColumnType::MYSQL_TYPE_LONG_BLOB
        | ColumnType::MYSQL_TYPE_BLOB
        | ColumnType::MYSQL_TYPE_VARCHAR
        | ColumnType::MYSQL_TYPE_VAR_STRING
        | ColumnType::MYSQL_TYPE_STRING => {
            if col.character_set() == BINARY_CHARSET {
                MyDecode::Blob
            } else {
                MyDecode::Text
            }
        }
        _ => MyDecode::Raw,
    }
}

/// Decode one cell. Falls back to the driver value's own shape when a typed
/// decode does not apply (mirroring the SQLite decoder's fallback contract).
pub(crate) fn decode_my(plan: MyDecode, v: MyValue) -> Value {
    match v {
        MyValue::NULL => Value::Null,
        MyValue::Int(i) => match plan {
            MyDecode::Bool => Value::Bool(i != 0),
            _ => Value::Int(i),
        },
        MyValue::UInt(u) => match i64::try_from(u) {
            Ok(i) => match plan {
                MyDecode::Bool => Value::Bool(i != 0),
                _ => Value::Int(i),
            },
            // A BIGINT UNSIGNED beyond i64: keep it exact as a decimal rather
            // than wrapping or erroring.
            Err(_) => Value::Decimal(rust_decimal::Decimal::from(u)),
        },
        MyValue::Float(f) => Value::Float(f64::from(f)),
        MyValue::Double(f) => Value::Float(f),
        MyValue::Date(y, mo, d, h, mi, s, us) => {
            // MySQL zero dates ('0000-00-00') have no chrono form; surface NULL
            // (they only occur with legacy sql_modes).
            let Some(date) = NaiveDate::from_ymd_opt(i32::from(y), u32::from(mo), u32::from(d))
            else {
                return Value::Null;
            };
            if plan == MyDecode::Date {
                return Value::Date(date);
            }
            match date.and_hms_micro_opt(u32::from(h), u32::from(mi), u32::from(s), us) {
                Some(dt) => Value::Timestamp(dt),
                None => Value::Null,
            }
        }
        MyValue::Time(neg, days, h, m, s, us) => {
            // TIME is a duration in MySQL (up to ±838h); only a plain
            // time-of-day maps to a Python time, anything else keeps MySQL's
            // text form.
            if !neg && days == 0 {
                if let Some(t) =
                    NaiveTime::from_hms_micro_opt(u32::from(h), u32::from(m), u32::from(s), us)
                {
                    return Value::Time(t);
                }
            }
            let sign = if neg { "-" } else { "" };
            let hours = days * 24 + u32::from(h);
            Value::Text(format!("{sign}{hours:02}:{m:02}:{s:02}.{us:06}"))
        }
        MyValue::Bytes(bytes) => match plan {
            MyDecode::Decimal => {
                match rust_decimal::Decimal::from_str_exact(&String::from_utf8_lossy(&bytes)) {
                    Ok(d) => Value::Decimal(d),
                    Err(_) => Value::Text(String::from_utf8_lossy(&bytes).into_owned()),
                }
            }
            MyDecode::Json => match serde_json::from_slice(&bytes) {
                Ok(j) => Value::Json(j),
                Err(_) => Value::Text(String::from_utf8_lossy(&bytes).into_owned()),
            },
            MyDecode::Blob => Value::Bytes(bytes),
            _ => Value::Text(String::from_utf8_lossy(&bytes).into_owned()),
        },
    }
}

// ---------------------------------------------------------------------------
// Statement runners (shared by the pooled backend and transactions)
// ---------------------------------------------------------------------------

/// Column names + positional rows of one statement execution.
type Fetched = (Vec<Arc<str>>, Vec<Vec<Value>>);

/// Run `sql` and decode every row of its (first) result set.
///
/// A statement with **no result set** that generated an auto-increment id
/// (`INSERT` into an AUTO_INCREMENT table — for a multi-row insert MySQL
/// reports the *first* generated id) returns one synthetic
/// `[Value::Int(last_insert_id)]` row: this is how the model layer's
/// RETURNING-less insert paths receive the new primary key through the
/// unchanged `fetch_row` call. Other row-less statements return no rows.
async fn run_fetch(conn: &mut Conn, sql: &str, params: &[Value]) -> Result<Fetched, EngineError> {
    let result = conn
        .exec_iter(sql, to_params(params))
        .await
        .map_err(map_mysql)?;
    let cols = result.columns().filter(|c| !c.is_empty());
    match cols {
        Some(cols) => {
            let names: Vec<Arc<str>> = cols
                .iter()
                .map(|c| Arc::from(c.name_str().as_ref()))
                .collect();
            let plans: Vec<MyDecode> = cols.iter().map(plan_for).collect();
            let raw: Vec<MyRow> = result.collect_and_drop().await.map_err(map_mysql)?;
            let rows = raw
                .into_iter()
                .map(|row| {
                    row.unwrap()
                        .into_iter()
                        .zip(&plans)
                        .map(|(cell, plan)| decode_my(*plan, cell))
                        .collect()
                })
                .collect();
            Ok((names, rows))
        }
        None => {
            let id = result.last_insert_id();
            result.drop_result().await.map_err(map_mysql)?;
            // The result-set handle may not carry the id; the connection's OK
            // packet always does.
            let id = id.or_else(|| conn.last_insert_id());
            match id {
                Some(id) if id != 0 => Ok((
                    vec![Arc::from("last_insert_id")],
                    vec![vec![Value::Int(i64::try_from(id).unwrap_or(i64::MAX))]],
                )),
                _ => Ok((Vec::new(), Vec::new())),
            }
        }
    }
}

/// Run `sql` and return the affected row count.
async fn run_execute(conn: &mut Conn, sql: &str, params: &[Value]) -> Result<u64, EngineError> {
    let result = conn
        .exec_iter(sql, to_params(params))
        .await
        .map_err(map_mysql)?;
    let affected = result.affected_rows();
    result.drop_result().await.map_err(map_mysql)?;
    Ok(affected)
}

fn to_named(names: &[Arc<str>], rows: Vec<Vec<Value>>) -> Vec<Row> {
    rows.into_iter()
        .map(|vals| names.iter().cloned().zip(vals).collect::<Row>())
        .collect()
}

// ---------------------------------------------------------------------------
// Backend
// ---------------------------------------------------------------------------

pub struct MySqlBackend {
    pool: Pool,
}

impl MySqlBackend {
    pub async fn connect(url: &str) -> Result<Self, EngineError> {
        let (clean_url, params) = extract_pool_params(url)?;
        // Remaining URL query parameters pass through to mysql_async's own
        // parser (mirroring how the postgres backend forwards driver params).
        let opts = Opts::from_url(&clean_url)
            .map_err(|e| EngineError::Config(redact(e.to_string(), url)))?;
        let max = params.max_size.unwrap_or(DEFAULT_MAX_SIZE).max(1);
        // The `min` constraint is how many *idle* connections the pool
        // retains: mysql_async DISCONNECTS a returned connection beyond it, so
        // a low floor would make every pooled statement pay a fresh TCP +
        // auth + setup handshake (measured ~14x on point queries). Default to
        // `max` — keep every connection, like the deadpool-backed postgres
        // backend — and let an explicit `min_size` lower the retained count.
        let min = params.min_size.unwrap_or(max).min(max);
        let constraints = PoolConstraints::new(min, max).ok_or_else(|| {
            EngineError::Config(format!(
                "invalid pool sizes: min_size={min} must not exceed max_size={max}"
            ))
        })?;
        let mut builder = OptsBuilder::from_opts(opts)
            // No per-check-in session reset: the driver's default fires a
            // COM_RESET_CONNECTION *and* re-runs the `setup` statements on
            // every return to the pool — ~3 extra round trips per pooled
            // statement (measured ~4x on point queries). The postgres backend
            // made the same call (`RecyclingMethod::Fast`); an abandoned
            // transaction is the tx guard's job (explicit ROLLBACK), and the
            // only session state the engine sets is idempotent.
            .pool_opts(
                PoolOpts::default()
                    .with_constraints(constraints)
                    .with_reset_connection(false),
            )
            // Pin every session to UTC, matching the postgres backend's
            // `SET TIME ZONE 'UTC'`: the engine stores/returns timestamps in
            // UTC, so CURRENT_TIMESTAMP(6) defaults and EXTRACT(...) must not
            // depend on the server's locale. ANSI_QUOTES makes double-quoted
            // identifiers valid, so portable raw SQL written for
            // PostgreSQL/SQLite ("table"."column") runs unchanged; string
            // literals must use single quotes (which everything the ORM emits
            // already does). `setup` runs on each new connection (and would
            // re-run after a reset, were resets enabled).
            .setup(vec![
                "SET time_zone = '+00:00'".to_string(),
                "SET SESSION sql_mode = CONCAT(@@SESSION.sql_mode, ',ANSI_QUOTES')".to_string(),
            ]);
        if !params.cache_statements {
            // URL `statement_cache_size=0`: disable the per-connection
            // prepared-statement cache (parity with the other backends).
            builder = builder.stmt_cache_size(0_usize);
        }
        let pool = Pool::new(builder);

        // Pre-warm connections: always at least one (fail fast on an
        // unreachable server / bad credentials), up to the URL's `min_size`
        // (the retained-idle constraint above defaults higher; opening that
        // many connections up front would slow every connect()).
        let warm = params.min_size.unwrap_or(0).clamp(1, max);
        let mut held = Vec::with_capacity(warm);
        for _ in 0..warm {
            held.push(
                pool.get_conn()
                    .await
                    .map_err(|e| EngineError::Connection(redact(e.to_string(), url)))?,
            );
        }
        drop(held); // return the warmed connections to the pool as idle

        Ok(Self { pool })
    }

    async fn conn(&self) -> Result<Conn, EngineError> {
        self.pool
            .get_conn()
            .await
            .map_err(|e| EngineError::Connection(e.to_string()))
    }
}

#[async_trait]
impl Backend for MySqlBackend {
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
        // One transaction for the whole batch (all-or-nothing), with the tx
        // guard's drop safety covering cancellation mid-batch.
        let tx = MySqlTx::begin(self.conn().await?, None).await?;
        let result: Result<Vec<Row>, EngineError> = async {
            let mut guard = tx.conn.lock().await;
            let conn = guard.as_mut().expect("MySqlTx conn is present until drop");
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
            // Text protocol, one statement per call, each in autocommit — so
            // SET/session state and explicit BEGIN/COMMIT hold together on
            // this one connection.
            if let Err(e) = conn.query_drop(statement.as_str()).await {
                result = Err(map_mysql(e));
                break;
            }
        }
        // Safety net: a script that opened a transaction and failed (or forgot
        // COMMIT) must not hand a mid-transaction connection back to the pool.
        // (The pool's reset would also roll it back; this keeps it explicit.)
        let _ = conn.query_drop("ROLLBACK").await;
        result
    }

    fn dialect(&self) -> &'static str {
        "mysql"
    }

    async fn close(&self) {
        let _ = self.pool.clone().disconnect().await;
    }

    async fn begin_tx(&self, isolation: Option<&str>) -> Result<Box<dyn TxConn>, EngineError> {
        let tx = MySqlTx::begin(self.conn().await?, isolation).await?;
        Ok(Box::new(tx))
    }
}

// ---------------------------------------------------------------------------
// Transactions
// ---------------------------------------------------------------------------

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

/// A pinned-connection MySQL transaction, mirroring the PgTx guard.
///
/// The pool's per-check-in COM_RESET_CONNECTION (which would roll back an
/// open transaction) is disabled for round-trip economy — see `connect` — so
/// this guard is the *only* safety net: dropped in any state other than a
/// clean COMMIT/ROLLBACK it rolls back explicitly on the background runtime,
/// and disconnects the connection outright when even that fails, exactly like
/// the PostgreSQL twin.
///
/// The connection lives in a tokio `Mutex` because mysql_async statements need
/// `&mut Conn` while `TxConn` methods take `&self`; the engine layer already
/// serialises calls per transaction, so the lock is effectively uncontended.
struct MySqlTx {
    conn: tokio::sync::Mutex<Option<Conn>>,
    state: std::sync::Mutex<TxState>,
}

impl MySqlTx {
    /// Acquire-and-BEGIN with the drop guard armed *before* BEGIN is sent, so
    /// a cancellation mid-BEGIN can never recycle a possibly-in-transaction
    /// connection unguarded.
    async fn begin(conn: Conn, isolation: Option<&str>) -> Result<Self, EngineError> {
        let tx = MySqlTx {
            conn: tokio::sync::Mutex::new(Some(conn)),
            state: std::sync::Mutex::new(TxState::Active),
        };
        {
            let mut guard = tx.conn.lock().await;
            let conn = guard.as_mut().expect("MySqlTx conn is present until drop");
            if let Some(level) = isolation {
                // MySQL applies the level to the *next* transaction; it must be
                // set before BEGIN. Validated by the Python layer.
                conn.query_drop(format!("SET TRANSACTION ISOLATION LEVEL {level}"))
                    .await
                    .map_err(map_mysql)?;
            }
            conn.query_drop("BEGIN").await.map_err(map_mysql)?;
        }
        Ok(tx)
    }

    fn set_state(&self, state: TxState) {
        *self.state.lock().expect("tx state lock never poisoned") = state;
    }

    async fn control(self: Box<Self>, sql: &str) -> Result<(), EngineError> {
        let mut guard = self.conn.lock().await;
        let conn = guard.as_mut().expect("MySqlTx conn is present until drop");
        let result = conn.query_drop(sql).await.map_err(map_mysql);
        drop(guard);
        self.set_state(if result.is_ok() {
            TxState::Finished
        } else {
            TxState::Broken
        });
        result
    }
}

impl Drop for MySqlTx {
    fn drop(&mut self) {
        // Drop gives exclusive access, so `get_mut` reaches the Conn without
        // locking.
        let Some(conn) = self.conn.get_mut().take() else {
            return;
        };
        let state = *self.state.lock().expect("tx state lock never poisoned");
        match state {
            // Clean end: dropping the Conn returns it to the pool (which also
            // resets the session by default).
            TxState::Finished => drop(conn),
            // A COMMIT/ROLLBACK failed outright: session state unknown — close
            // the connection instead of recycling it.
            TxState::Broken => {
                pyo3_async_runtimes::tokio::get_runtime().spawn(async move {
                    let _ = conn.disconnect().await;
                });
            }
            // Dropped mid-transaction (cancellation windows around BEGIN /
            // COMMIT / ROLLBACK, or an abandoned transaction object): roll
            // back on the background runtime; only if that fails is the
            // connection closed rather than returned.
            TxState::Active => {
                pyo3_async_runtimes::tokio::get_runtime().spawn(async move {
                    let mut conn = conn;
                    if conn.query_drop("ROLLBACK").await.is_err() {
                        let _ = conn.disconnect().await;
                    }
                });
            }
        }
    }
}

#[async_trait]
impl TxConn for MySqlTx {
    async fn execute(&self, sql: &str, params: &[Value]) -> Result<u64, EngineError> {
        let mut guard = self.conn.lock().await;
        let conn = guard.as_mut().expect("MySqlTx conn is present until drop");
        run_execute(conn, sql, params).await
    }

    async fn fetch_all(&self, sql: &str, params: &[Value]) -> Result<Vec<Row>, EngineError> {
        let mut guard = self.conn.lock().await;
        let conn = guard.as_mut().expect("MySqlTx conn is present until drop");
        let (names, rows) = run_fetch(conn, sql, params).await?;
        Ok(to_named(&names, rows))
    }

    async fn fetch_all_values(
        &self,
        sql: &str,
        params: &[Value],
    ) -> Result<Vec<Vec<Value>>, EngineError> {
        let mut guard = self.conn.lock().await;
        let conn = guard.as_mut().expect("MySqlTx conn is present until drop");
        let (_, rows) = run_fetch(conn, sql, params).await?;
        Ok(rows)
    }

    async fn commit(self: Box<Self>) -> Result<(), EngineError> {
        self.control("COMMIT").await
    }

    async fn rollback(self: Box<Self>) -> Result<(), EngineError> {
        self.control("ROLLBACK").await
    }

    async fn savepoint(&self, name: &str) -> Result<(), EngineError> {
        let mut guard = self.conn.lock().await;
        let conn = guard.as_mut().expect("MySqlTx conn is present until drop");
        conn.query_drop(format!("SAVEPOINT {name}"))
            .await
            .map_err(map_mysql)
    }

    async fn release(&self, name: &str) -> Result<(), EngineError> {
        let mut guard = self.conn.lock().await;
        let conn = guard.as_mut().expect("MySqlTx conn is present until drop");
        conn.query_drop(format!("RELEASE SAVEPOINT {name}"))
            .await
            .map_err(map_mysql)
    }

    async fn rollback_to(&self, name: &str) -> Result<(), EngineError> {
        let mut guard = self.conn.lock().await;
        let conn = guard.as_mut().expect("MySqlTx conn is present until drop");
        conn.query_drop(format!("ROLLBACK TO SAVEPOINT {name}"))
            .await
            .map_err(map_mysql)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::{DateTime, Utc};

    fn utc(s: &str) -> DateTime<Utc> {
        DateTime::parse_from_rfc3339(s).unwrap().with_timezone(&Utc)
    }

    #[test]
    fn params_encode_scalars_and_temporal_values() {
        // GIVEN the crate's scalar values WHEN encoded for MySQL THEN each maps
        // to the matching wire value (bool -> 0/1, text/uuid/decimal -> bytes).
        assert_eq!(to_my_value(&Value::Null), MyValue::NULL);
        assert_eq!(to_my_value(&Value::Bool(true)), MyValue::Int(1));
        assert_eq!(to_my_value(&Value::Bool(false)), MyValue::Int(0));
        assert_eq!(to_my_value(&Value::Int(-7)), MyValue::Int(-7));
        assert_eq!(to_my_value(&Value::Float(1.5)), MyValue::Double(1.5));
        assert_eq!(
            to_my_value(&Value::Text("héllo".into())),
            MyValue::Bytes("héllo".as_bytes().to_vec())
        );
        assert_eq!(
            to_my_value(&Value::Bytes(vec![0, 1, 2])),
            MyValue::Bytes(vec![0, 1, 2])
        );
        let u = uuid::Uuid::parse_str("6a3e93b6-16f6-4d9b-9c07-4d5a3f5d2a10").unwrap();
        assert_eq!(
            to_my_value(&Value::Uuid(u)),
            MyValue::Bytes(u.to_string().into_bytes())
        );
        let d = rust_decimal::Decimal::from_str_exact("12.340").unwrap();
        assert_eq!(
            to_my_value(&Value::Decimal(d)),
            MyValue::Bytes(b"12.340".to_vec())
        );
    }

    #[test]
    fn aware_datetimes_are_stored_as_utc_naive() {
        // GIVEN an aware datetime at +02:00 WHEN encoded THEN the wire value is
        // the UTC-naive equivalent, microseconds preserved.
        let dt = utc("2024-01-02T03:04:05.123456+02:00");
        assert_eq!(
            to_my_value(&Value::TimestampTz(dt)),
            MyValue::Date(2024, 1, 2, 1, 4, 5, 123_456)
        );
        // Naive datetimes pass through unchanged.
        let naive = dt.naive_utc();
        assert_eq!(
            to_my_value(&Value::Timestamp(naive)),
            MyValue::Date(2024, 1, 2, 1, 4, 5, 123_456)
        );
    }

    #[test]
    fn arrays_and_json_encode_as_json_text() {
        // GIVEN a JSON value and an array parameter WHEN encoded THEN both are
        // sent as JSON text (MySQL has no array type).
        let json = serde_json::json!({"a": [1, 2]});
        assert_eq!(
            to_my_value(&Value::Json(json.clone())),
            MyValue::Bytes(json.to_string().into_bytes())
        );
        assert_eq!(
            to_my_value(&Value::Array(vec![Value::Int(1), Value::Text("x".into())])),
            MyValue::Bytes(br#"[1,"x"]"#.to_vec())
        );
    }

    #[test]
    fn decode_bool_plan_maps_tinyint_cells() {
        // GIVEN a TINYINT(1) column plan WHEN decoding 0/1 THEN real bools come
        // back; other plans keep the integer.
        assert!(matches!(
            decode_my(MyDecode::Bool, MyValue::Int(1)),
            Value::Bool(true)
        ));
        assert!(matches!(
            decode_my(MyDecode::Bool, MyValue::Int(0)),
            Value::Bool(false)
        ));
        assert!(matches!(
            decode_my(MyDecode::Raw, MyValue::Int(1)),
            Value::Int(1)
        ));
    }

    #[test]
    fn decode_handles_unsigned_overflow_exactly() {
        // GIVEN a BIGINT UNSIGNED value beyond i64 WHEN decoded THEN it stays
        // exact as a decimal instead of wrapping.
        match decode_my(MyDecode::Raw, MyValue::UInt(u64::MAX)) {
            Value::Decimal(d) => assert_eq!(d.to_string(), u64::MAX.to_string()),
            other => panic!("expected Decimal, got {other:?}"),
        }
        assert!(matches!(
            decode_my(MyDecode::Raw, MyValue::UInt(42)),
            Value::Int(42)
        ));
    }

    #[test]
    fn decode_typed_bytes_cells() {
        // GIVEN decimal/json/text/blob plans WHEN decoding Bytes cells THEN
        // each reconstructs its native value, with a text fallback on garbage.
        match decode_my(MyDecode::Decimal, MyValue::Bytes(b"12.34".to_vec())) {
            Value::Decimal(d) => assert_eq!(d.to_string(), "12.34"),
            other => panic!("expected Decimal, got {other:?}"),
        }
        match decode_my(MyDecode::Json, MyValue::Bytes(br#"{"a": 1}"#.to_vec())) {
            Value::Json(j) => assert_eq!(j, serde_json::json!({"a": 1})),
            other => panic!("expected Json, got {other:?}"),
        }
        match decode_my(MyDecode::Json, MyValue::Bytes(b"not json".to_vec())) {
            Value::Text(s) => assert_eq!(s, "not json"),
            other => panic!("expected Text fallback, got {other:?}"),
        }
        match decode_my(MyDecode::Text, MyValue::Bytes(b"hello".to_vec())) {
            Value::Text(s) => assert_eq!(s, "hello"),
            other => panic!("expected Text, got {other:?}"),
        }
        match decode_my(MyDecode::Blob, MyValue::Bytes(vec![0, 255])) {
            Value::Bytes(b) => assert_eq!(b, vec![0, 255]),
            other => panic!("expected Bytes, got {other:?}"),
        }
    }

    #[test]
    fn decode_temporal_cells() {
        // GIVEN DATETIME/DATE/TIME wire values WHEN decoded THEN they map to
        // naive chrono values with microseconds preserved.
        match decode_my(MyDecode::Raw, MyValue::Date(2024, 1, 2, 3, 4, 5, 123_456)) {
            Value::Timestamp(dt) => {
                assert_eq!(dt, utc("2024-01-02T03:04:05.123456Z").naive_utc())
            }
            other => panic!("expected Timestamp, got {other:?}"),
        }
        match decode_my(MyDecode::Date, MyValue::Date(2024, 1, 2, 0, 0, 0, 0)) {
            Value::Date(d) => assert_eq!(d, NaiveDate::from_ymd_opt(2024, 1, 2).unwrap()),
            other => panic!("expected Date, got {other:?}"),
        }
        match decode_my(MyDecode::Raw, MyValue::Time(false, 0, 3, 4, 5, 123_456)) {
            Value::Time(t) => {
                assert_eq!(t, NaiveTime::from_hms_micro_opt(3, 4, 5, 123_456).unwrap())
            }
            other => panic!("expected Time, got {other:?}"),
        }
        // Zero dates (legacy sql_modes) have no chrono form: surface NULL.
        assert!(matches!(
            decode_my(MyDecode::Raw, MyValue::Date(0, 0, 0, 0, 0, 0, 0)),
            Value::Null
        ));
        // A TIME that is really a (negative / multi-day) duration keeps
        // MySQL's text form instead of silently truncating.
        match decode_my(MyDecode::Raw, MyValue::Time(true, 1, 2, 3, 4, 0)) {
            Value::Text(s) => assert_eq!(s, "-26:03:04.000000"),
            other => panic!("expected Text, got {other:?}"),
        }
    }

    #[test]
    fn integrity_error_codes_map_to_integrity() {
        // GIVEN the documented constraint-violation codes WHEN mapped THEN they
        // become Integrity; a deadlock stays a Query error.
        for code in [1062u16, 1586, 1452, 1451, 1048, 1364, 3819] {
            let err = mysql_async::Error::Server(mysql_async::ServerError {
                code,
                message: "boom".into(),
                state: "23000".into(),
            });
            assert!(
                matches!(map_mysql(err), EngineError::Integrity(_)),
                "code {code}"
            );
        }
        let deadlock = mysql_async::Error::Server(mysql_async::ServerError {
            code: 1213,
            message: "Deadlock found".into(),
            state: "40001".into(),
        });
        match map_mysql(deadlock) {
            EngineError::Query(msg) => {
                assert!(msg.contains("Deadlock"));
                assert!(msg.contains("1213"));
            }
            other => panic!("expected Query, got {other:?}"),
        }
    }

    #[test]
    fn empty_params_bind_as_params_empty() {
        assert!(matches!(to_params(&[]), Params::Empty));
        assert!(matches!(to_params(&[Value::Int(1)]), Params::Positional(_)));
    }
}

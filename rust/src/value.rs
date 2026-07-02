//! Database-agnostic value type and Python <-> Rust <-> SQL conversions.
//!
//! `Value` is the single currency exchanged between the Python layer, the Rust
//! engine and the database driver. Keeping every conversion in one place means a
//! new backend only has to map `Value` onto its own driver types.

use std::error::Error;

use chrono::{DateTime, FixedOffset, NaiveDate, NaiveDateTime, NaiveTime, Utc};
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::PyType;
use pyo3::types::{
    PyBool, PyByteArray, PyBytes, PyDate, PyDateTime, PyDict, PyFloat, PyFrozenSet, PyInt, PyList,
    PySet, PyString, PyTime, PyTuple,
};
use pyo3::Borrowed;
use tokio_postgres::types::{to_sql_checked, IsNull, ToSql, Type};

use crate::error::EngineError;

/// Build an "out of range" SQL bind error for a narrowing integer cast.
fn oob(v: i64, sql_type: &str) -> Box<dyn Error + Sync + Send> {
    format!("integer {v} is out of range for {sql_type} column").into()
}

// Text wire formats shared by every encoder (pg-text, JSON, SQLite), so the
// timestamp/date/time representation cannot drift between the paths. `TimestampTz`
// uses RFC 3339 directly (`to_rfc3339`) on the PostgreSQL text path.
const FMT_TIMESTAMP: &str = "%Y-%m-%d %H:%M:%S%.6f";
// SQLite stores datetimes as TEXT compared *lexicographically*, so aware
// values must share the naive layout (same fixed-width space-separated prefix;
// RFC 3339's 'T' would sort every aware value after every naive one on the
// same day). The value is already UTC; the explicit "+00:00" suffix keeps
// awareness distinguishable on read while ordering stays correct — any two
// distinct instants differ within the fixed-width prefix.
const FMT_TIMESTAMPTZ_SQLITE: &str = "%Y-%m-%d %H:%M:%S%.6f+00:00";
const FMT_DATE: &str = "%Y-%m-%d";
const FMT_TIME: &str = "%H:%M:%S%.6f";

// `uuid.UUID` and `decimal.Decimal` have no dedicated Python C-type, so they are
// resolved by import. They are returned on essentially every row (UUID primary
// keys) and bound on every `WHERE id = ?`, so the type objects are imported and
// cached once per interpreter instead of re-imported per value.
static UUID_TYPE: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static DECIMAL_TYPE: PyOnceLock<Py<PyType>> = PyOnceLock::new();
// `yara_orm.Array` marks a sequence to bind as a PostgreSQL array (a bare list
// binds as JSON). Resolved by import and cached like the scalar types above.
static ARRAY_TYPE: PyOnceLock<Py<PyType>> = PyOnceLock::new();
// `enum.Enum` base, to coerce enum members inside a JSON value (their `.value`).
static ENUM_TYPE: PyOnceLock<Py<PyType>> = PyOnceLock::new();

fn uuid_type(py: Python<'_>) -> PyResult<&Bound<'_, PyType>> {
    UUID_TYPE.import(py, "uuid", "UUID")
}

fn decimal_type(py: Python<'_>) -> PyResult<&Bound<'_, PyType>> {
    DECIMAL_TYPE.import(py, "decimal", "Decimal")
}

fn array_type(py: Python<'_>) -> PyResult<&Bound<'_, PyType>> {
    ARRAY_TYPE.import(py, "yara_orm", "Array")
}

fn enum_type(py: Python<'_>) -> PyResult<&Bound<'_, PyType>> {
    ENUM_TYPE.import(py, "enum", "Enum")
}

/// Standard base64 (with padding), matching Python's `base64.b64encode`. Used to
/// represent `bytes` inside a JSON value (JSON has no byte type).
fn base64_encode(input: &[u8]) -> String {
    const CHARS: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(input.len().div_ceil(3) * 4);
    for chunk in input.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = *chunk.get(1).unwrap_or(&0) as u32;
        let b2 = *chunk.get(2).unwrap_or(&0) as u32;
        let n = (b0 << 16) | (b1 << 8) | b2;
        out.push(CHARS[((n >> 18) & 63) as usize] as char);
        out.push(CHARS[((n >> 12) & 63) as usize] as char);
        out.push(if chunk.len() > 1 {
            CHARS[((n >> 6) & 63) as usize] as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            CHARS[(n & 63) as usize] as char
        } else {
            '='
        });
    }
    out
}

#[derive(Debug, Clone)]
pub enum Value {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Text(String),
    Bytes(Vec<u8>),
    Json(serde_json::Value),
    Array(Vec<Value>),
    Uuid(uuid::Uuid),
    Decimal(rust_decimal::Decimal),
    Timestamp(NaiveDateTime),
    TimestampTz(DateTime<Utc>),
    Date(NaiveDate),
    Time(NaiveTime),
}

// ---------------------------------------------------------------------------
// Python -> Value (binding query parameters)
// ---------------------------------------------------------------------------

impl<'a, 'py> FromPyObject<'a, 'py> for Value {
    type Error = PyErr;

    fn extract(obj: Borrowed<'a, 'py, PyAny>) -> Result<Self, PyErr> {
        let ob: &Bound<'py, PyAny> = &obj;
        // Scalars first: these dominate real workloads, so checking them before
        // the date/time C-API calls avoids per-value overhead in bulk binds.
        if ob.is_none() {
            return Ok(Value::Null);
        }
        // bool must be checked before int (bool is a subclass of int in Python).
        if ob.is_instance_of::<PyBool>() {
            return Ok(Value::Bool(ob.extract::<bool>()?));
        }
        if ob.is_instance_of::<PyInt>() {
            return Ok(Value::Int(ob.extract::<i64>()?));
        }
        if ob.is_instance_of::<PyFloat>() {
            return Ok(Value::Float(ob.extract::<f64>()?));
        }
        if ob.is_instance_of::<PyString>() {
            return Ok(Value::Text(ob.extract::<String>()?));
        }
        // datetime must be checked before date (datetime subclasses date).
        if ob.is_instance_of::<PyDateTime>() {
            // A tz-aware datetime with any offset extracts as FixedOffset; we
            // normalise it to UTC. (Extracting straight to DateTime<Utc> only
            // succeeds for UTC-tagged values, so a +05:00 datetime would
            // otherwise fall through and be mis-handled as naive.)
            if let Ok(dt) = ob.extract::<DateTime<FixedOffset>>() {
                return Ok(Value::TimestampTz(dt.with_timezone(&Utc)));
            }
            return Ok(Value::Timestamp(ob.extract::<NaiveDateTime>()?));
        }
        if ob.is_instance_of::<PyDate>() {
            return Ok(Value::Date(ob.extract::<NaiveDate>()?));
        }
        if ob.is_instance_of::<PyTime>() {
            return Ok(Value::Time(ob.extract::<NaiveTime>()?));
        }
        if ob.is_instance_of::<PyBytes>() {
            return Ok(Value::Bytes(ob.extract::<Vec<u8>>()?));
        }
        if ob.is_instance_of::<PyDict>() || ob.is_instance_of::<PyList>() {
            // ``yara_orm.Array`` (a list subclass) binds as a PostgreSQL array;
            // a bare list/dict binds as JSON (so ``JSONField`` round-trips).
            if ob.is_instance(array_type(ob.py())?.as_any())? {
                let mut items = Vec::new();
                for item in ob.try_iter()? {
                    items.push(item?.extract::<Value>()?);
                }
                return Ok(Value::Array(items));
            }
            return Ok(Value::Json(py_to_json(ob)?));
        }
        // uuid.UUID and decimal.Decimal have no dedicated Python C-type; dispatch
        // against the cached type objects (avoids a per-bind qualname() string
        // alloc + compare). Decimal is bound as an exact NUMERIC value rather
        // than going through f64, which would silently lose precision.
        let py = ob.py();
        if ob.is_instance(uuid_type(py)?.as_any())? {
            let s = ob.str()?.to_string();
            let parsed = uuid::Uuid::parse_str(&s)
                .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
            return Ok(Value::Uuid(parsed));
        }
        if ob.is_instance(decimal_type(py)?.as_any())? {
            // Python's ``str(Decimal)`` may use scientific notation
            // (e.g. ``1E-10``), which ``from_str_exact`` rejects — fall back to
            // ``from_scientific`` so either form parses exactly.
            let s = ob.str()?.to_string();
            let parsed = rust_decimal::Decimal::from_str_exact(&s)
                .or_else(|_| rust_decimal::Decimal::from_scientific(&s))
                .map_err(|e| {
                    pyo3::exceptions::PyValueError::new_err(format!("invalid decimal {s:?}: {e}"))
                })?;
            return Ok(Value::Decimal(parsed));
        }
        Err(pyo3::exceptions::PyTypeError::new_err(format!(
            "unsupported parameter type: {}",
            ob.get_type().name()?
        )))
    }
}

fn py_to_json(ob: &Bound<'_, PyAny>) -> PyResult<serde_json::Value> {
    if ob.is_none() {
        return Ok(serde_json::Value::Null);
    }
    if ob.is_instance_of::<PyBool>() {
        return Ok(serde_json::Value::Bool(ob.extract::<bool>()?));
    }
    if ob.is_instance_of::<PyInt>() {
        return Ok(serde_json::Value::from(ob.extract::<i64>()?));
    }
    if ob.is_instance_of::<PyFloat>() {
        return Ok(serde_json::Value::from(ob.extract::<f64>()?));
    }
    if ob.is_instance_of::<PyString>() {
        return Ok(serde_json::Value::String(ob.extract::<String>()?));
    }
    // ``bytes``/``bytearray`` have no JSON form; base64 keeps them reversible.
    if ob.is_instance_of::<PyBytes>() || ob.is_instance_of::<PyByteArray>() {
        return Ok(serde_json::Value::String(base64_encode(
            &ob.extract::<Vec<u8>>()?,
        )));
    }
    // ``datetime``/``date``/``time`` (datetime subclasses date) -> ISO string.
    if ob.is_instance_of::<PyDateTime>()
        || ob.is_instance_of::<PyDate>()
        || ob.is_instance_of::<PyTime>()
    {
        return Ok(serde_json::Value::String(
            ob.call_method0("isoformat")?.extract::<String>()?,
        ));
    }
    // list / tuple / set / frozenset -> JSON array.
    if ob.is_instance_of::<PyList>()
        || ob.is_instance_of::<PyTuple>()
        || ob.is_instance_of::<PySet>()
        || ob.is_instance_of::<PyFrozenSet>()
    {
        let mut arr = Vec::new();
        for item in ob.try_iter()? {
            arr.push(py_to_json(&item?)?);
        }
        return Ok(serde_json::Value::Array(arr));
    }
    if ob.is_instance_of::<PyDict>() {
        let dict = ob.cast::<PyDict>()?;
        let mut map = serde_json::Map::new();
        for (k, v) in dict.iter() {
            map.insert(k.str()?.to_string(), py_to_json(&v)?);
        }
        return Ok(serde_json::Value::Object(map));
    }
    // ``uuid.UUID`` / ``decimal.Decimal`` -> their string form; ``enum`` -> its
    // ``.value`` (recursed). These have no dedicated C-type, so dispatch against
    // the cached type objects (mirrors the Python `_json_safe` this replaced).
    let py = ob.py();
    if ob.is_instance(uuid_type(py)?.as_any())? || ob.is_instance(decimal_type(py)?.as_any())? {
        return Ok(serde_json::Value::String(ob.str()?.to_string()));
    }
    if ob.is_instance(enum_type(py)?.as_any())? {
        return py_to_json(&ob.getattr("value")?);
    }
    Err(pyo3::exceptions::PyTypeError::new_err(format!(
        "value of type {} is not JSON serialisable",
        ob.get_type().name()?
    )))
}

// ---------------------------------------------------------------------------
// Value -> Python (returning result rows)
// ---------------------------------------------------------------------------

impl<'py> IntoPyObject<'py> for Value {
    type Target = PyAny;
    type Output = Bound<'py, PyAny>;
    type Error = PyErr;

    fn into_pyobject(self, py: Python<'py>) -> Result<Self::Output, Self::Error> {
        let obj = match self {
            Value::Null => py.None().into_bound(py),
            Value::Bool(v) => v.into_pyobject(py)?.to_owned().into_any(),
            Value::Int(v) => v.into_pyobject(py)?.into_any(),
            Value::Float(v) => v.into_pyobject(py)?.into_any(),
            Value::Text(v) => v.into_pyobject(py)?.into_any(),
            Value::Bytes(v) => PyBytes::new(py, &v).into_any(),
            Value::Json(v) => json_to_py(py, &v)?,
            Value::Array(items) => {
                let list = PyList::empty(py);
                for item in items {
                    list.append(item.into_pyobject(py)?)?;
                }
                list.into_any()
            }
            Value::Uuid(v) => uuid_type(py)?.call1((v.to_string(),))?.into_any(),
            Value::Decimal(v) => decimal_type(py)?.call1((v.to_string(),))?.into_any(),
            Value::Timestamp(v) => v.into_pyobject(py)?.into_any(),
            Value::TimestampTz(v) => v.into_pyobject(py)?.into_any(),
            Value::Date(v) => v.into_pyobject(py)?.into_any(),
            Value::Time(v) => v.into_pyobject(py)?.into_any(),
        };
        Ok(obj)
    }
}

fn json_to_py<'py>(py: Python<'py>, v: &serde_json::Value) -> PyResult<Bound<'py, PyAny>> {
    let obj = match v {
        serde_json::Value::Null => py.None().into_bound(py),
        serde_json::Value::Bool(b) => b.into_pyobject(py)?.to_owned().into_any(),
        serde_json::Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                i.into_pyobject(py)?.into_any()
            } else {
                n.as_f64().unwrap_or(0.0).into_pyobject(py)?.into_any()
            }
        }
        serde_json::Value::String(s) => s.into_pyobject(py)?.into_any(),
        serde_json::Value::Array(arr) => {
            let list = PyList::empty(py);
            for item in arr {
                list.append(json_to_py(py, item)?)?;
            }
            list.into_any()
        }
        serde_json::Value::Object(map) => {
            let dict = PyDict::new(py);
            for (k, val) in map {
                dict.set_item(k, json_to_py(py, val)?)?;
            }
            dict.into_any()
        }
    };
    Ok(obj)
}

// ---------------------------------------------------------------------------
// Value -> SQL parameter (tokio-postgres ToSql)
// ---------------------------------------------------------------------------

impl Value {
    /// The PostgreSQL type to declare for this value as a bind parameter, so the
    /// server uses it instead of inferring from context (as asyncpg does). This
    /// keeps e.g. a `float` param `float8` when compared to an `int` column, and
    /// lets a bare `SELECT $1` return the value's real type. `Null`/`Json` return
    /// `None` so the server still infers them (a NULL has no type, and JSON must
    /// match the column's `json` vs `jsonb`).
    pub fn pg_type(&self) -> Option<Type> {
        Some(match self {
            Value::Bool(_) => Type::BOOL,
            Value::Int(_) => Type::INT8,
            Value::Float(_) => Type::FLOAT8,
            Value::Text(_) => Type::TEXT,
            Value::Bytes(_) => Type::BYTEA,
            Value::Uuid(_) => Type::UUID,
            Value::Decimal(_) => Type::NUMERIC,
            Value::Timestamp(_) => Type::TIMESTAMP,
            Value::TimestampTz(_) => Type::TIMESTAMPTZ,
            Value::Date(_) => Type::DATE,
            Value::Time(_) => Type::TIME,
            // Defer arrays to the server: the element type comes from the
            // ``::type[]`` cast or the target column, not from the value alone.
            Value::Null | Value::Json(_) | Value::Array(_) => return None,
        })
    }

    /// Render this value as PostgreSQL text, for params the server typed as
    /// textual/unknown (e.g. a bare `SELECT $1` with no column context, where a
    /// binary scalar would be misread as UTF-8). Returns `None` for values that
    /// are already textual or have no meaningful text form here.
    fn as_pg_text(&self) -> Option<String> {
        match self {
            Value::Bool(v) => Some(if *v { "true" } else { "false" }.to_string()),
            Value::Int(v) => Some(v.to_string()),
            Value::Float(v) => Some(v.to_string()),
            Value::Uuid(v) => Some(v.to_string()),
            Value::Decimal(v) => Some(v.to_string()),
            Value::Json(v) => Some(v.to_string()),
            Value::Timestamp(v) => Some(v.format(FMT_TIMESTAMP).to_string()),
            Value::TimestampTz(v) => Some(v.to_rfc3339()),
            Value::Date(v) => Some(v.format(FMT_DATE).to_string()),
            Value::Time(v) => Some(v.format(FMT_TIME).to_string()),
            Value::Null | Value::Text(_) | Value::Bytes(_) | Value::Array(_) => None,
        }
    }
}

impl ToSql for Value {
    fn to_sql(
        &self,
        ty: &Type,
        out: &mut tokio_postgres::types::private::BytesMut,
    ) -> Result<IsNull, Box<dyn Error + Sync + Send>> {
        // When the server inferred a textual (or unknown) type for the
        // parameter, encode scalars as their text representation rather than the
        // binary format the matching branch below would emit — otherwise the
        // server reads e.g. an 8-byte int as a UTF-8 string and rejects it.
        if matches!(
            *ty,
            Type::TEXT | Type::VARCHAR | Type::BPCHAR | Type::NAME | Type::UNKNOWN
        ) {
            if let Some(text) = self.as_pg_text() {
                return text.to_sql(ty, out);
            }
        }
        match self {
            Value::Null => Ok(IsNull::Yes),
            Value::Bool(v) => v.to_sql(ty, out),
            Value::Int(v) => match *ty {
                // Range-check narrowing casts: silently wrapping a too-large
                // integer into a smaller column would corrupt data, so error
                // instead and let the caller see the out-of-range value.
                Type::INT2 => i16::try_from(*v)
                    .map_err(|_| oob(*v, "SMALLINT"))?
                    .to_sql(ty, out),
                Type::INT4 => i32::try_from(*v)
                    .map_err(|_| oob(*v, "INTEGER"))?
                    .to_sql(ty, out),
                // When the inferred parameter type is NUMERIC/FLOAT (e.g. the
                // server inferred it from a numeric expression), encode the
                // integer in that type rather than mislabelling int8 bytes.
                Type::NUMERIC => rust_decimal::Decimal::from(*v).to_sql(ty, out),
                Type::FLOAT4 => (*v as f32).to_sql(ty, out),
                Type::FLOAT8 => (*v as f64).to_sql(ty, out),
                _ => v.to_sql(ty, out),
            },
            Value::Float(v) => match *ty {
                Type::FLOAT4 => (*v as f32).to_sql(ty, out),
                Type::NUMERIC => match rust_decimal::Decimal::from_f64_retain(*v) {
                    Some(d) => d.to_sql(ty, out),
                    None => v.to_sql(ty, out),
                },
                _ => v.to_sql(ty, out),
            },
            Value::Text(v) => {
                // A string bound where the server expects a uuid — e.g. an
                // element of a ``::uuid[]`` array, whose element type is UUID —
                // is parsed and encoded as uuid binary, since a raw string would
                // be rejected as an "improper binary format".
                if *ty == Type::UUID {
                    return uuid::Uuid::parse_str(v)?.to_sql(ty, out);
                }
                v.to_sql(ty, out)
            }
            Value::Bytes(v) => v.to_sql(ty, out),
            Value::Json(v) => v.to_sql(ty, out),
            Value::Array(items) => items.to_sql(ty, out),
            Value::Uuid(v) => v.to_sql(ty, out),
            Value::Decimal(v) => v.to_sql(ty, out),
            Value::Timestamp(v) => v.to_sql(ty, out),
            Value::TimestampTz(v) => v.to_sql(ty, out),
            Value::Date(v) => v.to_sql(ty, out),
            Value::Time(v) => v.to_sql(ty, out),
        }
    }

    fn accepts(_ty: &Type) -> bool {
        // Dispatch happens per-value in `to_sql`; accept everything and let the
        // server reject genuine mismatches.
        true
    }

    to_sql_checked!();
}

// ---------------------------------------------------------------------------
// SQL row -> Value (decoding result columns)
// ---------------------------------------------------------------------------

/// A single result row as ordered (column-name, value) pairs.
pub type Row = Vec<(String, Value)>;

/// Wrapper so a result row converts straight into a Python `dict`.
pub struct PyRow(pub Row);

impl<'py> IntoPyObject<'py> for PyRow {
    type Target = PyDict;
    type Output = Bound<'py, PyDict>;
    type Error = PyErr;

    fn into_pyobject(self, py: Python<'py>) -> Result<Self::Output, Self::Error> {
        let dict = PyDict::new(py);
        for (name, value) in self.0 {
            dict.set_item(name, value.into_pyobject(py)?)?;
        }
        Ok(dict)
    }
}

/// Decode a tokio-postgres row into our backend-agnostic representation.
pub fn decode_pg_row(row: &tokio_postgres::Row) -> Result<Row, EngineError> {
    let mut out = Row::with_capacity(row.columns().len());
    for (idx, col) in row.columns().iter().enumerate() {
        out.push((
            col.name().to_string(),
            decode_pg_cell(row, idx, col.type_())?,
        ));
    }
    Ok(out)
}

// ---------------------------------------------------------------------------
// SQLite conversions
// ---------------------------------------------------------------------------

/// Convert a [`Value`] into JSON, used to store an array parameter as JSON text
/// on SQLite (which has no array type). Element scalars map to their JSON form.
fn value_to_json(v: Value) -> serde_json::Value {
    use serde_json::Value as J;
    match v {
        Value::Null => J::Null,
        // JSON has no byte type; base64 keeps bytes reversible and matches the
        // `py_to_json` encoding for bytes inside JSON parameters.
        Value::Bytes(b) => J::String(base64_encode(&b)),
        Value::Bool(b) => J::Bool(b),
        Value::Int(i) => J::from(i),
        Value::Float(f) => J::from(f),
        Value::Text(s) => J::String(s),
        Value::Json(j) => j,
        Value::Array(items) => J::Array(items.into_iter().map(value_to_json).collect()),
        Value::Uuid(u) => J::String(u.to_string()),
        Value::Decimal(d) => J::String(d.to_string()),
        Value::Timestamp(t) => J::String(t.format(FMT_TIMESTAMP).to_string()),
        Value::TimestampTz(t) => J::String(t.format(FMT_TIMESTAMPTZ_SQLITE).to_string()),
        Value::Date(d) => J::String(d.format(FMT_DATE).to_string()),
        Value::Time(t) => J::String(t.format(FMT_TIME).to_string()),
    }
}

/// Bind an owned [`Value`] as a SQLite parameter, moving (not cloning) the
/// `String`/`Bytes` payloads. SQLite has few storage classes, so richer types
/// are encoded as TEXT and reconstructed on read via the declared column type
/// (see [`decode_sqlite`]).
pub fn value_into_sqlite(v: Value) -> rusqlite::types::Value {
    use rusqlite::types::Value as S;
    match v {
        Value::Null => S::Null,
        Value::Bool(b) => S::Integer(if b { 1 } else { 0 }),
        Value::Int(i) => S::Integer(i),
        Value::Float(f) => S::Real(f),
        Value::Text(s) => S::Text(s),
        Value::Bytes(b) => S::Blob(b),
        Value::Json(j) => S::Text(j.to_string()),
        // SQLite has no array type; store as a JSON text array.
        Value::Array(items) => S::Text(
            serde_json::Value::Array(items.into_iter().map(value_to_json).collect()).to_string(),
        ),
        Value::Uuid(u) => S::Text(u.to_string()),
        Value::Decimal(d) => S::Text(d.to_string()),
        Value::Timestamp(dt) => S::Text(dt.format(FMT_TIMESTAMP).to_string()),
        // Aware datetimes are UTC here; render them with the same fixed-width
        // space-separated layout as naive values (plus the "+00:00" marker) so
        // SQLite's lexicographic TEXT comparisons order naive and aware values
        // consistently. (`to_rfc3339`'s 'T' separator would break that; the
        // decoder still accepts the old RFC 3339 rows for existing databases.)
        Value::TimestampTz(dt) => S::Text(dt.format(FMT_TIMESTAMPTZ_SQLITE).to_string()),
        Value::Date(d) => S::Text(d.format(FMT_DATE).to_string()),
        Value::Time(t) => S::Text(t.format(FMT_TIME).to_string()),
    }
}

/// Decode a SQLite cell using the column's declared type so that uuid/json/
/// datetime/decimal columns round-trip to native Python types — keeping the
/// model layer identical across backends. Aggregates (no declared type) fall
/// back to the storage class.
pub fn decode_sqlite(decl: &str, vr: rusqlite::types::ValueRef) -> Value {
    use rusqlite::types::ValueRef as R;
    if let R::Null = vr {
        return Value::Null;
    }
    // `decl` is already upper-cased once per column by `column_meta`, so the
    // per-cell path here only does substring scans, no allocation.
    let text = |vr: R| -> Option<String> {
        match vr {
            R::Text(t) => Some(String::from_utf8_lossy(t).into_owned()),
            _ => None,
        }
    };

    if decl.contains("BOOL") {
        if let R::Integer(i) = vr {
            return Value::Bool(i != 0);
        }
    }
    if decl.contains("UUID") {
        if let Some(s) = text(vr) {
            if let Ok(u) = uuid::Uuid::parse_str(&s) {
                return Value::Uuid(u);
            }
        }
    }
    if decl.contains("JSON") {
        if let Some(s) = text(vr) {
            if let Ok(j) = serde_json::from_str(&s) {
                return Value::Json(j);
            }
        }
    }
    if decl.contains("TIMESTAMP") || decl.contains("DATETIME") {
        if let Some(s) = text(vr) {
            // Canonical aware layout (space-separated, explicit offset) first;
            // RFC 3339 ('T' separator) next, accepting rows written by older
            // releases so existing databases keep decoding as aware values.
            if let Ok(dt) = chrono::DateTime::parse_from_str(&s, "%Y-%m-%d %H:%M:%S%.f%:z") {
                return Value::TimestampTz(dt.with_timezone(&chrono::Utc));
            }
            if let Ok(dt) = chrono::DateTime::parse_from_rfc3339(&s) {
                return Value::TimestampTz(dt.with_timezone(&chrono::Utc));
            }
            for fmt in [
                "%Y-%m-%d %H:%M:%S%.f",
                "%Y-%m-%dT%H:%M:%S%.f",
                "%Y-%m-%d %H:%M:%S",
            ] {
                if let Ok(ndt) = chrono::NaiveDateTime::parse_from_str(&s, fmt) {
                    return Value::Timestamp(ndt);
                }
            }
        }
    } else if decl.contains("DATE") {
        if let Some(s) = text(vr) {
            if let Ok(nd) = chrono::NaiveDate::parse_from_str(&s, "%Y-%m-%d") {
                return Value::Date(nd);
            }
        }
    } else if decl.contains("TIME") {
        if let Some(s) = text(vr) {
            for fmt in ["%H:%M:%S%.f", "%H:%M:%S"] {
                if let Ok(nt) = chrono::NaiveTime::parse_from_str(&s, fmt) {
                    return Value::Time(nt);
                }
            }
        }
    }
    if decl.contains("DECIMAL") || decl.contains("NUMERIC") {
        let raw = match vr {
            R::Text(t) => String::from_utf8_lossy(t).into_owned(),
            R::Real(r) => r.to_string(),
            R::Integer(i) => i.to_string(),
            _ => String::new(),
        };
        if let Ok(d) = rust_decimal::Decimal::from_str_exact(&raw) {
            return Value::Decimal(d);
        }
    }

    match vr {
        R::Null => Value::Null,
        R::Integer(i) => Value::Int(i),
        R::Real(r) => Value::Float(r),
        R::Text(t) => Value::Text(String::from_utf8_lossy(t).into_owned()),
        R::Blob(b) => Value::Bytes(b.to_vec()),
    }
}

/// Decode a row into positional values only — no column-name allocation and no
/// per-row dict. The model layer maps positions to fields itself, so this is the
/// fast path for `SELECT`ing known columns.
pub fn decode_pg_row_values(row: &tokio_postgres::Row) -> Result<Vec<Value>, EngineError> {
    let cols = row.columns();
    let mut out = Vec::with_capacity(cols.len());
    for (idx, col) in cols.iter().enumerate() {
        out.push(decode_pg_cell(row, idx, col.type_())?);
    }
    Ok(out)
}

fn decode_pg_cell(row: &tokio_postgres::Row, idx: usize, ty: &Type) -> Result<Value, EngineError> {
    macro_rules! get {
        ($rust:ty, $wrap:expr) => {
            // A genuine SQL NULL is ``Ok(None)``; a decode failure is a real
            // error (type mismatch, value out of the Rust type's range) and is
            // surfaced rather than silently masked as NULL — which would drop
            // data, e.g. a high-precision NUMERIC beyond rust_decimal's range.
            match row.try_get::<_, Option<$rust>>(idx) {
                Ok(Some(v)) => Ok($wrap(v)),
                Ok(None) => Ok(Value::Null),
                Err(e) => Err(EngineError::Conversion(format!(
                    "failed to decode column {} ({}): {}",
                    idx,
                    ty.name(),
                    e
                ))),
            }
        };
    }

    macro_rules! get_arr {
        ($rust:ty, $wrap:expr) => {
            // PostgreSQL arrays may contain NULL elements, so decode through
            // ``Vec<Option<_>>`` and map a missing element to ``Value::Null``.
            match row.try_get::<_, Option<Vec<Option<$rust>>>>(idx) {
                Ok(Some(vs)) => Ok(Value::Array(
                    vs.into_iter()
                        .map(|o| o.map($wrap).unwrap_or(Value::Null))
                        .collect(),
                )),
                Ok(None) => Ok(Value::Null),
                Err(e) => Err(EngineError::Conversion(format!(
                    "failed to decode column {} ({}): {}",
                    idx,
                    ty.name(),
                    e
                ))),
            }
        };
    }

    // Dispatch on the stable type OID so the compiler lowers this to a jump
    // table / binary search instead of a ~16-deep chain of `Type` equality
    // comparisons per cell. OIDs are fixed catalog values (see pg_type.dat).
    match ty.oid() {
        16 => get!(bool, Value::Bool),                       // BOOL
        21 => get!(i16, |v| Value::Int(v as i64)),           // INT2
        23 => get!(i32, |v| Value::Int(v as i64)),           // INT4
        20 => get!(i64, Value::Int),                         // INT8
        700 => get!(f32, |v| Value::Float(v as f64)),        // FLOAT4
        701 => get!(f64, Value::Float),                      // FLOAT8
        25 | 1043 | 1042 | 19 => get!(String, Value::Text),  // TEXT/VARCHAR/BPCHAR/NAME
        17 => get!(Vec<u8>, Value::Bytes),                   // BYTEA
        114 | 3802 => get!(serde_json::Value, Value::Json),  // JSON/JSONB
        2950 => get!(uuid::Uuid, Value::Uuid),               // UUID
        1700 => get!(rust_decimal::Decimal, Value::Decimal), // NUMERIC
        1114 => get!(NaiveDateTime, Value::Timestamp),       // TIMESTAMP
        1184 => get!(DateTime<Utc>, Value::TimestampTz),     // TIMESTAMPTZ
        1082 => get!(NaiveDate, Value::Date),                // DATE
        1083 => get!(NaiveTime, Value::Time),                // TIME
        2278 => Ok(Value::Null),                             // VOID (e.g. pg_sleep)
        // Array element types (``_xxx`` OIDs), decoded to ``Value::Array``.
        1000 => get_arr!(bool, Value::Bool),             // _bool
        1005 => get_arr!(i16, |v| Value::Int(v as i64)), // _int2
        1007 => get_arr!(i32, |v| Value::Int(v as i64)), // _int4
        1016 => get_arr!(i64, Value::Int),               // _int8
        1021 => get_arr!(f32, |v| Value::Float(v as f64)), // _float4
        1022 => get_arr!(f64, Value::Float),             // _float8
        1009 | 1015 | 1014 => get_arr!(String, Value::Text), // _text/_varchar/_bpchar
        2951 => get_arr!(uuid::Uuid, Value::Uuid),       // _uuid
        1231 => get_arr!(rust_decimal::Decimal, Value::Decimal), // _numeric
        1115 => get_arr!(NaiveDateTime, Value::Timestamp), // _timestamp
        1185 => get_arr!(DateTime<Utc>, Value::TimestampTz), // _timestamptz
        1182 => get_arr!(NaiveDate, Value::Date),        // _date
        1183 => get_arr!(NaiveTime, Value::Time),        // _time
        _ => {
            // Genuinely unknown type: try its text representation (this covers
            // text-family types the jump table doesn't list). If that fails,
            // a genuine SQL NULL still decodes as None, but a non-NULL value we
            // cannot decode is an *error* — silently returning NULL would drop
            // data (the old behaviour).
            match row.try_get::<_, Option<String>>(idx) {
                Ok(Some(v)) => Ok(Value::Text(v)),
                Ok(None) => Ok(Value::Null),
                Err(_) => match row.try_get::<_, Option<NullProbe>>(idx) {
                    Ok(None) => Ok(Value::Null),
                    _ => Err(EngineError::Conversion(format!(
                        "unsupported PostgreSQL type OID {} for column {:?}; \
                         cast the column to text in SQL to read it",
                        ty.oid(),
                        row.columns()[idx].name(),
                    ))),
                },
            }
        }
    }
}

/// Accepts any PostgreSQL type without decoding it — used solely to tell a
/// genuine SQL NULL apart from an undecodable non-NULL value in a column of an
/// unsupported type (`String`'s `FromSql` rejects such columns before it can
/// see that the cell is NULL).
struct NullProbe;

impl<'a> tokio_postgres::types::FromSql<'a> for NullProbe {
    fn from_sql(
        _ty: &Type,
        _raw: &'a [u8],
    ) -> Result<Self, Box<dyn std::error::Error + Sync + Send>> {
        Ok(NullProbe)
    }

    fn accepts(_ty: &Type) -> bool {
        true
    }
}

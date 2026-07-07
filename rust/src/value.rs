//! Database-agnostic value type and Python <-> Rust <-> SQL conversions.
//!
//! `Value` is the single currency exchanged between the Python layer, the Rust
//! engine and the database driver. Keeping every conversion in one place means a
//! new backend only has to map `Value` onto its own driver types.

use std::borrow::Cow;
use std::error::Error;
use std::sync::Arc;

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
        // JSON has no representation for NaN/±Infinity; `serde_json::Value::from`
        // would silently turn them into `null` (data corruption). Preserve the
        // value as its textual form ("inf"/"-inf"/"NaN") instead — this keeps
        // the insert succeeding (raising would break rows that previously wrote)
        // and matches `value_to_json`, so both JSON bind paths encode a
        // non-finite float identically.
        let f = ob.extract::<f64>()?;
        if !f.is_finite() {
            return Ok(serde_json::Value::String(f.to_string()));
        }
        return Ok(serde_json::Value::from(f));
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
            } else if let Some(u) = n.as_u64() {
                // A JSON integer above i64::MAX (e.g. a uint64 id) is exact as
                // u64; the old code skipped straight to as_f64 and lost precision.
                u.into_pyobject(py)?.into_any()
            } else {
                // Every remaining JSON number is an f64: serde_json stores each
                // number as i64/u64/f64 and `arbitrary_precision` is not enabled.
                n.as_f64()
                    .expect("serde_json number is i64, u64 or f64")
                    .into_pyobject(py)?
                    .into_any()
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
                    // NaN/±Inf (and floats beyond Decimal's range) have no
                    // NUMERIC encoding. The old fallback called `f64::to_sql`,
                    // which only accepts FLOAT8 and would write 8 float bytes
                    // into a numeric field — silent corruption. Reject cleanly.
                    None => Err(format!(
                        "cannot encode non-finite / out-of-range float {v} as NUMERIC"
                    )
                    .into()),
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

/// A single result row as ordered (column-name, value) pairs. Names are
/// `Arc<str>` so the backends can decode them once per result set and share
/// them across rows (a per-row clone is a refcount bump, not an allocation).
pub type Row = Vec<(Arc<str>, Value)>;

/// Wrapper so a result row converts straight into a Python `dict`.
pub struct PyRow(pub Row);

impl<'py> IntoPyObject<'py> for PyRow {
    type Target = PyDict;
    type Output = Bound<'py, PyDict>;
    type Error = PyErr;

    fn into_pyobject(self, py: Python<'py>) -> Result<Self::Output, Self::Error> {
        let dict = PyDict::new(py);
        for (name, value) in self.0 {
            dict.set_item(&*name, value.into_pyobject(py)?)?;
        }
        Ok(dict)
    }
}

/// Wrapper so a whole named result set converts into a Python list of dicts.
///
/// All rows of one result set share the same column names in the same order
/// (both backends derive them from the statement's column list), so each name
/// becomes a Python string once — interned, since column names are identifiers
/// that recur across queries — and that one `PyString` keys every row's dict,
/// instead of allocating a fresh key per cell per row.
pub struct PyRows(pub Vec<Row>);

impl<'py> IntoPyObject<'py> for PyRows {
    type Target = PyList;
    type Output = Bound<'py, PyList>;
    type Error = PyErr;

    fn into_pyobject(self, py: Python<'py>) -> Result<Self::Output, Self::Error> {
        let list = PyList::empty(py);
        let mut keys: Vec<Bound<'py, PyString>> = Vec::new();
        for row in self.0 {
            let dict = PyDict::new(py);
            for (idx, (name, value)) in row.into_iter().enumerate() {
                if idx >= keys.len() {
                    keys.push(PyString::intern(py, &name));
                }
                dict.set_item(&keys[idx], value.into_pyobject(py)?)?;
            }
            list.append(dict)?;
        }
        Ok(list)
    }
}

/// Decode a tokio-postgres row into our backend-agnostic representation.
pub fn decode_pg_row(row: &tokio_postgres::Row) -> Result<Row, EngineError> {
    let mut out = Row::with_capacity(row.columns().len());
    for (idx, col) in row.columns().iter().enumerate() {
        out.push((
            Arc::from(col.name()),
            decode_pg_cell(row, idx, col.type_())?,
        ));
    }
    Ok(out)
}

/// Decode a whole result set, interning the column names once.
///
/// All rows of one result set carry the same columns, so the names and types
/// are built a single time and each row clones the shared `Arc<str>` name (a
/// refcount bump, not a fresh allocation) — the invariant the [`Row`] doc
/// describes. Decoding row-by-row via [`decode_pg_row`] would instead allocate
/// every name N times over an N-row set.
pub fn decode_pg_rows(rows: &[tokio_postgres::Row]) -> Result<Vec<Row>, EngineError> {
    let Some(first) = rows.first() else {
        return Ok(Vec::new());
    };
    let cols = first.columns();
    let names: Vec<Arc<str>> = cols.iter().map(|c| Arc::from(c.name())).collect();
    let types: Vec<Type> = cols.iter().map(|c| c.type_().clone()).collect();
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        let mut r = Row::with_capacity(names.len());
        for idx in 0..names.len() {
            r.push((names[idx].clone(), decode_pg_cell(row, idx, &types[idx])?));
        }
        out.push(r);
    }
    Ok(out)
}

// ---------------------------------------------------------------------------
// SQLite conversions
// ---------------------------------------------------------------------------

/// Convert a [`Value`] into JSON, used to store an array parameter as JSON text
/// on SQLite and MySQL (which have no array type). Element scalars map to their
/// JSON form.
pub(crate) fn value_to_json(v: &Value) -> serde_json::Value {
    use serde_json::Value as J;
    match v {
        Value::Null => J::Null,
        // JSON has no byte type; base64 keeps bytes reversible and matches the
        // `py_to_json` encoding for bytes inside JSON parameters.
        Value::Bytes(b) => J::String(base64_encode(b)),
        Value::Bool(b) => J::Bool(*b),
        Value::Int(i) => J::from(*i),
        // `J::from(f64)` yields `null` for NaN/±Infinity, silently dropping the
        // element. This bind path is infallible, so preserve the value as its
        // textual form ("inf"/"-inf"/"NaN") instead — like Decimal/Uuid below.
        Value::Float(f) if !f.is_finite() => J::String(f.to_string()),
        Value::Float(f) => J::from(*f),
        Value::Text(s) => J::String(s.clone()),
        Value::Json(j) => j.clone(),
        Value::Array(items) => J::Array(items.iter().map(value_to_json).collect()),
        Value::Uuid(u) => J::String(u.to_string()),
        Value::Decimal(d) => J::String(d.to_string()),
        Value::Timestamp(t) => J::String(t.format(FMT_TIMESTAMP).to_string()),
        Value::TimestampTz(t) => J::String(t.format(FMT_TIMESTAMPTZ_SQLITE).to_string()),
        Value::Date(d) => J::String(d.format(FMT_DATE).to_string()),
        Value::Time(t) => J::String(t.format(FMT_TIME).to_string()),
    }
}

/// Bind a [`Value`] as a SQLite parameter. `Text`/`Bytes` payloads are lent to
/// the statement (no copy); richer types are encoded as TEXT — SQLite has few
/// storage classes — and reconstructed on read via the declared column type
/// (see [`decode_sqlite`]).
impl rusqlite::types::ToSql for Value {
    fn to_sql(&self) -> rusqlite::Result<rusqlite::types::ToSqlOutput<'_>> {
        use rusqlite::types::{ToSqlOutput, Value as S, ValueRef as R};
        Ok(match self {
            Value::Null => ToSqlOutput::Owned(S::Null),
            Value::Bool(b) => ToSqlOutput::Owned(S::Integer(if *b { 1 } else { 0 })),
            Value::Int(i) => ToSqlOutput::Owned(S::Integer(*i)),
            Value::Float(f) => ToSqlOutput::Owned(S::Real(*f)),
            Value::Text(s) => ToSqlOutput::Borrowed(R::Text(s.as_bytes())),
            Value::Bytes(b) => ToSqlOutput::Borrowed(R::Blob(b)),
            Value::Json(j) => ToSqlOutput::Owned(S::Text(j.to_string())),
            // SQLite has no array type; store as a JSON text array.
            Value::Array(items) => ToSqlOutput::Owned(S::Text(
                serde_json::Value::Array(items.iter().map(value_to_json).collect()).to_string(),
            )),
            Value::Uuid(u) => ToSqlOutput::Owned(S::Text(u.to_string())),
            Value::Decimal(d) => ToSqlOutput::Owned(S::Text(d.to_string())),
            Value::Timestamp(dt) => {
                ToSqlOutput::Owned(S::Text(dt.format(FMT_TIMESTAMP).to_string()))
            }
            // Aware datetimes are UTC here; render them with the same
            // fixed-width space-separated layout as naive values (plus the
            // "+00:00" marker) so SQLite's lexicographic TEXT comparisons order
            // naive and aware values consistently. (`to_rfc3339`'s 'T'
            // separator would break that; the decoder still accepts the old
            // RFC 3339 rows for existing databases.)
            Value::TimestampTz(dt) => {
                ToSqlOutput::Owned(S::Text(dt.format(FMT_TIMESTAMPTZ_SQLITE).to_string()))
            }
            Value::Date(d) => ToSqlOutput::Owned(S::Text(d.format(FMT_DATE).to_string())),
            Value::Time(t) => ToSqlOutput::Owned(S::Text(t.format(FMT_TIME).to_string())),
        })
    }
}

/// Per-column SQLite decode plan, computed once per result set by
/// [`sqlite_decode_plan`]. The per-cell path in [`decode_sqlite`] then
/// dispatches on this tag directly instead of re-scanning the declared type
/// string (up to six substring searches) for every cell.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SqliteDecode {
    Bool,
    Uuid,
    Json,
    DateTime,
    Date,
    Time,
    Decimal,
    /// No recognised declared type (aggregates, expressions, plain TEXT/
    /// INTEGER/... columns): decode by storage class only.
    Raw,
}

/// Classify a declared SQLite column type (any case) into its decode plan.
///
/// The keyword precedence mirrors the substring-scan chain this replaced:
/// BOOL, UUID, JSON, TIMESTAMP/DATETIME, then DATE, then TIME — the timestamp
/// keywords must win over DATE/TIME because "DATETIME" contains both and
/// "TIMESTAMP" contains "TIME" — then DECIMAL/NUMERIC.
pub fn sqlite_decode_plan(decl: &str) -> SqliteDecode {
    let decl = decl.to_ascii_uppercase();
    if decl.contains("BOOL") {
        SqliteDecode::Bool
    } else if decl.contains("UUID") {
        SqliteDecode::Uuid
    } else if decl.contains("JSON") {
        SqliteDecode::Json
    } else if decl.contains("TIMESTAMP") || decl.contains("DATETIME") {
        SqliteDecode::DateTime
    } else if decl.contains("DATE") {
        SqliteDecode::Date
    } else if decl.contains("TIME") {
        SqliteDecode::Time
    } else if decl.contains("DECIMAL") || decl.contains("NUMERIC") {
        SqliteDecode::Decimal
    } else {
        SqliteDecode::Raw
    }
}

/// Parse a SQLite datetime TEXT cell, accepting every format previous releases
/// wrote (each is still attempted, so the accepted set is unchanged):
///
/// - `%Y-%m-%d %H:%M:%S%.f%:z` — the canonical aware layout (space separator,
///   explicit offset) written for `TimestampTz` values;
/// - RFC 3339 (`T` separator) — aware rows written by older releases;
/// - `%Y-%m-%d %H:%M:%S%.f` / `%Y-%m-%dT%H:%M:%S%.f` / `%Y-%m-%d %H:%M:%S` —
///   naive layouts.
///
/// A cheap shape probe *orders* the attempts so a well-formed value parses on
/// its first try instead of paying for failed trial parses: an offset can only
/// occur after the seconds field (byte 19 of the fixed-width prefix), where a
/// naive value holds only digits and `.`; the byte at index 10 separates date
/// from time. The probe never excludes a parser — oddly-shaped strings (e.g.
/// chrono's tolerance for 1-digit month/day) still walk the full chain, and
/// since no string is accepted by two of these parsers with different results,
/// reordering cannot change what a given string decodes to.
fn parse_sqlite_datetime(s: &str) -> Option<Value> {
    const AWARE_SPACE: &str = "%Y-%m-%d %H:%M:%S%.f%:z";
    let bytes = s.as_bytes();
    let aware_hint = bytes.len() > 19
        && bytes[19..]
            .iter()
            .any(|c| matches!(c, b'+' | b'-' | b'Z' | b'z'));
    let t_sep_hint = bytes.get(10) == Some(&b'T');

    let parse_aware = |first_rfc3339: bool| -> Option<DateTime<Utc>> {
        let space = || {
            DateTime::parse_from_str(s, AWARE_SPACE)
                .ok()
                .map(|dt| dt.with_timezone(&Utc))
        };
        let rfc3339 = || {
            DateTime::parse_from_rfc3339(s)
                .ok()
                .map(|dt| dt.with_timezone(&Utc))
        };
        if first_rfc3339 {
            rfc3339().or_else(space)
        } else {
            space().or_else(rfc3339)
        }
    };

    if aware_hint {
        if let Some(dt) = parse_aware(t_sep_hint) {
            return Some(Value::TimestampTz(dt));
        }
    }
    let naive_fmts = if t_sep_hint {
        [
            "%Y-%m-%dT%H:%M:%S%.f",
            "%Y-%m-%d %H:%M:%S%.f",
            "%Y-%m-%d %H:%M:%S",
        ]
    } else {
        [
            "%Y-%m-%d %H:%M:%S%.f",
            "%Y-%m-%dT%H:%M:%S%.f",
            "%Y-%m-%d %H:%M:%S",
        ]
    };
    for fmt in naive_fmts {
        if let Ok(ndt) = NaiveDateTime::parse_from_str(s, fmt) {
            return Some(Value::Timestamp(ndt));
        }
    }
    if !aware_hint {
        // The probe guessed "naive" but nothing matched: still try the aware
        // parsers (chrono accepts e.g. 1-digit month/day, which shifts the
        // offset before byte 19), so the probe can never reject a string the
        // exhaustive chain used to accept.
        if let Some(dt) = parse_aware(false) {
            return Some(Value::TimestampTz(dt));
        }
    }
    None
}

/// Decode a SQLite cell using the column's decode plan (derived from its
/// declared type) so that uuid/json/datetime/decimal columns round-trip to
/// native Python types — keeping the model layer identical across backends.
/// Aggregates (no declared type) fall back to the storage class, as does any
/// cell whose content fails its column's typed decode.
pub fn decode_sqlite(plan: SqliteDecode, vr: rusqlite::types::ValueRef) -> Value {
    use rusqlite::types::ValueRef as R;

    // Borrow the cell's text instead of copying it for each typed decode
    // attempt. SQLite does not enforce UTF-8, so invalid bytes take the lossy
    // (allocating) path — exactly the string the previous
    // `from_utf8_lossy(..).into_owned()` produced.
    fn text(vr: rusqlite::types::ValueRef<'_>) -> Option<Cow<'_, str>> {
        match vr {
            rusqlite::types::ValueRef::Text(t) => Some(String::from_utf8_lossy(t)),
            _ => None,
        }
    }

    if let R::Null = vr {
        return Value::Null;
    }

    match plan {
        SqliteDecode::Bool => {
            if let R::Integer(i) = vr {
                return Value::Bool(i != 0);
            }
        }
        SqliteDecode::Uuid => {
            if let Some(s) = text(vr) {
                if let Ok(u) = uuid::Uuid::parse_str(&s) {
                    return Value::Uuid(u);
                }
            }
        }
        SqliteDecode::Json => {
            if let Some(s) = text(vr) {
                if let Ok(j) = serde_json::from_str(&s) {
                    return Value::Json(j);
                }
            }
        }
        SqliteDecode::DateTime => {
            if let Some(s) = text(vr) {
                if let Some(v) = parse_sqlite_datetime(&s) {
                    return v;
                }
            }
        }
        SqliteDecode::Date => {
            if let Some(s) = text(vr) {
                if let Ok(nd) = NaiveDate::parse_from_str(&s, "%Y-%m-%d") {
                    return Value::Date(nd);
                }
            }
        }
        SqliteDecode::Time => {
            if let Some(s) = text(vr) {
                for fmt in ["%H:%M:%S%.f", "%H:%M:%S"] {
                    if let Ok(nt) = NaiveTime::parse_from_str(&s, fmt) {
                        return Value::Time(nt);
                    }
                }
            }
        }
        SqliteDecode::Decimal => {
            let parsed = match vr {
                R::Text(t) => {
                    rust_decimal::Decimal::from_str_exact(&String::from_utf8_lossy(t)).ok()
                }
                R::Real(r) => rust_decimal::Decimal::from_str_exact(&r.to_string()).ok(),
                R::Integer(i) => rust_decimal::Decimal::from_str_exact(&i.to_string()).ok(),
                _ => None,
            };
            if let Some(d) = parsed {
                return Value::Decimal(d);
            }
        }
        SqliteDecode::Raw => {}
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

#[cfg(test)]
mod tests {
    use super::*;
    use rusqlite::types::ValueRef as R;

    #[test]
    fn decode_plan_maps_declared_types_to_tags() {
        assert_eq!(sqlite_decode_plan("BOOLEAN"), SqliteDecode::Bool);
        assert_eq!(sqlite_decode_plan("UUID"), SqliteDecode::Uuid);
        assert_eq!(sqlite_decode_plan("uuid"), SqliteDecode::Uuid); // any case
        assert_eq!(sqlite_decode_plan("JSON"), SqliteDecode::Json);
        assert_eq!(sqlite_decode_plan("JSONB"), SqliteDecode::Json);
        assert_eq!(sqlite_decode_plan("DATE"), SqliteDecode::Date);
        assert_eq!(sqlite_decode_plan("TIME"), SqliteDecode::Time);
        assert_eq!(sqlite_decode_plan("DECIMAL(10,2)"), SqliteDecode::Decimal);
        assert_eq!(sqlite_decode_plan("NUMERIC"), SqliteDecode::Decimal);
        // Aggregates / expressions have no declared type; plain storage-class
        // declarations carry no recognised keyword.
        assert_eq!(sqlite_decode_plan(""), SqliteDecode::Raw);
        assert_eq!(sqlite_decode_plan("TEXT"), SqliteDecode::Raw);
        assert_eq!(sqlite_decode_plan("VARCHAR(255)"), SqliteDecode::Raw);
        assert_eq!(sqlite_decode_plan("INTEGER"), SqliteDecode::Raw);
    }

    #[test]
    fn decode_plan_timestamp_keywords_beat_their_date_time_substrings() {
        // "TIMESTAMP" contains "TIME" and "DATETIME" contains both "DATE" and
        // "TIME": the datetime tag must win.
        assert_eq!(sqlite_decode_plan("TIMESTAMP"), SqliteDecode::DateTime);
        assert_eq!(sqlite_decode_plan("TIMESTAMPTZ"), SqliteDecode::DateTime);
        assert_eq!(sqlite_decode_plan("DATETIME"), SqliteDecode::DateTime);
    }

    fn dt_text(s: &str) -> Value {
        decode_sqlite(SqliteDecode::DateTime, R::Text(s.as_bytes()))
    }

    fn utc(s: &str) -> DateTime<Utc> {
        DateTime::parse_from_rfc3339(s).unwrap().with_timezone(&Utc)
    }

    #[test]
    fn datetime_decodes_the_canonical_aware_layout() {
        // Space-separated with explicit offset: what `TimestampTz` binds write.
        match dt_text("2024-01-02 03:04:05.123456+00:00") {
            Value::TimestampTz(dt) => assert_eq!(dt, utc("2024-01-02T03:04:05.123456+00:00")),
            other => panic!("expected TimestampTz, got {other:?}"),
        }
        // A non-UTC offset must normalise to UTC.
        match dt_text("2024-01-02 03:04:05+02:00") {
            Value::TimestampTz(dt) => assert_eq!(dt, utc("2024-01-02T01:04:05+00:00")),
            other => panic!("expected TimestampTz, got {other:?}"),
        }
    }

    #[test]
    fn datetime_decodes_legacy_rfc3339_rows() {
        // 'T'-separated rows written by older releases stay decodable as aware.
        match dt_text("2024-01-02T03:04:05.123456+00:00") {
            Value::TimestampTz(dt) => assert_eq!(dt, utc("2024-01-02T03:04:05.123456+00:00")),
            other => panic!("expected TimestampTz, got {other:?}"),
        }
        match dt_text("2024-01-02T03:04:05Z") {
            Value::TimestampTz(dt) => assert_eq!(dt, utc("2024-01-02T03:04:05+00:00")),
            other => panic!("expected TimestampTz, got {other:?}"),
        }
    }

    #[test]
    fn datetime_decodes_naive_layouts() {
        let expected = NaiveDate::from_ymd_opt(2024, 1, 2)
            .unwrap()
            .and_hms_micro_opt(3, 4, 5, 123_456)
            .unwrap();
        for s in [
            "2024-01-02 03:04:05.123456", // canonical naive (space, fraction)
            "2024-01-02T03:04:05.123456", // legacy 'T' separator
        ] {
            match dt_text(s) {
                Value::Timestamp(ndt) => assert_eq!(ndt, expected, "{s}"),
                other => panic!("expected Timestamp for {s}, got {other:?}"),
            }
        }
        // No fractional part at all.
        match dt_text("2024-01-02 03:04:05") {
            Value::Timestamp(ndt) => {
                assert_eq!(
                    ndt,
                    NaiveDate::from_ymd_opt(2024, 1, 2)
                        .unwrap()
                        .and_hms_opt(3, 4, 5)
                        .unwrap()
                )
            }
            other => panic!("expected Timestamp, got {other:?}"),
        }
    }

    #[test]
    fn datetime_shape_probe_never_rejects_flexible_chrono_forms() {
        // chrono tolerates 1-digit month/day, which shifts the offset before
        // byte 19 — the probe's fallback chain must still accept it.
        match dt_text("2024-1-2 03:04:05+02:00") {
            Value::TimestampTz(dt) => assert_eq!(dt, utc("2024-01-02T01:04:05+00:00")),
            other => panic!("expected TimestampTz, got {other:?}"),
        }
    }

    #[test]
    fn undecodable_cells_fall_back_to_the_storage_class() {
        match dt_text("not a datetime") {
            Value::Text(s) => assert_eq!(s, "not a datetime"),
            other => panic!("expected Text fallback, got {other:?}"),
        }
        match decode_sqlite(SqliteDecode::Uuid, R::Text(b"not-a-uuid")) {
            Value::Text(s) => assert_eq!(s, "not-a-uuid"),
            other => panic!("expected Text fallback, got {other:?}"),
        }
        // A BOOL-tagged TEXT cell keeps its text (only INTEGER maps to bool).
        match decode_sqlite(SqliteDecode::Bool, R::Text(b"true")) {
            Value::Text(s) => assert_eq!(s, "true"),
            other => panic!("expected Text fallback, got {other:?}"),
        }
        match decode_sqlite(SqliteDecode::Decimal, R::Blob(b"\x01")) {
            Value::Bytes(b) => assert_eq!(b, vec![1]),
            other => panic!("expected Bytes fallback, got {other:?}"),
        }
    }

    #[test]
    fn typed_tags_decode_their_cells() {
        assert!(matches!(
            decode_sqlite(SqliteDecode::Bool, R::Integer(1)),
            Value::Bool(true)
        ));
        assert!(matches!(
            decode_sqlite(SqliteDecode::Bool, R::Integer(0)),
            Value::Bool(false)
        ));
        match decode_sqlite(
            SqliteDecode::Uuid,
            R::Text(b"6a3e93b6-16f6-4d9b-9c07-4d5a3f5d2a10"),
        ) {
            Value::Uuid(u) => {
                assert_eq!(u.to_string(), "6a3e93b6-16f6-4d9b-9c07-4d5a3f5d2a10")
            }
            other => panic!("expected Uuid, got {other:?}"),
        }
        match decode_sqlite(SqliteDecode::Json, R::Text(br#"{"a": 1}"#)) {
            Value::Json(j) => assert_eq!(j, serde_json::json!({"a": 1})),
            other => panic!("expected Json, got {other:?}"),
        }
        match decode_sqlite(SqliteDecode::Date, R::Text(b"2024-01-02")) {
            Value::Date(d) => assert_eq!(d, NaiveDate::from_ymd_opt(2024, 1, 2).unwrap()),
            other => panic!("expected Date, got {other:?}"),
        }
        match decode_sqlite(SqliteDecode::Time, R::Text(b"03:04:05.123456")) {
            Value::Time(t) => {
                assert_eq!(t, NaiveTime::from_hms_micro_opt(3, 4, 5, 123_456).unwrap())
            }
            other => panic!("expected Time, got {other:?}"),
        }
        match decode_sqlite(SqliteDecode::Time, R::Text(b"03:04:05")) {
            Value::Time(t) => assert_eq!(t, NaiveTime::from_hms_opt(3, 4, 5).unwrap()),
            other => panic!("expected Time, got {other:?}"),
        }
        match decode_sqlite(SqliteDecode::Decimal, R::Text(b"12.34")) {
            Value::Decimal(d) => assert_eq!(d.to_string(), "12.34"),
            other => panic!("expected Decimal, got {other:?}"),
        }
        // DECIMAL columns also accept numeric storage classes.
        match decode_sqlite(SqliteDecode::Decimal, R::Integer(7)) {
            Value::Decimal(d) => assert_eq!(d.to_string(), "7"),
            other => panic!("expected Decimal, got {other:?}"),
        }
        match decode_sqlite(SqliteDecode::Decimal, R::Real(2.5)) {
            Value::Decimal(d) => assert_eq!(d.to_string(), "2.5"),
            other => panic!("expected Decimal, got {other:?}"),
        }
    }

    #[test]
    fn raw_tag_decodes_by_storage_class() {
        assert!(matches!(
            decode_sqlite(SqliteDecode::Raw, R::Null),
            Value::Null
        ));
        assert!(matches!(
            decode_sqlite(SqliteDecode::Raw, R::Integer(42)),
            Value::Int(42)
        ));
        assert!(matches!(
            decode_sqlite(SqliteDecode::Raw, R::Real(1.5)),
            Value::Float(f) if f == 1.5
        ));
        match decode_sqlite(SqliteDecode::Raw, R::Text(b"hello")) {
            Value::Text(s) => assert_eq!(s, "hello"),
            other => panic!("expected Text, got {other:?}"),
        }
        match decode_sqlite(SqliteDecode::Raw, R::Blob(b"\x00\x01")) {
            Value::Bytes(b) => assert_eq!(b, vec![0, 1]),
            other => panic!("expected Bytes, got {other:?}"),
        }
    }
}

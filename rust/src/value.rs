//! Database-agnostic value type and Python <-> Rust <-> SQL conversions.
//!
//! `Value` is the single currency exchanged between the Python layer, the Rust
//! engine and the database driver. Keeping every conversion in one place means a
//! new backend only has to map `Value` onto its own driver types.

use std::error::Error;

use chrono::{DateTime, NaiveDate, NaiveDateTime, NaiveTime, Utc};
use pyo3::prelude::*;
use pyo3::types::{
    PyBool, PyBytes, PyDate, PyDateTime, PyDict, PyFloat, PyInt, PyList, PyString, PyTime,
};
use pyo3::Borrowed;
use tokio_postgres::types::{to_sql_checked, IsNull, ToSql, Type};

#[derive(Debug, Clone)]
pub enum Value {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Text(String),
    Bytes(Vec<u8>),
    Json(serde_json::Value),
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
            if let Ok(dt) = ob.extract::<DateTime<Utc>>() {
                return Ok(Value::TimestampTz(dt));
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
            return Ok(Value::Json(py_to_json(ob)?));
        }
        // uuid.UUID has no dedicated Python C-type; match on its qualified name.
        if let Ok(qual) = ob.get_type().qualname() {
            if qual.to_string() == "UUID" {
                let s = ob.str()?.to_string();
                let parsed = uuid::Uuid::parse_str(&s)
                    .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
                return Ok(Value::Uuid(parsed));
            }
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
    if ob.is_instance_of::<PyList>() {
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
    Err(pyo3::exceptions::PyTypeError::new_err(
        "value is not JSON serialisable",
    ))
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
            Value::Uuid(v) => {
                let uuid_mod = py.import("uuid")?;
                uuid_mod
                    .getattr("UUID")?
                    .call1((v.to_string(),))?
                    .into_any()
            }
            Value::Decimal(v) => {
                let decimal_mod = py.import("decimal")?;
                decimal_mod
                    .getattr("Decimal")?
                    .call1((v.to_string(),))?
                    .into_any()
            }
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

impl ToSql for Value {
    fn to_sql(
        &self,
        ty: &Type,
        out: &mut tokio_postgres::types::private::BytesMut,
    ) -> Result<IsNull, Box<dyn Error + Sync + Send>> {
        match self {
            Value::Null => Ok(IsNull::Yes),
            Value::Bool(v) => v.to_sql(ty, out),
            Value::Int(v) => match *ty {
                Type::INT2 => (*v as i16).to_sql(ty, out),
                Type::INT4 => (*v as i32).to_sql(ty, out),
                _ => v.to_sql(ty, out),
            },
            Value::Float(v) => match *ty {
                Type::FLOAT4 => (*v as f32).to_sql(ty, out),
                _ => v.to_sql(ty, out),
            },
            Value::Text(v) => v.to_sql(ty, out),
            Value::Bytes(v) => v.to_sql(ty, out),
            Value::Json(v) => v.to_sql(ty, out),
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
pub fn decode_pg_row(row: &tokio_postgres::Row) -> Row {
    let mut out = Row::with_capacity(row.columns().len());
    for (idx, col) in row.columns().iter().enumerate() {
        out.push((col.name().to_string(), decode_pg_cell(row, idx, col.type_())));
    }
    out
}

// ---------------------------------------------------------------------------
// SQLite conversions
// ---------------------------------------------------------------------------

/// Bind a [`Value`] as a SQLite parameter. SQLite has few storage classes, so
/// richer types are encoded as TEXT and reconstructed on read via the declared
/// column type (see [`decode_sqlite`]).
pub fn value_to_sqlite(v: &Value) -> rusqlite::types::Value {
    use rusqlite::types::Value as S;
    match v {
        Value::Null => S::Null,
        Value::Bool(b) => S::Integer(if *b { 1 } else { 0 }),
        Value::Int(i) => S::Integer(*i),
        Value::Float(f) => S::Real(*f),
        Value::Text(s) => S::Text(s.clone()),
        Value::Bytes(b) => S::Blob(b.clone()),
        Value::Json(j) => S::Text(j.to_string()),
        Value::Uuid(u) => S::Text(u.to_string()),
        Value::Decimal(d) => S::Text(d.to_string()),
        Value::Timestamp(dt) => S::Text(dt.format("%Y-%m-%d %H:%M:%S%.6f").to_string()),
        Value::TimestampTz(dt) => S::Text(dt.to_rfc3339()),
        Value::Date(d) => S::Text(d.format("%Y-%m-%d").to_string()),
        Value::Time(t) => S::Text(t.format("%H:%M:%S%.6f").to_string()),
    }
}

/// Decode a SQLite cell using the column's declared type so that uuid/json/
/// datetime/decimal columns round-trip to native Python types — keeping the
/// model layer identical across backends. Aggregates (no declared type) fall
/// back to the storage class.
pub fn decode_sqlite(decl: Option<&str>, vr: rusqlite::types::ValueRef) -> Value {
    use rusqlite::types::ValueRef as R;
    if let R::Null = vr {
        return Value::Null;
    }
    let decl = decl.unwrap_or("").to_ascii_uppercase();
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
            if let Ok(dt) = chrono::DateTime::parse_from_rfc3339(&s) {
                return Value::TimestampTz(dt.with_timezone(&chrono::Utc));
            }
            for fmt in ["%Y-%m-%d %H:%M:%S%.f", "%Y-%m-%dT%H:%M:%S%.f", "%Y-%m-%d %H:%M:%S"] {
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
pub fn decode_pg_row_values(row: &tokio_postgres::Row) -> Vec<Value> {
    let cols = row.columns();
    let mut out = Vec::with_capacity(cols.len());
    for (idx, col) in cols.iter().enumerate() {
        out.push(decode_pg_cell(row, idx, col.type_()));
    }
    out
}

fn decode_pg_cell(row: &tokio_postgres::Row, idx: usize, ty: &Type) -> Value {
    macro_rules! get {
        ($rust:ty, $wrap:expr) => {
            match row.try_get::<_, Option<$rust>>(idx) {
                Ok(Some(v)) => $wrap(v),
                Ok(None) => Value::Null,
                Err(_) => Value::Null,
            }
        };
    }

    if *ty == Type::BOOL {
        get!(bool, Value::Bool)
    } else if *ty == Type::INT2 {
        get!(i16, |v| Value::Int(v as i64))
    } else if *ty == Type::INT4 {
        get!(i32, |v| Value::Int(v as i64))
    } else if *ty == Type::INT8 {
        get!(i64, Value::Int)
    } else if *ty == Type::FLOAT4 {
        get!(f32, |v| Value::Float(v as f64))
    } else if *ty == Type::FLOAT8 {
        get!(f64, Value::Float)
    } else if *ty == Type::VARCHAR
        || *ty == Type::TEXT
        || *ty == Type::BPCHAR
        || *ty == Type::NAME
    {
        get!(String, Value::Text)
    } else if *ty == Type::BYTEA {
        get!(Vec<u8>, Value::Bytes)
    } else if *ty == Type::JSON || *ty == Type::JSONB {
        get!(serde_json::Value, Value::Json)
    } else if *ty == Type::UUID {
        get!(uuid::Uuid, Value::Uuid)
    } else if *ty == Type::NUMERIC {
        get!(rust_decimal::Decimal, Value::Decimal)
    } else if *ty == Type::TIMESTAMP {
        get!(NaiveDateTime, Value::Timestamp)
    } else if *ty == Type::TIMESTAMPTZ {
        get!(DateTime<Utc>, Value::TimestampTz)
    } else if *ty == Type::DATE {
        get!(NaiveDate, Value::Date)
    } else if *ty == Type::TIME {
        get!(NaiveTime, Value::Time)
    } else {
        // Unknown type: fall back to its text representation when possible.
        get!(String, Value::Text)
    }
}

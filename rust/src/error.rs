//! Engine-level error type shared across backends.

use pyo3::exceptions::{PyConnectionError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::PyErr;
use thiserror::Error;

#[derive(Debug, Error)]
pub enum EngineError {
    #[error("unsupported database url: {0}")]
    UnsupportedUrl(String),

    #[error("configuration error: {0}")]
    Config(String),

    #[error("connection error: {0}")]
    Connection(String),

    #[error("query error: {0}")]
    Query(String),

    /// A database integrity constraint was violated (unique, foreign-key,
    /// not-null, check). Surfaced to Python as `yara_orm.exceptions.IntegrityError`
    /// so callers can distinguish "duplicate row" from "bad SQL".
    #[error("{0}")]
    Integrity(String),

    /// Reserved for backends that decode values in Rust (kept for parity across
    /// future backends; the Postgres path surfaces these as `Query`).
    #[allow(dead_code)]
    #[error("type conversion error: {0}")]
    Conversion(String),
}

impl From<tokio_postgres::Error> for EngineError {
    fn from(e: tokio_postgres::Error) -> Self {
        // Prefer the structured DB error: its SQLSTATE distinguishes constraint
        // violations (class 23) from other failures, and its message is the real
        // server text (the top-level `Error` Display is just "db error").
        if let Some(db) = e.as_db_error() {
            let code = db.code().code();
            let msg = db.message().to_string();
            if code.starts_with("23") {
                return EngineError::Integrity(msg);
            }
            return EngineError::Query(format!("{msg} (SQLSTATE {code})"));
        }
        EngineError::Query(e.to_string())
    }
}

/// Build a Python exception of the given `yara_orm.exceptions` class, falling
/// back to `PyRuntimeError` if the class cannot be resolved.
fn typed_pyerr(class: &str, msg: String) -> PyErr {
    Python::attach(|py| {
        match py
            .import("yara_orm.exceptions")
            .and_then(|m| m.getattr(class))
            .and_then(|cls| cls.call1((msg.clone(),)))
        {
            Ok(inst) => PyErr::from_value(inst),
            Err(_) => PyRuntimeError::new_err(msg),
        }
    })
}

/// Convert an engine error into the most appropriate Python exception.
pub fn to_pyerr(e: EngineError) -> PyErr {
    match &e {
        EngineError::UnsupportedUrl(_) | EngineError::Config(_) | EngineError::Conversion(_) => {
            PyValueError::new_err(e.to_string())
        }
        EngineError::Connection(_) => PyConnectionError::new_err(e.to_string()),
        EngineError::Integrity(msg) => typed_pyerr("IntegrityError", msg.clone()),
        EngineError::Query(_) => PyRuntimeError::new_err(e.to_string()),
    }
}

//! Engine-level error type shared across backends.

use pyo3::exceptions::{PyConnectionError, PyRuntimeError, PyValueError};
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

    /// Reserved for backends that decode values in Rust (kept for parity across
    /// future backends; the Postgres path surfaces these as `Query`).
    #[allow(dead_code)]
    #[error("type conversion error: {0}")]
    Conversion(String),
}

impl From<tokio_postgres::Error> for EngineError {
    fn from(e: tokio_postgres::Error) -> Self {
        EngineError::Query(e.to_string())
    }
}

/// Convert an engine error into the most appropriate Python exception.
pub fn to_pyerr(e: EngineError) -> PyErr {
    match e {
        EngineError::UnsupportedUrl(_) | EngineError::Config(_) | EngineError::Conversion(_) => {
            PyValueError::new_err(e.to_string())
        }
        EngineError::Connection(_) => PyConnectionError::new_err(e.to_string()),
        EngineError::Query(_) => PyRuntimeError::new_err(e.to_string()),
    }
}

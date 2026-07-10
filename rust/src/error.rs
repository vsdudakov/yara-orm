//! Engine-level error type shared across backends.

use pyo3::exceptions::{PyRuntimeError, PyValueError};
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

    /// A result value could not be decoded into a Python-visible type (e.g. an
    /// unsupported PostgreSQL type OID). Surfaced to Python as
    /// `yara_orm.exceptions.OperationalError` so it lands in the ORM hierarchy.
    #[error("type conversion error: {0}")]
    Conversion(String),

    /// A no-wait pool checkout (the engine's sync fast path; see
    /// `backend::nowait_scope`) found no free connection. Engine-internal: the
    /// engine catches it and retries the statement on the async bridge, so it
    /// never reaches Python under normal operation.
    #[error("connection pool has no free connection")]
    PoolBusy,
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
pub fn typed_pyerr(class: &str, msg: String) -> PyErr {
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
        EngineError::UnsupportedUrl(_) | EngineError::Config(_) => {
            PyValueError::new_err(e.to_string())
        }
        // Connect/pool-acquire failures land in the ORM hierarchy
        // (`DBConnectionError` subclasses `OperationalError`), so callers can
        // distinguish "database unreachable" from "bad SQL".
        EngineError::Connection(_) => typed_pyerr("DBConnectionError", e.to_string()),
        EngineError::Conversion(_) => typed_pyerr("OperationalError", e.to_string()),
        EngineError::Integrity(msg) => typed_pyerr("IntegrityError", msg.clone()),
        // Query failures (bad SQL, deadlocks, serialization failures) land in the
        // ORM hierarchy so callers can catch `OperationalError` uniformly — the
        // default hot path returns the engine directly (no proxy translation),
        // and retry loops need a stable, catchable type.
        EngineError::Query(_) => typed_pyerr("OperationalError", e.to_string()),
        // Engine-internal (the sync fast path retries it asynchronously); if it
        // ever escapes, treat it like any other pool-acquire failure.
        EngineError::PoolBusy => typed_pyerr("OperationalError", e.to_string()),
    }
}

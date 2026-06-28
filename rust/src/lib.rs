//! Native engine for the `orm` Python package.
//!
//! Responsibilities live behind a thin module: connection pooling, parameter
//! binding and result decoding all happen in Rust, while the Python package
//! owns the model layer and SQL generation. Backends are pluggable (see
//! [`backend`]); PostgreSQL is the first implementation.

mod backend;
mod engine;
mod error;
mod value;

use pyo3::prelude::*;

#[pymodule]
fn _engine(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<engine::Engine>()?;
    m.add_function(wrap_pyfunction!(engine::connect, m)?)?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}

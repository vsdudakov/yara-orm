//! The `Engine` object exposed to Python: a thin async facade over a `Backend`.

use std::sync::Arc;

use pyo3::prelude::*;
use pyo3_async_runtimes::tokio::future_into_py;
use tokio::sync::Mutex;

use crate::backend::{self, Backend, TxConn};
use crate::error::{to_pyerr, typed_pyerr};
use crate::value::{PyRow, PyRows, Value};

#[pyclass]
pub struct Engine {
    backend: Arc<dyn Backend>,
}

#[pymethods]
impl Engine {
    /// Dialect identifier used by the Python layer to render SQL.
    #[getter]
    fn dialect(&self) -> &'static str {
        self.backend.dialect()
    }

    /// Execute a non-returning statement; resolves to the affected row count.
    #[pyo3(signature = (sql, params=Vec::new()))]
    fn execute<'p>(
        &self,
        py: Python<'p>,
        sql: String,
        params: Vec<Value>,
    ) -> PyResult<Bound<'p, PyAny>> {
        let backend = self.backend.clone();
        future_into_py(py, async move {
            backend.execute(&sql, &params).await.map_err(to_pyerr)
        })
    }

    /// Run a query; resolves to a list of dict rows.
    #[pyo3(signature = (sql, params=Vec::new()))]
    fn fetch_all<'p>(
        &self,
        py: Python<'p>,
        sql: String,
        params: Vec<Value>,
    ) -> PyResult<Bound<'p, PyAny>> {
        let backend = self.backend.clone();
        future_into_py(py, async move {
            let rows = backend.fetch_all(&sql, &params).await.map_err(to_pyerr)?;
            Ok(PyRows(rows))
        })
    }

    /// Run a query; resolves to a list of positional value lists (no column
    /// names). The fast path the model layer uses for SELECTs.
    #[pyo3(signature = (sql, params=Vec::new()))]
    fn fetch_rows<'p>(
        &self,
        py: Python<'p>,
        sql: String,
        params: Vec<Value>,
    ) -> PyResult<Bound<'p, PyAny>> {
        let backend = self.backend.clone();
        future_into_py(py, async move {
            backend
                .fetch_all_values(&sql, &params)
                .await
                .map_err(to_pyerr)
        })
    }

    /// Run a query; resolves to the first positional value list or `None`.
    #[pyo3(signature = (sql, params=Vec::new()))]
    fn fetch_row<'p>(
        &self,
        py: Python<'p>,
        sql: String,
        params: Vec<Value>,
    ) -> PyResult<Bound<'p, PyAny>> {
        let backend = self.backend.clone();
        future_into_py(py, async move {
            let rows = backend
                .fetch_all_values(&sql, &params)
                .await
                .map_err(to_pyerr)?;
            Ok(rows.into_iter().next())
        })
    }

    /// Execute the same statement once per row set, pipelined on one
    /// connection; resolves to a list with the first returned row of each
    /// execution (the fast path for bulk inserts with RETURNING).
    fn execute_many<'p>(
        &self,
        py: Python<'p>,
        sql: String,
        rows: Vec<Vec<Value>>,
    ) -> PyResult<Bound<'p, PyAny>> {
        let backend = self.backend.clone();
        future_into_py(py, async move {
            let out = backend.execute_many(&sql, &rows).await.map_err(to_pyerr)?;
            Ok(PyRows(out))
        })
    }

    /// Run a query; resolves to the first row dict or `None`.
    #[pyo3(signature = (sql, params=Vec::new()))]
    fn fetch_one<'p>(
        &self,
        py: Python<'p>,
        sql: String,
        params: Vec<Value>,
    ) -> PyResult<Bound<'p, PyAny>> {
        let backend = self.backend.clone();
        future_into_py(py, async move {
            let rows = backend.fetch_all(&sql, &params).await.map_err(to_pyerr)?;
            Ok(rows.into_iter().next().map(PyRow))
        })
    }

    /// Run pre-split script statements sequentially on one pooled connection,
    /// each in autocommit. Spawned as a detached task so cancelling the Python
    /// awaitable cannot abandon the connection mid-script: the script (and its
    /// trailing open-transaction rollback) always runs to completion.
    fn execute_script<'p>(
        &self,
        py: Python<'p>,
        statements: Vec<String>,
    ) -> PyResult<Bound<'p, PyAny>> {
        let backend = self.backend.clone();
        let handle = pyo3_async_runtimes::tokio::get_runtime()
            .spawn(async move { backend.execute_script(&statements).await });
        future_into_py(py, async move {
            match handle.await {
                Ok(result) => result.map_err(to_pyerr),
                Err(join_err) => Err(to_pyerr(crate::error::EngineError::Connection(
                    join_err.to_string(),
                ))),
            }
        })
    }

    /// Close the underlying connection pool.
    fn close<'p>(&self, py: Python<'p>) -> PyResult<Bound<'p, PyAny>> {
        let backend = self.backend.clone();
        future_into_py(py, async move {
            backend.close().await;
            Ok(())
        })
    }

    /// Begin a transaction; resolves to a `Transaction` bound to one connection.
    ///
    /// `isolation`, when given, names a SQL isolation level applied at BEGIN
    /// (validated per-dialect by the Python layer).
    #[pyo3(signature = (isolation=None))]
    fn begin<'p>(&self, py: Python<'p>, isolation: Option<String>) -> PyResult<Bound<'p, PyAny>> {
        let backend = self.backend.clone();
        future_into_py(py, async move {
            let tx = backend
                .begin_tx(isolation.as_deref())
                .await
                .map_err(to_pyerr)?;
            Ok(Transaction {
                inner: Arc::new(Mutex::new(Some(tx))),
            })
        })
    }
}

#[pyclass]
pub struct Transaction {
    inner: Arc<Mutex<Option<Box<dyn TxConn>>>>,
}

impl Drop for Transaction {
    fn drop(&mut self) {
        // Safety net: if a transaction is dropped without commit/rollback (e.g.
        // the owning coroutine was discarded before `__aexit__` ran), roll it
        // back on the background runtime. Otherwise the pinned connection would
        // be recycled into the pool with `BEGIN` still open (deadpool's Fast
        // recycling performs no reset), corrupting the next consumer's session.
        //
        // The spawned task *awaits* the lock (never skips under contention): a
        // holder is an in-flight statement future, and once it resolves/drops
        // the guard the rollback proceeds. If even the rollback goes wrong, the
        // backend transaction's own drop guard refuses to recycle the
        // connection (it is detached from the pool and closed instead).
        let inner = self.inner.clone();
        pyo3_async_runtimes::tokio::get_runtime().spawn(async move {
            if let Some(tx) = inner.lock().await.take() {
                let _ = tx.rollback().await;
            }
        });
    }
}

fn tx_finished() -> PyErr {
    typed_pyerr(
        "TransactionManagementError",
        "transaction already committed or rolled back".to_string(),
    )
}

#[pymethods]
impl Transaction {
    #[pyo3(signature = (sql, params=Vec::new()))]
    fn execute<'p>(
        &self,
        py: Python<'p>,
        sql: String,
        params: Vec<Value>,
    ) -> PyResult<Bound<'p, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let guard = inner.lock().await;
            let tx = guard.as_ref().ok_or_else(tx_finished)?;
            tx.execute(&sql, &params).await.map_err(to_pyerr)
        })
    }

    #[pyo3(signature = (sql, params=Vec::new()))]
    fn fetch_rows<'p>(
        &self,
        py: Python<'p>,
        sql: String,
        params: Vec<Value>,
    ) -> PyResult<Bound<'p, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let guard = inner.lock().await;
            let tx = guard.as_ref().ok_or_else(tx_finished)?;
            tx.fetch_all_values(&sql, &params).await.map_err(to_pyerr)
        })
    }

    #[pyo3(signature = (sql, params=Vec::new()))]
    fn fetch_row<'p>(
        &self,
        py: Python<'p>,
        sql: String,
        params: Vec<Value>,
    ) -> PyResult<Bound<'p, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let guard = inner.lock().await;
            let tx = guard.as_ref().ok_or_else(tx_finished)?;
            let rows = tx.fetch_all_values(&sql, &params).await.map_err(to_pyerr)?;
            Ok(rows.into_iter().next())
        })
    }

    #[pyo3(signature = (sql, params=Vec::new()))]
    fn fetch_all<'p>(
        &self,
        py: Python<'p>,
        sql: String,
        params: Vec<Value>,
    ) -> PyResult<Bound<'p, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let guard = inner.lock().await;
            let tx = guard.as_ref().ok_or_else(tx_finished)?;
            let rows = tx.fetch_all(&sql, &params).await.map_err(to_pyerr)?;
            Ok(PyRows(rows))
        })
    }

    fn commit<'p>(&self, py: Python<'p>) -> PyResult<Bound<'p, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let tx = inner.lock().await.take().ok_or_else(tx_finished)?;
            tx.commit().await.map_err(to_pyerr)
        })
    }

    fn rollback<'p>(&self, py: Python<'p>) -> PyResult<Bound<'p, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let tx = inner.lock().await.take().ok_or_else(tx_finished)?;
            tx.rollback().await.map_err(to_pyerr)
        })
    }

    /// Establish a savepoint within this transaction (nested-block support).
    fn savepoint<'p>(&self, py: Python<'p>, name: String) -> PyResult<Bound<'p, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let guard = inner.lock().await;
            let tx = guard.as_ref().ok_or_else(tx_finished)?;
            tx.savepoint(&name).await.map_err(to_pyerr)
        })
    }

    /// Release (merge) a previously established savepoint.
    fn release<'p>(&self, py: Python<'p>, name: String) -> PyResult<Bound<'p, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let guard = inner.lock().await;
            let tx = guard.as_ref().ok_or_else(tx_finished)?;
            tx.release(&name).await.map_err(to_pyerr)
        })
    }

    /// Roll back to a previously established savepoint.
    fn rollback_to<'p>(&self, py: Python<'p>, name: String) -> PyResult<Bound<'p, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let guard = inner.lock().await;
            let tx = guard.as_ref().ok_or_else(tx_finished)?;
            tx.rollback_to(&name).await.map_err(to_pyerr)
        })
    }
}

/// Connect to a database and resolve to an [`Engine`].
#[pyfunction]
pub fn connect(py: Python<'_>, url: String) -> PyResult<Bound<'_, PyAny>> {
    future_into_py(py, async move {
        let backend = backend::connect(&url).await.map_err(to_pyerr)?;
        Ok(Engine {
            backend: Arc::from(backend),
        })
    })
}

//! The `Engine` object exposed to Python: a thin async facade over a `Backend`.
//!
//! # The opt-in synchronous fast path
//!
//! When the backend reports [`Backend::sync_capable`] (SQLite with
//! `sync_fast_path=1` in the URL), the statement methods do not schedule the
//! backend future on the tokio runtime and hand Python a pending awaitable
//! (`future_into_py`, measured at ~41µs of bridge overhead around ~0.5–6µs of
//! actual SQLite work). Instead they drive the *same* future to completion
//! right on the calling Python thread with `block_on` and return an
//! already-completed [`Ready`] awaitable. `await`-ing it resumes the caller
//! immediately — no task hop, no `call_soon_threadsafe` round trip.
//!
//! Blocking here is legal: the calling thread is a Python thread, never a
//! tokio worker, so `Runtime::block_on` cannot deadlock the runtime; and the
//! GIL is released for the duration (`py.detach`), so other Python threads
//! keep running. What *does* stay blocked is the current thread's event loop —
//! which is exactly the documented trade-off of the opt-in.

use std::sync::Arc;

use pyo3::exceptions::{PyRuntimeError, PyStopIteration};
use pyo3::prelude::*;
use pyo3::IntoPyObjectExt;
use pyo3_async_runtimes::tokio::future_into_py;
use tokio::sync::Mutex;

use crate::backend::{self, Backend, TxConn};
use crate::error::{to_pyerr, typed_pyerr};
use crate::value::{PyRow, PyRows, Value};

/// What one `__next__` step of a completed awaitable must do.
///
/// Extracted from [`Ready`] so the take-once protocol is testable without a
/// Python interpreter (the pyclass itself needs the GIL).
#[derive(Debug, PartialEq, Eq)]
enum ReadyStep<T, E> {
    /// First step: finish the coroutine, returning the value via
    /// `StopIteration(value)`.
    Return(T),
    /// First step of a failed call: raise the deferred error.
    Raise(E),
    /// Any later step: awaiting a completed awaitable twice is a caller bug.
    AlreadyConsumed,
}

/// Consume `slot` and return the step to perform (take-once semantics).
fn ready_step<T, E>(slot: &mut Option<Result<T, E>>) -> ReadyStep<T, E> {
    match slot.take() {
        Some(Ok(value)) => ReadyStep::Return(value),
        Some(Err(err)) => ReadyStep::Raise(err),
        None => ReadyStep::AlreadyConsumed,
    }
}

/// An already-completed awaitable: the standard completed-awaitable protocol.
///
/// `__await__` returns an iterator whose first `__next__` immediately raises
/// `StopIteration(value)` (or the deferred exception), so `await ready`
/// resumes the caller with the value without ever suspending. The result —
/// success *or* error — is stored and only surfaced on `await`, keeping the
/// fast path's semantics and exception provenance identical to the async path
/// (errors raise where you `await`, not where you call).
///
/// Also usable with `asyncio.ensure_future` / `gather`, which wrap generic
/// awaitables. Like a coroutine, it can only be awaited once.
#[pyclass]
pub struct Ready {
    result: Option<PyResult<Py<PyAny>>>,
}

#[pymethods]
impl Ready {
    fn __await__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    fn __next__(&mut self) -> PyResult<()> {
        match ready_step(&mut self.result) {
            ReadyStep::Return(value) => Err(PyStopIteration::new_err((value,))),
            ReadyStep::Raise(err) => Err(err),
            ReadyStep::AlreadyConsumed => Err(PyRuntimeError::new_err(
                "cannot reuse already awaited engine result",
            )),
        }
    }
}

/// Run a statement future to completion synchronously and wrap the outcome in
/// a [`Ready`] awaitable (the sync fast path; see the module docs).
fn run_sync<'p, T, F>(py: Python<'p>, fut: F) -> PyResult<Bound<'p, PyAny>>
where
    F: std::future::Future<Output = PyResult<T>> + Send,
    T: for<'py> IntoPyObject<'py> + Send,
{
    // block_on from this thread is legal: the Python caller thread is never a
    // tokio worker (worker threads never call into Python entry points), so
    // blocking it cannot starve the runtime driving the future. The GIL is
    // released while we wait, so other Python threads proceed.
    let result = py.detach(|| pyo3_async_runtimes::tokio::get_runtime().block_on(fut));
    // Errors are *stored*, not raised here: they must surface on `await`,
    // exactly like the async path.
    let result = result.and_then(|value| value.into_py_any(py));
    let ready = Bound::new(
        py,
        Ready {
            result: Some(result),
        },
    )?;
    Ok(ready.into_any())
}

#[pyclass]
pub struct Engine {
    backend: Arc<dyn Backend>,
    /// Statement calls run the backend future synchronously (see module docs).
    sync: bool,
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
        let fut = async move { backend.execute(&sql, &params).await.map_err(to_pyerr) };
        if self.sync {
            return run_sync(py, fut);
        }
        future_into_py(py, fut)
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
        let fut = async move {
            let rows = backend.fetch_all(&sql, &params).await.map_err(to_pyerr)?;
            Ok(PyRows(rows))
        };
        if self.sync {
            return run_sync(py, fut);
        }
        future_into_py(py, fut)
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
        let fut = async move {
            backend
                .fetch_all_values(&sql, &params)
                .await
                .map_err(to_pyerr)
        };
        if self.sync {
            return run_sync(py, fut);
        }
        future_into_py(py, fut)
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
        let fut = async move {
            let rows = backend
                .fetch_all_values(&sql, &params)
                .await
                .map_err(to_pyerr)?;
            Ok(rows.into_iter().next())
        };
        if self.sync {
            return run_sync(py, fut);
        }
        future_into_py(py, fut)
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
        let fut = async move {
            let out = backend.execute_many(&sql, &rows).await.map_err(to_pyerr)?;
            Ok(PyRows(out))
        };
        if self.sync {
            return run_sync(py, fut);
        }
        future_into_py(py, fut)
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
        let fut = async move {
            let rows = backend.fetch_all(&sql, &params).await.map_err(to_pyerr)?;
            Ok(rows.into_iter().next().map(PyRow))
        };
        if self.sync {
            return run_sync(py, fut);
        }
        future_into_py(py, fut)
    }

    /// Run pre-split script statements sequentially on one pooled connection,
    /// each in autocommit. Spawned as a detached task so cancelling the Python
    /// awaitable cannot abandon the connection mid-script: the script (and its
    /// trailing open-transaction rollback) always runs to completion.
    ///
    /// Always async, even on the sync fast path: scripts are arbitrary user
    /// SQL (migrations, bulk DDL) that can run for seconds — never acceptable
    /// to run while blocking the caller.
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
    ///
    /// Always async — even on the sync fast path. On SQLite, `BEGIN IMMEDIATE`
    /// queues behind whole competing transactions and can park on
    /// `busy_timeout` for up to 5 seconds; blocking the event loop that long
    /// is never acceptable. The returned `Transaction` inherits the fast-path
    /// flag: once BEGIN has succeeded the connection holds the write lock, so
    /// its statements and COMMIT/ROLLBACK/savepoints complete in microseconds
    /// and may run synchronously.
    #[pyo3(signature = (isolation=None))]
    fn begin<'p>(&self, py: Python<'p>, isolation: Option<String>) -> PyResult<Bound<'p, PyAny>> {
        let backend = self.backend.clone();
        let sync = self.sync;
        future_into_py(py, async move {
            let tx = backend
                .begin_tx(isolation.as_deref())
                .await
                .map_err(to_pyerr)?;
            Ok(Transaction {
                inner: Arc::new(Mutex::new(Some(tx))),
                sync,
            })
        })
    }
}

#[pyclass]
pub struct Transaction {
    inner: Arc<Mutex<Option<Box<dyn TxConn>>>>,
    /// Inherited from the parent engine: statement and control calls run the
    /// backend future synchronously (see the module docs). The Drop rollback
    /// guard is unaffected — it always runs on the background runtime.
    sync: bool,
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
    // Statement *and* control methods honour the sync fast path: the
    // transaction already holds its pinned connection (and, on SQLite, the
    // write lock since BEGIN IMMEDIATE), so nothing here waits on other
    // connections — each call is microseconds of in-process work. The tokio
    // mutex is only ever contended by this transaction's own calls.

    #[pyo3(signature = (sql, params=Vec::new()))]
    fn execute<'p>(
        &self,
        py: Python<'p>,
        sql: String,
        params: Vec<Value>,
    ) -> PyResult<Bound<'p, PyAny>> {
        let inner = self.inner.clone();
        let fut = async move {
            let guard = inner.lock().await;
            let tx = guard.as_ref().ok_or_else(tx_finished)?;
            tx.execute(&sql, &params).await.map_err(to_pyerr)
        };
        if self.sync {
            return run_sync(py, fut);
        }
        future_into_py(py, fut)
    }

    #[pyo3(signature = (sql, params=Vec::new()))]
    fn fetch_rows<'p>(
        &self,
        py: Python<'p>,
        sql: String,
        params: Vec<Value>,
    ) -> PyResult<Bound<'p, PyAny>> {
        let inner = self.inner.clone();
        let fut = async move {
            let guard = inner.lock().await;
            let tx = guard.as_ref().ok_or_else(tx_finished)?;
            tx.fetch_all_values(&sql, &params).await.map_err(to_pyerr)
        };
        if self.sync {
            return run_sync(py, fut);
        }
        future_into_py(py, fut)
    }

    #[pyo3(signature = (sql, params=Vec::new()))]
    fn fetch_row<'p>(
        &self,
        py: Python<'p>,
        sql: String,
        params: Vec<Value>,
    ) -> PyResult<Bound<'p, PyAny>> {
        let inner = self.inner.clone();
        let fut = async move {
            let guard = inner.lock().await;
            let tx = guard.as_ref().ok_or_else(tx_finished)?;
            let rows = tx.fetch_all_values(&sql, &params).await.map_err(to_pyerr)?;
            Ok(rows.into_iter().next())
        };
        if self.sync {
            return run_sync(py, fut);
        }
        future_into_py(py, fut)
    }

    #[pyo3(signature = (sql, params=Vec::new()))]
    fn fetch_all<'p>(
        &self,
        py: Python<'p>,
        sql: String,
        params: Vec<Value>,
    ) -> PyResult<Bound<'p, PyAny>> {
        let inner = self.inner.clone();
        let fut = async move {
            let guard = inner.lock().await;
            let tx = guard.as_ref().ok_or_else(tx_finished)?;
            let rows = tx.fetch_all(&sql, &params).await.map_err(to_pyerr)?;
            Ok(PyRows(rows))
        };
        if self.sync {
            return run_sync(py, fut);
        }
        future_into_py(py, fut)
    }

    fn commit<'p>(&self, py: Python<'p>) -> PyResult<Bound<'p, PyAny>> {
        let inner = self.inner.clone();
        let fut = async move {
            let tx = inner.lock().await.take().ok_or_else(tx_finished)?;
            tx.commit().await.map_err(to_pyerr)
        };
        if self.sync {
            return run_sync(py, fut);
        }
        future_into_py(py, fut)
    }

    fn rollback<'p>(&self, py: Python<'p>) -> PyResult<Bound<'p, PyAny>> {
        let inner = self.inner.clone();
        let fut = async move {
            let tx = inner.lock().await.take().ok_or_else(tx_finished)?;
            tx.rollback().await.map_err(to_pyerr)
        };
        if self.sync {
            return run_sync(py, fut);
        }
        future_into_py(py, fut)
    }

    /// Establish a savepoint within this transaction (nested-block support).
    fn savepoint<'p>(&self, py: Python<'p>, name: String) -> PyResult<Bound<'p, PyAny>> {
        let inner = self.inner.clone();
        let fut = async move {
            let guard = inner.lock().await;
            let tx = guard.as_ref().ok_or_else(tx_finished)?;
            tx.savepoint(&name).await.map_err(to_pyerr)
        };
        if self.sync {
            return run_sync(py, fut);
        }
        future_into_py(py, fut)
    }

    /// Release (merge) a previously established savepoint.
    fn release<'p>(&self, py: Python<'p>, name: String) -> PyResult<Bound<'p, PyAny>> {
        let inner = self.inner.clone();
        let fut = async move {
            let guard = inner.lock().await;
            let tx = guard.as_ref().ok_or_else(tx_finished)?;
            tx.release(&name).await.map_err(to_pyerr)
        };
        if self.sync {
            return run_sync(py, fut);
        }
        future_into_py(py, fut)
    }

    /// Roll back to a previously established savepoint.
    fn rollback_to<'p>(&self, py: Python<'p>, name: String) -> PyResult<Bound<'p, PyAny>> {
        let inner = self.inner.clone();
        let fut = async move {
            let guard = inner.lock().await;
            let tx = guard.as_ref().ok_or_else(tx_finished)?;
            tx.rollback_to(&name).await.map_err(to_pyerr)
        };
        if self.sync {
            return run_sync(py, fut);
        }
        future_into_py(py, fut)
    }
}

/// Connect to a database and resolve to an [`Engine`].
#[pyfunction]
pub fn connect(py: Python<'_>, url: String) -> PyResult<Bound<'_, PyAny>> {
    future_into_py(py, async move {
        let backend = backend::connect(&url).await.map_err(to_pyerr)?;
        // Latched once at connect time: the backend's capability is fixed by
        // its URL, so the statement methods branch on a plain bool.
        let sync = backend.sync_capable();
        Ok(Engine {
            backend: Arc::from(backend),
            sync,
        })
    })
}

#[cfg(test)]
mod tests {
    use super::{ready_step, ReadyStep};

    #[test]
    fn ready_step_returns_the_value_once_then_reports_reuse() {
        // GIVEN a completed awaitable slot holding a success value
        let mut slot: Option<Result<u64, &str>> = Some(Ok(7));
        // WHEN it is stepped: the first step finishes with the value
        // (StopIteration(value) at the pyclass layer)...
        assert_eq!(ready_step(&mut slot), ReadyStep::Return(7));
        // ...THEN every later step reports the double-await misuse.
        assert_eq!(ready_step(&mut slot), ReadyStep::AlreadyConsumed);
        assert_eq!(ready_step(&mut slot), ReadyStep::AlreadyConsumed);
    }

    #[test]
    fn ready_step_defers_errors_to_the_await() {
        // GIVEN a completed awaitable slot holding a deferred error
        let mut slot: Option<Result<u64, &str>> = Some(Err("integrity"));
        // WHEN it is stepped THEN the stored error is raised at that step
        // (i.e. at `await`, not at call time), exactly once.
        assert_eq!(ready_step(&mut slot), ReadyStep::Raise("integrity"));
        assert_eq!(ready_step(&mut slot), ReadyStep::AlreadyConsumed);
    }
}

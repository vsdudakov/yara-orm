//! Connection-URL pool/cache options.
//!
//! The backends below build their pools from a plain connection URL. We let a
//! handful of pool/cache knobs ride along as URL query parameters and strip
//! them out here *before* the URL reaches the driver's own parser (which would
//! otherwise reject the unknown keys). The recognised keys are:
//!
//! - `max_size` — maximum pooled connections.
//! - `min_size` — connections to pre-warm at startup (best effort: the pools
//!   are lazy and keep no hard minimum, so this just primes idle connections).
//! - `statement_cache_size` — `0` disables server-side prepared-statement
//!   caching, which is what makes the backend safe behind a transaction-pooling
//!   connection proxy (e.g. PgBouncer). Any non-zero value keeps caching on.

use crate::error::EngineError;

/// Pool/cache options parsed out of a connection URL.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PoolParams {
    pub max_size: Option<usize>,
    pub min_size: Option<usize>,
    /// Whether to cache prepared statements per connection. `false` when the
    /// URL carries `statement_cache_size=0`.
    pub cache_statements: bool,
}

impl Default for PoolParams {
    fn default() -> Self {
        PoolParams {
            max_size: None,
            min_size: None,
            cache_statements: true,
        }
    }
}

fn parse_usize(key: &str, val: &str) -> Result<usize, EngineError> {
    val.parse::<usize>()
        .map_err(|_| EngineError::Config(format!("invalid {key} value: {val:?}")))
}

/// Split the recognised pool/cache parameters out of `url`, returning the URL
/// with those parameters removed plus the parsed [`PoolParams`]. Unrecognised
/// query parameters are preserved so the driver still sees them.
pub fn extract_pool_params(url: &str) -> Result<(String, PoolParams), EngineError> {
    let mut params = PoolParams::default();
    let (base, query) = match url.split_once('?') {
        Some(parts) => parts,
        None => return Ok((url.to_string(), params)),
    };

    let mut kept: Vec<&str> = Vec::new();
    for pair in query.split('&') {
        if pair.is_empty() {
            continue;
        }
        let (key, val) = pair.split_once('=').unwrap_or((pair, ""));
        match key {
            "max_size" => params.max_size = Some(parse_usize(key, val)?),
            "min_size" => params.min_size = Some(parse_usize(key, val)?),
            "statement_cache_size" => params.cache_statements = parse_usize(key, val)? != 0,
            _ => kept.push(pair),
        }
    }

    let cleaned = if kept.is_empty() {
        base.to_string()
    } else {
        format!("{base}?{}", kept.join("&"))
    };
    Ok((cleaned, params))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn no_query_is_untouched() {
        let (url, p) = extract_pool_params("postgres://h/db").unwrap();
        assert_eq!(url, "postgres://h/db");
        assert_eq!(p, PoolParams::default());
        assert!(p.cache_statements);
    }

    #[test]
    fn pool_params_are_extracted_and_stripped() {
        let (url, p) =
            extract_pool_params("postgres://h/db?max_size=32&min_size=4&statement_cache_size=0")
                .unwrap();
        // Every recognised key is removed, leaving a bare URL for the driver.
        assert_eq!(url, "postgres://h/db");
        assert_eq!(p.max_size, Some(32));
        assert_eq!(p.min_size, Some(4));
        assert!(!p.cache_statements);
    }

    #[test]
    fn unknown_params_are_preserved() {
        let (url, p) =
            extract_pool_params("postgres://h/db?sslmode=require&max_size=8&application_name=x")
                .unwrap();
        assert_eq!(url, "postgres://h/db?sslmode=require&application_name=x");
        assert_eq!(p.max_size, Some(8));
    }

    #[test]
    fn nonzero_statement_cache_keeps_caching_on() {
        let (_, p) = extract_pool_params("postgres://h/db?statement_cache_size=100").unwrap();
        assert!(p.cache_statements);
    }

    #[test]
    fn invalid_numeric_value_is_an_error() {
        let err = extract_pool_params("postgres://h/db?max_size=lots").unwrap_err();
        assert!(matches!(err, EngineError::Config(_)));
    }

    #[test]
    fn sqlite_url_params_are_extracted() {
        let (url, p) = extract_pool_params("sqlite://./data.db?max_size=4").unwrap();
        assert_eq!(url, "sqlite://./data.db");
        assert_eq!(p.max_size, Some(4));
    }
}

//! Unified logging system
//!
//! Writes to ~/.evocli/logs/evocli.log (daily rotation, 7-day retention).
//! When `debug` is true, also prints to stderr.

use anyhow::Result;
use tracing_appender::non_blocking::WorkerGuard;
use tracing_subscriber::prelude::*;
use tracing_subscriber::{fmt, EnvFilter};

/// Initialize the unified logging system.
///
/// Returns a `WorkerGuard` that **must** be held alive in `main` for the
/// non-blocking file appender to flush correctly.
pub fn init(debug_mode: bool) -> Result<WorkerGuard> {
    // Ensure log directory exists
    let log_dir = dirs::home_dir()
        .ok_or_else(|| anyhow::anyhow!("Cannot determine home directory"))?
        .join(".evocli")
        .join("logs");
    std::fs::create_dir_all(&log_dir)?;

    // Daily-rotating file appender
    let file_appender = tracing_appender::rolling::daily(&log_dir, "evocli.log");
    let (non_blocking, guard) = tracing_appender::non_blocking(file_appender);

    let env_filter = if debug_mode {
        "evocli=debug,soul_bridge=debug,evocli_tui=debug"
    } else {
        "evocli=info,soul_bridge=warn,evocli_tui=info"
    };

    // File layer — always active
    let file_layer = fmt::layer()
        .with_writer(non_blocking)
        .with_ansi(false)
        .with_target(true)
        .with_thread_ids(false);

    if debug_mode {
        // Debug mode: file + stderr
        let stderr_layer = fmt::layer().with_writer(std::io::stderr).with_target(true);

        tracing_subscriber::registry()
            .with(EnvFilter::new(env_filter))
            .with(file_layer)
            .with(stderr_layer)
            .init();
    } else {
        // Normal mode: file only
        tracing_subscriber::registry()
            .with(EnvFilter::new(env_filter))
            .with(file_layer)
            .init();
    }

    tracing::info!("Logging initialized");
    Ok(guard)
}

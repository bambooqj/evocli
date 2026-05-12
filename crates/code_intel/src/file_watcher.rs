//! crates/code_intel/src/file_watcher.rs — OS-native file watcher
//!
//! Uses the `notify` crate (v6) for zero-latency OS events:
//!   - Windows: ReadDirectoryChangesW
//!   - Linux:   inotify
//!   - macOS:   FSEvents / kqueue
//!
//! Replaces the previous 500ms polling implementation.
//! API is backward-compatible: same `FileChangedEvent`, `ChangeKind`, `WatcherHandle`.

use anyhow::{Context, Result};
use notify::{Config, EventKind, RecommendedWatcher, RecursiveMode, Watcher};
use std::path::{Path, PathBuf};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    mpsc, Arc,
};

/// File change event delivered to the caller callback.
#[derive(Debug, Clone)]
pub struct FileChangedEvent {
    pub path: PathBuf,
    pub kind: ChangeKind,
}

#[derive(Debug, Clone, PartialEq)]
pub enum ChangeKind {
    Created,
    Modified,
    Deleted,
}

/// Source-code file extensions the watcher pays attention to.
const WATCHED_EXTENSIONS: &[&str] = &["rs", "py", "ts", "tsx", "js", "go"];

/// Start an OS-native file watcher on `root`.
///
/// `callback` is invoked (from a background thread) whenever a watched source
/// file changes.  Returns a `WatcherHandle`; dropping it stops the watcher.
pub fn start_watcher<F>(root: &Path, callback: F) -> Result<WatcherHandle>
where
    F: Fn(FileChangedEvent) + Send + 'static,
{
    let stop = Arc::new(AtomicBool::new(false));
    let stop_dispatch = Arc::clone(&stop);

    let (tx, rx) = mpsc::channel::<notify::Result<notify::Event>>();

    // Create the OS watcher.  The closure sends raw notify events into the channel.
    let mut watcher = RecommendedWatcher::new(
        move |result| {
            let _ = tx.send(result);
        },
        Config::default(),
    )
    .context("Failed to create OS file watcher")?;

    watcher
        .watch(root, RecursiveMode::Recursive)
        .with_context(|| format!("Failed to watch directory: {}", root.display()))?;

    // Dispatch thread: filter events by extension and call the user callback.
    std::thread::spawn(move || {
        for result in rx {
            if stop_dispatch.load(Ordering::Relaxed) {
                break;
            }
            let event = match result {
                Ok(e) => e,
                Err(e) => {
                    tracing::warn!("[file_watcher] notify error: {}", e);
                    continue;
                }
            };

            let kind = match &event.kind {
                EventKind::Create(_) => ChangeKind::Created,
                EventKind::Modify(_) => ChangeKind::Modified,
                EventKind::Remove(_) => ChangeKind::Deleted,
                // Access, Other, Any — not relevant for reindexing
                _ => continue,
            };

            for path in &event.paths {
                let ext = path.extension().and_then(|e| e.to_str()).unwrap_or("");
                if WATCHED_EXTENSIONS.contains(&ext) {
                    callback(FileChangedEvent {
                        path: path.clone(),
                        kind: kind.clone(),
                    });
                }
            }
        }
    });

    Ok(WatcherHandle {
        stop,
        _watcher: watcher,
    })
}

/// Watcher stop handle.
///
/// Dropping this value:
///   1. Sets the `stop` flag so the dispatch thread exits on its next iteration.
///   2. Drops `_watcher`, which causes the OS backend to stop delivering events
///      and closes the internal channel — the dispatch thread loop ends cleanly.
pub struct WatcherHandle {
    stop: Arc<AtomicBool>,
    /// Kept alive so that OS events keep flowing; dropping stops the backend.
    _watcher: RecommendedWatcher,
}

impl Drop for WatcherHandle {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        // _watcher is dropped automatically, which closes the event channel.
    }
}

/// Convenience: watch `root` and automatically re-index changed source files.
pub fn watch_and_reindex(root: &Path) -> Result<WatcherHandle> {
    let root_cb = root.to_path_buf();
    start_watcher(root, move |event| {
        if event.kind == ChangeKind::Deleted {
            return;
        }
        let db_path = root_cb.join(".evocli").join("code_index.db");
        if let Ok(mut idx) = crate::CodeIndex::new(&db_path) {
            if let Err(e) = idx.index_file(&event.path) {
                tracing::warn!(
                    "[file_watcher] Auto-reindex failed for {:?}: {}",
                    event.path,
                    e
                );
            } else {
                tracing::debug!("[file_watcher] Auto-reindexed: {:?}", event.path);
            }
        }
    })
}

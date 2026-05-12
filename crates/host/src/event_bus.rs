//! event_bus.rs — 事件总线（Section 5.4）
//!
//! 所有行为进入统一事件流，写入 SQLite events 表。
//! 用于：观察、分析、Evolution、Replay、Telemetry

use rusqlite::Connection;
use serde::{Deserialize, Serialize};
use tokio::sync::mpsc;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EvoEvent {
    pub id: String,
    pub session_id: String,
    pub event_type: String,
    pub payload: serde_json::Value,
    pub created_at: String,
}

pub struct EventBus {
    tx: mpsc::UnboundedSender<EvoEvent>,
}

impl EventBus {
    pub fn new() -> Self {
        let (tx, mut rx) = mpsc::unbounded_channel::<EvoEvent>();

        // 后台写入任务
        tokio::spawn(async move {
            let db_path = dirs::home_dir()
                .unwrap_or_default()
                .join(".evocli")
                .join("events.db");
            let _ = std::fs::create_dir_all(
                db_path
                    .parent()
                    .unwrap_or_else(|| std::path::Path::new(".")),
            );

            let conn = match Connection::open(&db_path) {
                Ok(c) => c,
                Err(e) => {
                    eprintln!("EventBus DB error: {}", e);
                    return;
                }
            };

            let _ = conn.execute_batch(
                "
                CREATE TABLE IF NOT EXISTS events (
                    id         TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    type       TEXT NOT NULL,
                    payload    TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
                CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
            ",
            );

            while let Some(event) = rx.recv().await {
                let _ = conn.execute(
                    "INSERT OR IGNORE INTO events (id, session_id, type, payload, created_at) VALUES (?1, ?2, ?3, ?4, ?5)",
                    rusqlite::params![
                        event.id,
                        event.session_id,
                        event.event_type,
                        serde_json::to_string(&event.payload).unwrap_or_default(),
                        event.created_at
                    ]
                );
            }
        });

        Self { tx }
    }

    pub fn emit(&self, session_id: &str, event_type: &str, payload: serde_json::Value) {
        let event = EvoEvent {
            id: uuid::Uuid::new_v4().to_string(),
            session_id: session_id.to_string(),
            event_type: event_type.to_string(),
            payload,
            created_at: chrono::Utc::now().to_rfc3339(),
        };
        let _ = self.tx.send(event);
    }

    pub fn tool_called(&self, session_id: &str, tool: &str, success: bool) {
        self.emit(
            session_id,
            "tool_call",
            serde_json::json!({
                "tool": tool, "success": success
            }),
        );
    }

    /// Emitted when a Skill step executes (used by Evolution Engine for pattern detection).
    #[allow(dead_code)]
    pub fn skill_executed(&self, session_id: &str, skill_id: &str, step: &str) {
        self.emit(
            session_id,
            "skill_exec",
            serde_json::json!({
                "skill_id": skill_id, "step": step
            }),
        );
    }

    /// Emitted when memory is recalled (used for memory hit rate stats).
    #[allow(dead_code)]
    pub fn memory_recalled(&self, session_id: &str, query: &str, hits: usize) {
        self.emit(
            session_id,
            "memory_recall",
            serde_json::json!({
                "query": query, "hits": hits
            }),
        );
    }
}

impl Default for EventBus {
    fn default() -> Self {
        Self::new()
    }
}

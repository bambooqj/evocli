//! labeler.rs — SQLite 训练样本存储

use anyhow::Result;
use rusqlite::{params, Connection};
use std::path::{Path, PathBuf};

use crate::{MemoryLabel, MIN_SAMPLES_PER_CLASS, RETRAIN_DELTA};

#[derive(Debug, Clone)]
pub struct LabeledSample {
    pub text: String,
    pub label: MemoryLabel,
    pub project_id: String,
    pub created_at: String,
}

/// SQLite-backed training data store
pub struct TrainingStore {
    conn: Connection,
    path: PathBuf,
}

impl TrainingStore {
    /// Open or create the training store at ~/.evocli/mem_router/training.db
    pub fn open(base_dir: &Path) -> Result<Self> {
        let dir = base_dir.join("mem_router");
        std::fs::create_dir_all(&dir)?;
        let path = dir.join("training.db");
        let conn = Connection::open(&path)?;
        conn.execute_batch(
            r#"
            CREATE TABLE IF NOT EXISTS labeled_samples (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                text       TEXT NOT NULL,
                label      INTEGER NOT NULL,   -- MemoryLabel::as_idx()
                label_name TEXT NOT NULL,      -- human-readable
                project_id TEXT NOT NULL DEFAULT '',
                confidence REAL DEFAULT 1.0,  -- LLM = 1.0, classifier = actual
                source     TEXT DEFAULT 'llm', -- 'llm' | 'classifier'
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_label ON labeled_samples(label);
            CREATE TABLE IF NOT EXISTS model_meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        "#,
        )?;
        Ok(Self { conn, path })
    }

    /// Store an LLM-generated label
    pub fn add_label(&self, sample: &LabeledSample, confidence: f32, source: &str) -> Result<()> {
        let now = chrono::Utc::now().to_rfc3339();
        self.conn.execute(
            "INSERT INTO labeled_samples (text, label, label_name, project_id, confidence, source, created_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
            params![
                sample.text,
                sample.label.as_idx() as i64,
                sample.label.to_string(),
                sample.project_id,
                confidence as f64,
                source,
                now,
            ],
        )?;
        Ok(())
    }

    /// Total number of labeled samples
    pub fn count(&self) -> usize {
        self.conn
            .query_row("SELECT COUNT(*) FROM labeled_samples", [], |r| {
                r.get::<_, i64>(0)
            })
            .unwrap_or(0) as usize
    }

    /// Count per class
    pub fn count_per_class(&self) -> [usize; 6] {
        let mut counts = [0usize; 6];
        let mut stmt = self
            .conn
            .prepare("SELECT label, COUNT(*) FROM labeled_samples GROUP BY label")
            .unwrap();
        let _ = stmt
            .query_map([], |r| {
                let idx: usize = r.get::<_, i64>(0)? as usize;
                let cnt: i64 = r.get(1)?;
                Ok((idx, cnt as usize))
            })
            .map(|rows| {
                for row in rows.flatten() {
                    if row.0 < 6 {
                        counts[row.0] = row.1;
                    }
                }
            });
        counts
    }

    /// Check if we have enough samples to train (all classes have MIN_SAMPLES_PER_CLASS)
    pub fn ready_to_train(&self) -> bool {
        let counts = self.count_per_class();
        // Allow training if at least 4 of 6 classes have enough data
        // (NoWrite might be rare, Constraint might also be rare early on)
        let sufficient = counts
            .iter()
            .filter(|&&c| c >= MIN_SAMPLES_PER_CLASS)
            .count();
        sufficient >= 4
    }

    /// Check if we should retrain (delta since last training)
    pub fn should_retrain(&self) -> bool {
        let total = self.count();
        let last_trained = self
            .get_meta("last_trained_count")
            .and_then(|s| s.parse::<usize>().ok())
            .unwrap_or(0);
        total >= last_trained + RETRAIN_DELTA && self.ready_to_train()
    }

    /// Load all samples for training
    pub fn load_all(&self) -> Result<Vec<(String, usize)>> {
        let mut stmt = self
            .conn
            .prepare("SELECT text, label FROM labeled_samples ORDER BY id")?;
        let rows = stmt.query_map([], |r| {
            Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)? as usize))
        })?;
        Ok(rows.collect::<rusqlite::Result<Vec<_>>>()?)
    }

    pub fn set_meta(&self, key: &str, value: &str) -> Result<()> {
        self.conn.execute(
            "INSERT OR REPLACE INTO model_meta (key, value) VALUES (?1, ?2)",
            params![key, value],
        )?;
        Ok(())
    }

    pub fn get_meta(&self, key: &str) -> Option<String> {
        self.conn
            .query_row(
                "SELECT value FROM model_meta WHERE key = ?1",
                params![key],
                |r| r.get(0),
            )
            .ok()
    }

    pub fn db_path(&self) -> &Path {
        &self.path
    }
}

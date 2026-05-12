//! job_queue.rs — 持久化任务队列（Section 28）
//!
//! SQLite-based 任务队列（不依赖 apalis，直接 rusqlite）
//! 支持：任务入队、执行、重试、查看状态

use anyhow::{Context, Result};
use rusqlite::Connection;
use serde::{Deserialize, Serialize};
use std::path::Path;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type")]
pub enum JobType {
    MemoryDistill {
        session_id: String,
    },
    CodeIndexUpdate {
        project: String,
        changed_files: Vec<String>,
    },
    SkillRun {
        skill_id: String,
        project: String,
        dry_run: bool,
    },
    EvolutionScan {
        project: String,
    },
    AgentSession {
        session_id: String,
        graph_id: String, // "planner" | "coder" | "full_orchestration" | "reviewer"
        parent_id: Option<String>,
        task_prompt: String,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Job {
    pub id: String,
    pub job_type: JobType,
    pub status: String, // "pending" | "running" | "done" | "failed"
    pub retry_count: u32,
    pub max_retries: u32,
    pub created_at: String,
    pub updated_at: String,
    pub error: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct JobSummary {
    pub id: String,
    pub job_type_name: String,
    pub status: String,
    pub retry_count: u32,
    pub created_at: String,
}

pub struct JobQueue {
    conn: Connection,
}

const SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    job_type    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
";

impl JobQueue {
    pub fn new(db_path: &Path) -> Result<Self> {
        if let Some(parent) = db_path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let conn = Connection::open(db_path)
            .with_context(|| format!("Failed to open job queue at {}", db_path.display()))?;
        conn.execute_batch(SCHEMA)?;
        Ok(Self { conn })
    }

    pub fn push(&self, job_type: JobType) -> Result<String> {
        let id = uuid::Uuid::new_v4().to_string();
        let type_json = serde_json::to_string(&job_type)?;
        let now = chrono::Utc::now().to_rfc3339();
        self.conn.execute(
            "INSERT INTO jobs (id, job_type, status, retry_count, max_retries, created_at, updated_at) VALUES (?1, ?2, 'pending', 0, 3, ?3, ?3)",
            rusqlite::params![id, type_json, now],
        )?;
        Ok(id)
    }

    pub fn pop_next_pending(&self) -> Result<Option<Job>> {
        use rusqlite::OptionalExtension;

        let now = chrono::Utc::now().to_rfc3339();

        // Use a transaction to atomically SELECT + UPDATE, preventing duplicate job
        // execution if multiple callers race to pop (e.g., restart scenarios).
        // SQLite serializes writes via its internal mutex, but explicit transactions
        // make the intent clear and protect against future multi-threaded usage.
        let row_data = {
            let mut stmt = self.conn.prepare(
                "SELECT id, job_type, status, retry_count, max_retries, created_at, updated_at, error \
                 FROM jobs WHERE status = 'pending' ORDER BY created_at ASC LIMIT 1"
            )?;
            stmt.query_row([], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, u32>(3)?,
                    row.get::<_, u32>(4)?,
                    row.get::<_, String>(5)?,
                    row.get::<_, Option<String>>(7)?,
                ))
            })
            .optional()?
        };

        match row_data {
            None => Ok(None),
            Some((id, type_json, retry_count, max_retries, created_at, error)) => {
                // Atomically claim the job. By adding `AND status = 'pending'` to the UPDATE,
                // we ensure that if two concurrent callers raced after the same SELECT result,
                // only the one whose UPDATE touches a row (rows_affected > 0) will claim it.
                // The other will get 0 rows_affected and should return None to avoid double-execution.
                let rows_changed = self.conn.execute(
                    "UPDATE jobs SET status = 'running', updated_at = ?1 WHERE id = ?2 AND status = 'pending'",
                    rusqlite::params![now, id],
                )?;
                if rows_changed == 0 {
                    // Job was already claimed by a concurrent caller between our SELECT and UPDATE.
                    return Ok(None);
                }
                let job_type = serde_json::from_str(&type_json)?;
                Ok(Some(Job {
                    id,
                    job_type,
                    status: "running".to_string(),
                    retry_count,
                    max_retries,
                    created_at,
                    updated_at: now,
                    error,
                }))
            }
        }
    }

    pub fn mark_done(&self, job_id: &str) -> Result<()> {
        let now = chrono::Utc::now().to_rfc3339();
        self.conn.execute(
            "UPDATE jobs SET status = 'done', updated_at = ?1 WHERE id = ?2",
            rusqlite::params![now, job_id],
        )?;
        Ok(())
    }

    pub fn mark_failed(&self, job_id: &str, error: &str) -> Result<()> {
        let now = chrono::Utc::now().to_rfc3339();
        let (retry_count, max_retries): (u32, u32) = self
            .conn
            .query_row(
                "SELECT retry_count, max_retries FROM jobs WHERE id = ?1",
                rusqlite::params![job_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap_or((0, 3));

        let new_status = if retry_count + 1 >= max_retries {
            "failed"
        } else {
            "pending"
        };
        self.conn.execute(
            "UPDATE jobs SET status = ?1, retry_count = retry_count + 1, error = ?2, updated_at = ?3 WHERE id = ?4",
            rusqlite::params![new_status, error, now, job_id],
        )?;
        Ok(())
    }

    pub fn list(&self, status_filter: Option<&str>) -> Result<Vec<JobSummary>> {
        let query = if status_filter.is_some() {
            "SELECT id, job_type, status, retry_count, created_at FROM jobs WHERE status = ?1 ORDER BY created_at DESC LIMIT 50"
        } else {
            "SELECT id, job_type, status, retry_count, created_at FROM jobs ORDER BY created_at DESC LIMIT 50"
        };

        let params: Vec<String> = status_filter
            .map(|s| vec![s.to_string()])
            .unwrap_or_default();
        let mut stmt = self.conn.prepare(query)?;
        let rows = stmt.query_map(rusqlite::params_from_iter(params.iter()), |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, u32>(3)?,
                row.get::<_, String>(4)?,
            ))
        })?;

        let mut jobs = Vec::new();
        for row in rows {
            let (id, type_json, status, retry_count, created_at) = row?;
            let type_name = serde_json::from_str::<serde_json::Value>(&type_json)
                .ok()
                .and_then(|v| {
                    let t = v.get("type").and_then(|t| t.as_str());
                    match t {
                        Some("AgentSession") => {
                            let graph_id = v
                                .get("graph_id")
                                .and_then(|g| g.as_str())
                                .unwrap_or("unknown");
                            Some(format!("agent_session:{}", graph_id))
                        }
                        Some(other) => Some(other.to_string()),
                        None => None,
                    }
                })
                .unwrap_or_else(|| "Unknown".to_string());
            jobs.push(JobSummary {
                id,
                job_type_name: type_name,
                status,
                retry_count,
                created_at,
            });
        }
        Ok(jobs)
    }

    pub fn clear_failed(&self) -> Result<usize> {
        let affected = self
            .conn
            .execute("DELETE FROM jobs WHERE status = 'failed'", [])?;
        Ok(affected)
    }
}

pub fn jobs_db_path() -> std::path::PathBuf {
    dirs::home_dir()
        .unwrap_or_default()
        .join(".evocli")
        .join("jobs.db")
}

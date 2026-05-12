//! commands/constraint_cmd.rs — evocli constraint 子命令（Section 6.2）
//!
//! 管理 L1 约束记忆（最高优先级，永不裁剪）
//!
//! 存储：写入 ~/.evocli/data/memories.jsonl（Python Soul 的统一存储格式，JSONL 回退层）
//! H1 迁移后：Python 通过 LanceDB + memories.jsonl 读取约束；Rust memory.db 已废弃。
//! 直接写 JSONL 保证 constraint add 的结果对 Python Soul 可见，无需 IPC。

use anyhow::Result;
use clap::Subcommand;
use std::path::PathBuf;

#[derive(Subcommand)]
pub enum ConstraintAction {
    /// Add a constraint rule to L1 memory
    Add {
        /// Constraint rule text (e.g. "禁止使用 .unwrap()")
        rule: String,
        /// File scope pattern (e.g. "*.rs", "src/**")
        #[arg(long, default_value = "global")]
        scope: String,
        /// Severity level: error | warning | preference
        #[arg(long, default_value = "error")]
        level: String,
    },
    /// List all active constraint rules
    List {
        /// Show only this scope
        #[arg(long)]
        scope: Option<String>,
    },
    /// Remove a constraint rule by ID or text match
    Remove {
        /// Rule ID (first 8 chars) or text to match
        rule: String,
    },
    /// Edit an existing constraint
    Edit {
        /// Rule ID to edit
        id: String,
    },
}

/// Path to the Python Soul's unified JSONL memory store (shared with LanceDB fallback).
/// After H1 migration, all memory operations must use this file so Python can read them.
fn memories_jsonl_path() -> PathBuf {
    dirs::home_dir()
        .unwrap_or_default()
        .join(".evocli")
        .join("data")
        .join("memories.jsonl")
}

/// Read all constraint entries from the shared JSONL store.
fn read_constraints() -> Vec<serde_json::Value> {
    let path = memories_jsonl_path();
    if !path.exists() {
        return vec![];
    }
    match std::fs::read_to_string(&path) {
        Ok(content) => content
            .lines()
            .filter(|l| !l.trim().is_empty())
            .filter_map(|l| serde_json::from_str::<serde_json::Value>(l).ok())
            .filter(|v| v.get("memory_type").and_then(|t| t.as_str()) == Some("constraint"))
            .collect(),
        Err(_) => vec![],
    }
}

/// Append one JSONL entry to the shared memories store.
fn append_entry(entry: &serde_json::Value) -> Result<()> {
    let path = memories_jsonl_path();
    // Ensure directory exists
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let line = serde_json::to_string(entry)? + "\n";
    use std::io::Write;
    let mut f = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)?;
    f.write_all(line.as_bytes())?;
    Ok(())
}

/// Rewrite the JSONL file, dropping entries that match the predicate.
fn remove_entries<F>(should_remove: F) -> Result<usize>
where
    F: Fn(&serde_json::Value) -> bool,
{
    let path = memories_jsonl_path();
    if !path.exists() {
        return Ok(0);
    }
    let content = std::fs::read_to_string(&path)?;
    let mut kept = Vec::new();
    let mut removed = 0usize;
    for line in content.lines() {
        if line.trim().is_empty() {
            continue;
        }
        match serde_json::from_str::<serde_json::Value>(line) {
            Ok(v) if should_remove(&v) => removed += 1,
            _ => kept.push(line.to_string()),
        }
    }
    if removed > 0 {
        let new_content = kept.join("\n") + if kept.is_empty() { "" } else { "\n" };
        std::fs::write(&path, new_content)?;
    }
    Ok(removed)
}

pub fn run(action: ConstraintAction) -> Result<()> {
    match action {
        ConstraintAction::Add { rule, scope, level } => {
            println!("Adding L1 constraint:");
            println!("  Rule:  {}", rule);
            println!("  Scope: {}", scope);
            println!("  Level: {}", level);

            let id = uuid::Uuid::new_v4().to_string();
            let now = chrono::Utc::now().to_rfc3339();
            let body = format!("Constraint [{scope}]: {rule}");
            let tags = serde_json::json!(["constraint", level, scope]);

            // Write to the unified JSONL store that Python Soul reads (H1: LanceDB fallback).
            // Format mirrors Python _JSONLinesStore.add() schema exactly.
            // char-based truncation: constraint rules are user-entered and likely contain Chinese text.
            let title: String = rule.chars().take(80).collect();
            let entry = serde_json::json!({
                "id":               id,
                "title":            title,
                "body":             body,
                "memory_type":      "constraint",
                "priority_scope":   "project",
                "project_id":       std::env::current_dir()
                                        .map(|p| p.to_string_lossy().into_owned())
                                        .unwrap_or_else(|_| ".".to_string()),
                "severity":         level,
                "tags":             tags,
                "importance_score": 1.0,
                "recall_count":     0,
                "created_at":       now,
                "last_accessed_at": now,
            });

            append_entry(&entry)?;
            println!("✅ Constraint saved to memories.jsonl (id: {})", &id[..8]);
            println!("   Python Soul will use this constraint on next session.");
        }

        ConstraintAction::List { scope } => {
            let constraints = read_constraints();
            if constraints.is_empty() {
                println!("No constraints found. Use 'evocli constraint add' to add rules.");
                return Ok(());
            }
            let filtered: Vec<_> = if let Some(ref s) = scope {
                constraints
                    .iter()
                    .filter(|v| v["body"].as_str().map_or(false, |b| b.contains(s.as_str())))
                    .collect()
            } else {
                constraints.iter().collect()
            };
            if filtered.is_empty() {
                println!(
                    "No constraints found for scope '{}'.",
                    scope.unwrap_or_default()
                );
                return Ok(());
            }
            println!("{:<10} {:<12} {}", "ID", "Severity", "Rule");
            println!("{}", "─".repeat(70));
            for v in filtered {
                let id = v["id"].as_str().unwrap_or("?");
                let sev = v["severity"].as_str().unwrap_or("-");
                let body = v["body"]
                    .as_str()
                    .unwrap_or(v["title"].as_str().unwrap_or("?"));
                // Use char-boundary-safe truncation: byte slicing panics on multibyte UTF-8
                // (e.g., Chinese chars span 3 bytes — &str[..60] panics if byte 60 is mid-char).
                let body_display: String = body.chars().take(60).collect();
                println!(
                    "{:<10} {:<12} {}",
                    &id[..8.min(id.len())],
                    sev,
                    body_display
                );
            }
        }

        ConstraintAction::Remove { rule } => {
            let n = remove_entries(|v| {
                let id = v["id"].as_str().unwrap_or("");
                let body = v["body"].as_str().unwrap_or("");
                let title = v["title"].as_str().unwrap_or("");
                id.starts_with(&rule) || body.contains(&rule) || title.contains(&rule)
            })?;
            println!("{} constraint(s) removed.", n);
        }

        ConstraintAction::Edit { id } => {
            println!("To edit constraint {}: use evocli constraint remove {} && evocli constraint add <new_rule>", id, id);
        }
    }
    Ok(())
}

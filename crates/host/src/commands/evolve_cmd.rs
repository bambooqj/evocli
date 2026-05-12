//! commands/evolve_cmd.rs — evocli evolve 子命令（Section 9.5）
//!
//! 进化飞轮仪表盘：查看模式、Skill 草案、飞轮健康度
//! 设计参考：Section 9.5 进化数据飞轮

use anyhow::Result;
use clap::Subcommand;

#[derive(Subcommand)]
pub enum EvolveAction {
    /// Show evolution flywheel health dashboard (Section 9.5)
    Status,
    /// List detected patterns from event log
    Patterns {
        #[arg(short, long, default_value = "10")]
        limit: usize,
    },
    /// List generated Skill drafts
    Drafts,
    /// Run evolution scan now (async via job queue)
    Scan,
    /// Show circuit breaker status for all Skills
    CircuitStatus,
}

pub fn run(action: EvolveAction) -> Result<()> {
    match action {
        EvolveAction::Status => show_status()?,
        EvolveAction::Patterns { limit } => show_patterns(limit)?,
        EvolveAction::Drafts => show_drafts()?,
        EvolveAction::Scan => trigger_scan()?,
        EvolveAction::CircuitStatus => show_circuit_status()?,
    }
    Ok(())
}

fn show_status() -> Result<()> {
    let home = dirs::home_dir().unwrap_or_default();
    let events_db = home.join(".evocli").join("events.db");
    let skills_dir = home.join(".evocli").join("skills");
    let stats_file = home.join(".evocli").join("skill_stats.json");

    println!("\n━━━ EvoCLI 进化飞轮状态（Section 9.5）━━━━━━━━━━━━━━━━━━━━━\n");

    // Event statistics
    if events_db.exists() {
        use rusqlite::Connection;
        let conn = Connection::open(&events_db)?;
        let total: i64 = conn
            .query_row("SELECT COUNT(*) FROM events", [], |r| r.get(0))
            .unwrap_or(0);
        let recent: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM events WHERE created_at > datetime('now', '-7 days')",
                [],
                |r| r.get(0),
            )
            .unwrap_or(0);
        println!(
            "  📊 事件日志:     总计 {} 条 / 最近 7 天 {} 条",
            total, recent
        );
    } else {
        println!("  📊 事件日志:     未初始化（运行 evocli 后自动创建）");
    }

    // Skill statistics
    let skill_count = if skills_dir.exists() {
        std::fs::read_dir(&skills_dir)
            .map(|d| {
                d.filter_map(|e| e.ok())
                    .filter(|e| e.path().extension().map_or(false, |x| x == "toml"))
                    .count()
            })
            .unwrap_or(0)
    } else {
        0
    };
    println!("  🎯 Skill 库:      {} 个已定义", skill_count);

    // Circuit breaker stats
    if stats_file.exists() {
        let raw = std::fs::read_to_string(&stats_file).unwrap_or_default();
        if let Ok(stats) = serde_json::from_str::<serde_json::Value>(&raw) {
            let total_skills = stats.as_object().map_or(0, |m| m.len());
            let open_circuits = stats.as_object().map_or(0, |m| {
                m.values()
                    .filter(|v| v["circuit_open"].as_bool().unwrap_or(false))
                    .count()
            });
            println!(
                "  🔌 熔断器:        {} 个 Skill 被监控，{} 个熔断",
                total_skills, open_circuits
            );
        }
    }

    // Memory count — read from Python Soul's JSONL store (H1: unified storage after migration)
    // memory.db (Rust SQLite) is deprecated; Python reads ~/.evocli/data/memories.jsonl
    let memories_jsonl = home.join(".evocli").join("data").join("memories.jsonl");
    let mem_count: usize = if memories_jsonl.exists() {
        std::fs::read_to_string(&memories_jsonl)
            .map(|c| c.lines().filter(|l| !l.trim().is_empty()).count())
            .unwrap_or(0)
    } else {
        0
    };
    if mem_count > 0 {
        println!("  🧠 记忆库:        {} 条记忆已积累", mem_count);
    } else {
        println!("  🧠 记忆库:        未初始化（运行 evocli 后自动积累）");
    }

    println!("\n  建议操作:");
    println!("  • evocli evolve patterns  查看检测到的行为模式");
    println!("  • evocli evolve drafts    查看待审批的 Skill 草案");
    println!("  • evocli evolve scan      立即触发进化扫描");
    println!("  • evocli evolve circuit-status  查看熔断器状态\n");

    Ok(())
}

fn show_patterns(_limit: usize) -> Result<()> {
    println!("\n━━━ 行为模式检测 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n");
    println!("  Pattern analysis runs via Python Evolution Engine.");
    println!("  Trigger with: evocli evolve scan\n");
    println!("  The Evolution Engine uses PrefixSpan sequence mining for");
    println!("  accurate pattern detection across all sessions.\n");
    println!("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n");
    Ok(())
}

fn show_drafts() -> Result<()> {
    let home = dirs::home_dir().unwrap_or_default();
    let skills_dir = home.join(".evocli").join("skills");

    let drafts: Vec<_> = if skills_dir.exists() {
        std::fs::read_dir(&skills_dir)
            .map(|d| {
                d.filter_map(|e| e.ok())
                    .filter(|e| e.path().extension().map_or(false, |x| x == "toml"))
                    .filter(|e| {
                        std::fs::read_to_string(e.path())
                            .map(|c| c.contains("status = \"draft\""))
                            .unwrap_or(false)
                    })
                    .map(|e| e.path())
                    .collect()
            })
            .unwrap_or_default()
    } else {
        vec![]
    };

    if drafts.is_empty() {
        println!("No Skill drafts found. The Evolution Engine will generate drafts automatically.");
        println!("Draft Skill files will appear in: {}", skills_dir.display());
    } else {
        println!("{} draft Skill(s) awaiting review:", drafts.len());
        for p in &drafts {
            println!("  • {}", p.display());
        }
        println!("\nTo approve: evocli skill run <id> --dry-run");
    }
    Ok(())
}

fn trigger_scan() -> Result<()> {
    let db_path = crate::job_queue::jobs_db_path();
    let q = crate::job_queue::JobQueue::new(&db_path)?;
    let cwd = std::env::current_dir()?;
    let id = q.push(crate::job_queue::JobType::EvolutionScan {
        project: cwd.to_string_lossy().to_string(),
    })?;
    println!("✅ Evolution scan queued: {}", &id[..8.min(id.len())]);
    println!("   The scan will run in the background on the next evocli session.");
    Ok(())
}

fn show_circuit_status() -> Result<()> {
    let stats_file = dirs::home_dir()
        .unwrap_or_default()
        .join(".evocli")
        .join("skill_stats.json");

    if !stats_file.exists() {
        println!("No circuit breaker data found. Skills haven't been executed yet.");
        return Ok(());
    }

    let raw = std::fs::read_to_string(&stats_file)?;
    let stats: serde_json::Value = serde_json::from_str(&raw)?;

    if let Some(map) = stats.as_object() {
        if map.is_empty() {
            println!("No skills tracked yet.");
            return Ok(());
        }
        println!(
            "{:<30} {:<12} {:<12} {}",
            "Skill ID", "Circuit", "Fail Rate", "Consecutive"
        );
        println!("{}", "─".repeat(70));
        for (skill_id, data) in map {
            let open = data["circuit_open"].as_bool().unwrap_or(false);
            let consec = data["consecutive_fail"].as_u64().unwrap_or(0);
            let executions = data["executions"].as_array().map(|a| a.len()).unwrap_or(0);
            let failures = data["executions"]
                .as_array()
                .map(|a| {
                    a.iter()
                        .filter(|e| !e["ok"].as_bool().unwrap_or(true))
                        .count()
                })
                .unwrap_or(0);
            let fail_rate = if executions > 0 {
                failures * 100 / executions
            } else {
                0
            };
            // Use char-based truncation for skill_id (may contain non-ASCII names)
            let skill_display: String = skill_id.chars().take(30).collect();
            println!(
                "{:<30} {:<12} {:<12} {}",
                skill_display,
                if open { "OPEN 🔴" } else { "CLOSED ✅" },
                format!("{}%", fail_rate),
                consec
            );
        }
    }
    Ok(())
}

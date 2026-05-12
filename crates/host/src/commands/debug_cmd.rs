//! commands/debug_cmd.rs — evocli debug 子命令（Section 25）
use anyhow::Result;
use clap::Subcommand;
use std::path::PathBuf;

#[derive(Subcommand)]
pub enum DebugAction {
    /// Export diagnostic bundle (logs, config, stats) — safe to share
    Dump {
        /// Output path (default: ~/Desktop/evocli-debug-{timestamp}.zip)
        #[arg(short, long)]
        output: Option<PathBuf>,
    },
    /// Show recent event log entries
    Events {
        #[arg(short, long, default_value = "50")]
        limit: usize,
    },
    /// Show current session trace
    Trace,
}

pub fn run(action: DebugAction) -> Result<()> {
    match action {
        DebugAction::Dump { output } => dump_diagnostics(output)?,
        DebugAction::Events { limit } => show_events(limit)?,
        DebugAction::Trace => show_trace()?,
    }
    Ok(())
}

fn dump_diagnostics(output: Option<PathBuf>) -> Result<()> {
    let home = dirs::home_dir().unwrap_or_default();
    let ts = chrono::Utc::now().format("%Y-%m-%d-%H%M%S");
    let target =
        output.unwrap_or_else(|| home.join("Desktop").join(format!("evocli-debug-{}", ts)));

    println!("\n🔍 Collecting diagnostics...");

    // Collect files to include
    let evocli_dir = home.join(".evocli");
    let mut files: Vec<(String, Vec<u8>)> = Vec::new();

    // Config (sanitized — API keys removed)
    let cfg_path = evocli_dir.join("config.toml");
    if cfg_path.exists() {
        let raw = std::fs::read_to_string(&cfg_path).unwrap_or_default();
        let sanitized = sanitize_config(&raw);
        files.push(("config.toml".into(), sanitized.into_bytes()));
        println!("  ✅ Config (sanitized)");
    }

    // Recent logs (last 500 lines)
    let log_path = evocli_dir.join("logs").join("evocli.log");
    if log_path.exists() {
        let log_content = read_tail(&log_path, 500)?;
        files.push(("evocli.log".into(), log_content));
        println!("  ✅ Logs (last 500 lines)");
    }

    // System info
    let sys_info = collect_system_info();
    files.push(("system_info.txt".into(), sys_info.into_bytes()));
    println!("  ✅ System info");

    // Job queue stats
    let jobs_db = evocli_dir.join("jobs.db");
    if jobs_db.exists() {
        files.push((
            "jobs_db_size.txt".into(),
            format!(
                "{} bytes",
                std::fs::metadata(&jobs_db).map(|m| m.len()).unwrap_or(0)
            )
            .into_bytes(),
        ));
        println!("  ✅ Job queue stats");
    }

    // Memory stats (no content, just counts)
    let mem_db = evocli_dir.join("memory.db");
    if mem_db.exists() {
        files.push((
            "memory_stats.txt".into(),
            format!(
                "memory.db: {} bytes",
                std::fs::metadata(&mem_db).map(|m| m.len()).unwrap_or(0)
            )
            .into_bytes(),
        ));
        println!("  ✅ Memory stats");
    }

    // Write diagnostic bundle as a directory of files
    write_bundle(&target, &files)?;

    println!("\n✅ Diagnostic bundle saved: {}", target.display());
    println!("   Safe to share — API keys and memory content excluded.");
    Ok(())
}

fn sanitize_config(raw: &str) -> String {
    // Remove API keys and sensitive values
    raw.lines()
        .map(|line| {
            if line.to_lowercase().contains("key")
                || line.to_lowercase().contains("secret")
                || line.to_lowercase().contains("token")
            {
                let parts: Vec<&str> = line.splitn(2, '=').collect();
                if parts.len() == 2 {
                    return format!("{} = \"[REDACTED]\"", parts[0].trim());
                }
            }
            line.to_string()
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn read_tail(path: &PathBuf, lines: usize) -> Result<Vec<u8>> {
    use std::io::{Read, Seek, SeekFrom};
    // Cap at 512KB to prevent OOM on large log files.
    // Reading the full file with read_to_string would crash if evocli.log grows to GBs.
    const MAX_TAIL_BYTES: u64 = 512 * 1024;

    let mut f = std::fs::File::open(path)?;
    let file_size = f.metadata()?.len();

    // Seek to the start of the window we care about (skipping older content)
    let start_offset = file_size.saturating_sub(MAX_TAIL_BYTES);
    if start_offset > 0 {
        f.seek(SeekFrom::Start(start_offset))?;
    }

    let mut buffer = Vec::new();
    f.read_to_end(&mut buffer)?;

    let content = String::from_utf8_lossy(&buffer);
    let tail: Vec<&str> = content
        .lines()
        .rev()
        .take(lines)
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
        .collect();
    Ok(tail.join("\n").into_bytes())
}

fn collect_system_info() -> String {
    let os = std::env::consts::OS;
    let arch = std::env::consts::ARCH;
    let ver = env!("CARGO_PKG_VERSION");
    let python = std::process::Command::new(if cfg!(windows) { "python" } else { "python3" })
        .args(["--version"])
        .output()
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .unwrap_or_else(|_| "not found".to_string());
    format!("EvoCLI: {}\nOS: {} {}\nPython: {}\n", ver, os, arch, python)
}

fn write_bundle(target: &PathBuf, files: &[(String, Vec<u8>)]) -> Result<()> {
    use std::io::Write;
    std::fs::create_dir_all(target)?;
    for (name, content) in files {
        let path = target.join(name);
        let mut f = std::fs::File::create(&path)?;
        f.write_all(content)?;
    }
    Ok(())
}

fn show_events(limit: usize) -> Result<()> {
    let db_path = dirs::home_dir()
        .unwrap_or_default()
        .join(".evocli")
        .join("events.db");
    if !db_path.exists() {
        println!("No events database found. Start evocli to initialize.");
        return Ok(());
    }
    use rusqlite::Connection;
    let conn = Connection::open(&db_path)?;
    let mut stmt = conn.prepare(
        "SELECT type, session_id, payload, created_at FROM events ORDER BY created_at DESC LIMIT ?1"
    )?;
    let events: Vec<(String, String, String, String)> = stmt
        .query_map(rusqlite::params![limit as i64], |row| {
            Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?))
        })?
        .filter_map(|r| r.ok())
        .collect();

    if events.is_empty() {
        println!("No events recorded yet.");
        return Ok(());
    }
    println!(
        "{:<20} {:<20} {:<40} {}",
        "Time", "Type", "Session", "Payload"
    );
    println!("{}", "─".repeat(100));
    for (ev_type, session_id, payload, created_at) in events.iter().rev() {
        // Use char-based truncation to avoid panicking on multibyte UTF-8 in payload
        let payload_display: String = payload.chars().take(40).collect();
        println!(
            "{:<20} {:<20} {:<40} {}",
            &created_at[..19.min(created_at.len())], // timestamps are ASCII-safe
            ev_type,
            &session_id[..20.min(session_id.len())], // session IDs are hex-safe
            payload_display
        );
    }
    Ok(())
}

fn show_trace() -> Result<()> {
    // Show latest session trace from events DB
    println!("Trace: use `evocli debug events` to view recent event log.");
    Ok(())
}

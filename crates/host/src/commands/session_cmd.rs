//! commands/session_cmd.rs — evocli session 子命令（Section 26）
use anyhow::Result;
use clap::Subcommand;
use std::path::PathBuf;

#[derive(Subcommand)]
pub enum SessionAction {
    /// List sessions
    List {
        #[arg(long)]
        status: Option<String>,
    },
    /// Resume latest interrupted session (or specify ID)
    Resume { session_id: Option<String> },
    /// Pause a session (saves snapshot)
    Pause { session_id: String },
}

fn sessions_dir() -> PathBuf {
    dirs::home_dir()
        .unwrap_or_default()
        .join(".evocli")
        .join("sessions")
}

pub fn run(action: SessionAction) -> Result<()> {
    let dir = sessions_dir();
    match action {
        SessionAction::List { status } => list(&dir, status.as_deref()),
        SessionAction::Resume { session_id } => resume(&dir, session_id.as_deref()),
        SessionAction::Pause { session_id } => pause(&dir, &session_id),
    }
}

fn list(dir: &PathBuf, status_filter: Option<&str>) -> Result<()> {
    if !dir.exists() {
        println!("无 Session 记录。运行 evocli 开始第一个 Session。");
        return Ok(());
    }
    let mut entries: Vec<serde_json::Value> = dir
        .read_dir()?
        .filter_map(|e| e.ok())
        .filter(|e| e.path().extension().map_or(false, |x| x == "json"))
        .filter_map(|e| {
            let txt = std::fs::read_to_string(e.path()).ok()?;
            serde_json::from_str(&txt).ok()
        })
        .filter(|v: &serde_json::Value| match status_filter {
            Some(s) => v["status"].as_str() == Some(s),
            None => true,
        })
        .collect();

    entries.sort_by(|a, b| {
        b["last_active"]
            .as_str()
            .unwrap_or("")
            .cmp(a["last_active"].as_str().unwrap_or(""))
    });

    if entries.is_empty() {
        println!("无匹配 Session。");
        return Ok(());
    }
    println!(
        "{:<22} {:<36} {:<14} {}",
        "Session ID", "Goal", "Status", "Last Active"
    );
    println!("{}", "─".repeat(90));
    for e in &entries {
        let id = e["id"].as_str().unwrap_or("?");
        let goal = e["goal"].as_str().unwrap_or("?");
        let stat = e["status"].as_str().unwrap_or("?");
        let time = e["last_active"].as_str().unwrap_or("?");
        // char-based truncation: user-entered session goals are likely to contain Chinese text
        let goal_display: String = goal.chars().take(36).collect();
        println!(
            "{:<22} {:<36} {:<14} {}",
            &id[..id.len().min(22)],
            goal_display,
            stat,
            &time[..time.len().min(19)]
        );
    }
    Ok(())
}

fn resume(dir: &PathBuf, session_id: Option<&str>) -> Result<()> {
    // 找到要恢复的 session ID
    let id = if let Some(given) = session_id {
        given.to_string()
    } else {
        // 自动选择最近的 interrupted / paused session
        let mut found = String::new();
        for status in &["interrupted", "paused"] {
            if let Ok(rd) = dir.read_dir() {
                let mut entries: Vec<serde_json::Value> = rd
                    .filter_map(|e| e.ok())
                    .filter_map(|e| {
                        let txt = std::fs::read_to_string(e.path()).ok()?;
                        let v: serde_json::Value = serde_json::from_str(&txt).ok()?;
                        if v["status"].as_str() == Some(status) {
                            Some(v)
                        } else {
                            None
                        }
                    })
                    .collect();
                entries.sort_by(|a, b| {
                    b["last_active"]
                        .as_str()
                        .unwrap_or("")
                        .cmp(a["last_active"].as_str().unwrap_or(""))
                });
                if let Some(e) = entries.first() {
                    found = e["id"].as_str().unwrap_or("").to_string();
                    break;
                }
            }
        }
        found
    };

    if id.is_empty() {
        println!("没有可恢复的 Session。运行 `evocli session list` 查看所有 Session。");
        return Ok(());
    }

    // 读取 session 元数据
    let meta_path = dir.join(format!("{}.json", id));
    if !meta_path.exists() {
        println!("Session {} 不存在。", id);
        return Ok(());
    }
    let meta: serde_json::Value = serde_json::from_str(&std::fs::read_to_string(&meta_path)?)?;

    println!();
    println!("  Session:     {}", id);
    println!(
        "  Goal:        {}",
        meta["goal"].as_str().unwrap_or("(unknown)")
    );
    println!(
        "  Status:      {}",
        meta["status"].as_str().unwrap_or("unknown")
    );
    println!(
        "  Last active: {}",
        meta["last_active"].as_str().unwrap_or("unknown")
    );
    println!();

    // 更新 session 状态为 active
    let mut updated = meta.clone();
    updated["status"] = serde_json::json!("active");
    updated["last_active"] = serde_json::json!(chrono::Utc::now().to_rfc3339());
    std::fs::write(&meta_path, serde_json::to_string_pretty(&updated)?)?;

    // 将 session_id 写入临时文件，供 TUI 启动时读取
    let resume_flag = dirs::home_dir()
        .unwrap_or_default()
        .join(".evocli")
        .join(".resume_session");
    std::fs::write(&resume_flag, &id)?;

    println!(
        "  Starting EvoCLI with session {}...",
        &id[..id.len().min(16)]
    );
    println!("  The AI will resume from where you left off.");
    println!();

    // 启动 TUI（通过 evocli 主程序，传入 session_id 环境变量）
    let exe = std::env::current_exe()?;
    let status = std::process::Command::new(&exe)
        .env("EVOCLI_RESUME_SESSION", &id)
        .status()?;

    // 清理 resume flag
    let _ = std::fs::remove_file(&resume_flag);

    if !status.success() {
        anyhow::bail!("TUI exited with error");
    }
    Ok(())
}

fn pause(dir: &PathBuf, session_id: &str) -> Result<()> {
    let path = dir.join(format!("{}.json", session_id));
    if path.exists() {
        let mut data: serde_json::Value = serde_json::from_str(&std::fs::read_to_string(&path)?)?;
        data["status"] = serde_json::json!("paused");
        data["last_active"] = serde_json::json!(chrono::Utc::now().to_rfc3339());
        std::fs::write(&path, serde_json::to_string_pretty(&data)?)?;
    }
    println!("Session {} 已暂停。", session_id);
    Ok(())
}

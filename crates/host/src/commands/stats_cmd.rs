/// commands/stats_cmd.rs — evocli stats（Section 9.5 飞轮指标仪表盘）
///
/// 聚合 ~/.evocli/events.db 中的历史数据，展示：
///   - 任务效率趋势（重复任务时间缩减）
///   - Skill 信任比率（Trusted vs Draft）
///   - 记忆命中率
///   - 失败率趋势
///   - 工具调用统计 Top 10
use anyhow::Result;

pub fn run() -> Result<()> {
    println!("\n━━━ EvoCLI Stats ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n");

    let db_path = dirs::home_dir()
        .unwrap_or_default()
        .join(".evocli")
        .join("events.db");

    if !db_path.exists() {
        println!("  No data yet — run some tasks first to collect stats.");
        println!("  Events DB will be created at: {}", db_path.display());
        return Ok(());
    }

    let conn = rusqlite::Connection::open(&db_path)?;

    // ── 1. 总体概况 ─────────────────────────────────────────────
    let total_events: i64 = conn
        .query_row("SELECT COUNT(*) FROM events", [], |r| r.get(0))
        .unwrap_or(0);
    let total_sessions: i64 = conn
        .query_row("SELECT COUNT(DISTINCT session_id) FROM events", [], |r| {
            r.get(0)
        })
        .unwrap_or(0);

    println!("  📊 Overview");
    println!("  ├─ Total events:   {}", total_events);
    println!("  └─ Total sessions: {}", total_sessions);
    println!();

    // ── 2. 工具调用统计 Top 10 ────────────────────────────────
    println!("  🔧 Top 10 Tools (by call count)");
    if let Ok(mut tool_stmt) = conn.prepare(
        "SELECT data, COUNT(*) as cnt FROM events WHERE event_type = 'tool_called'
         GROUP BY data ORDER BY cnt DESC LIMIT 10",
    ) {
        let mut has_tools = false;
        if let Ok(mut tool_rows) = tool_stmt.query([]) {
            while let Ok(Some(row)) = tool_rows.next() {
                let data: String = row.get(0).unwrap_or_default();
                let cnt: i64 = row.get(1).unwrap_or(0);
                let tool = extract_json_field(&data, "tool").unwrap_or_else(|| data.clone());
                println!("  ├─ {:30} {:>6}×", tool, cnt);
                has_tools = true;
            }
        }
        if !has_tools {
            println!("  └─ (no tool call data yet)");
        }
    } else {
        println!("  └─ (events table not yet initialized)");
    }
    println!();

    // ── 3. 成功率 ───────────────────────────────────────────────
    println!("  ✅ Tool Success Rate");
    let ok_count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM events WHERE event_type='tool_called' AND data LIKE '%\"ok\":true%'",
            [], |r| r.get(0)
        ).unwrap_or(0);
    let err_count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM events WHERE event_type='tool_error'",
            [],
            |r| r.get(0),
        )
        .unwrap_or(0);
    let total_calls = ok_count + err_count;
    let success_rate = if total_calls > 0 {
        format!("{:.1}%", ok_count as f64 / total_calls as f64 * 100.0)
    } else {
        "N/A".to_string()
    };
    println!(
        "  ├─ Successful: {}  /  Failed: {}  /  Rate: {}",
        ok_count, err_count, success_rate
    );
    println!();

    // ── 4. Skill 执行历史 ────────────────────────────────────
    println!("  🎯 Skill Executions");
    let skill_exec: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM events WHERE event_type = 'skill_executed'",
            [],
            |r| r.get(0),
        )
        .unwrap_or(0);
    let skill_fail: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM events WHERE event_type = 'skill_failed'",
            [],
            |r| r.get(0),
        )
        .unwrap_or(0);
    println!(
        "  ├─ Executions: {}  /  Failures: {}",
        skill_exec, skill_fail
    );

    // Skill stats from ~/.evocli/skill_stats.json
    let stats_file = dirs::home_dir()
        .unwrap_or_default()
        .join(".evocli")
        .join("skill_stats.json");
    if stats_file.exists() {
        if let Ok(raw) = std::fs::read_to_string(&stats_file) {
            if let Ok(json) = serde_json::from_str::<serde_json::Value>(&raw) {
                if let Some(obj) = json.as_object() {
                    let trusted = obj
                        .values()
                        .filter(|v| v["status"].as_str() == Some("trusted"))
                        .count();
                    let total_s = obj.len();
                    println!("  └─ Skill trust ratio: {}/{} trusted", trusted, total_s);
                }
            }
        }
    } else {
        println!("  └─ No skill stats yet");
    }
    println!();

    // ── 5. Memory 召回统计 ───────────────────────────────────
    println!("  🧠 Memory");
    let mem_recalls: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM events WHERE event_type = 'memory_recalled'",
            [],
            |r| r.get(0),
        )
        .unwrap_or(0);
    println!("  └─ Memory recalls: {}", mem_recalls);
    println!();

    // ── 6. 最近 7 天事件趋势 ────────────────────────────────
    println!("  📈 Last 7 days (events per day)");
    let mut daily: Vec<(String, i64)> = vec![];
    if let Ok(mut day_stmt) = conn.prepare(
        "SELECT strftime('%Y-%m-%d', created_at) as day, COUNT(*) as cnt
         FROM events
         WHERE created_at >= datetime('now', '-7 days')
         GROUP BY day ORDER BY day",
    ) {
        if let Ok(mut day_rows) = day_stmt.query([]) {
            while let Ok(Some(row)) = day_rows.next() {
                let day: String = row.get(0).unwrap_or_default();
                let cnt: i64 = row.get(1).unwrap_or(0);
                daily.push((day, cnt));
            }
        }
    }
    if daily.is_empty() {
        println!("  └─ (no events in last 7 days)");
    } else {
        let max_cnt = daily.iter().map(|(_, c)| *c).max().unwrap_or(1);
        for (day, cnt) in &daily {
            let bar_len = (cnt * 30 / max_cnt) as usize;
            let bar: String = "█".repeat(bar_len);
            println!("  {} │{:<30}│ {}", day, bar, cnt);
        }
    }
    println!();

    println!("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n");
    Ok(())
}

fn extract_json_field(json_str: &str, field: &str) -> Option<String> {
    // 简单 JSON 字段提取，不依赖完整解析
    let key = format!("\"{}\":", field);
    let pos = json_str.find(&key)?;
    let rest = &json_str[pos + key.len()..].trim_start();
    if rest.starts_with('"') {
        let end = rest[1..].find('"')?;
        Some(rest[1..end + 1].to_string())
    } else {
        None
    }
}

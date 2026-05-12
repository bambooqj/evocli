//! EvoCLI — 入口
//!
//! 唯一职责：CLI 参数解析 + 分发到 commands/ 子模块。
//! 业务逻辑全部在 commands/*.rs 中。

mod commands;
mod config;
mod errors;
mod event_bus;
mod fs_tools;
mod git;
mod init;
mod job_queue;
mod keystore;
mod logging;

mod python_manager;   // v3.x: uv 自管理 Python 运行时
mod security;
mod tool_dispatch;

use anyhow::Result;
use clap::{Parser, Subcommand};

use commands::{
    config_cmd::ConfigAction,
    constraint_cmd::ConstraintAction,
    debug_cmd::DebugAction,
    evolve_cmd::EvolveAction,
    git_cmd::GitAction,
    lsp_cmd::LspAction,
    mcp_cmd::McpAction,        // P3-2
    session_cmd::SessionAction,
    skill_cmd::SkillAction,
    snapshot_cmd::SnapshotAction,
    jobs_cmd::JobsAction,
    tools_cmd::ToolsAction,
};

#[derive(Parser)]
#[command(name = "evocli", version, about = "AI-native coding companion")]
struct Cli {
    #[command(subcommand)]
    command: Option<Commands>,

    #[arg(long, global = true)]
    debug: bool,
}

#[derive(Subcommand)]
enum Commands {
    /// Initialize EvoCLI (select provider, set API key)
    Init,
    /// Configuration management (show / edit / explain / set)
    Config   { #[command(subcommand)] action: ConfigAction },
    /// Manage skills
    Skill    { #[command(subcommand)] action: SkillAction },
    /// Git operations
    Git      { #[command(subcommand)] action: GitAction },
    /// Index source code symbols
    Index    { #[arg(short, long)] dir: Option<String> },
    /// LSP-powered code intelligence
    Lsp      { #[command(subcommand)] action: LspAction },
    /// Manage L1 constraint rules (Section 6.2)
    Constraint { #[command(subcommand)] action: ConstraintAction },
    /// Evolution flywheel dashboard (Section 9.5)
    Evolve     { #[command(subcommand)] action: EvolveAction },
    /// System health check (7 checks)
    Doctor,
    /// Session management (save / resume / list)
    Session  { #[command(subcommand)] action: SessionAction },
    /// Workspace snapshot management (side-git, never touches project .git)
    Snapshot { #[command(subcommand)] action: SnapshotAction },
    /// Background job queue management (Section 28)
    Jobs     { #[command(subcommand)] action: JobsAction },
    /// Debug diagnostics (dump, events, trace) — Section 25
    Debug    { #[command(subcommand)] action: DebugAction },
    /// Manage user-registered tools discoverable by LLM (G-09)
    Tool     { #[command(subcommand)] action: ToolsAction },
    /// Project stats — flywheel metrics dashboard (v2.3)
    Stats,
    /// MCP (Model Context Protocol) server management (P3-2)
    Mcp      { #[command(subcommand)] action: McpAction },
}
#[tokio::main]
async fn main() -> Result<()> {
    let cli      = Cli::parse();
    let _guard   = logging::init(cli.debug)?;

    // ── 环境自检：任何命令运行前确保 Python 环境就绪 ─────────────────────────
    // 原来只在 TUI 启动时检查，导致 evocli doctor/skill/index 等子命令跳过安装
    // 排除：init（它自己处理环境设置）、doctor（诊断命令可以在没有环境时运行）
    let skip_setup = matches!(cli.command, Some(Commands::Init) | Some(Commands::Doctor));
    if !skip_setup {
        ensure_python_env_ready();
    }

    match cli.command {
        Some(Commands::Init)                  => init::run_init().await?,
        Some(Commands::Config  { action })    => commands::config_cmd::run(action)?,
        Some(Commands::Constraint { action }) => commands::constraint_cmd::run(action)?,
        Some(Commands::Evolve     { action }) => commands::evolve_cmd::run(action)?,
        Some(Commands::Doctor)               => commands::doctor_cmd::run()?,
        Some(Commands::Skill    { action })  => commands::skill_cmd::run(action).await?,
        Some(Commands::Git      { action })  => commands::git_cmd::run(action)?,
        Some(Commands::Index    { dir    })  => commands::index_cmd::run(dir.as_deref())?,
        Some(Commands::Lsp      { action })  => commands::lsp_cmd::run(action).await?,
        Some(Commands::Session  { action })  => commands::session_cmd::run(action)?,
        Some(Commands::Snapshot { action })  => commands::snapshot_cmd::run(action)?,
        Some(Commands::Jobs    { action })  => commands::jobs_cmd::run(action)?,
        Some(Commands::Debug   { action })  => commands::debug_cmd::run(action)?,
        Some(Commands::Tool    { action })  => commands::tools_cmd::run(action)?,
        Some(Commands::Stats)               => commands::stats_cmd::run()?,
        Some(Commands::Mcp     { action })  => commands::mcp_cmd::run(action)?,
        None                                 => run_tui(cli.debug).await?,
    }
    Ok(())
}

/// 确保 Python 环境就绪（任何需要 Soul 的命令都调用此函数）
fn ensure_python_env_ready() {
    let needs_setup = !python_manager::PythonManager::python_ready()
        || !python_manager::PythonManager::full_extras_installed();

    if !needs_setup { return; }

    println!("EvoCLI v{}", env!("CARGO_PKG_VERSION"));
    println!();
    if !python_manager::PythonManager::python_ready() {
        println!("  ⚡ First run — setting up Python environment (~3-5 min, once only)");
    } else {
        println!("  ⚡ Installing missing packages...");
    }
    println!();

    let soul_src_exe = find_soul_dir_relative_to_exe();
    let soul_src_cwd = std::path::PathBuf::from("evocli-soul");
    let soul_arg: Option<&std::path::Path> = soul_src_exe.as_ref()
        .filter(|p| p.exists())
        .map(|p| p.as_path())
        .or_else(|| if soul_src_cwd.exists() { Some(&soul_src_cwd) } else { None });

    match python_manager::PythonManager::setup(soul_arg) {
        Ok(py) => {
            println!("  ✓ Python environment ready: {}", py.display());
            println!();
        }
        Err(e) => {
            // Clear marker so next run retries
            let marker = python_manager::PythonManager::venv_dir()
                .join(".evocli_full_installed");
            let _ = std::fs::remove_file(&marker);

            eprintln!("  ✗ Setup failed: {}", e);
            eprintln!("  → Run: evocli init    (runs setup interactively)");
            eprintln!("  → Or:  evocli doctor  (diagnose the issue)");
            eprintln!();
            std::process::exit(1);
        }
    }
}

async fn run_tui(_debug: bool) -> Result<()> {
    let soul_path = config::resolve_soul_path();
    let cfg = std::sync::Arc::new(config::Config::load_or_default().unwrap_or_default());

    // P2-1: 检查是否有待恢复的 session
    let resume_session = std::env::var("EVOCLI_RESUME_SESSION").ok()
        .filter(|s| !s.is_empty());

    // ensure_python_env_ready() already called from main() before dispatch.
    // No duplicate setup check needed here.

    println!("EvoCLI v{}", env!("CARGO_PKG_VERSION"));
    println!("  Endpoint: {}", cfg.llm.base_url.as_deref().unwrap_or("(auto)"));
    println!("  Fast:     {}", cfg.llm.tiers.fast);
    println!("  Smart:    {}", cfg.llm.tiers.smart);
    println!("  Soul:     {}", soul_path);
    if let Some(ref sid) = resume_session {
        println!("  Resuming: {}", sid);
    }
    println!();

    let bridge = soul_bridge::SoulBridge::spawn(&soul_path).await?;
    match bridge.ping().await {
        Ok(true)  => println!("  Soul connected ✓"),
        Ok(false) => {
            // Soul responded but returned something other than "pong" — fatal.
            // Starting the TUI in this state means all agent.stream calls will fail
            // silently, leaving the user staring at a frozen interface.
            eprintln!("\n[E501] Python Soul ping returned unexpected response.");
            eprintln!("  This usually means the Python environment is broken.");
            eprintln!("  Run: evocli doctor   to diagnose the issue.\n");
            anyhow::bail!("Soul process did not respond correctly to ping");
        }
        Err(e) => {
            eprintln!("\n[E500] Python Soul failed to start: {e}");
            eprintln!("  Possible causes:");
            eprintln!("    • Python venv not set up   →  run setup.sh / setup.ps1");
            eprintln!("    • evocli-soul not installed →  run: pip install evocli-soul[full]");
            eprintln!("    • Soul script missing       →  check EVOCLI_SOUL env var");
            eprintln!("  Run: evocli doctor   for full diagnostics.\n");
            anyhow::bail!("Soul process ping failed: {e}");
        }
    }
    println!();

    // ── 启动 Capability Contract 工具调度循环 ────────────────────
    // 在后台持续处理 Python Soul 发来的 tool.call 请求
    let bridge_arc = std::sync::Arc::new(bridge);
    let bridge_dispatch = std::sync::Arc::clone(&bridge_arc);
    let cfg_dispatch = std::sync::Arc::clone(&cfg);
    // 生成唯一 session_id（每次 TUI 启动）
    let session_id = format!("ses_{}", uuid::Uuid::new_v4().simple());
    let session_id_dispatch = session_id.clone();

    // ── 启动 Python Soul 自动重启看门狗 ──────────────────────────
    // 当 Python Soul 崩溃时（stdout EOF），watchdog 自动重启 Python 进程。
    // Rust TUI 和所有 channel 保持不变——用户不会感知到崩溃。
    soul_bridge::spawn_restart_watchdog(std::sync::Arc::clone(&bridge_arc));

    tokio::spawn(async move {
        // Fix H2: 并行工具调度
        // - while let 一次性取出全部待处理请求（原 if let 只处理一个）
        // - 每个工具调用独立 tokio::spawn（真正并行，消除串行阻塞）
        // - 空闲时 2ms 休眠（原 10ms，降低工具调用延迟 5x）
        loop {
            let mut dispatched = 0usize;

            // 取出当前队列中全部待处理工具调用
            while let Some(req) = bridge_dispatch.next_tool_call().await {
                dispatched += 1;
                let id        = req.id.clone();
                let tool_name = req.tool.clone();
                let bridge_c  = std::sync::Arc::clone(&bridge_dispatch);
                let cfg_c     = std::sync::Arc::clone(&cfg_dispatch);
                let sid_c     = session_id_dispatch.clone();

                // 每个工具调用独立 spawn — 并行执行，互不阻塞
                tokio::spawn(async move {
                    let event_bus = event_bus::EventBus::new();
                    let result    = tool_dispatch::dispatch(&req, Some(&*bridge_c), &cfg_c).await;
                    let ok        = result.is_ok();

                    event_bus.tool_called(&sid_c, &tool_name, ok);
                    if !ok {
                        event_bus.emit(&sid_c, "tool_error", serde_json::json!({
                            "tool":  &tool_name,
                            "error": result.as_ref().err().map(|e| e.to_string()).unwrap_or_default()
                        }));
                    }
                    bridge_c.reply_tool(&id, result);
                });
            }

            // 队列为空时等待 Notify 信号（事件驱动，零延迟唤醒）
            if dispatched == 0 {
                bridge_dispatch.wait_for_tool().await;
            }
        }
    });

    // ── 启动 Job Queue 消费循环（Section 28）────────────────────
    // 后台持续处理 jobs.db 中的 pending 任务
    let bridge_jobs = std::sync::Arc::clone(&bridge_arc);
    tokio::spawn(async move {
        let db_path = job_queue::jobs_db_path();

        loop {
            tokio::time::sleep(tokio::time::Duration::from_secs(5)).await;

            // spawn_blocking: 避免同步 SQLite 阻塞 tokio worker thread
            let db_p = db_path.clone();
            let maybe_job = tokio::task::spawn_blocking(move || {
                let q = job_queue::JobQueue::new(&db_p)?;
                q.pop_next_pending()
            }).await;

            let maybe_job = match maybe_job {
                Ok(Ok(j)) => j,
                Ok(Err(e)) => { tracing::debug!("Job queue poll error: {e}"); continue; }
                Err(e) => { tracing::debug!("Job queue spawn error: {e}"); continue; }
            };

            if let Some(job) = maybe_job {
                    tracing::info!("Processing job: {} ({:?})", job.id, job.job_type);
                     let (method, params, timeout_ms) = match &job.job_type {
                         job_queue::JobType::MemoryDistill { session_id } =>
                             ("memory.distill",
                              serde_json::json!({"session_id": session_id, "events": []}),
                              60_000u64),
                         job_queue::JobType::CodeIndexUpdate { project, changed_files } =>
                             ("code_intel.reindex",
                              serde_json::json!({"project": project, "files": changed_files}),
                              120_000u64),
                         job_queue::JobType::SkillRun { skill_id, project, dry_run } =>
                             ("skill.run",
                              serde_json::json!({"id": skill_id, "project": project, "dry_run": dry_run}),
                              60_000u64),
                         job_queue::JobType::EvolutionScan { project } =>
                             ("evolution.observe",
                              serde_json::json!({"project": project, "events": []}),
                              60_000u64),
                         job_queue::JobType::AgentSession { session_id, graph_id, parent_id, task_prompt } =>
                             ("agent.run",
                              serde_json::json!({
                                  "session_id": session_id,
                                  "graph_id":   graph_id,
                                  "parent_id":  parent_id,
                                  "prompt":     task_prompt,
                              }),
                              300_000u64),   // 5 分钟：复杂 Agent 任务可能需要多轮 LLM + 工具调用
                     };
                    let result = bridge_jobs.call_with_timeout(method, params, timeout_ms).await;
                    match result {
                        Ok(_) => {
                            let db_p = db_path.clone();
                            let jid = job.id.clone();
                            let _ = tokio::task::spawn_blocking(move || {
                                if let Ok(q) = job_queue::JobQueue::new(&db_p) { let _ = q.mark_done(&jid); }
                            }).await;
                            tracing::info!("Job {} done", job.id);
                        }
                        Err(e) => {
                            let db_p = db_path.clone();
                            let jid = job.id.clone();
                            let err_msg = e.to_string();
                            let _ = tokio::task::spawn_blocking(move || {
                                if let Ok(q) = job_queue::JobQueue::new(&db_p) { let _ = q.mark_failed(&jid, &err_msg); }
                            }).await;
                            tracing::warn!("Job {} failed: {e}", job.id);
                        }
                    }
            }
        }
    });

    evocli_tui::run(std::sync::Arc::clone(&bridge_arc), &cfg.llm.tiers.fast, resume_session.as_deref(), cfg.context.max_total, cfg.agent.first_chunk_timeout_s, cfg.tui.enable_mouse).await?;
    Ok(())
}

/// B3 FIX: 从可执行文件位置向上查找 evocli-soul/ 目录（pub 供 init.rs 使用）
/// dist/ 结构：evocli.exe 和 evocli-soul/ 在同一目录
pub fn find_soul_dir_relative_to_exe() -> Option<std::path::PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let mut dir = exe.parent()?.to_path_buf();
    // 最多向上 6 层查找 evocli-soul/
    for _ in 0..6 {
        let candidate = dir.join("evocli-soul");
        if candidate.exists() && candidate.join("evocli_soul").exists() {
            return Some(candidate);
        }
        dir = dir.parent()?.to_path_buf();
    }
    None
}



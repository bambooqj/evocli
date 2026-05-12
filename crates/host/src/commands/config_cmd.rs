/// commands/config_cmd.rs — evocli config 子命令
///
/// evocli config show      显示当前配置（带注释）
/// evocli config edit      打开配置文件编辑器
/// evocli config explain   解释所有配置字段含义
/// evocli config set <key> <value>  快速设置单个配置项
use anyhow::{Context, Result};
use clap::Subcommand;

use crate::config::Config;

#[derive(Subcommand)]
pub enum ConfigAction {
    /// Show current config with annotations
    Show,
    /// Open config file in system editor
    Edit,
    /// Explain all config fields with examples
    Explain,
    /// Set a config value (key.path value)
    Set {
        /// Config key path (e.g. llm.provider, safety.auto_approve_writes)
        key: String,
        /// New value
        value: String,
    },
    /// Show config file path
    Path,
}

pub fn run(action: ConfigAction) -> Result<()> {
    match action {
        ConfigAction::Show => cmd_show(),
        ConfigAction::Edit => cmd_edit(),
        ConfigAction::Explain => cmd_explain(),
        ConfigAction::Set { key, value } => cmd_set(&key, &value),
        ConfigAction::Path => cmd_path(),
    }
}

// ── show ─────────────────────────────────────────────────────────────────────

fn cmd_show() -> Result<()> {
    let cfg_path = Config::path()?;
    let cfg = Config::load_or_default()?;

    println!("\n━━━ EvoCLI Configuration ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");
    println!("  File: {}", cfg_path.display());
    if !cfg_path.exists() {
        println!("  Status: ⚠️  Not found — defaults in use. Run: evocli init");
    } else {
        println!("  Status: ✅ Loaded");
    }
    println!();

    println!("  [LLM]");
    println!(
        "  base_url          = {}",
        cfg.llm
            .base_url
            .as_deref()
            .unwrap_or("(auto-detect from model name)")
    );
    println!("  tiers.fast        = {:?}", cfg.llm.tiers.fast);
    println!("  tiers.smart       = {:?}", cfg.llm.tiers.smart);
    println!(
        "  api_key           = {}",
        if cfg.llm.api_key.is_some() {
            "*** (set)"
        } else {
            "(keyring / env var)"
        }
    );
    println!();

    println!("  [Context]");
    println!("  max_total         = {} tokens", cfg.context.max_total);
    println!("  max_code          = {} tokens", cfg.context.max_code);
    println!();

    println!("  [Safety]");
    println!("  auto_approve_writes = {}", cfg.safety.auto_approve_writes);
    println!(
        "  shell_whitelist   = [{}]",
        cfg.safety
            .shell_whitelist
            .iter()
            .take(3)
            .cloned()
            .collect::<Vec<_>>()
            .join(", ")
            + if cfg.safety.shell_whitelist.len() > 3 {
                ", ..."
            } else {
                ""
            }
    );
    println!();

    println!("  [Memory]");
    println!("  max_episodes      = {}", cfg.memory.max_episodes);
    println!();

    if let Some(soul) = &cfg.soul_script {
        println!("  [Soul]");
        println!("  soul_script       = {:?}", soul);
        println!();
    }

    println!("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");
    println!("  To edit: evocli config edit");
    println!("  To explain fields: evocli config explain\n");
    Ok(())
}

// ── edit ─────────────────────────────────────────────────────────────────────

fn cmd_edit() -> Result<()> {
    let cfg_path = Config::path()?;

    // 如果不存在，先创建示例配置
    if !cfg_path.exists() {
        println!(
            "Config not found. Creating example config at {}",
            cfg_path.display()
        );
        create_example_config(&cfg_path)?;
    }

    // 打开编辑器
    let editor = std::env::var("EDITOR")
        .or_else(|_| std::env::var("VISUAL"))
        .unwrap_or_else(|_| {
            if cfg!(windows) {
                "notepad".into()
            } else {
                "nano".into()
            }
        });

    println!("Opening {} with {}...", cfg_path.display(), editor);
    let status = std::process::Command::new(&editor)
        .arg(cfg_path.to_str().unwrap_or(""))
        .status()
        .with_context(|| format!("Failed to open editor: {}", editor))?;

    if !status.success() {
        println!("⚠️  Editor exited with non-zero status. Config may not have been saved.");
    }
    Ok(())
}

// ── explain ──────────────────────────────────────────────────────────────────

fn cmd_explain() -> Result<()> {
    println!("\n━━━ EvoCLI Configuration Reference ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");
    println!("  File location: ~/.evocli/config.toml");
    println!("  Project-level: <project>/.evocli/config.toml  (overrides global)\n");

    println!("┌─ [llm] ─────────────────────────────────────────────────────────┐");
    println!("│ provider = \"anthropic\"          LLM provider                    │");
    println!("│   Choices: anthropic, openai, deepseek, ollama                 │");
    println!("│                                                                 │");
    println!("│ [llm.tiers]                                                     │");
    println!("│ fast  = \"claude-3-5-haiku-latest\"  Quick tasks (search, short) │");
    println!("│ smart = \"claude-sonnet-4-5-...\"    Complex tasks (refactor...)  │");
    println!("│   Tip: Use smaller/cheaper models for fast tier to save cost.  │");
    println!("│                                                                 │");
    println!("│ api_key = \"sk-...\"  (optional, prefer keyring storage)         │");
    println!("│   Better: set ANTHROPIC_API_KEY env var or use evocli init     │");
    println!("│                                                                 │");
    println!("│ base_url = \"http://localhost:11434\"  (Ollama only)             │");
    println!("└─────────────────────────────────────────────────────────────────┘");
    println!();

    println!("┌─ [context] ─────────────────────────────────────────────────────┐");
    println!("│ max_total = 128000   Total token budget per request            │");
    println!("│   Reduce to 32000 if you hit rate limits or want faster resp.  │");
    println!("│ max_code  = 32000    Tokens reserved for code context          │");
    println!("└─────────────────────────────────────────────────────────────────┘");
    println!();

    println!("┌─ [safety] ──────────────────────────────────────────────────────┐");
    println!("│ auto_approve_writes = false                                     │");
    println!("│   false = AI must request approval before modifying files      │");
    println!("│   true  = AI can write/modify files without asking (risky!)    │");
    println!("│                                                                 │");
    println!("│ shell_whitelist = [\"cargo *\", \"npm *\", \"git *\", ...]            │");
    println!("│   Add your custom commands: e.g. \"./scripts/*\", \"make *\"       │");
    println!("│   Wildcard * matches any args. Exact match also works.         │");
    println!("└─────────────────────────────────────────────────────────────────┘");
    println!();

    println!("┌─ [memory] ──────────────────────────────────────────────────────┐");
    println!("│ max_episodes = 1000   Max memories to retain in SQLite         │");
    println!("│   Increase for long-running projects, decrease if slow.        │");
    println!("└─────────────────────────────────────────────────────────────────┘");
    println!();

    println!("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");
    println!("  Project-level overrides: create <project>/.evocli/config.toml");
    println!("  Project rules / constraints: create <project>/AGENTS.md");
    println!("  Example: evocli config edit  →  opens ~/.evocli/config.toml\n");
    Ok(())
}

// ── set ──────────────────────────────────────────────────────────────────────

fn cmd_set(key: &str, value: &str) -> Result<()> {
    let mut cfg = Config::load_or_default()?;

    match key {
        "llm.base_url" => cfg.llm.base_url = Some(value.to_string()),
        "llm.api_key" => cfg.llm.api_key = Some(value.to_string()),
        "llm.tiers.fast" => cfg.llm.tiers.fast = value.to_string(),
        "llm.tiers.smart" => cfg.llm.tiers.smart = value.to_string(),
        "context.max_total" => cfg.context.max_total = value.parse().context("Expected integer")?,
        "context.max_code" => cfg.context.max_code = value.parse().context("Expected integer")?,
        "safety.auto_approve_writes" => {
            cfg.safety.auto_approve_writes = value.parse().context("Expected true/false")?
        }
        "memory.max_episodes" => {
            cfg.memory.max_episodes = value.parse().context("Expected integer")?
        }
        _ => anyhow::bail!(
            "Unknown config key: '{}'.\nUse 'evocli config explain' to see all available keys.",
            key
        ),
    }

    cfg.save()?;
    println!("✅  Set {} = {:?}", key, value);
    println!("  Config saved to {}", Config::path()?.display());
    Ok(())
}

// ── path ─────────────────────────────────────────────────────────────────────

fn cmd_path() -> Result<()> {
    let p = Config::path()?;
    println!("{}", p.display());
    Ok(())
}

// ── helpers ───────────────────────────────────────────────────────────────────

fn create_example_config(path: &std::path::Path) -> Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(path, EXAMPLE_CONFIG)?;
    Ok(())
}

/// 带完整注释的示例配置文件内容（同时作为文档）
const EXAMPLE_CONFIG: &str = r#"# EvoCLI Configuration
# Location: ~/.evocli/config.toml
# Override per-project: <project>/.evocli/config.toml
#
# Quick setup: run `evocli init` to configure interactively
# Field docs:  run `evocli config explain`

[llm]
# LLM provider: anthropic | openai | deepseek | ollama
provider = "anthropic"

# API key (stored here as fallback; prefer `evocli init` which uses OS keyring)
# api_key = "sk-ant-..."

# Ollama base URL (only needed for Ollama provider)
# base_url = "http://localhost:11434"

[llm.tiers]
# Fast tier — used for quick tasks (code search, short generations)
# Anthropic: claude-3-5-haiku-latest
# OpenAI:    gpt-4o-mini
# DeepSeek:  deepseek-chat
# Ollama:    qwen2.5-coder:7b
fast  = "claude-3-5-haiku-latest"

# Smart tier — used for complex tasks (refactoring, architecture decisions)
# Anthropic: claude-sonnet-4-5-20250514
# OpenAI:    gpt-4o
# DeepSeek:  deepseek-reasoner
# Ollama:    qwen2.5-coder:32b
smart = "claude-sonnet-4-5-20250514"

[context]
# Total token budget per LLM request (P1+P2+P3 memory + code + diff + history)
max_total = 128000

# Tokens reserved for code context (current file + ranked symbols)
max_code = 32000

[safety]
# If false (recommended), AI must request approval before modifying files.
# If true, AI can write files autonomously (only for trusted environments).
auto_approve_writes = false

# Shell commands the AI is allowed to run.
# Wildcards (*) match any arguments after the prefix.
# Add your project-specific commands here.
shell_whitelist = [
    "cargo *",
    "rustc *",
    "rustup *",
    "npm *",
    "npx *",
    "node *",
    "python *",
    "python3 *",
    "pip *",
    "uv *",
    "go *",
    "make *",
    "cmake *",
    "git *",
    # Add project-specific commands:
    # "./scripts/*",
    # "docker *",
    # "kubectl *",
]

[memory]
# Maximum episodic memories to retain (older ones are pruned)
max_episodes = 1000
"#;

/// G-09: evocli tool — 用户工具自动发现与注册
///
/// 注册的工具会被 LLM 自动感知（通过 Python agent reload_user_tools()）。
/// 存储路径：~/.evocli/user_tools.toml
use anyhow::{Context, Result};
use clap::Subcommand;
use std::collections::BTreeMap;
use std::path::PathBuf;

fn tools_path() -> PathBuf {
    dirs::home_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join(".evocli")
        .join("user_tools.toml")
}

/// 用户工具定义
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct UserTool {
    pub cmd: String,
    pub description: String,
    #[serde(default)]
    pub readonly: bool, // true = 可在分析模式下运行
}

fn load_tools() -> Result<BTreeMap<String, UserTool>> {
    let path = tools_path();
    if !path.exists() {
        return Ok(BTreeMap::new());
    }
    let raw = std::fs::read_to_string(&path)
        .with_context(|| format!("Cannot read {}", path.display()))?;
    let table: toml::Value = toml::from_str(&raw)?;
    let mut tools = BTreeMap::new();
    if let Some(tool_table) = table.get("tool").and_then(|t| t.as_table()) {
        for (name, val) in tool_table {
            if let Ok(t) = val.clone().try_into::<UserTool>() {
                tools.insert(name.clone(), t);
            }
        }
    }
    Ok(tools)
}

fn save_tools(tools: &BTreeMap<String, UserTool>) -> Result<()> {
    let path = tools_path();
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let mut content = String::from(
        "# EvoCLI 用户注册工具\n# evocli tool register <name> <cmd> --description \"...\"\n\n",
    );
    for (name, tool) in tools {
        content.push_str(&format!("[tool.{}]\n", name));
        content.push_str(&format!("cmd         = {:?}\n", tool.cmd));
        content.push_str(&format!("description = {:?}\n", tool.description));
        if tool.readonly {
            content.push_str("readonly    = true\n");
        }
        content.push('\n');
    }
    std::fs::write(&path, content)?;
    Ok(())
}

#[derive(Subcommand)]
pub enum ToolsAction {
    /// List all registered user tools
    List,
    /// Register a shell command as a user tool (LLM will auto-discover it)
    Register {
        /// Tool name (used as function name in LLM)
        name: String,
        /// Shell command to execute (e.g., "./tools/my-lint.sh")
        cmd: String,
        /// Human-readable description for LLM
        #[arg(short, long, default_value = "")]
        description: String,
        /// Mark as safe for read-only / analyze mode
        #[arg(long)]
        readonly: bool,
    },
    /// Remove a registered user tool
    Remove { name: String },
}

pub fn run(action: ToolsAction) -> Result<()> {
    match action {
        ToolsAction::List => {
            let tools = load_tools()?;
            if tools.is_empty() {
                println!("No user tools registered.");
                println!("Use: evocli tool register <name> <cmd> --description \"...\"");
            } else {
                println!("{:<20} {:<40} {}", "NAME", "CMD", "DESCRIPTION");
                println!("{}", "-".repeat(80));
                for (name, tool) in &tools {
                    // Use char-based truncation — tool commands can contain Unicode paths
                    let cmd_display: String = tool.cmd.chars().take(38).collect();
                    let cmd_str = if tool.cmd.chars().count() > 38 {
                        format!("{}…", cmd_display)
                    } else {
                        tool.cmd.clone()
                    };
                    println!("{:<20} {:<40} {}", name, cmd_str, tool.description);
                }
                println!(
                    "\n{} tool(s). LLM will discover these after next agent reload.",
                    tools.len()
                );
            }
        }
        ToolsAction::Register {
            name,
            cmd,
            description,
            readonly,
        } => {
            let mut tools = load_tools()?;
            let desc = if description.is_empty() {
                format!("Run: {}", cmd)
            } else {
                description
            };
            tools.insert(
                name.clone(),
                UserTool {
                    cmd: cmd.clone(),
                    description: desc.clone(),
                    readonly,
                },
            );
            save_tools(&tools)?;
            println!("✅ Registered tool '{}'", name);
            println!("   cmd:  {}", cmd);
            println!("   desc: {}", desc);
            println!("\nRun 'evocli tool list' to see all tools.");
            println!("LLM will auto-discover on next session start.");
        }
        ToolsAction::Remove { name } => {
            let mut tools = load_tools()?;
            if tools.remove(&name).is_some() {
                save_tools(&tools)?;
                println!("✅ Removed tool '{}'", name);
            } else {
                println!("Tool '{}' not found.", name);
            }
        }
    }
    Ok(())
}

/// 供 tool_dispatch.rs 调用：返回已注册的用户工具列表（JSON）
pub fn list_user_tools_json() -> serde_json::Value {
    match load_tools() {
        Ok(tools) => {
            let items: Vec<serde_json::Value> = tools
                .iter()
                .map(|(name, tool)| {
                    serde_json::json!({
                        "name":        name,
                        "cmd":         tool.cmd,
                        "description": tool.description,
                        "readonly":    tool.readonly,
                    })
                })
                .collect();
            serde_json::json!({ "tools": items, "count": items.len() })
        }
        Err(e) => serde_json::json!({ "tools": [], "error": e.to_string() }),
    }
}

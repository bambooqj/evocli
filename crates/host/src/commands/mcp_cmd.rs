/// commands/mcp_cmd.rs — evocli mcp 子命令（P3-2 MCP 集成）
///
/// evocli mcp list                    列出已注册的 MCP servers
/// evocli mcp connect <name> <cmd>    注册并测试连接一个 MCP server
/// evocli mcp tools <name>            列出指定 server 暴露的工具
/// evocli mcp call <server> <tool>    调用单个工具（测试用）
/// evocli mcp serve                   作为 stdin/stdout JSON-RPC MCP server 运行 EvoCLI 内置工具
/// evocli mcp expose                  输出 EvoCLI 内置工具的 MCP server 配置（tools 定义）
use anyhow::{Context, Result};
use clap::Subcommand;

/// Translate MCP tool name (underscore) to tool_dispatch name (dot).
///
/// MCP convention uses underscores: `git_status`, `code_intel_list_symbols`
/// tool_dispatch uses dots:          `git.status`, `code_intel.list_symbols`
///
/// Rules (longest-prefix matching on known namespaces):
///   code_intel_* → code_intel.*
///   fs_*         → fs.*
///   git_*        → git.*
///   shell_*      → shell.*
///   search_*     → search.*
///   memory_*     → memory.*
///   symbol_*     → symbol.*
///   assume_*     → assume.*
///   impact_*     → impact.*
///   equiv_*      → equiv.*
///   verify_*     → verify.*
///   approval_*   → approval.*
///   tool_*       → tool.*
fn mcp_name_to_dispatch(mcp_name: &str) -> String {
    const NAMESPACES: &[&str] = &[
        "code_intel", // must be before any single-word prefix
        "fs",
        "git",
        "shell",
        "search",
        "memory",
        "symbol",
        "assume",
        "impact",
        "equiv",
        "verify",
        "approval",
        "tool",
    ];
    for ns in NAMESPACES {
        let prefix = format!("{}_", ns);
        if let Some(rest) = mcp_name.strip_prefix(&prefix as &str) {
            return format!("{}.{}", ns, rest);
        }
    }
    // Fallback: replace first underscore with dot
    if let Some(pos) = mcp_name.find('_') {
        let (ns, rest) = mcp_name.split_at(pos);
        return format!("{}.{}", ns, &rest[1..]);
    }
    mcp_name.to_string()
}
use std::path::PathBuf;

fn mcp_config_path() -> PathBuf {
    dirs::home_dir()
        .unwrap_or_default()
        .join(".evocli")
        .join("mcp_servers.json")
}

#[derive(Debug, serde::Serialize, serde::Deserialize, Clone)]
struct McpServerConfig {
    name: String,
    program: String,
    args: Vec<String>,
}

fn load_servers() -> Vec<McpServerConfig> {
    let path = mcp_config_path();
    if !path.exists() {
        return vec![];
    }
    std::fs::read_to_string(&path)
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default()
}

fn save_servers(servers: &[McpServerConfig]) -> Result<()> {
    let path = mcp_config_path();
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(&path, serde_json::to_string_pretty(servers)?)?;
    Ok(())
}

#[derive(Subcommand)]
pub enum McpAction {
    /// List registered MCP servers
    List,
    /// Register an MCP server and test connection
    Connect {
        /// Server name (used as identifier)
        name: String,
        /// Program to run (e.g. npx, python)
        program: String,
        /// Arguments (e.g. -y @modelcontextprotocol/server-filesystem .)
        #[arg(trailing_var_arg = true)]
        args: Vec<String>,
    },
    /// List tools exposed by a registered MCP server
    Tools {
        /// Server name
        name: String,
    },
    /// Remove a registered MCP server
    Remove { name: String },
    /// Show EvoCLI tools in MCP server format (for use by external AI clients)
    Expose,
    /// Run EvoCLI as an MCP server (stdin/stdout JSON-RPC)
    Serve,
}

pub fn run(action: McpAction) -> Result<()> {
    match action {
        McpAction::List => cmd_list(),
        McpAction::Connect {
            name,
            program,
            args,
        } => cmd_connect(&name, &program, &args),
        McpAction::Tools { name } => cmd_tools(&name),
        McpAction::Remove { name } => cmd_remove(&name),
        McpAction::Expose => cmd_expose(),
        McpAction::Serve => cmd_serve(),
    }
}

// ── list ─────────────────────────────────────────────────────────────────────

fn cmd_list() -> Result<()> {
    let servers = load_servers();
    if servers.is_empty() {
        println!("No MCP servers registered.");
        println!("Register one: evocli mcp connect <name> <program> [args...]");
        println!();
        println!("Examples:");
        println!(
            "  evocli mcp connect filesystem npx -- -y @modelcontextprotocol/server-filesystem ."
        );
        println!("  evocli mcp connect git        uvx -- mcp-server-git --repository .");
        return Ok(());
    }
    println!("\n  {:<20} {}", "Name", "Command");
    println!("  {}", "─".repeat(60));
    for s in &servers {
        let cmd = format!("{} {}", s.program, s.args.join(" "));
        // char-based truncation: MCP commands can contain Unicode paths/args
        let cmd_display: String = cmd.chars().take(40).collect();
        let cmd_str = if cmd.chars().count() > 40 {
            format!("{}…", cmd_display)
        } else {
            cmd
        };
        println!("  {:<20} {}", s.name, cmd_str);
    }
    println!(
        "\n  {} server(s). Use `evocli mcp tools <name>` to see tools.\n",
        servers.len()
    );
    Ok(())
}

// ── connect ───────────────────────────────────────────────────────────────────

fn cmd_connect(name: &str, program: &str, args: &[String]) -> Result<()> {
    println!("Connecting to MCP server '{}'...", name);
    println!("  Program: {}", program);
    println!("  Args:    {}", args.join(" "));
    println!();

    // Test connection synchronously
    let rt = tokio::runtime::Runtime::new()?;
    let result = rt.block_on(async {
        let args_str: Vec<&str> = args.iter().map(|s| s.as_str()).collect();
        let client = mcp::McpClient::connect_stdio(program, &args_str).await?;
        let tools = client.list_tools().await?;
        Ok::<Vec<mcp::McpTool>, anyhow::Error>(tools)
    });

    match result {
        Ok(tools) => {
            println!("  ✅ Connected! {} tool(s) available:", tools.len());
            for t in tools.iter().take(10) {
                let desc = t.description.as_deref().unwrap_or("(no description)");
                // char-based truncation: MCP tool descriptions from external servers can be any language
                let desc_display: String = desc.chars().take(50).collect();
                println!("    • {:<30} {}", t.name, desc_display);
            }
            if tools.len() > 10 {
                println!("    ... and {} more", tools.len() - 10);
            }

            // Save to config
            let mut servers = load_servers();
            servers.retain(|s| s.name != name);
            servers.push(McpServerConfig {
                name: name.to_string(),
                program: program.to_string(),
                args: args.to_vec(),
            });
            save_servers(&servers)?;
            println!("\n  Saved to {}", mcp_config_path().display());
        }
        Err(e) => {
            println!("  ❌ Connection failed: {}", e);
            println!(
                "  Make sure '{}' is installed and the server starts correctly.",
                program
            );
        }
    }
    Ok(())
}

// ── tools ─────────────────────────────────────────────────────────────────────

fn cmd_tools(name: &str) -> Result<()> {
    let servers = load_servers();
    let server = servers
        .iter()
        .find(|s| s.name == name)
        .with_context(|| format!("Server '{}' not found. Run: evocli mcp list", name))?;

    let rt = tokio::runtime::Runtime::new()?;
    let tools = rt.block_on(async {
        let args: Vec<&str> = server.args.iter().map(|s| s.as_str()).collect();
        let client = mcp::McpClient::connect_stdio(&server.program, &args).await?;
        client.list_tools().await
    })?;

    println!("\n  MCP Server: {} ({} tool(s))", name, tools.len());
    println!("  {:<35} {}", "Tool", "Description");
    println!("  {}", "─".repeat(75));
    for t in &tools {
        let desc = t.description.as_deref().unwrap_or("—");
        // char-based truncation: external MCP tool descriptions can be any language
        let desc_display: String = desc.chars().take(40).collect();
        println!("  {:<35} {}", t.name, desc_display);
    }
    println!();
    Ok(())
}

// ── remove ────────────────────────────────────────────────────────────────────

fn cmd_remove(name: &str) -> Result<()> {
    let mut servers = load_servers();
    let before = servers.len();
    servers.retain(|s| s.name != name);
    if servers.len() == before {
        println!("Server '{}' not found.", name);
    } else {
        save_servers(&servers)?;
        println!("✅ Removed MCP server '{}'", name);
    }
    Ok(())
}

// ── expose ────────────────────────────────────────────────────────────────────

fn cmd_expose() -> Result<()> {
    println!("EvoCLI MCP Server — tool definitions for external AI clients:");
    println!("(Paste this into your AI client's MCP server configuration)\n");
    let tools = mcp::evocli_as_mcp_tools();
    println!("{}", serde_json::to_string_pretty(&tools)?);
    println!();
    println!("To use EvoCLI as an MCP server from Claude Desktop / Cursor:");
    println!("  1. Build: cargo build --release");
    println!("  2. Add to your MCP config (CLI: `evocli mcp serve`");
    println!("     {{\"evocli\": {{\"command\": \"evocli\", \"args\": [\"mcp\", \"serve\"]}}}}");
    Ok(())
}

// ── serve ─────────────────────────────────────────────────────────────────────

/// Run EvoCLI as a standards-compliant MCP server over stdin/stdout (JSON-RPC 2.0).
///
/// External AI clients (Claude Desktop, Cursor, Zed, etc.) can connect by adding:
/// ```json
/// {"evocli": {"command": "evocli", "args": ["mcp", "serve"]}}
/// ```
/// to their MCP server configuration.
fn cmd_serve() -> Result<()> {
    use serde_json::json;
    use std::io::{BufRead, BufReader, Write};

    let cfg = crate::config::Config::load_or_default().unwrap_or_default();
    eprintln!(
        "[EvoCLI MCP] Server v{} starting on stdin/stdout",
        env!("CARGO_PKG_VERSION")
    );

    let stdin = std::io::stdin();
    let stdout = std::io::stdout();
    let mut out = stdout.lock();
    let reader = BufReader::new(stdin.lock());
    let mut req_counter: u64 = 0;

    // Helper: write a JSON-RPC response line
    let mut send = |val: serde_json::Value| -> Result<()> {
        let line = serde_json::to_string(&val)?;
        writeln!(out, "{}", line)?;
        out.flush()?;
        Ok(())
    };

    for raw in reader.lines() {
        let raw = raw.context("stdin read error")?;
        let raw = raw.trim();
        if raw.is_empty() {
            continue;
        }

        let msg: serde_json::Value = match serde_json::from_str(raw) {
            Ok(v) => v,
            Err(e) => {
                eprintln!("[EvoCLI MCP] JSON parse error: {}", e);
                continue;
            }
        };

        let method = msg["method"].as_str().unwrap_or("").to_string();
        let id = msg.get("id").cloned();
        let params = msg.get("params").cloned().unwrap_or(json!({}));

        match method.as_str() {
            // MCP handshake: respond with server capabilities
            "initialize" => {
                let resp = json!({
                    "jsonrpc": "2.0",
                    "id": id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {
                            "tools": { "listChanged": false }
                        },
                        "serverInfo": {
                            "name": "evocli",
                            "version": env!("CARGO_PKG_VERSION")
                        }
                    }
                });
                send(resp)?;
            }
            // Notification — no response required
            "notifications/initialized" => {
                eprintln!("[EvoCLI MCP] Client initialized, ready for tool calls");
            }
            // Tool discovery: return EvoCLI's built-in tool set
            "tools/list" => {
                let tools_val = mcp::evocli_as_mcp_tools();
                let resp = json!({
                    "jsonrpc": "2.0",
                    "id": id,
                    "result": tools_val
                });
                send(resp)?;
            }
            // Tool execution: dispatch to EvoCLI's tool_dispatch
            "tools/call" => {
                let mcp_tool_name = params["name"].as_str().unwrap_or("").to_string();
                let dispatch_tool = mcp_name_to_dispatch(&mcp_tool_name);
                let arguments = params.get("arguments").cloned().unwrap_or(json!({}));

                req_counter += 1;
                let req_id = format!("mcp_{}", req_counter);

                // Fix: cmd_serve() runs inside #[tokio::main], so Runtime::new().block_on()
                // panics with "Cannot start a runtime from within a runtime".
                // Use block_in_place() + Handle::current() instead.
                let tool_result: std::result::Result<serde_json::Value, String> =
                    tokio::task::block_in_place(|| {
                        tokio::runtime::Handle::current().block_on(async {
                            let tc_req = soul_bridge::ToolCallRequest {
                                id: req_id.clone(),
                                tool: dispatch_tool.clone(),
                                args: arguments.clone(),
                            };
                            crate::tool_dispatch::dispatch(&tc_req, None, &cfg)
                                .await
                                .map_err(|e| e.to_string())
                        })
                    });

                let (content, is_error) = match tool_result {
                    Ok(val) => (json!([{"type": "text", "text": val.to_string()}]), false),
                    Err(err) => (
                        json!([{"type": "text", "text": format!("Error: {}", err)}]),
                        true,
                    ),
                };

                let resp = json!({
                    "jsonrpc": "2.0",
                    "id": id,
                    "result": {
                        "content": content,
                        "isError": is_error
                    }
                });
                send(resp)?;
            }
            // Unrecognised method — return JSON-RPC error if request had an id
            other => {
                if let Some(req_id) = &id {
                    let resp = json!({
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {
                            "code": -32601,
                            "message": format!("Method not found: {}", other)
                        }
                    });
                    send(resp)?;
                }
            }
        }
    }

    eprintln!("[EvoCLI MCP] stdin closed, server shutting down");
    Ok(())
}

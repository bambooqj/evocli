//! MCP (Model Context Protocol) 客户端 — v3.x
//!
//! 功能：
//!   1. 连接外部 MCP servers（stdio / SSE 两种传输）
//!   2. 发现 MCP server 暴露的工具列表
//!   3. 调用 MCP 工具，返回结果
//!   4. 将 EvoCLI 内置工具暴露为 MCP server（供其他 AI 消费）
//!
//! 参考设计：Section 4.1 + L216/234/255/3651
//! 传输协议：JSON-RPC 2.0 over stdin/stdout（stdio mode）
//!
//! 使用方式：
//!   let mut client = McpClient::connect_stdio("npx", &["-y", "@modelcontextprotocol/server-filesystem", "."]).await?;
//!   let tools = client.list_tools().await?;
//!   let result = client.call_tool("read_file", json!({"path": "src/main.rs"})).await?;

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::HashMap;
use std::process::Stdio;
use std::sync::Arc;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, Command};
use tokio::sync::{mpsc, Mutex};

// ── MCP Protocol Types ────────────────────────────────────────────────────────

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct McpTool {
    pub name: String,
    pub description: Option<String>,
    #[serde(rename = "inputSchema")]
    pub input_schema: Value,
}

#[derive(Debug, Serialize, Deserialize)]
struct McpRequest {
    jsonrpc: String,
    id: u64,
    method: String,
    params: Option<Value>,
}

#[derive(Debug, Serialize, Deserialize)]
struct McpResponse {
    jsonrpc: String,
    id: Option<u64>,
    result: Option<Value>,
    error: Option<McpError>,
}

#[derive(Debug, Serialize, Deserialize)]
struct McpError {
    code: i64,
    message: String,
}

// ── MCP Stdio Client ──────────────────────────────────────────────────────────

pub struct McpClient {
    _child: Child,
    stdin_tx: mpsc::UnboundedSender<String>,
    pending: Arc<Mutex<HashMap<u64, tokio::sync::oneshot::Sender<McpResponse>>>>,
    next_id: Arc<Mutex<u64>>,
}

impl McpClient {
    /// 通过 stdio 连接 MCP server（最常见模式）
    ///
    /// 示例：
    ///   McpClient::connect_stdio("npx", &["-y", "@modelcontextprotocol/server-filesystem", "."]).await?;
    ///   McpClient::connect_stdio("python", &["-m", "mcp_server_git"]).await?;
    pub async fn connect_stdio(program: &str, args: &[&str]) -> Result<Self> {
        let mut child = Command::new(program)
            .args(args)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .kill_on_drop(true) // Prevent zombie MCP server processes on McpClient drop
            .spawn()
            .with_context(|| format!("Failed to start MCP server: {} {:?}", program, args))?;

        let stdout = child.stdout.take().unwrap();
        let stdin = child.stdin.take().unwrap();

        let pending: Arc<Mutex<HashMap<u64, tokio::sync::oneshot::Sender<McpResponse>>>> =
            Arc::new(Mutex::new(HashMap::new()));
        let pending_clone = Arc::clone(&pending);

        let (stdin_tx, mut stdin_rx) = mpsc::unbounded_channel::<String>();

        // stdin writer task
        tokio::spawn(async move {
            let mut writer = tokio::io::BufWriter::new(stdin);
            while let Some(msg) = stdin_rx.recv().await {
                let _ = writer.write_all(msg.as_bytes()).await;
                let _ = writer.flush().await;
            }
        });

        // stdout reader task
        tokio::spawn(async move {
            let reader = BufReader::new(stdout);
            let mut lines = reader.lines();
            while let Ok(Some(line)) = lines.next_line().await {
                if line.trim().is_empty() {
                    continue;
                }
                if let Ok(resp) = serde_json::from_str::<McpResponse>(&line) {
                    if let Some(id) = resp.id {
                        let mut lock = pending_clone.lock().await;
                        if let Some(tx) = lock.remove(&id) {
                            let _ = tx.send(resp);
                        }
                    }
                }
            }
        });

        let client = Self {
            _child: child,
            stdin_tx,
            pending,
            next_id: Arc::new(Mutex::new(1)),
        };

        client.initialize().await?;
        Ok(client)
    }

    async fn next_id(&self) -> u64 {
        let mut id = self.next_id.lock().await;
        let v = *id;
        *id += 1;
        v
    }

    async fn send_request(&self, method: &str, params: Option<Value>) -> Result<Value> {
        let id = self.next_id().await;
        let req = McpRequest {
            jsonrpc: "2.0".into(),
            id,
            method: method.into(),
            params,
        };
        let json = serde_json::to_string(&req)? + "\n";
        let (tx, rx) = tokio::sync::oneshot::channel();
        self.pending.lock().await.insert(id, tx);
        self.stdin_tx.send(json).context("MCP stdin closed")?;
        let resp = tokio::time::timeout(std::time::Duration::from_secs(30), rx)
            .await
            .context("MCP timeout")?
            .context("MCP channel closed")?;
        if let Some(err) = resp.error {
            anyhow::bail!("MCP error {}: {}", err.code, err.message);
        }
        Ok(resp.result.unwrap_or(Value::Null))
    }

    async fn initialize(&self) -> Result<()> {
        let _ = self
            .send_request(
                "initialize",
                Some(serde_json::json!({
                    "protocolVersion": "2024-11-05",
                    "capabilities": { "roots": { "listChanged": true }, "sampling": {} },
                    "clientInfo": { "name": "evocli", "version": env!("CARGO_PKG_VERSION") }
                })),
            )
            .await?;
        let notif = serde_json::json!({ "jsonrpc": "2.0", "method": "notifications/initialized" });
        let _ = self.stdin_tx.send(notif.to_string() + "\n");
        Ok(())
    }

    pub async fn list_tools(&self) -> Result<Vec<McpTool>> {
        let result = self.send_request("tools/list", None).await?;
        let tools: Vec<McpTool> =
            serde_json::from_value(result.get("tools").cloned().unwrap_or(Value::Array(vec![])))?;
        Ok(tools)
    }

    pub async fn call_tool(&self, name: &str, arguments: Value) -> Result<Value> {
        self.send_request(
            "tools/call",
            Some(serde_json::json!({
                "name": name, "arguments": arguments
            })),
        )
        .await
    }

    pub async fn list_resources(&self) -> Result<Value> {
        self.send_request("resources/list", None).await
    }

    pub async fn read_resource(&self, uri: &str) -> Result<Value> {
        self.send_request("resources/read", Some(serde_json::json!({ "uri": uri })))
            .await
    }
}

// ── MCP Registry ──────────────────────────────────────────────────────────────

pub struct McpRegistry {
    servers: HashMap<String, McpClient>,
    tool_index: HashMap<String, String>,
}

impl McpRegistry {
    pub fn new() -> Self {
        Self {
            servers: HashMap::new(),
            tool_index: HashMap::new(),
        }
    }

    pub async fn register(
        &mut self,
        name: &str,
        program: &str,
        args: &[&str],
    ) -> Result<Vec<McpTool>> {
        let client = McpClient::connect_stdio(program, args).await?;
        let tools = client.list_tools().await?;
        for tool in &tools {
            self.tool_index.insert(tool.name.clone(), name.to_string());
        }
        self.servers.insert(name.to_string(), client);
        tracing::info!(
            "MCP server '{}' registered with {} tools",
            name,
            tools.len()
        );
        Ok(tools)
    }

    pub async fn call_tool(&self, tool_name: &str, args: Value) -> Result<Value> {
        let server_name = self
            .tool_index
            .get(tool_name)
            .with_context(|| format!("MCP tool '{}' not registered", tool_name))?;
        let client = self
            .servers
            .get(server_name)
            .with_context(|| format!("MCP server '{}' not found", server_name))?;
        client.call_tool(tool_name, args).await
    }

    pub async fn all_tool_definitions(&self) -> Vec<Value> {
        let mut defs = vec![];
        for client in self.servers.values() {
            if let Ok(tools) = client.list_tools().await {
                for tool in tools {
                    defs.push(serde_json::json!({
                        "type": "function",
                        "function": {
                            "name":        format!("mcp_{}", tool.name),
                            "description": tool.description.unwrap_or_else(|| tool.name.clone()),
                            "parameters":  tool.input_schema
                        }
                    }));
                }
            }
        }
        defs
    }

    pub fn registered_servers(&self) -> Vec<&str> {
        self.servers.keys().map(|s| s.as_str()).collect()
    }
}

impl Default for McpRegistry {
    fn default() -> Self {
        Self::new()
    }
}

// ── EvoCLI as MCP Server ──────────────────────────────────────────────────────

/// Number of EvoCLI built-in tools exposed via MCP.
///
/// **Maintenance contract**: when you add or remove a tool from
/// `evocli_as_mcp_tools()`, update this constant accordingly.
/// The `#[test] tool_count_matches_expected` below will catch drift.
pub const EVOCLI_MCP_TOOL_COUNT: usize = 59;

/// 将 EvoCLI 所有内置工具暴露为标准 MCP tools/list 响应（62个工具完整版）
/// 供其他 AI（Claude Desktop、Cursor 等）通过 MCP 协议使用 EvoCLI 能力
pub fn evocli_as_mcp_tools() -> Value {
    // Helper macro shorthand: object schema with string fields
    macro_rules! str_obj {
        ($($field:expr),*) => {{
            let mut props = serde_json::Map::new();
            let required_fields: Vec<&str> = vec![$($field),*];
            for f in &required_fields {
                props.insert(f.to_string(), serde_json::json!({"type":"string"}));
            }
            serde_json::json!({"type":"object","properties":props,"required":required_fields})
        }}
    }
    macro_rules! tool {
        ($name:expr, $desc:expr, $schema:expr) => {
            serde_json::json!({"name":$name,"description":$desc,"inputSchema":$schema})
        }
    }

    serde_json::json!({ "tools": [
        // ── File System ──────────────────────────────────────────────────
        tool!("fs_read",       "Read file contents",                   str_obj!("path")),
        tool!("fs_write",      "Write (overwrite) file contents",      serde_json::json!({"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"}},"required":["path","content"]})),
        tool!("fs_apply_diff", "Apply unified diff patch to a file",   serde_json::json!({"type":"object","properties":{"path":{"type":"string"},"diff":{"type":"string"},"dry_run":{"type":"boolean"}},"required":["path","diff"]})),
        tool!("fs_diff",       "Compute diff between two text strings", serde_json::json!({"type":"object","properties":{"path":{"type":"string"},"original":{"type":"string"},"modified":{"type":"string"}},"required":["path","original","modified"]})),

        // ── Git ──────────────────────────────────────────────────────────
        tool!("git_status",         "Get git working tree status",                         serde_json::json!({"type":"object","properties":{}})),
        tool!("git_commit",         "Commit staged changes with message",                  serde_json::json!({"type":"object","properties":{"message":{"type":"string"},"files":{"type":"array","items":{"type":"string"}}},"required":["message"]})),
        tool!("git_diff",           "Show current git diff (unstaged + staged)",           serde_json::json!({"type":"object","properties":{}})),
        tool!("git_snapshot",       "Create a side-git safety snapshot before edits",      serde_json::json!({"type":"object","properties":{}})),
        tool!("git_restore",        "Restore from last git snapshot",                      serde_json::json!({"type":"object","properties":{"stash_ref":{"type":"string"}},"required":["stash_ref"]})),
        tool!("git_shadow_snapshot","Create a shadow-git snapshot (won't touch .git)",     serde_json::json!({"type":"object","properties":{"label":{"type":"string"}}})),
        tool!("git_shadow_restore", "Restore from a shadow-git snapshot by label",         serde_json::json!({"type":"object","properties":{"snapshot":{"type":"string"},"project":{"type":"string"}},"required":["snapshot"]})),

        // ── Shell (allowlist-enforced) ────────────────────────────────────
        tool!("shell_run",   "Run allowlisted shell command (cargo/git/npm/python/make/go)", serde_json::json!({"type":"object","properties":{"cmd":{"type":"string"},"cwd":{"type":"string"},"timeout_s":{"type":"integer"},"dry_run":{"type":"boolean"}},"required":["cmd"]})),
        tool!("shell_grep",  "Search for regex pattern in files (like grep -rn)",          serde_json::json!({"type":"object","properties":{"pattern":{"type":"string"},"path":{"type":"string"}},"required":["pattern"]})),
        tool!("shell_find",  "Find files by name pattern",                                 serde_json::json!({"type":"object","properties":{"name":{"type":"string"},"path":{"type":"string"}}})),
        tool!("shell_ls",    "List directory contents",                                    serde_json::json!({"type":"object","properties":{"path":{"type":"string"},"long":{"type":"boolean"}}})),
        tool!("shell_cat",   "Display file contents",                                      str_obj!("file")),
        tool!("shell_mkdir", "Create directory",                                           str_obj!("path")),
        tool!("shell_wc",    "Count words/lines in file",                                  str_obj!("file")),
        tool!("shell_head",  "Show first N lines of file",                                 serde_json::json!({"type":"object","properties":{"file":{"type":"string"},"n":{"type":"integer"}},"required":["file"]})),
        tool!("shell_tail",  "Show last N lines of file",                                  serde_json::json!({"type":"object","properties":{"file":{"type":"string"},"n":{"type":"integer"}},"required":["file"]})),
        tool!("shell_mv",    "Move/rename a file or directory",                            serde_json::json!({"type":"object","properties":{"src":{"type":"string"},"dst":{"type":"string"}},"required":["src","dst"]})),
        tool!("shell_cp",    "Copy a file or directory",                                   serde_json::json!({"type":"object","properties":{"src":{"type":"string"},"dst":{"type":"string"}},"required":["src","dst"]})),
        tool!("shell_rm",    "Remove a file or directory (safety checks enforced)",        serde_json::json!({"type":"object","properties":{"path":{"type":"string"},"recursive":{"type":"boolean"}},"required":["path"]})),
        tool!("shell_touch", "Create an empty file or update timestamp",                   str_obj!("file")),

        // ── Search ───────────────────────────────────────────────────────
        tool!("search_code", "Semantic + BM25 hybrid code search across codebase",         serde_json::json!({"type":"object","properties":{"query":{"type":"string"},"path":{"type":"string"}},"required":["query"]})),

        // ── Memory ───────────────────────────────────────────────────────
        tool!("memory_recall",      "Recall project/tool/global memories relevant to query", serde_json::json!({"type":"object","properties":{"query":{"type":"string"},"top_k":{"type":"integer"}},"required":["query"]})),
        tool!("memory_write",       "Write a new memory (constraint/episodic/semantic/procedural)", serde_json::json!({"type":"object","properties":{"title":{"type":"string"},"body":{"type":"string"},"memory_type":{"type":"string"},"tags":{"type":"array","items":{"type":"string"}}},"required":["title","body"]})),
        tool!("memory_constraints", "List all constraint memories for the project",         serde_json::json!({"type":"object","properties":{"project_id":{"type":"string"}}})),

        // ── Code Intelligence ─────────────────────────────────────────────
        tool!("symbol_lookup",              "Look up symbol definition location",                   str_obj!("name")),
        tool!("symbol_variants",            "Find all variants/implementations of a type",          str_obj!("type_name")),
        tool!("symbol_usages",              "Find all usages of a symbol",                          str_obj!("symbol_id")),
        tool!("symbol_lifecycle",           "Trace symbol lifecycle (create/use/destroy)",          str_obj!("name")),
        tool!("code_intel_incoming_calls",  "Find all callers of a symbol (upstream callers)",      str_obj!("symbol_id")),
        tool!("code_intel_outgoing_calls",  "Find all callees of a symbol (downstream calls)",      str_obj!("symbol_id")),
        tool!("code_intel_full_chain",      "Full call chain (both directions) up to max_depth",    serde_json::json!({"type":"object","properties":{"symbol_id":{"type":"string"},"max_depth":{"type":"integer"}},"required":["symbol_id"]})),
        tool!("code_intel_impact_radius",   "Blast radius: all affected code if this symbol changes", str_obj!("symbol_id")),
        tool!("code_intel_list_symbols",    "List all indexed symbols in the project",              serde_json::json!({"type":"object","properties":{}})),
        tool!("code_intel_index_status",    "Get indexing status (last run, symbol count)",          serde_json::json!({"type":"object","properties":{}})),
        tool!("code_intel_ranked_context",  "PageRank-ranked symbols most relevant to a file",      serde_json::json!({"type":"object","properties":{"modified_file":{"type":"string"},"mentioned":{"type":"array","items":{"type":"string"}},"limit":{"type":"integer"}},"required":["modified_file"]})),

        // ── Assume / Analysis ─────────────────────────────────────────────
        tool!("assume_has_tests",       "Check if a symbol has associated tests",          str_obj!("symbol")),
        tool!("assume_caller_count",    "Count how many callers a symbol has",             str_obj!("symbol")),
        tool!("assume_is_pure",         "Check if a function is pure (no side effects)",   str_obj!("symbol")),
        tool!("assume_has_side_effects","Check if a function has side effects",             str_obj!("symbol")),
        tool!("assume_verify",          "Verify a specific assumption about code",         serde_json::json!({"type":"object","properties":{"assumption":{"type":"string"},"subject":{"type":"string"}},"required":["assumption","subject"]})),
        tool!("assume_is_deprecated",   "Check if a symbol is deprecated",                 str_obj!("symbol")),
        tool!("assume_is_only_caller",  "Check if caller is the only caller of target",   serde_json::json!({"type":"object","properties":{"caller":{"type":"string"},"target":{"type":"string"}},"required":["caller","target"]})),
        tool!("assume_types_match",     "Check if two symbols have compatible types",      serde_json::json!({"type":"object","properties":{"symbol_a":{"type":"string"},"symbol_b":{"type":"string"}},"required":["symbol_a","symbol_b"]})),

        // ── Impact ───────────────────────────────────────────────────────
        tool!("impact_check",         "Check full impact of changing a symbol",           serde_json::json!({"type":"object","properties":{"symbol":{"type":"string"},"change_type":{"type":"string"}},"required":["symbol"]})),
        tool!("impact_affected_tests", "Find tests that would be affected by changing symbol", str_obj!("symbol")),
        tool!("impact_batch_check",   "Check impact of multiple symbols at once",          serde_json::json!({"type":"object","properties":{"symbols":{"type":"array","items":{"type":"string"}},"change_type":{"type":"string"}},"required":["symbols"]})),

        // ── Equivalence ───────────────────────────────────────────────────
        tool!("equiv_find",             "Find existing code equivalent to a described intent", str_obj!("intent")),
        tool!("equiv_check_deps",       "Check if intent can be implemented with existing deps", str_obj!("intent")),
        tool!("equiv_find_similar_code","Find code similar to a given snippet",           str_obj!("code")),

        // ── Verification / Contracts ──────────────────────────────────────
        tool!("verify_task",     "Verify task completion against a contract",             serde_json::json!({"type":"object","properties":{"contract_id":{"type":"string"},"run_tests":{"type":"boolean"}}})),
        tool!("verify_coverage", "Check test coverage for a contract",                    serde_json::json!({"type":"object","properties":{"contract_id":{"type":"string"}}})),
        tool!("verify_drift",    "Detect drift between contract and implementation",      serde_json::json!({"type":"object","properties":{"contract_id":{"type":"string"}}})),

        // ── Approval / User Tools ─────────────────────────────────────────
        tool!("approval_request", "Request human approval before a destructive action",   serde_json::json!({"type":"object","properties":{"skill_id":{"type":"string"},"step_id":{"type":"string"},"action":{"type":"string"},"message":{"type":"string"}}})),
        tool!("tool_list_user",   "List user-registered custom tools",                    serde_json::json!({"type":"object","properties":{}})),
        tool!("tool_run_user",    "Run a user-registered custom tool",                    serde_json::json!({"type":"object","properties":{"name":{"type":"string"},"args":{"type":"string"},"dry_run":{"type":"boolean"}},"required":["name"]})),
    ]})
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Verifies that the number of tools in evocli_as_mcp_tools() matches
    /// EVOCLI_MCP_TOOL_COUNT.  If you add or remove a tool, update the
    /// constant — this test will fail loudly until you do.
    #[test]
    fn tool_count_matches_expected() {
        let tools_val = evocli_as_mcp_tools();
        let tools = tools_val["tools"]
            .as_array()
            .expect("evocli_as_mcp_tools() must return {\"tools\": [...]}");
        assert_eq!(
            tools.len(),
            EVOCLI_MCP_TOOL_COUNT,
            "MCP tool count mismatch: got {}, expected {}. \
             Update EVOCLI_MCP_TOOL_COUNT after adding/removing tools.",
            tools.len(),
            EVOCLI_MCP_TOOL_COUNT
        );
    }
}

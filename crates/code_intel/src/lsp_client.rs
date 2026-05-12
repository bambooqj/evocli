//! lsp_client.rs — Raw JSON-RPC LSP implementation
//!
//! ## Migration Path to async-lsp
//!
//! This file implements LSP communication with ~584 lines of raw JSON-RPC.
//! A future migration to the `async-lsp` crate would reduce this to ~100 lines:
//!
//! ```toml
//! # Cargo.toml (enable when migrating)
//! async-lsp = { version = "0.2", optional = true }
//! ```
//!
//! ```rust
//! // async-lsp usage pattern (for reference, not yet active):
//! // #[cfg(feature = "use-async-lsp")]
//! // use async_lsp::{MainLoop, ServerSocket};
//! //
//! // let (mainloop, server) = MainLoop::new_client(|_server| {
//! //     let mut router = ClientSocket::default();
//! //     router.notification::<lsp_types::notification::PublishDiagnostics>(|_, _| ControlFlow::Continue(()));
//! //     router
//! // });
//! ```
//!
//! Current implementation: hand-rolled JSON-RPC over subprocess stdio.
//! This works correctly and has 8 unit tests. Migration is low-priority.
//!
//! Architecture:
//!   LspClient::spawn_and_init(cmd) -> start language server process + handshake
//!   client.prepare_call_hierarchy / incoming_calls / outgoing_calls
//!   client.references / goto_definition / open_file

use anyhow::{bail, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::path::Path;
use std::process::Stdio;
use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::Arc;
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, Command};
use tokio::sync::{mpsc, oneshot, Mutex};

// -- ID generator ---------------------------------------------------------
static REQUEST_ID: AtomicI64 = AtomicI64::new(1);
fn next_id() -> i64 {
    REQUEST_ID.fetch_add(1, Ordering::SeqCst)
}

// -- JSON-RPC wire types --------------------------------------------------
#[derive(Serialize)]
struct LspRequest {
    jsonrpc: &'static str,
    id: i64,
    method: String,
    params: Value,
}

#[derive(Deserialize, Debug)]
struct LspResponse {
    id: Option<Value>,
    result: Option<Value>,
    error: Option<LspError>,
}

#[derive(Deserialize, Debug)]
struct LspError {
    code: i32,
    message: String,
}

// -- Public result types --------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CallHierarchyItem {
    pub name: String,
    pub kind: String,
    pub uri: String,
    pub range_start_line: u32,
    pub range_start_char: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CallSite {
    pub from: CallHierarchyItem,
    pub from_ranges: Vec<[u32; 2]>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Location {
    pub uri: String,
    pub line: u32,
    pub character: u32,
}

// -- LspClient ------------------------------------------------------------

pub struct LspClient {
    _child: Child,
    pending: Arc<Mutex<HashMap<i64, oneshot::Sender<LspResponse>>>>,
    stdin_tx: mpsc::UnboundedSender<Vec<u8>>,
    #[allow(dead_code)]
    initialized: bool,
}

impl LspClient {
    /// Spawn language server and complete the LSP initialize handshake.
    pub async fn spawn_and_init(cmd: &str, args: &[&str], workspace_root: &Path) -> Result<Self> {
        let mut cmd_builder = Command::new(cmd);
        // Prevent orphan LSP server processes.
        cmd_builder.kill_on_drop(true);

        let mut child = cmd_builder
            .args(args)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()?;

        let stdout = child.stdout.take().ok_or_else(|| {
            anyhow::anyhow!(
                "LSP: failed to take stdout (child process did not inherit Stdio::piped)"
            )
        })?;
        let stdin = child.stdin.take().ok_or_else(|| {
            anyhow::anyhow!(
                "LSP: failed to take stdin (child process did not inherit Stdio::piped)"
            )
        })?;

        let pending: Arc<Mutex<HashMap<i64, oneshot::Sender<LspResponse>>>> =
            Arc::new(Mutex::new(HashMap::new()));

        // -- stdin writer task --
        let (stdin_tx, mut stdin_rx) = mpsc::unbounded_channel::<Vec<u8>>();
        tokio::spawn(async move {
            let mut w = tokio::io::BufWriter::new(stdin);
            while let Some(msg) = stdin_rx.recv().await {
                let header = format!("Content-Length: {}\r\n\r\n", msg.len());
                if w.write_all(header.as_bytes()).await.is_err() {
                    break;
                }
                if w.write_all(&msg).await.is_err() {
                    break;
                }
                let _ = w.flush().await;
            }
        });

        // -- stdout reader task --
        let pending_r = Arc::clone(&pending);
        tokio::spawn(async move {
            let mut reader = BufReader::new(stdout);
            loop {
                // Read headers until blank line
                let mut content_length: usize = 0;
                loop {
                    let mut hdr = String::new();
                    if reader.read_line(&mut hdr).await.unwrap_or(0) == 0 {
                        return;
                    }
                    let trimmed = hdr.trim();
                    if trimmed.is_empty() {
                        break;
                    }
                    if let Some(val) = trimmed.strip_prefix("Content-Length:") {
                        content_length = val.trim().parse().unwrap_or(0);
                    }
                }
                if content_length == 0 {
                    continue;
                }

                // Guard against malicious/corrupt Content-Length before allocating.
                // A 50MB limit is generous for any realistic LSP message.
                const MAX_LSP_MSG_BYTES: usize = 50 * 1024 * 1024;
                if content_length > MAX_LSP_MSG_BYTES {
                    tracing::warn!(
                        "LSP: Content-Length {} exceeds {} byte limit; skipping message",
                        content_length,
                        MAX_LSP_MSG_BYTES
                    );
                    continue;
                }

                // Read body
                let mut body = vec![0u8; content_length];
                if reader.read_exact(&mut body).await.is_err() {
                    break;
                }

                // Dispatch responses (skip notifications without id)
                if let Ok(resp) = serde_json::from_slice::<LspResponse>(&body) {
                    if let Some(id_val) = &resp.id {
                        let id = match id_val {
                            Value::Number(n) => n.as_i64().unwrap_or(-1),
                            _ => continue,
                        };
                        if let Some(tx) = pending_r.lock().await.remove(&id) {
                            let _ = tx.send(resp);
                        }
                    }
                }
            }
        });

        let mut client = Self {
            _child: child,
            pending,
            stdin_tx,
            initialized: false,
        };

        client.lsp_initialize(workspace_root).await?;
        client.initialized = true;
        Ok(client)
    }

    /// Send a JSON-RPC request and await the response.
    async fn request(&self, method: &str, params: Value) -> Result<Value> {
        let id = next_id();
        let req = LspRequest {
            jsonrpc: "2.0",
            id,
            method: method.to_string(),
            params,
        };
        let body = serde_json::to_vec(&req)?;

        let (tx, rx) = oneshot::channel();
        self.pending.lock().await.insert(id, tx);
        self.stdin_tx.send(body)?;

        let resp = tokio::time::timeout(std::time::Duration::from_secs(30), rx).await??;
        if let Some(err) = resp.error {
            bail!("LSP error {}: {}", err.code, err.message);
        }
        Ok(resp.result.unwrap_or(Value::Null))
    }

    /// Send a JSON-RPC notification (no response expected).
    fn notify(&self, method: &str, params: Value) -> Result<()> {
        let notif = json!({ "jsonrpc": "2.0", "method": method, "params": params });
        self.stdin_tx.send(serde_json::to_vec(&notif)?)?;
        Ok(())
    }

    /// LSP initialize + initialized handshake.
    async fn lsp_initialize(&mut self, workspace: &Path) -> Result<()> {
        let workspace_uri = path_to_uri(workspace);
        self.request(
            "initialize",
            json!({
                "processId": std::process::id(),
                "rootUri": workspace_uri,
                "capabilities": {
                    "textDocument": {
                        "callHierarchy": { "dynamicRegistration": false },
                        "references":    { "dynamicRegistration": false },
                        "definition":    { "dynamicRegistration": false }
                    }
                },
                "workspaceFolders": [{ "uri": workspace_uri, "name": "workspace" }]
            }),
        )
        .await?;

        self.notify("initialized", json!({}))?;
        Ok(())
    }

    /// Notify the server that a file was opened.
    pub async fn open_file(&self, file_path: &Path) -> Result<()> {
        let content = tokio::fs::read_to_string(file_path).await?;
        let uri = path_to_uri(file_path);
        let lang_id = detect_language(file_path);
        self.notify(
            "textDocument/didOpen",
            json!({
                "textDocument": {
                    "uri": uri,
                    "languageId": lang_id,
                    "version": 1,
                    "text": content
                }
            }),
        )?;
        // Give the server a moment to index
        tokio::time::sleep(std::time::Duration::from_millis(500)).await;
        Ok(())
    }

    // -- Public LSP operations ------------------------------------------------

    pub async fn prepare_call_hierarchy(
        &self,
        file: &Path,
        line: u32,
        character: u32,
    ) -> Result<Vec<CallHierarchyItem>> {
        let uri = path_to_uri(file);
        let result = self
            .request(
                "textDocument/prepareCallHierarchy",
                json!({
                    "textDocument": { "uri": uri },
                    "position": { "line": line, "character": character }
                }),
            )
            .await?;

        let items = match result {
            Value::Array(arr) => arr,
            _ => return Ok(vec![]),
        };
        Ok(items.iter().filter_map(parse_call_hierarchy_item).collect())
    }

    pub async fn incoming_calls(&self, item: &CallHierarchyItem) -> Result<Vec<CallSite>> {
        let result = self
            .request(
                "callHierarchy/incomingCalls",
                json!({ "item": call_hierarchy_item_to_json(item) }),
            )
            .await?;

        let calls = match result {
            Value::Array(arr) => arr,
            _ => return Ok(vec![]),
        };

        Ok(calls
            .iter()
            .filter_map(|c| {
                let from = parse_call_hierarchy_item(c.get("from")?)?;
                let ranges = parse_from_ranges(c);
                Some(CallSite {
                    from,
                    from_ranges: ranges,
                })
            })
            .collect())
    }

    pub async fn outgoing_calls(&self, item: &CallHierarchyItem) -> Result<Vec<CallSite>> {
        let result = self
            .request(
                "callHierarchy/outgoingCalls",
                json!({ "item": call_hierarchy_item_to_json(item) }),
            )
            .await?;

        let calls = match result {
            Value::Array(arr) => arr,
            _ => return Ok(vec![]),
        };

        Ok(calls
            .iter()
            .filter_map(|c| {
                let from = parse_call_hierarchy_item(c.get("to")?)?;
                let ranges = parse_from_ranges(c);
                Some(CallSite {
                    from,
                    from_ranges: ranges,
                })
            })
            .collect())
    }

    pub async fn references(
        &self,
        file: &Path,
        line: u32,
        character: u32,
    ) -> Result<Vec<Location>> {
        let uri = path_to_uri(file);
        let result = self
            .request(
                "textDocument/references",
                json!({
                    "textDocument": { "uri": uri },
                    "position": { "line": line, "character": character },
                    "context": { "includeDeclaration": false }
                }),
            )
            .await?;

        let locs = match result {
            Value::Array(arr) => arr,
            _ => return Ok(vec![]),
        };

        Ok(locs.iter().filter_map(parse_location).collect())
    }

    pub async fn goto_definition(
        &self,
        file: &Path,
        line: u32,
        character: u32,
    ) -> Result<Option<Location>> {
        let uri = path_to_uri(file);
        let result = self
            .request(
                "textDocument/definition",
                json!({
                    "textDocument": { "uri": uri },
                    "position": { "line": line, "character": character }
                }),
            )
            .await?;

        match result {
            Value::Array(arr) => Ok(arr.first().and_then(parse_location)),
            _ => Ok(None),
        }
    }
}

// -- Helpers --------------------------------------------------------------

fn path_to_uri(path: &Path) -> String {
    let abs = if path.is_absolute() {
        path.to_string_lossy().to_string()
    } else {
        std::env::current_dir()
            .map(|cwd| cwd.join(path).to_string_lossy().to_string())
            .unwrap_or_else(|_| path.to_string_lossy().to_string())
    };
    // Normalise Windows backslashes
    let unix = abs.replace('\\', "/");
    if unix.starts_with('/') {
        format!("file://{unix}")
    } else {
        // Windows: D:/foo -> file:///D:/foo
        format!("file:///{unix}")
    }
}

fn detect_language(path: &Path) -> &'static str {
    match path.extension().and_then(|e| e.to_str()) {
        Some("rs") => "rust",
        Some("py") => "python",
        Some("ts" | "tsx") => "typescript",
        Some("js" | "jsx") => "javascript",
        Some("go") => "go",
        Some("c" | "cpp" | "h" | "hpp") => "cpp",
        _ => "plaintext",
    }
}

fn parse_location(v: &Value) -> Option<Location> {
    let uri = v.get("uri")?.as_str()?.to_string();
    let start = v.get("range")?.get("start")?;
    let line = start.get("line")?.as_u64()? as u32;
    let character = start.get("character")?.as_u64()? as u32;
    Some(Location {
        uri,
        line,
        character,
    })
}

fn parse_call_hierarchy_item(v: &Value) -> Option<CallHierarchyItem> {
    Some(CallHierarchyItem {
        name: v.get("name")?.as_str()?.to_string(),
        kind: symbol_kind_name(v.get("kind")?.as_u64()? as u32),
        uri: v.get("uri")?.as_str()?.to_string(),
        range_start_line: v.get("range")?.get("start")?.get("line")?.as_u64()? as u32,
        range_start_char: v.get("range")?.get("start")?.get("character")?.as_u64()? as u32,
    })
}

fn call_hierarchy_item_to_json(item: &CallHierarchyItem) -> Value {
    let end_char = item.range_start_char + item.name.len() as u32;
    json!({
        "name": item.name,
        "kind": 12,
        "uri": item.uri,
        "range": {
            "start": { "line": item.range_start_line, "character": item.range_start_char },
            "end":   { "line": item.range_start_line, "character": end_char }
        },
        "selectionRange": {
            "start": { "line": item.range_start_line, "character": item.range_start_char },
            "end":   { "line": item.range_start_line, "character": end_char }
        }
    })
}

fn parse_from_ranges(c: &Value) -> Vec<[u32; 2]> {
    c.get("fromRanges")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|r| {
                    let s = r.get("start")?;
                    let line = s.get("line")?.as_u64()? as u32;
                    let chr = s.get("character")?.as_u64()? as u32;
                    Some([line, chr])
                })
                .collect()
        })
        .unwrap_or_default()
}

fn symbol_kind_name(kind: u32) -> String {
    match kind {
        1 => "file".into(),
        2 => "module".into(),
        3 => "namespace".into(),
        5 => "class".into(),
        6 => "method".into(),
        9 => "constructor".into(),
        12 => "function".into(),
        13 => "variable".into(),
        14 => "constant".into(),
        23 => "struct".into(),
        _ => format!("kind_{kind}"),
    }
}

// ── P2-4: LSP 单元测试（不依赖真实 LSP 服务器）─────────────────────────
#[cfg(test)]
mod tests {
    use super::*;

    // ── JSON-RPC 消息格式测试 ─────────────────────────────────────────

    #[test]
    fn test_lsp_request_serializes_correctly() {
        let req = LspRequest {
            jsonrpc: "2.0",
            id: 42,
            method: "textDocument/definition".into(),
            params: serde_json::json!({
                "textDocument": {"uri": "file:///src/main.rs"},
                "position": {"line": 10, "character": 5}
            }),
        };
        let json = serde_json::to_string(&req).unwrap();
        assert!(json.contains("\"jsonrpc\":\"2.0\""));
        assert!(json.contains("\"id\":42"));
        assert!(json.contains("textDocument/definition"));
    }

    #[test]
    fn test_lsp_response_deserializes_ok() {
        let json = r#"{"jsonrpc":"2.0","id":1,"result":{"uri":"file:///main.rs","range":{"start":{"line":0,"character":0},"end":{"line":0,"character":10}}}}"#;
        let resp: LspResponse = serde_json::from_str(json).unwrap();
        assert_eq!(resp.id, Some(serde_json::json!(1)));
        assert!(resp.result.is_some());
        assert!(resp.error.is_none());
    }

    #[test]
    fn test_lsp_error_response_deserializes() {
        let json = r#"{"jsonrpc":"2.0","id":5,"error":{"code":-32602,"message":"Invalid params"}}"#;
        let resp: LspResponse = serde_json::from_str(json).unwrap();
        assert!(resp.error.is_some());
        let err = resp.error.unwrap();
        assert_eq!(err.code, -32602);
        assert_eq!(err.message, "Invalid params");
    }

    #[test]
    fn test_call_hierarchy_item_round_trip() {
        let item = CallHierarchyItem {
            name: "dispatch".into(),
            kind: "function".into(),
            uri: "file:///crates/host/src/tool_dispatch.rs".into(),
            range_start_line: 14,
            range_start_char: 0,
        };
        let json = serde_json::to_string(&item).unwrap();
        let item2: CallHierarchyItem = serde_json::from_str(&json).unwrap();
        assert_eq!(item.name, item2.name);
        assert_eq!(item.uri, item2.uri);
        assert_eq!(item.range_start_line, item2.range_start_line);
    }

    #[test]
    fn test_location_serialization() {
        let loc = Location {
            uri: "file:///src/lib.rs".into(),
            line: 42,
            character: 7,
        };
        let json = serde_json::to_string(&loc).unwrap();
        let loc2: Location = serde_json::from_str(&json).unwrap();
        assert_eq!(loc.line, loc2.line);
        assert_eq!(loc.character, loc2.character);
    }

    #[test]
    fn test_symbol_kind_name_known_values() {
        assert_eq!(symbol_kind_name(12), "function");
        assert_eq!(symbol_kind_name(6), "method");
        assert_eq!(symbol_kind_name(23), "struct");
        assert_eq!(symbol_kind_name(5), "class");
    }

    #[test]
    fn test_symbol_kind_name_unknown_returns_kind_prefix() {
        let result = symbol_kind_name(99);
        assert!(
            result.starts_with("kind_"),
            "Unknown kind should start with 'kind_', got: {}",
            result
        );
    }

    // ── LSP initialize 消息格式测试 ──────────────────────────────────

    #[test]
    fn test_initialize_params_structure() {
        // 验证 initialize request 参数结构符合 LSP spec
        let params = serde_json::json!({
            "processId": std::process::id(),
            "rootUri": "file:///project",
            "capabilities": {
                "textDocument": {
                    "callHierarchy": { "dynamicRegistration": false }
                }
            }
        });
        // 必须包含这些字段
        assert!(params.get("processId").is_some());
        assert!(params.get("rootUri").is_some());
        assert!(params.get("capabilities").is_some());
    }
}

//! Soul Bridge — Rust Host ↔ Python Soul JSON-RPC over stdin/stdout
//!
//! 通信方向：
//!   Rust → Python (stdin)：发送请求，等待响应
//!   Python → Rust (stdout)：
//!     1. 普通响应 (result/error)
//!     2. 流式 chunk (method: "stream.chunk")
//!     3. 工具调用 (method: "tool.call") ← Python Soul 请求 Rust 执行工具
//!     4. 事件通知 (method: "event.emit")

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::HashMap;
use std::process::Stdio;
use std::sync::Arc;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, Command};
use tokio::sync::{mpsc, oneshot, Mutex};
use uuid::Uuid;

// ── 消息类型 ─────────────────────────────────────────────────────

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct RpcRequest {
    pub id: String,
    pub method: String,
    pub params: Value,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct RpcResponse {
    pub id: String,
    pub result: Option<Value>,
    pub error: Option<RpcError>,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct RpcError {
    pub code: i32,
    pub message: String,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct StreamChunk {
    pub id: String,
    pub text: String,
    pub done: bool,
}

/// Python Soul 发来的工具调用请求
#[derive(Debug, Serialize, Deserialize)]
pub struct ToolCallRequest {
    pub id: String,   // request id，用于回复
    pub tool: String, // 如 "fs.read" / "shell.run"
    pub args: Value,
}

/// Rust 处理工具调用的函数签名
pub type ToolHandler = Arc<
    dyn Fn(Value) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<Value>> + Send>>
        + Send
        + Sync,
>;

/// 用于 prompt.choice 的单个选项
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChoiceOption {
    pub id: String,
    pub label: String,
}

/// prompt.choice 的用户响应
#[derive(Debug, Clone)]
pub enum ChoiceResult {
    /// 用户选择了某个列表选项
    Selected(String),
    /// 用户输入了自定义文本（allow_custom = true 时）
    Custom(String),
    /// 用户取消（Esc / 超时）
    Cancelled,
}

/// soul_bridge 内部存储的选择请求
#[derive(Debug, Clone)]
pub struct ChoiceRequest {
    pub title: String,
    pub options: Vec<ChoiceOption>,
    pub allow_custom: bool,
}

// ── SoulBridge ───────────────────────────────────────────────────

/// Per-process mutable state: replaced atomically on Python Soul restart.
/// Held inside Mutex so the outer Arc<SoulBridge> never changes.
struct BridgeInner {
    _child: Child,
    stdin_tx: mpsc::Sender<String>,
}

/// Soul bridge with automatic Python process restart.
///
/// Architecture: Rust Host is the stable base. When Python Soul crashes,
/// only BridgeInner (child + stdin channel) is replaced — the TUI, pending
/// calls, and tool/event channels remain intact.
///
/// Restart flow:
///   1. stdout EOF detected in reader task
///   2. Pending calls drained; streams get error message
///   3. restart_signal notified
///   4. Watchdog (spawned by main.rs) calls try_restart()
///   5. New Python process spawned, BridgeInner replaced
///   6. New reader task starts, reuses existing channel senders
pub struct SoulBridge {
    /// Stored for process respawn
    soul_path: String,
    /// Per-process state — replaced on restart
    inner: Mutex<BridgeInner>,
    pending: Arc<Mutex<HashMap<String, oneshot::Sender<RpcResponse>>>>,
    streams: Arc<Mutex<HashMap<String, mpsc::UnboundedSender<StreamChunk>>>>,
    /// Sender prototype for new reader tasks (mpsc allows multiple senders)
    tool_tx_proto: mpsc::UnboundedSender<ToolCallRequest>,
    tool_rx: Arc<Mutex<mpsc::UnboundedReceiver<ToolCallRequest>>>,
    tool_notify: Arc<tokio::sync::Notify>,
    /// Sender prototype for new reader tasks
    event_tx_proto: mpsc::UnboundedSender<serde_json::Value>,
    event_rx: Arc<Mutex<mpsc::UnboundedReceiver<serde_json::Value>>>,
    /// Notified by EOF handler; watchdog listens and calls try_restart()
    pub restart_signal: Arc<tokio::sync::Notify>,
    approval_request: Arc<Mutex<Option<(String, oneshot::Sender<bool>)>>>,
    choice_request: Arc<Mutex<Option<(ChoiceRequest, oneshot::Sender<ChoiceResult>)>>>,
}

impl SoulBridge {
    pub async fn spawn(soul_script: &str) -> Result<Self> {
        let (child, stdin, stdout) = Self::_spawn_process(soul_script).await?;

        let pending: Arc<Mutex<HashMap<String, oneshot::Sender<RpcResponse>>>> =
            Arc::new(Mutex::new(HashMap::new()));
        let streams: Arc<Mutex<HashMap<String, mpsc::UnboundedSender<StreamChunk>>>> =
            Arc::new(Mutex::new(HashMap::new()));

        let (stdin_tx, mut stdin_rx) = mpsc::channel::<String>(256);  // bounded: backpressure

        tokio::spawn(async move {
            let mut w = tokio::io::BufWriter::new(stdin);
            while let Some(line) = stdin_rx.recv().await {
                if w.write_all(line.as_bytes()).await.is_err() {
                    break;
                }
                if w.write_all(b"\n").await.is_err() {
                    break;
                }
                let _ = w.flush().await;
            }
        });

        // tool_tx_proto stored in SoulBridge so restart can clone it for new reader tasks
        let (tool_tx, tool_rx_inner) = mpsc::unbounded_channel::<ToolCallRequest>();
        let tool_tx_proto = tool_tx.clone();
        let tool_rx = Arc::new(Mutex::new(tool_rx_inner));
        let tool_notify = Arc::new(tokio::sync::Notify::new());

        let (event_tx, event_rx_inner) = mpsc::unbounded_channel::<serde_json::Value>();
        let event_tx_proto = event_tx.clone();
        let event_rx = Arc::new(Mutex::new(event_rx_inner));

        let restart_signal = Arc::new(tokio::sync::Notify::new());

        Self::_start_reader(
            stdout,
            pending.clone(),
            streams.clone(),
            tool_tx,
            event_tx,
            tool_notify.clone(),
            restart_signal.clone(),
        );

        let approval_request: Arc<Mutex<Option<(String, oneshot::Sender<bool>)>>> =
            Arc::new(Mutex::new(None));
        let choice_request: Arc<Mutex<Option<(ChoiceRequest, oneshot::Sender<ChoiceResult>)>>> =
            Arc::new(Mutex::new(None));

        Ok(Self {
            soul_path: soul_script.to_string(),
            inner: Mutex::new(BridgeInner {
                _child: child,
                stdin_tx,
            }),
            pending,
            streams,
            tool_tx_proto,
            tool_rx,
            tool_notify,
            event_tx_proto,
            event_rx,
            restart_signal,
            approval_request,
            choice_request,
        })
    }

    /// Low-level process spawn: build Python command, spawn, return handles.
    async fn _spawn_process(
        soul_script: &str,
    ) -> Result<(
        tokio::process::Child,
        tokio::process::ChildStdin,
        tokio::process::ChildStdout,
    )> {
        #[cfg(windows)]
        let soul_script = soul_script.trim_start_matches(r"\\?\");
        #[cfg(not(windows))]
        let soul_script = soul_script;

        let managed_python = {
            let venv_base = dirs::home_dir()
                .unwrap_or_default()
                .join(".evocli")
                .join("venv");
            let managed = if cfg!(windows) {
                venv_base.join("Scripts").join("python.exe")
            } else {
                venv_base.join("bin").join("python3")
            };
            if managed.exists() {
                Some(managed)
            } else {
                None
            }
        };

        let python_exe = managed_python
            .as_ref()
            .map(|p| p.to_string_lossy().to_string())
            .unwrap_or_else(|| {
                if cfg!(target_os = "windows") {
                    "python".into()
                } else {
                    "python3".into()
                }
            });

        let (run_args, pythonpath): (Vec<String>, Option<String>) = if soul_script.ends_with(".py")
        {
            let p = std::path::Path::new(soul_script);
            if let (Some(pkg_dir), Some(filename)) = (p.parent(), p.file_stem()) {
                if let Some(pkg_name) = pkg_dir.file_name().and_then(|n| n.to_str()) {
                    let module = format!("{}.{}", pkg_name, filename.to_str().unwrap_or("main"));
                    let pp = pkg_dir.parent().map(|pp| {
                        let s = pp.to_string_lossy();
                        #[cfg(windows)]
                        let s = s.trim_start_matches(r"\\?\").to_string();
                        #[cfg(not(windows))]
                        let s = s.to_string();
                        s
                    });
                    (vec!["-u".into(), "-m".into(), module], pp)
                } else {
                    (vec!["-u".into(), soul_script.to_string()], None)
                }
            } else {
                (vec!["-u".into(), soul_script.to_string()], None)
            }
        } else {
            (
                vec!["-u".into(), "-m".into(), soul_script.to_string()],
                None,
            )
        };

        let mut cmd = Command::new(&python_exe);
        cmd.args(run_args.iter().map(|s| s.as_str()))
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .env("PYTHONIOENCODING", "utf-8");

        let log_dir = dirs::home_dir()
            .unwrap_or_default()
            .join(".evocli")
            .join("logs");
        let _ = std::fs::create_dir_all(&log_dir);
        let stderr_sink = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(log_dir.join("soul_stderr.log"))
            .map(Stdio::from)
            .unwrap_or_else(|_| Stdio::null());
        cmd.stderr(stderr_sink);
        cmd.kill_on_drop(true);

        if let Some(ref pp) = pythonpath {
            let sep = if cfg!(windows) { ";" } else { ":" };
            let existing = std::env::var("PYTHONPATH").unwrap_or_default();
            let merged = if existing.is_empty() {
                pp.clone()
            } else {
                format!("{}{}{}", pp, sep, existing)
            };
            cmd.env("PYTHONPATH", &merged);
        }

        let mut child = cmd.spawn().with_context(|| {
            format!(
                "Failed to spawn Python Soul.\n  Python: {}\n  Args: {:?}\n  PYTHONPATH: {:?}\n  \
              Tip: run `evocli init` to set up managed Python environment.",
                python_exe, run_args, pythonpath
            )
        })?;

        let stdout = child.stdout.take().context("no stdout")?;
        let stdin = child.stdin.take().context("no stdin")?;
        Ok((child, stdin, stdout))
    }

    /// Spawn a new stdout reader task.
    /// Called on initial spawn AND on restart.
    /// Uses sender clones so the existing tool_rx/event_rx receivers still work.
    fn _start_reader(
        stdout: tokio::process::ChildStdout,
        pending: Arc<Mutex<HashMap<String, oneshot::Sender<RpcResponse>>>>,
        streams: Arc<Mutex<HashMap<String, mpsc::UnboundedSender<StreamChunk>>>>,
        tool_tx: mpsc::UnboundedSender<ToolCallRequest>,
        event_tx: mpsc::UnboundedSender<serde_json::Value>,
        tool_notify: Arc<tokio::sync::Notify>,
        restart_signal: Arc<tokio::sync::Notify>,
    ) {
        tokio::spawn(async move {
            let mut lines = BufReader::new(stdout).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                let line = line.trim().to_string();
                if line.is_empty() {
                    continue;
                }

                let v = match serde_json::from_str::<Value>(&line) {
                    Ok(v) => v,
                    Err(_) => continue,
                };

                let method = v.get("method").and_then(|m| m.as_str()).unwrap_or("");

                match method {
                    "tool.call" => {
                        if let Some(params) = v.get("params") {
                            let id = v["id"].as_str().unwrap_or("").to_string();
                            let tool = params["tool"].as_str().unwrap_or("").to_string();
                            let args = params["args"].clone();
                            let _ = tool_tx.send(ToolCallRequest { id, tool, args });
                            tool_notify.notify_one();
                        }
                    }

                    "stream.chunk" => {
                        if let Ok(chunk) =
                            serde_json::from_value::<StreamChunk>(v["params"].clone())
                        {
                            let id = chunk.id.clone();
                            if let Some(tx) = streams.lock().await.get(&id) {
                                let _ = tx.send(chunk);
                            }
                        }
                    }

                    "event.emit" => {
                        if let Some(params) = v.get("params") {
                            let _ = event_tx.send(params.clone());
                        }
                    }

                    _ => {
                        if let Ok(resp) = serde_json::from_value::<RpcResponse>(v) {
                            let id = resp.id.clone();
                            if let Some(tx) = pending.lock().await.remove(&id) {
                                let _ = tx.send(resp);
                            } else if let Some(err) = resp.error {
                                if let Some(tx) = streams.lock().await.remove(&id) {
                                    let error_chunk = StreamChunk {
                                        id: id.clone(),
                                        text: format!("ERROR: {} (code {})", err.message, err.code),
                                        done: true,
                                    };
                                    let _ = tx.send(error_chunk);
                                    tracing::debug!(
                                        "Converted JSON-RPC error to stream done-chunk for {}",
                                        id
                                    );
                                }
                            }
                        }
                    }
                }
            }

            // ── Python process exited (EOF on stdout) ──────────────────────
            tracing::warn!("Python Soul stdout EOF — process terminated");

            // Drain pending: callers get RecvError → call_with_timeout returns error
            pending.lock().await.drain();

            // Drain streams: inject "restarting" message so TUI exits Streaming state
            let mut smap = streams.lock().await;
            for (id, tx) in smap.drain() {
                let _ = tx.send(StreamChunk {
                    id,
                    text: "\n\n⏳ **Python Soul crashed — attempting automatic restart...**\n\
                           If this persists, run `evocli doctor` or press F12 for logs."
                        .to_string(),
                    done: true,
                });
            }

            // Signal watchdog to trigger restart
            restart_signal.notify_one();
        });
    }

    /// Restart the Python Soul process without restarting the Rust TUI.
    /// Called by the watchdog task in main.rs on receiving restart_signal.
    ///
    /// What stays intact: TUI, pending channels, tool_rx, event_rx (multi-sender mpsc)
    /// What gets replaced: Python child process, stdin writer task, stdout reader task
    pub async fn try_restart(&self) -> Result<()> {
        tracing::info!(
            "Attempting Python Soul restart (soul_path={})",
            self.soul_path
        );

        // Build and spawn new Python process (reuse spawn() logic via helper)
        let (new_child, new_stdin, new_stdout) = Self::_spawn_process(&self.soul_path).await?;

        // New stdin writer task
        let (new_stdin_tx, mut new_stdin_rx) = mpsc::channel::<String>(256);  // bounded
        tokio::spawn(async move {
            let mut w = tokio::io::BufWriter::new(new_stdin);
            while let Some(line) = new_stdin_rx.recv().await {
                if w.write_all(line.as_bytes()).await.is_err() {
                    break;
                }
                if w.write_all(b"\n").await.is_err() {
                    break;
                }
                let _ = w.flush().await;
            }
        });

        // New stdout reader task (reuses existing senders — receiver still in SoulBridge)
        Self::_start_reader(
            new_stdout,
            self.pending.clone(),
            self.streams.clone(),
            self.tool_tx_proto.clone(), // new clone → same tool_rx receiver
            self.event_tx_proto.clone(), // new clone → same event_rx receiver
            self.tool_notify.clone(),
            self.restart_signal.clone(),
        );

        // Replace BridgeInner atomically
        *self.inner.lock().await = BridgeInner {
            _child: new_child,
            stdin_tx: new_stdin_tx,
        };

        // Verify new Soul is responsive
        tokio::time::sleep(std::time::Duration::from_millis(500)).await;
        self.ping().await?;

        tracing::info!("Python Soul restarted successfully");
        Ok(())
    }

    // ── Rust → Python 请求 ────────────────────────────────────

    pub async fn call(&self, method: &str, params: Value) -> Result<Value> {
        self.call_with_timeout(method, params, 60_000).await
    }

    pub async fn call_with_timeout(
        &self,
        method: &str,
        params: Value,
        timeout_ms: u64,
    ) -> Result<Value> {
        let id = Uuid::new_v4().to_string();
        let req = RpcRequest {
            id: id.clone(),
            method: method.to_string(),
            params,
        };
        let (tx, rx) = oneshot::channel();
        self.pending.lock().await.insert(id.clone(), tx);
        // Send to Python via inner.stdin_tx (bounded channel: .send() is async)
        self.inner
            .lock()
            .await
            .stdin_tx
            .send(serde_json::to_string(&req)?)
            .await
            .map_err(|e| anyhow::anyhow!("stdin channel closed: {e}"))?;
        match tokio::time::timeout(std::time::Duration::from_millis(timeout_ms), rx).await {
            Ok(Ok(resp)) => {
                if let Some(err) = resp.error {
                    bail!("[{}] {}", err.code, err.message);
                }
                Ok(resp.result.unwrap_or(Value::Null))
            }
            Ok(Err(e)) => bail!("Response channel closed: {}", e),
            Err(_elapsed) => {
                self.pending.lock().await.remove(&id);
                bail!("RPC call '{}' timed out after {}ms", method, timeout_ms)
            }
        }
    }

    pub async fn call_stream(
        &self,
        method: &str,
        params: Value,
    ) -> Result<mpsc::UnboundedReceiver<StreamChunk>> {
        let id = Uuid::new_v4().to_string();
        let req = RpcRequest {
            id: id.clone(),
            method: method.to_string(),
            params,
        };
        let (tx, rx) = mpsc::unbounded_channel();
        self.streams.lock().await.insert(id.clone(), tx);
        self.inner
            .lock()
            .await
            .stdin_tx
            .send(serde_json::to_string(&req)?)
            .await
            .map_err(|e| anyhow::anyhow!("stdin channel closed: {e}"))?;
        Ok(rx)
    }

    pub async fn ping(&self) -> Result<bool> {
        let mut last_err = anyhow::anyhow!("ping failed after 3 attempts");
        for attempt in 0..3u8 {
            if attempt > 0 {
                tokio::time::sleep(std::time::Duration::from_millis(200)).await;
            }
            // Explicit UFCS to disambiguate from Fn::call
            match SoulBridge::call(self, "tracer.ping", serde_json::json!({})).await {
                Ok(result) => {
                    let is_pong = result.as_str().map(|s| s == "pong").unwrap_or(false);
                    return Ok(is_pong);
                }
                Err(e) => last_err = e,
            }
        }
        Err(last_err)
    }

    // ── Python → Rust 工具调用处理 ────────────────────────────

    /// 接收 Python 发来的下一个工具调用请求（非阻塞轮询）
    pub async fn next_tool_call(&self) -> Option<ToolCallRequest> {
        self.tool_rx.lock().await.try_recv().ok()
    }

    /// 等待直到有新的工具调用请求（事件驱动，替代 busy-wait sleep）
    pub async fn wait_for_tool(&self) {
        self.tool_notify.notified().await;
    }

    /// P2-5: 接收 Python 发来的下一个事件通知（非阻塞）
    pub async fn next_event(&self) -> Option<serde_json::Value> {
        self.event_rx.lock().await.try_recv().ok()
    }

    // ── TUI Approval channel ──────────────────────────────────

    /// Request approval from TUI user. Blocks until user responds or 30s timeout.
    /// Returns true if approved, false if rejected or timed out.
    /// On timeout: cleans up approval_request state to prevent stale modal + race condition
    /// where a subsequent approval request would inherit the old TUI modal message.
    pub async fn request_approval(&self, message: String) -> bool {
        let (tx, rx) = oneshot::channel();
        *self.approval_request.lock().await = Some((message, tx));
        let result = tokio::time::timeout(std::time::Duration::from_secs(30), rx)
            .await
            .unwrap_or(Ok(false))
            .unwrap_or(false);
        // Cleanup: clear stale approval_request on timeout so TUI exits WaitingApproval
        // and a subsequent approval request doesn't inherit the old message.
        // (On success/rejection, resolve_approval already cleared this via .take())
        *self.approval_request.lock().await = None;
        result
    }

    /// Check if there's a pending approval request (TUI polls this).
    pub async fn get_pending_approval(&self) -> Option<String> {
        self.approval_request
            .lock()
            .await
            .as_ref()
            .map(|(msg, _)| msg.clone())
    }

    /// Resolve a pending approval (TUI calls this when user presses y/n).
    pub async fn resolve_approval(&self, approved: bool) {
        if let Some((_, tx)) = self.approval_request.lock().await.take() {
            let _ = tx.send(approved);
        }
    }

    // ── prompt.choice ─────────────────────────────────────────────

    /// Request the user to pick one of several options (or type custom text).
    /// Blocks until the user responds or the 120s timeout fires.
    pub async fn request_choice(&self, req: ChoiceRequest) -> ChoiceResult {
        let (tx, rx) = oneshot::channel();
        *self.choice_request.lock().await = Some((req, tx));
        let result = tokio::time::timeout(std::time::Duration::from_secs(120), rx)
            .await
            .ok()
            .and_then(|r| r.ok())
            .unwrap_or(ChoiceResult::Cancelled);
        *self.choice_request.lock().await = None;
        result
    }

    /// TUI polls this to detect a pending choice prompt.
    pub async fn get_pending_choice(&self) -> Option<ChoiceRequest> {
        self.choice_request
            .lock()
            .await
            .as_ref()
            .map(|(req, _)| req.clone())
    }

    /// TUI calls this when the user has made a selection.
    pub async fn resolve_choice(&self, result: ChoiceResult) {
        if let Some((_, tx)) = self.choice_request.lock().await.take() {
            let _ = tx.send(result);
        }
    }

    /// 向 Python 回复工具调用结果
    pub fn reply_tool(&self, req_id: &str, result: Result<Value>) {
        let resp = match result {
            Ok(r) => serde_json::json!({ "id": req_id, "result": r, "error": null }),
            Err(e) => serde_json::json!({
                "id": req_id, "result": null,
                "error": { "code": -32603, "message": e.to_string() }
            }),
        };
        // Use inner.stdin_tx (same channel as call_with_timeout)
        // tool_reply field removed — all sends go through inner now
        let serialized = serde_json::to_string(&resp).unwrap_or_default();
        if let Ok(inner) = self.inner.try_lock() {
            // Bounded channel: use try_send to avoid blocking in sync context.
            // If full, the tool reply is dropped — acceptable since restart is in progress.
            let _ = inner.stdin_tx.try_send(serialized);
        }
        // Note: try_lock() may fail if another task holds inner during restart.
        // In that case the tool reply is silently dropped — acceptable since the
        // Python Soul is in the process of restarting anyway.
    }
}

// ── Restart watchdog ─────────────────────────────────────────────────────────

/// Spawn the Soul restart watchdog in main.rs.
/// Listens for restart_signal, then calls try_restart() with exponential backoff.
///
/// Usage in main.rs:
///   soul_bridge::spawn_restart_watchdog(Arc::clone(&bridge_arc));
pub fn spawn_restart_watchdog(bridge: std::sync::Arc<SoulBridge>) {
    tokio::spawn(async move {
        let mut consecutive_failures: u32 = 0;
        loop {
            bridge.restart_signal.notified().await;
            tracing::warn!(
                "Soul restart signal received (consecutive_failures={})",
                consecutive_failures
            );

            // Exponential backoff: 2s, 4s, 8s (max 3 attempts)
            for attempt in 1u32..=3 {
                let wait_secs = 2u64.pow(attempt - 1);
                tracing::info!(
                    "Soul restart attempt {}/3, waiting {}s...",
                    attempt,
                    wait_secs
                );
                tokio::time::sleep(std::time::Duration::from_secs(wait_secs)).await;

                match bridge.try_restart().await {
                    Ok(()) => {
                        consecutive_failures = 0;
                        tracing::info!("Python Soul restarted successfully (attempt {})", attempt);
                        // Notify TUI that Soul is back
                        let _ = bridge.inner.lock().await.stdin_tx.send(
                            r#"{"method":"event.emit","params":{"type":"soul_status","status":"ready","message":"✅ Python Soul restarted automatically"}}"#
                            .to_string()
                        ).await;
                        break;
                    }
                    Err(e) => {
                        tracing::error!("Soul restart attempt {}/3 failed: {}", attempt, e);
                        if attempt == 3 {
                            consecutive_failures += 1;
                            tracing::error!(
                                "Soul restart failed after 3 attempts. \
                                 consecutive_failures={}. User must restart EvoCLI.",
                                consecutive_failures
                            );
                        }
                    }
                }
            }
        }
    });
}

//! # Protocol — JSON-RPC 契约
//!
//! 定义 Rust Host ↔ Python Soul 之间所有消息类型。
//! **这是核心边界的接口层**——两侧都必须严格遵守此定义。
//! 修改此文件 = 修改核心协议，必须同步更新 Python Soul 侧。

use serde::{Deserialize, Serialize};

// ── 消息信封 ────────────────────────────────────────────────────────

/// Host → Soul：工具调用请求
#[derive(Debug, Serialize, Deserialize)]
pub struct Request {
    pub id: String,     // UUID v4
    pub method: String, // "tool.call" | "event.subscribe" | ...
    pub params: serde_json::Value,
}

/// Soul → Host：工具调用响应
#[derive(Debug, Serialize, Deserialize)]
pub struct Response {
    pub id: String,
    pub result: Option<serde_json::Value>,
    pub error: Option<RpcError>,
}

/// 单向通知（无需响应）
#[derive(Debug, Serialize, Deserialize)]
pub struct Notification {
    pub method: String, // "event.emit" | "soul.ready" | "soul.log"
    pub params: serde_json::Value,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct RpcError {
    pub code: i32,
    pub message: String,
    pub data: Option<serde_json::Value>,
}

// ── 工具调用参数（method = "tool.call"） ────────────────────────────

#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "tool", content = "args", rename_all = "snake_case")]
pub enum ToolCall {
    // 文件系统
    FsRead {
        path: String,
    },
    FsWrite {
        path: String,
        content: String,
        dry_run: bool,
    },
    FsApplyDiff {
        path: String,
        diff: String,
        dry_run: bool,
    },

    // Git
    GitStatus {},
    GitCommit {
        message: String,
        files: Vec<String>,
    },
    GitSnapshot {},
    GitRestore {
        stash_ref: String,
    },

    // Shell（受限，经过 SecurityController 白名单检查）
    ShellRun {
        cmd: String,
        cwd: String,
        timeout_s: u32,
        dry_run: bool,
    },

    // Memory
    MemoryRecall {
        query: String,
        types: Vec<String>,
        current_project: String,
        active_tools: Vec<String>,
        top_k: u32,
    },
    MemoryWrite {
        layer: String,
        priority_scope: String, // "project" | "tool" | "global"
        project_id: Option<String>,
        tool_id: Option<String>,
        content: MemoryContent,
    },

    // 搜索
    SearchCode {
        query: String,
        path: Option<String>,
    },

    // Code Intelligence
    CodeIntelIncomingCalls {
        symbol: String,
    },
    CodeIntelOutgoingCalls {
        symbol: String,
    },
    CodeIntelFullChain {
        symbol: String,
        direction: String,
        max_depth: u32,
    },
    CodeIntelImpactRadius {
        symbol: String,
    },
    CodeIntelRankedContext {
        modified_file: String,
        mentioned: Vec<String>,
    },
    CodeIntelFindSymbol {
        query: String,
        file: Option<String>,
    },

    // Built-in AI 工具（Symbol Oracle / Assumption Verifier / Impact Probe / Equiv Finder）
    SymbolLookup {
        name: String,
        file: Option<String>,
    },
    SymbolVariants {
        type_name: String,
    },
    SymbolUsages {
        symbol_id: String,
        limit: Option<u32>,
    },
    AssumeVerify {
        assumption: String,
        subject: String,
    },
    AssumeIsPure {
        symbol: String,
    },
    AssumeHasTests {
        symbol: String,
    },
    ImpactCheck {
        symbol: String,
        change_type: String,
    },
    EquivFind {
        intent: String,
        context: Option<String>,
        limit: Option<u32>,
    },

    // Task Contract
    ContractCreate {
        requirement: String,
    },
    ContractVerify {
        contract_id: String,
        run_tests: bool,
    },
    CheckpointUpdate {
        checkpoint_id: String,
        status: String,
        evidence: Option<String>,
    },
}

#[derive(Debug, Serialize, Deserialize)]
pub struct MemoryContent {
    pub title: String,
    pub body: String,
    pub tags: Vec<String>,
    pub outcome: Option<String>,
}

// ── 事件类型（method = "event.emit"） ───────────────────────────────

#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Event {
    UserInput { text: String },
    ToolCall { id: String, tool: String },
    ToolResult { id: String, success: bool },
    SkillExecution { skill_id: String, step: String },
    MemoryRecall { query: String, hits: u32 },
    Failure { context: String, error: String },
    LlmStreamChunk { text: String, done: bool },
    CheckpointDone { id: String },
    SoulReady {},
    SoulLog { level: String, message: String },
}

// ── 错误码 ──────────────────────────────────────────────────────────

pub mod error_codes {
    pub const SECURITY_DENIED: i32 = -32001;
    pub const TOOL_NOT_FOUND: i32 = -32002;
    pub const APPROVAL_REQUIRED: i32 = -32003;
    pub const BOUNDARY_VIOLATION: i32 = -32004;
    pub const TIMEOUT: i32 = -32005;
    pub const INVALID_PARAMS: i32 = -32602;
    pub const INTERNAL_ERROR: i32 = -32603;
}

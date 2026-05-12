//! Configuration management — reads/writes ~/.evocli/config.toml

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::path::PathBuf;

/// Top-level configuration
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Config {
    #[serde(default)]
    pub llm: LlmConfig,
    #[serde(default)]
    pub context: ContextConfig,
    #[serde(default)]
    pub safety: SafetyConfig,
    #[serde(default)]
    pub security: SecurityConfig,
    #[serde(default)]
    pub memory: MemoryConfig,
    #[serde(default)]
    pub agent: AgentConfig,
    #[serde(default)]
    pub tui: TuiConfig,
    /// Python Soul 脚本路径（evocli init 时自动检测并保存；优先级低于 EVOCLI_SOUL 环境变量）
    #[serde(default)]
    pub soul_script: Option<String>,
    #[serde(default)]
    pub graph: GraphConfig,
}

/// LLM connection — only the essentials.
///
/// Protocol: OpenAI-compatible by default (works with 99% of providers).
/// litellm handles protocol differences automatically.
///
/// Users fill in THREE things:
///   base_url  — endpoint URL (any OpenAI-compatible API)
///   api_key   — stored in OS keyring by evocli init
///   tiers     — model names to use (fast + smart)
///
/// ```toml
/// [llm]
/// base_url = "https://api.openai.com/v1"   # OpenAI, or any compatible endpoint
///
/// [llm.tiers]
/// fast  = "gpt-4o-mini"   # routine tasks: edits, commits, summaries
/// smart = "gpt-4o"        # complex tasks: architecture, code review
/// ```
///
/// Common endpoint examples:
///   OpenAI:     https://api.openai.com/v1
///   Anthropic:  (leave blank — litellm auto-detects from "claude-*" model name)
///   DeepSeek:   https://api.deepseek.com/v1
///   Groq:       https://api.groq.com/openai/v1
///   SiliconFlow: https://api.siliconflow.cn/v1
///   Ollama:     http://localhost:11434  (no key needed)
///   Custom proxy: https://your-proxy.com/v1
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LlmConfig {
    /// Default API endpoint. Any OpenAI-compatible URL, or blank for auto-detection.
    pub base_url: Option<String>,
    /// Default API key (stored in OS keyring by evocli init).
    pub api_key: Option<String>,
    /// Default model tiers — used when a role has no specific override.
    #[serde(default)]
    pub tiers: LlmTiers,
    /// Global and per-task LLM parameters (token limits, temperature, etc.)
    #[serde(default)]
    pub params: LlmGlobalParams,
    /// Per-task model routing (which tier alias each task uses by default)
    #[serde(default)]
    pub tasks: LlmTasksConfig,
    /// Per-role full overrides — each role can have its own provider/model/key.
    /// Takes priority over [llm.tasks] routing when present.
    /// Missing fields fall back to the global [llm] settings.
    #[serde(default)]
    pub roles: LlmRolesConfig,
}

/// Per-role LLM configuration — each role can use a completely different provider.
///
/// Roles map to the task types in [llm.tasks]. Configure only the roles you want
/// to customize; the rest fall back to the global [llm] settings.
///
/// Example — use Anthropic for planning, DeepSeek for editing, OpenAI for the rest:
/// ```toml
/// [llm.roles.architect]
/// base_url = "https://api.anthropic.com"
/// model    = "claude-opus-4-7"
///
/// [llm.roles.editor]
/// base_url = "https://api.deepseek.com/v1"
/// model    = "deepseek-coder"
///
/// [llm.roles.code_review]
/// base_url = "https://api.anthropic.com"
/// model    = "claude-sonnet-4-7"
/// api_key  = "sk-ant-..."       # optional per-role key override
/// ```
///
/// Anthropic-specific: when base_url is "https://api.anthropic.com" (or blank
/// with a "claude-*" model), litellm automatically uses the Anthropic SDK protocol
/// with the correct auth headers — no extra configuration needed.
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct LlmRolesConfig {
    pub chat: Option<LlmRoleConfig>,
    pub architect: Option<LlmRoleConfig>,
    pub editor: Option<LlmRoleConfig>,
    pub summarize: Option<LlmRoleConfig>,
    pub commit: Option<LlmRoleConfig>,
    pub lint: Option<LlmRoleConfig>,
    pub memory_label: Option<LlmRoleConfig>,
    pub code_review: Option<LlmRoleConfig>,
    pub wiki: Option<LlmRoleConfig>,
}

/// Configuration for a single agent role.
/// All fields are optional — unset fields inherit from the global [llm] section.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LlmRoleConfig {
    /// Endpoint URL for this role. Falls back to [llm].base_url if not set.
    pub base_url: Option<String>,
    /// API key for this role. Falls back to [llm].api_key / keyring if not set.
    pub api_key: Option<String>,
    /// Model name or tier alias ("fast"/"smart") for this role.
    /// Required: there is no fallback model if this is blank.
    pub model: String,
}

/// Global default parameters for all LLM calls.
/// Individual tasks can override these in [llm.params.<task_name>].
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LlmGlobalParams {
    /// Default output token limit for all completions
    #[serde(default = "default_max_tokens")]
    pub max_tokens: usize,
    /// Default sampling temperature (0.0 = deterministic, 1.0 = creative)
    #[serde(default = "default_temperature")]
    pub temperature: f64,
    /// Retry count for failed API calls
    #[serde(default = "default_max_retries")]
    pub max_retries: u32,
    /// Per-task parameter overrides (indexed by task name)
    #[serde(default)]
    pub architect: LlmTaskParams,
    #[serde(default)]
    pub editor: LlmTaskParams,
    #[serde(default)]
    pub commit: LlmTaskParams,
    #[serde(default)]
    pub summarize: LlmTaskParams,
    #[serde(default)]
    pub lint: LlmTaskParams,
    #[serde(default)]
    pub memory_label: LlmTaskParams,
    #[serde(default)]
    pub code_review: LlmTaskParams,
    #[serde(default)]
    pub wiki: LlmTaskParams,
}

impl Default for LlmGlobalParams {
    fn default() -> Self {
        Self {
            max_tokens: default_max_tokens(),
            temperature: default_temperature(),
            max_retries: default_max_retries(),
            architect: LlmTaskParams {
                max_tokens: Some(8192),
                temperature: Some(0.7),
            },
            editor: LlmTaskParams {
                max_tokens: Some(4096),
                temperature: Some(0.2),
            },
            commit: LlmTaskParams {
                max_tokens: Some(120),
                temperature: Some(0.3),
            },
            summarize: LlmTaskParams {
                max_tokens: Some(1500),
                temperature: Some(0.3),
            },
            lint: LlmTaskParams {
                max_tokens: Some(2048),
                temperature: Some(0.0),
            },
            memory_label: LlmTaskParams {
                max_tokens: Some(60),
                temperature: Some(0.0),
            },
            code_review: LlmTaskParams {
                max_tokens: Some(4096),
                temperature: Some(0.5),
            },
            wiki: LlmTaskParams {
                max_tokens: Some(400),
                temperature: Some(0.4),
            },
        }
    }
}

/// Per-task parameter overrides (both optional — falls back to global defaults)
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct LlmTaskParams {
    pub max_tokens: Option<usize>,
    pub temperature: Option<f64>,
}

/// Per-task model routing.
/// Each field is either a tier alias ("fast"/"smart") or a specific model name.
/// This is the core of the fine-grained routing system — users control exactly
/// which model handles each type of work.
///
/// Example config.toml:
/// ```toml
/// [llm.tasks]
/// chat       = "smart"              # tier alias
/// architect  = "smart"
/// editor     = "fast"
/// commit     = "fast"
/// summarize  = "fast"
/// lint       = "fast"
/// code_review = "claude-opus-4-7"  # specific model override
/// ```
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LlmTasksConfig {
    /// Main conversation and generic agent requests
    #[serde(default = "tier_smart")]
    pub chat: String,
    /// Architect mode planning (system design, multi-file analysis)
    #[serde(default = "tier_smart")]
    pub architect: String,
    /// Code editing (SEARCH/REPLACE, precise changes — fast model usually fine)
    #[serde(default = "tier_fast")]
    pub editor: String,
    /// Session compression and context summarization
    #[serde(default = "tier_fast")]
    pub summarize: String,
    /// Commit message generation
    #[serde(default = "tier_fast")]
    pub commit: String,
    /// Lint/test output analysis and fix suggestions
    #[serde(default = "tier_fast")]
    pub lint: String,
    /// Memory classification (MemRouter labeling)
    #[serde(default = "tier_fast")]
    pub memory_label: String,
    /// Code review (needs deeper reasoning)
    #[serde(default = "tier_smart")]
    pub code_review: String,
    /// Wiki and documentation generation
    #[serde(default = "tier_fast")]
    pub wiki: String,
}

impl Default for LlmTasksConfig {
    fn default() -> Self {
        Self {
            chat: tier_smart(),
            architect: tier_smart(),
            editor: tier_fast(),
            summarize: tier_fast(),
            commit: tier_fast(),
            lint: tier_fast(),
            memory_label: tier_fast(),
            code_review: tier_smart(),
            wiki: tier_fast(),
        }
    }
}

/// Model name tiers — user fills these in with whatever their endpoint provides.
///
/// No defaults are provided because model names are endpoint-specific.
/// evocli init will prompt you to enter these.
///
/// Use tier aliases ("fast"/"smart") in [llm.tasks] to route tasks,
/// or specify the full model name for per-task overrides.
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct LlmTiers {
    /// Fast/cheap model — used for: edits, commits, summaries, lint feedback
    /// Examples: gpt-4o-mini, claude-haiku-4-5, deepseek-chat, qwen2.5-coder:7b
    #[serde(default)]
    pub fast: String,
    /// Smart/powerful model — used for: architecture, code review, planning
    /// Examples: gpt-4o, claude-sonnet-4-7, deepseek-reasoner, qwen2.5-coder:32b
    #[serde(default)]
    pub smart: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ContextConfig {
    /// Max total context tokens
    #[serde(default = "default_max_total")]
    pub max_total: usize,
    /// Max code context tokens
    #[serde(default = "default_max_code")]
    pub max_code: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SafetyConfig {
    /// Auto-approve file writes in project dir
    #[serde(default)]
    pub auto_approve_writes: bool,
    /// Shell command whitelist patterns (legacy field, now merged into security.extra_allowed)
    #[serde(default = "default_shell_whitelist")]
    pub shell_whitelist: Vec<String>,
    /// Show a unified diff and ask for confirmation before applying any file edit.
    /// Prevents surprise changes. Mirrors Cursor's "preview before apply" behavior.
    ///
    /// Example config.toml:
    /// ```toml
    /// [safety]
    /// require_diff_preview = true
    /// ```
    #[serde(default)]
    pub require_diff_preview: bool,
}

/// 安全策略配置（config.toml [security] 节）
///
/// 示例配置（~/.evocli/config.toml）:
/// ```toml
/// [security]
/// allow_all_commands    = false   # 严格模式：只允许 allowed_commands 中的命令
/// allow_all_paths       = false   # 严格模式：只允许访问非 denied_paths 路径
/// block_dangerous_always = true   # 永远阻断高危操作（即使 allow_all=true）
///
/// # 完整命令白名单（覆盖内置默认值）
/// allowed_commands = ["cargo", "git", "python", "npm", "ls", "cat", ...]
///
/// # 额外追加的允许命令（不影响 allowed_commands 内置列表）
/// extra_allowed_commands = ["docker", "kubectl", "terraform"]
///
/// # 危险模式黑名单（覆盖内置默认值）
/// blocked_patterns = ["rm -rf /", "mkfs", ":(){:|:&};:"]
///
/// # 额外追加的危险模式（不影响 blocked_patterns 内置列表）
/// extra_blocked_patterns = ["curl * | bash", "wget * -O- | sh"]
///
/// # 路径访问黑名单（覆盖内置默认值）
/// denied_paths = [".evocli/config.toml", ".ssh", ".gnupg", "/etc/passwd"]
///
/// # 额外追加的禁止路径
/// extra_denied_paths = ["/prod", "/etc/nginx"]
/// ```
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SecurityConfig {
    /// 黑名单模式（default）：允许所有命令，仅阻断危险模式。
    /// false = 严格白名单模式：只允许 allowed_commands + extra_allowed_commands。
    #[serde(default = "default_true")]
    pub allow_all_commands: bool,

    /// 允许访问任意路径。false = 只允许访问非 denied_paths 路径。
    #[serde(default)]
    pub allow_all_paths: bool,

    /// 即使 allow_all_commands=true，也永远阻断已知危险模式。
    /// 强烈建议保持 true。
    #[serde(default = "default_true")]
    pub block_dangerous_always: bool,

    /// 命令白名单（严格模式下使用）。
    /// 默认为内置安全命令列表。可以完全覆盖（替换而非追加）。
    #[serde(default = "default_allowed_commands")]
    pub allowed_commands: Vec<String>,

    /// 额外追加到白名单的命令（叠加在 allowed_commands 之上）。
    /// 例如：["docker", "kubectl", "terraform", "ansible"]
    #[serde(default)]
    pub extra_allowed_commands: Vec<String>,

    /// 危险模式黑名单（子串匹配）。
    /// 默认为内置高危模式列表。可以完全覆盖。
    #[serde(default = "default_blocked_patterns")]
    pub blocked_patterns: Vec<String>,

    /// 额外追加到危险模式黑名单的模式（正则或子串匹配）。
    #[serde(default)]
    pub extra_blocked_patterns: Vec<String>,

    /// 路径访问黑名单。
    /// 默认为内置保护路径。可以完全覆盖。
    /// 注意：config.toml 本身始终受保护（代码兜底），不受此配置影响。
    #[serde(default = "default_denied_paths")]
    pub denied_paths: Vec<String>,

    /// 额外追加到路径黑名单的路径前缀。
    #[serde(default)]
    pub extra_denied_paths: Vec<String>,
}

fn default_true() -> bool {
    true
}

/// 内置命令白名单默认值（等价于之前硬编码的 ALLOWED_PREFIXES）
pub fn default_allowed_commands() -> Vec<String> {
    vec![
        // Build tools
        "cargo",
        "rustc",
        "rustup",
        "rust-analyzer",
        "npm",
        "npx",
        "node",
        "pnpm",
        "yarn",
        "bun",
        "deno",
        "python",
        "python3",
        "pip",
        "uv",
        "go",
        "gofmt",
        "gopls",
        "make",
        "cmake",
        "ninja",
        "mvn",
        "gradle",
        "java",
        "javac",
        "dotnet",
        // EvoCLI itself
        "evocli",
        // Version control
        "git",
        // Navigation
        "cd",
        // Shell read-only
        "cat",
        "ls",
        "dir",
        "echo",
        "head",
        "tail",
        "wc",
        "grep",
        "find",
        "fd",
        "rg",
        "pwd",
        "which",
        "type",
        "env",
        "printenv",
        "stat",
        "file",
        "diff",
        "patch",
        "sort",
        "uniq",
        "cut",
        "awk",
        "sed",
        "xargs",
        "tr",
        "curl",
        "wget",
        "jq",
        "yq",
        "zip",
        "unzip",
        "tar",
        "gzip",
        "gunzip",
        // Process inspection
        "ps",
        "top",
        "htop",
        // Create / move
        "mkdir",
        "touch",
        "cp",
        "mv",
    ]
    .into_iter()
    .map(String::from)
    .collect()
}

/// 内置危险模式黑名单默认值（等价于之前硬编码的 SHELL_BLOCKED_DANGEROUS）
pub fn default_blocked_patterns() -> Vec<String> {
    vec![
        // 递归删除根目录
        "rm -rf /",
        "rm -rf /*",
        "rm -rf ~",
        "rm -rf ~/",
        "rm -rf /etc",
        "rm -rf /usr",
        "rm -rf /bin",
        "rm -rf /sbin",
        "rm -rf /lib",
        "rm -rf /var",
        "rm -rf /home",
        "rm -rf /root",
        "rm -rf /boot",
        "rm -rf /proc",
        "rm -rf /sys",
        // 权限核弹
        "chmod -r 777 /",
        "chmod 777 /",
        // 原始磁盘写入
        "> /dev/sda",
        "> /dev/nvme",
        "dd if=",
        "dd of=/dev/",
        // 格式化
        "mkfs",
        "wipefs",
        "shred /dev/",
        // Fork bomb
        ":(){ :|:& };:",
        // find -delete on root
        "find / -delete",
        "find /* -delete",
        // Windows 核弹
        "format c:",
        "format d:",
        "del /f /s /q c:\\",
        "rd /s /q c:\\",
        "rd /s /q d:\\",
    ]
    .into_iter()
    .map(String::from)
    .collect()
}

/// 内置路径黑名单默认值（等价于之前硬编码的 PATH_DENY_IMMUTABLE）
pub fn default_denied_paths() -> Vec<String> {
    vec![
        ".evocli/config.toml",
        ".evocli\\config.toml",
        ".ssh",
        ".gnupg",
        "/etc/passwd",
        "/etc/shadow",
        "/etc/sudoers",
        "\\Windows\\System32",
    ]
    .into_iter()
    .map(String::from)
    .collect()
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MemoryConfig {
    /// Max episodic memories to retain
    #[serde(default = "default_max_episodes")]
    pub max_episodes: usize,
}

// ── Defaults ─────────────────────────────────────────────

fn default_provider() -> String {
    "openai".into()
}
// Global defaults use OpenAI since it's the most widely available provider.
// These are overridden by evocli init based on the selected provider.
// Users should set [llm.tiers] in config.toml for their actual provider.
fn default_fast_model() -> String {
    "gpt-4o-mini".into()
}
fn default_smart_model() -> String {
    "gpt-4o".into()
}
fn default_max_total() -> usize {
    128_000
}
fn default_max_code() -> usize {
    32_000
}
fn default_max_tokens() -> usize {
    4096
}
fn default_temperature() -> f64 {
    0.7
}
fn default_max_retries() -> u32 {
    3
}
fn tier_fast() -> String {
    "fast".into()
}
fn tier_smart() -> String {
    "smart".into()
}
fn default_shell_whitelist() -> Vec<String> {
    vec![
        "cargo *".into(),
        "npm *".into(),
        "git *".into(),
        "python *".into(),
        "rustc *".into(),
        "ls *".into(),
        "cat *".into(),
        "grep *".into(),
        "find *".into(),
    ]
}
fn default_max_episodes() -> usize {
    1000
}

/// Agent behaviour tuning — all values configurable in config.toml [agent]
///
/// Example config.toml:
/// ```toml
/// [agent]
/// max_tool_calls          = 20   # max tool-call iterations per agent.run()
/// max_reflections         = 3    # max lint/test reflection retries
/// stream_timeout_s        = 30   # LLM stream start timeout (seconds)
/// context_build_timeout_s = 20   # context engine timeout (seconds)
/// rpc_timeout_ms          = 90000 # Rust→Python RPC timeout (ms)
/// history_compress_turns  = 10   # compress history after N exchanges
/// history_compress_tokens = 8000 # or when history exceeds this token estimate
/// ```
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentConfig {
    /// Maximum tool-call iterations per agent invocation.
    /// 10 was too low for real "edit → lint → test → commit" flows.
    #[serde(default = "default_max_tool_calls")]
    pub max_tool_calls: usize,

    /// Maximum reflection retries on lint/test failure.
    #[serde(default = "default_max_reflections")]
    pub max_reflections: usize,

    /// Timeout in seconds for the initial LLM streaming connection.
    #[serde(default = "default_stream_timeout_s")]
    pub stream_timeout_s: u64,

    /// Timeout in seconds for the context engine build phase.
    #[serde(default = "default_context_build_timeout_s")]
    pub context_build_timeout_s: u64,

    /// Timeout in milliseconds for Rust→Python RPC calls (default: 90s).
    /// Complex tool calls like `shell.run cargo test` may need > 60s.
    #[serde(default = "default_rpc_timeout_ms")]
    pub rpc_timeout_ms: u64,

    /// How many seconds to wait for the first streaming chunk from the AI before
    /// showing "No response" error in the TUI. Increase if your context building
    /// (RepoMap, memory search) takes longer than 120s on a large project.
    /// config.toml: [agent] first_chunk_timeout_s = 120
    #[serde(default = "default_first_chunk_timeout_s")]
    pub first_chunk_timeout_s: u64,

    /// Compress history after this many message exchanges.
    #[serde(default = "default_history_compress_turns")]
    pub history_compress_turns: usize,

    /// Compress history when token estimate exceeds this value.
    #[serde(default = "default_history_compress_tokens")]
    pub history_compress_tokens: usize,
}

impl Default for AgentConfig {
    fn default() -> Self {
        Self {
            max_tool_calls: default_max_tool_calls(),
            max_reflections: default_max_reflections(),
            stream_timeout_s: default_stream_timeout_s(),
            context_build_timeout_s: default_context_build_timeout_s(),
            rpc_timeout_ms: default_rpc_timeout_ms(),
            first_chunk_timeout_s: default_first_chunk_timeout_s(),
            history_compress_turns: default_history_compress_turns(),
            history_compress_tokens: default_history_compress_tokens(),
        }
    }
}

fn default_max_tool_calls() -> usize {
    20
}
fn default_max_reflections() -> usize {
    3
}
fn default_stream_timeout_s() -> u64 {
    30
}
fn default_context_build_timeout_s() -> u64 {
    20
}
fn default_rpc_timeout_ms() -> u64 {
    90_000
}
fn default_first_chunk_timeout_s() -> u64 {
    120
} // raised from 60s — context building can take time
fn default_history_compress_turns() -> usize {
    10
}
fn default_history_compress_tokens() -> usize {
    8_000
}

fn default_lpa_max_iter() -> usize {
    20
}
fn default_min_community_size() -> usize {
    2
}
fn default_blast_radius_depth() -> usize {
    5
}
fn default_rrf_k() -> f32 {
    60.0
}
fn default_bm25_weight() -> f32 {
    0.4
}
fn default_vector_weight() -> f32 {
    0.6
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GraphConfig {
    /// Max iterations for Label Propagation community detection (default: 20)
    #[serde(default = "default_lpa_max_iter")]
    pub lpa_max_iter: usize,
    /// Merge communities smaller than this into file-based groups (default: 2)
    #[serde(default = "default_min_community_size")]
    pub min_community_size: usize,
    /// Max BFS depth for blast radius analysis (default: 5)
    #[serde(default = "default_blast_radius_depth")]
    pub blast_radius_depth: usize,
    /// RRF K 值（Reciprocal Rank Fusion，default: 60）
    /// 越大排名对最终分数影响越平缓
    #[serde(default = "default_rrf_k")]
    pub rrf_k: f32,
    /// BM25 结果在混合搜索中的权重（default: 0.4）
    #[serde(default = "default_bm25_weight")]
    pub bm25_weight: f32,
    /// 向量搜索结果在混合搜索中的权重（default: 0.6）
    #[serde(default = "default_vector_weight")]
    pub vector_weight: f32,
}

impl Default for GraphConfig {
    fn default() -> Self {
        Self {
            lpa_max_iter: default_lpa_max_iter(),
            min_community_size: default_min_community_size(),
            blast_radius_depth: default_blast_radius_depth(),
            rrf_k: default_rrf_k(),
            bm25_weight: default_bm25_weight(),
            vector_weight: default_vector_weight(),
        }
    }
}

/// TUI 显示配置（config.toml [tui] 节）
///
/// ```toml
/// [tui]
/// # 是否启用鼠标捕获（影响原生文本选择和滚轮行为）
/// #
/// # false（默认）：
/// #   - 终端原生文本选择/复制 完全可用（点击拖拽选择，Ctrl+C 或右键复制）
/// #   - 鼠标滚轮不控制消息列表（改用键盘：PageUp/Down, Home/End）
/// #   - 推荐大多数用户使用此模式
/// #
/// # true：
/// #   - 鼠标滚轮控制消息列表滚动
/// #   - 原生文本选择被屏蔽（改用 Ctrl+Y 复制最后一条 AI 消息）
/// #   - 在 Windows Terminal 中按住 Shift 可绕过捕获做原生选择
/// enable_mouse = false
/// ```
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TuiConfig {
    /// Enable terminal mouse capture. Default: false.
    ///
    /// false = native text selection/copy works; PageUp/Down scrolls messages.
    /// true  = mouse wheel scrolls messages; native selection blocked (use Ctrl+Y to copy).
    #[serde(default)]
    pub enable_mouse: bool,
}

impl Default for TuiConfig {
    fn default() -> Self {
        Self {
            enable_mouse: false,
        }
    }
}

impl Default for Config {
    fn default() -> Self {
        Self {
            llm: LlmConfig::default(),
            context: ContextConfig::default(),
            safety: SafetyConfig::default(),
            security: SecurityConfig::default(),
            memory: MemoryConfig::default(),
            agent: AgentConfig::default(),
            tui: TuiConfig::default(),
            soul_script: None,
            graph: GraphConfig::default(),
        }
    }
}

impl Default for LlmConfig {
    fn default() -> Self {
        Self {
            base_url: None,
            api_key: None,
            tiers: LlmTiers::default(),
            params: LlmGlobalParams::default(),
            tasks: LlmTasksConfig::default(),
            roles: LlmRolesConfig::default(),
        }
    }
}
// LlmTiers derives Default → fast="" smart="" (user must configure)

impl Default for ContextConfig {
    fn default() -> Self {
        Self {
            max_total: default_max_total(),
            max_code: default_max_code(),
        }
    }
}

impl Default for SecurityConfig {
    fn default() -> Self {
        Self {
            allow_all_commands: true,
            // allow_all_paths=true by default: the user controls their own project files.
            // Add specific denied_paths in config.toml if you need to restrict access.
            allow_all_paths: true,
            block_dangerous_always: true,
            allowed_commands: default_allowed_commands(),
            extra_allowed_commands: vec![],
            blocked_patterns: default_blocked_patterns(),
            extra_blocked_patterns: vec![],
            // Default deny list is empty — user opts in to path restrictions.
            // Example protected paths to add manually:
            //   denied_paths = [".evocli/config.toml", ".ssh", ".gnupg"]
            denied_paths: vec![],
            extra_denied_paths: vec![],
        }
    }
}

impl Default for SafetyConfig {
    fn default() -> Self {
        Self {
            auto_approve_writes: false,
            shell_whitelist: default_shell_whitelist(),
            require_diff_preview: false,
        }
    }
}

impl Default for MemoryConfig {
    fn default() -> Self {
        Self {
            max_episodes: default_max_episodes(),
        }
    }
}

impl Config {
    /// Config directory: ~/.evocli/
    pub fn dir() -> Result<PathBuf> {
        let home = dirs::home_dir().context("Cannot determine home directory")?;
        Ok(home.join(".evocli"))
    }

    /// Config file path: ~/.evocli/config.toml
    pub fn path() -> Result<PathBuf> {
        Ok(Self::dir()?.join("config.toml"))
    }

    /// Load config with priority chain:
    ///   1. Project-local: {cwd}/.evocli/config.toml  (project-specific overrides)
    ///   2. Global:        ~/.evocli/config.toml       (user defaults)
    ///   3. Defaults                                   (built-in defaults)
    ///
    /// Project config only needs to specify fields it wants to override.
    /// All other fields fall back to global, then to defaults.
    pub fn load_or_default() -> Result<Self> {
        // Load global config as base
        let global_path = Self::path()?;
        let mut config = if global_path.exists() {
            let content = std::fs::read_to_string(&global_path)
                .with_context(|| format!("Failed to read {}", global_path.display()))?;
            toml::from_str::<Config>(&content)
                .with_context(|| format!("Failed to parse {}", global_path.display()))?
        } else {
            Self::default()
        };

        // Try to load project-local config and merge on top
        if let Ok(cwd) = std::env::current_dir() {
            let project_path = cwd.join(".evocli").join("config.toml");
            if project_path.exists() {
                match std::fs::read_to_string(&project_path).and_then(|s| {
                    toml::from_str::<toml::Value>(&s)
                        .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))
                }) {
                    Ok(project_val) => {
                        // Re-parse global as Value for merging
                        let global_str = toml::to_string(&config).unwrap_or_default();
                        match toml::from_str::<toml::Value>(&global_str) {
                            Ok(mut merged_val) => {
                                merge_toml(&mut merged_val, project_val);
                                // Convert merged value back to Config
                                match toml::to_string(&merged_val)
                                    .map_err(|e| format!("re-serialize: {}", e))
                                    .and_then(|s| {
                                        toml::from_str::<Config>(&s)
                                            .map_err(|e| format!("re-parse: {}", e))
                                    }) {
                                    Ok(merged) => {
                                        config = merged;
                                        tracing::debug!(
                                            "Loaded project config: {}",
                                            project_path.display()
                                        );
                                    }
                                    Err(e) => {
                                        tracing::warn!(
                                            "Project config {} has incompatible field types — ignored. Error: {}",
                                            project_path.display(), e
                                        );
                                    }
                                }
                            }
                            Err(e) => {
                                tracing::warn!(
                                    "Failed to re-serialize global config for merge: {}",
                                    e
                                );
                            }
                        }
                    }
                    Err(e) => {
                        tracing::warn!(
                            "Failed to parse project config {}: {}",
                            project_path.display(),
                            e
                        );
                    }
                }
            }
        }

        // ── Security config migration ─────────────────────────────────────────
        // Old builds defaulted to allow_all_commands = false (whitelist mode).
        // New builds default to allow_all_commands = true (blacklist mode).
        // If the loaded config still has the old false default AND no extra commands
        // were configured (i.e. user never deliberately chose strict mode), migrate.
        if !config.security.allow_all_commands && config.security.extra_allowed_commands.is_empty()
        {
            tracing::info!(
                "[Config] Migrating security.allow_all_commands false→true (blacklist mode). \
                 Add extra_blocked_patterns to config.toml for custom restrictions."
            );
            config.security.allow_all_commands = true;
        }

        // ── Path access migration ─────────────────────────────────────────────
        // Old builds defaulted to allow_all_paths = false with a hardcoded deny list.
        // New builds default to allow_all_paths = true (no path restrictions).
        // If the loaded config has allow_all_paths=false AND the deny_paths list
        // matches the old hardcoded defaults (user never customized it), migrate to
        // allow_all_paths=true so the AI can read project files freely.
        let old_default_denied = default_denied_paths();
        let user_denied_set: std::collections::HashSet<&str> = config
            .security
            .denied_paths
            .iter()
            .map(|s| s.as_str())
            .collect();
        let old_default_set: std::collections::HashSet<&str> =
            old_default_denied.iter().map(|s| s.as_str()).collect();
        let is_default_deny_list =
            user_denied_set == old_default_set || config.security.denied_paths.is_empty();

        if !config.security.allow_all_paths
            && config.security.extra_denied_paths.is_empty()
            && is_default_deny_list
        {
            tracing::info!(
                "[Config] Migrating security.allow_all_paths false→true (no path restrictions). \
                 The AI can now read all project files. Add denied_paths to config.toml \
                 to restrict specific paths."
            );
            config.security.allow_all_paths = true;
            config.security.denied_paths = vec![];
        }

        Ok(config)
    }

    /// Save config to disk (always saves to global config)
    pub fn save(&self) -> Result<()> {
        let path = Self::path()?;
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let content = toml::to_string_pretty(self).context("Failed to serialize config")?;
        std::fs::write(&path, content)
            .with_context(|| format!("Failed to write {}", path.display()))?;
        tracing::info!("Config saved to {}", path.display());
        Ok(())
    }
}

// NOTE: memory_db_path is deprecated after H1 migration to Python LanceDB.
// Python now manages all memory via ~/.evocli/data/memories.jsonl + LanceDB.
// Retained for reference only.
#[allow(dead_code)]
pub fn memory_db_path() -> std::path::PathBuf {
    dirs::home_dir()
        .unwrap_or_default()
        .join(".evocli")
        .join("memory.db")
}

/// Recursively merge `src` TOML value into `dst`.
/// Fields in `src` override fields in `dst`; missing fields are kept from `dst`.
fn merge_toml(dst: &mut toml::Value, src: toml::Value) {
    match (dst, src) {
        (toml::Value::Table(dst_table), toml::Value::Table(src_table)) => {
            for (key, src_val) in src_table {
                let dst_entry = dst_table
                    .entry(key)
                    .or_insert(toml::Value::Table(toml::map::Map::new()));
                merge_toml(dst_entry, src_val);
            }
        }
        (dst_val, src_val) => {
            // Scalar or array: project value overwrites global
            *dst_val = src_val;
        }
    }
}

/// Python Soul 脚本路径解析（4 级优先级）
///
/// 1. `EVOCLI_SOUL` 环境变量（绝对路径或模块名）
/// 2. `~/.evocli/config.toml` 中保存的 `soul_script`（evocli init 时写入）
/// 3. 相对于 CWD（开发时从项目根运行）
/// 4. 从可执行文件向上查找项目根（从 target/debug/ 运行时）
/// 5. Python 模块模式 fallback：`evocli_soul.main`（pip install 后）
pub fn resolve_soul_path() -> String {
    // 优先级 1：环境变量
    if let Ok(p) = std::env::var("EVOCLI_SOUL") {
        if !p.is_empty() {
            return p;
        }
    }

    // 优先级 2：config.toml soul_script 字段
    if let Ok(cfg) = Config::load_or_default() {
        if let Some(ref p) = cfg.soul_script {
            if !p.is_empty() && (p.contains('.') || std::path::Path::new(p).exists()) {
                return p.clone();
            }
        }
    }

    // 优先级 3：相对于 CWD（开发时 `cargo run` 在项目根）
    let rel = "evocli-soul/evocli_soul/main.py";
    if std::path::Path::new(rel).exists() {
        // 不使用 canonicalize()：Windows 上会产生 \\?\ 前缀，导致 Python 路径解析异常
        // 返回原始相对路径，soul_bridge 会把它转为模块模式并设置 PYTHONPATH
        return rel.to_string();
    }

    // 优先级 4：从可执行文件向上查找（处理 target/debug/ 场景）
    if let Ok(exe) = std::env::current_exe() {
        let mut dir = exe
            .parent()
            .unwrap_or_else(|| std::path::Path::new("."))
            .to_path_buf();
        for _ in 0..6 {
            let candidate = dir.join("evocli-soul").join("evocli_soul").join("main.py");
            if candidate.exists() {
                return candidate.to_string_lossy().to_string();
            }
            match dir.parent() {
                Some(p) => dir = p.to_path_buf(),
                None => break,
            }
        }
    }

    // 优先级 5：Python 模块模式（pip install evocli-soul 后）
    "evocli_soul.main".to_string()
}

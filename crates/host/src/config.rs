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
    /// Python Soul 脚本路径（evocli init 时自动检测并保存；优先级低于 EVOCLI_SOUL 环境变量）
    #[serde(default)]
    pub soul_script: Option<String>,
    #[serde(default)]
    pub graph: GraphConfig,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LlmConfig {
    /// Provider: "anthropic" | "openai" | "deepseek" | "ollama"
    #[serde(default = "default_provider")]
    pub provider: String,
    #[serde(default)]
    pub tiers: LlmTiers,
    /// Optional API key (plain text fallback — prefer keyring)
    pub api_key: Option<String>,
    /// Ollama base URL
    pub base_url: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LlmTiers {
    /// Fast tier model (e.g. claude-3-5-haiku-latest, gpt-4o-mini)
    #[serde(default = "default_fast_model")]
    pub fast: String,
    /// Smart tier model (e.g. claude-sonnet-4-5-20250514, gpt-4o)
    #[serde(default = "default_smart_model")]
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
}

/// 安全策略配置（config.toml [security] 节）
///
/// 示例配置（~/.evocli/config.toml）:
/// ```toml
/// [security]
/// allow_all_commands = true          # 跳过命令白名单检查（仍阻断危险模式）
/// allow_all_paths = true             # 跳过路径访问限制
/// block_dangerous_always = true      # 即使 allow_all 也阻断 rm -rf / 等危险操作
/// extra_allowed_commands = ["docker", "kubectl", "terraform"]
/// extra_blocked_patterns = ["curl * | bash", "wget * -O- | sh"]
/// ```
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SecurityConfig {
    /// 跳过命令白名单（允许任意命令）。默认 false。
    /// 开启后仍受 block_dangerous_always 约束。
    #[serde(default)]
    pub allow_all_commands: bool,

    /// 跳过路径访问限制（允许访问任意路径）。默认 false。
    #[serde(default)]
    pub allow_all_paths: bool,

    /// 即使 allow_all_commands=true，也永远阻断已知危险模式（rm -rf / 等）。
    /// 强烈建议保持 true。默认 true。
    #[serde(default = "default_true")]
    pub block_dangerous_always: bool,

    /// 追加到命令白名单的额外命令（不影响内置白名单）。
    /// 例如：["docker", "kubectl", "terraform", "ansible"]
    #[serde(default)]
    pub extra_allowed_commands: Vec<String>,

    /// 追加到危险模式黑名单的额外模式（正则子串匹配）。
    #[serde(default)]
    pub extra_blocked_patterns: Vec<String>,

    /// 追加到路径访问黑名单的路径前缀。
    #[serde(default)]
    pub extra_denied_paths: Vec<String>,
}

fn default_true() -> bool { true }


#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MemoryConfig {
    /// Max episodic memories to retain
    #[serde(default = "default_max_episodes")]
    pub max_episodes: usize,
}

// ── Defaults ─────────────────────────────────────────────

fn default_provider() -> String { "anthropic".into() }
fn default_fast_model() -> String { "claude-3-5-haiku-latest".into() }
fn default_smart_model() -> String { "claude-sonnet-4-5-20250514".into() }
fn default_max_total() -> usize { 128_000 }
fn default_max_code() -> usize { 32_000 }
fn default_shell_whitelist() -> Vec<String> {
    vec![
        "cargo *".into(), "npm *".into(), "git *".into(),
        "python *".into(), "rustc *".into(), "ls *".into(),
        "cat *".into(), "grep *".into(), "find *".into(),
    ]
}
fn default_max_episodes() -> usize { 1000 }

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
            max_tool_calls:          default_max_tool_calls(),
            max_reflections:         default_max_reflections(),
            stream_timeout_s:        default_stream_timeout_s(),
            context_build_timeout_s: default_context_build_timeout_s(),
            rpc_timeout_ms:          default_rpc_timeout_ms(),
            history_compress_turns:  default_history_compress_turns(),
            history_compress_tokens: default_history_compress_tokens(),
        }
    }
}

fn default_max_tool_calls()          -> usize { 20 }
fn default_max_reflections()         -> usize { 3 }
fn default_stream_timeout_s()        -> u64   { 30 }
fn default_context_build_timeout_s() -> u64   { 20 }
fn default_rpc_timeout_ms()          -> u64   { 90_000 }
fn default_history_compress_turns()  -> usize { 10 }
fn default_history_compress_tokens() -> usize { 8_000 }

fn default_lpa_max_iter()         -> usize { 20 }
fn default_min_community_size()   -> usize { 2 }
fn default_blast_radius_depth()   -> usize { 5 }
fn default_rrf_k()                -> f32   { 60.0 }
fn default_bm25_weight()          -> f32   { 0.4 }
fn default_vector_weight()        -> f32   { 0.6 }

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
            lpa_max_iter:       default_lpa_max_iter(),
            min_community_size: default_min_community_size(),
            blast_radius_depth: default_blast_radius_depth(),
            rrf_k:              default_rrf_k(),
            bm25_weight:        default_bm25_weight(),
            vector_weight:      default_vector_weight(),
        }
    }
}

impl Default for Config {
    fn default() -> Self {
        Self {
            llm:         LlmConfig::default(),
            context:     ContextConfig::default(),
            safety:      SafetyConfig::default(),
            security:    SecurityConfig::default(),
            memory:      MemoryConfig::default(),
            agent:       AgentConfig::default(),
            soul_script: None,
            graph:       GraphConfig::default(),
        }
    }
}

impl Default for LlmConfig {
    fn default() -> Self {
        Self {
            provider: default_provider(),
            tiers: LlmTiers::default(),
            api_key: None,
            base_url: None,
        }
    }
}

impl Default for LlmTiers {
    fn default() -> Self {
        Self {
            fast: default_fast_model(),
            smart: default_smart_model(),
        }
    }
}

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
            // Blacklist mode by default: allow all commands, block only known-dangerous.
            // Developer tools need to run many different commands; a whitelist requires
            // constant maintenance and blocks legitimate operations (pwd, mkdir, sed, …).
            // Users who want strict whitelist control can set allow_all_commands = false.
            allow_all_commands:     true,
            allow_all_paths:        false,
            block_dangerous_always: true,
            extra_allowed_commands: vec![],
            extra_blocked_patterns: vec![],
            extra_denied_paths:     vec![],
        }
    }
}

impl Default for SafetyConfig {
    fn default() -> Self {
        Self {
            auto_approve_writes: false,
            shell_whitelist: default_shell_whitelist(),
        }
    }
}

impl Default for MemoryConfig {
    fn default() -> Self {
        Self { max_episodes: default_max_episodes() }
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
                match std::fs::read_to_string(&project_path)
                    .and_then(|s| toml::from_str::<toml::Value>(&s).map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e)))
                {
                    Ok(project_val) => {
                        // Re-parse global as Value for merging
                        let global_str = toml::to_string(&config).unwrap_or_default();
                        match toml::from_str::<toml::Value>(&global_str) {
                            Ok(mut merged_val) => {
                                merge_toml(&mut merged_val, project_val);
                                // Convert merged value back to Config
                                match toml::to_string(&merged_val)
                                    .map_err(|e| format!("re-serialize: {}", e))
                                    .and_then(|s| toml::from_str::<Config>(&s).map_err(|e| format!("re-parse: {}", e)))
                                {
                                    Ok(merged) => {
                                        config = merged;
                                        tracing::debug!("Loaded project config: {}", project_path.display());
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
                                tracing::warn!("Failed to re-serialize global config for merge: {}", e);
                            }
                        }
                    }
                    Err(e) => {
                        tracing::warn!("Failed to parse project config {}: {}", project_path.display(), e);
                    }
                }
            }
        }

        // ── Security config migration ─────────────────────────────────────────
        // Old builds defaulted to allow_all_commands = false (whitelist mode).
        // New builds default to allow_all_commands = true (blacklist mode).
        // If the loaded config still has the old false default AND no extra commands
        // were configured (i.e. user never deliberately chose strict mode), migrate.
        if !config.security.allow_all_commands
            && config.security.extra_allowed_commands.is_empty()
        {
            tracing::info!(
                "[Config] Migrating security.allow_all_commands false→true (blacklist mode). \
                 Add extra_blocked_patterns to config.toml for custom restrictions."
            );
            config.security.allow_all_commands = true;
        }

        Ok(config)
    }

    /// Save config to disk (always saves to global config)
    pub fn save(&self) -> Result<()> {
        let path = Self::path()?;
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let content = toml::to_string_pretty(self)
            .context("Failed to serialize config")?;
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
                let dst_entry = dst_table.entry(key).or_insert(toml::Value::Table(toml::map::Map::new()));
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
        let mut dir = exe.parent()
            .unwrap_or_else(|| std::path::Path::new("."))
            .to_path_buf();
        for _ in 0..6 {
            let candidate = dir
                .join("evocli-soul")
                .join("evocli_soul")
                .join("main.py");
            if candidate.exists() {
                return candidate.to_string_lossy().to_string();
            }
            match dir.parent() {
                Some(p) => dir = p.to_path_buf(),
                None    => break,
            }
        }
    }

    // 优先级 5：Python 模块模式（pip install evocli-soul 后）
    "evocli_soul.main".to_string()
}

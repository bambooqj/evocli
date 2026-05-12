//! security.rs — SecurityController
//!
//! 安全模型（两层），**全部由 config.toml 驱动，无任何硬编码列表**：
//!
//! ① 命令执行（shell.run）
//!    - 黑名单模式（allow_all_commands = true，默认）：允许一切，阻断 blocked_patterns
//!    - 严格模式（allow_all_commands = false）：仅允许 allowed_commands + extra_allowed_commands
//!    - block_dangerous_always=true：即使 allow_all 也阻断 blocked_patterns
//!
//! ② 文件路径访问
//!    - denied_paths + extra_denied_paths：路径黑名单
//!    - .evocli/config.toml 在代码层面兜底保护（AI 无法修改自身安全策略）
//!
//! config.toml 示例：
//! ```toml
//! [security]
//! allow_all_commands    = true               # 黑名单模式（默认）
//! block_dangerous_always = true              # 永远阻断高危操作
//! allowed_commands      = ["cargo", "git"]   # 严格模式下的白名单
//! extra_allowed_commands = ["docker"]        # 追加允许
//! blocked_patterns      = ["rm -rf /", ...]  # 危险模式列表
//! extra_blocked_patterns = ["curl|bash"]     # 追加危险模式
//! denied_paths          = [".ssh", ...]      # 路径黑名单
//! extra_denied_paths    = ["/prod"]          # 追加禁止路径
//! ```

use anyhow::{bail, Result};
use std::path::Path;
use crate::config::SecurityConfig;

pub struct SecurityController {
    cfg: SecurityConfig,
    /// Compiled regex patterns from cfg.blocked_patterns (cached at construction)
    blocked_regex: Vec<(regex::Regex, String)>,
}

// ── 代码级兜底保护（不可绕过，不在此列表的路径由 config 控制）─────────────────
// 只保留最小集：防止 AI 修改自身安全配置（如果 config.toml 不包含则始终保护）
const CONFIG_SELF_PROTECT: &[&str] = &[
    ".evocli/config.toml",
    ".evocli\\config.toml",
];

impl SecurityController {
    pub fn new(cfg: &SecurityConfig) -> Self {
        // Pre-compile regex patterns for performance (avoid re-compiling per call)
        // Blocked patterns that look like regex (contain \, ^, *, etc.) are compiled;
        // plain substring patterns are matched directly in validate_shell_cmd.
        let blocked_regex: Vec<(regex::Regex, String)> = cfg.blocked_patterns.iter()
            .chain(cfg.extra_blocked_patterns.iter())
            .filter(|p| p.contains('\\') || p.contains('^') || p.contains('('))
            .filter_map(|p| {
                regex::Regex::new(p).ok().map(|re| (re, p.clone()))
            })
            .collect();

        let controller = Self { cfg: cfg.clone(), blocked_regex };
        if cfg.allow_all_commands {
            tracing::info!(
                "[Security] blacklist mode — {} patterns, {} path rules",
                cfg.blocked_patterns.len() + cfg.extra_blocked_patterns.len(),
                cfg.denied_paths.len() + cfg.extra_denied_paths.len(),
            );
        } else {
            tracing::info!(
                "[Security] strict mode — {} allowed commands",
                cfg.allowed_commands.len() + cfg.extra_allowed_commands.len()
            );
        }
        controller
    }

    pub fn default_config() -> Self {
        Self::new(&SecurityConfig::default())
    }

    pub fn validate_shell_cmd(&self, cmd: &str) -> Result<()> {
        let normalized = cmd.to_lowercase()
            .split_whitespace()
            .collect::<Vec<_>>()
            .join(" ");

        // 1. Dangerous patterns — always checked when block_dangerous_always=true
        if self.cfg.block_dangerous_always {
            // Substring patterns (fast path)
            for pattern in self.cfg.blocked_patterns.iter()
                .chain(self.cfg.extra_blocked_patterns.iter())
                .filter(|p| !p.contains('\\') && !p.contains('^') && !p.contains('('))
            {
                if normalized.contains(pattern.to_lowercase().as_str()) {
                    self.audit_log("shell.validate", cmd, false);
                    bail!("[E401] Blocked: dangerous pattern '{}' detected", pattern);
                }
            }

            // Regex patterns (pre-compiled)
            for (re, reason) in &self.blocked_regex {
                if re.is_match(&normalized) {
                    self.audit_log("shell.validate", cmd, false);
                    bail!("[E401] Blocked: dangerous pattern '{}' matched", reason);
                }
            }
        }

        // 2. Blacklist mode (default): pass if not dangerous
        if self.cfg.allow_all_commands {
            self.audit_log("shell.validate", cmd, true);
            return Ok(());
        }

        // 3. Strict whitelist mode: only allowed_commands + extra_allowed_commands
        let first_token = cmd.trim().split_whitespace().next().unwrap_or("");
        let binary = Path::new(first_token)
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or(first_token);

        let allowed = self.cfg.allowed_commands.iter()
            .chain(self.cfg.extra_allowed_commands.iter())
            .any(|a| binary == a.as_str());

        if !allowed {
            self.audit_log("shell.validate", cmd, false);
            bail!(
                "[E401] Strict mode: '{}' not in allowed_commands.\n\
                 Edit ~/.evocli/config.toml [security]:\n\
                 \x20 extra_allowed_commands = [\"{}\", ...]",
                binary, binary,
            );
        }

        self.audit_log("shell.validate", cmd, true);
        Ok(())
    }

    pub fn validate_path_access(&self, path: &Path) -> Result<()> {
        let path_str = path.to_string_lossy().to_lowercase();

        // 1. Code-level self-protect: config.toml itself is ALWAYS off-limits.
        // This is a minimal code-level guard (not in config) to prevent the AI
        // from modifying its own security rules via any config manipulation.
        for protected in CONFIG_SELF_PROTECT {
            if path_str.contains(&protected.to_lowercase()) {
                self.audit_log("path.validate", &path.display().to_string(), false);
                bail!(
                    "[E202] '{}' is permanently protected (AI cannot modify security config).",
                    path.display()
                );
            }
        }

        // 2. allow_all_paths skips the remaining checks
        if self.cfg.allow_all_paths {
            return Ok(());
        }

        // 3. Config-driven denied paths (denied_paths + extra_denied_paths)
        for denied in self.cfg.denied_paths.iter().chain(self.cfg.extra_denied_paths.iter()) {
            if path_str.contains(denied.to_lowercase().as_str()) {
                self.audit_log("path.validate", &path.display().to_string(), false);
                bail!(
                    "[E202] '{}' denied by path rule '{}'.\n\
                     Edit ~/.evocli/config.toml [security] denied_paths to customize.",
                    path.display(), denied
                );
            }
        }

        Ok(())
    }

    #[allow(dead_code)]
    pub fn requires_approval(&self, tool: &str) -> bool {
        matches!(tool, "git.commit" | "fs.write" | "fs.apply_diff" | "shell.run")
    }

    pub fn audit_log(&self, operation: &str, detail: &str, allowed: bool) {
        let timestamp = chrono::Utc::now().to_rfc3339();
        let entry = format!("[{}] {} detail={:?} allowed={}\n",
                            timestamp, operation, detail, allowed);
        let audit_path = dirs::home_dir()
            .unwrap_or_default()
            .join(".evocli")
            .join("audit.log");
        use std::fs::OpenOptions;
        use std::io::Write;
        if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(&audit_path) {
            let _ = f.write_all(entry.as_bytes());
        }
    }
}

impl Default for SecurityController {
    fn default() -> Self { Self::default_config() }
}

//! security.rs — SecurityController
//!
//! 纯 config 驱动的安全策略执行器。代码本身不做任何主观判断：
//! 所有白名单、黑名单、路径规则全部来自 config.toml [security]。
//! 默认值在 config.rs 的 default_* 函数中定义，用户可以完整替换。
//!
//! config.toml 示例：
//! ```toml
//! [security]
//! allow_all_commands    = true               # 黑名单模式（默认）
//! block_dangerous_always = true              # 是否检查 blocked_patterns
//! allowed_commands      = ["cargo", "git"]   # 严格模式白名单（完整替换）
//! extra_allowed_commands = ["docker"]        # 追加到 allowed_commands
//! blocked_patterns      = ["rm -rf /", ...]  # 危险模式（完整替换）
//! extra_blocked_patterns = ["curl|bash"]     # 追加到 blocked_patterns
//! denied_paths          = [".ssh", ...]      # 路径黑名单（完整替换）
//! extra_denied_paths    = ["/prod"]          # 追加到 denied_paths
//! ```

use crate::config::SecurityConfig;
use anyhow::{bail, Result};
use std::path::Path;

pub struct SecurityController {
    cfg: SecurityConfig,
    /// Pre-compiled regex patterns for performance
    blocked_regex: Vec<(regex::Regex, String)>,
}

impl SecurityController {
    pub fn new(cfg: &SecurityConfig) -> Self {
        let blocked_regex: Vec<(regex::Regex, String)> = cfg
            .blocked_patterns
            .iter()
            .chain(cfg.extra_blocked_patterns.iter())
            .filter(|p| p.contains('\\') || p.contains('^') || p.contains('('))
            .filter_map(|p| regex::Regex::new(p).ok().map(|re| (re, p.clone())))
            .collect();

        let controller = Self {
            cfg: cfg.clone(),
            blocked_regex,
        };
        tracing::info!(
            "[Security] mode={} patterns={} path_rules={} allowed_cmds={}",
            if cfg.allow_all_commands {
                "blacklist"
            } else {
                "strict-whitelist"
            },
            cfg.blocked_patterns.len() + cfg.extra_blocked_patterns.len(),
            cfg.denied_paths.len() + cfg.extra_denied_paths.len(),
            cfg.allowed_commands.len() + cfg.extra_allowed_commands.len(),
        );
        controller
    }

    pub fn default_config() -> Self {
        Self::new(&SecurityConfig::default())
    }

    pub fn validate_shell_cmd(&self, cmd: &str) -> Result<()> {
        let normalized = cmd
            .to_lowercase()
            .split_whitespace()
            .collect::<Vec<_>>()
            .join(" ");

        // Dangerous pattern check (only when block_dangerous_always=true)
        if self.cfg.block_dangerous_always {
            for pattern in self
                .cfg
                .blocked_patterns
                .iter()
                .chain(self.cfg.extra_blocked_patterns.iter())
                .filter(|p| !p.contains('\\') && !p.contains('^') && !p.contains('('))
            {
                if normalized.contains(pattern.to_lowercase().as_str()) {
                    self.audit_log("shell.validate", cmd, false);
                    bail!("[E401] Blocked: pattern '{}' matched", pattern);
                }
            }
            for (re, reason) in &self.blocked_regex {
                if re.is_match(&normalized) {
                    self.audit_log("shell.validate", cmd, false);
                    bail!("[E401] Blocked: pattern '{}' matched", reason);
                }
            }
        }

        // Blacklist mode (default): allow all if not dangerous
        if self.cfg.allow_all_commands {
            self.audit_log("shell.validate", cmd, true);
            return Ok(());
        }

        // Strict whitelist mode
        let first_token = cmd.trim().split_whitespace().next().unwrap_or("");
        let binary = Path::new(first_token)
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or(first_token);

        let allowed = self
            .cfg
            .allowed_commands
            .iter()
            .chain(self.cfg.extra_allowed_commands.iter())
            .any(|a| binary == a.as_str());

        if !allowed {
            self.audit_log("shell.validate", cmd, false);
            bail!(
                "[E401] Strict mode: '{}' not in allowed_commands.\n\
                 Edit ~/.evocli/config.toml [security] extra_allowed_commands = [\"{}\", ...]",
                binary,
                binary,
            );
        }

        self.audit_log("shell.validate", cmd, true);
        Ok(())
    }

    pub fn validate_path_access(&self, path: &Path) -> Result<()> {
        // allow_all_paths: skip all checks
        if self.cfg.allow_all_paths {
            return Ok(());
        }

        // ── Symlink resolution (CWE-22 mitigation) ───────────────────────────
        // Simple string matching on the raw path is vulnerable to symlink attacks:
        //   ln -s ~/.ssh ./evil_link  →  fs.read("evil_link/id_rsa") bypasses check
        // We canonicalize to resolve symlinks before checking denied patterns.
        let resolved: std::path::PathBuf = if path.exists() {
            std::fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf())
        } else {
            // New file: canonicalize parent directory, then re-append filename
            match path.parent() {
                Some(parent) if !parent.as_os_str().is_empty() => {
                    let canon_parent = std::fs::canonicalize(parent)
                        .unwrap_or_else(|_| parent.to_path_buf());
                    canon_parent.join(path.file_name().unwrap_or_default())
                }
                _ => {
                    // Relative path with no parent — join with CWD
                    std::env::current_dir()
                        .unwrap_or_default()
                        .join(path)
                }
            }
        };

        let path_str = resolved.to_string_lossy().to_lowercase();

        // Config-driven denied paths — checked against the resolved path
        for denied in self
            .cfg
            .denied_paths
            .iter()
            .chain(self.cfg.extra_denied_paths.iter())
        {
            if path_str.contains(denied.to_lowercase().as_str()) {
                self.audit_log("path.validate", &resolved.display().to_string(), false);
                bail!(
                    "[E202] '{}' denied by path rule '{}'.\n\
                     Edit ~/.evocli/config.toml [security] denied_paths to change.",
                    resolved.display(),
                    denied
                );
            }
        }

        Ok(())
    }

    #[allow(dead_code)]
    pub fn requires_approval(&self, tool: &str) -> bool {
        matches!(
            tool,
            "git.commit" | "fs.write" | "fs.apply_diff" | "shell.run"
        )
    }

    pub fn audit_log(&self, operation: &str, detail: &str, allowed: bool) {
        let timestamp = chrono::Utc::now().to_rfc3339();
        let entry = format!(
            "[{}] {} detail={:?} allowed={}\n",
            timestamp, operation, detail, allowed
        );
        let audit_path = dirs::home_dir()
            .unwrap_or_default()
            .join(".evocli")
            .join("audit.log");
        use std::fs::OpenOptions;
        use std::io::Write;
        if let Ok(mut f) = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&audit_path)
        {
            let _ = f.write_all(entry.as_bytes());
        }
    }
}

impl Default for SecurityController {
    fn default() -> Self {
        Self::default_config()
    }
}

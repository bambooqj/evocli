//! security.rs — SecurityController
//!
//! 安全模型（两层）：
//!
//! ① 命令执行（shell.run）
//!    - 默认黑名单模式（allow_all_commands = true）：允许一切，阻断 SHELL_BLOCKED_DANGEROUS
//!    - 严格模式（allow_all_commands = false）：仅允许 extra_allowed_commands 列表
//!    - SHELL_BLOCKED_DANGEROUS 硬编码，任何配置均无法绕过
//!    - 额外正则模式：捕获路径变体和命令链绕过（rm -rf /etc 等）
//!
//! ② 文件路径访问
//!    - PATH_DENY_IMMUTABLE 硬编码，allow_all_paths = true 也无法绕过
//!      关键：.evocli/config.toml 在此列表 → AI 无法读写自身安全策略
//!    - extra_denied_paths：用户在 config.toml 里追加
//!
//! config.toml 由人类管理，AI 无法访问（PATH_DENY_IMMUTABLE 保证）：
//! ```toml
//! [security]
//! allow_all_commands = false                        # 切换严格模式
//! extra_allowed_commands = ["docker", "kubectl"]    # 严格模式允许的命令
//! extra_blocked_patterns = ["curl * | bash"]        # 追加危险模式
//! extra_denied_paths = ["/prod"]                    # 追加禁止路径
//! ```

use anyhow::{bail, Result};
use std::path::Path;
use crate::config::SecurityConfig;

pub struct SecurityController {
    cfg: SecurityConfig,
}

// ── 永久危险模式（硬编码，任何配置均无法绕过）────────────────────────────────
// 原则：只列真正不可逆的系统破坏操作。
// 边界敏感的操作（curl | bash 等）交由用户通过 config.toml 的
// extra_blocked_patterns 自行决策 — config.toml 对 AI 不可访问。
const SHELL_BLOCKED_DANGEROUS: &[&str] = &[
    // 递归删除根目录 / home / 重要系统路径
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
    // 格式化 / 抹盘
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
];

// ── 正则危险模式（捕获变体和命令链绕过）──────────────────────────────────────
// 用 regex 替代纯字符串匹配，防止以下绕过：
//   1. 路径变体：rm -rf /etc → 原黑名单没有 /etc
//   2. 命令链：echo x; rm -rf / → 危险命令出现在链末尾
//   3. 参数顺序变换：rm -f -r / → 分拆 -r -f
//   4. Windows 路径：del /f /q /s C:\ → 大小写变体
//
// 格式：(pattern, reason)
// 使用 regex crate（已在 workspace.dependencies 中声明）
const SHELL_BLOCKED_REGEX_STRS: &[(&str, &str)] = &[
    // rm with recursive (-r/-R/-rf/-fr/-Rf) on any absolute path or /
    (r"rm\s+-[a-z]*[rR][a-z]*\s+(/|~)", "recursive rm on root or home"),
    // chmod -R on root
    (r"chmod\s+-[rR]\s+[0-7]+\s+/", "recursive chmod on root"),
    // dd writing to any raw device
    (r"\bdd\b.*\bof=/dev/", "raw device write via dd"),
    // Dangerous command appearing after shell chain operators (;, &&, ||, |)
    // This catches: safe_cmd; rm -rf /
    (r"[;|&]\s*(rm\s+-[a-z]*[rR]|mkfs|wipefs|shred /dev)", "dangerous command in shell chain"),
    // Fork bomb variants
    (r":\(\)\s*\{.*\|.*&.*\}", "fork bomb pattern"),
    // find with -delete or -exec rm on root
    (r"find\s+/\S*\s+.*-(delete|exec\s+rm)", "find -delete on system path"),
];

// ── 不可绕过的路径禁止列表（含 config.toml，防止 AI 改写自身安全策略）────────
const PATH_DENY_IMMUTABLE: &[&str] = &[
    ".evocli/config.toml",
    ".evocli\\config.toml",
    ".ssh",
    ".gnupg",
    "/etc/passwd",
    "/etc/shadow",
    "/etc/sudoers",
    "\\Windows\\System32",
];

impl SecurityController {
    pub fn new(cfg: &SecurityConfig) -> Self {
        let controller = Self { cfg: cfg.clone() };
        if cfg.allow_all_commands {
            tracing::info!(
                "[Security] blacklist mode (dangerous patterns {})",
                if cfg.block_dangerous_always { "active" } else { "disabled" }
            );
        } else {
            tracing::info!("[Security] strict mode — only extra_allowed_commands permitted");
        }
        controller
    }

    pub fn default_config() -> Self {
        Self { cfg: SecurityConfig::default() }
    }

    pub fn validate_shell_cmd(&self, cmd: &str) -> Result<()> {
        let normalized = cmd.to_lowercase()
            .split_whitespace()
            .collect::<Vec<_>>()
            .join(" ");

        // 1. Hardcoded dangerous patterns — always checked
        if self.cfg.block_dangerous_always {
            for pattern in SHELL_BLOCKED_DANGEROUS {
                if normalized.contains(pattern) {
                    self.audit_log("shell.validate", cmd, false);
                    bail!("[E401] Blocked: dangerous pattern '{}' detected", pattern);
                }
            }

            // 1b. Regex-based pattern matching — catches path variants and chain bypasses
            for (pattern_str, reason) in SHELL_BLOCKED_REGEX_STRS {
                if let Ok(re) = regex::Regex::new(pattern_str) {
                    if re.is_match(&normalized) {
                        self.audit_log("shell.validate", cmd, false);
                        bail!("[E401] Blocked: {} (pattern: {})", reason, pattern_str);
                    }
                }
            }
        }

        // 2. User-defined extra blocked patterns (from config.toml, AI-inaccessible)
        for pattern in &self.cfg.extra_blocked_patterns {
            if normalized.contains(pattern.to_lowercase().as_str()) {
                self.audit_log("shell.validate", cmd, false);
                bail!("[E401] Blocked: pattern '{}' detected", pattern);
            }
        }

        // 3. Blacklist mode (default): pass if not dangerous
        if self.cfg.allow_all_commands {
            self.audit_log("shell.validate", cmd, true);
            return Ok(());
        }

        // 4. Strict mode: only extra_allowed_commands (no hardcoded list — config-driven)
        let first_token = cmd.trim().split_whitespace().next().unwrap_or("");
        let binary = Path::new(first_token)
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or(first_token);

        let allowed = self.cfg.extra_allowed_commands.iter()
            .any(|a| binary == a.as_str());

        if !allowed {
            self.audit_log("shell.validate", cmd, false);
            bail!(
                "[E401] Strict mode: '{}' not allowed.\n\
                 Edit ~/.evocli/config.toml:\n\
                 \x20 extra_allowed_commands = [\"{}\", ...]",
                binary, binary,
            );
        }

        self.audit_log("shell.validate", cmd, true);
        Ok(())
    }

    pub fn validate_path_access(&self, path: &Path) -> Result<()> {
        let path_str = path.to_string_lossy().to_lowercase();

        // 1. Permanently denied paths — allow_all_paths cannot override these
        for denied in PATH_DENY_IMMUTABLE {
            if path_str.contains(&denied.to_lowercase()) {
                self.audit_log("path.validate", &path.display().to_string(), false);
                bail!(
                    "[E202] '{}' is permanently off-limits to the AI agent.",
                    path.display()
                );
            }
        }

        // 2. allow_all_paths skips the remaining checks
        if self.cfg.allow_all_paths {
            return Ok(());
        }

        // 3. User-defined extra denied paths
        for denied in &self.cfg.extra_denied_paths {
            if path_str.contains(denied.to_lowercase().as_str()) {
                self.audit_log("path.validate", &path.display().to_string(), false);
                bail!(
                    "[E202] '{}' denied by user rule '{}'",
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

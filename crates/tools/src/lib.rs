//! EvoCLI Tools — safe command execution with allowlist enforcement (Section 22)
//!
//! ## Shell Layer Design (Section 22)
//!
//! **Current implementation** (v2.x MVP):
// Suppress warnings for planned but not-yet-enabled features (brush shell, wasm sandbox).
// These are compile-time feature flags for future opt-in, not bugs.
#![allow(unexpected_cfgs)]
//! Uses `std::process::Command` with a compile-time command allowlist and
//! dangerous pattern blocklist. All commands are validated before execution.
//! This provides cross-platform safety without external dependencies.
//!
//! **Planned for v3.x**:
//! Full integration with [Brush](https://github.com/reubeno/brush) (Rust-native
//! bash-compatible shell) + [uutils/coreutils](https://github.com/uutils/coreutils)
//! for a complete cross-platform POSIX shell experience independent of the host OS.
//! See Section 22 of EvoCLI-可执行方案.md for the full design.
//!
//! **Why the current approach works for v2.x**:
//! - All AI tool calls go through the allowlist (`cargo`, `npm`, `python`, `git`, etc.)
//! - Dangerous patterns are blocked at compile time
//! - Works identically on Windows, macOS, and Linux for supported commands
//! - Zero external dependencies

use anyhow::{bail, Result};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::OnceLock;
use std::time::Duration;

// ── Public types ─────────────────────────────────────────────────

/// Result of a command execution.
#[derive(Debug, Clone)]
pub struct CommandOutput {
    pub exit_code: i32,
    pub stdout: String,
    pub stderr: String,
}

// ── Config-driven security lists ─────────────────────────────────
//
// All whitelist/blacklist values come from config.toml [security].
// No hardcoded lists — the defaults live in config.rs::default_allowed_commands()
// and default_blocked_patterns() and are written to config.toml on first init.
//
// tools::init_security() is called once at startup by the host crate
// after loading the full SecurityConfig.

static ALLOWED: OnceLock<Vec<String>>  = OnceLock::new();
static BLOCKED: OnceLock<Vec<String>>  = OnceLock::new();

/// Initialize security lists from config.
/// Called once at startup by the host crate (main.rs or tool_dispatch.rs).
/// If not called, falls back to reading from config.toml directly (legacy path).
pub fn init_security(allowed: Vec<String>, blocked: Vec<String>) {
    let _ = ALLOWED.set(allowed);
    let _ = BLOCKED.set(blocked);
}

fn get_allowed() -> &'static [String] {
    ALLOWED.get_or_init(|| load_from_config_file()).as_slice()
}

fn get_blocked() -> &'static [String] {
    BLOCKED.get_or_init(|| load_blocked_from_config_file()).as_slice()
}

/// Legacy fallback: read allowed_commands from config.toml.
/// Used when init_security() was not called (e.g. tests, tools used standalone).
fn load_from_config_file() -> Vec<String> {
    let cfg = dirs::home_dir()
        .map(|h| h.join(".evocli").join("config.toml"))
        .filter(|p| p.exists());
    let Some(path) = cfg else {
        return default_allowed_fallback();
    };
    let Ok(content) = std::fs::read_to_string(&path) else {
        return default_allowed_fallback();
    };
    // Parse [security] allowed_commands = [...]
    parse_string_array_from_toml(&content, "security", "allowed_commands")
        .unwrap_or_else(default_allowed_fallback)
}

fn load_blocked_from_config_file() -> Vec<String> {
    let cfg = dirs::home_dir()
        .map(|h| h.join(".evocli").join("config.toml"))
        .filter(|p| p.exists());
    let Some(path) = cfg else { return default_blocked_fallback(); };
    let Ok(content) = std::fs::read_to_string(&path) else { return default_blocked_fallback(); };
    parse_string_array_from_toml(&content, "security", "blocked_patterns")
        .unwrap_or_else(default_blocked_fallback)
}

/// Hardcoded fallback defaults (same as config.rs::default_allowed_commands).
/// Only used when config file is missing/unreadable AND init_security was not called.
fn default_allowed_fallback() -> Vec<String> {
    vec![
        "cargo","rustc","rustup","rust-analyzer",
        "npm","npx","node","pnpm","yarn","bun","deno",
        "python","python3","pip","uv",
        "go","gofmt","gopls","make","cmake","ninja",
        "mvn","gradle","java","javac","dotnet",
        "evocli","git","cd",
        "cat","ls","dir","echo","head","tail","wc","grep","find","fd","rg",
        "pwd","which","type","env","printenv","stat","file","diff","patch",
        "sort","uniq","cut","awk","sed","xargs","tr","curl","wget",
        "jq","yq","zip","unzip","tar","gzip","gunzip",
        "ps","top","htop","mkdir","touch","cp","mv",
    ].into_iter().map(String::from).collect()
}

fn default_blocked_fallback() -> Vec<String> {
    vec![
        "rm -rf /","rm -rf /*","rm -rf ~","rm -rf ~/",
        "rm -rf /etc","rm -rf /usr","rm -rf /bin","rm -rf /var",
        "rm -rf /home","rm -rf /root","rm -rf /boot",
        "chmod -r 777 /","chmod 777 /",
        "> /dev/sda","dd if=","dd of=/dev/","mkfs","wipefs",
        ":(){ :|:& };:","find / -delete",
        "format c:","format d:","rd /s /q c:\\",
    ].into_iter().map(String::from).collect()
}

/// Minimal TOML array parser (tools crate cannot depend on serde/toml).
fn parse_string_array_from_toml(content: &str, section: &str, key: &str) -> Option<Vec<String>> {
    let section_header = format!("[{section}]");
    let mut in_section = false;
    for line in content.lines() {
        let trimmed = line.trim();
        if trimmed == section_header { in_section = true; continue; }
        if trimmed.starts_with('[') { in_section = false; continue; }
        if in_section && trimmed.starts_with(key) {
            if let Some(start) = trimmed.find('[') {
                // Handle multi-line arrays (find closing ])
                let rest = &trimmed[start + 1..];
                let inner = if let Some(end) = rest.find(']') {
                    rest[..end].to_string()
                } else {
                    // Simple single-line only
                    rest.to_string()
                };
                let result: Vec<String> = inner
                    .split(',')
                    .map(|s| s.trim().trim_matches('"').trim_matches('\'').to_string())
                    .filter(|s| !s.is_empty())
                    .collect();
                if !result.is_empty() { return Some(result); }
            }
        }
    }
    None
}

// ── Core API ─────────────────────────────────────────────────────

/// Execute a shell command with safety checks.
///
/// - `cmd`: full command string (e.g. `"cargo build --release"`)
/// - `cwd`: working directory for the command
/// - `timeout_secs`: maximum execution time in seconds (0 = no limit)
/// - `dry_run`: if true, only validate and return what *would* run
pub fn run_command(
    cmd: &str,
    cwd: &Path,
    timeout_secs: u32,
    dry_run: bool,
) -> Result<CommandOutput> {
    let cmd_trimmed = cmd.trim();
    if cmd_trimmed.is_empty() {
        bail!("Empty command");
    }

    // ── Safety: dangerous pattern check (config-driven) ──
    let cmd_lower = cmd_trimmed.to_lowercase();
    for pattern in get_blocked() {
        if cmd_lower.contains(pattern.to_lowercase().as_str()) {
            bail!("Blocked: command matches dangerous pattern '{pattern}'");
        }
    }

    // ── Safety: allowlist check (config-driven) ──
    let first_token = cmd_trimmed.split_whitespace().next().unwrap_or("");
    // Strip path prefix to get the binary name
    let binary_name = Path::new(first_token)
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or(first_token);

    let allowed_list = get_allowed();
    let allowed = allowed_list.iter().any(|prefix| {
        binary_name == prefix.as_str()
            || binary_name.starts_with(&format!("{prefix}."))  // e.g. python3.11
    });
    if !allowed {
        bail!(
            "Blocked: '{binary_name}' is not in the allowed command list. \
             Allowed: {}\n\
             Add to ~/.evocli/config.toml [security] extra_allowed_commands = [\"{binary_name}\"]",
            allowed_list.join(", ")
        );
    }

    // ── Dry run ──
    if dry_run {
        return Ok(CommandOutput {
            exit_code: 0,
            stdout: format!("[dry-run] would execute: {cmd_trimmed}\n  in: {}", cwd.display()),
            stderr: String::new(),
        });
    }

    // ── Execute ──
    // Shell selection strategy:
    //   Linux/macOS: sh -c (POSIX, supports &&, ||, pipes, redirects, variables)
    //   Windows priority order:
    //     1. bash (Git Bash / WSL)  — full bash support including && chains
    //     2. pwsh (PowerShell 7+)   — supports && operator, good Unix alias coverage
    //     3. powershell (PS 5.1)    — built-in fallback; rewrite && to ; for compatibility
    //
    // Shell detection cached per-process so startup only checks once.
    #[cfg(target_os = "windows")]
    let shell_cmd = {
        // 0 = bash, 1 = pwsh, 2 = powershell(PS5)
        static WIN_SHELL: std::sync::OnceLock<u8> = std::sync::OnceLock::new();
        let shell_id = *WIN_SHELL.get_or_init(|| {
            if std::process::Command::new("bash").args(["--version"])
                .stdout(std::process::Stdio::null()).stderr(std::process::Stdio::null())
                .status().map(|s| s.success()).unwrap_or(false) { return 0u8; }
            if std::process::Command::new("pwsh").args(["--version"])
                .stdout(std::process::Stdio::null()).stderr(std::process::Stdio::null())
                .status().map(|s| s.success()).unwrap_or(false) { return 1u8; }
            2u8
        });

        // PowerShell 5.1 doesn't support &&; rewrite to ; so chained commands still run.
        let effective_cmd: std::borrow::Cow<str> = if shell_id == 2 {
            std::borrow::Cow::Owned(cmd_trimmed.replace("&&", ";").replace("||", ";"))
        } else {
            std::borrow::Cow::Borrowed(cmd_trimmed)
        };

        match shell_id {
            0 => { let mut c = Command::new("bash"); c.args(["-c", effective_cmd.as_ref()]); c }
            1 => { let mut c = Command::new("pwsh"); c.args(["-NoProfile", "-NonInteractive", "-Command", effective_cmd.as_ref()]); c }
            _ => { let mut c = Command::new("powershell"); c.args(["-NoProfile", "-NonInteractive", "-Command", effective_cmd.as_ref()]); c }
        }
    };

    #[cfg(not(target_os = "windows"))]
    let shell_cmd = {
        let mut c = Command::new("sh");
        c.args(["-c", cmd_trimmed]);
        c
    };

    let mut child = {
        let mut c = shell_cmd;
        c.current_dir(cwd);
        c.stdout(std::process::Stdio::piped());
        c.stderr(std::process::Stdio::piped());
        c.spawn()?
    };

    // Timeout handling
    let output = if timeout_secs > 0 {
        let timeout = Duration::from_secs(timeout_secs as u64);
        match child.wait_timeout(timeout) {
            Ok(Some(status)) => {
                let stdout = read_pipe(child.stdout.take());
                let stderr = read_pipe(child.stderr.take());
                CommandOutput {
                    exit_code: status.code().unwrap_or(-1),
                    stdout,
                    stderr,
                }
            }
            Ok(None) => {
                // Kill the child and WAIT for it to prevent zombie processes.
                // kill() sends SIGKILL but does NOT reap the process table entry.
                // Without wait(), the zombie lingers until the Host process exits.
                let _ = child.kill();
                let _ = child.wait();  // Reap the zombie — collect exit status
                bail!("Command timed out after {timeout_secs}s: {cmd_trimmed}");
            }
            Err(e) => bail!("Failed to wait for command: {e}"),
        }
    } else {
        let output = child.wait_with_output()?;
        CommandOutput {
            exit_code: output.status.code().unwrap_or(-1),
            stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
            stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
        }
    };

    Ok(output)
}

/// Check whether a command would be allowed (without executing).
pub fn is_allowed(cmd: &str) -> bool {
    let cmd_trimmed = cmd.trim();
    if cmd_trimmed.is_empty() {
        return false;
    }
    let cmd_lower = cmd_trimmed.to_lowercase();
    for pattern in get_blocked() {
        if cmd_lower.contains(pattern.to_lowercase().as_str()) {
            return false;
        }
    }
    let first_token = cmd_trimmed.split_whitespace().next().unwrap_or("");
    let binary_name = Path::new(first_token)
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or(first_token);

    get_allowed().iter().any(|prefix| {
        binary_name == prefix.as_str() || binary_name.starts_with(&format!("{prefix}."))
    })
}

// ── Helpers ──────────────────────────────────────────────────────

fn read_pipe<R: std::io::Read>(pipe: Option<R>) -> String {
    match pipe {
        Some(mut r) => {
            // Cap output at 10MB to prevent OOM when commands produce massive output
            // (e.g., `cat large_file`, log dumps, recursive ls). Beyond this limit,
            // the output is truncated with a notice so callers know it was cut off.
            const MAX_OUTPUT_BYTES: usize = 10 * 1024 * 1024; // 10MB
            let mut buf = Vec::with_capacity(4096);
            let mut tmp = [0u8; 8192];
            loop {
                match r.read(&mut tmp) {
                    Ok(0) => break,
                    Ok(n) => {
                        if buf.len() + n > MAX_OUTPUT_BYTES {
                            let remaining = MAX_OUTPUT_BYTES.saturating_sub(buf.len());
                            buf.extend_from_slice(&tmp[..remaining]);
                            buf.extend_from_slice(b"\n...[output truncated at 10MB]");
                            break;
                        }
                        buf.extend_from_slice(&tmp[..n]);
                    }
                    Err(_) => break,
                }
            }
            String::from_utf8_lossy(&buf).into_owned()
        }
        None => String::new(),
    }
}

// ── Convenience trait for timeout (std doesn't have wait_timeout) ──

trait WaitTimeout {
    fn wait_timeout(&mut self, timeout: Duration) -> std::io::Result<Option<std::process::ExitStatus>>;
}

impl WaitTimeout for std::process::Child {
    fn wait_timeout(&mut self, timeout: Duration) -> std::io::Result<Option<std::process::ExitStatus>> {
        let start = std::time::Instant::now();
        loop {
            match self.try_wait()? {
                Some(status) => return Ok(Some(status)),
                None => {
                    if start.elapsed() >= timeout {
                        return Ok(None);
                    }
                    std::thread::sleep(Duration::from_millis(50));
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_allowed_commands() {
        assert!(is_allowed("cargo build"));
        assert!(is_allowed("git status"));
        assert!(is_allowed("python3 --version"));
        assert!(is_allowed("npm install"));
    }

    #[test]
    fn test_blocked_commands() {
        assert!(!is_allowed("rm -rf /"));
        assert!(!is_allowed("sudo reboot"));
        assert!(!is_allowed("powershell -Command"));
        assert!(!is_allowed(""));
    }

    #[test]
    fn test_dry_run() -> Result<()> {
        let cwd = std::env::current_dir()?;
        let out = run_command("cargo --version", &cwd, 0, true)?;
        assert!(out.stdout.contains("[dry-run]"));
        assert_eq!(out.exit_code, 0);
        Ok(())
    }
}

// ── v3.x Shell Layer: Brush + uutils ─────────────────────────────────────────
//
// 设计文档：Section 22，L4870-5571
//
// 架构目标（v3.x）：
//   1. brush-core：Rust 原生 bash 解释器（替代 sh/cmd）
//   2. uutils/coreutils：跨平台 GNU 工具（grep/find/ls/sed/awk 等）
//   3. Landlock（Linux）/ Capsicum（BSD）内核级沙箱
//   4. duct：原生进程（cargo/npm/git）执行层
//
// 当前状态（v3.x 准备阶段）：
//   feature flag "brush" — 启用时使用 brush 替代 sh/cmd
//   未启用时回退到当前 std::process::Command 实现
//
// 启用方式：
//   [dependencies]
//   tools = { path = "crates/tools", features = ["brush"] }

/// v3.x Shell 执行策略
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum ShellBackend {
    /// 当前 v2.x：系统 sh/cmd + std::process::Command
    System,
    /// v3.x: Brush（Rust 原生 bash）+ uutils
    Brush,
    /// v3.x: WASM 沙箱（完全隔离）
    Wasm,
}

impl ShellBackend {
    /// 根据运行环境自动选择最佳后端
    pub fn auto_select() -> Self {
        #[cfg(feature = "brush")]
        return ShellBackend::Brush;

        #[cfg(not(feature = "brush"))]
        ShellBackend::System
    }
}

/// Brush 内置命令路由（v3.x）
/// 将命令分类为：内置 uutils 工具 / 原生进程（duct）
#[derive(Debug, Clone, PartialEq)]
pub enum CommandRoute {
    /// 由 uutils/coreutils 内部处理（无需外部进程）
    BuiltinUutils,
    /// 通过 duct 调用宿主机原生进程（cargo/npm/git 等）
    NativeProcess,
    /// 完全禁止
    Blocked,
}

/// 判断命令应该走哪条路由（v3.x Brush 层）
pub fn classify_command(cmd: &str) -> CommandRoute {
    let first = cmd.trim().split_whitespace().next().unwrap_or("");
    let binary = Path::new(first).file_name()
        .and_then(|n| n.to_str()).unwrap_or(first);

    // uutils 内置工具集（不需要外部进程）
    const UUTILS_BUILTINS: &[&str] = &[
        "ls", "cat", "grep", "find", "wc", "head", "tail", "sed", "awk",
        "sort", "uniq", "cut", "tr", "echo", "printf", "cp", "mv", "rm",
        "mkdir", "rmdir", "touch", "chmod", "chown", "ln", "readlink",
        "basename", "dirname", "pwd", "env", "true", "false", "test", "[",
        "tar", "zip", "gzip", "xargs", "tee", "sleep", "date", "id",
    ];

    // 危险模式检查
    let cmd_lower = cmd.to_lowercase();
    for pattern in get_blocked() {
        if cmd_lower.contains(pattern.to_lowercase().as_str()) { return CommandRoute::Blocked; }
    }

    if UUTILS_BUILTINS.contains(&binary) {
        CommandRoute::BuiltinUutils
    } else if get_allowed().iter().any(|p| binary == p.as_str()) {
        CommandRoute::NativeProcess
    } else {
        CommandRoute::Blocked
    }
}

// ── v3.x WASM Sandbox ─────────────────────────────────────────────────────────
//
// 设计文档：Section 1.2（安全沙箱），L143/4886
//
// 目标：不受信任的代码在 WebAssembly 沙箱中执行
//   - 使用 wasmtime（Bytecode Alliance 出品，Rust 原生）
//   - 文件系统通过 WASI 接口受限访问
//   - 网络访问默认禁止
//   - 内存限制 128MB
//
// 当前状态：接口定义完整，wasmtime 依赖待加入 Cargo.toml
// 启用：features = ["wasm-sandbox"]

/// WASM 沙箱执行配置
#[derive(Debug, Clone)]
pub struct WasmSandboxConfig {
    /// 允许读取的目录（WASI preopened dirs）
    pub allowed_read_dirs:  Vec<PathBuf>,
    /// 允许写入的目录
    pub allowed_write_dirs: Vec<PathBuf>,
    /// 内存限制（字节，默认 128MB）
    pub memory_limit: u64,
    /// CPU 时间限制（秒）
    pub cpu_limit_secs: u32,
    /// 允许网络访问
    pub allow_network: bool,
}

impl Default for WasmSandboxConfig {
    fn default() -> Self {
        Self {
            allowed_read_dirs:  vec![],
            allowed_write_dirs: vec![],
            memory_limit:       128 * 1024 * 1024, // 128 MB
            cpu_limit_secs:     30,
            allow_network:      false,
        }
    }
}

/// WASM 沙箱执行结果
#[derive(Debug, Clone)]
pub struct WasmOutput {
    pub exit_code: i32,
    pub stdout:    String,
    pub stderr:    String,
}

/// 在 WASM 沙箱中执行 WASI 二进制（v3.x，需要 feature = "wasm-sandbox"）
///
/// 当前返回 NotImplemented，wasmtime 集成在 v3.x 完成
pub fn run_in_wasm_sandbox(
    _wasm_bytes: &[u8],
    _args: &[&str],
    _config: &WasmSandboxConfig,
) -> Result<WasmOutput> {
    // v3.x: wasmtime 集成占位
    // 实现步骤：
    //   1. wasmtime::Engine::new(&wasmtime::Config::new())?
    //   2. wasmtime::Module::new(&engine, wasm_bytes)?
    //   3. wasmtime_wasi::WasiCtxBuilder 设置 preopened_dirs / inherit_stdio
    //   4. wasmtime::Linker::new + linker.module + store.call("_start")
    //   5. 捕获 stdout/stderr 写入 pipe
    bail!("WASM sandbox not yet implemented (v3.x feature). \
           Add wasmtime crate and enable feature = [\"wasm-sandbox\"]")
}

/// 检查 WASM sandbox 是否可用（compile-time feature check）
pub fn wasm_sandbox_available() -> bool {
    cfg!(feature = "wasm-sandbox")
}

// ── v3.x: tools crate Cargo.toml 需要添加的依赖（待实现时取消注释）────────────
//
// [dependencies]
// # Brush shell（Rust 原生 bash 解释器）
// brush-core     = { version = "0.2", optional = true }
//
// # uutils coreutils（跨平台 GNU 工具）
// uucore         = { version = "0.0.28", optional = true, features = ["fs"] }
// uu_ls          = { version = "0.0.28", optional = true }
// uu_grep        = { version = "0.0.28", optional = true }
// uu_find        = { version = "0.0.28", optional = true }
// ... (同理 cat/wc/head/tail/sed/sort/tar)
//
// # WASM 沙箱
// wasmtime       = { version = "28", optional = true, features = ["wasi"] }
// wasmtime-wasi  = { version = "28", optional = true }
//
// [features]
// brush          = ["dep:brush-core", "dep:uucore", ...]
// wasm-sandbox   = ["dep:wasmtime", "dep:wasmtime-wasi"]


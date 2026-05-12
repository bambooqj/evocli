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

// ── Allowlist / blocklist ────────────────────────────────────────

/// Commands whose first token must match one of these prefixes.
const ALLOWED_PREFIXES: &[&str] = &[
    // Build tools
    "cargo", "rustc", "rustup", "rust-analyzer",
    "npm", "npx", "node", "pnpm", "yarn", "bun", "deno",
    "python", "python3", "pip", "uv",
    "go", "gofmt", "gopls",
    "make", "cmake", "ninja",
    "mvn", "gradle", "java", "javac",
    "dotnet",
    // Version control
    "git",
    // Shell navigation & directory operations
    // Note: `cd` in shell.run only changes directory within THAT subprocess.
    // It does NOT affect subsequent shell.run calls (each runs in fresh subprocess).
    // Allowing it enables common patterns like: cd src && cargo build
    "cd",
    // Shell read-only / navigation (safe: no destructive side effects)
    "cat", "ls", "dir", "echo",
    "head", "tail", "wc", "grep", "find", "fd", "rg",
    "pwd",           // print working directory
    "which", "type", // find executable location
    "env", "printenv",// environment inspection
    "stat", "file",  // file metadata (read-only)
    "diff", "patch", // diff viewing
    "sort", "uniq", "cut", "awk", "sed", "xargs", "tr",
    "curl", "wget",  // network (non-destructive by default)
    "jq", "yq",      // JSON/YAML processing
    "zip", "unzip", "tar", "gzip", "gunzip",
    // Process inspection (read-only)
    "ps", "top", "htop",
    // Create / move (allowed with security blacklist protecting critical paths)
    "mkdir",         // create directories (safe — security blocks system dirs)
    "touch",         // create empty files (safe)
    "cp", "mv",      // copy/move (safe — security blacklist blocks /etc etc.)
    // NOTE: rm is intentionally NOT here. It is destructive and hard to reason
    // about safely. Use 'git checkout -- <file>' or trash-cli instead.
];

/// Patterns that are always rejected regardless of the command prefix.
const DANGEROUS_PATTERNS: &[&str] = &[
    "rm -rf /",
    "rm -rf /*",
    "chmod 777 /",
    "> /dev/",
    "mkfs",
    ":(){:|:&};:",
    "dd if=/dev/",
    "wget http",
    "curl http",
];

// ── User-configurable extra allowed commands ─────────────────────
// Loaded once from ~/.evocli/config.toml [shell] extra_commands = ["java", "mvn"]
static EXTRA_ALLOWED: OnceLock<Vec<String>> = OnceLock::new();

fn load_extra_allowed() -> &'static [String] {
    EXTRA_ALLOWED.get_or_init(|| {
        let cfg = dirs::home_dir()
            .map(|h| h.join(".evocli").join("config.toml"))
            .filter(|p| p.exists());
        let Some(path) = cfg else { return vec![] };
        let Ok(content) = std::fs::read_to_string(&path) else { return vec![] };
        // Minimal TOML parse: look for [shell] section then extra_commands = [...]
        let mut in_shell = false;
        for line in content.lines() {
            let trimmed = line.trim();
            if trimmed == "[shell]" { in_shell = true; continue; }
            if trimmed.starts_with('[') { in_shell = false; continue; }
            if in_shell && trimmed.starts_with("extra_commands") {
                // Parse: extra_commands = ["java", "mvn", "gradle"]
                if let Some(start) = trimmed.find('[') {
                    let inner = &trimmed[start + 1..];
                    if let Some(end) = inner.find(']') {
                        return inner[..end]
                            .split(',')
                            .map(|s| s.trim().trim_matches('"').trim_matches('\'').to_string())
                            .filter(|s| !s.is_empty())
                            .collect();
                    }
                }
            }
        }
        vec![]
    })
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

    // ── Safety: dangerous pattern check ──
    let cmd_lower = cmd_trimmed.to_lowercase();
    for pattern in DANGEROUS_PATTERNS {
        if cmd_lower.contains(pattern) {
            bail!("Blocked: command matches dangerous pattern '{pattern}'");
        }
    }

    // ── Safety: allowlist check ──
    let first_token = cmd_trimmed.split_whitespace().next().unwrap_or("");
    // Strip path prefix to get the binary name
    let binary_name = Path::new(first_token)
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or(first_token);

    let extra = load_extra_allowed();
    let allowed = ALLOWED_PREFIXES.iter().any(|prefix| {
        binary_name == *prefix || binary_name.starts_with(&format!("{prefix}.")) // e.g. python3.11
    }) || extra.iter().any(|prefix| {
        binary_name == prefix.as_str() || binary_name.starts_with(&format!("{prefix}."))
    });
    if !allowed {
        let extra_list = if extra.is_empty() {
            String::new()
        } else {
            format!(", {}", extra.join(", "))
        };
        bail!(
            "Blocked: '{binary_name}' is not in the allowed command list. \
             Allowed: {}{extra_list}",
            ALLOWED_PREFIXES.join(", ")
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
    let shell_cmd = if cfg!(target_os = "windows") {
        let mut c = Command::new("cmd");
        c.args(["/C", cmd_trimmed]);
        c
    } else {
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
    for pattern in DANGEROUS_PATTERNS {
        if cmd_lower.contains(pattern) {
            return false;
        }
    }
    let first_token = cmd_trimmed.split_whitespace().next().unwrap_or("");
    let binary_name = Path::new(first_token)
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or(first_token);

    ALLOWED_PREFIXES.iter().any(|prefix| {
        binary_name == *prefix || binary_name.starts_with(&format!("{prefix}."))
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
    for pattern in DANGEROUS_PATTERNS {
        if cmd_lower.contains(pattern) { return CommandRoute::Blocked; }
    }

    if UUTILS_BUILTINS.contains(&binary) {
        CommandRoute::BuiltinUutils
    } else if ALLOWED_PREFIXES.iter().any(|p| binary == *p) {
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


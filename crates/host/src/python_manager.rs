//! python_manager.rs — EvoCLI 自管理 Python 运行时（v3.x）
//!
//! 目标：完全不依赖系统 Python。零手动 pip。
//!
//! 使用 uv（Rust 编写的 Python 包管理器）自动管理：
//!   1. uv 自身（PATH → ~/.evocli/bin/uv → 自动下载）
//!   2. Python 3.11（通过 uv python install）
//!   3. evocli-soul[full] venv（所有功能库一次性安装）
//!
//! 架构：
//!   ~/.evocli/
//!   ├── bin/uv[.exe]               ← 托管的 uv 二进制
//!   ├── python/                    ← uv 管理的 CPython 3.11
//!   └── venv/                      ← evocli-soul 独立虚拟环境
//!       ├── bin/python             ← 最终使用的 Python 可执行文件
//!       ├── lib/site-packages/     ← 所有依赖（含 full extras）
//!       └── .evocli_full_installed ← 全量安装完成标记文件

use anyhow::{Context, Result};
use std::path::{Path, PathBuf};
use std::process::Command;

const PYTHON_VERSION: &str = "3.11";
const UV_GITHUB_RELEASE: &str = "https://github.com/astral-sh/uv/releases/latest/download";
/// 版本标记：变更此值将触发重新安装所有 extras
/// v1.2: 新增 jinja2, readability-lxml, html2text, watchfiles + Rust tree-sitter
const FULL_INSTALL_VERSION: &str = "evocli-full-v1.2";

// ── 平台相关常量 ──────────────────────────────────────────────────────────────

#[cfg(all(target_os = "windows", target_arch = "x86_64"))]
const UV_ARCHIVE: &str = "uv-x86_64-pc-windows-msvc.zip";

#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
const UV_ARCHIVE: &str = "uv-aarch64-apple-darwin.tar.gz";

#[cfg(all(target_os = "macos", target_arch = "x86_64"))]
const UV_ARCHIVE: &str = "uv-x86_64-apple-darwin.tar.gz";

#[cfg(all(target_os = "linux", target_arch = "x86_64"))]
const UV_ARCHIVE: &str = "uv-x86_64-unknown-linux-musl.tar.gz";

#[cfg(all(target_os = "linux", target_arch = "aarch64"))]
const UV_ARCHIVE: &str = "uv-aarch64-unknown-linux-musl.tar.gz";

// fallback for other platforms
#[cfg(not(any(
    all(target_os = "windows", target_arch = "x86_64"),
    all(target_os = "macos", target_arch = "aarch64"),
    all(target_os = "macos", target_arch = "x86_64"),
    all(target_os = "linux", target_arch = "x86_64"),
    all(target_os = "linux", target_arch = "aarch64"),
)))]
const UV_ARCHIVE: &str = "uv-x86_64-unknown-linux-musl.tar.gz";

// ── PythonManager ─────────────────────────────────────────────────────────────

pub struct PythonManager;

impl PythonManager {
    // ── 路径 helpers ──────────────────────────────────────────────

    fn evocli_dir() -> PathBuf {
        dirs::home_dir().unwrap_or_default().join(".evocli")
    }

    /// uv 可执行文件路径（优先 PATH，否则 ~/.evocli/bin/uv[.exe]）
    pub fn uv_exe() -> PathBuf {
        // 先检查 PATH
        let uv_name = if cfg!(windows) { "uv.exe" } else { "uv" };
        if let Ok(path_uv) = which(uv_name) {
            return path_uv;
        }
        Self::evocli_dir().join("bin").join(uv_name)
    }

    /// 托管 Python 可执行文件路径
    pub fn python_exe() -> PathBuf {
        let venv = Self::venv_dir();
        if cfg!(windows) {
            venv.join("Scripts").join("python.exe")
        } else {
            venv.join("bin").join("python3")
        }
    }

    /// 托管虚拟环境目录
    pub fn venv_dir() -> PathBuf {
        Self::evocli_dir().join("venv")
    }

    // ── 状态检查 ──────────────────────────────────────────────────

    /// Check if uv is available in PATH or ~/.evocli/bin.
    /// Used by doctor_cmd for health checks.
    #[allow(dead_code)]
    pub fn uv_available() -> bool {
        let uv = Self::uv_exe();
        uv.exists()
            && Command::new(&uv)
                .arg("--version")
                .output()
                .map(|o| o.status.success())
                .unwrap_or(false)
    }

    pub fn python_ready() -> bool {
        Self::python_exe().exists()
    }

    /// Check if evocli-soul Python package is installed in the venv.
    #[allow(dead_code)]
    pub fn soul_installed() -> bool {
        let site_pkg = if cfg!(windows) {
            Self::venv_dir().join("Lib").join("site-packages")
        } else {
            let lib = Self::venv_dir().join("lib");
            if let Ok(entries) = std::fs::read_dir(&lib) {
                for e in entries.flatten() {
                    let sp = e.path().join("site-packages").join("evocli_soul");
                    if sp.exists() {
                        return true;
                    }
                }
            }
            return false;
        };
        site_pkg.join("evocli_soul").exists()
    }

    /// 检查 [full] extras 是否已完整安装（通过版本标记文件）
    pub fn full_extras_installed() -> bool {
        let marker = Self::venv_dir().join(".evocli_full_installed");
        match std::fs::read_to_string(&marker) {
            Ok(content) => content.trim() == FULL_INSTALL_VERSION,
            Err(_) => false,
        }
    }

    fn write_full_marker() {
        let marker = Self::venv_dir().join(".evocli_full_installed");
        let _ = std::fs::write(marker, FULL_INSTALL_VERSION);
    }

    // ── Step 1: 确保 uv 可用 ─────────────────────────────────────

    pub fn ensure_uv() -> Result<PathBuf> {
        let uv = Self::uv_exe();
        if uv.exists() {
            return Ok(uv);
        }

        // 尝试从 PATH 中查找
        let uv_name = if cfg!(windows) { "uv.exe" } else { "uv" };
        if let Ok(p) = which(uv_name) {
            return Ok(p);
        }

        // 自动下载 uv
        println!("  → uv not found — downloading to ~/.evocli/bin/");
        Self::download_uv()
    }

    fn download_uv() -> Result<PathBuf> {
        let bin_dir = Self::evocli_dir().join("bin");
        std::fs::create_dir_all(&bin_dir)?;

        let uv_name = if cfg!(windows) { "uv.exe" } else { "uv" };
        let dst = bin_dir.join(uv_name);

        let url = format!("{}/{}", UV_GITHUB_RELEASE, UV_ARCHIVE);
        let archive_path = bin_dir.join(UV_ARCHIVE);

        // 使用 curl 或 Invoke-WebRequest 下载（避免引入 reqwest 依赖）
        println!("  → Downloading uv from {}", url);
        let dl_ok = if cfg!(windows) {
            Command::new("powershell")
                .args([
                    "-Command",
                    &format!(
                        "Invoke-WebRequest -Uri '{}' -OutFile '{}'",
                        url,
                        archive_path.display()
                    ),
                ])
                .status()
                .map(|s| s.success())
                .unwrap_or(false)
        } else {
            Command::new("curl")
                .args(["-fsSL", &url, "-o", archive_path.to_str().unwrap_or("")])
                .status()
                .map(|s| s.success())
                .unwrap_or(false)
        };

        anyhow::ensure!(dl_ok, "Failed to download uv from {}", url);

        // 解压
        if UV_ARCHIVE.ends_with(".zip") {
            Command::new("powershell")
                .args([
                    "-Command",
                    &format!(
                        "Expand-Archive -Path '{}' -DestinationPath '{}' -Force",
                        archive_path.display(),
                        bin_dir.display()
                    ),
                ])
                .status()?;
        } else {
            Command::new("tar")
                .args([
                    "-xzf",
                    archive_path.to_str().unwrap_or(""),
                    "-C",
                    bin_dir.to_str().unwrap_or(""),
                    "--strip-components=0",
                ])
                .status()?;
        }

        // 清理 archive
        let _ = std::fs::remove_file(&archive_path);

        // M1 FIX: uv zip 内含嵌套目录（如 uv-x86_64-pc-windows-msvc/uv.exe）
        // 提取后从嵌套目录找到 uv 二进制并移动到 bin_dir
        let uv_name = if cfg!(windows) { "uv.exe" } else { "uv" };
        if !dst.exists() {
            // 在 bin_dir 下递归查找 uv 可执行文件
            let found = std::fs::read_dir(&bin_dir)?
                .flatten()
                .filter(|e| e.file_type().map(|t| t.is_dir()).unwrap_or(false))
                .flat_map(|subdir| {
                    let candidate = subdir.path().join(uv_name);
                    if candidate.exists() {
                        Some(candidate)
                    } else {
                        None
                    }
                })
                .next();

            if let Some(nested_uv) = found {
                std::fs::rename(&nested_uv, &dst)
                    .context("Failed to move uv binary from nested archive dir")?;
                // 清理空的子目录
                if let Some(parent) = nested_uv.parent() {
                    let _ = std::fs::remove_dir(parent);
                }
            }
        }

        // 设置可执行权限（Unix）
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            if dst.exists() {
                let mut perms = std::fs::metadata(&dst)?.permissions();
                perms.set_mode(0o755);
                std::fs::set_permissions(&dst, perms)?;
            }
        }

        anyhow::ensure!(dst.exists(), "uv binary not found after extraction. Archive: {}, Expected: {}. \nInstall uv manually: https://docs.astral.sh/uv/getting-started/installation/", UV_ARCHIVE, dst.display());
        println!("  ✓ uv installed at {}", dst.display());
        Ok(dst)
    }

    // ── Step 2: 确保 Python 3.11 可用 ────────────────────────────

    pub fn ensure_python(uv: &Path) -> Result<PathBuf> {
        // ── Check if Python is already installed before triggering uv ──
        // On Windows: inspect the known installation path directly.
        // On Linux/macOS: uv uses platform-specific dirs; probe via `uv python find`.
        if cfg!(windows) {
            let python_install_dir = Self::evocli_dir().join("python");
            let expected_python = python_install_dir
                .join(format!("cpython-{}-windows-x86_64-none", PYTHON_VERSION))
                .join("python.exe");
            if expected_python.exists() {
                return Ok(expected_python);
            }
        } else {
            // `uv python find` returns the path to the managed Python if installed.
            if let Ok(output) = Command::new(uv)
                .args(["python", "find", PYTHON_VERSION])
                .output()
            {
                let found = String::from_utf8_lossy(&output.stdout).trim().to_string();
                if !found.is_empty() && std::path::Path::new(&found).exists() {
                    return Ok(PathBuf::from(found));
                }
            }
        }

        // Not found — install it
        println!("  → Installing Python {} via uv...", PYTHON_VERSION);
        let status = Command::new(uv)
            .args([
                "python",
                "install",
                PYTHON_VERSION,
                "--python-preference",
                "managed",
            ])
            .status()
            .context("Failed to run uv python install")?;

        anyhow::ensure!(
            status.success(),
            "uv python install {} failed",
            PYTHON_VERSION
        );

        // 获取实际 Python 路径
        let output = Command::new(uv)
            .args(["python", "find", PYTHON_VERSION])
            .output()
            .context("uv python find failed")?;
        let py_path = PathBuf::from(String::from_utf8_lossy(&output.stdout).trim().to_string());

        println!("  ✓ Python {} at {}", PYTHON_VERSION, py_path.display());
        Ok(py_path)
    }

    // ── Step 3: 确保 evocli-soul venv 及依赖 ─────────────────────

    pub fn ensure_venv(uv: &Path, soul_dir: Option<&Path>) -> Result<PathBuf> {
        let venv = Self::venv_dir();

        // 创建 venv（如不存在）
        if !venv
            .join(if cfg!(windows) { "Scripts" } else { "bin" })
            .exists()
        {
            println!("  → Creating evocli-soul venv at {}...", venv.display());
            let status = Command::new(uv)
                .args([
                    "venv",
                    venv.to_str().unwrap_or("."),
                    "--python",
                    PYTHON_VERSION,
                    "--seed",
                ])
                .status()
                .context("uv venv failed")?;
            anyhow::ensure!(status.success(), "Failed to create venv");
        }

        // 安装 evocli-soul[full]（如未完整安装）
        if !Self::full_extras_installed() {
            println!("  → Installing evocli-soul[full] — all features included...");
            println!("  → This may take 3-5 minutes on first run (downloading ML models etc.)");

            let venv_python = venv.join(if cfg!(windows) {
                "Scripts/python.exe"
            } else {
                "bin/python3"
            });
            let venv_python_str = venv_python.to_str().unwrap_or("python");

            let soul_path_str = soul_dir.map(|d| d.to_str().unwrap_or(".")).unwrap_or(".");
            let path_with_extras = format!("{}[full]", soul_path_str);

            if soul_dir.is_none() {
                anyhow::bail!(
                    "Cannot find evocli-soul/ directory.\n\
                     Expected location: same directory as evocli binary.\n\
                     Current exe: {}\n\
                     Please ensure the evocli-soul/ folder is in the same directory as evocli.",
                    std::env::current_exe()
                        .map(|p| p.display().to_string())
                        .unwrap_or_else(|_| "<unknown>".into())
                );
            }

            // Strategy 1: install [full] extras (preferred — all features)
            let full_install_ok = Command::new(uv)
                .args([
                    "pip",
                    "install",
                    "-e",
                    &path_with_extras,
                    "--python",
                    venv_python_str,
                ])
                .status()
                .map(|s| s.success())
                .unwrap_or(false);

            if full_install_ok {
                Self::write_full_marker();
                println!("  ✓ evocli-soul[full] installed — all features ready");
            } else {
                // Strategy 2: dependency conflict in [full] — install core packages only
                // This ensures litellm/pydantic-ai work even if optional extras fail
                eprintln!(
                    "  ⚠ Full install failed (dependency conflict). Installing core packages..."
                );
                let core_args = [
                    "pip",
                    "install",
                    // Required deps (same as pyproject.toml [dependencies])
                    "litellm>=1.83",
                    "tiktoken>=0.7",
                    "pydantic-ai>=0.0.46",
                    "langgraph>=0.3",
                    "langgraph-checkpoint-sqlite>=2.0",
                    "instructor>=1.15",
                    "anyio>=4.6",
                    "httpx>=0.27",
                    "jinja2>=3.1",
                    // Install Soul as editable (no extras)
                    "--python",
                    venv_python_str,
                ];
                let core_ok = Command::new(uv)
                    .args(core_args)
                    .status()
                    .map(|s| s.success())
                    .unwrap_or(false);

                // Also install Soul itself (editable, no extras)
                let soul_ok = Command::new(uv)
                    .args([
                        "pip",
                        "install",
                        "-e",
                        soul_path_str,
                        "--python",
                        venv_python_str,
                    ])
                    .status()
                    .map(|s| s.success())
                    .unwrap_or(false);

                if core_ok && soul_ok {
                    // Mark as installed (without all extras, but core works)
                    Self::write_full_marker();
                    println!("  ✓ Core packages installed (some optional features unavailable)");
                    println!("  → To enable all features: evocli init --full");
                } else {
                    anyhow::bail!(
                        "Failed to install evocli-soul core packages.\n\
                         Try running: evocli init\n\
                         Or manually: uv pip install litellm pydantic-ai langgraph --python {}",
                        venv_python_str
                    );
                }
            }
        } else {
            tracing::debug!("evocli-soul[full] already installed");
        }

        Ok(venv)
    }

    // ── 公共 API：一键确保就绪 ───────────────────────────────────

    /// 确保完整的 Python 环境就绪，返回 Python 可执行路径。
    /// 在 `evocli init` 中调用一次；后续启动复用已有环境。
    pub fn setup(soul_dir: Option<&Path>) -> Result<PathBuf> {
        println!("\n  Setting up managed Python environment...");

        let uv = Self::ensure_uv().context(
            "Failed to ensure uv is available. Install uv manually: https://docs.astral.sh/uv/",
        )?;

        let _python = Self::ensure_python(&uv)?;
        let _venv = Self::ensure_venv(&uv, soul_dir)?;

        let py = Self::python_exe();
        anyhow::ensure!(
            py.exists(),
            "Python exe not found at {} after setup",
            py.display()
        );
        println!("  ✓ Python environment ready: {}", py.display());
        Ok(py)
    }

    /// 仅检查环境，不自动设置。供 soul_bridge 启动时使用。
    pub fn get_python_exe() -> PathBuf {
        let managed = Self::python_exe();
        if managed.exists() {
            return managed;
        }
        // fallback: uv run（如果 uv 可用但 venv 还没建好）
        // 最后 fallback: 系统 Python（仅开发环境）
        PathBuf::from(if cfg!(windows) { "python" } else { "python3" })
    }

    /// 状态报告（供 doctor_cmd 使用）
    #[allow(dead_code)]
    pub fn status_report() -> String {
        let uv_ok = Self::uv_available();
        let py_ok = Self::python_ready();
        let soul_ok = Self::soul_installed();
        format!(
            "uv: {}  python{}: {}  evocli-soul: {}",
            if uv_ok { "✓" } else { "✗" },
            PYTHON_VERSION,
            if py_ok { "✓" } else { "✗" },
            if soul_ok { "✓" } else { "✗" },
        )
    }
}

// ── 工具函数 ──────────────────────────────────────────────────────────────────

/// 在 PATH 中查找可执行文件
fn which(name: &str) -> Result<PathBuf> {
    let path_var = std::env::var_os("PATH").unwrap_or_default();
    for dir in std::env::split_paths(&path_var) {
        let candidate = dir.join(name);
        if candidate.exists() {
            return Ok(candidate);
        }
    }
    anyhow::bail!("{} not found in PATH", name)
}

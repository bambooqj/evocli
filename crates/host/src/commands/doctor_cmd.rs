//! commands/doctor_cmd.rs — evocli doctor 健康检查（Section 14，10 项检查）
use anyhow::Result;

pub fn run() -> Result<()> {
    println!("\n━━━ EvoCLI System Health Check ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n");

    let mut pass = 0u32;
    let mut warn = 0u32;
    let mut fail = 0u32;

    macro_rules! ok {
        ($msg:expr) => {
            println!("  ✅ {}", $msg);
            pass += 1;
        };
    }
    macro_rules! warn {
        ($msg:expr) => {
            println!("  ⚠️  {}", $msg);
            warn += 1;
        };
    }
    macro_rules! fail {
        ($msg:expr) => {
            println!("  ❌ {}", $msg);
            fail += 1;
        };
    }

    let home = dirs::home_dir().unwrap_or_default();
    let evocli = home.join(".evocli");

    // [1] Python Soul 脚本存在
    print!("  [1] Python Soul script ... ");
    let soul = crate::config::resolve_soul_path();
    let soul_ok = if soul.ends_with(".py") {
        std::path::Path::new(&soul).exists()
    } else {
        // 模块模式：尝试 python -c "import evocli_soul"
        std::process::Command::new(if cfg!(windows) { "python" } else { "python3" })
            .args(["-c", &format!("import {}", soul.replace(".main", ""))])
            .output()
            .map(|o| o.status.success())
            .unwrap_or(false)
    };
    if soul_ok {
        ok!(format!("Soul found: {}", soul));
    } else {
        fail!(format!(
            "Soul NOT found: {} (run: evocli init or set EVOCLI_SOUL)",
            soul
        ));
    }

    // [2] Python 3.10+ 可用（优先检测托管 Python，回退到系统 Python）
    print!("  [2] Python runtime ... ");
    let managed_py = crate::python_manager::PythonManager::python_exe();
    let (py_cmd_path, py_source) = if managed_py.exists() {
        (managed_py.to_string_lossy().to_string(), "managed")
    } else {
        (
            if cfg!(windows) {
                "python".to_string()
            } else {
                "python3".to_string()
            },
            "system",
        )
    };
    let py_ok = std::process::Command::new(&py_cmd_path)
        .args(["--version"])
        .output()
        .map(|o| {
            let v = String::from_utf8_lossy(&o.stdout).to_string()
                + &String::from_utf8_lossy(&o.stderr);
            // Parse "Python 3.11.5" → require major=3, minor>=10
            // Previous check `contains("Python 3.1")` was wrong:
            //   - matched "Python 3.1.5" (too old, <3.10)
            //   - missed "Python 3.13+" (future versions)
            v.split_whitespace()
                .nth(1)
                .and_then(|ver_str| {
                    let parts: Vec<&str> = ver_str.split('.').collect();
                    let major: u32 = parts.first()?.parse().ok()?;
                    let minor: u32 = parts.get(1)?.parse().ok()?;
                    Some(major == 3 && minor >= 10)
                })
                .unwrap_or(false)
        })
        .unwrap_or(false);
    if py_ok {
        ok!(format!(
            "Python 3.10+ found ({} at {})",
            py_source, py_cmd_path
        ));
    } else {
        warn!("Python 3.10+ not found (run: evocli init)");
    }

    // [3] Python Soul 可 import（优先使用托管 Python）
    print!("  [3] Python Soul importable ... ");
    let soul = crate::config::resolve_soul_path();
    // 从 soul 路径推断 PYTHONPATH（evocli-soul/ 目录）
    let soul_pythonpath: Option<String> = std::path::Path::new(&soul)
        .parent() // evocli_soul/
        .and_then(|p| p.parent()) // evocli-soul/
        .and_then(|p| std::fs::canonicalize(p).ok())
        .map(|p| p.to_string_lossy().to_string());

    let import_ok = {
        let mut cmd = std::process::Command::new(&py_cmd_path);
        cmd.args(["-c", "import evocli_soul.agent; print('ok')"]);
        if let Some(ref pp) = soul_pythonpath {
            cmd.env("PYTHONPATH", pp);
        }
        cmd.output()
            .map(|o| String::from_utf8_lossy(&o.stdout).contains("ok"))
            .unwrap_or(false)
    };
    if import_ok {
        ok!("evocli_soul importable");
    } else {
        warn!(format!(
            "Cannot import evocli_soul (PYTHONPATH={:?}). Run: pip install -e evocli-soul/",
            soul_pythonpath.as_deref().unwrap_or("unset")
        ));
    }

    // [4] LLM API Key 配置
    print!("  [4] LLM API Key ... ");
    let api_configured = std::env::var("ANTHROPIC_API_KEY").is_ok()
        || std::env::var("OPENAI_API_KEY").is_ok()
        || std::env::var("DEEPSEEK_API_KEY").is_ok();
    if api_configured {
        ok!("API Key configured");
    } else {
        warn!("No API Key found (run: evocli init)");
    }

    // [5] 全局目录结构完整
    print!("  [5] Global dir structure ... ");
    let required_global = [
        evocli.clone(),
        evocli.join("memory"),
        evocli.join("skills"),
        evocli.join("logs"),
        evocli.join("sessions"),
        evocli.join("data"),
        evocli.join("vectors"),
        evocli.join("prompt_templates"),
    ];
    let missing_global: Vec<_> = required_global
        .iter()
        .filter(|d| !d.exists())
        .map(|d| {
            d.file_name()
                .unwrap_or_default()
                .to_string_lossy()
                .to_string()
        })
        .collect();
    if missing_global.is_empty() {
        ok!("~/.evocli/ structure complete");
    } else {
        warn!(format!(
            "Missing dirs: {} (run: evocli init)",
            missing_global.join(", ")
        ));
    }

    // [6] 项目级 .evocli/ 目录
    print!("  [6] Project .evocli/ dir ... ");
    let proj_evocli = std::path::Path::new(".evocli");
    if proj_evocli.exists() {
        ok!(".evocli/ found in working directory");
    } else {
        warn!(".evocli/ not found (run: evocli init in project root)");
    }

    // [7] Memory store (Python Soul unified storage)
    print!("  [7] Memory database ... ");
    // H1 migration: Python Soul reads/writes ~/.evocli/data/memories.jsonl (not memory.db)
    let memories_jsonl = evocli.join("data").join("memories.jsonl");
    let mem_db_legacy = evocli.join("memory.db");
    if memories_jsonl.exists() {
        let line_count = std::fs::read_to_string(&memories_jsonl)
            .map(|c| c.lines().filter(|l| !l.trim().is_empty()).count())
            .unwrap_or(0);
        ok!(format!("memories.jsonl found ({} entries)", line_count));
    } else if mem_db_legacy.exists() {
        warn!("Only legacy memory.db found — memories.jsonl not yet created (first evocli run creates it)");
    } else {
        warn!("No memory store yet (created automatically on first run)");
    }

    // [8] 代码索引
    print!("  [8] Code index ... ");
    let idx_db = std::path::Path::new(".evocli").join("code_index.db");
    if idx_db.exists() {
        ok!("code_index.db found");
    } else {
        warn!("No index (run: evocli index)");
    }

    // [9] 配置文件
    print!("  [9] Config file ... ");
    let cfg_file = evocli.join("config.toml");
    if cfg_file.exists() {
        ok!("config.toml found");
    } else {
        warn!("No config (run: evocli init)");
    }

    // [10] 磁盘写入权限
    print!("  [10] Disk write access ... ");
    let _ = std::fs::create_dir_all(&evocli);
    if evocli.exists() {
        ok!("~/.evocli/ writable");
    } else {
        fail!("Cannot write to ~/.evocli");
    }

    println!("\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");
    println!(
        "  Result: {} passed  {} warnings  {} failed",
        pass, warn, fail
    );

    if fail > 0 {
        println!("\n  → Run `evocli init` to fix critical issues.");
        std::process::exit(1);
    } else if warn > 0 {
        println!("\n  → System functional with warnings. Run `evocli init` for full setup.");
    } else {
        println!("\n  → All checks passed. EvoCLI is ready.");
    }

    Ok(())
}

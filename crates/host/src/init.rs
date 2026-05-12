//! Init wizard — `evocli init` interactive setup
//!
//! 4 steps:
//!   1. Select LLM Provider
//!   2. Enter API Key → store in keyring
//!   3. Connectivity test (ping LLM via Python Soul)
//!   4. Create directory structure

use anyhow::{Context, Result};
use dialoguer::{Input, Password, Select};

use crate::config::{Config, LlmTiers};
use crate::keystore::KeyStore;

/// Available LLM providers
const PROVIDERS: &[&str] = &["Anthropic", "OpenAI", "DeepSeek", "Ollama"];

/// Run the init wizard
pub async fn run_init() -> Result<()> {
    println!();
    println!("  EvoCLI Setup Wizard");
    println!("  ===================");
    println!();

    let mut config = Config::load_or_default()?;

    // ── Step 1: Select Provider ─────────────────────────
    let provider_idx = Select::new()
        .with_prompt("Step 1/4 — Select LLM provider")
        .items(PROVIDERS)
        .default(0)
        .interact()
        .context("Provider selection cancelled")?;

    let provider = PROVIDERS[provider_idx].to_lowercase();
    config.llm.provider = provider.clone();

    // Set default models based on provider
    config.llm.tiers = default_tiers_for(&provider);

    println!("  → Provider: {}", PROVIDERS[provider_idx]);
    println!("  → Fast model: {}", config.llm.tiers.fast);
    println!("  → Smart model: {}", config.llm.tiers.smart);
    println!();

    // ── Step 2: API Key ─────────────────────────────────
    if provider == "ollama" {
        // Ollama runs locally, no API key needed
        let base_url: String = Input::new()
            .with_prompt("Step 2/4 — Ollama base URL")
            .default("http://localhost:11434".into())
            .interact_text()
            .context("URL input cancelled")?;
        config.llm.base_url = Some(base_url);
        println!("  → No API key needed for Ollama");
    } else {
        let api_key = Password::new()
            .with_prompt(format!("Step 2/4 — Enter {} API key", PROVIDERS[provider_idx]))
            .interact()
            .context("API key input cancelled")?;

        if api_key.is_empty() {
            println!("  ⚠ No API key provided — you can set it later with environment variable");
        } else {
            // Store in OS keyring
            match KeyStore::set(&provider, &api_key) {
                Ok(()) => println!("  → API key stored in OS credential manager ✓"),
                Err(e) => {
                    tracing::warn!("Keyring storage failed: {}", e);
                    println!("  ⚠ Keyring unavailable, storing in config.toml (less secure)");
                    config.llm.api_key = Some(api_key);
                }
            }
        }
    }
    println!();

    // ── Step 3: Connectivity Test ───────────────────────
    println!("Step 3/6 — Connectivity test");
    test_llm_connectivity(&provider).await?;
    println!();

    // ── Step 4: Create Directory Structure ──────────────
    println!("Step 4/6 — Creating directory structure");
    let config_dir = Config::dir()?;
    let dirs_to_create = [
        config_dir.clone(),
        config_dir.join("memory"),
        config_dir.join("skills"),
        config_dir.join("logs"),
        config_dir.join("sessions"),
        config_dir.join("data"),
        config_dir.join("vectors"),
        config_dir.join("prompt_templates"),
    ];
    for dir in &dirs_to_create {
        std::fs::create_dir_all(dir)
            .with_context(|| format!("Failed to create {}", dir.display()))?;
        println!("  → {}", dir.display());
    }

    let project_evocli = std::path::Path::new(".evocli");
    let project_dirs = [
        project_evocli.to_path_buf(),
        project_evocli.join("skills"),
    ];
    for dir in &project_dirs {
        if let Err(e) = std::fs::create_dir_all(dir) {
            tracing::warn!("Could not create project dir {}: {}", dir.display(), e);
        } else {
            println!("  → {}", dir.display());
        }
    }
    println!();

    // ── Step 5 (v3.x): Managed Python Environment ───────
    println!("Step 5/6 — Setting up managed Python runtime (uv)");
    println!("  This installs a private Python 3.11 — independent of system Python.");
    // B3 FIX: 使用 exe 相对路径查找 soul dir，适配 dist/ 场景
    let soul_exe   = crate::find_soul_dir_relative_to_exe();
    let soul_cwd   = std::path::PathBuf::from("evocli-soul");
    let soul_arg: Option<&std::path::Path> = soul_exe.as_ref()
        .filter(|p| p.exists())
        .map(|p| p.as_path())
        .or_else(|| if soul_cwd.exists() { Some(&soul_cwd) } else { None });
    match crate::python_manager::PythonManager::setup(soul_arg) {
        Ok(py) => {
            config.soul_script = Some("evocli_soul.main".to_string());
            println!("  ✓ Python ready: {}", py.display());
        }
        Err(e) => {
            tracing::warn!("Managed Python setup failed: {}", e);
            println!("  ⚠ Managed Python setup failed: {}", e);
            println!("  → Falling back to system Python. Run `evocli init` again after");
            println!("    installing uv: https://docs.astral.sh/uv/getting-started/installation/");
            // Fallback to auto-detected path
            let detected_soul = crate::config::resolve_soul_path();
            config.soul_script = Some(detected_soul.clone());
            println!("  → Soul path: {}", detected_soul);
        }
    }
    println!();

    // ── Step 6: Download Embedding Model ────────────────
    println!("Step 6/6 — Pre-downloading embedding model for vector memory");
    println!("  Model: jinaai/jina-embeddings-v2-base-zh (~570 MB, one-time)");
    println!("  Mirror: hf-mirror.com (auto-enabled when HF_ENDPOINT not set)");
    println!();
    download_embedding_model();
    println!();

    // ── Save Config ─────────────────────────────────────
    config.save()?;
    println!("  ✓ Config saved to {}", Config::path()?.display());
    println!();

    // ── FIX-3: 首次验证流程 ──────────────────────────────
    println!("  Verifying installation...");
    let soul_path = crate::config::resolve_soul_path();
    let verification_ok = run_verification_check(&soul_path);
    println!();

    // ── 完成引导 ─────────────────────────────────────────
    println!("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");
    if verification_ok {
        println!("  ✅  EvoCLI is ready!");
    } else {
        println!("  ⚠️  Setup complete with warnings (see above).");
    }
    println!();
    println!("  Quick start:");
    println!("    evocli doctor         → full health check (10 items)");
    println!("    evocli index          → index project code symbols");
    println!("    evocli skill list     → see 5 built-in skills");
    println!("    evocli config explain → understand all config fields");
    println!("    evocli               → start AI coding session");
    println!();
    println!("  Config file:  {}", Config::path()?.display());
    println!("  Project rules: create AGENTS.md in your project root");
    println!("  (see docs/AGENTS.md.example for template)");
    println!();

    Ok(())
}

/// FIX-3: 启动 Soul 子进程，发送 tracer.ping，验证整个链路可通
fn run_verification_check(soul_path: &str) -> bool {
    use std::io::Write;

    let python_exe = crate::python_manager::PythonManager::get_python_exe();
    let py = python_exe.to_string_lossy().to_string();

    // 构建启动参数（与 SoulBridge::spawn 相同逻辑）
    let (args, pythonpath): (Vec<String>, Option<String>) = if soul_path.ends_with(".py") {
        let p = std::path::Path::new(soul_path);
        if let (Some(pkg_dir), Some(stem)) = (p.parent(), p.file_stem()) {
            let module = format!("{}.{}", pkg_dir.file_name().and_then(|n| n.to_str()).unwrap_or("evocli_soul"), stem.to_str().unwrap_or("main"));
            let pp = pkg_dir.parent().and_then(|pp| std::fs::canonicalize(pp).ok()).map(|pb| pb.to_string_lossy().to_string());
            (vec!["-u".into(), "-m".into(), module], pp)
        } else {
            (vec!["-u".into(), soul_path.to_string()], None)
        }
    } else {
        (vec!["-u".into(), "-m".into(), soul_path.to_string()], None)
    };

    let mut cmd = std::process::Command::new(&py);
    cmd.args(args.iter().map(|s| s.as_str()))
       .stdin(std::process::Stdio::piped())
       .stdout(std::process::Stdio::piped())
       .stderr(std::process::Stdio::piped())
       .env("PYTHONIOENCODING", "utf-8");
    if let Some(ref pp) = pythonpath {
        cmd.env("PYTHONPATH", pp);
    }

    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => {
            println!("  ✗  Could not start Python Soul: {}", e);
            println!("    Run `evocli doctor` for details.");
            return false;
        }
    };

    // 发送 tracer.ping
    let ping = r#"{"id":"verify-1","method":"tracer.ping","params":{}}"#;
    if let Some(ref mut stdin) = child.stdin {
        let _ = stdin.write_all(ping.as_bytes());
        let _ = stdin.write_all(b"\n");
        let _ = stdin.flush();
    }

    // 等待响应（最多 8 秒）
    let start = std::time::Instant::now();
    let timeout = std::time::Duration::from_secs(8);
    let mut buf = String::new();
    let mut ok = false;

    if let Some(ref mut stdout) = child.stdout {
        use std::io::BufRead;
        let reader = std::io::BufReader::new(stdout);
        for line in reader.lines() {
            if start.elapsed() > timeout { break; }
            if let Ok(line) = line {
                buf.push_str(&line);
                if line.contains("pong") || line.contains("\"result\"") {
                    ok = true;
                    break;
                }
            }
        }
    }

    let _ = child.kill();
    let _ = child.wait();

    if ok {
        println!("  ✓  Soul connected and responding");
    } else {
        // char-based truncation: Soul output may contain non-ASCII characters in error messages
        let buf_display: String = buf.chars().take(100).collect();
        println!("  ⚠  Soul did not respond in time (response: {:?})", buf_display);
        println!("    Run `evocli doctor` to diagnose.");
    }
    ok
}

/// Step 6: Pre-download the fastembed embedding model.
///
/// Runs `download_models.py` (next to the binary, or inside the dist package)
/// using the managed Python.  If the script is not found, falls back to an
/// inline Python one-liner so init always works even from a dev checkout.
fn download_embedding_model() {
    let python_exe = crate::python_manager::PythonManager::get_python_exe();
    let py = python_exe.to_string_lossy().to_string();

    // Locate download_models.py: next to the running binary, or next to CWD.
    let script: Option<std::path::PathBuf> = std::env::current_exe().ok()
        .and_then(|exe| exe.parent().map(|p| p.join("download_models.py")))
        .filter(|p| p.exists())
        .or_else(|| {
            let cwd = std::path::PathBuf::from("download_models.py");
            if cwd.exists() { Some(cwd) } else { None }
        })
        .or_else(|| {
            // Also try scripts/ subdir (developer checkout)
            let dev = std::path::PathBuf::from("scripts/download_models.py");
            if dev.exists() { Some(dev) } else { None }
        });

    let status = if let Some(ref script_path) = script {
        println!("  Script: {}", script_path.display());
        std::process::Command::new(&py)
            .arg(script_path)
            .env("PYTHONIOENCODING", "utf-8")
            .status()
    } else {
        // Fallback inline script — no file needed
        println!("  (running inline download)");
        let inline = r#"
import os, sys, pathlib
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
import importlib.util
if not importlib.util.find_spec("fastembed"):
    print("  skipped (fastembed not installed — run setup.ps1 first)")
    sys.exit(0)
from fastembed import TextEmbedding
cache = str(pathlib.Path.home() / ".evocli" / "models")
print(f"  downloading to {cache} ...")
m = TextEmbedding("jinaai/jina-embeddings-v2-base-zh", cache_dir=cache)
list(m.embed(["ok"]))
print("  done")
"#;
        std::process::Command::new(&py)
            .args(["-c", inline])
            .env("PYTHONIOENCODING", "utf-8")
            .status()
    };

    match status {
        Ok(s) if s.success() => {
            println!("  ✓  Embedding model ready");
        }
        Ok(s) => {
            println!("  ⚠  Download exited with code {:?}", s.code());
            println!("     EvoCLI works with text search. Retry:");
            if let Some(ref p) = script {
                println!("       {} {}", py, p.display());
            }
        }
        Err(e) => {
            println!("  ⚠  Could not run download: {}", e);
            println!("     Text search will be used until model is available.");
        }
    }
}


fn default_tiers_for(provider: &str) -> LlmTiers {
    match provider {
        "anthropic" => LlmTiers {
            fast: "claude-3-5-haiku-latest".into(),
            smart: "claude-sonnet-4-5-20250514".into(),
        },
        "openai" => LlmTiers {
            fast: "gpt-4o-mini".into(),
            smart: "gpt-4o".into(),
        },
        "deepseek" => LlmTiers {
            fast: "deepseek-chat".into(),
            smart: "deepseek-reasoner".into(),
        },
        "ollama" => LlmTiers {
            fast: "qwen2.5-coder:7b".into(),
            smart: "qwen2.5-coder:32b".into(),
        },
        _ => LlmTiers::default(),
    }
}

/// Step 3: LLM API connectivity and key validation.
///
/// Two-phase check:
/// 1. TCP connect (proves network path is open)
/// 2. Minimal API call to validate the key (proves auth works)
///
/// The key validation uses the cheapest possible call:
///   Anthropic / OpenAI: GET /v1/models (no tokens consumed)
///   DeepSeek:           GET /v1/models
///
/// Failure in phase 2 shows a clear "invalid API key" message instead of letting
/// the user discover the problem on their first real agent call.
async fn test_llm_connectivity(provider: &str) -> Result<()> {
    use std::time::Duration;

    print!("  Testing {} connectivity... ", provider);

    if provider == "ollama" {
        println!("⏭  Skipped (Ollama is local)");
        return Ok(());
    }

    let host = match provider {
        "anthropic" => "api.anthropic.com",
        "openai"    => "api.openai.com",
        "deepseek"  => "api.deepseek.com",
        _           => { println!("⏭  Unknown provider, skipped"); return Ok(()); }
    };

    // Phase 1: TCP connect
    let addr = format!("{}:443", host);
    match tokio::time::timeout(
        Duration::from_secs(5),
        tokio::net::TcpStream::connect(&addr),
    ).await {
        Ok(Ok(_))  => {},
        Ok(Err(e)) => {
            println!("❌ (TCP connect failed: {})", e);
            return Err(anyhow::anyhow!("Cannot reach {} — check network", host));
        }
        Err(_) => {
            println!("⚠️  TCP timeout — continuing (may work behind proxy)");
            return Ok(());
        }
    }

    println!("✓ (TCP)");
    print!("  Validating API key...   ");

    // Phase 2: Real API call to validate key
    // Read key from keyring or environment
    let api_key = {
        let from_keyring = crate::keystore::KeyStore::get(provider)
            .ok()
            .flatten();
        from_keyring.or_else(|| {
            let env_var = match provider {
                "anthropic" => "ANTHROPIC_API_KEY",
                "openai"    => "OPENAI_API_KEY",
                "deepseek"  => "DEEPSEEK_API_KEY",
                _           => return None,
            };
            std::env::var(env_var).ok()
        })
    };

    let Some(key) = api_key else {
        println!("⏭  No key stored yet (set later with evocli init or env var)");
        return Ok(());
    };

    // Use reqwest if available; otherwise skip
    let url = match provider {
        "anthropic" => "https://api.anthropic.com/v1/models",
        "openai"    => "https://api.openai.com/v1/models",
        "deepseek"  => "https://api.deepseek.com/v1/models",
        _           => { println!("⏭  Skipped"); return Ok(()); }
    };

    // Build HTTP request manually (avoid reqwest dependency — use std TcpStream + rustls if available)
    // Simpler: run a minimal curl/powershell check, or just trust the TCP succeeded.
    // For now, use a basic HTTP/1.1 request via tokio to avoid new Cargo dependencies.
    let auth_header = if provider == "anthropic" {
        format!("x-api-key: {}\r\nanthropic-version: 2023-06-01", key)
    } else {
        format!("Authorization: Bearer {}", key)
    };

    // Use Python Soul to make the actual test call (avoids adding reqwest to host)
    // We do this via a subcommand that spawns a quick Python one-liner
    let py_check = format!(
        r#"import urllib.request, sys; req=urllib.request.Request('{}',headers={{{}}}); \
           resp=urllib.request.urlopen(req,timeout=8); \
           sys.exit(0 if resp.status==200 else 1)"#,
        url,
        if provider == "anthropic" {
            format!("'x-api-key':'{}','anthropic-version':'2023-06-01'", key)
        } else {
            format!("'Authorization':'Bearer {}'", key)
        }
    );

    let result = tokio::process::Command::new("python3")
        .args(["-c", &py_check.replace('\n', " ")])
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status()
        .await;

    // Also try `python` on Windows
    let result = if result.map(|s| s.success()).unwrap_or(false) {
        Ok(true)
    } else {
        tokio::process::Command::new("python")
            .args(["-c", &py_check.replace('\n', " ")])
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .status()
            .await
            .map(|s| s.success())
    };

    match result {
        Ok(true) => {
            println!("✅ API key is valid");
        }
        Ok(false) => {
            println!("❌ API key rejected (401/403)");
            println!();
            println!("  Your API key was stored but appears to be invalid.");
            println!("  Double-check it at {} dashboard.", host);
            println!("  You can update it later with: evocli init");
            // Don't bail — let user continue and fix the key later
        }
        Err(_) => {
            println!("⏭  Python not available for key validation — skipped");
        }
    }

    Ok(())
}

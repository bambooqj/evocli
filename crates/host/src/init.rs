//! Init wizard — `evocli init` interactive setup
//!
//! Asks for exactly what's needed:
//!   1. Base URL  — any OpenAI-compatible endpoint
//!   2. API Key   — stored in OS keyring
//!   3. Model names (fast + smart) — you fill in whatever your endpoint provides
//!   4. Directory structure
//!   5. Managed Python environment
//!   6. AGENTS.md project rules

use anyhow::{Context, Result};
use dialoguer::{Input, Password};

use crate::config::{Config, LlmTiers};
use crate::keystore::KeyStore;

/// Generate a starter AGENTS.md template for the current project.
///
/// The AI reads this file automatically at the start of every session.
/// Includes AI Programming Bible 3.1 engineering principles that guide
/// how the AI should behave when developing this specific project.
///
/// Detects project type (Rust / Python / Node) for language-specific rules.
fn generate_agents_md_template() -> String {
    let is_rust   = std::path::Path::new("Cargo.toml").exists();
    let is_python = std::path::Path::new("pyproject.toml").exists()
        || std::path::Path::new("setup.py").exists();
    let is_node   = std::path::Path::new("package.json").exists();

    let lang_rules = if is_rust {
        "- Always use `anyhow::Result` for error handling\n\
         - Never use `.unwrap()` — use `?` or `.context()`\n\
         - Prefer `Arc<T>` over `Rc<T>` for shared ownership\n\
         - Run `cargo clippy` after every change\n"
    } else if is_python {
        "- Use type hints on all function signatures\n\
         - Prefer `pathlib.Path` over `os.path`\n\
         - Use `logging` not `print()` in library code\n\
         - All async functions must be properly awaited\n"
    } else if is_node {
        "- Use `const` and `let`, never `var`\n\
         - Prefer `async/await` over callbacks\n\
         - Use TypeScript strict mode\n\
         - Run `npm run lint` after every change\n"
    } else {
        "- Follow existing code style\n\
         - Run tests after every change\n"
    };

    let test_cmd = if is_rust   { "cargo test" }
                   else if is_python { "pytest" }
                   else if is_node   { "npm test" }
                   else              { "see README" };

    format!(
        "# AGENTS.md — Project Rules for EvoCLI\n\
         #\n\
         # This file is read by the AI at the start of every session.\n\
         # Add project-specific rules, conventions, and constraints here.\n\
         \n\
         ## Engineering Principles (AI Programming Bible 3.1)\n\
         #\n\
         # These constraints apply to ALL code generated for this project.\n\
         # The AI will follow them automatically when the bible-engineering skill is active.\n\
         \n\
         - **Rule 0 (Zero-Debt)**: This project has no legacy users. Boldly rewrite at the source.\n\
           Do NOT add compatibility layers or deprecated fallback paths.\n\
         - **Rule 2 (Decoupling)**: Every new feature in its own file. One file = one responsibility.\n\
         - **Rule 3 (Protocol First)**: Define Pydantic/TypeScript/Rust schemas BEFORE business logic.\n\
         - **Rule 8 (Defensive)**: All external inputs validated. All async calls have timeouts.\n\
         - **Rule 9 (Docs + Limit)**: Every public function has a docstring. Files under 2000 lines.\n\
           Verify with: `python evocli-soul/scripts/bible_check.py .`\n\
         - **Rule 10 (Observability)**: Structured logs at every critical state change.\n\
           Use `trace.get_logger()` or your project's structured logger.\n\
         \n\
         ## Code Style\n\
         {}\n\
         ## Architecture\n\
         - (Describe key architectural decisions here)\n\
         - (E.g.: \"This project uses repository pattern — no direct DB calls in services\")\n\
         \n\
         ## Forbidden\n\
         - Never commit directly to main/master\n\
         - Never delete tests to make builds pass\n\
         - Never use `TODO` comments without a linked issue\n\
         - Never use bare `except: pass` — always log or re-raise\n\
         \n\
         ## Testing\n\
         - Always run tests before declaring a task complete (bible-engineering Rule 6)\n\
         - Test command: {test_cmd}\n\
         \n\
         ## Naming Conventions\n\
         - (Describe naming conventions here)\n\
         \n\
         ## Notes\n\
         - (Add any other context the AI should know about this project)\n",
        lang_rules,
        test_cmd = test_cmd,
    )
}

/// Run the init wizard
pub async fn run_init() -> Result<()> {
    println!();
    println!("  EvoCLI Setup Wizard");
    println!("  ===================");
    println!();
    println!("  Fill in your LLM endpoint details.");
    println!("  Any OpenAI-compatible API works (OpenAI, DeepSeek, Groq, SiliconFlow,");
    println!("  local Ollama, custom proxy, etc.).");
    println!();

    let mut config = Config::load_or_default()?;

    // ── Step 1: Base URL ──────────────────────────────────
    println!("[1/6] API Endpoint");
    println!("  Common endpoints:");
    println!("    OpenAI:      https://api.openai.com/v1");
    println!("    DeepSeek:    https://api.deepseek.com/v1");
    println!("    Groq:        https://api.groq.com/openai/v1");
    println!("    SiliconFlow: https://api.siliconflow.cn/v1");
    println!("    Ollama:      http://localhost:11434");
    println!("    Anthropic:   (leave blank — detected from model name)");
    println!();

    let current_url = config.llm.base_url.as_deref().unwrap_or("");
    let base_url: String = Input::new()
        .with_prompt("  Base URL (leave blank for OpenAI default)")
        .default(current_url.to_string())
        .allow_empty(true)
        .interact_text()
        .context("URL input cancelled")?;

    config.llm.base_url = if base_url.trim().is_empty() {
        None // litellm will use each model's default endpoint
    } else {
        Some(base_url.trim().to_string())
    };
    println!(
        "  ✓  Endpoint: {}",
        config
            .llm
            .base_url
            .as_deref()
            .unwrap_or("(auto from model name)")
    );
    println!();

    // ── Step 2: API Key ───────────────────────────────────
    println!("[2/6] API Key");
    println!("  Stored securely in OS credential manager (not in config.toml).");
    println!("  Leave blank to configure later via environment variable.");
    println!();

    // Detect a reasonable service name for keyring storage
    let keyring_service = config
        .llm
        .base_url
        .as_deref()
        .and_then(|url| url.split("//").nth(1))
        .and_then(|host| host.split('/').next())
        .unwrap_or("default");

    let api_key = Password::new()
        .with_prompt(format!("  API Key for {}", keyring_service))
        .allow_empty_password(true)
        .interact()
        .context("API key input cancelled")?;

    if !api_key.is_empty() {
        match KeyStore::set(keyring_service, &api_key) {
            Ok(()) => println!("  ✓  API key stored in OS credential manager"),
            Err(e) => {
                tracing::warn!("Keyring storage failed: {}", e);
                println!("  ⚠  Keyring unavailable, storing in config.toml (less secure)");
                config.llm.api_key = Some(api_key);
            }
        }
    } else {
        println!("  ⚠  No key provided — set it later via environment variable or re-run init");
    }
    println!();

    // ── Step 3: Model Names ───────────────────────────────
    println!("[3/6] Model Names");
    println!("  Enter the model names your endpoint provides.");
    println!("  'fast' = cheap/quick (edits, commits, summaries)");
    println!("  'smart' = powerful (architecture, code review, planning)");
    println!();

    let fast_prompt = format!(
        "  Fast model{}",
        if config.llm.tiers.fast.is_empty() {
            " (e.g. gpt-4o-mini, deepseek-chat, qwen2.5-coder:7b)".to_string()
        } else {
            format!(" [{}]", config.llm.tiers.fast)
        }
    );
    let fast: String = Input::new()
        .with_prompt(fast_prompt)
        .default(config.llm.tiers.fast.clone())
        .allow_empty(false)
        .interact_text()
        .context("Fast model input cancelled")?;

    let smart_prompt = format!(
        "  Smart model{}",
        if config.llm.tiers.smart.is_empty() {
            " (e.g. gpt-4o, deepseek-reasoner, qwen2.5-coder:32b)".to_string()
        } else {
            format!(" [{}]", config.llm.tiers.smart)
        }
    );
    let smart: String = Input::new()
        .with_prompt(smart_prompt)
        .default(config.llm.tiers.smart.clone())
        .allow_empty(false)
        .interact_text()
        .context("Smart model input cancelled")?;

    config.llm.tiers = LlmTiers {
        fast: fast.trim().to_string(),
        smart: smart.trim().to_string(),
    };
    println!("  ✓  fast  = {}", config.llm.tiers.fast);
    println!("  ✓  smart = {}", config.llm.tiers.smart);
    println!();

    // ── Step 3.5: Test connectivity ───────────────────────
    println!("[3.5/6] Testing connectivity...");
    let endpoint_host = config.llm.base_url.as_deref().unwrap_or("api.openai.com");
    let host = endpoint_host
        .trim_start_matches("https://")
        .trim_start_matches("http://")
        .split('/')
        .next()
        .unwrap_or(endpoint_host);
    test_llm_connectivity_simple(host).await;
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
    let project_dirs = [project_evocli.to_path_buf(), project_evocli.join("skills")];
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
    let soul_exe = crate::find_soul_dir_relative_to_exe();
    let soul_cwd = std::path::PathBuf::from("evocli-soul");
    let soul_arg: Option<&std::path::Path> = soul_exe
        .as_ref()
        .filter(|p| p.exists())
        .map(|p| p.as_path())
        .or_else(|| {
            if soul_cwd.exists() {
                Some(&soul_cwd)
            } else {
                None
            }
        });
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

    // ── Step 6: AGENTS.md project rules ─────────────────────────────
    // AGENTS.md is the most impactful single config a user can create.
    // It tells the AI what NOT to do, naming conventions, forbidden libs, etc.
    // Without it, the AI guesses conventions and often gets them wrong.
    println!("Step 6/6 — Project rules (AGENTS.md)");
    let agents_md = std::path::Path::new("AGENTS.md");
    if agents_md.exists() {
        println!("  ✓ AGENTS.md already exists — AI will read it automatically.");
    } else {
        println!("  AGENTS.md tells the AI about your project's conventions.");
        println!("  Example rules: 'Always use anyhow::Result', 'No unwrap()', 'Follow SOLID principles'.");
        println!();

        let create = dialoguer::Confirm::new()
            .with_prompt("  Create AGENTS.md template now? (recommended)")
            .default(true)
            .interact()
            .unwrap_or(true);

        if create {
            let template = generate_agents_md_template();
            match std::fs::write("AGENTS.md", &template) {
                Ok(()) => {
                    println!("  ✓ Created AGENTS.md — edit it to add your project rules.");
                    println!(
                        "  → Location: {}",
                        std::env::current_dir()
                            .map(|d| d.join("AGENTS.md").display().to_string())
                            .unwrap_or("AGENTS.md".into())
                    );
                }
                Err(e) => {
                    println!("  ⚠ Could not create AGENTS.md: {}", e);
                    println!("  → Create it manually — see docs/AGENTS.md.example for template.");
                }
            }
        } else {
            println!("  Skipped. Create AGENTS.md later to improve AI code quality.");
            println!("  → Template: docs/AGENTS.md.example");
        }
    }
    println!();

    // ── FIX-3: 首次验证流程 ──────────────────────────────────────
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
            let module = format!(
                "{}.{}",
                pkg_dir
                    .file_name()
                    .and_then(|n| n.to_str())
                    .unwrap_or("evocli_soul"),
                stem.to_str().unwrap_or("main")
            );
            let pp = pkg_dir
                .parent()
                .and_then(|pp| std::fs::canonicalize(pp).ok())
                .map(|pb| pb.to_string_lossy().to_string());
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
            if start.elapsed() > timeout {
                break;
            }
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
        println!(
            "  ⚠  Soul did not respond in time (response: {:?})",
            buf_display
        );
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
    let script: Option<std::path::PathBuf> = std::env::current_exe()
        .ok()
        .and_then(|exe| exe.parent().map(|p| p.join("download_models.py")))
        .filter(|p| p.exists())
        .or_else(|| {
            let cwd = std::path::PathBuf::from("download_models.py");
            if cwd.exists() {
                Some(cwd)
            } else {
                None
            }
        })
        .or_else(|| {
            // Also try scripts/ subdir (developer checkout)
            let dev = std::path::PathBuf::from("scripts/download_models.py");
            if dev.exists() {
                Some(dev)
            } else {
                None
            }
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

/// Simple TCP connectivity test — just checks if the host is reachable.
/// We no longer do provider-specific validation here; users verify on first use.
async fn test_llm_connectivity_simple(host: &str) {
    use std::time::Duration;
    print!("  Testing connection to {}... ", host);

    // Skip localhost (Ollama, LM Studio, etc.)
    if host.starts_with("localhost") || host.starts_with("127.") || host.starts_with("0.0.0.0") {
        println!("⏭  local endpoint (skipped)");
        return;
    }

    let addr = format!("{}:443", host.split(':').next().unwrap_or(host));
    match tokio::time::timeout(
        Duration::from_secs(5),
        tokio::net::TcpStream::connect(&addr),
    )
    .await
    {
        Ok(Ok(_)) => println!("✓  reachable"),
        Ok(Err(_)) => println!("⚠  cannot connect (check network/VPN)"),
        Err(_) => println!("⚠  timeout (may work behind proxy — continuing)"),
    }
}

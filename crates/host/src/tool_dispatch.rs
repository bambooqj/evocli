//! tool_dispatch.rs — Capability Contract 工具调度器
//!
//! 当 Python Soul 发送 tool.call 请求时，此模块将其分发到对应的 Rust 实现。
//! 这是 HostBridge.call() 在 Rust 侧的对应处理器。

use crate::{fs_tools, git, security::SecurityController, web_tools};
use anyhow::Result;
use knowledge_graph::{Bm25Index, KnowledgeGraph};
use serde_json::Value;
use soul_bridge::{SoulBridge, ToolCallRequest};
use std::path::PathBuf;

// ── CodeIndex 连接缓存 ────────────────────────────────────────────────────────
//
// 之前的实现：每次 code_intel.* 工具调用都做 CodeIndex::new(&db_path)，
// 即每次都重新打开 SQLite 连接 + 运行 migration DDL，14+ 处重复。
//
// 修复：进程级 HashMap<PathBuf, Arc<Mutex<CodeIndex>>> 缓存。
// 同一 project 的第二次及后续调用直接复用已打开的连接，
// Arc<Mutex> 保证多 tokio task 并发访问安全。

use std::collections::HashMap;
use std::sync::{Arc, Mutex, OnceLock};

type CodeIndexCache = Mutex<HashMap<PathBuf, Arc<Mutex<code_intel::CodeIndex>>>>;

static CODE_INDEX_CACHE: OnceLock<CodeIndexCache> = OnceLock::new();

fn get_code_index_cache() -> &'static CodeIndexCache {
    CODE_INDEX_CACHE.get_or_init(|| Mutex::new(HashMap::new()))
}

/// 获取（或创建并缓存）指定路径的 CodeIndex。
/// 不存在的 db 文件返回 None（不强制创建）。
fn cached_code_index(db_path: &PathBuf) -> Option<Arc<Mutex<code_intel::CodeIndex>>> {
    if !db_path.exists() {
        return None;
    }
    let cache = get_code_index_cache();
    let mut map = cache.lock().unwrap();
    if let Some(idx) = map.get(db_path) {
        return Some(Arc::clone(idx));
    }
    match code_intel::CodeIndex::new(db_path) {
        Ok(idx) => {
            let arc = Arc::new(Mutex::new(idx));
            map.insert(db_path.clone(), Arc::clone(&arc));
            Some(arc)
        }
        Err(e) => {
            tracing::warn!("CodeIndex open failed for {}: {}", db_path.display(), e);
            None
        }
    }
}
/// 从连接缓存获取 CodeIndex 并执行操作，db 不存在时返回 `fallback`。
///
/// 替代之前每次工具调用都 `CodeIndex::new(&db_path)` 的模式（14+ 处重复）。
macro_rules! with_code_index {
    ($db_path:expr, fallback = $fallback:expr, |$idx:ident| $body:expr) => {{
        match cached_code_index(&$db_path) {
            Some(arc) => {
                #[allow(unused_mut)]
                let mut $idx = arc.lock().unwrap();
                Ok($body)
            }
            None => Ok($fallback),
        }
    }};
}

/// 处理 Python Soul 发来的工具调用请求，返回结果。
/// `bridge` — Some(&SoulBridge) in TUI mode (enables approval modal), None in CLI/test.
pub async fn dispatch(
    req: &ToolCallRequest,
    bridge: Option<&SoulBridge>,
    cfg: &crate::config::Config,
) -> Result<Value> {
    let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    let security = SecurityController::new(&cfg.security);

    // Initialize tools crate security lists from the fully-parsed config.
    // This ensures tools::run_command uses config-driven lists, not hardcoded arrays.
    // Called on every dispatch (OnceLock ensures it only runs once per process).
    {
        use crate::config::{default_allowed_commands, default_blocked_patterns};
        let mut allowed = cfg.security.allowed_commands.clone();
        allowed.extend(cfg.security.extra_allowed_commands.iter().cloned());
        let mut blocked = cfg.security.blocked_patterns.clone();
        blocked.extend(cfg.security.extra_blocked_patterns.iter().cloned());
        // init_security is idempotent (OnceLock — only first call takes effect)
        tools::init_security(
            if allowed.is_empty() {
                default_allowed_commands()
            } else {
                allowed
            },
            if blocked.is_empty() {
                default_blocked_patterns()
            } else {
                blocked
            },
        );
    }

    let args = &req.args;

    match req.tool.as_str() {
        // ── 文件系统工具 ──────────────────────────────────────
        // ── fs.* 工具（受 SecurityController 路径访问控制）──────
        "fs.read" => {
            if let Some(p) = args["path"].as_str() {
                security.validate_path_access(&std::path::Path::new(p))?;
                security.audit_log("fs.read", p, true);
            }
            fs_tools::fs_read(args)
        }
        "fs.read_range" => {
            if let Some(p) = args["path"].as_str() {
                security.validate_path_access(&std::path::Path::new(p))?;
                security.audit_log("fs.read_range", p, true);
            }
            fs_tools::fs_read_range(args)
        }
        "fs.write" => {
            if let Some(p) = args["path"].as_str() {
                security.validate_path_access(&std::path::Path::new(p))?;
                // P4: Path containment check — warn when writing outside project root.
                // The process CWD is frozen at evocli startup (never changed by Rust),
                // so `cwd` here is always the project root the user launched from.
                // Writing outside it is almost always a bug (directory drift).
                // We warn rather than block to avoid breaking legitimate writes to ~/.evocli/.
                let write_path = PathBuf::from(p);
                let abs_write_path = if write_path.is_absolute() {
                    write_path.clone()
                } else {
                    cwd.join(&write_path)
                };
                // Allow: inside project root OR inside ~/.evocli/ (evocli internal state)
                let home_evocli = std::env::var("HOME")
                    .or_else(|_| std::env::var("USERPROFILE"))
                    .map(|h| PathBuf::from(h).join(".evocli"))
                    .unwrap_or_else(|_| PathBuf::from(".evocli"));
                if !abs_write_path.starts_with(&cwd) && !abs_write_path.starts_with(&home_evocli) {
                    tracing::warn!(
                        "[fs.write] Path '{}' is outside project root '{}'. \
                         This may be a directory drift bug. \
                         Ensure the AI always uses absolute paths anchored to the project root.",
                        abs_write_path.display(),
                        cwd.display()
                    );
                }
                security.audit_log("fs.write", p, true);
            }
            fs_tools::fs_write(args)
        }
        "fs.diff" => fs_tools::fs_diff(args),
        "fs.apply_diff" => {
            if let Some(p) = args["path"].as_str() {
                security.validate_path_access(&std::path::Path::new(p))?;
                // P4: same path containment check as fs.write
                let write_path = PathBuf::from(p);
                let abs_write_path = if write_path.is_absolute() {
                    write_path.clone()
                } else {
                    cwd.join(&write_path)
                };
                let home_evocli = std::env::var("HOME")
                    .or_else(|_| std::env::var("USERPROFILE"))
                    .map(|h| PathBuf::from(h).join(".evocli"))
                    .unwrap_or_else(|_| PathBuf::from(".evocli"));
                if !abs_write_path.starts_with(&cwd) && !abs_write_path.starts_with(&home_evocli) {
                    tracing::warn!(
                        "[fs.apply_diff] Path '{}' is outside project root '{}'.",
                        abs_write_path.display(),
                        cwd.display()
                    );
                }
                security.audit_log("fs.apply_diff", p, true);
            }
            // Use spawn_blocking: fs_apply_diff can run external formatter/test suite
            // (run_format / run_tests optional params) — must not block tokio executor.
            let args_owned = args.clone();
            tokio::task::spawn_blocking(move || fs_tools::fs_apply_diff(&args_owned))
                .await
                .map_err(|e| anyhow::anyhow!("spawn_blocking join error: {}", e))?
        }

        // ── Git 工具 ─────────────────────────────────────────
        "git.status" => {
            let entries = git::git_status(&cwd)?;
            Ok(serde_json::to_value(entries)?)
        }
        "git.diff" => {
            let path = args["path"].as_str().unwrap_or("");
            let stat = args["stat"].as_bool().unwrap_or(false);
            let base = args["base"].as_str().unwrap_or("");
            // staged: true=staged only, false=unstaged only, absent=both
            let staged = if args["staged"].is_null() {
                None
            } else {
                args["staged"].as_bool()
            };
            let diff = git::git_diff_ext(&cwd, path, staged, stat, base)?;
            Ok(Value::String(diff))
        }
        "git.commit" => {
            let message = args["message"].as_str().unwrap_or("evocli auto commit");
            let files: Vec<String> = args["files"]
                .as_array()
                .map(|a| {
                    a.iter()
                        .filter_map(|v| v.as_str().map(|s| s.to_string()))
                        .collect()
                })
                .unwrap_or_default();
            let hash = git::git_commit(&cwd, message, &files)?;
            Ok(serde_json::json!({ "hash": hash }))
        }
        "git.snapshot" => {
            let hash = git::git_snapshot(&cwd)?;
            Ok(serde_json::json!({ "stash_ref": hash }))
        }
        "git.restore" => {
            let stash_ref = args["stash_ref"].as_str().unwrap_or("stash@{0}");
            git::git_restore(&cwd, stash_ref)?;
            Ok(serde_json::json!({ "ok": true }))
        }
        "git.shadow_snapshot" => {
            let label = args["label"].as_str().unwrap_or("auto");
            let project = args["project"]
                .as_str()
                .map(PathBuf::from)
                .unwrap_or(cwd.clone());
            let hash = git::shadow_snapshot(&project, label)?;
            Ok(serde_json::json!({ "hash": hash }))
        }
        "git.shadow_restore" => {
            let snapshot = args["snapshot"].as_str().unwrap_or("");
            let project = args["project"]
                .as_str()
                .map(PathBuf::from)
                .unwrap_or(cwd.clone());
            git::shadow_restore(&project, snapshot)?;
            Ok(serde_json::json!({ "ok": true }))
        }
        "git.snapshot_list" => {
            let project = args["project"]
                .as_str()
                .map(PathBuf::from)
                .unwrap_or(cwd.clone());
            let limit = args["limit"].as_u64().unwrap_or(20) as usize;
            match git::shadow_log(&project, limit) {
                Ok(entries) => Ok(serde_json::to_value(entries)?),
                Err(_) => Ok(serde_json::json!([])), // no snapshots yet = empty list
            }
        }
        "git.snapshot_restore" => {
            let snapshot_ref = args["ref"].as_str().unwrap_or("");
            let project = args["project"]
                .as_str()
                .map(PathBuf::from)
                .unwrap_or(cwd.clone());
            if snapshot_ref.is_empty() {
                return Ok(serde_json::json!({"ok": false, "error": "ref is required"}));
            }
            git::shadow_restore(&project, snapshot_ref)?;
            Ok(serde_json::json!({ "ok": true, "restored": snapshot_ref }))
        }

        // ── Shell 工具 ───────────────────────────────────────
        "shell.run" => {
            let cmd_owned = args["cmd"].as_str().unwrap_or("").to_string();
            let work_dir = args["cwd"]
                .as_str()
                .map(PathBuf::from)
                .unwrap_or(cwd.clone());
            let timeout_s = args["timeout_s"].as_u64().unwrap_or(30) as u32;
            let dry_run = args["dry_run"].as_bool().unwrap_or(false);

            security.validate_shell_cmd(&cmd_owned)?;

            // Fix F3: 使用 spawn_blocking 避免阻塞 tokio async 线程池。
            // shell.run 可能执行 cargo build / npm install 等长时间任务，
            // 直接调用会占用 tokio worker 线程，影响 IPC 消息处理。
            // spawn_blocking 将其移至独立的 "blocking thread pool"。
            let output = tokio::task::spawn_blocking(move || {
                tools::run_command(&cmd_owned, &work_dir, timeout_s, dry_run)
            })
            .await
            .map_err(|e| anyhow::anyhow!("spawn_blocking join error: {}", e))??;

            Ok(serde_json::json!({
                "exit_code": output.exit_code,
                "stdout":    output.stdout,
                "stderr":    output.stderr,
            }))
        }

        // ── 搜索工具 ─────────────────────────────────────────
        "search.code" => {
            let query = args["query"].as_str().unwrap_or("");
            let path = args["path"]
                .as_str()
                .map(PathBuf::from)
                .unwrap_or(cwd.clone());
            let ignore = load_evocliignore();
            let mut results = search_code(query, &path)?;
            // Filter out paths matching .evocliignore patterns
            results.retain(|m| !is_ignored(std::path::Path::new(&m.file), &ignore));
            Ok(serde_json::to_value(results)?)
        }

        // ── 代码智能工具 ──────────────────────────────────────
        "code_intel.find_symbol" => {
            let query = args["query"].as_str().unwrap_or("").to_string();
            let db_path = cwd.join(".evocli").join("code_index.db");
            with_code_index!(db_path, fallback = serde_json::json!([]), |idx| {
                serde_json::to_value(idx.find_symbol(&query)?)?
            })
        }
        "code_intel.list_symbols" => {
            let file = args["file"].as_str().unwrap_or(".").to_string();
            let db_path = cwd.join(".evocli").join("code_index.db");
            with_code_index!(db_path, fallback = serde_json::json!([]), |idx| {
                serde_json::to_value(idx.list_symbols(std::path::Path::new(&file))?)?
            })
        }

        // ── Code Intel Layer 2: Call Graph (Section 16) ──────────
        "code_intel.incoming_calls" => {
            let symbol_id = args["symbol_id"].as_str().unwrap_or("").to_string();
            let db_path = cwd.join(".evocli").join("code_index.db");
            with_code_index!(db_path, fallback = serde_json::json!([]), |idx| {
                serde_json::to_value(idx.incoming_calls(&symbol_id)?)?
            })
        }
        "code_intel.outgoing_calls" => {
            let symbol_id = args["symbol_id"].as_str().unwrap_or("").to_string();
            let db_path = cwd.join(".evocli").join("code_index.db");
            with_code_index!(db_path, fallback = serde_json::json!([]), |idx| {
                serde_json::to_value(idx.outgoing_calls(&symbol_id)?)?
            })
        }
        "code_intel.full_chain" => {
            let symbol_id = args["symbol_id"].as_str().unwrap_or("").to_string();
            let max_depth = args["max_depth"].as_u64().unwrap_or(5) as usize;
            let db_path = cwd.join(".evocli").join("code_index.db");
            with_code_index!(
                db_path,
                fallback = serde_json::json!({"chain": []}),
                |idx| {
                    let chain = idx.full_upstream_chain(&symbol_id, max_depth)?;
                    serde_json::json!({"symbol_id": symbol_id, "chain": chain})
                }
            )
        }
        "code_intel.impact_radius" => {
            let symbol_id = args["symbol_id"].as_str().unwrap_or("").to_string();
            let db_path = cwd.join(".evocli").join("code_index.db");
            with_code_index!(
                db_path,
                fallback = serde_json::json!({"callers": 0, "test_files": []}),
                |idx| {
                    let callers = idx.incoming_calls(&symbol_id)?;
                    let test_files = idx.impact_test_files(&symbol_id)?;
                    serde_json::json!({
                        "symbol_id":    symbol_id,
                        "callers":      callers.len(),
                        "test_files":   test_files,
                        "chain_length": callers.len(),
                    })
                }
            )
        }

        // ── Memory 工具（Bridge → crates/memory）────────────
        "config.get" => {
            // Return sanitized config (API keys redacted for security)
            let mut cfg_json = serde_json::to_value(&cfg.clone())?;
            // Redact top-level llm.api_key and all per-role llm.roles.*.api_key
            if let Some(obj) = cfg_json.as_object_mut() {
                if let Some(llm) = obj.get_mut("llm") {
                    if let Some(llm_obj) = llm.as_object_mut() {
                        llm_obj.insert("api_key".into(), serde_json::json!(null));
                        // Redact per-role api_keys: llm.roles.<role>.api_key
                        if let Some(roles) = llm_obj.get_mut("roles") {
                            if let Some(roles_obj) = roles.as_object_mut() {
                                for role_val in roles_obj.values_mut() {
                                    if let Some(role_obj) = role_val.as_object_mut() {
                                        if role_obj.contains_key("api_key") {
                                            role_obj
                                                .insert("api_key".into(), serde_json::json!(null));
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
            Ok(cfg_json)
        }
        "memory.constraints" => {
            // H1 Memory Unification: Rust reads from JSONL fallback for standalone contexts
            // (e.g., evocli mcp serve without Python Soul running).
            // Returns constraints from ~/.evocli/data/memories.jsonl filtered by type=constraint.
            let jsonl_path = dirs::home_dir()
                .unwrap_or_default()
                .join(".evocli")
                .join("data")
                .join("memories.jsonl");
            if !jsonl_path.exists() {
                tracing::debug!("memory.constraints: no JSONL store found, returning empty");
                return Ok(serde_json::json!([]));
            }
            let content = std::fs::read_to_string(&jsonl_path).unwrap_or_default();
            let constraints: Vec<String> = content
                .lines()
                .filter_map(|line| serde_json::from_str::<serde_json::Value>(line).ok())
                .filter(|v| {
                    v.get("memory_type").and_then(|t| t.as_str()).unwrap_or("") == "constraint"
                })
                .filter_map(|v| {
                    v.get("body")
                        .and_then(|b| b.as_str())
                        .map(|s| s.to_string())
                        .or_else(|| {
                            v.get("content")
                                .and_then(|b| b.as_str())
                                .map(|s| s.to_string())
                        })
                })
                .collect();
            Ok(serde_json::to_value(constraints)?)
        }
        "memory.recall" => {
            // H1 fallback: keyword search over JSONL store for standalone MCP contexts.
            let query = args["query"].as_str().unwrap_or("").to_lowercase();
            let top_k = args["top_k"].as_u64().unwrap_or(5) as usize;
            let jsonl_path = dirs::home_dir()
                .unwrap_or_default()
                .join(".evocli")
                .join("data")
                .join("memories.jsonl");
            if !jsonl_path.exists() || query.is_empty() {
                return Ok(serde_json::json!([]));
            }
            let content = std::fs::read_to_string(&jsonl_path).unwrap_or_default();
            let mut results: Vec<serde_json::Value> = content
                .lines()
                .filter_map(|line| serde_json::from_str::<serde_json::Value>(line).ok())
                .filter(|v| {
                    let text = format!(
                        "{} {}",
                        v.get("title").and_then(|t| t.as_str()).unwrap_or(""),
                        v.get("body")
                            .or_else(|| v.get("content"))
                            .and_then(|b| b.as_str())
                            .unwrap_or("")
                    )
                    .to_lowercase();
                    text.contains(&query)
                })
                .take(top_k)
                .collect();
            results.truncate(top_k);
            Ok(serde_json::to_value(results)?)
        }
        "memory.write" => {
            // H1 fallback: append to JSONL store for standalone MCP contexts.
            let title = args["title"].as_str().unwrap_or("").to_string();
            let body = args
                .get("body")
                .and_then(|b| b.as_str())
                .or_else(|| args.get("content").and_then(|b| b.as_str()))
                .unwrap_or("")
                .to_string();
            let mtype = args
                .get("memory_type")
                .and_then(|t| t.as_str())
                .unwrap_or("episodic");
            let scope = args
                .get("priority_scope")
                .and_then(|s| s.as_str())
                .unwrap_or("project");
            if title.is_empty() && body.is_empty() {
                return Ok(serde_json::json!({"ok": false, "error": "title and body required"}));
            }
            let entry = serde_json::json!({
                "id":             uuid::Uuid::new_v4().to_string(),
                "title":          title,
                "body":           body,
                "memory_type":    mtype,
                "priority_scope": scope,
                "project_id":     cwd.to_string_lossy().to_string(),
                "created_at":     chrono::Utc::now().to_rfc3339(),
            });
            let jsonl_path = dirs::home_dir()
                .unwrap_or_default()
                .join(".evocli")
                .join("data")
                .join("memories.jsonl");
            if let Some(parent) = jsonl_path.parent() {
                let _ = std::fs::create_dir_all(parent);
            }
            let line = format!("{}\n", serde_json::to_string(&entry)?);
            let mut file = std::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(&jsonl_path)?;
            std::io::Write::write_all(&mut file, line.as_bytes())?;
            Ok(serde_json::json!({"ok": true, "id": entry["id"]}))
        }

        // ── 审批工具（TUI modal / CLI stdin）──────────────────────
        "approval.request" => {
            let message = args["message"]
                .as_str()
                .unwrap_or("Action requires approval")
                .to_string();
            let skill_id = args["skill_id"].as_str().unwrap_or("").to_string();
            let step_id = args["step_id"].as_str().unwrap_or("").to_string();
            let action = args["action"].as_str().unwrap_or("").to_string();

            let display_msg = if !skill_id.is_empty() {
                format!(
                    "[Skill: {} | Step: {} | Action: {}]\n{}",
                    skill_id, step_id, action, message
                )
            } else {
                message.clone()
            };

            let approved = if let Some(b) = bridge {
                b.request_approval(display_msg).await
            } else {
                let msg_cli = message.clone();
                tokio::task::spawn_blocking(move || {
                    use std::io::{self, BufRead, Write};
                    if !skill_id.is_empty() || !step_id.is_empty() {
                        eprintln!(
                            "\n⚠️  [Skill: {} | Step: {} | Action: {}]",
                            skill_id, step_id, action
                        );
                    }
                    eprintln!("    {}", msg_cli);
                    eprint!("Approve? [y/N]: ");
                    io::stderr().flush().ok();
                    io::stdin()
                        .lock()
                        .lines()
                        .next()
                        .and_then(|r| r.ok())
                        .map(|line| {
                            let t = line.trim();
                            t.eq_ignore_ascii_case("y") || t.eq_ignore_ascii_case("yes")
                        })
                        .unwrap_or(false)
                })
                .await
                .unwrap_or(false)
            };

            Ok(serde_json::json!({ "approved": approved }))
        }

        // ── Interactive choice prompt ────────────────────────────────────
        // Python Soul calls: bridge.call("prompt.choice", {
        //   "title": "How to fix?",
        //   "options": [{"id": "fix1", "label": "Change type"}, ...],
        //   "allow_custom": true,
        // })
        // Returns: {"type":"selected","id":"fix1"} | {"type":"custom","text":"..."} | {"type":"cancelled"}
        "prompt.choice" => {
            let title = args["title"]
                .as_str()
                .unwrap_or("Choose an option")
                .to_string();
            let options: Vec<soul_bridge::ChoiceOption> = args["options"]
                .as_array()
                .unwrap_or(&vec![])
                .iter()
                .map(|o| soul_bridge::ChoiceOption {
                    id: o["id"].as_str().unwrap_or("").to_string(),
                    label: o["label"].as_str().unwrap_or("").to_string(),
                })
                .collect();
            let allow_custom = args["allow_custom"].as_bool().unwrap_or(false);

            let req = soul_bridge::ChoiceRequest {
                title,
                options,
                allow_custom,
            };

            if let Some(b) = bridge {
                let result = b.request_choice(req).await;
                use soul_bridge::ChoiceResult;
                Ok(match result {
                    ChoiceResult::Selected(id) => {
                        serde_json::json!({ "type": "selected", "id": id })
                    }
                    ChoiceResult::Custom(text) => {
                        serde_json::json!({ "type": "custom", "text": text })
                    }
                    ChoiceResult::Cancelled => serde_json::json!({ "type": "cancelled" }),
                })
            } else {
                // CLI fallback: print options and read number from stdin
                use std::io::{self, BufRead, Write};
                let result = tokio::task::spawn_blocking(move || {
                    eprintln!("\n{}", req.title);
                    for (i, opt) in req.options.iter().enumerate() {
                        eprintln!("  [{}] {}", i + 1, opt.label);
                    }
                    if req.allow_custom {
                        eprintln!("  [c] Custom input");
                    }
                    eprint!("Choice: ");
                    io::stderr().flush().ok();
                    let line = io::stdin()
                        .lock()
                        .lines()
                        .next()
                        .and_then(|r| r.ok())
                        .unwrap_or_default();
                    let t = line.trim();
                    if req.allow_custom && (t == "c" || t == "i") {
                        eprint!("Enter custom text: ");
                        io::stderr().flush().ok();
                        let custom = io::stdin()
                            .lock()
                            .lines()
                            .next()
                            .and_then(|r| r.ok())
                            .unwrap_or_default();
                        serde_json::json!({ "type": "custom", "text": custom.trim() })
                    } else if let Ok(n) = t.parse::<usize>() {
                        if n >= 1 && n <= req.options.len() {
                            serde_json::json!({ "type": "selected", "id": req.options[n-1].id })
                        } else {
                            serde_json::json!({ "type": "cancelled" })
                        }
                    } else {
                        serde_json::json!({ "type": "cancelled" })
                    }
                })
                .await
                .unwrap_or(serde_json::json!({ "type": "cancelled" }));
                Ok(result)
            }
        }

        // ── 未知工具 ─────────────────────────────────────────

        // ── Code Intel: tree-sitter 集成（Python Soul 分析结果写入 Rust SQLite）──
        "code_intel.ingest_tree_sitter" => {
            let file_str = args["file"].as_str().unwrap_or("").to_string();
            let symbols = args
                .get("symbols")
                .and_then(|s| s.as_array())
                .cloned()
                .unwrap_or_default();
            let db_path = cwd.join(".evocli").join("code_index.db");
            match cached_code_index(&db_path) {
                Some(arc) => {
                    let mut index = arc.lock().unwrap();
                    let mut inserted = 0usize;
                    for sym in &symbols {
                        let name = sym["name"].as_str().unwrap_or("");
                        let kind = sym["kind"].as_str().unwrap_or("function");
                        let line = sym["line"].as_u64().unwrap_or(1) as u32;
                        let sig = sym["signature"].as_str().unwrap_or("");
                        let lang = sym["language"].as_str().unwrap_or("unknown");
                        if !name.is_empty() && !file_str.is_empty() {
                            let _ = index.add_symbol_direct(name, kind, &file_str, line, sig, lang);
                            inserted += 1;
                        }
                    }
                    Ok(serde_json::json!({
                        "ok": true, "file": file_str,
                        "inserted": inserted, "engine": "tree-sitter"
                    }))
                }
                None => {
                    // db doesn't exist yet — create fresh via new() (first index operation)
                    let mut index = code_intel::CodeIndex::new(&db_path)?;
                    let mut inserted = 0usize;
                    for sym in &symbols {
                        let name = sym["name"].as_str().unwrap_or("");
                        let kind = sym["kind"].as_str().unwrap_or("function");
                        let line = sym["line"].as_u64().unwrap_or(1) as u32;
                        let sig = sym["signature"].as_str().unwrap_or("");
                        let lang = sym["language"].as_str().unwrap_or("unknown");
                        if !name.is_empty() && !file_str.is_empty() {
                            let _ = index.add_symbol_direct(name, kind, &file_str, line, sig, lang);
                            inserted += 1;
                        }
                    }
                    Ok(serde_json::json!({
                        "ok": true, "file": file_str,
                        "inserted": inserted, "engine": "tree-sitter"
                    }))
                }
            }
        }

        "code_intel.index_status" => {
            let db_path = cwd.join(".evocli").join("code_index.db");
            with_code_index!(
                db_path,
                fallback = serde_json::json!({
                    "indexed": false,
                    "hint": "Run 'evocli index' to build the code index"
                }),
                |idx| {
                    let total_symbols = idx.count_symbols();
                    let total_edges = idx.count_edges();
                    let metadata = std::fs::metadata(&db_path)?;
                    serde_json::json!({
                        "indexed":        true,
                        "total_symbols":  total_symbols,
                        "total_edges":    total_edges,
                        "db_size_bytes":  metadata.len(),
                        "last_indexed":   metadata.modified()
                            .map(|t| t.duration_since(std::time::UNIX_EPOCH).unwrap_or_default().as_secs())
                            .unwrap_or(0),
                    })
                }
            )
        }

        // ── Fix: code_intel.full_downstream_chain（向下调用链）──────────
        "code_intel.full_downstream_chain" => {
            let symbol_id = args["symbol_id"].as_str().unwrap_or("").to_string();
            let max_depth = args["max_depth"].as_u64().unwrap_or(5) as usize;
            let db_path = cwd.join(".evocli").join("code_index.db");
            with_code_index!(
                db_path,
                fallback = serde_json::json!({"chain": []}),
                |idx| {
                    let mut visited = std::collections::HashSet::new();
                    let mut chain = Vec::new();
                    fn collect_downstream(
                        index: &mut code_intel::CodeIndex,
                        symbol_id: &str,
                        depth: usize,
                        visited: &mut std::collections::HashSet<String>,
                        chain: &mut Vec<code_intel::SymbolInfo>,
                    ) -> anyhow::Result<()> {
                        if depth == 0 || visited.contains(symbol_id) {
                            return Ok(());
                        }
                        visited.insert(symbol_id.to_string());
                        for callee in index.outgoing_calls(symbol_id)? {
                            let callee_id = callee.id.clone();
                            chain.push(callee);
                            collect_downstream(index, &callee_id, depth - 1, visited, chain)?;
                        }
                        Ok(())
                    }
                    collect_downstream(&mut *idx, &symbol_id, max_depth, &mut visited, &mut chain)?;
                    serde_json::json!({"symbol_id": symbol_id, "downstream_chain": chain})
                }
            )
        }

        // ── Knowledge Graph tools (GitNexus-inspired): BM25, blast_radius, communities ──────
        // These are missing from tool_dispatch.rs — Python agent calls bridge.call() to reach them.
        // Implemented using the knowledge_graph crate (tantivy BM25 + petgraph LPA).
        "code_intel.bm25_search" => {
            // Full-text BM25 code search using tantivy (GitNexus query tool — BM25 part).
            // Python's hybrid_search calls this first, then merges with LanceDB vector results.
            let query = args["query"].as_str().unwrap_or("");
            let limit = args["limit"].as_u64().unwrap_or(20) as usize;
            if query.is_empty() {
                return Ok(
                    serde_json::json!({"results": [], "count": 0, "error": "query is required"}),
                );
            }
            let index_dir = cwd.join(".evocli").join("bm25_index");
            if !index_dir.exists() {
                return Ok(serde_json::json!({
                    "results": [], "count": 0,
                    "hint": "Run 'evocli index' to build the BM25 code index"
                }));
            }
            match Bm25Index::open_or_create(&index_dir) {
                Ok(idx) => {
                    let hits = idx.search(query, limit).unwrap_or_default();
                    let results: Vec<serde_json::Value> = hits
                        .iter()
                        .map(|h| {
                            serde_json::json!({
                                "symbol_id": h.symbol_id, "name": h.name, "kind": h.kind,
                                "file": h.file, "signature": h.signature,
                                "score": h.score, "rank": h.rank,
                            })
                        })
                        .collect();
                    Ok(
                        serde_json::json!({"query": query, "results": results, "count": results.len()}),
                    )
                }
                Err(e) => {
                    Ok(serde_json::json!({"results": [], "count": 0, "error": e.to_string()}))
                }
            }
        }

        "code_intel.blast_radius" => {
            // Blast radius / impact analysis (GitNexus impact tool).
            // BFS upstream callers + downstream callees with risk assessment.
            let symbol_id = args["symbol_id"].as_str().unwrap_or("");
            let max_depth = args["max_depth"].as_u64().unwrap_or(5) as usize;
            if symbol_id.is_empty() {
                return Ok(serde_json::json!({"error": "symbol_id is required"}));
            }
            let db_path = cwd.join(".evocli").join("code_index.db");
            if !db_path.exists() {
                return Ok(serde_json::json!({
                    "symbol_id": symbol_id, "upstream": [], "downstream": [], "risk": "unknown",
                    "hint": "Run 'evocli index' first"
                }));
            }
            match KnowledgeGraph::from_sqlite(&db_path) {
                Ok(graph) => match graph.blast_radius(symbol_id, max_depth) {
                    Some(br) => Ok(serde_json::to_value(&br).unwrap_or(serde_json::json!({}))),
                    None => Ok(serde_json::json!({
                        "symbol_id": symbol_id, "upstream": [], "downstream": [],
                        "risk": "not_found", "note": "Symbol not found in index"
                    })),
                },
                Err(e) => Ok(serde_json::json!({"symbol_id": symbol_id, "error": e.to_string()})),
            }
        }

        "code_intel.symbol_context" => {
            // 360° symbol context: callers, callees, community, process membership.
            let symbol_id = args["symbol_id"].as_str().unwrap_or("");
            if symbol_id.is_empty() {
                return Ok(serde_json::json!({"error": "symbol_id is required"}));
            }
            let db_path = cwd.join(".evocli").join("code_index.db");
            if !db_path.exists() {
                return Ok(serde_json::json!({
                    "symbol_id": symbol_id, "callers": [], "callees": [],
                    "hint": "Run 'evocli index' first"
                }));
            }
            match KnowledgeGraph::from_sqlite(&db_path) {
                Ok(graph) => match graph.symbol_360_context(symbol_id) {
                    Some(ctx) => Ok(ctx),
                    None => Ok(serde_json::json!({
                        "symbol_id": symbol_id, "callers": [], "callees": [],
                        "note": "Symbol not found in graph"
                    })),
                },
                Err(e) => Ok(serde_json::json!({"symbol_id": symbol_id, "error": e.to_string()})),
            }
        }

        "code_intel.communities" => {
            // List functional code communities detected by Label Propagation Algorithm.
            // Groups related symbols into logical modules (GitNexus communities resource).
            let db_path = cwd.join(".evocli").join("code_index.db");
            if !db_path.exists() {
                return Ok(serde_json::json!({
                    "communities": [], "count": 0,
                    "hint": "Run 'evocli index' first"
                }));
            }
            match KnowledgeGraph::from_sqlite(&db_path) {
                Ok(graph) => {
                    let communities = graph.detect_communities_with_params(
                        cfg.graph.lpa_max_iter,
                        cfg.graph.min_community_size,
                    );
                    let result: Vec<serde_json::Value> = communities
                        .iter()
                        .map(|c| {
                            serde_json::json!({
                                "id": c.id, "label": c.label,
                                "members": c.members, "cohesion": c.cohesion,
                                "size": c.members.len(),
                            })
                        })
                        .collect();
                    Ok(serde_json::json!({
                        "communities": result,
                        "count": result.len(),
                        "algorithm": "Label Propagation (LPA)",
                    }))
                }
                Err(e) => {
                    Ok(serde_json::json!({"communities": [], "count": 0, "error": e.to_string()}))
                }
            }
        }

        // ── Symbol Oracle: symbol.lookup / symbol.variants (数据层，保留在 Rust) ──
        "code_intel.processes" => {
            // Execution flow / process detection (GitNexus processes resource).
            // Traces call chains from entry points to discover execution flows.
            let max_depth = args["max_depth"].as_u64().unwrap_or(10) as usize;
            let db_path = cwd.join(".evocli").join("code_index.db");
            if !db_path.exists() {
                return Ok(serde_json::json!({
                    "processes": [], "count": 0,
                    "hint": "Run 'evocli index' first"
                }));
            }
            match KnowledgeGraph::from_sqlite(&db_path) {
                Ok(graph) => {
                    let flows = graph.detect_processes(max_depth);
                    let result: Vec<serde_json::Value> = flows
                        .iter()
                        .map(|f| {
                            serde_json::json!({
                                "id": f.id, "name": f.name, "entry": f.entry,
                                "steps": f.steps, "depth": f.depth,
                            })
                        })
                        .collect();
                    Ok(serde_json::json!({
                        "processes": result,
                        "count":     result.len(),
                    }))
                }
                Err(e) => Ok(serde_json::json!({
                    "processes": [], "count": 0, "error": e.to_string()
                })),
            }
        }

        "symbol.lookup" => {
            let name = args["name"].as_str().unwrap_or("").to_string();
            let db_path = cwd.join(".evocli").join("code_index.db");
            with_code_index!(
                db_path,
                fallback = serde_json::json!({"found": false, "symbols": [], "hint": "Run evocli index first"}),
                |idx| {
                    let symbols = idx.find_symbol(&name)?;
                    serde_json::json!({"found": !symbols.is_empty(), "symbols": symbols, "did_you_mean": []})
                }
            )
        }
        "symbol.variants" => {
            let type_name = args["type_name"].as_str().unwrap_or("").to_string();
            let db_path = cwd.join(".evocli").join("code_index.db");
            with_code_index!(
                db_path,
                fallback = serde_json::json!({"variants": []}),
                |idx| {
                    let symbols = idx.find_symbol(&type_name)?;
                    let variants: Vec<_> = symbols
                        .iter()
                        .map(
                            |s| serde_json::json!({"name": s.name, "file": s.file, "line": s.line}),
                        )
                        .collect();
                    serde_json::json!({"variants": variants})
                }
            )
        }
        // symbol.usages / symbol.lifecycle → 已迁移至 Python handlers/code_analysis.py

        // ── Assumption Verifier (Section 17.2) ──────────────────────────
        // assume.* / impact.* / equiv.* / verify.* → 已全部迁移至 Python handlers/code_analysis.py
        // 理由：这些工具包含策略逻辑（评分权重、启发式规则），应在可进化的 Python 层

        // ── Contracts 原始数据工具（供 Python verify.* handlers 使用）────────────
        // 注：策略逻辑（进度计算、需求漂移检测）已移至 Python handlers/code_analysis.py
        // Rust 只提供原始数据访问，不做任何策略判断
        "contracts.list" => {
            // 列出所有活跃合约（供 Python verify.task / verify.coverage 使用）
            let db_path = dirs::home_dir()
                .unwrap_or_default()
                .join(".evocli")
                .join("contracts.db");
            if !db_path.exists() {
                return Ok(serde_json::json!([]));
            }
            let store = contracts::ContractStore::new(&db_path)?;
            let active = store.list_active()?;
            Ok(serde_json::to_value(active)?)
        }

        "contracts.get_checkpoints" => {
            // 获取指定合约的检查点列表（供 Python verify.task / verify.coverage 使用）
            let contract_id = args["contract_id"].as_str().unwrap_or("");
            let db_path = dirs::home_dir()
                .unwrap_or_default()
                .join(".evocli")
                .join("contracts.db");
            if !db_path.exists() {
                return Ok(serde_json::json!([]));
            }
            let store = contracts::ContractStore::new(&db_path)?;
            let checkpoints = store.get_checkpoints(contract_id)?;
            Ok(serde_json::to_value(checkpoints)?)
        }

        // ── Analysis tools — assume.* / impact.* / equiv.* / verify.* / symbol.* ──
        // These were migrated from Rust to Python for evolving strategy logic.
        // For standalone contexts (e.g., MCP serve without Python Soul), these Rust
        // arms provide code_intel-based analysis using the existing infrastructure.
        // When Python Soul is running, pydantic/LiteLLM paths route to Python handlers
        // directly (bypassing these arms) for richer LLM-assisted analysis.
        "assume.has_tests" | "assume.has_tests.stub" => {
            let symbol = args["symbol"].as_str().unwrap_or("");
            let db_path = cwd.join(".evocli").join("code_index.db");
            with_code_index!(
                db_path,
                fallback = serde_json::json!({"has_tests": false, "symbol": symbol, "note": "index not built"}),
                |idx| {
                    let test_matches: Vec<_> = idx
                        .find_symbol(&format!("test_{}", symbol))
                        .unwrap_or_default()
                        .into_iter()
                        .chain(
                            idx.find_symbol(&format!("{}_test", symbol))
                                .unwrap_or_default(),
                        )
                        .chain(
                            idx.find_symbol(&format!("test{}", symbol))
                                .unwrap_or_default(),
                        )
                        .collect();
                    serde_json::json!({"has_tests": !test_matches.is_empty(), "symbol": symbol, "test_symbols": test_matches.len()})
                }
            )
        }
        "assume.is_pure" => {
            let symbol = args["symbol"].as_str().unwrap_or("");
            let db_path = cwd.join(".evocli").join("code_index.db");
            with_code_index!(
                db_path,
                fallback = serde_json::json!({"is_pure": null, "symbol": symbol, "note": "index not built"}),
                |idx| {
                    let sym_id = idx
                        .find_symbol(symbol)
                        .unwrap_or_default()
                        .into_iter()
                        .next()
                        .map(|s| s.id)
                        .unwrap_or_else(|| symbol.to_string());
                    let outgoing = idx.outgoing_calls(&sym_id).unwrap_or_default();
                    let io_calls: Vec<String> = outgoing
                        .iter()
                        .filter(|s| {
                            let n = s.name.to_lowercase();
                            n.contains("write")
                                || n.contains("read")
                                || n.contains("print")
                                || n.contains("log")
                                || n.contains("send")
                        })
                        .map(|s| s.name.clone())
                        .collect();
                    serde_json::json!({"is_pure": io_calls.is_empty(), "symbol": symbol, "io_calls": io_calls, "confidence": "heuristic"})
                }
            )
        }
        "assume.caller_count" => {
            let symbol = args["symbol"].as_str().unwrap_or("");
            let db_path = cwd.join(".evocli").join("code_index.db");
            with_code_index!(
                db_path,
                fallback = serde_json::json!({"caller_count": 0, "symbol": symbol}),
                |idx| {
                    let sym_id = idx
                        .find_symbol(symbol)
                        .unwrap_or_default()
                        .into_iter()
                        .next()
                        .map(|s| s.id)
                        .unwrap_or_else(|| symbol.to_string());
                    let callers = idx.incoming_calls(&sym_id).unwrap_or_default();
                    let caller_names: Vec<String> =
                        callers.iter().map(|s| s.name.clone()).collect();
                    serde_json::json!({"caller_count": callers.len(), "symbol": symbol, "callers": caller_names})
                }
            )
        }
        "assume.has_side_effects" => {
            let symbol = args["symbol"].as_str().unwrap_or("");
            let db_path = cwd.join(".evocli").join("code_index.db");
            with_code_index!(
                db_path,
                fallback = serde_json::json!({"has_side_effects": null, "symbol": symbol}),
                |idx| {
                    let sym_id = idx
                        .find_symbol(symbol)
                        .unwrap_or_default()
                        .into_iter()
                        .next()
                        .map(|s| s.id)
                        .unwrap_or_else(|| symbol.to_string());
                    let outgoing = idx.outgoing_calls(&sym_id).unwrap_or_default();
                    let effect_calls: Vec<String> = outgoing
                        .iter()
                        .filter(|s| {
                            let n = s.name.to_lowercase();
                            n.contains("write")
                                || n.contains("send")
                                || n.contains("mutate")
                                || n.contains("update")
                                || n.contains("delete")
                                || n.contains("save")
                        })
                        .map(|s| s.name.clone())
                        .collect();
                    serde_json::json!({"has_side_effects": !effect_calls.is_empty(), "symbol": symbol, "effect_calls": effect_calls, "confidence": "heuristic"})
                }
            )
        }
        "assume.verify" => {
            let assumption = args["assumption"].as_str().unwrap_or("");
            let subject = args["subject"].as_str().unwrap_or("");
            let results = search_code(assumption, &cwd)?;
            let relevant: Vec<_> = results
                .iter()
                .filter(|m| subject.is_empty() || m.file.contains(subject))
                .take(5)
                .collect();
            Ok(
                serde_json::json!({"assumption": assumption, "subject": subject, "evidence": relevant, "verified": !relevant.is_empty(), "confidence": "heuristic"}),
            )
        }
        "assume.is_deprecated" => {
            let symbol = args["symbol"].as_str().unwrap_or("");
            let markers = [
                "#[deprecated",
                "@deprecated",
                "# Deprecated",
                "DEPRECATED",
                ".. deprecated::",
                "@Deprecated",
            ];
            let mut hits: usize = 0;
            for marker in &markers {
                if let Ok(r) = search_code(marker, &cwd) {
                    hits += r
                        .iter()
                        .filter(|m| m.file.contains(symbol) || m.content.contains(symbol))
                        .count();
                }
            }
            Ok(
                serde_json::json!({"is_deprecated": hits > 0, "symbol": symbol, "evidence_count": hits}),
            )
        }
        "assume.is_only_caller" => {
            let caller = args["caller"].as_str().unwrap_or("");
            let target = args["target"].as_str().unwrap_or("");
            let db_path = cwd.join(".evocli").join("code_index.db");
            with_code_index!(
                db_path,
                fallback = serde_json::json!({"is_only_caller": false}),
                |idx| {
                    let target_id = idx
                        .find_symbol(target)
                        .unwrap_or_default()
                        .into_iter()
                        .next()
                        .map(|s| s.id)
                        .unwrap_or_else(|| target.to_string());
                    let callers = idx.incoming_calls(&target_id).unwrap_or_default();
                    let is_only =
                        callers.len() == 1 && callers.iter().any(|c| c.name.contains(caller));
                    serde_json::json!({"is_only_caller": is_only, "caller": caller, "target": target, "total_callers": callers.len()})
                }
            )
        }
        "assume.types_match" => {
            let symbol_a = args["symbol_a"].as_str().unwrap_or("");
            let symbol_b = args["symbol_b"].as_str().unwrap_or("");
            let db_path = cwd.join(".evocli").join("code_index.db");
            with_code_index!(
                db_path,
                fallback = serde_json::json!({"types_match": null, "note": "index not built"}),
                |idx| {
                    let a_syms = idx.find_symbol(symbol_a).unwrap_or_default();
                    let b_syms = idx.find_symbol(symbol_b).unwrap_or_default();
                    let a_files: std::collections::HashSet<&str> =
                        a_syms.iter().map(|s| s.file.as_str()).collect();
                    let b_files: std::collections::HashSet<&str> =
                        b_syms.iter().map(|s| s.file.as_str()).collect();
                    let shared: Vec<&&str> = a_files.intersection(&b_files).collect();
                    serde_json::json!({"types_match": !shared.is_empty(), "symbol_a": symbol_a, "symbol_b": symbol_b, "shared_file_count": shared.len(), "confidence": "heuristic"})
                }
            )
        }
        "impact.check" => {
            let symbol = args["symbol"].as_str().unwrap_or("");
            let change_type = args["change_type"].as_str().unwrap_or("behavior");
            let db_path = cwd.join(".evocli").join("code_index.db");
            with_code_index!(
                db_path,
                fallback = serde_json::json!({"symbol": symbol, "callers": [], "risk": "unknown"}),
                |idx| {
                    let sym_id = idx
                        .find_symbol(symbol)
                        .unwrap_or_default()
                        .into_iter()
                        .next()
                        .map(|s| s.id)
                        .unwrap_or_else(|| symbol.to_string());
                    let callers = idx.incoming_calls(&sym_id).unwrap_or_default();
                    let callees = idx.outgoing_calls(&sym_id).unwrap_or_default();
                    let risk = if callers.len() > 10 {
                        "high"
                    } else if callers.len() > 3 {
                        "medium"
                    } else {
                        "low"
                    };
                    let caller_names: Vec<String> =
                        callers.iter().map(|s| s.name.clone()).collect();
                    let callee_names: Vec<String> =
                        callees.iter().map(|s| s.name.clone()).collect();
                    serde_json::json!({"symbol": symbol, "change_type": change_type, "caller_count": callers.len(), "callers": caller_names, "callees": callee_names, "risk": risk})
                }
            )
        }
        "impact.affected_tests" => {
            let symbol = args["symbol"].as_str().unwrap_or("");
            let db_path = cwd.join(".evocli").join("code_index.db");
            with_code_index!(
                db_path,
                fallback = serde_json::json!({"symbol": symbol, "affected_tests": []}),
                |idx| {
                    let sym_id = idx
                        .find_symbol(symbol)
                        .unwrap_or_default()
                        .into_iter()
                        .next()
                        .map(|s| s.id)
                        .unwrap_or_else(|| symbol.to_string());
                    let callers = idx.incoming_calls(&sym_id).unwrap_or_default();
                    let tests: Vec<String> = callers
                        .iter()
                        .filter(|c| {
                            c.name.contains("test")
                                || c.name.contains("spec")
                                || c.file.contains("test")
                        })
                        .map(|c| c.name.clone())
                        .collect();
                    serde_json::json!({"symbol": symbol, "affected_tests": tests, "total_callers": callers.len()})
                }
            )
        }
        "impact.batch_check" => {
            let change_type = args["change_type"].as_str().unwrap_or("behavior");
            let symbols: Vec<String> = args["symbols"]
                .as_array()
                .map(|a| {
                    a.iter()
                        .filter_map(|v| v.as_str().map(|s| s.to_string()))
                        .collect()
                })
                .unwrap_or_default();
            let db_path = cwd.join(".evocli").join("code_index.db");
            let mut results = vec![];
            if db_path.exists() {
                if let Some(arc) = cached_code_index(&db_path) {
                    let idx = arc.lock().unwrap();
                    for sym in &symbols {
                        // Resolve name → ID (same pattern as other analysis arms)
                        let sym_id = idx
                            .find_symbol(sym)
                            .unwrap_or_default()
                            .into_iter()
                            .next()
                            .map(|s| s.id)
                            .unwrap_or_else(|| sym.clone());
                        let callers = idx.incoming_calls(&sym_id).unwrap_or_default();
                        let risk = if callers.len() > 10 {
                            "high"
                        } else if callers.len() > 3 {
                            "medium"
                        } else {
                            "low"
                        };
                        results.push(serde_json::json!({"symbol": sym, "change_type": change_type, "caller_count": callers.len(), "risk": risk}));
                    }
                }
            }
            Ok(serde_json::json!({"results": results, "change_type": change_type}))
        }
        "equiv.find" => {
            let intent = args["intent"].as_str().unwrap_or("");
            let limit = args["limit"].as_u64().unwrap_or(5) as usize;
            let results = search_code(intent, &cwd)?;
            Ok(
                serde_json::json!({"intent": intent, "matches": results.into_iter().take(limit).collect::<Vec<_>>()}),
            )
        }
        "equiv.check_deps" => {
            let intent = args["intent"].as_str().unwrap_or("");
            let results = search_code(intent, &cwd)?;
            let feasible = !results.is_empty();
            Ok(
                serde_json::json!({"intent": intent, "feasible": feasible, "existing_matches": results.len(), "top_matches": results.into_iter().take(3).collect::<Vec<_>>()}),
            )
        }
        "equiv.find_similar_code" => {
            let code = args["code"].as_str().unwrap_or("");
            let limit = args["limit"].as_u64().unwrap_or(5) as usize;
            let query: String = code
                .split_whitespace()
                .filter(|w| w.len() > 4 && w.chars().all(|c| c.is_alphanumeric() || c == '_'))
                .take(5)
                .collect::<Vec<_>>()
                .join(" ");
            let results = if !query.is_empty() {
                search_code(&query, &cwd)?
            } else {
                vec![]
            };
            Ok(serde_json::json!({"matches": results.into_iter().take(limit).collect::<Vec<_>>()}))
        }
        "verify.task" => {
            let contract_id = args["contract_id"].as_str().unwrap_or("");
            let db_path = dirs::home_dir()
                .unwrap_or_default()
                .join(".evocli")
                .join("contracts.db");
            if !db_path.exists() {
                return Ok(
                    serde_json::json!({"ok": false, "error": format!("Contract '{}' not found (no contracts.db)", contract_id)}),
                );
            }
            let store = contracts::ContractStore::new(&db_path)?;
            let active = store.list_active()?;
            let contract = active
                .iter()
                .find(|c| c.id == contract_id || c.id.starts_with(contract_id));
            match contract {
                None => Ok(
                    serde_json::json!({"ok": false, "error": format!("Contract '{}' not found", contract_id)}),
                ),
                Some(c) => {
                    let checkpoints = store.get_checkpoints(&c.id)?;
                    let done = checkpoints.iter().filter(|cp| cp.status == "done").count();
                    let total = checkpoints.len();
                    let pct = if total > 0 { done * 100 / total } else { 0 };
                    Ok(
                        serde_json::json!({"contract_id": c.id, "requirement": c.requirement, "overall_pct": pct, "checkpoints_done": done, "checkpoints_total": total, "status": if pct == 100 { "complete" } else { "in_progress" }}),
                    )
                }
            }
        }
        "verify.coverage" => {
            let contract_id = args["contract_id"].as_str().unwrap_or("");
            let db_path = dirs::home_dir()
                .unwrap_or_default()
                .join(".evocli")
                .join("contracts.db");
            if !db_path.exists() {
                return Ok(
                    serde_json::json!({"error": format!("Contract '{}' not found (no contracts.db)", contract_id)}),
                );
            }
            let store = contracts::ContractStore::new(&db_path)?;
            let checkpoints = store.get_checkpoints(contract_id)?;
            let covered: Vec<&str> = checkpoints
                .iter()
                .filter(|cp| cp.status == "done")
                .map(|cp| cp.description.as_str())
                .collect();
            let uncovered: Vec<&str> = checkpoints
                .iter()
                .filter(|cp| cp.status != "done")
                .map(|cp| cp.description.as_str())
                .collect();
            let pct = if checkpoints.is_empty() {
                0usize
            } else {
                covered.len() * 100 / checkpoints.len()
            };
            Ok(
                serde_json::json!({"contract_id": contract_id, "coverage_pct": pct, "covered": covered, "uncovered": uncovered}),
            )
        }
        "verify.drift" => {
            let contract_id = args["contract_id"].as_str().unwrap_or("");
            let diff = git::git_diff_ext(&cwd, "", None, false, "").unwrap_or_default();
            let has_changes = !diff.trim().is_empty();
            Ok(
                serde_json::json!({"contract_id": contract_id, "has_changes": has_changes, "diff_size": diff.len(), "drift_detected": has_changes, "note": "heuristic: based on current git diff"}),
            )
        }
        "symbol.usages" => {
            let symbol_id = args["symbol_id"].as_str().unwrap_or("");
            let limit = args["limit"].as_u64().unwrap_or(20) as usize;
            let db_path = cwd.join(".evocli").join("code_index.db");
            with_code_index!(
                db_path,
                fallback = serde_json::json!({"symbol_id": symbol_id, "usages": []}),
                |idx| {
                    // Accept both symbol_id (from indexed data) and name (user-provided)
                    let resolved_id = if idx
                        .incoming_calls(symbol_id)
                        .map(|v| !v.is_empty())
                        .unwrap_or(false)
                    {
                        symbol_id.to_string()
                    } else {
                        idx.find_symbol(symbol_id)
                            .unwrap_or_default()
                            .into_iter()
                            .next()
                            .map(|s| s.id)
                            .unwrap_or_else(|| symbol_id.to_string())
                    };
                    let callers = idx.incoming_calls(&resolved_id).unwrap_or_default();
                    let usages: Vec<serde_json::Value> = callers
                        .into_iter()
                        .take(limit)
                        .map(
                            |s| serde_json::json!({"name": s.name, "file": s.file, "line": s.line}),
                        )
                        .collect();
                    serde_json::json!({"symbol_id": symbol_id, "usages": usages})
                }
            )
        }
        "symbol.lifecycle" => {
            let name = args["name"].as_str().unwrap_or("");
            let db_path = cwd.join(".evocli").join("code_index.db");
            with_code_index!(
                db_path,
                fallback = serde_json::json!({"name": name, "lifecycle": []}),
                |idx| {
                    let syms = idx.find_symbol(name).unwrap_or_default();
                    let sym_id = syms
                        .iter()
                        .next()
                        .map(|s| s.id.as_str())
                        .unwrap_or(name)
                        .to_string();
                    let callers = idx.incoming_calls(&sym_id).unwrap_or_default();
                    let caller_names: Vec<String> =
                        callers.iter().map(|s| s.name.clone()).collect();
                    serde_json::json!({"name": name, "definitions": serde_json::to_value(syms).unwrap_or_default(), "callers": caller_names})
                }
            )
        }
        "code_intel.ranked_context" => {
            let modified_file = args["modified_file"].as_str().unwrap_or("");
            let limit = args["limit"].as_u64().unwrap_or(20) as usize;
            let mentioned: Vec<String> = args["mentioned"]
                .as_array()
                .unwrap_or(&vec![])
                .iter()
                .filter_map(|v| v.as_str().map(|s| s.to_string()))
                .collect();
            let db_path = cwd.join(".evocli").join("code_index.db");
            with_code_index!(
                db_path,
                fallback = serde_json::json!({"ranked": [], "modified_file": modified_file}),
                |idx| {
                    // Score symbols by: explicitly_mentioned(+2.0) + caller_count(normalized)
                    // This is a lightweight proxy for PageRank until the Rust graph crate
                    // exposes a proper PageRank implementation over the call graph.
                    let symbols = idx
                        .list_symbols(std::path::Path::new(modified_file))
                        .unwrap_or_default();
                    let mut scored: Vec<(f64, serde_json::Value)> = symbols
                        .into_iter()
                        .map(|s| {
                            let mention_bonus = if mentioned
                                .iter()
                                .any(|m| s.name.contains(m.as_str()) || m.contains(s.name.as_str()))
                            {
                                2.0
                            } else {
                                0.0
                            };
                            let caller_count =
                                idx.incoming_calls(&s.id).map(|c| c.len()).unwrap_or(0);
                            let score = mention_bonus + (caller_count as f64).sqrt();
                            let entry = serde_json::json!({
                                "name": s.name, "kind": s.kind, "file": s.file, "line": s.line,
                                "score": (score * 100.0).round() / 100.0,
                                "caller_count": caller_count,
                            });
                            (score, entry)
                        })
                        .collect();
                    scored
                        .sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));
                    let ranked: Vec<serde_json::Value> =
                        scored.into_iter().take(limit).map(|(_, v)| v).collect();
                    serde_json::json!({
                        "ranked": ranked,
                        "modified_file": modified_file,
                        "algorithm": "caller_count_sqrt + mention_bonus (proxy-PageRank)",
                    })
                }
            )
        }

        // ── Shell built-ins (Section 22) ─────────────────────────────────
        "shell.grep" => {
            let pattern = args["pattern"].as_str().unwrap_or("");
            let path = args["path"]
                .as_str()
                .map(PathBuf::from)
                .unwrap_or(cwd.clone());
            let case_sensitive = args["case_sensitive"].as_bool().unwrap_or(false);
            let context_lines = args["context_lines"].as_u64().unwrap_or(0) as usize;
            let max_results = args["max_results"].as_u64().unwrap_or(100) as usize;
            // include: file extension or glob-like suffix, e.g. ".rs" ".py" or "*.toml"
            let include_ext = args["include"].as_str().unwrap_or("");
            // exclude: path substring to skip, e.g. "target" "node_modules"
            let exclude_sub = args["exclude"].as_str().unwrap_or("");

            let pat_lower = if case_sensitive {
                pattern.to_string()
            } else {
                pattern.to_lowercase()
            };

            let mut matches: Vec<serde_json::Value> = vec![];
            let mut total_count = 0usize;

            'outer: for entry in walkdir::WalkDir::new(&path)
                .follow_links(false)
                .into_iter()
                .filter_map(|e| e.ok())
                .filter(|e| e.file_type().is_file())
            {
                let p = entry.path();
                let p_str = p.to_str().unwrap_or("");

                // Skip common noise dirs
                if p_str.contains("/target/")
                    || p_str.contains("\\target\\")
                    || p_str.contains("node_modules")
                    || p_str.contains(".git/")
                    || p_str.contains("\\.git\\")
                {
                    continue;
                }

                // User-specified exclude
                if !exclude_sub.is_empty() && p_str.contains(exclude_sub) {
                    continue;
                }

                // User-specified include (extension filter)
                if !include_ext.is_empty() {
                    let ext_filter = include_ext.trim_start_matches('*').trim_start_matches('.');
                    let file_ext = p.extension().and_then(|e| e.to_str()).unwrap_or("");
                    let file_name = p.file_name().and_then(|n| n.to_str()).unwrap_or("");
                    if !file_ext.eq_ignore_ascii_case(ext_filter)
                        && !file_name.contains(include_ext)
                    {
                        continue;
                    }
                }

                let Ok(content) = std::fs::read_to_string(p) else {
                    continue;
                };
                let lines: Vec<&str> = content.lines().collect();

                for (i, line) in lines.iter().enumerate() {
                    let hay = if case_sensitive {
                        line.to_string()
                    } else {
                        line.to_lowercase()
                    };
                    if hay.contains(&pat_lower) {
                        total_count += 1;
                        if matches.len() < max_results {
                            let mut m = serde_json::json!({
                                "file":    p.to_string_lossy(),
                                "line":    i + 1,
                                "content": line,
                            });
                            // Context lines: before + after
                            if context_lines > 0 {
                                let before_start = i.saturating_sub(context_lines);
                                let after_end = (i + context_lines + 1).min(lines.len());
                                let before: Vec<&str> = lines[before_start..i].to_vec();
                                let after: Vec<&str> = lines[(i + 1)..after_end].to_vec();
                                m["before"] = serde_json::json!(before);
                                m["after"] = serde_json::json!(after);
                            }
                            matches.push(m);
                        }
                        if total_count >= max_results * 10 {
                            break 'outer;
                        } // safety cap
                    }
                }
            }

            Ok(serde_json::json!({
                "pattern":     pattern,
                "matches":     matches,
                "count":       matches.len(),
                "total_found": total_count,
                "truncated":   total_count > max_results,
            }))
        }
        "shell.find" => {
            let name_pat = args["name"].as_str().unwrap_or("");
            let path = args["path"]
                .as_str()
                .map(PathBuf::from)
                .unwrap_or(cwd.clone());
            // extension: filter by file extension, e.g. "rs" or ".rs" or "*.rs"
            let ext_filter = args["extension"]
                .as_str()
                .unwrap_or("")
                .trim_start_matches('*')
                .trim_start_matches('.');
            // type: "file" | "dir" | "" (both, default)
            let type_filter = args["type"].as_str().unwrap_or("");
            // depth: max recursion depth (0 = unlimited)
            let max_depth = args["depth"].as_u64().unwrap_or(0) as usize;
            // case_sensitive: default false
            let case_sensitive = args["case_sensitive"].as_bool().unwrap_or(false);
            // max_results: default 200
            let max_results = args["max_results"].as_u64().unwrap_or(200) as usize;
            // exclude: path substring to skip
            let exclude_sub = args["exclude"].as_str().unwrap_or("");

            let name_lower = if case_sensitive {
                name_pat.to_string()
            } else {
                name_pat.to_lowercase()
            };

            let walker = if max_depth > 0 {
                walkdir::WalkDir::new(&path).max_depth(max_depth)
            } else {
                walkdir::WalkDir::new(&path)
            };

            let mut found: Vec<String> = vec![];

            for entry in walker.into_iter().flatten() {
                if found.len() >= max_results {
                    break;
                }

                let p = entry.path();
                let p_str = p.to_str().unwrap_or("");

                // Skip noise
                if p_str.contains("/target/")
                    || p_str.contains("\\target\\")
                    || p_str.contains("node_modules")
                    || p_str.contains("\\.git\\")
                    || p_str.contains("/.git/")
                {
                    continue;
                }

                if !exclude_sub.is_empty() && p_str.contains(exclude_sub) {
                    continue;
                }

                let is_dir = entry.file_type().is_dir();
                let is_file = entry.file_type().is_file();

                // Type filter
                match type_filter {
                    "file" if !is_file => continue,
                    "dir" if !is_dir => continue,
                    _ => {}
                }

                // Extension filter (files only)
                if !ext_filter.is_empty() && is_file {
                    let file_ext = p.extension().and_then(|e| e.to_str()).unwrap_or("");
                    if !file_ext.eq_ignore_ascii_case(ext_filter) {
                        continue;
                    }
                }

                // Name filter
                if !name_pat.is_empty() {
                    let fname = entry.file_name().to_string_lossy().to_string();
                    let hay = if case_sensitive {
                        fname.clone()
                    } else {
                        fname.to_lowercase()
                    };
                    if !hay.contains(&name_lower) {
                        continue;
                    }
                }

                found.push(p.display().to_string());
            }

            let count = found.len();
            Ok(serde_json::json!({
                "path":    path.display().to_string(),
                "name":    name_pat,
                "files":   found,
                "count":   count,
            }))
        }
        "shell.ls" => {
            let path = args["path"]
                .as_str()
                .map(PathBuf::from)
                .unwrap_or(cwd.clone());
            let long = args["long"].as_bool().unwrap_or(false);
            let tree = args["tree"].as_bool().unwrap_or(false);
            let depth = args["depth"].as_u64().unwrap_or(1) as usize; // 1=flat, 0=unlimited
            let show_hidden = args["show_hidden"].as_bool().unwrap_or(false);

            if tree {
                // Tree-format output as a single string
                let mut buf = String::new();
                buf.push_str(&format!("{}\n", path.display()));
                fn write_tree(
                    dir: &std::path::Path,
                    prefix: &str,
                    buf: &mut String,
                    current_depth: usize,
                    max_depth: usize,
                    show_hidden: bool,
                ) {
                    let Ok(entries) = std::fs::read_dir(dir) else {
                        return;
                    };
                    let mut items: Vec<_> = entries
                        .flatten()
                        .filter(|e| {
                            show_hidden || !e.file_name().to_string_lossy().starts_with('.')
                        })
                        .collect();
                    items.sort_by_key(|e| {
                        let is_dir = e.file_type().map(|t| t.is_dir()).unwrap_or(false);
                        (!is_dir, e.file_name()) // dirs first, then alpha
                    });
                    let total = items.len();
                    for (i, entry) in items.iter().enumerate() {
                        let is_last = i + 1 == total;
                        let connector = if is_last { "└── " } else { "├── " };
                        let child_prefix = if is_last { "    " } else { "│   " };
                        let name = entry.file_name().to_string_lossy().to_string();
                        let is_dir = entry.file_type().map(|t| t.is_dir()).unwrap_or(false);
                        let display = if is_dir { format!("{name}/") } else { name };
                        buf.push_str(&format!("{prefix}{connector}{display}\n"));
                        if is_dir && (max_depth == 0 || current_depth < max_depth) {
                            write_tree(
                                &entry.path(),
                                &format!("{prefix}{child_prefix}"),
                                buf,
                                current_depth + 1,
                                max_depth,
                                show_hidden,
                            );
                        }
                    }
                }
                write_tree(&path, "", &mut buf, 1, depth, show_hidden);
                Ok(serde_json::json!({
                    "path":   path.display().to_string(),
                    "tree":   buf,
                    "format": "tree",
                }))
            } else {
                // Flat or recursive JSON listing
                fn collect_entries(
                    dir: &std::path::Path,
                    current_depth: usize,
                    max_depth: usize,
                    long: bool,
                    show_hidden: bool,
                ) -> Vec<serde_json::Value> {
                    let Ok(rd) = std::fs::read_dir(dir) else {
                        return vec![];
                    };
                    let mut items: Vec<_> = rd
                        .flatten()
                        .filter(|e| {
                            show_hidden || !e.file_name().to_string_lossy().starts_with('.')
                        })
                        .collect();
                    items.sort_by_key(|e| {
                        let is_dir = e.file_type().map(|t| t.is_dir()).unwrap_or(false);
                        (!is_dir, e.file_name())
                    });
                    let mut out = vec![];
                    for e in items {
                        let meta = e.metadata().ok();
                        let is_dir = meta.as_ref().map(|m| m.is_dir()).unwrap_or(false);
                        let size = meta.as_ref().map(|m| m.len()).unwrap_or(0);
                        let name = e.file_name().to_string_lossy().to_string();
                        if long {
                            out.push(serde_json::json!({
                                "name":   name,
                                "is_dir": is_dir,
                                "size":   size,
                            }));
                        } else {
                            out.push(serde_json::json!(if is_dir {
                                format!("{name}/")
                            } else {
                                name
                            }));
                        }
                        if is_dir && (max_depth == 0 || current_depth < max_depth) {
                            out.extend(collect_entries(
                                &e.path(),
                                current_depth + 1,
                                max_depth,
                                long,
                                show_hidden,
                            ));
                        }
                    }
                    out
                }
                let entries = if path.exists() {
                    collect_entries(&path, 1, depth, long, show_hidden)
                } else {
                    vec![]
                };
                let count = entries.len();
                Ok(serde_json::json!({
                    "path":    path.display().to_string(),
                    "entries": entries,
                    "count":   count,
                    "depth":   depth,
                }))
            }
        }
        "shell.cat" => {
            let file = args["file"].as_str().unwrap_or("");
            let fp = if std::path::Path::new(file).is_absolute() {
                PathBuf::from(file)
            } else {
                cwd.join(file)
            };
            let content = std::fs::read_to_string(&fp)
                .map_err(|e| anyhow::anyhow!("shell.cat: cannot read '{}': {}", fp.display(), e))?;
            Ok(serde_json::json!({"file": file, "content": content}))
        }
        "shell.mkdir" => {
            let path = args["path"].as_str().unwrap_or("");
            let fp = if std::path::Path::new(path).is_absolute() {
                PathBuf::from(path)
            } else {
                cwd.join(path)
            };
            std::fs::create_dir_all(&fp)?;
            Ok(serde_json::json!({"created": fp.display().to_string()}))
        }
        "shell.wc" => {
            let file = args["file"].as_str().unwrap_or("");
            let fp = if std::path::Path::new(file).is_absolute() {
                PathBuf::from(file)
            } else {
                cwd.join(file)
            };
            let text = std::fs::read_to_string(&fp).unwrap_or_default();
            let lines = text.lines().count();
            let words = text.split_whitespace().count();
            let chars = text.len();
            Ok(serde_json::json!({"file": file, "lines": lines, "words": words, "chars": chars}))
        }
        "shell.head" => {
            let file = args["file"].as_str().unwrap_or("");
            let n = args["n"].as_u64().unwrap_or(10) as usize;
            let fp = if std::path::Path::new(file).is_absolute() {
                PathBuf::from(file)
            } else {
                cwd.join(file)
            };
            let text = std::fs::read_to_string(&fp).unwrap_or_default();
            let out = text.lines().take(n).collect::<Vec<_>>().join("\n");
            Ok(serde_json::json!({"file": file, "n": n, "content": out}))
        }
        "shell.tail" => {
            let file = args["file"].as_str().unwrap_or("");
            let n = args["n"].as_u64().unwrap_or(10) as usize;
            let fp = if std::path::Path::new(file).is_absolute() {
                PathBuf::from(file)
            } else {
                cwd.join(file)
            };
            let text = std::fs::read_to_string(&fp).unwrap_or_default();
            let lines: Vec<&str> = text.lines().collect();
            let start = lines.len().saturating_sub(n);
            let out = lines[start..].join("\n");
            Ok(serde_json::json!({"file": file, "n": n, "content": out}))
        }
        "shell.mv" => {
            let src = args["src"].as_str().unwrap_or("");
            let dst = args["dst"].as_str().unwrap_or("");
            let sfp = if std::path::Path::new(src).is_absolute() {
                PathBuf::from(src)
            } else {
                cwd.join(src)
            };
            let dfp = if std::path::Path::new(dst).is_absolute() {
                PathBuf::from(dst)
            } else {
                cwd.join(dst)
            };
            std::fs::rename(&sfp, &dfp)?;
            Ok(serde_json::json!({"moved": {"from": src, "to": dst}}))
        }
        "shell.cp" => {
            let src = args["src"].as_str().unwrap_or("");
            let dst = args["dst"].as_str().unwrap_or("");
            let sfp = if std::path::Path::new(src).is_absolute() {
                PathBuf::from(src)
            } else {
                cwd.join(src)
            };
            let dfp = if std::path::Path::new(dst).is_absolute() {
                PathBuf::from(dst)
            } else {
                cwd.join(dst)
            };
            std::fs::copy(&sfp, &dfp)?;
            Ok(serde_json::json!({"copied": {"from": src, "to": dst}}))
        }
        "shell.rm" => {
            let path = args["path"].as_str().unwrap_or("");
            let recursive = args["recursive"].as_bool().unwrap_or(false);
            let fp = if std::path::Path::new(path).is_absolute() {
                PathBuf::from(path)
            } else {
                cwd.join(path)
            };
            if fp.is_dir() && recursive {
                std::fs::remove_dir_all(&fp)?;
            } else if fp.is_dir() {
                std::fs::remove_dir(&fp)?;
            } else {
                std::fs::remove_file(&fp)?;
            }
            Ok(serde_json::json!({"removed": path}))
        }
        "shell.touch" => {
            let file = args["file"].as_str().unwrap_or("");
            let fp = if std::path::Path::new(file).is_absolute() {
                PathBuf::from(file)
            } else {
                cwd.join(file)
            };
            if let Some(parent) = fp.parent() {
                let _ = std::fs::create_dir_all(parent);
            }
            std::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(&fp)?;
            Ok(serde_json::json!({"touched": file}))
        }

        // ── G-09: 用户工具发现 ───────────────────────────────────────
        "tool.list_user" => Ok(crate::commands::tools_cmd::list_user_tools_json()),

        // ── Web fetch (native Rust: reqwest + scraper + htmd) ────────────────
        // Replaces Python web_fetcher.py — no httpx/readability-lxml/html2text needed.
        // Parameters: url (required), max_chars (default 8000), selector (optional CSS)
        "web.fetch" => web_tools::fetch(args).await,
        "tool.run_user" => {
            // 按名称执行用户注册的工具（安全：cmd 来自 ~/.evocli/user_tools.toml，非用户输入）
            let name = args["name"].as_str().unwrap_or("");
            let extra = args["args"].as_str().unwrap_or("");
            let dry_run = args["dry_run"].as_bool().unwrap_or(false);
            let list = crate::commands::tools_cmd::list_user_tools_json();
            let tools = list["tools"].as_array().cloned().unwrap_or_default();
            let found = tools.iter().find(|t| t["name"].as_str() == Some(name));
            match found {
                None => anyhow::bail!(
                    "User tool '{}' not registered. Use: evocli tool register",
                    name
                ),
                Some(tool) => {
                    let base_cmd = tool["cmd"].as_str().unwrap_or("");
                    let full_cmd = if extra.is_empty() {
                        base_cmd.to_string()
                    } else {
                        format!("{} {}", base_cmd, extra)
                    };

                    security.validate_shell_cmd(&full_cmd)?;
                    // Use spawn_blocking to avoid blocking the tokio async executor
                    // (mirrors the shell.run fix — user tools can run long-lived processes).
                    let full_cmd_owned = full_cmd.clone();
                    let cwd_clone = cwd.clone();
                    let output = tokio::task::spawn_blocking(move || {
                        tools::run_command(&full_cmd_owned, &cwd_clone, 60, dry_run)
                    })
                    .await
                    .map_err(|e| anyhow::anyhow!("spawn_blocking join error: {}", e))??;
                    Ok(serde_json::json!({
                        "name":      name,
                        "cmd":       full_cmd,
                        "exit_code": output.exit_code,
                        "stdout":    output.stdout,
                        "stderr":    output.stderr,
                        "dry_run":   dry_run,
                    }))
                }
            }
        }

        unknown => {
            anyhow::bail!("Unknown tool: {}", unknown)
        }
    }
}

// ── 代码搜索（简单 grep 实现）────────────────────────────────────

#[derive(serde::Serialize)]
pub struct SearchMatch {
    pub file: String,
    pub line: u32,
    pub content: String,
}

fn search_code(query: &str, root: &std::path::Path) -> Result<Vec<SearchMatch>> {
    let mut matches = Vec::new();
    let extensions = ["rs", "py", "ts", "tsx", "js", "go"];

    for entry in walkdir::WalkDir::new(root)
        .follow_links(false)
        .into_iter()
        .filter_map(|e| e.ok())
        .filter(|e| e.file_type().is_file())
    {
        let path = entry.path();
        let ext = path.extension().and_then(|e| e.to_str()).unwrap_or("");
        if !extensions.contains(&ext) {
            continue;
        }
        // skip target / node_modules / .git
        if path.to_str().map_or(false, |s| {
            s.contains("\\target\\")
                || s.contains("/target/")
                || s.contains("node_modules")
                || s.contains(".git")
        }) {
            continue;
        }

        if let Ok(content) = std::fs::read_to_string(path) {
            for (i, line) in content.lines().enumerate() {
                if line.to_lowercase().contains(&query.to_lowercase()) {
                    matches.push(SearchMatch {
                        file: path.to_string_lossy().to_string(),
                        line: (i + 1) as u32,
                        content: line.trim().to_string(),
                    });
                    if matches.len() >= 100 {
                        return Ok(matches);
                    } // 上限 100 条
                }
            }
        }
    }
    Ok(matches)
}

// ── FIX-5: 单元测试 ──────────────────────────────────────────────────────────

/// Load ignore patterns from .evocliignore (project) and ~/.evocli/ignore (global).
/// Returns a pathspec-compatible list of glob patterns to exclude.
///
/// File format: same as .gitignore — one pattern per line, # for comments.
/// This is used by search.code, shell.grep, and index to filter out noise
/// (node_modules, build artifacts, generated code, etc.).
pub fn load_evocliignore() -> Vec<String> {
    let mut patterns: Vec<String> = Vec::new();

    // Built-in always-ignored paths (saves users from having to list them)
    let builtins = [
        "node_modules/",
        "target/",
        ".git/",
        "dist/",
        "build/",
        "__pycache__/",
        ".pytest_cache/",
        ".mypy_cache/",
        "*.pyc",
        "*.pyo",
        "*.class",
        "*.min.js",
        "*.min.css",
    ];
    for p in &builtins {
        patterns.push(p.to_string());
    }

    // Project-level .evocliignore
    let project_ignore = std::path::Path::new(".evocliignore");
    if let Ok(content) = std::fs::read_to_string(project_ignore) {
        for line in content.lines() {
            let line = line.trim();
            if !line.is_empty() && !line.starts_with('#') {
                patterns.push(line.to_string());
            }
        }
    }

    // Global ~/.evocli/ignore
    if let Some(home) = dirs::home_dir() {
        let global_ignore = home.join(".evocli").join("ignore");
        if let Ok(content) = std::fs::read_to_string(global_ignore) {
            for line in content.lines() {
                let line = line.trim();
                if !line.is_empty() && !line.starts_with('#') {
                    patterns.push(line.to_string());
                }
            }
        }
    }

    patterns
}

/// Check if a path should be excluded based on evocliignore patterns.
pub fn is_ignored(path: &std::path::Path, patterns: &[String]) -> bool {
    let path_str = path.to_string_lossy();
    let path_str_fwd = path_str.replace('\\', "/");
    for pattern in patterns {
        let pat = pattern.trim_end_matches('/');
        // Simple glob: check if path contains the pattern segment or matches suffix
        if path_str_fwd.contains(pat) || path_str_fwd.ends_with(pat) {
            return true;
        }
        // Wildcard extension patterns (e.g. *.pyc)
        if pat.starts_with("*.") {
            let ext = &pat[1..]; // ".pyc"
            if path_str_fwd.ends_with(ext) {
                return true;
            }
        }
    }
    false
}

#[cfg(test)]
mod tests {
    use super::*;
    use soul_bridge::ToolCallRequest;
    use std::path::PathBuf;

    fn root() -> String {
        // CARGO_MANIFEST_DIR = crates/host  →  ../.. = project root
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .parent()
            .unwrap()
            .to_string_lossy()
            .to_string()
    }

    fn req(tool: &str, args: serde_json::Value) -> ToolCallRequest {
        ToolCallRequest {
            id: "test".into(),
            tool: tool.into(),
            args,
        }
    }

    async fn test_dispatch(req: &ToolCallRequest) -> Result<Value> {
        let cfg = crate::config::Config::default();
        dispatch(req, None, &cfg).await
    }

    // ── fs.read ───────────────────────────────────────────────────────
    #[tokio::test]
    async fn test_fs_read_cargo_toml() {
        let r = test_dispatch(&req(
            "fs.read",
            serde_json::json!({
                "path": "Cargo.toml",
                "_cwd": root()
            }),
        ))
        .await;
        // Either succeeds or fails gracefully — no panic
        let _ = r; // just ensure it doesn't panic
    }

    #[tokio::test]
    async fn test_fs_read_nonexistent() {
        let r = test_dispatch(&req(
            "fs.read",
            serde_json::json!({
                "path": "does_not_exist_xyz.txt",
                "_cwd": root()
            }),
        ))
        .await;
        assert!(r.is_err(), "Reading nonexistent file should error");
    }

    // ── search.code ───────────────────────────────────────────────────
    #[tokio::test]
    async fn test_search_code_basic() {
        let r = test_dispatch(&req(
            "search.code",
            serde_json::json!({
                "query": "EvoCLI",
                "path": "crates/host/src",
                "_cwd": root()
            }),
        ))
        .await;
        assert!(r.is_ok(), "search.code should succeed: {:?}", r.err());
    }

    // ── shell.run dry_run ─────────────────────────────────────────────
    #[tokio::test]
    async fn test_shell_run_dry_run() {
        let r = test_dispatch(&req(
            "shell.run",
            serde_json::json!({
                "cmd": "cargo --version",
                "dry_run": true,
                "_cwd": root()
            }),
        ))
        .await;
        assert!(r.is_ok(), "shell.run dry_run should succeed");
        let stdout = r.unwrap()["stdout"].as_str().unwrap_or("").to_string();
        assert!(
            stdout.contains("[dry-run]"),
            "Should contain dry-run marker, got: {}",
            stdout
        );
    }

    // ── shell.run blocked ─────────────────────────────────────────────
    #[tokio::test]
    async fn test_shell_run_blocks_dangerous() {
        let r = test_dispatch(&req(
            "shell.run",
            serde_json::json!({
                "cmd": "rm -rf /",
                "dry_run": false,
                "_cwd": root()
            }),
        ))
        .await;
        assert!(r.is_err(), "Dangerous command must be blocked");
    }

    // ── shell.ls ──────────────────────────────────────────────────────
    #[tokio::test]
    async fn test_shell_ls_project_root() {
        let r = test_dispatch(&req(
            "shell.ls",
            serde_json::json!({
                "path": ".",
                "_cwd": root()
            }),
        ))
        .await;
        assert!(r.is_ok(), "shell.ls should succeed");
        let count = r.unwrap()["count"].as_u64().unwrap_or(0);
        assert!(count > 0, "Project root should have entries");
    }

    // ── unknown tool ──────────────────────────────────────────────────
    #[tokio::test]
    async fn test_unknown_tool_error() {
        let r = test_dispatch(&req("totally.unknown.xyz", serde_json::json!({}))).await;
        assert!(r.is_err());
        assert!(r.unwrap_err().to_string().contains("Unknown tool"));
    }

    // ── git.status ────────────────────────────────────────────────────
    #[tokio::test]
    async fn test_git_status() {
        let r = test_dispatch(&req(
            "git.status",
            serde_json::json!({
                "_cwd": root()
            }),
        ))
        .await;
        // May succeed or fail depending on git state; must not panic
        let _ = r;
    }

    // ── shell.wc ──────────────────────────────────────────────────────
    #[tokio::test]
    async fn test_shell_wc_cargo_toml() {
        let r = test_dispatch(&req(
            "shell.wc",
            serde_json::json!({
                "file": "Cargo.toml",
                "_cwd": root()
            }),
        ))
        .await;
        assert!(r.is_ok(), "shell.wc should succeed: {:?}", r.err());
        let lines = r.unwrap()["lines"].as_u64().unwrap_or(0);
        assert!(lines > 0, "Cargo.toml should have lines");
    }
}

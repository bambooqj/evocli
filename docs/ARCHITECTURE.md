# Architecture

EvoCLI uses a dual-engine design: a Rust Host that handles everything requiring speed, safety, or OS access; and a Python Soul that handles everything requiring flexibility, LLM integration, and evolving logic.

---

## Overview

```
User
 │
 ▼
┌─────────────────────────────────────────────────────────────────┐
│  Rust Host                                                      │
│                                                                 │
│  ┌──────────┐  ┌────────────┐  ┌──────────┐  ┌─────────────┐  │
│  │   TUI    │  │  Security  │  │  62 Rust │  │  SQLite /   │  │
│  │(ratatui) │  │  Sandbox   │  │  Tools   │  │  Code Index │  │
│  └────┬─────┘  └─────┬──────┘  └────┬─────┘  └──────┬──────┘  │
│       └──────────────┴──────────────┴────────────────┘         │
│                         IPC Bridge                              │
│                  (JSON-RPC over stdin/stdout)                   │
│                         IPC Bridge                              │
│       ┌──────────────────────────────────────────────┐         │
│       │             Python Soul                       │         │
│       │  Agent (Pydantic AI)  ·  LiteLLM Router       │         │
│       │  Memory (LanceDB)     ·  Skill Engine          │         │
│       │  Context Engine       ·  Evolution Engine      │         │
│       │  66 RPC Handlers      ·  55+ LLM-visible tools │         │
│       └──────────────────────────────────────────────┘         │
└─────────────────────────────────────────────────────────────────┘
```

---

## Rust Host Crates

| Crate | Responsibility |
|---|---|
| `evocli` (host) | CLI entry point, 15 subcommands, config loading, TUI launch |
| `evocli-tui` | Full-screen ratatui TUI: App state, event loop, renderer |
| `soul_bridge` | JSON-RPC IPC to Python Soul; streams, events, choice/approval channels |
| `code_intel` | tree-sitter AST indexing, LSP client, file watcher |
| `knowledge_graph` | BM25 (Tantivy), hybrid search (RRF), community detection, blast radius |
| `mem_router` | Self-training memory classifier (fastembed + linfa) |
| `tools` | Secure shell execution with whitelist/blacklist enforcement |
| `mcp` | MCP server and client |
| `contracts` | Task contract + checkpoint tracking (SQLite) |
| `protocol` | Shared `ToolCall` / `Event` type definitions |

---

## Python Soul Modules

The Soul lives in `evocli-soul/evocli_soul/`.

**Core pipeline** (per user message):
```
main.py (JSON-RPC server)
  └─ router.py            dispatch to handler
       └─ handlers/agent.py
            └─ agent.py (EvoCLIAgent)
                 ├─ context_engine.py  build RAG context (P1/P2/P3 tiers)
                 ├─ llm_client.py      LiteLLM router → LLM API
                 └─ memory_client.py   record + recall (LanceDB / SQLite)
```

**Key modules**:

| Module | Role |
|---|---|
| `agent.py` | Pydantic AI agent, 22 registered tools, Architect/Editor dual-model mode |
| `llm_client.py` | LiteLLM Router with fast/smart tiers, retries, cost tracking |
| `memory_client.py` | LanceDB vector memory + jina-embeddings + SQLite FTS fallback |
| `skill_engine.py` | Load, validate, and execute TOML skills; circuit breaker |
| `context_engine.py` | Token-budget-aware context assembly (P1=4k, P2=2k, P3=1.5k) |
| `evolution/` | PrefixSpan pattern detection, skill draft generation, decay detection |
| `handlers/` | 66 RPC handlers grouped by domain (agent, memory, skill, system, ...) |

---

## JSON-RPC Protocol

All IPC uses line-delimited JSON over the Python process's stdin/stdout.

**Rust → Python (tool call request)**:
```json
{ "id": "uuid", "method": "tool.call",
  "params": { "tool": "fs.read", "args": { "path": "src/main.rs" } } }
```

**Python → Rust (response)**:
```json
{ "id": "uuid", "result": "file contents here...", "error": null }
```

**Python → Rust (stream chunk)**:
```json
{ "method": "stream.chunk",
  "params": { "id": "stream-uuid", "text": "Hello ", "done": false } }
```

**Python → Rust (event notification)**:
```json
{ "method": "event.emit",
  "params": { "type": "soul_status", "status": "loading",
              "message": "Loading embedding model..." } }
```

**Python → Rust (tool call, reverse direction)**:
```json
{ "id": "uuid", "method": "tool.call",
  "params": { "tool": "shell.run", "args": { "cmd": "cargo check" } } }
```

---

## Memory System

```
User message
     │
     ▼
context_engine.py
  ├─ P1 (4k tokens)  ─ critical constraints + current file
  ├─ P2 (2k tokens)  ─ tool usage patterns + recent facts
  └─ P3 (1.5k tokens) ─ global preferences + cross-project knowledge

memory_client.py
  ├─ LanceDB  ─ jinaai/jina-embeddings-v2-base-zh (768-dim, bilingual)
  │             vector similarity search, semantic recall
  └─ SQLite FTS ─ BM25 keyword search, fallback when LanceDB unavailable

After session:
  memory_distill.py ─ extract key facts, write to L2 memory
```

---

## Tool Security

Every `shell.run` call goes through this chain:

```
Python bridge.call("shell.run", {"cmd": "..."})
  └─ Rust tool_dispatch.rs
       └─ SecurityController::validate_shell_cmd(cmd)
            ├─ Step 1: SHELL_BLOCKED_DANGEROUS  (hardcoded, always enforced)
            ├─ Step 2: extra_blocked_patterns    (from config.toml)
            ├─ Step 3: if allow_all_commands → allow
            └─ Step 4: extra_allowed_commands    (strict mode whitelist)
```

File system access goes through `validate_path_access()`:

```
├─ PATH_DENY_IMMUTABLE  (hardcoded, cannot be bypassed by any config)
│    includes: .evocli/config.toml, .ssh, /etc/passwd, etc.
├─ if allow_all_paths → allow remaining
└─ extra_denied_paths   (from config.toml)
```

**Why `config.toml` is in `PATH_DENY_IMMUTABLE`**: if the AI could write `config.toml`, it could set `block_dangerous_always = false` and bypass the entire security model — a bootstrapping attack. The immutable list in Rust source code prevents this.

---

## TUI Architecture

The TUI (`crates/tui/`) has four files:

- **`app.rs`** — `App` struct (all mutable state), `AppState` enum, message list, scroll offset, notification system
- **`event_handler.rs`** — converts `crossterm::event::KeyEvent` → `EventAction` enum; handles all input states
- **`ui.rs`** — pure rendering: `draw_title_bar`, `draw_chat_area`, `draw_input_area`, `draw_status_bar`, modals
- **`lib.rs`** — async event loop (`tokio::select!`): keyboard, stream chunks, soul events, blink timer, approval/choice polling

**Virtual scrolling**: `build_all_lines()` pre-renders all messages into a `Vec<Line<'static>>` cache. Scroll uses visual-row counting (handles word-wrap correctly) rather than naive line indexing.

**Streaming**: chunks arrive via `mpsc::unbounded_channel`. The event loop sends each chunk to `app.append_token()`, which finds the last `ChatMessage::Assistant` via `iter_mut().rev().find()` (necessary because soul events may push System messages during streaming).

---

## Skill System

Skills are TOML files. Example:

```toml
[skill]
id   = "fix_compile_error"
name = "Fix Compile Error"

[[steps]]
action = "shell.run"
params = { cmd = "cargo check 2>&1" }

[[steps]]
action = "llm.analyze"
params = { prompt_template = "fix_error", input = "${steps.0.result}" }

[[steps]]
action = "fs.apply_diff"
params = { path = "${steps.1.file}", diff = "${steps.1.diff}" }
```

The `SkillEngine` in Python loads TOML skills from `~/.evocli/skills/` and the project's `.evocli/skills/`. Built-in skills live in `evocli-soul/evocli_soul/builtin_skills/`.

---

## Adding New Capabilities

See [CONTRIBUTING.md](../CONTRIBUTING.md) for step-by-step guides on adding Rust tools and Python RPC handlers.

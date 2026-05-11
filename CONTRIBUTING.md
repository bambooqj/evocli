# Contributing to EvoCLI

Thank you for your interest in contributing! This guide walks you through setting up a development environment and submitting changes.

## Table of Contents

- [Development Setup](#development-setup)
- [Project Structure](#project-structure)
- [Building and Running](#building-and-running)
- [Testing](#testing)
- [Code Style](#code-style)
- [Adding New Capabilities](#adding-new-capabilities)
- [Pull Request Process](#pull-request-process)
- [Issue Labels](#issue-labels)

---

## Development Setup

**Prerequisites**

| Tool | Version | Purpose |
|---|---|---|
| Rust | 1.82+ | Rust host compilation |
| Python | 3.11+ | Python Soul runtime |
| uv | latest (optional) | Fast Python env management |
| git | any | Version control |

**Clone and install**

```bash
git clone https://github.com/bambooqj/evocli.git
cd evocli

# Install Python Soul in editable mode
pip install -e evocli-soul/

# Or with uv (recommended)
uv pip install -e evocli-soul/
```

---

## Project Structure

```
crates/host/          CLI entry, commands, config, security, tool dispatch
crates/soul_bridge/   JSON-RPC bridge between Rust and Python
crates/tui/           Terminal UI (ratatui): app state, event handler, renderer
crates/code_intel/    tree-sitter indexing, LSP client, file watcher
crates/knowledge_graph/ BM25 index, hybrid search, community detection
crates/mem_router/    Self-training memory classifier
crates/tools/         Shell command execution with security checks
crates/mcp/           MCP server and client
evocli-soul/
  evocli_soul/
    agent.py          Pydantic AI Agent, tool definitions
    llm_client.py     LiteLLM router (fast/smart tiers)
    memory_client.py  LanceDB + SQLite memory
    skill_engine.py   TOML skill loading and execution
    context_engine.py Token budget management
    evolution/        Pattern detection, skill drafting
    handlers/         One file per RPC method group
```

---

## Building and Running

**Development mode** (uses local Python source):

```bash
# Windows
$env:EVOCLI_SOUL = "evocli-soul/evocli_soul/main.py"
cargo run -p evocli

# Linux / macOS
EVOCLI_SOUL=evocli-soul/evocli_soul/main.py cargo run -p evocli
```

**Compile checks without running**:

```bash
cargo check --workspace       # fast type check
cargo check -p evocli-tui     # single crate
```

**Release build**:

```bash
cargo build --release -p evocli
# Binary: target/release/evocli(.exe)
```

**Full distribution package**:

```powershell
.\scripts\build_dist.ps1 -Clean   # Windows
```

---

## Testing

**Rust tests**:

```bash
cargo test --workspace
cargo test -p evocli-tui      # single crate
```

**Python tests**:

```bash
python -m pytest evocli-soul/tests/ -v
```

**Python Soul manual test** (ping/pong without TUI):

```bash
echo '{"id":"1","method":"tracer.ping","params":{}}' | \
  python evocli-soul/evocli_soul/main.py
```

---

## Code Style

**Rust**:

```bash
cargo fmt --all               # auto-format
cargo clippy -- -D warnings   # lint (all warnings become errors)
```

**Python**:

```bash
ruff check evocli-soul/       # lint
ruff format evocli-soul/      # auto-format
```

Both formatters are enforced in CI. PRs that fail either check will not be merged.

---

## Adding New Capabilities

### New Rust tool (callable by Python Soul)

1. Open `crates/host/src/tool_dispatch.rs`
2. Add a new match arm:

```rust
"mymod.my_tool" => {
    let param = args["param"].as_str().unwrap_or("");
    // ... implementation ...
    Ok(serde_json::json!({ "result": value }))
}
```

3. Add the tool to the security audit log call if it performs privileged actions.

### New Python RPC handler

1. Create `evocli-soul/evocli_soul/handlers/mymodule.py`:

```python
import logging
log = logging.getLogger("evocli.handlers.mymodule")

def register(router) -> None:
    router.add("mymod.my_method", handle_my_method)

async def handle_my_method(req_id: str, params: dict, send, state) -> None:
    try:
        result = ...
        await send.response(req_id, result)
    except Exception as e:
        log.exception("my_method failed")
        await send.error(req_id, -32603, str(e))
```

2. Register in `evocli-soul/evocli_soul/handlers/__init__.py`:

```python
from . import mymodule

def register_all(router):
    # ... existing registrations ...
    mymodule.register(router)
```

### New TUI feature

The TUI lives in `crates/tui/src/`:

- `app.rs` — `App` state struct and `AppState` enum
- `event_handler.rs` — keyboard event routing and `EventAction` enum
- `ui.rs` — all rendering functions (ratatui Widgets)
- `lib.rs` — main event loop (`tokio::select!`)

Follow the existing patterns. Run `cargo check -p evocli-tui` frequently.

---

## Pull Request Process

1. **Fork** the repository and create a branch from `master`.
2. **Write code** and keep commits focused (one logical change per commit).
3. **Test** locally: `cargo test`, `cargo clippy`, `cargo fmt --check`.
4. **Open a PR** with a clear title and description. Reference any related issues.
5. A maintainer will review within a few days.

**Commit message format** (conventional commits preferred):

```
feat(tui): add token progress bar to title bar
fix(security): strip \\?\ prefix from PYTHONPATH on Windows
docs: update CONTRIBUTING.md with Python handler guide
```

---

## Issue Labels

| Label | Meaning |
|---|---|
| `bug` | Something is broken |
| `enhancement` | New feature or improvement |
| `documentation` | Docs update needed |
| `good first issue` | Approachable for first-time contributors |
| `help wanted` | Extra attention or expertise needed |
| `question` | Further information needed |

---

## Code of Conduct

Please follow our [Code of Conduct](CODE_OF_CONDUCT.md) in all project spaces.

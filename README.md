# EvoCLI

[![CI](https://github.com/bambooqj/evocli/actions/workflows/ci.yml/badge.svg)](https://github.com/bambooqj/evocli/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-MIT%2FApache--2.0-blue.svg)](LICENSE)
[![Rust](https://img.shields.io/badge/rust-1.85%2B-orange.svg)](https://www.rust-lang.org)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey.svg)]()

**AI coding runtime — local-first, long memory, self-evolving**

[简体中文](README.zh-CN.md)

---

```text
╭ EvoCLI  gpt-4o-mini  ⌂ ~/projects/myapp  [████░░░░░░] 15k/128k  12%
╭ Messages ──────────────────────────────────────────────────────────────────╮
│  ▌ You                                                                      │
│    Analyze the project architecture                                         │
│                                                                             │
│  ◆ gpt-4o-mini                                                              │
│  ⚙ Building context…  🧠 Searching memory…  📊 Scanning codebase…          │
│                                                                             │
│    ## Architecture Overview                                                 │
│    Rust Host (immutable) ↔ Python Soul (evolvable) via JSON-RPC            │
│    64 tools registered · Intent-aware routing · Auto tool flows            │
╰─────────────────────────────────────────────────────────────────────────────╯
 ● Ready   Ctrl+C:quit  Enter:send  PgUp/Dn:scroll  Ctrl+Y:copy  /help:cmds
```

## Features

### Core
- **Full-screen TUI** — ratatui terminal UI with streaming responses, real-time token context bar, thinking animation, progress indicators during context building
- **64 AI-visible tools** — file system, git, shell (Rust-native cross-platform), code intelligence, memory, web fetch, approval prompts
- **Long-term memory** — LanceDB vector memory (jina-embeddings-v2-base-zh, 768-dim bilingual) + SQLite FTS fallback
- **Multi-provider LLM** — OpenAI, Anthropic, DeepSeek, Ollama via LiteLLM; any OpenAI-compatible API; per-role model config

### Intelligent Tooling
- **Dynamic tool routing** — intent-aware selection sends only 12 relevant tools per request (saves ~55% context tokens). 3-stage pipeline: keyword gate → tag matching → embedding similarity
- **Auto tool flow learning** — repeating tool sequences (e.g. `symbol_lookup → fs_read_range → fs_apply_search_replace → fs_lint_file`) are automatically abstracted into named workflows and suggested on future similar tasks
- **Native web fetch** — `web.fetch` built in Rust (reqwest + scraper + htmd): fetches any URL and returns clean Markdown. No browser, no curl, no Python HTTP dependencies
- **Executable skills** — TOML-defined multi-step workflows; AI discovers and runs them automatically

### Shell Layer (Cross-Platform Rust Native)
All shell utilities are pure Rust implementations — no system shell required:

| Tool | Implementation |
|---|---|
| `shell.ls`, `shell.find` | `std::fs::read_dir` + `walkdir` |
| `shell.cat`, `shell.head`, `shell.tail`, `shell.wc` | `std::fs::read_to_string` |
| `shell.mkdir`, `shell.mv`, `shell.cp`, `shell.touch`, `shell.rm` | `std::fs` ops |
| `shell.grep` | Rust regex + walkdir |
| `shell.run` | bash (Git Bash/WSL) → pwsh → powershell fallback on Windows |

### Security & Configuration
- **All security settings in `config.toml`** — allowed commands, blocked patterns, denied paths are all user-configurable lists with sensible defaults. No hardcoded restrictions
- **Blacklist mode by default** — AI can run any whitelisted command; `config.toml` is the only code-level protected file (AI cannot modify its own security rules)
- **Project-local config** — `.evocli/config.toml` per-project overrides merge with global `~/.evocli/config.toml`

### Code Intelligence
- tree-sitter AST + BM25 full-text + PageRank hybrid search
- Blast radius analysis, incoming/outgoing call chains, community detection
- MCP native — serve and consume Model Context Protocol

---

## Quick Start

### Download pre-built binary

Visit [Releases](https://github.com/bambooqj/evocli/releases) and grab the package for your platform.

```powershell
# Windows
.\setup.ps1          # First run: installs Python environment (2-5 min, once only)
.\evocli.exe init    # Configure LLM provider + API key
.\evocli.exe         # Launch TUI
```

```bash
# Linux / macOS
bash setup.sh
./evocli init
./evocli
```

### Build from source

**Requirements**: Rust 1.85+, Python 3.11+

```bash
git clone https://github.com/bambooqj/evocli.git
cd evocli

# Development mode (Windows)
$env:EVOCLI_SOUL = "evocli-soul/evocli_soul/main.py"
cargo run -p evocli

# Development mode (Linux/macOS)
EVOCLI_SOUL=evocli-soul/evocli_soul/main.py cargo run -p evocli
```

### Configure API key

```bash
evocli init   # Interactive wizard — stores key in system keyring

# Or set an environment variable:
export OPENAI_API_KEY="sk-..."          # OpenAI / any OpenAI-compatible API
export ANTHROPIC_API_KEY="sk-ant-..."   # Anthropic Claude
export DEEPSEEK_API_KEY="..."           # DeepSeek
```

See [docs/CONFIGURATION.md](docs/CONFIGURATION.md) for all configuration options.

---

## TUI Keyboard Shortcuts

| Key | Action |
|---|---|
| `Enter` | Send message |
| `Shift+Enter` | Insert newline (multi-line input) |
| `Tab` | Autocomplete `/` commands |
| `Esc` | Interrupt generation / close modal |
| `PageUp / PageDown` | Scroll chat history |
| `Home / End` | Jump to oldest / newest message |
| `Alt+Up / Alt+Down` | Scroll 5 rows fast |
| `Ctrl+Y` | Copy last AI message to clipboard |
| `Ctrl+L` | Clear screen |
| `F12` | Toggle debug log panel |
| `Ctrl+C` | Exit |

**Text selection & copy:**
- **Default** (`enable_mouse = false`): Click + drag for native terminal selection, Ctrl+C to copy
- **Mouse mode** (`enable_mouse = true` in config): Mouse wheel scrolls messages, Ctrl+Y copies last AI message

```toml
# ~/.evocli/config.toml
[tui]
enable_mouse = false   # false = native terminal selection (default)
                       # true  = mouse wheel scroll
```

---

## Slash Commands

| Command | Description |
|---|---|
| `/help` | Show all commands and keyboard shortcuts |
| `/compress` | Compress session history to free context space |
| `/flows` | List automatically learned tool workflows |
| `/add <file>` | Pin a file to context for all turns |
| `/chain <symbol>` | Visualize function call chain |
| `/skills` | List available skills |
| `/skill <name>` | Run a skill |
| `/cost` | Session cost and token usage |
| `/index` | Re-index project code symbols |
| `/memory <query>` | Search project memory |
| `/clear` | Clear chat history |
| `/log [N]` | Show last N log lines (default 30) |

---

## Configuration

All behaviour is controlled by `~/.evocli/config.toml` (global) and `.evocli/config.toml` (project-local). Project config is deep-merged over global.

### Quick reference

```toml
[llm]
base_url  = "https://api.openai.com/v1"   # any OpenAI-compatible endpoint
# api_key stored in OS keyring via evocli init

[llm.tiers]
fast  = "gpt-4o-mini"   # fast tasks: commits, lint, Q&A
smart = "gpt-4o"        # complex: architecture, refactoring

[llm.roles.architect]   # per-role model override
model    = "claude-opus-4-5"
base_url = "https://api.anthropic.com"

[agent]
first_chunk_timeout_s = 120  # seconds before "No response" error (default 120)
max_tool_calls        = 20

[tui]
enable_mouse = false   # true = mouse wheel scroll; false = native selection

[security]
allow_all_commands    = true       # blacklist mode (default)
allowed_commands      = ["cargo", "git", "python", ...]   # full whitelist (replaceable)
blocked_patterns      = ["rm -rf /", "mkfs", ...]         # dangerous patterns (replaceable)
extra_allowed_commands = ["docker", "kubectl"]             # additive
extra_blocked_patterns = ["curl | bash"]                   # additive
```

Full reference: [docs/CONFIGURATION.md](docs/CONFIGURATION.md)

---

## Security Model

EvoCLI uses a **config-driven** security model — all lists are in `config.toml`, nothing is hardcoded except the config file itself:

- `allow_all_commands = true` (default) — blacklist mode: AI can run any command except `blocked_patterns`
- `allow_all_commands = false` — strict whitelist: only `allowed_commands` + `extra_allowed_commands`
- `allow_all_paths = true` (default) — no path restrictions; add `denied_paths` to restrict

The single code-level protection: `~/.evocli/config.toml` itself is permanently off-limits to the AI agent, preventing it from modifying its own security rules or reading your API keys.

---

## Project Structure

```
evocli/
├── crates/
│   ├── host/            CLI entry, config, security, git, web fetch (15 RS files)
│   ├── soul_bridge/     Rust↔Python JSON-RPC bridge
│   ├── tui/             Full-screen TUI (ratatui) — mouse config, Ctrl+Y copy
│   ├── code_intel/      Symbol indexing (tree-sitter + BM25 + LSP)
│   ├── knowledge_graph/ Blast radius + community detection
│   ├── mem_router/      Self-training memory classifier
│   ├── tools/           Secure command execution (cross-platform shell)
│   └── mcp/             MCP server/client
├── evocli-soul/
│   └── evocli_soul/     Python Soul (66 modules)
│       ├── agent.py           Pydantic AI Agent (64 tools) + LiteLLM
│       ├── tool_registry.py   Single source of truth for all 66 tools
│       ├── tool_router.py     Intent-aware dynamic tool selection + memory scoring
│       ├── tool_flow_miner.py Auto tool workflow learning and execution
│       ├── memory_client.py   LanceDB vector memory
│       ├── skill_engine.py    TOML skill loader and executor
│       ├── context_engine.py  Token budget + context assembly + progress events
│       └── handlers/          RPC handlers
├── docs/          Documentation
├── scripts/       Build and deploy scripts
└── skills/        Built-in skill definitions
```

---

## Documentation

| Document | Description |
|---|---|
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Every config option: LLM, agent, security, tui, context |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Dual-engine design, crate map, JSON-RPC, memory/security |
| [docs/TOOLS_REFERENCE.md](docs/TOOLS_REFERENCE.md) | All 64+ Python tools + Rust tools — params, returns, examples |
| [docs/SKILLS_GUIDE.md](docs/SKILLS_GUIDE.md) | TOML skills, actions, variable interpolation, prompt templates |
| [docs/MEMORY_SYSTEM.md](docs/MEMORY_SYSTEM.md) | LanceDB, distillation, embeddings, context injection |
| [docs/PROTOCOL.md](docs/PROTOCOL.md) | JSON-RPC protocol spec, event types, handler authoring |
| [docs/TUI_INTERNALS.md](docs/TUI_INTERNALS.md) | App state, event loop, renderer, virtual scrolling |
| [docs/ROADMAP.md](docs/ROADMAP.md) | v0.1.0 shipped, v0.2.0 planned |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Dev setup, build, test, code style, PR process |
| [CHANGELOG.md](CHANGELOG.md) | Release history |

---

## Architecture Data Flow / 架构数据流向图

The diagram below maps every layer of EvoCLI — from key press to LLM response — including all branch points, state transitions, and file references.

> **[🔍 Open Interactive Diagram (中英双语)](https://htmlpreview.github.io/?https://github.com/bambooqj/evocli/blob/main/docs/dataflow.html)** — hover nodes to trace data flow, click to expand `file:line` references, toggle layers.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  L1 用户输入        L2 流分发          L3 Python Soul     L4 Agent循环       │
│  User Input         Stream Dispatch    Entry               Exec Loop          │
│                                                                               │
│  键盘事件           bridge.call_       router.dispatch()  for iter in        │
│  KeyEvent     ───▶  stream()     ───▶  run_agent_   ───▶  max_auto_iters:   │
│                     JSON-RPC           stream_body         LLM call           │
│  Enter ──▶ Submit   stdout/stdin       ↓                   ↓                 │
│  IME guard          ↓                  意图分类             工具调用           │
│  KeyEventKind       StreamChunk        Intent Classify      _execute_tool()   │
│  ::Press only       ::Text → TUI       8 intents            Python/Rust路由   │
│                     ::Event → State    ↓                   ↓                 │
│                                        上下文构建           断路器 circuit    │
│                                        context_engine       breaker (3次)     │
│                                                             ↓                 │
│  L5 记忆层          L6 审批门          AppState:           task_complete?    │
│  Memory             Gates              Idle→Thinking        ↙       ↘        │
│                                        →Streaming           继续     退出     │
│  memory_recall()    WaitingApproval    →CallingTool                          │
│  LanceDB vector     WaitingChoice      →WaitingApproval                      │
│  JSONL fallback     user: y/n          →Idle                                 │
│  P1/P2/P3 priority  user: 1-9                                                │
│  memory_distill ─▶  evolution_engine                                         │
│  (background)       (PrefixSpan)                                             │
└──────────────────────────────────────────────────────────────────────────────┘
```

[Full interactive diagram with hover/click](https://htmlpreview.github.io/?https://github.com/bambooqj/evocli/blob/main/docs/dataflow.html) | [Source](docs/dataflow.html)

## Contributing

Contributions are welcome — bug fixes, new features, documentation, new skills.

Please read [CONTRIBUTING.md](CONTRIBUTING.md) to get started.

- **Bug reports** — open an issue with the `bug` label
- **Feature requests** — open an issue with the `enhancement` label
- **Roadmap** — see [docs/ROADMAP.md](docs/ROADMAP.md)

## License

Licensed under either of:

- MIT License ([LICENSE](LICENSE))
- Apache License, Version 2.0

at your option.

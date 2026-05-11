# EvoCLI

[![License](https://img.shields.io/badge/license-MIT%2FApache--2.0-blue.svg)](LICENSE)
[![Rust](https://img.shields.io/badge/rust-1.82%2B-orange.svg)](https://www.rust-lang.org)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey.svg)]()
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

**AI coding runtime — local-first, long memory, self-evolving**

[简体中文](README.zh-CN.md)

---

```text
╭ EvoCLI  gpt-4o-mini  ⌂ ~/projects/myapp  [████░░░░░░] 15k/128k  12%
╭ Messages ──────────────────────────────────────────────────────────────────╮
│  ▌ You                                                                      │
│    Fix the authentication bug in src/auth.rs                               │
│                                                                             │
│  ◆ gpt-4o-mini                                                              │
│    I found the issue. The token validation skips the expiry check when     │
│    the user role is "admin". Here is the fix:                              │
│    ╭─ rust ──────────────────────────────────────────────────────────      │
│  │ - if user.role == Role::Admin { return Ok(()); }                        │
│  │ + if token.is_expired() { return Err(AuthError::TokenExpired); }       │
│    ╰─────────────────────────────────────────────────────────────          │
╰─────────────────────────────────────────────────────────────────────────────╯
 ● Ready   ^C:quit  Enter:send  PgUp/Dn:scroll  /help:cmds  F12:log
```

## Features

- **Full-screen TUI** — ratatui terminal UI with streaming responses, token progress bar, thinking animation
- **62 Rust tools** — file system, git, shell, code intelligence, memory, approval, interactive choice prompts
- **Long-term memory** — LanceDB vector memory (jina-embeddings-v2-base-zh, 768-dim bilingual) + SQLite FTS fallback
- **Multi-provider LLM** — OpenAI, Anthropic, DeepSeek, Ollama via LiteLLM router; any OpenAI-compatible API works
- **Executable skills** — TOML-defined multi-step workflows; AI can discover and run them automatically
- **Code intelligence** — tree-sitter AST + BM25 full-text + PageRank hybrid search across your entire codebase
- **MCP native** — serves and consumes Model Context Protocol; connect to external data sources and tools
- **Security by default** — blacklist model; `config.toml` is permanently inaccessible to the AI agent
- **Zero-setup deploy** — single ~14 MB binary; auto-installs Python deps via `uv` on first run

## Architecture

```text
┌─ Rust Host (immutable core) ────────────────────────────────────────────────┐
│  TUI rendering  ·  Security sandbox  ·  IPC dispatch  ·  SQLite  ·  Git     │
└──────────────────────────────┬──────────────────────────────────────────────┘
                               │  JSON-RPC over stdin/stdout
┌─ Python Soul (evolvable) ────┴──────────────────────────────────────────────┐
│  LLM calls (LiteLLM)  ·  Agent orchestration  ·  Skill execution  ·  Memory │
└─────────────────────────────────────────────────────────────────────────────┘
```

**Core constraint**: the Python Soul never touches the filesystem, shell, or database directly. Every operation goes through `bridge.call(tool, params)` to the Rust Host, which enforces the security sandbox.

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

**Requirements**: Rust 1.82+, Python 3.11+

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
export ANTHROPIC_API_KEY="sk-ant-..."   # Anthropic Claude
export OPENAI_API_KEY="sk-..."          # OpenAI GPT
export DEEPSEEK_API_KEY="..."           # DeepSeek
```

See [docs/config.toml.example](docs/config.toml.example) for all configuration options.

## TUI Keyboard Shortcuts

| Key | Action |
|---|---|
| `Enter` | Send message |
| `Shift+Enter` | Insert newline (multi-line input) |
| `Tab` | Autocomplete `/` commands |
| `Esc` | Interrupt generation / close modal |
| `PageUp / PageDown` | Scroll chat history |
| `Home / End` | Jump to oldest / newest message |
| `F12` | Toggle debug log panel (Esc to close) |
| `Ctrl+C` | Exit |

## Slash Commands

| Command | Description |
|---|---|
| `/help` | Show all available commands |
| `/chain <symbol>` | Visualize function call chain |
| `/skills` | List available skills |
| `/skill <name>` | Run a skill |
| `/cost` | Session cost and token usage |
| `/index` | Re-index project code symbols |
| `/memory <query>` | Search project memory |
| `/clear` | Clear chat history |
| `/log [N]` | Show last N log lines (default 30) |

## Security Model

EvoCLI uses a **blacklist** approach: the AI can execute any command except hardcoded dangerous operations (`rm -rf /`, `dd`, `mkfs`, `format c:`, and 18 more).

Critically, `~/.evocli/config.toml` is **permanently off-limits** to the AI agent. This prevents the AI from modifying its own security rules or reading your API keys — even if it tries.

Users control all policy via `config.toml` (humans only):

```toml
[security]
extra_blocked_patterns = ["curl * | bash"]   # add custom dangerous patterns
extra_denied_paths     = ["/prod"]           # restrict directory access
allow_all_commands     = false               # switch to strict whitelist mode
```

## Project Structure

```
evocli/
├── crates/
│   ├── host/            CLI entry, config, git, logging (15 Rust files)
│   ├── soul_bridge/     Rust↔Python JSON-RPC bridge
│   ├── tui/             Full-screen TUI (ratatui)
│   ├── code_intel/      Symbol indexing (tree-sitter + LSP)
│   ├── knowledge_graph/ BM25 + community detection + blast radius
│   ├── mem_router/      Self-training memory classifier
│   ├── tools/           Secure command execution
│   └── mcp/             MCP server/client
├── evocli-soul/
│   └── evocli_soul/     Python Soul (43 modules)
│       ├── agent.py           Pydantic AI Agent + LiteLLM
│       ├── memory_client.py   LanceDB vector memory
│       ├── skill_engine.py    TOML skill loader and executor
│       ├── context_engine.py  Token budget + context assembly
│       ├── evolution/         Self-evolution engine (7 submodules)
│       └── handlers/          66 RPC handlers
├── docs/          Documentation and config examples
├── scripts/       Build and deployment scripts
└── skills/        Built-in skill definitions
```

## Documentation

| Document | Description |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Dual-engine design, crate map, JSON-RPC internals, memory/security/TUI deep dive |
| [docs/TOOLS_REFERENCE.md](docs/TOOLS_REFERENCE.md) | All 62 Rust tools + 55 Python tools — params, return values, examples |
| [docs/SKILLS_GUIDE.md](docs/SKILLS_GUIDE.md) | How to write TOML skills, all actions, variable interpolation, prompt templates |
| [docs/MEMORY_SYSTEM.md](docs/MEMORY_SYSTEM.md) | LanceDB + SQLite tiers, distillation, embedding model, context injection |
| [docs/PROTOCOL.md](docs/PROTOCOL.md) | JSON-RPC protocol spec, all message types, event types, handler authoring |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Every config option with types, defaults, and environment variable overrides |
| [docs/TUI_INTERNALS.md](docs/TUI_INTERNALS.md) | App state machine, event loop, renderer, virtual scrolling, adding widgets |
| [docs/ROADMAP.md](docs/ROADMAP.md) | v0.1.0 shipped, v0.2.0 planned, v1.0 vision |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Dev setup, build, test, code style, PR process |
| [CHANGELOG.md](CHANGELOG.md) | Release history |

## Contributing

Contributions from everyone are welcome — whether that is a bug fix, new feature, documentation improvement, or a new built-in skill.

Please read [CONTRIBUTING.md](CONTRIBUTING.md) to get started.

- **Bug reports** — open an issue with the `bug` label
- **Feature requests** — open an issue with the `enhancement` label
- **Roadmap discussion** — see [docs/ROADMAP.md](docs/ROADMAP.md)

## License

Licensed under either of:

- MIT License ([LICENSE](LICENSE))
- Apache License, Version 2.0

at your option.

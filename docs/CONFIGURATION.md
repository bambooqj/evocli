# Configuration Reference

All configuration options for EvoCLI. The config file lives at `~/.evocli/config.toml` (global) or `{project}/.evocli/config.toml` (project-specific).

**Priority** (highest to lowest):
1. Environment variables
2. Project config `{project}/.evocli/config.toml`
3. Global config `~/.evocli/config.toml`
4. Built-in defaults

Run `evocli init` for an interactive setup wizard. See `docs/config.toml.example` for a full annotated example.

---

## `[llm]` — Language Model

```toml
[llm]
provider = "anthropic"   # "anthropic" | "openai" | "deepseek" | "ollama"

# API key (prefer system keyring via `evocli init`)
# api_key = "sk-ant-..."

# Custom API base URL (for proxies or self-hosted)
# base_url = "https://your-proxy.com/v1"

[llm.tiers]
# Fast tier: quick tasks, commit messages, lint fixes
fast  = "claude-3-5-haiku-latest"   # or "gpt-4o-mini", "deepseek-chat"

# Smart tier: complex reasoning, architecture, refactoring
smart = "claude-sonnet-4-5-20250514"  # or "gpt-4o", "deepseek-reasoner"
```

**Environment variable alternatives** (override `api_key`):
```bash
ANTHROPIC_API_KEY="sk-ant-..."
OPENAI_API_KEY="sk-..."
DEEPSEEK_API_KEY="..."
```

**Provider notes**:
- `anthropic`: Claude models. Requires `ANTHROPIC_API_KEY`.
- `openai`: GPT models. Requires `OPENAI_API_KEY`. Works with any OpenAI-compatible API via `base_url`.
- `deepseek`: DeepSeek models. Requires `DEEPSEEK_API_KEY`.
- `ollama`: Local models. Set `base_url = "http://localhost:11434"`. No API key needed.

---

## `[llm.tiers]` — Model Tiers

EvoCLI routes requests to different models based on task complexity.

| Tier | When used | Recommended |
|---|---|---|
| `fast` | Commit messages, lint, simple Q&A | claude-3-5-haiku, gpt-4o-mini |
| `smart` | Refactoring, architecture, complex bugs | claude-sonnet-4-5, gpt-4o |

Python code can specify tier explicitly:
```python
result = await llm.complete(prompt, tier="smart")
```

---

## `[context]` — Token Budget

```toml
[context]
# Total token budget per request (default: 128,000)
# Lower this for faster/cheaper responses with less context
max_total = 128000

# Max tokens allocated to code context (symbols, current file)
max_code = 32000
```

**Budget allocation** (approximate):
- P1 constraints: 4,000 tokens
- P2 tool patterns: 2,000 tokens
- P3 global prefs: 1,500 tokens
- Code context: up to `max_code`
- Remaining: conversation history

---

## `[safety]` — Write Approval

```toml
[safety]
# Require user confirmation before AI writes files
# false (default): AI asks before each write
# true:  AI can write without asking (suitable for CI/automation)
auto_approve_writes = false
```

---

## `[security]` — Command Execution

```toml
[security]
# Command execution mode:
# true (default):  blacklist mode — allow all except SHELL_BLOCKED_DANGEROUS
# false:           strict mode — only extra_allowed_commands are permitted
allow_all_commands = true

# Path access mode:
# true:   allow all paths (PATH_DENY_IMMUTABLE still enforced)
# false (default): extra_denied_paths also enforced
allow_all_paths = false

# Permanent dangerous pattern blocking (strongly recommended: true)
block_dangerous_always = true

# Additional commands to allow in strict mode (allow_all_commands = false)
extra_allowed_commands = ["docker", "kubectl", "terraform"]

# Additional patterns to always block (supports glob * wildcard)
extra_blocked_patterns = [
    "curl * | bash",
    "wget * | sh",
    "rm -rf ${HOME}",
]

# Additional paths to deny (substring match, case-insensitive)
extra_denied_paths = [
    "/prod",
    "/etc/nginx",
]
```

**Important**: `~/.evocli/config.toml` itself is in `PATH_DENY_IMMUTABLE` — the AI can never read or modify this file. This prevents the AI from changing its own security rules.

---

## `[memory]` — Memory System

```toml
[memory]
# Maximum episodic memories to retain (default: 1,000)
# Older memories are pruned when this limit is reached
# Large projects benefit from higher values (e.g., 5,000)
max_episodes = 1000
```

---

## `[graph]` — Knowledge Graph

Advanced settings for the code intelligence graph. Most users do not need to change these.

```toml
[graph]
# Label Propagation community detection iterations (default: 20)
lpa_max_iter = 20

# Merge communities smaller than this (default: 2)
min_community_size = 2

# Blast radius BFS depth (default: 5)
blast_radius_depth = 5

# Reciprocal Rank Fusion k constant (default: 60.0)
# Higher = more uniform ranking across BM25 and vector results
rrf_k = 60.0

# BM25 weight in hybrid search (default: 0.4)
bm25_weight = 0.4

# Vector search weight in hybrid search (default: 0.6)
vector_weight = 0.6
```

---

## Soul Script Path

```toml
# Path to the Python Soul entry point
# Set by `evocli init` or the EVOCLI_SOUL environment variable
soul_script = "/path/to/evocli-soul/evocli_soul/main.py"
```

**Priority for soul script resolution**:
1. `EVOCLI_SOUL` environment variable
2. `soul_script` in `~/.evocli/config.toml`
3. Relative path `evocli-soul/evocli_soul/main.py` from CWD
4. Walking up from the binary location
5. Python module fallback: `evocli_soul.main`

---

## Project-local Config

Create `{project}/.evocli/config.toml` to override settings for a specific project:

```toml
# .evocli/config.toml (project-level — checked into git is OK)

[llm.tiers]
# Use a more powerful model for this complex project
smart = "claude-opus-4-5"

[context]
# This project has a large codebase — increase budget
max_code = 64000

[security]
# This project needs Docker access
extra_allowed_commands = ["docker", "docker-compose"]
extra_denied_paths = ["/etc/", "/prod/"]
```

Only the fields you specify are overridden. Everything else falls through to the global config.

---

## Environment Variables

| Variable | Purpose |
|---|---|
| `EVOCLI_SOUL` | Override path to Python Soul |
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `OPENAI_API_KEY` | OpenAI API key |
| `DEEPSEEK_API_KEY` | DeepSeek API key |
| `HF_ENDPOINT` | Hugging Face mirror URL (e.g., `https://hf-mirror.com`) |
| `EVOCLI_RESUME_SESSION` | Session ID to resume on startup |

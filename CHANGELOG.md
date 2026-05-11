# Changelog

All notable changes to EvoCLI will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

## [0.1.0] — 2026-05-12

### Added

**Core Runtime**
- Rust Host + Python Soul dual-engine architecture with JSON-RPC IPC
- 62 Rust-side tools (fs, git, shell, code_intel, memory, approval, prompt.choice)
- 55+ Python LLM-visible tools via Pydantic AI + LiteLLM router

**TUI**
- Full-screen ratatui terminal UI with Catppuccin × Tokyo Night color theme
- Streaming AI responses with live cursor animation
- Token usage progress bar with context-window fill indicator (`[████░░] 12%  15k/128k`)
- Thinking animation (`◆ model  ⠸`) in chat area while AI processes
- Word-wrap and virtual scrolling for all message types
- Notification bar (transient 6-second alerts between chat and input)
- Debug log overlay (F12) with auto-scroll
- `prompt.choice` interactive modal — AI presents numbered options, user picks or types custom answer
- Responsive layout (Wide ≥120 / Normal 60–119 / Compact 40–59 / Tiny <40)

**Memory System**
- LanceDB vector memory with `jinaai/jina-embeddings-v2-base-zh` (768-dim, bilingual)
- SQLite FTS fallback when LanceDB unavailable
- Background pre-warm so first response isn't blocked by model loading
- Memory distillation on session pause

**Code Intelligence**
- tree-sitter AST indexing (Rust, Python, JS, TS)
- BM25 full-text search (Tantivy embedded)
- PageRank-weighted symbol ranking
- Hybrid BM25 + vector search (RRF fusion)
- LSP client for incoming/outgoing call chains

**Skill System**
- TOML-defined executable skills with multi-step pipelines
- Built-in skills: TDD, brainstorming, debugging, code review, git workflow
- `/chain <symbol>` call-chain visualization in TUI

**Security**
- Blacklist security model (allow all, block known-dangerous)
- `PATH_DENY_IMMUTABLE`: AI cannot read/write `config.toml` or SSH keys
- `SHELL_BLOCKED_DANGEROUS`: 22 hardcoded patterns (rm -rf /, dd, mkfs, etc.)
- User-configurable `extra_blocked_patterns` in `config.toml`

**Providers & Integrations**
- OpenAI, Anthropic, DeepSeek, Ollama via LiteLLM router
- MCP server and client (`evocli mcp serve/connect/tools`)
- Auto Python environment setup via `uv` (zero manual pip)

**CLI Commands**
- `evocli` — launch TUI
- `evocli init` — interactive setup wizard
- `evocli doctor` — 10-point health check
- `evocli index` — code symbol indexing
- `evocli skill list/run/export/import`
- `evocli git status/commit/snapshot/restore`
- `evocli session list/resume/pause`
- `evocli mcp serve/connect/list/tools`
- `evocli stats` — flywheel metrics dashboard
- `evocli tool register/list` — user-defined tool registration

---

[Unreleased]: https://github.com/bambooqj/evocli/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/bambooqj/evocli/releases/tag/v0.1.0

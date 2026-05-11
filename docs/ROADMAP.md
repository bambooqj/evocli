# Roadmap

This document describes where EvoCLI is today and where it is headed.

Community input is welcome — open an issue with `[ROADMAP]` in the title to discuss ideas.

---

## v0.1.0 — Current

Released May 2026. Core runtime is complete and functional.

**TUI**
- [x] Full-screen ratatui terminal UI with streaming responses
- [x] Token usage progress bar (`[████░░░░░░] 15k/128k  12%`)
- [x] Thinking animation (`◆ model  ⠸`) in chat area
- [x] Responsive layout (Wide / Normal / Compact / Tiny)
- [x] Word-wrap with visual-row-aware virtual scrolling
- [x] Transient notification bar (6-second auto-dismiss)
- [x] Debug log overlay (F12)
- [x] `prompt.choice` interactive modal — AI presents options, user picks

**Memory**
- [x] LanceDB vector memory (jina-embeddings-v2-base-zh, 768-dim bilingual)
- [x] SQLite FTS fallback
- [x] Background pre-warm (no blocking on first message)
- [x] Memory distillation on session pause

**Code Intelligence**
- [x] tree-sitter AST indexing (Rust, Python, JS, TypeScript)
- [x] BM25 full-text search (Tantivy embedded)
- [x] PageRank symbol ranking
- [x] Hybrid BM25 + vector search (RRF fusion)
- [x] LSP client for call chains

**Skill System**
- [x] TOML-defined multi-step skills
- [x] 15 built-in skills (TDD, debugging, code review, git workflows, brainstorming, ...)
- [x] `/chain <symbol>` call-chain visualization

**Security**
- [x] Blacklist model (allow all, block known-dangerous)
- [x] `config.toml` permanently off-limits to AI (bootstrapping attack prevention)
- [x] `SHELL_BLOCKED_DANGEROUS` — 22 hardcoded patterns

**Integrations**
- [x] OpenAI, Anthropic, DeepSeek, Ollama via LiteLLM router
- [x] MCP server and client
- [x] Auto Python environment via `uv` (zero manual pip)

---

## v0.2.0 — Planned

Focus: execution isolation, multi-agent, and ecosystem growth.

**Execution**
- [ ] WASM sandbox for untrusted tool execution (wasmtime / Wasmer)
- [ ] Multi-agent DAG execution (manager-worker pattern, git worktree isolation)
- [ ] Cross-agent shared memory

**Developer Experience**
- [ ] LSP L3 full verification (8 unit tests exist, full integration pending)
- [ ] `evocli watch` — background file watcher with incremental re-indexing
- [ ] Session compression command (`/compress`) for long conversations

**Ecosystem**
- [ ] Skill marketplace (install community skills: `evocli skill install <name>`)
- [ ] Plugin SDK documentation
- [ ] VS Code extension for launching EvoCLI from the editor

---

## v1.0 — Vision

A production-grade AI coding runtime trusted by teams.

**Collaboration**
- [ ] Team memory sharing (cloud-optional, E2E encrypted)
- [ ] Shared skill library with version pinning
- [ ] Audit log viewer (who ran what, when)

**Intelligence**
- [ ] Self-evolving skill library (learns from repeated tasks)
- [ ] Cross-project knowledge transfer
- [ ] Automatic constraint inference from project history

**Operations**
- [ ] Binary size < 10 MB (stripped release builds)
- [ ] Windows ARM64 and Linux ARM64 builds
- [ ] Docker image for headless/CI usage

---

## How to contribute to the roadmap

1. Check if an issue already exists for your idea.
2. If not, open a new issue with `[ROADMAP]` in the title.
3. Describe the use case, not just the solution.
4. Community votes (`+1` reactions) help prioritize.

Items marked `help wanted` in the issue tracker are actively looking for contributors.

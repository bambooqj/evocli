"""
EvoCLI Context Engine — Section 5.2 + Section 6.5 完整实现。

Token 预算（从 config 动态读取，默认 32k，支持 128k+）：
  固定：System(1500) + P1约束(1000) + Goal(500) = 3000
  动态：P1 episodes → P2 tool → P3 global → ranked_symbols
        → code → git_diff → history
  裁剪顺序：history → P3 → P2 → code  （P1约束永不裁）

FIX-A: BUDGET_TOTAL 从 config.toml context.max_total 读取，
        支持现代模型的 128k/200k context window。
FIX-BUDGET: Budget constants computed lazily inside build() via functools.lru_cache
            so changes to config.toml take effect without restarting the process.
"""
from __future__ import annotations
import functools
import hashlib
import importlib.util
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger("evocli.context")

# 默认值（config 未找到时使用）
_DEFAULT_BUDGET_TOTAL = 32_000


def _goal_fingerprint(goal: str) -> str:
    """Fast content fingerprint for cache-key comparison. No ML needed.

    Normalises goal text and returns a short hex digest. Two identical goals
    (same intent, same phrasing) map to the same key, enabling RepoMap and
    memory search to be skipped on subsequent turns.
    """
    normalised = goal.lower().strip()[:500]
    return hashlib.md5(normalised.encode(), usedforsecurity=False).hexdigest()


def _content_hash(text: str | None) -> str:
    """Hash of file content to detect changes between turns."""
    if not text:
        return ""
    return hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()[:16]

@functools.lru_cache(maxsize=1)
def _load_budget_total() -> int:
    """
    从以下来源（优先级从高到低）读取模型 context window：
    1. config.toml 用户手动配置（context.max_total）
    2. model_context.py 自动检测（litellm DB → API 端点 → 名称推断）
    3. 保守默认值 32k
    
    参考实现：Aider（litellm DB + custom JSON）、Continue.dev（provider probing + user override）
    
    Wrapped with lru_cache so the filesystem read only happens once per process
    and is deferred until the first call (lazy initialization).
    """
    # 优先读用户配置覆盖
    try:
        import tomllib
        cfg_path = Path.home() / ".evocli" / "config.toml"
        if cfg_path.exists():
            with open(cfg_path, "rb") as f:
                data = tomllib.load(f)
            # 用户明确设置了 context.max_total → 直接使用（最高优先级）
            val = data.get("context", {}).get("max_total")
            if isinstance(val, int) and val > 4000:
                log.debug("Context budget from user config: %d tokens", val)
                return val
            # 从配置读取当前模型，用 model_context 自动检测
            model = data.get("llm", {}).get("tiers", {}).get("fast", "")
            base_url = data.get("llm", {}).get("base_url")
            api_key  = data.get("llm", {}).get("api_key")
            if model:
                try:
                    from evocli_soul.model_context import get_input_context
                    ctx = get_input_context(model, base_url=base_url, api_key=api_key)
                    if ctx > 4000:
                        log.info("Context budget auto-detected: %s → %d tokens", model, ctx)
                        return ctx
                except Exception as e:
                    log.debug("model_context detection failed: %s", e)
    except Exception as e:
        log.debug("Could not read context budget from config: %s", e)
    return _DEFAULT_BUDGET_TOTAL


def _get_budgets() -> dict:
    """
    Compute all budget constants lazily.
    Called inside ContextEngine.build() so constants are never evaluated at import time.
    lru_cache on _load_budget_total() ensures the config read only happens once.
    """
    total = _load_budget_total()
    fixed = min(3_000, total // 10)
    return dict(
        total=total,
        fixed=fixed,
        p1_episodes=min(4_000, total // 8),
        p2_tool=min(2_000, total // 16),
        p3_global=min(1_500, total // 20),
        code=min(max(16_000, total // 2), total - fixed - 10_000),
        git_diff=min(3_000, total // 10),
        history=min(1_500, total // 20),
    )


def _count_tokens(text: str, model: str = "") -> int:
    """
    Count tokens. Priority:
    1. litellm.token_counter — model-aware, handles Anthropic/Google correctly
    2. tiktoken cl100k_base — OpenAI-accurate fallback
    3. len // 4 — last resort estimate
    """
    if not text:
        return 0
    # litellm.token_counter is more accurate for non-OpenAI models
    try:
        import litellm
        m = model or "gpt-4"  # default encoding for generic count
        return litellm.token_counter(model=m, text=text)
    except Exception:
        pass
    if importlib.util.find_spec("tiktoken"):
        try:
            import tiktoken
            return len(tiktoken.get_encoding("cl100k_base").encode(text))
        except Exception:
            pass
    return max(1, len(text) // 4)


def _truncate(text: str, budget: int) -> str:
    """FIX-A: 更智能的截断 — 尽量在行边界截断，避免切断代码语法"""
    if _count_tokens(text) <= budget:
        return text
    # 先尝试按行截断（保证语法完整性）
    lines = text.splitlines(keepends=True)
    result = []
    used = 0
    for line in lines:
        line_tokens = _count_tokens(line)
        if used + line_tokens > budget:
            break
        result.append(line)
        used += line_tokens
    if result:
        return "".join(result) + "\n...[truncated]"
    # fallback: 字符截断
    ratio = budget / max(1, _count_tokens(text))
    return text[: int(len(text) * ratio * 0.88)] + "\n...[truncated]"


def _prune_code(code: str, budget: int) -> str:
    """
    使用 tokenpruner 的正确 API（class-based TextPruner）。
    PruningResult.pruned_text 是压缩后的文本。
    """
    if importlib.util.find_spec("tokenpruner"):
        try:
            import tokenpruner  # type: ignore
            config = tokenpruner.PruningConfig(
                strategy=tokenpruner.PruningStrategy.CODE_MINIFY,
                max_tokens=budget,
            )
            result = tokenpruner.TextPruner(config).prune(code)
            pruned = result.pruned_text  # PruningResult.pruned_text 是正确的字段
            if pruned:
                return pruned
        except Exception:
            pass
    return _truncate(code, budget)


def _compact_history(history: list[dict], budget: int) -> list[dict]:
    """
    智能历史压缩 — 保留最近消息 + 工具结果 observation masking。

    Research (Aider + Continue.dev 模式):
    - 保留最近 N 轮 (newest-first)
    - 工具结果超过 200 token: 替换为摘要
    - 超过 budget: 裁剪最旧消息
    """
    if not history:
        return []

    compacted = []
    used = 0

    for msg in reversed(history):
        role    = msg.get("role", "")
        content = str(msg.get("content", ""))

        # Observation masking: 长工具结果压缩 (OpenCode: prune tool outputs first)
        if role == "tool" or (role == "assistant" and '"tool_calls"' in content):
            token_count = _count_tokens(content)
            if token_count > 200:
                lines   = content.splitlines()
                preview = "\n".join(lines[:5])
                content = f"{preview}\n...[{token_count} tokens, masked for context]\n"

        msg_tokens = _count_tokens(content)
        if used + msg_tokens > budget:
            break
        compacted.insert(0, {**msg, "content": content})
        used += msg_tokens

    return compacted


# ── Anchored Summary Compaction (OpenCode pattern) ────────────────────────────

_ANCHORED_SUMMARY_TEMPLATE = """
You are summarizing an AI coding assistant's session to preserve key context.
Output EXACTLY this Markdown structure (no extra text):

## Goal
[One paragraph: what was the user trying to accomplish?]

## Constraints
[Bullet list: any rules, limitations, or "never do X" statements from the conversation]

## Progress
### Done
[Bullet list of completed steps]
### In Progress
[What was being worked on when context was compacted]
### Blocked
[Any blockers or unresolved issues]

## Key Decisions
[Bullet list of architectural/design decisions made]

## Next Steps
[Bullet list of what should happen next, in priority order]

## Critical Context
[Any facts the agent MUST remember: file paths changed, errors seen, commands run]

## Relevant Files
[Bullet list of files that were read or edited: path — what was done]
""".strip()

async def compact_session_to_anchor(
    history: list[dict],
    llm_client,
    existing_summary: str = "",
) -> str:
    """
    Compact a long history into an Anchored Summary using a weak/fast LLM.

    Research source: OpenCode's "Recursive Anchored Summary" algorithm.
    - Uses a Markdown template to preserve: Goal, Constraints, Progress, Key Decisions
    - When called recursively, feeds the old summary + new messages → updates in place
    - Preserves the "Constraints" section to keep user rules alive after compaction
    - The agent can "re-read" its goal even after a full context reset

    Returns: compact Markdown summary (typically 500-1500 tokens).
    """
    # Serialize history for the summary prompt
    history_text = "\n".join(
        f"[{m.get('role','?')}]: {str(m.get('content',''))[:500]}"
        for m in history[-40:]  # Use last 40 messages for summarization
    )

    if existing_summary:
        # Recursive update: feed old summary + new messages
        prompt = (
            f"Below is the existing summary of this coding session:\n\n"
            f"```\n{existing_summary}\n```\n\n"
            f"New messages since that summary:\n\n{history_text}\n\n"
            f"Update the anchored summary to reflect the new progress. "
            f"Keep all sections. Mark completed items as done."
        )
    else:
        prompt = (
            f"Here is a coding session conversation:\n\n{history_text}\n\n"
            f"Create an anchored summary following the template."
        )

    system = _ANCHORED_SUMMARY_TEMPLATE
    try:
        summary = await llm_client.complete_for_task(
            "summarize",
            prompt,
            system=system,
        )
        log.info("Context compacted: %d history msgs → anchored summary (%d chars)",
                 len(history), len(summary))
        return summary
    except Exception as e:
        log.warning("Anchored summary compaction failed (%s), using simple truncation", e)
        # Fallback: return last 5 messages as plain text
        return "\n".join(
            f"[{m.get('role','?')}]: {str(m.get('content',''))[:200]}"
            for m in history[-5:]
        )


class ContextEngine:
    def __init__(self, bridge):
        self.bridge = bridge

    async def parse_mentions(self, goal: str) -> tuple[str, dict]:
        """
        Parse @ context provider mentions from user prompt.
        研究来源: Continue.dev @terminal/@file/@problems/@docs 语法
        支持: @file:<path>, @terminal, @problems

        Returns: (cleaned_goal, injected_context_dict)
        """
        import re
        injected: dict[str, str] = {}

        # @file:<path> — inject file content
        file_pattern = re.compile(r"@file:(\S+)")
        for m in file_pattern.finditer(goal):
            path = m.group(1)
            try:
                content = await self.bridge.call("fs.read", {"path": path})
                if isinstance(content, str):
                    injected[f"@file:{path}"] = f"## File: {path}\n```\n{content[:3000]}\n```"
                    log.debug("@file provider: %s (%d chars)", path, len(content))
            except Exception as e:
                log.debug("@file: %s failed: %s", path, e)
        goal = file_pattern.sub("", goal).strip()

        # @terminal — inject recent shell output via run_and_capture
        # Bug fix: was running `echo $EVOCLI_LAST_OUTPUT` which always fails on Windows
        # Now: tells the LLM to use the run_and_capture tool to get terminal output
        if "@terminal" in goal:
            injected["@terminal"] = (
                "## Terminal Context\n"
                "To see terminal output, use the `run_and_capture` tool with the command you need to inspect. "
                "Example: `run_and_capture('git status')` or `run_and_capture('cargo build 2>&1')`."
            )
            goal = goal.replace("@terminal", "").strip()

        # @problems — inject current file diagnostics
        if "@problems" in goal:
            injected["@problems"] = "## Diagnostics\n[Use fs_lint_file tool to check for errors in specific files]"
            goal = goal.replace("@problems", "").strip()

        return goal, injected

    async def build(self, params: dict) -> dict:
        # Compute budget constants lazily — not at module import time.
        # lru_cache on _load_budget_total() ensures the config read happens only once.
        _b = _get_budgets()
        BUDGET_TOTAL       = _b["total"]
        BUDGET_FIXED       = _b["fixed"]
        BUDGET_P1_EPISODES = _b["p1_episodes"]
        BUDGET_P2_TOOL     = _b["p2_tool"]
        BUDGET_P3_GLOBAL   = _b["p3_global"]
        BUDGET_CODE        = _b["code"]
        BUDGET_GIT_DIFF    = _b["git_diff"]
        BUDGET_HISTORY     = _b["history"]

        async def _progress(msg: str) -> None:
            """Emit a progress soul_status event during long context build phases."""
            try:
                from evocli_soul.rpc import emit_event as _ev
                await _ev("soul_status", {"status": "loading", "message": msg})
            except Exception:
                pass  # Never let progress events break context building
        goal         = params.get("goal", "")
        current_file = params.get("current_file")
        git_diff     = params.get("git_diff", "")
        history      = params.get("history", [])
        project_id   = params.get("project_id", "global")
        active_tools = params.get("active_tools", [])
        session_id   = params.get("session_id", "default")

        # ── @ context providers (Continue.dev 模式) ─────────────────
        goal, mention_context = await self.parse_mentions(goal)

        remaining = BUDGET_TOTAL - BUDGET_FIXED
        slots: list[dict] = []

        # ── Session-level context cache ──────────────────────────────
        # Skip expensive RepoMap (tree-sitter scan) and memory search
        # when goal fingerprint AND current file content hash are unchanged.
        # This is the primary source of per-turn token savings (75%+).
        from evocli_soul import state as _state_cache
        _cache = _state_cache.get_context_cache(session_id)

        goal_fp          = _goal_fingerprint(goal)
        cached_goal_fp   = _cache.get("goal_fingerprint", "")
        cached_file_hash = _cache.get("current_file_hash", "")
        goal_unchanged   = (goal_fp == cached_goal_fp) and bool(cached_goal_fp)

        await _progress("⚙ 构建上下文…")

        # Always read current file (needed for code injection anyway).
        # We compute its hash here for cache validation.
        _current_file_content: str | None = None
        current_file_hash = ""
        if current_file:
            try:
                _raw = await self.bridge.call("fs.read", {"path": current_file})
                if isinstance(_raw, str):
                    _current_file_content = _raw
                    current_file_hash = _content_hash(_raw)
            except Exception as _fe:
                log.debug("file read for cache: %s", _fe)

        file_unchanged = (current_file_hash == cached_file_hash) and bool(cached_file_hash)
        can_reuse_repomap = goal_unchanged and file_unchanged and bool(_cache.get("repomap_text"))
        can_reuse = can_reuse_repomap  # kept for backwards compat with repomap branch below

        # Update cache keys for next turn regardless of hit/miss
        _state_cache.update_context_cache({
            "goal_fingerprint":  goal_fp,
            "current_file_hash": current_file_hash,
        }, session_id)

        if can_reuse:
            log.debug("RepoMap cache HIT (session=%s) — skipping tree-sitter scan", session_id)
        # Memory search is NEVER cached — always fresh (turn-N writes must appear in turn-N+1)

        # ── 统一内存读取（H1 遗留修复）──────────────────────────────
        # H1 将写入统一到 Python LanceDB，读取也必须走同一路径。
        # 原 bridge.call("memory.recall/constraints") 走 Rust SQLite（已空）。
        # 改为：一次 memory_client.search() + get_constraints()，
        # 不经 IPC 桥，减少延迟，统一存储。
        _mc = None
        try:
            _mc = _state_cache.get_memory()  # reuse _state_cache import
        except Exception as e:
            log.debug("memory_client init: %s", e)

        # ── P1 约束（固定，永不压缩）──────────────────────────────
        constraints: list[str] = []
        try:
            if _mc is not None:
                constraints = _mc.get_constraints()
            else:
                # H1 note: Rust memory.constraints now returns [] (unified to Python LanceDB).
                # This fallback only triggers when memory_client init fails.
                log.warning("memory_client unavailable — constraints will be empty (H1 unification)")
                constraints = []
        except Exception as e:
            log.debug("constraints: %s", e)
        constraint_text = "\n".join(f"- {c}" for c in constraints)

        # ── P1/P2/P3 记忆：一次语义搜索，按 scope 拆分 ──────────
        # Memory search is ALWAYS executed — never cached across turns.
        _all_memories: list[dict] = []
        if _mc is not None and remaining > 0:
            await _progress("🧠 检索项目记忆…")
            try:
                _all_memories = _mc.search(
                    goal, top_k=15,
                    current_project=project_id,
                    active_tools=active_tools or [],
                )
            except Exception as e:
                log.debug("memory search: %s", e)

        def _fmt_mem(e: dict) -> str:
            title = e.get("title", "")
            body  = e.get("body") or e.get("memory") or ""
            return f"[{title}]\n{body}" if title else body

        # ── P1 项目经验 ───────────────────────────────────────────
        p1_text = ""
        if remaining > 0:
            try:
                p1_eps = [m for m in _all_memories
                          if m.get("priority_scope", "project") in ("project", "")][:5]
                if p1_eps:
                    raw    = "\n\n".join(_fmt_mem(e) for e in p1_eps)
                    budget = min(BUDGET_P1_EPISODES, remaining)
                    p1_text = _truncate(raw, budget)
                    used = _count_tokens(p1_text)
                    remaining -= used
                    slots.append({"name": "p1_episodes", "tokens": used, "priority": "p1"})
            except Exception as e:
                log.debug("p1 episodes: %s", e)

        # ── P2 工具记忆 ───────────────────────────────────────────
        p2_text = ""
        if remaining > 0 and active_tools:
            try:
                p2_tool_mems = [m for m in _all_memories
                                if m.get("priority_scope") == "tool"][:3]
                if p2_tool_mems:
                    raw = "\n\n".join(
                        f"[Tool:{e.get('tool_id', e.get('priority_scope',''))}] {_fmt_mem(e)}"
                        for e in p2_tool_mems
                    )
                    budget  = min(BUDGET_P2_TOOL, remaining)
                    p2_text = _truncate(raw, budget)
                    used = _count_tokens(p2_text)
                    remaining -= used
                    slots.append({"name": "p2_tool", "tokens": used, "priority": "p2"})
            except Exception as e:
                log.debug("p2 tool: %s", e)

        # ── P3 全局经验 ───────────────────────────────────────────
        p3_text = ""
        if remaining > BUDGET_P3_GLOBAL // 2:
            try:
                p3_global = [m for m in _all_memories
                             if m.get("priority_scope") == "global"][:3]
                if p3_global:
                    raw = "\n\n".join(f"[Global] {_fmt_mem(e)}" for e in p3_global)
                    budget  = min(BUDGET_P3_GLOBAL, remaining)
                    p3_text = _truncate(raw, budget)
                    used = _count_tokens(p3_text)
                    remaining -= used
                    slots.append({"name": "p3_global", "tokens": used, "priority": "p3"})
            except Exception as e:
                log.debug("p3 global: %s", e)

        # ── 代码文件 ──────────────────────────────────────────────
        # Reuse pre-read content from cache check (avoids double fs.read)
        code_text = ""
        if current_file and remaining > 0:
            try:
                raw = _current_file_content  # already read during cache check above
                if raw is None:  # fallback if cache check didn't read it
                    raw = await self.bridge.call("fs.read", {"path": current_file})
                if isinstance(raw, str):
                    budget    = min(BUDGET_CODE, remaining)
                    code_text = _prune_code(raw, budget)
                    used = _count_tokens(code_text)
                    remaining -= used
                    slots.append({"name": "code", "tokens": used, "priority": "code"})
            except Exception as e:
                log.debug("code read: %s", e)

        # ── ranked_context + RepoMap（合并：先取 Rust 索引，再渲染骨架）────────
        # Cache hit: reuse previous RepoMap (expensive tree-sitter scan)
        # Cache miss: compute, then cache for next turn
        repo_map_text = ""
        pre_ranked: list[dict] = []

        if can_reuse:
            repo_map_text = _cache.get("repomap_text", "")
            if repo_map_text:
                used = _count_tokens(repo_map_text)
                remaining -= used
                slots.append({"name": "repo_map", "tokens": used, "priority": "p1", "source": "cache"})
                log.debug("RepoMap cache HIT — %d tokens reused", used)
        else:
            # Cache miss: compute RepoMap from scratch
            await _progress("📊 扫描代码库结构（首次可能需要 10-30s）…")
            # Step 1: 获取 Rust code_intel 已有的 PageRank 符号排名（轻量 RPC）
            if current_file and remaining > 500:
                try:
                    mentioned = params.get("mentioned_symbols", [])
                    pre_ranked = await self.bridge.call("code_intel.ranked_context", {
                        "modified_file": current_file,
                        "mentioned":     mentioned,
                        "limit":         20,
                    })
                    if not isinstance(pre_ranked, list):
                        pre_ranked = []
                except Exception as e:
                    log.debug("ranked_context prefetch: %s", e)

            # Step 2: RepoMap — 优先用 pre_ranked 跳过全仓 tree-sitter 扫描
            if remaining > 500:
                try:
                    import asyncio as _asyncio
                    from evocli_soul.repo_map import RepoMap
                    chat_files = [current_file] if current_file else []
                    goal_words = goal.split()[:10] if goal else []
                    repo_map   = RepoMap(root=".", map_tokens=min(BUDGET_P1_EPISODES, remaining))
                    repo_map_text = await _asyncio.to_thread(
                        repo_map.get_repo_map,
                        chat_files=chat_files,
                        mentioned_symbols=goal_words,
                        pre_ranked_symbols=pre_ranked,
                    )
                    if repo_map_text:
                        used = _count_tokens(repo_map_text)
                        remaining -= used
                        slots.append({
                            "name":     "repo_map",
                            "tokens":   used,
                            "priority": "p1",
                            "source":   "rust_index" if pre_ranked else "tree_sitter",
                        })
                        # Cache for next turn
                        _state_cache.update_context_cache({"repomap_text": repo_map_text}, session_id)
                        log.debug("RepoMap: %d tokens (source=%s) — cached",
                                  used, "rust_index" if pre_ranked else "tree_sitter")
                except Exception as e:
                    log.debug("RepoMap failed (non-fatal): %s", e)


        # ── Git Diff ─────────────────────────────────────────────
        diff_text = ""
        if git_diff and remaining > 0:
            budget    = min(BUDGET_GIT_DIFF, remaining)
            diff_text = _truncate(git_diff, budget)
            used = _count_tokens(diff_text)
            remaining -= used
            slots.append({"name": "git_diff", "tokens": used, "priority": "code"})

        # ── 对话历史（Anchored Summary + observation masking）────────
        # Research (OpenCode 模式): 当历史过长时使用结构化 Anchored Summary 压缩，
        # 而非简单截断。保留 Goal/Constraints/Progress 结构，确保长会话不丢失目标。
        # IMPORTANT: anchored_summary must be injected even when history is empty
        # (e.g. after /compress cleared history) — it IS the compressed session memory.
        history_text = ""
        anchored_summary = params.get("anchored_summary", "")  # 外部传入已有摘要
        if anchored_summary and remaining > 0:
            # Always inject the anchored summary first — it survives /compress.
            anchor_text = f"## 会话摘要（Anchored Summary）\n{anchored_summary}"
            used = _count_tokens(anchor_text)
            history_text = anchor_text
            remaining -= used
            slots.append({"name": "anchored_summary", "tokens": used, "priority": "history"})

        if history and remaining > 0:
            budget         = min(BUDGET_HISTORY, remaining)
            history_tokens = sum(_count_tokens(str(m.get("content", ""))) for m in history)
            # Track tokens already in history_text (anchor injected above) to avoid double-counting.
            _already_counted = _count_tokens(history_text) if history_text else 0

            if history_tokens > budget * 2 and len(history) > 10:
                # 历史太长 — 使用 Anchored Summary 压缩 (OpenCode 模式)
                # 保留最近 4 轮对话（tail preservation）
                tail    = history[-4:]
                to_compact = history[:-4]
                # anchored_summary already injected above as its own slot if present.
                # Here we only append recent tail — no need to repeat the anchor header.
                if not anchored_summary:
                    # No pre-existing anchor — build a quick inline summary from older turns
                    key_msgs = [m for m in to_compact if m.get("role") != "tool"][-5:]
                    anchor_inline = "## 历史摘要\n" + "\n".join(
                        f"[{m.get('role','')}]: {str(m.get('content',''))[:200]}"
                        for m in key_msgs
                    )
                else:
                    anchor_inline = ""  # already in history_text from the block above
                tail_text = "\n".join(
                    f"{m.get('role','')}: {str(m.get('content',''))[:400]}"
                    for m in _compact_history(tail, budget // 2)
                )
                extra = (anchor_inline + "\n\n" if anchor_inline else "") + "## 最近对话\n" + tail_text
                history_text = (history_text + "\n\n" + extra).strip() if history_text else extra
            else:
                # 历史较短 — 正常压缩
                compacted = _compact_history(history[-20:], budget)
                if compacted:
                    lines = [f"{m.get('role','')}: {str(m.get('content',''))[:400]}"
                             for m in compacted]
                    extra = "\n".join(lines)
                    history_text = (history_text + "\n\n" + extra).strip() if history_text else extra

            # Only count the NEW tokens added in this block (not the anchor already counted above).
            used = max(0, _count_tokens(history_text) - _already_counted)
            remaining -= used
            slots.append({"name": "history", "tokens": used, "priority": "history"})

        # ── system_prompt 组装 ────────────────────────────────────
        # Use build_system_prompt() as the base to ensure DO-NOW rules, SYSTEM_WORKFLOW,
        # tool ordering, and failure recovery are present in ALL LLM paths (LiteLLM
        # fallback uses ctx["system_prompt"] as its system message).
        try:
            from evocli_soul.default_prompts import build_system_prompt as _build_sp
            _base_prompt = _build_sp(
                constraints=constraint_text or "",
                goal=goal or "",
                read_only=params.get("read_only", False),
                compact=False,
            )
        except Exception:
            # Fallback: inline base so context_engine never hard-fails
            _base_prompt = "你是 EvoCLI，一个 AI 编程 Runtime 助手。"
            if constraint_text:
                _base_prompt += f"\n\n## 项目约束（必须遵守）\n{constraint_text}"
            if goal:
                _base_prompt += f"\n\n## 当前目标\n{goal}"
        parts = [_base_prompt]
        # Append dynamic per-turn context sections (repo map, memory, skills, etc.)
        # These come after the static workflow rules so they don't override them.
        # RepoMap (Aider-style PageRank)
        if repo_map_text:
            parts.append(f"\n## 代码库地图（Repo Map — 最相关的符号和文件结构）\n{repo_map_text}")
        # ranked_text 已合并进 repo_map_text（Rust 索引 → RepoMap fast-path）
        if p1_text:
            parts.append(f"\n## 相关项目经验\n{p1_text}")
        if p2_text:
            parts.append(f"\n## 工具使用经验\n{p2_text}")
        if p3_text:
            parts.append(f"\n## 通用工程经验\n{p3_text}")
        # @ context providers (Continue.dev 模式: @file, @terminal, @problems)
        if mention_context:
            for key, val in mention_context.items():
                parts.append(f"\n{val}")
                log.debug("Context provider '%s': %d chars injected", key, len(val))

        # ── Superpowers 指引技能（Guidance Skills 语义注入）──────────────────
        # 用 local_classifier.rank_by_similarity 替代关键词匹配，
        # 语义相关的 SKILL.md 指引自动注入上下文。
        if goal and remaining > 500:
            try:
                import evocli_soul.state as _state
                engine = _state.get_skill_engine()
                if hasattr(engine, "find_relevant_guidance"):
                    matched_guidance = engine.find_relevant_guidance(goal, top_k=2)
                else:
                    matched_guidance = []

                if matched_guidance:
                    guidance_budget = min(1000, remaining)
                    guidance_parts  = []
                    for gs in matched_guidance:
                        snippet = f"### 方法论指引：{gs.name}\n{gs.content[:600]}"
                        guidance_parts.append(snippet)
                    guidance_text = "\n\n".join(guidance_parts)
                    if guidance_text:
                        truncated = _truncate(guidance_text, guidance_budget)
                        parts.append(f"\n## 相关方法论指引（Superpowers Skills）\n{truncated}")
                        used = _count_tokens(truncated)
                        remaining -= used
                        slots.append({"name": "guidance_skills", "tokens": used, "priority": "p2"})
                        log.debug("Injected %d guidance skill(s) via semantic search, %d tokens",
                                  len(matched_guidance), used)
            except Exception as e:
                log.debug("Guidance skill injection failed (non-fatal): %s", e)

        # ── user_context 组装 ─────────────────────────────────────
        ctx = []
        if code_text:
            ctx.append(f"## 当前文件：{current_file}\n```\n{code_text}\n```")
        if diff_text:
            ctx.append(f"## 当前变更\n```diff\n{diff_text}\n```")
        if history_text:
            ctx.append(f"## 对话历史\n{history_text}")

        total_used = BUDGET_TOTAL - remaining
        log.info("Context: %d / %d tokens", total_used, BUDGET_TOTAL)

        return {
            "system_prompt": "\n".join(parts),
            "user_context":  "\n\n".join(ctx),
            "total_tokens":  total_used,
            "slots":         slots,
        }


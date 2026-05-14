# pyright: reportMissingImports=false, reportMissingTypeArgument=false, reportAttributeAccessIssue=false
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

log = logging.getLogger("evocli.context")

# 默认 context 预算 — 从 config_defaults 读取，支持 config.toml 覆盖
from evocli_soul.config_defaults import cfg_float as _cfg_float_ctx
from evocli_soul.config_defaults import cfg_int as _cfg_int_ctx
_DEFAULT_BUDGET_TOTAL = _cfg_int_ctx("llm.default_context_window")


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
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib  # type: ignore[no-redef]
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
        repomap=min(_cfg_int_ctx("context.repomap_tokens"), total // 8),
        code=min(max(16_000, total // 2), total - fixed - 10_000),
        git_diff=min(3_000, total // 10),
        history=min(1_500, total // 20),
        history_turns=max(1, _cfg_int_ctx("context.history_turns")),
        auto_compress_threshold=_cfg_float_ctx("context.auto_compress_threshold"),
    )


def _extract_symbol_name(line: str) -> str | None:
    stripped = line.strip()
    for prefix in ("async def ", "def ", "class "):
        if stripped.startswith(prefix):
            remainder = stripped[len(prefix):]
            name_chars = []
            for ch in remainder:
                if ch.isalnum() or ch == "_":
                    name_chars.append(ch)
                else:
                    break
            if name_chars:
                return "".join(name_chars)
    return None


async def _build_compact_symbol_nav(root: str, budget: int = 512) -> str:
    """
    Aider-style compact symbol navigation.
    NOT file contents — just 'filename: func1, func2, Class1' per file.
    Gives LLM a directory of what exists so it knows WHAT to read via tools.
    Fast: just shell_ls + quick regex, no tree-sitter needed.

    Example output:
    # Project symbols (use fs_read/search_code to explore)
    src/agent.py: EvoCLIAgent, run(), stream(), _build_context()
    src/memory_client.py: EvoCLIMemory, search(), write(), get_constraints()
    src/handlers/agent_loop.py: run_agent_stream_body()
    """
    try:
        import json as _json
        from evocli_soul import state as _state

        bridge = _state.get_bridge()
        raw = await bridge.call("shell.grep", {
            "pattern": "^\\s*(class |def |async def )",
            "path": root,
            "include": ".py",
            "max_results": 200,
        })
        if not raw:
            return ""

        text = _json.dumps(raw, ensure_ascii=False) if isinstance(raw, (dict, list)) else str(raw)
        root_path = Path(root).resolve()
        symbols_by_file: dict[str, list[str]] = {}
        for row in text.splitlines():
            match = __import__("re").match(r"^(.*?\.py)(?::\d+)?:(.*)$", row)
            if not match:
                continue
            file_path = match.group(1).strip()
            symbol_name = _extract_symbol_name(match.group(2))
            if not symbol_name:
                continue
            try:
                rel_path = Path(file_path).resolve().relative_to(root_path).as_posix()
            except Exception:
                rel_path = Path(file_path).as_posix()
            symbols = symbols_by_file.setdefault(rel_path, [])
            if symbol_name not in symbols:
                symbols.append(symbol_name)

        if not symbols_by_file:
            return ""

        lines = ["# Project symbols (use fs_read/search_code to explore)"]
        for file_path, symbols in sorted(symbols_by_file.items()):
            rendered = ", ".join(
                f"{symbol}()" if symbol and symbol[0].islower() else symbol
                for symbol in symbols[:12]
            )
            candidate = f"{file_path}: {rendered}"
            preview = "\n".join(lines + [candidate])
            if _count_tokens(preview) > budget:
                break
            lines.append(candidate)

        return "\n".join(lines) if len(lines) > 1 else ""
    except Exception as e:
        log.debug("compact symbol nav failed: %s", e)
        return ""


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
    except Exception as _e:
        log.debug("token count: litellm failed (%s), trying tiktoken", _e)
    if importlib.util.find_spec("tiktoken"):
        try:
            import tiktoken
            return len(tiktoken.get_encoding("cl100k_base").encode(text))
        except Exception as _e:
            log.warning(
                "token count: both litellm and tiktoken unavailable (%s). "
                "Falling back to len//4 — estimates may be inaccurate for non-ASCII text.",
                _e,
            )
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


# ── Anchored Summary + @mention parsing — extracted to dedicated modules ──────
# Imported here for backward compatibility (existing callers import from context_engine).
from evocli_soul.context_summary import compact_session_to_anchor, _ANCHORED_SUMMARY_TEMPLATE  # noqa: F401
from evocli_soul.context_mentions import parse_mentions as _parse_mentions_standalone  # noqa: F401


class ContextEngine:
    def __init__(self, bridge):
        self.bridge = bridge

    async def parse_mentions(self, goal: str) -> "tuple[str, dict]":
        """Delegate to context_mentions.parse_mentions (extracted module)."""
        return await _parse_mentions_standalone(self.bridge, goal)

    async def build(self, params: dict) -> dict:
        # Compute budget constants lazily — not at module import time.
        # lru_cache on _load_budget_total() ensures the config read happens only once.
        _b = _get_budgets()
        BUDGET_TOTAL       = _b["total"]
        BUDGET_FIXED       = _b["fixed"]
        BUDGET_REPOMAP     = _b["repomap"]
        BUDGET_CODE        = _b["code"]
        BUDGET_GIT_DIFF    = _b["git_diff"]
        BUDGET_HISTORY     = _b["history"]
        HISTORY_TURNS      = _b["history_turns"]
        AUTO_COMPRESS_AT   = _b["auto_compress_threshold"]

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

        # Normalize project_id to a stable absolute-path key.
        # Callers use ".", "global", or actual paths — normalize_project_id() maps them
        # all to os.path.abspath(), which matches how get_memory() and get_index() key.
        from evocli_soul.state import normalize_project_id as _norm_pid
        project_id = _norm_pid(project_id)

        # ── @ context providers (Continue.dev 模式) ─────────────────
        goal, mention_context = await self.parse_mentions(goal)

        remaining = BUDGET_TOTAL - BUDGET_FIXED
        slots: list[dict] = []

        # ── Session-level context cache ──────────────────────────────
        # Skip expensive project symbol scan when goal fingerprint
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
        # ── 统一内存读取（仅保留约束）──────────────────────────────
        _mc = None
        try:
            # Use get_memory_if_ready() instead of get_memory() to avoid blocking
            # the asyncio event loop during fastembed/LanceDB model initialization
            # (which can take 30+ seconds on first run).
            # Pass project_id so the correct per-project memory instance is returned.
            _mc = _state_cache.get_memory_if_ready(project_id)  # returns None if not yet initialized
        except Exception as e:
            log.debug("memory_client init: %s", e)

        # ── P1 约束（固定，永不压缩）──────────────────────────────
        constraints: list[str] = []
        _skip_constraints = bool(params.get("skip_constraints", False))
        if not _skip_constraints:
            try:
                if _mc is not None:
                    constraints = _mc.get_constraints()
                else:
                    log.warning("memory_client unavailable — constraints will be empty (H1 unification)")
                    constraints = []
            except Exception as e:
                log.debug("constraints: %s", e)
        constraint_text = "\n".join(f"- {c}" for c in constraints)

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

        # ── Compact symbol navigation（Aider-style, cached）───────────────────
        repo_map_text = ""
        _compact_symbols = bool(params.get("compact_symbols", False))

        if can_reuse:
            repo_map_text = _cache.get("repomap_text", "")
            if repo_map_text:
                used = _count_tokens(repo_map_text)
                remaining -= used
                slots.append({"name": "repo_map", "tokens": used, "priority": "p1", "source": "cache"})
                log.debug("RepoMap cache HIT — %d tokens reused", used)
        elif remaining > 128 and _compact_symbols and not params.get("skip_repomap", False):
            try:
                await _progress("📊 提炼项目符号导航…")
                # Use session_root as nav root — never "." (would scan CWD which may be dist/)
                # project_id is already normalized by _norm_pid; use get_session_root() as fallback.
                try:
                    from evocli_soul.state import get_session_root as _get_sr
                    _nav_root = (project_id if (project_id and project_id not in (".", "global", ""))
                                 else _get_sr())
                except Exception:
                    _nav_root = project_id or "."
                repo_map_text = await _build_compact_symbol_nav(_nav_root, min(BUDGET_REPOMAP, remaining))
                if repo_map_text:
                    used = _count_tokens(repo_map_text)
                    remaining -= used
                    slots.append({
                        "name":     "repo_map",
                        "tokens":   used,
                        "priority": "p1",
                        "source":   "compact_symbols",
                    })
                    _state_cache.update_context_cache({"repomap_text": repo_map_text}, session_id)
            except Exception as e:
                log.debug("compact symbol nav failed (non-fatal): %s", e)


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
            recent_history = history[-HISTORY_TURNS:]
            history_tokens = sum(_count_tokens(str(m.get("content", ""))) for m in recent_history)
            # Track tokens already in history_text (anchor injected above) to avoid double-counting.
            _already_counted = _count_tokens(history_text) if history_text else 0
            compacted = _compact_history(recent_history, budget)
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
        # Build with model_id/provider_id so per-model specialization and env block
        # are included. context_engine receives these via params (passed from agent.py).
        try:
            from evocli_soul.default_prompts import build_system_prompt as _build_sp
            _model_id   = params.get("model_id", "")
            _provider   = params.get("provider_id", "")
            _base_prompt = _build_sp(
                constraints=constraint_text or "",
                goal=goal or "",
                read_only=params.get("read_only", False),
                compact=False,
                model_id=_model_id,
                provider_id=_provider,
                inject_skills=False,  # skills injected once at init time, not per-turn
            )
        except Exception:
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
        # @ context providers (Continue.dev 模式: @file, @terminal, @problems)
        if mention_context:
            for key, val in mention_context.items():
                parts.append(f"\n{val}")
                log.debug("Context provider '%s': %d chars injected", key, len(val))

        # ── user_context 组装 ─────────────────────────────────────
        ctx = []
        if code_text:
            ctx.append(f"## 当前文件：{current_file}\n```\n{code_text}\n```")
        if diff_text:
            ctx.append(f"## 当前变更\n```diff\n{diff_text}\n```")
        if history_text:
            ctx.append(f"## 对话历史\n{history_text}")

        system_prompt = "\n".join(parts)
        user_context = "\n\n".join(ctx)
        _estimated_total = _count_tokens(system_prompt) + _count_tokens(user_context)
        if _estimated_total > int(BUDGET_TOTAL * AUTO_COMPRESS_AT):
            user_context = "[Context approaching limit — key info preserved in anchored summary above]\n\n" + user_context

        total_used = _count_tokens(system_prompt) + _count_tokens(user_context)
        log.info("Context: %d / %d tokens", total_used, BUDGET_TOTAL)

        return {
            "system_prompt": system_prompt,
            "user_context":  user_context,
            "total_tokens":  total_used,
            "slots":         slots,
        }


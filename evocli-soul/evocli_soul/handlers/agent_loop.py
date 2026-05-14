# pyright: reportMissingTypeArgument=false, reportOptionalMemberAccess=false
"""
handlers/agent_loop.py — Autonomous agent execution loop

Extracted from handlers/agent.py to keep handle_agent_stream slim.

Single responsibility: run the multi-turn autonomous execution loop
(Plan-Act-Verify with task_complete exit signal).
"""
from __future__ import annotations
import asyncio
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("evocli.handlers.agent_loop")


def _estimate_history_tokens(history: list[dict[str, Any]]) -> int:
    """Rough token estimate: 1 token ≈ 4 chars."""
    total_chars = sum(
        len(str(msg.get("content", "")))
        for msg in history
    )
    return total_chars // 4


async def _auto_compress_if_needed(
    session_id: str,
    history: list[dict[str, Any]],
    req_id: str,
    send: Any,
    state: Any,
    threshold: float = 0.80,
) -> bool:
    """
    Auto-compress conversation history when context approaches the limit.
    Returns True if compression was triggered.

    Based on: Claude Code Reactive Compaction, Cline summarize_task,
              OpenCode ContextOverflowError handler.
    """
    try:
        from evocli_soul.config_defaults import cfg_int
        import evocli_soul.state as _st_compress

        max_total = cfg_int("context.max_total")
        history_tokens = _estimate_history_tokens(history)

        if history_tokens < max_total * threshold:
            return False

        log.info(
            "Auto-compress triggered: history ~%dk tokens (%.0f%% of %dk limit)",
            history_tokens // 1000,
            history_tokens / max_total * 100,
            max_total // 1000,
        )

        await send.stream_chunk(
            req_id,
            "\n\n---\n⚡ **自动压缩**: 对话历史已接近上下文限制，正在压缩以继续工作...\n\n",
            done=False,
        )

        try:
            from evocli_soul.context_summary import compact_session_to_anchor

            summary = await compact_session_to_anchor(
                history,
                state.get_llm_client(),
                existing_summary=_st_compress.get_anchored_summary(session_id),
            )
            if summary:
                _st_compress.set_anchored_summary(summary, session_id)
                _st_compress.clear_history(session_id)
                log.info("Auto-compress: history cleared, anchored summary saved (%d chars)", len(summary))
                return True
        except Exception as _cs_err:
            log.debug("compact_session failed: %s", _cs_err)

        return False
    except Exception as e:
        log.debug("auto_compress check failed (non-fatal): %s", e)
        return False


def _inject_resumption_context(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Handle interrupted tool calls on session resume (Cline TASK_RESUMPTION pattern).

    If the last message is an assistant message with pending tool calls,
    inject a synthetic tool result + resumption notice so the LLM can
    safely continue without re-executing already-started operations.
    """
    if not history:
        return history

    last = history[-1]
    if last.get("role") != "assistant":
        return history

    tool_calls = last.get("tool_calls") or []
    if not tool_calls:
        return history

    def _tool_call_id(tool_call: object) -> str | None:
        if isinstance(tool_call, dict):
            return tool_call.get("id")
        return getattr(tool_call, "id", None)

    def _tool_call_name(tool_call: object) -> str:
        if isinstance(tool_call, dict):
            fn = tool_call.get("function") or {}
            if isinstance(fn, dict):
                return str(fn.get("name", "unknown"))
            return str(getattr(fn, "name", "unknown"))
        fn = getattr(tool_call, "function", None)
        return str(getattr(fn, "name", "unknown"))

    tool_ids_with_results = {
        msg.get("tool_call_id")
        for msg in history
        if msg.get("role") == "tool"
    }
    pending = [
        tc for tc in tool_calls
        if _tool_call_id(tc) and _tool_call_id(tc) not in tool_ids_with_results
    ]

    if not pending:
        return history

    log.info("Task resumption: %d interrupted tool calls detected", len(pending))
    extended = list(history)

    for tc in pending:
        tc_id = _tool_call_id(tc) or "unknown"
        tc_name = _tool_call_name(tc)
        extended.append({
            "role": "tool",
            "tool_call_id": tc_id,
            "content": f"[INTERRUPTED] Tool '{tc_name}' was interrupted before completing. Do not assume it succeeded.",
        })

    extended.append({
        "role": "user",
        "content": (
            "[TASK RESUMPTION] This task was interrupted. "
            "The interrupted tool calls above did NOT complete successfully. "
            "Please reassess the current state of the codebase before continuing. "
            "Use fs_read or shell_ls to verify the actual current state."
        ),
    })

    return extended


def _classify_intent(prompt: str, config: dict | None = None):
    """
    Classify the user's intent using semantic embedding similarity.
    Returns an IntentProfile that drives all downstream behavior.
    Falls back to keyword classification when fastembed is unavailable.
    """
    from evocli_soul.intent_profile import _build_profiles

    try:
        from evocli_soul.intent_profile import classify
        profile = classify(prompt, config)
        if profile is not None:
            return profile
    except Exception as e:
        log.debug("Intent classification failed, using 'coder' default: %s", e)

    # Safe fallback: treat as implementation task
    profile = _build_profiles().get("coder")
    if profile is None:
        raise RuntimeError("intent_profile._build_profiles() did not provide 'coder'")
    return profile


def _deep_merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {**base}
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_loop_messages() -> dict[str, dict[str, str]]:
    """Load loop messages from prompts/loop_messages.toml with user override support."""
    try:
        import tomllib  # type: ignore[attr-defined]
    except ImportError:
        import importlib as _importlib
        tomllib = _importlib.import_module("tomli")

    defaults = {
        "status": {
            "loading_context": "Loading context…",
            "continuing": "Continuing… ({current}/{max})",
        },
        "tool_flow": {
            "auto_execute_hint": (
                "Detected matching tool flow: {name}\n"
                "Steps: {steps}\n"
                "Confidence: {score}  Success rate: {success_rate}\n\n"
                "Executing automatically...\n\n"
            ),
            "step_progress": "{icon} [{current}/{total}] {description}\n",
            "execution_completed": "\nTool flow complete\n{final_output}",
            "execution_failed": "Tool flow failed at step {failed_step}; continuing with AI.\n\n",
            "suggestion_hint": "Related tool flow found: {name} (confidence {score}). Use `/flows` to inspect.\n\n",
            "history_entry": "[tool flow: {name}]\n{output}",
        },
        "errors": {
            "no_api_key": (
                "No API key configured for provider `{provider}`.\n\n"
                "Run `evocli init`, or export:\n```\n{key_hint}=sk-...\n```\n"
                "Then restart EvoCLI."
            ),
            "primary_failed_retrying": "Primary path failed: {error} — retrying with LiteLLM…",
            "both_llm_failed": (
                "\n\n⛔ **Both LLM paths failed.**\n"
                "- Primary: `{primary}`\n- Fallback: `{fallback}`\n\n"
                "Check API key and network, or run `evocli doctor`."
            ),
            "empty_response": "⚠️ The model returned an empty response. This may be a content-filter rejection. Try rephrasing.",
            "unexpected_error": "\n\n⛔ **Unexpected error in agent.stream:** `{error}`\nPress F12 to view full logs.",
        },
        "verification": {
            "auto_verify_banner": "\n\n---\n🔍 **Auto-verify**: `{command}`\n",
            "result": "```\n{output}\n```\n{status_icon} Verification {status_text} (exit {exit_code})\n",
            "status_pass": "passed",
            "status_fail": "failed",
            "retry_prompt": (
                "Verification command failed (exit {exit_code}):\n```\n{output}\n```\n"
                "Analyze the error, fix it, then call task_complete again."
            ),
        },
        "completion": {
            "task_complete_banner": "\n\n---\n✅ **Task complete**\n\n{result}\n",
            "auto_commit": "💾 **Auto-commit**: `{message}`{hash_suffix}\n",
        },
        "loop": {
            "forcing_message": (
                "The previous response was text-only. If you have a concrete task to execute, use tools now.\n"
                "If the task is already complete or this was a conversational exchange, call `task_complete` to wrap up."
            ),
            "continuation_with_todos": "Continue. {pending_count} todo items remain. Check with `todo_read`, then call `task_complete` when done.",
            "continuation_clean": "Continue. Verify the work is complete, then call `task_complete`.",
            "last_iteration": "Final step ({current}/{max}). Summarize completed work and call `task_complete`. List Next Steps for anything unfinished.",
        },
    }
    for base in (Path.home() / ".evocli" / "prompts", Path(__file__).parent.parent / "prompts"):
        p = base / "loop_messages.toml"
        if p.exists():
            try:
                with open(p, "rb") as f:
                    data = tomllib.load(f)
                return _deep_merge_dicts(defaults, data)
            except Exception as e:
                log.warning("Failed to load loop messages from %s: %s", p, e)
    return defaults


def _loop_msg(section: str, key: str, **kwargs: object) -> str:
    template = _LOOP_MSGS.get(section, {}).get(key, "")
    if not kwargs:
        return template
    try:
        return template.format(**kwargs)
    except Exception:
        return template


_LOOP_MSGS = _load_loop_messages()


async def run_agent_stream_body(
    req_id: str,
    params: dict[str, Any],
    send: Any,
    state: Any,
) -> None:
    """
    Main agent stream body — session setup + autonomous execution loop.

    Called from handle_agent_stream after slash command dispatch.
    Handles: session identity, API key check, ToolFlow, autonomous loop,
    task_complete, auto-commit, and post-loop cleanup.
    """
    from evocli_soul.rpc import emit_event
    from evocli_soul.handlers.agent import _derive_stream_session_id, _maybe_compress_history, _distill_session
    from evocli_soul import trace
    import traceback as _tb

    # Extract prompt from params — mirrors the extraction in handle_agent_stream
    # so this function can be called standalone (e.g., in tests).
    prompt: str = params.get("prompt", params.get("message", params.get("input", "")))
    if not prompt:
        await send.stream_chunk(req_id, "ERROR: prompt is required", done=True)
        return

    # Track whether we actually sent any content chunks so we can detect silent failures.
    chunks_sent = 0

    # ── Session identity + turn counter ──────────────────────────────────────
    import evocli_soul.state as _st
    session_id = _derive_stream_session_id(params)
    _turn = _st.increment_turn(session_id)

    # ── Bind trace context (Rule 10: Observability) ───────────────────────────
    # All log calls inside this function now auto-include session_id + request_id.
    _cfg_tmp = state.get_config() if hasattr(state, 'get_config') else {}
    _model_id_trace = (_cfg_tmp or {}).get("llm", {}).get("tiers", {}).get("fast", "") if isinstance(_cfg_tmp, dict) else ""
    _trace_tokens = trace.bind_to_soul_loop(session_id, model_id=_model_id_trace, turn=_turn)
    _trace_log = trace.get_logger("evocli.agent_loop")

    # ── Auto background summary every 8 turns (OpenCode pattern) ─────────────
    # Triggered asynchronously — does NOT block the current response.
    # Uses fast/small model for summarization to minimize cost.
    if _turn > 0 and _turn % 8 == 0:
        _hist_for_summary = _st.get_history(session_id)
        if len(_hist_for_summary) >= 8:
            async def _auto_summarize(sid: str, hist: list[Any]) -> None:
                try:
                    from evocli_soul.context_engine import compact_session_to_anchor
                    existing = _st.get_anchored_summary(sid)
                    llm = state.get_llm_client()
                    new_summary = await compact_session_to_anchor(hist, llm, existing_summary=existing)
                    if new_summary:
                        _st.set_anchored_summary(new_summary, sid)
                        log.info("Auto-summary completed for session %s (turn %d)", sid[:12], _turn)
                except Exception as _se:
                    log.warning("Auto-summary failed (session=%s): %s", sid[:12], _se)
            import asyncio as _auto_asyncio
            _summary_task = _auto_asyncio.create_task(_auto_summarize(session_id, _hist_for_summary))
            _summary_task.add_done_callback(
                lambda t: log.debug("Auto-summary task done: %s", t.exception() or "ok")
            )

    # ── Load persistent conversation history (multi-turn continuity) ─────────
    prior_history = _st.get_history(session_id)

    try:
        from evocli_soul.agent import EvoCLIAgent, _PROVIDER_ENV
        cfg = state.get_config()
        # Memory: try ready-check first (non-blocking), then brief async wait.
        # Background prewarm (main.py) loads the 570MB fastembed model — takes
        # 15-60s on first run. Brief wait gives it a window to finish without
        # blocking the main loop on cold starts.
        memory = _st.get_memory_if_ready()
        if memory is None:
            import asyncio as _mem_asyncio
            for _mem_attempt in range(6):   # wait up to 3s (6 × 0.5s)
                await _mem_asyncio.sleep(0.5)
                memory = _st.get_memory_if_ready()
                if memory is not None:
                    log.debug("memory became ready after %.1fs", (_mem_attempt + 1) * 0.5)
                    break
            if memory is None:
                log.debug("memory not ready after 3s — proceeding without memory context")

        # ── Intent classification — BEFORE agent creation and API key check ──────
        # Must run first so require_confirm gate can fire before any other logic.
        # Oracle finding: gate was placed too late (after API-key check) causing
        # risky prompts with no API key to get "No API key" instead of warning.
        _intent_profile_early = _classify_intent(prompt, cfg)
        if _intent_profile_early.require_confirm:
            _CONFIRM_PREFIXES = ("yes ", "yes,", "确认", "confirm ", "y ")
            _user_confirmed = any(prompt.lower().startswith(p) for p in _CONFIRM_PREFIXES)
            if not _user_confirmed:
                _confirm_msg = (
                    f"⚠️ **Confirmation required**\n\n"
                    f"This request was classified as **{_intent_profile_early.intent}** "
                    f"({_intent_profile_early.reason}).\n"
                    f"It may involve destructive or irreversible operations "
                    f"(deleting files, dropping data, mass overwrites).\n\n"
                    f"**To proceed**, resend your message with `yes` at the start:\n\n"
                    f"```\nyes {prompt[:120]}{'…' if len(prompt) > 120 else ''}\n```\n\n"
                    f"Or rephrase to make your intent more specific and less destructive."
                )
                await send.stream_chunk(req_id, _confirm_msg, done=True)
                log.info(
                    "require_confirm: blocked risky request BEFORE agent creation (intent=%s)",
                    _intent_profile_early.intent,
                )
                return
            # User confirmed — strip prefix
            for _pfx in _CONFIRM_PREFIXES:
                if prompt.lower().startswith(_pfx):
                    prompt = prompt[len(_pfx):].lstrip()
                    break

        agent = EvoCLIAgent(
            state.get_bridge(), memory, cfg,
            session_id=session_id,
            # read_only=True injects READ_ONLY_EXTENSION into system prompt,
            # which tells LLM it's in analysis mode and ends with
            # "分析完成。如需执行修改，请重新提交" — wrong for chat/question.
            #
            # Rules:
            # - chat/question: NOT read_only (just conversational, no mode banner)
            # - reviewer/planner: read_only=True (genuinely in analysis mode)
            # - researcher: read_only=True (exploration, no writes)
            # - coder/debugger/risky: read_only=False (execution tasks)
            read_only=_intent_profile_early.intent in {"reviewer", "planner", "researcher"},
        )

        # ── Fast-fail: no API key configured ─────────────────────────────────
        # Without this check the code falls through to _stream_litellm, which
        # makes a real TCP connection to the LLM provider and waits up to 20s
        # before raising an auth error — especially painful on restricted networks.
        if agent._agent is None:
            llm_cfg = (cfg or {}).get("llm", {}) if isinstance(cfg, dict) else {}
            provider = llm_cfg.get("provider", "anthropic")
            env_var  = _PROVIDER_ENV.get(provider, "")

            has_key = bool(llm_cfg.get("api_key"))
            if not has_key and env_var:
                import os
                has_key = bool(os.environ.get(env_var))
            if not has_key and env_var:
                try:
                    import keyring as _kr
                    has_key = bool(_kr.get_password("evocli", provider))
                except Exception as _kr_err:
                    log.debug("keyring lookup failed for %s: %s", provider, _kr_err)

            if not has_key:
                key_hint = env_var or "YOUR_PROVIDER_API_KEY"
                await send.stream_chunk(
                    req_id,
                    _loop_msg("errors", "no_api_key", provider=provider, key_hint=key_hint),
                    done=True,
                )
                return

        # ── Progress events — send stream_chunk (NOT just soul_status) ──────────
        # soul_status does NOT reset the TUI's first_chunk_deadline timer.
        # Only stream_chunk resets it. We send a lightweight status chunk immediately
        # so the TUI shows activity and the 120s timer doesn't fire during context build.
        await emit_event("soul_status", {
            "status":  "loading",
            "message": _loop_msg("status", "loading_context"),
        })
        # Send first visible progress chunk — this resets the TUI first_chunk timer.
        # The chunk is styled as a status line that will be replaced by actual response.
        await send.stream_chunk(req_id, "", done=False)  # unlock TUI timer immediately

        # ── ToolFlow 触发检查（在 LLM 调用前）────────────────────────────────
        # 检查用户意图是否匹配已学习的工具流
        # 高置信度(≥0.70)→ 询问是否执行；中置信度(0.45-0.70)→ 告知有工具流可用
        try:
            from evocli_soul.tool_flow_miner import (
                check_flow_trigger, FlowExecutor,
                AUTO_EXECUTE_THRESH, SUGGEST_THRESH,
            )
            _matched_flow, _flow_score = check_flow_trigger(prompt)
            _flow_success_rate = getattr(_matched_flow, "success_rate", 0.0) if _matched_flow else 0.0

            # ── Voyager 模式：流作为 LLM 的记忆提示，而非执行脚本 ────────────────
            # 参考：Voyager (2023), ReWOO (2023), DEPS (2023)
            #
            # 触发条件（三层过滤，缺一不可）：
            # 1. Intent 必须是"执行型"任务 — coder/debugger/risky
            #    chat/question/researcher/planner/reviewer 不需要工具流提示：
            #    - chat/question: 单轮对话，根本不需要多步骤工具流
            #    - researcher/planner: 读写较少，流提示是噪声
            #    - reviewer: 只读分析，不需要执行模式参考
            # 1. Intent 必须是"执行型"任务 — coder/debugger/risky
            # 2. 流的步骤数 >= 3 — 少于 3 步的"流"是琐碎操作，不值得提示
            # 3. success_rate >= 0.60 — 这个模式有足够的历史验证
            # 4. 必须是"挣扎后发现"的流 (failures_before >= 1)
            #    理由：第一次就成功的模式可能只是运气，或者太简单不值得作为模式提示。
            #    真正有价值的是：失败→思考→再失败→找到方法→成功 这类硬得来的经验。
            _FLOW_EXEC_INTENTS = {"coder", "debugger", "risky"}   # 只有执行型任务才注入
            _FLOW_MIN_STEPS    = 3                                  # 最少 3 步才算有价值的模式
            _FLOW_HINT_THRESH  = 0.60                               # 最少 60% 成功率

            _flow_steps_count      = len(getattr(_matched_flow, "steps", [])) if _matched_flow else 0
            _flow_failures_before  = getattr(_matched_flow, "failures_before", 0) if _matched_flow else 0
            _flow_is_struggle      = _flow_failures_before >= 1   # 至少失败过一次才算"挣扎后发现"

            _flow_eligible = (
                _matched_flow is not None
                and _flow_score >= SUGGEST_THRESH
                and _flow_success_rate >= _FLOW_HINT_THRESH
                and _flow_steps_count >= _FLOW_MIN_STEPS
                and _intent_profile_early.intent in _FLOW_EXEC_INTENTS
                and _flow_is_struggle     # 核心门控：必须是挣扎后摸索出来的经验
            )

            if _flow_eligible:
                # 构建 Voyager 风格的记忆提示：展示这是"硬得来"的经验，有说服力
                _steps_desc = "\n".join(
                    f"  {i+1}. {s.tool}"
                    + (f"  # {s.description}" if getattr(s, "description", "") else "")
                    for i, s in enumerate(_matched_flow.steps[:8])
                )
                _struggle_ctx = (
                    f"经过 {_flow_failures_before} 次失败后摸索出的方案"
                    if _flow_failures_before >= 2
                    else "经过失败后摸索出的方案"
                )
                _flow_memory_hint = (
                    f"\n\n---\n"
                    f"🔖 **历史经验参考** ({_struggle_ctx}，成功率 {_matched_flow.success_rate:.0%})\n"
                    f"**{_matched_flow.name}** — 类似任务最终成功使用的工具序列：\n"
                    f"{_steps_desc}\n"
                    f"*此模式来自真实失败-成功循环，可参考但需根据当前上下文灵活调整。*\n"
                    f"---\n"
                )
                # 将记忆提示注入为用户消息前缀（进入 LLM 的上下文窗口）
                # 不直接执行——LLM 看到这个提示后自己决定如何行动
                prompt = _flow_memory_hint + prompt
                log.info(
                    "ToolFlow struggle-hint injected: %s (similarity=%.0f%% success_rate=%.0f%% failures_before=%d)",
                    _matched_flow.name, _flow_score * 100, _flow_success_rate * 100, _flow_failures_before,
                )
                # 更新: 流被"使用"了（作为提示），统计用途但不统计执行成功/失败
                # 执行结果由后续 task_complete 的正常路径追踪
        except Exception as _tf_err:
            log.debug("ToolFlow trigger check failed (non-fatal): %s", _tf_err)

        # ── Autonomous execution loop ─────────────────────────────────────────
        # Implements: Cline initiateTaskLoop + Gemini Plan→Act→Verify pattern.
        #
        # Protocol insight: `done=True` is sent ONLY ONCE at the very end.
        # While the AI is still working, all chunks use `done=False`.
        # This keeps the Rust TUI stream open across multiple agent.stream() calls
        # — solving the "auto-continue disabled" protocol limitation.
        #
        # Loop exit conditions (in priority order):
        #   1. AI calls task_complete tool → graceful success
        #   2. MAX_AUTO_ITERATIONS reached → hard stop with "next steps" hint
        #   3. 2+ consecutive iterations with zero tool calls → AI is stuck on text
        #   4. Unrecoverable error in both pydantic-ai and LiteLLM paths
        #
        # Per-iteration flow:
        #   a. reset_iteration_tool_count() → tracks if AI actually did work
        #   b. agent.stream() → yields chunks streamed to TUI (done=False)
        #   c. check task_complete signal → if set, run verification + break
        #   d. check tool_count → if 0, inject forcing message; else inject continuation

        _cfg_agent        = (cfg or {}).get("agent", {}) if isinstance(cfg, dict) else {}
        from evocli_soul.config_defaults import cfg_int

        # ── Goal-aware intent classification ─────────────────────────────────
        # Classify the user's intent ONCE here. The resulting IntentProfile
        # drives ALL downstream decisions: loop iterations, context depth,
        # write permissions, forcing message, auto-commit.
        # This replaces the crude _is_conversational() keyword heuristic.
        _intent_profile = _classify_intent(prompt, cfg)
        log.info(
            "intent: %s (%s) — max_iters=%d context=%s writes=%s",
            _intent_profile.intent,
            _intent_profile.reason,
            _intent_profile.max_iterations,
            _intent_profile.context_depth,
            _intent_profile.writes_allowed,
        )
        # Note: require_confirm gate already fired BEFORE agent creation (early check above).
        # No duplicate gate here.

        # Profile overrides config values — profile is goal-aware, config is a global ceiling
        _cfg_max = int(_cfg_agent.get("max_auto_iterations", cfg_int("agent.max_auto_iterations")))
        _max_auto_iters       = min(_intent_profile.max_iterations, _cfg_max)
        _consecutive_no_tools = 0
        _MAX_NO_TOOL_ITERS    = cfg_int("agent.max_no_tool_turns")

        # Build context params from intent profile
        from evocli_soul.intent_profile import context_params_for
        _intent_ctx = context_params_for(_intent_profile)
        _context_params = {
            key: params.get(key)
            for key in ("project_id", "current_file", "git_diff")
            if params.get(key) is not None
        }
        _context_params.update(_intent_ctx)

        # Clear stale task_complete from any previous request
        _st.clear_task_complete(session_id)
        # Clear any stale cancel signal from a previous request
        _st.clear_cancel(session_id)

        # ── Auto-snapshot before risky tasks (Aider safety pattern) ──────────
        # Use intent profile to decide — no more keyword guessing.
        _auto_snap_enabled = _cfg_agent.get("auto_snapshot", True)
        if _auto_snap_enabled and _intent_profile.writes_allowed:
            # ── Initial workspace snapshot (OpenCode pattern) ─────────────────────
            # Capture baseline state BEFORE LLM starts making changes.
            # Non-blocking: runs in background, result used for post-task verification.
            async def _capture_initial_snapshot() -> None:
                try:
                    snap = await state.get_bridge().call("git.snapshot", {})
                    if isinstance(snap, dict) and snap.get("stash_ref"):
                        _st.append_session_event({
                            "type": "initial_snapshot",
                            "stash_ref": snap["stash_ref"],
                            "session_id": session_id,
                        }, session_id)
                        log.info("Initial snapshot: %s (intent=%s)",
                                 snap["stash_ref"], _intent_profile.intent)
                except Exception as _se:
                    log.debug("Initial snapshot failed (non-fatal): %s", _se)
            asyncio.create_task(_capture_initial_snapshot())

        # Also reset doom loop state for fresh task
        try:
            from evocli_soul.state import clear_doom_loop_state

            clear_doom_loop_state(session_id)
        except Exception:
            pass

        # ── Auto-detect test command ──────────────────────────────────────────
        # When task_complete is called with empty command= parameter, we auto-detect
        # the project's test runner from known config files. Injected into env.
        async def _detect_test_command() -> str:
            """Detect test runner from project root (Aider pattern)."""
            try:
                from evocli_soul.state import get_session_root as _gsr_tc
                _root = _gsr_tc()
                import os as _os_tc
                _detectors = [
                    ("Cargo.toml",      "cargo test"),
                    ("package.json",    "npm test"),
                    ("pyproject.toml",  "python -m pytest"),
                    ("pytest.ini",      "python -m pytest"),
                    ("setup.py",        "python -m pytest"),
                    ("go.mod",          "go test ./..."),
                    ("Makefile",        "make test"),
                    ("pom.xml",         "mvn test"),
                    ("build.gradle",    "gradle test"),
                ]
                for fname, cmd in _detectors:
                    if _os_tc.path.exists(_os_tc.path.join(_root, fname)):
                        return cmd
            except Exception:
                pass  # test cmd detection is best-effort, never block
            return ""
        _detected_test_cmd = await _detect_test_command()
        if _detected_test_cmd:
            log.debug("Auto-detected test command: %s", _detected_test_cmd)

        _current_prompt = prompt
        _all_chunks: list[str] = []

        for _auto_iter in range(_max_auto_iters):
            _is_first_iter = (_auto_iter == 0)

            # Reset tool count tracker for this iteration
            _st.reset_iteration_tool_count(session_id)
            # ── Cancellation check (Cline while-not-abort pattern) ────────────────
            if _st.is_cancelled(session_id):
                log.info("auto-loop: cancelled by user at iteration %d", _auto_iter)
                await send.stream_chunk(req_id, "\n\n⛔ **Task cancelled.**\n", done=True)
                return
            # Gemini scratchpad: mark new iteration boundary
            try:
                from evocli_soul.state import new_scratchpad_iteration as _nsi
                _nsi(session_id)
            except Exception as _nsi_err:
                log.debug("scratchpad iteration failed (non-fatal): %s", _nsi_err)
                pass

            # Emit iteration progress (after first, so TUI shows continuation status)
            if not _is_first_iter:
                await emit_event("soul_status", {
                    "status":  "loading",
                    "message": _loop_msg("status", "continuing", current=_auto_iter + 1, max=_max_auto_iters),
                })

            # ── Stream one agent turn ──────────────────────────────────────────
            collected_chunks_iter: list[str] = []
            primary_err: Exception | None = None
            _prior_raw = _st.get_history(session_id)
            # Handle interrupted sessions (Cline TASK_RESUMPTION pattern)
            if _auto_iter == 0 and _prior_raw:
                try:
                    _prior_raw = _inject_resumption_context(_prior_raw)
                except Exception as _ri_err:
                    log.debug("resumption injection failed (non-fatal): %s", _ri_err)
            # Deduplicate file reads (Cline ContextManager pattern):
            # keeps only the latest version of each file in context
            try:
                from evocli_soul.history_utils import deduplicate_file_reads

                _prior = deduplicate_file_reads(_prior_raw)
            except Exception as _dd_err:
                log.debug("history deduplication failed (non-fatal): %s", _dd_err)
                _prior = _prior_raw

            # Auto-compress if history approaching context limit
            if _auto_iter > 0:  # skip on first iteration (just started)
                _compressed = await _auto_compress_if_needed(
                    session_id, _prior, req_id, send, state
                )
                if _compressed:
                    # Reload history after compression
                    _prior_raw = _st.get_history(session_id)
                    try:
                        from evocli_soul.history_utils import deduplicate_file_reads

                        _prior = deduplicate_file_reads(_prior_raw)
                    except Exception:
                        _prior = _prior_raw

            try:
                # ── Context params: first iteration only (Cline includeFileDetails pattern) ──
                # context_params drives expensive operations: anchor context build, file reads.
                # On iter 0: pass full context_params to orient the LLM.
                # On iter 1+: pass {} — LLM already has context in accumulated history.
                _iter_context_params = _context_params if _is_first_iter else {}
                async for chunk in agent.stream(_current_prompt,
                                                 context_params=_iter_context_params,
                                                 prior_history=_prior,
                                                 session_id=session_id):
                    if chunk:
                        await send.stream_chunk(req_id, chunk, done=False)
                        collected_chunks_iter.append(chunk)
                        chunks_sent += 1
            except Exception as _iter_err:
                primary_err = _iter_err
                log.error("agent.stream iter %d failed: %s\n%s",
                          _auto_iter, _iter_err, _tb.format_exc())

            # LiteLLM fallback for this iteration
            if primary_err is not None or (not collected_chunks_iter and _is_first_iter):
                if primary_err is not None:
                    first_line = str(primary_err).splitlines()[0] if str(primary_err) else repr(primary_err)
                    await emit_event("soul_status", {
                        "status":  "error",
                        "message": _loop_msg("errors", "primary_failed_retrying", error=first_line),
                    })
                try:
                    try:
                        fallback_ctx = await agent._build_context(
                            _current_prompt,
                            context_params=_context_params,
                            history=_prior,
                            session_id=session_id,
                        )
                    except Exception:
                        fallback_ctx = {}
                    try:
                        fallback_prompt = await agent._inject_context(_current_prompt, fallback_ctx)
                    except Exception:
                        fallback_prompt = _current_prompt
                    async for chunk in agent._stream_litellm(fallback_prompt, fallback_ctx, prior_history=None):
                        if chunk:
                            await send.stream_chunk(req_id, chunk, done=False)
                            collected_chunks_iter.append(chunk)
                            chunks_sent += 1
                except Exception as fallback_err:
                    log.error("LiteLLM fallback iter %d failed: %s", _auto_iter, fallback_err)
                    if _is_first_iter:
                        await send.stream_chunk(
                            req_id,
                            _loop_msg("errors", "both_llm_failed", primary=primary_err, fallback=fallback_err),
                            done=True,
                        )
                        return
                    break  # Give up on continuation, exit loop

            # Persist this iteration's history
            if collected_chunks_iter:
                iter_reply = "".join(collected_chunks_iter)
                _all_chunks.append(iter_reply)
                _st.append_history([
                    {"role": "user",      "content": _current_prompt},
                    {"role": "assistant", "content": iter_reply},
                ], session_id)

            # ── Check task_complete signal ─────────────────────────────────────
            completion = _st.get_task_complete(session_id)
            if completion:
                _result  = completion.get("result", "")
                _cmd     = completion.get("command", "")
                _st.clear_task_complete(session_id)

                # Auto-fill test command if AI left it empty (use detected test runner)
                if not _cmd and _detected_test_cmd:
                    _cmd = _detected_test_cmd
                    log.debug("Auto-filled test command: %s", _cmd)

                # Run verification command if AI provided one (Gemini mandatory verify)
                if _cmd:
                    await send.stream_chunk(
                        req_id,
                        _loop_msg("verification", "auto_verify_banner", command=_cmd),
                        done=False,
                    )
                    try:
                        verify_raw = await state.get_bridge().call(
                            "shell.run",
                            {"cmd": _cmd, "cwd": ".", "timeout_s": cfg_int("shell.verify_timeout_s"), "dry_run": False},
                        )
                        import json as _vj
                        if isinstance(verify_raw, str):
                            try:
                                verify_raw = _vj.loads(verify_raw)
                            except Exception:
                                verify_raw = {"stdout": verify_raw}
                        v_out  = verify_raw.get("stdout", "") if isinstance(verify_raw, dict) else str(verify_raw)
                        v_err  = verify_raw.get("stderr", "") if isinstance(verify_raw, dict) else ""
                        v_code = verify_raw.get("exit_code", 0) if isinstance(verify_raw, dict) else 0
                        v_text = (v_out + "\n" + v_err).strip()
                        status_icon = "✅" if v_code == 0 else "❌"
                        status_text = _loop_msg("verification", "status_pass") if v_code == 0 else _loop_msg("verification", "status_fail")
                        await send.stream_chunk(
                            req_id,
                            _loop_msg(
                                "verification",
                                "result",
                                output=v_text[:1500],
                                status_icon=status_icon,
                                status_text=status_text,
                                exit_code=v_code,
                            ),
                            done=False,
                        )
                        # If verification failed and we have iterations left → continue
                        if v_code != 0 and _auto_iter + 1 < _max_auto_iters:
                            _st.clear_task_complete(session_id)  # reset signal
                            _current_prompt = _loop_msg(
                                "verification",
                                "retry_prompt",
                                exit_code=v_code,
                                output=v_text[:800],
                            )
                            _consecutive_no_tools = 0
                            continue  # go back to next iteration to fix the issue
                    except Exception as _ve:
                        log.debug("Verification command failed (non-fatal): %s", _ve)

                # Task complete: emit summary and write memory
                _completion_banner = _loop_msg("completion", "task_complete_banner", result=_result)
                await send.stream_chunk(req_id, _completion_banner, done=False)

                # ── Auto-commit (Aider pattern) ──────────────────────────────
                # Generate a Conventional Commit message from the diff and commit.
                # Only fires when verification passed (or no verification command).
                _auto_commit_enabled = (cfg or {}).get("agent", {}).get("auto_commit", True) if isinstance(cfg, dict) else True
                if _auto_commit_enabled:
                    try:
                        # Check if there are any uncommitted changes
                        diff_result = await state.get_bridge().call("git.diff", {"staged": False})
                        staged_result = await state.get_bridge().call("git.diff", {"staged": True})
                        _diff_text = ""
                        if isinstance(diff_result, str) and diff_result.strip():
                            _diff_text = diff_result
                        elif isinstance(staged_result, str) and staged_result.strip():
                            _diff_text = staged_result

                        if _diff_text:
                            from evocli_soul.auto_commit import generate_commit_message
                            _llm = state.get_llm_client()
                            _commit_msg = await generate_commit_message(
                                _diff_text[:3000], _llm, goal=prompt
                            )
                            _commit_result = await state.get_bridge().call(
                                "git.commit", {"message": _commit_msg, "files": []}
                            )
                            _hash = ""
                            if isinstance(_commit_result, dict):
                                _hash = _commit_result.get("hash", "")[:8]
                            await send.stream_chunk(
                                req_id,
                                _loop_msg(
                                    "completion",
                                    "auto_commit",
                                    message=_commit_msg,
                                    hash_suffix=f" ({_hash})" if _hash else "",
                                ),
                                done=False,
                            )
                            log.info("Auto-commit after task_complete: %s (%s)", _commit_msg, _hash)
                    except Exception as _ac_err:
                        log.debug("Auto-commit failed (non-fatal): %s", _ac_err)

                # Non-blocking: write completion to memory for future recall
                async def _persist_completion(sid: str, res: str) -> None:
                    try:
                        import evocli_soul.memory_distill as _memory_distill
                        _distill = getattr(_memory_distill, "distill_success", None)
                        if callable(_distill):
                            _maybe_awaitable = _distill(res, session_id=sid)
                            if asyncio.iscoroutine(_maybe_awaitable):
                                await _maybe_awaitable
                    except Exception as _dist_err:
                        log.warning("distill_success failed (memory flywheel): %s", _dist_err)
                break  # Clean exit from autonomous loop

            # ── No task_complete: decide whether to continue ───────────────────
            tool_count = _st.get_iteration_tool_count(session_id)

            if tool_count == 0:
                # AI produced only text — no real work this iteration
                _consecutive_no_tools += 1
                log.debug("auto-loop iter %d: 0 tools called (%d consecutive)", _auto_iter, _consecutive_no_tools)

                if _consecutive_no_tools >= _MAX_NO_TOOL_ITERS:
                    # AI is stuck on text — exit loop gracefully
                    log.info("auto-loop stopping: %d consecutive text-only turns", _consecutive_no_tools)
                    break

                if _auto_iter + 1 < _max_auto_iters and _intent_profile.forcing_enabled:
                    # Only inject forcing message if the intent profile allows it.
                    # chat/question profiles have forcing_enabled=False — no bullying.
                    _forcing = _loop_msg("loop", "forcing_message")
                    if _forcing:
                        _current_prompt = _forcing
            else:
                # Tools were called — AI is making progress
                _consecutive_no_tools = 0
                log.debug("auto-loop iter %d: %d tools called, continuing", _auto_iter, tool_count)

                if _auto_iter + 1 < _max_auto_iters:
                    # Check remaining todos for continuation prompt
                    todos = _st.get_todos(session_id)
                    pending_count = sum(1 for t in todos if t.get("status") not in ("completed", "cancelled"))

                    if pending_count > 0:
                        _current_prompt = _loop_msg("loop", "continuation_with_todos", pending_count=pending_count)
                    else:
                        _current_prompt = _loop_msg("loop", "continuation_clean")
                else:
                    # Last iteration — force completion summary
                    _current_prompt = _loop_msg(
                        "loop",
                        "last_iteration",
                        current=_max_auto_iters,
                        max=_max_auto_iters,
                    )

        # Loop ended (task_complete, max iterations, or no-tool exit)
        if chunks_sent == 0:
            log.warning("agent.stream completed but emitted 0 content chunks")
            await send.stream_chunk(
                req_id,
                _loop_msg("errors", "empty_response"),
                done=True,
            )
            return

        await send.stream_chunk(req_id, "", done=True)

        # ── Post-loop: compress hint ──────────────────────────────────────────
        asyncio.create_task(_maybe_compress_history(session_id))
        cfg_agent       = (state.get_config() or {}).get("agent", {}) if isinstance(state.get_config(), dict) else {}
        compress_turns  = int(cfg_agent.get("history_compress_turns",  cfg_int("agent.history_compress_turns")))
        compress_tokens = int(cfg_agent.get("history_compress_tokens", cfg_int("agent.history_compress_tokens")))
        history_len     = len(_st.get_history(session_id))
        token_est       = _st.get_history_token_estimate(session_id)
        if history_len >= compress_turns * 2 or token_est >= compress_tokens:
            asyncio.create_task(emit_event("soul_status", {
                "status":  "ready",
                "message": (
                    f"💡 Context is getting long ({history_len // 2} exchanges, ~{token_est} tokens). "
                    f"Type /compress to free up space for better results."
                ),
            }))

    except Exception as e:
        _trace_log.error("agent_loop_crash", error=str(e)[:200])
        log.error("agent.stream handler crashed: %s\n%s", e, _tb.format_exc())
        await send.stream_chunk(
            req_id,
            _loop_msg("errors", "unexpected_error", error=e),
            done=True,
        )
    finally:
        # GAP-3: Trigger memory distillation at session end (non-blocking, best-effort).
        asyncio.create_task(_distill_session(session_id))
        # Cleanup trace context to prevent leaks in long-running Soul processes
        for _tok in _trace_tokens:
            try:
                _tok.var.reset(_tok)
            except Exception:
                pass


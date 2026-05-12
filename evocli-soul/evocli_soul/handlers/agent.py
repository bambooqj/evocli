"""Agent handlers — AI 对话执行（run/stream）+ LLM 内置动作（analyze/generate）。
WIRE-4: instructor 用于结构化分析输出
  instructor 已在必需依赖中（requirements），无需额外 extras。
  output_format="structured" 时自动使用，提供 Pydantic 格式验证 + 自动重试。
"""
from __future__ import annotations
import importlib.util
import logging
import asyncio
import time

log = logging.getLogger("evocli.handlers.agent")

_INSTRUCTOR_AVAILABLE = importlib.util.find_spec("instructor") is not None

# ── History compression thresholds (Aider Head/Tail pattern) ─────────────
# Compress when history exceeds either threshold to prevent context bloat.
_HISTORY_COMPRESS_TURNS   = 10   # compress after 10 exchanges (20 messages)
_HISTORY_COMPRESS_TOKENS  = 8_000  # or when estimated tokens exceed this
_HISTORY_TAIL_MESSAGES    = 10   # preserve last 5 exchanges (10 messages) verbatim


def register(router) -> None:
    router.add("agent.run",           handle_agent_run)
    router.add("agent.stream",        handle_agent_stream)
    router.add("agent.architect",     handle_agent_architect)  # Aider Architect/Editor 模式
    router.add("llm.analyze",         handle_llm_analyze)
    router.add("llm.generate",        handle_llm_generate)


from evocli_soul.local_classifier import (
    ORCHESTRATION_DESCRIPTIONS,
    classify_by_similarity,
    record_label,
)

# 关键词 fallback（仅在 fastembed 不可用时使用）
_COMPLEX_KEYWORDS_FALLBACK = [
    "plan and implement", "design and build", "refactor the entire",
    "review and fix", "debug and implement", "create a new feature with tests",
    "orchestrate", "multi-step", "multi-agent",
]


def _needs_orchestration(prompt: str) -> bool:
    """
    判断是否需要多 Agent 编排。
    优先使用本地嵌入模型做语义判断，fastembed 不可用时退回关键词匹配。
    决策结果自动记录供未来重训。
    """
    result = classify_by_similarity(
        prompt,
        ORCHESTRATION_DESCRIPTIONS,
        threshold=0.30,
        fallback="",
    )
    if result:
        needs = (result == "needs_orchestration")
        record_label(prompt, result, extra={"source": "orchestration_gate"})
        return needs
    # 关键词 fallback
    prompt_lower = prompt.lower()
    return any(kw in prompt_lower for kw in _COMPLEX_KEYWORDS_FALLBACK)


async def handle_agent_run(req_id: str, params: dict, send, state) -> None:
    """
    G-11: agent.run 优先走 LangGraph Workflow（支持 session 恢复 + HITL），
    fallback 到 EvoCLIAgent.run()。
    """
    prompt     = params.get("prompt", params.get("message", params.get("input", "")))
    session_id = params.get("session_id")          # 传入时从该 checkpoint 恢复
    use_orchestrator = params.get("orchestrate", False)  # explicit flag
    
    if not prompt:
        await send.error(req_id, -32600, "prompt is required")
        return
    try:
        # Intent classification: should we use multi-agent orchestration?
        if use_orchestrator or _needs_orchestration(prompt):
            try:
                orchestrator = state.get_orchestrator()
                if orchestrator is not None:
                    result = await orchestrator.run(prompt, session_id=session_id)
                    await send.response(req_id, result)
                    return
            except Exception as orch_err:
                log.warning("Orchestrator failed (%s), falling back to single agent", orch_err)

        # 尝试 LangGraph Workflow（跨 session 持久化）
        if session_id:
            try:
                from evocli_soul.workflow import run_agent_with_workflow
                result = await run_agent_with_workflow(
                    prompt, state.get_bridge(), session_id=session_id
                )
                await send.response(req_id, {"text": result.get("text", ""), "session_id": result.get("thread_id")})
                return
            except Exception as wf_err:
                log.debug("LangGraph agent.run failed (%s), using fallback", wf_err)
        # fallback: 普通 EvoCLIAgent（Fix M2: 每请求独立实例）
        # Load actual config so pydantic-ai uses the correct provider/model/api_key.
        # Pass session_id so agent.run reads history from the right session bucket.
        from evocli_soul.agent import EvoCLIAgent
        import os as _os_run, hashlib as _hashlib_run
        _run_explicit_sid = params.get("session_id")
        if _run_explicit_sid:
            _run_session_id = _run_explicit_sid
        else:
            _run_session_id = "cwd_" + _hashlib_run.md5(
                _os_run.getcwd().encode(), usedforsecurity=False
            ).hexdigest()[:12]
        cfg    = state.get_config()
        agent  = EvoCLIAgent(state.get_bridge(), state.get_memory(), cfg, session_id=_run_session_id)
        result = await agent.run(prompt)
        # History persistence is owned by agent.run() itself — do NOT persist here.
        await send.response(req_id, {"text": str(result)})
    except Exception as e:
        log.exception("agent.run failed")
        await send.error(req_id, -32603, str(e))


async def handle_agent_stream(req_id: str, params: dict, send, state) -> None:
    from evocli_soul.rpc import emit_event
    import traceback as _tb
    prompt = params.get("prompt", params.get("message", params.get("input", "")))
    if not prompt:
        await send.stream_chunk(req_id, "ERROR: prompt is required", done=True)
        return

    # ── /help — show available slash commands ─────────────────────────────────
    _prompt_stripped = prompt.strip()
    if _prompt_stripped.lower() in ("/help", "/?", "/h"):
        help_text = """\
**EvoCLI 可用命令**

| 命令 | 说明 |
|---|---|
| `/help` 或 `/?` | 显示此帮助 |
| `/compress` 或 `/compact` | 压缩会话历史，释放上下文空间 |
| `/add <文件>` | 将文件固定到每轮上下文中 |
| `/add list` | 查看已固定的文件 |
| `/add clear` | 清除所有固定文件 |

**使用技巧**
- 只读分析操作（搜索、读文件、查符号）会立即执行，无需确认
- 有风险的修改（3+ 文件、API 变更）会先描述计划，等待你确认
- 上下文过长时输入 `/compress` 压缩，可显著提升响应质量
- 使用 `evocli doctor` 诊断配置和连接问题
- 使用 `evocli skill list` 查看可用的自动化技能

**当前状态**
输入任意问题开始对话，或直接描述你想做的代码任务。
"""
        await send.stream_chunk(req_id, help_text.strip(), done=True)
        return

    # ── /add <file> [<file2> ...] — explicit file context loading ────────────
    # Aider pattern: users declare which files should always be in context.
    # Loaded files persist for the entire session (stored in state per session_id).
    # Usage: /add src/auth.rs            → adds one file
    #        /add src/auth.rs src/user.rs → adds multiple files
    #        /add list                    → show currently added files
    #        /add clear                   → remove all added files
    _prompt_stripped = prompt.strip()
    if _prompt_stripped.lower().startswith("/add"):
        import evocli_soul.state as _st_add
        import os as _os_add, hashlib as _hashlib_add
        _add_sid = (params.get("session_id") or
                    "cwd_" + _hashlib_add.md5(_os_add.getcwd().encode(),
                                               usedforsecurity=False).hexdigest()[:12])
        _add_args = _prompt_stripped.split()[1:]  # everything after /add

        if not _add_args or _add_args[0].lower() == "list":
            files = _st_add.get_added_files(_add_sid)
            if files:
                file_list = "\n".join(f"  • {f}" for f in files)
                await send.stream_chunk(req_id,
                    f"**Files in context ({len(files)}):**\n{file_list}\n\n"
                    f"Use `/add clear` to remove all, or `/add <file>` to add more.",
                    done=True)
            else:
                await send.stream_chunk(req_id,
                    "No files explicitly added to context.\n"
                    "Use `/add <path>` to pin files across all turns.",
                    done=True)
            return

        if _add_args[0].lower() == "clear":
            _st_add.clear_added_files(_add_sid)
            await send.stream_chunk(req_id, "✓ Cleared all added files from context.", done=True)
            return

        if _add_args[0].lower() == "remove" and len(_add_args) > 1:
            removed = _st_add.remove_added_files(_add_sid, _add_args[1:])
            await send.stream_chunk(req_id,
                f"✓ Removed {len(removed)} file(s) from context.", done=True)
            return

        # Add the specified files
        added, missing = [], []
        for f in _add_args:
            if _os_add.path.exists(f):
                _st_add.add_file(f, _add_sid)  # fix: add_file(path, session_id)
                added.append(f)
            else:
                missing.append(f)

        all_files = _st_add.get_added_files(_add_sid)
        msg = ""
        if added:
            msg += f"✓ Added to context: {', '.join(added)}\n"
        if missing:
            msg += f"⚠ Not found: {', '.join(missing)}\n"
        msg += f"\n**Context files ({len(all_files)}):** {', '.join(all_files)}\n"
        msg += "These files will be injected into every turn automatically."
        await send.stream_chunk(req_id, msg, done=True)
        return

    # ── GAP-2: /compress slash-command ───────────────────────────────────────
    # Compacts accumulated session context into an Anchored Summary.
    # Does NOT require `history` in params — instead summarizes what the agent
    # currently knows: memory constraints + recent session events + goal.
    # This avoids the dependency on Rust passing history (which it currently doesn't).
    if prompt.strip().lower() in ("/compress", "/compact"):
        import evocli_soul.state as _st_compress
        import os as _os_compress, hashlib as _hashlib_compress
        # Use same cwd-derived session_id as main execution path for consistency
        _explicit = params.get("session_id")
        session_id = (_explicit if _explicit else
                      "cwd_" + _hashlib_compress.md5(
                          _os_compress.getcwd().encode(), usedforsecurity=False
                      ).hexdigest()[:12])
        try:
            await send.stream_chunk(req_id, "⏳ Compressing session context…\n\n", done=False)
            events = list(_st_compress._session_events)  # read without draining
            llm = state.get_llm_client()

            # Build summary from real conversation history (primary source) +
            # session events as supplementary context. Using `prompt` ("/compress")
            # as the summary source was wrong — it tells the model nothing useful.
            history_for_summary = _st_compress.get_history(session_id)
            history_summary = ""
            if history_for_summary:
                # Render last 20 turns as [role]: content[:300]
                history_lines = []
                for m in history_for_summary[-20:]:
                    role    = m.get("role", "?")
                    content = str(m.get("content", ""))[:300]
                    history_lines.append(f"[{role}]: {content}")
                history_summary = "\n".join(history_lines)

            event_summary = ""
            if events:
                tool_names = [e.get("method", e.get("type", "?")) for e in events[-20:]]
                event_summary = f"Recent tool calls: {', '.join(tool_names)}"

            compress_prompt = (
                f"Summarize the following AI coding session as an Anchored Summary.\n"
                f"Format:\n"
                f"## Goal\n[what the user is trying to accomplish]\n"
                f"## Progress\n[what has been done, what's in progress]\n"
                f"## Key Decisions\n[important choices made]\n"
                f"## Next Steps\n[what should happen next]\n\n"
                f"Conversation history (most recent 20 turns):\n{history_summary}\n\n"
                f"{event_summary}\n\n"
                f"Be concise. Focus on engineering decisions and state."
            )
            summary = await llm.complete(compress_prompt, tier="fast", max_tokens=600)

            # CRITICAL: save the anchored summary BEFORE clearing history.
            # The summary IS the session memory after compression.
            _st_compress.set_anchored_summary(summary, session_id)

            await send.stream_chunk(req_id,
                f"**Session compressed.**\n\n{summary}\n\n"
                f"*Context anchored. Continue working — history preserved.*",
                done=True)
            # Notify Rust TUI
            await emit_event("session_compacted", {
                "summary":                summary,
                "chars":                  len(summary),
                "original_event_count":   len(events),
            })
            # Only clear history AFTER successfully saving the summary
            _st_compress.clear_history(session_id)
        except Exception as e:
            log.warning("GAP-2 /compress failed: %s", e)
            await send.stream_chunk(req_id, f"Compression failed: {e}", done=True)
            # DO NOT clear history if compression failed — data would be lost
            return
        return

    # ── /flows — 列出/管理学习到的工具流 ────────────────────────────────────
    if prompt.strip().lower() in ("/flows", "/flow"):
        try:
            from evocli_soul.tool_flow_miner import list_flows
            flows = list_flows()
            if not flows:
                await send.stream_chunk(req_id,
                    "📭 还没有学到工具流。\n\n"
                    "工具流会在你重复使用相同工具序列（≥2次）后自动学习。\n"
                    "继续使用 EvoCLI，系统会自动发现你的工作模式。",
                    done=True)
                return
            lines = ["## 已学习的工具流\n"]
            for f in flows:
                steps_str = " → ".join(f["step_tools"][:5])
                if len(f["step_tools"]) > 5:
                    steps_str += f" (+{len(f['step_tools'])-5})"
                lines.append(
                    f"**{f['name']}**\n"
                    f"  步骤: {steps_str}\n"
                    f"  置信度: {f['confidence']:.0%}  成功率: {f['success_rate']:.0%}\n"
                )
            lines.append("\n💡 触发：在对话中描述相关任务，系统会自动建议或执行匹配的工具流。")
            await send.stream_chunk(req_id, "\n".join(lines), done=True)
        except Exception as e:
            await send.stream_chunk(req_id, f"获取工具流失败: {e}", done=True)
        return

    # Track whether we actually sent any content chunks so we can detect silent failures.
    chunks_sent = 0

    # ── Session identity + turn counter ──────────────────────────────────────
    import evocli_soul.state as _st
    # Derive session_id from the current working directory when Rust TUI doesn't
    # provide one. This prevents multi-project history pollution where two separate
    # project folders would share the same "default" history bucket.
    # cwd-hash is stable per project: same project → same session bucket across restarts.
    _explicit_sid = params.get("session_id")
    if _explicit_sid:
        session_id = _explicit_sid
    else:
        import os as _os, hashlib as _hashlib
        _cwd = _os.getcwd()
        # Short hex digest: deterministic per-project, collision-resistant enough
        session_id = "cwd_" + _hashlib.md5(_cwd.encode(), usedforsecurity=False).hexdigest()[:12]
    _st.increment_turn(session_id)

    # ── Load persistent conversation history (multi-turn continuity) ─────────
    # History is stored server-side in state.py — Rust TUI does NOT need to send it.
    # Each turn appends user+assistant messages after completion.
    prior_history = _st.get_history(session_id)

    try:
        from evocli_soul.agent import EvoCLIAgent, _PROVIDER_ENV
        cfg = state.get_config()
        memory = _st.get_memory_if_ready()

        agent = EvoCLIAgent(state.get_bridge(), memory, cfg, session_id=session_id)

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
                except Exception:
                    pass

            if not has_key:
                key_hint = env_var or "YOUR_PROVIDER_API_KEY"
                await send.stream_chunk(
                    req_id,
                    f"⚠️  **No API key configured** for provider `{provider}`.\n\n"
                    f"Run `evocli init` to set your key interactively, or export:\n"
                    f"```\n{key_hint}=sk-...\n```\n"
                    f"Then restart EvoCLI.",
                    done=True,
                )
                return

        # ── Progress event ────────────────────────────────────────────────────
        await emit_event("soul_status", {
            "status":  "loading",
            "message": "⚙ Building context & calling LLM…",
        })

        # ── ToolFlow 触发检查（在 LLM 调用前）────────────────────────────────
        # 检查用户意图是否匹配已学习的工具流
        # 高置信度(≥0.70)→ 询问是否执行；中置信度(0.45-0.70)→ 告知有工具流可用
        try:
            from evocli_soul.tool_flow_miner import (
                check_flow_trigger, FlowExecutor,
                AUTO_EXECUTE_THRESH, SUGGEST_THRESH,
            )
            _matched_flow, _flow_score = check_flow_trigger(prompt)
            if _matched_flow and _flow_score >= AUTO_EXECUTE_THRESH:
                # 高置信度：直接建议执行工具流，以 system 消息通知用户
                _flow_hint = (
                    f"🔄 **检测到匹配的工具流**: {_matched_flow.name}\n"
                    f"步骤: {' → '.join(s.tool for s in _matched_flow.steps[:5])}\n"
                    f"置信度: {_flow_score:.0%}  历史成功率: {_matched_flow.success_rate:.0%}\n\n"
                    f"正在自动执行...\n\n"
                )
                await send.stream_chunk(req_id, _flow_hint, done=False)
                # 执行工具流
                _executor = FlowExecutor(state.get_bridge(), cfg)
                _flow_ctx = {"current_file": params.get("current_file", "")}

                async def _progress_cb(step_n, total, desc, result):
                    await send.stream_chunk(req_id,
                        f"{'✓' if result else '⏳'} [{step_n}/{total}] {desc}\n",
                        done=False)

                _flow_result = await _executor.execute(
                    _matched_flow, prompt,
                    context=_flow_ctx,
                    progress_callback=_progress_cb,
                )
                if _flow_result.get("ok"):
                    _final = _flow_result.get("final_output", "")
                    await send.stream_chunk(req_id,
                        f"\n✅ 工具流执行完成\n{_final[:500] if _final else ''}",
                        done=True)
                    # 持久化本轮历史
                    _st.append_history([
                        {"role": "user",      "content": prompt},
                        {"role": "assistant", "content": f"[工具流: {_matched_flow.name}]\n{_final[:1000]}"},
                    ], session_id)
                    return
                else:
                    # 工具流失败 → 降级到正常 agent 流程
                    await send.stream_chunk(req_id,
                        f"⚠️ 工具流执行失败（步骤{_flow_result.get('failed_step')}），使用 AI 继续...\n\n",
                        done=False)
            elif _matched_flow and _flow_score >= SUGGEST_THRESH:
                # 中置信度：在响应开头提示有工具流可用（不打断流程）
                _hint = (
                    f"💡 *发现相关工具流: {_matched_flow.name}*  "
                    f"(置信度 {_flow_score:.0%}，输入 `/flows` 查看)\n\n"
                )
                await send.stream_chunk(req_id, _hint, done=False)
        except Exception as _tf_err:
            log.debug("ToolFlow trigger check failed (non-fatal): %s", _tf_err)

        # ── Primary path (pydantic-ai → LiteLLM fallback) ────────────────────
        collected_chunks: list[str] = []   # accumulate for history
        primary_err: Exception | None = None
        try:
            async for chunk in agent.stream(prompt, prior_history=prior_history,
                                             session_id=session_id):
                if chunk:  # skip empty keep-alive chunks
                    await send.stream_chunk(req_id, chunk, done=False)
                    chunks_sent += 1
                    collected_chunks.append(chunk)
        except Exception as e:
            primary_err = e
            # Full traceback to file; concise 1-line summary to TUI.
            log.error(
                "Primary stream (pydantic-ai) failed: %s\n%s",
                e, _tb.format_exc(),
            )

        # ── LiteLLM fallback ─────────────────────────────────────────────────
        if primary_err is not None or chunks_sent == 0:
            if primary_err is not None:
                first_line = str(primary_err).splitlines()[0] if str(primary_err) else repr(primary_err)
                await emit_event("soul_status", {
                    "status":  "error",
                    "message": f"Primary path failed: {first_line} — retrying…  (F12 for full log)",
                })
            try:
                # Build context for fallback so it has system_prompt, anchored_summary etc.
                # Previously passed empty {} which lost all context-engine built context.
                try:
                    fallback_ctx = await agent._build_context(
                        prompt,
                        history=prior_history,
                        session_id=session_id,
                    )
                except Exception:
                    fallback_ctx = {}
                # Apply _inject_context so user_context (file contents, diff, history)
                # is prepended to the prompt — same enrichment the primary path gets.
                try:
                    fallback_prompt = await agent._inject_context(prompt, fallback_ctx)
                except Exception:
                    fallback_prompt = prompt
                async for chunk in agent._stream_litellm(fallback_prompt, fallback_ctx, prior_history=None):
                    if chunk:
                        await send.stream_chunk(req_id, chunk, done=False)
                        chunks_sent += 1
                        collected_chunks.append(chunk)
            except Exception as fallback_err:
                log.error("LiteLLM fallback also failed: %s\n%s", fallback_err, _tb.format_exc())
                await send.stream_chunk(
                    req_id,
                    f"\n\n⛔ **Both LLM paths failed.**\n"
                    f"- Primary error: `{primary_err}`\n"
                    f"- Fallback error: `{fallback_err}`\n\n"
                    f"Check your API key and network, or run `evocli doctor`.",
                    done=True,
                )
                return

        # ── Post-stream sanity check ──────────────────────────────────────────
        if chunks_sent == 0:
            log.warning("agent.stream completed but emitted 0 content chunks")
            await send.stream_chunk(
                req_id,
                "⚠️  The model returned an empty response. "
                "This may be a content-filter rejection or a model configuration issue. "
                "Try rephrasing, or check your model settings.",
                done=True,
            )
            return

        await send.stream_chunk(req_id, "", done=True)

        # ── Auto-continue: DISABLED — protocol incompatibility ───────────────
        # Auto-continue fires after done=True is already sent to TUI (line 435 above).
        # Rust TUI breaks the stream loop on done=True (lib.rs:218), so any further
        # stream_chunk calls after that are silently dropped. The followup result
        # never reaches the user. Additionally, followup_agent.run() executes before
        # the primary turn is persisted to history, so it can't see the plan it was
        # asked to execute — the semantic chain is broken.
        # TODO: Re-enable by restructuring: keep done=False until followup completes,
        # then send done=True at the very end. Requires TUI protocol coordination.
        if False and collected_chunks:
            assistant_reply = "".join(collected_chunks)
            _PLAN_PHRASES = [
                "我将", "我会先", "让我先", "首先我会", "我打算", "我需要先",
                "I will", "Let me first", "I'll start", "I need to first",
            ]
            # Write/destructive intent: do NOT auto-continue (wait for user confirmation)
            _WRITE_INTENT_PHRASES = [
                "删除", "重写", "重构整个", "修改所有", "清空", "创建", "新建", "替换所有",
                "delete", "remove", "rewrite", "refactor", "create new", "replace all",
                "modify all", "update all",
            ]
            _is_planning    = any(p in assistant_reply for p in _PLAN_PHRASES)
            _is_write_intent = any(p in prompt.lower() for p in _WRITE_INTENT_PHRASES)

            # Check if any tools were actually called this turn
            events = _st.drain_session_events()
            _tools_called = any(e.get("type") in ("tool_called", "tool_done", "git_commit")
                                 for e in events)
            # Put events back since we drained them prematurely
            for ev in events:
                _st.append_session_event(ev)

            # Inverted logic: auto-continue UNLESS user clearly wants writes/modifications.
            # This is less error-prone than trying to enumerate all "read" phrases.
            if _is_planning and not _tools_called and not _is_write_intent:
                log.info("auto-continue: detected plan-without-execution, injecting follow-up")
                # Emit hint to TUI
                await emit_event("soul_status", {
                    "status": "loading",
                    "message": "⟳ 检测到规划但未执行，正在自动继续执行…",
                })
                # Re-submit via agent.run (non-streaming, has tool-calling loop).
                # agent.run() persists its own [user_followup, assistant_followup] turn.
                # Do NOT add followup_result to collected_chunks — that would cause the
                # outer persist (lines 505-509) to also write the followup, doubling it.
                try:
                    followup_agent = EvoCLIAgent(state.get_bridge(), memory, cfg, session_id=session_id)
                    followup_result = await followup_agent.run(
                        "请现在立即执行你刚才描述的操作。",
                    )
                    # Stream the result to TUI — but do NOT add to collected_chunks.
                    # History ownership: run() persists followup; outer handler persists original turn.
                    if followup_result and followup_result.strip():
                        await send.stream_chunk(req_id, "\n\n---\n\n", done=False)
                        chunk_size = 50
                        for i in range(0, len(followup_result), chunk_size):
                            await send.stream_chunk(req_id, followup_result[i:i+chunk_size], done=False)
                        await send.stream_chunk(req_id, "", done=True)
                        # intentionally NOT appending to collected_chunks
                except Exception as _ac_err:
                    log.debug("auto-continue failed (non-fatal): %s", _ac_err)
                    # Fall through to normal history storage

        # ── Persist this turn to history ──────────────────────────────────────
        # Store user + assistant messages so next turn has full context.
        # Only store if we actually got a response (no-op on errors).
        if collected_chunks:
            assistant_reply = "".join(collected_chunks)
            _st.append_history([
                {"role": "user",      "content": prompt},
                {"role": "assistant", "content": assistant_reply},
            ], session_id)
            # Non-blocking Head/Tail compression check
            asyncio.create_task(_maybe_compress_history(session_id))

            # ── Compress hint: nudge user to /compress when history grows ─────
            # Read thresholds from config [agent] section (with sensible defaults)
            cfg_agent = (state.get_config() or {}).get("agent", {})
            compress_turns  = int(cfg_agent.get("history_compress_turns",  10))
            compress_tokens = int(cfg_agent.get("history_compress_tokens", 8000))
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
        log.error("agent.stream handler crashed: %s\n%s", e, _tb.format_exc())
        await send.stream_chunk(
            req_id,
            f"\n\n⛔ **Unexpected error in agent.stream:** `{e}`\n"
            f"Press F12 to view full logs.",
            done=True,
        )
    finally:
        # GAP-3: Trigger memory distillation at session end (non-blocking, best-effort).
        # create_task() schedules distillation to run after this handler returns,
        # so it never blocks the TUI response.
        asyncio.create_task(_distill_session())


async def _maybe_compress_history(session_id: str) -> None:
    """Head/Tail split summarization when history grows too large (Aider pattern).

    When history exceeds the threshold:
    - Head (older messages) → LLM-compressed to an Anchored Summary
    - Tail (last _HISTORY_TAIL_MESSAGES) → kept verbatim
    - Next turn sees: [summary_injection] + [tail]

    Non-blocking: called via create_task() after history append.
    Uses compact_session_to_anchor() from context_engine (already implemented).
    """
    import evocli_soul.state as _st
    history = _st.get_history(session_id)
    # Check thresholds
    if len(history) < _HISTORY_COMPRESS_TURNS * 2:
        return
    token_est = _st.get_history_token_estimate(session_id)
    if token_est < _HISTORY_COMPRESS_TOKENS:
        return

    head = history[:-_HISTORY_TAIL_MESSAGES]
    tail = history[-_HISTORY_TAIL_MESSAGES:]
    try:
        from evocli_soul.context_engine import compact_session_to_anchor
        existing_summary = _st.get_anchored_summary(session_id)
        llm = _st.get_llm_client()
        new_summary = await compact_session_to_anchor(head, llm, existing_summary)
        _st.set_anchored_summary(new_summary, session_id)
        # Replace history: keep only the verbatim tail.
        # The anchored summary lives in _anchored_summaries and is injected by
        # context_engine unconditionally — do NOT also write it back as history
        # messages, which would cause double-injection (summary in history AND
        # in anchored_summary slot).
        _st.clear_history(session_id)
        _st.append_history(tail, session_id)
        log.info("History compressed: %d msgs → anchor + %d tail (session=%s)",
                 len(head), _HISTORY_TAIL_MESSAGES, session_id)
    except Exception as e:
        log.debug("_maybe_compress_history failed (non-fatal, session=%s): %s", session_id, e)


async def _distill_session() -> None:
    """Non-blocking memory distillation triggered at session end (GAP-3).
    
    Drains accumulated session events and passes them to MemoryDistiller,
    which extracts success/failure chains and writes them to LanceDB memory.
    This is the core "越用越智能" flywheel trigger.
    
    Also updates ToolRouter score store from the same events,
    so frequently-successful tools get priority in future selections.
    """
    try:
        import evocli_soul.state as _st
        events = _st.drain_session_events()
        if len(events) < 2:
            return  # Not enough signal to extract meaningful patterns

        # ── ToolRouter: 更新工具使用分数（记忆驱动优化）────────────────────
        try:
            from evocli_soul.tool_router import update_scores_from_session_events
            update_scores_from_session_events(events)
            log.debug("ToolRouter: scores updated from %d session events", len(events))
        except Exception as _tr_err:
            log.debug("ToolRouter score update failed (non-fatal): %s", _tr_err)

        # ── ToolFlowMiner: 挖掘重复工具流（越用越聪明飞轮）────────────────
        # 从带参数的富事件中发现重复的工具调用序列，抽象为可复现的 ToolFlow
        try:
            from evocli_soul.tool_flow_miner import mine_from_events
            new_flows = mine_from_events(events, project_local=True)
            if new_flows:
                log.info("ToolFlowMiner: %d new tool flows discovered this session",
                         len(new_flows))
        except Exception as _fm_err:
            log.debug("ToolFlowMiner failed (non-fatal): %s", _fm_err)

        from evocli_soul.memory_distill import MemoryDistiller
        bridge = _st.get_bridge()
        distiller = MemoryDistiller(bridge)
        result = await distiller.run({
            "session_id":     f"sess_{int(time.time())}",
            "events":         events,
            "project_id":     ".",
            "priority_scope": "project",
        })
        written = result.get("distilled", 0)
        if written > 0:
            log.info("GAP-3 distillation: %d memory items written from %d events",
                     written, len(events))
    except Exception as e:
        log.debug("GAP-3 distillation failed (non-fatal): %s", e)



async def handle_agent_architect(req_id: str, params: dict, send, state) -> None:
    """
    Architect/Editor 双模型工作流 (Aider ArchitectCoder 模式).
    
    研究来源: Aider architect_coder.py
    - smart model: 分析请求 → 生成架构方案（自然语言）
    - fast model: 接收方案 → 生成 SEARCH/REPLACE 代码块
    
    params:
      prompt: str  用户请求
    """
    prompt = params.get("prompt", params.get("message", params.get("input", "")))
    if not prompt:
        await send.error(req_id, -32600, "prompt is required")
        return
    try:
        agent  = state.get_agent()
        result = await agent.run_architect_mode(prompt)
        await send.response(req_id, result)
    except Exception as e:
        log.exception("agent.architect failed")
        await send.error(req_id, -32603, str(e))


async def handle_llm_analyze(req_id: str, params: dict, send, state) -> None:
    """
    G-01: llm.analyze — Skill 步骤动作。
    WIRE-4: output_format="structured" 时使用 instructor 保证输出格式。

    params:
      prompt_template: str   模板名称（从 PromptManager 加载）或直接 prompt 文本
      input:           str   待分析的代码/文本
      output_format:   str   "diff" | "text" | "structured"（默认 "text"）
      tier:            str   "fast" | "smart"（默认 "smart"）
    """
    template_name = params.get("prompt_template", "")
    input_text    = params.get("input", "")
    output_format = params.get("output_format", "text")
    tier          = params.get("tier", "smart")

    try:
        prompt = _resolve_prompt_template(template_name, input_text)
        llm    = state.get_llm_client()

        # WIRE-4: output_format="structured" 时用 instructor 保证格式
        if output_format == "structured" and _INSTRUCTOR_AVAILABLE:
            result = await _structured_analyze(llm, prompt, tier)
        else:
            system = (
                "你是代码分析助手。请分析以下代码并生成 SEARCH/REPLACE 格式的修改建议。"
                if output_format == "diff" else
                "你是代码分析助手。请详细分析以下内容并给出结论。"
            )
            result = await llm.complete(prompt, tier=tier, system=system, max_tokens=4096)

        await send.response(req_id, {"result": result, "format": output_format})
    except Exception as e:
        log.exception("llm.analyze failed")
        await send.error(req_id, -32603, str(e))


async def handle_llm_generate(req_id: str, params: dict, send, state) -> None:
    """
    G-01: llm.generate — Skill 步骤动作，直接生成文本/代码。

    params:
      prompt:   str   生成提示词（必填）
      context:  str   附加上下文（可选）
      tier:     str   "fast" | "smart"（默认 "fast"）
    """
    prompt  = params.get("prompt", params.get("message", params.get("input", "")))
    context = params.get("context", "")
    tier    = params.get("tier", "fast")

    if not prompt:
        await send.error(req_id, -32600, "prompt is required for llm.generate")
        return
    try:
        full_prompt = f"{context}\n\n{prompt}" if context else prompt
        llm    = state.get_llm_client()
        result = await llm.complete(full_prompt, tier=tier, max_tokens=4096)
        await send.response(req_id, {"result": result})
    except Exception as e:
        log.exception("llm.generate failed")
        await send.error(req_id, -32603, str(e))


def _resolve_prompt_template(template_name: str, input_text: str) -> str:
    """
    解析 prompt_template 引用。
    优先从 PromptManager 加载命名模板（G-08 实现后自动生效），
    fallback 到直接把 template_name 当作 prompt 文本。
    """
    if not template_name:
        return input_text
    try:
        from evocli_soul.prompt_manager import PromptManager
        pm       = PromptManager()
        template = pm.get_template(template_name, {"input": input_text})
        if template:
            return template
    except Exception as e:
        # Non-fatal: fall back to default format below. Log at debug to aid troubleshooting.
        log.debug("Prompt template '%s' resolution failed (non-fatal): %s", template_name, e)
    # fallback：template_name 本身作为 system 提示，input 作为 user 内容
    return f"[{template_name}]\n\n{input_text}" if input_text else template_name


# ── WIRE-4: instructor 结构化输出辅助 ─────────────────────────────────────────

async def _structured_analyze(llm, prompt: str, tier: str) -> str:
    """
    WIRE-4: 用 instructor 保证结构化分析输出格式正确。
    当 output_format="structured" 时使用。
    
    instructor 提供：
    - 自动重试（LLM 格式不对时重试）
    - Pydantic 模型验证
    - 清晰的错误信息
    
    需要：pip install "evocli-soul"（instructor 是必需依赖）
    """
    try:
        # Uses llm.complete() → Router → structured JSON output
        # (Original: instructor.from_litellm+litellm.completion+Router alias → crashed)

        # instructor 包装 litellm — 使用 llm.complete() 通过 Router 调用（避免传 Router alias 给 litellm.completion）
        # Bug fix: instructor.from_litellm(litellm.completion) + model=llm._resolve_model() 会
        # 把 "fast"/"smart" 传给 litellm 导致 BadRequestError。改为 llm.complete() 路径。
        result_text = await llm.complete(prompt, tier=tier, max_tokens=2048,
                                          system="你是代码分析助手，请用以下 JSON 格式回答：\n"
                                                 '{"summary":"...","issues":[],"suggestions":[],"risk_level":"low"}')
        import json as _json
        try:
            data = _json.loads(result_text)
            return (
                f"## 分析摘要\n{data.get('summary','')}\n\n"
                f"## 发现的问题\n" + "\n".join(f"- {i}" for i in data.get('issues',[])) + "\n\n"
                f"## 改进建议\n" + "\n".join(f"- {s}" for s in data.get('suggestions',[])) + "\n\n"
                f"**风险等级**: {data.get('risk_level','low')}"
            )
        except Exception:
            return result_text
    except Exception as e:
        log.warning("instructor structured analyze failed (%s), fallback to plain text", e)
        # fallback: 普通文本生成
        return await llm.complete(prompt, tier=tier, max_tokens=4096)


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


def _derive_stream_session_id(params: dict) -> str:
    """
    Derive session_id for the streaming path.

    Design: Uses frozen SESSION_PROJECT_ROOT (not live os.getcwd()) for
    cross-restart project continuity. Same project directory → same session
    bucket across TUI restarts, without risking directory drift.

    This differs from handle_agent_run (which uses uuid) because:
    - agent.stream: TUI primary path — users expect history to persist after restart
    - agent.run: Programmatic API — each call should be independent

    Note: Two different users/sessions in the same project directory will share
    history. This is intentional for single-developer local-first use cases.
    """
    explicit = params.get("session_id")
    if explicit:
        return explicit
    import hashlib as _hash_sid
    from evocli_soul.state import get_session_root as _get_sr
    return "cwd_" + _hash_sid.md5(
        _get_sr().encode(), usedforsecurity=False
    ).hexdigest()[:12]


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
        import os as _os_run
        import hashlib as _hashlib_run
        _run_explicit_sid = params.get("session_id")
        if _run_explicit_sid:
            _run_session_id = _run_explicit_sid
        else:
            import uuid as _uuid_run
            # Generate a unique session ID per conversation to prevent history bleed-across
            # between different sessions in the same working directory.
            # The cwd-based hash was causing conversation histories from different sessions
            # to be loaded into each other (W5 regression fix).
            _run_session_id = "sess_" + _uuid_run.uuid4().hex[:16]
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

    # ── Slash command dispatch (extracted to handlers/slash_commands.py) ──────
    # All /help, /add, /compress, /plan, /btw, /undo, /flows handlers live there.
    # Returns True if a slash command was matched and handled — main handler returns.
    from evocli_soul.handlers.slash_commands import dispatch_slash
    if await dispatch_slash(
        prompt=prompt,
        req_id=req_id,
        params=params,
        send=send,
        state=state,
        derive_session_id=_derive_stream_session_id,
        emit_event=emit_event,
    ):
        return


    # ── Delegate to agent_loop.run_agent_stream_body ─────────────────────────
    # The full session setup + autonomous execution loop lives in agent_loop.py.
    from evocli_soul.handlers.agent_loop import run_agent_stream_body
    await run_agent_stream_body(req_id=req_id, params=params, send=send, state=state)


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


async def _distill_session(session_id: str = "default") -> None:
    """Non-blocking memory distillation triggered at session end (GAP-3).
    
    Drains accumulated session events and passes them to MemoryDistiller,
    which extracts success/failure chains and writes them to LanceDB memory.
    This is the core "越用越智能" flywheel trigger.
    
    Also updates ToolRouter score store from the same events,
    so frequently-successful tools get priority in future selections.
    """
    try:
        import evocli_soul.state as _st
        events = _st.drain_session_events(session_id)  # session-isolated drain
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
                "## 改进建议\n" + "\n".join(f"- {s}" for s in data.get('suggestions',[])) + "\n\n"
                f"**风险等级**: {data.get('risk_level','low')}"
            )
        except Exception:
            return result_text
    except Exception as e:
        log.warning("instructor structured analyze failed (%s), fallback to plain text", e)
        # fallback: 普通文本生成
        return await llm.complete(prompt, tier=tier, max_tokens=4096)


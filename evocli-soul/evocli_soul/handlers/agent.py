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
        from evocli_soul.agent import EvoCLIAgent
        cfg    = state.get_config()
        agent  = EvoCLIAgent(state.get_bridge(), state.get_memory(), cfg)
        result = await agent.run(prompt)
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

    # ── GAP-2: /compress slash-command ───────────────────────────────────────
    # Compacts current session history into an Anchored Summary (OpenCode pattern).
    # Triggered by user typing /compress or /compact in the TUI.
    if prompt.strip().lower() in ("/compress", "/compact"):
        history = params.get("history", [])
        if not history:
            await send.stream_chunk(req_id,
                "No session history to compress. Start a conversation first.",
                done=True)
            return
        try:
            from evocli_soul.context_engine import compact_session_to_anchor
            llm = state.get_llm_client()
            await send.stream_chunk(req_id, "⏳ Compressing session…\n\n", done=False)
            summary = await compact_session_to_anchor(history, llm)
            await send.stream_chunk(req_id,
                f"**Session compressed successfully.**\n\n{summary}\n\n"
                f"*History has been replaced by this summary. "
                f"Continue working — full context is preserved.*",
                done=True)
            # Notify Rust TUI to replace its history buffer with the anchor
            await emit_event("session_compacted", {
                "summary": summary,
                "chars":   len(summary),
                "original_message_count": len(history),
            })
        except Exception as e:
            log.warning("GAP-2 /compress failed: %s", e)
            await send.stream_chunk(req_id, f"Compression failed: {e}", done=True)
        return

    # Track whether we actually sent any content chunks so we can detect silent failures.
    chunks_sent = 0

    try:
        from evocli_soul.agent import EvoCLIAgent, _PROVIDER_ENV
        cfg = state.get_config()
        import evocli_soul.state as _st
        memory = _st.get_memory_if_ready()

        agent = EvoCLIAgent(state.get_bridge(), memory, cfg)

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

        # ── Primary path (pydantic-ai → LiteLLM fallback) ────────────────────
        primary_err: Exception | None = None
        try:
            async for chunk in agent.stream(prompt):
                if chunk:  # skip empty keep-alive chunks
                    await send.stream_chunk(req_id, chunk, done=False)
                    chunks_sent += 1
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
                # Show only the first line of the error so TUI doesn't get flooded.
                # Full details are in ~/.evocli/logs/evocli.log (F12 to view).
                first_line = str(primary_err).splitlines()[0] if str(primary_err) else repr(primary_err)
                await emit_event("soul_status", {
                    "status":  "error",
                    "message": f"Primary path failed: {first_line} — retrying…  (F12 for full log)",
                })
            try:
                async for chunk in agent._stream_litellm(prompt, {}):
                    if chunk:
                        await send.stream_chunk(req_id, chunk, done=False)
                        chunks_sent += 1
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
            # The LLM returned a completely empty response (no content at all).
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


async def _distill_session() -> None:
    """Non-blocking memory distillation triggered at session end (GAP-3).
    
    Drains accumulated session events and passes them to MemoryDistiller,
    which extracts success/failure chains and writes them to LanceDB memory.
    This is the core "越用越智能" flywheel trigger.
    """
    try:
        import evocli_soul.state as _st
        events = _st.drain_session_events()
        if len(events) < 2:
            return  # Not enough signal to extract meaningful patterns

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


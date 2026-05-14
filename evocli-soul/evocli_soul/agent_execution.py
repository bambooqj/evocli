"""
agent_execution.py — EvoCLIAgent execution methods mixin

Extracted from agent.py to keep the core agent class focused on
initialization and tool setup. This mixin provides:
  - _build_context / _inject_context: context assembly
  - run / stream: primary execution paths
  - run_architect_mode: Aider Architect/Editor mode
  - _run_litellm / _stream_litellm: LiteLLM fallback with tool loop
  - _detect_test_cmd: auto-detect test runner
  - _emit_fallback_warning_once: one-time warning

Usage:
  from evocli_soul.agent_execution import AgentExecutionMixin
  class EvoCLIAgent(AgentExecutionMixin): ...
"""
from __future__ import annotations
import logging
from typing import AsyncGenerator

log = logging.getLogger("evocli.agent")


class AgentExecutionMixin:
    """Mixin providing execution methods for EvoCLIAgent."""

    async def run(self, user_input: str, context_params: dict | None = None) -> str:
        """Run agent with context injection."""
        await self._emit_fallback_warning_once()
        # ── ToolRouter: 选工具（prepare hook 会读取 _selected_tool_names）──────
        self._select_tools_for_request(user_input)
        # Load session history for multi-turn continuity.
        # It is passed directly to _run_litellm's messages array (prior_history).
        # We do NOT pass it to _build_context to avoid embedding it twice —
        # once in user_context and again in the LiteLLM messages array.
        # Pydantic-AI path gets history via full_input (user_context).
        try:
            import evocli_soul.state as _st_run
            _run_history = _st_run.get_history(self._session_id)
        except Exception as _e:
            log.debug("context inject skipped: %s", _e)
            _run_history = []
        # Build context WITHOUT history (anchored_summary still loads via session_id)
        ctx = await self._build_context(
            user_input, context_params,
            history=_run_history,   # history goes into user_context for pydantic-ai
            session_id=self._session_id,
        )
        full_input = await self._inject_context(user_input, ctx)
    
        if self._agent is not None:
            try:
                result = await self._agent.run(full_input)
                reply = str(getattr(result, "output", None) or getattr(result, "data", "") or "")
                # Emit cost_update with real token counts from pydantic-ai RunResult.usage()
                try:
                    from evocli_soul.rpc import emit_event as _emit_pai_cost
                    _usage = result.usage() if callable(getattr(result, "usage", None)) else None
                    if _usage is not None:
                        _in  = int(getattr(_usage, "request_tokens",  0) or 0)
                        _out = int(getattr(_usage, "response_tokens", 0) or 0)
                        if _in > 0 or _out > 0:
                            await _emit_pai_cost("cost_update", {
                                "input_tokens":  _in,
                                "output_tokens": _out,
                                "cost_usd":      0.0,  # pydantic-ai doesn't expose cost directly
                            })
                            log.debug("pydantic-ai run usage: in=%d out=%d", _in, _out)
                except Exception as _ue:
                    log.debug("pydantic-ai run cost_update failed (non-fatal): %s", _ue)
                if reply:
                    try:
                        import evocli_soul.state as _st_persist
                        _st_persist.append_history([
                            {"role": "user",      "content": user_input},
                            {"role": "assistant", "content": reply},
                        ], self._session_id)
                    except Exception:
                        pass
                return reply
            except Exception as e:
                log.warning("Pydantic AI run failed (%s), falling back", e)
    
        # LiteLLM fallback: history goes into the messages array via prior_history.
        # user_context section of full_input already has context (files, diff, summary)
        # but NOT raw history turns — those come via prior_history in the messages array.
        # Pass history=[] to avoid double-injecting into full_input above:
        # actually full_input already has history in user_context from _build_context above.
        # Use the existing full_input but pass prior_history=[] to _run_litellm to avoid
        # doubling history in the messages array.
        # The history is already in full_input's user_context section.
        litellm_reply = await self._run_litellm(full_input, ctx, prior_history=None)
        if litellm_reply:
            try:
                import evocli_soul.state as _st_persist2
                _st_persist2.append_history([
                    {"role": "user",      "content": user_input},
                    {"role": "assistant", "content": litellm_reply},
                ], self._session_id)
            except Exception:
                pass
        return litellm_reply
    
    async def run_architect_mode(
        self,
        user_input: str,
        context_params: dict | None = None,
    ) -> dict:
        """
        Architect/Editor dual-model workflow (Aider architect_coder.py pattern).
    
        研究来源: Aider ArchitectCoder
        - Architect (smart model): 分析请求 → 描述修改方案（自然语言，不生成代码）
        - Editor (fast model): 接收 Architect 方案 → 生成 SEARCH/REPLACE 代码块
    
        流程:
        1. smart model (GPT-4o/Claude-3-7-Sonnet) 分析上下文并描述架构方案
        2. fast model (GPT-4o-mini/Haiku) 将方案转换为具体 SEARCH/REPLACE 编辑
        3. 自动应用所有编辑块到文件系统
    
        Returns: {"architect_plan": str, "editor_output": str, "apply_results": list}
        """
        from evocli_soul.llm_client import LLMClient
        llm = LLMClient(self.config)
    
        ctx        = await self._build_context(user_input, context_params)
        full_input = await self._inject_context(user_input, ctx)
    
        # ── Step 1: Architect (smart model) ──────────────────────────
        ARCHITECT_SYSTEM = (
            "You are a Senior Software Architect. Analyze the codebase and the user's request. "
            "Describe clearly and concisely HOW to implement the changes — which files to modify, "
            "what logic to change, and why. "
            "DO NOT write code or SEARCH/REPLACE blocks yourself. "
            "The editor engineer will take your description and implement the actual edits. "
            "Be specific about file paths, function names, and what exactly changes."
        )
        log.info("Architect/Editor: calling smart model for plan...")
        architect_plan = await llm.complete_for_task(
            "architect",
            full_input,
            system=ARCHITECT_SYSTEM,
        )
        log.info("Architect plan generated: %d chars", len(architect_plan))
    
        # ── Step 2: Editor (fast model) ──────────────────────────────
        EDITOR_SYSTEM = (
            "You are an expert code editor. "
            "Given the architectural plan below and the original user request, "
            "generate the precise SEARCH/REPLACE blocks to implement the changes. "
            "Use EXACTLY this format for each edit:\n\n"
            "path/to/file.ext\n"
            "<<<<<<< SEARCH\n[exact existing code]\n=======\n[new code]\n>>>>>>> REPLACE\n\n"
            "Make sure the SEARCH block is an EXACT match of existing file content."
        )
        editor_prompt = (
            f"## Original Request\n{user_input}\n\n"
            f"## Architectural Plan\n{architect_plan}\n\n"
            "Now generate the SEARCH/REPLACE blocks to implement this plan."
        )
        log.info("Architect/Editor: calling fast model for edits...")
    
        # ── Provide file content to Editor (Bug 5 fix: Editor needs to see files)
        # Aider passes chat_files to editor so it can generate accurate SEARCH blocks
        chat_files_context = ""
        if context_params and context_params.get("current_file"):
            try:
                cf_path    = context_params["current_file"]
                cf_content = await self.bridge.call("fs.read", {"path": cf_path})
                if isinstance(cf_content, str):
                    chat_files_context = f"\n\n## Current File: {cf_path}\n```\n{cf_content[:3000]}\n```"
            except Exception as e:
                # Log at debug: Architect/Editor mode will proceed without file context.
                # Silent failure here causes the editor to make changes without seeing current file state.
                log.debug("run_architect_mode: failed to read current file %s: %s",
                          context_params.get("current_file"), e)
    
        editor_output = await llm.complete_for_task(
            "editor",
            editor_prompt + chat_files_context,
            system=EDITOR_SYSTEM,
        )
    
        # ── Step 3: Apply all blocks with git checkpoint (Aider atomicity pattern) ──
        # Bug fix: add git checkpoint before edits, rollback on failure (matches handlers/edit.py)
        from evocli_soul.edit_engine import parse_search_replace_blocks, apply_search_replace, AmbiguousSearchError
        blocks = parse_search_replace_blocks(editor_output)
        checkpoint_ref = None
        if blocks:
            try:
                snap = await self.bridge.call("git.snapshot", {})
                checkpoint_ref = snap.get("stash_ref") if isinstance(snap, dict) else None
                log.debug("Architect/Editor: git checkpoint created (%s)", checkpoint_ref)
            except Exception as e:
                log.debug("Architect/Editor: no git checkpoint (non-fatal): %s", e)
    
        apply_results = []
        failed = False
        for block in blocks:
            filename = block.get("file") or ""
            if not filename:
                apply_results.append({"file": "(unknown)", "ok": False, "error": "no file"})
                failed = True
                continue
            try:
                content = await self.bridge.call("fs.read", {"path": filename})
                if not isinstance(content, str):
                    apply_results.append({"file": filename, "ok": False, "error": "read failed"})
                    failed = True
                    continue
                try:
                    new_content, strategy = apply_search_replace(content, block["search"], block["replace"])
                    await self.bridge.call("fs.write", {"path": filename, "content": new_content})
                    apply_results.append({"file": filename, "ok": True, "strategy": strategy})
                except AmbiguousSearchError as amb:
                    apply_results.append({
                        "file": filename, "ok": False, "strategy": "ambiguous",
                        "ambiguous": True, "match_count": amb.match_count,
                        "match_lines": amb.match_line_numbers,
                        "error": amb.to_ai_feedback(),
                    })
                    failed = True
            except ValueError as e:
                apply_results.append({"file": filename, "ok": False, "error": str(e)})
                failed = True
            except Exception as e:
                apply_results.append({"file": filename, "ok": False, "error": str(e)})
                failed = True
    
        # Rollback on failure (Aider: git reset --hard)
        if failed and checkpoint_ref:
            try:
                await self.bridge.call("git.restore", {"stash_ref": checkpoint_ref})
                log.warning("Architect/Editor: rolled back due to %d failures", sum(1 for r in apply_results if not r.get("ok")))
            except Exception as e:
                log.error("Architect/Editor: rollback failed: %s", e)
    
        return {
            "architect_plan":  architect_plan,
            "editor_output":   editor_output,
            "apply_results":   apply_results,
            "applied":         sum(1 for r in apply_results if r.get("ok") and not failed),
            "rolled_back":     failed and checkpoint_ref is not None,
        }
    
    async def _emit_fallback_warning_once(self) -> None:
        """Emit a TUI warning if pydantic-ai failed to initialize (once per agent instance)."""
        if self._fallback_reason:
            try:
                from evocli_soul.rpc import emit_event
                await emit_event("soul_status", {
                    "status":  "ready",
                    "message": f"⚠️ Using LiteLLM fallback (tool calling may be limited): {self._fallback_reason}",
                })
            except Exception:
                pass
            self._fallback_reason = None  # emit only once
    
    async def stream(self, user_input: str, context_params: dict | None = None,
                     prior_history: list[dict] | None = None,
                     session_id: str = "default") -> AsyncGenerator[str, None]:
        """Stream agent response with multi-turn history support."""
        # Import once; used for progress events so TUI shows real-time stage names
        # instead of a frozen "Connecting…" spinner. OpenCode/Continue.dev pattern.
        from evocli_soul.rpc import emit_event as _emit_prog
    
        await self._emit_fallback_warning_once()
        # ── ToolRouter: 选工具（prepare hook 会读取 _selected_tool_names）──────
        self._select_tools_for_request(user_input)
        import asyncio
        # Read timeout from config [agent] section (default 20s)
        _ctx_timeout = float((self.config or {}).get("agent", {}).get("context_build_timeout_s", 20))
        # ── Stage 1: context build — emit progress so TUI shows "Loading context…"
        # instead of a frozen spinner. The soul_status event updates app.thinking_label
        # in the Rust TUI (app.rs) which is displayed in the input bar border.
        await _emit_prog("soul_status", {"status": "loading", "message": "Loading context…"})
        # History strategy: embed prior_history in user_context via _build_context.
        # This makes it available to ALL downstream paths (pydantic-ai and LiteLLM)
        # as part of the user message. We do NOT also pass message_history to
        # pydantic-ai or extend messages arrays — history appears exactly once.
        try:
            ctx = await asyncio.wait_for(
                self._build_context(user_input, context_params,
                                    history=prior_history, session_id=session_id),
                timeout=_ctx_timeout,
            )
        except asyncio.TimeoutError:
            log.debug("_build_context timed out (%.0fs) — using minimal context", _ctx_timeout)
            ctx = {}
        # ── Stage 2: LLM call — update progress label before blocking network I/O
        await _emit_prog("soul_status", {"status": "loading", "message": "Calling LLM…"})
        full_input = await self._inject_context(user_input, ctx)
    
        if self._agent is not None:
            try:
                import asyncio as _pai_asyncio

                # pydantic-ai run_stream has NO built-in timeout.
                # When a tool call fails, pydantic-ai can hang indefinitely
                # waiting for internal state that never resolves.
                # Fix: wrap streaming in asyncio.timeout so we can fallback to LiteLLM.
                _stream_timeout = float(
                    (self.config or {}).get("agent", {}).get("stream_timeout_s", 120)
                )
                _first_chunk_deadline = float(
                    (self.config or {}).get("agent", {}).get("first_chunk_timeout_s", 30)
                )

                async def _run_pai_stream():
                    """Run pydantic-ai stream and yield chunks. Raises on hang."""
                    chunks_yielded = 0
                    async with self._agent.run_stream(full_input) as result:
                        async for chunk in result.stream_text(delta=True):
                            yield chunk
                            chunks_yielded += 1
                    # Emit cost_update after stream ends
                    try:
                        from evocli_soul.rpc import emit_event as _emit_pai_cost
                        _usage = result.usage() if callable(getattr(result, "usage", None)) else None
                        if _usage is not None:
                            _in  = int(getattr(_usage, "request_tokens",  0) or 0)
                            _out = int(getattr(_usage, "response_tokens", 0) or 0)
                            if _in > 0 or _out > 0:
                                await _emit_pai_cost("cost_update", {
                                    "input_tokens": _in, "output_tokens": _out, "cost_usd": 0.0,
                                })
                    except Exception as _ue:
                        log.debug("pydantic-ai stream cost_update failed (non-fatal): %s", _ue)

                # Use asyncio.timeout (Python 3.11+) or asyncio.wait_for wrapper
                try:
                    async with _pai_asyncio.timeout(_stream_timeout):
                        first_chunk = True
                        _first_chunk_task = None
                        async for chunk in _run_pai_stream():
                            if first_chunk:
                                first_chunk = False
                            yield chunk
                    return
                except _pai_asyncio.TimeoutError:
                    log.warning(
                        "Pydantic AI stream timed out after %.0fs — falling back to LiteLLM",
                        _stream_timeout,
                    )
            except AttributeError:
                # asyncio.timeout not available (Python < 3.11) — use wait_for pattern
                try:
                    async with self._agent.run_stream(full_input) as result:
                        async for chunk in result.stream_text(delta=True):
                            yield chunk
                    try:
                        from evocli_soul.rpc import emit_event as _emit_pai_stream_cost
                        _usage = result.usage() if callable(getattr(result, "usage", None)) else None
                        if _usage is not None:
                            _in  = int(getattr(_usage, "request_tokens",  0) or 0)
                            _out = int(getattr(_usage, "response_tokens", 0) or 0)
                            if _in > 0 or _out > 0:
                                await _emit_pai_stream_cost("cost_update", {
                                    "input_tokens":  _in,
                                    "output_tokens": _out,
                                    "cost_usd":      0.0,
                                })
                    except Exception as _ue:
                        log.debug("pydantic-ai stream cost_update failed (non-fatal): %s", _ue)
                    return
                except Exception as e:
                    log.warning("Pydantic AI stream failed (%s), falling back", e)
            except Exception as e:
                log.warning("Pydantic AI stream failed (%s), falling back", e)
    
        # Fallback: full_input already has history embedded via _inject_context;
        # _stream_litellm uses system+user message format (history not re-injected).
        async for chunk in self._stream_litellm(full_input, ctx, prior_history=None):
            yield chunk
    

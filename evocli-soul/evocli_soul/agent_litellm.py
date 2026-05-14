"""agent_litellm.py - LiteLLM fallback execution mixin
Extracted from agent_execution.py.
Single responsibility: _run_litellm tool-calling loop and _stream_litellm.
"""
from __future__ import annotations
import logging
from typing import AsyncGenerator
log = logging.getLogger('evocli.agent.litellm')

# Shared helpers — imported explicitly to avoid runtime NameError.
# Cannot import from agent.py (circular: agent.py imports this module's Mixin).
# Instead import from the canonical source locations:
from evocli_soul.agent_executor import _tool_display_name  # noqa: E402
from evocli_soul.default_prompts import build_system_prompt  # noqa: E402


class AgentLiteLLMMixin:
    """Mixin: LiteLLM fallback for EvoCLIAgent."""

    async def _run_litellm(self, user_input: str, ctx: dict,
                           prior_history: list[dict] | None = None) -> str:
        """Raw LiteLLM fallback with tool calling loop."""
        from evocli_soul.llm_client import LLMClient
        
        llm = LLMClient(self.config)
    
        # Fix: 使用 context_engine 构建的完整 system_prompt（含约束、记忆、RepoMap）。
        # 原版将 system_prompt 截断到 500 字符作为 "goal" 参数，丢失了大量上下文。
        # 与 _stream_litellm 的 Fix 3 逻辑一致，确保所有 LiteLLM 路径行为统一。
        if ctx and ctx.get("system_prompt"):
            system = ctx["system_prompt"]
        else:
            constraints = "（无）"
            if self.memory:
                try:
                    c = self.memory.get_constraints()
                    if c:
                        constraints = "\n".join(f"- {x}" for x in c)
                except Exception as e:
                    log.debug("_run_litellm: failed to load constraints: %s", e)
            system = build_system_prompt(
                constraints=constraints,
                goal=user_input[:200],
                read_only=self.read_only,
                compact=False,
            )
        conversation = [
            {"role": "system", "content": system},
        ]
        # Do NOT inject prior_history into the messages array — history is already
        # embedded in user_input (enriched by _inject_context from _build_context).
        # Injecting prior_history here would double the history content.
        # (prior_history param is kept for API compatibility but intentionally unused.)
        conversation.append({"role": "user", "content": user_input})
        
        tools = self._build_tool_definitions()

        # ── Loop limits — read from config_defaults (no magic numbers) ───────
        from evocli_soul.config_defaults import cfg_int, cfg_float
        _agent_cfg      = (self.config or {}).get("agent", {}) if self.config else {}
        MAX_REFLECTIONS = int(_agent_cfg.get("max_reflections", cfg_int("agent.max_reflections")))
        MAX_TOOL_CALLS  = int(_agent_cfg.get("max_tool_calls",  cfg_int("agent.max_tool_calls")))

        _REFLECTION_TRIGGERS = frozenset({
            "fs_lint_file",
            "test_and_capture",
            "fs_apply_search_replace",
        })
        reflection_count = 0

        # ── Step counting + approaching-limit reminder ────────────────────────
        step_count = 0
        _warn_at   = max(1, MAX_TOOL_CALLS - 3)

        for _ in range(MAX_TOOL_CALLS):
            step_count += 1

            try:
                from evocli_soul.rpc import emit_event as _step_ev
                await _step_ev("soul_status", {
                    "status":  "loading",
                    "message": f"步骤 {step_count}/{MAX_TOOL_CALLS}…",
                })
            except Exception:  # noqa: BLE001 — non-fatal event/metric, never block execution
                pass

            if step_count == _warn_at:
                conversation.append({
                    "role": "user",
                    "content": (
                        f"[系统提示] 你已使用 {step_count}/{MAX_TOOL_CALLS} 步工具调用，"
                        f"还剩约 {MAX_TOOL_CALLS - step_count} 步。"
                        f"请优先完成最关键的任务并给出最终总结，避免被强制中断。"
                        f"若有未完成项，请在回复中明确列出 Next Steps。"
                    ),
                })

            tier_alias = llm._resolve_model("auto")
            _task_params = llm.get_task_params("agent")
            call_kwargs: dict = {
                "model":       tier_alias,
                "messages":    conversation,
                "tools":       tools,
                "max_tokens":  _task_params.get("max_tokens",  cfg_int("llm.max_tokens")),
                "temperature": _task_params.get("temperature", cfg_float("llm.temperature")),
            }
            response = await llm._router.acompletion(**call_kwargs)
            msg = response.choices[0].message
            conversation.append({"role": "assistant", "content": msg.content or "", "tool_calls": msg.tool_calls or None})
    
            # Emit cost_update for every LLM call in the tool loop
            try:
                from evocli_soul.rpc import emit_event as _ev
                import litellm as _ll
                usage = getattr(response, 'usage', None) or {}
                in_tok  = int(getattr(usage, "prompt_tokens",     0))
                out_tok = int(getattr(usage, "completion_tokens", 0))
                if in_tok > 0 or out_tok > 0:
                    try:
                        cost_usd = _ll.completion_cost(completion_response=response)
                    except Exception:
                        cost_usd = 0.0
                    await _ev("cost_update", {
                        "input_tokens":  in_tok,
                        "output_tokens": out_tok,
                        "cost_usd":      cost_usd or 0.0,
                    })
            except Exception:
                pass  # non-fatal
    
            if not msg.tool_calls:
                return msg.content or ""
    
            # ── Parallel-safe tool classification (Claude Code / Cline pattern) ──────
            # When LLM returns multiple tool calls in one response AND all are read-only,
            # execute them simultaneously with asyncio.gather — dramatically faster for
            # "read 5 files" or "search across 3 modules" style requests.
            # Write tools must be sequential to preserve file system consistency.
            _PARALLEL_SAFE_TOOLS = frozenset({
                "fs_read", "fs_read_range", "fs_read_symbol",
                "shell_grep", "shell_ls", "shell_find", "shell_cat",
                "shell_head", "shell_tail", "shell_wc",
                "symbol_lookup", "symbol_usages", "symbol_variants",
                "code_intel_list_symbols", "code_intel_incoming_calls",
                "code_intel_outgoing_calls", "code_semantic_search",
                "code_hybrid_search", "code_blast_radius", "code_symbol_context",
                "code_communities", "memory_recall", "todo_read",
                "git_status", "git_diff", "search_code", "diff_parse_stats",
            })
    
            # Observation masking: truncate very long tool results (Cline pattern).
            # Preserves head + tail, drops the middle — model sees key context without bloat.
            _OBS_MAX = cfg_int("agent.observation_max_chars")
    
            def _mask_obs(result: str, tool: str) -> str:
                if not isinstance(result, str) or len(result) <= _OBS_MAX:
                    return result if isinstance(result, str) else str(result)
                head = result[:_OBS_MAX // 2]
                tail = result[-(min(800, _OBS_MAX // 8)):]
                omitted = len(result) - len(head) - len(tail)
                hint = "use fs_read_range for specific line ranges" if "fs_read" in tool else "result truncated"
                return f"{head}\n\n...[{omitted:,} chars omitted — {hint}]...\n\n{tail}"
    
            import json as _json
            # Pre-parse all arguments
            _parsed_calls = []
            for tc in msg.tool_calls:
                try:
                    targs = _json.loads(tc.function.arguments)
                except Exception as _e:
                    log.warning("_run_litellm: JSON decode failed for tool '%s' args=%r: %s",
                                tc.function.name, tc.function.arguments[:200], _e)
                    targs = {}
                _parsed_calls.append((tc, targs))
    
            _all_parallel = (
                len(_parsed_calls) > 1 and
                all(tc.function.name in _PARALLEL_SAFE_TOOLS for tc, _ in _parsed_calls)
            )
    
            if _all_parallel:
                # ── Parallel execution ──────────────────────────────────────────
                _tool_names = [tc.function.name for tc, _ in _parsed_calls]
                try:
                    from evocli_soul.rpc import emit_event as _par_ev
                    await _par_ev("soul_status", {
                        "status":  "loading",
                        "message": f"⚡ 并行执行 {len(_parsed_calls)} 个工具: {', '.join(_tool_names[:3])}{'…' if len(_tool_names) > 3 else ''}",
                    })
                except Exception:  # noqa: BLE001 — non-fatal event/metric, never block execution
                    pass
    
                import asyncio as _par_asyncio
                # ── Per-tool timeout wrapper (prevents one hung tool stalling the batch) ──
                # Oracle finding: asyncio.gather waits for ALL tools — one hung read blocks all.
                # Fix: wrap each _execute_tool call with asyncio.wait_for and per-tool timeout.
                _par_tool_timeout = cfg_float("shell.timeout_s")

                async def _execute_with_timeout(name: str, args: dict) -> str:
                    try:
                        return await _par_asyncio.wait_for(
                            self._execute_tool(name, args),
                            timeout=_par_tool_timeout,
                        )
                    except _par_asyncio.TimeoutError:
                        log.warning("Parallel tool %s timed out after %.0fs", name, _par_tool_timeout)
                        return f"Error: tool '{name}' timed out after {_par_tool_timeout:.0f}s"

                _par_results = await _par_asyncio.gather(
                    *[_execute_with_timeout(tc.function.name, targs) for tc, targs in _parsed_calls],
                    return_exceptions=True,
                )
                for (tc, _targs), _pres in zip(_parsed_calls, _par_results):
                    if isinstance(_pres, BaseException):
                        _pres = f"Error: {_pres}"
                    _pres = _mask_obs(str(_pres), tc.function.name)
                    conversation.append({"role": "tool", "tool_call_id": tc.id, "content": _pres})
                # Parallel reads rarely fail — reset circuit breaker
                try:
                    from evocli_soul.state import reset_tool_failure as _rf_par
                    _rf_par(self._session_id)
                except Exception:  # noqa: BLE001 — non-fatal event/metric, never block execution
                    pass
    
            else:
                # ── Sequential execution (writes + mixed batches) ──────────────
                for tc, targs in _parsed_calls:
                    # Emit progress so user sees which tool is running
                    try:
                        from evocli_soul.rpc import emit_event as _progress_ev
                        _tool_display = _tool_display_name(tc.function.name, targs)
                        await _progress_ev("soul_status", {
                            "status":  "loading",
                            "message": f"🔧 {_tool_display}",
                        })
                    except Exception:  # noqa: BLE001 — non-fatal event/metric, never block execution
                        pass
                    result = await self._execute_tool(tc.function.name, targs)
                    # Observation masking
                    result = _mask_obs(result, tc.function.name)
                    conversation.append({"role": "tool", "tool_call_id": tc.id, "content": result})
    
                    # ── Circuit breaker (inside loop — per-tool failure tracking) ──
                    try:
                        from evocli_soul.state import (
                            increment_tool_failure as _incf, reset_tool_failure as _resetf,
                        )
                        _is_error = (
                            isinstance(result, str) and (
                                result.startswith("Error:") or
                                (result.startswith("{") and '"ok": false' in result and '"error"' in result)
                            )
                        )
                        if _is_error:
                            _fail_count = _incf(self._session_id)
                            _cb_thresh  = cfg_int("agent.max_consecutive_failures")
                            if _fail_count >= _cb_thresh:
                                log.warning("Circuit breaker: %d consecutive failures (session %s)",
                                            _fail_count, self._session_id[:12])
                                conversation.append({
                                    "role": "user",
                                    "content": (
                                        f"[断路器] 连续 {_fail_count} 次工具调用失败。\n"
                                        f"请停止重试同一操作，改为：\n"
                                        f"1. 分析失败原因\n2. 尝试完全不同的方法\n"
                                        f"3. 如无法继续，调用 task_complete 报告阻塞原因\n"
                                        f"最近错误: {result[:200]}"
                                    ),
                                })
                                _resetf(self._session_id)
                        else:
                            _resetf(self._session_id)
                    except Exception:  # noqa: BLE001 — non-fatal event/metric, never block execution
                        pass
    
                    # ── Reflection loop (per-tool, sequential only) ──────────────
                    if tc.function.name in _REFLECTION_TRIGGERS and reflection_count < MAX_REFLECTIONS:
                        try:
                            reflection_msg = ""
                            try:
                                r_data = _json.loads(result)
                                rp = r_data.get("reflection_prompt", "")
                                is_failed = not r_data.get("ok", True)
                                is_ambiguous = r_data.get("ambiguous", False)
                                if rp and (is_failed or is_ambiguous):
                                    reflection_msg = rp
                            except (_json.JSONDecodeError, TypeError, AttributeError):
                                pass
                            if not reflection_msg and isinstance(result, str):
                                if result.startswith("✗") or "FAILED" in result or "You MUST fix" in result:
                                    reflection_msg = result[:600]
                            if reflection_msg:
                                reflection_count += 1
                                conversation.append({
                                    "role": "user",
                                    "content": f"[Auto-reflection {reflection_count}/{MAX_REFLECTIONS}] {reflection_msg}",
                                })
                                log.info("Reflection %d/%d triggered by %s",
                                         reflection_count, MAX_REFLECTIONS, tc.function.name)
                        except Exception:  # noqa: BLE001 — non-fatal event/metric, never block execution
                            pass
    
        # Tool iteration limit reached — give the user actionable context.
        # Check if tests/lint were still failing when we hit the limit.
        last_test_status = "unknown"
        for msg in reversed(conversation):
            c = str(msg.get("content", ""))
            if "FAILED" in c or "✗" in c or "test_failed" in c.lower():
                last_test_status = "failing"
                break
            if "✓" in c or "passed" in c.lower() or "All tests" in c:
                last_test_status = "passing"
                break
    
        log.warning("_run_litellm: max tool iterations (%d) reached. reflections=%d last_test=%s",
                    MAX_TOOL_CALLS, reflection_count, last_test_status)
    
        if last_test_status == "failing":
            return (
                f"⚠️ **Reached the tool call limit ({MAX_TOOL_CALLS} iterations) "
                f"but tests/checks are STILL FAILING.**\n\n"
                f"**Do not treat this as complete** — the code changes may be broken.\n\n"
                f"Next steps:\n"
                f"1. Run `{self._detect_test_cmd()}` manually to see current errors\n"
                f"2. Break the task into smaller steps and try again\n"
                f"3. Use `/compress` then describe the specific failing test\n\n"
                f"Reflection retries exhausted: {reflection_count}/{MAX_REFLECTIONS}"
            )
        return (
            f"⚠️ **Reached the maximum tool call limit ({MAX_TOOL_CALLS} iterations).**\n\n"
            f"This usually means the task is too complex for one invocation.\n"
            f"Try breaking it into smaller steps or using `/compress` to free context."
        )
    
    def _detect_test_cmd(self) -> str:
        """Guess the test command from project files (best-effort)."""
        import os
        cwd = os.getcwd()
        if os.path.exists(os.path.join(cwd, "Cargo.toml")):
            return "cargo test"
        if os.path.exists(os.path.join(cwd, "package.json")):
            return "npm test"
        if os.path.exists(os.path.join(cwd, "pyproject.toml")) or os.path.exists(os.path.join(cwd, "setup.py")):
            return "pytest"
        return "your test command"
    
    async def _stream_litellm(self, user_input: str, ctx: dict,
                               prior_history: list[dict] | None = None) -> AsyncGenerator[str, None]:
        """Streaming LiteLLM fallback with multi-turn history support."""
        import asyncio
        import litellm
        from evocli_soul.llm_client import LLMClient
    
        llm   = LLMClient(self.config)
        tier  = llm._resolve_model("auto")   # Router alias ("fast"/"smart")
    
        # Fix: 使用 context_engine 构建的 system_prompt（含项目约束、记忆、RepoMap）。
        if ctx and ctx.get("system_prompt"):
            system = ctx["system_prompt"]
            log.debug("_stream_litellm: using context_engine system_prompt (%d chars)", len(system))
        else:
            # Fallback: build prompt with model info for per-model specialization
            constraints = "（无）"
            if self.memory:
                try:
                    c = self.memory.get_constraints()
                    if c:
                        constraints = "\n".join(f"- {x}" for x in c)
                except Exception as _constr_err:
                    log.warning("_stream_litellm: constraint load failed — AI will run without project constraints: %s", _constr_err)
            llm_cfg    = (self.config or {}).get("llm", {})
            _model_id  = llm_cfg.get("tiers", {}).get("fast", "")
            _provider  = llm_cfg.get("provider", "")
            system = build_system_prompt(
                constraints=constraints,
                goal=user_input[:200],
                read_only=self.read_only,
                compact=True,
                model_id=_model_id,
                provider_id=_provider,
            )
    
        # Build messages: [system] + [prior history] + [current user turn]
        messages: list[dict] = [{"role": "system", "content": system}]
        # Do NOT extend messages with prior_history here — history is already embedded
        # in user_input (via _inject_context / user_context). Adding it again as
        # separate message turns would double the history in the conversation array.
        messages.append({"role": "user", "content": user_input})
        # Read stream timeout from config [agent] section (default 30s)
        _stream_timeout = float((self.config or {}).get("agent", {}).get("stream_timeout_s", 30))
    
        # Stream WITH tools so the model can legally signal finish_reason="tool_calls".
        # Some providers reject stream=True when tools= is present; we detect that error
        # and fall back to text-only streaming (degraded mode: model can narrate but not act).
        tools = self._build_tool_definitions()
        _stream_task_params = llm.get_task_params("stream")
        stream_call_kwargs: dict = {
            "model":       tier,
            "messages":    messages,
            "tools":       tools,
            "stream":      True,
            "max_tokens":  _stream_task_params.get("max_tokens",  2048),
            "temperature": _stream_task_params.get("temperature", 0.7),
            # Request usage in the final streaming chunk so we can emit cost_update.
            # Not all providers support this; non-fatal if absent.
            "stream_options": {"include_usage": True},
        }
        _tools_in_stream = True  # whether tools= was accepted by the provider
        try:
            response = await asyncio.wait_for(
                llm._router.acompletion(**stream_call_kwargs),
                timeout=_stream_timeout,
            )
        except Exception as _stream_tools_err:
            # Provider rejected stream+tools (e.g. older Ollama, some Azure configs).
            # Retry without tools= — model degrades to text-only narration.
            _err_str = str(_stream_tools_err).lower()
            _is_compat_err = any(k in _err_str for k in (
                "tool", "function", "not supported", "invalid", "unsupported",
            ))
            if _is_compat_err:
                log.warning("_stream_litellm: provider rejected stream+tools (%s), retrying text-only", type(_stream_tools_err).__name__)
                _tools_in_stream = False
                try:
                    response = await asyncio.wait_for(
                        llm._router.acompletion(
                            model=tier, messages=messages,
                            stream=True,
                            max_tokens=_stream_task_params.get("max_tokens", 2048),
                            temperature=_stream_task_params.get("temperature", 0.7),
                            stream_options={"include_usage": True},
                        ),
                        timeout=_stream_timeout,
                    )
                except asyncio.TimeoutError:
                    log.error("_stream_litellm: LLM API call timed out after %.0fs (model=%s)", _stream_timeout, tier)
                    yield f"\n\n⚠️ LLM API timed out ({_stream_timeout:.0f}s). Check your API key and network, then try again."
                    return
            elif isinstance(_stream_tools_err, asyncio.TimeoutError):
                log.error("_stream_litellm: LLM API call timed out after %.0fs (model=%s)", _stream_timeout, tier)
                yield f"\n\n⚠️ LLM API timed out ({_stream_timeout:.0f}s). Check your API key and network, then try again."
                return
            else:
                raise
    
        text_yielded = False
        tool_call_seen = False  # True if any delta contained tool_calls (even partial)
        finish_reason = None
        _stream_usage = None   # accumulated usage from stream_options include_usage chunk
        async for chunk in response:
            # Capture usage from the final usage-reporting chunk (stream_options include_usage=True)
            if hasattr(chunk, 'usage') and chunk.usage is not None:
                _stream_usage = chunk.usage
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta if hasattr(choice, 'delta') else None
            # ── Thinking / reasoning tokens (Claude 3.7, Gemini 2.5) ──────────
            # Some models emit reasoning_content or thinking on the delta before
            # the final answer. Cline pattern: check ThinkingDelta.reasoning_content
            # We yield as italicised text so it's visually distinct.
            if delta:
                _reasoning = (
                    getattr(delta, "reasoning_content", None)
                    or getattr(delta, "thinking", None)
                    or ""
                )
                if _reasoning:
                    yield f"*{_reasoning}*"
                    text_yielded = True
            text = (delta.content or "") if delta else ""
            if text:
                yield text
                text_yielded = True
            # Detect tool-call deltas: provider streams tool_calls list on delta
            if delta and getattr(delta, 'tool_calls', None):
                tool_call_seen = True
            # Track finish reason to detect tool-call requests
            if hasattr(choice, 'finish_reason') and choice.finish_reason:
                finish_reason = choice.finish_reason
    
        # Route to _run_litellm when tool use was detected.
        # Use finish_reason OR tool_call_seen — some providers set finish_reason="stop"
        # even when tool deltas were streamed (non-standard behaviour).
        # Always route when tool_call_seen regardless of text_yielded: a model that
        # streams prose then requests a tool is still requesting a tool.
        _tool_requested = (
            _tools_in_stream and (
                finish_reason in ("tool_calls", "function_call") or tool_call_seen
            )
        )
        if _tool_requested:
            log.info(
                "_stream_litellm: tool use detected (finish_reason=%s, tool_call_seen=%s), routing to _run_litellm",
                finish_reason, tool_call_seen,
            )
            if text_yielded:
                yield "\n\n"  # separate any streamed preamble from tool result
            tool_result = await self._run_litellm(user_input, ctx, prior_history=None)
            if tool_result:
                yield tool_result
    
        # After streaming completes, emit cost_update with real token counts.
        # Priority: (1) usage from stream_options include_usage final chunk,
        #           (2) response.usage populated post-stream by some providers,
        #           (3) _hidden_params fallback.
        try:
            from evocli_soul.rpc import emit_event as _emit_cost
            usage = _stream_usage  # from include_usage chunk (most reliable)
            if usage is None:
                usage = getattr(response, 'usage', None)
            if usage is None:
                try:
                    usage = response._hidden_params.get("response_cost_dict", {})
                except Exception:  # noqa: BLE001 — non-fatal event/metric, never block execution
                    pass
            in_tok  = int(getattr(usage, "prompt_tokens",     0) if hasattr(usage, "prompt_tokens")     else 0)
            out_tok = int(getattr(usage, "completion_tokens", 0) if hasattr(usage, "completion_tokens") else 0)
            if in_tok > 0 or out_tok > 0:
                try:
                    cost_usd = litellm.completion_cost(completion_response=response)
                except Exception:
                    cost_usd = 0.0
                await _emit_cost("cost_update", {
                    "input_tokens":  in_tok,
                    "output_tokens": out_tok,
                    "cost_usd":      cost_usd or 0.0,
                })
                log.debug("_stream_litellm usage: in=%d out=%d cost=%.4f", in_tok, out_tok, cost_usd or 0)
        except Exception as _e:
            log.debug("_stream_litellm: cost_update failed (non-fatal): %s", _e)
    

# pyright: reportMissingTypeArgument=false, reportAttributeAccessIssue=false, reportIndexIssue=false
"""agent_executor.py - Tool execution dispatcher mixin
Extracted from agent.py.
Single responsibility: _execute_tool — the main tool dispatch+bridge entry point.
"""
from __future__ import annotations
import logging
log = logging.getLogger('evocli.agent.executor')


def _tool_display_name(rpc_method: str, args: dict) -> str:
    """Convert an RPC method name to a human-readable display string for the TUI.

    Kept here (mirrored from agent.py) to avoid circular import:
    agent.py imports AgentExecutorMixin from this module, so this module
    cannot import from agent.py.
    """
    if rpc_method == "shell.run":
        cmd = args.get("cmd", "")
        return f"$ {cmd[:60]}{'…' if len(cmd) > 60 else ''}"
    if rpc_method in ("fs.read", "shell.cat"):
        return f"📖 {args.get('path', '')}"
    if rpc_method in ("fs.write", "fs.apply_diff"):
        return f"✏️  {args.get('path', '')}"
    if rpc_method == "git.commit":
        msg = args.get("message", "")
        return f"💾 git commit: {msg[:50]}"
    if rpc_method in ("search.code", "shell.grep"):
        return f"🔍 search: {args.get('query', args.get('pattern', ''))[:40]}"
    if rpc_method.startswith("symbol."):
        return f"🧩 {rpc_method}({args.get('name', args.get('symbol_id', ''))})"
    if rpc_method.startswith("code_intel."):
        return f"📊 {rpc_method.split('.')[-1]}"
    if rpc_method.startswith("memory."):
        return f"🧠 {rpc_method}"
    return rpc_method


class AgentExecutorMixin:
    """Mixin: _execute_tool for EvoCLIAgent."""

    async def _execute_tool(self, name: str, args: dict) -> str:
        """
        Execute a tool call. Handles two categories:
        1. Python-native tools (fs_apply_search_replace, fs_lint_file) — call Python directly
           without routing through Rust bridge (which doesn't know these methods)
        2. Standard tools — look up in _TOOL_TO_RPC and call bridge.call()
        """
        from evocli_soul.rpc import emit_event

        # require_diff_preview: show a diff and ask approval before any file edit.
        # Enabled via config.toml [safety] require_diff_preview = true.
        # Mirrors Cursor's "preview before apply" — prevents surprise changes.
        _safety = (self.config or {}).get("safety", {})
        _require_preview = bool(_safety.get("require_diff_preview", False))
        _EDIT_TOOLS = {"fs_apply_search_replace", "fs_apply_batch", "fs_write"}
        if _require_preview and name in _EDIT_TOOLS:
            preview_result = await self._diff_preview_and_confirm(name, args)
            if preview_result == "rejected":
                import json as _json_preview
                return _json_preview.dumps({
                    "ok": False,
                    "error": "User rejected the change preview. Try a different approach.",
                }, ensure_ascii=False)
            # If approved or preview failed gracefully, continue with actual edit

        # ── Doom loop detection ───────────────────────────────────────────────
        try:
            from evocli_soul.state import is_doom_loop, record_tool_call

            if is_doom_loop(name, args, self._session_id):
                log.warning("DOOM LOOP detected: tool=%s called 3+ times with same args", name)
                import json as _json_doom

                return _json_doom.dumps({
                    "ok": False,
                    "error": f"Doom loop detected: '{name}' was called with identical arguments 3+ times.",
                    "suggestion": "The same approach is not working. Try a completely different strategy, or call give_up if the task cannot be completed.",
                })
            record_tool_call(name, args, self._session_id)
        except Exception as _dl_err:
            log.debug("doom loop check failed (non-fatal): %s", _dl_err)

        import json as _json

        # GAP-3: Record tool call to session event buffer for memory distillation.
        # Map tool names to event types that MemoryDistiller._extract_*_chains() recognizes:
        #   success anchors: git_commit, test_passed, skill_success
        #   failure anchors: test_failed, error, skill_failed
        # We record BEFORE execution so failures are captured even if the tool raises.
        _DISTILL_EVENT_MAP: dict[str, str] = {
            "git_commit":             "git_commit",   # success anchor
            "test_and_capture":       "test_call",    # outcome resolved after result
            "fs_apply_search_replace":"code_edit",
            "fs_apply_batch":         "code_edit",
            "fs_lint_file":           "lint_call",
            "run_and_capture":        "shell_run",
        }
        _ev_type = _DISTILL_EVENT_MAP.get(name, "tool_called")
        try:
            import evocli_soul.state as _st
            # ToolFlowMiner: 记录带参数的富事件（用于工具流挖掘）
            # 之前只记录工具名；现在加上 params，使 FlowMiner 能重建序列
            _st.append_session_event({
                "type":   "tool_called",
                "method": self._TOOL_TO_RPC.get(name, (name, None))[0] if name in self._TOOL_TO_RPC else name,
                "tool":   name,
                "params": {k: v for k, v in args.items() if k not in ("content", "diff", "edits_json")},  # 不存大内容
                "session_id": self._session_id,
            })
        except Exception as _e:
            log.debug("stream tool parse skipped: %s", _e)  # Never let event recording break tool execution

        # ── Python-native tools (architecture fix: Oracle routing bug) ──────────
        # These tools use Python logic but call bridge for IO operations.
        # They CANNOT use bridge.call("fs.apply_search_replace") because Rust doesn't handle it.
        if name == "fs_apply_search_replace":
            await emit_event("tool_call_start", {"tool": "fs.apply_search_replace", "display": f"✏️  SEARCH/REPLACE {args.get('path','')}"})
            try:
                content = await self.bridge.call("fs.read", {"path": args["path"]})
                if not isinstance(content, str):
                    result = {"ok": False, "error": f"Could not read: {args['path']}"}
                else:
                    from evocli_soul.edit_engine import apply_search_replace, AmbiguousSearchError
                    try:
                        new_content, strategy = apply_search_replace(content, args.get("search",""), args.get("replace",""))
                        await self.bridge.call("fs.write", {"path": args["path"], "content": new_content})
                        result = {"ok": True, "strategy": strategy}
                    except AmbiguousSearchError as amb:
                        # Return match locations to LLM — let it add more context and retry
                        feedback = amb.to_ai_feedback()
                        result = {
                            "ok": False, "strategy": "ambiguous",
                            "ambiguous": True,
                            "match_count": amb.match_count,
                            "match_lines": amb.match_line_numbers,
                            "error": feedback,
                            "reflection_prompt": (
                                f"Ambiguous SEARCH block matched {amb.match_count} locations at lines "
                                f"{amb.match_line_numbers}. Add more surrounding context lines to your "
                                f"search pattern to uniquely identify the target. {feedback}"
                            ),
                        }
            except ValueError as e:
                error_msg = str(e)
                result = {
                    "ok": False, "strategy": "all_failed", "error": error_msg,
                    "reflection_prompt": (
                        f"The SEARCH block was not found in {args.get('path', '?')}. "
                        f"Read the file first with fs_read, then copy the exact text you want to replace "
                        f"as the search parameter. Details: {error_msg[:300]}"
                    ),
                }
            except Exception as e:
                result = {"ok": False, "error": str(e)}
            await emit_event("tool_call_done", {"tool": "fs.apply_search_replace", "ok": result.get("ok", False)})
            # GAP-3: Record outcome for distillation
            try:
                import evocli_soul.state as _st3
                _st3.append_session_event({
                    "type":   "tool_done",
                    "method": "fs_apply_search_replace",
                    "ok":     result.get("ok", False),
                }, self._session_id)
            except Exception as _e:
                log.debug("stream tool parse skipped: %s", _e)
            return _json.dumps(result, ensure_ascii=False)

        # ── fs_read_symbol: Python-composite tool (symbol lookup + range read) ──
        # NOT in _TOOL_TO_RPC because it's not a single Rust RPC — it chains
        # symbol.lookup then fs.read_range. Handled here like fs_apply_search_replace.
        if name == "fs_read_symbol":
            symbol_name   = args.get("symbol_name", "")
            path_hint     = args.get("path", "")
            context_lines = int(args.get("context_lines", 10))
            await emit_event("tool_call_start", {"tool": "fs.read_symbol", "display": f"🔍 {symbol_name}"})
            try:
                search_params = {"name": symbol_name}
                if path_hint:
                    search_params["file"] = path_hint
                symbols = await self.bridge.call("symbol.lookup", search_params)
                # Normalize response: Rust may return list OR {"found":..,"symbols":[..]}
                if isinstance(symbols, dict):
                    symbols = symbols.get("symbols", []) or ([] if not symbols.get("found") else [symbols])
                if not isinstance(symbols, list) or not symbols:
                    grep_result = await self.bridge.call("shell.grep", {
                        "pattern": rf"\b{symbol_name}\b", "path": path_hint or ".",
                    })
                    result = {"symbol": symbol_name, "found": False,
                              "fallback": str(grep_result)[:1000],
                              "note": "Symbol not in index — run 'evocli index' for better results."}
                else:
                    sym = symbols[0]
                    sym_file = sym.get("file", path_hint)
                    sym_line = int(sym.get("line", 0))
                    if sym_file and sym_line > 0:
                        start = max(1, sym_line - context_lines)
                        end   = sym_line + 80 + context_lines
                        range_result = await self.bridge.call("fs.read_range", {
                            "path": sym_file, "start_line": start, "end_line": end,
                        })
                        if isinstance(range_result, dict):
                            range_result["symbol"] = symbol_name
                            range_result["symbol_line"] = sym_line
                            range_result["symbol_kind"] = sym.get("kind", "unknown")
                        result = range_result if isinstance(range_result, dict) else {"content": str(range_result)}
                    else:
                        result = {"symbol": symbol_name, "found": True, "error": "No file/line info", "raw": sym}
                await emit_event("tool_call_done", {"tool": "fs.read_symbol", "ok": True})
                return _json.dumps(result, ensure_ascii=False)
            except Exception as e:
                await emit_event("tool_call_done", {"tool": "fs.read_symbol", "ok": False})
                return _json.dumps({"symbol": symbol_name, "error": str(e)}, ensure_ascii=False)

        if name == "fs_lint_file":
            from pathlib import Path as _Path
            ext = _Path(args.get("path", "")).suffix.lower()
            lang_cmds = {".py": f"python -m py_compile {args.get('path','')}", ".rs": "cargo check --message-format short 2>&1"}
            cmd = lang_cmds.get(ext)
            if not cmd:
                return _json.dumps({"ok": True, "output": f"No linter for {ext}", "errors": []}, ensure_ascii=False)
            await emit_event("tool_call_start", {"tool": "fs.lint_file", "display": f"🔍 lint {args.get('path','')}"})
            try:
                r = await self.bridge.call("shell.run", {"cmd": cmd, "cwd": ".", "timeout_s": 30, "dry_run": False})
                stdout    = r.get("stdout","") if isinstance(r,dict) else str(r)
                stderr    = r.get("stderr","") if isinstance(r,dict) else ""
                exit_code = r.get("exit_code",0) if isinstance(r,dict) else 0
                output    = (stdout+"\n"+stderr).strip()
                passed    = (exit_code == 0)
                result    = {"ok": passed, "output": output[:1000], "exit_code": exit_code,
                             "reflection_prompt": f"Lint failed:\n```\n{output[:500]}\n```\nFix errors." if not passed else ""}
            except Exception as e:
                result = {"ok": False, "error": str(e)}
            await emit_event("tool_call_done", {"tool": "fs.lint_file", "ok": result.get("ok", False)})
            # GAP-3: Lint failure = error anchor for distillation
            if not result.get("ok", True):
                try:
                    import evocli_soul.state as _st_lint
                    _st_lint.append_session_event({
                        "type": "error", "method": "fs_lint_file",
                        "error": result.get("output", "")[:200],
                    }, self._session_id)
                except Exception:
                    pass
            return _json.dumps(result, ensure_ascii=False)

        # ── GAP-6: Atomic multi-file batch edit (Option C: in-memory rollback) ─
        # Aider pattern: save originals in memory before any writes; on any failure,
        # restore from memory. No git dependency — always safe even with dirty workdir.
        if name == "fs_apply_batch":
            await emit_event("tool_call_start", {"tool": "fs.apply_batch", "display": "✏️  Batch SEARCH/REPLACE"})
            skip_failed = bool(args.get("skip_failed", False))
            try:
                edits = _json.loads(args.get("edits_json", "[]"))
            except Exception as e:
                return _json.dumps({"ok": False, "error": f"edits_json parse error: {e}"}, ensure_ascii=False)
            if not isinstance(edits, list) or not edits:
                return _json.dumps({"ok": False, "error": "edits_json must be a non-empty JSON array"}, ensure_ascii=False)

            from evocli_soul.edit_engine import apply_search_replace, AmbiguousSearchError

            # Phase 1: Read all originals into memory (rollback checkpoint)
            originals: dict[str, str] = {}
            for edit in edits:
                path = edit.get("path", "")
                if path not in originals:
                    try:
                        content = await self.bridge.call("fs.read", {"path": path})
                        if isinstance(content, str):
                            originals[path] = content
                    except Exception as e:
                        await emit_event("tool_call_done", {"tool": "fs.apply_batch", "ok": False})
                        return _json.dumps({
                            "ok": False, "rolled_back": False,
                            "error": f"Cannot read {path} before edit: {e}",
                        }, ensure_ascii=False)

            # Phase 2: Apply all edits
            results: list[dict] = []
            failed = False
            for edit in edits:
                path = edit.get("path", "")
                # Use the latest content (previous edit may have modified same file)
                current = originals.get(path, "")
                # Reflect previous successful edits in same file
                for prev in results:
                    if prev.get("path") == path and prev.get("ok"):
                        current = prev.get("_new_content", current)
                try:
                    new_content, strategy = apply_search_replace(
                        current, edit.get("search", ""), edit.get("replace", "")
                    )
                    results.append({
                        "path": path, "ok": True, "strategy": strategy,
                        "_new_content": new_content,  # internal; stripped before return
                    })
                except AmbiguousSearchError as amb:
                    results.append({
                        "path": path, "ok": False, "ambiguous": True,
                        "error": amb.to_ai_feedback(),
                        "reflection_prompt": (
                            f"SEARCH block is ambiguous in {path}: {amb.to_ai_feedback()}\n"
                            f"Add more surrounding context lines to uniquely identify the target."
                        ),
                    })
                    failed = True
                except ValueError as e:
                    results.append({"path": path, "ok": False, "strategy": "all_failed", "error": str(e),
                                    "reflection_prompt": f"SEARCH block not found in {path}: {e}"})
                    failed = True
                except Exception as e:
                    results.append({"path": path, "ok": False, "error": str(e)})
                    failed = True

            if failed:
                # Strip internal _new_content before returning
                clean_results = [{k: v for k, v in r.items() if k != "_new_content"} for r in results]
                if skip_failed:
                    # skip_failed=True: DON'T rollback successful edits.
                    # Write the successful ones, skip the failures, report both.
                    actually_written = 0
                    for i, r in enumerate(results):
                        if r.get("ok") and r.get("_new_content") is not None:
                            try:
                                await self.bridge.call("fs.write", {"path": r["path"], "content": r["_new_content"]})
                                actually_written += 1
                            except Exception as _we:
                                # Write failed: update result to reflect actual failure
                                log.warning("fs_apply_batch skip_failed write error %s: %s", r["path"], _we)
                                results[i] = {**r, "ok": False, "error": f"write failed: {_we}"}
                    # Recompute clean_results after potential status updates
                    clean_results = [{k: v for k, v in r.items() if k != "_new_content"} for r in results]
                    log.info("fs_apply_batch(skip_failed=True): %d written, %d failed",
                             actually_written, sum(1 for r in results if not r.get("ok")))
                    await emit_event("tool_call_done", {"tool": "fs.apply_batch", "ok": False})
                    return _json.dumps({
                        "ok": False, "rolled_back": False, "partial": True,
                        "applied": actually_written,
                        "failed":  sum(1 for r in results if not r.get("ok")),
                        "error": "Some edits failed (skip_failed=True: successes written). Fix the failed ones individually.",
                        "results": clean_results,
                    }, ensure_ascii=False)
                else:
                    # skip_failed=False (atomic): rollback ALL
                    for path, original_content in originals.items():
                        try:
                            await self.bridge.call("fs.write", {"path": path, "content": original_content})
                        except Exception as re:
                            log.warning("fs_apply_batch rollback failed for %s: %s", path, re)
                    # skip_failed=False (default, atomic): roll back everything
                    await emit_event("tool_call_done", {"tool": "fs.apply_batch", "ok": False})
                    return _json.dumps({
                        "ok": False, "rolled_back": True,
                        "error": "One or more edits failed — all files restored. Use skip_failed=True to keep successes.",
                        "results": clean_results,
                    }, ensure_ascii=False)

            # Phase 3b: Commit all writes — if ANY write fails, restore ALL from originals
            write_errors = []
            committed_paths: list[str] = []
            for r in results:
                if r.get("ok"):
                    try:
                        await self.bridge.call("fs.write", {"path": r["path"], "content": r["_new_content"]})
                        committed_paths.append(r["path"])
                    except Exception as e:
                        write_errors.append(f"{r['path']}: {e}")
            if write_errors:
                # Write-phase failure: restore ALL already-committed files from originals
                for path in committed_paths:
                    if path in originals:
                        try:
                            await self.bridge.call("fs.write", {"path": path, "content": originals[path]})
                        except Exception as re:
                            log.warning("fs_apply_batch commit-rollback failed for %s: %s", path, re)
                clean_results = [{k: v for k, v in r.items() if k != "_new_content"} for r in results]
                await emit_event("tool_call_done", {"tool": "fs.apply_batch", "ok": False})
                return _json.dumps({
                    "ok": False, "rolled_back": True,
                    "error": f"Write errors during commit — all files restored: {write_errors}",
                    "results": clean_results,
                }, ensure_ascii=False)
            applied = sum(1 for r in results if r.get("ok"))
            clean_results = [{k: v for k, v in r.items() if k != "_new_content"} for r in results]
            await emit_event("tool_call_done", {"tool": "fs.apply_batch", "ok": True})
            return _json.dumps({
                "ok": True, "rolled_back": False,
                "applied": applied, "total": len(edits),
                "results": clean_results,
            }, ensure_ascii=False)

        # ── Fix H1: Memory tools → Python LanceDB (统一存储，不走 Rust SQLite) ────
        if name == "memory_recall":
            await emit_event("tool_call_start", {"tool": "memory.recall", "display": "🧠 memory.recall"})
            try:
                from evocli_soul import state as _state
                memory = _state.get_memory()
                results = memory.search(args.get("query", ""), top_k=int(args.get("top_k", 5)))
                await emit_event("tool_call_done", {"tool": "memory.recall", "ok": True})
                return _json.dumps(results, ensure_ascii=False)
            except Exception as e:
                await emit_event("tool_call_done", {"tool": "memory.recall", "ok": False})
                return _json.dumps({"error": str(e)}, ensure_ascii=False)

        if name == "memory_write":
            await emit_event("tool_call_start", {"tool": "memory.write", "display": "🧠 memory.write"})
            try:
                from evocli_soul import state as _state
                from evocli_soul.memory_router import get_memory_router
                from evocli_soul.handlers.metrics import _classify_with_model

                title   = args.get("title", "")
                body    = args.get("body", "")
                content = f"{title}\n{body}" if body else title
                if not content or not content.strip():
                    await emit_event("tool_call_done", {"tool": "memory.write", "ok": False})
                    return _json.dumps({"ok": False, "reason": "empty content"}, ensure_ascii=False)

                memory = _state.get_memory()
                router = get_memory_router()
                recent = memory.get_all(limit=20)
                should, rule_type, rule_importance = router.should_memorize(content, recent)

                if not should:
                    await emit_event("tool_call_done", {"tool": "memory.write", "ok": False})
                    return _json.dumps({"ok": False, "reason": "not worth memorizing"}, ensure_ascii=False)

                ml_result = _classify_with_model(content)
                if ml_result and ml_result.get("confidence", 0) >= 0.6:
                    mem_type   = ml_result["label"]
                    importance = float(ml_result.get("importance", rule_importance))
                else:
                    mem_type   = rule_type
                    importance = rule_importance

                mid = memory.add(content, memory_type=mem_type, priority="project", importance=importance)

                # MemRouter training data accumulation (hot-path fix):
                # When ML model is unavailable or low-confidence, fall back to LLM labeling
                # AND persist the label to JSONL for future Phase-1 classifier training.
                # This is the fix for the broken MemRouter training pipeline — the issue was
                # that label_with_llm() was only called from seed_labels_from_existing(),
                # which is never triggered automatically.
                if ml_result is None or ml_result.get("confidence", 0) < 0.6:
                    # Background LLM labeling — non-blocking, best-effort
                    import asyncio as _asyncio
                    async def _label_in_background(c: str, t: str) -> None:
                        try:
                            from evocli_soul.mem_router_labeler import label_with_llm
                            from evocli_soul import state as _st_llm
                            llm_client = _st_llm.get_llm_client()
                            await label_with_llm(c, llm_client)  # store_label_direct called inside
                        except Exception:
                            pass
                    _asyncio.create_task(_label_in_background(content, mem_type))

                await emit_event("tool_call_done", {"tool": "memory.write", "ok": True})
                return _json.dumps({"ok": True, "id": mid, "memory_type": mem_type}, ensure_ascii=False)
            except Exception as e:
                await emit_event("tool_call_done", {"tool": "memory.write", "ok": False})
                return _json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)

        if name == "memory_constraints":
            await emit_event("tool_call_start", {"tool": "memory.constraints", "display": "🧠 memory.constraints"})
            try:
                from evocli_soul import state as _state
                memory = _state.get_memory()
                constraints = memory.get_constraints()
                await emit_event("tool_call_done", {"tool": "memory.constraints", "ok": True})
                return _json.dumps(constraints, ensure_ascii=False)
            except Exception as e:
                await emit_event("tool_call_done", {"tool": "memory.constraints", "ok": False})
                return _json.dumps({"error": str(e)}, ensure_ascii=False)

        # ── MCP tools (externally registered MCP servers) ────────────────────────
        if name.startswith("mcp_") or name in ("mcp_call", "mcp_list_tools"):
            try:
                from evocli_soul.handlers.mcp_bridge import call_mcp_tool, _mcp_tools
                import json as _mjson
                if name == "mcp_list_tools":
                    tools = [{"name": k, "server": v["server"], "description": v["description"][:80]} for k, v in _mcp_tools.items()]
                    return _mjson.dumps({"total": len(tools), "tools": tools}, ensure_ascii=False)
                if name == "mcp_call":
                    tool_key  = args.get("tool_name", "")
                    raw_args  = args.get("arguments_json", "{}")
                    arguments = _mjson.loads(raw_args) if isinstance(raw_args, str) else raw_args
                else:
                    tool_key  = name
                    arguments = args
                await emit_event("tool_call_start", {"tool": tool_key, "display": f"MCP: {tool_key}"})
                result = await call_mcp_tool(tool_key, arguments)
                await emit_event("tool_call_done", {"tool": tool_key, "ok": True})
                return _mjson.dumps(result, ensure_ascii=False) if not isinstance(result, str) else result
            except Exception as e:
                await emit_event("tool_call_done", {"tool": name, "ok": False})
                return f"MCP error: {e}"

        # ── Migrated Python handlers (H1/H2: Rust arms removed, route to Python directly) ──
        _MIGRATED_TO_HANDLER = {
            "assume_has_tests":        lambda a: (_ca.handle_assume_has_tests,     {"symbol": a.get("symbol","")}),
            "assume_is_pure":          lambda a: (_ca.handle_assume_is_pure,        {"symbol": a.get("symbol","")}),
            "assume_caller_count":     lambda a: (_ca.handle_assume_caller_count,   {"symbol": a.get("symbol","")}),
            "assume_has_side_effects": lambda a: (_ca.handle_assume_has_side_effects, {"symbol": a.get("symbol","")}),
            "assume_verify":           lambda a: (_ca.handle_assume_verify,         {"assumption": a.get("assumption",""), "subject": a.get("subject","")}),
            "assume_is_deprecated":    lambda a: (_ca.handle_assume_is_deprecated,  {"symbol": a.get("symbol","")}),
            "assume_is_only_caller":   lambda a: (_ca.handle_assume_is_only_caller, {"caller": a.get("caller", a.get("symbol","")), "target": a.get("target","")}),
            "assume_types_match":      lambda a: (_ca.handle_assume_types_match,    {"symbol_a": a.get("symbol_a", a.get("symbol","")), "symbol_b": a.get("symbol_b","")}),
            "impact_check":            lambda a: (_ca.handle_impact_check,          {"symbol": a.get("symbol",""), "change_type": a.get("change_type","behavior")}),
            "impact_affected_tests":   lambda a: (_ca.handle_impact_affected_tests, {"symbol": a.get("symbol","")}),
            "impact_batch_check":      lambda a: (_ca.handle_impact_batch_check,    {"symbols": a.get("symbols", []), "change_type": a.get("change_type", "behavior")}),
            "verify_task":             lambda a: (_ca.handle_verify_task,           {"contract_id": a.get("contract_id", a.get("task_id",""))}),
            "verify_coverage":         lambda a: (_ca.handle_verify_coverage,       {"contract_id": a.get("contract_id", a.get("symbol",""))}),
            "verify_drift":            lambda a: (_ca.handle_verify_drift,          {"contract_id": a.get("contract_id", a.get("spec","")), "use_llm": True}),
            "equiv_find":              lambda a: (_ca.handle_equiv_find,            {"intent": a.get("intent",""), "limit": a.get("limit", 5)}),
            "equiv_check_deps":        lambda a: (_ca.handle_equiv_check_deps,     {"intent": a.get("intent","")}),
            "equiv_find_similar_code": lambda a: (_ca.handle_equiv_find_similar_code, {"code": a.get("code",""), "limit": a.get("limit", 5)}),
            "symbol_usages":           lambda a: (_ca.handle_symbol_usages,        {"symbol_id": a.get("symbol_id",""), "limit": a.get("limit", 20)}),
            "symbol_lifecycle":        lambda a: (_ca.handle_symbol_lifecycle,      {"symbol": a.get("name", a.get("symbol",""))}),
            "code_intel_ranked_context": lambda a: (_ca.handle_ranked_context,    {"modified_file": a.get("modified_file", "."), "mentioned": a.get("mentioned", []), "limit": a.get("limit", 20)}),
        }
        if name in _MIGRATED_TO_HANDLER:
            from evocli_soul.handlers import code_analysis as _ca
            import json as _mj
            import evocli_soul.state as _m_state
            _m_result: dict = {}
            class _MSend:
                async def response(self, req_id, data): _m_result['data'] = data
                async def error(self, req_id, code, msg): _m_result['error'] = msg
            fn, params_dict = _MIGRATED_TO_HANDLER[name](args)
            try:
                await fn("local", params_dict, _MSend(), _m_state)
            except Exception as e:
                return f"Error: {e}"
            if 'error' in _m_result:
                return f"Error: {_m_result['error']}"
            return _mj.dumps(_m_result.get('data', {}), ensure_ascii=False)

        if name == "skill_search":
            import json as _ssj
            try:
                import evocli_soul.state as _ss_state
                engine = _ss_state.get_skill_engine()
                if not hasattr(engine, "find_relevant_guidance"):
                    return _ssj.dumps({
                        "query": args.get("query", ""),
                        "results": [],
                        "hint": "Guidance search is unavailable in the current skill engine.",
                    }, ensure_ascii=False)

                matches = engine.find_relevant_guidance(args.get("query", ""), top_k=3) or []
                return _ssj.dumps({
                    "query": args.get("query", ""),
                    "count": len(matches),
                    "results": [
                        {
                            "id": gs.id,
                            "name": gs.name,
                            "description": gs.description,
                            "content": gs.content[:1500],
                        }
                        for gs in matches
                    ],
                }, ensure_ascii=False)
            except Exception as e:
                return _ssj.dumps({"error": str(e), "query": args.get("query", "")}, ensure_ascii=False)

        if name == "experience_lookup":
            import json as _elj
            try:
                from evocli_soul.tool_flow_miner import check_flow_trigger
                matched_flow, score = check_flow_trigger(args.get("task_description", ""))
                if not matched_flow:
                    return _elj.dumps({"found": False, "message": "No matching past experience found."}, ensure_ascii=False)
                failures_before = getattr(matched_flow, "failures_before", 0)
                struggle_note = (
                    f"Discovered after {failures_before} failed attempts — battle-tested knowledge."
                    if failures_before >= 1 else "First-try success pattern."
                )
                return _elj.dumps({
                    "found": True,
                    "name": matched_flow.name,
                    "similarity": round(score, 2),
                    "success_rate": round(getattr(matched_flow, "success_rate", 0.0), 2),
                    "failures_before": failures_before,
                    "note": struggle_note,
                    "steps": [
                        {"step": i + 1, "tool": s.tool,
                         "description": getattr(s, "description", s.tool)}
                        for i, s in enumerate(matched_flow.steps[:8])
                    ],
                    "guidance": "Use this pattern as a reference; adapt to current context.",
                }, ensure_ascii=False)
            except Exception as e:
                return _elj.dumps({"error": str(e)}, ensure_ascii=False)

        # ── Task planning + completion tools (Python-native, state-based) ──────────
        # These were previously registered as @agent.tool_plain in agent_tools_code.py.
        # With pydantic-ai removed, they are handled here so LiteLLM can call them.

        if name == "todo_write":
            import json as _tw_j
            from evocli_soul import state as _tw_st
            from evocli_soul.rpc import emit_event as _tw_ev
            try:
                todos_json = args.get("todos_json", args.get("todos", "[]"))
                todos = _tw_j.loads(todos_json) if isinstance(todos_json, str) else todos_json
                if not isinstance(todos, list):
                    return _tw_j.dumps({"ok": False, "error": "todos_json must be a JSON array"})
                normalized = [{"id": str(t.get("id", i+1)), "content": str(t.get("content", "")),
                               "status": t.get("status", "pending"), "priority": t.get("priority", "medium")}
                              for i, t in enumerate(todos)]
                _tw_st.set_todos(normalized, self._session_id)
                status_icons = {"pending": "⬜", "in_progress": "🔄", "completed": "✅", "cancelled": "❌"}
                priority_icons = {"high": "🔴", "medium": "🟡", "low": "🟢"}
                lines = [f"📋 **任务计划** ({len(normalized)} 项)"]
                for t in normalized:
                    lines.append(f"{status_icons.get(t['status'], '⬜')} {priority_icons.get(t['priority'], '')} {t['content']}")
                display = "\n".join(lines)
                await _tw_ev("todo_update", {"todos": normalized, "display": display})
                return _tw_j.dumps({"ok": True, "count": len(normalized), "display": display}, ensure_ascii=False)
            except Exception as e:
                return _tw_j.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)

        if name == "todo_read":
            import json as _tr_j
            from evocli_soul import state as _tr_st
            todos = _tr_st.get_todos(self._session_id)
            if not todos:
                return _tr_j.dumps({"todos": [], "hint": "No task plan yet. Use todo_write to create one."})
            pending   = [t for t in todos if t.get("status") == "pending"]
            in_prog   = [t for t in todos if t.get("status") == "in_progress"]
            completed = [t for t in todos if t.get("status") == "completed"]
            return _tr_j.dumps({
                "todos": todos,
                "summary": f"{len(completed)} done, {len(in_prog)} in progress, {len(pending)} pending",
                "remaining": len(pending) + len(in_prog),
            }, ensure_ascii=False)

        if name == "task_complete":
            import json as _tc_j
            from evocli_soul import state as _tc_st
            result  = args.get("result", "")
            command = args.get("command", "")
            # Cline double-check pattern: first call rejected, second accepted
            if not _tc_st.is_task_double_checked(self._session_id):
                _tc_st.mark_task_double_checked(self._session_id)
                todos = _tc_st.get_todos(self._session_id)
                incomplete = [t for t in todos if t.get("status") not in ("completed", "cancelled")]
                if incomplete:
                    items = "\n".join(f"  - [{t.get('id','')}] {t.get('content','')}" for t in incomplete)
                    return _tc_j.dumps({"ok": False, "re_verify": True,
                                        "message": f"⚠️ Re-verify required.\n\nIncomplete todos:\n{items}\n\nComplete all items then call task_complete again."})
                return _tc_j.dumps({"ok": False, "re_verify": True,
                                    "message": "⚠️ Final self-audit: read your changes, verify tests pass, confirm requirements met. Then call task_complete again."})
            # Second call: accepted
            _tc_st.set_task_complete(self._session_id, result, command)
            todos = _tc_st.get_todos(self._session_id)
            if todos:
                for t in todos:
                    if t.get("status") == "pending":
                        t["status"] = "completed"
                _tc_st.set_todos(todos, self._session_id)
            try:
                from evocli_soul.rpc import emit_event as _tc_ev
                await _tc_ev("task_complete", {"result": result, "command": command})
            except Exception:
                pass
            return _tc_j.dumps({"ok": True, "signal": "task_complete", "message": "✅ Task complete signal accepted. Finalizing…"}, ensure_ascii=False)

        if name == "give_up":
            import json as _gu_j
            from evocli_soul import state as _gu_st
            reason          = args.get("reason", "")
            what_was_tried  = args.get("what_was_tried", "")
            suggestion      = args.get("suggestion", "")
            result_text = f"[WITHDRAWN] {reason}"
            if suggestion:
                result_text += f"\n\nSuggestion: {suggestion}"
            _gu_st.mark_task_double_checked(self._session_id)
            _gu_st.set_task_complete(self._session_id, result_text, "")
            try:
                _gu_st.append_session_event({"type": "give_up", "reason": reason, "tried": what_was_tried}, self._session_id)
            except Exception:
                pass
            return _gu_j.dumps({"withdrawn": True, "reason": reason, "what_was_tried": what_was_tried,
                                 "suggestion": suggestion or "Please clarify requirements."}, ensure_ascii=False)

        # ── Standard tools via Rust bridge ──────────────────────────────────────
        if name not in self._TOOL_TO_RPC:
            return f"Error: Unknown tool '{name}'"
        rpc_method, args_fn = self._TOOL_TO_RPC[name]
        try:
            rpc_args = args_fn(args)
            _WRITE_METHODS = {"shell.run", "fs.apply_diff", "fs.write", "git.commit",
                              "git.shadow_snapshot", "git.restore", "git.shadow_restore"}
            if self.read_only and rpc_method in _WRITE_METHODS:
                rpc_args["dry_run"] = True

            # FIX-B: 工具开始执行 → TUI 实时显示
            tool_display = _tool_display_name(rpc_method, rpc_args)
            await emit_event("tool_call_start", {"tool": rpc_method, "display": tool_display})

            result = await self.bridge.call(rpc_method, rpc_args)

            # Graceful skip for unreadable files — return a note instead of raw error.
            # When a file doesn't exist or can't be read, the LLM should skip and continue,
            # not get confused by an opaque error code.
            _READ_METHODS = {"fs.read", "fs.read_range", "fs.read_symbol"}
            if rpc_method in _READ_METHODS and isinstance(result, str):
                _err = result.lower()
                if ("cannot read" in _err or "not found" in _err
                        or "no such file" in _err or "permission denied" in _err
                        or result.startswith("Error:")):
                    _path = rpc_args.get("path", name)
                    # Strip the Rust error prefix for a clean note
                    _reason = result.split("] ", 1)[-1].strip() if "] " in result else result
                    result = f"[Skipped: '{_path}' — {_reason}]"
                    await emit_event("tool_call_done", {"tool": rpc_method, "ok": False})
                    log.debug("File read skipped gracefully: %s", _path)
                    return result

            # C6: Duplicate file read deduplication (Cline pattern)
            # When the same file is read multiple times in a session, annotate the
            # result so the LLM knows it already saw this content. Prevents history
            # from bloating with redundant large file copies across turns.
            if rpc_method == "fs.read" and isinstance(result, str):
                path = rpc_args.get("path", "")
                if path:
                    try:
                        import evocli_soul.state as _st_dr
                        read_count = _st_dr.record_file_read(path, self._session_id)
                        if read_count >= 2:
                            first_turn = _st_dr.get_file_first_read_turn(path, self._session_id)
                            note = (
                                f"\n\n[Note: {path} was also read in turn {first_turn}. "
                                f"Content may be identical if unchanged since then.]"
                            )
                            result = result + note
                    except Exception:
                        pass  # never let dedup annotation break file reads

            # FIX-B: 工具执行完成 → TUI 更新状态
            await emit_event("tool_call_done", {"tool": rpc_method, "ok": True})

            # GAP-3: Record semantic outcome events for MemoryDistiller
            # These map to the anchor types distiller recognizes: git_commit, test_passed/failed
            try:
                import evocli_soul.state as _st3
                if rpc_method == "git.commit":
                    # Successful git commit = success chain anchor
                    _st3.append_session_event({"type": "git_commit", "method": name, "session_id": self._session_id}, self._session_id)
                elif name == "test_and_capture":
                    # shell.run used for test — check exit code in result
                    exit_code = result.get("exit_code", 0) if isinstance(result, dict) else 0
                    ev_type = "test_passed" if exit_code == 0 else "test_failed"
                    _st3.append_session_event({
                        "type": ev_type, "method": name,
                        "error": result.get("stderr", "")[:200] if isinstance(result, dict) and exit_code != 0 else "",
                        "session_id": self._session_id,
                    }, self._session_id)
                # ToolFlowMiner: 记录 tool_done（带结果摘要）
                result_summary = ""
                if isinstance(result, str):
                    result_summary = result[:80]
                elif isinstance(result, dict):
                    result_summary = str(result.get("ok", result.get("content", "")))[:80]
                _st3.append_session_event({
                    "type":    "tool_done",
                    "method":  rpc_method,
                    "tool":    name,
                    "ok":      True,
                    "result":  result_summary,
                    "session_id": self._session_id,
                }, self._session_id)
            except Exception:
                pass

            if isinstance(result, str):
                return result
            import json
            return json.dumps(result, ensure_ascii=False, indent=2)
        except Exception as e:
            await emit_event("tool_call_done", {"tool": rpc_method, "ok": False, "error": str(e)})
            # GAP-3: Record error event as failure chain anchor
            try:
                import evocli_soul.state as _st3e
                _st3e.append_session_event({"type": "error", "method": name, "error": str(e)}, self._session_id)
            except Exception:
                pass
            log.exception("Tool %s failed", name)
            return f"Error: {e}"


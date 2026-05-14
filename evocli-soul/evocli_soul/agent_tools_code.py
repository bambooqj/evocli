"""
agent_tools_code.py — Code intelligence and task planning tools registration
Part 2 of agent_tools.py (atomized per 500-line limit).
Contains: fetch_url, code_semantic_search, code_*, mcp_*, fs_apply_batch,
         todo_write, todo_read, task_complete, spawn_agent
"""
from __future__ import annotations
import logging
_log = logging.getLogger("evocli.agent.tools.code")

def register(agent, _sc, _call_handler, _sid, _json, bridge=None, config=None, memory=None):
    """Register code intelligence and task planning tools on agent."""

    
    @agent.tool_plain
    async def fetch_url(url: str, max_chars: int = 8000, selector: str = "") -> str:
        """
        Fetch a URL and return clean Markdown content.
        Uses native Rust (reqwest + scraper + htmd) — no browser, no Python httpx needed.
        url:       HTTP/HTTPS URL to fetch
        max_chars: max characters to return (default 8000 ≈ 2k tokens)
        selector:  optional CSS selector to extract specific element (e.g. 'article', 'main')
        """
        # Use native Rust web.fetch RPC — faster, no Python dependency on httpx/readability
        _params: dict = {"url": url, "max_chars": max_chars}
        if selector:
            _params["selector"] = selector
        return await _sc("web.fetch", _params)
    
    @agent.tool_plain
    async def code_semantic_search(
        query: str,
        top_k: int = 5,
        language: str = "",
        kind: str = "",
        file_filter: str = "",
    ) -> str:
        """
        Semantic search over indexed code function/class bodies using vector similarity.
    
        Unlike shell_grep (string matching), this understands INTENT:
        "find where user input is validated" finds validation logic even if
        the word 'validate' doesn't appear in the code.
    
        Requires `evocli index` to have been run first.
    
        query:       natural language description of what you're looking for
        top_k:       number of results to return (default: 5)
        language:    filter by language, e.g. "rust" "python" "typescript"
        kind:        filter by kind, e.g. "function" "class"
        file_filter: only return results from files containing this substring
    
        Examples:
          code_semantic_search("user authentication and token validation")
          code_semantic_search("database connection pool setup", language="python")
          code_semantic_search("error handling for network requests", top_k=3)
          code_semantic_search("parse config file", file_filter="config")
        """
        try:
            from evocli_soul.code_chunks import get_index
            import os as _os_ci
            # Use the session project root as project_id (stable invariant set at startup).
            # os.getcwd() was a bug: it changes if any shell command changes the working
            # directory, causing the global _index singleton to scope to the wrong project.
            from evocli_soul.state import get_session_root as _get_proj_root
            idx = get_index(_get_proj_root())
            results = idx.search(
                query,
                top_k=top_k,
                language=language,
                kind=kind,
                file_filter=file_filter,
            )
            if not results:
                return _json.dumps({
                    "query":   query,
                    "results": [],
                    "hint":    "No results. Run 'evocli index' first to build the semantic code index.",
                }, ensure_ascii=False)
            # Format results for readability
            formatted = []
            for r in results:
                formatted.append({
                    "symbol":   r.get("symbol", ""),
                    "file":     r.get("file", ""),
                    "line":     r.get("line_start", 0),
                    "kind":     r.get("kind", ""),
                    "language": r.get("language", ""),
                    "body":     r.get("body", "")[:800],  # cap for context
                    "signature": r.get("signature", ""),
                })
            return _json.dumps({
                "query":   query,
                "count":   len(formatted),
                "results": formatted,
            }, ensure_ascii=False)
        except Exception as e:
            return _json.dumps({"error": str(e), "query": query}, ensure_ascii=False)
    
    # ── GitNexus-inspired knowledge graph tools ──────────────────────
    @agent.tool_plain
    async def generate_community_summaries(max_communities: int = 20) -> str:
        """
        Generate LLM summaries for each code community (GraphRAG capability).
    
        After indexing ('evocli index'), this builds a semantic understanding of
        the codebase at the community level. Each community of related functions/classes
        gets a natural language summary stored in project memory.
    
        This enables answering global questions like:
          "How does the authentication system work?"
          "What are the main components of this codebase?"
        without reading every file — the summaries are recalled from memory.
    
        max_communities: number of communities to summarize (default 20)
    
        Run once after major refactors to update the understanding.
        """
        try:
            from evocli_soul.handlers import system as _sys_h
            result_str = await _call_handler(_sys_h.handle_code_generate_community_summaries, {
                "max_communities": max_communities,
            })
            import json as _csj
            result = _csj.loads(result_str) if isinstance(result_str, str) else result_str
            if isinstance(result, dict):
                count = result.get("summaries_count", 0)
                return _json.dumps({
                    "ok":      True,
                    "message": f"Generated {count} community summaries. Now stored in project memory for future queries.",
                    "count":   count,
                }, ensure_ascii=False)
            return _json.dumps(result, ensure_ascii=False)
        except Exception as e:
            return _json.dumps({"error": str(e)}, ensure_ascii=False)
    
    @agent.tool_plain
    async def code_hybrid_search(query: str, limit: int = 10) -> str:
        """
        Hybrid BM25 + vector search (GitNexus-style query tool).
        Better than regular search: combines BM25 keyword precision (tantivy)
        with semantic similarity (LanceDB), merged via Reciprocal Rank Fusion.
        Use as primary code search.
        """
        # Architecture fix: hybrid_search needs Python LanceDB (vector) + Rust tantivy (BM25).
        # Rust tool_dispatch has code_intel.bm25_search; vector search is Python-only.
        # Implement RRF merge inline to avoid the broken bridge→Rust→Unknown chain.
        import evocli_soul.state as _st
        top_k = limit * 2
        # BM25 from Rust tantivy (Rust tool_dispatch has this)
        bm25_hits: list = []
        try:
            bm25_raw = await bridge.call("code_intel.bm25_search", {"query": query, "limit": top_k})
            bm25_hits = bm25_raw.get("results", []) if isinstance(bm25_raw, dict) else []
        except Exception as e:
            _log.debug("BM25 search failed (non-fatal): %s", e)
        # Vector search from Python LanceDB
        vec_hits: list = []
        try:
            memory = _st.get_memory()
            vec_raw = memory.search(query, top_k=top_k)
            for i, item in enumerate(vec_raw[:top_k]):
                vec_hits.append({
                    "symbol_id": item.get("id", item.get("title", "")),
                    "name":      item.get("title", ""),
                    "file":      item.get("body", "")[:50],
                    "rank":      i + 1,
                })
        except Exception as e:
            _log.debug("Vector search failed (non-fatal): %s", e)
        # RRF merge (K=60, standard GitNexus approach)
        K = 60.0
        scores: dict = {}
        meta:   dict = {}
        for r in bm25_hits:
            sid = r.get("symbol_id", "")
            scores[sid] = scores.get(sid, 0) + 1.0 / (K + r.get("rank", 99))
            meta[sid] = {"name": r.get("name",""), "kind": r.get("kind",""), "file": r.get("file","")}
        for r in vec_hits:
            sid = r.get("symbol_id", "")
            scores[sid] = scores.get(sid, 0) + 1.0 / (K + r.get("rank", 99))
            if sid not in meta:
                meta[sid] = {"name": r.get("name",""), "file": r.get("file","")}
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:limit]
        results = [{"symbol_id": sid, "rrf_score": round(sc, 4), **meta.get(sid, {})} for sid, sc in ranked]
        return _json.dumps({"query": query, "results": results, "count": len(results)}, ensure_ascii=False)
    
    @agent.tool_plain
    async def code_blast_radius(symbol_id: str, max_depth: int = 5) -> str:
        """
        Blast radius / impact analysis (GitNexus impact tool).
        Shows ALL callers (upstream) and callees (downstream) with risk level.
        Use BEFORE modifying a symbol to understand full impact.
        """
        return await _sc("code_intel.blast_radius", {"symbol_id": symbol_id, "max_depth": max_depth})
    
    @agent.tool_plain
    async def code_symbol_context(symbol_id: str) -> str:
        """
        360° symbol context (GitNexus context tool).
        Returns callers, callees, community membership, process participation.
        """
        return await _sc("code_intel.symbol_context", {"symbol_id": symbol_id})
    
    @agent.tool_plain
    async def code_communities() -> str:
        """
        List functional code communities (GitNexus communities).
        Communities are groups of related symbols detected by graph analysis.
        Use to understand codebase high-level structure.
        """
        return await _sc("code_intel.communities", {})
    
    @agent.tool_plain
    async def mcp_call(tool_name: str, arguments_json: str = "{}") -> str:
        """
        Call an external MCP (Model Context Protocol) tool registered via `evocli mcp connect`.
        Use this when you need capabilities from external MCP servers (filesystem, git, databases, APIs).
        
        Before calling, use mcp_list_tools() to see what tools are available.
        tool_name: The exact MCP tool name (e.g. "mcp_filesystem_read_file")
        arguments_json: JSON string of arguments matching the tool's schema
        """
        import json as _json
        from evocli_soul.handlers.mcp_bridge import call_mcp_tool, _mcp_tools
        if not _mcp_tools:
            return "No MCP tools loaded. Register a server: evocli mcp connect <name> <program> [args]"
        try:
            args = _json.loads(arguments_json) if isinstance(arguments_json, str) else arguments_json
            result = await call_mcp_tool(tool_name, args)
            return _json.dumps(result, ensure_ascii=False) if not isinstance(result, str) else result
        except Exception as e:
            return f"MCP tool error: {e}"
    
    @agent.tool_plain
    async def mcp_list_tools() -> str:
        """
        List all available MCP tools from registered external servers.
        Returns tool names, server names, and descriptions.
        Use this to discover what external capabilities are available.
        """
        import json as _json
        from evocli_soul.handlers.mcp_bridge import _mcp_tools, load_mcp_config
        if not _mcp_tools:
            servers = load_mcp_config()
            if not servers:
                return "No MCP servers registered. Add one: evocli mcp connect <name> <program> [args...]"
            return f"MCP tools loading in background ({len(servers)} server(s) registered). Retry in a moment."
        tools = [
            {"name": k, "server": v["server"], "description": v["description"][:100]}
            for k, v in _mcp_tools.items()
        ]
        return _json.dumps({"total": len(tools), "tools": tools}, ensure_ascii=False, indent=2)
    
    @agent.tool_plain
    async def fs_apply_batch(edits_json: str, skip_failed: bool = False) -> str:
        """
        Apply SEARCH/REPLACE edits to multiple files.
    
        Two modes (use skip_failed to choose):
    
        skip_failed=False (default — atomic):
          If ANY edit fails, ALL files are rolled back. Safe for tightly
          coupled changes where partial application would break compilation.
    
        skip_failed=True (partial — recommended for independent files):
          Failed edits are skipped and reported; successful edits are kept.
          Use when files are loosely coupled and partial success is useful.
          Aider pattern: fix failures individually rather than restart everything.
    
        edits_json: JSON array of objects, each with:
          - path: str (file path)
          - search: str (exact code to find)
          - replace: str (replacement code)
        """
        # fs_apply_batch is implemented as a Python-native tool in agent_executor.py.
        # Build a minimal proxy that satisfies all attributes agent_executor._execute_tool needs:
        # self.bridge, self.read_only, self._session_id, self.config, self._TOOL_TO_RPC
        import json as _fbj
        from evocli_soul.agent_executor import AgentExecutorMixin
        from evocli_soul.agent_tool_selector import AgentToolSelectorMixin

        class _BatchProxy(AgentToolSelectorMixin, AgentExecutorMixin):
            """Minimal proxy that lets us call _execute_tool('fs_apply_batch', ...) without
            instantiating a full EvoCLIAgent (no LLM init, no tool registration needed)."""
            def __init__(self, b, sid):
                self.bridge        = b
                self.read_only     = False
                self._session_id   = sid
                self.config        = {}
                self.memory        = None
                self._current_query = ""
                self._selected_tool_names: frozenset = frozenset()
                # Minimal _TOOL_TO_RPC so that _execute_tool can find fs_apply_batch
                # AgentToolSelectorMixin defines the full _TOOL_TO_RPC as a class attr

        proxy = _BatchProxy(bridge, _sid)
        try:
            result = await proxy._execute_tool("fs_apply_batch", {
                "edits_json":  edits_json,
                "skip_failed": skip_failed,
            })
            return result if isinstance(result, str) else _fbj.dumps(result, ensure_ascii=False)
        except Exception as e:
            return _fbj.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
    
    # ══════════════════════════════════════════════════════════════════════
    # Task planning tools — OpenCode TodoWrite pattern
    # Enables the AI to create a visible task plan before multi-step work.
    # The plan is stored per-session and displayed in the TUI as progress.
    # ══════════════════════════════════════════════════════════════════════
    
    @agent.tool_plain
    async def todo_write(todos_json: str) -> str:
        """Write or update the task plan for the current session.
    
        ALWAYS use this at the start of any multi-step task (3+ steps) to create
        a visible work plan before starting. Update status as you progress.
    
        todos_json: JSON array of todo items, each with:
          id:       unique identifier (e.g. "1", "2a", "setup")
          content:  clear description of the task step
          status:   "pending" | "in_progress" | "completed" | "cancelled"
          priority: "high" | "medium" | "low"
    
        Example:
          todo_write('[
            {"id":"1","content":"Read auth.rs to understand current auth flow","status":"pending","priority":"high"},
            {"id":"2","content":"Locate the JWT validation bug","status":"pending","priority":"high"},
            {"id":"3","content":"Apply fix with fs_apply_search_replace","status":"pending","priority":"medium"},
            {"id":"4","content":"Run tests to verify fix","status":"pending","priority":"medium"}
          ]')
    
        Call again with updated statuses as work progresses:
          todo_write('[{"id":"1","content":"...","status":"completed","priority":"high"}, ...]')
        """
        import json as _tj
        from evocli_soul import state as _ts
        from evocli_soul.rpc import emit_event as _te
        try:
            todos = _tj.loads(todos_json) if isinstance(todos_json, str) else todos_json
            if not isinstance(todos, list):
                return _tj.dumps({"ok": False, "error": "todos_json must be a JSON array"}, ensure_ascii=False)
    
            # Normalize items
            normalized = []
            for item in todos:
                normalized.append({
                    "id":       str(item.get("id", len(normalized) + 1)),
                    "content":  str(item.get("content", "")),
                    "status":   item.get("status", "pending"),
                    "priority": item.get("priority", "medium"),
                })
            _ts.set_todos(normalized, _sid)
    
            # Build human-readable display
            status_icons = {"pending": "⬜", "in_progress": "🔄", "completed": "✅", "cancelled": "❌"}
            priority_icons = {"high": "🔴", "medium": "🟡", "low": "🟢"}
            lines = [f"📋 **任务计划** ({len(normalized)} 项)"]
            for t in normalized:
                icon = status_icons.get(t["status"], "⬜")
                pri  = priority_icons.get(t["priority"], "")
                lines.append(f"{icon} {pri} {t['content']}")
            display = "\n".join(lines)
    
            # Emit event so TUI can surface the plan
            await _te("todo_update", {"todos": normalized, "display": display})
            return _tj.dumps({"ok": True, "count": len(normalized), "display": display}, ensure_ascii=False)
        except Exception as e:
            return _tj.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
    
    @agent.tool_plain
    async def todo_read() -> str:
        """Read the current task plan. Check progress and see what's left to do.
    
        Returns the current todo list with status of each item.
        Use this to:
        - Check which tasks are remaining before declaring work complete
        - Resume an interrupted multi-step task
        - Show the user what has been accomplished
        """
        import json as _tr
        from evocli_soul import state as _ts
        todos = _ts.get_todos(_sid)
        if not todos:
            return _tr.dumps({
                "todos": [],
                "hint": "No task plan yet. Use todo_write to create one for multi-step tasks.",
            }, ensure_ascii=False)
        pending   = [t for t in todos if t.get("status") == "pending"]
        in_prog   = [t for t in todos if t.get("status") == "in_progress"]
        completed = [t for t in todos if t.get("status") == "completed"]
        return _tr.dumps({
            "todos":     todos,
            "summary":   f"{len(completed)} done, {len(in_prog)} in progress, {len(pending)} pending",
            "remaining": len(pending) + len(in_prog),
        }, ensure_ascii=False)
    
    # ══════════════════════════════════════════════════════════════════════
    # Task completion signal — Cline attempt_completion / Gemini complete_task
    # The AI MUST call this to exit the autonomous loop. The loop only ends
    # when this tool is called (or max iterations reached).
    # ══════════════════════════════════════════════════════════════════════
    
    @agent.tool_plain
    async def task_complete(result: str, command: str = "") -> str:
        """Signal that the current task is fully complete.
    
        ONLY call this when ALL of the following are true:
        1. Every item in your todo_read list is marked "completed"
        2. Code changes have been verified (lint passed, tests passed)
        3. You can provide a clear summary of what was accomplished
    
        This is the ONLY way to exit autonomous execution mode.
        If you call this without completing the work, you will be asked to re-verify.
    
        result:  Clear summary of what was accomplished (shown to user)
        command: Optional verification command to run automatically
                 e.g. "cargo test", "npm test", "python -m pytest", "cargo check"
                 Leave empty if no automated tests exist.
    
        Example:
          task_complete(
            result="Added JWT authentication to the API. Created auth.rs with token generation,
                    modified routes.rs to add /login endpoint, updated Cargo.toml with jsonwebtoken dep.",
            command="cargo test"
          )
        """
        import json as _tc_j
        from evocli_soul import state as _tc_st
    
        # ── Cline double-check pattern ─────────────────────────────────────
        # First attempt_completion → rejected. AI must re-verify its work.
        # Second attempt_completion → accepted (after explicit self-audit).
        if not _tc_st.is_task_double_checked(_sid):
            _tc_st.mark_task_double_checked(_sid)
            # Also check todos
            todos = _tc_st.get_todos(_sid)
            incomplete = [t for t in todos if t.get("status") not in ("completed", "cancelled")]
            if incomplete:
                items = "\n".join(f"  - [{t.get('id','')}] {t.get('content','')}" for t in incomplete)
                return _tc_j.dumps({
                    "ok": False,
                    "re_verify": True,
                    "message": (
                        f"⚠️ Re-verify required before completing.\n\n"
                        f"The following todo items are NOT yet completed:\n{items}\n\n"
                        f"Complete all items, verify the changes work, then call task_complete again."
                    ),
                }, ensure_ascii=False)
            return _tc_j.dumps({
                "ok": False,
                "re_verify": True,
                "message": (
                    "⚠️ Before declaring done, perform a final self-audit:\n"
                    "1. Read through every file you modified\n"
                    "2. Run tests or lint if possible\n"
                    "3. Confirm the original task requirements are fully met\n\n"
                    "If everything checks out, call task_complete again with your summary."
                ),
            }, ensure_ascii=False)
    
        # ── Second call: accepted ──────────────────────────────────────────
        _tc_st.set_task_complete(_sid, result, command)
        # Mark all pending todos as completed
        todos = _tc_st.get_todos(_sid)
        if todos:
            for t in todos:
                if t.get("status") == "pending":
                    t["status"] = "completed"
            _tc_st.set_todos(todos, _sid)
    
        # Emit event so TUI shows completion badge
        try:
            from evocli_soul.rpc import emit_event as _tc_ev
            await _tc_ev("task_complete", {"result": result, "command": command})
        except Exception:
            pass
    
        return _tc_j.dumps({
            "ok":      True,
            "signal":  "task_complete",
            "message": "✅ Task complete signal accepted. Finalizing…",
        }, ensure_ascii=False)
    
    # ══════════════════════════════════════════════════════════════════════
    # Subagent delegation — Claude Code "Task" tool pattern
    # Spawn an independent sub-agent for a bounded subtask.
    # Use when a subtask is complex enough to warrant independent execution.
    # ══════════════════════════════════════════════════════════════════════
    
    @agent.tool_plain
    async def spawn_agent(task: str, context: str = "", model: str = "fast") -> str:
        """
        Delegate a complex subtask to an independent sub-agent.
    
        Use this when a subtask is:
        - Large enough to be independent (writing all tests, documenting all APIs)
        - Clearly bounded (specific files, specific goal)
        - Parallelizable with other work
    
        IMPORTANT: The sub-agent works autonomously and returns a summary.
        You are responsible for integrating its output back into the main task.
    
        task:    Clear, self-contained description of the subtask
        context: Relevant files, requirements, constraints for the sub-agent
        model:   "fast" (default) or "smart" (for complex reasoning)
    
        Examples:
          spawn_agent("Write pytest tests for auth.py", context="File: auth.py\\n<content>")
          spawn_agent("Document all public functions in utils.rs", context="Path: src/utils.rs")
          spawn_agent("Refactor error handling in service layer", context="Files: service/*.py")
        """
        import json as _sa_j
        # WorkerAgent was consolidated into EvoCLIAgent — use it directly.
        # The WorkerPool is for multi-task batching; spawn_agent just needs a single sub-agent.
        try:
            from evocli_soul.agent import EvoCLIAgent
            import evocli_soul.state as _sa_st
            _sa_bridge  = _sa_st.get_bridge()
            _sa_cfg     = _sa_st.get_config()
            _sa_memory  = _sa_st.get_memory_if_ready()
            _sub_sid    = f"{_sid}_sub_{hash(task) & 0xFFFF:04x}"

            full_task = f"{task}\n\n{context}" if context else task

            from evocli_soul.rpc import emit_event as _sa_ev
            await _sa_ev("soul_status", {"status": "loading", "message": f"Sub-agent: {task[:50]}..."})

            sub_agent = EvoCLIAgent(
                _sa_bridge, _sa_memory, _sa_cfg,
                session_id=_sub_sid,
            )
            result = await sub_agent.run(full_task)

            if not result:
                result = "Sub-agent completed with no output."

            # Track in session for memory — pass session_id for concurrent session safety
            _sa_st.append_session_event({
                "type": "spawn_agent",
                "task": task[:200],
                "result_len": len(result),
            }, session_id=_sid)

            return _sa_j.dumps({
                "ok":       True,
                "task":     task[:100],
                "result":   result[:3000],
                "truncated": len(result) > 3000,
            }, ensure_ascii=False)

        except Exception as e:
            return _sa_j.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
    
    # ══════════════════════════════════════════════════════════════════════
    # Shell convenience tools — pydantic-ai parity with LiteLLM path
    # ══════════════════════════════════════════════════════════════════════
    

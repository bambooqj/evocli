"""
agent_tools_fs.py — File system and core tools registration
Part 1 of agent_tools.py (atomized per 500-line limit).
Contains: fs_read, fs_write, fs_apply_*, shell_run, shell_grep,
         search_code, symbol_lookup, memory_recall/write, git_*, diff_parse_stats
"""
from __future__ import annotations

def register(agent, _sc, _call_handler, _sid, _json, bridge=None, config=None, memory=None):
    """Register FS and core tools on agent."""

    async def fs_read(path: str) -> str:
        """Read the full contents of a file. path: absolute or relative file path."""
        return await _sc("fs.read", {"path": path})
    
    @agent.tool_plain
    async def fs_write(path: str, content: str) -> str:
        """Write (or overwrite) a file with the given content.
    
        IMPORTANT — Prior-read enforcement (Cline pattern):
        You MUST call fs_read or fs_read_range on this file BEFORE writing it,
        unless you are creating a brand-new file that doesn't exist yet.
        Writing over a file you haven't read may destroy important code.
    
        For editing existing files, PREFER fs_apply_search_replace which:
        - Only modifies specific sections (safer)
        - Automatically verifies the target text exists
        - Prevents accidentally overwriting the whole file
    
        Use fs_write only for:
        - Creating entirely new files
        - Replacing a small, fully-read file
        """
        # Prior-read enforcement: warn if writing a file not read this session
        try:
            import evocli_soul.state as _st_pr
            files_read = _st_pr.get_files_read_this_session(_sid)
            # Normalize path for comparison
            import os as _os_pr
            from evocli_soul.state import get_session_root as _gsr_pr
            abs_path = path if _os_pr.path.isabs(path) else _os_pr.path.join(_gsr_pr(), path)
            already_read = any(
                _os_pr.path.abspath(r) == _os_pr.path.abspath(abs_path)
                for r in files_read
            ) or path in files_read
            # Only warn if the file actually exists (new files are always OK to write)
            if not already_read and _os_pr.path.exists(abs_path):
                # Return warning but allow the write (advisory, not blocking)
                # Blocking would be too disruptive for autonomous execution
                _warn = (
                    f"⚠️ Prior-read warning: '{path}' exists but was not read this session. "
                    f"Writing without reading may overwrite important code. "
                    f"Consider using fs_read_range first to verify content."
                )
                # Log but proceed - user autonomy over safety in autonomous mode
                import logging as _log_pr
                _log_pr.getLogger("evocli.agent").warning("prior-read skipped for %s", path)
                # Prepend warning to result so AI sees it
                result = await _sc("fs.write", {"path": path, "content": content})
                import json as _pw_j
                try:
                    r = _pw_j.loads(result) if isinstance(result, str) else result
                    if isinstance(r, dict):
                        r["prior_read_warning"] = _warn
                        return _pw_j.dumps(r, ensure_ascii=False)
                except Exception:
                    pass
                return result
        except Exception:
            pass  # Never block write on prior-read check failure
        return await _sc("fs.write", {"path": path, "content": content})
    
    @agent.tool_plain
    async def fs_apply_diff(path: str, diff: str, dry_run: bool = False) -> str:
        """Apply a unified diff patch to a file. Set dry_run=True to preview only."""
        return await _sc("fs.apply_diff", {"path": path, "diff": diff, "dry_run": dry_run})
    
    @agent.tool_plain
    async def shell_run(cmd: str, cwd: str = "", timeout_s: int = 0) -> str:
        """Run a whitelisted shell command. Returns stdout+stderr.

        cmd:       command to run (whitelisted by Rust security layer)
        cwd:       working directory (default: project root where evocli was launched)
        timeout_s: timeout in seconds (0 = use config default: shell.timeout_s)

        The default cwd is always the project root, not the current process directory.
        Pass an explicit relative path (e.g. "src/") to run in a subdirectory.
        """
        from evocli_soul.state import get_session_root as _gsr
        from evocli_soul.config_defaults import cfg_int
        _cwd = cwd if cwd else _gsr()
        _timeout = timeout_s if timeout_s > 0 else cfg_int("shell.timeout_s")
        return await _sc("shell.run", {"cmd": cmd, "cwd": _cwd, "timeout_s": _timeout, "dry_run": False})

    @agent.tool_plain
    async def shell_grep(
        pattern: str,
        path: str = ".",
        include: str = "",
        exclude: str = "",
        case_sensitive: bool = False,
        context_lines: int = 0,
        max_results: int = 0,
    ) -> str:
        """Search for a pattern in files. Pure Rust — cross-platform, no grep binary needed.

        pattern:        text to search for (substring match)
        path:           directory to search in (default: current dir)
        include:        filter by file extension, e.g. ".rs" "*.py" ".toml"
        exclude:        skip paths containing this string, e.g. "target" "dist"
        case_sensitive: case-sensitive search (default: false)
        context_lines:  N lines of context before/after each match (default: 0)
        max_results:    maximum matches to return (0 = use config default: shell.max_results)
    
        Examples:
          shell_grep("fn main")                         — find 'fn main' in all code files
          shell_grep("TODO", include=".rs")             — find TODOs in Rust files only
          shell_grep("import", include=".py", context_lines=2)
          shell_grep("ERROR", path="logs/", max_results=50)
        """
        from evocli_soul.config_defaults import cfg_int
        _max = max_results if max_results > 0 else cfg_int("shell.max_results")
        return await _sc("shell.grep", {
            "pattern":        pattern,
            "path":           path,
            "include":        include,
            "exclude":        exclude,
            "case_sensitive": case_sensitive,
            "context_lines":  context_lines,
            "max_results":    _max,
        })
    
    @agent.tool_plain
    async def search_code(query: str, path: str = ".") -> str:
        """Semantic / regex search across the codebase."""
        return await _sc("search.code", {"query": query, "path": path})
    
    @agent.tool_plain
    async def symbol_lookup(name: str) -> str:
        """Look up a symbol's exact definition, file, and line in the codebase."""
        return await _sc("symbol.lookup", {"name": name})
    
    @agent.tool_plain
    async def memory_recall(query: str, top_k: int = 5) -> str:
        """Search project memory for context relevant to the query."""
        # Fix H1: 直接调用 Python LanceDB（统一存储，避免 Rust SQLite 孤岛）
        from evocli_soul import state as _state
        memory = _state.get_memory()
        results = memory.search(query, top_k=int(top_k))
        return _json.dumps(results, ensure_ascii=False)
    
    @agent.tool_plain
    async def memory_write(title: str, body: str) -> str:
        """Save a note, decision, or lesson to project memory."""
        # Fix H1: 直接写入 Python LanceDB，与 smart_add/distill 统一存储
        from evocli_soul import state as _state
        from evocli_soul.memory_router import get_memory_router
        from evocli_soul.handlers.metrics import _classify_with_model
    
        content = f"{title}\n{body}" if body else title
        if not content or not content.strip():
            return _json.dumps({"ok": False, "reason": "empty content"}, ensure_ascii=False)
    
        memory = _state.get_memory()
        router = get_memory_router()
        recent = memory.get_all(limit=20)
        should, rule_type, rule_importance = router.should_memorize(content, recent)
    
        if not should:
            return _json.dumps({"ok": False, "reason": "not worth memorizing"}, ensure_ascii=False)
    
        # ML 分类器优先（与 handle_memory_smart_add 逻辑一致）
        ml_result = _classify_with_model(content)
        if ml_result and ml_result.get("confidence", 0) >= 0.6:
            mem_type   = ml_result["label"]
            importance = float(ml_result.get("importance", rule_importance))
        else:
            mem_type   = rule_type
            importance = rule_importance
    
        mid = memory.add(content, memory_type=mem_type, priority="project", importance=importance)
        return _json.dumps({"ok": True, "id": mid, "memory_type": mem_type}, ensure_ascii=False)
    
    @agent.tool_plain
    async def git_status() -> str:
        """Get the current git working tree status."""
        return await _sc("git.status", {})
    
    @agent.tool_plain
    async def git_diff(
        path: str = "",
        staged: bool | None = None,
        stat: bool = False,
        base: str = "",
    ) -> str:
        """Show git diff with flexible options.
    
        path:   specific file path to diff (empty = whole working tree)
        staged: True = only staged changes; False = only unstaged; None = both (default)
        stat:   True = show summary (files changed, insertions, deletions) instead of full diff
        base:   compare against a branch or commit, e.g. "main", "origin/main", "abc123"
    
        Examples:
          git_diff()                           — both staged and unstaged (default)
          git_diff(staged=True)               — only staged changes
          git_diff(path="src/main.rs")        — diff specific file
          git_diff(stat=True)                 — show summary: N files changed, +X -Y
          git_diff(base="main")               — compare current branch vs main
          git_diff(path="src/", staged=False) — unstaged changes in src/ directory
        """
        params: dict = {}
        if path:   params["path"]   = path
        if stat:   params["stat"]   = True
        if base:   params["base"]   = base
        if staged is not None: params["staged"] = staged
        return await _sc("git.diff", params)
    
    @agent.tool_plain
    async def git_commit(message: str) -> str:
        """Commit current changes to git with the given message."""
        return await _sc("git.commit", {"message": message, "files": []})
    
    @agent.tool_plain
    async def diff_parse_stats(diff: str) -> str:
        """
        Parse a unified diff and return statistics: files_changed, lines_added, lines_removed.
        Use this to validate an LLM-generated patch before applying it with fs_apply_diff.
        """
        # Architecture fix: diff.parse_stats is a Soul-side Python operation (uses whatthepatch).
        # Call Python implementation directly — do NOT route via bridge→Rust (Rust has no arm for this).
        try:
            import importlib.util as _iu
            if _iu.find_spec("whatthepatch"):
                import whatthepatch
                changes = list(whatthepatch.parse_patch(diff))
                files   = len(changes)
                # whatthepatch change tuple: (old_lineno, new_lineno, text)
                # added line:   old=None, new=N  → c[0] is None
                # removed line: old=N, new=None  → c[1] is None  ← Oracle fix: was c[0]
                added   = sum(
                    sum(1 for c in ch.changes if c[0] is None)
                    for ch in changes if ch.changes
                )
                removed = sum(
                    sum(1 for c in ch.changes if c[1] is None)
                    for ch in changes if ch.changes
                )
            else:
                # Pure-regex fallback (no external library needed)
                import re as _re
                added   = len(_re.findall(r'^\+(?!\+\+)', diff, _re.MULTILINE))
                removed = len(_re.findall(r'^-(?!--)', diff, _re.MULTILINE))
                files   = len(_re.findall(r'^diff --git ', diff, _re.MULTILINE)) or \
                          len(_re.findall(r'^--- ', diff, _re.MULTILINE))
            return _json.dumps({
                "files_changed": files,
                "lines_added":   added,
                "lines_removed": removed,
                "valid":         files > 0,
            }, ensure_ascii=False)
        except Exception as e:
            return _json.dumps({"error": str(e), "valid": False}, ensure_ascii=False)
    
    @agent.tool_plain
    async def fs_apply_search_replace(path: str, search: str, replace: str) -> str:
        """
        Apply a SEARCH/REPLACE block to a file using multi-strategy matching.
        PREFERRED over fs_apply_diff for LLM-generated edits — more reliable because
        it does not require exact line numbers. Uses 5-strategy fallback (Aider/OpenCode pattern).
        Format: search=exact code to find, replace=new code to substitute.
        If the SEARCH block appears multiple times, you will get back the line numbers
        of all matches — add more surrounding context lines to uniquely identify the target.
        """
        try:
            content = await bridge.call("fs.read", {"path": path})
            if not isinstance(content, str):
                return _json.dumps({"ok": False, "error": f"Could not read: {path}"}, ensure_ascii=False)
            from evocli_soul.edit_engine import apply_search_replace, AmbiguousSearchError
            try:
                new_content, strategy = apply_search_replace(content, search, replace)
                await bridge.call("fs.write", {"path": path, "content": new_content})
                return _json.dumps({"ok": True, "strategy": strategy}, ensure_ascii=False)
            except AmbiguousSearchError as amb:
                # Return structured feedback — LLM must add more context and retry
                feedback = amb.to_ai_feedback()
                return _json.dumps({
                    "ok": False, "ambiguous": True,
                    "match_count": amb.match_count,
                    "match_lines": amb.match_line_numbers,
                    "error": feedback,
                    "reflection_prompt": (
                        f"Ambiguous SEARCH block matched {amb.match_count} locations at lines "
                        f"{amb.match_line_numbers}. Add more surrounding context lines to your "
                        f"search pattern to uniquely identify the target. {feedback}"
                    ),
                }, ensure_ascii=False)
        except ValueError as e:
            # Include reflection_prompt so the reflection loop in _run_litellm can inject
            # this failure back into the conversation and trigger a retry.
            error_msg = str(e)
            return _json.dumps({
                "ok": False,
                "strategy": "all_failed",
                "error": error_msg,
                "reflection_prompt": (
                    f"The SEARCH block was not found in {path}. "
                    f"Read the file first with fs_read, then copy the exact text you want to replace "
                    f"as the search parameter. Details: {error_msg[:300]}"
                ),
            }, ensure_ascii=False)
        except Exception as e:
            return _json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
    
    @agent.tool_plain
    async def fs_read_range(path: str, start_line: int = 0, end_line: int = 0) -> str:
        """
        Read a specific line range from a file (1-indexed, inclusive).
    
        PREFER over fs_read for large files (>200 lines). Dramatically reduces
        context usage: reading lines 50-120 of a 2000-line file uses 3% of the tokens.
    
        Args:
          path:       file path — must be a real file path (e.g. "src/main.rs"), NOT a description
          start_line: first line to include (1-indexed). 0 = start of file.
          end_line:   last line to include (1-indexed, inclusive). 0 = end of file.
    
        Returns JSON with: content, start_line, end_line, total_lines, note.
        The 'note' field tells you if there are more lines outside the range.
    
        Examples:
          Read lines 40-80:        fs_read_range("src/auth.rs", 40, 80)
          Read first 60 lines:     fs_read_range("src/auth.rs", 0, 60)
          Read from line 200:      fs_read_range("src/auth.rs", 200, 0)
        """
        params: dict = {"path": path}
        if start_line > 0:
            params["start_line"] = start_line
        if end_line > 0:
            params["end_line"] = end_line
        return await _sc("fs.read_range", params)
    
    @agent.tool_plain
    async def fs_read_symbol(symbol_name: str, path: str = "", context_lines: int = 10) -> str:
        """
        Read the source code of a specific function, class, or symbol by name.
    
        PREFER over fs_read_range when you know the symbol name but not the line number.
        Looks up the symbol in the code index, then reads that section of the file.
    
        Args:
          symbol_name:   exact name (e.g. "authenticate", "UserService", "handle_login")
          path:          optional file path hint to narrow search (leave empty to search all)
          context_lines: how many lines before/after the symbol to include (default 10)
    
        Returns the function/class body with surrounding context.
        Much more efficient than fs_read when you only need one symbol from a large file.
        """
        try:
            # Step 1: Find symbol location via code index
            search_params = {"name": symbol_name}
            if path:
                search_params["file"] = path
            symbols = await bridge.call("symbol.lookup", search_params)
            # Normalize: Rust may return list OR {"found":bool, "symbols":[...]}
            if isinstance(symbols, dict):
                symbols = symbols.get("symbols", []) or []
            if not isinstance(symbols, list) or not symbols:
                # Fallback: text search
                grep_result = await bridge.call("shell.grep", {
                    "pattern": rf"\b{symbol_name}\b",
                    "path": path or ".",
                })
                return _json.dumps({
                    "symbol":  symbol_name,
                    "found":   False,
                    "fallback": str(grep_result)[:1000],
                    "note": "Symbol not in index — showing grep results. Run 'evocli index' for better results.",
                }, ensure_ascii=False)
    
            sym = symbols[0]
            sym_file = sym.get("file", path)
            sym_line = int(sym.get("line", 0))
    
            if not sym_file or sym_line == 0:
                return _json.dumps({"symbol": symbol_name, "found": False,
                                    "error": "Symbol found but no file/line info"}, ensure_ascii=False)
    
            # Step 2: Read the file section around the symbol
            start = max(1, sym_line - context_lines)
            end   = sym_line + 80 + context_lines  # 80 lines covers most functions
            range_result = await bridge.call("fs.read_range", {
                "path":       sym_file,
                "start_line": start,
                "end_line":   end,
            })
            if isinstance(range_result, dict):
                range_result["symbol"] = symbol_name
                range_result["symbol_line"] = sym_line
                range_result["symbol_kind"] = sym.get("kind", "unknown")
                return _json.dumps(range_result, ensure_ascii=False)
            return str(range_result)
        except Exception as e:
            return _json.dumps({"symbol": symbol_name, "error": str(e)}, ensure_ascii=False)
    
    @agent.tool_plain
    async def fs_lint_file(path: str) -> str:
        """
        Run a linter on a file after making edits. Returns errors with line numbers.
        Use this AFTER fs_apply_search_replace or fs_apply_diff to validate your changes.
        If it returns errors, fix them before declaring the task done (Aider reflection loop).
        """
        # Architecture fix: uses bridge.call("shell.run") via Rust security layer.
        from pathlib import Path as _Path
        ext = _Path(path).suffix.lower()
        lang_cmds = {".py": f"python -m py_compile {path}", ".rs": "cargo check --message-format short 2>&1"}
        cmd = lang_cmds.get(ext)
        if not cmd:
            return f"✓ No built-in linter for {ext} — skipped."
        try:
            result = await bridge.call("shell.run", {"cmd": cmd, "cwd": ".", "timeout_s": 30, "dry_run": False})
            stdout    = result.get("stdout", "") if isinstance(result, dict) else str(result)
            stderr    = result.get("stderr", "") if isinstance(result, dict) else ""
            exit_code = result.get("exit_code", 0) if isinstance(result, dict) else 0
            output    = (stdout + "\n" + stderr).strip()
            passed    = (exit_code == 0)
            if passed:
                return f"✓ Lint passed for {path}."
            # GAP-1 (pydantic-ai path): Return plain-text error so LLM clearly sees
            # the failure and MUST respond with a fix. JSON with 'reflection_prompt'
            # is ambiguous to LLM — plain text is unambiguous.
            return (
                f"✗ Lint FAILED for {path}:\n"
                f"```\n{output[:800]}\n```\n"
                f"You MUST fix these errors before declaring the task done."
            )
        except Exception as e:
            return f"✗ Lint error for {path}: {e}"
    
    @agent.tool_plain
    async def run_and_capture(cmd: str, cwd: str = "") -> str:
        """
        Run a shell command and return the output. Use for: running tests, building,
        checking output. Research: Aider's /run command — executes and adds to context.
        Output is also stored for @terminal mention context injection.
        cwd: working directory (default: project root)
        """
        import json as _j
        from evocli_soul.state import get_session_root as _gsr_rc
        _cwd = cwd if cwd else _gsr_rc()
        raw = await _sc("shell.run", {"cmd": cmd, "cwd": _cwd, "timeout_s": 60, "dry_run": False})
        try:
            result = _j.loads(raw) if raw.startswith("{") else {"stdout": raw}
        except Exception:
            result = {"stdout": raw}
        stdout    = result.get("stdout", "") if isinstance(result, dict) else str(result)
        stderr    = result.get("stderr", "") if isinstance(result, dict) else ""
        exit_code = result.get("exit_code", 0) if isinstance(result, dict) else 0
        output    = (stdout + "\n" + stderr).strip()
        # Store for @terminal mention — user can reference last terminal output in next message
        try:
            from evocli_soul.state import set_terminal_output
            set_terminal_output(f"$ {cmd}\n{output}")
        except Exception:
            pass
        return _json.dumps({"ok": True, "output": output[:2000], "exit_code": exit_code, "cmd": cmd}, ensure_ascii=False)
    
    @agent.tool_plain
    async def test_and_capture(cmd: str, cwd: str = "") -> str:
        """
        Run tests and return output ONLY if they fail (saves tokens on passing tests).
        Research: Aider's /test command — reflection loop for test-driven development.
        Use after code changes to verify correctness.
        cwd: working directory (default: project root)
        """
        import json as _j
        from evocli_soul.state import get_session_root as _gsr_tc
        _cwd = cwd if cwd else _gsr_tc()
        raw = await _sc("shell.run", {"cmd": cmd, "cwd": _cwd, "timeout_s": 120, "dry_run": False})
        try:
            result = _j.loads(raw) if raw.startswith("{") else {"stdout": raw}
        except Exception:
            result = {"stdout": raw}
        stdout    = result.get("stdout", "") if isinstance(result, dict) else str(result)
        stderr    = result.get("stderr", "") if isinstance(result, dict) else ""
        exit_code = result.get("exit_code", 0) if isinstance(result, dict) else 0
        passed    = (exit_code == 0)
        output    = (stdout + "\n" + stderr).strip()
        if passed:
            return "✓ All tests passed."
        # GAP-1 (pydantic-ai path): Plain-text failure forces LLM to fix before declaring done
        return (
            f"✗ Tests FAILED (exit code {exit_code}):\n"
            f"```\n{output[:800]}\n```\n"
            f"You MUST fix these test failures before declaring the task done."
        )
    

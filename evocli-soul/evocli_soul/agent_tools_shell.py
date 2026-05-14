"""
agent_tools_shell.py — Shell convenience and code analysis tools registration
Part 3 of agent_tools.py (atomized per 500-line limit).
Contains: shell_ls/find/cat/head/tail/wc/mkdir/mv/cp/touch, symbol_variants,
         code_intel_*, assume_*, impact_*, verify_*, equiv_*, git_snapshot, misc
"""
from __future__ import annotations

def register(agent, _sc, _call_handler, _sid, _json, bridge=None, config=None, memory=None):
    """Register shell and analysis tools on agent."""
    # _ca: code_analysis handlers module — needed for assume_*, impact_*, verify_*, equiv_* tools.
    # Import inside function to avoid circular imports at module level.
    from evocli_soul.handlers import code_analysis as _ca  # noqa: F841

    @agent.tool_plain
    async def shell_ls(
        path: str = ".",
        long_format: bool = False,
        tree: bool = False,
        depth: int = 1,
        show_hidden: bool = False,
    ) -> str:
        """List directory contents. Supports tree view and recursive depth control.
    
        path:        directory to list (default: current directory)
        long_format: include file sizes and type info (default: False)
        tree:        render as ASCII tree showing directory hierarchy (default: False)
        depth:       recursion depth — 1=flat (default), 2=one level deep, 0=unlimited
        show_hidden: include hidden files/dirs starting with '.' (default: False)
    
        Examples:
          shell_ls(".")                              — flat listing of current dir
          shell_ls("src", tree=True)                 — ASCII tree of src/
          shell_ls(".", depth=2, long_format=True)   — recursive 2 levels with sizes
          shell_ls(".", depth=0)                     — full recursive listing
        """
        # Uses Rust std::fs::read_dir — cross-platform, no system shell required.
        return await _sc("shell.ls", {
            "path":        path,
            "long":        long_format,
            "tree":        tree,
            "depth":       depth,
            "show_hidden": show_hidden,
        })
    
    @agent.tool_plain
    async def shell_find(
        path: str = ".",
        pattern: str = "",
        extension: str = "",
        type: str = "",
        depth: int = 0,
        exclude: str = "",
        case_sensitive: bool = False,
        max_results: int = 200,
    ) -> str:
        """Find files and directories. Pure Rust walkdir — cross-platform.
    
        path:           directory to search in (default: current dir)
        pattern:        filename substring to match (empty = all)
        extension:      filter by extension, e.g. "rs" ".py" "*.toml"
        type:           "file" | "dir" | "" (both, default)
        depth:          max recursion depth (0 = unlimited, default)
        exclude:        skip paths containing this substring, e.g. "target"
        case_sensitive: case-sensitive name matching (default: false)
        max_results:    maximum results to return (default: 200)
    
        Examples:
          shell_find(extension="rs")               — all Rust files recursively
          shell_find(pattern="config", type="file")— files with 'config' in name
          shell_find(type="dir", depth=2)          — subdirectories up to 2 levels
          shell_find(extension="py", exclude="test")— Python files not in test dirs
        """
        return await _sc("shell.find", {
            "path":           path,
            "name":           pattern,
            "extension":      extension,
            "type":           type,
            "depth":          depth,
            "exclude":        exclude,
            "case_sensitive": case_sensitive,
            "max_results":    max_results,
        })
    
    @agent.tool_plain
    async def shell_cat(path: str) -> str:
        """Read file contents. Uses Rust std::fs — cross-platform. Prefer fs_read for code files."""
        return await _sc("shell.cat", {"file": path})
    
    @agent.tool_plain
    async def shell_head(path: str, lines: int = 20) -> str:
        """Read the first N lines of a file. Uses Rust — cross-platform."""
        return await _sc("shell.head", {"file": path, "n": lines})
    
    @agent.tool_plain
    async def shell_tail(path: str, lines: int = 20) -> str:
        """Read the last N lines of a file. Uses Rust — cross-platform."""
        return await _sc("shell.tail", {"file": path, "n": lines})
    
    @agent.tool_plain
    async def shell_wc(path: str) -> str:
        """Count lines, words, and characters in a file. Uses Rust — cross-platform."""
        return await _sc("shell.wc", {"file": path})
    
    @agent.tool_plain
    async def shell_mkdir(path: str) -> str:
        """Create a directory (and parents) recursively. Uses Rust std::fs::create_dir_all — cross-platform."""
        return await _sc("shell.mkdir", {"path": path})
    
    @agent.tool_plain
    async def shell_mv(src: str, dst: str) -> str:
        """Move or rename a file or directory. Uses Rust std::fs::rename — cross-platform."""
        return await _sc("shell.mv", {"src": src, "dst": dst})
    
    @agent.tool_plain
    async def shell_cp(src: str, dst: str) -> str:
        """Copy a file or directory. Uses Rust std::fs::copy — cross-platform."""
        return await _sc("shell.cp", {"src": src, "dst": dst})
    
    @agent.tool_plain
    async def shell_touch(path: str) -> str:
        """Create an empty file or update its timestamp. Uses Rust std::fs::OpenOptions — cross-platform."""
        return await _sc("shell.touch", {"file": path})
    
    # ══════════════════════════════════════════════════════════════════════
    # Symbol & code intelligence tools
    # ══════════════════════════════════════════════════════════════════════
    
    @agent.tool_plain
    async def symbol_variants(type_name: str) -> str:
        """Find all variants or implementations of a type (e.g. enum variants, trait impls)."""
        return await _sc("symbol.variants", {"type_name": type_name})
    
    @agent.tool_plain
    async def symbol_usages(symbol_id: str, limit: int = 20) -> str:
        """Find all call sites and usages of a symbol across the codebase."""
        return await _call_handler(_ca.handle_symbol_usages, {"symbol_id": symbol_id, "limit": limit})
    
    @agent.tool_plain
    async def code_intel_list_symbols(path: str) -> str:
        """List all symbols (functions, structs, classes) defined in a file."""
        return await _sc("code_intel.list_symbols", {"file": path})
    
    @agent.tool_plain
    async def code_intel_incoming_calls(symbol_id: str) -> str:
        """List functions that directly call a given symbol (direct callers)."""
        return await _sc("code_intel.incoming_calls", {"symbol_id": symbol_id})
    
    @agent.tool_plain
    async def code_intel_outgoing_calls(symbol_id: str) -> str:
        """List functions that a given symbol calls (callees)."""
        return await _sc("code_intel.outgoing_calls", {"symbol_id": symbol_id})
    
    # ══════════════════════════════════════════════════════════════════════
    # Assumption verifiers — run BEFORE modifying shared/complex code
    # ══════════════════════════════════════════════════════════════════════
    
    @agent.tool_plain
    async def assume_has_tests(symbol: str) -> str:
        """Check if a function or class has test coverage."""
        return await _call_handler(_ca.handle_assume_has_tests, {"symbol": symbol})
    
    @agent.tool_plain
    async def assume_is_pure(symbol: str) -> str:
        """Check if a function is pure (no side effects, deterministic output)."""
        return await _call_handler(_ca.handle_assume_is_pure, {"symbol": symbol})
    
    @agent.tool_plain
    async def assume_caller_count(symbol: str) -> str:
        """Count how many places call a given symbol (helps assess change risk)."""
        return await _call_handler(_ca.handle_assume_caller_count, {"symbol": symbol})
    
    @agent.tool_plain
    async def assume_has_side_effects(symbol: str) -> str:
        """Check if a function has observable side effects (I/O, mutation, etc.)."""
        return await _call_handler(_ca.handle_assume_has_side_effects, {"symbol": symbol})
    
    @agent.tool_plain
    async def assume_verify(assumption: str, subject: str) -> str:
        """Verify a natural language assumption about a code element.
        assumption: what you believe to be true. subject: symbol or file being tested."""
        return await _call_handler(_ca.handle_assume_verify, {"assumption": assumption, "subject": subject})
    
    @agent.tool_plain
    async def assume_is_deprecated(symbol: str) -> str:
        """Check if a symbol is deprecated or has a recommended replacement."""
        return await _call_handler(_ca.handle_assume_is_deprecated, {"symbol": symbol})
    
    @agent.tool_plain
    async def assume_is_only_caller(caller: str, target: str) -> str:
        """Check if a given caller is the only place that calls a target symbol."""
        return await _call_handler(_ca.handle_assume_is_only_caller, {"caller": caller, "target": target})
    
    @agent.tool_plain
    async def assume_types_match(symbol_a: str, symbol_b: str) -> str:
        """Check if two type signatures are compatible for a substitution."""
        return await _call_handler(_ca.handle_assume_types_match, {"symbol_a": symbol_a, "symbol_b": symbol_b})
    
    # ══════════════════════════════════════════════════════════════════════
    # Impact analysis — use BEFORE modifying widely-used symbols
    # ══════════════════════════════════════════════════════════════════════
    
    @agent.tool_plain
    async def impact_check(symbol: str, change_type: str = "behavior") -> str:
        """Check the full impact radius of modifying a symbol.
        change_type: 'behavior' | 'signature' | 'delete'"""
        return await _call_handler(_ca.handle_impact_check, {"symbol": symbol, "change_type": change_type})
    
    @agent.tool_plain
    async def impact_affected_tests(symbol: str) -> str:
        """List all tests that would be affected by changing a symbol."""
        return await _call_handler(_ca.handle_impact_affected_tests, {"symbol": symbol})
    
    @agent.tool_plain
    async def impact_batch_check(symbols_json: str) -> str:
        """Batch impact check for multiple symbols at once (JSON array of symbol names)."""
        import json as _ibj
        symbols_list = _ibj.loads(symbols_json) if isinstance(symbols_json, str) else symbols_json
        return await _call_handler(_ca.handle_impact_batch_check, {"symbols": symbols_list})
    
    # ══════════════════════════════════════════════════════════════════════
    # Verification tools
    # ══════════════════════════════════════════════════════════════════════
    
    @agent.tool_plain
    async def verify_task(task_id: str) -> str:
        """Verify that a task contract has been completed as specified."""
        return await _call_handler(_ca.handle_verify_task, {"contract_id": task_id})
    
    @agent.tool_plain
    async def verify_coverage(symbol: str) -> str:
        """Verify that test coverage for a symbol meets the required threshold."""
        return await _call_handler(_ca.handle_verify_coverage, {"contract_id": symbol})
    
    @agent.tool_plain
    async def verify_drift(spec: str) -> str:
        """Check if the implementation has drifted from the original specification."""
        return await _call_handler(_ca.handle_verify_drift, {"contract_id": spec, "use_llm": True})
    
    # ══════════════════════════════════════════════════════════════════════
    # Equivalence search
    # ══════════════════════════════════════════════════════════════════════
    
    @agent.tool_plain
    async def equiv_find(intent: str, limit: int = 5) -> str:
        """Find existing implementations that match a described intent (avoid re-inventing)."""
        return await _call_handler(_ca.handle_equiv_find, {"intent": intent, "limit": limit})
    
    @agent.tool_plain
    async def equiv_find_similar_code(code: str, limit: int = 5) -> str:
        """Find code snippets semantically similar to a given code block."""
        return await _call_handler(_ca.handle_equiv_find_similar_code, {"code": code, "limit": limit})
    
    # ══════════════════════════════════════════════════════════════════════
    # Git safety tools
    # ══════════════════════════════════════════════════════════════════════
    
    @agent.tool_plain
    async def git_snapshot() -> str:
        """Create a git stash snapshot for safe rollback BEFORE risky changes."""
        return await _sc("git.snapshot", {})
    
    @agent.tool_plain
    async def git_restore(ref: str = "") -> str:
        """Restore files from a git snapshot (pass stash ref or leave empty for latest)."""
        return await _sc("git.restore", {"stash_ref": ref} if ref else {})
    
    # ══════════════════════════════════════════════════════════════════════
    # System tools
    # ══════════════════════════════════════════════════════════════════════
    
    @agent.tool_plain
    async def approval_request(action: str, reason: str = "") -> str:
        """Request user confirmation before a risky or irreversible operation.
        action: what you are about to do. reason: why it is necessary."""
        return await _sc("approval.request", {"action": action, "message": reason})
    
    @agent.tool_plain
    async def memory_constraints() -> str:
        """Retrieve all active constraints and rules for this project."""
        # python-native: LanceDB
        try:
            import evocli_soul.state as _st_mc
            memory = _st_mc.get_memory()
            constraints = memory.get_constraints() if hasattr(memory, "get_constraints") else []
            import json as _j
            return _j.dumps({"constraints": constraints}, ensure_ascii=False)
        except Exception as e:
            return f"Error: {e}"
    
    @agent.tool_plain
    async def tool_list_user() -> str:
        """List all user-registered custom tools available for this project."""
        return await _sc("tool.list_user", {})
    
    @agent.tool_plain
    async def tool_run_user(name: str, args: str = "") -> str:
        """Run a user-registered custom tool by name."""
        return await _sc("tool.run_user", {"name": name, "args": args, "dry_run": False})
    

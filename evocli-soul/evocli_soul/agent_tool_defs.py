"""agent_tool_defs.py - Tool definition builders mixin
Extracted from agent.py.
Single responsibility: build OpenAI-format tool schemas for LiteLLM fallback.
"""
from __future__ import annotations
import logging
log = logging.getLogger('evocli.agent.defs')


class AgentToolDefsMixin:
    """Mixin: _build_tool_definitions and _all_tool_definitions for EvoCLIAgent."""

    def _build_tool_definitions(self) -> list[dict]:
        """OpenAI function calling format tool definitions（LLM 可见的工具列表）。
        
        ToolRouter 接入点：
          - 如果 _selected_tool_names 非空（已通过 select_tools 选择），
            只返回选中工具的 schema（节省 ~55% token）
          - 如果 _selected_tool_names 为空（降级/首次调用），返回全部
          - LiteLLM 路径上限：MAX_TOOLS_LITELLM=20
        """
        _all_defs = self._all_tool_definitions()
        
        # 路由过滤（来自 _select_tools_for_request）
        selected = self._selected_tool_names
        if selected:
            filtered = [d for d in _all_defs if d.get("function", {}).get("name") in selected]
            if filtered:
                log.debug("_build_tool_definitions: %d/%d tools (ToolRouter filtered)",
                          len(filtered), len(_all_defs))
                return filtered
        
        return _all_defs

    def _all_tool_definitions(self) -> list[dict]:
        """完整工具 schema 列表（不受路由过滤）。供 _build_tool_definitions 调用。"""
        return [
            # ── Core tools ─────────────────────────────────────────────
            {"type": "function", "function": {"name": "fs_read", "description": "Read file contents. For files >200 lines, prefer fs_read_range to save context.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
            {"type": "function", "function": {
                "name": "fs_read_range",
                "description": (
                    "Read a specific line range from a file (1-indexed, inclusive). "
                    "PREFER over fs_read for large files — reading lines 50-120 of a 2000-line file "
                    "uses only 3% of the tokens. Use when you know roughly where the relevant code is."
                ),
                "parameters": {"type": "object", "properties": {
                    "path":       {"type": "string", "description": "File path"},
                    "start_line": {"type": "integer", "description": "First line (1-indexed). Omit or 0 = start of file."},
                    "end_line":   {"type": "integer", "description": "Last line inclusive (1-indexed). Omit or 0 = end of file."},
                }, "required": ["path"]},
            }},
            {"type": "function", "function": {
                "name": "fs_read_symbol",
                "description": (
                    "Read the source code of a specific function/class/symbol by name. "
                    "PREFER over fs_read_range when you know the symbol name. "
                    "Much faster than reading entire files — finds the symbol via code index "
                    "and returns just that section with surrounding context."
                ),
                "parameters": {"type": "object", "properties": {
                    "symbol_name":   {"type": "string", "description": "Function/class/variable name to find"},
                    "path":          {"type": "string", "description": "Optional file path hint to narrow search"},
                    "context_lines": {"type": "integer", "description": "Lines of context around the symbol (default 10)"},
                }, "required": ["symbol_name"]},
            }},
            {"type": "function", "function": {"name": "fs_apply_diff", "description": "Apply unified diff to a file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "diff": {"type": "string"}, "dry_run": {"type": "boolean"}}, "required": ["path", "diff"]}}},
            {"type": "function", "function": {"name": "shell_run", "description": "Run a shell command (restricted whitelist)", "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}, "cwd": {"type": "string"}, "timeout_s": {"type": "integer"}}, "required": ["cmd"]}}},
            {"type": "function", "function": {"name": "memory_recall", "description": "Search memory for relevant context", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "top_k": {"type": "integer"}}, "required": ["query"]}}},
            {"type": "function", "function": {"name": "memory_write", "description": "Write a note to memory", "parameters": {"type": "object", "properties": {"title": {"type": "string"}, "body": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}}, "required": ["title", "body"]}}},
            {"type": "function", "function": {"name": "git_status", "description": "Get git status of current repo", "parameters": {"type": "object", "properties": {}}}},
            {"type": "function", "function": {"name": "git_commit", "description": "Commit changes to git", "parameters": {"type": "object", "properties": {"message": {"type": "string"}, "files": {"type": "array", "items": {"type": "string"}}}, "required": ["message"]}}},
            {"type": "function", "function": {"name": "git_snapshot", "description": "Create a git stash snapshot for rollback safety", "parameters": {"type": "object", "properties": {}}}},
            {"type": "function", "function": {"name": "search_code", "description": "Search codebase for a pattern", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "path": {"type": "string"}}, "required": ["query"]}}},
            # ── Symbol Oracle (Section 17.1) ────────────────────────────
            {"type": "function", "function": {"name": "symbol_lookup", "description": "Look up a symbol's exact signature and location in the codebase", "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}},
            {"type": "function", "function": {"name": "symbol_variants", "description": "Get all variants/implementations of a type or enum", "parameters": {"type": "object", "properties": {"type_name": {"type": "string"}}, "required": ["type_name"]}}},
            {"type": "function", "function": {"name": "symbol_usages", "description": "Find all places where a symbol is used", "parameters": {"type": "object", "properties": {"symbol_id": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["symbol_id"]}}},
            # ── Assumption Verifier (Section 17.2) ──────────────────────
            {"type": "function", "function": {"name": "assume_has_tests", "description": "Check if a symbol/function has test coverage", "parameters": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}}},
            {"type": "function", "function": {"name": "assume_caller_count", "description": "Count how many places call a given symbol", "parameters": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}}},
            {"type": "function", "function": {"name": "assume_is_pure", "description": "Check if a function is pure (no side effects)", "parameters": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}}},
            {"type": "function", "function": {"name": "assume_has_side_effects", "description": "Check what side effects a function has", "parameters": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}}},
            {"type": "function", "function": {"name": "assume_verify", "description": "Verify a natural-language assumption about code (e.g. 'X only has 1 caller')", "parameters": {"type": "object", "properties": {"assumption": {"type": "string"}, "subject": {"type": "string"}}, "required": ["assumption", "subject"]}}},
            # ── Impact Probe (Section 17.3) ─────────────────────────────
            {"type": "function", "function": {"name": "impact_check", "description": "Check the impact radius of modifying a symbol (callers, risk level CRITICAL/HIGH/MEDIUM/LOW)", "parameters": {"type": "object", "properties": {"symbol": {"type": "string"}, "change_type": {"type": "string", "enum": ["behavior", "signature", "delete"]}}, "required": ["symbol"]}}},
            {"type": "function", "function": {"name": "impact_affected_tests", "description": "Find which test files would be affected by changing a symbol", "parameters": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}}},
            # ── Equivalent Finder (Section 17.4) ────────────────────────
            {"type": "function", "function": {"name": "equiv_find", "description": "Find existing code that does something similar — avoid reinventing the wheel", "parameters": {"type": "object", "properties": {"intent": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["intent"]}}},
            {"type": "function", "function": {"name": "equiv_check_deps", "description": "Check if existing dependencies already provide a needed capability", "parameters": {"type": "object", "properties": {"intent": {"type": "string"}}, "required": ["intent"]}}},
            # ── Task Contract Verifier (Section 18) ─────────────────────
            {"type": "function", "function": {"name": "verify_task", "description": "Check completion percentage of a task contract", "parameters": {"type": "object", "properties": {"contract_id": {"type": "string"}, "run_tests": {"type": "boolean"}}, "required": ["contract_id"]}}},
            {"type": "function", "function": {"name": "verify_coverage", "description": "List done vs pending checkpoints for a task contract", "parameters": {"type": "object", "properties": {"contract_id": {"type": "string"}}, "required": ["contract_id"]}}},
            # ── G-05: 新增工具 function definitions ─────────────────────
            # Assume 扩展
            {"type": "function", "function": {"name": "assume_is_deprecated", "description": "Check if a symbol is marked deprecated via #[deprecated], @deprecated or DEPRECATED comment", "parameters": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}}},
            {"type": "function", "function": {"name": "assume_is_only_caller", "description": "Verify whether a given caller is the only place that calls a target symbol", "parameters": {"type": "object", "properties": {"caller": {"type": "string"}, "target": {"type": "string"}}, "required": ["caller", "target"]}}},
            {"type": "function", "function": {"name": "assume_types_match", "description": "Heuristic check whether two symbols are type-compatible (co-located in same files)", "parameters": {"type": "object", "properties": {"symbol_a": {"type": "string"}, "symbol_b": {"type": "string"}}, "required": ["symbol_a", "symbol_b"]}}},
            # Impact 扩展
            {"type": "function", "function": {"name": "impact_batch_check", "description": "Run impact analysis on multiple symbols at once, returns risk level for each", "parameters": {"type": "object", "properties": {"symbols": {"type": "array", "items": {"type": "string"}}, "change_type": {"type": "string", "enum": ["behavior", "signature", "delete"]}}, "required": ["symbols"]}}},
            # 文件系统扩展
            {"type": "function", "function": {"name": "fs_write", "description": "Write (or overwrite) a file with given content", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
            {"type": "function", "function": {"name": "fs_diff", "description": "Compute unified diff between original and modified text", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "original": {"type": "string"}, "modified": {"type": "string"}}, "required": ["path", "original", "modified"]}}},
            # Git 扩展
            {"type": "function", "function": {"name": "git_diff", "description": "Get current working tree diff (unstaged and staged changes)", "parameters": {"type": "object", "properties": {}}}},
            {"type": "function", "function": {"name": "git_shadow_snapshot", "description": "Create a side-git shadow snapshot for safe rollback (does not pollute main git history)", "parameters": {"type": "object", "properties": {"label": {"type": "string"}}}}},
            {"type": "function", "function": {"name": "git_shadow_restore", "description": "Restore workspace from a side-git shadow snapshot", "parameters": {"type": "object", "properties": {"snapshot": {"type": "string"}, "project": {"type": "string"}}, "required": ["snapshot"]}}},
            {"type": "function", "function": {"name": "git_restore", "description": "Restore workspace from a git stash snapshot created by git_snapshot", "parameters": {"type": "object", "properties": {"stash_ref": {"type": "string"}}}}},
            # Code Intel 扩展
            {"type": "function", "function": {"name": "code_intel_full_chain", "description": "Get the full upstream call chain (all callers of callers) for a symbol", "parameters": {"type": "object", "properties": {"symbol_id": {"type": "string"}, "max_depth": {"type": "integer"}}, "required": ["symbol_id"]}}},
            {"type": "function", "function": {"name": "code_intel_full_downstream_chain", "description": "Get the full downstream call chain (all callees of callees) for a symbol", "parameters": {"type": "object", "properties": {"symbol_id": {"type": "string"}, "max_depth": {"type": "integer"}}, "required": ["symbol_id"]}}},
            {"type": "function", "function": {"name": "code_intel_impact_radius", "description": "Get the complete impact radius: all symbols transitively affected by changing this one", "parameters": {"type": "object", "properties": {"symbol_id": {"type": "string"}}, "required": ["symbol_id"]}}},
            {"type": "function", "function": {"name": "code_intel_incoming_calls", "description": "List all direct callers of a symbol", "parameters": {"type": "object", "properties": {"symbol_id": {"type": "string"}}, "required": ["symbol_id"]}}},
            {"type": "function", "function": {"name": "code_intel_outgoing_calls", "description": "List all direct callees of a symbol", "parameters": {"type": "object", "properties": {"symbol_id": {"type": "string"}}, "required": ["symbol_id"]}}},
            {"type": "function", "function": {"name": "code_intel_list_symbols", "description": "List all indexed symbols in the current project", "parameters": {"type": "object", "properties": {}}}},
            {"type": "function", "function": {"name": "code_intel_index_status", "description": "Check the current state of the code intelligence index (symbol count, last updated)", "parameters": {"type": "object", "properties": {}}}},
            {"type": "function", "function": {"name": "code_intel_ranked_context", "description": "Get PageRank-ranked relevant symbols for the current file — use for context-aware code generation", "parameters": {"type": "object", "properties": {"modified_file": {"type": "string"}, "mentioned": {"type": "array", "items": {"type": "string"}}, "limit": {"type": "integer"}}, "required": ["modified_file"]}}},
            {"type": "function", "function": {"name": "symbol_lifecycle", "description": "Get the full git history of a symbol: when it was created, modified, and by whom", "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}},
            {"type": "function", "function": {"name": "equiv_find_similar_code", "description": "Find code snippets semantically similar to a given code fragment", "parameters": {"type": "object", "properties": {"code": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["code"]}}},
            # 安全审批
            {"type": "function", "function": {"name": "approval_request", "description": "Request explicit user approval before executing a high-risk or irreversible operation", "parameters": {"type": "object", "properties": {"skill_id": {"type": "string"}, "step_id": {"type": "string"}, "action": {"type": "string"}, "message": {"type": "string"}}, "required": ["message"]}}},
            # 记忆
            {"type": "function", "function": {"name": "memory_constraints", "description": "Retrieve all active constraints/rules for the current project", "parameters": {"type": "object", "properties": {"project_id": {"type": "string"}}}}},
            # 验证扩展
            {"type": "function", "function": {"name": "verify_drift", "description": "Detect implementation drift: check if recent file changes diverge from contract requirements", "parameters": {"type": "object", "properties": {"contract_id": {"type": "string"}}, "required": ["contract_id"]}}},
            # Shell 内置工具（12 个）
            {"type": "function", "function": {"name": "shell_grep", "description": "Search for a pattern in files (like grep)", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}}, "required": ["pattern"]}}},
            {"type": "function", "function": {"name": "shell_find", "description": "Find files by name pattern in a directory tree", "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "path": {"type": "string"}}}}},
            {"type": "function", "function": {"name": "shell_ls", "description": "List directory contents", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "long": {"type": "boolean"}}}}},
            {"type": "function", "function": {"name": "shell_cat", "description": "Read and return the full contents of a file", "parameters": {"type": "object", "properties": {"file": {"type": "string"}}, "required": ["file"]}}},
            {"type": "function", "function": {"name": "shell_mkdir", "description": "Create a directory (and all parents)", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
            {"type": "function", "function": {"name": "shell_wc", "description": "Count lines, words and characters in a file", "parameters": {"type": "object", "properties": {"file": {"type": "string"}}, "required": ["file"]}}},
            {"type": "function", "function": {"name": "shell_head", "description": "Return first N lines of a file", "parameters": {"type": "object", "properties": {"file": {"type": "string"}, "n": {"type": "integer"}}, "required": ["file"]}}},
            {"type": "function", "function": {"name": "shell_tail", "description": "Return last N lines of a file", "parameters": {"type": "object", "properties": {"file": {"type": "string"}, "n": {"type": "integer"}}, "required": ["file"]}}},
            {"type": "function", "function": {"name": "shell_mv", "description": "Move or rename a file or directory", "parameters": {"type": "object", "properties": {"src": {"type": "string"}, "dst": {"type": "string"}}, "required": ["src", "dst"]}}},
            {"type": "function", "function": {"name": "shell_cp", "description": "Copy a file", "parameters": {"type": "object", "properties": {"src": {"type": "string"}, "dst": {"type": "string"}}, "required": ["src", "dst"]}}},
            {"type": "function", "function": {"name": "shell_rm", "description": "Remove a file or directory", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "recursive": {"type": "boolean"}}, "required": ["path"]}}},
            {"type": "function", "function": {"name": "shell_touch", "description": "Create a file if it does not exist (touch)", "parameters": {"type": "object", "properties": {"file": {"type": "string"}}, "required": ["file"]}}},
            # ── G-09: 用户工具发现
            {"type": "function", "function": {"name": "tool_list_user", "description": "List all user-registered custom tools available in this project", "parameters": {"type": "object", "properties": {}}}},
            {"type": "function", "function": {"name": "tool_run_user", "description": "Execute a user-registered custom tool by name (use tool_list_user to discover available tools)", "parameters": {"type": "object", "properties": {"name": {"type": "string", "description": "Tool name from tool_list_user"}, "args": {"type": "string", "description": "Additional CLI arguments"}, "dry_run": {"type": "boolean"}}, "required": ["name"]}}},
            # ── 研究驱动新工具（Aider/OpenCode 功能差距补齐）────────────────────────────
            # SEARCH/REPLACE (Aider模式 — 比 unified diff 可靠3x，LLM不擅长行号)
            {"type": "function", "function": {"name": "fs_apply_search_replace", "description": "Apply a SEARCH/REPLACE block to a file. PREFERRED over fs_apply_diff for LLM-generated edits — uses 5-strategy multi-replacer for robustness (Aider/OpenCode pattern). search=exact code to find, replace=new code.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "search": {"type": "string", "description": "Exact (or near-exact) code block to find"}, "replace": {"type": "string", "description": "New code to substitute"}}, "required": ["path", "search", "replace"]}}},
            {"type": "function", "function": {"name": "fs_lint_file", "description": "Run a linter on a file after making edits. Returns errors with line numbers. Use AFTER edits to validate changes — part of the Aider reflection loop.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "language": {"type": "string", "description": "python, rust, typescript (auto-detected if omitted)"}}, "required": ["path"]}}},
            # /run /test commands (Aider commands.py)
            {"type": "function", "function": {"name": "run_and_capture", "description": "Run a shell command and return output for analysis. Equivalent to Aider's /run — captures stdout/stderr and returns it.", "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}, "cwd": {"type": "string"}}, "required": ["cmd"]}}},
            {"type": "function", "function": {"name": "test_and_capture", "description": "Run tests and return output ONLY if they fail (saves tokens on passing tests). Equivalent to Aider's /test — use for test-driven development workflow.", "parameters": {"type": "object", "properties": {"cmd": {"type": "string", "description": "Test command: cargo test, pytest, npm test, etc."}, "cwd": {"type": "string"}}, "required": ["cmd"]}}},
            # MCP tools bridge (Section P3-2): dispatch to external MCP servers
            {"type": "function", "function": {
                "name": "mcp_list_tools",
                "description": "List all available external MCP tools from registered servers (registered via 'evocli mcp connect'). Call this first to discover what external capabilities are available.",
                "parameters": {"type": "object", "properties": {}}}},
            {"type": "function", "function": {
                "name": "mcp_call",
                "description": "Call an external MCP tool from a registered MCP server. Use mcp_list_tools() first to discover available tools and their schemas.",
                "parameters": {"type": "object", "properties": {
                    "tool_name": {"type": "string", "description": "Full MCP tool key (e.g. mcp_filesystem_read_file)"},
                    "arguments_json": {"type": "string", "description": "JSON string of arguments matching the tool's input schema"},
                }, "required": ["tool_name"]}}},
            # ── GAP-6: Atomic multi-file batch edit with in-memory rollback ──────
            {"type": "function", "function": {
                "name": "fs_apply_batch",
                "description": (
                    "Apply SEARCH/REPLACE edits to multiple files atomically. "
                    "If ANY edit fails, ALL files are instantly restored from in-memory originals — no data loss. "
                    "PREFER over calling fs_apply_search_replace multiple times when changing related files together. "
                    "Aider-style transactional safety without git dependency."
                ),
                "parameters": {"type": "object", "properties": {
                    "edits_json": {
                        "type": "string",
                        "description": (
                            'JSON array of edits: [{"path":"src/lib.rs","search":"old code","replace":"new code"}, ...]'
                        ),
                    }
                }, "required": ["edits_json"]},
            }},
        ]
    

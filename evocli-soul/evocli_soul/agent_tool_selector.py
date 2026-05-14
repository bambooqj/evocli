"""agent_tool_selector.py - Tool selection and diff preview mixin
Extracted from agent.py.
Single responsibility: select tool subset per request, preview risky diffs.
"""
from __future__ import annotations
import logging
log = logging.getLogger('evocli.agent.selector')


class AgentToolSelectorMixin:
    """Mixin: tool selection and diff preview for EvoCLIAgent."""

    def _select_tools_for_request(self, user_input: str) -> frozenset[str]:
        """
        为本次请求选择工具子集。更新 self._selected_tool_names。
        
        来源：
          - tool_router.select_tools()（3阶段：keyword→tag→embedding）
          - 记忆加权：ToolScoreStore 自动加权历史成功工具
          - 降级：tool_router 不可用时返回全部 pydantic-ai 工具名
        
        副作用：更新 self._selected_tool_names（prepare hook 读取此值）
        """
        try:
            from evocli_soul.tool_router import get_tool_names_for_llm, auto_classify_unknown
            from evocli_soul.tool_registry import PYDANTIC_TOOL_NAMES, REGISTRY_BY_NAME

            # 自动分类本 agent 中已注册但未在 REGISTRY 中的工具
            # （防止开发者忘记添加 ToolSpec 导致工具消失）
            for tool_name in list(PYDANTIC_TOOL_NAMES):
                if tool_name not in REGISTRY_BY_NAME:
                    # 从 @agent.tool_plain 的函数获取 docstring
                    if self._agent is not None:
                        try:
                            func = getattr(self._agent, tool_name, None)
                            doc  = (func.__doc__ or "") if func else ""
                        except Exception:
                            doc = ""
                    else:
                        doc = ""
                    auto_classify_unknown(tool_name, doc)

            names = get_tool_names_for_llm(
                user_input,
                pydantic_only=True,
                config=self.config,
            )
            self._selected_tool_names = names
            log.info("ToolRouter: %d tools selected for '%s...'",
                     len(names), user_input[:50])
            return names
        except Exception as e:
            log.debug("ToolRouter unavailable, using all tools (non-fatal): %s", e)
            # 降级：全部 pydantic-ai 工具
            try:
                from evocli_soul.tool_registry import PYDANTIC_TOOL_NAMES
                self._selected_tool_names = PYDANTIC_TOOL_NAMES
            except Exception:
                self._selected_tool_names = frozenset()
            return self._selected_tool_names

    _TOOL_TO_RPC = {
        "fs_read":       ("fs.read",       lambda args: {"path": args["path"]}),
        "fs_read_range": ("fs.read_range", lambda args: {
            "path":       args["path"],
            **({} if not args.get("start_line") else {"start_line": args["start_line"]}),
            **({} if not args.get("end_line")   else {"end_line":   args["end_line"]}),
        }),
        "fs_apply_diff": ("fs.apply_diff", lambda args: args),
        "shell_run":     ("shell.run",     lambda args: {"cmd": args["cmd"], "cwd": args.get("cwd", "."), "timeout_s": args.get("timeout_s", 30), "dry_run": False}),
        "git_status":    ("git.status",    lambda _: {}),
        "git_commit":    ("git.commit",    lambda args: args),
        "git_snapshot":  ("git.snapshot",  lambda _: {}),
        "git_restore":   ("git.restore",   lambda args: args),
        "search_code":   ("search.code",   lambda args: {"query": args["query"], "path": args.get("path", ".")}),
        # Section 17: Symbol Oracle（全部暴露给 LLM）
        "symbol_lookup":    ("symbol.lookup",    lambda args: {"name": args["name"]}),
        "symbol_variants":  ("symbol.variants",  lambda args: {"type_name": args["type_name"]}),
        "symbol_usages":    ("symbol.usages",    lambda args: {"symbol_id": args["symbol_id"], "limit": args.get("limit", 20)}),
        "assume_has_tests": ("assume.has_tests",  lambda args: {"symbol": args["symbol"]}),
        "assume_caller_count": ("assume.caller_count", lambda args: {"symbol": args["symbol"]}),
        "assume_is_pure":   ("assume.is_pure",   lambda args: {"symbol": args["symbol"]}),
        "assume_has_side_effects": ("assume.has_side_effects", lambda args: {"symbol": args["symbol"]}),
        "assume_verify":    ("assume.verify",    lambda args: {"assumption": args["assumption"], "subject": args["subject"]}),
        "impact_check":     ("impact.check",     lambda args: {"symbol": args["symbol"], "change_type": args.get("change_type","behavior")}),
        "impact_affected_tests": ("impact.affected_tests", lambda args: {"symbol": args["symbol"]}),
        "equiv_find":       ("equiv.find",       lambda args: {"intent": args["intent"], "limit": args.get("limit", 5)}),
        "equiv_check_deps": ("equiv.check_deps", lambda args: {"intent": args["intent"]}),
        "equiv_find_similar_code": ("equiv.find_similar_code", lambda args: {"code": args["code"], "limit": args.get("limit", 5)}),
        # Section 16: Code Intelligence（完整工具集）
        "code_intel_index_status":         ("code_intel.index_status",          lambda _: {}),
        "code_intel_full_downstream_chain":("code_intel.full_downstream_chain", lambda args: {"symbol_id": args["symbol_id"], "max_depth": args.get("max_depth", 5)}),
        "code_intel_ranked_context":       ("code_intel.ranked_context",        lambda args: {"modified_file": args["modified_file"], "mentioned": args.get("mentioned", []), "limit": args.get("limit", 20)}),
        # Section 18: Task Contract Verifier
        "verify_task":      ("verify.task",      lambda args: {"contract_id": args.get("contract_id", ""), "run_tests": args.get("run_tests", False)}),
        "verify_coverage":  ("verify.coverage",  lambda args: {"contract_id": args.get("contract_id", "")}),
        # ── G-05: 新增工具 RPC 映射 ──────────────────────────────────
        # Assume 扩展（3 个）
        "assume_is_deprecated":  ("assume.is_deprecated",  lambda args: {"symbol": args["symbol"]}),
        "assume_is_only_caller": ("assume.is_only_caller",  lambda args: {"caller": args["caller"], "target": args["target"]}),
        "assume_types_match":    ("assume.types_match",     lambda args: {"symbol_a": args["symbol_a"], "symbol_b": args["symbol_b"]}),
        # Impact 扩展
        "impact_batch_check":    ("impact.batch_check",    lambda args: {"symbols": args["symbols"], "change_type": args.get("change_type", "behavior")}),
        # 文件系统扩展
        "fs_write":              ("fs.write",               lambda args: {"path": args["path"], "content": args["content"]}),
        "fs_diff":               ("fs.diff",                lambda args: {"path": args["path"], "original": args["original"], "modified": args["modified"]}),
        # Git 扩展
        "git_diff":              ("git.diff",               lambda _: {}),
        "git_shadow_snapshot":   ("git.shadow_snapshot",    lambda args: {"label": args.get("label", "auto")}),
        "git_shadow_restore":    ("git.shadow_restore",     lambda args: {"snapshot": args["snapshot"], "project": args.get("project", ".")}),
        # Code Intel 扩展
        "code_intel_full_chain":     ("code_intel.full_chain",     lambda args: {"symbol_id": args["symbol_id"], "max_depth": args.get("max_depth", 5)}),
        "code_intel_impact_radius":  ("code_intel.impact_radius",  lambda args: {"symbol_id": args["symbol_id"]}),
        "code_intel_incoming_calls": ("code_intel.incoming_calls", lambda args: {"symbol_id": args["symbol_id"]}),
        "code_intel_outgoing_calls": ("code_intel.outgoing_calls", lambda args: {"symbol_id": args["symbol_id"]}),
        "code_intel_list_symbols":   ("code_intel.list_symbols",   lambda args: {"file": args.get("file", ".")}),  # Fix MEDIUM: 支持指定目标文件（之前总传空）
        # Fix MEDIUM: code_intel.find_symbol 存在于 Rust L109 但之前缺失映射
        "code_intel_find_symbol":    ("code_intel.find_symbol",    lambda args: {"query": args["query"]}),
        "symbol_lifecycle":          ("symbol.lifecycle",          lambda args: {"symbol": args["name"]}),  # Fix: Rust reads args["symbol"], not args["name"]
        # 安全审批
        "approval_request":     ("approval.request",     lambda args: {"skill_id": args.get("skill_id", ""), "step_id": args.get("step_id", ""), "action": args.get("action", ""), "message": args.get("message", "请求操作审批")}),
        # 记忆: memory_recall / memory_write / memory_constraints 已改为 Python-native
        # （Fix H1: 统一存储到 Python LanceDB，不再走 Rust SQLite 孤岛）
        # 验证扩展
        "verify_drift":         ("verify.drift",          lambda args: {"contract_id": args.get("contract_id", "")}),
        # ── 研究驱动新工具 (Aider/OpenCode/Claude Code 功能差距补齐) ─────────────
        # Architecture note: fs_apply_search_replace and fs_lint_file are Python-native tools
        # that use bridge.call("fs.read/write/shell.run") internally.
        # In the LiteLLM fallback path, _execute_tool() handles these specially (see below).
        # They are NOT in _TOOL_TO_RPC because bridge.call("fs.apply_search_replace") would
        # fail (Rust doesn't know this method). Instead _execute_tool() calls Python directly.
        # /run /test: call shell.run directly (existing Rust tool)
        "run_and_capture":         ("shell.run",  lambda args: {"cmd": args["cmd"], "cwd": args.get("cwd", "."), "timeout_s": 60, "dry_run": False}),
        "test_and_capture":        ("shell.run",  lambda args: {"cmd": args["cmd"], "cwd": args.get("cwd", "."), "timeout_s": 120, "dry_run": False}),
        # Shell 内置工具（12 个）
        "shell_grep":  ("shell.grep",  lambda args: {"pattern": args["pattern"], "path": args.get("path", ".")}),
        "shell_find":  ("shell.find",  lambda args: {"name": args.get("name", ""), "path": args.get("path", ".")}),
        "shell_ls":    ("shell.ls",    lambda args: {"path": args.get("path", "."), "long": args.get("long", False)}),
        "shell_cat":   ("shell.cat",   lambda args: {"file": args["file"]}),
        "shell_mkdir": ("shell.mkdir", lambda args: {"path": args["path"]}),
        "shell_wc":    ("shell.wc",    lambda args: {"file": args["file"]}),
        "shell_head":  ("shell.head",  lambda args: {"file": args["file"], "n": args.get("n", 10)}),
        "shell_tail":  ("shell.tail",  lambda args: {"file": args["file"], "n": args.get("n", 10)}),
        "shell_mv":    ("shell.mv",    lambda args: {"src": args["src"], "dst": args["dst"]}),
        "shell_cp":    ("shell.cp",    lambda args: {"src": args["src"], "dst": args["dst"]}),
        "shell_rm":    ("shell.rm",    lambda args: {"path": args["path"], "recursive": args.get("recursive", False)}),
        "shell_touch": ("shell.touch", lambda args: {"file": args["file"]}),
        # ── G-09: 用户工具发现 ────────────────────────────────────
        "tool_list_user": ("tool.list_user", lambda _: {}),
        "tool_run_user":  ("tool.run_user",  lambda args: {"name": args["name"], "args": args.get("args", ""), "dry_run": args.get("dry_run", False)}),
    }

    async def _diff_preview_and_confirm(self, tool_name: str, args: dict) -> str:
        """Show a unified diff of proposed changes and wait for user approval.

        Returns 'approved', 'rejected', or 'skipped' (if preview failed).
        Called when config [safety] require_diff_preview = true.
        """
        from evocli_soul.rpc import emit_event
        try:
            # Build preview diff
            if tool_name == "fs_apply_search_replace":
                path    = args.get("path", "")
                search  = args.get("search", "")
                replace = args.get("replace", "")
                if not path or not search:
                    return "skipped"
                original = await self.bridge.call("fs.read", {"path": path})
                if not isinstance(original, str):
                    return "skipped"
                preview = original.replace(search, replace, 1)
                diff_result = await self.bridge.call("fs.diff", {"old": original, "new": preview, "path": path})
                diff_text = str(diff_result) if diff_result else ""
            elif tool_name == "fs_write":
                path = args.get("path", "")
                try:
                    original = await self.bridge.call("fs.read", {"path": path})
                    original = str(original) if isinstance(original, str) else ""
                except Exception as _e:
                    log.debug("event record skipped: %s", _e)
                    original = ""
                diff_result = await self.bridge.call("fs.diff", {
                    "old": original, "new": args.get("content", ""), "path": path
                })
                diff_text = str(diff_result) if diff_result else ""
            else:
                # fs_apply_batch — compute per-file diffs
                import json as _pj
                edits = _pj.loads(args.get("edits_json", "[]"))
                diff_parts = []
                for edit in edits[:3]:  # preview first 3 files
                    try:
                        orig = await self.bridge.call("fs.read", {"path": edit["path"]})
                        if isinstance(orig, str):
                            new = orig.replace(edit["search"], edit["replace"], 1)
                            d = await self.bridge.call("fs.diff", {"old": orig, "new": new, "path": edit["path"]})
                            diff_parts.append(str(d))
                    except Exception as _e:
                        log.debug("event record skipped: %s", _e)
                diff_text = "\n".join(diff_parts)

            if not diff_text or diff_text.strip() == "--- \n+++ \n":
                return "skipped"  # No actual changes

            # Send preview to TUI
            await emit_event("soul_status", {
                "status":  "ready",
                "message": (
                    f"**Proposed changes preview** (require_diff_preview=true):\n"
                    f"```diff\n{diff_text[:2000]}\n```\n"
                    f"Type `yes` to approve, anything else to reject."
                ),
            })

            # Request approval via the standard approval modal
            approved = await self.bridge.request_approval(
                f"Apply changes to {args.get('path', 'files')}? (see diff above)"
            )
            return "approved" if approved else "rejected"

        except Exception as e:
            log.debug("_diff_preview_and_confirm failed (non-fatal): %s", e)
            return "skipped"  # Never block actual edits due to preview failure


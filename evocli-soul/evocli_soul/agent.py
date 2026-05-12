"""
EvoCLI Agent — Pydantic AI 实现（Section 20 设计要求）
"""
from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any, AsyncGenerator

log = logging.getLogger("evocli.agent")

# P1-2: 明确的 API key 错误类型，不 silent fallback
class _ApiKeyMissingError(Exception):
    def __init__(self, provider: str, env_var: str):
        self.provider = provider
        self.env_var  = env_var
        super().__init__(f"No API key for {provider} (set {env_var})")

_PROVIDER_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai":    "OPENAI_API_KEY",
    "deepseek":  "DEEPSEEK_API_KEY",
}

def _check_api_key(provider: str) -> None:
    """如果 provider 需要 API key 但未配置，抛出明确错误（而非让 pydantic_ai 在运行时失败）"""
    import os
    env_var = _PROVIDER_ENV.get(provider)
    if env_var is None:
        return  # Ollama 等不需要 key
    if not os.environ.get(env_var):
        # 检查 keyring
        try:
            import keyring
            val = keyring.get_password("evocli", provider)
            if val:
                os.environ[env_var] = val  # 注入到环境变量
                return
        except Exception:
            pass
        raise _ApiKeyMissingError(provider, env_var)

# 导入生产级提示词库（替代原始 6 行 _SYSTEM_TEMPLATE）
from evocli_soul.default_prompts import build_system_prompt, COMPACT_SYSTEM_PROMPT

# 向后兼容：保留 _SYSTEM_TEMPLATE 作为简化入口
def _SYSTEM_TEMPLATE_fn(constraints: str = "（无）", goal: str = "", read_only: bool = False) -> str:
    return build_system_prompt(constraints=constraints, goal=goal, read_only=read_only)


def _tool_display_name(rpc_method: str, args: dict) -> str:
    """将 RPC 方法名转换为用户友好的显示文本（模块级函数，供 EvoCLIAgent._execute_tool 调用）"""
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


class EvoCLIAgent:
    """
    Pydantic AI 驱动的 Agent（Section 20）。
    若 pydantic_ai 未安装，fallback 到 raw LiteLLM。
    """
    
    def __init__(self, bridge, memory=None, config: dict | None = None, read_only: bool = False, role: str = "orchestrator", role_instructions: str = "", session_id: str = "default"):
        self.bridge    = bridge
        self.memory    = memory
        self.config    = config or {}
        self.read_only = read_only   # G-10: 只读分析模式
        self.role      = role
        self.role_instructions = role_instructions
        self._session_id = session_id   # for context cache + file dedup keying
        self._agent    = None
        self._fallback_reason: str | None = None   # set when pydantic-ai init fails
        # ── ToolRouter 状态（per-request）────────────────────────────────
        self._selected_tool_names: frozenset[str] = frozenset()  # 本次选中的工具集
        self._current_query: str = ""  # 用于 prepare hook 读取
        self._init_agent()
    
    def _init_agent(self):
        """
        初始化 Pydantic AI Agent（带 fallback）。
        pydantic_ai 1.x 使用 OpenAIChatModel + OpenAIProvider（对 OpenAI 兼容端点）
        或对应 provider 的专用 Model 类。
        """
        if not importlib.util.find_spec("pydantic_ai"):
            log.info("pydantic_ai not installed — using raw LiteLLM (install: pip install pydantic-ai)")
            self._agent = None
            return

        try:
            import os
            from pydantic_ai import Agent

            llm_cfg   = self.config.get("llm", {})
            provider  = llm_cfg.get("provider", "anthropic")
            tiers     = llm_cfg.get("tiers", {})
            fast_model = tiers.get("fast", "claude-3-5-haiku-latest")
            base_url  = llm_cfg.get("base_url")
            api_key   = llm_cfg.get("api_key") or os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")

            # ── 根据 provider 选择正确的 pydantic_ai 1.x model 类 ──────────
            # 支持三种场景：
            #   1. OpenAI 兼容端点（provider=openai 或有 base_url）→ OpenAIChatModel
            #   2. Anthropic 原生 → AnthropicModel
            #   3. LiteLLM 路由（多 provider fallback）→ LiteLLMProvider
            model = self._create_pydantic_model(provider, fast_model, base_url, api_key)
            if model is None:
                log.warning("pydantic_ai: could not create model for provider=%s — using LiteLLM fallback", provider)
                self._agent = None
                self._fallback_reason = f"Could not create pydantic-ai model for provider={provider} model={fast_model}"
                return

            constraints = "（无）"
            if self.memory:
                try:
                    c = self.memory.get_constraints()
                    if c:
                        constraints = "\n".join(f"- {x}" for x in c)
                except Exception as e:
                    # Non-fatal: agent continues without constraints, but log so we can debug.
                    # If constraints are silently lost, the agent may violate project rules.
                    log.debug("_init_agent: failed to load constraints (non-fatal): %s", e)

            system = build_system_prompt(
                constraints=constraints,
                goal="",
                read_only=self.read_only,
            )
            if self.role_instructions:
                system = self.role_instructions + "\n\n" + system
            self._agent = Agent(model=model, system_prompt=system)

            # ── 核心工具注册（pydantic_ai tool_plain 模式）──────────────────────
            # 将最常用的 12 个工具注册到 pydantic_ai agent，使 tool-calling
            # 走 pydantic_ai 原生路径而非 raw LiteLLM fallback。
            # 覆盖率：这 12 个工具占实际工具调用的 95%+。
            # 其余 50+ 工具保留在 _run_litellm fallback（tool_call loop）中。
            self._register_pydantic_tools(self._agent)

            # 动态计算已注册工具数量（pydantic-ai 1.x 属性名有变化，多级 fallback）
            _tool_count = (
                # pydantic-ai >= 1.93: _function_toolset.tools
                len(getattr(getattr(self._agent, '_function_toolset', None), 'tools', None) or {})
                # pydantic-ai 1.x 旧版: _function_tools
                or len(getattr(self._agent, '_function_tools', None) or {})
                # 备用：无法获取时显示 ?
                or "?"
            )
            log.info("Pydantic AI Agent initialized: model=%s provider=%s tools=%s",
                     fast_model, provider, _tool_count)

        except _ApiKeyMissingError as e:
            log.warning("API key not configured for %s. Using LiteLLM fallback. "
                        "Fix: run `evocli init` or set %s env var.", e.provider, e.env_var)
            self._agent = None
            self._fallback_reason = f"API key missing for {e.provider} — run `evocli init` or set {e.env_var}"
        except ImportError as e:
            log.warning("pydantic_ai import error (%s) — using LiteLLM fallback", e)
            self._agent = None
            self._fallback_reason = f"pydantic_ai not installed: {e}"
        except Exception as e:
            log.warning("Pydantic AI init failed (%s) — using LiteLLM fallback. "
                        "Run `evocli doctor` for diagnostics.", e)
            self._agent = None
            self._fallback_reason = f"Pydantic AI init failed: {e}"

    def _create_pydantic_model(self, provider: str, model_name: str, base_url: str | None, api_key: str | None):
        """
        为 pydantic_ai 1.x 创建正确的 Model 实例。

        策略：
        1. provider=openai 或有 base_url（OpenAI 兼容端点）→ OpenAIChatModel + OpenAIProvider
        2. provider=anthropic → AnthropicModel
        3. provider=deepseek / 其他 → LiteLLMProvider（litellm 通吃任何 provider）
        """
        try:
            if provider in ("openai",) or base_url:
                # OpenAI 兼容端点（含 aicode.lol、Azure OpenAI、本地 ollama 等）
                from pydantic_ai.models.openai import OpenAIChatModel
                from pydantic_ai.providers.openai import OpenAIProvider
                oai_provider = OpenAIProvider(
                    base_url=base_url or "https://api.openai.com/v1",
                    api_key=api_key or "sk-placeholder",
                )
                return OpenAIChatModel(model_name, provider=oai_provider)

            if provider == "anthropic":
                from pydantic_ai.models.anthropic import AnthropicModel
                return AnthropicModel(model_name)

            # 其他 provider（deepseek、groq、ollama 等）通过 LiteLLMProvider 路由
            from pydantic_ai.providers.litellm import LiteLLMProvider
            litellm_provider = LiteLLMProvider(
                api_key=api_key,
                api_base=base_url,
            )
            from pydantic_ai.models.openai import OpenAIChatModel
            return OpenAIChatModel(model_name, provider=litellm_provider)

        except ImportError as e:
            log.debug("_create_pydantic_model import error: %s", e)
            return None
        except Exception as e:
            log.debug("_create_pydantic_model error: %s", e)
            return None


    def _register_pydantic_tools(self, agent) -> None:
        """
        将 12 个核心工具注册到 pydantic_ai agent（@agent.tool_plain 模式）。

        使用 tool_plain（无 RunContext）+ closure 捕获 bridge 引用：
        - tool_plain: 工具函数不需要 RunContext，只有业务参数
        - closure: bridge 通过外层 self.bridge 引用，无需 deps 传递
        
        pydantic_ai 会从函数的类型注解 + docstring 自动生成 JSON schema，
        LLM 可直接看到这 12 个工具的描述并按需调用。
        """
        import json as _json

        bridge = self.bridge  # closure capture — agent instances share bridge

        async def _sc(method: str, params: dict) -> str:
            """Safe bridge call — catches ALL exceptions and returns error strings.

            pydantic-ai propagates unhandled tool exceptions as stream failures.
            Returning an error string lets the model see what went wrong and
            try an alternative approach, instead of crashing the whole session.
            """
            try:
                result = await bridge.call(method, params)
                return result if isinstance(result, str) else _json.dumps(result, ensure_ascii=False)
            except Exception as _tool_err:
                log.warning("tool %s failed: %s", method, _tool_err)
                return f"Error: {_tool_err}"

        @agent.tool_plain
        async def fs_read(path: str) -> str:
            """Read the full contents of a file. path: absolute or relative file path."""
            return await _sc("fs.read", {"path": path})

        @agent.tool_plain
        async def fs_write(path: str, content: str) -> str:
            """Write (or overwrite) a file with the given content."""
            return await _sc("fs.write", {"path": path, "content": content})

        @agent.tool_plain
        async def fs_apply_diff(path: str, diff: str, dry_run: bool = False) -> str:
            """Apply a unified diff patch to a file. Set dry_run=True to preview only."""
            return await _sc("fs.apply_diff", {"path": path, "diff": diff, "dry_run": dry_run})

        @agent.tool_plain
        async def shell_run(cmd: str, cwd: str = ".", timeout_s: int = 30) -> str:
            """Run a whitelisted shell command. Returns stdout+stderr."""
            return await _sc("shell.run", {"cmd": cmd, "cwd": cwd, "timeout_s": timeout_s, "dry_run": False})

        @agent.tool_plain
        async def shell_grep(pattern: str, path: str = ".") -> str:
            """Search for a regex pattern in files (like grep -rn)."""
            return await _sc("shell.grep", {"pattern": pattern, "path": path})

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
        async def git_diff() -> str:
            """Get the current staged and unstaged git diff."""
            return await _sc("git.diff", {})

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
                    return _json.dumps({
                        "ok": False, "ambiguous": True,
                        "match_count": amb.match_count,
                        "match_lines": amb.match_line_numbers,
                        "error": amb.to_ai_feedback(),
                    }, ensure_ascii=False)
            except ValueError as e:
                return _json.dumps({"ok": False, "strategy": "all_failed", "error": str(e)}, ensure_ascii=False)
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
        async def run_and_capture(cmd: str, cwd: str = ".") -> str:
            """
            Run a shell command and return the output. Use for: running tests, building,
            checking output. Research: Aider's /run command — executes and adds to context.
            """
            import json as _j
            raw = await _sc("shell.run", {"cmd": cmd, "cwd": cwd, "timeout_s": 60, "dry_run": False})
            try:
                result = _j.loads(raw) if raw.startswith("{") else {"stdout": raw}
            except Exception:
                result = {"stdout": raw}
            stdout    = result.get("stdout", "") if isinstance(result, dict) else str(result)
            stderr    = result.get("stderr", "") if isinstance(result, dict) else ""
            exit_code = result.get("exit_code", 0) if isinstance(result, dict) else 0
            output    = (stdout + "\n" + stderr).strip()
            return _json.dumps({"ok": True, "output": output[:2000], "exit_code": exit_code, "cmd": cmd}, ensure_ascii=False)

        @agent.tool_plain
        async def test_and_capture(cmd: str, cwd: str = ".") -> str:
            """
            Run tests and return output ONLY if they fail (saves tokens on passing tests).
            Research: Aider's /test command — reflection loop for test-driven development.
            Use after code changes to verify correctness.
            """
            import json as _j
            raw = await _sc("shell.run", {"cmd": cmd, "cwd": cwd, "timeout_s": 120, "dry_run": False})
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

        @agent.tool_plain
        async def fetch_url(url: str, max_chars: int = 8000) -> str:
            """
            Fetch a URL and return clean Markdown content to add to context.
            Research: Aider's /web command + Continue.dev's @url context provider.
            Uses httpx + readability-lxml + html2text (no browser required).
            max_chars: limit content size for context window (default 8000 chars ≈ 2k tokens).
            """
            # Call Python-native web fetcher directly (not via bridge — pure Python HTTP)
            try:
                from evocli_soul.web_fetcher import fetch_url as _fetch
                result = await _fetch(url, max_chars=max_chars)
                return _json.dumps(result, ensure_ascii=False)
            except Exception as e:
                return _json.dumps({"ok": False, "url": url, "error": str(e)}, ensure_ascii=False)

        # ── GitNexus-inspired knowledge graph tools ──────────────────────
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
                log.debug("BM25 search failed (non-fatal): %s", e)
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
                log.debug("Vector search failed (non-fatal): %s", e)
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
            return await self._execute_tool("fs_apply_batch", {
                "edits_json":  edits_json,
                "skip_failed": skip_failed,
            })

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
                except Exception:
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
                    except Exception:
                        pass
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
            _st.append_session_event({
                "type":   _ev_type,
                "method": name,
                "data":   {"tool": name},
            })
        except Exception:
            pass  # Never let event recording break tool execution

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
                        result = {
                            "ok": False, "strategy": "ambiguous",
                            "ambiguous": True,
                            "match_count": amb.match_count,
                            "match_lines": amb.match_line_numbers,
                            "error": amb.to_ai_feedback(),
                        }
            except ValueError as e:
                result = {"ok": False, "strategy": "all_failed", "error": str(e)}
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
                })
            except Exception:
                pass
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
                    })
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
                    _st3.append_session_event({"type": "git_commit", "method": name})
                elif name == "test_and_capture":
                    # shell.run used for test — check exit code in result
                    exit_code = result.get("exit_code", 0) if isinstance(result, dict) else 0
                    ev_type = "test_passed" if exit_code == 0 else "test_failed"
                    _st3.append_session_event({
                        "type": ev_type, "method": name,
                        "error": result.get("stderr", "")[:200] if isinstance(result, dict) and exit_code != 0 else "",
                    })
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
                _st3e.append_session_event({"type": "error", "method": name, "error": str(e)})
            except Exception:
                pass
            log.exception("Tool %s failed", name)
            return f"Error: {e}"

    async def _build_context(self, user_input: str, context_params: dict | None = None,
                              history: list[dict] | None = None,
                              session_id: str = "default") -> dict:
        """Build context via ContextEngine."""
        try:
            from evocli_soul.context_engine import ContextEngine
            ctx_engine = ContextEngine(self.bridge)

            # Inject /add-ed files into context params (Aider /add pattern)
            try:
                import evocli_soul.state as _st_add
                added_files = _st_add.get_added_files(session_id)
                if added_files:
                    # Build a "@file:path" prefix for each added file so context_engine
                    # picks them up as @mention providers (highest priority context)
                    add_prefix = " ".join(f"@file:{f}" for f in added_files[:5])  # max 5
                    enriched_goal = f"{add_prefix}\n\n{user_input}"
                    log.debug("_build_context: injecting %d /add-ed files", len(added_files))
                else:
                    enriched_goal = user_input
            except Exception:
                enriched_goal = user_input

            # Load anchored summary (preserved across /compress — this is the compact session memory)
            try:
                import evocli_soul.state as _st_anchor
                anchored_summary = _st_anchor.get_anchored_summary(session_id)
            except Exception:
                anchored_summary = ""

            return await ctx_engine.build({
                "goal":             enriched_goal,
                "project_id":       (context_params or {}).get("project_id", "."),
                "current_file":     (context_params or {}).get("current_file"),
                "git_diff":         (context_params or {}).get("git_diff", ""),
                "history":          history or [],
                "active_tools":     list(self._TOOL_TO_RPC.keys()),
                "session_id":       session_id,
                "anchored_summary": anchored_summary,  # injected after /compress
                "read_only":        self.read_only,    # passed through so context_engine uses correct prompt mode
            })
        except Exception as e:
            log.debug("Context build failed: %s", e)
            return {}
    
    async def _inject_context(self, user_input: str, ctx: dict) -> str:
        """Prefix user input with file context from ContextEngine.

        注入策略（避免双重注入）：
        - user_context（当前文件、git diff、对话历史）→ 注入 user message（所有路径）
        - system_prompt（约束、记忆、RepoMap）→ 由各 LLM 路径的 system message 处理：
            * pydantic-ai: 初始化时设置静态约束 + memory_recall 工具按需召回
            * _stream_litellm: Fix 3 直接使用 ctx["system_prompt"] 作为 system message
            * _run_litellm:    同上
        不在此处注入 system_prompt 以避免 LiteLLM 路径的 token 双重消耗。
        """
        parts = []

        # 注入 user_context（当前文件内容、git diff、对话历史）
        if ctx.get("user_context"):
            parts.append(ctx["user_context"])

        parts.append(user_input)
        return "\n\n---\n".join(p for p in parts if p.strip()) if len(parts) > 1 else user_input
    
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
        except Exception:
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
        await self._emit_fallback_warning_once()
        # ── ToolRouter: 选工具（prepare hook 会读取 _selected_tool_names）──────
        self._select_tools_for_request(user_input)
        import asyncio
        # Read timeout from config [agent] section (default 20s)
        _ctx_timeout = float((self.config or {}).get("agent", {}).get("context_build_timeout_s", 20))
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
        full_input = await self._inject_context(user_input, ctx)

        if self._agent is not None:
            try:
                # History is embedded in full_input via _inject_context (user_context).
                # We do NOT pass message_history to pydantic-ai because our prior_history
                # is plain {role, content} dicts — pydantic-ai expects typed ModelMessage
                # objects. Passing dicts risks silent double-injection if pydantic-ai
                # happens to accept them. Full context is already in full_input.
                async with self._agent.run_stream(full_input) as result:
                    async for chunk in result.stream_text(delta=True):
                        yield chunk
                # After stream ends, emit cost_update with real usage from pydantic-ai.
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
                            log.debug("pydantic-ai stream usage: in=%d out=%d", _in, _out)
                except Exception as _ue:
                    log.debug("pydantic-ai stream cost_update failed (non-fatal): %s", _ue)
                return
            except Exception as e:
                log.warning("Pydantic AI stream failed (%s), falling back", e)

        # Fallback: full_input already has history embedded via _inject_context;
        # _stream_litellm uses system+user message format (history not re-injected).
        async for chunk in self._stream_litellm(full_input, ctx, prior_history=None):
            yield chunk
    
    async def _run_litellm(self, user_input: str, ctx: dict,
                           prior_history: list[dict] | None = None) -> str:
        """Raw LiteLLM fallback with tool calling loop."""
        import litellm
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

        # GAP-1: Hard reflection loop constants — read from config [agent] section
        # Allows users to tune via config.toml: [agent] max_reflections = 5
        _agent_cfg      = (self.config or {}).get("agent", {})
        MAX_REFLECTIONS = int(_agent_cfg.get("max_reflections", 3))
        MAX_TOOL_CALLS  = int(_agent_cfg.get("max_tool_calls",  20))

        _REFLECTION_TRIGGERS = frozenset({
            "fs_lint_file",        # lint failure → fix errors
            "test_and_capture",    # test failure → fix code
            "fs_apply_search_replace",  # ambiguous match → add more context
        })
        reflection_count = 0

        for _ in range(MAX_TOOL_CALLS):
            # Bug fix: _resolve_model() now returns Router group alias ("fast"/"smart").
            # We must resolve the alias to the real model name for litellm.acompletion().
            # Use llm._router.acompletion() which understands the group aliases.
            tier_alias = llm._resolve_model("auto")
            # Read max_tokens/temperature from config [llm.params.agent] if present.
            _task_params = llm.get_task_params("agent")
            call_kwargs: dict = {
                "model":       tier_alias,  # Router group alias — handled by router
                "messages":    conversation,
                "tools":       tools,
                "max_tokens":  _task_params.get("max_tokens",  4096),
                "temperature": _task_params.get("temperature", 0.7),
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

            for tc in msg.tool_calls:
                import json as _json
                try:
                    targs = _json.loads(tc.function.arguments)
                except Exception as e:
                    # HIGH: Silently using empty args causes tools to execute with wrong parameters.
                    # Log the invalid JSON so we can diagnose LLM output format regressions.
                    log.warning("_run_litellm: JSON decode failed for tool '%s' args=%r: %s",
                                tc.function.name, tc.function.arguments[:200], e)
                    targs = {}
                result = await self._execute_tool(tc.function.name, targs)
                conversation.append({"role": "tool", "tool_call_id": tc.id, "content": result})

                # GAP-1: Hard reflection — auto-inject error context when lint/test/edit fails.
                # Aider pattern: failure → inject "[Auto-reflection N/MAX] <error>" as user msg
                # so the LLM is forced to address the error before declaring done.
                # Detection handles BOTH response formats:
                #   - JSON: {"ok": false, "reflection_prompt": "..."} (from _execute_tool path)
                #   - Plain text: "✗ Tests FAILED..." or "✗ Lint FAILED..." (from pydantic-ai closures
                #     called via _execute_tool when tools are Python-native)
                if tc.function.name in _REFLECTION_TRIGGERS and reflection_count < MAX_REFLECTIONS:
                    try:
                        reflection_msg = ""
                        # Try JSON format first
                        try:
                            r_data = _json.loads(result)
                            rp = r_data.get("reflection_prompt", "")
                            is_failed = not r_data.get("ok", True)
                            is_ambiguous = r_data.get("ambiguous", False)
                            if rp and (is_failed or is_ambiguous):
                                reflection_msg = rp
                        except (_json.JSONDecodeError, TypeError, AttributeError):
                            pass
                        # Fallback: detect plain-text failure markers (new format from lint/test)
                        if not reflection_msg and isinstance(result, str):
                            if result.startswith("✗") or "FAILED" in result or "You MUST fix" in result:
                                reflection_msg = result[:600]
                        if reflection_msg:
                            reflection_count += 1
                            conversation.append({
                                "role": "user",
                                "content": (
                                    f"[Auto-reflection {reflection_count}/{MAX_REFLECTIONS}] "
                                    f"{reflection_msg}"
                                ),
                            })
                            log.info(
                                "GAP-1 reflection %d/%d triggered by %s",
                                reflection_count, MAX_REFLECTIONS, tc.function.name,
                            )
                    except Exception:
                        pass  # Non-fatal: never break the tool loop

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
        # 原版硬编码 constraints="（无）" 导致所有精心构建的上下文被丢弃。
        # ctx["system_prompt"] 由 _build_context() 通过 ContextEngine.build() 生成。
        if ctx and ctx.get("system_prompt"):
            system = ctx["system_prompt"]
            log.debug("_stream_litellm: using context_engine system_prompt (%d chars)", len(system))
        else:
            # Fallback: 从 memory 加载约束（比"（无）"更准确）
            constraints = "（无）"
            if self.memory:
                try:
                    c = self.memory.get_constraints()
                    if c:
                        constraints = "\n".join(f"- {x}" for x in c)
                except Exception:
                    pass
            system = build_system_prompt(
                constraints=constraints,
                goal=user_input[:200],
                read_only=self.read_only,
                compact=True,
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
                except Exception:
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
    
    def reset(self) -> None:
        self._init_agent()

    async def reload_user_tools(self) -> int:
        """
        G-09: 从 Rust 获取用户注册工具列表，动态追加到 _TOOL_TO_RPC 和 function definitions。
        返回新增工具数量。
        """
        try:
            result = await self.bridge.call("tool.list_user", {})
            tools  = result.get("tools", []) if isinstance(result, dict) else []
        except Exception as e:
            log.debug("reload_user_tools: %s", e)
            return 0

        added = 0
        for tool in tools:
            name = tool.get("name", "").replace("-", "_").replace(".", "_")
            cmd  = tool.get("cmd", "")
            desc = tool.get("description", f"Run: {cmd}")
            tool_key = f"user_{name}"
            if tool_key in self._TOOL_TO_RPC:
                continue  # 已注册
            # 追加到 _TOOL_TO_RPC
            raw_name = tool.get("name", name)
            self._TOOL_TO_RPC[tool_key] = (
                "tool.run_user",
                lambda args, n=raw_name: {"name": n, "args": args.get("args", ""), "dry_run": args.get("dry_run", False)},
            )
            log.info("Registered user tool: %s → tool.run_user(%s)", tool_key, raw_name)
            added += 1

        if added:
            log.info("reload_user_tools: added %d user tool(s)", added)
        return added

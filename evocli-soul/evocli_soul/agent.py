"""
EvoCLI Agent — Pydantic AI 实现（Section 20 设计要求）
"""
from __future__ import annotations

import importlib.util
import logging
from typing import AsyncGenerator

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
        except Exception as _e:
            log.debug("keyring lookup failed for %s: %s", provider, _e)
        raise _ApiKeyMissingError(provider, env_var)

# 导入生产级提示词库（替代原始 6 行 _SYSTEM_TEMPLATE）
from evocli_soul.default_prompts import build_system_prompt
from evocli_soul.agent_execution import AgentExecutionMixin  # noqa: F401
from evocli_soul.agent_context       import AgentContextMixin       # noqa: F401
from evocli_soul.agent_litellm       import AgentLiteLLMMixin       # noqa: F401
from evocli_soul.agent_executor      import AgentExecutorMixin      # noqa: F401
from evocli_soul.agent_tool_selector import AgentToolSelectorMixin  # noqa: F401
from evocli_soul.agent_tool_defs     import AgentToolDefsMixin      # noqa: F401

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


class EvoCLIAgent(
    AgentExecutionMixin,
    AgentContextMixin,
    AgentLiteLLMMixin,
    AgentExecutorMixin,
    AgentToolSelectorMixin,
    AgentToolDefsMixin,
):
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
                model_id=fast_model,        # per-model specialization + env block
                provider_id=provider,
                inject_skills=True,
            )
            if self.role_instructions:
                system = self.role_instructions + "\n\n" + system
            self._agent = Agent(model=model, system_prompt=system)

            # ── 核心工具注册（pydantic_ai tool_plain 模式）──────────────────────
            # 所有工具定义在 agent_tools_*.py 子模块中，通过 agent_tools.py 编排注册。
            # 工具数量由自省自动计算，不再硬编码。
            self._register_pydantic_tools(self._agent)

            # 动态计算已注册工具数量 — 多级 fallback 覆盖不同 pydantic-ai 版本
            _tool_count = self._count_registered_tools()
            log.info("Pydantic AI Agent initialized: model=%s provider=%s tools=%d",
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
        from evocli_soul.handlers import code_analysis as _ca

        bridge = self.bridge      # closure capture — agent instances share bridge
        _sid   = self._session_id # capture session_id for todo tool closures

        def _sc_display(method: str, params: dict) -> str:
            """Build a short human-readable display string for a tool call."""
            # Pick the most meaningful param value for the label
            for key in ("path", "cmd", "pattern", "query", "symbol", "name",
                        "url", "content", "prompt", "text"):
                val = params.get(key)
                if val and isinstance(val, str):
                    short = val.replace("\n", " ").strip()
                    short = short if len(short) <= 48 else short[:45] + "…"
                    return f"{method}  {short}"
            return method

        async def _sc(method: str, params: dict) -> str:
            """Safe bridge call — catches ALL exceptions and returns error strings.

            pydantic-ai propagates unhandled tool exceptions as stream failures.
            Returning an error string lets the model see what went wrong and
            try an alternative approach, instead of crashing the whole session.

            Emits tool_call_start / tool_call_done events so the TUI shows real-time
            tool activity for ALL pydantic-ai tools (was only shown in LiteLLM fallback).

            Also increments per-iteration tool count so the autonomous loop can
            detect whether real work happened (vs. the AI only producing text).
            """
            from evocli_soul.rpc import emit_event as _emit_sc
            display = _sc_display(method, params)
            await _emit_sc("tool_call_start", {"tool": method, "display": display})
            try:
                result = await bridge.call(method, params)
                out = result if isinstance(result, str) else _json.dumps(result, ensure_ascii=False)
                await _emit_sc("tool_call_done", {"tool": method, "ok": True})
                # Track tool call for autonomous loop iteration detection
                try:
                    from evocli_soul.state import increment_iteration_tool_count as _inc_tc
                    from evocli_soul.state import record_tool_in_scratchpad as _rts
                    _inc_tc(_sid)
                    _rts(method, _sid)  # Gemini scratchpad
                except Exception:
                    pass
                return out
            except Exception as _tool_err:
                log.warning("tool %s failed: %s", method, _tool_err)
                await _emit_sc("tool_call_done", {"tool": method, "ok": False})
                return f"Error: {_tool_err}"

        async def _call_handler(handler_fn, params: dict) -> str:
            """Call a Python-side RPC handler directly (bypasses deprecated Rust bridge).
            
            Used for tools migrated from Rust to Python (H1/H2 migration):
            assume.*, impact.*, equiv.*, verify.*, symbol.usages, symbol.lifecycle
            """
            import json as _hj
            import evocli_soul.state as _h_state
            _result: dict = {}

            class _MockSend:
                async def response(self, req_id, data):
                    _result['data'] = data
                async def error(self, req_id, code, msg):
                    _result['error'] = msg

            try:
                await handler_fn("local", params, _MockSend(), _h_state)
            except Exception as _he:
                log.warning("handler %s failed: %s", getattr(handler_fn, '__name__', '?'), _he)
                return f"Error: {_he}"

            if 'error' in _result:
                return f"Error: {_result['error']}"
            return _hj.dumps(_result.get('data', {}), ensure_ascii=False)


        # ── Delegate tool registration to agent_tools.py ─────────────────────
        # All @agent.tool_plain definitions extracted to agent_tools.py.
        # Keeps _register_pydantic_tools focused on setup, not tool definitions.
        from evocli_soul.agent_tools import register_tools as _register_tools_ext
        _register_tools_ext(
            agent           = agent,
            bridge          = bridge,
            sid             = _sid,
            sc_fn           = _sc,
            call_handler_fn = _call_handler,
            config          = self.config,
            memory          = self.memory,
        )

    def _count_registered_tools(self) -> int:
        """
        Auto-introspect the number of pydantic-ai registered tools.
        Works across pydantic-ai 1.x versions via multi-level fallback.
        Also counts _TOOL_TO_RPC entries for the LiteLLM fallback path.
        Returns the LARGER of the two counts (pydantic-ai path vs litellm path).
        """
        pydantic_count = 0
        if self._agent is not None:
            # pydantic-ai >= 1.93: _function_toolset.tools
            ts = getattr(self._agent, '_function_toolset', None)
            if ts is not None:
                pydantic_count = len(getattr(ts, 'tools', None) or {})
            # pydantic-ai 1.x: _function_tools
            if not pydantic_count:
                pydantic_count = len(getattr(self._agent, '_function_tools', None) or {})
        litellm_count = len(getattr(self, '_TOOL_TO_RPC', {}))
        return max(pydantic_count, litellm_count)

    @classmethod
    def get_tool_count_from_registry(cls) -> int:
        """
        Return the expected total tool count from agent_tools sub-modules
        WITHOUT instantiating a full agent. Used by opencode.json generation
        and AGENTS.md auto-update scripts.

        Counts @agent.tool_plain definitions in all agent_tools_*.py files.
        """
        import re
        from pathlib import Path
        soul_dir = Path(__file__).parent
        total = 0
        for pattern in ("agent_tools_fs.py", "agent_tools_code.py", "agent_tools_shell.py"):
            fp = soul_dir / pattern
            if fp.exists():
                text = fp.read_text(encoding="utf-8")
                # Count @agent.tool_plain decorator occurrences
                total += len(re.findall(r'@agent\.tool_plain', text))
        return total

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
            tool.get("description", f"Run: {cmd}")
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

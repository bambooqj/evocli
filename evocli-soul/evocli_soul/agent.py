"""EvoCLI Agent — LiteLLM-only implementation."""
from __future__ import annotations

import logging

log = logging.getLogger("evocli.agent")

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
    """LiteLLM-driven EvoCLI agent."""
    
    def __init__(self, bridge, memory=None, config: dict | None = None, read_only: bool = False, role: str = "orchestrator", role_instructions: str = "", session_id: str = "default"):
        self.bridge    = bridge
        self.memory    = memory
        self.config    = config or {}
        self.read_only = read_only   # G-10: 只读分析模式
        self.role      = role
        self.role_instructions = role_instructions
        self._session_id = session_id   # for context cache + file dedup keying
        # ── ToolRouter 状态（per-request）────────────────────────────────
        self._selected_tool_names: frozenset[str] = frozenset()  # 本次选中的工具集
        self._current_query: str = ""  # 用于 prepare hook 读取

    def _count_registered_tools(self) -> int:
        """Return the current LiteLLM-callable tool count."""
        return max(self.get_tool_count_from_registry(), len(getattr(self, '_TOOL_TO_RPC', {})))

    @classmethod
    def get_tool_count_from_registry(cls) -> int:
        """
        Return the expected total LiteLLM tool count without instantiating a
        fully initialized agent.
        """
        helper = cls.__new__(cls)
        return len(AgentToolDefsMixin._all_tool_definitions(helper))

    def reset(self) -> None:
        self._selected_tool_names = frozenset()
        self._current_query = ""

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

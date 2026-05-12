"""
Expert Agent 工厂函数 — 基于角色创建专门化的 EvoCLIAgent 实例。

每个 Expert 有:
- 特定的系统提示词（角色约束）
- 工具白名单（read-only vs write）
- LLM tier 设置（fast/smart）
"""
from __future__ import annotations
import logging
from pathlib import Path

log = logging.getLogger("evocli.agents")

# 工具权限定义
READ_ONLY_TOOLS = [
    "fs_read", "shell_grep", "shell_find", "shell_ls", "shell_cat",
    "shell_head", "shell_tail", "shell_wc", "search_code",
    "symbol_lookup", "symbol_variants", "symbol_usages", "symbol_lifecycle",
    "code_intel_incoming_calls", "code_intel_outgoing_calls",
    "code_intel_full_chain", "code_intel_impact_radius",
    "code_intel_list_symbols", "code_intel_index_status",
    "assume_has_tests", "assume_caller_count", "assume_is_pure",
    "assume_has_side_effects", "impact_check", "impact_affected_tests",
    "equiv_find", "equiv_find_similar_code",
    "memory_recall", "memory_constraints",
    "mcp_list_tools",
]

WRITE_TOOLS = READ_ONLY_TOOLS + [
    "fs_write", "fs_apply_diff",
    "shell_run", "shell_mkdir", "shell_mv", "shell_cp", "shell_rm", "shell_touch",
    "git_status", "git_commit", "git_diff", "git_snapshot", "git_restore",
    "git_shadow_snapshot", "git_shadow_restore",
    "memory_write", "approval_request",
    "mcp_call",
]


def _load_role_md(role_name: str) -> str:
    """加载 agents/<role_name>.md 中的角色定义。"""
    md_path = Path(__file__).parent / f"{role_name}.md"
    if md_path.exists():
        return md_path.read_text(encoding="utf-8")
    return f"You are the {role_name} agent."


def create_planner_agent(bridge, memory, config: dict | None = None):
    """Planner: 任务分解 → 结构化计划。只读权限。"""
    from evocli_soul.agent import EvoCLIAgent
    role_instructions = _load_role_md("planner")
    return EvoCLIAgent(bridge, memory, config or {}, role="planner",
                       role_instructions=role_instructions, read_only=True)


def create_reviewer_agent(bridge, memory, config: dict | None = None):
    """Reviewer: 代码审查 + 标准执行。只读 + 测试运行。"""
    from evocli_soul.agent import EvoCLIAgent
    role_instructions = _load_role_md("reviewer")
    return EvoCLIAgent(bridge, memory, config or {}, role="reviewer",
                       role_instructions=role_instructions, read_only=True)


def create_researcher_agent(bridge, memory, config: dict | None = None):
    """Researcher: 代码库 + 网络搜索。只读权限。"""
    from evocli_soul.agent import EvoCLIAgent
    role_instructions = _load_role_md("researcher")
    return EvoCLIAgent(bridge, memory, config or {}, role="researcher",
                       role_instructions=role_instructions, read_only=True)


def create_coder_agent(bridge, memory, config: dict | None = None):
    """Coder: 代码变更实现。完整写权限。"""
    from evocli_soul.agent import EvoCLIAgent
    role_instructions = _load_role_md("coder")
    return EvoCLIAgent(bridge, memory, config or {}, role="coder",
                       role_instructions=role_instructions, read_only=False)


def create_debugger_agent(bridge, memory, config: dict | None = None):
    """Debugger: 系统化调试。只读 + 测试运行。"""
    from evocli_soul.agent import EvoCLIAgent
    role_instructions = _load_role_md("debugger")
    return EvoCLIAgent(bridge, memory, config or {}, role="debugger",
                       role_instructions=role_instructions, read_only=True)


EXPERT_FACTORY = {
    "planner":    create_planner_agent,
    "reviewer":   create_reviewer_agent,
    "researcher": create_researcher_agent,
    "coder":      create_coder_agent,
    "debugger":   create_debugger_agent,
}


def create_expert(role: str, bridge, memory, config: dict | None = None):
    """按角色名创建 Expert Agent。"""
    factory = EXPERT_FACTORY.get(role)
    if factory is None:
        raise ValueError(f"Unknown expert role: {role}. Valid: {list(EXPERT_FACTORY)}")
    return factory(bridge, memory, config)

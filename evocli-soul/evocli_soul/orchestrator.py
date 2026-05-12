"""
orchestrator.py — EvoCLI 多 Agent 编排引擎

使用 LangGraph 实现 Supervisor-Worker 架构：
  - SemanticRouter: 意图分类 → 路由到正确 Expert
  - Expert Nodes: Planner/Coder/Reviewer/Debugger/Researcher
  - Blackboard: 共享上下文（跨 agent 传递信息）
  - Synthesizer: 汇总多 agent 结果为最终响应
  - SqliteSaver: 跨 session 持久化

设计原则（来自 ECC 和框架研究）：
  - Code Sovereign: 只有 Coder 有写权限
  - Context Pruning: subagent 返回摘要，不是原文
  - Max Depth: 防止递归调用超过 MAX_DEPTH=3
  - Graceful Fallback: LangGraph 不可用时退化为单 agent
"""
from __future__ import annotations

import importlib.util
import logging
import uuid
from typing import TypedDict, Annotated, Any

log = logging.getLogger("evocli.orchestrator")

MAX_DEPTH = 3
MAX_AGENT_TOKENS = 4000  # per-agent context budget


# ── Shared State (Blackboard) ─────────────────────────────────────────────────

def _add_messages(left: list, right: list) -> list:
    return left + right


class OrchestratorState(TypedDict):
    """
    多 Agent 共享状态（Blackboard 模式）。
    通过 LangGraph reducer 安全合并来自不同 agent 的更新。
    """
    # Core
    messages:       Annotated[list[dict], _add_messages]  # 消息历史（reducer 追加）
    goal:           str              # 用户原始目标（不变）
    thread_id:      str

    # Routing
    next_agent:     str              # Router 决定的下一个 agent
    visited_agents: list[str]        # 已经运行过的 agents（防循环）
    depth:          int              # 当前调用深度

    # Blackboard
    blackboard:     dict[str, Any]   # 共享上下文（各 agent 可读写）
    agent_outputs:  list[dict]       # 每个 agent 的输出摘要

    # Result
    final_response: str
    errors:         list[str]


# ── Intent Classification ─────────────────────────────────────────────────────
# 使用本地嵌入模型（paraphrase-multilingual-MiniLM-L12-v2）做零样本 cosine similarity 分类。
# 关键词列表仅作 fastembed 不可用时的冷启动 fallback。
# 每次分类结果自动记录到 ~/.evocli/intent_router/labels.jsonl 供 Phase 1 重训。

from evocli_soul.local_classifier import (
    AGENT_INTENT_DESCRIPTIONS,
    classify_by_similarity,
    record_label,
)

# 关键词 fallback（仅在 fastembed 不可用时使用）
_INTENT_ROUTES_FALLBACK: dict[str, list[str]] = {
    "planner":    ["plan", "design", "break down", "decompose", "roadmap",
                   "规划", "方案", "拆解", "架构"],
    "coder":      ["implement", "write code", "create", "fix bug", "refactor",
                   "实现", "写代码", "新增", "修复", "重构"],
    "reviewer":   ["review", "check", "audit", "quality", "lint",
                   "审查", "检查", "代码质量"],
    "debugger":   ["debug", "error", "failing test", "not working", "crash",
                   "调试", "报错", "测试失败", "为什么"],
    "researcher": ["find", "search", "where is", "explain", "understand",
                   "查找", "搜索", "解释", "了解"],
}


def _classify_intent_keywords(goal: str) -> str:
    """关键词 fallback 分类（fastembed 不可用时使用）。"""
    goal_lower = goal.lower()
    scores: dict[str, int] = {role: 0 for role in _INTENT_ROUTES_FALLBACK}
    for role, keywords in _INTENT_ROUTES_FALLBACK.items():
        for kw in keywords:
            if kw in goal_lower:
                scores[role] += 1
    best = max(scores, key=lambda r: scores[r])
    return best if scores[best] > 0 else "orchestrator"


def _classify_intent(goal: str) -> str:
    """
    语义意图分类。优先用本地嵌入模型（paraphrase-multilingual-MiniLM-L12-v2 cosine similarity），
    fastembed 不可用时退回关键词匹配。
    分类结果自动记录供未来 Phase 1 逻辑回归重训。
    """
    result = classify_by_similarity(
        goal,
        AGENT_INTENT_DESCRIPTIONS,
        threshold=0.25,
        fallback="",
    )
    if not result:
        result = _classify_intent_keywords(goal)

    # 积累标签（非阻塞，Phase 1 重训数据）
    record_label(goal, result, extra={"source": "orchestrator_routing"})
    return result


# ── Orchestrator Class ────────────────────────────────────────────────────────

class Orchestrator:
    """
    EvoCLI 多 Agent 编排器。

    用法：
        orchestrator = Orchestrator(bridge, memory)
        result = await orchestrator.run("implement a retry mechanism with tests")
    """

    def __init__(self, bridge, memory, config: dict | None = None):
        self.bridge  = bridge
        self.memory  = memory
        self.config  = config or {}
        self._graph  = None
        self._checkpointer = None

    def _has_langgraph(self) -> bool:
        """Check if LangGraph core is available (checkpoint is optional)."""
        return importlib.util.find_spec("langgraph") is not None

    def _has_checkpoint(self) -> bool:
        """Check if LangGraph SQLite checkpoint is available."""
        return importlib.util.find_spec("langgraph.checkpoint.sqlite") is not None

    def _get_checkpointer(self):
        """
        Get a LangGraph checkpointer.

        Strategy:
        - Try AsyncSqliteSaver (persistent across process restarts)
        - Fall back to MemorySaver (in-memory, survives within same process)
        - Fall back to None (no persistence, graph still works)

        Note: from_conn_string() returns a context manager in newer LangGraph.
        Must use direct connection or MemorySaver instead.
        """
        if self._checkpointer is not None:
            return self._checkpointer
        if not self._has_checkpoint():
            # Fallback: in-memory saver (no cross-restart persistence)
            try:
                from langgraph.checkpoint.memory import MemorySaver
                self._checkpointer = MemorySaver()
                log.debug("Using MemorySaver (no SQLite persistence)")
                return self._checkpointer
            except Exception:
                return None
        try:
            # Use MemorySaver by default — async SQLite requires managed connection lifecycle
            # (aiosqlite.connect() is async and can't be called in sync context here).
            # Use AsyncSqliteSaver only when explicitly requested.
            from langgraph.checkpoint.memory import MemorySaver
            self._checkpointer = MemorySaver()
            log.debug("Using MemorySaver checkpointer (SQLite available but using in-memory for simplicity)")
            return self._checkpointer
        except Exception as e:
            log.debug("Checkpointer init failed: %s — running without persistence", e)
            return None

    def _build_graph(self):
        """Build the LangGraph multi-agent state machine."""
        if not self._has_langgraph():
            return None

        from langgraph.graph import StateGraph, END, START

        builder = StateGraph(OrchestratorState)

        # ── Nodes ──────────────────────────────────────────────────────────

        async def semantic_router(state: OrchestratorState) -> dict:
            """Route the request to the appropriate expert agent."""
            goal  = state["goal"]
            depth = state.get("depth", 0)

            if depth >= MAX_DEPTH:
                log.warning("Max depth %d reached, routing to synthesizer", MAX_DEPTH)
                return {"next_agent": "__end__", "depth": depth}

            # Fast keyword routing
            intent = _classify_intent(goal)

            # Check if agent already visited (prevent loops)
            visited = state.get("visited_agents", [])
            if intent in visited and intent != "orchestrator":
                # Already ran this agent — go to synthesizer
                intent = "__end__"

            log.info("Router: '%s...' → %s", goal[:50], intent)
            return {"next_agent": intent}

        async def planner_node(state: OrchestratorState) -> dict:
            """Planner: decompose goal into structured task list."""
            from evocli_soul.agents.definitions import create_planner_agent
            from evocli_soul.context_pruner import prune_agent_output
            blackboard = state.get("blackboard", {})
            try:
                agent  = create_planner_agent(self.bridge, self.memory, self.config)
                goal   = state["goal"]
                context_hint = f"Blackboard context: {blackboard}" if blackboard else ""
                result = await agent.run(f"{goal}\n\n{context_hint}".strip())
                from evocli_soul import state as _st; pruned = await prune_agent_output(str(result), "planner", _st.get_llm_client())
            except Exception as e:
                log.warning("planner_node failed: %s — using empty output", e)
                pruned = f"[Planner unavailable: {e}]"
            return {
                "visited_agents": state.get("visited_agents", []) + ["planner"],
                "agent_outputs": state.get("agent_outputs", []) + [{"role": "planner", "output": pruned}],
                "blackboard": {**blackboard, "plan": pruned},
                "next_agent": "__end__",
                "depth": state.get("depth", 0) + 1,
            }

        async def coder_node(state: OrchestratorState) -> dict:
            """Coder: implement code changes. Has write access. Follows TDD."""
            from evocli_soul.agents.definitions import create_coder_agent
            from evocli_soul.context_pruner import prune_agent_output
            blackboard = state.get("blackboard", {})
            try:
                agent = create_coder_agent(self.bridge, self.memory, self.config)
                plan  = blackboard.get("plan", "")
                prompt = state["goal"]
                if plan:
                    prompt = f"Goal: {prompt}\n\nPlan from Planner:\n{plan}\n\nImplement this."
                result = await agent.run(prompt)
                from evocli_soul import state as _st; pruned = await prune_agent_output(str(result), "coder", _st.get_llm_client())
            except Exception as e:
                log.warning("coder_node failed: %s", e)
                pruned = f"[Coder unavailable: {e}]"
            return {
                "visited_agents": state.get("visited_agents", []) + ["coder"],
                "agent_outputs": state.get("agent_outputs", []) + [{"role": "coder", "output": pruned}],
                "blackboard": {**blackboard, "implementation": pruned},
                "next_agent": "__end__",
                "depth": state.get("depth", 0) + 1,
            }

        async def reviewer_node(state: OrchestratorState) -> dict:
            """Reviewer: review code changes, enforce standards."""
            from evocli_soul.agents.definitions import create_reviewer_agent
            from evocli_soul.context_pruner import prune_agent_output
            blackboard = state.get("blackboard", {})
            try:
                agent = create_reviewer_agent(self.bridge, self.memory, self.config)
                impl  = blackboard.get("implementation", "")
                prompt = state["goal"]
                if impl:
                    prompt = f"Review the following implementation for: {prompt}\n\nImplementation summary:\n{impl}"
                result = await agent.run(prompt)
                from evocli_soul import state as _st; pruned = await prune_agent_output(str(result), "reviewer", _st.get_llm_client())
            except Exception as e:
                log.warning("reviewer_node failed: %s", e)
                pruned = f"[Reviewer unavailable: {e}]"
            return {
                "visited_agents": state.get("visited_agents", []) + ["reviewer"],
                "agent_outputs": state.get("agent_outputs", []) + [{"role": "reviewer", "output": pruned}],
                "blackboard": {**blackboard, "review": pruned},
                "next_agent": "__end__",
                "depth": state.get("depth", 0) + 1,
            }

        async def debugger_node(state: OrchestratorState) -> dict:
            """Debugger: systematic bug investigation."""
            from evocli_soul.agents.definitions import create_debugger_agent
            from evocli_soul.context_pruner import prune_agent_output
            blackboard = state.get("blackboard", {})
            try:
                agent  = create_debugger_agent(self.bridge, self.memory, self.config)
                result = await agent.run(state["goal"])
                from evocli_soul import state as _st; pruned = await prune_agent_output(str(result), "debugger", _st.get_llm_client())
            except Exception as e:
                log.warning("debugger_node failed: %s", e)
                pruned = f"[Debugger unavailable: {e}]"
            return {
                "visited_agents": state.get("visited_agents", []) + ["debugger"],
                "agent_outputs": state.get("agent_outputs", []) + [{"role": "debugger", "output": pruned}],
                "blackboard": {**blackboard, "debug_findings": pruned},
                "next_agent": "__end__",
                "depth": state.get("depth", 0) + 1,
            }

        async def researcher_node(state: OrchestratorState) -> dict:
            """Researcher: codebase and web search."""
            from evocli_soul.agents.definitions import create_researcher_agent
            from evocli_soul.context_pruner import prune_agent_output
            blackboard = state.get("blackboard", {})
            try:
                agent  = create_researcher_agent(self.bridge, self.memory, self.config)
                result = await agent.run(state["goal"])
                from evocli_soul import state as _st; pruned = await prune_agent_output(str(result), "researcher", _st.get_llm_client())
            except Exception as e:
                log.warning("researcher_node failed: %s", e)
                pruned = f"[Researcher unavailable: {e}]"
            return {
                "visited_agents": state.get("visited_agents", []) + ["researcher"],
                "agent_outputs": state.get("agent_outputs", []) + [{"role": "researcher", "output": pruned}],
                "blackboard": {**blackboard, "research": pruned},
                "next_agent": "__end__",
                "depth": state.get("depth", 0) + 1,
            }

        async def synthesizer_node(state: OrchestratorState) -> dict:
            """Synthesize all agent outputs into a final response."""
            outputs = state.get("agent_outputs", [])
            if not outputs:
                # No expert ran — fall back to main agent
                from evocli_soul import state as _state
                agent = _state.get_agent()
                text  = await agent.run(state["goal"])
                return {"final_response": str(text)}

            # Format the synthesis
            parts = [f"# Task: {state['goal']}\n"]
            for out in outputs:
                role   = out.get("role", "agent")
                output = out.get("output", "")
                parts.append(f"\n## {role.title()} Output\n{output}")

            final = "\n".join(parts)
            return {"final_response": final}

        # ── Register Nodes ────────────────────────────────────────────────
        builder.add_node("router",      semantic_router)
        builder.add_node("planner",     planner_node)
        builder.add_node("coder",       coder_node)
        builder.add_node("reviewer",    reviewer_node)
        builder.add_node("debugger",    debugger_node)
        builder.add_node("researcher",  researcher_node)
        builder.add_node("synthesizer", synthesizer_node)

        # ── Edges ─────────────────────────────────────────────────────────
        builder.add_edge(START, "router")

        # Conditional routing from router → expert
        def route_decision(state: OrchestratorState) -> str:
            return state.get("next_agent", "__end__")

        builder.add_conditional_edges(
            "router",
            route_decision,
            {
                "planner":    "planner",
                "coder":      "coder",
                "reviewer":   "reviewer",
                "debugger":   "debugger",
                "researcher": "researcher",
                "orchestrator": "synthesizer",  # no clear intent → synthesize directly
                "__end__":    "synthesizer",
            }
        )

        # All experts → synthesizer
        for expert in ["planner", "coder", "reviewer", "debugger", "researcher"]:
            builder.add_edge(expert, "synthesizer")

        builder.add_edge("synthesizer", END)

        # Compile with checkpointer
        compile_kwargs: dict = {}
        checkpointer = self._get_checkpointer()
        if checkpointer:
            compile_kwargs["checkpointer"] = checkpointer

        return builder.compile(**compile_kwargs)

    async def run(
        self,
        goal: str,
        session_id: str | None = None,
        blackboard: dict | None = None,
    ) -> dict:
        """
        Run multi-agent orchestration for a goal.

        Returns: {"text": str, "thread_id": str, "agents_used": list, "blackboard": dict}
        """
        if not self._has_langgraph():
            # Graceful fallback to single agent
            from evocli_soul import state as _state
            agent = _state.get_agent()
            text  = await agent.run(goal)
            return {"text": str(text), "thread_id": session_id or "", "agents_used": [], "blackboard": {}}

        if self._graph is None:
            self._graph = self._build_graph()

        if self._graph is None:
            from evocli_soul import state as _state
            agent = _state.get_agent()
            text  = await agent.run(goal)
            return {"text": str(text), "thread_id": session_id or "", "agents_used": []}

        thread_id = session_id or f"orch_{uuid.uuid4().hex[:12]}"
        config    = {"configurable": {"thread_id": thread_id}}

        initial_state: OrchestratorState = {
            "messages":       [{"role": "user", "content": goal}],
            "goal":           goal,
            "thread_id":      thread_id,
            "next_agent":     "",
            "visited_agents": [],
            "depth":          0,
            "blackboard":     blackboard or {},
            "agent_outputs":  [],
            "final_response": "",
            "errors":         [],
        }

        try:
            final_state: dict = {}
            async for event in self._graph.astream(initial_state, config=config):
                final_state = event
                log.debug("Orchestrator event keys: %s", list(event.keys()))

            # Fix (Oracle A4): LangGraph default stream_mode="values" yields the
            # COMPLETE state dict, NOT keyed by node name.
            # Read final_response / visited_agents / blackboard directly from state.
            final_resp  = final_state.get("final_response", "")
            agents_used = final_state.get("visited_agents", [])
            bb          = final_state.get("blackboard", {})

            # Defensive: if still empty (shouldn't happen after synthesizer runs)
            if not final_resp:
                agent_outs = final_state.get("agent_outputs", [])
                if agent_outs:
                    final_resp = "\n\n".join(
                        f"[{o.get('role','?')}]\n{o.get('output','')}" for o in agent_outs
                    )

            return {
                "text":        final_resp,
                "thread_id":   thread_id,
                "agents_used": agents_used,
                "blackboard":  bb,
                "ok":          True,
            }

        except Exception as e:
            log.exception("Orchestrator run failed: %s", e)
            # Fallback to single agent
            try:
                from evocli_soul import state as _state
                agent = _state.get_agent()
                text  = await agent.run(goal)
                return {"text": str(text), "thread_id": thread_id, "agents_used": [], "ok": True}
            except Exception as e2:
                return {"text": f"Error: {e2}", "thread_id": thread_id, "ok": False, "error": str(e2)}


# ── RPC Handler registration ───────────────────────────────────────────────────

def register(router) -> None:
    """Register orchestrator RPC handlers."""
    router.add("orchestrator.run",    handle_orchestrator_run)
    router.add("orchestrator.status", handle_orchestrator_status)


async def handle_orchestrator_run(req_id: str, params: dict, send, state) -> None:
    """
    orchestrator.run — 多 Agent 编排执行

    params:
      goal:       str   用户目标
      session_id: str   (可选) 继续已有 session
      blackboard: dict  (可选) 初始 Blackboard 数据
    """
    goal       = params.get("goal", params.get("prompt", ""))
    session_id = params.get("session_id")
    blackboard = params.get("blackboard", {})

    if not goal:
        await send.error(req_id, -32600, "goal is required")
        return

    try:
        orch = state.get_orchestrator()
        if orch is None:
            # Fallback
            agent  = state.get_agent()
            result = await agent.run(goal)
            await send.response(req_id, {"text": str(result), "agents_used": []})
            return

        result = await orch.run(goal, session_id=session_id, blackboard=blackboard)
        await send.response(req_id, result)
    except Exception as e:
        log.exception("orchestrator.run failed")
        await send.error(req_id, -32603, str(e))


async def handle_orchestrator_status(req_id: str, params: dict, send, state) -> None:
    """orchestrator.status — 返回当前活跃 subagents 和状态"""
    try:
        active = state.get_active_subagents() if hasattr(state, "get_active_subagents") else {}
        orch   = state.get_orchestrator()
        await send.response(req_id, {
            "active_subagents": list(active.keys()),
            "orchestrator_ready": orch is not None,
            "langgraph_available": importlib.util.find_spec("langgraph") is not None,
        })
    except Exception as e:
        await send.error(req_id, -32603, str(e))



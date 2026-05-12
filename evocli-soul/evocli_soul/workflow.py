"""
LangGraph Workflow 集成（Section 26）
使用 LangGraph SQLite Checkpointer 实现真正的 Session 持久化。
"""
from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import TypedDict, Any

log = logging.getLogger("evocli.workflow")

CHECKPOINT_DB = Path.home() / ".evocli" / "sessions.db"


def _has_langgraph() -> bool:
    """LangGraph core availability (checkpoint is separate / optional)."""
    return importlib.util.find_spec("langgraph") is not None


def _has_checkpoint() -> bool:
    return importlib.util.find_spec("langgraph.checkpoint.sqlite") is not None


def get_checkpointer():
    """获取 LangGraph SQLite Checkpointer（如可用）。"""
    if not _has_checkpoint():
        return None
    try:
        # 架构豁免：LangGraph SqliteSaver 直接管理 ~/.evocli/sessions.db
        # 这是 evocli 内部状态存储（非用户项目文件），属于 ~/.evocli 内部数据豁免范围
        # 不通过 Rust bridge 是因为 LangGraph 内部需要同步 sqlite3 连接
        import sqlite3  # noqa: S403
        from langgraph.checkpoint.sqlite import SqliteSaver
        CHECKPOINT_DB.parent.mkdir(parents=True, exist_ok=True)
        # from_conn_string() is a context manager in newer versions.
        # Use direct sqlite3 connection for long-lived checkpointer.
        conn = sqlite3.connect(str(CHECKPOINT_DB), check_same_thread=False)
        saver = SqliteSaver(conn)
        saver.setup()
        return saver
    except Exception as e:
        log.warning("LangGraph checkpointer init failed: %s", e)
        return None


class WorkflowState(TypedDict):
    """LangGraph 工作流状态。"""
    messages: list[dict]
    goal: str
    current_step: str
    results: list[dict]
    errors: list[str]
    thread_id: str
    blackboard: dict
    agent_role: str
    depth: int
    parent_thread_id: str | None


def update_blackboard(state: WorkflowState, key: str, value: Any) -> dict:
    """更新 Blackboard 共享数据。"""
    bb = state.get("blackboard", {})
    bb[key] = value
    return {"blackboard": bb}


def build_skill_workflow(skill, bridge, checkpointer=None):
    """
    将 Skill 的步骤序列构建为 LangGraph 状态机。
    每个节点真正执行 bridge.call(step.action, step.params)。
    """
    if not _has_langgraph():
        log.debug("LangGraph not available, using sequential execution")
        return None

    try:
        from langgraph.graph import StateGraph, END, START

        builder = StateGraph(WorkflowState)

        # 为每个步骤创建节点（真正执行 bridge.call）
        for step in skill.steps:
            def make_node(s, b):
                async def node_fn(state: WorkflowState) -> dict:
                    # HITL：需要确认的步骤通过 interrupt() 暂停
                    if s.requires_approval:
                        from langgraph.types import interrupt
                        interrupt({"step": s.id, "action": s.action, "params": s.params})

                    # 真正执行工具调用
                    try:
                        result = await b.call(s.action, s.params)
                        step_result = {"step": s.id, "ok": True, "result": result}
                    except Exception as e:
                        log.error("Step %s failed: %s", s.id, e)
                        step_result = {"step": s.id, "ok": False, "error": str(e)}
                        return {
                            "current_step": s.id,
                            "results": state.get("results", []) + [step_result],
                            "errors": state.get("errors", []) + [f"{s.id}: {e}"],
                        }

                    return {
                        "current_step": s.id,
                        "results": state.get("results", []) + [step_result],
                    }
                return node_fn

            builder.add_node(step.id, make_node(step, bridge))

        # 连接步骤（顺序执行）
        step_ids = [s.id for s in skill.steps]
        if step_ids:
            builder.add_edge(START, step_ids[0])
            for i in range(len(step_ids) - 1):
                builder.add_edge(step_ids[i], step_ids[i + 1])
            builder.add_edge(step_ids[-1], END)

        compile_kwargs = {}
        if checkpointer:
            compile_kwargs["checkpointer"] = checkpointer
            # HITL 步骤在节点前中断
            hitl_steps = [s.id for s in skill.steps if s.requires_approval]
            if hitl_steps:
                compile_kwargs["interrupt_before"] = hitl_steps

        return builder.compile(**compile_kwargs)

    except Exception as e:
        log.warning("Workflow build failed: %s", e)
        return None


async def run_skill_with_workflow(skill, bridge, session_id: str | None = None) -> dict:
    """使用 LangGraph 执行 Skill（含 checkpointer 和 HITL fallback）。"""
    checkpointer = get_checkpointer()
    workflow     = build_skill_workflow(skill, bridge, checkpointer)

    if workflow is None:
        # fallback: 顺序执行
        from evocli_soul.skill_engine import SkillEngine
        engine = SkillEngine(bridge)
        return await engine.execute(skill.id)

    import uuid
    thread_id = session_id or f"skill_{skill.id}_{uuid.uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": thread_id}}

    initial_state: WorkflowState = {
        "messages": [],
        "goal":         f"Execute skill: {skill.name}",
        "current_step": "",
        "results":      [],
        "errors":       [],
        "thread_id":    thread_id,
    }

    try:
        results = []
        async for event in workflow.astream(initial_state, config=config):
            if "__interrupt__" in event:
                # HITL 暂停点
                interrupt_data = event["__interrupt__"]
                log.info("Workflow paused for approval: %s", interrupt_data)
                # 发送 approval.request 到 Rust Host
                approval = await bridge.call("approval.request", {
                    "skill_id": skill.id,
                    "step_id":  interrupt_data[0].get("step", "") if interrupt_data else "",
                    "message":  "Skill step requires approval",
                })
                if not approval.get("approved", False):
                    return {"ok": False, "error": "User rejected approval", "results": results}
                # 恢复执行
                async for cont_event in workflow.astream(None, config=config):
                    results.append(cont_event)
                break
            results.append(event)

        return {"ok": True, "results": results, "thread_id": thread_id}

    except Exception as e:
        log.exception("Workflow execution failed: %s", e)
        return {"ok": False, "error": str(e), "thread_id": thread_id}


# ── G-11: Agent 对话 Workflow（支持 session 恢复）──────────────────────────


async def run_agent_with_workflow(
    prompt: str,
    bridge,
    session_id: str | None = None,
) -> dict:
    """
    G-11: 将 agent.run 包装进 LangGraph，支持跨 session 恢复。

    如果 LangGraph 不可用，直接使用 EvoCLIAgent fallback。
    """
    if not _has_langgraph():
        from evocli_soul import state as _state
        agent  = _state.get_agent()
        text   = await agent.run(prompt)
        return {"text": str(text), "thread_id": session_id or ""}

    import uuid
    from langgraph.graph import StateGraph, END, START

    thread_id   = session_id or f"agent_{uuid.uuid4().hex[:12]}"
    checkpointer = get_checkpointer()

    class AgentState(WorkflowState):
        pass

    builder = StateGraph(AgentState)

    async def agent_node(state: AgentState) -> dict:
        from evocli_soul import state as _state
        agent  = _state.get_agent()
        text   = await agent.run(state.get("goal", ""))
        return {
            "results":      state.get("results", []) + [{"role": "assistant", "content": text}],
            "current_step": "done",
        }

    builder.add_node("agent", agent_node)
    builder.add_edge(START, "agent")
    builder.add_edge("agent", END)

    compile_kwargs: dict = {}
    if checkpointer:
        compile_kwargs["checkpointer"] = checkpointer

    try:
        graph  = builder.compile(**compile_kwargs)
        config = {"configurable": {"thread_id": thread_id}}
        initial: AgentState = {
            "messages": [{"role": "user", "content": prompt}],
            "goal":         prompt,
            "current_step": "agent",
            "results":      [],
            "errors":       [],
            "thread_id":    thread_id,
            "blackboard":   {},
            "agent_role":   "orchestrator",
            "depth":        0,
            "parent_thread_id": None,
        }
        final_state: dict = {}
        async for event in graph.astream(initial, config=config):
            final_state = event

        # 从最终状态提取助手回复
        results = final_state.get("agent", {}).get("results", [])
        text    = results[-1]["content"] if results else ""
        return {"text": text, "thread_id": thread_id, "ok": True}

    except Exception as e:
        log.warning("LangGraph agent workflow failed: %s", e)
        from evocli_soul import state as _state
        agent = _state.get_agent()
        text  = await agent.run(prompt)
        return {"text": str(text), "thread_id": thread_id, "ok": True}

"""
multi_agent.py — 多 Agent 并发执行系统（Section 23）

提供：
1. 并行工具执行（Map-Reduce 模式）
2. Manager-Worker 基础框架
3. Daemon Workers（后台常驻任务）
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("evocli.multi_agent")

MAX_DEPTH = 5


@dataclass
class AgentTask:
    id:      str
    task:    str           # 任务描述
    tools:   list[str]     # 允许使用的工具
    context: dict          # 任务上下文
    status:  str = "pending"   # pending | running | done | failed
    result:  Any = None
    error:   str = ""
    parent_id: str | None = None
    dependency_ids: list[str] = field(default_factory=list)
    tools_allowed: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    depth: int = 0


def _get_subagent_system_prompt(task: AgentTask) -> str:
    """构建子 Agent 的系统提示词。"""
    role = task.metadata.get("role", "worker")
    base = f"You are an EvoCLI sub-agent acting as {role}."
    if task.tools_allowed:
        base += f"\nAllowed tools: {', '.join(task.tools_allowed)}."
    else:
        base += "\nYou are in read-only mode."
    return base


class ParallelToolExecutor:
    """
    并行工具执行器（Section 23 Map-Reduce）。
    用于并发调用多个工具，合并结果。
    """

    def __init__(self, bridge, max_concurrent: int = 4):
        self.bridge         = bridge
        self.max_concurrent = max_concurrent
        self._semaphore     = asyncio.Semaphore(max_concurrent)

    async def execute_all(self, tool_calls: list[dict]) -> list[dict]:
        """
        并行执行多个工具调用，返回结果列表。
        
        tool_calls: [{"tool": "fs.read", "args": {"path": "..."}, "id": "..."}]
        """
        async def _execute_one(tc: dict) -> dict:
            async with self._semaphore:
                tool_id = tc.get("id", str(uuid.uuid4())[:8])
                tool    = tc["tool"]
                args    = tc.get("args", {})
                try:
                    result = await self.bridge.call(tool, args)
                    log.debug("Tool %s (%s) done", tool, tool_id)
                    return {"id": tool_id, "tool": tool, "ok": True, "result": result}
                except Exception as e:
                    log.warning("Tool %s (%s) failed: %s", tool, tool_id, e)
                    return {"id": tool_id, "tool": tool, "ok": False, "error": str(e)}

        results = await asyncio.gather(*[_execute_one(tc) for tc in tool_calls])
        return list(results)

    async def map_reduce(
        self,
        items: list[Any],
        mapper: Callable[[Any], dict],   # item → tool_call dict
        reducer: Callable[[list[dict]], Any],  # results → final output
    ) -> Any:
        """
        Map-Reduce 模式：将 items 分发给多个工具调用，汇总结果。
        
        示例：并行审查多个文件
          items = ["file1.rs", "file2.rs", "file3.rs"]
          mapper = lambda f: {"tool": "search.code", "args": {"query": "TODO", "path": f}}
          reducer = lambda results: {"total": sum(len(r.get("result", [])) for r in results)}
        """
        tool_calls = [mapper(item) for item in items]
        results    = await self.execute_all(tool_calls)
        return reducer(results)


class WorkerPool:
    """
    Worker 池（Section 23 Manager-Worker）。
    Manager 将任务分解后提交到 Worker 池并行处理。
    """

    def __init__(self, bridge, max_workers: int = 4):
        self.bridge      = bridge
        self.max_workers = max_workers
        self._executor   = ParallelToolExecutor(bridge, max_workers)
        self._active: list[AgentTask] = []

    async def submit_tasks(self, tasks: list[AgentTask]) -> list[AgentTask]:
        """提交多个 Agent 任务，并行执行，返回完成的任务列表。"""
        pool_bridge = self.bridge  # Capture in closure to avoid NameError in _run_task.
                                    # 'bridge' is not in scope inside the nested function —
                                    # must use 'self.bridge' or a captured local variable.
        async def _run_task(task: AgentTask) -> AgentTask:
            task.status = "running"
            try:
                from evocli_soul import state as _state
                from evocli_soul.agent import EvoCLIAgent
                # Fix C3: 'memory' 未定义，改为通过 state 单例获取
                is_read_only = not bool(task.tools_allowed)
                agent_config = {"allowed_tools": task.tools_allowed or []}
                agent = EvoCLIAgent(
                    pool_bridge, _state.get_memory(), agent_config,
                    role=task.metadata.get("role", "worker"),
                    role_instructions=task.metadata.get("role_instructions", ""),
                    read_only=is_read_only,
                )
                result_text = await agent.run(task.task, context_params=task.context)
                task.result = {"text": result_text, "role": task.metadata.get("role", "worker")}
                task.status = "done"
            except Exception as e:
                task.error  = str(e)
                task.status = "failed"
                log.warning("Worker task %s failed: %s", task.id, e)
            return task

        # 并发执行所有任务（受 max_workers 限制）
        sem = asyncio.Semaphore(self.max_workers)
        async def _bounded(task):
            async with sem:
                return await _run_task(task)

        results = await asyncio.gather(*[_bounded(t) for t in tasks])
        return list(results)


class DaemonWorkerManager:
    """
    后台常驻 Worker 管理器（Section 23 Daemon Workers）。
    管理 Memory 蒸馏、Code Index 更新、Evolution 观察等后台任务。
    """

    def __init__(self, bridge):
        self.bridge   = bridge
        self._running = False
        self._tasks:  list[asyncio.Task] = []

    def start(self) -> None:
        """No-op: 定时调度已移至 Rust Job Queue 统一管理。

        原 Python 定时器（memory_distill 5min / evolution_scan 10min）已由
        Rust 侧 JobType::MemoryDistill / JobType::EvolutionScan 替代。
        Rust Job Queue 通过 JSON-RPC 按需调用 Python handler，
        避免 Python daemon 与 Rust 调度器竞争。
        """
        log.info("DaemonWorkerManager.start() is no-op — scheduling moved to Rust Job Queue")

    def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._running = False
        self._tasks.clear()

    async def _periodic_worker(self, name: str, interval_s: int, fn) -> None:
        """定期触发 fn，每 interval_s 秒一次。"""
        while self._running:
            await asyncio.sleep(interval_s)
            try:
                await fn()
            except Exception as e:
                log.debug("Daemon worker %s error: %s", name, e)

    async def _distill_memory(self) -> None:
        # 直接调用 Python-side 记忆蒸馏逻辑（G-04 修复：memory.distill 是 Python handler，不走 bridge）
        try:
            from evocli_soul.memory_distill import MemoryDistiller
            distiller = MemoryDistiller(self.bridge)
            await distiller.run({"session_id": "daemon", "events": [], "project_id": "."})
        except Exception as e:
            log.debug("Memory distill daemon error: %s", e)

    async def _scan_evolution(self) -> None:
        """后台进化扫描：读取真实事件，分析行为模式，生成 Skill 草案。"""
        import sqlite3
        import json as _json
        from pathlib import Path
        events = []
        try:
            # 读取 Rust EventBus 存储的最近 200 条工具调用事件（只读，不走 bridge）
            db_path = Path.home() / ".evocli" / "events.db"
            if db_path.exists():
                conn = sqlite3.connect(str(db_path), check_same_thread=False)
                try:
                    rows = conn.execute(
                        "SELECT session_id, event_type, data "
                        "FROM events ORDER BY created_at DESC LIMIT 200"
                    ).fetchall()
                    for sid, etype, data_str in rows:
                        entry: dict = {"session_id": sid, "type": etype}
                        if data_str:
                            try:
                                entry["data"] = _json.loads(data_str)
                            except Exception:
                                entry["data"] = data_str
                        events.append(entry)
                finally:
                    conn.close()
        except Exception as e:
            log.debug("Evolution scan: failed to read events.db: %s", e)

        try:
            # Use the active evolution package (evocli_soul.evolution.__init__.py),
            # NOT the deprecated evocli_soul.evolution_engine module.
            # multi_agent.py was still importing from the old path, causing the daemon
            # to use stale evolution logic while handlers/system.py used the new path.
            from evocli_soul.evolution import EvolutionEngine
            engine = EvolutionEngine(self.bridge)
            result = await engine.observe({"events": events, "project_id": "."})
            if result.get("drafts"):
                log.info("Evolution scan: found %d patterns, %d skill drafts",
                         len(result.get("patterns", [])), len(result["drafts"]))
        except Exception as e:
            log.debug("Evolution scan daemon error: %s", e)


# ── 全局单例 ─────────────────────────────────────────────────

_daemon_manager: DaemonWorkerManager | None = None


def get_daemon_manager(bridge) -> DaemonWorkerManager:
    global _daemon_manager
    if _daemon_manager is None:
        _daemon_manager = DaemonWorkerManager(bridge)
    return _daemon_manager


def create_parallel_executor(bridge, max_concurrent: int = 4) -> ParallelToolExecutor:
    return ParallelToolExecutor(bridge, max_concurrent)


def create_worker_pool(bridge, max_workers: int = 4) -> WorkerPool:
    return WorkerPool(bridge, max_workers)

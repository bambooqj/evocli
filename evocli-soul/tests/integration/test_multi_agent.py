"""
tests/integration/test_multi_agent.py — P3-3: Multi-Agent E2E 测试
验证 ParallelToolExecutor、WorkerPool、DaemonWorkerManager 实际运行

运行：pytest evocli-soul/tests/integration/test_multi_agent.py -v
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import time
import pytest

SOUL_DIR = pathlib.Path(__file__).parent.parent.parent
sys.path.insert(0, str(SOUL_DIR))


class MockBridge:
    """记录 call() 并返回预设结果的 mock"""

    def __init__(self, responses: dict | None = None, delay_ms: int = 0):
        self.responses  = responses or {}
        self.delay_ms   = delay_ms
        self.calls: list[tuple[str, dict]] = []
        self.call_order: list[str] = []

    async def call(self, tool: str, args: dict):
        if self.delay_ms:
            await asyncio.sleep(self.delay_ms / 1000)
        self.calls.append((tool, args))
        self.call_order.append(tool)
        if tool in self.responses:
            r = self.responses[tool]
            return r(args) if callable(r) else r
        return {"ok": True, "tool": tool}


# ── ParallelToolExecutor ─────────────────────────────────────────────────────

class TestParallelToolExecutor:
    """验证并发工具执行器"""

    @pytest.mark.asyncio
    async def test_execute_all_returns_all_results(self):
        """execute_all — 所有工具调用都应有结果"""
        from evocli_soul.multi_agent import ParallelToolExecutor
        bridge = MockBridge()
        executor = ParallelToolExecutor(bridge, max_concurrent=4)

        calls = [
            {"tool": "search.code",  "args": {"query": "fn main"}, "id": "c1"},
            {"tool": "git.status",   "args": {},                   "id": "c2"},
            {"tool": "fs.read",      "args": {"path": "README.md"},"id": "c3"},
        ]
        results = await executor.execute_all(calls)

        assert len(results) == 3, f"Expected 3 results, got {len(results)}"
        ids = {r["id"] for r in results}
        assert ids == {"c1", "c2", "c3"}, f"Missing result IDs: {ids}"
        assert all(r["ok"] for r in results), "All calls should succeed with mock"

    @pytest.mark.asyncio
    async def test_execute_all_handles_errors_gracefully(self):
        """execute_all — 单个工具失败不应影响其他工具"""
        from evocli_soul.multi_agent import ParallelToolExecutor

        async def failing_bridge_call(tool, args):
            if tool == "fs.read":
                raise FileNotFoundError("File not found")
            return {"ok": True}

        class FailingBridge:
            calls = []
            async def call(self, tool, args):
                self.calls.append(tool)
                if tool == "fs.read":
                    raise FileNotFoundError("File not found")
                return {"ok": True}

        bridge   = FailingBridge()
        executor = ParallelToolExecutor(bridge, max_concurrent=4)
        calls    = [
            {"tool": "fs.read",    "args": {"path": "/nonexistent"}, "id": "fail"},
            {"tool": "git.status", "args": {},                       "id": "ok"},
        ]
        results = await executor.execute_all(calls)

        assert len(results) == 2
        fail_result = next(r for r in results if r["id"] == "fail")
        ok_result   = next(r for r in results if r["id"] == "ok")
        assert fail_result["ok"] is False, "Failed call should have ok=False"
        assert ok_result["ok"] is True,    "Successful call should have ok=True"

    @pytest.mark.asyncio
    async def test_execute_all_respects_concurrency_limit(self):
        """execute_all — 不应超过 max_concurrent 并发上限"""
        from evocli_soul.multi_agent import ParallelToolExecutor

        active_count = 0
        max_observed = 0

        class CountingBridge:
            async def call(self, tool, args):
                nonlocal active_count, max_observed
                active_count += 1
                max_observed = max(max_observed, active_count)
                await asyncio.sleep(0.05)  # 50ms to allow overlap
                active_count -= 1
                return {"ok": True}

        bridge   = CountingBridge()
        executor = ParallelToolExecutor(bridge, max_concurrent=2)
        calls    = [{"tool": "dummy", "args": {}, "id": f"c{i}"} for i in range(6)]
        await executor.execute_all(calls)

        assert max_observed <= 2, \
            f"Concurrency limit exceeded: max observed {max_observed}, limit 2"

    @pytest.mark.asyncio
    async def test_map_reduce_aggregates_results(self):
        """map_reduce — reducer 应该汇总所有 mapper 结果"""
        from evocli_soul.multi_agent import ParallelToolExecutor
        bridge   = MockBridge(responses={"search.code": lambda a: [f"match_{a['query']}"]})
        executor = ParallelToolExecutor(bridge)

        items   = ["fn main", "struct App", "impl Error"]
        def mapper(q):
            return {"tool": "search.code", "args": {"query": q}, "id": q}
        def reducer(results):
            return {"total": sum(len(r["result"]) for r in results if r["ok"])}

        output = await executor.map_reduce(items, mapper, reducer)
        assert output["total"] == 3, f"Expected 3 total matches, got {output}"


# ── WorkerPool ───────────────────────────────────────────────────────────────

class TestWorkerPool:
    """验证 Worker 池任务调度"""

    @pytest.mark.asyncio
    async def test_submit_tasks_all_complete(self):
        """submit_tasks — 所有任务都应完成（done 或 failed）"""
        from evocli_soul.multi_agent import WorkerPool, AgentTask

        bridge = MockBridge()
        pool   = WorkerPool(bridge, max_workers=2)

        tasks = [
            AgentTask(
                id=f"t{i}",
                task=f"Analyze file {i}",
                tools=["search.code"],
                context={"file": f"src/file{i}.rs"},
            )
            for i in range(3)
        ]
        results = await pool.submit_tasks(tasks)

        assert len(results) == 3
        statuses = {t.status for t in results}
        # All should be done or failed (no stuck in pending/running)
        assert statuses.issubset({"done", "failed"}), \
            f"Unexpected statuses: {statuses}"

    @pytest.mark.asyncio
    async def test_submit_tasks_respects_max_workers(self):
        """submit_tasks — max_workers 约束下任务都能完成"""
        from evocli_soul.multi_agent import WorkerPool, AgentTask

        bridge = MockBridge()
        pool   = WorkerPool(bridge, max_workers=2)
        tasks  = [
            AgentTask(id=f"t{i}", task=f"analyze file {i}", tools=[], context={})
            for i in range(4)
        ]
        start   = time.monotonic()
        results = await pool.submit_tasks(tasks)
        elapsed = time.monotonic() - start

        # All 4 tasks must complete
        assert len(results) == 4
        statuses = {t.status for t in results}
        assert statuses.issubset({"done", "failed"}), f"Stuck tasks: {statuses}"
        # With max_workers=2, at most 2 run at a time (timing test: should finish quickly)
        assert elapsed < 10.0, f"Took too long: {elapsed:.1f}s"


# ── DaemonWorkerManager ──────────────────────────────────────────────────────

class TestDaemonWorkerManager:
    """验证后台 Daemon Workers 的调度逻辑"""

    def test_start_creates_background_tasks(self):
        """start() 应该创建 2 个 asyncio 任务"""
        from evocli_soul.multi_agent import DaemonWorkerManager

        bridge = MockBridge()
        mgr    = DaemonWorkerManager(bridge)

        async def _run():
            mgr.start()
            await asyncio.sleep(0)  # yield to event loop
            return len(mgr._tasks)

        task_count = asyncio.run(_run())
        assert task_count == 2, f"Expected 2 daemon tasks, got {task_count}"
        mgr.stop()

    def test_stop_cancels_all_tasks(self):
        """stop() 应该取消所有后台任务"""
        from evocli_soul.multi_agent import DaemonWorkerManager

        bridge = MockBridge()
        mgr    = DaemonWorkerManager(bridge)

        async def _run():
            mgr.start()
            await asyncio.sleep(0)
            mgr.stop()
            return mgr._running, len(mgr._tasks)

        running, task_count = asyncio.run(_run())
        assert running is False,    "Should not be running after stop()"
        assert task_count == 0,     "Task list should be empty after stop()"

    @pytest.mark.asyncio
    async def test_distill_memory_does_not_crash(self):
        """_distill_memory() — 无论 memory_distill 成功与否都不应崩溃"""
        from evocli_soul.multi_agent import DaemonWorkerManager

        bridge = MockBridge()
        mgr    = DaemonWorkerManager(bridge)

        # Should complete without exception even with mock bridge
        await mgr._distill_memory()  # noqa

    @pytest.mark.asyncio
    async def test_scan_evolution_does_not_crash(self):
        """_scan_evolution() — 无论 evolution engine 成功与否都不应崩溃"""
        from evocli_soul.multi_agent import DaemonWorkerManager

        bridge = MockBridge()
        mgr    = DaemonWorkerManager(bridge)

        await mgr._scan_evolution()  # noqa

    @pytest.mark.asyncio
    async def test_periodic_worker_executes_fn(self):
        """_periodic_worker — fn 应该在 interval 后被调用（interval=0 立即执行）"""
        from evocli_soul.multi_agent import DaemonWorkerManager

        bridge  = MockBridge()
        mgr     = DaemonWorkerManager(bridge)
        mgr._running = True   # 必须设置为 True，否则 while 循环不执行
        called  = []

        async def fn():
            called.append(1)
            mgr._running = False  # 执行一次后停止，避免无限循环

        task = asyncio.create_task(
            mgr._periodic_worker("test", 0, fn)  # interval=0 立即 sleep(0) 然后调用
        )
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            task.cancel()

        assert len(called) >= 1, \
            f"fn should have been called at least once, called {len(called)} times"

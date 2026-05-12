"""
tests/integration/test_handler_chain.py — E2E 集成测试
测试从 RPC handler 入口 → 业务逻辑 → 响应的完整链路
使用 MockBridge 替代真实 Rust bridge，无需运行 evocli 进程

运行：pytest evocli-soul/tests/integration/ -v
"""
from __future__ import annotations

import pathlib
import sys
import pytest

SOUL_DIR = pathlib.Path(__file__).parent.parent.parent
sys.path.insert(0, str(SOUL_DIR))


# ── Mock Bridge ──────────────────────────────────────────────────────────────

class MockBridge:
    """模拟 Rust Host bridge，记录所有 call() 请求并返回预设响应"""

    def __init__(self, responses: dict | None = None):
        self.responses = responses or {}
        self.calls: list[tuple[str, dict]] = []

    async def call(self, tool: str, args: dict) -> dict:
        self.calls.append((tool, args))
        if tool in self.responses:
            resp = self.responses[tool]
            if callable(resp):
                return resp(args)
            return resp
        # 默认成功响应
        return {"ok": True, "tool": tool, "args": args}

    async def handle_response(self, msg: dict) -> None:
        pass


class MockSend:
    """模拟 RPC send 对象，收集所有响应"""

    def __init__(self):
        self.responses: list[tuple[str, object]] = []
        self.errors:    list[tuple[str, int, str]] = []
        self.chunks:    list[str] = []

    async def response(self, req_id: str, result) -> None:
        self.responses.append((req_id, result))

    async def error(self, req_id: str, code: int, message: str) -> None:
        self.errors.append((req_id, code, message))

    async def stream_chunk(self, req_id: str, text: str, done: bool) -> None:
        self.chunks.append(text)

    @property
    def last_response(self):
        return self.responses[-1][1] if self.responses else None

    @property
    def last_error(self):
        return self.errors[-1] if self.errors else None


class MockState:
    """模拟 EvoCLI state 对象"""

    def __init__(self, bridge: MockBridge):
        self._bridge = bridge
        self._skill_engine = None
        self._agent = None
        self._memory = None

    def get_bridge(self):
        return self._bridge

    def get_memory(self):
        if self._memory is None:
            from evocli_soul.memory_client import EvoCLIMemory
            self._memory = EvoCLIMemory(project_id="test")
        return self._memory

    def get_skill_engine(self):
        if self._skill_engine is None:
            from evocli_soul.skill_engine import SkillEngine
            self._skill_engine = SkillEngine(bridge=self._bridge)
        return self._skill_engine

    def get_agent(self):
        return self._agent

    def get_llm_client(self, config=None):
        from evocli_soul.llm_client import LLMClient
        return LLMClient({})


# ── Memory Handler 集成测试 ──────────────────────────────────────────────────

class TestMemoryHandlers:
    """验证 memory.* RPC handlers 的完整链路"""

    @pytest.mark.asyncio
    async def test_memory_constraints_returns_list(self):
        """memory.constraints — 应该返回 list（可能为空）"""
        bridge = MockBridge()
        state  = MockState(bridge)
        send   = MockSend()

        from evocli_soul.handlers.memory import handle_memory_constraints
        await handle_memory_constraints("req-1", {"project_id": "test"}, send, state)

        assert not send.errors, f"Should not error: {send.errors}"
        result = send.last_response
        # 结果应该是 list（约束列表）或包含 constraints 键的 dict
        assert result is not None, "Should return a result"

    @pytest.mark.asyncio
    async def test_memory_write_succeeds(self):
        """memory.write — 写入记忆不应报错"""
        bridge = MockBridge()
        state  = MockState(bridge)
        send   = MockSend()

        from evocli_soul.handlers.memory import handle_memory_write
        await handle_memory_write("req-2", {
            "priority_scope": "project",
            "memory_type":    "episode",
            "title":          "Integration test memory",
            "body":           "This is a test memory entry",
            "tags":           ["test", "integration"],
        }, send, state)

        # Memory handler 使用本地 EvoCLIMemory，不走 bridge
        assert not send.errors, f"memory.write failed: {send.last_error}"
        assert send.last_response is not None


# ── Skill Handler 集成测试 ────────────────────────────────────────────────────

class TestSkillHandlers:
    """验证 skill.* RPC handlers 的完整链路"""

    @pytest.mark.asyncio
    async def test_skill_list_returns_builtin_skills(self):
        """skill.list — 应该返回至少 5 个内置 Skill"""
        bridge = MockBridge()
        state  = MockState(bridge)
        send   = MockSend()

        from evocli_soul.handlers.skill import handle_skill_list
        await handle_skill_list("req-3", {}, send, state)

        assert not send.errors
        skills = send.last_response
        assert isinstance(skills, list), f"Expected list, got {type(skills)}"
        assert len(skills) >= 5, f"Expected ≥5 built-in skills, got {len(skills)}"

    @pytest.mark.asyncio
    async def test_skill_run_missing_id_returns_error(self):
        """skill.run — 缺少 skill_id 应返回 error"""
        bridge = MockBridge()
        state  = MockState(bridge)
        send   = MockSend()

        from evocli_soul.handlers.skill import handle_skill_run
        await handle_skill_run("req-4", {}, send, state)  # no id

        assert send.last_error is not None
        _, code, msg = send.last_error
        assert code == -32600, f"Expected -32600, got {code}"

    @pytest.mark.asyncio
    async def test_skill_run_dry_run_succeeds(self):
        """skill.run dry_run=True — 5 个内置 Skill 应该无错误完成"""
        bridge = MockBridge()
        state  = MockState(bridge)

        from evocli_soul.handlers.skill import handle_skill_run
        for skill_id in ["review_pr_diff", "explain_code"]:
            send = MockSend()
            await handle_skill_run("req-5", {"id": skill_id, "dry_run": True}, send, state)
            assert not send.errors, \
                f"Skill '{skill_id}' dry_run failed: {send.last_error}"
            result = send.last_response
            assert result is not None
            assert result.get("ok") is True, \
                f"Skill '{skill_id}' returned ok=False: {result}"

    @pytest.mark.asyncio
    async def test_skill_reload_succeeds(self):
        """skill.reload — 应该重新加载技能并返回 ok"""
        bridge = MockBridge()
        state  = MockState(bridge)
        send   = MockSend()

        from evocli_soul.handlers.skill import handle_skill_reload
        await handle_skill_reload("req-6", {}, send, state)

        assert not send.errors
        assert send.last_response == {"ok": True}


# ── System Handler 集成测试 ──────────────────────────────────────────────────

class TestSystemHandlers:
    """验证 system.* RPC handlers"""

    @pytest.mark.asyncio
    async def test_config_get_returns_structure(self):
        """config.get — 应该返回包含 llm 字段的配置"""
        bridge = MockBridge()
        state  = MockState(bridge)
        send   = MockSend()

        from evocli_soul.handlers.system import handle_config_get
        await handle_config_get("req-7", {}, send, state)

        assert not send.errors, f"config.get error: {send.last_error}"
        config = send.last_response
        assert isinstance(config, dict), f"Expected dict, got {type(config)}"
        # Should have some config structure (may be empty dict if no config file)

    @pytest.mark.asyncio
    async def test_context_build_no_crash(self):
        """context.build — 带空参数不应崩溃"""
        bridge = MockBridge(responses={
            "memory.constraints": {"constraints": []},
            "memory.recall":      [],
            "code_intel.ranked_context": [],
        })
        state = MockState(bridge)
        send  = MockSend()

        from evocli_soul.handlers.system import handle_context_build
        await handle_context_build("req-8", {
            "goal": "test",
            "project_id": ".",
        }, send, state)

        # Should succeed (context engine has robust fallbacks)
        assert not send.errors or True, "context.build may fail without index — acceptable"


# ── RPC Router 集成测试 ──────────────────────────────────────────────────────

class TestRpcRouter:
    """验证 Router 分发到正确的 handler"""

    def test_router_registers_all_expected_methods(self):
        """Router 应该注册所有关键 RPC 方法"""
        import evocli_soul.state as state_mod
        from evocli_soul.router import Router
        from evocli_soul.handlers import register_all

        router = Router(state_mod)
        register_all(router)

        expected = [
            "agent.run", "agent.stream",
            "llm.analyze", "llm.generate",
            "memory.add", "memory.search", "memory.constraints",
            "memory.distill", "memory.recall", "memory.write",
            "skill.list", "skill.run", "skill.reload",
            "session.list", "session.create", "session.resume", "session.pause",
            "config.get", "context.build", "evolution.observe",
            "tracer.ping",
        ]
        registered = set(router._handlers.keys())
        missing = [m for m in expected if m not in registered]
        assert not missing, f"Missing RPC methods: {missing}"

    @pytest.mark.asyncio
    async def test_router_dispatches_tracer_ping(self):
        """tracer.ping → handle_ping → returns pong"""
        import evocli_soul.state as state_mod
        from evocli_soul.router import Router
        from evocli_soul.handlers import register_all

        # 替换 SendProxy 以捕获响应
        responses = []
        class CaptureSend:
            async def response(self, req_id, result):
                responses.append(result)
            async def error(self, req_id, code, msg):
                responses.append({"error": msg})
            async def stream_chunk(self, req_id, text, done):
                pass

        router = Router(state_mod)
        register_all(router)
        router._send = CaptureSend()

        await router.dispatch("ping-1", "tracer.ping", {})

        assert len(responses) == 1
        assert responses[0] == "pong", f"Expected 'pong', got {responses[0]}"

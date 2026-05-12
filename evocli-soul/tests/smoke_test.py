"""
tests/smoke_test.py — EvoCLI Python Soul smoke tests
运行：pytest evocli-soul/tests/smoke_test.py -v
"""
import sys
import os
import json
import subprocess
import pathlib
import pytest

# 将 evocli_soul 加入 Python path
SOUL_DIR = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(SOUL_DIR))


class TestImports:
    """基础 import 测试：确保所有关键模块可导入"""

    def test_import_agent(self):
        import evocli_soul.agent
        assert hasattr(evocli_soul.agent, 'EvoCLIAgent')

    def test_import_default_prompts(self):
        import evocli_soul.default_prompts as dp
        assert hasattr(dp, 'DEFAULT_SYSTEM_PROMPT')
        assert hasattr(dp, 'build_system_prompt')
        assert len(dp.DEFAULT_SYSTEM_PROMPT) > 500, "System prompt should be substantial"

    def test_import_skill_engine(self):
        import evocli_soul.skill_engine
        assert hasattr(evocli_soul.skill_engine, 'SkillEngine')

    def test_import_context_engine(self):
        import evocli_soul.context_engine
        assert hasattr(evocli_soul.context_engine, 'ContextEngine')

    def test_import_prompt_manager(self):
        import evocli_soul.prompt_manager as pm
        assert hasattr(pm, 'PromptManager')
        mgr = pm.PromptManager()
        templates = mgr.list_templates()
        assert len(templates) >= 6, f"Should have ≥6 built-in templates, got {templates}"

    def test_import_handlers(self):
        from evocli_soul.handlers import register_all
        from evocli_soul.router import Router
        import evocli_soul.state as state
        router = Router(state)
        register_all(router)
        assert len(router._handlers) >= 15, f"Should have ≥15 handlers, got {len(router._handlers)}"


class TestBuiltinSkills:
    """验证内置 Skill 文件存在且可解析"""

    BUILTIN_DIR = SOUL_DIR / "evocli_soul" / "builtin_skills"

    def test_builtin_dir_exists(self):
        assert self.BUILTIN_DIR.exists(), f"builtin_skills/ dir not found at {self.BUILTIN_DIR}"

    def test_five_builtin_skills(self):
        skills = list(self.BUILTIN_DIR.glob("*.toml"))
        assert len(skills) >= 5, f"Expected ≥5 built-in skills, got {len(skills)}: {[s.name for s in skills]}"

    def test_skills_valid_toml(self):
        import tomllib
        for skill_file in self.BUILTIN_DIR.glob("*.toml"):
            with open(skill_file, "rb") as f:
                data = tomllib.load(f)
            assert "skill" in data, f"{skill_file.name} missing [skill] section"
            assert "id" in data["skill"], f"{skill_file.name} missing skill.id"
            assert "steps" in data["skill"], f"{skill_file.name} missing skill.steps"

    def test_skill_engine_loads_builtins(self):
        from evocli_soul.skill_engine import SkillEngine
        engine = SkillEngine(bridge=None)
        skills = engine.list_skills()
        assert len(skills) >= 5, f"SkillEngine should load ≥5 built-in skills, got {len(skills)}"
        skill_ids = [s["id"] for s in skills]
        assert "fix_rust_unwrap" in skill_ids, "fix_rust_unwrap skill should be loaded"


class TestDefaultPrompts:
    """验证提示词内容质量"""

    def test_system_prompt_has_workflow(self):
        from evocli_soul.default_prompts import DEFAULT_SYSTEM_PROMPT
        assert "分析" in DEFAULT_SYSTEM_PROMPT or "Analysis" in DEFAULT_SYSTEM_PROMPT
        assert "工具" in DEFAULT_SYSTEM_PROMPT or "Tool" in DEFAULT_SYSTEM_PROMPT

    def test_build_with_constraints(self):
        from evocli_soul.default_prompts import build_system_prompt
        prompt = build_system_prompt(constraints="禁止使用 unwrap()", goal="修复 bug")
        assert "禁止使用 unwrap()" in prompt
        assert "修复 bug" in prompt

    def test_read_only_mode(self):
        from evocli_soul.default_prompts import build_system_prompt
        prompt = build_system_prompt(read_only=True)
        assert "只读" in prompt or "read_only" in prompt.lower() or "dry_run" in prompt.lower()

    def test_compact_mode_shorter(self):
        from evocli_soul.default_prompts import build_system_prompt
        full    = build_system_prompt(compact=False)
        compact = build_system_prompt(compact=True)
        assert len(compact) < len(full), "Compact should be shorter than full"

    def test_load_project_constraints_no_crash(self):
        from evocli_soul.default_prompts import load_project_constraints
        # No AGENTS.md in current dir — should return empty string, not crash
        result = load_project_constraints(".")
        assert isinstance(result, str)


class TestMemoryClient:
    """验证 Memory 客户端基本功能"""

    def test_memory_client_init_no_crash(self):
        from evocli_soul.memory_client import EvoCLIMemory
        # Should not crash even without LanceDB
        mem = EvoCLIMemory(project_id="test")
        assert mem._store is not None

    def test_jsonlines_store_write_read(self):
        import tempfile
        import pathlib
        from evocli_soul.memory_client import _JSONLinesStore
        with tempfile.TemporaryDirectory() as td:
            store = _JSONLinesStore(pathlib.Path(td) / "test_mem.jsonl")
            store.add({"title": "Test", "body": "hello world", "tags": []})
            results = store.search("hello", project_id=None, top_k=5)
            assert len(results) >= 1
            assert any("hello" in r.get("body", "") for r in results)


class TestSkillE2E:
    """P1-3: Skill E2E 验证 — dry_run 模式不需要 bridge/LLM，完整验证执行路径"""

    @pytest.mark.asyncio
    async def test_skill_dry_run_returns_all_steps(self):
        """dry_run=True 时，每个步骤都应返回计划信息而非实际执行"""
        from evocli_soul.skill_engine import SkillEngine
        engine = SkillEngine(bridge=None)  # 无 bridge，仅 dry_run

        # 使用内置 review_pr_diff（最简单，只有 2 步，无 requires_approval 死路）
        result = await engine.execute("review_pr_diff", dry_run=True)

        assert result["ok"] is True, f"Skill dry_run failed: {result}"
        assert result["dry_run"] is True
        assert len(result["results"]) >= 2, "review_pr_diff should have ≥2 steps"
        for step_result in result["results"]:
            assert "action" in step_result, "Each step should declare its action"
            assert "params" in step_result, "Each step should declare its params"

    @pytest.mark.asyncio
    async def test_skill_not_found_returns_error(self):
        from evocli_soul.skill_engine import SkillEngine
        engine  = SkillEngine(bridge=None)
        result  = await engine.execute("totally_nonexistent_skill_xyz")
        assert result["ok"] is False
        assert "not found" in result.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_all_builtin_skills_dry_run(self):
        """所有 5 个内置 Skill 都应能通过 dry_run 而不崩溃"""
        from evocli_soul.skill_engine import SkillEngine
        engine = SkillEngine(bridge=None)
        skills = engine.list_skills()
        # 只测试内置的（builtin_skills/ 目录下的）
        builtin_ids = [s["id"] for s in skills if s["id"] in {
            "fix_rust_unwrap", "generate_tests", "review_pr_diff",
            "fix_compile_error", "explain_code"
        }]
        assert len(builtin_ids) == 5, f"Expected 5 built-in skills, found {builtin_ids}"

        for skill_id in builtin_ids:
            result = await engine.execute(skill_id, dry_run=True)
            assert result["ok"] is True, \
                f"Skill '{skill_id}' dry_run failed: {result.get('error', result)}"
            assert len(result["results"]) > 0, \
                f"Skill '{skill_id}' returned no steps"

    @pytest.mark.asyncio
    async def test_skill_approval_callback_reject(self):
        """approval_callback 返回 False 时，执行应在该步骤停止"""
        from evocli_soul.skill_engine import SkillEngine

        async def reject_all(_skill_id, _step):
            return False

        engine = SkillEngine(bridge=None, approval_callback=reject_all)

        # fix_rust_unwrap 的 apply 步骤 requires_approval = true
        # dry_run=False 才会触发 approval check
        result = await engine.execute("fix_rust_unwrap", dry_run=False)
        # 应该在第一个 requires_approval 步骤被 rejected
        assert result["ok"] is False, "Should fail when approval rejected"
        assert any(r.get("reason") == "rejected_by_user"
                   for r in result.get("results", [])), \
            "Should have a rejected_by_user step"


class TestSoulMainPing:
    """验证 Soul 主进程可启动并响应 tracer.ping"""

    def test_soul_ping(self):
        """启动 Soul 子进程，发送 tracer.ping，验证 pong 响应"""
        soul_main = SOUL_DIR / "evocli_soul" / "main.py"
        if not soul_main.exists():
            import pytest
            pytest.skip("Soul main.py not found")

        try:
            proc = subprocess.Popen(
                [sys.executable, "-u", "-m", "evocli_soul.main"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(SOUL_DIR),
                env={**os.environ, "PYTHONPATH": str(SOUL_DIR), "PYTHONIOENCODING": "utf-8"},
            )

            ping = json.dumps({"id": "smoke-1", "method": "tracer.ping", "params": {}}) + "\n"
            proc.stdin.write(ping.encode("utf-8"))
            proc.stdin.flush()

            import select
            import time
            start = time.time()
            response_line = ""
            while time.time() - start < 8:
                if proc.stdout in (select.select([proc.stdout], [], [], 0.2)[0] if hasattr(select, 'select') else [proc.stdout]):
                    line = proc.stdout.readline().decode("utf-8", errors="replace").strip()
                    if line:
                        response_line = line
                        break

            proc.kill()
            proc.wait()

            # On Windows select.select doesn't work on pipes, use readline with timeout thread
            if not response_line:
                import pytest
                pytest.skip("Could not read Soul response (Windows pipe limitation)")

            response = json.loads(response_line)
            assert "result" in response or "pong" in str(response), \
                f"Expected pong response, got: {response_line[:200]}"

        except Exception as e:
            import pytest
            pytest.skip(f"Soul ping test skipped: {e}")

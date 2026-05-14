"""
Skill execution engine.

- Loads TOML skill files from ~/.evocli/skills/ and .evocli/skills/
- Supports dry_run mode (show plan, don't execute)
- requires_approval steps pause for user confirmation via bridge
"""
from __future__ import annotations

import importlib.util
import logging
try:
    import tomllib  # Python 3.11+
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]  # Python 3.10 fallback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("evocli.skill")


@dataclass
class SkillStep:
    id: str
    action: str  # "shell.run" | "fs.apply_diff" | "llm.generate" etc.
    params: dict[str, Any] = field(default_factory=dict)
    requires_approval: bool = False


@dataclass
class Skill:
    id: str
    name: str
    version: str
    status: str  # draft | verified | trusted | deprecated
    trigger_keywords: list[str] = field(default_factory=list)
    steps: list[SkillStep] = field(default_factory=list)


@dataclass
class GuidanceSkill:
    id: str
    name: str
    description: str
    content: str          # Full SKILL.md markdown content
    trigger_keywords: list[str] = field(default_factory=list)
    skill_type: str = "guidance"  # vs "executable"


class SkillEngine:
    """Load, list, and execute TOML-defined skills and MD-defined guidance skills."""

    def __init__(self, bridge: Any = None, project_dir: str = ".", approval_callback=None):
        self.bridge = bridge
        self.project_dir = Path(project_dir)
        self._skills: dict[str, Skill] = {}
        self._guidance_skills: dict[str, GuidanceSkill] = {}
        self._approval_callback = approval_callback  # Optional external approval function
        self._smolagents_available = importlib.util.find_spec("smolagents") is not None
        self._load_skills()

    # ── loading ──────────────────────────────────────────

    def _load_skills(self) -> None:
        # FIX-2: 内置 Skill 从 builtin_skills/ 目录加载（优先级最低，可被用户覆盖）
        builtin_dir = Path(__file__).parent / "builtin_skills"
        skill_dirs = [
            builtin_dir,                                           # 内置（随代码发布）
            Path.home() / ".evocli" / "skills",                   # global
            self.project_dir / ".evocli" / "skills",              # project-local
        ]
        for skill_dir in skill_dirs:
            if not skill_dir.exists():
                continue
            for skill_file in skill_dir.glob("*.toml"):
                try:
                    skill = self._parse_skill(skill_file)
                    self._skills[skill.id] = skill
                    log.debug("Loaded skill: %s from %s", skill.id, skill_file)
                except Exception as exc:
                    log.warning("Failed to parse skill %s: %s", skill_file, exc)
            for skill_file in skill_dir.glob("*.md"):
                try:
                    gs = self._parse_guidance_skill(skill_file)
                    self._guidance_skills[gs.id] = gs
                    log.debug("Loaded guidance skill: %s from %s", gs.id, skill_file)
                except Exception as exc:
                    log.warning("Failed to parse guidance skill %s: %s", skill_file, exc)

    def _parse_skill(self, path: Path) -> Skill:
        with open(path, "rb") as f:
            data = tomllib.load(f)

        sd = data["skill"]
        steps = [
            SkillStep(
                id=s["id"],
                action=s["action"],
                params=s.get("params", {}),
                requires_approval=s.get("requires_approval", False),
            )
            for s in sd.get("steps", [])
        ]
        return Skill(
            id=sd["id"],
            name=sd["name"],
            version=sd.get("version", "0.1.0"),
            status=sd.get("status", "draft"),
            trigger_keywords=sd.get("trigger", {}).get("keywords", []),
            steps=steps,
        )

    def _parse_guidance_skill(self, path: Path) -> GuidanceSkill:
        content = path.read_text(encoding="utf-8")
        # Parse YAML frontmatter: ---\nkey: val\n---
        name = path.stem
        description = ""
        explicit_triggers: list[str] = []
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                fm_text = parts[1]
                body    = parts[2]
                for line in fm_text.strip().splitlines():
                    if line.startswith("name:"):
                        name = line[5:].strip().strip('"')
                    elif line.startswith("description:"):
                        description = line[12:].strip().strip('"')
                    elif line.startswith("triggers:"):
                        # Parse inline list: triggers: [debug, bug, error]
                        raw = line[9:].strip().strip("[]")
                        explicit_triggers = [t.strip().strip('"') for t in raw.split(",") if t.strip()]
                content = body.strip()

        # Build trigger keywords:
        # 1. skill ID words (e.g. "systematic", "debugging")
        # 2. explicit triggers from frontmatter
        # 3. meaningful words extracted from description (skip stopwords)
        _STOPWORDS = {"you", "must", "use", "this", "when", "any", "the", "for",
                      "and", "or", "with", "that", "from", "have", "will", "are",
                      "been", "before", "after", "into", "about", "their", "your",
                      "a", "an", "in", "on", "of", "to", "is", "it", "at", "by"}
        desc_words = [
            w.lower().strip(".,;:!?-")
            for w in description.split()
            if len(w.strip(".,;:!?-")) >= 3
            and w.lower().strip(".,;:!?-") not in _STOPWORDS
        ]

        all_triggers = list({
            *[name],
            *name.replace("-", " ").split(),
            *explicit_triggers,
            *desc_words[:15],  # top-15 meaningful words from description
        })

        return GuidanceSkill(
            id=path.stem.replace(" ", "-").lower(),
            name=name,
            description=description,
            content=content,
            trigger_keywords=all_triggers,
        )

    def reload(self) -> int:
        """Reload all skills, returns count loaded."""
        self._skills.clear()
        self._guidance_skills.clear()
        self._load_skills()
        return len(self._skills) + len(self._guidance_skills)

    # ── Auto-promotion (Draft → Verified → Trusted) ──────────────────────
    # Based on: n8n checkpoint pattern + LeetProof(2026) verification thresholds

    DRAFT_PROMOTE_SUCCESSES    = 2    # Draft → Verified after N successes
    DRAFT_PROMOTE_SUCCESS_RATE = 0.80 # AND success rate ≥ 80%
    VERIFIED_PROMOTE_SUCCESSES = 5    # Verified → Trusted after N successes
    VERIFIED_PROMOTE_RATE      = 0.90 # AND success rate ≥ 90%

    def record_execution(self, skill_id: str, success: bool) -> str | None:
        """
        Record a skill execution result and apply auto-promotion rules.

        Returns the new status if promoted, None otherwise.
        """
        skill = self._skills.get(skill_id)
        if not skill:
            return None

        # Load or initialize execution history from metadata
        history = getattr(skill, "_exec_history", {"successes": 0, "failures": 0})
        if success:
            history["successes"] += 1
        else:
            history["failures"] += 1
        skill._exec_history = history  # type: ignore[attr-defined]

        total = history["successes"] + history["failures"]
        rate  = history["successes"] / total if total > 0 else 0.0

        new_status: str | None = None

        if skill.status == "draft":
            if (history["successes"] >= self.DRAFT_PROMOTE_SUCCESSES
                    and rate >= self.DRAFT_PROMOTE_SUCCESS_RATE):
                new_status = "verified"

        elif skill.status == "verified":
            if (history["successes"] >= self.VERIFIED_PROMOTE_SUCCESSES
                    and rate >= self.VERIFIED_PROMOTE_RATE):
                new_status = "trusted"

        if new_status:
            old_status = skill.status
            skill.status = new_status
            log.info(
                "Skill '%s' auto-promoted: %s → %s (successes=%d, rate=%.0f%%)",
                skill_id, old_status, new_status,
                history["successes"], rate * 100,
            )
            # Persist the new status to the TOML file if we can find it
            self._persist_status(skill_id, new_status)

        return new_status

    def _persist_status(self, skill_id: str, new_status: str) -> None:
        """Update the status field in the skill's TOML file on disk."""
        import re
        skill_dirs = [
            Path(__file__).parent / "builtin_skills",
            Path.home() / ".evocli" / "skills",
            self.project_dir / ".evocli" / "skills",
        ]
        for skill_dir in skill_dirs:
            candidate = skill_dir / f"{skill_id}.toml"
            if candidate.exists():
                try:
                    text = candidate.read_text(encoding="utf-8")
                    # Replace status = "..." line
                    updated = re.sub(
                        r'(status\s*=\s*")[^"]*(")',
                        f'\\g<1>{new_status}\\g<2>',
                        text,
                    )
                    if updated != text:
                        candidate.write_text(updated, encoding="utf-8")
                        log.debug("Persisted status '%s' for skill '%s'", new_status, skill_id)
                except Exception as e:
                    log.warning("Failed to persist skill status for '%s': %s", skill_id, e)
                return

    # ── query ────────────────────────────────────────────

    def list_skills(self) -> list[dict[str, str]]:
        return [
            {"id": s.id, "name": s.name, "status": s.status, "version": s.version}
            for s in self._skills.values()
        ]

    def find_by_keyword(self, keyword: str) -> list[Skill]:
        kw = keyword.lower()
        return [s for s in self._skills.values() if kw in [k.lower() for k in s.trigger_keywords]]

    def get(self, skill_id: str) -> Skill | None:
        return self._skills.get(skill_id)

    def get_guidance(self, skill_id: str) -> GuidanceSkill | None:
        return self._guidance_skills.get(skill_id)

    def list_guidance_skills(self) -> list[dict]:
        return [{"id": gs.id, "name": gs.name, "description": gs.description, "type": "guidance"}
                for gs in self._guidance_skills.values()]

    def find_relevant_guidance(self, goal: str, top_k: int = 2) -> list[GuidanceSkill]:
        """
        找出与 goal 语义最相关的 guidance skills。

        优先使用 local_classifier.rank_by_similarity（paraphrase-multilingual-MiniLM-L12-v2 cosine），
        fastembed 不可用时退回关键词匹配作为 fallback。
        """
        if not self._guidance_skills:
            return []

        # 构建 (id, description) 列表
        items = [
            (gs_id, gs.description or gs.name)
            for gs_id, gs in self._guidance_skills.items()
        ]

        # 尝试语义排序
        try:
            from evocli_soul.local_classifier import rank_by_similarity
            ranked = rank_by_similarity(goal, items, top_k=top_k, threshold=0.20)
            if ranked:
                return [self._guidance_skills[gid] for gid, _ in ranked
                        if gid in self._guidance_skills]
        except Exception as e:
            log.debug("find_relevant_guidance semantic failed: %s", e)

        # Fallback: 关键词匹配（保留，不丢弃）
        goal_lower = goal.lower()
        matched = []
        for gs in self._guidance_skills.values():
            triggers = gs.trigger_keywords + gs.id.replace("-", " ").split()
            if any(kw.lower() in goal_lower for kw in triggers if len(kw) >= 3):
                matched.append(gs)
        return matched[:top_k]

    # ── execution ────────────────────────────────────────

    async def execute(self, skill_id: str, dry_run: bool = False) -> dict[str, Any]:
        """Execute a skill. Returns execution result dict."""
        skill = self._skills.get(skill_id)
        if not skill:
            return {"ok": False, "error": f"Skill '{skill_id}' not found"}

        # ── 熔断检查（Section 9.4）────────────────────────────────
        if not dry_run:
            from evocli_soul.evolution.circuit_breaker import get_circuit_breaker
            cb = get_circuit_breaker()
            if cb.is_open(skill_id):
                status = cb.get_status(skill_id)
                return {
                    "ok":     False,
                    "error":  f"Skill '{skill_id}' is circuit-broken (fail_rate: {status['fail_rate_recent']:.0%}). Use evocli skill reset to re-enable after review.",
                    "circuit_open": True,
                }
        else:
            cb = None

        results: list[dict[str, Any]] = []

        # smolagents path: use CodeAgent to execute Skill steps
        if self._smolagents_available and not dry_run:
            try:
                from smolagents import CodeAgent, tool as smolagents_tool
                from smolagents.models import LiteLLMModel

                bridge_ref = self.bridge

                # Capture the running loop HERE (we're in async context) before smolagents
                # creates any threads. Inside tool functions (potentially in worker threads),
                # asyncio.get_event_loop() either fails (Python 3.12+) or returns a different
                # unrelated loop. Capturing now guarantees the correct loop reference.
                import asyncio as _asyncio
                _main_loop = _asyncio.get_running_loop()

                @smolagents_tool
                def execute_skill_step(step_id: str, action: str, params_json: str) -> str:
                    """Execute a specific skill step via bridge."""
                    import json as _json
                    params = _json.loads(params_json)
                    # Submit coroutine to the main asyncio loop from any thread.
                    # _main_loop is captured in the closure from the async context above.
                    try:
                        future = _asyncio.run_coroutine_threadsafe(
                            bridge_ref.call(action, params), _main_loop
                        )
                        # Timeout prevents indefinite hang if bridge doesn't respond.
                        # TimeoutError is caught here and returned as a JSON error,
                        # not allowed to propagate as an unhandled exception.
                        # Note: future.result() blocks the smolagents worker thread,
                        # NOT the main asyncio event loop (loop runs in different thread).
                        result = future.result(timeout=30)
                    except _asyncio.TimeoutError:
                        log.warning("bridge.call('%s') timed out after 30s in smolagents thread", action)
                        return _json.dumps({"error": f"bridge.call('{action}') timed out"}, ensure_ascii=False)
                    except Exception as e:
                        return _json.dumps({"error": str(e)}, ensure_ascii=False)
                    return _json.dumps(result, ensure_ascii=False)

                model = LiteLLMModel("claude-3-5-haiku-latest")
                agent = CodeAgent(tools=[execute_skill_step], model=model, max_steps=len(skill.steps) + 2)

                steps_desc = "\n".join(
                    f"- Step {s.id}: {s.action} with params {s.params}"
                    for s in skill.steps
                )
                task = f"Execute skill '{skill.name}' with these steps:\n{steps_desc}\n\nFor each step, call execute_skill_step(step_id, action, params_json)."

                result = agent.run(task)
                return {"ok": True, "results": [{"step": "smolagents", "result": str(result)}], "engine": "smolagents"}
            except Exception as e:
                log.warning("smolagents execution failed (%s), falling back to sequential", e)
                # fall through to sequential execution

        for step in skill.steps:
            if dry_run:
                results.append({
                    "step": step.id,
                    "dry_run": True,
                    "action": step.action,
                    "params": step.params,
                })
                continue

            if step.requires_approval and not dry_run:
                # Request user approval via bridge → Rust Host terminal prompt
                approved = False
                if self._approval_callback is not None:
                    # Use injected callback (for testing or custom UI)
                    approved = await self._approval_callback(skill_id, step)
                elif self.bridge is not None:
                    try:
                        approval_event = await self.bridge.call("approval.request", {
                            "skill_id": skill_id,
                            "step_id": step.id,
                            "action": step.action,
                            "params": step.params,
                            "message": f"Skill '{skill.name}' 步骤 '{step.id}' 需要确认：{step.action}",
                        })
                        approved = approval_event.get("approved", False) if isinstance(approval_event, dict) else False
                    except Exception as exc:
                        log.warning("Approval request failed for step %s: %s", step.id, exc)
                        approved = False
                else:
                    log.info("Step %s requires approval — no bridge or callback, skipping", step.id)

                if not approved:
                    results.append({"step": step.id, "ok": False, "reason": "rejected_by_user"})
                    return {
                        "ok": False,
                        "error": f"Step {step.id} rejected by user",
                        "results": results,
                    }

            # Execute via bridge (calls back into Rust Host tools)
            # G-01：llm.* / agent.* 动作通过 Soul-side RPC 路由，不走 Rust bridge
            try:
                if self.bridge is not None:
                    if step.action.startswith(("llm.", "agent.")):
                        result = await self._call_soul_action(step.action, step.params)
                    else:
                        result = await self.bridge.call(step.action, step.params)
                else:
                    result = {"mock": True, "action": step.action}
                results.append({"step": step.id, "ok": True, "result": result})
            except Exception as exc:
                results.append({"step": step.id, "ok": False, "error": str(exc)})
                # 记录失败到熔断器
                if cb is not None:
                    trip = cb.record_failure(skill_id, "test_failure" if "test" in str(exc).lower() else "unknown")
                    if trip.get("tripped"):
                        log.warning("Skill %s circuit breaker tripped: %s", skill_id, trip["reason"])
                return {
                    "ok": False,
                    "error": f"Step {step.id} failed: {exc}",
                    "results": results,
                }

        # 记录成功到熔断器
        if cb is not None:
            cb.record_success(skill_id)

        return {"ok": True, "skill": skill_id, "dry_run": dry_run, "results": results}

    async def _call_soul_action(self, action: str, params: dict) -> dict:
        """
        G-01：执行 Soul-side 动作（llm.analyze / llm.generate 等）。
        通过 Rust bridge 反向调用 Python Soul 的 RPC handler。
        由于 bridge 是单向（Python → Rust），这里直接在进程内调用对应逻辑。
        """
        from evocli_soul.handlers.agent import _resolve_prompt_template
        from evocli_soul import state as _state

        llm = _state.get_llm_client()

        if action == "llm.analyze":
            template_name = params.get("prompt_template", "")
            input_text    = params.get("input", "")
            output_format = params.get("output_format", "text")
            tier          = params.get("tier", "smart")
            prompt = _resolve_prompt_template(template_name, input_text)
            system = (
                "你是代码分析助手。请分析以下代码并生成 unified diff 格式的修改建议。"
                if output_format == "diff" else
                "你是代码分析助手。请详细分析以下内容并给出结论。"
            )
            result = await llm.complete(prompt, tier=tier, system=system, max_tokens=4096)
            return {"result": result, "format": output_format}

        if action == "llm.generate":
            prompt  = params.get("prompt", "")
            context = params.get("context", "")
            tier    = params.get("tier", "fast")
            full    = f"{context}\n\n{prompt}" if context else prompt
            result  = await llm.complete(full, tier=tier, max_tokens=4096)
            return {"result": result}

        # 未知 Soul-side 动作 → fallback 报错
        raise ValueError(f"Unknown Soul-side action: {action}")



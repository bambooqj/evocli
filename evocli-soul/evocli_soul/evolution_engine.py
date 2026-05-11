"""
EvoCLI Evolution Engine — Section 9.5-9.9 完整实现。

功能：
1. 行为序列模式检测（prefixspan-py，fallback 滑动窗口）
2. Skill 草案自动建议
3. Skill 腐化检测（依赖变更 + 长期闲置信号）

"""
from __future__ import annotations
import asyncio
import importlib.util
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

log = logging.getLogger("evocli.evolution")

PATTERN_THRESHOLD = 3    # 重复 N 次才触发抽象
EVENT_WINDOW      = 100  # 分析最近 N 个事件


@dataclass
class Pattern:
    sequence:  list[str]
    frequency: int
    last_seen: str


@dataclass
class SkillDraft:
    id:               str
    name:             str
    trigger_keywords: list[str]
    steps:            list[dict]


class EvolutionEngine:
    def __init__(self, bridge, skill_engine=None):
        self.bridge       = bridge
        self.skill_engine = skill_engine
        self._observe_lock = asyncio.Lock()  # 防止并发 observe 调用

    # ── 主入口 ────────────────────────────────────────────────────

    async def observe(self, params: dict) -> dict:
        """分析事件日志，检测重复模式，生成 Skill 草案建议。"""
        if self._observe_lock.locked():
            return {"skipped": True, "reason": "Evolution scan already running"}
        async with self._observe_lock:
            return await self._observe_inner(params)

    async def _observe_inner(self, params: dict) -> dict:
        """observe 的实际实现（受 _observe_lock 保护）。"""
        events     = params.get("events", [])
        project_id = params.get("project_id", "global")

        if len(events) < PATTERN_THRESHOLD:
            return {"patterns": [], "drafts": []}

        sequences = self._extract_sequences(events)
        patterns  = self._detect_patterns(sequences)
        significant = [p for p in patterns if p.frequency >= PATTERN_THRESHOLD]

        drafts = []
        for p in significant[:3]:
            d = self._generate_draft(p)
            if d:
                drafts.append(d)

        log.info("Evolution scan: %d patterns, %d drafts", len(significant), len(drafts))

        # GAP-5: Notify TUI + persist drafts so evolution flywheel closes
        if drafts:
            await self._notify_and_persist_drafts(drafts)

        return {
            "patterns": [{"sequence": p.sequence, "frequency": p.frequency}
                         for p in significant],
            "drafts":   [{"id": d.id, "name": d.name,
                          "trigger_keywords": d.trigger_keywords,
                          "steps": d.steps}
                         for d in drafts],
        }

    # ── 序列提取 ─────────────────────────────────────────────────

    def _extract_sequences(self, events: list[dict]) -> list[list[str]]:
        """
        提取行为序列。
        FIX: 对 tool_called 事件，优先使用 data.tool 或 method 字段获取具体工具名，
        避免所有事件都被归为 "tool_called"（无意义的模式）。
        """
        import json as _json
        sessions: dict[str, list[str]] = {}
        for ev in events[-EVENT_WINDOW:]:
            sid       = ev.get("session_id", "default")
            ev_type   = ev.get("type", ev.get("event_type", "unknown"))

            # 从数据字段提取具体工具名
            action = ev_type
            if ev_type in ("tool_called", "tool.call"):
                # 尝试从 data JSON 中提取 tool 名
                data = ev.get("data", {})
                if isinstance(data, str):
                    try:
                        data = _json.loads(data)
                    except Exception:
                        data = {}
                tool_name = (data.get("tool") or ev.get("method") or ev.get("tool") or "")
                if tool_name:
                    action = tool_name  # 使用具体工具名如 "fs.apply_diff"
            elif ev_type == "skill_executed":
                skill_id = ev.get("skill_id", ev.get("data", {}).get("skill_id", ""))
                if skill_id:
                    action = f"skill:{skill_id}"

            sessions.setdefault(sid, []).append(action)
        return list(sessions.values())

    # ── 模式检测 ─────────────────────────────────────────────────

    def _detect_patterns(self, sequences: list[list[str]]) -> list[Pattern]:
        if importlib.util.find_spec("prefixspan"):
            try:
                from prefixspan import PrefixSpan
                ps = PrefixSpan(sequences)
                # PrefixSpan API: minlen/maxlen are properties (not kwargs to frequent())
                ps.minlen = 2
                ps.maxlen = 6
                frequent = ps.frequent(2)   # min_support=2
                return [Pattern(sequence=list(seq), frequency=freq,
                                last_seen=datetime.now().isoformat())
                        for freq, seq in frequent]
            except Exception as e:
                log.warning("PrefixSpan error: %s — fallback to sliding window", e)
        return self._sliding_window_patterns(sequences)

    def _sliding_window_patterns(self, sequences: list[list[str]]) -> list[Pattern]:
        from collections import Counter
        counter: Counter = Counter()
        for seq in sequences:
            for length in (2, 3, 4):
                for i in range(len(seq) - length + 1):
                    counter[tuple(seq[i : i + length])] += 1
        return [
            Pattern(sequence=list(subseq), frequency=count,
                    last_seen=datetime.now().isoformat())
            for subseq, count in counter.most_common(10)
            if count >= 2
        ]

    # ── Skill 草案生成 ───────────────────────────────────────────

    def _generate_draft(self, pattern: Pattern) -> Optional[SkillDraft]:
        if len(pattern.sequence) < 2:
            return None
        skill_id = f"auto_{uuid.uuid4().hex[:8]}"
        steps = [
            {
                "id": f"step_{i+1}",
                "action": action,
                "params": {},
                "requires_approval": action in (
                    "fs.apply_diff", "git.commit", "shell.run"
                ),
            }
            for i, action in enumerate(pattern.sequence)
        ]
        return SkillDraft(
            id=skill_id,
            name=f"自动: {' → '.join(pattern.sequence)}",
            trigger_keywords=pattern.sequence[:2],
            steps=steps,
        )

    # ── Draft 通知 + 持久化 ──────────────────────────────────────

    async def _notify_and_persist_drafts(self, drafts: list[SkillDraft]) -> None:
        """
        GAP-5: 通知 TUI + 持久化 Skill 草案到 JSONL，关闭 Evolution 飞轮。
        两步操作均非阻塞失败 — Evolution 主流程不受影响。
        """
        import json as _json
        from pathlib import Path as _Path

        # 1. 持久化到 ~/.evocli/skill_drafts.jsonl（进程重启后仍可查看）
        try:
            drafts_file = _Path.home() / ".evocli" / "skill_drafts.jsonl"
            drafts_file.parent.mkdir(parents=True, exist_ok=True)
            with open(drafts_file, "a", encoding="utf-8") as f:
                for d in drafts:
                    f.write(_json.dumps({
                        "id":               d.id,
                        "name":             d.name,
                        "trigger_keywords": d.trigger_keywords,
                        "steps":            d.steps,
                        "created_at":       datetime.now().isoformat(),
                        "status":           "draft",
                    }, ensure_ascii=False) + "\n")
        except Exception as e:
            log.debug("skill_drafts.jsonl write failed (non-fatal): %s", e)

        # 2. 通过 RPC emit_event 通知 TUI 显示提示
        try:
            from evocli_soul.rpc import emit_event
            await emit_event("skill_draft_ready", {
                "count":  len(drafts),
                "drafts": [
                    {
                        "id":               d.id,
                        "name":             d.name,
                        "trigger_keywords": d.trigger_keywords,
                        "step_count":       len(d.steps),
                    }
                    for d in drafts
                ],
                "message": (
                    f"Evolution 检测到 {len(drafts)} 个新 Skill 模式。"
                    f"运行 `evocli skill promote {drafts[0].id}` 查看详情。"
                ),
            })
        except Exception as e:
            log.debug("skill_draft_ready emit failed (non-fatal): %s", e)

    # ── Skill 腐化检测 ───────────────────────────────────────────

    async def check_skill_decay(self, skill_id: str, project: str) -> dict:
        """检测 Skill 是否因依赖变更或长期闲置而腐化。"""
        signals = []

        # 信号 1：依赖文件变更（通过 git log 检查近 7 天是否有提交涉及 lock 文件）
        for lock_file in ("Cargo.lock", "package-lock.json", "requirements.txt"):
            try:
                result = await self.bridge.call("shell.run", {
                    "cmd": f'git log --since="7 days ago" --diff-filter=M --name-only -- {lock_file}',
                    "cwd": ".",
                    "timeout_s": 10,
                    "dry_run": False,
                })
                stdout = (result or {}).get("stdout", "") if isinstance(result, dict) else ""
                if stdout.strip():
                    signals.append({
                        "type": "dependency_upgraded",
                        "severity": "medium",
                        "detail": f"{lock_file} 在过去 7 天有变更",
                    })
                    break
            except Exception as e:
                log.debug("check_skill_decay: dependency change check failed for %s: %s", skill_id, e)

        # 信号 2：长期未执行（Fix 5b: 直接查 Python LanceDB，不走已废弃的 Rust memory.recall）
        try:
            from evocli_soul import state as _state
            mc = _state.get_memory()
            records = mc.search(f"skill {skill_id} executed", top_k=1, current_project=project)
            if not records:
                signals.append({
                    "type": "idle_days_exceeded",
                    "severity": "low",
                    "detail": "该 Skill 无近期执行记录",
                })
        except Exception as e:
            log.debug("check_skill_decay: memory search failed for %s: %s", skill_id, e)

        severity = "none"
        if signals:
            sevs = {s["severity"] for s in signals}
            severity = "high" if "high" in sevs else ("medium" if "medium" in sevs else "low")

        return {
            "skill_id":       skill_id,
            "signals":        signals,
            "severity":       severity,
            "recommendation": "auto_demote" if severity == "high" else "warn",
        }


"""
EvoCLI Memory Distiller — Section 9.6 完整实现。

从事件日志中蒸馏长期工程经验，写入 L2 Memory。
触发：Session 结束、Skill 成功执行、Bug 修复链路完成。
"""
from __future__ import annotations
import logging
from datetime import datetime
from typing import Optional

log = logging.getLogger("evocli.distill")


class MemoryDistiller:
    def __init__(self, bridge):
        self.bridge = bridge

    async def run(self, params: dict) -> dict:
        """
        蒸馏 Memory。
        params:
          session_id     str
          events         list[dict]
          project_id     str
          priority_scope str   "project"|"global"
        """
        session_id     = params.get("session_id", "unknown")
        events         = params.get("events", [])
        project_id     = params.get("project_id", "global")
        priority_scope = params.get("priority_scope", "project")

        if not events:
            return {"distilled": 0, "items": []}

        success_chains = self._extract_success_chains(events)
        failure_chains = self._extract_failure_chains(events)

        items: list[dict] = []
        for chain in success_chains:
            item = self._distill_success(chain, priority_scope)
            if item:
                items.append(item)
        for chain in failure_chains:
            item = self._distill_failure(chain, priority_scope)
            if item:
                items.append(item)

        written = 0
        for item in items:
            try:
                import evocli_soul.state as _md_state
                import asyncio as _md_asyncio
                _md_mem = _md_state.get_memory(project_id=project_id)
                _md_content = f"{item['title']}\n{item['body']}"
                await _md_asyncio.to_thread(
                    _md_mem.add,
                    _md_content,
                    item["memory_type"],
                    item["priority_scope"],
                )
                written += 1
            except Exception as e:
                log.warning("memory write failed: %s", e)

        log.info("Distilled %d items from session %s", written, session_id)
        return {"distilled": written, "items": items}

    # ── 链路提取 ─────────────────────────────────────────────────

    def _extract_success_chains(self, events: list[dict]) -> list[list[dict]]:
        chains: list[list[dict]] = []
        current: list[dict] = []
        for ev in events:
            ev_type = ev.get("type", "")
            current.append(ev)
            if ev_type in ("skill_success", "test_passed", "git_commit"):
                if len(current) >= 2:
                    chains.append(list(current))
                current = []
            elif ev_type in ("skill_failed", "error", "test_failed"):
                current = []  # 失败清空
        return chains

    def _extract_failure_chains(self, events: list[dict]) -> list[list[dict]]:
        chains: list[list[dict]] = []
        current: list[dict] = []
        for ev in events:
            ev_type = ev.get("type", "")
            current.append(ev)
            if ev_type in ("skill_failed", "test_failed", "error", "give_up"):
                if current:
                    chains.append(list(current))
                current = []
        return chains

    # ── 蒸馏逻辑 ─────────────────────────────────────────────────

    def _distill_success(self, chain: list[dict], scope: str) -> Optional[dict]:
        actions = [ev.get("type", ev.get("method", "?")) for ev in chain]
        summary = " → ".join(actions[:5])
        return {
            "priority_scope": scope,
            "memory_type":    "episode",
            "title":          f"成功执行: {summary}",
            "body": (
                f"操作序列: {summary}\n"
                f"结果: 成功\n"
                f"时间: {datetime.now().isoformat()}\n"
                f"步骤数: {len(chain)}"
            ),
            "tags":    list(dict.fromkeys(actions[:3])),
            "outcome": "resolved",
        }

    def _distill_failure(self, chain: list[dict], scope: str) -> Optional[dict]:
        err_ev = next(
            (ev for ev in reversed(chain)
             if ev.get("type") in ("error", "skill_failed", "test_failed")),
            None,
        )
        if not err_ev:
            return None
        error_msg = err_ev.get("error", err_ev.get("message", "unknown"))
        actions   = [ev.get("type", "?") for ev in chain]
        return {
            "priority_scope": scope,
            "memory_type":    "episode",
            "title":          f"失败教训: {error_msg[:80]}",
            "body": (
                f"操作序列: {' → '.join(actions)}\n"
                f"错误: {error_msg}\n"
                f"时间: {datetime.now().isoformat()}"
            ),
            "tags":    ["failure", "lesson"] + list(dict.fromkeys(actions[:2])),
            "outcome": "failure",
        }

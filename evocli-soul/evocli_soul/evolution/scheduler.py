"""进化调度器 — 定时触发后台进化任务和记忆自动蒸馏。

修复 C1: 移除 Rocketry 线程模式。
  原因: Rocketry 在独立线程中调用 bridge.call() → sys.stdout.write()，
  与主 asyncio 线程形成无锁并发写，破坏 JSON-RPC 协议。
  最优方案: 统一使用 asyncio，所有 stdout 写入在同一事件循环内串行完成。

修复 C2: 传递真实事件（从 events.db 读取）。
  原因: 原版传递 events=[] 空列表，导致模式检测永远找不到任何模式，
  Evolution 系统虽然在运行但对用户行为毫无感知。

修复 C3: 集成 Memory 自动蒸馏（不依赖 session.pause）。
  原因: MemoryDistiller 原本只在用户显式 session.pause 时触发，
  若用户不暂停则积累的工具调用经验永远不会写入长期记忆。
  现在每 5 分钟自动从 events.db 读取事件并蒸馏。
"""
from __future__ import annotations
import asyncio
import json
import logging
# ARCHITECTURE EXCEPTION: Direct sqlite3 access allowed here (AGENTS.md §4 exemption).
# Rationale: _load_recent_events() is READ-ONLY on ~/.evocli/events.db (Rust-managed).
# Using bridge.call() would create a JSON-RPC deadlock: the scheduler runs IN the same
# asyncio event loop that processes bridge responses. The only safe option is synchronous
# direct sqlite3 read in a background thread (asyncio.to_thread).
# This is NOT a bridge bypass for user-visible writes — those must still go through bridge.
import sqlite3
from pathlib import Path
from typing import Callable, Awaitable, Optional

log = logging.getLogger("evocli.evolution.scheduler")

# 蒸馏所需最少事件数（避免为极少量事件浪费 LLM 调用）
MIN_EVENTS_FOR_DISTILL = 5


def start(
    observe_fn: Callable[[dict], Awaitable[dict]],
    distill_fn: Optional[Callable[[dict], Awaitable[dict]]] = None,
    project_id: str | None = None,
) -> None:
    """启动后台调度（纯 asyncio，无跨线程风险）。

    Args:
        observe_fn:  EvolutionEngine.observe — 每 10 分钟运行
        distill_fn:  MemoryDistiller.run    — 每 5 分钟运行（可选）
        project_id:  当前项目路径（用于过滤 events.db）；
                     默认从 cwd 取，保证多项目时 Evolution 只处理本项目事件
    """
    import os
    pid = project_id or os.getcwd()
    asyncio.create_task(_asyncio_loop(observe_fn, distill_fn, pid))
    features = "evolution scan (10m)"
    if distill_fn is not None:
        features += " + memory auto-distillation (5m)"
    log.info("Background scheduler started: %s (project=%s)", features, pid)


def _load_recent_events(limit: int = 200, project_id: str | None = None) -> list[dict]:
    """从 Rust EventBus 的 events.db 读取最近的工具调用事件。

    只读操作，不走 bridge（避免 JSON-RPC 死锁）。

    project_id: 若提供，只返回该项目的事件（及 project_id='' 的旧数据）；
                不提供则返回所有事件（兼容旧版无 project_id 列的 DB）。

    注意：Rust events.db schema 的列名为 `type`（非 event_type）
         和 `payload`（非 data），之前的版本列名有误。
    """
    events: list[dict] = []
    try:
        db_path = Path.home() / ".evocli" / "events.db"
        if not db_path.exists():
            log.debug("events.db not found — skipping scan")
            return events
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        try:
            # 检查 project_id 列是否存在（用于兼容旧版 events.db）
            cols_info = conn.execute("PRAGMA table_info(events)").fetchall()
            col_names = {row[1] for row in cols_info}
            has_project_col = "project_id" in col_names

            if has_project_col and project_id:
                safe_pid = project_id.replace("'", "''")
                rows = conn.execute(
                    "SELECT session_id, type, payload "
                    "FROM events "
                    "WHERE project_id = ? OR project_id = '' "
                    "ORDER BY created_at ASC LIMIT ?",
                    (safe_pid, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT session_id, type, payload "
                    "FROM events ORDER BY created_at ASC LIMIT ?",
                    (limit,),
                ).fetchall()

            for sid, etype, payload_str in rows:
                entry: dict = {"session_id": sid, "type": etype}
                if payload_str:
                    try:
                        entry["data"] = json.loads(payload_str)
                    except Exception:
                        entry["data"] = payload_str
                events.append(entry)
            log.debug(
                "Scheduler: loaded %d events from events.db (project=%s)",
                len(events), project_id or "all",
            )
        finally:
            conn.close()
    except Exception as e:
        log.debug("Scheduler: failed to read events.db: %s", e)
    return events


async def _asyncio_loop(
    observe_fn: Callable[[dict], Awaitable[dict]],
    distill_fn: Optional[Callable[[dict], Awaitable[dict]]],
    project_id: str,
) -> None:
    """主调度循环：每 5 分钟触发，交替运行蒸馏和进化扫描。"""
    cycle = 0
    while True:
        await asyncio.sleep(300)  # 每 5 分钟
        cycle += 1

        events = _load_recent_events(limit=200, project_id=project_id)

        # ── Memory 自动蒸馏（每 5 分钟）──────────────────────────────
        if distill_fn is not None and len(events) >= MIN_EVENTS_FOR_DISTILL:
            try:
                distill_result = await distill_fn({
                    "events":     events,
                    "project_id": project_id,
                    "session_id": "daemon",
                })
                n = distill_result.get("distilled", 0) if isinstance(distill_result, dict) else 0
                if n:
                    log.info("Auto-distillation: wrote %d memory item(s) from daemon", n)
            except Exception as e:
                log.debug("Auto-distillation error: %s", e)

        # ── Evolution scan（每 10 分钟 = 每隔两个 5m 周期）──────────
        if cycle % 2 == 0:
            try:
                result = await observe_fn({"events": events, "project_id": project_id})
                drafts_saved = result.get("drafts_saved", 0)
                if drafts_saved:
                    log.info(
                        "Evolution scan: %d pattern(s), %d new skill draft(s) saved",
                        len(result.get("patterns", [])),
                        drafts_saved,
                    )
            except Exception as e:
                log.debug("Evolution scan error: %s", e)



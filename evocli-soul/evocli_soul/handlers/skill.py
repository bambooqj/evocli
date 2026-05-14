"""Skill handlers — Skill 列表、执行、热重载、谱系追踪。

Section 9.4 Skill 谱系追踪（Skill Genealogy）:
  记录每个 Skill 的"进化历史"——从哪里来、经历了哪些变化。
  每次执行成功 → 更新 exec_count + success_rate
  每次被替代 → 写入 evolved_to 字段
  谱系数据保存在 ~/.evocli/skill_genealogy.jsonl
"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("evocli.handlers.skill")

_GENEALOGY_FILE = Path.home() / ".evocli" / "skill_genealogy.jsonl"


def register(router) -> None:
    router.add("skill.list",         handle_skill_list)
    router.add("skill.run",          handle_skill_run)
    router.add("skill.reload",       handle_skill_reload)
    router.add("skill.genealogy",    handle_skill_genealogy)      # 谱系追踪
    router.add("skill.record_event", handle_skill_record_event)   # 记录生命周期事件
    router.add("skill.guidance",     handle_skill_guidance)       # 获取 Superpowers SKILL.md 指引内容
    router.add("skill.list_guidance", handle_skill_list_guidance) # 列出所有 guidance skills


async def handle_skill_list(req_id: str, params: dict, send, state) -> None:
    try:
        skills = state.get_skill_engine().list_skills()
        await send.response(req_id, skills)
    except Exception as e:
        log.exception("skill.list failed")
        await send.error(req_id, -32603, str(e))


async def handle_skill_run(req_id: str, params: dict, send, state) -> None:
    """
    执行 Skill。优先使用 LangGraph Workflow（支持断点续作 + HITL），
    fallback 到顺序执行。
    P2-5: 执行过程通过 event.emit 通知 TUI 更新进度条。
    """
    from evocli_soul.rpc import emit_event

    skill_id   = params.get("id", params.get("skill_id", ""))
    dry_run    = bool(params.get("dry_run", False))
    session_id = params.get("session_id")

    if not skill_id:
        await send.error(req_id, -32600, "skill id is required")
        return
    try:
        engine = state.get_skill_engine()

        if dry_run:
            result = await engine.execute(skill_id, dry_run=True)
            await send.response(req_id, result)
            return

        skill = engine._skills.get(skill_id)
        if skill is None:
            await send.error(req_id, -32600, f"Skill '{skill_id}' not found")
            return

        # P2-5: 通知 TUI Skill 开始执行
        total_steps = len(skill.steps)
        await emit_event("skill_started", {
            "skill_id":   skill_id,
            "skill_name": skill.name,
            "total_steps": total_steps,
        })

        try:
            from evocli_soul.workflow import run_skill_with_workflow
            result = await run_skill_with_workflow(
                skill,
                state.get_bridge(),
                session_id=session_id,
            )
            # P2-5: 通知 TUI 执行完成
            steps_done = len(result.get("results", []))
            ok = result.get("ok", True)
            await emit_event("skill_finished", {
                "skill_id": skill_id,
                "ok":       ok,
                "steps":    steps_done,
                "summary":  result.get("error", "") if not ok else f"{steps_done}/{total_steps} steps completed",
            })
            await send.response(req_id, result)
        except Exception as wf_err:
            log.warning("LangGraph workflow failed (%s), using sequential fallback", wf_err)
            result = await engine.execute(skill_id, dry_run=False)
            # P2-5: 通知 TUI 执行完成（fallback 路径）
            ok = result.get("ok", False)
            await emit_event("skill_finished", {
                "skill_id": skill_id,
                "ok":       ok,
                "steps":    len(result.get("results", [])),
                "summary":  result.get("error", "") if not ok else "completed",
            })
            await send.response(req_id, result)

        # ── Auto-promotion: record result and check threshold ─────────────
        # Implements Draft→Verified→Trusted based on success rate (PROMPT-09)
        try:
            ok_final = result.get("ok", False) if isinstance(result, dict) else False
            new_status = engine.record_execution(skill_id, success=ok_final)
            if new_status:
                log.info("Skill '%s' auto-promoted to '%s'", skill_id, new_status)
        except Exception as promo_err:
            log.debug("Skill auto-promotion check failed (non-fatal): %s", promo_err)

    except Exception as e:
        log.exception("skill.run failed")
        await send.error(req_id, -32603, str(e))


async def handle_skill_reload(req_id: str, params: dict, send, state) -> None:
    try:
        state.get_skill_engine().reload()
        await send.response(req_id, {"ok": True})
    except Exception as e:
        log.exception("skill.reload failed")
        await send.error(req_id, -32603, str(e))


# ── Skill Genealogy (Section 9.4) ─────────────────────────────────────────────

async def handle_skill_genealogy(req_id: str, params: dict, send, state) -> None:
    """
    Skill 谱系追踪（Section 9.4）。
    查询一个 Skill 的"进化历史"。

    params:
      skill_id: str   要查询的 Skill ID（省略则返回所有）
    """
    skill_id = params.get("skill_id")
    try:
        genealogy = _load_genealogy()
        if skill_id:
            records = [r for r in genealogy if r.get("skill_id") == skill_id]
            await send.response(req_id, {"skill_id": skill_id, "events": records, "count": len(records)})
        else:
            by_skill: dict = {}
            for r in genealogy:
                sid = r.get("skill_id", "unknown")
                by_skill.setdefault(sid, []).append(r)
            await send.response(req_id, {"skills": list(by_skill.keys()), "total_events": len(genealogy)})
    except Exception as e:
        log.exception("skill.genealogy failed")
        await send.error(req_id, -32603, str(e))


async def handle_skill_record_event(req_id: str, params: dict, send, state) -> None:
    """
    记录 Skill 生命周期事件（Section 9.4 谱系追踪）。
    事件类型: created/dry_run_passed/promoted/trusted/decayed/re_verified/executed/failed

    params:
      skill_id:    str   Skill ID
      event:       str   事件类型
      reason:      str   事件原因
      parent_skill: str  父 Skill ID（演化来源，可选）
    """
    skill_id     = params.get("skill_id", "")
    event        = params.get("event", "executed")
    reason       = params.get("reason", "")
    parent_skill = params.get("parent_skill")
    if not skill_id:
        await send.error(req_id, -32600, "skill_id is required")
        return
    try:
        entry = {
            "skill_id": skill_id,
            "event":    event,
            "reason":   reason,
            "at":       datetime.now(timezone.utc).isoformat(),
        }
        if parent_skill:
            entry["parent_skill"] = parent_skill
        _append_genealogy(entry)
        await send.response(req_id, {"ok": True, "event": event, "skill_id": skill_id})
    except Exception as e:
        log.exception("skill.record_event failed")
        await send.error(req_id, -32603, str(e))


def _load_genealogy() -> list:
    if not _GENEALOGY_FILE.exists():
        return []
    try:
        return [json.loads(l) for l in _GENEALOGY_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
    except Exception:
        return []


def _append_genealogy(entry: dict) -> None:
    _GENEALOGY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_GENEALOGY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ── Superpowers SKILL.md guidance handlers ───────────────────────────────────

async def handle_skill_guidance(req_id: str, params: dict, send, state) -> None:
    """
    获取 Superpowers SKILL.md 指引内容（用于注入 AI 上下文）。
    params:
      id: str  — 技能 ID (如 "test-driven-development")
    returns:
      {id, name, description, content}
    """
    try:
        skill_id = params.get("id", "")
        engine   = state.get_skill_engine()
        gs       = engine.get_guidance(skill_id)
        if gs is None:
            await send.error(req_id, -32602, f"Guidance skill not found: {skill_id}")
            return
        await send.response(req_id, {
            "id":          gs.id,
            "name":        gs.name,
            "description": gs.description,
            "content":     gs.content,
        })
    except Exception as e:
        log.exception("skill.guidance failed")
        await send.error(req_id, -32603, str(e))


async def handle_skill_list_guidance(req_id: str, params: dict, send, state) -> None:
    """
    列出所有已加载的 Superpowers SKILL.md guidance skills。
    返回: [{id, name, description, type}]
    """
    try:
        engine  = state.get_skill_engine()
        skills  = engine.list_skills()           # TOML executable skills
        try:
            guidance = engine.list_guidance_skills()  # SKILL.md guidance skills
        except AttributeError:
            guidance = []
        await send.response(req_id, {
            "executable": skills,
            "guidance":   guidance,
            "total_executable": len(skills),
            "total_guidance":   len(guidance),
        })
    except Exception as e:
        log.exception("skill.list_guidance failed")
        await send.error(req_id, -32603, str(e))

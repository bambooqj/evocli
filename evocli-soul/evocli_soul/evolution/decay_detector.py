"""Skill 腐化检测 — 唯一职责：检测 Skill 是否因依赖或闲置而失效。"""
from __future__ import annotations
import logging

log = logging.getLogger("evocli.evolution.decay")

LOCK_FILES = ("Cargo.lock", "package-lock.json", "requirements.txt")


async def check(skill_id: str, project: str, bridge) -> dict:
    signals = []

    # 信号 1：依赖文件变更（检测过去 7 天历史提交中是否修改了 lock 文件）
    # Fix CRITICAL-1 v2 (Oracle review): git.diff 只显示当前未提交变更，不是历史记录。
    # 使用 shell.run + git log --since 检测过去 7 天是否有对应 lock_file 的提交。
    # 每个 lock_file 独立查询（跳出逻辑：找到第一个变更的 lock_file 即停止）。
    for lock_file in LOCK_FILES:
        try:
            result = await bridge.call("shell.run", {
                "cmd":       f"git log --since=\"7 days ago\" --oneline -- {lock_file}",
                "cwd":       ".",
                "timeout_s": 10,
                "dry_run":   False,
            })
            # shell.run 返回 {"exit_code": 0, "stdout": "...", "stderr": "..."}
            stdout = result.get("stdout", "") if isinstance(result, dict) else str(result)
            if stdout and stdout.strip():
                signals.append({
                    "type":     "dependency_upgraded",
                    "severity": "medium",
                    "detail":   f"{lock_file} 在过去 7 天有提交变更",
                })
                break
        except Exception:
            pass

    # 信号 2：长期未执行
    try:
        records = await bridge.call("memory.recall", {
            "query":          f"skill {skill_id} executed",
            "priority_scope": "project",
            "project_id":     project,
            "top_k":          1,
        })
        if not records:
            signals.append({
                "type":     "idle_days_exceeded",
                "severity": "low",
                "detail":   "该 Skill 无近期执行记录",
            })
    except Exception:
        pass

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

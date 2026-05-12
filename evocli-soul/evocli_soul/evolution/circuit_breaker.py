"""
circuit_breaker.py — Skill 执行熔断机制（Section 9.4 MANDATORY）

当 Skill 失败率超过阈值时，自动降级（Trusted → Deprecated）。
防止有问题的 Skill 持续损坏工作区。
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional
import json

log = logging.getLogger("evocli.evolution.circuit_breaker")

# 熔断配置（对应设计文档 Section 9.4）
FAILURE_RATE_THRESHOLD = 0.30   # 最近 10 次执行失败率 > 30% → 自动 Deprecated
CONSECUTIVE_FAILURES   = 3      # 连续失败 3 次 → 立即暂停
CRITICAL_ERRORS        = {"data_loss", "test_failure", "permission_denied"}


@dataclass
class SkillStats:
    skill_id:        str
    executions:      list[dict] = field(default_factory=list)  # {"ok": bool, "ts": str}
    consecutive_fail: int = 0
    circuit_open:    bool = False   # True = 熔断（不可执行）
    last_check:      str  = ""


class CircuitBreaker:
    """
    Skill 执行熔断器。
    记录执行历史，当失败率超阈值时触发熔断。

    线程安全：_lock 保证 record_success/record_failure 的读-改-写原子性。
    若无锁，并发 Skill 执行会产生 consecutive_fail 计数丢失（非原子自增）。
    """

    def __init__(self):
        self._stats: dict[str, SkillStats] = {}
        self._stats_file = Path.home() / ".evocli" / "skill_stats.json"
        self._lock = threading.Lock()  # Protects _stats dict and file I/O atomicity
        self._load()

    def _load(self) -> None:
        """从磁盘加载 Skill 执行统计。"""
        if self._stats_file.exists():
            try:
                data = json.loads(self._stats_file.read_text(encoding="utf-8"))
                for skill_id, s in data.items():
                    self._stats[skill_id] = SkillStats(
                        skill_id=skill_id,
                        executions=s.get("executions", []),
                        consecutive_fail=s.get("consecutive_fail", 0),
                        circuit_open=s.get("circuit_open", False),
                        last_check=s.get("last_check", ""),
                    )
            except Exception as e:
                log.debug("Failed to load skill stats: %s", e)

    def _save(self) -> None:
        """持久化执行统计到磁盘。调用者必须持有 _lock。"""
        try:
            self._stats_file.parent.mkdir(parents=True, exist_ok=True)
            data = {}
            for skill_id, s in self._stats.items():
                data[skill_id] = {
                    "executions":       s.executions[-20:],  # 保留最近 20 次
                    "consecutive_fail": s.consecutive_fail,
                    "circuit_open":     s.circuit_open,
                    "last_check":       s.last_check,
                }
            self._stats_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:
            log.debug("Failed to save skill stats: %s", e)

    def _get_stats(self, skill_id: str) -> SkillStats:
        if skill_id not in self._stats:
            self._stats[skill_id] = SkillStats(skill_id=skill_id)
        return self._stats[skill_id]

    def is_open(self, skill_id: str) -> bool:
        """检查 Skill 是否被熔断（不可执行）。"""
        with self._lock:
            return self._get_stats(skill_id).circuit_open

    def record_success(self, skill_id: str) -> None:
        """记录一次成功执行。原子操作：读-改-存在同一个锁内完成。"""
        with self._lock:
            s = self._get_stats(skill_id)
            s.executions.append({"ok": True, "ts": datetime.now().isoformat()})
            s.consecutive_fail = 0
            s.last_check = datetime.now().isoformat()
            self._save()

    def record_failure(self, skill_id: str, error_type: str = "unknown") -> dict:
        """
        记录一次失败执行。原子操作：读-改-存在同一个锁内完成。
        返回熔断决策：{"tripped": bool, "reason": str, "action": str}
        """
        with self._lock:
            s = self._get_stats(skill_id)
            s.executions.append({"ok": False, "ts": datetime.now().isoformat(), "error": error_type})
            s.consecutive_fail += 1
            s.last_check = datetime.now().isoformat()

            # 检查熔断条件
            result = {"tripped": False, "reason": "", "action": "continue"}

            # 条件 1：关键错误立即熔断
            if error_type in CRITICAL_ERRORS:
                s.circuit_open = True
                result = {
                    "tripped": True,
                    "reason":  f"Critical error: {error_type}",
                    "action":  "disable_skill",
                }
                log.warning("CIRCUIT BREAKER: Skill %s tripped by critical error %s", skill_id, error_type)

            # 条件 2：连续失败次数超限
            elif s.consecutive_fail >= CONSECUTIVE_FAILURES:
                s.circuit_open = True
                result = {
                    "tripped": True,
                    "reason":  f"Consecutive failures: {s.consecutive_fail}",
                    "action":  "disable_skill",
                }
                log.warning("CIRCUIT BREAKER: Skill %s tripped by %d consecutive failures", skill_id, s.consecutive_fail)

            # 条件 3：最近 10 次失败率 > 30%
            else:
                recent = s.executions[-10:]
                if len(recent) >= 5:  # 至少 5 次才计算
                    fail_rate = sum(1 for e in recent if not e["ok"]) / len(recent)
                    if fail_rate >= FAILURE_RATE_THRESHOLD:
                        s.circuit_open = True
                        result = {
                            "tripped":    True,
                            "reason":     f"Failure rate {fail_rate:.0%} > {FAILURE_RATE_THRESHOLD:.0%}",
                            "action":     "disable_skill",
                            "fail_rate":  fail_rate,
                        }
                        log.warning("CIRCUIT BREAKER: Skill %s tripped by failure rate %.0f%%",
                                    skill_id, fail_rate * 100)

            self._save()
            return result

    def reset(self, skill_id: str) -> None:
        """人工重置熔断（用户审查后恢复）。"""
        with self._lock:
            s = self._get_stats(skill_id)
            s.circuit_open     = False
            s.consecutive_fail = 0
            s.last_check       = datetime.now().isoformat()
            self._save()
        log.info("Circuit breaker reset for skill: %s", skill_id)

    def get_status(self, skill_id: str) -> dict:
        """获取 Skill 的熔断状态和统计。"""
        with self._lock:
            s      = self._get_stats(skill_id)
            recent = s.executions[-10:]
            fail_rate = (sum(1 for e in recent if not e["ok"]) / len(recent)) if recent else 0.0
            return {
                "skill_id":          skill_id,
                "circuit_open":      s.circuit_open,
                "consecutive_fail":  s.consecutive_fail,
                "fail_rate_recent":  fail_rate,
                "total_executions":  len(s.executions),
                "last_check":        s.last_check,
            }


# 全局单例
_circuit_breaker: Optional[CircuitBreaker] = None
# Thread-safe singleton initialization lock.
# Without this, concurrent calls to get_circuit_breaker() could create multiple instances,
# leading to race conditions in _load()/_save() on the same stats file.
_singleton_lock = threading.Lock()


def get_circuit_breaker() -> CircuitBreaker:
    global _circuit_breaker
    if _circuit_breaker is None:
        with _singleton_lock:
            if _circuit_breaker is None:  # double-check after acquiring lock
                _circuit_breaker = CircuitBreaker()
    return _circuit_breaker

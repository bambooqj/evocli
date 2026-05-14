# pyright: reportMissingTypeArgument=false, reportArgumentType=false, reportMissingTypeStubs=false
"""模式检测 — 唯一职责：从事件序列中发现重复模式。"""
from __future__ import annotations
import importlib.util
import logging
from dataclasses import dataclass
from datetime import datetime

log = logging.getLogger("evocli.evolution.pattern")

PATTERN_THRESHOLD = 2
EVENT_WINDOW      = 200

# 不应形成 Skill 的"噪声"工具调用（太通用，缺乏自动化价值）
_NOISE_TOOLS = frozenset({
    "memory_recall", "todo_read", "todo_write", "task_complete",
    "memory_write", "shell_ls", "fs_read",
})


@dataclass
class Pattern:
    sequence:  list[str]
    frequency: int
    last_seen: str


def extract_sequences(events: list[dict]) -> list[list[str]]:
    sessions: dict[str, list[str]] = {}
    for ev in events[-EVENT_WINDOW:]:
        sid    = ev.get("session_id", "default")
        action = ev.get("type", ev.get("method", "unknown"))
        sessions.setdefault(sid, []).append(action)
    return list(sessions.values())


def detect_patterns(sequences: list[list[str]]) -> list[Pattern]:
    if importlib.util.find_spec("prefixspan"):
        try:
            from prefixspan import PrefixSpan
            ps = PrefixSpan(sequences)
            # Use closed=True to return closed patterns (reduces redundancy).
            # The 'minlen' parameter was removed in newer prefixspan versions —
            # we filter by length manually to avoid DeprecationWarning.
            try:
                raw = ps.frequent(PATTERN_THRESHOLD, closed=True)
            except TypeError:
                # Older prefixspan API without 'closed' support
                raw = ps.frequent(PATTERN_THRESHOLD)

            return [
                Pattern(sequence=list(seq), frequency=freq,
                        last_seen=datetime.now().isoformat())
                for freq, seq in raw
                if 2 <= len(seq) <= 6 and _is_useful_pattern(seq)
            ]
        except Exception as e:
            log.warning("PrefixSpan error: %s \u2014 sliding window fallback", e)
    return _sliding_window(sequences)


def _is_useful_pattern(seq: list[str]) -> bool:
    """
    Quality filter: a pattern is useful for Skill generation only if
    it contains at least one non-trivial tool call.
    Pure noise sequences (all reads/remembers) are not worth automating.
    """
    return not all(tool in _NOISE_TOOLS for tool in seq)


def _sliding_window(sequences: list[list[str]]) -> list[Pattern]:
    """
    Fallback pattern detection using sliding window frequency counting.
    Improved quality: filters noise-only patterns and prefers longer sequences.
    """
    from collections import Counter
    counter: Counter = Counter()
    for seq in sequences:
        for length in (2, 3, 4):
            for i in range(len(seq) - length + 1):
                window = tuple(seq[i : i + length])
                # Quality filter: at least one non-noise tool
                if _is_useful_pattern(list(window)):
                    counter[window] += 1
    return [
        Pattern(sequence=list(s), frequency=c, last_seen=datetime.now().isoformat())
        for s, c in counter.most_common(10) if c >= PATTERN_THRESHOLD
    ]




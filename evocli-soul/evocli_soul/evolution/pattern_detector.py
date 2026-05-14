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
            return [
                Pattern(sequence=list(seq), frequency=freq,
                        last_seen=datetime.now().isoformat())
                for freq, seq in ps.frequent(2, maxlen=6)
                if len(seq) >= 2
            ]
        except Exception as e:
            log.warning("PrefixSpan error: %s — sliding window fallback", e)
    return _sliding_window(sequences)


def _sliding_window(sequences: list[list[str]]) -> list[Pattern]:
    from collections import Counter
    counter: Counter = Counter()
    for seq in sequences:
        for length in (2, 3, 4):
            for i in range(len(seq) - length + 1):
                counter[tuple(seq[i : i + length])] += 1
    return [
        Pattern(sequence=list(s), frequency=c, last_seen=datetime.now().isoformat())
        for s, c in counter.most_common(10) if c >= 2
    ]



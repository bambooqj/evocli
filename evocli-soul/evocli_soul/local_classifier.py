"""
local_classifier.py — 基于本地嵌入模型的零样本文本分类器

使用 jinaai/jina-embeddings-v2-base-zh（768维，中英双语）做 cosine similarity
零样本分类，随使用自动积累标签供 Phase 1 逻辑回归训练。

模型选型说明：
  - jina-embeddings-v2-base-zh: 768维，中英双语，jina-v2 系列
  - 比原 MiniLM-L12 (384维) 更高维度，分类精度更高
  - 与 memory_client.py 使用同一模型 → 进程内复用，无额外开销
  - 配置在 embedder.py，通过 config.toml [embedder] 可覆盖

架构：
  Phase 0: 零样本 cosine similarity（无需训练数据，立即可用）
  Phase 1: 积累 4000条/类后 → 逻辑回归（metrics.py 触发训练）
  Fallback: 模型不可用时退回关键词匹配
"""
from __future__ import annotations

import importlib.util
import json
import logging
import math
from pathlib import Path
from typing import Any

log = logging.getLogger("evocli.local_classifier")

# ── 标签积累路径（供 Phase 1 重训使用）────────────────────────────────────────
_INTENT_LABELS_FILE = Path.home() / ".evocli" / "intent_router" / "labels.jsonl"

# ── 嵌入模型缓存（进程级，与 memory_client + metrics 共用同一 jina-zh 模型）──────
_embedder_cache: Any = None


def get_shared_embedder():
    """
    获取文本嵌入模型实例（jina-embeddings-v2-base-zh, 768维，中英双语）。
    通过 embedder.py 中央配置获取，与 memory_client / metrics 共用同一实例。
    """
    global _embedder_cache
    if _embedder_cache is not None:
        return _embedder_cache
    if not importlib.util.find_spec("fastembed"):
        return None
    try:
        from evocli_soul.embedder import get_text_embedder
        _embedder_cache = get_text_embedder()
        log.debug("local_classifier: jina-embeddings-v2-base-zh (768-dim) loaded via embedder.py")
        return _embedder_cache
    except Exception as e:
        log.debug("local_classifier: embedder load failed: %s", e)
        return None


def _cosine(a: list, b: list) -> float:
    """
    Cosine similarity. fastembed returns L2-normalized vectors by default,
    so np.dot is sufficient (no division needed). Falls back to pure Python.
    """
    try:
        import numpy as np
        va = np.asarray(a, dtype=np.float32)
        vb = np.asarray(b, dtype=np.float32)
        # fastembed vectors are pre-normalized → dot product = cosine similarity
        dot = float(np.dot(va, vb))
        # Guard against non-normalized vectors (e.g. custom models)
        na, nb = float(np.linalg.norm(va)), float(np.linalg.norm(vb))
        return dot / (na * nb) if na * nb > 0 else 0.0
    except ImportError:
        dot = sum(x * y for x, y in zip(a, b))
        na  = math.sqrt(sum(x * x for x in a))
        nb  = math.sqrt(sum(x * x for x in b))
        return dot / (na * nb) if na * nb > 0 else 0.0


def _embed(text: str, embedder) -> list | None:
    """生成单条文本的嵌入向量。"""
    try:
        embs = list(embedder.embed([text]))
        return list(embs[0]) if embs else None
    except Exception as e:
        log.debug("_embed failed: %s", e)
        return None


# ── 核心 API ──────────────────────────────────────────────────────────────────

def classify_by_similarity(
    text: str,
    descriptions: dict[str, str],
    threshold: float = 0.25,
    fallback: str = "",
) -> str:
    """
    零样本意图分类：计算 text 与每个类别描述的 cosine similarity，
    返回最相似的类别名。

    Args:
        text:         待分类文本（用户输入）
        descriptions: {class_name: natural_language_description}
        threshold:    最低置信度，低于此返回 fallback
        fallback:     低置信度时的默认返回值

    Example:
        classify_by_similarity(
            "implement a retry mechanism",
            {"coder": "User wants code written...", "planner": "User wants to design..."},
        )  -> "coder"
    """
    embedder = get_shared_embedder()
    if embedder is None:
        return fallback

    query_emb = _embed(text, embedder)
    if query_emb is None:
        return fallback

    # Cache description embeddings using a content-based fingerprint.
    # Previous fix used id(descriptions) which is UNSAFE: Python reuses memory addresses
    # after GC, so a new dict at the same address would incorrectly get stale cached embeddings.
    # hash(frozenset(descriptions.items())) is content-stable for string-valued dicts.
    try:
        _desc_fingerprint = hash(frozenset(descriptions.items()))
    except TypeError:
        # Fallback: if values aren't hashable, skip caching entirely
        _desc_fingerprint = id(descriptions)
    _desc_cache_key = (id(embedder), _desc_fingerprint)
    if not hasattr(classify_by_similarity, "_desc_emb_cache"):
        classify_by_similarity._desc_emb_cache = {}  # type: ignore[attr-defined]
    desc_emb_cache = classify_by_similarity._desc_emb_cache  # type: ignore[attr-defined]
    if _desc_cache_key not in desc_emb_cache:
        desc_emb_cache[_desc_cache_key] = {
            name: _embed(desc, embedder)
            for name, desc in descriptions.items()
        }
    cached_desc_embs = desc_emb_cache[_desc_cache_key]

    best_name, best_score = fallback, -1.0
    for name, desc_emb in cached_desc_embs.items():
        if desc_emb is None:
            continue
        score = _cosine(query_emb, desc_emb)
        if score > best_score:
            best_score, best_name = score, name

    if best_score < threshold:
        log.debug("classify_by_similarity: low confidence %.3f < %.3f, fallback=%s",
                  best_score, threshold, fallback)
        return fallback

    log.debug("classify_by_similarity: '%s...' -> %s (score=%.3f)",
              text[:40], best_name, best_score)
    return best_name


def similarity_score(text: str, description: str) -> float:
    """
    Compute cosine similarity between text and a single description.
    Used by intent_profile.py for per-intent threshold calibration.
    Returns 0.0 if embedder is unavailable.
    """
    embedder = get_shared_embedder()
    if embedder is None:
        return 0.0
    q = _embed(text, embedder)
    d = _embed(description, embedder)
    if q is None or d is None:
        return 0.0
    return float(_cosine(q, d))


def rank_by_similarity(
    text: str,
    items: list[tuple[str, str]],  # [(id, description)]
    top_k: int = 3,
    threshold: float = 0.20,
) -> list[tuple[str, float]]:
    """
    按语义相关性排序 items，返回 top_k 个 (id, score)。

    Args:
        text:      查询文本
        items:     [(id, description_text)] 列表
        top_k:     返回最多 top_k 个结果
        threshold: score 低于此的不返回

    Example:
        rank_by_similarity(
            "write unit tests for the parser",
            [("tdd", "Test-driven development..."), ("review", "Code review...")],
        )  ->  [("tdd", 0.82), ...]
    """
    embedder = get_shared_embedder()
    if embedder is None:
        return []

    query_emb = _embed(text, embedder)
    if query_emb is None:
        return []

    scored = []
    for item_id, desc in items:
        desc_emb = _embed(desc, embedder)
        if desc_emb is None:
            continue
        score = _cosine(query_emb, desc_emb)
        if score >= threshold:
            scored.append((item_id, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]


def record_label(text: str, label: str, extra: dict | None = None) -> None:
    """
    记录一次分类决策到标签文件，供未来 Phase 1 重训使用。
    非阻塞：失败静默忽略。
    """
    try:
        _INTENT_LABELS_FILE.parent.mkdir(parents=True, exist_ok=True)
        entry = {"text": text[:500], "label": label}
        if extra:
            entry.update(extra)
        with open(_INTENT_LABELS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        log.debug("record_label failed: %s", e)


# ── 预定义描述（零样本类别描述，Phase 0 直接使用）─────────────────────────────

AGENT_INTENT_DESCRIPTIONS: dict[str, str] = {
    "planner": (
        "The user wants to plan, design, architect, or break down a task into steps. "
        "They are asking for a roadmap, approach, or decomposition strategy before writing code. "
        "Keywords: plan, design, roadmap, decompose, structure, approach, strategy, "
        "规划, 方案, 拆解, 架构, 设计, 步骤"
    ),
    "coder": (
        "The user wants code written, implemented, created, or refactored. "
        "They want a working implementation of a feature or fix. "
        "Keywords: implement, write, create, add, build, code, fix, refactor, generate, "
        "实现, 写代码, 新增, 创建, 修复, 重构, 实现功能"
    ),
    "reviewer": (
        "The user wants existing code reviewed, checked, audited, or quality assessed. "
        "They want feedback on correctness, style, or best practices. "
        "Keywords: review, check, audit, quality, lint, assess, feedback, "
        "审查, 检查, 代码质量, 评审, 规范, 标准"
    ),
    "debugger": (
        "The user has a bug, error, test failure, or unexpected behavior to investigate. "
        "They need systematic root cause analysis and a fix. "
        "Keywords: debug, error, crash, fail, wrong, broken, why, exception, "
        "调试, 报错, 错误, 失败, 为什么, 异常, bug, 测试失败"
    ),
    "researcher": (
        "The user wants to find, search, understand, or explore existing code or documentation. "
        "They need information gathered before taking action. "
        "Keywords: find, search, where, what, how does, explain, show me, understand, "
        "查找, 搜索, 在哪, 解释, 了解, 理解, 找到"
    ),
}

ORCHESTRATION_DESCRIPTIONS: dict[str, str] = {
    "needs_orchestration": (
        "This is a complex multi-step task requiring planning followed by implementation. "
        "It involves multiple phases like design, code, test, and review. "
        "Examples: 'plan and implement', 'design and build', 'review and fix all issues', "
        "'create a full feature with tests and documentation'. "
        "The task cannot be done by a single action."
    ),
    "simple_task": (
        "This is a straightforward single-step request that one agent can handle directly. "
        "Examples: 'what is X', 'show me Y', 'fix this error', 'write a function', "
        "'explain this code'. One clear action is needed."
    ),
}




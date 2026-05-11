"""
mem_router_labeler.py — LLM 驱动的记忆分类标签生成器 (Phase 1: 冷启动)

## 设计 (用户原始思路)
"前期数据不够时，使用自身大模型用提示词进行区分并结构化"

这个模块在 Phase 1 时工作:
- 对每条新内容调用 LLM 进行分类
- 返回结构化标签 (memory_type, should_write, importance)
- 将标签通过 RPC 发给 Rust 端存储到 SQLite

当 Rust 端累积足够标签后，自动切换到 fastembed+linfa 分类器，
这个模块只在低置信度时被回调 (主动学习/Active Learning)。

## LLM 提示词设计
使用 JSON 输出格式确保可解析性。
简短提示，最多 50 token 输出 → fast model 即可完成。
"""
from __future__ import annotations

import json
import logging
from typing import Optional

log = logging.getLogger("evocli.mem_router_labeler")

# 分类提示词 (设计原则: 简短、清晰、JSON 输出)
_LABEL_SYSTEM = """\
You are a memory classifier for an AI coding assistant.
Classify the given text into one memory category.

Categories:
- constraint: rules/requirements (MUST, NEVER, forbidden, 必须, 禁止, 不能)
- preference: user preferences/likes (prefer, like, 偏好, 喜欢, 希望)
- semantic: factual knowledge (project uses X, config is Y, 使用, 配置, 版本)
- procedural: how-to skills (steps, how to do X, 如何, 步骤, 先...再)
- episodic: specific events (today/just now/fixed X, 今天, 刚才, 已完成)
- no_write: noise/trivial (ok, yes, thanks, short ack)

Reply ONLY with valid JSON: {"label": "<category>", "importance": <0.0-1.0>}
Do not explain."""

_LABEL_USER_TEMPLATE = 'Classify: """{text}"""'


async def label_with_llm(
    content: str,
    llm_client,
) -> Optional[dict]:
    """
    Phase 1: 用 LLM 为内容生成记忆类型标签。

    Returns: {"label": str, "importance": float, "should_write": bool}
    或 None (解析失败)
    """
    prompt = _LABEL_USER_TEMPLATE.format(text=content[:500])  # 截断避免 token 浪费
    try:
        response = await llm_client.complete(
            prompt,
            tier="fast",           # 用 fast model (gpt-4o-mini/haiku) 节省成本
            system=_LABEL_SYSTEM,
            max_tokens=60,         # 只需要 JSON 输出
            temperature=0.0,       # 分类任务用 0 温度确保确定性
        )
        # 解析 JSON
        response = response.strip()
        if response.startswith("```"):
            response = response.split("```")[1].strip()
            if response.startswith("json"):
                response = response[4:].strip()

        data = json.loads(response)
        label     = data.get("label", "episodic")
        importance = float(data.get("importance", 0.5))

        # 标准化 label
        valid_labels = {"constraint", "preference", "semantic", "procedural", "episodic", "no_write"}
        if label not in valid_labels:
            label = "episodic"

        should_write = label != "no_write"

        # GAP-4: Persist label to JSONL so Rust MemRouter can accumulate training data
        # for Phase 1 activation (needs 200+ samples/class to switch from LLM slow-path
        # to fastembed+linfa fast-path <5ms). Non-fatal — label is returned even on failure.
        try:
            from evocli_soul.handlers.metrics import store_label_direct
            store_label_direct(
                text=content,
                label_idx=label_index_from_str(label),
                label_name=label,
                confidence=importance,
                source="llm",
            )
        except Exception as _store_err:
            log.debug("label storage skipped (non-fatal): %s", _store_err)

        log.debug("LLM labeled '%s...' → %s (importance=%.2f)", content[:40], label, importance)
        return {
            "label":        label,
            "importance":   importance,
            "should_write": should_write,
        }
    except json.JSONDecodeError as e:
        log.debug("LLM label parse failed: %s | response: %s", e, response[:100])
        return None
    except Exception as e:
        log.warning("LLM labeling failed: %s", e)
        return None


def label_index_from_str(label: str) -> int:
    """Map label string to index for Rust SQLite storage."""
    mapping = {
        "constraint": 0,
        "preference": 1,
        "semantic":   2,
        "procedural": 3,
        "episodic":   4,
        "no_write":   5,
    }
    return mapping.get(label, 4)


# Batch labeling for seeding initial training data
async def seed_labels_from_existing(
    existing_memories: list[dict],
    llm_client,
    bridge,
    max_seed: int = 100,
) -> int:
    """
    Seed training data by labeling existing memories with LLM.
    Called once during Phase 1 bootstrap.
    
    Returns: number of successfully labeled samples.
    """
    labeled = 0
    for mem in existing_memories[:max_seed]:
        content = mem.get("body") or mem.get("title") or ""
        if not content or len(content) < 5:
            continue
        result = await label_with_llm(content, llm_client)
        if not result:
            continue
        try:
            # Fix CRITICAL-2: mem_router.store_label 是 Python-side RPC handler（在 handlers/metrics.py 注册）。
            # 不能通过 bridge.call() 路由到 Rust（Rust tool_dispatch.rs 无此 arm）。
            # 正确做法：直接调用 Python 存储函数，绕过 JSON-RPC bridge。
            from evocli_soul.handlers.metrics import store_label_direct
            store_result = store_label_direct(
                text=content,
                label_idx=label_index_from_str(result["label"]),
                label_name=result["label"],
                project_id=mem.get("project_id", ""),
                confidence=1.0,
                source="llm_seed",
            )
            if store_result.get("ok"):
                labeled += 1
            else:
                log.debug("store_label_direct failed: %s", store_result.get("error"))
        except Exception as e:
            log.debug("store_label failed: %s", e)

    log.info("Seeded %d labels from existing memories", labeled)
    return labeled

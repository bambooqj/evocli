"""
context_pruner.py — 子 Agent 结果压缩
防止多 Agent 链式调用中 context window 爆炸。

策略：
- 输出 < 500 tokens: 原文返回
- 输出 500-2000 tokens: 提取关键发现
- 输出 > 2000 tokens: LLM 摘要（保留 Goal/Findings/Next Steps）
"""
from __future__ import annotations
import logging

log = logging.getLogger("evocli.context_pruner")

MAX_RAW_TOKENS = 500
MAX_SUMMARY_TOKENS = 300


def _count_rough_tokens(text: str) -> int:
    return len(text) // 4  # rough approximation


async def prune_agent_output(text: str, role: str, llm_client=None) -> str:
    """压缩 subagent 输出到合理大小。"""
    token_count = _count_rough_tokens(text)
    
    if token_count <= MAX_RAW_TOKENS:
        return text  # Small enough: return as-is
    
    if token_count <= 2000 or llm_client is None:
        # Medium: extract key lines (findings, errors, file paths)
        lines = text.split("\n")
        key_lines = [l for l in lines if any(kw in l.lower() for kw in 
                     ["error", "found", "result", "issue", "warning", "fail", "pass", 
                      "fix", "change", "create", ".rs", ".py", ".toml"])]
        summary = "\n".join(key_lines[:50])
        return f"[{role} summary]\n{summary}\n[{len(lines) - len(key_lines[:50])} more lines omitted]"
    
    # Large: LLM summarization
    try:
        prompt = f"""Summarize this {role} agent output in max {MAX_SUMMARY_TOKENS} tokens.
Keep: key findings, file paths, errors, next actions.
Drop: verbose explanations, repeated info.

OUTPUT:
{text[:4000]}"""
        summary = await llm_client.complete_for_task("summarize", prompt)
        return f"[{role} summary]\n{summary}"
    except Exception as e:
        log.debug("LLM pruning failed: %s", e)
        return text[:MAX_RAW_TOKENS * 4] + f"\n... [{role} output truncated]"

"""
wiki_generator.py — GitNexus-inspired AGENTS.md + 知识图谱 wiki 生成

GitNexus 对应: src/core/wiki/ + generate AI context files (AGENTS.md, CLAUDE.md)

功能:
1. 从代码图谱数据（社区 + 进程 + 符号）生成 AGENTS.md
2. 为每个社区生成技能文件（skill per community）
3. 生成项目 wiki 摘要

需要: LLM (fast model) + 代码图谱 JSON 数据 (来自 Rust knowledge_graph)
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger("evocli.wiki_generator")

AGENTS_MD_TEMPLATE = """\
# AGENTS.md — {project_name}

> 本文件由 EvoCLI 自动生成（GitNexus-inspired knowledge graph analysis）
> 更新时间: {timestamp}

## 项目概览

{overview}

## 代码结构（功能社区）

{communities_section}

## 主要执行流程

{processes_section}

## 关键符号

{symbols_section}

## 工具使用指南

- **代码搜索**: 使用 `search_code` 或 `code_intel_ranked_context` 查找相关符号
- **影响分析**: 修改符号前使用 `impact_check` 评估影响范围
- **调用图**: 使用 `code_intel_incoming_calls` / `code_intel_outgoing_calls` 查看调用关系
- **知识图谱**: 使用 `code_intel.communities` / `code_intel.blast_radius` 深度分析
"""


async def generate_agents_md(
    graph_data: dict,
    project_path: str,
    llm_client,
) -> str:
    """
    Generate AGENTS.md from knowledge graph data.
    
    graph_data: output from code_intel.communities + code_intel.processes + code_intel.stats
    """
    project_name = Path(project_path).name
    stats = graph_data.get("stats", {})
    communities = graph_data.get("communities", [])
    processes = graph_data.get("processes", [])

    from datetime import datetime
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Generate overview using LLM
    try:
        overview_prompt = (
            f"Analyze this codebase summary and write a concise 2-3 paragraph overview:\n\n"
            f"Project: {project_name}\n"
            f"Symbols: {stats.get('symbol_count', 0)}, Files: {stats.get('file_count', 0)}\n"
            f"Communities: {len(communities)}, Processes: {len(processes)}\n"
            f"Top communities: {', '.join(c.get('label', '') for c in communities[:5])}\n\n"
            "Write a technical description for AI agents working on this codebase."
        )
        overview = await llm_client.complete_for_task("wiki", overview_prompt)
    except Exception as e:
        log.debug("Overview generation failed: %s", e)
        overview = f"Codebase with {stats.get('symbol_count', 0)} symbols across {stats.get('file_count', 0)} files."

    # Format communities section
    communities_section = ""
    for i, comm in enumerate(communities[:10], 1):
        label = comm.get("label", f"Community {i}")
        members = comm.get("members", [])
        cohesion = comm.get("cohesion", 0)
        communities_section += f"### {i}. {label}\n"
        communities_section += f"- **符号数量**: {len(members)}\n"
        communities_section += f"- **内聚度**: {cohesion:.0%}\n"
        if members:
            communities_section += f"- **代表符号**: {', '.join(members[:5])}\n"
        communities_section += "\n"

    if not communities_section:
        communities_section = "_运行 `evocli index` 后自动生成社区分析_\n"

    # Format processes section
    processes_section = ""
    for i, proc in enumerate(processes[:8], 1):
        name = proc.get("name", f"Process {i}")
        steps = proc.get("steps", [])
        processes_section += f"### {i}. {name}\n"
        processes_section += f"- **步骤数**: {len(steps)}\n"
        processes_section += f"- **入口**: `{proc.get('entry', 'unknown')}`\n\n"

    if not processes_section:
        processes_section = "_运行 `evocli index` 后自动生成执行流程分析_\n"

    # Key symbols (top by importance from graph)
    symbols_section = "_使用 `code_intel_ranked_context` 获取当前任务相关符号_\n"

    content = AGENTS_MD_TEMPLATE.format(
        project_name=project_name,
        timestamp=timestamp,
        overview=overview,
        communities_section=communities_section,
        processes_section=processes_section,
        symbols_section=symbols_section,
    )
    return content


async def generate_skill_per_community(
    community: dict,
    project_path: str,
    llm_client,
) -> Optional[str]:
    """
    Generate a skill file for a functional community.
    GitNexus: `gitnexus analyze --skills` generates per-community SKILL.md files.
    """
    label = community.get("label", "unknown")
    members = community.get("members", [])
    if not members:
        return None

    try:
        prompt = (
            f"Write a skill guide for AI agents working on the '{label}' module.\n"
            f"Key symbols: {', '.join(members[:10])}\n"
            f"Include: what this module does, how to navigate it, common patterns, gotchas.\n"
            "Keep it under 200 words. Write for an AI coding assistant."
        )
        content = await llm_client.complete_for_task("wiki", prompt)
        return f"# {label} Module Skill\n\n{content}\n"
    except Exception as e:
        log.debug("Skill generation failed for %s: %s", label, e)
        return None

"""
tool_router.py — EvoCLI 动态工具路由器

融合设计来源：
  工业实践:
    - Re-Invoke (2024)：意图提取后检索 nDCG@5 +39%
    - LlamaIndex ObjectIndex：embedding top_k tool retrieval
    - pydantic-ai prepare hook：per-turn 细粒度过滤
    - Claude Code tool_search：deferred loading pattern
  学术论文:
    - TSCG (2026)：>15工具 → 小模型准确率0-49%，结构化文本恢复至90%
    - ObjectGraph (2026)：三层渐进式披露，context节省94%
    - ProbeLogits (2026)：内部探针可预测工具需求（准确率>0.9 AUROC）
    - SkillReducer (2026)：最小化工具描述，48%压缩无质量损失
  EvoCLI 基础设施:
    - local_classifier.rank_by_similarity (fastembed, 384维)
    - local_classifier.classify_by_similarity (零样本分类)
    - memory_client priority_scope="tool" (工具级记忆)
    - memory_distill 成功/失败链 (工具历史学习)

核心算法（3阶段流水线）：
  Stage 1: Keyword Gate（0ms）
    - 正则匹配快速识别意图标签
    - 覆盖 90% 常见请求
    - 来源：Re-Invoke 的 canonical intent 提取思想

  Stage 2: Tag-Based Selection（<1ms）
    - 基于意图标签聚合候选工具
    - 按 (memory_score × base_score) 排序
    - 填满 MAX_TOOLS_PYDANTIC(12) 限额

  Stage 3: Semantic Fill（5-15ms，按需）
    - 用 fastembed embedding 补充语义相似工具
    - 仅当 Stage 2 未填满时触发
    - 来源：LlamaIndex ObjectIndex top_k retrieval

记忆加权机制：
  - 每次工具调用成功/失败后更新 tool_scores
  - 分数 = base_score × (1 + 0.1×recent_successes) × (1 - 0.05×recent_failures)
  - 衰减：每24小时向 base_score 收缩10%（防止过拟合某段时间的使用模式）
  - 存储：~/.evocli/tool_routing_scores.json（轻量，不依赖 LanceDB）

自动化新工具适配：
  开发者添加新工具时，只需：
    1. 在 _register_pydantic_tools 中加 @agent.tool_plain
    2. 在 tool_registry.REGISTRY 中加一行 ToolSpec（指定 tags + tier）
    3. 不需要修改任何路由逻辑
  
  如果没加 ToolSpec（遗忘情况）：
    - auto_classify_unknown() 用 embedding 自动分类并添加到运行时 REGISTRY
    - 新工具会归入 Tier 2，使用 embedding 推断意图标签
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pydantic_ai.tools import ToolDefinition

from evocli_soul import tool_registry as reg
from evocli_soul.tool_registry import REGISTRY, Tag, ToolSpec

log = logging.getLogger("evocli.tool_router")

# ══════════════════════════════════════════════════════════════════════════════
# 配置常量（均可通过 config.toml [agent] 覆盖）
# ══════════════════════════════════════════════════════════════════════════════

MAX_TOOLS_PYDANTIC  = 12   # pydantic-ai 路径上限（TSCG: >15 小模型掉分）
MAX_TOOLS_LITELLM   = 20   # LiteLLM fallback 路径上限（较宽松）
MEMORY_SCORE_FILE   = Path.home() / ".evocli" / "tool_routing_scores.json"
SCORE_DECAY_HOURS   = 24   # 分数向 base_score 收缩的周期
SCORE_DECAY_RATE    = 0.10  # 每个周期收缩比例
SEMANTIC_THRESHOLD  = 0.18  # embedding 最低相似度门槛


# ══════════════════════════════════════════════════════════════════════════════
# Stage 1：关键词意图门（Keyword Intent Gate）
#
# 设计来源：Re-Invoke (2024) "intent canonicalization"
# 每个 Tag 对应一组触发关键词正则。
# 运行成本：O(n_patterns) ≈ 0ms，无模型调用。
# ══════════════════════════════════════════════════════════════════════════════

_INTENT_PATTERNS: list[tuple[re.Pattern, Tag]] = [
    # READ — 读取/查看内容
    (re.compile(
        r'\b(read|open|show|view|cat|look at|see|what.s in|check|display|print|content|code of)\b',
        re.I), Tag.READ),

    # EDIT — 修改代码/文件
    (re.compile(
        r'\b(write|edit|modify|change|fix|update|add|create|implement|refactor|rename|'
        r'delete line|remove|insert|replace|rewrite|improve)\b',
        re.I), Tag.EDIT),

    # RUN — 执行/构建/命令
    (re.compile(
        r'\b(run|execute|build|compile|cargo|python|npm|make|start|launch|generate|'
        r'install|deploy|serve|script)\b',
        re.I), Tag.RUN),

    # SEARCH — 查找
    (re.compile(
        r'\b(search|find|where|which file|grep|look for|locate|who calls?|'
        r'usages?|references?|all places|across)\b',
        re.I), Tag.SEARCH),

    # DEBUG — 调试/错误修复
    (re.compile(
        r'\b(debug|error|crash|fail|broken|exception|traceback|lint|'
        r'warning|bug|issue|fix error|not working|test fail)\b',
        re.I), Tag.DEBUG),

    # ANALYZE — 理解/架构分析
    (re.compile(
        r'\b(analyze|design|architecture|structure|understand|how does|explain|'
        r'overview|diagram|dependency|module|pattern|overview of|describe)\b',
        re.I), Tag.ANALYZE),

    # GIT — 版本控制
    (re.compile(
        r'\b(git|commit|diff|branch|merge|status|push|pull|stash|revert|'
        r'history|changelog|staged|unstaged|log)\b',
        re.I), Tag.GIT),

    # MEMORY — 记忆操作
    (re.compile(
        r'\b(remember|memory|recall|constraint|rule|preference|saved|stored|'
        r'previously|past decision|lesson)\b',
        re.I), Tag.MEMORY),

    # VERIFY — 验证/测试
    (re.compile(
        r'\b(test|verify|validate|coverage|check if|assert|confirm|pass|'
        r'regression|spec|expectation)\b',
        re.I), Tag.VERIFY),

    # IMPACT — 影响分析（通常出现在修改大型代码库前）
    (re.compile(
        r'\b(impact|affect|break|callers?|blast radius|ripple|cascade|'
        r'downstream|upstream|who uses|breaking change)\b',
        re.I), Tag.IMPACT),

    # WEB — 网络请求
    (re.compile(
        r'\b(url|http|https|fetch|web|download|curl|site|page|link|api call)\b',
        re.I), Tag.WEB),

    # MCP — 外部插件
    (re.compile(
        r'\b(mcp|plugin|external tool|server|integration|connect to)\b',
        re.I), Tag.MCP),
]


def classify_intent(query: str) -> list[Tag]:
    """
    Stage 1：关键词门控意图分类（0ms）。
    
    返回 1-3 个最匹配的意图标签。
    当没有任何匹配时，返回 [Tag.ANALYZE]（最宽泛意图，触发最多有用工具）。
    
    设计：Re-Invoke 的 canonical intent canonicalization，
         但用关键词规则替代 LLM 调用，减少延迟。
    """
    tags: set[Tag] = set()
    for pattern, tag in _INTENT_PATTERNS:
        if pattern.search(query):
            tags.add(tag)

    # 语义互补：EDIT 通常需要先 READ
    if Tag.EDIT in tags:
        tags.add(Tag.READ)
    # DEBUG 通常需要 READ + RUN
    if Tag.DEBUG in tags:
        tags.add(Tag.READ)
    # ANALYZE 拉取 SEARCH 能力
    if Tag.ANALYZE in tags:
        tags.add(Tag.SEARCH)
    # IMPACT 拉取 ANALYZE
    if Tag.IMPACT in tags:
        tags.add(Tag.ANALYZE)

    if not tags:
        tags.add(Tag.ANALYZE)  # 兜底：触发 READ + SEARCH + ANALYZE 工具集

    return list(tags)


# ══════════════════════════════════════════════════════════════════════════════
# 记忆加权系统（Memory-Weighted Scoring）
#
# 设计来源：
#   - memory_distill.py 已有成功/失败链路记录
#   - EvoCLI memory_client priority_scope="tool"
#   - 论文：工具历史使用经验驱动选择权重调整
#
# 轻量实现：不依赖 LanceDB，只用本地 JSON 文件
# 分数公式：effective_score = base × success_mult × (1 - failure_penalty) × decay
# ══════════════════════════════════════════════════════════════════════════════

class ToolScoreStore:
    """
    工具使用历史分数存储。
    
    每个工具维护：
      - success_count:  总成功次数
      - failure_count:  总失败次数
      - last_used_ts:   最后使用时间戳
      - recent_successes: 近期成功（用于 session 内实时加权）
      - recent_failures:  近期失败
    
    持久化：~/.evocli/tool_routing_scores.json
    格式轻量，启动时加载，每次更新立即写入。
    """

    def __init__(self) -> None:
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            if MEMORY_SCORE_FILE.exists():
                self._data = json.loads(MEMORY_SCORE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            log.debug("ToolScoreStore: load failed (non-fatal): %s", e)
            self._data = {}

    def _save(self) -> None:
        try:
            MEMORY_SCORE_FILE.parent.mkdir(parents=True, exist_ok=True)
            MEMORY_SCORE_FILE.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            log.debug("ToolScoreStore: save failed (non-fatal): %s", e)

    def _entry(self, tool_name: str) -> dict:
        if tool_name not in self._data:
            spec = reg.REGISTRY_BY_NAME.get(tool_name)
            self._data[tool_name] = {
                "success_count":    0,
                "failure_count":    0,
                "last_used_ts":     0.0,
                "recent_successes": 0,
                "recent_failures":  0,
                "base_score":       spec.base_score if spec else 1.0,
            }
        return self._data[tool_name]

    def record_success(self, tool_name: str) -> None:
        e = self._entry(tool_name)
        e["success_count"]    += 1
        e["recent_successes"] += 1
        e["last_used_ts"]      = time.time()
        self._save()
        log.debug("ToolScore: %s success (total=%d)", tool_name, e["success_count"])

    def record_failure(self, tool_name: str) -> None:
        e = self._entry(tool_name)
        e["failure_count"]   += 1
        e["recent_failures"] += 1
        e["last_used_ts"]     = time.time()
        self._save()
        log.debug("ToolScore: %s failure (total=%d)", tool_name, e["failure_count"])

    def reset_recent(self) -> None:
        """session 结束时调用：清零 recent_* 计数器（保留历史统计）。"""
        for e in self._data.values():
            e["recent_successes"] = 0
            e["recent_failures"]  = 0
        self._save()

    def effective_score(self, tool_name: str) -> float:
        """
        计算工具的有效路由分数。
        
        公式（启发自 MemoryDistill 的记忆重要性评分）：
          base_score × success_mult × failure_mult × recency_mult × decay_mult
        
        成功倍率:  1 + 0.10 × recent_successes（近期成功最多+50%）
        失败惩罚:  max(0.3, 1 - 0.07 × recent_failures)（最多惩罚到30%）
        频率奖励:  min(1.2, 1 + 0.01 × total_successes)（使用越多越受信任）
        时间衰减:  向 base_score 收缩（防止过拟合短期使用模式）
        """
        spec = reg.REGISTRY_BY_NAME.get(tool_name)
        base = spec.base_score if spec else 1.0

        e = self._data.get(tool_name)
        if not e:
            return base

        # 近期成功倍率（上限 +50%）
        success_mult = 1.0 + min(5, e.get("recent_successes", 0)) * 0.10

        # 近期失败惩罚（最低保留 30%）
        failure_mult = max(0.3, 1.0 - e.get("recent_failures", 0) * 0.07)

        # 历史频率奖励（使用越多越可信，上限 +20%）
        freq_mult = min(1.2, 1.0 + e.get("success_count", 0) * 0.01)

        # 时间衰减（距上次使用超过 N 小时，分数向 base 收缩）
        last_used = e.get("last_used_ts", 0.0)
        hours_since = (time.time() - last_used) / 3600.0
        decay_cycles = hours_since / max(1, SCORE_DECAY_HOURS)
        # 指数衰减：每个周期乘以 (1 - decay_rate)，趋近于 base_score
        decay_mult = (1 - SCORE_DECAY_RATE) ** decay_cycles
        # 最终分 = 加权分 × 衰减 + base × (1 - 衰减)
        weighted = base * success_mult * failure_mult * freq_mult
        score = weighted * decay_mult + base * (1.0 - decay_mult)

        return min(1.5, score)  # 硬上限防止溢出


# 进程级单例（复用 fastembed 模型加载，与 local_classifier 共享）
_score_store: ToolScoreStore | None = None

def get_score_store() -> ToolScoreStore:
    global _score_store
    if _score_store is None:
        _score_store = ToolScoreStore()
    return _score_store


# ══════════════════════════════════════════════════════════════════════════════
# Stage 2 + 3：工具选择核心
# ══════════════════════════════════════════════════════════════════════════════

def select_tools(
    query: str,
    *,
    max_tools: int = MAX_TOOLS_PYDANTIC,
    pydantic_only: bool = True,
    force_include: list[str] | None = None,
    force_exclude: list[str] | None = None,
    config: dict | None = None,
) -> list[ToolSpec]:
    """
    主路由函数：三阶段选工具。
    
    Args:
        query:         用户原始输入
        max_tools:     工具上限（默认12，pydantic-ai路径）
        pydantic_only: True = 只返回已在 pydantic-ai 注册的工具
        force_include: 无论意图如何都必须包含的工具名
        force_exclude: 本次禁用的工具名
        config:        config.toml [agent] 配置（可覆盖上限）
    
    Returns:
        ≤max_tools 个 ToolSpec，按 effective_score DESC 排序
    
    流水线：
        Stage 1: Keyword Gate → detect intent tags (0ms)
        Stage 2: Tag-Based Selection → fill with intent-matched tools (<1ms)
        Stage 3: Semantic Fill → embedding similarity補全 (5-15ms, 按需)
    
    论文来源：
        ObjectGraph 三层渐进披露 + Re-Invoke 意图提取 + LlamaIndex top_k retrieval
    """
    # 从 config 覆盖上限
    if config:
        agent_cfg = config.get("agent", {})
        max_tools = int(agent_cfg.get("max_tools_pydantic", max_tools))

    store      = get_score_store()
    exclude_set = set(force_exclude or [])
    selected: list[ToolSpec] = []
    selected_names: set[str] = set()

    def _add(spec: ToolSpec) -> bool:
        """添加一个工具到 selected，返回是否成功。"""
        if spec.name in selected_names:
            return False
        if spec.name in exclude_set:
            return False
        if pydantic_only and not spec.pydantic:
            return False
        if len(selected) >= max_tools:
            return False
        selected.append(spec)
        selected_names.add(spec.name)
        return True

    # ── Step A：Always-On（必须先加）────────────────────────────────────────
    for spec in REGISTRY:
        if spec.always_on:
            _add(spec)

    # ── Step B：force_include（调用方强制指定）────────────────────────────────
    for name in (force_include or []):
        spec = reg.get(name)
        if spec:
            _add(spec)

    if len(selected) >= max_tools:
        log.debug("ToolRouter: filled by always_on+force (%d tools)", len(selected))
        return selected

    # ── Step C：Stage 1 意图识别 → Stage 2 标签匹配────────────────────────────
    tags = classify_intent(query)
    log.debug("ToolRouter: intent tags=%s for query=%r", [t.name for t in tags], query[:60])

    # 收集所有匹配标签的 Tier 2 工具
    candidates: list[ToolSpec] = []
    seen_candidates: set[str] = set()
    for tag in tags:
        for spec in reg.TAG_TO_TOOLS.get(tag, []):
            if spec.tier == 2 and spec.name not in seen_candidates and spec.name not in selected_names:
                candidates.append(spec)
                seen_candidates.add(spec.name)

    # 按有效分排序（记忆加权 × base_score）
    candidates.sort(
        key=lambda s: store.effective_score(s.name),
        reverse=True,
    )

    for spec in candidates:
        if len(selected) >= max_tools:
            break
        _add(spec)

    # ── Step D：Stage 3 语义补充（embedding，仅当 Tier 2 未填满时）────────────
    remaining = max_tools - len(selected)
    if remaining > 0:
        semantic_extras = _semantic_fill(query, selected_names, remaining, pydantic_only)
        for spec in semantic_extras:
            _add(spec)

    log.info(
        "ToolRouter: selected %d/%d tools (tags=%s always_on=%d tier2=%d semantic=%d)",
        len(selected), max_tools,
        [t.name for t in tags],
        sum(1 for s in selected if s.always_on),
        sum(1 for s in selected if s.tier == 2 and not s.always_on),
        sum(1 for s in selected if s.tier == 3),
    )
    return selected


def _semantic_fill(
    query: str,
    exclude: set[str],
    top_k: int,
    pydantic_only: bool,
) -> list[ToolSpec]:
    """
    Stage 3：用 fastembed embedding 补充语义相似的工具。
    
    来源：LlamaIndex ObjectIndex top_k retrieval
    候选池：Tier 2 中未被选入的工具（Tier 3 不做语义填充，保持 On-Demand 语义）
    """
    try:
        from evocli_soul.local_classifier import rank_by_similarity
        candidates = [
            s for s in REGISTRY
            if s.name not in exclude
            and s.tier == 2
            and (not pydantic_only or s.pydantic)
        ]
        if not candidates:
            return []
        items = [(s.name, s.description) for s in candidates]
        ranked = rank_by_similarity(query, items, top_k=top_k, threshold=SEMANTIC_THRESHOLD)
        name_to_spec = {s.name: s for s in candidates}
        result = []
        for name, score in ranked:
            if name in name_to_spec:
                result.append(name_to_spec[name])
        return result
    except Exception as e:
        log.debug("ToolRouter: semantic fill failed (non-fatal): %s", e)
        return []


# ══════════════════════════════════════════════════════════════════════════════
# pydantic-ai prepare hook 工厂
#
# 来源：pydantic-ai 官方 prepare 模式
# 用法：agent.py 中对每个 @agent.tool_plain 传入 prepare=tool_prepare_hook(name)
# ══════════════════════════════════════════════════════════════════════════════

def make_prepare_hook(tool_name: str):
    """
    为单个工具生成 pydantic-ai prepare hook。
    
    prepare hook 在每次 LLM 调用前执行：
      - 如果工具不在本次请求的 selected_names 中 → 返回 None（隐藏该工具）
      - 否则 → 返回 ToolDefinition（工具对 LLM 可见）
    
    usage in agent.py:
        @agent.tool_plain(prepare=make_prepare_hook("fs_read_range"))
        async def fs_read_range(...) -> str:
            ...
    
    注：deps 中需要有 selected_tool_names: frozenset[str] 字段。
    如果 deps 不含该字段（向后兼容），默认显示工具。
    """
    async def _prepare(ctx, tool_def: "ToolDefinition") -> "ToolDefinition | None":
        try:
            selected = getattr(ctx.deps, "selected_tool_names", None)
            if selected is None:
                return tool_def  # 兼容旧路径：没有路由时全部显示
            if tool_name in selected:
                return tool_def
            return None  # 隐藏该工具（不消耗 schema token）
        except Exception:
            return tool_def  # 任何错误都默认显示（安全降级）
    return _prepare


# ══════════════════════════════════════════════════════════════════════════════
# 自动化新工具适配（Auto-classify Unknown Tools）
#
# 来源：ObjectGraph (2026) 自动工具描述分类思想
#
# 当开发者忘记在 REGISTRY 中添加 ToolSpec 时，自动用 embedding 推断分类
# 并注册到运行时 REGISTRY（不持久化，下次启动重新推断）
# ══════════════════════════════════════════════════════════════════════════════

def auto_classify_unknown(
    tool_name: str,
    tool_docstring: str,
) -> ToolSpec | None:
    """
    对未注册工具自动推断意图标签并创建临时 ToolSpec。
    
    算法：
      1. 用 classify_by_similarity 将 docstring 对照每个 Tag 的描述
      2. 取相似度 > 0.2 的标签
      3. 创建 Tier 2 ToolSpec 并注入运行时 REGISTRY
    
    运行时注册（进程生命周期内有效，不写磁盘）。
    下次启动会再次自动分类，直到开发者显式添加到 REGISTRY。
    """
    if tool_name in reg.REGISTRY_BY_NAME:
        return reg.REGISTRY_BY_NAME[tool_name]

    # Tag 标准描述（用于零样本分类）
    tag_descriptions = {
        Tag.READ.name:    "Read, view, or inspect file contents or code",
        Tag.EDIT.name:    "Modify, write, update, or create files and code",
        Tag.RUN.name:     "Execute commands, build, run scripts or programs",
        Tag.SEARCH.name:  "Search, find, or locate code, symbols, or files",
        Tag.ANALYZE.name: "Analyze architecture, structure, dependencies, or understand code",
        Tag.DEBUG.name:   "Debug, test, lint, or fix errors and failures",
        Tag.GIT.name:     "Git operations: commit, diff, status, branch",
        Tag.MEMORY.name:  "Store or recall project memory, decisions, or constraints",
        Tag.VERIFY.name:  "Verify, validate, or confirm code behavior or specs",
        Tag.IMPACT.name:  "Analyze impact, callers, blast radius of code changes",
        Tag.WEB.name:     "Fetch web content, URLs, or external HTTP APIs",
        Tag.MCP.name:     "External MCP plugin tools and server integrations",
    }

    try:
        from evocli_soul.local_classifier import rank_by_similarity
        items = [(name, desc) for name, desc in tag_descriptions.items()]
        ranked = rank_by_similarity(tool_docstring, items, top_k=3, threshold=0.2)
        inferred_tags = []
        for tag_name, _score in ranked:
            try:
                inferred_tags.append(Tag[tag_name])
            except KeyError:
                pass
    except Exception:
        inferred_tags = [Tag.ANALYZE]  # 推断失败 → 最宽泛意图

    if not inferred_tags:
        inferred_tags = [Tag.ANALYZE]

    # 创建临时 ToolSpec
    spec = ToolSpec(
        name=tool_name,
        rpc="",
        description=tool_docstring[:80].replace("\n", " ").strip(),
        tags=inferred_tags,
        tier=2,
        pydantic=True,
        base_score=0.7,  # 未知工具给保守初始分
    )

    # 注入运行时 REGISTRY（不持久化）
    REGISTRY.append(spec)
    reg.REGISTRY_BY_NAME[tool_name] = spec
    for tag in inferred_tags:
        reg.TAG_TO_TOOLS.setdefault(tag, []).append(spec)

    log.info(
        "ToolRouter: auto-classified unknown tool %r → tags=%s (add to tool_registry.py for persistence)",
        tool_name, [t.name for t in inferred_tags],
    )
    return spec


# ══════════════════════════════════════════════════════════════════════════════
# Skills / MCP 动态路由（统一原则：默认不暴露，按需激活）
#
# 设计来源：
#   - Claude Code deferred_loading 模式
#   - SkillReducer：只注入激活 skill 的 1-liner 描述，不注入全部 SKILL.md
# ══════════════════════════════════════════════════════════════════════════════

# 触发 Skill 激活的意图标签（只有分析/深度调试时才注入方法论指引）
_SKILL_ACTIVATION_TAGS: frozenset[Tag] = frozenset({
    Tag.ANALYZE, Tag.DEBUG, Tag.VERIFY, Tag.IMPACT,
})

def should_activate_skills(tags: list[Tag]) -> bool:
    """是否应该激活 Skill 指引注入（context_engine find_relevant_guidance）。"""
    return bool(set(tags) & _SKILL_ACTIVATION_TAGS)


def should_activate_mcp(query: str, tags: list[Tag]) -> bool:
    """是否应该激活 MCP 工具。"""
    return Tag.MCP in tags


def get_tool_names_for_llm(
    query: str,
    *,
    pydantic_only: bool = True,
    config: dict | None = None,
) -> frozenset[str]:
    """
    便捷接口：返回本次请求应该发送给 LLM 的工具名集合（frozenset）。
    
    用于：
    1. pydantic-ai deps.selected_tool_names（prepare hook 过滤依据）
    2. _build_tool_definitions() 筛选（LiteLLM 路径）
    3. context_engine active_tools 参数（记忆检索加权）
    """
    specs = select_tools(query, pydantic_only=pydantic_only, config=config)
    return frozenset(s.name for s in specs)


# ══════════════════════════════════════════════════════════════════════════════
# 记忆驱动的工具优化（从 memory_distill 结果更新分数）
#
# 调用时机：
#   - 每次 session 结束（handlers/agent.py _distill_session 后）
#   - 或者 memory_distill.py 写入成功/失败记忆后
# ══════════════════════════════════════════════════════════════════════════════

def update_scores_from_session_events(session_events: list[dict]) -> None:
    """
    从 session_events 中提取工具成功/失败记录，更新 ToolScoreStore。
    
    事件格式（来自 state.py append_session_event）：
        {"type": "tool_called", "method": "fs.read", ...}
        {"type": "tool_done",   "method": "fs.read", "ok": True, ...}
        {"type": "error",       "method": "shell.run", "error": "...", ...}
    
    RPC 方法 → 工具名：通过 REGISTRY rpc 字段反向查找
    """
    store = get_score_store()
    rpc_to_name: dict[str, str] = {
        s.rpc: s.name for s in REGISTRY if s.rpc
    }

    for event in session_events:
        ev_type = event.get("type", "")
        method  = event.get("method", "")
        tool_name = rpc_to_name.get(method) or method  # fallback to method name

        if ev_type == "tool_done":
            ok = event.get("ok", True)
            if ok:
                store.record_success(tool_name)
            else:
                store.record_failure(tool_name)
        elif ev_type == "error" and method:
            store.record_failure(tool_name)

    log.debug("ToolRouter: updated scores from %d session events", len(session_events))


def update_scores_from_memory(tool_name: str, succeeded: bool) -> None:
    """
    单条记忆驱动更新（memory_distill 写入成功/失败记忆时调用）。
    """
    store = get_score_store()
    if succeeded:
        store.record_success(tool_name)
    else:
        store.record_failure(tool_name)


# ══════════════════════════════════════════════════════════════════════════════
# 诊断/调试接口
# ══════════════════════════════════════════════════════════════════════════════

def explain_selection(query: str, max_tools: int = MAX_TOOLS_PYDANTIC) -> str:
    """
    返回工具选择的可读解释（用于 /help debug 或开发者调试）。
    
    输出示例：
        Query: "fix the login function"
        Intent: EDIT, READ, DEBUG
        Selected (12/27):
          [T1] fs_read          score=1.00  always_on
          [T1] shell_run        score=1.00  always_on
          [T2] fs_apply_search_replace  score=0.95  tags=EDIT
          [T2] fs_lint_file     score=0.85  tags=DEBUG,EDIT
          ...
    """
    store  = get_score_store()
    tags   = classify_intent(query)
    tools  = select_tools(query, max_tools=max_tools)
    lines  = [
        f"Query:  {query[:80]}",
        f"Intent: {', '.join(t.name for t in tags)}",
        f"Selected ({len(tools)}/{len(REGISTRY)} total):",
    ]
    for spec in tools:
        score  = store.effective_score(spec.name)
        reason = "always_on" if spec.always_on else f"tags={','.join(t.name for t in spec.tags[:2])}"
        lines.append(f"  [T{spec.tier}] {spec.name:<35} score={score:.2f}  {reason}")

    omitted = [s for s in REGISTRY if s.name not in {t.name for t in tools} and s.pydantic]
    if omitted:
        lines.append(f"Omitted ({len(omitted)}): {', '.join(s.name for s in omitted[:8])}...")

    return "\n".join(lines)


def registry_stats() -> str:
    """返回注册表统计摘要。"""
    s = reg.stats()
    return (
        f"ToolRegistry: {s['total']} tools total | "
        f"T1={s['tier1']} always-on | "
        f"T2={s['tier2']} intent-selected | "
        f"T3={s['tier3']} on-demand | "
        f"pydantic-ai={s['pydantic']} | "
        f"litellm-only={s['litellm_only']}"
    )

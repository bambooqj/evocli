"""
tool_registry.py — EvoCLI 统一工具注册表（Single Source of Truth）

设计来源融合：
  工业实践:
    - Claude Code：tool_search 按需加载，工具变成"可检索资源"
    - Aider：PageRank token budget，按重要性填满窗口
    - LlamaIndex ObjectIndex：工具描述向量化，query对齐 top_k
    - pydantic-ai prepare hook：per-turn 细粒度过滤
  学术论文:
    - ObjectGraph (2026)：三层渐进式披露，context 节省94%
    - TSCG (2026)：JSON > 15个工具 → 小模型准确率跌至0-49%
    - Re-Invoke (2024)：意图提取后检索 nDCG@5 提升39%
    - SkillReducer (2026)：工具描述48%压缩无质量损失
  EvoCLI 基础设施:
    - local_classifier.rank_by_similarity：fastembed MiniLM 384维
    - memory_client priority_scope="tool"：工具级记忆
    - memory_distill 成功/失败链：工具历史学习

架构：
  ┌──────────────────────────────────────────────────┐
  │  TOOL_REGISTRY  (所有工具的元数据)                │
  │  ├── Tier 1: Always-On (3-4个，每次都发送)        │
  │  ├── Tier 2: Intent-Selected (意图匹配，填满12)   │
  │  └── Tier 3: On-Demand (明确请求才激活)           │
  └──────────────────────────────────────────────────┘

新增工具的自动化适应：
  1. 在 _register_pydantic_tools 中加 @agent.tool_plain，写好 docstring
  2. 在 TOOL_REGISTRY 中添加一行 ToolSpec（30秒工作）
  3. 不需要修改任何路由逻辑 — 标签驱动自动适配
  4. 如果不加 ToolSpec：工具仍可注册，但每次都会发送（旧行为）

命名约定：
  tool_name 必须与 @agent.tool_plain 函数名完全一致。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto


# ══════════════════════════════════════════════════════════════════════════════
# 意图标签（Intent Tags）
#
# 分类依据：
#   - 操作语义（读/写/运行/搜索）
#   - 风险级别（只读 < 只读系统 < 写文件 < 执行命令）
#   - 工具重叠度（同标签工具互补，跨标签工具独立）
# ══════════════════════════════════════════════════════════════════════════════

class Tag(Enum):
    # ── 核心操作 ──────────────────────────────────────────────────────────
    READ     = auto()   # 读取已有内容：文件/符号/范围
    EDIT     = auto()   # 修改内容：写文件/应用补丁
    RUN      = auto()   # 执行命令：shell/build/test
    SEARCH   = auto()   # 查找：代码/文件/符号

    # ── 理解与分析 ─────────────────────────────────────────────────────────
    ANALYZE  = auto()   # 架构/设计/依赖分析
    DEBUG    = auto()   # 调试：lint/test/错误定位

    # ── 专业功能 ───────────────────────────────────────────────────────────
    GIT      = auto()   # 版本控制操作
    MEMORY   = auto()   # 项目记忆读写
    VERIFY   = auto()   # 验证/假设检查
    ASSUME   = auto()   # 代码特性假设验证（impact前置）
    IMPACT   = auto()   # 影响半径/调用链分析
    WEB      = auto()   # HTTP/URL 获取
    MCP      = auto()   # 外部 MCP 插件工具


# ══════════════════════════════════════════════════════════════════════════════
# 工具规格（ToolSpec）
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ToolSpec:
    """单个工具的完整元数据。

    Attributes:
        name:        工具函数名（必须与 @agent.tool_plain 函数名完全一致）
        rpc:         对应的 Rust bridge RPC 方法（python-native 工具填 ""）
        description: 30-50字的功能描述（用于 embedding 对齐，参考 SkillReducer: 精简不失质量）
        tags:        意图标签列表（可多个，用于快速路由）
        tier:        1=Always-On, 2=Intent-Selected, 3=On-Demand
        always_on:   True 时无论意图如何都加载（覆盖 tier 判断）
        pydantic:    True = 已在 _register_pydantic_tools 注册；False = 仅 LiteLLM 路径
        base_score:  初始优先分（0.0-1.0），记忆系统在此基础上动态调整
        keywords:    快速关键词匹配（比 embedding 快100倍，用于第一阶段过滤）
    """
    name:        str
    rpc:         str
    description: str
    tags:        list[Tag]
    tier:        int = 2
    always_on:   bool = False
    pydantic:    bool = True     # 是否已在 pydantic-ai 路径注册
    base_score:  float = 1.0
    keywords:    list[str] = field(default_factory=list)


# ══════════════════════════════════════════════════════════════════════════════
# 完整工具注册表
#
# 排列顺序：tier ASC，相同 tier 内按使用频率 DESC（体现优先级）
# ══════════════════════════════════════════════════════════════════════════════

REGISTRY: list[ToolSpec] = [

    # ──────────────────────────────────────────────────────────────────────────
    # Tier 1：Always-On（每次 LLM 调用必带，不受意图影响）
    # 论文依据：ObjectGraph "Index Layer" — 永远加载最基础工具
    # 数量上限：≤4 个（含入 12 个总上限的配额）
    # ──────────────────────────────────────────────────────────────────────────
    ToolSpec(
        name="fs_read", rpc="fs.read",
        description="Read full contents of any file",
        tags=[Tag.READ, Tag.ANALYZE, Tag.EDIT, Tag.DEBUG],
        tier=1, always_on=True, base_score=1.0,
        keywords=["read", "open", "show", "view", "file", "content"],
    ),
    ToolSpec(
        name="shell_run", rpc="shell.run",
        description="Execute any whitelisted shell command",
        tags=[Tag.RUN, Tag.DEBUG, Tag.ANALYZE],
        tier=1, always_on=True, base_score=1.0,
        keywords=["run", "execute", "build", "cargo", "python", "make", "npm"],
    ),
    ToolSpec(
        name="shell_grep", rpc="shell.grep",
        description="Search files by regex pattern (like grep -rn)",
        tags=[Tag.SEARCH, Tag.READ, Tag.DEBUG],
        tier=1, always_on=True, base_score=0.95,
        keywords=["grep", "search", "find", "pattern", "regex", "where"],
    ),

    # ──────────────────────────────────────────────────────────────────────────
    # Tier 2：Intent-Selected — 文件系统操作
    # ──────────────────────────────────────────────────────────────────────────
    ToolSpec(
        name="fs_read_range", rpc="fs.read_range",
        description="Read specific line range of a large file (saves tokens vs full read)",
        tags=[Tag.READ],
        tier=2, base_score=0.9,
        keywords=["lines", "range", "section", "from", "to", "part of"],
    ),
    ToolSpec(
        name="fs_read_symbol", rpc="",  # python-native: symbol.lookup + fs.read_range
        description="Read source code of a function or class by symbol name",
        tags=[Tag.READ, Tag.ANALYZE],
        tier=2, base_score=0.9,
        keywords=["function", "class", "method", "symbol", "definition", "impl"],
    ),
    ToolSpec(
        name="fs_write", rpc="fs.write",
        description="Write or overwrite a file with new content",
        tags=[Tag.EDIT],
        tier=2, base_score=0.85,
        keywords=["write", "create", "new file", "overwrite", "save"],
    ),
    ToolSpec(
        name="fs_apply_search_replace", rpc="",  # python-native: edit_engine
        description="Apply SEARCH/REPLACE block edit (preferred for LLM edits, no line numbers needed)",
        tags=[Tag.EDIT],
        tier=2, base_score=0.95,
        keywords=["edit", "modify", "change", "replace", "refactor", "fix", "update"],
    ),
    ToolSpec(
        name="fs_apply_diff", rpc="fs.apply_diff",
        description="Apply a unified diff patch to a file",
        tags=[Tag.EDIT],
        tier=2, base_score=0.75,
        keywords=["diff", "patch", "apply", "hunk"],
    ),
    ToolSpec(
        name="fs_apply_batch", rpc="",  # python-native: _execute_tool
        description="Apply SEARCH/REPLACE edits to multiple files in one call",
        tags=[Tag.EDIT],
        tier=2, base_score=0.8,
        keywords=["multiple files", "batch", "all files", "many changes"],
    ),
    ToolSpec(
        name="fs_lint_file", rpc="",  # python-native: shell.run + lang detection
        description="Run linter on a file after edits and return errors with line numbers",
        tags=[Tag.DEBUG, Tag.VERIFY, Tag.EDIT],
        tier=2, base_score=0.85,
        keywords=["lint", "check", "error", "syntax", "validate", "after edit"],
    ),

    # ──────────────────────────────────────────────────────────────────────────
    # Tier 2：Intent-Selected — 代码搜索与符号
    # ──────────────────────────────────────────────────────────────────────────
    ToolSpec(
        name="symbol_lookup", rpc="symbol.lookup",
        description="Look up where a symbol (function/class/var) is defined",
        tags=[Tag.SEARCH, Tag.READ, Tag.ANALYZE],
        tier=2, base_score=0.92,
        keywords=["where", "defined", "symbol", "function", "class", "declaration"],
    ),
    ToolSpec(
        name="search_code", rpc="search.code",
        description="Semantic or regex search across entire codebase",
        tags=[Tag.SEARCH],
        tier=2, base_score=0.88,
        keywords=["search", "find", "where is", "codebase", "across"],
    ),
    ToolSpec(
        name="code_hybrid_search", rpc="",  # python-native: BM25+vector RRF
        description="Hybrid BM25+vector search with RRF fusion (better than plain regex)",
        tags=[Tag.SEARCH, Tag.ANALYZE],
        tier=2, base_score=0.85,
        keywords=["semantic search", "fuzzy", "similar", "related code"],
    ),

    # ──────────────────────────────────────────────────────────────────────────
    # Tier 2：Intent-Selected — 执行/测试
    # ──────────────────────────────────────────────────────────────────────────
    ToolSpec(
        name="run_and_capture", rpc="shell.run",
        description="Run a command and capture stdout/stderr/exit_code",
        tags=[Tag.RUN, Tag.DEBUG],
        tier=2, base_score=0.87,
        keywords=["run", "output", "result", "capture", "stdout"],
    ),
    ToolSpec(
        name="test_and_capture", rpc="shell.run",
        description="Run tests and return output only on failure (saves tokens)",
        tags=[Tag.DEBUG, Tag.VERIFY],
        tier=2, base_score=0.88,
        keywords=["test", "pytest", "cargo test", "jest", "fail", "pass"],
    ),

    # ──────────────────────────────────────────────────────────────────────────
    # Tier 2：Intent-Selected — Git
    # ──────────────────────────────────────────────────────────────────────────
    ToolSpec(
        name="git_status", rpc="git.status",
        description="Show git working tree status (modified/staged/untracked files)",
        tags=[Tag.GIT],
        tier=2, base_score=0.9,
        keywords=["git", "status", "modified", "staged", "changes", "uncommitted"],
    ),
    ToolSpec(
        name="git_diff", rpc="git.diff",
        description="Show current git diff (staged and unstaged changes)",
        tags=[Tag.GIT],
        tier=2, base_score=0.88,
        keywords=["diff", "git diff", "what changed", "changes"],
    ),
    ToolSpec(
        name="git_commit", rpc="git.commit",
        description="Create a git commit with a message",
        tags=[Tag.GIT],
        tier=2, base_score=0.85,
        keywords=["commit", "save", "git commit", "submit"],
    ),
    ToolSpec(
        name="diff_parse_stats", rpc="",  # python-native: whatthepatch
        description="Parse a unified diff and return statistics (files changed, lines added/removed)",
        tags=[Tag.GIT, Tag.VERIFY],
        tier=2, base_score=0.7,
        keywords=["diff stats", "patch size", "lines added", "validate diff"],
    ),

    # ──────────────────────────────────────────────────────────────────────────
    # Tier 2：Intent-Selected — 记忆
    # ──────────────────────────────────────────────────────────────────────────
    ToolSpec(
        name="memory_recall", rpc="",  # python-native: LanceDB
        description="Search project memory for past decisions, constraints, lessons learned",
        tags=[Tag.MEMORY, Tag.ANALYZE],
        tier=2, base_score=0.82,
        keywords=["remember", "memory", "recall", "previous", "past", "decision", "constraint"],
    ),
    ToolSpec(
        name="memory_write", rpc="",  # python-native: LanceDB
        description="Save a decision, lesson, or constraint to project memory",
        tags=[Tag.MEMORY],
        tier=2, base_score=0.75,
        keywords=["save", "remember", "note", "constraint", "rule", "lesson"],
    ),

    # ──────────────────────────────────────────────────────────────────────────
    # Tier 3：On-Demand — 深度代码分析（Claude Code "deferred_loading" 模式）
    # ──────────────────────────────────────────────────────────────────────────
    ToolSpec(
        name="code_blast_radius", rpc="code_intel.blast_radius",
        description="Show all callers and callees of a symbol with impact risk level",
        tags=[Tag.ANALYZE, Tag.IMPACT],
        tier=3, base_score=0.8,
        keywords=["impact", "callers", "who calls", "blast radius", "breaking change"],
    ),
    ToolSpec(
        name="code_symbol_context", rpc="code_intel.symbol_context",
        description="Get 360° context for a symbol: callers, callees, communities, processes",
        tags=[Tag.ANALYZE, Tag.IMPACT],
        tier=3, base_score=0.78,
        keywords=["context", "360", "full context", "symbol info", "dependencies"],
    ),
    ToolSpec(
        name="code_communities", rpc="code_intel.communities",
        description="Detect functional code communities (clusters of related symbols)",
        tags=[Tag.ANALYZE],
        tier=3, base_score=0.7,
        keywords=["communities", "clusters", "modules", "architecture", "groups"],
    ),

    # ──────────────────────────────────────────────────────────────────────────
    # Tier 3：On-Demand — 假设验证（impact 分析前置检查）
    # ──────────────────────────────────────────────────────────────────────────
    ToolSpec(
        name="impact_check", rpc="impact.check",
        description="Check the impact radius of modifying a symbol",
        tags=[Tag.IMPACT, Tag.VERIFY],
        tier=3, pydantic=False, base_score=0.8,
        keywords=["impact", "modify", "change", "affect", "risk"],
    ),
    ToolSpec(
        name="impact_affected_tests", rpc="impact.affected_tests",
        description="List tests that would be affected by changing a symbol",
        tags=[Tag.IMPACT, Tag.VERIFY],
        tier=3, pydantic=False, base_score=0.75,
        keywords=["affected tests", "which tests", "test coverage"],
    ),
    ToolSpec(
        name="impact_batch_check", rpc="impact.batch_check",
        description="Batch impact check for multiple symbols at once",
        tags=[Tag.IMPACT],
        tier=3, pydantic=False, base_score=0.7,
        keywords=["batch impact", "multiple symbols"],
    ),
    ToolSpec(
        name="assume_has_tests", rpc="assume.has_tests",
        description="Check if a function/class has test coverage",
        tags=[Tag.ASSUME, Tag.VERIFY],
        tier=3, pydantic=False, base_score=0.75,
        keywords=["has tests", "test coverage", "tested"],
    ),
    ToolSpec(
        name="assume_is_pure", rpc="assume.is_pure",
        description="Check if a function is pure (no side effects, deterministic)",
        tags=[Tag.ASSUME],
        tier=3, pydantic=False, base_score=0.7,
        keywords=["pure", "side effects", "deterministic"],
    ),
    ToolSpec(
        name="assume_caller_count", rpc="assume.caller_count",
        description="Count how many places call a given symbol",
        tags=[Tag.ASSUME, Tag.ANALYZE],
        tier=3, pydantic=False, base_score=0.72,
        keywords=["how many callers", "usage count", "called from"],
    ),
    ToolSpec(
        name="assume_has_side_effects", rpc="assume.has_side_effects",
        description="Check if a function has observable side effects (I/O, mutation)",
        tags=[Tag.ASSUME],
        tier=3, pydantic=False, base_score=0.68,
        keywords=["side effects", "IO", "mutation", "pure"],
    ),
    ToolSpec(
        name="assume_verify", rpc="assume.verify",
        description="Verify a natural language assumption about a code element",
        tags=[Tag.ASSUME, Tag.VERIFY],
        tier=3, pydantic=False, base_score=0.72,
        keywords=["verify assumption", "is it true that", "does this"],
    ),
    ToolSpec(
        name="assume_is_deprecated", rpc="assume.is_deprecated",
        description="Check if a symbol is deprecated or has a replacement",
        tags=[Tag.ASSUME],
        tier=3, pydantic=False, base_score=0.65,
        keywords=["deprecated", "obsolete", "replacement", "old API"],
    ),
    ToolSpec(
        name="assume_is_only_caller", rpc="assume.is_only_caller",
        description="Check if a given call site is the only caller of a function",
        tags=[Tag.ASSUME],
        tier=3, pydantic=False, base_score=0.65,
        keywords=["only caller", "single caller", "unique caller"],
    ),
    ToolSpec(
        name="assume_types_match", rpc="assume.types_match",
        description="Check if two type signatures are compatible",
        tags=[Tag.ASSUME, Tag.VERIFY],
        tier=3, pydantic=False, base_score=0.68,
        keywords=["type match", "compatible", "signature"],
    ),

    # ──────────────────────────────────────────────────────────────────────────
    # Tier 3：On-Demand — 等价性检查
    # ──────────────────────────────────────────────────────────────────────────
    ToolSpec(
        name="equiv_find", rpc="equiv.find",
        description="Find existing implementations that match a described intent",
        tags=[Tag.SEARCH, Tag.ANALYZE],
        tier=3, pydantic=False, base_score=0.7,
        keywords=["already exists", "duplicate", "similar implementation", "equivalent"],
    ),
    ToolSpec(
        name="equiv_check_deps", rpc="equiv.check_deps",
        description="Check if dependencies are already available for an intent",
        tags=[Tag.ANALYZE],
        tier=3, pydantic=False, base_score=0.65,
        keywords=["dependencies", "already have", "available"],
    ),
    ToolSpec(
        name="equiv_find_similar_code", rpc="equiv.find_similar_code",
        description="Find code snippets similar to a given code block",
        tags=[Tag.SEARCH],
        tier=3, pydantic=False, base_score=0.68,
        keywords=["similar code", "clone", "duplicate code"],
    ),

    # ──────────────────────────────────────────────────────────────────────────
    # Tier 3：On-Demand — 符号详情（超出基础 lookup）
    # ──────────────────────────────────────────────────────────────────────────
    ToolSpec(
        name="symbol_variants", rpc="symbol.variants",
        description="Find all variants/implementations of a type or trait",
        tags=[Tag.ANALYZE, Tag.SEARCH],
        tier=3, pydantic=False, base_score=0.7,
        keywords=["variants", "implementations", "trait impls", "types"],
    ),
    ToolSpec(
        name="symbol_usages", rpc="symbol.usages",
        description="Find all call sites and usages of a symbol in the codebase",
        tags=[Tag.ANALYZE, Tag.SEARCH],
        tier=3, pydantic=False, base_score=0.75,
        keywords=["usages", "used by", "callers", "references"],
    ),
    ToolSpec(
        name="symbol_lifecycle", rpc="symbol.lifecycle",
        description="Trace the full lifecycle of a symbol (creation→use→deletion)",
        tags=[Tag.ANALYZE],
        tier=3, pydantic=False, base_score=0.65,
        keywords=["lifecycle", "created", "destroyed", "scope"],
    ),

    # ──────────────────────────────────────────────────────────────────────────
    # Tier 3：On-Demand — 代码智能（深度分析）
    # ──────────────────────────────────────────────────────────────────────────
    ToolSpec(
        name="code_intel_full_chain", rpc="code_intel.full_chain",
        description="Get the complete recursive call chain upstream from a symbol",
        tags=[Tag.ANALYZE, Tag.IMPACT],
        tier=3, pydantic=False, base_score=0.72,
        keywords=["call chain", "upstream", "full chain", "recursive callers"],
    ),
    ToolSpec(
        name="code_intel_full_downstream_chain", rpc="code_intel.full_downstream_chain",
        description="Get the complete downstream call chain from a symbol",
        tags=[Tag.ANALYZE, Tag.IMPACT],
        tier=3, pydantic=False, base_score=0.7,
        keywords=["downstream", "what it calls", "outgoing chain"],
    ),
    ToolSpec(
        name="code_intel_incoming_calls", rpc="code_intel.incoming_calls",
        description="List functions that directly call a given symbol",
        tags=[Tag.ANALYZE],
        tier=3, pydantic=False, base_score=0.73,
        keywords=["incoming calls", "direct callers", "who calls"],
    ),
    ToolSpec(
        name="code_intel_outgoing_calls", rpc="code_intel.outgoing_calls",
        description="List functions called by a given symbol",
        tags=[Tag.ANALYZE],
        tier=3, pydantic=False, base_score=0.7,
        keywords=["outgoing calls", "calls to", "what it calls"],
    ),
    ToolSpec(
        name="code_intel_list_symbols", rpc="code_intel.list_symbols",
        description="List all symbols defined in a specific file",
        tags=[Tag.ANALYZE, Tag.READ],
        tier=3, pydantic=False, base_score=0.72,
        keywords=["list symbols", "all functions", "file structure", "outline"],
    ),
    ToolSpec(
        name="code_intel_impact_radius", rpc="code_intel.impact_radius",
        description="Calculate the BFS impact radius if a symbol is modified",
        tags=[Tag.IMPACT, Tag.ANALYZE],
        tier=3, pydantic=False, base_score=0.75,
        keywords=["impact radius", "BFS", "cascade", "ripple effect"],
    ),
    ToolSpec(
        name="code_intel_ranked_context", rpc="code_intel.ranked_context",
        description="Get PageRank-weighted relevant symbols for a modified file",
        tags=[Tag.ANALYZE],
        tier=3, pydantic=False, base_score=0.7,
        keywords=["ranked", "context", "pagerank", "relevant symbols"],
    ),
    ToolSpec(
        name="code_intel_index_status", rpc="code_intel.index_status",
        description="Check code index status and database size",
        tags=[Tag.ANALYZE],
        tier=3, pydantic=False, base_score=0.6,
        keywords=["index", "indexed", "status", "database size"],
    ),

    # ──────────────────────────────────────────────────────────────────────────
    # Tier 3：On-Demand — 验证
    # ──────────────────────────────────────────────────────────────────────────
    ToolSpec(
        name="verify_task", rpc="verify.task",
        description="Verify that a task contract has been completed as specified",
        tags=[Tag.VERIFY],
        tier=3, pydantic=False, base_score=0.72,
        keywords=["verify task", "done", "complete", "contract"],
    ),
    ToolSpec(
        name="verify_coverage", rpc="verify.coverage",
        description="Verify test coverage meets the required threshold",
        tags=[Tag.VERIFY, Tag.DEBUG],
        tier=3, pydantic=False, base_score=0.7,
        keywords=["coverage", "test coverage", "threshold"],
    ),
    ToolSpec(
        name="verify_drift", rpc="verify.drift",
        description="Check if implementation has drifted from the original spec",
        tags=[Tag.VERIFY],
        tier=3, pydantic=False, base_score=0.65,
        keywords=["drift", "spec", "implementation mismatch", "deviated"],
    ),

    # ──────────────────────────────────────────────────────────────────────────
    # Tier 3：On-Demand — Git 高级操作
    # ──────────────────────────────────────────────────────────────────────────
    ToolSpec(
        name="git_snapshot", rpc="git.snapshot",
        description="Create a git stash snapshot for safe rollback before risky changes",
        tags=[Tag.GIT],
        tier=3, pydantic=False, base_score=0.75,
        keywords=["snapshot", "stash", "backup", "rollback", "safe"],
    ),
    ToolSpec(
        name="git_restore", rpc="git.restore",
        description="Restore files from a git snapshot",
        tags=[Tag.GIT],
        tier=3, pydantic=False, base_score=0.72,
        keywords=["restore", "rollback", "revert", "undo"],
    ),
    ToolSpec(
        name="git_shadow_snapshot", rpc="git.shadow_snapshot",
        description="Create a shadow (side-git) snapshot for non-destructive experiments",
        tags=[Tag.GIT],
        tier=3, pydantic=False, base_score=0.65,
        keywords=["shadow", "experiment", "side branch"],
    ),
    ToolSpec(
        name="git_shadow_restore", rpc="git.shadow_restore",
        description="Restore from a shadow git snapshot",
        tags=[Tag.GIT],
        tier=3, pydantic=False, base_score=0.65,
        keywords=["shadow restore"],
    ),

    # ──────────────────────────────────────────────────────────────────────────
    # Tier 3：On-Demand — Shell 工具（特殊用途）
    # ──────────────────────────────────────────────────────────────────────────
    ToolSpec(
        name="shell_find", rpc="shell.find",
        description="Find files by name pattern (like find -name)",
        tags=[Tag.SEARCH, Tag.READ],
        tier=3, pydantic=False, base_score=0.75,
        keywords=["find file", "file name", "glob", "pattern"],
    ),
    ToolSpec(
        name="shell_ls", rpc="shell.ls",
        description="List directory contents with details",
        tags=[Tag.READ, Tag.ANALYZE],
        tier=3, pydantic=False, base_score=0.72,
        keywords=["list directory", "ls", "dir", "files in"],
    ),
    ToolSpec(
        name="shell_cat", rpc="shell.cat",
        description="Print file contents (prefer fs_read for code files)",
        tags=[Tag.READ],
        tier=3, pydantic=False, base_score=0.6,
        keywords=["cat", "print file"],
    ),
    ToolSpec(
        name="shell_mkdir", rpc="shell.mkdir",
        description="Create a directory recursively",
        tags=[Tag.EDIT],
        tier=3, pydantic=False, base_score=0.65,
        keywords=["mkdir", "create directory", "new folder"],
    ),
    ToolSpec(
        name="shell_wc", rpc="shell.wc",
        description="Count lines, words, and characters in files",
        tags=[Tag.READ, Tag.ANALYZE],
        tier=3, pydantic=False, base_score=0.6,
        keywords=["count lines", "file size", "wc"],
    ),
    ToolSpec(
        name="shell_head", rpc="shell.head",
        description="Read the first N lines of a file",
        tags=[Tag.READ],
        tier=3, pydantic=False, base_score=0.65,
        keywords=["head", "first lines", "beginning"],
    ),
    ToolSpec(
        name="shell_tail", rpc="shell.tail",
        description="Read the last N lines of a file (useful for logs)",
        tags=[Tag.READ],
        tier=3, pydantic=False, base_score=0.65,
        keywords=["tail", "last lines", "end of file", "log"],
    ),
    ToolSpec(
        name="shell_mv", rpc="shell.mv",
        description="Move or rename a file or directory",
        tags=[Tag.EDIT],
        tier=3, pydantic=False, base_score=0.7,
        keywords=["move", "rename", "mv"],
    ),
    ToolSpec(
        name="shell_cp", rpc="shell.cp",
        description="Copy a file or directory",
        tags=[Tag.EDIT],
        tier=3, pydantic=False, base_score=0.65,
        keywords=["copy", "cp", "duplicate"],
    ),
    ToolSpec(
        name="shell_touch", rpc="shell.touch",
        description="Create an empty file or update its timestamp",
        tags=[Tag.EDIT],
        tier=3, pydantic=False, base_score=0.6,
        keywords=["touch", "create empty file", "new file"],
    ),

    # ──────────────────────────────────────────────────────────────────────────
    # Tier 3：On-Demand — 系统/审批
    # ──────────────────────────────────────────────────────────────────────────
    ToolSpec(
        name="approval_request", rpc="approval.request",
        description="Request user confirmation before a risky or irreversible operation",
        tags=[Tag.VERIFY],
        tier=3, pydantic=False, base_score=0.8,
        keywords=["approval", "confirm", "dangerous", "irreversible", "ask user"],
    ),
    ToolSpec(
        name="memory_constraints", rpc="",  # python-native: LanceDB
        description="Retrieve all active constraints and rules for this project",
        tags=[Tag.MEMORY, Tag.ANALYZE],
        tier=3, pydantic=False, base_score=0.75,
        keywords=["constraints", "rules", "project rules", "restrictions"],
    ),
    ToolSpec(
        name="tool_list_user", rpc="tool.list_user",
        description="List all user-registered custom tools for this project",
        tags=[Tag.ANALYZE],
        tier=3, pydantic=False, base_score=0.65,
        keywords=["custom tools", "user tools", "registered tools"],
    ),
    ToolSpec(
        name="tool_run_user", rpc="tool.run_user",
        description="Run a user-registered custom tool by name",
        tags=[Tag.RUN],
        tier=3, pydantic=False, base_score=0.65,
        keywords=["custom tool", "user tool", "run custom"],
    ),
    ToolSpec(
        name="fs_diff", rpc="fs.diff",
        description="Generate unified diff between two strings",
        tags=[Tag.ANALYZE, Tag.GIT],
        tier=3, pydantic=False, base_score=0.6,
        keywords=["generate diff", "compare strings"],
    ),

    # ──────────────────────────────────────────────────────────────────────────
    # Tier 3：On-Demand — 网络 / MCP（外部资源）
    # Claude Code 模式：默认不加载，明确请求才激活
    # ──────────────────────────────────────────────────────────────────────────
    ToolSpec(
        name="fetch_url", rpc="",  # python-native: web_fetcher
        description="Fetch a URL and return clean Markdown content",
        tags=[Tag.WEB],
        tier=3, base_score=0.75,
        keywords=["url", "http", "https", "fetch", "web", "download", "page"],
    ),
    ToolSpec(
        name="mcp_call", rpc="",  # python-native: mcp_bridge
        description="Call an external MCP tool from a connected server",
        tags=[Tag.MCP],
        tier=3, base_score=0.8,
        keywords=["mcp", "external", "plugin", "server", "integration"],
    ),
    ToolSpec(
        name="mcp_list_tools", rpc="",  # python-native: mcp_bridge
        description="List all available tools from connected MCP servers",
        tags=[Tag.MCP],
        tier=3, base_score=0.7,
        keywords=["mcp tools", "list plugins", "available tools", "external"],
    ),
]


# ══════════════════════════════════════════════════════════════════════════════
# 便捷查询接口
# ══════════════════════════════════════════════════════════════════════════════

# 按名称的快速查找字典
REGISTRY_BY_NAME: dict[str, ToolSpec] = {t.name: t for t in REGISTRY}

# 所有 pydantic-ai 路径工具名（用于 prepare hook 的快速检查）
PYDANTIC_TOOL_NAMES: frozenset[str] = frozenset(
    t.name for t in REGISTRY if t.pydantic
)

# Always-On 工具名
ALWAYS_ON_NAMES: frozenset[str] = frozenset(
    t.name for t in REGISTRY if t.always_on
)

# 按标签聚合（标签 → 工具列表，按 base_score DESC）
TAG_TO_TOOLS: dict[Tag, list[ToolSpec]] = {}
for _spec in REGISTRY:
    for _tag in _spec.tags:
        TAG_TO_TOOLS.setdefault(_tag, []).append(_spec)
for _tag in TAG_TO_TOOLS:
    TAG_TO_TOOLS[_tag].sort(key=lambda s: s.base_score, reverse=True)


def get(name: str) -> ToolSpec | None:
    """按名称获取 ToolSpec。"""
    return REGISTRY_BY_NAME.get(name)


def all_names() -> list[str]:
    """返回所有注册工具名。"""
    return [t.name for t in REGISTRY]


def stats() -> dict:
    """返回注册表统计信息。"""
    tiers = {1: 0, 2: 0, 3: 0}
    for t in REGISTRY:
        tiers[t.tier] += 1
    return {
        "total":   len(REGISTRY),
        "tier1":   tiers[1],
        "tier2":   tiers[2],
        "tier3":   tiers[3],
        "pydantic": sum(1 for t in REGISTRY if t.pydantic),
        "litellm_only": sum(1 for t in REGISTRY if not t.pydantic),
    }

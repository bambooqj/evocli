"""
tool_flow_miner.py — EvoCLI 工具流自动抽象系统

设计理念：
  "记住你用工具做事的方式，下次帮你自动做"

核心功能：
  1. 从 session_events 中挖掘重复的工具调用序列（Mine）
  2. 将具体参数抽象为模板槽位（Abstract）
  3. 存储为可执行的 ToolFlow（Store）
  4. 根据用户意图触发匹配的工具流（Trigger）
  5. 串联执行，前一步输出作为后一步输入（Execute）

架构原理：
  用户用 [symbol_lookup → fs_read_range → fs_apply_search_replace → fs_lint_file]
  修复了 3 次 bug。系统自动发现这个模式，命名为"修复符号代码"，
  下次用户说"修复 authenticate 函数"时，自动问"是否用学到的工具流？"

触发阈值（来自 ToolLLM + ObjectGraph 论文）：
  - ≥ 0.70 相似度：自动执行（高置信度）
  - ≥ 0.45 相似度：主动建议（中置信度）
  - < 0.45：不触发，走正常 agent 流程

数据流：
  session_events (rich: method + params + result)
    ├── _extract_tool_sequences()     提取带参数的工具调用序列
    ├── _abstract_params()            参数模板化（路径→{{file}}, 错误→{{error}}）
    ├── _find_repeated_flows()        识别重复出现的序列（≥2次）
    ├── _create_tool_flow()           创建 ToolFlow 对象
    └── _save_flow()                  持久化到 ~/.evocli/flows/

存储格式（JSON，动态友好）：
  ~/.evocli/flows/global/        全局工具流（跨项目）
  .evocli/flows/                 项目本地工具流（优先级更高）

参数模板槽位：
  {{file}}         文件路径参数
  {{symbol}}       符号/函数名
  {{error}}        错误信息
  {{cmd}}          shell 命令
  {{query}}        用户原始输入
  {{step_N.output}} 第 N 步的执行结果
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("evocli.tool_flow")

# ══════════════════════════════════════════════════════════════════════════════
# 配置
# ══════════════════════════════════════════════════════════════════════════════

MIN_FLOW_LENGTH      = 2     # 工具流最少包含的步骤数
MIN_REPEAT_COUNT     = 2     # 至少出现 N 次才抽象为工具流
AUTO_EXECUTE_THRESH  = 0.70  # 相似度 ≥ 0.70 → 自动执行
SUGGEST_THRESH       = 0.45  # 相似度 ≥ 0.45 → 主动建议
MAX_FLOWS_PER_PROJECT = 50   # 项目本地工具流上限（防止过拟合）
FLOW_GLOBAL_DIR      = Path.home() / ".evocli" / "flows"
STEP_TIMEOUT_S       = 30    # 单步执行超时


# ══════════════════════════════════════════════════════════════════════════════
# 数据结构
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class FlowStep:
    """工具流中的一个步骤。"""
    tool:         str               # pydantic-ai 工具名（如 "fs_read_range"）
    rpc:          str               # bridge RPC 方法（如 "fs.read_range"）
    params:       dict[str, Any]    # 模板化参数，可含 {{slot}} 占位符
    output_slot:  str = ""          # 本步输出存入的槽位名（如 "step_1.output"）
    description:  str = ""         # 人类可读描述
    requires_approval: bool = False # 危险操作需要确认


@dataclass
class ToolFlow:
    """
    从用户行为中自动抽象出的工具调用序列。
    
    不同于 Skill（静态 TOML），ToolFlow：
    - 参数是模板化的（支持 {{slot}} 占位符）
    - 步骤间可以传递输出（step_N.output → step_N+1.params）
    - 由真实使用记录学习而来（有成功率统计）
    - 会随使用自动更新（成功→提升置信度，失败→降低置信度）
    """
    id:            str
    name:          str              # "修复符号代码" / "阅读并理解文件"
    description:   str              # embedding 相似度匹配用
    steps:         list[FlowStep]
    trigger_tags:  list[str]        # 意图标签（与 tool_router.Tag 对应）
    source_hash:   str              # 用于去重的序列哈希
    success_count: int = 0
    failure_count: int = 0
    created_at:    float = field(default_factory=time.time)
    last_used_at:  float = 0.0
    confidence:    float = 0.5      # 初始置信度，随使用更新
    project_local: bool = False     # True = 项目本地流

    # ── 挣扎标记 ──────────────────────────────────────────────────────────────
    # 记录这个流是"第一次就成功"还是"失败多次后才摸索出来"。
    # 后者才是真正有价值的经验（失败后积累的成功模式）。
    # failures_before: 发现这个成功序列之前，session 里经历了几次失败重置
    failures_before: int  = 0       # 0 = 一次成功；>= 1 = 挣扎后发现
    struggle_score:  float = 0.0    # 挣扎程度分数，越高越难得 (failures_before / (failures_before + 1))

    @property
    def success_rate(self) -> float:
        total = self.success_count + self.failure_count
        return self.success_count / total if total > 0 else 0.5

    @property
    def is_struggle_discovered(self) -> bool:
        """True if this flow was found after at least one failure — battle-tested knowledge."""
        return self.failures_before >= 1

    def to_dict(self) -> dict:
        d = asdict(self)
        d["steps"] = [asdict(s) for s in self.steps]
        return d

    @staticmethod
    def from_dict(d: dict) -> "ToolFlow":
        steps = [FlowStep(**s) for s in d.pop("steps", [])]
        return ToolFlow(steps=steps, **d)


# ══════════════════════════════════════════════════════════════════════════════
# 参数模板化（Abstract Params）
#
# 将具体值替换为槽位，使工具流可以用于不同的输入。
# 来源：AnyTool (2024) 的 API 参数抽象策略
# ══════════════════════════════════════════════════════════════════════════════

# 文件路径模式
_FILE_PATH_RE = re.compile(
    r'^[./\\]?[\w.\-/\\]+\.(rs|py|ts|tsx|js|jsx|go|java|cpp|c|h|toml|json|yaml|yml|md|txt)$',
    re.I,
)
# 错误信息模式（通常较长，包含 error/failed 关键词）
_ERROR_RE = re.compile(r'(error|failed|exception|traceback|panic)', re.I)
# 符号名模式（驼峰/下划线函数名）
_SYMBOL_RE = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]{2,}$')
# 行号
_LINE_NO_RE = re.compile(r'^\d+$')


def abstract_value(key: str, value: Any) -> Any:
    """
    将单个参数值抽象为模板槽位。
    
    规则（优先级从高到低）：
    1. key 包含 "path"/"file" → {{file}}
    2. key 包含 "symbol"/"name"/"function" → {{symbol}}
    3. key 包含 "error"/"message"/"query" → {{query}}
    4. key 包含 "cmd"/"command" → {{cmd}}
    5. key 包含 "line" → {{line}}
    6. 值是文件路径 → {{file}}
    7. 值是错误字符串 → {{error}}
    8. 值是符号名 → {{symbol}}
    9. 其他短字符串 → 保留原值（可能是固定参数如 dry_run=False）
    10. 数字/布尔 → 保留原值
    """
    if not isinstance(value, str):
        return value  # 数字、布尔、列表直接保留

    key_lower = key.lower()

    # Key-based 推断
    if any(k in key_lower for k in ("path", "file")):
        return "{{file}}"
    if any(k in key_lower for k in ("symbol", "function", "name", "method")) and _SYMBOL_RE.match(str(value)):
        return "{{symbol}}"
    if any(k in key_lower for k in ("error", "message", "query", "prompt")):
        return "{{query}}"
    if any(k in key_lower for k in ("cmd", "command", "script")):
        return "{{cmd}}"
    if any(k in key_lower for k in ("line", "start", "end")) and _LINE_NO_RE.match(str(value)):
        return "{{line}}"

    # Value-based 推断
    v = str(value)
    if _FILE_PATH_RE.match(v):
        return "{{file}}"
    if _ERROR_RE.search(v) and len(v) > 20:
        return "{{error}}"
    if _SYMBOL_RE.match(v) and len(v) <= 50:
        return "{{symbol}}"

    # 短固定字符串（如 cwd=".", format="short"）→ 保留
    if len(v) <= 30:
        return value

    # 长字符串 → 视为动态内容
    return "{{content}}"


def abstract_params(tool_name: str, params: dict) -> dict:
    """将工具调用的参数字典模板化。"""
    return {k: abstract_value(k, v) for k, v in params.items()}


# ══════════════════════════════════════════════════════════════════════════════
# 序列提取（Extract Sequences from Rich Events）
# ══════════════════════════════════════════════════════════════════════════════

def _extract_tool_sequences(events: list[dict]) -> list[tuple[list[dict], int]]:
    """
    从 session_events 中提取完整的工具调用序列。

    返回 list of (sequence, failures_before) — 其中 failures_before 记录
    在这个成功序列被发现之前，同一 session 经历了几次失败重置。
    failures_before >= 1 表示"挣扎后发现"的经验，比一次成功更有价值。

    每个工具调用被表示为：
    {
        "tool": "fs_read_range",   # python 函数名
        "rpc":  "fs.read_range",   # bridge RPC
        "params": {"path": "...", "start_line": 42},
        "ok": True,
        "result_summary": "content(150 chars)",
    }

    按 session_id 分组，提取连续的成功工具调用序列。
    遇到 error/failure 则截断当前序列，并记录一次"失败"计数。
    """
    sessions: dict[str, list[dict]] = {}
    pending: dict[str, dict] = {}              # tool_name → 等待 done 事件的 call 记录
    session_failure_counts: dict[str, int] = {}  # sid → 该 session 失败重置次数

    for ev in events:
        sid      = ev.get("session_id", "default")
        ev_type  = ev.get("type", "")
        method   = ev.get("method", "")
        tool     = ev.get("tool", ev.get("name", method))

        if ev_type == "tool_called":
            # 记录调用开始（params 在这里）
            key = f"{sid}:{tool}"
            pending[key] = {
                "tool":   tool,
                "rpc":    method,
                "params": ev.get("params", {}),
            }

        elif ev_type == "tool_done":
            key = f"{sid}:{tool}"
            call_record = pending.pop(key, {
                "tool": tool, "rpc": method, "params": {}
            })
            ok = ev.get("ok", True)
            if ok:
                call_record["ok"] = True
                call_record["result_summary"] = str(ev.get("result", ""))[:100]
                sessions.setdefault(sid, []).append(call_record)
            else:
                # 失败 → 记录一次挣扎，截断当前序列
                session_failure_counts[sid] = session_failure_counts.get(sid, 0) + 1
                sessions[sid] = []  # 重置，下次从头开始

        elif ev_type in ("error", "skill_failed", "test_failed", "give_up"):
            # 显式错误事件 → 记录挣扎次数，截断
            session_failure_counts[sid] = session_failure_counts.get(sid, 0) + 1
            sessions[sid] = []  # 重置

        elif ev_type in ("skill_success", "git_commit", "test_passed"):
            # 成功锚点 → 当前序列有意义，标记一个边界
            if sid in sessions:
                sessions.setdefault(f"{sid}_committed", []).extend(sessions[sid])
                sessions[sid] = []

    # 合并所有序列，携带失败计数
    all_sequences: list[tuple[list[dict], int]] = []
    for sid, seq in sessions.items():
        if len(seq) >= MIN_FLOW_LENGTH:
            # 从 sid 推断原始 session_id（去掉 _committed 后缀）
            orig_sid = sid.replace("_committed", "")
            failures = session_failure_counts.get(orig_sid, 0)
            all_sequences.append((seq, failures))
    return all_sequences


def _sequence_hash(steps: list[dict]) -> str:
    """工具序列的结构哈希（工具名+参数键，不包含具体值）。"""
    fingerprint = "|".join(
        f"{s.get('tool', s.get('rpc', '?'))}:{sorted(s.get('params', {}).keys())}"
        for s in steps
    )
    return hashlib.md5(fingerprint.encode()).hexdigest()[:12]


# ══════════════════════════════════════════════════════════════════════════════
# 流名称生成（Auto-Naming）
# ══════════════════════════════════════════════════════════════════════════════

# 工具名 → 人类可读描述
_TOOL_VERBS: dict[str, str] = {
    "fs_read":                 "读取",
    "fs_read_range":           "读取代码段",
    "fs_read_symbol":          "查看符号",
    "fs_write":                "写入",
    "fs_apply_search_replace": "修改",
    "fs_apply_diff":           "应用补丁",
    "fs_apply_batch":          "批量修改",
    "fs_lint_file":            "检查语法",
    "shell_run":               "执行命令",
    "shell_grep":              "搜索",
    "run_and_capture":         "运行并捕获",
    "test_and_capture":        "运行测试",
    "symbol_lookup":           "查找符号",
    "code_hybrid_search":      "搜索代码",
    "git_status":              "查看Git状态",
    "git_diff":                "查看Git差异",
    "git_commit":              "提交",
    "git_snapshot":            "创建快照",
    "memory_recall":           "召回记忆",
    "memory_write":            "写入记忆",
    "code_blast_radius":       "分析影响",
    "fetch_url":               "获取网页",
    "mcp_call":                "调用MCP",
}

# 常见工具流模式的语义命名
_FLOW_PATTERNS: list[tuple[frozenset, str, str]] = [
    # (工具名集合, 名称, 描述)
    (frozenset({"fs_read_symbol", "fs_apply_search_replace", "fs_lint_file"}),
     "修复符号并验证", "读取符号代码，应用修复，然后验证语法"),
    (frozenset({"fs_read", "fs_apply_search_replace", "fs_lint_file"}),
     "读改检三连", "读取文件，应用修改，检查语法"),
    (frozenset({"symbol_lookup", "fs_read_range", "fs_apply_search_replace"}),
     "定位符号并修改", "查找符号位置，读取代码，精准修改"),
    (frozenset({"test_and_capture", "fs_apply_search_replace", "test_and_capture"}),
     "测试-修复-测试循环", "运行测试，根据失败信息修复，再次验证"),
    (frozenset({"shell_grep", "fs_read_range", "fs_apply_search_replace"}),
     "搜索定位修改", "搜索代码模式，读取上下文，应用修改"),
    (frozenset({"fs_read", "memory_write"}),
     "读取并记忆", "读取文件内容并存入项目记忆"),
    (frozenset({"git_snapshot", "fs_apply_search_replace", "test_and_capture", "git_commit"}),
     "安全修改提交", "创建快照，修改代码，验证测试，提交"),
    (frozenset({"code_blast_radius", "fs_apply_search_replace"}),
     "影响分析后修改", "评估影响范围，然后安全修改"),
    (frozenset({"fs_read", "fs_write"}),
     "读取并重写", "读取文件后进行完整重写"),
    (frozenset({"shell_grep", "symbol_lookup"}),
     "双重搜索定位", "用grep和符号索引联合定位目标代码"),
]


def _auto_name_flow(steps: list[dict]) -> tuple[str, str]:
    """
    根据工具序列自动生成名称和描述。
    
    Returns: (name, description)
    """
    tool_names = {s.get("tool", "") for s in steps}

    # 检查是否匹配已知模式
    for pattern_tools, name, desc in _FLOW_PATTERNS:
        if pattern_tools & tool_names:  # 至少包含模式工具集的一部分
            overlap = len(pattern_tools & tool_names) / len(pattern_tools)
            if overlap >= 0.6:
                return name, desc

    # 通用命名：取前3个工具的动词描述
    verbs = [
        _TOOL_VERBS.get(s.get("tool", ""), s.get("tool", "?"))
        for s in steps[:3]
    ]
    name = " → ".join(verbs)
    desc = f"自动学习的工具流：{name}"
    return name, desc


# ══════════════════════════════════════════════════════════════════════════════
# 流存储（Storage）
# ══════════════════════════════════════════════════════════════════════════════

def _flow_dir(project_local: bool = False) -> Path:
    if project_local:
        return Path(".evocli") / "flows"
    return FLOW_GLOBAL_DIR


def save_flow(flow: ToolFlow, project_local: bool = False) -> Path:
    """持久化 ToolFlow 到 JSON 文件。"""
    d = _flow_dir(project_local)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{flow.id}.json"
    path.write_text(json.dumps(flow.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("ToolFlow saved: %s → %s", flow.name, path)
    return path


def load_flows(project_local: bool = False) -> list[ToolFlow]:
    """从磁盘加载所有 ToolFlow，自动跳过已淘汰（deprecated=True）的流。"""
    d = _flow_dir(project_local)
    if not d.exists():
        return []
    flows = []
    for fp in d.glob("*.json"):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            # 跳过已淘汰的流（低成功率 + 足够数据后自动标记）
            if data.get("deprecated"):
                log.debug("Skipping deprecated flow: %s", fp.stem)
                continue
            flows.append(ToolFlow.from_dict(data))
        except Exception as e:
            log.debug("Failed to load flow %s: %s", fp, e)
    return flows


def load_all_flows() -> list[ToolFlow]:
    """加载全局 + 项目本地工具流（本地优先覆盖全局同 source_hash）。"""
    global_flows = {f.source_hash: f for f in load_flows(project_local=False)}
    local_flows  = {f.source_hash: f for f in load_flows(project_local=True)}
    # 本地覆盖全局
    merged = {**global_flows, **local_flows}
    return list(merged.values())


def update_flow_stats(flow_id: str, succeeded: bool, project_local: bool = False) -> None:
    """更新工具流成功/失败统计，并自动淘汰长期低成功率的流。

    淘汰规则（优胜劣汰，而非试图修复）：
    - total_runs >= 10 AND success_rate < 0.40 → deprecated=True
    - deprecated 的流不会被加载到 FlowTrigger（load_flows 过滤）
    - 用户可手动删除 ~/.evocli/flows/*.json 清理
    
    设计理由：
    - 工具流是参数模板，失败原因（上下文不匹配、文件结构变化）只有 LLM 才能理解
    - 静态规则无法"修复"一个流，只能判断它是否值得保留
    - 低成功率流不应继续触发，但保留文件供用户审计
    """
    d = _flow_dir(project_local)
    fp = d / f"{flow_id}.json"
    if not fp.exists():
        # 也尝试全局目录
        fp = _flow_dir(False) / f"{flow_id}.json"
    if not fp.exists():
        return
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        if succeeded:
            data["success_count"] = data.get("success_count", 0) + 1
        else:
            data["failure_count"] = data.get("failure_count", 0) + 1
        # 置信度更新（ELO 风格：成功 +0.05，失败 -0.10）
        total = data["success_count"] + data["failure_count"]
        data["confidence"] = data["success_count"] / total if total > 0 else 0.5
        data["last_used_at"] = time.time()

        # ── 自动淘汰：有足够数据且持续表现差的流标记为 deprecated ─────────────
        # 条件：至少 10 次尝试 + 成功率 < 40%
        # 不删除文件（供审计），但 deprecated=True 让 load_flows 跳过它
        _DEPRECATE_MIN_RUNS    = 10
        _DEPRECATE_MAX_SUCCESS = 0.40
        _success_rate = data["success_count"] / total if total > 0 else 0.5
        if (total >= _DEPRECATE_MIN_RUNS
                and _success_rate < _DEPRECATE_MAX_SUCCESS
                and not data.get("deprecated")):
            data["deprecated"] = True
            log.info(
                "ToolFlow %s deprecated: success_rate=%.0f%% after %d runs",
                flow_id, _success_rate * 100, total,
            )

        fp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.debug("Failed to update flow stats: %s", e)


# ══════════════════════════════════════════════════════════════════════════════
# 核心挖掘器（Tool Flow Miner）
# ══════════════════════════════════════════════════════════════════════════════

class ToolFlowMiner:
    """
    从 session_events 挖掘重复工具流。
    
    设计来源：
    - PrefixSpan (pattern_detector.py) — 频繁序列挖掘
    - AnyTool (2024) — 参数抽象模板化
    - ObjectGraph (2026) — 渐进式工具流学习
    
    挖掘流程：
    1. 提取带参数的工具调用序列
    2. 对参数进行模板化
    3. 计算序列哈希去重
    4. 检查是否已存在该流（合并统计）
    5. 达到重复阈值 → 创建/更新 ToolFlow
    """

    def __init__(self, project_local: bool = True):
        self.project_local = project_local
        self._known_hashes: set[str] = {f.source_hash for f in load_all_flows()}

    def mine(self, events: list[dict]) -> list[ToolFlow]:
        """
        从事件列表挖掘工具流。
        
        Returns: 新发现/更新的工具流列表
        """
        sequences = _extract_tool_sequences(events)
        if not sequences:
            return []

        new_flows: list[ToolFlow] = []
        hash_counts: dict[str, int] = {}
        # 记录每个 hash 对应的最大 failures_before（取最难得的那次经验）
        hash_failures: dict[str, int] = {}

        # 统计各序列出现次数（跨 session），并追踪挣扎程度
        for seq, failures_before in sequences:
            if len(seq) < MIN_FLOW_LENGTH:
                continue
            # 对序列按长度从长到短取子序列（找最有意义的流）
            for length in range(min(len(seq), 6), MIN_FLOW_LENGTH - 1, -1):
                for start in range(len(seq) - length + 1):
                    sub = seq[start:start + length]
                    abstracted = self._abstract_sequence(sub)
                    h = _sequence_hash(abstracted)
                    hash_counts[h] = hash_counts.get(h, 0) + 1
                    # 取这个 hash 见过的最大 failures_before（最难得的经验）
                    hash_failures[h] = max(hash_failures.get(h, 0), failures_before)

        # 找到重复次数达到阈值的序列
        candidates = [h for h, c in hash_counts.items() if c >= MIN_REPEAT_COUNT]

        # 为每个候选序列创建工具流
        for h in candidates:
            if h in self._known_hashes:
                # 已知流：更新统计
                update_flow_stats(h, succeeded=True, project_local=self.project_local)
                continue

            # 找到对应的实际序列（取第一个匹配的）
            representative = self._find_representative(
                [seq for seq, _ in sequences], h
            )
            if not representative:
                continue

            failures = hash_failures.get(h, 0)
            flow = self._create_flow(representative, h, failures_before=failures)
            if flow:
                save_flow(flow, project_local=self.project_local)
                self._known_hashes.add(h)
                new_flows.append(flow)
                struggle_tag = f" (struggle-discovered, {failures} failures before)" if failures > 0 else ""
                log.info("ToolFlowMiner: new flow discovered: %s (%d steps)%s",
                         flow.name, len(flow.steps), struggle_tag)

        return new_flows

    def _abstract_sequence(self, seq: list[dict]) -> list[dict]:
        """对序列中的参数进行模板化。"""
        abstracted = []
        for i, step in enumerate(seq):
            a_step = {
                "tool":  step.get("tool", ""),
                "rpc":   step.get("rpc", ""),
                "params": abstract_params(step.get("tool", ""), step.get("params", {})),
            }
            abstracted.append(a_step)
        return abstracted

    def _find_representative(self, sequences: list[list[dict]], target_hash: str) -> list[dict] | None:
        """找到哈希匹配的序列。"""
        for seq in sequences:
            for length in range(len(seq), MIN_FLOW_LENGTH - 1, -1):
                for start in range(len(seq) - length + 1):
                    sub = seq[start:start + length]
                    abstracted = self._abstract_sequence(sub)
                    if _sequence_hash(abstracted) == target_hash:
                        return abstracted
        return None

    def _create_flow(self, abstracted_seq: list[dict], source_hash: str,
                     failures_before: int = 0) -> ToolFlow | None:
        """从抽象序列创建 ToolFlow，携带挣扎元数据。"""
        name, desc = _auto_name_flow(abstracted_seq)

        steps = []
        for i, step in enumerate(abstracted_seq):
            tool_name = step.get("tool", step.get("rpc", "unknown"))
            fs = FlowStep(
                tool=tool_name,
                rpc=step.get("rpc", ""),
                params=step.get("params", {}),
                output_slot=f"step_{i+1}.output",
                description=_TOOL_VERBS.get(tool_name, tool_name),
                requires_approval=tool_name in (
                    "fs_write", "fs_apply_diff", "git_commit", "shell_run"
                ),
            )
            # 链接：如果前一步有输出槽，且当前步骤需要文件或内容参数
            if i > 0:
                prev_slot = f"step_{i}.output"
                for key, val in fs.params.items():
                    if val in ("{{content}}", "{{query}}"):
                        fs.params[key] = f"{{{{{prev_slot}}}}}"
                        break
            steps.append(fs)

        # 提取触发标签（基于工具的 Tag）
        from evocli_soul.tool_registry import REGISTRY_BY_NAME
        trigger_tags: set[str] = set()
        for step in abstracted_seq:
            spec = REGISTRY_BY_NAME.get(step.get("tool", ""))
            if spec:
                trigger_tags.update(t.name for t in spec.tags[:2])

        return ToolFlow(
            id=source_hash,  # 用 hash 作为 ID，保证幂等
            name=name,
            description=desc,
            steps=steps,
            trigger_tags=list(trigger_tags),
            source_hash=source_hash,
            confidence=0.5,
            project_local=self.project_local,
            failures_before=failures_before,
            struggle_score=failures_before / (failures_before + 1) if failures_before > 0 else 0.0,
        )


# ══════════════════════════════════════════════════════════════════════════════
# 触发器（Flow Trigger）
#
# 根据用户输入，检测是否有匹配的工具流
# ══════════════════════════════════════════════════════════════════════════════

class FlowTrigger:
    """
    工具流触发器：根据用户意图匹配已学习的工具流。
    
    设计：
    - 先用 tool_router 的意图标签快速过滤（< 1ms）
    - 再用 embedding 精确匹配（5-15ms）
    - 只对 confidence ≥ 0.4 的工具流做 embedding 比较
    
    来源：Re-Invoke (2024) 意图检索 + LlamaIndex top_k retrieval
    """

    def __init__(self):
        self._flows: list[ToolFlow] = []
        self._reload()

    def _reload(self) -> None:
        self._flows = load_all_flows()
        log.debug("FlowTrigger: loaded %d tool flows", len(self._flows))

    def reload(self) -> None:
        """重新加载（新流被挖掘后调用）。"""
        self._reload()

    def match(self, query: str, threshold: float = SUGGEST_THRESH) -> list[tuple[ToolFlow, float]]:
        """
        查找与 query 匹配的工具流。
        
        Returns: [(flow, similarity), ...] 按相似度 DESC 排序
        """
        if not self._flows:
            return []

        # Stage 1: 标签预过滤（快速）
        try:
            from evocli_soul.tool_router import classify_intent
            intent_tags = {t.name for t in classify_intent(query)}
            # 只保留有标签交集的流
            candidates = [
                f for f in self._flows
                if not f.trigger_tags or bool(set(f.trigger_tags) & intent_tags)
                if f.confidence >= 0.35 and getattr(f, 'success_rate', 1.0) >= 0.60  # 至少60%成功率才进候选池
            ]
        except Exception:
            candidates = self._flows

        if not candidates:
            return []

        # Stage 2: embedding 相似度匹配
        try:
            from evocli_soul.local_classifier import rank_by_similarity
            items = [(f.id, f"{f.name}: {f.description}") for f in candidates]
            ranked = rank_by_similarity(query, items, top_k=5, threshold=threshold)

            id_to_flow = {f.id: f for f in candidates}
            results = [
                (id_to_flow[fid], score)
                for fid, score in ranked
                if fid in id_to_flow
            ]
            return results
        except Exception as e:
            log.debug("FlowTrigger embedding failed (fallback to keyword): %s", e)
            # Fallback: 关键词匹配
            results = []
            query_lower = query.lower()
            for flow in candidates:
                kw_score = sum(
                    1 for kw in flow.name.lower().split()
                    if len(kw) > 2 and kw in query_lower
                ) / max(1, len(flow.name.split()))
                if kw_score >= 0.3:
                    results.append((flow, kw_score))
            return sorted(results, key=lambda x: x[1], reverse=True)[:3]

    def suggest(self, query: str) -> ToolFlow | None:
        """
        返回最匹配的工具流（如果相似度 ≥ SUGGEST_THRESH）。
        高于 AUTO_EXECUTE_THRESH 时调用方可直接执行，否则仅建议。
        """
        matches = self.match(query, threshold=SUGGEST_THRESH)
        if matches:
            return matches[0][0]
        return None

    def confidence_for(self, query: str, flow: ToolFlow) -> float:
        """计算特定工具流对于该查询的置信度。"""
        matches = self.match(query, threshold=0.0)
        for f, score in matches:
            if f.id == flow.id:
                return score * flow.confidence  # 结合历史成功率
        return 0.0


# ══════════════════════════════════════════════════════════════════════════════
# 执行引擎（Flow Executor）
#
# 串联执行工具流，步骤间传递输出
# ══════════════════════════════════════════════════════════════════════════════

class FlowExecutor:
    """
    工具流执行引擎。
    
    核心特性：
    1. 上下文变量（Context Variables）：
       - {{file}}     从 agent context 获取当前文件
       - {{symbol}}   从用户输入提取符号名
       - {{query}}    用户原始输入
       - {{step_N.output}}  第 N 步的执行结果
    
    2. 步骤间数据传递：
       每个步骤执行后，结果存入 context["step_N.output"]
       下一步骤的参数通过 _resolve_params() 注入
    
    3. 流式进度（streaming）：
       通过 progress_callback 实时报告步骤状态
    
    4. 失败处理：
       单步失败 → 记录错误 → 是否继续由 continue_on_error 决定
    """

    def __init__(self, bridge, config: dict | None = None):
        self.bridge = bridge
        self.config = config or {}

    async def execute(
        self,
        flow: ToolFlow,
        user_input: str,
        context: dict | None = None,
        progress_callback=None,  # async callable(step_num, total, description)
        dry_run: bool = False,
    ) -> dict:
        """
        执行工具流的所有步骤。
        
        Args:
            flow:              要执行的工具流
            user_input:        用户原始输入（用于提取 {{query}} 等）
            context:           额外上下文（current_file, symbol 等）
            progress_callback: 进度回调 async fn(step_n, total, desc, result)
            dry_run:           True = 只显示计划，不执行
        
        Returns:
            {
                "ok": bool,
                "steps": [{step_id, tool, ok, result, error}],
                "final_output": str,  # 最后一步的输出
                "flow_id": str,
            }
        """
        ctx: dict[str, Any] = {
            "query":        user_input,
            "file":         (context or {}).get("current_file", ""),
            "symbol":       _extract_symbol_from_query(user_input),
            "error":        (context or {}).get("last_error", ""),
            "cmd":          "",
            "content":      "",
        }
        if context:
            ctx.update(context)

        step_results = []
        final_output = ""

        for i, step in enumerate(flow.steps):
            step_num = i + 1
            total    = len(flow.steps)

            # 解析模板参数
            resolved_params = self._resolve_params(step.params, ctx)
            desc = f"[{step_num}/{total}] {step.description or step.tool}"

            if progress_callback:
                await progress_callback(step_num, total, desc, None)

            if dry_run:
                step_results.append({
                    "step":   step_num,
                    "tool":   step.tool,
                    "params": resolved_params,
                    "dry_run": True,
                })
                continue

            # 执行步骤
            result_str, ok, error_msg = await self._execute_step(
                step, resolved_params
            )

            # 存储结果到上下文（供下一步使用）
            ctx[f"step_{step_num}.output"] = result_str
            if step.output_slot:
                ctx[step.output_slot] = result_str

            # 更新 file/content 上下文（如果本步读了文件）
            if step.tool in ("fs_read", "fs_read_range", "fs_read_symbol") and ok:
                ctx["content"] = result_str[:2000]  # 截断避免 token 爆炸

            step_results.append({
                "step":   step_num,
                "tool":   step.tool,
                "params": resolved_params,
                "ok":     ok,
                "result": result_str[:500] if ok else "",
                "error":  error_msg,
            })

            if progress_callback:
                await progress_callback(step_num, total, desc, result_str if ok else error_msg)

            if not ok:
                log.warning("FlowExecutor: step %d/%d failed: %s → %s",
                            step_num, total, step.tool, error_msg)
                # 失败 → 记录并停止（工具流保持原子性）
                update_flow_stats(flow.id, succeeded=False,
                                  project_local=flow.project_local)
                return {
                    "ok":          False,
                    "steps":       step_results,
                    "final_output": error_msg,
                    "flow_id":     flow.id,
                    "failed_step": step_num,
                }

            final_output = result_str

        # 全部成功
        if not dry_run:
            update_flow_stats(flow.id, succeeded=True,
                              project_local=flow.project_local)

        return {
            "ok":          True,
            "steps":       step_results,
            "final_output": final_output,
            "flow_id":     flow.id,
        }

    async def _execute_step(
        self,
        step: FlowStep,
        resolved_params: dict,
    ) -> tuple[str, bool, str]:
        """
        执行单个步骤，返回 (result_str, ok, error_msg)。
        """
        import asyncio as _asyncio
        try:
            result = await _asyncio.wait_for(
                self.bridge.call(step.rpc or step.tool, resolved_params),
                timeout=STEP_TIMEOUT_S,
            )
            if isinstance(result, str):
                return result, True, ""
            return json.dumps(result, ensure_ascii=False), True, ""
        except _asyncio.TimeoutError:
            return "", False, f"Timeout ({STEP_TIMEOUT_S}s) on {step.tool}"
        except Exception as e:
            return "", False, str(e)

    def _resolve_params(self, params: dict, ctx: dict) -> dict:
        """
        将参数中的 {{slot}} 占位符替换为实际值。
        
        Example:
            params = {"path": "{{file}}", "start_line": "{{line}}"}
            ctx = {"file": "src/main.rs", "line": "42"}
            → {"path": "src/main.rs", "start_line": "42"}
        """
        resolved = {}
        for key, val in params.items():
            if isinstance(val, str):
                # 替换所有 {{slot}} 占位符
                def replace_slot(m):
                    slot_name = m.group(1)
                    return str(ctx.get(slot_name, m.group(0)))  # 不认识的槽位保留原样
                resolved[key] = re.sub(r'\{\{([^}]+)\}\}', replace_slot, val)
            else:
                resolved[key] = val
        return resolved


def _extract_symbol_from_query(query: str) -> str:
    """从用户输入中提取可能的符号名。"""
    # 匹配 backtick 包裹的名称：`authenticate`
    m = re.search(r'`([a-zA-Z_][a-zA-Z0-9_]+)`', query)
    if m:
        return m.group(1)
    # 匹配驼峰/下划线命名（独立词语）
    m = re.search(r'\b([A-Z][a-zA-Z0-9]+|[a-z][a-zA-Z0-9]*_[a-zA-Z0-9_]+)\b', query)
    if m:
        return m.group(1)
    return ""


# ══════════════════════════════════════════════════════════════════════════════
# 便捷接口（供外部调用）
# ══════════════════════════════════════════════════════════════════════════════

# 进程级单例
_trigger: FlowTrigger | None = None


def get_trigger() -> FlowTrigger:
    global _trigger
    if _trigger is None:
        _trigger = FlowTrigger()
    return _trigger


def mine_from_events(events: list[dict], project_local: bool = True) -> list[ToolFlow]:
    """从 session events 挖掘工具流（在 _distill_session 中调用）。"""
    miner = ToolFlowMiner(project_local=project_local)
    new_flows = miner.mine(events)
    if new_flows:
        # 刷新触发器单例
        get_trigger().reload()
        log.info("ToolFlowMiner: %d new flows discovered", len(new_flows))
    return new_flows


def check_flow_trigger(query: str) -> tuple[ToolFlow | None, float]:
    """
    检查是否有匹配的工具流。
    
    Returns: (flow, similarity) or (None, 0.0)
    
    调用方根据 similarity 决定：
    - ≥ AUTO_EXECUTE_THRESH(0.70): 自动执行
    - ≥ SUGGEST_THRESH(0.45):      向用户建议
    - < 0.45: 不触发
    """
    trigger = get_trigger()
    matches = trigger.match(query, threshold=SUGGEST_THRESH)
    if matches:
        best_flow, score = matches[0]
        return best_flow, score
    return None, 0.0


def list_flows(project_local_only: bool = False) -> list[dict]:
    """列出所有工具流（用于 /flows 命令）。"""
    flows = load_flows(project_local=True) if project_local_only else load_all_flows()
    return [
        {
            "id":          f.id,
            "name":        f.name,
            "steps":       len(f.steps),
            "confidence":  round(f.confidence, 2),
            "success_rate": round(f.success_rate, 2),
            "step_tools":  [s.tool for s in f.steps],
        }
        for f in sorted(flows, key=lambda x: x.confidence, reverse=True)
    ]

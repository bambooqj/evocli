"""
handlers/code_analysis.py — 代码分析工具（从 Rust tool_dispatch.rs 迁移）

架构原则（地基 vs 房子）：
  Rust 提供：原始数据（符号索引、调用图、代码搜索结果、合约数据库）
  Python 决定：所有策略逻辑（评分权重、风险阈值、语义判断规则）

迁移原因：
  这些工具的策略逻辑（评分公式、关键词列表、风险标准）原来硬编码在 Rust 里，
  导致任何改进都需要重新编译二进制。现在策略在 Python 层，可以通过 AI 对话进化。

包含的 RPC 方法：
  assume.*              — 代码假设验证（8 个工具）
  impact.*              — 变更影响分析（3 个工具）
  equiv.*               — 等价代码查找（3 个工具）
  verify.*              — 任务合约验证（3 个工具，使用 LLM 替代关键词启发式）
  symbol.usages         — 符号用法查找
  symbol.lifecycle      — 符号版本历史
  code_intel.ranked_context — 相关符号排名（Aider PageRank，权重可进化）
"""
from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger("evocli.handlers.code_analysis")


def register(router) -> None:
    # assume.* — 代码假设验证
    router.add("assume.verify",           handle_assume_verify)
    router.add("assume.is_pure",          handle_assume_is_pure)
    router.add("assume.caller_count",     handle_assume_caller_count)
    router.add("assume.has_tests",        handle_assume_has_tests)
    router.add("assume.has_side_effects", handle_assume_has_side_effects)
    router.add("assume.is_only_caller",   handle_assume_is_only_caller)
    router.add("assume.is_deprecated",    handle_assume_is_deprecated)
    router.add("assume.types_match",      handle_assume_types_match)

    # impact.* — 变更影响分析
    router.add("impact.check",            handle_impact_check)
    router.add("impact.affected_tests",   handle_impact_affected_tests)
    router.add("impact.batch_check",      handle_impact_batch_check)

    # equiv.* — 等价代码查找
    router.add("equiv.find",              handle_equiv_find)
    router.add("equiv.check_deps",        handle_equiv_check_deps)
    router.add("equiv.find_similar_code", handle_equiv_find_similar_code)

    # verify.* — 任务合约验证
    router.add("verify.task",             handle_verify_task)
    router.add("verify.coverage",         handle_verify_coverage)
    router.add("verify.drift",            handle_verify_drift)

    # symbol.* — 符号分析（补充查找和历史）
    router.add("symbol.usages",           handle_symbol_usages)
    router.add("symbol.lifecycle",        handle_symbol_lifecycle)

    # code_intel — 相关符号排名
    router.add("code_intel.ranked_context", handle_ranked_context)


# ── 共用辅助函数 ──────────────────────────────────────────────────────────────

async def _search(bridge, query: str, path: str = ".") -> list[dict]:
    """调用 Rust search.code，返回匹配列表。失败时返回空列表（非致命）。"""
    try:
        result = await bridge.call("search.code", {"query": query, "path": path})
        return result if isinstance(result, list) else []
    except Exception as e:
        log.debug("search.code(%r) failed: %s", query, e)
        return []


async def _search_in_tests(bridge, query: str) -> list[dict]:
    """在测试文件中搜索（根据文件路径过滤 test/spec/_test）。"""
    all_matches = await _search(bridge, query)
    return [
        m for m in all_matches
        if any(x in m.get("file", "").lower() for x in ["test", "spec", "_test"])
    ]


async def _incoming_calls(bridge, symbol_id: str) -> list[dict]:
    """获取调用此符号的调用者列表。失败时静默回退到空列表。"""
    try:
        result = await bridge.call("code_intel.incoming_calls", {"symbol_id": symbol_id})
        return result if isinstance(result, list) else []
    except Exception as e:
        import logging as _log
        _log.getLogger("evocli.code_analysis").debug("incoming_calls failed for %s: %s", symbol_id, e)
        return []


def _filter_callers(matches: list[dict]) -> list[dict]:
    """从搜索结果中过滤掉定义行（fn/def/class/struct 开头的不算调用）。"""
    skip_prefixes = ("fn ", "def ", "class ", "struct ", "impl ", "pub fn ", "async fn ")
    return [
        m for m in matches
        if not m.get("content", "").strip().startswith(skip_prefixes)
    ]


async def _get_contracts(bridge) -> list[dict]:
    """从 Rust contracts 层获取全部活跃合约。"""
    try:
        result = await bridge.call("contracts.list", {})
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return result.get("contracts", [])
        return []
    except Exception as e:
        log.debug("contracts.list failed: %s", e)
        return []


async def _get_checkpoints(bridge, contract_id: str) -> list[dict]:
    """获取合约的检查点列表。"""
    try:
        result = await bridge.call("contracts.get_checkpoints", {"contract_id": contract_id})
        return result if isinstance(result, list) else []
    except Exception as e:
        log.debug("contracts.get_checkpoints failed: %s", e)
        return []


# ── assume.* handlers ────────────────────────────────────────────────────────

async def handle_assume_verify(req_id: str, params: dict, send, state) -> None:
    """
    通用假设验证入口（根据假设描述自动路由到具体检查）。

    params:
      assumption: str   假设描述（"has test coverage" / "is pure" / "is deprecated" 等）
      subject:    str   被检查的符号名或文件名
    """
    assumption = params.get("assumption", "").lower()
    subject    = params.get("subject", "")
    try:
        bridge = state.get_bridge()

        if any(k in assumption for k in ["test", "covered", "coverage"]):
            matches = await _search_in_tests(bridge, subject)
            await send.response(req_id, {
                "verified":   bool(matches),
                "confidence": "medium",
                "evidence":   [{"file": m["file"], "line": m["line"]} for m in matches[:3]],
            })

        elif any(k in assumption for k in ["caller", "called by"]):
            all_m   = await _search(bridge, subject)
            callers = _filter_callers(all_m)
            await send.response(req_id, {
                "verified":   True,
                "confidence": "high",
                "callers":    len(callers),
            })

        elif any(k in assumption for k in ["pure", "side effect"]):
            await handle_assume_is_pure(req_id, {"symbol": subject}, send, state)

        elif "deprecated" in assumption:
            await handle_assume_is_deprecated(req_id, {"symbol": subject}, send, state)

        else:
            await send.response(req_id, {
                "verified":   False,
                "confidence": "low",
                "note": (
                    "Could not map assumption to a known check. "
                    "Supported: 'has test coverage', 'has callers', 'is pure', 'is deprecated'"
                ),
            })

    except Exception as e:
        log.exception("assume.verify failed")
        await send.error(req_id, -32603, str(e))


# 副作用信号（Python 层 — 可通过 AI 对话扩展）
_IMPURE_PATTERNS: list[str] = [
    # Rust I/O
    "println!", "eprintln!", "print!", "eprint!",
    "fs::", "std::fs", "File::",
    "Command::", "std::process",
    "TcpStream", "UdpSocket", "std::net",
    # 数据库写入
    "INSERT", "UPDATE", "DELETE", ".execute(",
    # 全局可变状态
    "static mut", "lazy_static!", "once_cell",
    # Python I/O
    "open(", "subprocess", "os.system",
    "requests.", "httpx.", "aiohttp.",
]


async def handle_assume_is_pure(req_id: str, params: dict, send, state) -> None:
    """
    检查符号是否为纯函数（无副作用）。

    策略说明：通过搜索函数体中的副作用信号判断。
    _IMPURE_PATTERNS 列表现在在 Python 层，可随时通过对话更新。

    params:
      symbol: str   要检查的符号名
    """
    sym = params.get("symbol", "")
    try:
        bridge  = state.get_bridge()
        matches = await _search(bridge, sym)
        all_content = " ".join(m.get("content", "") for m in matches)

        effects = [p for p in _IMPURE_PATTERNS if p in all_content]

        await send.response(req_id, {
            "is_pure":        not effects,
            "effects":        effects,
            "symbol":         sym,
            "patterns_checked": len(_IMPURE_PATTERNS),
        })
    except Exception as e:
        log.exception("assume.is_pure failed")
        await send.error(req_id, -32603, str(e))


async def handle_assume_caller_count(req_id: str, params: dict, send, state) -> None:
    """
    统计符号的调用者数量（排除定义行）。

    params:
      symbol: str   要检查的符号名
    """
    sym = params.get("symbol", "")
    try:
        bridge  = state.get_bridge()
        matches = await _search(bridge, sym)
        callers = _filter_callers(matches)
        await send.response(req_id, {
            "count":   len(callers),
            "callers": [{"file": m["file"], "line": m["line"]} for m in callers],
            "symbol":  sym,
        })
    except Exception as e:
        log.exception("assume.caller_count failed")
        await send.error(req_id, -32603, str(e))


async def handle_assume_has_tests(req_id: str, params: dict, send, state) -> None:
    """
    检查符号是否有测试覆盖。

    params:
      symbol: str   要检查的符号名
    """
    sym = params.get("symbol", "")
    try:
        bridge       = state.get_bridge()
        test_matches = await _search_in_tests(bridge, sym)
        files        = list({m["file"] for m in test_matches})
        await send.response(req_id, {
            "covered":    bool(test_matches),
            "test_files": files,
            "symbol":     sym,
        })
    except Exception as e:
        log.exception("assume.has_tests failed")
        await send.error(req_id, -32603, str(e))


async def handle_assume_has_side_effects(req_id: str, params: dict, send, state) -> None:
    """
    检查符号是否有明显的副作用（文件 I/O、网络调用、数据库写入）。

    params:
      symbol: str   要检查的符号名
    """
    sym = params.get("symbol", "")
    try:
        bridge  = state.get_bridge()
        matches = await _search(bridge, sym)
        content = " ".join(m.get("content", "") for m in matches)

        writes_file  = any(p in content for p in [
            "fs::write", "File::create", "open(", ".write(", "std::fs::write",
        ])
        network_call = any(p in content for p in [
            "connect(", ".request(", "TcpStream", "httpx", "requests", "aiohttp",
        ])
        db_write     = any(p in content for p in [
            "INSERT", "UPDATE", "DELETE", ".execute(",
        ])

        await send.response(req_id, {
            "has_side_effects": writes_file or network_call or db_write,
            "effects": {
                "writes_file":   writes_file,
                "network_call":  network_call,
                "db_write":      db_write,
            },
            "symbol": sym,
        })
    except Exception as e:
        log.exception("assume.has_side_effects failed")
        await send.error(req_id, -32603, str(e))


async def handle_assume_is_only_caller(req_id: str, params: dict, send, state) -> None:
    """
    检查 caller 是否是 target 的唯一调用者。

    params:
      caller: str   声称的唯一调用者名
      target: str   被调用的符号名
    """
    caller = params.get("caller", "")
    target = params.get("target", "")
    try:
        bridge  = state.get_bridge()
        matches = await _search(bridge, target)
        all_callers = _filter_callers(matches)
        is_only = (
            len(all_callers) == 1
            and caller in all_callers[0].get("content", "")
        ) if all_callers else False

        await send.response(req_id, {
            "is_only_caller": is_only,
            "caller":         caller,
            "target":         target,
            "total_callers":  len(all_callers),
            "confidence":     "high" if len(all_callers) <= 1 else "medium",
        })
    except Exception as e:
        log.exception("assume.is_only_caller failed")
        await send.error(req_id, -32603, str(e))


# 废弃标记（Python 层 — 可扩展）
_DEPRECATED_MARKERS: list[str] = [
    "#[deprecated", "@deprecated", "# Deprecated", "# DEPRECATED",
    "DEPRECATED", ".. deprecated::", "@Deprecated",
    "# 已废弃", "# 弃用",
]


async def handle_assume_is_deprecated(req_id: str, params: dict, send, state) -> None:
    """
    检查符号是否被标记为废弃。

    params:
      symbol: str   要检查的符号名
    """
    sym = params.get("symbol", "")
    try:
        bridge  = state.get_bridge()
        matches = await _search(bridge, sym)

        deprecated_matches = [
            m for m in matches
            if any(marker in m.get("content", "") for marker in _DEPRECATED_MARKERS)
        ]

        await send.response(req_id, {
            "is_deprecated": bool(deprecated_matches),
            "symbol":        sym,
            "evidence":      [
                {"file": m["file"], "line": m["line"]}
                for m in deprecated_matches[:3]
            ],
        })
    except Exception as e:
        log.exception("assume.is_deprecated failed")
        await send.error(req_id, -32603, str(e))


async def handle_assume_types_match(req_id: str, params: dict, send, state) -> None:
    """
    启发式检查两个符号是否类型兼容（基于共现文件）。

    params:
      symbol_a: str
      symbol_b: str
    """
    sym_a = params.get("symbol_a", "")
    sym_b = params.get("symbol_b", "")
    try:
        bridge    = state.get_bridge()
        matches_a = await _search(bridge, sym_a)
        matches_b = await _search(bridge, sym_b)

        files_a = {m["file"] for m in matches_a}
        files_b = {m["file"] for m in matches_b}
        shared  = list(files_a & files_b)

        await send.response(req_id, {
            "types_match":  bool(shared),
            "symbol_a":     sym_a,
            "symbol_b":     sym_b,
            "shared_files": shared,
            "confidence":   "low",
            "note":         "Heuristic: symbols co-located in same file suggests compatibility",
        })
    except Exception as e:
        log.exception("assume.types_match failed")
        await send.error(req_id, -32603, str(e))


# ── impact.* handlers ────────────────────────────────────────────────────────

# 风险评分权重（Python 层 — 可通过 AI 对话调整）
_RISK_WEIGHTS: dict[str, Any] = {
    "caller_weight":      10,   # 每个调用者贡献的基础分
    "signature_penalty":  30,   # 修改函数签名的额外风险
    "delete_penalty":     40,   # 删除符号的额外风险
    "behavior_penalty":   10,   # 修改行为的基础风险
    "no_test_penalty":    15,   # 没有测试覆盖的额外风险
    "critical_threshold": 80,
    "high_threshold":     50,
    "medium_threshold":   25,
}


def _compute_risk(caller_count: int, change_type: str, has_tests: bool) -> tuple[float, str]:
    """计算风险分数和等级（策略完全在 Python，可进化）。"""
    w = _RISK_WEIGHTS
    score = (
        caller_count * w["caller_weight"]
        + {"signature": w["signature_penalty"],
           "delete":    w["delete_penalty"]}.get(change_type, w["behavior_penalty"])
        + (w["no_test_penalty"] if not has_tests else 0)
    )
    risk = (
        "CRITICAL" if score >= w["critical_threshold"] else
        "HIGH"     if score >= w["high_threshold"]     else
        "MEDIUM"   if score >= w["medium_threshold"]   else
        "LOW"
    )
    return score, risk


async def handle_impact_check(req_id: str, params: dict, send, state) -> None:
    """
    分析修改符号的影响范围和风险等级。

    策略说明：
      风险评分算法在 Python 层（_RISK_WEIGHTS），权重可通过对话调整。
      优先使用调用图数据（code_intel.incoming_calls），无索引时回退到文本搜索。

    params:
      symbol:      str   要分析的符号名或 ID
      change_type: str   修改类型："behavior"|"signature"|"delete"（默认 "behavior"）
    """
    sym         = params.get("symbol", "")
    change_type = params.get("change_type", "behavior")
    try:
        bridge = state.get_bridge()

        # 优先用调用图（精确），回退到文本搜索
        callers = await _incoming_calls(bridge, sym)
        if not callers:
            all_m   = await _search(bridge, sym)
            callers = _filter_callers(all_m)

        test_matches   = await _search_in_tests(bridge, sym)
        affected_files = list({c.get("file", "") for c in callers if c.get("file")})
        has_tests      = bool(test_matches)
        score, risk    = _compute_risk(len(callers), change_type, has_tests)

        await send.response(req_id, {
            "symbol":         sym,
            "change_type":    change_type,
            "direct_callers": len(callers),
            "affected_files": affected_files,
            "risk_level":     risk,
            "has_tests":      has_tests,
            "score":          score,
        })
    except Exception as e:
        log.exception("impact.check failed")
        await send.error(req_id, -32603, str(e))


async def handle_impact_affected_tests(req_id: str, params: dict, send, state) -> None:
    """
    查找会受符号修改影响的测试文件。

    params:
      symbol: str   要检查的符号名
    """
    sym = params.get("symbol", "")
    try:
        bridge       = state.get_bridge()
        test_matches = await _search_in_tests(bridge, sym)
        files        = list({m["file"] for m in test_matches})
        await send.response(req_id, {
            "symbol":         sym,
            "affected_tests": files,
        })
    except Exception as e:
        log.exception("impact.affected_tests failed")
        await send.error(req_id, -32603, str(e))


async def handle_impact_batch_check(req_id: str, params: dict, send, state) -> None:
    """
    批量分析多个符号的影响范围（单次 RPC 调用）。

    params:
      symbols:     list[str]  要分析的符号列表
      change_type: str        修改类型（默认 "behavior"）
    """
    symbols     = params.get("symbols", [])
    change_type = params.get("change_type", "behavior")
    try:
        bridge  = state.get_bridge()
        results = []
        for sym_val in symbols:
            sym = sym_val if isinstance(sym_val, str) else str(sym_val)
            callers = await _incoming_calls(bridge, sym)
            if not callers:
                all_m   = await _search(bridge, sym)
                callers = _filter_callers(all_m)

            test_matches   = await _search_in_tests(bridge, sym)
            affected_files = list({c.get("file", "") for c in callers if c.get("file")})
            has_tests      = bool(test_matches)
            score, risk    = _compute_risk(len(callers), change_type, has_tests)

            results.append({
                "symbol":         sym,
                "change_type":    change_type,
                "direct_callers": len(callers),
                "affected_files": affected_files,
                "risk_level":     risk,
                "has_tests":      has_tests,
                "score":          score,
            })

        await send.response(req_id, {"batch": results, "total": len(results)})
    except Exception as e:
        log.exception("impact.batch_check failed")
        await send.error(req_id, -32603, str(e))


# ── equiv.* handlers ─────────────────────────────────────────────────────────

async def handle_equiv_find(req_id: str, params: dict, send, state) -> None:
    """
    基于意图描述查找等价的已有实现。

    params:
      intent: str   功能意图描述（拆分为关键词搜索）
      limit:  int   最多返回结果数（默认 5）
    """
    intent = params.get("intent", "")
    limit  = int(params.get("limit", 5))
    try:
        bridge      = state.get_bridge()
        all_matches: list[dict] = []
        for kw in intent.split():
            if len(kw) > 3:
                all_matches.extend(await _search(bridge, kw))

        # 去重（按 file:line）
        seen: set[str] = set()
        unique = []
        for m in all_matches:
            key = f"{m.get('file','')}:{m.get('line',0)}"
            if key not in seen:
                seen.add(key)
                unique.append(m)

        await send.response(req_id, unique[:limit])
    except Exception as e:
        log.exception("equiv.find failed")
        await send.error(req_id, -32603, str(e))


async def handle_equiv_check_deps(req_id: str, params: dict, send, state) -> None:
    """
    检查实现某意图所需的依赖是否已存在于项目。

    params:
      intent: str   功能意图描述（关键词匹配依赖文件）
    """
    intent = params.get("intent", "")
    try:
        bridge = state.get_bridge()

        # 读取所有可能的依赖声明文件
        dep_files = ["Cargo.toml", "pyproject.toml", "package.json", "requirements.txt"]
        all_dep_content = ""
        for dep_file in dep_files:
            try:
                content = await bridge.call("fs.read", {"path": dep_file})
                if isinstance(content, str):
                    all_dep_content += f"\n# {dep_file}\n{content}"
            except Exception as e:
                log.debug("equiv.check_deps: skipping %s (read failed): %s", dep_file, e)

        found = []
        for word in intent.split():
            if len(word) > 4 and word.lower() in all_dep_content.lower():
                found.append({
                    "dep_name":         word,
                    "already_imported": True,
                })

        await send.response(req_id, {"existing_deps": found})
    except Exception as e:
        log.exception("equiv.check_deps failed")
        await send.error(req_id, -32603, str(e))


async def handle_equiv_find_similar_code(req_id: str, params: dict, send, state) -> None:
    """
    通过代码片段查找结构相似的已有实现（关键词提取 + 搜索）。

    params:
      code:  str   代码片段
      limit: int   最多返回结果数（默认 5）
    """
    code  = params.get("code", "")
    limit = int(params.get("limit", 5))
    try:
        bridge = state.get_bridge()

        # 从代码中提取有意义的标识符（长度 > 3）
        keywords = [w for w in re.split(r"[^a-zA-Z_]", code) if len(w) > 3][:5]

        all_matches: list[dict] = []
        for kw in keywords:
            all_matches.extend(await _search(bridge, kw))

        # 去重
        seen: set[str] = set()
        unique = []
        for m in all_matches:
            key = f"{m.get('file','')}:{m.get('line',0)}"
            if key not in seen:
                seen.add(key)
                unique.append(m)

        await send.response(req_id, {
            "similar":    unique[:limit],
            "match_type": "keyword_similarity",
            "note":       "Semantic similarity available via code_intel.hybrid_search",
        })
    except Exception as e:
        log.exception("equiv.find_similar_code failed")
        await send.error(req_id, -32603, str(e))


# ── verify.* handlers ────────────────────────────────────────────────────────

async def handle_verify_task(req_id: str, params: dict, send, state) -> None:
    """
    验证任务合约的完成度。

    原 Rust 版本：直接访问 ContractStore SQLite。
    新 Python 版本：通过 contracts.list + contracts.get_checkpoints bridge 工具获取数据，
                   在 Python 中计算进度（策略可进化）。

    params:
      contract_id: str   合约 ID（或前缀）
    """
    contract_id = params.get("contract_id", "")
    try:
        bridge         = state.get_bridge()
        contracts_list = await _get_contracts(bridge)

        contract = next(
            (c for c in contracts_list
             if c.get("id", "") == contract_id or c.get("id", "").startswith(contract_id)),
            None,
        )
        if not contract:
            await send.response(req_id, {
                "ok":    False,
                "error": f"Contract '{contract_id}' not found",
            })
            return

        checkpoints = await _get_checkpoints(bridge, contract["id"])
        done        = sum(1 for cp in checkpoints if cp.get("status") == "done")
        total       = len(checkpoints)
        pct         = int(done / total * 100) if total > 0 else 0

        status = "complete" if pct == 100 else "partial" if pct >= 50 else "in_progress"

        await send.response(req_id, {
            "contract_id":       contract["id"],
            "requirement":       contract.get("requirement", ""),
            "overall_pct":       pct,
            "status":            status,
            "checkpoints_done":  done,
            "checkpoints_total": total,
            "recommendation":    "ship" if pct == 100 else "continue",
        })
    except Exception as e:
        log.exception("verify.task failed")
        await send.error(req_id, -32603, str(e))


async def handle_verify_coverage(req_id: str, params: dict, send, state) -> None:
    """
    查询任务合约的检查点覆盖情况（已完成 vs 待完成）。

    params:
      contract_id: str   合约 ID（或前缀）
    """
    contract_id = params.get("contract_id", "")
    try:
        bridge         = state.get_bridge()
        contracts_list = await _get_contracts(bridge)

        contract = next(
            (c for c in contracts_list
             if c.get("id", "") == contract_id or c.get("id", "").startswith(contract_id)),
            None,
        )
        if not contract:
            await send.response(req_id, {"error": f"Contract '{contract_id}' not found"})
            return

        checkpoints = await _get_checkpoints(bridge, contract["id"])
        covered     = [cp["description"] for cp in checkpoints if cp.get("status") == "done"]
        uncovered   = [cp["description"] for cp in checkpoints if cp.get("status") != "done"]
        pct         = int(len(covered) / len(checkpoints) * 100) if checkpoints else 0

        await send.response(req_id, {
            "contract_id":  contract["id"],
            "coverage_pct": pct,
            "covered":      covered,
            "uncovered":    uncovered,
        })
    except Exception as e:
        log.exception("verify.coverage failed")
        await send.error(req_id, -32603, str(e))


async def handle_verify_drift(req_id: str, params: dict, send, state) -> None:
    """
    检测实现是否偏离了原始需求（需求漂移检测）。

    改进：
      原 Rust 版本：用关键词匹配变更文件名（极不准确）。
      新 Python 版本：优先用 LLM 语义分析，LLM 不可用时回退到关键词启发式。

    params:
      contract_id: str    合约 ID
      use_llm:     bool   是否使用 LLM 分析（默认 True）
    """
    contract_id = params.get("contract_id", "")
    use_llm     = params.get("use_llm", True)
    try:
        bridge = state.get_bridge()

        # 获取最近 git diff
        diff_result = await bridge.call("git.diff", {})
        diff_text   = diff_result if isinstance(diff_result, str) else ""

        # 获取合约需求
        contracts_list = await _get_contracts(bridge)
        contract = next(
            (c for c in contracts_list
             if c.get("id", "") == contract_id or c.get("id", "").startswith(contract_id)),
            None,
        )
        if not contract:
            await send.response(req_id, {"error": f"Contract '{contract_id}' not found", "drifts": []})
            return

        requirement   = contract.get("requirement", "")
        changed_files = [
            line[6:] for line in diff_text.splitlines()
            if line.startswith("+++ b/")
        ][:20]

        drift_result: dict[str, Any] = {
            "contract_id":    contract["id"],
            "requirement":    requirement[:300],
            "changed_files":  len(changed_files),
            "drifts":         [],
            "drift_detected": False,
            "method":         "pending",
        }

        # 优先使用 LLM 进行语义分析
        if use_llm and diff_text:
            try:
                llm = state.get_llm_client()
                if llm:
                    import json as _json
                    prompt = (
                        f"Contract requirement:\n{requirement}\n\n"
                        f"Recent code changes (first 2000 chars):\n{diff_text[:2000]}\n\n"
                        "Analyze: Do these changes align with the requirement? "
                        "Or do they introduce scope creep / implementation drift?\n"
                        'Answer ONLY with valid JSON: {"drift_detected": bool, '
                        '"reason": "one sentence", "confidence": "high"|"medium"|"low"}'
                    )
                    response = await llm.complete_for_task("lint", prompt)
                    m = re.search(r"\{[^{}]+\}", response, re.DOTALL)
                    if m:
                        analysis = _json.loads(m.group())
                        drift_result.update({
                            "drift_detected": bool(analysis.get("drift_detected", False)),
                            "drifts":         [analysis["reason"]] if analysis.get("drift_detected") else [],
                            "confidence":     analysis.get("confidence", "medium"),
                            "method":         "llm",
                        })
            except Exception as e:
                log.debug("LLM drift analysis failed, falling back to heuristic: %s", e)

        # 回退到关键词启发式
        if drift_result["method"] == "pending":
            keywords = [w for w in requirement.split() if len(w) > 4]
            scope_creep = [
                f for f in changed_files
                if not any(kw.lower() in f.lower() for kw in keywords)
            ]
            drift_result.update({
                "drift_detected": bool(scope_creep),
                "drifts":         scope_creep[:5],
                "method":         "heuristic_keyword",
                "recommendation": "manual_review",
            })

        await send.response(req_id, drift_result)
    except Exception as e:
        log.exception("verify.drift failed")
        await send.error(req_id, -32603, str(e))


# ── symbol.* handlers ────────────────────────────────────────────────────────

async def handle_symbol_usages(req_id: str, params: dict, send, state) -> None:
    """
    查找符号在代码库中的所有用法（调用点）。

    params:
      symbol_id: str   符号名
      limit:     int   最多返回结果数（默认 20）
    """
    sym   = params.get("symbol_id", "")
    limit = int(params.get("limit", 20))
    try:
        bridge  = state.get_bridge()
        matches = await _search(bridge, sym)
        usages  = [
            {"file": m["file"], "line": m["line"], "snippet": m.get("content", "")}
            for m in matches[:limit]
        ]
        await send.response(req_id, {
            "usages": usages,
            "total":  len(matches),
        })
    except Exception as e:
        log.exception("symbol.usages failed")
        await send.error(req_id, -32603, str(e))


async def handle_symbol_lifecycle(req_id: str, params: dict, send, state) -> None:
    """
    查询符号的 Git 版本历史（何时引入、最近修改等）。

    params:
      symbol: str   符号名
    """
    sym = params.get("symbol", "")
    try:
        bridge = state.get_bridge()
        # Sanitize sym to prevent shell injection via git log -S argument.
        # Only allow valid code identifier characters (letters, digits, underscore, dot, hyphen).
        import re as _re
        sym_safe = _re.sub(r'[^\w.\-]', '', sym)
        if not sym_safe:
            await send.response(req_id, {"symbol": sym, "git_history": [], "deprecated": False})
            return
        result = await bridge.call("shell.run", {
            "cmd":       f"git log --oneline -S \"{sym_safe}\" -5",
            "cwd":       ".",
            "timeout_s": 15,
        })
        log_text = result.get("stdout", "") if isinstance(result, dict) else str(result)
        history  = [line for line in log_text.strip().splitlines() if line]

        await send.response(req_id, {
            "symbol":      sym,
            "git_history": history[:5],
            "deprecated":  False,  # 使用 assume.is_deprecated 做专项检查
        })
    except Exception as e:
        log.exception("symbol.lifecycle failed")
        await send.error(req_id, -32603, str(e))


# ── code_intel.ranked_context ─────────────────────────────────────────────────

# PageRank 权重配置（Python 层 — 可通过 AI 对话调整）
_RANKED_CONTEXT_WEIGHTS: dict[str, Any] = {
    "modified_file_base":  50.0,  # 修改文件中的符号基础权重（Aider PageRank 算法）
    "mentioned_boost":     10.0,  # 被用户或 AI 明确提及的符号权重
    "long_name_boost":      2.0,  # 长命名（超过 min_name_length 字符）的提升
    "private_penalty":      0.1,  # 私有符号（_ 前缀）的降权
    "min_name_length":        8,  # 触发 long_name_boost 的最小名称长度
}


async def handle_ranked_context(req_id: str, params: dict, send, state) -> None:
    """
    基于 Aider PageRank 算法的相关符号排名。

    改进（相对原 Rust 版本）：
      - 评分权重在 Python 层（_RANKED_CONTEXT_WEIGHTS），可通过对话调整
      - 与 repo_map.py 使用相同的 Aider 算法原则（一致性）
      - 在响应中返回所用权重，保证透明性

    params:
      modified_file: str        当前修改的文件路径
      mentioned:     list[str]  用户或 AI 提及的符号名列表
      limit:         int        最多返回符号数（默认 20）
    """
    modified_file  = params.get("modified_file", ".")
    mentioned_list = params.get("mentioned", [])
    limit          = int(params.get("limit", 20))
    try:
        bridge        = state.get_bridge()
        w             = _RANKED_CONTEXT_WEIGHTS
        mentioned_set = set(mentioned_list)

        # 获取修改文件中的符号（最高相关度）
        file_symbols_raw = await bridge.call("code_intel.list_symbols", {"file": modified_file})
        file_symbols     = file_symbols_raw if isinstance(file_symbols_raw, list) else []

        scored: list[tuple[float, dict]] = []

        for sym in file_symbols:
            score = float(w["modified_file_base"])
            if sym.get("name", "") in mentioned_set:
                score *= w["mentioned_boost"]
            if len(sym.get("name", "")) > w["min_name_length"]:
                score *= w["long_name_boost"]
            if sym.get("name", "").startswith("_"):
                score *= w["private_penalty"]
            scored.append((score, sym))

        # 被提及符号（来自其他文件）
        for name in mentioned_set:
            syms_raw = await bridge.call("code_intel.find_symbol", {"query": name})
            syms     = syms_raw if isinstance(syms_raw, list) else []
            for sym in syms:
                scored.append((float(w["mentioned_boost"]), sym))

        # 排序 + 去重
        scored.sort(key=lambda x: x[0], reverse=True)
        seen: set[str] = set()
        ranked = []
        for score, sym in scored:
            key = f"{sym.get('file', '')}:{sym.get('name', '')}"
            if key not in seen:
                seen.add(key)
                ranked.append({
                    "name":  sym.get("name", ""),
                    "kind":  sym.get("kind", ""),
                    "file":  sym.get("file", ""),
                    "line":  sym.get("line", 0),
                    "score": round(score, 2),
                })

        await send.response(req_id, {
            "modified_file": modified_file,
            "ranked":        ranked[:limit],
            "algorithm":     "PageRank-inspired (Aider repomap, Python layer)",
            "weights":       w,   # 透明度：调用方可以看到使用的权重
        })
    except Exception as e:
        log.exception("code_intel.ranked_context failed")
        await send.error(req_id, -32603, str(e))

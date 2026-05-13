"""System handlers — 配置、Context 构建、Evolution 观察、代码搜索。"""
from __future__ import annotations
import logging
import os
from pathlib import Path

log = logging.getLogger("evocli.handlers.system")


def register(router) -> None:
    router.add("config.get",          handle_config_get)
    router.add("config.get_debug",    handle_config_get_debug)
    router.add("context.build",       handle_context_build)
    router.add("evolution.observe",   handle_evolution_observe)
    router.add("code_intel.reindex",  handle_code_intel_reindex)
    router.add("code_intel.analyze",  handle_code_intel_analyze)
    router.add("code_intel.ingest_tree_sitter", handle_ingest_tree_sitter)
    router.add("code.ingest_chunks",  handle_code_ingest_chunks)
    router.add("code.search_semantic", handle_code_search_semantic)
    router.add("code.generate_community_summaries", handle_code_generate_community_summaries)
    router.add("search.code_context", handle_search_code_context)
    # Soul 自更新协议 (Section 9.8)
    router.add("soul.propose_update", handle_soul_propose_update)
    router.add("soul.approve",        handle_soul_approve)
    router.add("soul.reject",         handle_soul_reject)
    router.add("soul.version",        handle_soul_version)


async def handle_config_get(req_id: str, params: dict, send, _state) -> None:
    config_path = Path.home() / ".evocli" / "config.toml"
    cfg: dict   = {}
    if config_path.exists():
        try:
            import tomllib
            with open(config_path, "rb") as f:
                cfg = tomllib.load(f)
        except Exception:
            pass
    # Apply env var overrides for LLM section
    llm = cfg.setdefault("llm", {})
    if os.environ.get("OPENAI_API_KEY"):
        llm.setdefault("provider", "openai")
    if os.environ.get("ANTHROPIC_API_KEY"):
        llm.setdefault("provider", "anthropic")
    
    # 自动检测并注入 context window 信息（方便 agent 了解真实限制）
    model = llm.get("tiers", {}).get("fast", "")
    if model:
        try:
            from evocli_soul.model_context import get_model_context
            ctx_info = get_model_context(
                model,
                base_url=llm.get("base_url"),
                api_key=llm.get("api_key") or os.environ.get("OPENAI_API_KEY"),
            )
            cfg.setdefault("_detected", {})["context"] = ctx_info
        except Exception:
            pass
    
    await send.response(req_id, cfg)


async def handle_config_get_debug(req_id: str, params: dict, send, _state) -> None:
    debug = os.environ.get("EVOCLI_DEBUG", "").lower() in ("1", "true", "yes")
    await send.response(req_id, {"debug": debug})


async def handle_context_build(req_id: str, params: dict, send, state) -> None:
    try:
        from evocli_soul.context_engine import ContextEngine
        engine = ContextEngine(state.get_bridge())
        result = await engine.build(params)
        await send.response(req_id, result)
    except Exception as e:
        log.exception("context.build failed")
        await send.error(req_id, -32603, str(e))


async def handle_evolution_observe(req_id: str, params: dict, send, state) -> None:
    try:
        from evocli_soul.evolution import EvolutionEngine
        engine = EvolutionEngine(state.get_bridge())
        result = await engine.observe(params)
        await send.response(req_id, result)
    except Exception as e:
        log.exception("evolution.observe failed")
        await send.error(req_id, -32603, str(e))


async def handle_code_intel_reindex(req_id: str, params: dict, send, state) -> None:
    """
    code_intel.reindex — 通过 tree-sitter + bridge 触发代码索引更新。

    M4 FIX: 移除 subprocess.run() 违规调用，改为：
    1. 使用 tree-sitter 直接分析变更文件（Python Soul 侧）
    2. 通过 bridge.call 写入 Rust SQLite 索引
    符合 AGENTS.md: 所有操作必须通过 bridge，禁止 subprocess/os.system。
    """
    params.get("project", ".")
    files   = params.get("files", [])

    if not files:
        # 没有指定文件时，通知 Rust 侧异步处理（通过 Job Queue 已入队）
        await send.response(req_id, {
            "ok":  True,
            "note": "Reindex scheduled via job queue — run `evocli index` for immediate reindex",
        })
        return

    try:
        # 对每个变更文件使用 tree-sitter 分析后写入 Rust 索引
        from evocli_soul.tree_sitter_analyzer import analyze_file
        results = []
        bridge  = state.get_bridge()
        # Keep analyzed symbols for LanceDB semantic embedding (used by _ingest_chunks_bg below)
        _analyzed_symbols: list[dict] = []

        # Use caller-supplied limit or default to 100. Was hardcoded to 20 which silently
        # dropped files in medium-sized changesets (e.g., a refactor touching 30+ files).
        max_files = params.get("max_files", 100)
        for file_path in files[:max_files]:
            try:
                from pathlib import Path as _Path
                content = _Path(file_path).read_text(encoding="utf-8", errors="ignore")
                analysis = analyze_file(file_path, content)
                syms = analysis.get("symbols", [])
                # 通过 bridge 写入 Rust SQLite（不使用 subprocess）
                await bridge.call("code_intel.ingest_tree_sitter", {
                    "file":    file_path,
                    "symbols": syms,
                })
                results.append({"file": file_path, "ok": True, "symbols": len(syms)})
                _analyzed_symbols.extend(syms)  # collect for LanceDB embedding
            except Exception as e:
                log.debug("Reindex failed for %s: %s", file_path, e)
                results.append({"file": file_path, "ok": False, "error": str(e)})

        await send.response(req_id, {
            "ok":     True,
            "files":  len(results),
            "results": results,
        })

        # After reindexing, trigger semantic code chunk ingestion asynchronously.
        # This updates the LanceDB code_chunks table so code_semantic_search works.
        # Non-blocking: ingestion runs in background and does not delay the response.
        import asyncio as _asyncio
        async def _ingest_chunks_bg():
            try:
                from evocli_soul.code_chunks import get_index as _get_ci
                if not _analyzed_symbols:
                    log.debug("code_chunks: no symbols to embed — skipping background ingest")
                    return
                idx = _get_ci(params.get("project", "."))
                ingest_result = await idx.ingest_symbols(_analyzed_symbols)
                log.debug(
                    "code_chunks: background ingest done — ingested=%d skipped=%d errors=%d",
                    ingest_result.get("ingested", 0),
                    ingest_result.get("skipped", 0),
                    ingest_result.get("errors", 0),
                )
            except Exception as _e:
                log.debug("code_chunks: background ingest failed (non-fatal): %s", _e)
        _asyncio.create_task(_ingest_chunks_bg())
    except Exception as e:
        log.exception("code_intel.reindex failed")
        await send.error(req_id, -32603, str(e))

async def handle_code_intel_analyze(req_id: str, params: dict, send, _state) -> None:
    """code_intel.analyze — 使用 tree-sitter 精确分析文件符号（Section 16 Layer 1）。"""
    file_path = params.get("file", "")
    content   = params.get("content", "")
    if not file_path:
        await send.error(req_id, -32600, "file path is required")
        return
    if not content:
        # 尝试读取文件
        try:
            from pathlib import Path
            content = Path(file_path).read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            await send.error(req_id, -32603, str(e))
            return
    try:
        from evocli_soul.tree_sitter_analyzer import analyze_file
        result = analyze_file(file_path, content)
        await send.response(req_id, result)
    except Exception as e:
        await send.error(req_id, -32603, str(e))

async def handle_ingest_tree_sitter(req_id: str, params: dict, send, state) -> None:
    """
    code_intel.ingest_tree_sitter — 接收 Python tree-sitter 分析结果，
    通过 bridge 写入 Rust SQLite code_index.db（Section 16 Layer 1 集成）。
    """
    file_path = params.get("file", "")
    content   = params.get("content", "")

    if not file_path:
        await send.error(req_id, -32600, "file path is required")
        return

    if not content:
        try:
            from pathlib import Path
            content = Path(file_path).read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            await send.error(req_id, -32603, str(e))
            return

    try:
        # 1. Python 端：tree-sitter 分析
        from evocli_soul.tree_sitter_analyzer import analyze_file
        analysis = analyze_file(file_path, content)

        # 2. Rust 端：写入 SQLite via bridge
        result = await state.get_bridge().call("code_intel.ingest_tree_sitter", {
            "file":    file_path,
            "symbols": analysis["symbols"],
        })

        await send.response(req_id, {
            "ok":      True,
            "file":    file_path,
            "symbols": len(analysis["symbols"]),
            "engine":  analysis["engine"],
            "rust":    result,
        })
    except Exception as e:
        # Fallback: return tree-sitter results without Rust persistence
        import logging
        logging.getLogger("evocli.system").debug("Rust ingestion failed: %s", e)
        await send.response(req_id, {
            "ok":      True,
            "file":    file_path,
            "symbols": len(analysis.get("symbols", [])) if "analysis" in dir() else 0,
            "engine":  "tree-sitter",
            "note":    "Rust SQLite ingestion deferred",
        })

async def handle_search_code_context(req_id: str, params: dict, send, _state) -> None:
    """
    WIRE-1: search.code_context — grep-ast 上下文感知代码搜索。
    
    比 search.code 更强大：每个匹配结果包含 AST 上下文（所在函数/类名），
    让 LLM 更准确理解代码位置。
    
    需要 pip install "evocli-soul[code]"（grep-ast + tree-sitter-languages）
    """
    pattern  = params.get("pattern", params.get("query", ""))
    path     = params.get("path", ".")
    max_res  = int(params.get("max_results", 50))

    if not pattern:
        await send.error(req_id, -32600, "pattern is required")
        return
    try:
        from evocli_soul.code_search import search_with_context, _GREP_AST_AVAILABLE
        results = search_with_context(pattern, path, max_results=max_res)
        await send.response(req_id, {
            "results":   results,
            "count":     len(results),
            "engine":    "grep-ast" if _GREP_AST_AVAILABLE else "plain-search",
            "pattern":   pattern,
        })
    except Exception as e:
        log.exception("search.code_context failed")
        await send.error(req_id, -32603, str(e))


# ── Soul 自更新协议 (Section 9.8) ─────────────────────────────────────────────

async def handle_soul_propose_update(req_id: str, params: dict, send, state) -> None:
    """
    提议 Soul 更新（Step 1-3：检测 → 生成提案 → 安全审查）。
    通常由 Evolution Engine 调用，也可手动触发。

    params:
      module:               str   要修改的模块（如 "context_engine.py"）
      diff:                 str   unified diff 内容
      reason:               str   变更原因
      risk_level:           str   LOW/MEDIUM/HIGH（默认 LOW）
      expected_improvement: str   预期改进说明
    """
    module     = params.get("module", "")
    diff       = params.get("diff", "")
    reason     = params.get("reason", "")
    risk_level = params.get("risk_level", "LOW")
    expected   = params.get("expected_improvement", "")
    if not module or not diff:
        await send.error(req_id, -32600, "module and diff are required")
        return
    try:
        from evocli_soul.soul_updater import get_soul_updater
        updater = get_soul_updater(state.get_bridge())
        result  = updater.propose_update(module, diff, reason, risk_level, expected)
        await send.response(req_id, result)
    except Exception as e:
        log.exception("soul.propose_update failed")
        await send.error(req_id, -32603, str(e))


async def handle_soul_approve(req_id: str, params: dict, send, state) -> None:
    """
    用户批准 Soul 更新（Step 5：应用补丁 → 验证 → 版本记录）。

    params:
      proposal_id: str   提案 ID（来自 soul.propose_update 的返回值）
    """
    proposal_id = params.get("proposal_id", "")
    if not proposal_id:
        await send.error(req_id, -32600, "proposal_id is required")
        return
    try:
        from evocli_soul.soul_updater import get_soul_updater
        updater = get_soul_updater(state.get_bridge())
        result  = await updater.approve_and_apply(proposal_id)
        await send.response(req_id, result)
    except Exception as e:
        log.exception("soul.approve failed")
        await send.error(req_id, -32603, str(e))


async def handle_soul_reject(req_id: str, params: dict, send, _state) -> None:
    """用户拒绝 Soul 更新。"""
    proposal_id = params.get("proposal_id", "")
    reason      = params.get("reason", "")
    try:
        from evocli_soul.soul_updater import get_soul_updater
        updater = get_soul_updater()
        result  = updater.reject(proposal_id, reason)
        await send.response(req_id, result)
    except Exception as e:
        await send.error(req_id, -32603, str(e))


async def handle_soul_version(req_id: str, params: dict, send, _state) -> None:
    """查询 Soul 版本信息和更新历史。"""
    try:
        from evocli_soul.soul_updater import get_soul_updater
        updater = get_soul_updater()
        result  = updater.get_version_info()
        await send.response(req_id, result)
    except Exception as e:
        await send.error(req_id, -32603, str(e))


async def handle_code_ingest_chunks(req_id: str, params: dict, send, state) -> None:
    """
    code.ingest_chunks — 将代码符号的函数体嵌入向量，写入 LanceDB code_chunks 表。

    这是 GraphRAG 语义层的入口：调用后，code_semantic_search 才能工作。
    通常在 evocli index 完成后自动触发，也可手动调用。

    params:
      symbols:    list of {id, name, kind, file, line, line_end, signature, language}
                  (如为空，从 bridge code_intel.list_symbols 自动获取)
      project_id: string (default ".")
      force:      bool — re-embed even if unchanged (default false)
    """
    try:
        from evocli_soul.code_chunks import get_index as _get_chunk_idx

        project_id = params.get("project_id", ".")
        force      = params.get("force", False)
        symbols    = params.get("symbols", [])

        # If no symbols provided, fetch all from Rust index
        if not symbols:
            try:
                bridge = state.get_bridge()
                result = await bridge.call("code_intel.list_symbols", {"file": ""})
                if isinstance(result, dict):
                    symbols = result.get("symbols", [])
                elif isinstance(result, list):
                    symbols = result
            except Exception as e:
                log.warning("code.ingest_chunks: could not fetch symbols from index: %s", e)

        idx = _get_chunk_idx(project_id)
        stats = await idx.ingest_symbols(symbols, project_id=project_id, force=force)
        await send.response(req_id, {
            "ok":       True,
            "ingested": stats.get("ingested", 0),
            "skipped":  stats.get("skipped", 0),
            "errors":   stats.get("errors", 0),
            "project":  project_id,
        })
    except Exception as e:
        log.exception("code.ingest_chunks failed")
        await send.error(req_id, -32603, str(e))


async def handle_code_search_semantic(req_id: str, params: dict, send, state) -> None:
    """
    code.search_semantic — 语义代码搜索（向量相似度）。

    使用自然语言描述查找相关函数/类，返回匹配的代码体。
    比 shell_grep 更智能：能找到语义相关的代码，即使关键词不匹配。

    params:
      query:       natural language query (required)
      top_k:       max results (default 5)
      language:    filter by language
      kind:        filter by kind (function/class)
      file_filter: filter by file path substring
      project_id:  string (default ".")
    """
    try:
        from evocli_soul.code_chunks import get_index as _get_chunk_idx

        query      = params.get("query", "")
        top_k      = int(params.get("top_k", 5))
        language   = params.get("language", "")
        kind       = params.get("kind", "")
        file_filter = params.get("file_filter", "")
        project_id = params.get("project_id", ".")

        if not query:
            await send.error(req_id, -32600, "query is required")
            return

        idx     = _get_chunk_idx(project_id)
        results = idx.search(query, top_k=top_k, language=language, kind=kind,
                             file_filter=file_filter, project_id=project_id)

        await send.response(req_id, {
            "query":   query,
            "count":   len(results),
            "results": results,
        })
    except Exception as e:
        log.exception("code.search_semantic failed")
        await send.error(req_id, -32603, str(e))


async def handle_code_generate_community_summaries(req_id: str, params: dict, send, state) -> None:
    """
    code.generate_community_summaries — GraphRAG 核心：为代码社区生成 LLM 摘要。

    调用流程：
      1. 从 code_intel.communities 获取社区列表
      2. 为每个社区的符号从 code_chunks 获取代码体
      3. 调用 LLM 生成自然语言摘要
      4. 存入 LanceDB memory（可通过语义搜索召回）

    完成后，全局问题（"认证系统怎么工作"）可直接检索到摘要。

    params:
      project_id:          string (default ".")
      max_communities:     int (default 20)
      communities:         list — 如果提供则直接使用，否则从 bridge 获取
    """
    try:
        from evocli_soul.code_chunks import get_index as _get_chunk_idx
        from evocli_soul.llm_client import LLMClient

        project_id        = params.get("project_id", ".")
        max_communities   = int(params.get("max_communities", 20))
        communities       = params.get("communities", [])

        # Fetch communities from Rust if not provided
        if not communities:
            try:
                bridge = state.get_bridge()
                result = await bridge.call("code_intel.communities", {})
                if isinstance(result, dict):
                    communities = result.get("communities", [])
                elif isinstance(result, list):
                    communities = result
            except Exception as e:
                log.warning("code.generate_community_summaries: could not fetch communities: %s", e)

        if not communities:
            await send.response(req_id, {
                "ok": False,
                "error": "No communities found. Run 'evocli index' first.",
            })
            return

        cfg       = state.get_config()
        llm       = LLMClient(cfg)
        idx       = _get_chunk_idx(project_id)
        summaries = await idx.generate_community_summaries(
            communities,
            llm,
            project_id=project_id,
            max_communities=max_communities,
        )

        await send.response(req_id, {
            "ok":             True,
            "summaries_count": len(summaries),
            "summaries":      summaries,
            "project":        project_id,
        })
    except Exception as e:
        log.exception("code.generate_community_summaries failed")
        await send.error(req_id, -32603, str(e))

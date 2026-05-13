"""
Knowledge graph handlers — GitNexus-inspired built-in code intelligence

RPC 方法 (对应 GitNexus MCP tools):
  code_intel.bm25_search    → GitNexus query (BM25 部分)
  code_intel.hybrid_search  → GitNexus query (BM25 + vector + RRF)
  code_intel.blast_radius   → GitNexus impact tool
  code_intel.symbol_context → GitNexus context tool (360° view)
  code_intel.communities    → GitNexus communities resource
  code_intel.processes      → GitNexus processes resource
  wiki.generate             → GitNexus analyze --skills + AGENTS.md
"""
from __future__ import annotations
import logging

log = logging.getLogger("evocli.handlers.knowledge")


def register(router) -> None:
    router.add("code_intel.bm25_search",    handle_bm25_search)
    router.add("code_intel.hybrid_search",  handle_hybrid_search)
    router.add("code_intel.blast_radius",   handle_blast_radius)
    router.add("code_intel.symbol_context", handle_symbol_context)
    router.add("code_intel.communities",    handle_communities)
    router.add("code_intel.processes",      handle_processes)
    router.add("wiki.generate",             handle_wiki_generate)


async def handle_bm25_search(req_id: str, params: dict, send, state) -> None:
    """
    BM25 full-text code search using tantivy (GitNexus query tool — BM25 部分).
    
    params:
      query: str   Search query
      limit: int   Max results (default 20)
    """
    query = params.get("query", "")
    limit = int(params.get("limit", 20))
    if not query:
        await send.error(req_id, -32600, "query is required")
        return
    try:
        bridge = state.get_bridge()
        result = await bridge.call("code_intel.bm25_search", {"query": query, "limit": limit})
        await send.response(req_id, result)
    except Exception as e:
        log.exception("code_intel.bm25_search failed")
        await send.error(req_id, -32603, str(e))


async def handle_hybrid_search(req_id: str, params: dict, send, state) -> None:
    """
    Hybrid BM25 + vector search with RRF merging (GitNexus query tool).
    
    GitNexus 对应: hybrid BM25 + semantic + RRF (K=60)
    EvoCLI: BM25 from Rust tantivy + vector from Python LanceDB + RRF merge
    
    params:
      query:  str   Search query
      limit:  int   Max results (default 10)
      top_k:  int   Per-ranker top K before merge (default 20)
    """
    query  = params.get("query", "")
    limit  = int(params.get("limit", 10))
    top_k  = int(params.get("top_k", 20))
    if not query:
        await send.error(req_id, -32600, "query is required")
        return
    try:
        bridge = state.get_bridge()

        # 1. BM25 results from Rust tantivy
        bm25_result = await bridge.call("code_intel.bm25_search", {"query": query, "limit": top_k})
        bm25_hits = bm25_result.get("results", []) if isinstance(bm25_result, dict) else []

        # 2. Vector results from Python LanceDB
        memory = state.get_memory()
        vec_hits = []
        try:
            vec_raw = memory.search(query, top_k=top_k)
            for i, item in enumerate(vec_raw[:top_k]):
                vec_hits.append({
                    "symbol_id": item.get("id", item.get("title", "")),
                    "name":      item.get("title", ""),
                    "file":      item.get("body", "")[:50],
                    "score":     1.0 - (i / top_k),
                    "rank":      i + 1,
                })
        except Exception as e:
            log.warning("Vector search failed (non-fatal, BM25-only fallback): %s", e)

        # 3. RRF merge (K=60)
        K = 60.0
        scores: dict[str, float] = {}
        meta: dict[str, dict] = {}

        for r in bm25_hits:
            sid = r.get("symbol_id", "")
            scores[sid] = scores.get(sid, 0) + 1.0 / (K + r.get("rank", 99))
            meta[sid] = {"name": r.get("name",""), "kind": r.get("kind",""), "file": r.get("file",""), "bm25_rank": r.get("rank")}

        for r in vec_hits:
            sid = r.get("symbol_id", "")
            scores[sid] = scores.get(sid, 0) + 1.0 / (K + r.get("rank", 99))
            if sid not in meta:
                meta[sid] = {"name": r.get("name",""), "file": r.get("file",""), "vec_rank": r.get("rank")}
            else:
                meta[sid]["vec_rank"] = r.get("rank")

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:limit]
        results = [{"symbol_id": sid, "rrf_score": sc, **meta.get(sid, {})} for sid, sc in ranked]

        await send.response(req_id, {"query": query, "results": results, "count": len(results)})
    except Exception as e:
        log.exception("code_intel.hybrid_search failed")
        await send.error(req_id, -32603, str(e))


async def handle_blast_radius(req_id: str, params: dict, send, state) -> None:
    """
    Blast radius / impact analysis (GitNexus impact tool).
    
    Returns upstream callers + downstream callees with risk assessment.
    
    params:
      symbol_id: str   Symbol to analyze
      max_depth: int   BFS depth (default 5)
    """
    symbol_id = params.get("symbol_id", "")
    max_depth = int(params.get("max_depth", 5))
    if not symbol_id:
        await send.error(req_id, -32600, "symbol_id is required")
        return
    try:
        bridge = state.get_bridge()
        result = await bridge.call("code_intel.blast_radius", {"symbol_id": symbol_id, "max_depth": max_depth})
        await send.response(req_id, result)
    except Exception as e:
        log.exception("code_intel.blast_radius failed")
        await send.error(req_id, -32603, str(e))


async def handle_symbol_context(req_id: str, params: dict, send, state) -> None:
    """
    360° symbol context (GitNexus context tool).
    
    Returns callers + callees + community + process membership.
    
    params:
      symbol_id: str   Symbol to inspect
    """
    symbol_id = params.get("symbol_id", "")
    if not symbol_id:
        await send.error(req_id, -32600, "symbol_id is required")
        return
    try:
        bridge = state.get_bridge()
        result = await bridge.call("code_intel.symbol_context", {"symbol_id": symbol_id})
        await send.response(req_id, result)
    except Exception as e:
        log.exception("code_intel.symbol_context failed")
        await send.error(req_id, -32603, str(e))


async def handle_communities(req_id: str, params: dict, send, state) -> None:
    """
    List functional communities (GitNexus communities resource).
    """
    try:
        bridge = state.get_bridge()
        result = await bridge.call("code_intel.communities", {})
        await send.response(req_id, result)
    except Exception as e:
        log.exception("code_intel.communities failed")
        await send.error(req_id, -32603, str(e))


async def handle_processes(req_id: str, params: dict, send, state) -> None:
    """
    List execution flows (GitNexus processes resource).
    """
    try:
        bridge = state.get_bridge()
        result = await bridge.call("code_intel.processes", {})
        await send.response(req_id, result)
    except Exception as e:
        log.exception("code_intel.processes failed")
        await send.error(req_id, -32603, str(e))


async def handle_wiki_generate(req_id: str, params: dict, send, state) -> None:
    """
    Generate AGENTS.md / wiki from knowledge graph (GitNexus wiki command).
    
    Fetches graph data from Rust, uses LLM to generate human-readable docs.
    
    params:
      project_path: str   Path to project (default ".")
      output:       str   "agents_md" | "wiki" | "skills" (default "agents_md")
    """
    project_path = params.get("project_path", ".")
    output_type  = params.get("output", "agents_md")
    try:
        bridge = state.get_bridge()
        llm    = state.get_llm_client()

        # Fetch graph summary from Rust
        stats       = await bridge.call("code_intel.index_status", {})
        communities = await bridge.call("code_intel.communities", {})
        processes   = await bridge.call("code_intel.processes", {})

        graph_data = {
            "stats":       stats       if isinstance(stats, dict) else {},
            "communities": communities.get("communities", []) if isinstance(communities, dict) else [],
            "processes":   processes.get("processes", [])   if isinstance(processes, dict) else [],
        }

        from evocli_soul.wiki_generator import generate_agents_md, generate_skill_per_community

        if output_type == "agents_md":
            content = await generate_agents_md(graph_data, project_path, llm)
            # Write to AGENTS.md
            agents_path = f"{project_path}/AGENTS.md"
            try:
                await bridge.call("fs.write", {"path": agents_path, "content": content})
                await send.response(req_id, {"ok": True, "path": agents_path, "chars": len(content)})
            except Exception as e:
                # Return content even if write fails
                await send.response(req_id, {"ok": False, "content": content, "error": str(e)})

        elif output_type == "skills":
            skills = []
            for comm in graph_data["communities"][:5]:
                skill = await generate_skill_per_community(comm, project_path, llm)
                if skill:
                    skills.append({"community": comm.get("label",""), "content": skill})
            await send.response(req_id, {"ok": True, "skills": skills})

        else:
            await send.response(req_id, {"ok": False, "error": f"Unknown output type: {output_type}"})

    except Exception as e:
        log.exception("wiki.generate failed")
        await send.error(req_id, -32603, str(e))

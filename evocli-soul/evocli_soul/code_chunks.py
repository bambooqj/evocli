"""
code_chunks.py — Semantic Code Chunk Index

将函数体/类体向量化存入 LanceDB，实现语义代码搜索。

这是 EvoCLI 走向 GraphRAG 的关键一步：
  之前：LanceDB 只存 AI 生成的文字记忆
  现在：LanceDB 同时存代码语义块 → 可以语义搜索代码内容

架构：
  Rust code_intel (symbols table)
    → symbol.name + symbol.file + symbol.line_start + symbol.line_end
    ↓
  code_chunks.py extract_body()
    → 读文件，按行范围提取函数体
    ↓
  jina-embeddings-v2-base-zh embed()
    → 768维向量
    ↓
  LanceDB code_chunks 表
    → 支持向量相似度搜索

与 GraphRAG 的关联：
  knowledge_graph 的节点（符号）将拥有语义内容（向量）
  blast_radius 找到的相关符号可以直接召回其代码内容
  = 结构图 + 语义检索 = 完整 GraphRAG
"""
from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path

log = logging.getLogger("evocli.code_chunks")

# LanceDB collection name (separate from memories to avoid schema conflict)
COLLECTION = "code_chunks"
# Max function body lines to embed
MAX_BODY_LINES = 150
# Min body lines — 1 allows single-line functions
MIN_BODY_LINES = 1
# Embedding dimension (jina-v2-base-code = 768-dim)
EMBED_DIM = 768


class CodeChunkIndex:
    """
    语义代码块索引。

    将代码符号（函数/类）的实际内容向量化，支持：
    1. ingest()     — 从 bridge 获取符号列表，提取函数体，嵌入存储
    2. search()     — 按自然语言查询检索最相关的代码块
    3. get_body()   — 按符号ID获取函数体文本
    4. update_file()— 文件变化时增量更新
    """

    def __init__(self, project_id: str = "."):
        self.project_id = project_id
        self._db = None
        self._tbl = None
        self._embedder = None

    # ── 内部工具 ──────────────────────────────────────────────────────────

    def _get_embedder(self):
        """使用中央 embedder 配置的代码专用模型（jina-v2-base-code）。"""
        if self._embedder is not None:
            return self._embedder
        from evocli_soul.embedder import get_code_embedder
        self._embedder = get_code_embedder()
        return self._embedder

    def _embed(self, text: str) -> list[float] | None:
        from evocli_soul.embedder import embed_code
        return embed_code(text)

    def _get_table(self):
        """Get or create LanceDB code_chunks table."""
        if self._tbl is not None:
            return self._tbl
        try:
            import lancedb
            db_path = str(Path.home() / ".evocli" / "vectors")
            self._db = lancedb.connect(db_path)

            schema = {
                "id":         "",
                "symbol":     "",
                "file":       "",
                "language":   "",
                "kind":       "",
                "body":       "",
                "signature":  "",
                "project_id": "",
                "line_start": 0,
                "line_end":   0,
                "body_hash":  "",
                "indexed_at": 0.0,
                "vector":     [0.0] * EMBED_DIM,  # jina-v2-base-code = 768-dim
            }

            if COLLECTION in self._db.table_names():
                self._tbl = self._db.open_table(COLLECTION)
            else:
                # Create table with first dummy row to establish schema
                dummy = {**schema}
                dummy["id"] = "__init__"
                self._tbl = self._db.create_table(
                    COLLECTION,
                    data=[dummy],
                    mode="overwrite",
                )
                # Remove dummy
                self._tbl.delete("id = '__init__'")

            return self._tbl
        except Exception as e:
            log.debug("code_chunks: table init failed: %s", e)
            return None

    @staticmethod
    def _body_hash(body: str) -> str:
        return hashlib.md5(body.encode(), usedforsecurity=False).hexdigest()[:16]

    # ── 函数体提取 ────────────────────────────────────────────────────────

    @staticmethod
    def extract_body(
        file_path: str,
        line_start: int,
        line_end: int | None = None,
        max_lines: int = MAX_BODY_LINES,
    ) -> str | None:
        """
        从文件中提取函数/类体。

        line_start: 1-indexed definition line (from symbol index)
        line_end:   1-indexed end line (None = estimate from indentation)
        """
        try:
            path = Path(file_path)
            if not path.exists():
                return None
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            if line_start < 1 or line_start > len(lines):
                return None

            start_idx = line_start - 1  # 0-indexed

            if line_end is not None and line_end > line_start:
                end_idx = min(line_end - 1, start_idx + max_lines, len(lines) - 1)
            else:
                # Estimate end by indentation heuristic:
                # Body ends when indentation returns to same/lower level as def line
                def_indent = len(lines[start_idx]) - len(lines[start_idx].lstrip())
                end_idx = start_idx
                for i in range(start_idx + 1, min(start_idx + max_lines, len(lines))):
                    stripped = lines[i].strip()
                    if not stripped:
                        end_idx = i  # blank lines are part of body
                        continue
                    curr_indent = len(lines[i]) - len(lines[i].lstrip())
                    if curr_indent <= def_indent and stripped:
                        break  # Back to outer scope
                    end_idx = i

            body_lines = lines[start_idx : end_idx + 1]
            if len(body_lines) < MIN_BODY_LINES:
                return None

            return "\n".join(body_lines)
        except Exception as e:
            log.debug("code_chunks: extract_body failed for %s: %s", file_path, e)
            return None

    # ── 核心 API ─────────────────────────────────────────────────────────

    async def ingest_symbols(
        self,
        symbols: list[dict],
        project_id: str | None = None,
        *,
        force: bool = False,
    ) -> dict:
        """
        从符号列表提取函数体并嵌入存储。

        symbols: list of {id, name, kind, file, line, line_end?, signature, language}
        force:   re-embed even if body_hash matches (for full reindex)

        Returns: {ingested: N, skipped: N, errors: N}
        """
        tbl = self._get_table()
        if tbl is None:
            return {"ingested": 0, "skipped": 0, "errors": 0, "error": "LanceDB unavailable"}

        pid = project_id or self.project_id
        ingested = skipped = errors = 0

        # Build existing hash map to detect unchanged symbols
        # Hash key format: "{chunk_id}:{embed_version}" — bumping version forces re-embed
        # when embedding strategy changes (e.g. we now include filename+signature).
        EMBED_VERSION = "v3"  # v3: switched to jina-v2-base-code (768-dim, code-specific)  # bump when embedding text format changes
        existing_hashes: dict[str, str] = {}
        if not force:
            try:
                rows = tbl.search().where(f"project_id = '{pid}'").select(["id", "body_hash"]).to_list()
                # Only skip if hash matches AND embed version matches
                existing_hashes = {
                    r["id"]: r["body_hash"]
                    for r in rows
                    if r["body_hash"].endswith(f":{EMBED_VERSION}")
                }
            except Exception:
                pass

        batch: list[dict] = []

        for sym in symbols:
            name     = sym.get("name", "")
            kind     = sym.get("kind", "function")
            file_path = sym.get("file", "")
            line_start = int(sym.get("line", 0))
            line_end   = sym.get("line_end")
            if line_end:
                line_end = int(line_end)
            signature = sym.get("signature", "") or ""
            language  = sym.get("language", "")

            # Skip non-function symbols to save space
            if kind not in ("function", "method", "class", "impl", "struct", "def"):
                continue

            body = self.extract_body(file_path, line_start, line_end)
            if not body:
                continue

            # body_hash includes embed version so format changes trigger re-embed
            bh = self._body_hash(body) + f":{EMBED_VERSION}"
            chunk_id = f"{file_path}:{line_start}"

            # Skip if unchanged (same body AND same embed version)
            if not force and existing_hashes.get(chunk_id) == bh:
                skipped += 1
                continue

            # ── Embedding text construction ──────────────────────────────
            # Strategy: prioritize identity (name + file) + signature,
            # then body content. This helps ranking:
            #   - "fetch" in web_tools.rs ranks above helper functions
            #     because name+file identity anchors the top result
            #   - Public/exported functions are naturally in more specific files
            import os as _os
            file_basename = _os.path.basename(file_path)
            # Visibility prefix: "pub fn" / "def" signals public API
            is_public = (
                signature.startswith("pub ") or
                (language == "python" and not name.startswith("_"))
            )
            # Build embed text: identity section + signature + body
            # Repeat name twice to strengthen its weight in the vector
            identity = f"{name} {name}"
            if is_public:
                identity = f"public {identity}"
            embed_text = (
                f"{identity} in {file_basename}\n"
                f"{signature}\n"
                f"{body[:2000]}"  # cap body to avoid token overflow
            )

            vec = self._embed(embed_text)
            if vec is None:
                errors += 1
                continue

            # Normalize to EMBED_DIM
            from evocli_soul.embedder import normalize_vector
            vec = normalize_vector(vec, EMBED_DIM)

            batch.append({
                "id":         chunk_id,
                "symbol":     name,
                "file":       file_path,
                "language":   language,
                "kind":       kind,
                "body":       body[:4000],     # cap at ~1k tokens
                "signature":  signature[:200],
                "project_id": pid,
                "line_start": line_start,
                "line_end":   line_end or (line_start + len(body.splitlines())),
                "body_hash":  bh,
                "indexed_at": time.time(),
                "vector":     vec,
            })

            if len(batch) >= 50:
                self._flush(tbl, batch, existing_hashes)
                ingested += len(batch)
                batch = []

        if batch:
            self._flush(tbl, batch, existing_hashes)
            ingested += len(batch)

        log.info(
            "CodeChunkIndex: ingested=%d skipped=%d errors=%d project=%s",
            ingested, skipped, errors, pid,
        )
        return {"ingested": ingested, "skipped": skipped, "errors": errors}

    def _flush(self, tbl, batch: list[dict], existing_hashes: dict) -> None:
        """Upsert batch into LanceDB (delete old + insert new)."""
        try:
            ids_to_delete = [r["id"] for r in batch if r["id"] in existing_hashes]
            if ids_to_delete:
                id_list = ", ".join(f"'{i}'" for i in ids_to_delete)
                tbl.delete(f"id IN ({id_list})")
            tbl.add(batch)
        except Exception as e:
            log.warning("code_chunks: flush failed: %s", e)

    def search(
        self,
        query: str,
        top_k: int = 5,
        language: str = "",
        kind: str = "",
        file_filter: str = "",
        project_id: str | None = None,
    ) -> list[dict]:
        """
        语义搜索代码块。

        query:       自然语言或代码片段描述
        top_k:       返回最相关的 N 个代码块
        language:    过滤语言（"rust", "python" 等）
        kind:        过滤类型（"function", "class"）
        file_filter: 过滤文件路径包含的字符串
        """
        tbl = self._get_table()
        if tbl is None:
            return []

        vec = self._embed(query)
        if vec is None:
            log.debug("code_chunks: search embed failed, returning []")
            return []

        from evocli_soul.embedder import normalize_vector
        vec = normalize_vector(vec, EMBED_DIM)

        pid = project_id or self.project_id

        try:
            q = tbl.search(vec).limit(top_k * 3)  # oversample for filtering

            # Build where clause
            conditions = [f"project_id = '{pid}'"]
            if language:
                conditions.append(f"language = '{language}'")
            if kind:
                conditions.append(f"kind = '{kind}'")
            where = " AND ".join(conditions)

            results = q.where(where).select(
                ["id", "symbol", "file", "language", "kind", "body", "signature",
                 "line_start", "line_end"]
            ).to_list()

            # Optional file filter (LanceDB LIKE not always available)
            if file_filter:
                results = [r for r in results if file_filter.lower() in r.get("file", "").lower()]

            return results[:top_k]
        except Exception as e:
            log.debug("code_chunks: search failed: %s", e)
            return []

    def get_body(self, symbol_id: str) -> str | None:
        """按 id 直接取函数体（用于 blast_radius 后的内容召回）。"""
        tbl = self._get_table()
        if tbl is None:
            return None
        try:
            rows = tbl.search().where(f"id = '{symbol_id}'").select(["body"]).to_list()
            return rows[0]["body"] if rows else None
        except Exception:
            return None

    def get_bodies_for_symbols(self, symbol_names: list[str], project_id: str | None = None) -> list[dict]:
        """批量获取多个符号的代码体（用于 blast_radius 结果增强）。"""
        if not symbol_names:
            return []
        tbl = self._get_table()
        if tbl is None:
            return []
        pid = project_id or self.project_id
        try:
            names_list = ", ".join(f"'{n}'" for n in symbol_names[:20])
            rows = tbl.search().where(
                f"project_id = '{pid}' AND symbol IN ({names_list})"
            ).select(["symbol", "file", "body", "line_start", "kind"]).to_list()
            return rows
        except Exception as e:
            log.debug("code_chunks: get_bodies failed: %s", e)
            return []

    def stats(self, project_id: str | None = None) -> dict:
        """返回索引统计信息。"""
        tbl = self._get_table()
        if tbl is None:
            return {"total": 0, "error": "LanceDB unavailable"}
        pid = project_id or self.project_id
        try:
            rows = tbl.search().where(f"project_id = '{pid}'").select(["language", "kind"]).to_list()
            langs: dict[str, int] = {}
            kinds: dict[str, int] = {}
            for r in rows:
                langs[r.get("language", "")] = langs.get(r.get("language", ""), 0) + 1
                kinds[r.get("kind", "")] = kinds.get(r.get("kind", ""), 0) + 1
            return {"total": len(rows), "by_language": langs, "by_kind": kinds}
        except Exception as e:
            return {"total": 0, "error": str(e)}

    async def generate_community_summaries(
        self,
        communities: list[dict],
        llm_client,
        project_id: str | None = None,
        max_communities: int = 20,
        max_symbols_per_community: int = 8,
    ) -> list[dict]:
        """
        为每个代码社区生成 LLM 自然语言摘要（GraphRAG 的核心能力）。

        communities: 来自 code_intel.communities RPC 的社区列表
                     每项包含 {id, label, symbols: [symbol_name, ...], cohesion}
        llm_client:  LLMClient 实例（用于生成摘要）
        max_communities: 最多处理 N 个社区（按大小降序）

        Returns: [{community_id, label, summary, symbols, stored_in_memory}, ...]

        工作流程：
          1. 取社区符号名 → 从 code_chunks 搜索对应代码体
          2. 拼装 prompt → 发给 LLM（fast model，摘要任务）
          3. 存入 memory_client（priority_scope="project"，memory_type="semantic"）
             key: "社区摘要: {label}"
          4. 返回摘要列表

        这使 "这个项目的认证系统怎么工作？" 这类全局问题
        可以直接检索到 LLM 生成的社区摘要，而不需要扫描所有代码。
        """
        pid = project_id or self.project_id
        results = []

        # Sort by size descending — larger communities tend to be more important
        sorted_communities = sorted(
            communities,
            key=lambda c: len(c.get("symbols", [])),
            reverse=True,
        )[:max_communities]

        for comm in sorted_communities:
            comm_id  = comm.get("id", "")
            label    = comm.get("label", comm_id)
            symbols  = comm.get("symbols", [])[:max_symbols_per_community]

            if not symbols:
                continue

            # Fetch code bodies for this community's symbols
            bodies = self.get_bodies_for_symbols(symbols, project_id=pid)
            if not bodies:
                continue

            # Build prompt with code snippets
            snippets = []
            for body_info in bodies[:max_symbols_per_community]:
                file_    = body_info.get("file", "")
                body     = (body_info.get("body", "") or "")[:500]
                if body:
                    snippets.append(f"// {file_}\n{body}")

            if not snippets:
                continue

            prompt = (
                f"Analyze this group of related code functions/classes "
                f"(community '{label}') and write a concise technical summary.\n\n"
                f"Answer:\n"
                f"1. What is the primary responsibility of this code group?\n"
                f"2. What key operations/patterns does it implement?\n"
                f"3. What external systems/APIs does it interact with?\n\n"
                f"Keep the summary under 150 words, technical and precise.\n\n"
                f"Code:\n" + "\n\n---\n\n".join(snippets)
            )

            try:
                summary = await llm_client.complete_for_task("summarize", prompt)
                summary = summary.strip()

                # Store in LanceDB memory for future retrieval
                try:
                    import evocli_soul.state as _st
                    mem = _st.get_memory()
                    if mem:
                        body_text = (
                            f"Community: {label}\n"
                            f"Symbols: {', '.join(symbols)}\n\n"
                            f"{summary}"
                        )
                        mem.add(
                            body_text,
                            memory_type="semantic",
                            priority="project",
                            importance=0.85,
                        )
                        stored = True
                    else:
                        stored = False
                except Exception as _me:
                    log.debug("community_summary: memory store failed: %s", _me)
                    stored = False

                results.append({
                    "community_id": comm_id,
                    "label":        label,
                    "summary":      summary,
                    "symbols":      symbols,
                    "stored":       stored,
                })
                log.info("Community summary generated: %s (%d symbols)", label, len(symbols))

            except Exception as e:
                log.warning("community_summary failed for %s: %s", label, e)
                continue

        return results


# ── 进程级单例 ────────────────────────────────────────────────────────────────

_index: CodeChunkIndex | None = None


def get_index(project_id: str = ".") -> CodeChunkIndex:
    global _index
    if _index is None:
        _index = CodeChunkIndex(project_id)
    return _index



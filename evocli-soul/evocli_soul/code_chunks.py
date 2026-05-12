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
import json
import logging
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("evocli.code_chunks")

# LanceDB collection name (separate from memories to avoid schema conflict)
COLLECTION = "code_chunks"
# Max function body lines to embed (避免超长类/文件爆内存)
MAX_BODY_LINES = 150
# Min body lines — skip one-liners
MIN_BODY_LINES = 2


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
        if self._embedder is not None:
            return self._embedder
        try:
            import warnings
            from fastembed import TextEmbedding
            cache_dir = str(Path.home() / ".evocli" / "models")
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", category=UserWarning)
                self._embedder = TextEmbedding(
                    model_name="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
                    cache_dir=cache_dir,
                )
            return self._embedder
        except Exception as e:
            log.debug("code_chunks: embedder init failed: %s", e)
            return None

    def _embed(self, text: str) -> list[float] | None:
        emb = self._get_embedder()
        if emb is None:
            return None
        try:
            vecs = list(emb.embed([text]))
            return list(vecs[0]) if vecs else None
        except Exception as e:
            log.debug("code_chunks: embed failed: %s", e)
            return None

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
                "vector":     [0.0] * 384,  # MiniLM-L12 = 384 dim
            }

            if COLLECTION in self._db.table_names():
                self._tbl = self._db.open_table(COLLECTION)
            else:
                # Create table with first dummy row to establish schema
                dummy = {**schema}
                dummy["id"] = "__init__"
                import pyarrow as pa
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
        existing_hashes: dict[str, str] = {}
        if not force:
            try:
                rows = tbl.search().where(f"project_id = '{pid}'").select(["id", "body_hash"]).to_list()
                existing_hashes = {r["id"]: r["body_hash"] for r in rows}
            except Exception:
                pass

        batch: list[dict] = []

        for sym in symbols:
            sym_id   = sym.get("id", "")
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

            bh = self._body_hash(body)
            chunk_id = f"{file_path}:{line_start}"

            # Skip if unchanged
            if not force and existing_hashes.get(chunk_id) == bh:
                skipped += 1
                continue

            vec = self._embed(f"{name}\n{body}")
            if vec is None:
                errors += 1
                continue

            # Make vector exactly 384 dims (MiniLM)
            if len(vec) > 384:
                vec = vec[:384]
            elif len(vec) < 384:
                vec = vec + [0.0] * (384 - len(vec))

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

        if len(vec) > 384: vec = vec[:384]
        elif len(vec) < 384: vec = vec + [0.0] * (384 - len(vec))

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


# ── 进程级单例 ────────────────────────────────────────────────────────────────

_index: CodeChunkIndex | None = None


def get_index(project_id: str = ".") -> CodeChunkIndex:
    global _index
    if _index is None:
        _index = CodeChunkIndex(project_id)
    return _index

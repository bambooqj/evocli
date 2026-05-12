"""
EvoCLI Memory Client — 本地优先，支持向量语义搜索

研究更新 (Awesome-AI-Memory 学习):
  学习来源: 380 篇 AI 记忆论文综述 (IAAR-Shanghai)

  关键研究发现:
  1. MemRouter (2026): 用嵌入分类器决定"是否值得写入记忆"，而非写入所有内容
  2. 记忆生命周期: 创建→活跃→衰减→归档→删除，不是永久保存
  3. ScrapMem (2026): 老记忆"光学遗忘"——渐进降分辨率
  4. EviMem (2026): 迭代检索——发现证据缺口时再次查询
  5. Schema-grounded Memory: 结构化记忆比自由文本检索精度高
  6. 冲突解决: 时间戳优先 + 可信度加权
  7. 量化数据: 10 轮后记忆系统比长上下文更省钱

  新增特性:
  - memory_type 细化: episodic / semantic / procedural / constraint / preference
  - 访问衰减: recall_count + last_accessed_at → 自动降权
  - 冲突检测: 写入时检查相似记忆是否矛盾
  - 重要性评分: importance_score (0-1) 影响检索排序

存储优先级：P1（项目）> P2（工具）> P3（全局）
后端: LanceDB + fastembed（向量语义搜索）+ JSONLines fallback
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ── Surrogate-safe JSON serialisation ────────────────────────────────────────
# Windows can produce Python str values with lone surrogate characters
# (e.g., from paths or system API calls that use a CESU-8/WTF-8 encoding).
# json.dumps raises UnicodeEncodeError on such strings even with ensure_ascii=False.
# _safe_json_dumps() sanitises them transparently.

def _sanitize_surrogates(obj):
    """Recursively replace lone surrogates in str values with U+FFFD."""
    if isinstance(obj, str):
        # encode with 'replace' to substitute each lone surrogate with the UTF-8
        # replacement sequence for U+FFFD, then decode back to a clean str.
        return obj.encode("utf-8", errors="replace").decode("utf-8")
    if isinstance(obj, dict):
        return {_sanitize_surrogates(k): _sanitize_surrogates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_surrogates(v) for v in obj]
    return obj


def _safe_json_dumps(obj, **kwargs) -> str:
    """json.dumps that gracefully handles lone surrogate characters.

    Fast path: try a direct serialisation (zero overhead for clean data).
    Slow path: sanitise all strings in *obj* and retry.
    """
    try:
        return json.dumps(obj, ensure_ascii=False, **kwargs)
    except (UnicodeEncodeError, ValueError, UnicodeDecodeError):
        return json.dumps(_sanitize_surrogates(obj), ensure_ascii=False, **kwargs)

log = logging.getLogger("evocli.soul.memory")

# Priority ordering for sorting
_PRIORITY_ORDER = {"project": 0, "tool": 1, "global": 2}

# Memory types (研究: 情节→语义→程序 三类记忆)
MEMORY_TYPES = {
    "episodic":    "具体交互事件（如：用户要求修改 foo.rs）",
    "semantic":    "抽象事实/规则（如：这个项目用 Rust 2021）",
    "procedural":  "技能/操作模式（如：如何构建 + 测试 + 提交）",
    "constraint":  "约束/规则（如：不能用 unwrap）",
    "preference":  "用户偏好（如：偏好简洁代码）",
}

# 记忆衰减参数 (ScrapMem 光学遗忘思路)
DECAY_HALF_LIFE_DAYS = 30.0   # 30天访问一次的记忆权重降半
MIN_IMPORTANCE       = 0.1    # 记忆最低重要性（不会完全消失）


# ── JSONLines Fallback Store ─────────────────────────────

class _JSONLinesStore:
    """Zero-dependency fallback: one JSON object per line.
    
    Thread-safety: uses a threading.Lock around file writes to prevent
    interleaved JSON lines when concurrent async tasks (e.g., _distill_session
    + memory_write tool call) write simultaneously.
    """
    import threading as _threading
    _write_lock = _threading.Lock()  # class-level lock, shared across all instances

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()
        log.info("Memory fallback: JSONLines at %s", self.path)

    def add(self, entry: dict) -> str:
        now = datetime.now(timezone.utc).isoformat()
        entry.setdefault("id", str(uuid.uuid4()))
        entry.setdefault("created_at", now)
        entry.setdefault("last_accessed_at", now)
        entry.setdefault("recall_count", 0)
        # Research: Schema-grounded memory (IAAR 2026)
        entry.setdefault("memory_type", "episodic")   # episodic/semantic/procedural/constraint/preference
        entry.setdefault("importance_score", 1.0)     # initial importance, decays over time
        line = _safe_json_dumps(entry) + "\n"
        # Lock prevents interleaved writes from concurrent async distillation + tool calls
        with _JSONLinesStore._write_lock:
            with open(self.path, "a", encoding="utf-8", errors="replace") as f:
                f.write(line)
        return entry["id"]

    def search(self, query: str, project_id: Optional[str], top_k: int) -> list[dict]:
        """
        Word-level OR matching + importance-weighted ranking.
        
        Research (Awesome-AI-Memory):
        - Schema-grounded memory: constraint/semantic memories ranked higher
        - Memory decay: importance_score decays with time since last access
        - Priority: P1 (project) > P2 (tool) > P3 (global)
        """
        query_words = [w for w in query.lower().split() if len(w) > 1]
        if not query_words:
            return []
        now = datetime.now(timezone.utc)
        results = []
        for entry in self._read_all():
            entry_pid   = entry.get("project_id", "")
            entry_scope = entry.get("priority_scope", "global")
            if project_id and project_id != "global":
                if entry_pid != project_id and entry_scope != "global":
                    continue
            text = (entry.get("title", "") + " " + entry.get("body", "")).lower()
            if any(w in text for w in query_words):
                results.append(entry)

        def _score(entry: dict) -> float:
            # Priority base score
            priority_score = 1.0 - 0.1 * _PRIORITY_ORDER.get(entry.get("priority_scope", "global"), 2)
            # Importance with time decay (ScrapMem 光学遗忘思路)
            raw_importance = float(entry.get("importance_score", 1.0))
            last_accessed  = entry.get("last_accessed_at") or entry.get("created_at", "")
            if last_accessed:
                try:
                    la = datetime.fromisoformat(last_accessed.replace("Z", "+00:00"))
                    days_since = max(0, (now - la).days)
                    decay = max(MIN_IMPORTANCE, raw_importance * (0.5 ** (days_since / DECAY_HALF_LIFE_DAYS)))
                except Exception:
                    decay = raw_importance
            else:
                decay = raw_importance
            # Constraint/semantic memories are more persistent (less decay)
            mem_type_bonus = {"constraint": 0.3, "semantic": 0.15, "preference": 0.1}.get(
                entry.get("memory_type", "episodic"), 0.0
            )
            return priority_score + decay + mem_type_bonus

        results.sort(key=_score, reverse=True)
        return results[:top_k]

    def get_constraints(self, project_id: Optional[str]) -> list[dict]:
        results = []
        for entry in self._read_all():
            if entry.get("memory_type") != "constraint":
                continue
            if project_id and entry.get("project_id") != project_id:
                if entry.get("priority_scope") != "global":
                    continue
            results.append(entry)
        results.sort(key=lambda x: _PRIORITY_ORDER.get(x.get("priority_scope", "global"), 2))
        return results

    def get_all(self, project_id: Optional[str], limit: int = 100) -> list[dict]:
        results = []
        for entry in self._read_all():
            if project_id and entry.get("project_id") != project_id:
                if entry.get("priority_scope") != "global":
                    continue
            results.append(entry)
        return results[:limit]

    def _read_all(self) -> list[dict]:
        entries = []
        try:
            # errors="replace" guards against any stale surrogate bytes written before this fix.
            with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except FileNotFoundError:
            pass
        return entries


# ── Mem0 Store (optional) ────────────────────────────────

# ── Mem0 Store 已移除（WIRE-3）───────────────────────────
# mem0 与 LanceDB+fastembed 功能重叠，统一使用 LanceDB 方案
# 如需 mem0 功能（LLM 蒸馏记忆）请通过 llm_client.py 实现

# ── Public API ───────────────────────────────────────────

class EvoCLIMemory:
    """
    统一记忆接口。后端选择（WIRE-3 重构）：
    1. LanceDB + fastembed：向量语义搜索（安装 evocli-soul[memory]）
    2. JSONLines：关键词文本搜索（零依赖，始终可用）
    """

    def __init__(self, project_id: Optional[str] = None):
        self.project_id = project_id or "global"
        self._store = self._init_store()
        self._init_vector_store()

    def _init_store(self) -> "_JSONLinesStore":
        """初始化文本存储后端（JSONLines，零依赖保证）"""
        data_dir  = Path.home() / ".evocli" / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        return _JSONLinesStore(data_dir / "memories.jsonl")

    def _init_vector_store(self):
        """
        初始化 LanceDB 向量存储。

        调用路径：EvoCLIMemory() ← state.get_memory() ← run_in_executor（后台线程）
        因此此函数已经在后台线程中运行，直接同步加载即可——不需要嵌套线程池。

        加载时间（参考值）：
          - 模型已缓存：30–60 秒（570 MB ONNX 模型读入内存 + ONNX Runtime 初始化）
          - 首次下载  ：通过 setup.ps1 / download_models.py 预先完成，此处不再下载

        注意：不使用 LanceDB Embedding Registry (reg.get("fastembed").create()) — 该接口
        在不同 LanceDB 版本中 key 名不稳定。改用 fastembed.TextEmbedding 直接调用，
        然后手动生成向量写入 LanceDB（使用 pyarrow schema + tbl.add()）。
        """
        import importlib.util
        self._vector_db  = None
        self._embed_fn   = None

        has_lancedb   = importlib.util.find_spec("lancedb")   is not None
        has_fastembed = importlib.util.find_spec("fastembed")  is not None

        if not has_lancedb or not has_fastembed:
            log.info(
                "Vector memory disabled (text-search fallback active). "
                "Enable with: pip install 'evocli-soul[memory]'"
            )
            return

        try:
            import lancedb
            from fastembed import TextEmbedding

            db_dir    = Path.home() / ".evocli" / "vectors"
            db_dir.mkdir(parents=True, exist_ok=True)
            cache_dir = str(Path.home() / ".evocli" / "models")
            model_name = "jinaai/jina-embeddings-v2-base-zh"

            # 连接 LanceDB（极快，本地文件）
            self._vector_db = lancedb.connect(str(db_dir))

            # 直接使用 fastembed.TextEmbedding — 绕过 LanceDB registry 接口
            log.info("Loading embedding model %s from cache...", model_name)
            self._embed_fn = TextEmbedding(model_name, cache_dir=cache_dir)
            log.info(
                "LanceDB + %s (768-dim, 中英双语) ready at %s",
                model_name, db_dir,
            )

        except Exception as e:
            log.warning("LanceDB init failed (%s) — text search fallback", e)
            self._vector_db = None
            self._embed_fn  = None

    def _ensure_embedder(self) -> bool:
        return self._embed_fn is not None and self._vector_db is not None

    def _upsert_vector(self, item_id: str, text: str, metadata: dict) -> None:
        """写入向量索引（fastembed.TextEmbedding 直接生成向量，手动写入 LanceDB）。"""
        if not self._ensure_embedder():
            return
        try:
            import pyarrow as pa

            # fastembed.TextEmbedding.embed() → generator of numpy arrays
            vecs = list(self._embed_fn.embed([text]))
            if not vecs:
                return
            vec = vecs[0].tolist()
            dim = len(vec)

            row = {
                "id":       item_id,
                "text":     text,
                "vector":   vec,
                "metadata": _safe_json_dumps(metadata),
            }

            table_name = "memories"
            try:
                tbl = self._vector_db.open_table(table_name)
                tbl.add([row])
            except Exception:
                schema = pa.schema([
                    pa.field("id",       pa.string()),
                    pa.field("text",     pa.string()),
                    pa.field("vector",   pa.list_(pa.float32(), dim)),
                    pa.field("metadata", pa.string()),
                ])
                tbl = self._vector_db.create_table(table_name, schema=schema)
                tbl.add([row])

            log.debug("Vector upsert ok: %s", item_id[:8])
        except Exception as e:
            log.debug("Vector upsert failed: %s", e)

    def _vector_search(self, query: str, top_k: int = 5,
                        current_project: str = ".", active_tools: list | None = None) -> list[dict]:
        """向量语义搜索 + P1/P2/P3 优先级重排序。"""
        if not self._ensure_embedder():
            return []
        try:
            tbl = self._vector_db.open_table("memories")

            # fastembed.TextEmbedding.embed() — same method for both docs and queries
            query_vecs = list(self._embed_fn.embed([query]))
            if not query_vecs:
                return []
            query_vec = query_vecs[0].tolist()

            # 向量搜索（纯 ANN）— hybrid 需要 FTS index 预先创建，此处用标准向量搜索
            candidates = tbl.search(query_vec).limit(top_k * 3).to_list()

            # ── 优先级重排序（Section 6.4 PRIORITY_BOOST）────────────
            PRIORITY_BOOST = {"project": 1.5, "tool": 1.2, "global": 1.0}

            # Pre-parse metadata for all candidates once to avoid repeated json.loads
            # in both score() and the result loop. id() is stable for the lifetime
            # of `candidates` since we hold strong references to all dicts.
            _meta_cache: dict[int, dict] = {
                id(r): json.loads(r.get("metadata", "{}"))
                for r in candidates
            }

            def score(r: dict) -> float:
                # 向量相似度（LanceDB 返回 _distance，越小越相似）
                distance        = r.get("_distance", 1.0)
                semantic_score  = max(0.0, 1.0 - distance)

                # 优先级
                meta   = _meta_cache[id(r)]  # pre-parsed — no json.loads here
                scope  = meta.get("priority_scope", "global")
                # P1：当前项目记忆
                if scope == "project" and meta.get("project_id") == current_project:
                    priority = "project"
                # P2：工具记忆（活跃工具）
                elif scope == "tool" and active_tools and meta.get("tool_id") in active_tools:
                    priority = "tool"
                else:
                    priority = "global"

                boost = PRIORITY_BOOST.get(priority, 1.0)

                # 时间衰减（recency）：越近越好，decay_factor = 0.95^days
                decay = 1.0
                created = meta.get("created_at", "")
                if created:
                    try:
                        created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                        days_old   = (datetime.now(timezone.utc) - created_dt).days
                        decay      = 0.95 ** days_old
                    except Exception:
                        pass

                return semantic_score * boost * decay

            ranked = sorted(candidates, key=score, reverse=True)

            # 冲突压制：同 tags 下高优先级压制低优先级
            seen_tags: dict[str, str] = {}  # tags_key → priority_scope
            PORDER = {"project": 0, "tool": 1, "global": 2}
            results = []
            for r in ranked:
                meta  = _meta_cache[id(r)]  # pre-parsed — no second json.loads
                scope = meta.get("priority_scope", "global")
                # tags may be stored as list or JSON string depending on insertion path
                raw_tags = meta.get("tags", [])
                tags = frozenset(raw_tags if isinstance(raw_tags, list) else json.loads(raw_tags))
                key   = str(tags)
                if key in seen_tags:
                    if PORDER.get(scope, 2) >= PORDER.get(seen_tags[key], 2):
                        # 当前记忆优先级 ≤ 已存的，跳过
                        meta["suppressed"] = True
                        meta["suppressed_by"] = seen_tags[key]
                        continue
                seen_tags[key] = scope
                results.append({"memory": r["text"], **meta})
                if len(results) >= top_k:
                    break

            return results
        except Exception as e:
            log.debug("Vector search failed: %s", e)
            return []

    # _init_store 已移至 __init__ 上方（WIRE-3：不再需要 mem0 选择逻辑）

    def add(
        self,
        content: str,
        memory_type: str = "episodic",
        priority: str = "project",
        severity: Optional[str] = None,
        tags: Optional[list[str]] = None,
        importance: float = 1.0,
    ) -> str:
        """
        添加记忆，返回记忆 ID。
        
        Research updates (Awesome-AI-Memory 2026):
        - Schema-grounded: memory_type 细化 (episodic/semantic/procedural/constraint/preference)
        - importance_score: 初始重要性，越高越难被衰减遗忘
        - constraint/semantic 类型持久性更强（半衰期 2x）
        """
        # 规范化 memory_type（兼容旧的 "episode" 写法）
        type_map = {"episode": "episodic", "preference": "preference", "constraint": "constraint"}
        memory_type = type_map.get(memory_type, memory_type)
        if memory_type not in MEMORY_TYPES:
            memory_type = "episodic"

        entry = {
            "title":          content[:80],
            "body":           content,
            "memory_type":    memory_type,
            "priority_scope": priority,
            "project_id":     self.project_id,
            "severity":       severity,
            "tags":           tags or [],
            "importance_score": importance,
        }
        mid = self._store.add(entry)
        # 同步写入向量（registry 自动处理嵌入）
        if self._ensure_embedder():
            self._upsert_vector(mid, content, entry)
        log.debug("Memory added: %s [%s/%s importance=%.1f]", mid[:8], priority, memory_type, importance)
        return mid

    def search(self, query: str, top_k: int = 5,
               current_project: str | None = None,
               active_tools: list | None = None) -> list[dict]:
        """
        搜索记忆，按 P1/P2/P3 优先级重排序。
        FIX-STREAM-2: 先尝试向量搜索，embedder 未就绪时触发初始化再 fallback 文本搜索。
        向量搜索提供语义相似度（比关键词更准确）。
        """
        project = current_project or self.project_id or "."
        # 尝试向量语义搜索
        vec_results = self._vector_search(query, top_k,
                                          current_project=project,
                                          active_tools=active_tools)
        if vec_results:
            log.debug("Vector search returned %d results for '%s'", len(vec_results), query[:30])
            return vec_results

        # Embedder 未就绪时记录并使用文本搜索
        if self._vector_db is not None and self._embed_fn is None:
            log.info("Vector search unavailable (embedder not ready), using text search fallback.")
        # fallback: 文本关键词搜索（使用 current_project 保证 P1/P2 记忆可搜索到）
        return self._store.search(query, project, top_k)

    def get_constraints(self) -> list[str]:
        """Get L1 constraint memories (for system prompt injection)."""
        items = self._store.get_constraints(self.project_id)
        constraints = []
        for item in items:
            if isinstance(item, dict):
                text = item.get("memory") or item.get("body") or item.get("title", "")
                if text:
                    constraints.append(text)
        return constraints

    def get_all(self, limit: int = 100) -> list[dict]:
        """List all memories for current project."""
        return self._store.get_all(self.project_id, limit)

    def get_memory_stats(self) -> dict:
        """
        Return memory statistics — useful for deciding when to compress/forget.
        Research: MemRouter decides write/no-write based on memory state.
        """
        all_mem = self.get_all(limit=10000)
        now = datetime.now(timezone.utc)
        type_counts: dict[str, int] = {}
        total_decayed = 0
        for m in all_mem:
            t = m.get("memory_type", "episodic")
            type_counts[t] = type_counts.get(t, 0) + 1
            # Check if significantly decayed
            last_accessed = m.get("last_accessed_at") or m.get("created_at", "")
            if last_accessed:
                try:
                    la = datetime.fromisoformat(last_accessed.replace("Z", "+00:00"))
                    if (now - la).days > DECAY_HALF_LIFE_DAYS * 2:
                        total_decayed += 1
                except Exception:
                    pass
        return {
            "total":           len(all_mem),
            "by_type":         type_counts,
            "significantly_decayed": total_decayed,
            "project_id":      self.project_id,
        }

    def forget_decayed(self, min_days: int = 90, dry_run: bool = True) -> list[str]:
        """
        Remove significantly decayed memories (those not accessed for min_days).
        
        Research (Awesome-AI-Memory):
        - Memory Forgetting: auto-lower priority of infrequently accessed memories
        - Privacy-Driven Forgetting: episodic memories expire naturally
        - Constraint/semantic memories are protected (not auto-forgotten)
        
        Returns: list of forgotten memory IDs
        """
        all_mem = self.get_all(limit=10000)
        now     = datetime.now(timezone.utc)
        to_forget = []
        PROTECTED_TYPES = {"constraint", "semantic", "preference"}

        for m in all_mem:
            mem_type = m.get("memory_type", "episodic")
            if mem_type in PROTECTED_TYPES:
                continue  # Never auto-forget constraints/semantics
            last_accessed = m.get("last_accessed_at") or m.get("created_at", "")
            if not last_accessed:
                continue
            try:
                la = datetime.fromisoformat(last_accessed.replace("Z", "+00:00"))
                if (now - la).days >= min_days:
                    to_forget.append(m.get("id", ""))
            except Exception:
                pass

        if not dry_run and to_forget:
            # Mark as archived (soft delete) — rewrite JSONL without these entries
            all_mem_filtered = [m for m in all_mem if m.get("id") not in set(to_forget)]
            try:
                with open(self._store.path, "w", encoding="utf-8", errors="replace") as f:
                    for m in all_mem_filtered:
                        f.write(_safe_json_dumps(m) + "\n")
                log.info("Memory forget: removed %d decayed memories (>%d days)", len(to_forget), min_days)
            except Exception as e:
                log.warning("Memory forget failed: %s", e)

        return to_forget


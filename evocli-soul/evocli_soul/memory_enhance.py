"""
memory_enhance.py — 记忆增强功能集合

研究来源 (Awesome-AI-Memory 2026):

1. EviMem: "Evidence-Gap-Driven Iterative Retrieval for Long-Term Conversational Memory"
   - 核心: 首次检索后评估"证据缺口"，不足则自动补充查询
   - IRIS 闭环: 充分性评估 → 发现缺口 → 改写查询 → 再检索

2. Memory Reflection Loop (多篇论文共识):
   - 情节记忆(episodic) 频繁访问后 → 升格为语义记忆(semantic)
   - 类似人类"巩固记忆"过程 (睡眠期间将短期→长期)

3. Conflict Resolution (记忆冲突解决):
   - 写入前检查相似记忆是否矛盾
   - 时间戳优先: 新信息胜过旧信息 (对于事实)
   - 约束记忆例外: constraint 不会被覆盖，只能显式更新

4. MemCoE 两阶段优化:
   - 阶段1: 学习"全局记忆准则" (什么值得记)
   - 阶段2: 基于准则进行多轮强化学习
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("evocli.memory_enhance")

# Module-level lock protecting all read-modify-write operations on the JSONL memory file.
# Without this, concurrent background distillation tasks (e.g., from simultaneous session pauses)
# performing consolidate() or resolve_conflict() could interleave their reads and writes,
# causing one task's changes to silently overwrite the other's.
_jsonl_write_lock = threading.Lock()

# 情节→语义升格阈值 (论文: "Modular Memory is the Key to Continual Learning")
EPISODIC_PROMOTE_THRESHOLD = 3   # 被访问 N 次后升格
# 证据充分性阈值
EVIDENCE_SUFFICIENCY_MIN   = 2   # 至少需要 N 条相关记忆才算"充分"


class IterativeRetriever:
    """
    EviMem 风格的迭代检索器。

    论文核心思路:
      首次检索 → 充分性评估 → 发现证据缺口 → 改写查询 → 再检索
      在 LoCoMo 基准上显著提升时序和多跳问题准确率。

    EvoCLI 实现:
      1. 首次检索 top_k * 2 条
      2. 评估充分性 (数量 + 多样性)
      3. 若不足，用不同词汇二次查询
    """

    def __init__(self, memory_client):
        self._memory = memory_client

    def search_with_evidence_check(
        self,
        query: str,
        top_k: int = 5,
        max_iterations: int = 2,
    ) -> tuple[list[dict], dict]:
        """
        迭代检索，自动补充证据缺口。

        Returns:
            (results, meta)
            meta: {"iterations": int, "gap_queries": list, "evidence_sufficient": bool}
        """
        all_results: list[dict] = []
        seen_ids: set          = set()
        gap_queries: list[str] = []
        iterations = 0

        # 第一次检索
        first_results = self._memory.search(query, top_k=top_k * 2)
        for r in first_results:
            rid = r.get("id", "")
            if rid not in seen_ids:
                all_results.append(r)
                seen_ids.add(rid)
        iterations += 1

        # 评估充分性
        sufficient = self._is_sufficient(all_results, top_k)

        # 迭代补充 (最多 max_iterations 次)
        for _ in range(max_iterations - 1):
            if sufficient:
                break
            # 生成补充查询 (基于现有结果的词汇缺口)
            gap_q = self._generate_gap_query(query, all_results)
            if not gap_q or gap_q == query:
                break
            gap_queries.append(gap_q)
            additional = self._memory.search(gap_q, top_k=top_k)
            for r in additional:
                rid = r.get("id", "")
                if rid not in seen_ids:
                    all_results.append(r)
                    seen_ids.add(rid)
            iterations += 1
            sufficient = self._is_sufficient(all_results, top_k)

        # 按重要性重排序 (priority_scope + importance_score)
        all_results.sort(key=lambda x: (
            -{"project": 2, "tool": 1, "global": 0}.get(x.get("priority_scope", "global"), 0),
            -float(x.get("importance_score", 1.0)),
        ))

        return all_results[:top_k], {
            "iterations": iterations,
            "gap_queries": gap_queries,
            "evidence_sufficient": sufficient,
            "total_found": len(all_results),
        }

    def _is_sufficient(self, results: list[dict], needed: int) -> bool:
        """判断证据是否充分: 数量 + 文件多样性。"""
        if len(results) < min(EVIDENCE_SUFFICIENCY_MIN, needed):
            return False
        # 检查来自不同文件的多样性 (避免全是同一来源)
        sources = {r.get("project_id", "") for r in results}
        return len(results) >= needed and len(sources) >= 1

    def _generate_gap_query(self, original_query: str, existing: list[dict]) -> str:
        """
        基于现有结果生成补充查询。
        
        策略: 从 original_query 中提取不在现有结果中的关键词
        + 从现有结果标题中提取关联词
        """
        # 提取现有结果覆盖的词
        covered_words: set[str] = set()
        for r in existing:
            text = (r.get("title", "") + " " + r.get("body", "")).lower()
            covered_words.update(text.split())

        # 找 original_query 中未覆盖的重要词
        query_words = [w for w in original_query.lower().split() if len(w) > 2]
        uncovered   = [w for w in query_words if w not in covered_words]

        if uncovered:
            return " ".join(uncovered[:3])

        # 从现有结果提取关联词作为扩展查询
        if existing:
            titles = [r.get("title", "") for r in existing[:3]]
            all_title_words = " ".join(titles).lower().split()
            # 找不在 original_query 中的词
            new_words = [w for w in all_title_words if w not in original_query.lower() and len(w) > 2]
            if new_words:
                return original_query + " " + " ".join(new_words[:2])

        return ""  # 无法生成不同查询


class MemoryConsolidator:
    """
    记忆巩固器 — 情节记忆自动升格为语义记忆。

    研究基础 (Memory Reflection Loop, 多篇论文共识):
      人类记忆巩固: 睡眠期间大脑将短期记忆(海马体)转移到长期(大脑皮层)
      AI 等价: 高频访问的 episodic → semantic，提高持久性和泛化能力

    触发条件:
      1. recall_count >= EPISODIC_PROMOTE_THRESHOLD (频繁访问)
      2. memory_type == "episodic"
      3. 内容包含一般性事实 (非纯粹单次事件)
    """

    def __init__(self, store):
        self._store = store

    def consolidate(self, project_id: Optional[str] = None, dry_run: bool = False) -> dict:
        """
        运行记忆巩固。检查所有 episodic 记忆，将高频访问的升格为 semantic。

        Returns: {"promoted": N, "details": [...]}
        """
        all_mem = self._store._read_all()
        promoted = []
        updated  = []

        for entry in all_mem:
            if project_id and entry.get("project_id") != project_id:
                if entry.get("priority_scope") != "global":
                    continue
            if entry.get("memory_type") != "episodic":
                continue
            if entry.get("recall_count", 0) < EPISODIC_PROMOTE_THRESHOLD:
                continue
            # 内容检查: 情节描述("今天","刚才")不升格，通用事实升格
            body = entry.get("body", "")
            if any(kw in body for kw in ["今天", "刚才", "这次", "刚刚", "昨天", "just now", "today"]):
                continue  # 纯情节事件，不升格
            # 升格!
            entry["memory_type"]      = "semantic"
            entry["importance_score"] = min(1.0, float(entry.get("importance_score", 0.5)) * 1.5)
            entry["consolidated_at"]  = datetime.now(timezone.utc).isoformat()
            promoted.append(entry.get("id", ""))
            updated.append(entry)

        if not dry_run and promoted:
            # Build the updated memory list: replace promoted entries with their new versions.
            all_mem_updated = []
            promoted_set = set(promoted)
            promoted_map = {e.get("id"): e for e in updated}
            for m in all_mem:
                if m.get("id") in promoted_set:
                    all_mem_updated.append(promoted_map[m["id"]])
                else:
                    all_mem_updated.append(m)
            # Rewrite JSONL atomically under the module lock to prevent concurrent
            # consolidate() / resolve_conflict() calls from overwriting each other.
            try:
                import json
                with _jsonl_write_lock:
                    with open(self._store.path, "w", encoding="utf-8") as f:
                        for m in all_mem_updated:
                            f.write(json.dumps(m, ensure_ascii=False) + "\n")
                log.info("Memory consolidation: promoted %d episodic → semantic", len(promoted))
            except Exception as e:
                log.warning("Memory consolidation write failed: %s", e)

        return {"promoted": len(promoted), "ids": promoted}


class ConflictDetector:
    """
    记忆冲突检测器。

    研究来源:
      "Conflict-Driven Forgetting": 当新证据与旧记忆冲突时，策略性更新或淘汰旧记忆
      "Conflict Resolution": 矛盾信息仲裁 (时间戳优先、来源可信度加权)

    实现策略:
      1. 写入前检索相似记忆
      2. 检测是否存在逻辑矛盾 (同主题不同值)
      3. 约束记忆(constraint)受保护，不自动覆盖
      4. 其他类型: 时间戳新的覆盖旧的，旧记忆降权
    """

    def __init__(self, store):
        self._store = store

    def check_conflict(
        self,
        new_content: str,
        memory_type: str,
        project_id: Optional[str],
    ) -> dict:
        """
        检查新内容是否与现有记忆冲突。

        Returns:
          {
            "has_conflict": bool,
            "conflicting_ids": list[str],
            "action": "replace" | "update_score" | "skip" | "none",
            "reason": str,
          }
        """
        # 搜索相似现有记忆
        query_words = [w for w in new_content.lower().split() if len(w) > 2][:5]
        if not query_words:
            return {"has_conflict": False, "action": "none", "reason": ""}

        query = " ".join(query_words)
        existing = self._store.search(query, project_id, top_k=5)

        if not existing:
            return {"has_conflict": False, "action": "none", "reason": ""}

        # 约束记忆保护: 不自动覆盖
        if memory_type == "constraint":
            constraint_conflicts = [e for e in existing if e.get("memory_type") == "constraint"]
            if constraint_conflicts:
                return {
                    "has_conflict":    True,
                    "conflicting_ids": [e.get("id") for e in constraint_conflicts],
                    "action":          "skip",
                    "reason":          "Conflict with protected constraint memory — explicit update required",
                }

        # 高相似度: 时间戳新的覆盖 (Conflict-Driven Forgetting)
        high_sim = self._find_high_similarity(new_content, existing)
        if high_sim:
            cids = [e.get("id") for e in high_sim]
            return {
                "has_conflict":    True,
                "conflicting_ids": cids,
                "action":          "replace",  # 降低旧记忆的 importance_score
                "reason":          f"New information supersedes {len(cids)} existing memorie(s)",
            }

        return {"has_conflict": False, "action": "none", "reason": ""}

    def _find_high_similarity(self, content: str, candidates: list[dict]) -> list[dict]:
        """使用 Jaccard 相似度找高相似记忆。"""
        words_new = set(content.lower().split())
        high_sim  = []
        for c in candidates:
            text      = (c.get("body") or c.get("title") or "").lower()
            words_old = set(text.split())
            if not words_new or not words_old:
                continue
            jaccard = len(words_new & words_old) / len(words_new | words_old)
            if jaccard > 0.65:  # 65% 词重叠视为同主题
                high_sim.append(c)
        return high_sim

    def resolve_conflict(
        self,
        conflicting_ids: list[str],
        decay_factor: float = 0.5,
        dry_run: bool = False,
    ) -> int:
        """将冲突记忆的重要性降权 (Conflict-Driven Forgetting)。"""
        import json
        all_mem = self._store._read_all()
        cid_set = set(conflicting_ids)
        changed = 0
        updated = []
        for m in all_mem:
            if m.get("id") in cid_set:
                old_score = float(m.get("importance_score", 1.0))
                m["importance_score"] = max(0.05, old_score * decay_factor)
                m["conflict_resolved_at"] = datetime.now(timezone.utc).isoformat()
                changed += 1
            updated.append(m)
        if not dry_run and changed:
            try:
                with _jsonl_write_lock:  # Prevent race with concurrent consolidate() calls
                    with open(self._store.path, "w", encoding="utf-8") as f:
                        for m in updated:
                            f.write(json.dumps(m, ensure_ascii=False) + "\n")
            except Exception as e:
                log.warning("Conflict resolve write failed: %s", e)
        return changed

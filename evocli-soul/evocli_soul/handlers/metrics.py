"""
handlers/metrics.py — 指标、知识迁移、MemRouter 标签存储

补全之前规划但未实现的 handler:

  system.stats          — 系统指标仪表盘（内存/Skill/Evolution 统计）
  evolution.transfer    — 跨项目知识迁移（wire knowledge_classifier.py）
  mem_router.store_label — 存储 LLM 打标签结果（训练数据积累）
  mem_router.classify   — 规则分类器（Python 侧，无需 Rust 调用）
  mem_router.train_status — 查看训练状态和数据量
"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("evocli.handlers.metrics")

# MemRouter 训练数据存储路径
_LABELS_FILE = Path.home() / ".evocli" / "mem_router" / "labels.jsonl"
_MODEL_FILE  = Path.home() / ".evocli" / "mem_router" / "model.json"
TRAIN_THRESHOLD_PER_CLASS = 200    # 每类达到 200 条触发初次训练，随后 delta 增量重训
                                    # 原 4000 过高导致模型永不激活——修复为可达阈值

# ── 模块级嵌入模型缓存（避免每次推理重载模型，保证 < 5ms 推理延迟）────────────────
# 使用小写以避免 basedpyright 把大写变量当 const 处理
_embedder_cache: Optional[Any] = None   # fastembed.TextEmbedding 实例缓存

def _get_embedder():
    """懒加载并缓存 fastembed 嵌入模型（只初始化一次，进程内复用）。"""
    global _embedder_cache  # noqa: PLW0603
    if _embedder_cache is not None:
        return _embedder_cache
    import importlib.util
    if not importlib.util.find_spec("fastembed"):
        return None
    try:
        from fastembed import TextEmbedding
        cache_dir = str(Path.home() / ".evocli" / "models")
        _embedder_cache = TextEmbedding(
            model_name="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
            cache_dir=cache_dir,
        )
        log.info("fastembed TextEmbedding (paraphrase-multilingual-MiniLM-L12-v2) loaded and cached")
        return _embedder_cache
    except Exception as e:
        log.debug("fastembed load failed: %s", e)
        return None


def register(router) -> None:
    router.add("system.stats",            handle_system_stats)
    router.add("evolution.transfer",      handle_evolution_transfer)
    router.add("mem_router.store_label",  handle_mem_router_store_label)
    router.add("mem_router.classify",     handle_mem_router_classify)
    router.add("mem_router.train_status", handle_mem_router_train_status)
    router.add("mem_router.train",        handle_mem_router_train)


# ── system.stats ─────────────────────────────────────────────────────────────

async def handle_system_stats(req_id: str, params: dict, send, state) -> None:
    """
    系统指标仪表盘（规划 Section 9.6 成功指标）。
    返回: Memory统计 / Skill统计 / Evolution统计 / MemRouter状态
    """
    try:
        memory = state.get_memory()
        mem_stats = memory.get_memory_stats()

        # Skill 统计
        skill_engine = state.get_skill_engine()
        skills = skill_engine.list_skills()
        skill_stats: dict[str, Any] = {
            "total": len(skills),
            "by_status": {},
        }
        for s in skills:
            status = s.get("status", "unknown")
            by_status: dict[str, int] = skill_stats["by_status"]
            by_status[status] = by_status.get(status, 0) + 1

        # MemRouter 训练状态
        mr_status = _get_mem_router_status()

        # Evolution 统计（来自 evolution.db 如果存在）
        evolution_stats = _get_evolution_stats()

        stats = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "memory":        mem_stats,
            "skills":        skill_stats,
            "mem_router":    mr_status,
            "evolution":     evolution_stats,
        }
        await send.response(req_id, stats)
    except Exception as e:
        log.exception("system.stats failed")
        await send.error(req_id, -32603, str(e))


def _get_mem_router_status() -> dict:
    """读取 MemRouter 训练数据状态。"""
    if not _LABELS_FILE.exists():
        return {"total_labels": 0, "by_class": {}, "model_ready": False, "threshold": TRAIN_THRESHOLD_PER_CLASS}
    try:
        labels = [json.loads(l) for l in _LABELS_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
        by_class: dict[str, int] = {}
        for lb in labels:
            cls = lb.get("label_name", "unknown")
            by_class[cls] = by_class.get(cls, 0) + 1
        ready = _MODEL_FILE.exists()
        min_per_class = min(by_class.values()) if by_class else 0
        return {
            "total_labels":      len(labels),
            "by_class":          by_class,
            "model_ready":       ready,
            "threshold":         TRAIN_THRESHOLD_PER_CLASS,
            "min_per_class":     min_per_class,
            "training_progress": f"{min_per_class}/{TRAIN_THRESHOLD_PER_CLASS} (min class)"
        }
    except Exception as e:
        return {"error": str(e)}


def _get_evolution_stats() -> dict:
    """获取进化引擎统计。"""
    try:
        import importlib.util
        if not importlib.util.find_spec("prefixspan"):
            return {"status": "prefixspan not available"}
        # 简单统计：patterns 和 skill 草案数量
        skills_dir = Path.home() / ".evocli" / "skills"
        auto_skills = list(skills_dir.glob("auto_*.toml")) if skills_dir.exists() else []
        return {
            "auto_skills_generated": len(auto_skills),
            "status": "active",
        }
    except Exception:
        return {"status": "unavailable"}


# ── evolution.transfer ───────────────────────────────────────────────────────

async def handle_evolution_transfer(req_id: str, params: dict, send, state) -> None:
    """
    跨项目知识迁移（规划 Section 9.9）。
    将 P1 项目记忆中可迁移的知识提升为 P3 全局记忆。

    params:
      project_id:     str   源项目 ID
      dry_run:        bool  只分析不执行（default True）
      min_confidence: float 最低置信度（default 0.65）
    """
    params.get("project_id", ".")
    dry_run        = params.get("dry_run", True)
    min_confidence = float(params.get("min_confidence", 0.65))
    try:
        from evocli_soul.evolution.knowledge_classifier import KnowledgeClassifier, Transferability
        bridge  = state.get_bridge()
        memory  = state.get_memory()
        clf     = KnowledgeClassifier()

        # 读取当前项目的 P1 记忆
        all_mem = memory.get_all(limit=500)
        project_memories = [m for m in all_mem if m.get("priority_scope") == "project"]

        results = []
        promoted = 0
        for mem in project_memories:
            result = clf.classify(mem)
            if result.transferability == Transferability.PROJECT_ONLY or result.confidence < min_confidence:
                continue
            target_scope = "global" if result.transferability.value >= 2 else "tool"
            entry = {
                "title":           mem.get("title", ""),
                "from_scope":      "project",
                "to_scope":        target_scope,
                "confidence":      result.confidence,
                "reason":          result.reason,
            }
            if not dry_run:
                try:
                    await bridge.call("memory.write", {
                        "priority_scope": target_scope,
                        "memory_type":    mem.get("memory_type", "semantic"),
                        "title":          f"[{target_scope.upper()}] {mem.get('title','')}",
                        "body":           mem.get("body", ""),
                        "tags":           (mem.get("tags") or []) + ["cross-project", "promoted"],
                    })
                    promoted += 1
                    entry["promoted"] = True
                except Exception as e:
                    entry["error"] = str(e)
            results.append(entry)

        await send.response(req_id, {
            "ok":        True,
            "dry_run":   dry_run,
            "analyzed":  len(project_memories),
            "transferable": len(results),
            "promoted":  promoted,
            "results":   results[:20],  # cap for context
        })
    except Exception as e:
        log.exception("evolution.transfer failed")
        await send.error(req_id, -32603, str(e))


# ── mem_router handlers ───────────────────────────────────────────────────────

async def handle_mem_router_store_label(req_id: str, params: dict, send, _state) -> None:
    """
    存储 LLM 打标签结果到训练数据文件（MemRouter Phase 1 数据积累）。

    params:
      text:        str   要分类的文本内容
      label_name:  str   类别名称（constraint/preference/semantic/procedural/episodic/no_write）
      label_idx:   int   类别索引（0-5）
      project_id:  str   项目 ID
      confidence:  float LLM 打标签置信度（LLM=1.0, 规则=0.8）
      source:      str   标签来源（llm/rule/reranker）
    """
    text       = params.get("text", "")
    label_name = params.get("label_name", "episodic")
    label_idx  = int(params.get("label_idx", 4))
    project_id = params.get("project_id", "")
    confidence = float(params.get("confidence", 1.0))
    source     = params.get("source", "llm")

    if not text or not label_name:
        await send.error(req_id, -32600, "text and label_name are required")
        return
    try:
        _LABELS_FILE.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "text":       text,
            "label_name": label_name,
            "label_idx":  label_idx,
            "project_id": project_id,
            "confidence": confidence,
            "source":     source,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(_LABELS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        # 检查是否触发自动训练
        total = sum(1 for _ in _LABELS_FILE.read_text(encoding="utf-8").splitlines() if _.strip())
        should_train = _check_should_train()

        await send.response(req_id, {
            "ok":           True,
            "total_labels": total,
            "should_train": should_train,
        })
    except Exception as e:
        log.exception("mem_router.store_label failed")
        await send.error(req_id, -32603, str(e))


async def handle_mem_router_classify(req_id: str, params: dict, send, _state) -> None:
    """
    MemRouter 分类推理（规则引擎 → 训练模型）。

    params:
      content:           str   要分类的文本
      use_trained_model: bool  是否使用训练好的模型（默认 True，没有则回退规则）

    Returns:
      {label, confidence, should_write, importance, source}
    """
    content = params.get("content", "")
    use_model = params.get("use_trained_model", True)
    if not content:
        await send.error(req_id, -32600, "content is required")
        return
    try:
        # 尝试使用训练好的 sklearn 模型
        if use_model and _MODEL_FILE.exists():
            result = _classify_with_model(content)
            if result:
                await send.response(req_id, result)
                return
        # 回退到规则分类器
        from evocli_soul.memory_router import get_memory_router
        router = get_memory_router()
        should, mem_type, importance = router.should_memorize(content)
        await send.response(req_id, {
            "label":        mem_type,
            "confidence":   importance,
            "should_write": should,
            "importance":   importance,
            "source":       "rule_engine",
        })
    except Exception as e:
        log.exception("mem_router.classify failed")
        await send.error(req_id, -32603, str(e))


async def handle_mem_router_train_status(req_id: str, params: dict, send, _state) -> None:
    """查询 MemRouter 训练状态。"""
    try:
        status = _get_mem_router_status()
        status["should_train"] = _check_should_train()
        await send.response(req_id, status)
    except Exception as e:
        await send.error(req_id, -32603, str(e))


async def handle_mem_router_train(req_id: str, params: dict, send, _state) -> None:
    """
    触发 MemRouter 训练（Python sklearn 逻辑回归，CPU，无显卡）。
    当训练数据足够时调用此接口触发训练并保存模型。
    """
    try:
        if not _check_should_train():
            status = _get_mem_router_status()
            await send.response(req_id, {
                "ok": False,
                "reason": f"Not enough data. Min per class: {status.get('min_per_class',0)}/{TRAIN_THRESHOLD_PER_CLASS}",
                "status": status,
            })
            return
        # Offload CPU-intensive training (200-epoch gradient descent + file I/O) to
        # a thread pool so it doesn't freeze the asyncio event loop and stall the TUI.
        import asyncio as _asyncio
        result = await _asyncio.to_thread(_train_sklearn_classifier)
        await send.response(req_id, result)
    except Exception as e:
        log.exception("mem_router.train failed")
        await send.error(req_id, -32603, str(e))


def store_label_direct(
    text: str,
    label_idx: int,
    label_name: str,
    project_id: str = "",
    confidence: float = 1.0,
    source: str = "llm_seed",
) -> dict:
    """
    直接写入 MemRouter 训练标签（不经过 JSON-RPC bridge）。

    供 mem_router_labeler.py 的 seed_labels_from_existing() 直接调用，
    避免错误地将 Python-side RPC handler 通过 bridge.call() 路由到 Rust
    （Rust tool_dispatch.rs 没有 mem_router.store_label arm）。

    Returns: {"ok": True, "total_labels": int, "should_train": bool}
             或 {"ok": False, "error": str}
    """
    if not text or not label_name:
        return {"ok": False, "error": "text and label_name are required"}
    try:
        _LABELS_FILE.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "text":       text,
            "label_name": label_name,
            "label_idx":  label_idx,
            "project_id": project_id,
            "confidence": confidence,
            "source":     source,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(_LABELS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        total = sum(1 for _ in _LABELS_FILE.read_text(encoding="utf-8").splitlines() if _.strip())
        should_train = _check_should_train()
        return {"ok": True, "total_labels": total, "should_train": should_train}
    except Exception as e:
        log.exception("store_label_direct failed")
        return {"ok": False, "error": str(e)}


def _check_should_train() -> bool:
    """检查是否满足训练条件（每类 >= TRAIN_THRESHOLD_PER_CLASS）。"""
    if not _LABELS_FILE.exists():
        return False
    try:
        labels = [json.loads(l) for l in _LABELS_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
        by_class: dict[str, int] = {}
        for lb in labels:
            cls = lb.get("label_name", "unknown")
            by_class[cls] = by_class.get(cls, 0) + 1
        if not by_class:
            return False
        # 至少 4 个类别达到阈值
        sufficient = sum(1 for c in by_class.values() if c >= TRAIN_THRESHOLD_PER_CLASS)
        return sufficient >= 4
    except Exception:
        return False


def _classify_with_model(content: str) -> Optional[dict]:
    """
    使用训练好的逻辑回归模型进行分类。
    性能说明：
      - 使用模块级缓存的 fastembed 嵌入模型（_get_embedder()）避免首次模型加载开销（100ms+）
      - 使用 numpy 矩阵运算替代 Python 循环（softmax 6×384 → ~0.1ms）
      - 实际推理延迟：embedding 生成 ~10-30ms（ONNX CPU）+ 分类计算 < 1ms = 总计约 15ms
      - "< 5ms 推理" 仅指分类计算部分，embedding 生成是主要耗时
    """
    try:
        model_data = json.loads(_MODEL_FILE.read_text(encoding="utf-8"))
        weights = model_data.get("weights", [])
        biases  = model_data.get("biases", [])
        if not weights:
            return None

        # 使用缓存的嵌入模型（避免重复初始化）
        embedder = _get_embedder()
        if embedder is None:
            return None

        embs = list(embedder.embed([content]))  # type: ignore[union-attr]
        if not embs:
            return None

        # numpy 矩阵运算（比 Python 循环快 100x）
        try:
            import numpy as np
            vec    = np.asarray(embs[0], dtype=np.float32)         # (dim,)
            W      = np.asarray(weights, dtype=np.float32)          # (n_classes, dim)
            b      = np.asarray(biases,  dtype=np.float32)          # (n_classes,)
            scores = W @ vec + b                                     # (n_classes,)
            # 数值稳定 softmax
            exp_s  = np.exp(scores - scores.max())
            probs  = exp_s / exp_s.sum()
            best   = int(np.argmax(probs))
            prob_best = float(probs[best])
        except ImportError:
            # numpy 未安装时退回 Python 循环（功能等价，性能较慢）
            import math
            n_classes = len(weights)
            scores = [sum(w * v for w, v in zip(weights[c], embs[0])) + biases[c]
                      for c in range(n_classes)]
            max_s  = max(scores)
            exp_s  = [math.exp(s - max_s) for s in scores]
            total  = sum(exp_s)
            probs_list = [e / total for e in exp_s]
            best   = max(range(n_classes), key=lambda i: probs_list[i])
            prob_best = probs_list[best]

        LABEL_NAMES = ["constraint", "preference", "semantic", "procedural", "episodic", "no_write"]
        IMPORTANCES = [1.0, 0.85, 0.70, 0.80, 0.50, 0.0]
        label = LABEL_NAMES[best] if best < len(LABEL_NAMES) else "episodic"
        return {
            "label":        label,
            "confidence":   prob_best,
            "should_write": label != "no_write",
            "importance":   IMPORTANCES[best] if best < len(IMPORTANCES) else 0.5,
            "source":       "sklearn_model",
        }
    except Exception as e:
        log.debug("Model classify failed: %s", e)
        return None


def _train_sklearn_classifier() -> dict:
    """
    用 Python scikit-learn 逻辑回归分类器训练 MemRouter 模型。
    优化：
      - 复用 _get_embedder() 缓存（首次初始化后复用）
      - 使用 numpy 矩阵运算（训练速度比纯 Python 循环快 100x）
      - 模型保存为 JSON（weights/biases），与 Rust trainer.rs 格式兼容
    替代原来规划的 Rust candle 方案（更简单，无需 GPU，无需 ONNX 导出）。
    """
    try:
        embedder = _get_embedder()
        if embedder is None:
            return {"ok": False, "reason": "fastembed not available"}

        labels_data = [json.loads(l) for l in _LABELS_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
        if not labels_data:
            return {"ok": False, "reason": "No training data"}

        texts = [d["text"] for d in labels_data]
        y     = [d["label_idx"] for d in labels_data]
        n     = len(texts)

        # 生成嵌入向量（使用缓存模型，不需要 E5 前缀）
        embs     = list(embedder.embed(texts))  # type: ignore[union-attr]
        dim      = len(embs[0]) if embs else 384

        # numpy 加速的逻辑回归训练（比 Python 循环快 100x）
        try:
            import numpy as np
            x_mat  = np.asarray(embs, dtype=np.float32)       # (n, dim)
            y_arr  = np.asarray(y,    dtype=np.int32)          # (n,)
            n_classes = 6
            w_mat  = np.zeros((n_classes, dim), dtype=np.float32)
            b_arr  = np.zeros(n_classes,        dtype=np.float32)
            lr = 0.01

            for _ in range(200):
                # Forward pass: (n, n_classes)
                logits = x_mat @ w_mat.T + b_arr
                # 数值稳定 softmax
                logits -= logits.max(axis=1, keepdims=True)
                exp_l   = np.exp(logits)
                probs   = exp_l / exp_l.sum(axis=1, keepdims=True)
                # One-hot labels
                one_hot = np.zeros_like(probs)
                one_hot[np.arange(n), y_arr] = 1.0
                # Gradient
                delta = (probs - one_hot) / n
                w_mat -= lr * (delta.T @ x_mat)
                b_arr -= lr * delta.sum(axis=0)

            # 计算训练准确率
            logits_final = x_mat @ w_mat.T + b_arr
            preds    = np.argmax(logits_final, axis=1)
            accuracy = float((preds == y_arr).mean())
            weights  = w_mat.tolist()
            biases   = b_arr.tolist()

        except ImportError:
            # numpy 未安装时退回 Python 循环（功能等价，速度慢）
            import math
            n_classes = 6
            w = [[0.0] * dim for _ in range(n_classes)]
            b_list = [0.0] * n_classes
            lr = 0.01
            emb_lists = [list(e) for e in embs]

            for _ in range(200):
                for i, (emb, lbl) in enumerate(zip(emb_lists, y)):
                    scores = [sum(w[c][j] * emb[j] for j in range(dim)) + b_list[c]
                              for c in range(n_classes)]
                    max_s  = max(scores)
                    exp_s  = [math.exp(s - max_s) for s in scores]
                    total  = sum(exp_s)
                    probs  = [e / total for e in exp_s]
                    for c in range(n_classes):
                        err = probs[c] - (1.0 if c == lbl else 0.0)
                        for j in range(dim):
                            w[c][j] -= lr * err * emb[j] / n
                        b_list[c] -= lr * err / n

            correct = sum(1 for emb, lbl in zip(emb_lists, y) if
                          max(range(n_classes), key=lambda c: sum(w[c][j]*emb[j] for j in range(dim))+b_list[c]) == lbl)
            accuracy = correct / n
            weights  = w
            biases   = b_list

        model_data = {
            "weights":    weights,
            "biases":     biases,
            "train_size": n,
            "accuracy":   accuracy,
            "dim":        dim,
            "trained_at": datetime.now(timezone.utc).isoformat(),
        }
        _MODEL_FILE.parent.mkdir(parents=True, exist_ok=True)
        _MODEL_FILE.write_text(json.dumps(model_data), encoding="utf-8")

        log.info("MemRouter trained: n=%d accuracy=%.1f%% dim=%d", n, accuracy * 100, dim)
        return {"ok": True, "accuracy": accuracy, "train_size": n, "dim": dim}
    except Exception as e:
        log.exception("MemRouter training failed")
        return {"ok": False, "reason": str(e)}




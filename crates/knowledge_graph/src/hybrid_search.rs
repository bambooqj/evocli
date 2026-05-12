//! Hybrid search: BM25 + vector (RRF merging)
//!
//! GitNexus 对应: src/core/search/ (hybrid BM25 + semantic + RRF)
//! RRF = Reciprocal Rank Fusion: score = Σ 1/(k + rank_i), k 可在 config.toml 配置
//!
//! EvoCLI 实现:
//!   - BM25 部分: tantivy (本文件)
//!   - Vector 部分: Python LanceDB + fastembed (跨进程)
//!   - RRF 合并: 在 Rust 侧完成（权重可通过 GraphConfig 配置）

use super::bm25_index::Bm25Result;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

/// RRF K 值默认值（可通过 config.toml [graph] rrf_k 覆盖）
const DEFAULT_RRF_K: f32 = 60.0;
/// BM25 权重默认值（可通过 config.toml [graph] bm25_weight 覆盖）
const DEFAULT_BM25_WEIGHT: f32 = 0.4;
/// Vector 权重默认值（可通过 config.toml [graph] vector_weight 覆盖）
const DEFAULT_VECTOR_WEIGHT: f32 = 0.6;

/// Hybrid search result after RRF fusion
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HybridResult {
    pub symbol_id: String,
    pub name: String,
    pub kind: String,
    pub file: String,
    pub signature: String,
    /// Combined RRF score (higher = more relevant)
    pub rrf_score: f32,
    /// Individual scores for transparency
    pub bm25_rank: Option<usize>,
    pub vector_rank: Option<usize>,
}

/// 可配置的 RRF 参数（从 config.toml [graph] 读取）
#[derive(Debug, Clone)]
pub struct RrfConfig {
    /// K 值，越大分数分布越平缓（默认 60）
    pub k: f32,
    /// BM25 结果权重（默认 0.4）
    pub bm25_weight: f32,
    /// 向量搜索结果权重（默认 0.6）
    pub vector_weight: f32,
}

impl Default for RrfConfig {
    fn default() -> Self {
        Self {
            k: DEFAULT_RRF_K,
            bm25_weight: DEFAULT_BM25_WEIGHT,
            vector_weight: DEFAULT_VECTOR_WEIGHT,
        }
    }
}

/// Merge BM25 results with vector search results using Reciprocal Rank Fusion.
///
/// RRF formula (configurable K, default=60):
///   score(d) = bm25_weight * 1/(K + bm25_rank) + vector_weight * 1/(K + vector_rank)
///
/// 权重可在 config.toml [graph] 中配置：rrf_k / bm25_weight / vector_weight
pub fn rrf_merge(
    bm25_results: &[Bm25Result],
    vector_results: &[VectorResult],
    limit: usize,
) -> Vec<HybridResult> {
    rrf_merge_with_config(bm25_results, vector_results, limit, &RrfConfig::default())
}

/// 带自定义配置的 RRF 合并（供 tool_dispatch 从 Config 读取参数后调用）
pub fn rrf_merge_with_config(
    bm25_results: &[Bm25Result],
    vector_results: &[VectorResult],
    limit: usize,
    cfg: &RrfConfig,
) -> Vec<HybridResult> {
    let mut scores: HashMap<String, f32> = HashMap::new();

    // Accumulate BM25 contributions (weighted)
    for r in bm25_results {
        *scores.entry(r.symbol_id.clone()).or_default() +=
            cfg.bm25_weight / (cfg.k + r.rank as f32);
    }
    // Accumulate vector contributions (weighted)
    for r in vector_results {
        *scores.entry(r.symbol_id.clone()).or_default() +=
            cfg.vector_weight / (cfg.k + r.rank as f32);
    }

    // Build result map for metadata lookup
    let bm25_map: HashMap<&str, &Bm25Result> = bm25_results
        .iter()
        .map(|r| (r.symbol_id.as_str(), r))
        .collect();
    let vec_map: HashMap<&str, &VectorResult> = vector_results
        .iter()
        .map(|r| (r.symbol_id.as_str(), r))
        .collect();

    // Sort by RRF score descending
    let mut ranked: Vec<(String, f32)> = scores.into_iter().collect();
    ranked.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
    ranked.truncate(limit);

    ranked
        .into_iter()
        .map(|(id, rrf_score)| {
            let bm = bm25_map.get(id.as_str());
            let vr = vec_map.get(id.as_str());
            HybridResult {
                symbol_id: id.clone(),
                name: bm
                    .map(|r| r.name.clone())
                    .or_else(|| vr.map(|r| r.name.clone()))
                    .unwrap_or_default(),
                kind: bm.map(|r| r.kind.clone()).unwrap_or_default(),
                file: bm
                    .map(|r| r.file.clone())
                    .or_else(|| vr.map(|r| r.file.clone()))
                    .unwrap_or_default(),
                signature: bm.map(|r| r.signature.clone()).unwrap_or_default(),
                rrf_score,
                bm25_rank: bm.map(|r| r.rank),
                vector_rank: vr.map(|r| r.rank),
            }
        })
        .collect()
}

/// Vector search result from Python LanceDB (passed via JSON RPC)
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VectorResult {
    pub symbol_id: String,
    pub name: String,
    pub file: String,
    pub score: f32,
    pub rank: usize,
}

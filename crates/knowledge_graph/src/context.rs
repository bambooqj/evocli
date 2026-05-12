//! Symbol context — 360° view (GitNexus context tool)

use super::graph_analysis::KnowledgeGraph;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SymbolContext {
    pub symbol_id: String,
    pub name: String,
    pub kind: String,
    pub file: String,
    pub callers: Vec<serde_json::Value>,
    pub callees: Vec<serde_json::Value>,
}

pub fn get_symbol_context(graph: &KnowledgeGraph, symbol_id: &str) -> Option<serde_json::Value> {
    graph.symbol_360_context(symbol_id)
}

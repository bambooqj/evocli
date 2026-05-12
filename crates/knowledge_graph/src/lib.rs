//! EvoCLI Knowledge Graph — GitNexus-inspired built-in code intelligence
//!
//! 借鉴 GitNexus (github.com/abhigyanpatwari/GitNexus) 的核心能力，
//! 内置到 EvoCLI 作为工具自身特性（非外部 MCP 接入）。
//!
//! ## GitNexus 对应实现
//!
//! | GitNexus 功能          | EvoCLI 实现                        |
//! |------------------------|-------------------------------------|
//! | LadybugDB              | SQLite (code_index.db + 扩展表)      |
//! | BM25 hybrid search     | tantivy 嵌入式引擎                   |
//! | Community detection    | petgraph Louvain 算法               |
//! | Blast radius / impact  | petgraph BFS on call edges          |
//! | Process/execution flow | 从入口函数追踪调用链                  |
//! | Symbol context 360°    | callers + callees + community + proc |
//! | AGENTS.md / wiki       | Python wiki_generator.py + LLM      |
//!
//! ## 架构
//!
//! ```text
//! symbols + edges (SQLite, code_intel)
//!     | load_graph()
//! petgraph::DiGraph (in-memory)
//!     | louvain()              | bfs_blast_radius()
//! Community nodes          Upstream/Downstream sets
//!     |
//! tantivy BM25 index
//!     | query()
//! Ranked symbol results
//!     | rrf_merge()
//! Hybrid search results (BM25 + vector from Python LanceDB)
//! ```

pub mod bm25_index;
pub mod context;
pub mod graph_analysis;
pub mod hybrid_search;
pub mod wiki;

pub use bm25_index::Bm25Index;
pub use context::SymbolContext;
pub use graph_analysis::{BlastRadius, Community, ExecutionFlow, KnowledgeGraph};

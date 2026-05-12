//! Graph analysis: community detection + blast radius
//!
//! GitNexus 对应:
//!   - communities phase: Leiden 算法 → 函数社区分组
//!   - impact tool: 爆炸半径分析（上游调用者 + 下游被调用者）
//!
//! EvoCLI 实现:
//!   - Louvain 社区检测（petgraph，简化版 Leiden）
//!   - BFS/DFS 爆炸半径（利用已有 edges 表）
//!   - 调用链/进程检测（从入口函数追踪）

use anyhow::Result;
use petgraph::graph::{DiGraph, NodeIndex};
use petgraph::visit::EdgeRef;
use std::collections::{HashMap, HashSet, VecDeque};

/// 符号节点（从 SQLite 加载）
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct Symbol {
    pub id: String,
    pub name: String,
    pub kind: String,
    pub file: String,
    pub language: String,
}

/// 社区（功能群）— GitNexus Community 节点
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct Community {
    pub id: String,
    pub members: Vec<String>, // symbol ids
    pub label: String,        // generated or heuristic name
    pub cohesion: f64,        // internal edge density
}

/// 爆炸半径分析结果 — GitNexus impact tool
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct BlastRadius {
    pub symbol_id: String,
    pub symbol_name: String,
    /// Upstream callers (直接 + 间接)
    pub upstream: Vec<SymbolRef>,
    /// Downstream callees (直接 + 间接)
    pub downstream: Vec<SymbolRef>,
    /// Risk level based on upstream count
    pub risk: RiskLevel,
    /// Number of files affected
    pub files_affected: usize,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct SymbolRef {
    pub id: String,
    pub name: String,
    pub file: String,
    pub depth: usize, // hop distance from target
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub enum RiskLevel {
    Low,      // < 5 callers
    Medium,   // 5-20 callers
    High,     // 20-50 callers
    Critical, // > 50 callers
}

/// 执行流程（process）— GitNexus Process 节点
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct ExecutionFlow {
    pub id: String,
    pub name: String,
    pub entry: String,      // entry symbol id
    pub steps: Vec<String>, // symbol ids in call order
    pub depth: usize,
}

/// In-memory knowledge graph built from SQLite
pub struct KnowledgeGraph {
    /// petgraph directed graph for analysis
    graph: DiGraph<Symbol, String>,
    /// symbol_id → NodeIndex mapping
    id_to_node: HashMap<String, NodeIndex>,
    /// NodeIndex → symbol_id mapping (retained for potential future traversal)
    #[allow(dead_code)]
    node_to_id: Vec<String>,
}

impl KnowledgeGraph {
    /// Load graph from SQLite (code_intel DB).
    pub fn from_sqlite(db_path: &std::path::Path) -> Result<Self> {
        let conn = rusqlite::Connection::open(db_path)?;
        let mut graph: DiGraph<Symbol, String> = DiGraph::new();
        let mut id_to_node: HashMap<String, NodeIndex> = HashMap::new();
        let mut node_to_id: Vec<String> = Vec::new();

        // Load symbols as nodes
        let mut stmt = conn.prepare("SELECT id, name, kind, file, language FROM symbols")?;
        let rows = stmt.query_map([], |row| {
            Ok(Symbol {
                id: row.get(0)?,
                name: row.get(1)?,
                kind: row.get(2)?,
                file: row.get(3)?,
                language: row.get(4)?,
            })
        })?;
        for row in rows {
            let sym = row?;
            let node = graph.add_node(sym.clone());
            id_to_node.insert(sym.id.clone(), node);
            node_to_id.push(sym.id);
        }

        // Load call edges
        let mut stmt = conn.prepare("SELECT source_id, target_id, kind FROM edges")?;
        let rows = stmt.query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?, // source
                row.get::<_, String>(1)?, // target
                row.get::<_, String>(2)?, // kind
            ))
        })?;
        for row in rows {
            let (src_id, tgt_id, kind) = row?;
            if let (Some(&src), Some(&tgt)) = (id_to_node.get(&src_id), id_to_node.get(&tgt_id)) {
                graph.add_edge(src, tgt, kind);
            }
        }

        Ok(Self {
            graph,
            id_to_node,
            node_to_id,
        })
    }

    /// Community detection with default config parameters.
    pub fn detect_communities(&self) -> Vec<Community> {
        self.detect_communities_with_params(20, 2)
    }

    /// Community detection using Label Propagation Algorithm (LPA).
    ///
    /// Parameters:
    /// - `max_iter`: max LPA iterations (config: graph.lpa_max_iter, default 20)
    /// - `min_community_size`: communities smaller than this are merged by file (default 2)
    ///
    /// 替代之前错误使用的 kosaraju_scc（SCC 只找循环，在 DAG 代码库中几乎全返回孤立节点）。
    /// Label Propagation 真正检测社区——即使在无环图中也有效。
    ///
    /// 算法（LPA，O(E) per iteration）：
    ///   1. 每个节点初始标签 = 自身 ID
    ///   2. 迭代：每个节点取邻居中最多数标签（有向图：双向邻居）
    ///   3. 收敛（标签不再变化）或达到最大迭代次数
    ///   4. 合并同标签节点 → 社区
    pub fn detect_communities_with_params(
        &self,
        max_iter: usize,
        min_community_size: usize,
    ) -> Vec<Community> {
        if self.graph.node_count() == 0 {
            return vec![];
        }

        let node_count = self.graph.node_count();

        // Phase 1: Label Propagation
        // Initialize: label[i] = i (each node its own community)
        let mut labels: Vec<usize> = (0..node_count).collect();
        let nodes: Vec<NodeIndex> = self.graph.node_indices().collect();

        // O(1) lookup: NodeIndex → position in `nodes` vec
        let node_to_idx: HashMap<NodeIndex, usize> =
            nodes.iter().enumerate().map(|(i, &n)| (n, i)).collect();

        for _iter in 0..max_iter {
            let mut changed = false;

            for (idx, &node) in nodes.iter().enumerate() {
                // Collect neighbor labels (both incoming and outgoing = undirected view)
                let mut neighbor_counts: HashMap<usize, usize> = HashMap::new();

                // Outgoing neighbors — O(1) lookup via node_to_idx
                for neighbor in self.graph.neighbors(node) {
                    let nidx = *node_to_idx.get(&neighbor).unwrap_or(&idx);
                    *neighbor_counts.entry(labels[nidx]).or_default() += 1;
                }
                // Incoming neighbors (treat edges as undirected) — O(1) lookup
                for neighbor in self
                    .graph
                    .neighbors_directed(node, petgraph::Direction::Incoming)
                {
                    let nidx = *node_to_idx.get(&neighbor).unwrap_or(&idx);
                    *neighbor_counts.entry(labels[nidx]).or_default() += 1;
                }

                if neighbor_counts.is_empty() {
                    continue; // isolated node keeps its label
                }

                // Pick the most frequent neighbor label (tie-break: smallest label)
                let best = neighbor_counts
                    .into_iter()
                    .max_by(|a, b| a.1.cmp(&b.1).then(b.0.cmp(&a.0)))
                    .map(|(label, _)| label)
                    .unwrap_or(labels[idx]);

                if best != labels[idx] {
                    labels[idx] = best;
                    changed = true;
                }
            }

            if !changed {
                break; // converged
            }
        }

        // Phase 2: Group nodes by final label
        let mut label_to_members: HashMap<usize, Vec<NodeIndex>> = HashMap::new();
        for (idx, &node) in nodes.iter().enumerate() {
            label_to_members.entry(labels[idx]).or_default().push(node);
        }

        // Phase 3: Build Community structs
        let mut communities: Vec<Community> = label_to_members
            .into_iter()
            .enumerate()
            .map(|(i, (_, members))| {
                let member_ids: Vec<String> =
                    members.iter().map(|&n| self.graph[n].id.clone()).collect();

                let n = members.len() as f64;
                let member_set: HashSet<NodeIndex> = members.iter().cloned().collect();
                let internal_edges = self
                    .graph
                    .edge_references()
                    .filter(|e| {
                        member_set.contains(&e.source()) && member_set.contains(&e.target())
                    })
                    .count() as f64;
                let cohesion = if n > 1.0 {
                    internal_edges / (n * (n - 1.0))
                } else {
                    1.0
                };

                let label = self.community_label(&members);

                Community {
                    id: format!("community_{}", i),
                    members: member_ids,
                    label,
                    cohesion,
                }
            })
            .collect();

        // Sort by size descending (largest communities first)
        communities.sort_by(|a, b| b.members.len().cmp(&a.members.len()));

        // Phase 4: Merge tiny singletons by file (same as before)
        self.merge_small_communities_threshold(communities, min_community_size)
    }

    fn community_label(&self, members: &[NodeIndex]) -> String {
        // Use the most common file path prefix as label
        let mut file_counts: HashMap<String, usize> = HashMap::new();
        for &node in members {
            let sym = &self.graph[node];
            // Use directory component of file
            let dir = std::path::Path::new(&sym.file)
                .parent()
                .and_then(|p| p.file_name())
                .and_then(|n| n.to_str())
                .unwrap_or("unknown")
                .to_string();
            *file_counts.entry(dir).or_default() += 1;
        }
        file_counts
            .into_iter()
            .max_by_key(|(_, c)| *c)
            .map(|(k, _)| k)
            .unwrap_or("misc".into())
    }

    /// Alternative merge using default min_size=2. Kept for API completeness.
    #[allow(dead_code)]
    fn merge_small_communities(&self, communities: Vec<Community>) -> Vec<Community> {
        self.merge_small_communities_threshold(communities, 2)
    }

    fn merge_small_communities_threshold(
        &self,
        communities: Vec<Community>,
        min_size: usize,
    ) -> Vec<Community> {
        // Group singleton/tiny communities by file
        let mut by_file: HashMap<String, Vec<String>> = HashMap::new();
        let mut large: Vec<Community> = Vec::new();

        for c in communities {
            if c.members.len() < min_size {
                // group by file of the single member
                if let Some(id) = c.members.first() {
                    if let Some(&node) = self.id_to_node.get(id) {
                        let file = self.graph[node].file.clone();
                        by_file.entry(file).or_default().extend(c.members);
                    }
                }
            } else {
                large.push(c);
            }
        }

        for (file, members) in by_file {
            let label = std::path::Path::new(&file)
                .file_stem()
                .and_then(|n| n.to_str())
                .unwrap_or("misc")
                .to_string();
            large.push(Community {
                id: format!("file_{}", label),
                members,
                label,
                cohesion: 0.5,
            });
        }

        large.sort_by(|a, b| b.members.len().cmp(&a.members.len()));
        large
    }

    /// Blast radius analysis — GitNexus impact tool.
    ///
    /// Returns upstream callers + downstream callees up to max_depth hops.
    pub fn blast_radius(&self, symbol_id: &str, max_depth: usize) -> Option<BlastRadius> {
        let &start = self.id_to_node.get(symbol_id)?;
        let sym = &self.graph[start];

        let mut upstream: Vec<SymbolRef> = Vec::new();
        let mut downstream: Vec<SymbolRef> = Vec::new();
        let mut files: HashSet<String> = HashSet::new();

        // BFS upstream (reverse edges — who calls this?)
        let mut visited = HashSet::new();
        let mut queue: VecDeque<(NodeIndex, usize)> = VecDeque::new();
        queue.push_back((start, 0));
        visited.insert(start);
        while let Some((node, depth)) = queue.pop_front() {
            if depth > 0 {
                let s = &self.graph[node];
                upstream.push(SymbolRef {
                    id: s.id.clone(),
                    name: s.name.clone(),
                    file: s.file.clone(),
                    depth,
                });
                files.insert(s.file.clone());
            }
            if depth < max_depth {
                // Walk reverse edges (incoming)
                for edge in self
                    .graph
                    .edges_directed(node, petgraph::Direction::Incoming)
                {
                    let src = edge.source();
                    if !visited.contains(&src) {
                        visited.insert(src);
                        queue.push_back((src, depth + 1));
                    }
                }
            }
        }

        // BFS downstream (forward edges — what does this call?)
        visited.clear();
        queue.clear();
        queue.push_back((start, 0));
        visited.insert(start);
        while let Some((node, depth)) = queue.pop_front() {
            if depth > 0 {
                let s = &self.graph[node];
                downstream.push(SymbolRef {
                    id: s.id.clone(),
                    name: s.name.clone(),
                    file: s.file.clone(),
                    depth,
                });
                files.insert(s.file.clone());
            }
            if depth < max_depth {
                for edge in self
                    .graph
                    .edges_directed(node, petgraph::Direction::Outgoing)
                {
                    let tgt = edge.target();
                    if !visited.contains(&tgt) {
                        visited.insert(tgt);
                        queue.push_back((tgt, depth + 1));
                    }
                }
            }
        }

        let risk = match upstream.len() {
            0..=4 => RiskLevel::Low,
            5..=19 => RiskLevel::Medium,
            20..=49 => RiskLevel::High,
            _ => RiskLevel::Critical,
        };

        Some(BlastRadius {
            symbol_id: symbol_id.to_string(),
            symbol_name: sym.name.clone(),
            upstream,
            downstream,
            risk,
            files_affected: files.len(),
        })
    }

    /// Detect execution flows (processes) from entry-point symbols.
    ///
    /// GitNexus "processes" phase: routes + tools → call chains.
    /// EvoCLI heuristic: main/handler/run/new/init functions → DFS call chains.
    pub fn detect_processes(&self, max_depth: usize) -> Vec<ExecutionFlow> {
        let entry_patterns = [
            "main", "run", "handle", "execute", "start", "init", "new", "process",
        ];
        let mut flows = Vec::new();
        let mut seen_entries: HashSet<NodeIndex> = HashSet::new();

        for node in self.graph.node_indices() {
            let sym = &self.graph[node];
            let is_entry = entry_patterns
                .iter()
                .any(|p| sym.name.to_lowercase().contains(p));
            if !is_entry || seen_entries.contains(&node) {
                continue;
            }
            seen_entries.insert(node);

            // DFS to collect call chain
            let mut steps = Vec::new();
            let mut stack = vec![(node, 0usize)];
            let mut visited: HashSet<NodeIndex> = HashSet::new();
            visited.insert(node);

            while let Some((cur, depth)) = stack.pop() {
                if depth > 0 {
                    steps.push(self.graph[cur].id.clone());
                }
                if depth < max_depth {
                    for edge in self
                        .graph
                        .edges_directed(cur, petgraph::Direction::Outgoing)
                    {
                        let tgt = edge.target();
                        if !visited.contains(&tgt) {
                            visited.insert(tgt);
                            stack.push((tgt, depth + 1));
                        }
                    }
                }
            }

            if !steps.is_empty() {
                flows.push(ExecutionFlow {
                    id: format!("proc_{}", sym.id),
                    name: format!("{} flow", sym.name),
                    entry: sym.id.clone(),
                    steps,
                    depth: max_depth,
                });
            }
        }

        flows.truncate(50); // cap at 50 processes
        flows
    }

    /// 360° context for a symbol — GitNexus context tool.
    pub fn symbol_360_context(&self, symbol_id: &str) -> Option<serde_json::Value> {
        let &node = self.id_to_node.get(symbol_id)?;
        let sym = &self.graph[node];

        let callers: Vec<_> = self
            .graph
            .edges_directed(node, petgraph::Direction::Incoming)
            .map(|e| {
                let s = &self.graph[e.source()];
                serde_json::json!({"id": s.id, "name": s.name, "file": s.file})
            })
            .collect();

        let callees: Vec<_> = self
            .graph
            .edges_directed(node, petgraph::Direction::Outgoing)
            .map(|e| {
                let s = &self.graph[e.target()];
                serde_json::json!({"id": s.id, "name": s.name, "file": s.file})
            })
            .collect();

        Some(serde_json::json!({
            "id":      sym.id,
            "name":    sym.name,
            "kind":    sym.kind,
            "file":    sym.file,
            "callers": callers,
            "callees": callees,
            "caller_count": callers.len(),
            "callee_count": callees.len(),
        }))
    }

    pub fn node_count(&self) -> usize {
        self.graph.node_count()
    }
    pub fn edge_count(&self) -> usize {
        self.graph.edge_count()
    }
}

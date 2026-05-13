// EvoCLI Code Intelligence
pub mod file_watcher;
//
// Layer 1: tree-sitter AST-based symbol indexing (primary, accurate)
//          Research: Aider replaced ctags with tree-sitter for "richer symbol data"
//          tree-sitter used by GitHub, Neovim, VS Code — industry standard
//          Fallback: regex patterns for languages without tree-sitter grammar
// Layer 2: LSP client for call-hierarchy / references / goto-definition

pub mod lsp_client;
pub mod lsp_manager;
pub mod ts_indexer; // tree-sitter based indexer

pub use lsp_manager::{FunctionAnalysis, Language, LspManager};

use anyhow::{Context, Result};
use chrono::Utc;
use ignore::Walk;
use regex::Regex;
use rusqlite::{params, Connection};
use std::path::Path;
use uuid::Uuid;

// ── Public types ─────────────────────────────────────────────────

pub struct CodeIndex {
    conn: Connection,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct SymbolInfo {
    pub id: String,
    pub name: String,
    pub kind: String,
    pub file: String,
    pub line: u32,
    /// End line of the symbol body (0 = unknown). Used for semantic chunk extraction.
    #[serde(default)]
    pub line_end: u32,
    pub signature: Option<String>,
    pub language: String,
}

// ── Schema ───────────────────────────────────────────────────────

const SCHEMA: &str = r#"
CREATE TABLE IF NOT EXISTS symbols (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL,
    file       TEXT NOT NULL,
    line       INTEGER NOT NULL,
    line_end   INTEGER NOT NULL DEFAULT 0,
    signature  TEXT,
    language   TEXT NOT NULL,
    body_hash  TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file);
"#;

const SCHEMA_EDGES: &str = r#"
CREATE TABLE IF NOT EXISTS edges (
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    kind      TEXT NOT NULL,
    file      TEXT NOT NULL,
    line      INTEGER,
    PRIMARY KEY (source_id, target_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id, kind);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id, kind);
"#;

// ── Regex patterns (compiled once via OnceLock) ─────────────────

struct LangPatterns {
    language: &'static str,
    extensions: &'static [&'static str],
    patterns: Vec<(&'static str, Regex)>, // (kind, regex)
}

// SAFETY: Regex is Send+Sync; LangPatterns fields are all 'static or Vec.
// OnceLock ensures compile-once semantics across all calls to index_file().
static LANG_PATTERNS: std::sync::OnceLock<Vec<LangPatterns>> = std::sync::OnceLock::new();

fn get_patterns() -> &'static Vec<LangPatterns> {
    LANG_PATTERNS.get_or_init(build_patterns)
}

fn build_patterns() -> Vec<LangPatterns> {
    vec![
        LangPatterns {
            language: "rust",
            extensions: &["rs"],
            patterns: vec![
                ("function", Regex::new(r"(?m)^\s*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)\s*[\(<]").unwrap()),
                ("struct",   Regex::new(r"(?m)^\s*(?:pub\s+)?struct\s+(\w+)").unwrap()),
                ("enum",     Regex::new(r"(?m)^\s*(?:pub\s+)?enum\s+(\w+)").unwrap()),
                ("trait",    Regex::new(r"(?m)^\s*(?:pub\s+)?trait\s+(\w+)").unwrap()),
                ("impl",     Regex::new(r"(?m)^\s*impl(?:<[^>]*>)?\s+(\w+)").unwrap()),
            ],
        },
        LangPatterns {
            language: "python",
            extensions: &["py"],
            patterns: vec![
                ("function", Regex::new(r"(?m)^\s*(?:async\s+)?def\s+(\w+)\s*\(").unwrap()),
                ("class",    Regex::new(r"(?m)^\s*class\s+(\w+)").unwrap()),
            ],
        },
        LangPatterns {
            language: "typescript",
            extensions: &["ts", "tsx", "js", "jsx"],
            patterns: vec![
                ("function", Regex::new(r"(?m)^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*[\(<]").unwrap()),
                ("function", Regex::new(r"(?m)^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?(?:\([^)]*\)|[a-zA-Z_]\w*)\s*=>").unwrap()),
                ("class",    Regex::new(r"(?m)^\s*(?:export\s+)?class\s+(\w+)").unwrap()),
                ("interface",Regex::new(r"(?m)^\s*(?:export\s+)?interface\s+(\w+)").unwrap()),
            ],
        },
    ]
}

// ── Implementation ───────────────────────────────────────────────

impl CodeIndex {
    /// Open (or create) a symbol index at the given path.
    pub fn new(db_path: &Path) -> Result<Self> {
        let conn = Connection::open(db_path)
            .with_context(|| format!("Failed to open code_intel DB: {}", db_path.display()))?;
        conn.execute_batch(SCHEMA)?;
        conn.execute_batch(SCHEMA_EDGES)?;
        // Migration: add columns for old schemas
        let _ = conn
            .execute_batch("ALTER TABLE symbols ADD COLUMN line_end INTEGER NOT NULL DEFAULT 0");
        let _ =
            conn.execute_batch("ALTER TABLE symbols ADD COLUMN body_hash TEXT NOT NULL DEFAULT ''");
        Ok(Self { conn })
    }

    /// Open an in-memory index (useful for tests).
    pub fn in_memory() -> Result<Self> {
        let conn = Connection::open_in_memory()?;
        conn.execute_batch(SCHEMA)?;
        conn.execute_batch(SCHEMA_EDGES)?;
        Ok(Self { conn })
    }

    /// Count total indexed symbols (uses existing connection — avoids double-open).
    pub fn count_symbols(&self) -> i64 {
        self.conn
            .query_row("SELECT COUNT(*) FROM symbols", [], |r| r.get(0))
            .unwrap_or(0)
    }

    /// Count total call-graph edges (uses existing connection — avoids double-open).
    pub fn count_edges(&self) -> i64 {
        self.conn
            .query_row("SELECT COUNT(*) FROM edges", [], |r| r.get(0))
            .unwrap_or(0)
    }

    /// Index a single source file. Returns the number of symbols extracted.
    ///
    /// Strategy (research-backed):
    ///   Primary:  ts_indexer::extract_symbols() using tree-sitter AST queries
    ///             Research: Aider switched from ctags to tree-sitter for "richer symbol data"
    ///   Fallback: regex patterns (for unsupported languages or parse failures)
    pub fn index_file(&mut self, file_path: &Path) -> Result<usize> {
        let ext = file_path.extension().and_then(|e| e.to_str()).unwrap_or("");

        let content = std::fs::read_to_string(file_path)
            .with_context(|| format!("Cannot read {}", file_path.display()))?;

        let file_str = file_path.to_string_lossy().replace('\\', "/");
        let now = Utc::now().to_rfc3339();

        // Remove old symbols for this file
        self.conn
            .execute("DELETE FROM symbols WHERE file = ?1", params![file_str])?;

        // ── Layer 1a: tree-sitter (primary, accurate AST-based) ──────
        if let Some(ts_symbols) = crate::ts_indexer::extract_symbols(&content, ext) {
            // Load existing body_hash per symbol name for this file (incremental skip)
            let existing_hashes: std::collections::HashMap<String, (String, String)> = {
                let mut stmt = self
                    .conn
                    .prepare("SELECT name, id, body_hash FROM symbols WHERE file = ?1")?;
                let mut map = std::collections::HashMap::new();
                let mut rows = stmt.query(params![file_str])?;
                while let Some(row) = rows.next()? {
                    let name: String = row.get(0)?;
                    let id: String = row.get(1)?;
                    let hash: String = row.get(2).unwrap_or_default();
                    map.insert(name, (id, hash));
                }
                map
            };

            let tx = self.conn.transaction()?;
            let content_lines: Vec<&str> = content.lines().collect();
            let mut count = 0usize;
            let mut changed_symbols: Vec<String> = Vec::new(); // IDs needing edge rebuild

            for sym in &ts_symbols {
                let line_idx = (sym.line.saturating_sub(1)) as usize;
                let signature = content_lines
                    .get(line_idx)
                    .unwrap_or(&"")
                    .trim()
                    .to_string();

                // Extract body for hash: from line_start to line_end (or line_start+50 estimate)
                let body_end = if sym.line_end > sym.line {
                    (sym.line_end as usize).min(content_lines.len())
                } else {
                    (line_idx + 50).min(content_lines.len())
                };
                let body_text = content_lines[line_idx..body_end].join("\n");

                // SHA-256 of body content (use simple hash via std)
                let body_hash = {
                    use std::collections::hash_map::DefaultHasher;
                    use std::hash::{Hash, Hasher};
                    let mut h = DefaultHasher::new();
                    body_text.hash(&mut h);
                    format!("{:x}", h.finish())
                };

                // Check if this symbol is unchanged (same name + same hash)
                if let Some((existing_id, existing_hash)) = existing_hashes.get(&sym.name) {
                    if *existing_hash == body_hash {
                        // Symbol unchanged — skip re-insert and edge rebuild
                        count += 1;
                        continue;
                    }
                    // Changed — delete old entry so we can re-insert with new UUID
                    tx.execute("DELETE FROM symbols WHERE id = ?1", params![existing_id])?;
                    tx.execute(
                        "DELETE FROM edges WHERE source_id = ?1 OR target_id = ?1",
                        params![existing_id],
                    )?;
                }

                let id = Uuid::new_v4().to_string();
                tx.execute(
                    "INSERT OR REPLACE INTO symbols (id, name, kind, file, line, line_end, signature, language, body_hash, updated_at)
                     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)",
                    params![id, sym.name, sym.kind, file_str, sym.line, sym.line_end, signature, sym.language, body_hash, now],
                )?;
                changed_symbols.push(sym.name.clone());
                count += 1;
            }
            tx.commit()?;

            // Only rebuild edges for changed symbols (incremental)
            if !changed_symbols.is_empty() {
                self.populate_edges_for_file(file_path, &content, &file_str)?;
            }
            return Ok(count);
        }

        // ── Layer 1b: regex fallback (for unsupported languages) ─────
        let all_patterns = get_patterns(); // compiled once via OnceLock
        let lang = all_patterns.iter().find(|lp| lp.extensions.contains(&ext));
        let lang = match lang {
            Some(l) => l,
            None => return Ok(0),
        };

        let mut count = 0usize;
        let tx = self.conn.transaction()?;

        for (kind, re) in &lang.patterns {
            for mat in re.find_iter(&content) {
                // Determine line number
                let line_num = content[..mat.start()].matches('\n').count() as u32 + 1;

                // Extract the captured name
                let caps = match re.captures(&content[mat.start()..]) {
                    Some(c) => c,
                    None => continue,
                };
                let name = match caps.get(1) {
                    Some(m) => m.as_str().to_string(),
                    None => continue,
                };

                // Extract signature (the full matched line)
                let line_start = content[..mat.start()].rfind('\n').map_or(0, |p| p + 1);
                let line_end = content[mat.start()..]
                    .find('\n')
                    .map_or(content.len(), |p| mat.start() + p);
                let signature = content[line_start..line_end].trim().to_string();

                let id = Uuid::new_v4().to_string();

                tx.execute(
                    "INSERT OR REPLACE INTO symbols (id, name, kind, file, line, signature, language, updated_at)
                     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
                    params![id, name, *kind, file_str, line_num, signature, lang.language, now],
                )?;
                count += 1;
            }
        }
        tx.commit()?;

        // ── Layer 2：填充调用图 edges ──────────────────────────────
        // 对文件中每个已知符号，搜索其他符号的调用模式并写入 edges 表
        self.populate_edges_for_file(file_path, &content, &file_str)?;

        Ok(count)
    }

    /// 填充文件中的调用边（caller → callee）。
    ///
    /// 策略（优先级从高到低）：
    ///   1. tree-sitter AST CallExpression（精确，支持 Rust/Python/JS/TS）
    ///      → 解析真实调用节点，避免注释/字符串中的误报
    ///   2. word-matching 启发式（fallback，覆盖不支持的语言）
    ///      → 同之前的 Phase 1/2/3 实现
    fn populate_edges_for_file(
        &self,
        file_path: &Path,
        content: &str,
        file_str: &str,
    ) -> Result<()> {
        let callers = self.list_symbols(file_path)?;
        if callers.is_empty() {
            return Ok(());
        }
        let ext = file_path.extension().and_then(|e| e.to_str()).unwrap_or("");

        // ── Strategy A: tree-sitter AST call extraction (precise) ────────────────
        if let Some(call_refs) = crate::ts_indexer::extract_calls(content, ext) {
            if !call_refs.is_empty() {
                // Collect unique callee names for targeted SQLite lookup
                let callee_names: std::collections::HashSet<&str> =
                    call_refs.iter().map(|c| c.callee_name.as_str()).collect();
                let names_vec: Vec<&str> = callee_names.into_iter().collect();

                if names_vec.is_empty() {
                    return Ok(());
                }

                // Batch lookup: only resolve names that appear as actual call targets
                let placeholders = names_vec
                    .iter()
                    .enumerate()
                    .map(|(i, _)| format!("?{}", i + 2))
                    .collect::<Vec<_>>()
                    .join(", ");
                let sql = format!(
                    "SELECT id, name, file FROM symbols WHERE file != ?1 AND name IN ({})",
                    placeholders
                );
                let mut stmt = self.conn.prepare(&sql)?;
                let mut params: Vec<Box<dyn rusqlite::ToSql>> = Vec::new();
                params.push(Box::new(file_str.to_string()));
                for n in &names_vec {
                    params.push(Box::new(n.to_string()));
                }
                let param_refs: Vec<&dyn rusqlite::ToSql> =
                    params.iter().map(|b| b.as_ref()).collect();

                let callee_map: std::collections::HashMap<String, String> = stmt
                    .query_map(param_refs.as_slice(), |row| {
                        Ok((row.get::<_, String>(1)?, row.get::<_, String>(0)?))
                        // name → id
                    })?
                    .filter_map(|r| r.ok())
                    .collect();

                if callee_map.is_empty() {
                    return Ok(());
                }

                // Match each call_ref to a caller (by line range) and callee (by name)
                // caller owns a call if the call line falls within [caller.line, caller.line_end or next_symbol.line)
                let mut caller_sorted = callers.clone();
                caller_sorted.sort_by_key(|s| s.line);

                for call in &call_refs {
                    let callee_id = match callee_map.get(&call.callee_name) {
                        Some(id) => id,
                        None => continue,
                    };
                    // Find which caller owns this call line
                    let caller = caller_sorted.iter().rev().find(|s| s.line <= call.line);
                    if let Some(c) = caller {
                        let _ = self.add_edge(&c.id, callee_id, "calls", file_str, call.line);
                    }
                }
                return Ok(());
            }
        }

        // ── Strategy B: word-matching fallback (for unsupported languages) ────────
        // Phase 1: Collect all potential callee names from the file body
        let mut potential_callees: std::collections::HashSet<String> =
            std::collections::HashSet::new();
        for line in content.lines() {
            let chars: Vec<char> = line.chars().collect();
            let mut i = 0usize;
            while i < chars.len() {
                if chars[i].is_alphabetic() || chars[i] == '_' {
                    let start = i;
                    while i < chars.len() && (chars[i].is_alphanumeric() || chars[i] == '_') {
                        i += 1;
                    }
                    if i < chars.len() && chars[i] == '(' {
                        let name: String = chars[start..i].iter().collect();
                        if name.len() >= 3 {
                            potential_callees.insert(name);
                        }
                    }
                } else {
                    i += 1;
                }
            }
        }
        if potential_callees.is_empty() {
            return Ok(());
        }
        let names_vec: Vec<String> = potential_callees.into_iter().collect();
        let placeholders: String = names_vec
            .iter()
            .enumerate()
            .map(|(i, _)| format!("?{}", i + 2))
            .collect::<Vec<_>>()
            .join(", ");
        let sql = format!(
            "SELECT id, name, file, line FROM symbols WHERE file != ?1 AND name IN ({})",
            placeholders
        );
        let mut stmt = self.conn.prepare(&sql)?;
        let mut params_owned: Vec<Box<dyn rusqlite::ToSql>> = Vec::new();
        params_owned.push(Box::new(file_str.to_string()));
        for name in &names_vec {
            params_owned.push(Box::new(name.clone()));
        }
        let param_refs: Vec<&dyn rusqlite::ToSql> =
            params_owned.iter().map(|b| b.as_ref()).collect();
        let matched_callees: Vec<(String, String, String, u32)> = stmt
            .query_map(param_refs.as_slice(), |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, u32>(3)?,
                ))
            })?
            .filter_map(|r| r.ok())
            .collect();
        if matched_callees.is_empty() {
            return Ok(());
        }
        for caller in &callers {
            let line_offset = caller.line.saturating_sub(1) as usize;
            let body: String = content
                .lines()
                .skip(line_offset)
                .take(500)
                .collect::<Vec<_>>()
                .join("\n");
            for (callee_id, callee_name, _callee_file, _callee_line) in &matched_callees {
                let call_pattern = format!("{}(", callee_name);
                if body.contains(call_pattern.as_str()) {
                    let call_line = body
                        .lines()
                        .enumerate()
                        .find(|(_, l)| l.contains(call_pattern.as_str()))
                        .map(|(i, _)| caller.line + i as u32)
                        .unwrap_or(caller.line);
                    let _ = self.add_edge(&caller.id, callee_id, "calls", file_str, call_line);
                }
            }
        }
        Ok(())
    }

    /// Recursively index a directory. Only files matching `extensions` are scanned.
    /// If extensions is empty, all supported extensions are used.
    pub fn index_directory(&mut self, dir: &Path, extensions: &[&str]) -> Result<usize> {
        let all_patterns = build_patterns();
        let supported: Vec<&str> = if extensions.is_empty() {
            all_patterns
                .iter()
                .flat_map(|lp| lp.extensions.iter().copied())
                .collect()
        } else {
            extensions.to_vec()
        };

        let mut total = 0usize;
        // `ignore::Walk` respects .gitignore, .ignore, .git/info/exclude automatically.
        // Research source: ripgrep uses `ignore` crate; Aider respects .gitignore in RepoMap.
        // Previously used `walkdir` with a hardcoded skip list — fragile and misses custom ignore rules.
        for result in Walk::new(dir) {
            let entry = match result {
                Ok(e) => e,
                Err(_) => continue,
            };
            if !entry.file_type().map(|t| t.is_file()).unwrap_or(false) {
                continue;
            }
            let ext = entry
                .path()
                .extension()
                .and_then(|e| e.to_str())
                .unwrap_or("");
            if !supported.contains(&ext) {
                continue;
            }
            total += self.index_file(entry.path())?;
        }
        Ok(total)
    }

    /// Find symbols by exact name.

    /// 直接添加符号（来自 tree-sitter Python 分析结果）
    pub fn add_symbol_direct(
        &mut self,
        name: &str,
        kind: &str,
        file: &str,
        line: u32,
        signature: &str,
        language: &str,
    ) -> Result<()> {
        let id = uuid::Uuid::new_v4().to_string();
        let now = chrono::Utc::now().to_rfc3339();
        self.conn.execute(
            "INSERT OR REPLACE INTO symbols (id, name, kind, file, line, signature, language, updated_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
            rusqlite::params![id, name, kind, file, line, signature, language, now],
        )?;
        Ok(())
    }
    pub fn find_symbol(&self, name: &str) -> Result<Vec<SymbolInfo>> {
        let mut stmt = self.conn.prepare(
            "SELECT id, name, kind, file, line, line_end, signature, language FROM symbols WHERE name = ?1",
        )?;
        let rows = stmt.query_map(params![name], |row| {
            Ok(SymbolInfo {
                id: row.get(0)?,
                name: row.get(1)?,
                kind: row.get(2)?,
                file: row.get(3)?,
                line: row.get(4)?,
                line_end: row.get::<_, u32>(5).unwrap_or(0),
                signature: row.get(6)?,
                language: row.get(7)?,
            })
        })?;
        let mut results = Vec::new();
        for r in rows {
            results.push(r?);
        }
        Ok(results)
    }

    /// List all symbols in a given file.
    pub fn list_symbols(&self, file: &Path) -> Result<Vec<SymbolInfo>> {
        let file_str = file.to_string_lossy().replace('\\', "/");
        let mut stmt = self.conn.prepare(
            "SELECT id, name, kind, file, line, line_end, signature, language FROM symbols WHERE file = ?1 ORDER BY line",
        )?;
        let rows = stmt.query_map(params![file_str], |row| {
            Ok(SymbolInfo {
                id: row.get(0)?,
                name: row.get(1)?,
                kind: row.get(2)?,
                file: row.get(3)?,
                line: row.get(4)?,
                line_end: row.get::<_, u32>(5).unwrap_or(0),
                signature: row.get(6)?,
                language: row.get(7)?,
            })
        })?;
        let mut results = Vec::new();
        for r in rows {
            results.push(r?);
        }
        Ok(results)
    }

    /// Count total symbols in the index.
    pub fn symbol_count(&self) -> Result<usize> {
        let count: i64 = self
            .conn
            .query_row("SELECT COUNT(*) FROM symbols", [], |r| r.get(0))?;
        Ok(count as usize)
    }

    // ── Layer 2: Call Graph (Section 16) ────────────────────────

    /// Add a call edge (source calls/imports/references target).
    pub fn add_edge(
        &self,
        source_id: &str,
        target_id: &str,
        kind: &str,
        file: &str,
        line: u32,
    ) -> Result<()> {
        self.conn.execute(
            "INSERT OR REPLACE INTO edges (source_id, target_id, kind, file, line) VALUES (?1, ?2, ?3, ?4, ?5)",
            params![source_id, target_id, kind, file, line],
        )?;
        Ok(())
    }

    /// Find all symbols that call this symbol (upstream callers).
    pub fn incoming_calls(&self, symbol_id: &str) -> Result<Vec<SymbolInfo>> {
        let mut stmt = self.conn.prepare(
            "SELECT s.id, s.name, s.kind, s.file, s.line, COALESCE(s.line_end,0), s.signature, s.language FROM symbols s \
             INNER JOIN edges e ON e.source_id = s.id \
             WHERE e.target_id = ?1 AND e.kind = 'calls' \
             ORDER BY s.file, s.line",
        )?;
        let items = stmt
            .query_map(params![symbol_id], |row| {
                Ok(SymbolInfo {
                    id: row.get(0)?,
                    name: row.get(1)?,
                    kind: row.get(2)?,
                    file: row.get(3)?,
                    line: row.get(4)?,
                    line_end: row.get::<_, u32>(5).unwrap_or(0),
                    signature: row.get(6)?,
                    language: row.get(7)?,
                })
            })?
            .filter_map(|r| r.ok())
            .collect();
        Ok(items)
    }

    /// Find all symbols this symbol calls (downstream callees).
    pub fn outgoing_calls(&self, symbol_id: &str) -> Result<Vec<SymbolInfo>> {
        let mut stmt = self.conn.prepare(
            "SELECT s.id, s.name, s.kind, s.file, s.line, COALESCE(s.line_end,0), s.signature, s.language FROM symbols s \
             INNER JOIN edges e ON e.target_id = s.id \
             WHERE e.source_id = ?1 AND e.kind = 'calls' \
             ORDER BY s.file, s.line",
        )?;
        let items = stmt
            .query_map(params![symbol_id], |row| {
                Ok(SymbolInfo {
                    id: row.get(0)?,
                    name: row.get(1)?,
                    kind: row.get(2)?,
                    file: row.get(3)?,
                    line: row.get(4)?,
                    line_end: row.get::<_, u32>(5).unwrap_or(0),
                    signature: row.get(6)?,
                    language: row.get(7)?,
                })
            })?
            .filter_map(|r| r.ok())
            .collect();
        Ok(items)
    }

    /// Recursively find full upstream call chain up to `max_depth` levels.
    pub fn full_upstream_chain(
        &self,
        symbol_id: &str,
        max_depth: usize,
    ) -> Result<Vec<SymbolInfo>> {
        let mut visited = std::collections::HashSet::new();
        let mut result = Vec::new();
        self.collect_upstream(symbol_id, max_depth, &mut visited, &mut result)?;
        Ok(result)
    }

    fn collect_upstream(
        &self,
        symbol_id: &str,
        depth: usize,
        visited: &mut std::collections::HashSet<String>,
        result: &mut Vec<SymbolInfo>,
    ) -> Result<()> {
        if depth == 0 || visited.contains(symbol_id) {
            return Ok(());
        }
        visited.insert(symbol_id.to_string());
        for caller in self.incoming_calls(symbol_id)? {
            let caller_id = caller.id.clone();
            result.push(caller);
            self.collect_upstream(&caller_id, depth - 1, visited, result)?;
        }
        Ok(())
    }

    /// Find test files impacted by changes to the given symbol.
    pub fn impact_test_files(&self, symbol_id: &str) -> Result<Vec<String>> {
        let upstream = self.full_upstream_chain(symbol_id, 5)?;
        let test_files: std::collections::HashSet<String> = upstream
            .iter()
            .filter(|s| {
                s.file.contains("test") || s.file.contains("spec") || s.name.starts_with("test_")
            })
            .map(|s| s.file.clone())
            .collect();
        Ok(test_files.into_iter().collect())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    #[test]
    fn test_index_rust_file() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let rs_file = dir.path().join("example.rs");
        {
            let mut f = std::fs::File::create(&rs_file)?;
            writeln!(f, "pub fn hello_world() {{}}")?;
            writeln!(f, "async fn process_data(x: i32) -> bool {{}}")?;
            writeln!(f, "pub struct Config {{}}")?;
        }

        let mut idx = CodeIndex::in_memory()?;
        let count = idx.index_file(&rs_file)?;
        assert!(count >= 3, "Expected at least 3 symbols, got {count}");

        let results = idx.find_symbol("hello_world")?;
        assert_eq!(results.len(), 1);
        assert_eq!(results[0].kind, "function");
        Ok(())
    }

    #[test]
    fn test_index_python_file() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let py_file = dir.path().join("example.py");
        {
            let mut f = std::fs::File::create(&py_file)?;
            writeln!(f, "def greet(name):")?;
            writeln!(f, "    pass")?;
            writeln!(f, "class Agent:")?;
            writeln!(f, "    pass")?;
        }

        let mut idx = CodeIndex::in_memory()?;
        let count = idx.index_file(&py_file)?;
        assert!(count >= 2, "Expected at least 2 symbols, got {count}");
        Ok(())
    }
}

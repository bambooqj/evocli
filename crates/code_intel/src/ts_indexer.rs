//! ts_indexer.rs - Tree-sitter based symbol extraction (replaces regex Layer 1)
//!
//! Research: Aider replaced ctags with tree-sitter for "richer symbol data"
//! tree-sitter is the industry standard used by GitHub, Neovim, VS Code.

/// Extracted symbol from tree-sitter parsing
#[derive(Debug, Clone)]
pub struct TsSymbol {
    pub name: String,
    pub kind: String,
    pub line: u32,
    pub language: String,
}

/// Try to extract symbols using tree-sitter for supported languages.
/// Returns None for unsupported languages — caller falls back to regex.
pub fn extract_symbols(source: &str, ext: &str) -> Option<Vec<TsSymbol>> {
    match ext {
        "rs" => extract_rust(source),
        "py" => extract_python(source),
        "js" | "jsx" => extract_javascript(source),
        "ts" | "tsx" => extract_typescript(source),
        _ => None,
    }
}

// Run a tree-sitter query and collect @name captures.
// Uses while let because tree-sitter 0.24+ dropped Iterator impl on QueryMatches.
fn run_query(
    source: &str,
    lang: tree_sitter::Language,
    query_str: &str,
    kind_map: &[&str],
    lang_name: &str,
) -> Option<Vec<TsSymbol>> {
    use tree_sitter::{Parser, Query, QueryCursor};
    let mut parser = Parser::new();
    parser.set_language(&lang).ok()?;
    let tree = parser.parse(source, None)?;
    let query = Query::new(&lang, query_str).ok()?;
    let mut cursor = QueryCursor::new();
    let src_bytes = source.as_bytes();
    let mut results = Vec::new();
    // tree-sitter 0.23: for loop works via Iterator impl on QueryMatches
    for m in cursor.matches(&query, tree.root_node(), src_bytes) {
        let kind = kind_map
            .get(m.pattern_index as usize)
            .copied()
            .unwrap_or("unknown");
        for cap in m.captures {
            let idx = cap.index as usize;
            if query
                .capture_names()
                .get(idx)
                .map(|n| *n == "name")
                .unwrap_or(false)
            {
                if let Ok(text) = std::str::from_utf8(&src_bytes[cap.node.byte_range()]) {
                    results.push(TsSymbol {
                        name: text.to_string(),
                        kind: kind.to_string(),
                        line: cap.node.start_position().row as u32 + 1,
                        language: lang_name.to_string(),
                    });
                }
            }
        }
    }
    Some(results)
}

fn extract_rust(source: &str) -> Option<Vec<TsSymbol>> {
    run_query(
        source,
        tree_sitter_rust::LANGUAGE.into(),
        "(function_item name: (identifier) @name)\n\
         (struct_item   name: (type_identifier) @name)\n\
         (enum_item     name: (type_identifier) @name)\n\
         (trait_item    name: (type_identifier) @name)",
        &["function", "struct", "enum", "trait"],
        "rust",
    )
}

fn extract_python(source: &str) -> Option<Vec<TsSymbol>> {
    run_query(
        source,
        tree_sitter_python::LANGUAGE.into(),
        "(function_definition name: (identifier) @name)\n\
         (class_definition    name: (identifier) @name)",
        &["function", "class"],
        "python",
    )
}

fn extract_javascript(source: &str) -> Option<Vec<TsSymbol>> {
    run_query(
        source,
        tree_sitter_javascript::LANGUAGE.into(),
        "(function_declaration name: (identifier) @name)\n\
         (class_declaration    name: (identifier) @name)",
        &["function", "class"],
        "javascript",
    )
}

fn extract_typescript(source: &str) -> Option<Vec<TsSymbol>> {
    run_query(
        source,
        tree_sitter_typescript::LANGUAGE_TYPESCRIPT.into(),
        "(function_declaration  name: (identifier) @name)\n\
         (class_declaration     name: (type_identifier) @name)\n\
         (interface_declaration name: (type_identifier) @name)",
        &["function", "class", "interface"],
        "typescript",
    )
}

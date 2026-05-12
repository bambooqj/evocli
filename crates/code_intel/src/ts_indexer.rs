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
    /// End line of the function/class body (for semantic chunk extraction).
    /// Enables extracting the full function body for code embedding.
    pub line_end: u32,
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

/// Run a tree-sitter query and collect @name + @def captures.
///
/// Each query pattern must have:
///   @name — the identifier node (for name text + start line)
///   @def  — the full definition node (for end line / body extent)
///
/// If @def is absent in a pattern, line_end falls back to line + 1.
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

    for m in cursor.matches(&query, tree.root_node(), src_bytes) {
        let kind = kind_map
            .get(m.pattern_index as usize)
            .copied()
            .unwrap_or("unknown");

        let mut name_text: Option<String> = None;
        let mut name_line: u32 = 0;
        let mut def_line_end: u32 = 0;

        for cap in m.captures {
            let idx = cap.index as usize;
            let cap_name = query.capture_names().get(idx).copied().unwrap_or("");
            match cap_name {
                "name" => {
                    if let Ok(text) = std::str::from_utf8(&src_bytes[cap.node.byte_range()]) {
                        name_text = Some(text.to_string());
                        name_line = cap.node.start_position().row as u32 + 1;
                    }
                }
                "def" => {
                    def_line_end = cap.node.end_position().row as u32 + 1;
                }
                _ => {}
            }
        }

        if let Some(name) = name_text {
            let line_end = if def_line_end > name_line {
                def_line_end
            } else {
                name_line + 1  // fallback: at least one line
            };
            results.push(TsSymbol {
                name,
                kind: kind.to_string(),
                line: name_line,
                line_end,
                language: lang_name.to_string(),
            });
        }
    }
    Some(results)
}

fn extract_rust(source: &str) -> Option<Vec<TsSymbol>> {
    run_query(
        source,
        tree_sitter_rust::LANGUAGE.into(),
        "(function_item name: (identifier) @name) @def\n\
         (struct_item   name: (type_identifier) @name) @def\n\
         (enum_item     name: (type_identifier) @name) @def\n\
         (trait_item    name: (type_identifier) @name) @def",
        &["function", "struct", "enum", "trait"],
        "rust",
    )
}

fn extract_python(source: &str) -> Option<Vec<TsSymbol>> {
    run_query(
        source,
        tree_sitter_python::LANGUAGE.into(),
        "(function_definition name: (identifier) @name) @def\n\
         (class_definition    name: (identifier) @name) @def",
        &["function", "class"],
        "python",
    )
}

fn extract_javascript(source: &str) -> Option<Vec<TsSymbol>> {
    run_query(
        source,
        tree_sitter_javascript::LANGUAGE.into(),
        "(function_declaration name: (identifier) @name) @def\n\
         (class_declaration    name: (identifier) @name) @def",
        &["function", "class"],
        "javascript",
    )
}

fn extract_typescript(source: &str) -> Option<Vec<TsSymbol>> {
    run_query(
        source,
        tree_sitter_typescript::LANGUAGE_TYPESCRIPT.into(),
        "(function_declaration  name: (identifier) @name) @def\n\
         (class_declaration     name: (type_identifier) @name) @def\n\
         (interface_declaration name: (type_identifier) @name) @def",
        &["function", "class", "interface"],
        "typescript",
    )
}


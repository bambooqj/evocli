//! BM25 full-text search using tantivy (embedded, no server)
//!
//! GitNexus 对应: src/core/search/ (BM25 部分)
//! GitNexus 使用 BM25 + semantic vector 混合搜索，用 RRF 合并。
//! EvoCLI 用 tantivy 实现 BM25 部分（semantic 部分在 Python LanceDB）。
//!
//! tantivy 是 Rust 原生全文搜索引擎，同 Elasticsearch/Lucene 级别，
//! 但完全嵌入式、无服务器，直接作为库使用。

use anyhow::{Context, Result};
use std::path::{Path, PathBuf};
use tantivy::collector::TopDocs;
use tantivy::query::QueryParser;
use tantivy::schema::*;
use tantivy::{doc, Index, IndexWriter, TantivyDocument};

/// BM25 搜索结果
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct Bm25Result {
    pub symbol_id: String,
    pub name: String,
    pub kind: String,
    pub file: String,
    pub signature: String,
    pub score: f32,
    pub rank: usize, // for RRF merging
}

/// Tantivy BM25 index for code symbols
pub struct Bm25Index {
    /// Path to the index directory — used for the mtime stamp file.
    index_dir: PathBuf,
    index: Index,
    /// schema is retained for add_document operations (not read directly)
    #[allow(dead_code)]
    schema: Schema,
    // field handles
    f_id: Field,
    f_name: Field,
    f_kind: Field,
    f_file: Field,
    f_signature: Field,
    f_fulltext: Field,
}

impl Bm25Index {
    /// Create or open a BM25 index at the given directory.
    pub fn open_or_create(index_dir: &Path) -> Result<Self> {
        std::fs::create_dir_all(index_dir)?;

        let mut schema_builder = Schema::builder();

        // Stored fields (returned in results)
        let f_id = schema_builder.add_text_field("id", STRING | STORED);
        let f_name = schema_builder.add_text_field("name", TEXT | STORED);
        let f_kind = schema_builder.add_text_field("kind", STRING | STORED);
        let f_file = schema_builder.add_text_field("file", TEXT | STORED);
        let f_signature = schema_builder.add_text_field("signature", TEXT | STORED);
        // Full-text search field (combines name + signature + file)
        let f_fulltext = schema_builder.add_text_field("fulltext", TEXT);

        let schema = schema_builder.build();

        let index = if index_dir.join("meta.json").exists() {
            Index::open_in_dir(index_dir)
                .with_context(|| format!("Failed to open BM25 index at {}", index_dir.display()))?
        } else {
            Index::create_in_dir(index_dir, schema.clone()).with_context(|| {
                format!("Failed to create BM25 index at {}", index_dir.display())
            })?
        };

        Ok(Self {
            index_dir: index_dir.to_path_buf(),
            index,
            schema,
            f_id,
            f_name,
            f_kind,
            f_file,
            f_signature,
            f_fulltext,
        })
    }

    /// Build or rebuild the index from SQLite symbol data.
    /// Called after `code_intel.index` completes.
    pub fn rebuild_from_sqlite(&self, db_path: &Path) -> Result<usize> {
        let conn = rusqlite::Connection::open(db_path)?;

        let mut writer: IndexWriter = self.index.writer(50_000_000)?; // 50MB heap
        writer.delete_all_documents()?;

        let mut stmt =
            conn.prepare("SELECT id, name, kind, file, signature, language FROM symbols")?;

        let mut count = 0usize;
        let rows = stmt.query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,                             // id
                row.get::<_, String>(1)?,                             // name
                row.get::<_, String>(2)?,                             // kind
                row.get::<_, String>(3)?,                             // file
                row.get::<_, Option<String>>(4)?.unwrap_or_default(), // signature
                row.get::<_, String>(5)?,                             // language
            ))
        })?;

        for row in rows {
            let (id, name, kind, file, signature, _language) = row?;

            // fulltext combines all searchable text
            let fulltext = format!("{} {} {} {}", name, kind, file, signature);

            writer.add_document(doc!(
                self.f_id        => id,
                self.f_name      => name,
                self.f_kind      => kind,
                self.f_file      => file,
                self.f_signature => signature,
                self.f_fulltext  => fulltext,
            ))?;
            count += 1;
        }

        writer.commit()?;
        Ok(count)
    }

    /// Rebuild the BM25 index from SQLite **only if** the SQLite file has been
    /// modified since the last rebuild.  Returns `Ok(Some(count))` when a
    /// rebuild was performed, or `Ok(None)` when the index was already
    /// up-to-date and the rebuild was skipped.
    ///
    /// A tiny `.evocli_bm25_stamp` file inside `index_dir` is used as the
    /// "last rebuilt" timestamp (its mtime is updated after each rebuild).
    pub fn rebuild_from_sqlite_if_changed(&self, db_path: &Path) -> Result<Option<usize>> {
        let stamp_path = self.index_dir.join(".evocli_bm25_stamp");

        let sqlite_mtime = std::fs::metadata(db_path).and_then(|m| m.modified()).ok();
        let stamp_mtime = std::fs::metadata(&stamp_path)
            .and_then(|m| m.modified())
            .ok();

        if let (Some(sql_t), Some(bm25_t)) = (sqlite_mtime, stamp_mtime) {
            if sql_t <= bm25_t {
                tracing::debug!(
                    "[bm25] SQLite mtime {:?} <= stamp {:?}: skipping full rebuild",
                    sql_t,
                    bm25_t
                );
                return Ok(None);
            }
        }

        let count = self.rebuild_from_sqlite(db_path)?;

        // Touch stamp file (create or update mtime) to record rebuild time.
        std::fs::write(&stamp_path, b"")
            .with_context(|| format!("bm25: failed to update stamp file {:?}", stamp_path))?;

        tracing::info!("[bm25] Rebuilt index: {} symbols indexed", count);
        Ok(Some(count))
    }

    /// BM25 search — returns ranked results.
    pub fn search(&self, query_str: &str, limit: usize) -> Result<Vec<Bm25Result>> {
        let reader = self
            .index
            .reader()
            .with_context(|| "Failed to open tantivy reader")?;
        let searcher = reader.searcher();

        // Search across name + fulltext
        let query_parser = QueryParser::for_index(&self.index, vec![self.f_name, self.f_fulltext]);
        let query = query_parser
            .parse_query(query_str)
            .with_context(|| format!("Failed to parse BM25 query: {}", query_str))?;

        let top_docs = searcher.search(&query, &TopDocs::with_limit(limit))?;

        let mut results = Vec::new();
        for (rank, (score, doc_address)) in top_docs.into_iter().enumerate() {
            let doc: TantivyDocument = searcher.doc(doc_address)?;

            let get = |field: Field| -> String {
                doc.get_first(field)
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string()
            };

            results.push(Bm25Result {
                symbol_id: get(self.f_id),
                name: get(self.f_name),
                kind: get(self.f_kind),
                file: get(self.f_file),
                signature: get(self.f_signature),
                score,
                rank: rank + 1,
            });
        }

        Ok(results)
    }

    /// Number of indexed documents.
    pub fn doc_count(&self) -> Result<u64> {
        let reader = self.index.reader()?;
        Ok(reader.searcher().num_docs())
    }
}

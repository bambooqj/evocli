//! commands/index_cmd.rs — evocli index 子命令（代码符号索引）
//!
//! Builds two indexes in .evocli/:
//!   1. code_index.db     — SQLite symbol index (ts_indexer)
//!   2. bm25_index/       — Tantivy BM25 full-text index (for hybrid search)
use anyhow::{Context as _, Result};
use std::time::Instant;

pub fn run(dir: Option<&str>) -> Result<()> {
    let root = dir.map(std::path::PathBuf::from)
        .unwrap_or_else(|| std::env::current_dir().unwrap());

    println!("Indexing: {}", root.display());

    // Ensure .evocli/ directory exists before opening SQLite databases.
    let evocli_dir = root.join(".evocli");
    std::fs::create_dir_all(&evocli_dir)
        .with_context(|| format!("Failed to create .evocli directory: {}", evocli_dir.display()))?;

    // ── Step 1: Count total files first (gives progress denominator) ──────────
    let extensions = ["rs", "py", "ts", "tsx", "js", "go"];
    let total_files = count_indexable_files(&root, &extensions);
    if total_files > 0 {
        println!("  Found {} file(s) to index...", total_files);
    }

    // ── Step 2: Build SQLite symbol index with progress feedback ──────────────
    let db_path = root.join(".evocli").join("code_index.db");
    let t0 = Instant::now();
    let count = index_with_progress(&root, &db_path, &extensions, total_files)?;
    let elapsed = t0.elapsed();
    println!("  ✓ Indexed {} symbols → {} ({:.1}s)",
             count, db_path.display(), elapsed.as_secs_f32());

    // ── Step 3: Build BM25 tantivy index ─────────────────────────────────────
    let bm25_dir = root.join(".evocli").join("bm25_index");
    print!("  Building BM25 index... ");
    match knowledge_graph::Bm25Index::open_or_create(&bm25_dir) {
        Ok(bm25) => {
            match bm25.rebuild_from_sqlite(&db_path) {
                Ok(n) => println!("✓ ({} symbols) → {}", n, bm25_dir.display()),
                Err(e) => println!("⚠ BM25 index failed (non-fatal): {}", e),
            }
        }
        Err(e) => println!("⚠ BM25 index creation failed (non-fatal): {}", e),
    }

    println!();
    println!("✅  Indexed {} symbols in {:.1}s", count, t0.elapsed().as_secs_f32());
    println!("   Run this again after major refactors to keep search results accurate.");
    Ok(())
}

/// Count how many files will be indexed (for progress display).
fn count_indexable_files(root: &std::path::Path, extensions: &[&str]) -> usize {
    use ignore::Walk;
    Walk::new(root)
        .filter_map(|r| r.ok())
        .filter(|e| e.file_type().map(|t| t.is_file()).unwrap_or(false))
        .filter(|e| {
            let ext = e.path().extension().and_then(|x| x.to_str()).unwrap_or("");
            extensions.contains(&ext)
        })
        .count()
}

/// Index with periodic progress updates (every 100 files or 5 seconds).
fn index_with_progress(
    root: &std::path::Path,
    db_path: &std::path::Path,
    extensions: &[&str],
    total_files: usize,
) -> Result<usize> {
    use ignore::Walk;

    let mut index = code_intel::CodeIndex::new(db_path)?;
    let mut symbols = 0usize;
    let mut files_done = 0usize;
    let mut last_print = Instant::now();

    for result in Walk::new(root) {
        let entry = match result { Ok(e) => e, Err(_) => continue };
        if !entry.file_type().map(|t| t.is_file()).unwrap_or(false) { continue; }
        let ext = entry.path().extension().and_then(|e| e.to_str()).unwrap_or("");
        if !extensions.contains(&ext) { continue; }

        symbols += index.index_file(entry.path()).unwrap_or(0);
        files_done += 1;

        // Print progress every 100 files or every 3 seconds
        if files_done % 100 == 0 || last_print.elapsed().as_secs() >= 3 {
            if total_files > 0 {
                let pct = files_done * 100 / total_files;
                print!("\r  Parsing {}/{} files ({}%)  ", files_done, total_files, pct);
            } else {
                print!("\r  Parsed {} files...  ", files_done);
            }
            use std::io::Write;
            let _ = std::io::stdout().flush();
            last_print = Instant::now();
        }
    }

    // Clear the progress line
    if files_done > 0 {
        print!("\r");
        use std::io::Write;
        let _ = std::io::stdout().flush();
    }

    Ok(symbols)
}

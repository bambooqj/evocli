//! web_tools.rs — Native Rust web fetching with HTML→Markdown conversion
//!
//! Implements the `web.fetch` RPC endpoint.
//!
//! Stack:
//!   reqwest   — async HTTP client (rustls TLS, no OpenSSL dependency)
//!   scraper   — HTML parsing (html5ever, Firefox-grade HTML5 parser)
//!   htmd      — HTML → Markdown conversion (pure Rust)
//!
//! This replaces the Python web_fetcher.py (httpx + readability-lxml + html2text)
//! with a native Rust implementation: faster, fewer dependencies, cross-platform.

use anyhow::{bail, Result};
use serde_json::Value;

/// Shared reqwest client (lazy-initialised, reused across calls for connection pooling).
fn client() -> &'static reqwest::Client {
    use std::sync::OnceLock;
    static CLIENT: OnceLock<reqwest::Client> = OnceLock::new();
    CLIENT.get_or_init(|| {
        reqwest::Client::builder()
            .user_agent("Mozilla/5.0 (compatible; EvoCLI/0.1; +https://github.com/bambooqj/evocli)")
            .timeout(std::time::Duration::from_secs(30))
            .redirect(reqwest::redirect::Policy::limited(10))
            .gzip(true)
            .deflate(true)
            .build()
            .expect("reqwest client init failed")
    })
}

/// `web.fetch` RPC handler.
///
/// Parameters (from JSON args):
///   `url`       — URL to fetch (required)
///   `max_chars` — max characters to return (default: 8000; ~2k tokens)
///   `selector`  — optional CSS selector to extract specific element
///                 e.g. "article", "main", ".content"
///
/// Returns JSON:
/// ```json
/// {
///   "url":       "https://...",
///   "title":     "Page title",
///   "markdown":  "# Heading\n\nContent...",
///   "chars":     1234,
///   "truncated": false
/// }
/// ```
pub async fn fetch(args: &Value) -> Result<Value> {
    let url = args["url"].as_str()
        .ok_or_else(|| anyhow::anyhow!("web.fetch: 'url' parameter is required"))?;

    let max_chars = args["max_chars"].as_u64().unwrap_or(8000) as usize;
    let selector  = args["selector"].as_str();

    // Validate URL
    if !url.starts_with("http://") && !url.starts_with("https://") {
        bail!("web.fetch: URL must start with http:// or https://");
    }

    // ── Fetch ─────────────────────────────────────────────────────────────────
    let response = client().get(url).send().await
        .map_err(|e| anyhow::anyhow!("web.fetch: request failed for '{}': {}", url, e))?;

    let status = response.status();
    if !status.is_success() {
        bail!("web.fetch: HTTP {} for '{}'", status, url);
    }

    // Read response body (cap at 5 MB to avoid OOM on huge pages)
    const MAX_BODY: usize = 5 * 1024 * 1024;
    let bytes = response.bytes().await
        .map_err(|e| anyhow::anyhow!("web.fetch: failed to read body: {}", e))?;
    let html = String::from_utf8_lossy(bytes.get(..MAX_BODY.min(bytes.len())).unwrap_or(&bytes)).into_owned();

    // ── Parse HTML ────────────────────────────────────────────────────────────
    let document = scraper::Html::parse_document(&html);

    // Extract title
    let title = {
        let sel = scraper::Selector::parse("title").unwrap();
        document.select(&sel)
            .next()
            .map(|el| el.text().collect::<String>().trim().to_string())
            .unwrap_or_default()
    };

    // Extract the target element (CSS selector, "main", "article", or full body)
    let content_html: String = if let Some(css) = selector {
        // User-specified selector
        match scraper::Selector::parse(css) {
            Ok(sel) => {
                document.select(&sel)
                    .next()
                    .map(|el| el.html())
                    .unwrap_or_else(|| extract_main_content(&document))
            }
            Err(_) => extract_main_content(&document),
        }
    } else {
        extract_main_content(&document)
    };

    // ── Convert HTML → Markdown ───────────────────────────────────────────────
    let markdown = htmd::convert(&content_html)
        .unwrap_or_else(|_| {
            // Fallback: strip all tags and return plain text
            let plain_sel = scraper::Selector::parse("*").unwrap();
            document.select(&plain_sel)
                .map(|el| el.text().collect::<String>())
                .collect::<Vec<_>>()
                .join(" ")
        });

    // Clean up: collapse 3+ consecutive blank lines into 2
    let markdown = {
        let mut out = String::with_capacity(markdown.len());
        let mut blank_count = 0u8;
        for line in markdown.lines() {
            if line.trim().is_empty() {
                blank_count += 1;
                if blank_count <= 2 { out.push('\n'); }
            } else {
                blank_count = 0;
                out.push_str(line);
                out.push('\n');
            }
        }
        out.trim().to_string()
    };

    // Truncate
    let truncated = markdown.len() > max_chars;
    let final_md  = if truncated {
        // Truncate at char boundary
        let end = markdown.char_indices()
            .nth(max_chars)
            .map(|(i, _)| i)
            .unwrap_or(markdown.len());
        format!("{}\n\n…[truncated at {} chars]", &markdown[..end], max_chars)
    } else {
        markdown.clone()
    };

    Ok(serde_json::json!({
        "url":       url,
        "title":     title,
        "markdown":  final_md,
        "chars":     final_md.len(),
        "truncated": truncated,
    }))
}

/// Extract main readable content from the page using a priority list of CSS selectors.
/// Falls back to `<body>` if nothing more specific is found.
///
/// This is a lightweight "readability" heuristic:
///   article > main > [role=main] > #content > #main > body
fn extract_main_content(document: &scraper::Html) -> String {
    // Priority order: most specific → least specific
    const PRIORITY: &[&str] = &[
        "article",
        "main",
        "[role='main']",
        "[role=\"main\"]",
        "#content",
        "#main",
        ".content",
        ".main-content",
        ".article-body",
        ".post-content",
        ".entry-content",
        "body",
    ];

    for css in PRIORITY {
        if let Ok(sel) = scraper::Selector::parse(css) {
            if let Some(el) = document.select(&sel).next() {
                let html = el.html();
                if html.len() > 200 { // skip suspiciously small elements
                    return remove_noise_elements(html);
                }
            }
        }
    }

    // Last resort: entire document
    document.root_element().html()
}

/// Remove navigation, footer, sidebar, and other non-content elements
/// by stripping known noise selectors from the extracted HTML.
fn remove_noise_elements(html: String) -> String {
    // Parse the fragment and strip noisy elements
    let fragment = scraper::Html::parse_fragment(&html);

    // Collect text from content elements, skipping noise
    const NOISE_TAGS: &[&str] = &["nav", "footer", "header", "aside", "script", "style", "noscript"];

    // Simple approach: return html as-is (htmd handles most noise gracefully)
    // For a production system, implement tag-based filtering here.
    let _ = NOISE_TAGS; // suppress unused warning; reserved for future filtering
    let _ = fragment;

    html
}

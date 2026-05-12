//! embedder.rs - 中英双语嵌入 (intfloat/multilingual-e5-small)
//!
//! Python 侧: jinaai/jina-embeddings-v2-base-zh (768维, 中英专用)
//! Rust 侧:   intfloat/multilingual-e5-small (384维, 多语言, 轻量)
//!
//! 两者分工不同:
//!   Python LanceDB: 记忆语义搜索 (jina 中英双语质量更高)
//!   Rust MemRouter: 分类器训练用嵌入 (multilingual-e5 轻量快速)
//!
//! 两个模型共享缓存目录 ~/.evocli/models/，互不干扰。

use anyhow::Result;
use std::path::Path;
use std::sync::Mutex;

pub type Embedding = Vec<f32>;

pub struct TextEmbedder {
    inner: Mutex<fastembed::TextEmbedding>,
    dim: usize,
}

impl TextEmbedder {
    pub fn new(cache_dir: &Path) -> Result<Self> {
        std::fs::create_dir_all(cache_dir)?;

        // intfloat/multilingual-e5-small: 支持中英文, 384维, 轻量
        // 适合 MemRouter 分类器训练 (速度优先)
        // 如需更高质量改为 MultilingualE5Large (1024维)
        let model = fastembed::EmbeddingModel::MultilingualE5Small;
        let dim = 384usize; // multilingual-e5-small = 384

        let opts = fastembed::InitOptions::new(model)
            .with_cache_dir(cache_dir.to_path_buf())
            .with_show_download_progress(true);

        let inner = fastembed::TextEmbedding::try_new(opts)
            .map_err(|e| anyhow::anyhow!("fastembed init (multilingual-e5-small): {}", e))?;

        tracing::info!(
            "MemRouter embedder: multilingual-e5-small ({}dim, 中英双语)",
            dim
        );
        Ok(Self {
            inner: Mutex::new(inner),
            dim,
        })
    }

    /// 使用默认共享缓存 ~/.evocli/models/
    pub fn with_default_cache() -> Result<Self> {
        let cache = dirs::home_dir()
            .unwrap_or_default()
            .join(".evocli")
            .join("models");
        Self::new(&cache)
    }

    pub fn embed_one(&self, text: &str) -> Result<Embedding> {
        // multilingual-e5 需要前缀以获得最佳效果
        // "query: " 用于检索，"passage: " 用于文档
        // 对于分类任务，不加前缀或用 "passage: " 均可
        let prefixed = format!("passage: {}", text);
        let mut g = self.inner.lock().map_err(|_| anyhow::anyhow!("mutex"))?;
        let mut r = g
            .embed(vec![prefixed], None)
            .map_err(|e| anyhow::anyhow!("embed: {}", e))?;
        r.pop().ok_or_else(|| anyhow::anyhow!("empty"))
    }

    pub fn embed_batch(&self, texts: &[&str]) -> Result<Vec<Embedding>> {
        let prefixed: Vec<String> = texts.iter().map(|t| format!("passage: {}", t)).collect();
        let mut g = self.inner.lock().map_err(|_| anyhow::anyhow!("mutex"))?;
        g.embed(prefixed, None)
            .map_err(|e| anyhow::anyhow!("batch embed: {}", e))
    }

    pub fn dim(&self) -> usize {
        self.dim
    }
}

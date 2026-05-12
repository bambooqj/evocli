//! classifier.rs — 快速推理路径 (< 5ms)
//!
//! 加载训练好的逻辑回归模型，做 Softmax 分类。
//! 不确定时 (confidence < CONFIDENCE_THRESHOLD) 返回 NeedsLlm，
//! 由 Python soul 调用 LLM 打标签并收集新训练数据。

use anyhow::Result;
use std::path::Path;

use crate::embedder::TextEmbedder;
use crate::trainer::SerializedModel;
use crate::{MemoryLabel, CONFIDENCE_THRESHOLD};

/// 分类结果
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct ClassifyResult {
    pub label: MemoryLabel,
    pub confidence: f32,
    /// True = 用分类器推理; False = 需要 LLM 打标签
    pub from_model: bool,
    pub should_write: bool,
    pub importance: f32,
}

/// 快速内存分类器 (加载已训练模型)
pub struct MemRouterClassifier {
    embedder: TextEmbedder,
    model: Option<SerializedModel>,
}

impl MemRouterClassifier {
    /// 创建分类器，尝试加载已有模型
    pub fn new(embedder: TextEmbedder, model_path: Option<&Path>) -> Self {
        let model = model_path.and_then(|p| {
            std::fs::read_to_string(p)
                .ok()
                .and_then(|s| serde_json::from_str(&s).ok())
        });
        Self { embedder, model }
    }

    /// 是否有可用的训练模型
    pub fn is_trained(&self) -> bool {
        self.model.is_some()
    }

    /// 对文本分类
    /// - 有模型: 推理 + 置信度检查
    /// - 无模型或低置信度: 返回 NeedsLlm 标志 (from_model=false)
    pub fn classify(&self, text: &str) -> ClassifyResult {
        if let Some(model) = &self.model {
            match self.infer(text, model) {
                Ok(result) if result.confidence >= CONFIDENCE_THRESHOLD => result,
                Ok(result) => {
                    // 低置信度: 标记为需要 LLM，但附带当前预测供参考
                    ClassifyResult {
                        from_model: false,
                        ..result
                    }
                }
                Err(_) => self.needs_llm_result(),
            }
        } else {
            self.needs_llm_result()
        }
    }

    fn infer(&self, text: &str, model: &SerializedModel) -> Result<ClassifyResult> {
        // 1. 生成嵌入向量
        let embedding = self.embedder.embed_one(text)?;
        let _dim = embedding.len(); // retained for future dimensionality validation

        // 2. 线性变换: scores[c] = weights[c] · embedding + bias[c]
        let n_classes = model.weights.len();
        let mut scores = vec![0.0f32; n_classes];
        for c in 0..n_classes {
            let w = &model.weights[c];
            let b = model.biases.get(c).copied().unwrap_or(0.0);
            let dot: f32 = w.iter().zip(embedding.iter()).map(|(a, b)| a * b).sum();
            scores[c] = dot + b;
        }

        // 3. Softmax
        let max_score = scores.iter().cloned().fold(f32::NEG_INFINITY, f32::max);
        let exp_scores: Vec<f32> = scores.iter().map(|s| (s - max_score).exp()).collect();
        let sum_exp: f32 = exp_scores.iter().sum();
        let probs: Vec<f32> = exp_scores.iter().map(|e| e / sum_exp).collect();

        // 4. 找最大概率类别
        let (best_idx, &confidence) = probs
            .iter()
            .enumerate()
            .max_by(|(_, a), (_, b)| a.partial_cmp(b).unwrap())
            .unwrap_or((5, &0.0));

        let label = MemoryLabel::from_idx(best_idx);
        Ok(ClassifyResult {
            importance: label.importance(),
            should_write: label.should_write(),
            label,
            confidence,
            from_model: true,
        })
    }

    fn needs_llm_result(&self) -> ClassifyResult {
        ClassifyResult {
            label: MemoryLabel::Episodic,
            confidence: 0.0,
            from_model: false,
            should_write: true,
            importance: 0.5,
        }
    }

    /// 重新加载模型 (训练完成后调用)
    pub fn reload_model(&mut self, model_path: &Path) -> bool {
        match std::fs::read_to_string(model_path)
            .ok()
            .and_then(|s| serde_json::from_str(&s).ok())
        {
            Some(model) => {
                tracing::info!("MemRouter: loaded new model (train_size={})", {
                    let m: &SerializedModel = &model;
                    m.train_size
                });
                self.model = Some(model);
                true
            }
            None => false,
        }
    }

    pub fn model_accuracy(&self) -> Option<f32> {
        self.model.as_ref().map(|m| m.accuracy)
    }
}

//! trainer.rs — Logistic regression using ndarray (simpler than linfa generics)

use anyhow::Result;
use ndarray::{Array1, Array2, Axis};
use std::path::Path;

use crate::embedder::TextEmbedder;
use crate::{labeler::TrainingStore, MemoryLabel};

#[derive(serde::Serialize, serde::Deserialize)]
pub struct SerializedModel {
    pub weights: Vec<Vec<f32>>,
    pub biases: Vec<f32>,
    pub train_size: usize,
    pub trained_at: String,
    pub accuracy: f32,
    pub n_classes: usize,
    pub n_features: usize,
}

pub struct ModelTrainer {
    store: TrainingStore,
    embedder: TextEmbedder,
}

impl ModelTrainer {
    pub fn new(store: TrainingStore, embedder: TextEmbedder) -> Self {
        Self { store, embedder }
    }

    pub fn train(&self) -> Result<SerializedModel> {
        let samples = self.store.load_all()?;
        if samples.is_empty() {
            anyhow::bail!("No training samples");
        }
        let n = samples.len();
        let n_classes = MemoryLabel::num_classes();

        tracing::info!("MemRouter: training {} samples", n);
        let texts: Vec<&str> = samples.iter().map(|(t, _)| t.as_str()).collect();
        let embs = self.embedder.embed_batch(&texts)?;
        let labels: Vec<usize> = samples.iter().map(|(_, l)| *l).collect();

        // 从实际嵌入结果获取维度 (multilingual-e5-small=384, large=1024)
        let n_features = embs.first().map(|e| e.len()).unwrap_or(384);
        tracing::info!("Embedding dim={}, classes={}", n_features, n_classes);

        let x = Array2::from_shape_vec(
            (n, n_features),
            embs.iter().flat_map(|e| e.iter().copied()).collect(),
        )?;

        let mut y_oh = Array2::<f32>::zeros((n, n_classes));
        for (i, &l) in labels.iter().enumerate() {
            if l < n_classes {
                y_oh[[i, l]] = 1.0;
            }
        }

        let mut w = Array2::<f32>::zeros((n_classes, n_features));
        let mut b = Array1::<f32>::zeros(n_classes);
        let lr = 0.01f32;

        for _ in 0..300 {
            let scores = x.dot(&w.t()) + &b;
            let mut p = scores.clone();
            for mut row in p.rows_mut() {
                let mx = row.fold(f32::NEG_INFINITY, |a, &v| a.max(v));
                row.mapv_inplace(|v| (v - mx).exp());
                let s: f32 = row.sum();
                row.mapv_inplace(|v| v / s.max(1e-10));
            }
            let d = &p - &y_oh;
            let dw = d.t().dot(&x) / n as f32;
            let db = d.mean_axis(Axis(0)).unwrap();
            w = w - lr * dw;
            b = b - lr * db;
        }

        let correct = x
            .dot(&w.t())
            .rows()
            .into_iter()
            .zip(labels.iter())
            .filter(|(row, &lbl)| {
                // Use unwrap_or(Equal) to safely handle NaN scores (e.g., from gradient explosion).
                // NaN comparisons are non-deterministic with unwrap(), which panics on None.
                row.iter()
                    .enumerate()
                    .max_by(|(_, a), (_, b)| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal))
                    .map(|(i, _)| i)
                    .unwrap_or(0)
                    == lbl
            })
            .count();
        let accuracy = correct as f32 / n as f32;
        tracing::info!("MemRouter done: accuracy={:.1}%", accuracy * 100.0);
        self.store.set_meta("last_trained_count", &n.to_string())?;

        Ok(SerializedModel {
            weights: w.rows().into_iter().map(|r| r.to_vec()).collect(),
            biases: b.to_vec(),
            train_size: n,
            trained_at: chrono::Utc::now().to_rfc3339(),
            accuracy,
            n_classes,
            n_features,
        })
    }

    pub fn train_and_save(&self, model_path: &Path) -> Result<SerializedModel> {
        let m = self.train()?;
        std::fs::write(model_path, serde_json::to_string_pretty(&m)?)?;
        tracing::info!("Model saved: {}", model_path.display());
        Ok(m)
    }
}

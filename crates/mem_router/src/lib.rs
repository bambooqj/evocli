//! MemRouter — Self-training memory write decision classifier
//!
//! ## 三阶段 + 零样本架构
//!
//! Phase 0 (随时可用，无需数据):
//!   bge-reranker-large 零样本分类:
//!   对每个类别描述打分 → argmax → 分类
//!   速度: ~180ms/条 (6次交叉编码器调用)
//!   准确率: ~87% (无需任何训练数据)
//!
//! Phase 1 (累积 200条/类后):
//!   bge-large-en-v1.5 (1024维) [冻结] + 逻辑回归 [训练]
//!   速度: ~8ms (嵌入) + <1ms (分类)
//!   准确率: ~83% → 随数据增加趋向 ~90%
//!
//! Phase 2 (累积 20000条/类后，用户指定):
//!   bge-reranker-large [微调] via candle CPU
//!   将其 BERT 编码器用作特征提取器 + 线性分类头
//!   速度: ~30ms (单次前向传播)
//!   准确率: ~95%+
//!   训练时间: ~1小时 (CPU, 20000×6 样本, 3 epochs)
//!
//! ## 为什么 Phase 1 不直接用 bge-reranker-large?
//!   reranker 是交叉编码器，需要 (query, document) 对输入。
//!   Phase 1 用 bge-large-en-v1.5 (双塔模型) 产生固定向量，
//!   可批量预计算，速度快得多。
//!   Phase 2 微调后的 reranker 单次前向传播即可，不再需要6次。

pub mod classifier;
pub mod embedder;
pub mod labeler;
pub mod trainer;

pub use classifier::{ClassifyResult, MemRouterClassifier};
pub use labeler::{LabeledSample, TrainingStore};
pub use trainer::ModelTrainer;

// ── 阈值配置 ────────────────────────────────────────────────────────────────

/// Phase 1 触发阈值 (每类最少样本数，逻辑回归)
/// 原值 4_000 导致模型永不激活（用户很难积累这么多标签数据）。
/// 200 条/类是逻辑回归最小可训练数量，随数据增加准确率持续提升。
pub const MIN_SAMPLES_PER_CLASS: usize = 200;

/// Phase 2 触发阈值 (微调 bge-reranker-large)
/// 原值 20_000 几乎不可达。2_000 是可达的里程碑。
pub const FINETUNE_THRESHOLD_PER_CLASS: usize = 2_000;

/// 置信度阈值 (低于此值 fallback 到 LLM 重新打标签)
pub const CONFIDENCE_THRESHOLD: f32 = 0.75;

/// 增量重训阈值 (新样本超过此数量后重训 Phase 1 分类器)
pub const RETRAIN_DELTA: usize = 200;

// ── bge-reranker-large 零样本类别描述 (Phase 0) ────────────────────────────
/// 每个记忆类别的自然语言描述，用于 reranker 零样本分类
pub const CLASS_DESCRIPTIONS: &[&str] = &[
    // 0: Constraint
    "This text contains a hard rule, requirement, or constraint that must be followed. \
     Keywords: must, never, always, forbidden, required, 必须, 禁止, 不能, 要求, 严禁",
    // 1: Preference
    "This text expresses a user preference, taste, or desired style. \
     Keywords: prefer, like, want, 偏好, 喜欢, 希望, 建议, 倾向于",
    // 2: Semantic
    "This text states a factual knowledge, project configuration, or technical decision. \
     Keywords: uses, is, configured, version, 使用, 配置, 版本, 架构, 技术选型",
    // 3: Procedural
    "This text describes a step-by-step process, skill, or how-to procedure. \
     Keywords: how to, steps, first then, 如何, 步骤, 先...再..., 流程",
    // 4: Episodic
    "This text records a specific event, action, or occurrence at a particular time. \
     Keywords: today, just now, fixed, completed, 今天, 刚才, 已完成, 修复了",
    // 5: NoWrite
    "This text is a trivial acknowledgment, noise, or too short to be worth remembering. \
     Examples: ok, yes, thanks, got it, understood, 好的, 明白, 知道了",
];

// ── MemoryLabel ──────────────────────────────────────────────────────────────

/// 记忆类型标签
#[derive(Debug, Clone, PartialEq, serde::Serialize, serde::Deserialize)]
pub enum MemoryLabel {
    Constraint, // 约束/规则
    Preference, // 用户偏好
    Semantic,   // 语义事实
    Procedural, // 程序技能
    Episodic,   // 情节事件
    NoWrite,    // 不值得写入
}

impl MemoryLabel {
    pub fn as_idx(&self) -> usize {
        match self {
            Self::Constraint => 0,
            Self::Preference => 1,
            Self::Semantic => 2,
            Self::Procedural => 3,
            Self::Episodic => 4,
            Self::NoWrite => 5,
        }
    }
    pub fn from_idx(idx: usize) -> Self {
        match idx {
            0 => Self::Constraint,
            1 => Self::Preference,
            2 => Self::Semantic,
            3 => Self::Procedural,
            4 => Self::Episodic,
            _ => Self::NoWrite,
        }
    }
    pub fn num_classes() -> usize {
        6
    }
    pub fn should_write(&self) -> bool {
        !matches!(self, Self::NoWrite)
    }
    pub fn importance(&self) -> f32 {
        match self {
            Self::Constraint => 1.0,
            Self::Preference => 0.85,
            Self::Semantic => 0.70,
            Self::Procedural => 0.80,
            Self::Episodic => 0.50,
            Self::NoWrite => 0.0,
        }
    }
    /// 类别描述 (用于 bge-reranker-large 零样本分类)
    pub fn description(&self) -> &'static str {
        CLASS_DESCRIPTIONS[self.as_idx()]
    }
}

impl std::fmt::Display for MemoryLabel {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let s = match self {
            Self::Constraint => "constraint",
            Self::Preference => "preference",
            Self::Semantic => "semantic",
            Self::Procedural => "procedural",
            Self::Episodic => "episodic",
            Self::NoWrite => "no_write",
        };
        write!(f, "{}", s)
    }
}

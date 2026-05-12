//! User-friendly error types for EvoCLI
//!
//! Error codes:
//!   E101 — API key not configured
//!   E102 — Invalid provider configuration
//!   E103 — API connection failed
//!   E401 — Skill not found
//!   E402 — Skill execution failed

use std::fmt;

#[allow(dead_code)]
#[derive(Debug)]
pub enum EvoCLIError {
    /// E101: API key not set for the selected provider
    ApiKeyMissing { provider: String },
    /// E102: Invalid or unsupported provider name
    InvalidProvider { provider: String },
    /// E103: Failed to connect to the LLM API
    ApiConnectionFailed { provider: String, detail: String },
    /// E401: Requested skill does not exist
    SkillNotFound { skill: String },
    /// E402: Skill execution failed
    SkillExecutionFailed { skill: String, detail: String },
}

impl EvoCLIError {
    /// Human-friendly message (no technical details)
    pub fn user_message(&self) -> String {
        match self {
            Self::ApiKeyMissing { provider } => {
                format!(
                    "API key for '{provider}' is not configured. Run `evocli init` to set it up."
                )
            }
            Self::InvalidProvider { provider } => {
                format!("Unknown provider '{provider}'. Supported: anthropic, openai, deepseek, ollama.")
            }
            Self::ApiConnectionFailed { provider, .. } => {
                format!("Could not connect to {provider} API. Check your network and API key.")
            }
            Self::SkillNotFound { skill } => {
                format!("Skill '{skill}' not found. Use `evocli skills` to list available skills.")
            }
            Self::SkillExecutionFailed { skill, .. } => {
                format!("Skill '{skill}' failed to execute. See logs for details.")
            }
        }
    }

    /// Error code string
    pub fn code(&self) -> &'static str {
        match self {
            Self::ApiKeyMissing { .. } => "E101",
            Self::InvalidProvider { .. } => "E102",
            Self::ApiConnectionFailed { .. } => "E103",
            Self::SkillNotFound { .. } => "E401",
            Self::SkillExecutionFailed { .. } => "E402",
        }
    }
}

impl fmt::Display for EvoCLIError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "[{}] {}", self.code(), self.user_message())
    }
}

impl std::error::Error for EvoCLIError {}

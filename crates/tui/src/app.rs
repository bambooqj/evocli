//! TUI Application state machine
//!
//! Performance architecture:
//!   - `line_cache`: Pre-rendered `Vec<Line<'static>>` for all messages.
//!     Built ONCE when dirty, used on every frame. Virtual scrolling slices this vec.
//!   - `cache_dirty`: Set true when messages change. Cleared after rebuild.
//!   - `stream_skip_frames`: Throttle redraws during streaming (every 3rd token).

use ratatui::text::Line;

// ═══════════════════════════════════════════════════════════════════════════════
// NEW TYPES — Work Panel, Tool History, Approval Mode
// ═══════════════════════════════════════════════════════════════════════════════

/// Which tab is active in the Work Panel (right sidebar).
#[derive(Debug, Clone, PartialEq)]
pub enum WorkPanelTab {
    Tools,    // Tool timeline with timings for this session
    Context,  // Loaded files + memory summary
}

/// Approval mode — always visible in the footer.
/// Auto  = AI applies changes without asking (default)
/// Manual = AI asks before every write/shell operation
/// Plan   = read-only analysis mode, never writes
#[derive(Debug, Clone, PartialEq)]
pub enum ApproveMode {
    Auto,
    Manual,
    Plan,
}

impl ApproveMode {
    pub fn label(&self) -> &'static str {
        match self {
            ApproveMode::Auto => "AUTO",
            ApproveMode::Manual => "MANUAL",
            ApproveMode::Plan => "PLAN",
        }
    }
    pub fn cycle(&self) -> Self {
        match self {
            ApproveMode::Auto => ApproveMode::Manual,
            ApproveMode::Manual => ApproveMode::Plan,
            ApproveMode::Plan => ApproveMode::Auto,
        }
    }
}

/// A single tool call entry stored in the Work Panel tool history.
#[derive(Debug, Clone)]
pub struct ToolEntry {
    pub turn: usize,        // which conversation turn (for grouping)
    pub tool: String,       // raw tool name (e.g. "fs.read")
    pub display: String,    // human-readable display (e.g. "📖 src/main.rs")
    pub ok: Option<bool>,   // None = in-flight, Some(true/false) = done
    pub duration_ms: u64,   // elapsed from tool_call_start to tool_call_done
    pub start: Option<std::time::Instant>, // for live elapsed calculation
}

/// A context item loaded this turn — shown in the Context tab.
#[derive(Debug, Clone)]
pub struct ContextItem {
    pub label: String,      // e.g. "AGENTS.md" or "3 constraints"
    pub kind: ContextKind,
}

#[derive(Debug, Clone, PartialEq)]
pub enum ContextKind {
    File,
    Memory,
    Skill,
}


/// Current application state
#[derive(Debug, Clone)]
pub enum AppState {
    Idle,
    Thinking,
    Streaming {
        tokens_received: usize,
    },
    SkillRunning {
        skill_id: String,
        skill_name: String,
        current_step: usize,
        total_steps: usize,
        step_name: String,
        step_status: StepStatus,
    },
    CallingTool {
        tool: String,
        display: String,
    },
    WaitingApproval {
        message: String,
    },
    /// Interactive choice prompt — AI presents N options, user picks one
    /// or enters custom text (when allow_custom = true).
    WaitingChoice {
        title: String,
        options: Vec<ChoiceItem>,
        selected_idx: usize,
        allow_custom: bool,
        custom_input: String,
        custom_mode: bool, // true = user typing custom answer
    },
    Error(String),
}

/// A single option in a WaitingChoice prompt.
#[derive(Debug, Clone)]
pub struct ChoiceItem {
    pub id: String,
    pub label: String,
}

/// P1-5: 单步执行状态
#[derive(Debug, Clone, PartialEq)]
pub enum StepStatus {
    Running,
    WaitingApproval,
    Done,
    Failed(String),
}

/// A single chat message
#[derive(Debug, Clone)]
pub enum ChatMessage {
    User {
        text: String,
        timestamp: String,
    },
    Assistant {
        content: String,
        model: String,
        tokens: usize,
        timestamp: String,
    },
    System(String),
    /// P1-5: Skill 执行结果摘要
    SkillResult {
        skill_id: String,
        ok: bool,
        steps: usize,
        summary: String,
    },
    /// P3-1: 调用链面板
    CallChain {
        symbol: String,
        file: String,
        line: u32,
        incoming: Vec<String>,
        outgoing: Vec<String>,
    },
    /// FIX-B: 工具调用通知（内联显示在对话中）
    ToolCall {
        tool: String,
        display: String,
        ok: Option<bool>, // None=进行中, Some(true)=成功, Some(false)=失败
    },
}

/// Get current time as HH:MM string (UTC, no chrono dependency).
pub fn now_hhmm() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    let mins = (secs / 60) % (24 * 60);
    let h = mins / 60;
    let m = mins % 60;
    format!("{h:02}:{m:02}")
}

/// Main application state
pub struct App {
    pub messages: Vec<ChatMessage>,
    pub state: AppState,
    /// Session-accumulated totals (for /cost command display).
    /// tokens_input grows with each turn since it includes the full prompt+history.
    pub tokens_used: usize,
    pub tokens_input: usize,
    pub tokens_output: usize,
    pub session_cost_usd: f64,
    /// Current context window occupancy — SET (not accumulated) from the last
    /// cost_update event's input_tokens. This is what the token bar should display:
    /// "how full is my context window RIGHT NOW?"
    pub current_ctx_tokens: usize,
    /// Last turn's output token count (SET each cost_update, for ↓ display).
    pub last_out_tokens: usize,
    pub model_name: String,
    pub project_dir: String,
    /// Scroll offset in lines. 0=top (oldest), usize::MAX=bottom (clamped in ui.rs).
    /// Using usize (not u16) to support >65535 total cached lines without overflow.
    pub scroll: usize,
    pub should_quit: bool,
    pub cursor_visible: bool,
    /// Spinner frame counter — incremented each blink tick for smooth animation.
    /// Used by status bar to render animated spinner: ⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏
    pub spinner_tick: u8,
    /// 命令联想：当输入 / 开头时激活
    pub suggestions: Vec<(&'static str, &'static str)>,
    pub suggestion_idx: usize,
    pub show_suggestions: bool,

    // ── 性能优化：渲染缓存 ──────────────────────────────────────
    /// Pre-computed lines for all messages. Rebuilt only when dirty.
    pub line_cache: Option<(u16, Vec<Line<'static>>)>,
    /// True when messages changed and cache must be rebuilt before next render.
    pub cache_dirty: bool,
    /// Streaming throttle: skip redraws for N frames to reduce CPU during fast token emission.
    pub stream_skip_frames: u8,
    /// Last computed max_scroll from draw_chat_area. Used by scroll helpers to
    /// anchor from bottom before subtracting, fixing the usize::MAX - N overflow bug.
    pub last_max_scroll: usize,

    // ── Debug overlay (F12) ──────────────────────────────────────
    pub show_debug: bool,
    pub debug_log_lines: Vec<String>,
    pub debug_log_path: String,

    // ── Message queue ────────────────────────────────────────────
    pub message_queue: std::collections::VecDeque<String>,

    // ── Request timer ────────────────────────────────────────────
    pub request_start: Option<std::time::Instant>,

    // ── Activity strip ───────────────────────────────────────────
    /// When the current tool call started — used to show elapsed ms in activity strip.
    pub tool_start: Option<std::time::Instant>,
    /// Live token accumulator during streaming — incremented per token, reset at stream end.
    pub live_tokens: usize,

    // ── Work Panel (right sidebar in wide mode) ───────────────────
    /// Whether the work panel is visible (toggle with Ctrl+T).
    pub work_panel_visible: bool,
    /// Which tab is active in the work panel.
    pub work_panel_tab: WorkPanelTab,
    /// All tool calls this session, newest last.
    pub tool_history: Vec<ToolEntry>,
    /// Context items loaded this turn (files, memory, skills).
    pub context_items: Vec<ContextItem>,
    /// Current conversation turn counter — incremented on each Submit.
    pub turn_count: usize,
    /// Work panel scroll offset (tools list).
    pub work_panel_scroll: usize,

    // ── Approval mode ─────────────────────────────────────────────
    /// Current approval mode — cycled with Ctrl+A. Always shown in footer.
    pub approve_mode: ApproveMode,

    // ── Transcript mode ───────────────────────────────────────────
    /// When true (Ctrl+O), tool calls show expanded detail instead of compact badges.
    pub transcript_mode: bool,

    // ── Context window capacity ───────────────────────────────────
    pub max_context_tokens: usize,

    // ── Toast notification ────────────────────────────────────────
    /// Transient 1-line notification shown between chat and input areas.
    /// Auto-expires after NOTIF_TTL_SECS; does NOT go into chat history.
    pub notification: Option<Notification>,

    // ── Session identity ──────────────────────────────────────────
    /// When set (via `evocli session resume <id>`), all agent.stream calls use this ID
    /// instead of the CWD-based hash, enabling true cross-restart history continuity.
    /// None → fall back to CWD FNV-1a hash (default per-project bucket).
    pub override_session_id: Option<String>,

    // ── Dynamic Thinking state label ─────────────────────────────
    /// Set by soul_status "loading" events during context build / LLM call.
    /// Displayed in the input bar border instead of generic "Thinking…" so users
    /// see real-time progress (e.g. "Loading context…", "Calling LLM…").
    /// Cleared automatically when streaming starts or finishes.
    /// OpenCode/Continue.dev pattern: always show what the AI is doing.
    pub thinking_label: String,
}

/// Notification urgency level — controls icon and colour.
#[derive(Debug, Clone, PartialEq)]
pub enum NotifLevel {
    Info,
    Warn,
    Error,
}

/// A transient notification shown in the 1-line notification bar.
#[derive(Debug, Clone)]
pub struct Notification {
    pub message: String,
    pub level: NotifLevel,
    pub born: std::time::Instant,
}

/// How long (seconds) a notification stays visible before auto-dismissing.
pub const NOTIF_TTL_SECS: u64 = 6;

/// During streaming, redraw every N tokens instead of every single token.
pub const STREAM_REDRAW_EVERY: u8 = 1;

/// 所有支持的 / 命令
pub const SLASH_COMMANDS: &[(&str, &str)] = &[
    ("/help", "Show available commands and keyboard shortcuts"),
    ("/?", "Show available commands (alias)"),
    (
        "/compress",
        "Compress session history to free context space",
    ),
    (
        "/compact",
        "Compress session history to free context space (alias)",
    ),
    (
        "/undo",
        "Undo last turn: remove from history + restore git snapshot",
    ),
    (
        "/plan <task>",
        "Plan mode: read-only analysis, outputs structured PLAN",
    ),
    (
        "/btw <question>",
        "Aside question: not saved to history, no context pollution",
    ),
    ("/flows", "List automatically learned tool flows"),
    ("/add <file>", "Pin a file to context for all turns"),
    ("/add list", "Show all pinned files"),
    ("/add clear", "Remove all pinned files from context"),
    ("/chain <symbol>", "Show call chain for a code symbol"),
    ("/skills", "List available skills"),
    ("/skill <name>", "Run a skill by name"),
    ("/cost", "Show session cost and token usage"),
    ("/index", "Re-index current project"),
    ("/memory <query>", "Search project memory"),
    ("/clear", "Clear chat history"),
    (
        "/log [N]",
        "Show last N lines from the log file (default 30)",
    ),
];

impl App {
    pub fn new(model_name: String, max_context_tokens: usize) -> Self {
        // Store the full path with ~ substitution for home directory.
        // The title bar will truncate dynamically based on available space.
        let project_dir = std::env::current_dir()
            .ok()
            .map(|p| {
                // Replace home dir prefix with ~ (standard Unix convention)
                if let Some(home) = dirs::home_dir() {
                    if let Ok(rel) = p.strip_prefix(&home) {
                        let rel_str = rel.to_string_lossy().replace('\\', "/");
                        return if rel_str.is_empty() {
                            "~".to_string()
                        } else {
                            format!("~/{}", rel_str)
                        };
                    }
                }
                // Fallback: normalize separators
                p.to_string_lossy().replace('\\', "/")
            })
            .unwrap_or_else(|| ".".to_string());

        Self {
            messages: vec![ChatMessage::System(
                "Welcome to EvoCLI! Type a message and press Enter to send.  Type / for commands."
                    .into(),
            )],
            state: AppState::Idle,
            tokens_used: 0,
            tokens_input: 0,
            tokens_output: 0,
            session_cost_usd: 0.0,
            current_ctx_tokens: 0,
            last_out_tokens: 0,
            model_name,
            project_dir,
            scroll: 0,
            should_quit: false,
            cursor_visible: true,
            spinner_tick: 0,
            suggestions: vec![],
            suggestion_idx: 0,
            show_suggestions: false,
            line_cache: None,
            cache_dirty: true,
            stream_skip_frames: 0,
            last_max_scroll: 0,
            show_debug: false,
            debug_log_lines: vec![],
            debug_log_path: dirs::home_dir()
                .unwrap_or_default()
                .join(".evocli")
                .join("logs")
                .join("evocli.log")
                .to_string_lossy()
                .to_string(),
            message_queue: std::collections::VecDeque::new(),
            request_start: None,
            tool_start: None,
            live_tokens: 0,
            work_panel_visible: true,
            work_panel_tab: WorkPanelTab::Tools,
            tool_history: Vec::new(),
            context_items: Vec::new(),
            turn_count: 0,
            work_panel_scroll: 0,
            approve_mode: ApproveMode::Auto,
            transcript_mode: false,
            max_context_tokens,
            notification: None,
            override_session_id: None,
            thinking_label: String::new(),
        }
    }

    pub fn update_suggestions(&mut self, input: &str) {
        if !input.starts_with('/') {
            self.show_suggestions = false;
            self.suggestions.clear();
            return;
        }
        let input_lower = input.to_lowercase();
        self.suggestions = SLASH_COMMANDS
            .iter()
            .filter(|(cmd, _)| cmd.to_lowercase().starts_with(&input_lower))
            .copied()
            .collect();
        self.show_suggestions = !self.suggestions.is_empty();
        if self.suggestion_idx >= self.suggestions.len() {
            self.suggestion_idx = 0;
        }
    }

    pub fn suggestion_next(&mut self) {
        if !self.suggestions.is_empty() {
            self.suggestion_idx = (self.suggestion_idx + 1) % self.suggestions.len();
        }
    }

    pub fn suggestion_prev(&mut self) {
        if !self.suggestions.is_empty() {
            self.suggestion_idx = self
                .suggestion_idx
                .checked_sub(1)
                .unwrap_or(self.suggestions.len() - 1);
        }
    }

    pub fn accept_suggestion(&mut self) -> Option<String> {
        if self.show_suggestions && !self.suggestions.is_empty() {
            let cmd = self.suggestions[self.suggestion_idx].0;
            let cmd_name = cmd.split_whitespace().next().unwrap_or(cmd);
            let text = if cmd.contains('<') {
                format!("{} ", cmd_name)
            } else {
                cmd_name.to_string()
            };
            self.show_suggestions = false;
            self.suggestions.clear();
            Some(text)
        } else {
            None
        }
    }

    pub fn dismiss_suggestions(&mut self) {
        self.show_suggestions = false;
    }

    pub fn start_streaming(&mut self) {
        self.state = AppState::Streaming { tokens_received: 0 };
        self.messages.push(ChatMessage::Assistant {
            content: String::new(),
            model: self.model_name.clone(),
            tokens: 0,
            timestamp: now_hhmm(),
        });
        self.scroll = usize::MAX; // auto-scroll to bottom (clamped in ui.rs)
        self.cache_dirty = true;
        self.stream_skip_frames = 0;
    }

    pub fn append_token(&mut self, text: &str) {
        // Search from the end for the last Assistant message.
        // We cannot use messages.last_mut() because soul_status / event messages
        // may be pushed AFTER start_streaming() creates the Assistant placeholder,
        // which would make last_mut() return a System message instead.
        if let Some(ChatMessage::Assistant { content, .. }) = self
            .messages
            .iter_mut()
            .rev()
            .find(|m| matches!(m, ChatMessage::Assistant { .. }))
        {
            content.push_str(text);
        }
        if let AppState::Streaming { tokens_received } = &mut self.state {
            *tokens_received += 1;
        }
        self.live_tokens += 1;
        // Throttle: increment counter, mark dirty
        self.stream_skip_frames = self.stream_skip_frames.wrapping_add(1);
        self.cache_dirty = true;
    }

    /// Returns true if this token should trigger a redraw (streaming throttle).
    pub fn should_redraw_streaming(&self) -> bool {
        self.stream_skip_frames % STREAM_REDRAW_EVERY == 0
    }

    pub fn finish_streaming(&mut self, tokens: usize) {
        // Same rev().find() pattern as append_token: locate the last Assistant
        // message even if soul_status / event System messages were pushed after it.
        //
        // NOTE: `tokens` here is the streaming chunk COUNT, not a real token count.
        // Real token counts arrive via the `cost_update` event from Python Soul and
        // update `tokens_input`/`tokens_output` separately in handle_soul_event().
        // We do NOT add `tokens` to tokens_used/tokens_output here to avoid double-
        // counting with cost_update. The message badge is updated once cost_update
        // arrives with accurate numbers.
        let content_empty = {
            if let Some(ChatMessage::Assistant {
                tokens: t, content, ..
            }) = self
                .messages
                .iter_mut()
                .rev()
                .find(|m| matches!(m, ChatMessage::Assistant { .. }))
            {
                // Badge shows "?" until cost_update arrives with real count.
                // 0 means "not yet known" — ui.rs renders this as no badge.
                *t = 0;
                content.trim().is_empty()
            } else {
                false
            }
        };

        // If we streamed tokens but the assistant content is empty, something
        // went wrong silently (e.g. chunks routed to wrong slot).  Make it visible.
        if content_empty && tokens > 0 {
            self.messages.push(ChatMessage::System(
                "⚠️  Response received but content was empty — this is a bug. \
                 Press F12 to view logs, or try again."
                    .into(),
            ));
        }

        // Do NOT update tokens_used/tokens_output here.
        // cost_update event from Python Soul has the accurate numbers.
        // Updating here would double-count when cost_update arrives.
        self.state = AppState::Idle;
        self.stream_skip_frames = 0;
        self.live_tokens = 0;
        self.cache_dirty = true;
        self.request_start = None; // clear timer

        const MAX_MESSAGES: usize = 500;
        if self.messages.len() > MAX_MESSAGES {
            let excess = self.messages.len() - MAX_MESSAGES;
            let mut removed = 0usize;
            self.messages.retain(|m| {
                if removed >= excess {
                    return true;
                }
                if matches!(m, ChatMessage::User { .. } | ChatMessage::Assistant { .. }) {
                    removed += 1;
                    false
                } else {
                    true
                }
            });
        }
    }

    // ── Skill management ─────────────────────────────────────────

    pub fn start_skill(&mut self, skill_id: &str, skill_name: &str, total_steps: usize) {
        self.state = AppState::SkillRunning {
            skill_id: skill_id.to_string(),
            skill_name: skill_name.to_string(),
            current_step: 0,
            total_steps,
            step_name: "initializing".to_string(),
            step_status: StepStatus::Running,
        };
    }

    pub fn update_skill_step(&mut self, step_idx: usize, step_name: &str, status: StepStatus) {
        if let AppState::SkillRunning {
            current_step,
            step_name: sn,
            step_status,
            ..
        } = &mut self.state
        {
            *current_step = step_idx;
            *sn = step_name.to_string();
            *step_status = status;
        }
    }

    pub fn finish_skill(&mut self, skill_id: &str, ok: bool, steps_done: usize, summary: &str) {
        self.messages.push(ChatMessage::SkillResult {
            skill_id: skill_id.to_string(),
            ok,
            steps: steps_done,
            summary: summary.to_string(),
        });
        self.state = AppState::Idle;
        self.cache_dirty = true;
    }

    // ── Scroll helpers ───────────────────────────────────────────

    /// Scroll up by N visual rows. Anchors from last_max_scroll when at bottom.
    ///
    /// Fix: when scroll == usize::MAX (sentinel "follow bottom"), we must first
    /// anchor to the last known max_scroll before subtracting. Otherwise
    /// usize::MAX - N is still larger than max_scroll and nothing moves visually.
    pub fn scroll_up_n(&mut self, n: usize) {
        let current = if self.scroll >= self.last_max_scroll {
            self.last_max_scroll // anchor from actual bottom
        } else {
            self.scroll
        };
        self.scroll = current.saturating_sub(n);
    }

    /// Scroll down by N visual rows. Re-engages follow-bottom at end.
    pub fn scroll_down_n(&mut self, n: usize) {
        if self.scroll >= self.last_max_scroll {
            return; // already at bottom
        }
        let next = self.scroll.saturating_add(n);
        self.scroll = if next >= self.last_max_scroll {
            usize::MAX
        } else {
            next
        };
    }

    /// PgUp — scroll up 5 rows (keyboard shortcut).
    pub fn scroll_up(&mut self) {
        self.scroll_up_n(5);
    }

    /// PgDn — scroll down 5 rows (keyboard shortcut).
    pub fn scroll_down(&mut self) {
        self.scroll_down_n(5);
    }

    /// Alt+Up — fast scroll up (15 lines).
    pub fn scroll_fast_up(&mut self) {
        self.scroll_up_n(15);
    }

    /// Alt+Down — fast scroll down (15 lines).
    pub fn scroll_fast_down(&mut self) {
        self.scroll_down_n(15);
    }

    /// Ctrl+Home / Home — scroll to top (oldest messages).
    pub fn scroll_to_top(&mut self) {
        self.scroll = 0;
    }

    /// Ctrl+End / End — scroll to bottom (newest messages).
    pub fn scroll_to_bottom(&mut self) {
        self.scroll = usize::MAX; // clamped to max_scroll in draw_chat_area
    }

    /// Extract the plain text of the last Assistant message (for clipboard copy).
    /// Returns None if no assistant message exists yet.
    pub fn last_assistant_text(&self) -> Option<String> {
        self.messages.iter().rev().find_map(|m| {
            if let ChatMessage::Assistant { content, .. } = m {
                if !content.is_empty() {
                    Some(content.clone())
                } else {
                    None
                }
            } else {
                None
            }
        })
    }

    /// Copy the last Assistant message to the system clipboard via arboard.
    /// Returns Ok(chars_copied) on success, Err(description) on failure.
    pub fn copy_last_message_to_clipboard(&self) -> Result<usize, String> {
        let text = self
            .last_assistant_text()
            .ok_or_else(|| "No AI message to copy".to_string())?;
        let len = text.chars().count();
        arboard::Clipboard::new()
            .and_then(|mut cb| cb.set_text(text))
            .map_err(|e| format!("Clipboard error: {e}"))?;
        Ok(len)
    }

    /// Invalidate the render cache. Call after any message mutation.
    pub fn invalidate_cache(&mut self) {
        self.cache_dirty = true;
    }

    // ── Tool history (Work Panel) ────────────────────────────────────────────

    /// Record the start of a tool call in the tool history.
    pub fn push_tool_start(&mut self, tool: String, display: String) {
        self.tool_history.push(ToolEntry {
            turn: self.turn_count,
            tool,
            display,
            ok: None,
            duration_ms: 0,
            start: Some(std::time::Instant::now()),
        });
        // Keep history bounded to last 200 entries
        if self.tool_history.len() > 200 {
            self.tool_history.remove(0);
        }
    }

    /// Mark the most recent in-flight tool call as done.
    pub fn finish_tool(&mut self, ok: bool) {
        for entry in self.tool_history.iter_mut().rev() {
            if entry.ok.is_none() {
                entry.ok = Some(ok);
                entry.duration_ms = entry
                    .start
                    .map(|t| t.elapsed().as_millis() as u64)
                    .unwrap_or(0);
                entry.start = None;
                break;
            }
        }
    }

    /// Add or update a context item (file/memory/skill loaded this turn).
    pub fn set_context_item(&mut self, label: String, kind: ContextKind) {
        // Replace if same label exists, otherwise append
        if let Some(existing) = self.context_items.iter_mut().find(|c| c.label == label) {
            existing.kind = kind;
        } else {
            self.context_items.push(ContextItem { label, kind });
        }
    }

    /// Clear context items (called at start of each new turn).
    pub fn clear_context_items(&mut self) {
        self.context_items.clear();
    }

    /// Number of tool calls in the current turn.
    pub fn tools_this_turn(&self) -> usize {
        self.tool_history
            .iter()
            .filter(|e| e.turn == self.turn_count)
            .count()
    }

    /// Read the last `n` lines from the log file into `debug_log_lines`.
    /// Called when the user presses F12 to refresh the debug overlay.
    /// Reads at most 64 KB from the end of the file to avoid blocking.
    pub fn refresh_debug_log(&mut self, n: usize) {
        use std::io::{Read, Seek, SeekFrom};
        const MAX_READ: u64 = 65_536;
        let path = std::path::Path::new(&self.debug_log_path);
        self.debug_log_lines = match std::fs::File::open(path) {
            Ok(mut f) => {
                let size = f.metadata().map(|m| m.len()).unwrap_or(0);
                let offset = size.saturating_sub(MAX_READ);
                if offset > 0 {
                    let _ = f.seek(SeekFrom::Start(offset));
                }
                let mut buf = Vec::new();
                let _ = f.read_to_end(&mut buf);
                let text = String::from_utf8_lossy(&buf);
                text.lines()
                    .rev()
                    .take(n)
                    .collect::<Vec<_>>()
                    .into_iter()
                    .rev()
                    .map(|s| s.to_string())
                    .collect()
            }
            Err(e) => vec![format!("(cannot read log file: {})", e)],
        };
    }

    pub fn status_text(&self) -> String {
        match &self.state {
            AppState::Idle => "Idle".into(),
            AppState::Thinking => "Thinking...".into(),
            AppState::Streaming { tokens_received } => {
                format!("Streaming... ({tokens_received} tokens)")
            }
            AppState::CallingTool { display, .. } => format!("⚙ {display}"),
            AppState::WaitingApproval { .. } => "🔐 Waiting for approval (y/n)...".into(),
            AppState::WaitingChoice { .. } => "⌨ Waiting for your choice...".into(),
            AppState::SkillRunning {
                skill_name,
                current_step,
                total_steps,
                ..
            } => format!(
                "Skill: {skill_name}  step {}/{total_steps}",
                current_step + 1
            ),
            AppState::Error(msg) => format!("Error: {msg}"),
        }
    }

    pub fn is_skill_running(&self) -> bool {
        matches!(self.state, AppState::SkillRunning { .. })
    }

    pub fn queued_count(&self) -> usize {
        self.message_queue.len()
    }

    // ── Notification helpers ─────────────────────────────────────────

    /// Show a transient notification in the 1-line notification bar.
    /// Truncates to 120 chars so it always fits on one line.
    pub fn notify(&mut self, message: String, level: NotifLevel) {
        let msg: String = message.chars().take(120).collect();
        self.notification = Some(Notification {
            message: msg,
            level,
            born: std::time::Instant::now(),
        });
    }

    /// Called on each blink tick — clears expired notifications.
    pub fn tick_notification(&mut self) {
        if let Some(ref n) = self.notification {
            if n.born.elapsed().as_secs() >= NOTIF_TTL_SECS {
                self.notification = None;
            }
        }
    }
}

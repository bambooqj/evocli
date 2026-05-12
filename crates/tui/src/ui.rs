//! TUI rendering — "Conversational Clarity" design (2025)
//!
//! 设计理念（参考 Claude Code + Gemini CLI + OpenCode）：
//!   ┌─ 核心原则 ─────────────────────────────────────────────────────┐
//!   │  • 消息不加重型边框 — Claude Code 风格：用色彩和空间区分           │
//!   │  • 仅工具调用和代码块使用边框 — Gemini CLI 的 "系统动作" 设计     │
//!   │  • 动态宽度计算 — 所有分隔线自适应终端宽度                        │
//!   │  • 分层深度 — base → surface → overlay 视觉层次                  │
//!   │  • 输入框顶部锚定 — Gemini CLI 的固定底部输入风格                  │
//!   └────────────────────────────────────────────────────────────────┘
//!
//! 响应式布局：Wide(≥120) / Normal(60-119) / Compact(40-59) / Tiny(<40)

use crate::app::{App, AppState, ChatMessage, StepStatus};
use ratatui::{
    layout::{Constraint, Direction, Layout, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{
        Block, BorderType, Borders, Clear, Gauge, List, ListItem, ListState, Paragraph, Wrap,
    },
    Frame,
};
use tui_textarea::TextArea;

// ═══════════════════════════════════════════════════════════════════════════════
// COLOR SYSTEM — Catppuccin Mocha × Tokyo Night 融合
// 比纯 Tokyo Night 更柔和，长时间使用不累眼
// ═══════════════════════════════════════════════════════════════════════════════

// Backgrounds — 三层深度
const BG_BASE: Color = Color::Rgb(24, 25, 38); // #181926 极深，主背景
const BG_SURFACE: Color = Color::Rgb(30, 32, 48); // #1e2030 面板/输入框
const BG_CODE: Color = Color::Rgb(36, 38, 58); // #24263a 代码块背景（比surface深）

// Text hierarchy — 三级文字
const FG_TEXT: Color = Color::Rgb(202, 211, 245); // #cad3f5 主文字（比东京夜更亮）
const FG_SUBTEXT: Color = Color::Rgb(166, 173, 200); // #a6adc8 副文字
const FG_DIM: Color = Color::Rgb(110, 115, 141); // #6e738d 最弱（时间戳/行号）
const FG_BORDER: Color = Color::Rgb(54, 58, 79); // #363a4f 边框（足够可见）
const FG_SEP: Color = Color::Rgb(73, 77, 100); // #494d64 分隔符（比边框亮）

// User accent — 蓝色系
const USER_ACCENT: Color = Color::Rgb(138, 173, 244); // #8aadf4 用户标识
#[allow(dead_code)]
const USER_TEXT: Color = Color::Rgb(202, 211, 245); // 用户消息正文（保留供将来使用）

// AI accent — 绿色系（更柔和的薄荷绿，不刺眼）
const AI_ACCENT: Color = Color::Rgb(166, 218, 149); // #a6da95 AI标识
#[allow(dead_code)]
const AI_TEXT: Color = Color::Rgb(202, 211, 245); // AI正文（保留供将来使用）

// Semantic colors
const C_PURPLE: Color = Color::Rgb(198, 160, 246); // #c6a0f6 思考/流式 - 柔和紫
const C_ORANGE: Color = Color::Rgb(245, 169, 127); // #f5a97f 工具调用 - 桃橙色
const C_RED: Color = Color::Rgb(237, 135, 150); // #ed8796 错误/diff- - 玫瑰红
const C_GREEN: Color = Color::Rgb(166, 218, 149); // #a6da95 成功/diff+ - 薄荷绿
#[allow(dead_code)]
const C_BLUE: Color = Color::Rgb(138, 173, 244); // #8aadf4 信息/链接（保留供将来使用）
const C_CYAN: Color = Color::Rgb(145, 215, 227); // #91d7e3 调用链 - 冰川青
const C_YELLOW: Color = Color::Rgb(238, 212, 159); // #eed49f 警告/标题 - 沙金色
const C_TEAL: Color = Color::Rgb(139, 213, 202); // #8bd5ca Token/费用 - 薄荷绿

// ── Spinner ──────────────────────────────────────────────────────────────────
const SPINNERS: &[&str] = &["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
fn spinner_frame(tick: u8) -> &'static str {
    SPINNERS[(tick as usize) % SPINNERS.len()]
}

// ── Layout mode ──────────────────────────────────────────────────────────────
#[derive(Debug, Clone, Copy, PartialEq)]
enum LayoutMode {
    Wide,
    Normal,
    Compact,
    Tiny,
}
impl LayoutMode {
    fn from_width(w: u16) -> Self {
        match w {
            w if w >= 120 => Self::Wide,
            w if w >= 60 => Self::Normal,
            w if w >= 40 => Self::Compact,
            _ => Self::Tiny,
        }
    }
}

fn safe_prefix<'a>(s: &'a str, n: usize) -> &'a str {
    let end = s.char_indices().nth(n).map(|(i, _)| i).unwrap_or(s.len());
    &s[..end]
}
fn safe_tail<'a>(s: &'a str, n: usize) -> &'a str {
    let c = s.chars().count();
    if c <= n {
        return s;
    }
    let start = s.char_indices().nth(c - n).map(|(i, _)| i).unwrap_or(0);
    &s[start..]
}
fn input_area_height(h: u16) -> u16 {
    if h >= 40 {
        4
    } else if h >= 25 {
        3
    } else {
        3
    }
}

/// Word-wrap a single line of text to fit within `width` characters.
/// Returns one or more strings, each no wider than `width`.
/// Preserves leading whitespace (indent) on continuation lines.
fn word_wrap(text: &str, width: usize) -> Vec<String> {
    if width < 8 || text.chars().count() <= width {
        return vec![text.to_string()];
    }
    // Measure leading indent so continuation lines align.
    let indent_chars = text.chars().take_while(|c| *c == ' ').count();
    let indent = " ".repeat(indent_chars.min(width / 2));
    let effective_w = width.saturating_sub(indent_chars.min(width / 2));

    let mut result: Vec<String> = Vec::new();
    let mut current = String::new();
    let mut cur_len: usize = 0;

    for word in text.split(' ') {
        let wl = word.chars().count();
        if cur_len == 0 {
            if wl >= width {
                // Single oversized word: hard-break at `width`.
                let chars: Vec<char> = word.chars().collect();
                for chunk in chars.chunks(width) {
                    result.push(chunk.iter().collect());
                }
            } else {
                current.push_str(word);
                cur_len = wl;
            }
        } else if cur_len + 1 + wl <= effective_w {
            current.push(' ');
            current.push_str(word);
            cur_len += 1 + wl;
        } else {
            result.push(current.clone());
            current = format!("{indent}{word}");
            cur_len = indent_chars + wl;
        }
    }
    if !current.is_empty() {
        result.push(current);
    }
    if result.is_empty() {
        result.push(String::new());
    }
    result
}

// ═══════════════════════════════════════════════════════════════════════════════
// DRAW ENTRY
// ═══════════════════════════════════════════════════════════════════════════════
pub fn draw(f: &mut Frame, app: &mut App, textarea: &TextArea<'_>) {
    let area = f.area();
    let mode = LayoutMode::from_width(area.width);
    if area.width < 40 || area.height < 10 {
        draw_tiny_mode(f, app, area);
        return;
    }
    let input_h = input_area_height(area.height);
    let has_notif = app.notification.is_some();
    let notif_row = if has_notif {
        Constraint::Length(1)
    } else {
        Constraint::Length(0)
    };

    let constraints = if app.is_skill_running() {
        vec![
            Constraint::Length(2),
            Constraint::Min(6),
            Constraint::Length(3),
            notif_row,
            Constraint::Length(input_h),
            Constraint::Length(1),
        ]
    } else {
        vec![
            Constraint::Length(2),
            Constraint::Min(6),
            notif_row,
            Constraint::Length(input_h),
            Constraint::Length(1),
        ]
    };
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints(constraints)
        .split(area);

    if app.is_skill_running() {
        draw_title_bar(f, app, chunks[0], mode);
        draw_chat_area(f, app, chunks[1], mode);
        draw_skill_panel(f, app, chunks[2]);
        if has_notif {
            draw_notification_bar(f, app, chunks[3]);
        }
        draw_input_area(f, textarea, chunks[4]);
        draw_status_bar(f, app, chunks[5], mode);
    } else {
        draw_title_bar(f, app, chunks[0], mode);
        draw_chat_area(f, app, chunks[1], mode);
        if has_notif {
            draw_notification_bar(f, app, chunks[2]);
        }
        draw_input_area(f, textarea, chunks[3]);
        draw_status_bar(f, app, chunks[4], mode);
    }
    if app.show_suggestions && !app.suggestions.is_empty() {
        let ia = if app.is_skill_running() {
            chunks[4]
        } else {
            chunks[3]
        };
        draw_suggestions_popup(f, app, ia);
    }
    if let AppState::WaitingApproval { ref message } = app.state {
        draw_approval_modal(f, message);
    }
    if matches!(app.state, AppState::WaitingChoice { .. }) {
        draw_choice_modal(f, app);
    }
    if app.show_debug {
        draw_debug_overlay(f, app);
    }
}

// ── Tiny mode ─────────────────────────────────────────────────────────────────
fn draw_tiny_mode(f: &mut Frame, app: &App, area: Rect) {
    let s = match &app.state {
        AppState::Idle => "Ready",
        AppState::Thinking => "…",
        AppState::Streaming { .. } => "▸",
        _ => "⚙",
    };
    let p = Paragraph::new(vec![
        Line::from(vec![
            Span::styled(
                "◆ ",
                Style::default().fg(C_PURPLE).add_modifier(Modifier::BOLD),
            ),
            Span::styled(
                "EvoCLI ",
                Style::default()
                    .fg(USER_ACCENT)
                    .add_modifier(Modifier::BOLD),
            ),
            Span::styled(s, Style::default().fg(C_YELLOW)),
        ]),
        Line::from(Span::styled(
            "Terminal too small  (need ≥40×10)",
            Style::default().fg(FG_DIM),
        )),
    ])
    .block(
        Block::default()
            .borders(Borders::ALL)
            .border_type(BorderType::Rounded)
            .border_style(Style::default().fg(FG_BORDER))
            .style(Style::default().bg(BG_BASE)),
    );
    f.render_widget(p, area);
}

// ═══════════════════════════════════════════════════════════════════════════════
// TITLE BAR — 精简单行，高信息密度
// ═══════════════════════════════════════════════════════════════════════════════
fn draw_title_bar(f: &mut Frame, app: &App, area: Rect, mode: LayoutMode) {
    let cost_str = if app.session_cost_usd < 0.001 {
        String::new()
    } else {
        format!("  ${:.3}", app.session_cost_usd)
    };

    // Show "↑Xk ↓Xk" when we have accurate per-direction counts from cost_update.
    // ↑ = current context window usage (input_tokens this turn, includes history+system)
    // ↓ = this turn's generated output tokens
    // Falls back to plain token bar when no cost_update received yet.
    let tok_detail = if app.current_ctx_tokens > 0 || app.last_out_tokens > 0 {
        format!(
            "↑{} ↓{}",
            fmt_tokens(app.current_ctx_tokens),
            fmt_tokens(app.last_out_tokens)
        )
    } else {
        String::new()
    };

    let line = match mode {
        LayoutMode::Wide | LayoutMode::Normal => {
            let model_max = if mode == LayoutMode::Wide { 20 } else { 14 };
            let model: String = app.model_name.chars().take(model_max).collect();
            let bar_w = if mode == LayoutMode::Wide { 10 } else { 8 };
            let (tok_bar, tok_color) =
                token_bar(app.current_ctx_tokens, app.max_context_tokens, bar_w);

            // Dynamic path budget: use all remaining space after fixed elements.
            //  " ◆ "(3) + "EvoCLI"(6) + "  "(2) + model + "  ⌂ "(4)
            //  + "  "(2) + tok_bar + cost_str + padding(4)
            let fixed_len = 3
                + 6
                + 2
                + model.chars().count()
                + 4
                + 2
                + tok_bar.chars().count()
                + cost_str.chars().count()
                + 4;
            let dir_budget = (area.width as usize).saturating_sub(fixed_len).max(8);

            let dir = if app.project_dir.chars().count() <= dir_budget {
                app.project_dir.clone()
            } else {
                // Truncate from the LEFT so the most-specific (rightmost) path
                // components are always visible.
                format!(
                    "…{}",
                    safe_tail(&app.project_dir, dir_budget.saturating_sub(1))
                )
            };

            let mut spans = vec![
                Span::styled(
                    " ◆ ",
                    Style::default().fg(C_PURPLE).add_modifier(Modifier::BOLD),
                ),
                Span::styled(
                    "EvoCLI",
                    Style::default()
                        .fg(USER_ACCENT)
                        .add_modifier(Modifier::BOLD),
                ),
                Span::styled("  ", Style::default()),
                Span::styled(model.clone(), Style::default().fg(FG_SUBTEXT)),
                Span::styled("  ⌂ ", Style::default().fg(FG_DIM)),
                Span::styled(dir, Style::default().fg(FG_SUBTEXT)),
            ];
            if !tok_bar.is_empty() {
                spans.push(Span::styled("  ", Style::default()));
                spans.push(Span::styled(tok_bar, Style::default().fg(tok_color)));
            }
            if !tok_detail.is_empty() {
                spans.push(Span::styled("  ", Style::default()));
                spans.push(Span::styled(
                    tok_detail.clone(),
                    Style::default().fg(FG_DIM),
                ));
            }
            if !cost_str.is_empty() {
                spans.push(Span::styled(cost_str, Style::default().fg(FG_DIM)));
            }
            Line::from(spans)
        }
        LayoutMode::Compact => {
            let m: String = app.model_name.chars().take(10).collect();
            let (tok_bar, tok_color) = token_bar(app.current_ctx_tokens, app.max_context_tokens, 6);
            let mut spans = vec![
                Span::styled(
                    " ◆ ",
                    Style::default().fg(C_PURPLE).add_modifier(Modifier::BOLD),
                ),
                Span::styled(
                    "EVO",
                    Style::default()
                        .fg(USER_ACCENT)
                        .add_modifier(Modifier::BOLD),
                ),
                Span::raw("  "),
                Span::styled(m, Style::default().fg(FG_DIM)),
            ];
            if !tok_bar.is_empty() {
                spans.push(Span::raw("  "));
                spans.push(Span::styled(tok_bar, Style::default().fg(tok_color)));
            }
            Line::from(spans)
        }
        LayoutMode::Tiny => Line::from(Span::styled(" ◆ EvoCLI", Style::default().fg(USER_ACCENT))),
    };
    let para = Paragraph::new(line).block(
        Block::default()
            .borders(Borders::BOTTOM)
            .border_style(Style::default().fg(FG_BORDER))
            .style(Style::default().bg(BG_SURFACE)),
    );
    f.render_widget(para, area);
}

fn fmt_tokens(n: usize) -> String {
    if n >= 1_000_000 {
        format!("{:.1}M", n as f64 / 1_000_000.0)
    } else if n >= 1_000 {
        format!("{:.1}k", n as f64 / 1_000.0)
    } else if n > 0 {
        format!("{n}")
    } else {
        String::new()
    }
}

/// Build a compact token-usage progress bar.
///
/// Returns `(bar_str, color)` where color reflects urgency:
///   < 60 % → teal (safe)   60-80 % → yellow (watch)
///   80-95 % → orange (warn)   ≥ 95 % → red (compress session now)
///
/// Format: "[████░░░░] 15%  12k↑ 3k↓ / 128k"
///   ↑ = input tokens (the expensive part to optimize)
///   ↓ = output tokens
///
/// Returns plain token counts when max_ctx == 0 (unknown context size).
fn token_bar(used: usize, max_ctx: usize, bar_w: usize) -> (String, Color) {
    if max_ctx == 0 || bar_w == 0 {
        let s = if used > 0 {
            format!("{}tok", fmt_tokens(used))
        } else {
            String::new()
        };
        return (s, C_TEAL);
    }
    let pct = (used * 100 / max_ctx).min(100);
    let fill = (used * bar_w / max_ctx).min(bar_w);
    let bar = format!(
        "[{}{}] {}%  {}/{}",
        "█".repeat(fill),
        "░".repeat(bar_w - fill),
        pct,
        fmt_tokens(used),
        fmt_tokens(max_ctx),
    );
    let color = if pct >= 95 {
        C_RED
    } else if pct >= 80 {
        C_ORANGE
    } else if pct >= 60 {
        C_YELLOW
    } else {
        C_TEAL
    };
    (bar, color)
}

// ═══════════════════════════════════════════════════════════════════════════════
// SUGGESTIONS POPUP
// ═══════════════════════════════════════════════════════════════════════════════
fn draw_suggestions_popup(f: &mut Frame, app: &App, input_area: Rect) {
    let count = app.suggestions.len().min(8) as u16;
    if count == 0 {
        return;
    }
    let popup_h = count + 2;
    let popup_y = if input_area.y >= popup_h {
        input_area.y - popup_h
    } else {
        0
    };
    let popup_w = 64u16.min(input_area.width).max(32);
    let popup_x = input_area.x;
    let area = Rect {
        x: popup_x,
        y: popup_y,
        width: popup_w,
        height: popup_h,
    };
    f.render_widget(Clear, area);
    let cmd_w = (popup_w as usize).saturating_sub(4).min(24);
    let desc_w = (popup_w as usize).saturating_sub(cmd_w + 6);
    let items: Vec<ListItem> = app
        .suggestions
        .iter()
        .enumerate()
        .map(|(i, (cmd, desc))| {
            let sel = i == app.suggestion_idx;
            let c = safe_prefix(cmd, cmd_w);
            let content = if desc_w > 4 {
                format!("  {:<w$}  {}", c, safe_prefix(desc, desc_w), w = cmd_w)
            } else {
                format!("  {c}")
            };
            if sel {
                ListItem::new(content).style(
                    Style::default()
                        .fg(BG_BASE)
                        .bg(USER_ACCENT)
                        .add_modifier(Modifier::BOLD),
                )
            } else {
                ListItem::new(content).style(Style::default().fg(FG_TEXT).bg(BG_SURFACE))
            }
        })
        .collect();
    let list = List::new(items).block(
        Block::default()
            .borders(Borders::ALL)
            .border_type(BorderType::Rounded)
            .border_style(Style::default().fg(FG_SEP))
            .style(Style::default().bg(BG_SURFACE))
            .title(Span::styled(
                " ↑↓/Tab select · Esc close ",
                Style::default().fg(FG_DIM),
            )),
    );
    let mut state = ListState::default();
    state.select(Some(app.suggestion_idx));
    f.render_stateful_widget(list, area, &mut state);
}

// ═══════════════════════════════════════════════════════════════════════════════
// CHAT AREA — virtual scrolling + wrap-aware cache
//
// Scroll model: app.scroll is in VISUAL ROWS, not logical cache-line indices.
//
// Why: the cache can hold Lines wider than the terminal (e.g. long AI responses,
// user messages). When the Paragraph renders with Wrap, each such Line occupies
// ceil(chars / content_width) visual rows. The scroll offset must be in visual
// rows so that jumping one screen up/down moves by exactly `visible_h` rows,
// regardless of how many cache lines that spans.
//
// Algorithm:
//   1. Compute vrow[i] = visual rows for cache line i  (≥ 1)
//   2. Build vcum[] = prefix sums; total = vcum[n]
//   3. max_scroll = total − visible_h
//   4. scroll_vrow = clamp(app.scroll, 0, max_scroll)
//   5. start_idx  = largest i where vcum[i] ≤ scroll_vrow   (binary search)
//   6. row_offset  = scroll_vrow − vcum[start_idx]          (rows to skip in line start_idx)
//   7. end_idx    = first i where vcum[i] ≥ scroll_vrow + visible_h + vrow[start_idx]
//   8. Render slice[start_idx..end_idx] with Paragraph::wrap + .scroll((row_offset, 0))
//
// Performance: we only clone the visible slice (~30 lines), not all messages.
// ═══════════════════════════════════════════════════════════════════════════════
fn draw_chat_area(f: &mut Frame, app: &mut App, area: Rect, mode: LayoutMode) {
    let content_width = area.width.saturating_sub(4) as usize;
    let thinking = matches!(app.state, AppState::Thinking);
    // Reserve 1 visual row at bottom for the thinking indicator.
    // This ensures the indicator always has space and doesn't clip content.
    let visible_h = area
        .height
        .saturating_sub(2)
        .saturating_sub(if thinking { 1 } else { 0 }) as usize;
    let streaming = matches!(app.state, AppState::Streaming { .. });

    // Cache — invalidated automatically on terminal resize (width mismatch)
    let valid = app
        .line_cache
        .as_ref()
        .map(|(w, _)| *w == area.width && !app.cache_dirty)
        .unwrap_or(false);
    if !valid {
        let lines = build_all_lines(&app.messages, content_width, mode);
        app.line_cache = Some((area.width, lines));
        app.cache_dirty = false;
    }

    let all = &app.line_cache.as_ref().unwrap().1;
    let n = all.len();

    // ── Visual row counts ─────────────────────────────────────────────────────
    let vrow: Vec<usize> = all
        .iter()
        .map(|l| {
            let chars: usize = l.spans.iter().map(|s| s.content.chars().count()).sum();
            if content_width == 0 || chars == 0 {
                1
            } else {
                (chars + content_width - 1) / content_width
            }
        })
        .collect();

    // Prefix sums: vcum[i] = total visual rows before cache line i
    let mut vcum = vec![0usize; n + 1];
    for i in 0..n {
        vcum[i + 1] = vcum[i] + vrow[i];
    }
    let total_visual = vcum[n];

    let max_scroll = total_visual.saturating_sub(visible_h);
    // Save for scroll helpers (fixes usize::MAX - N overflow bug in scroll_up/down)
    app.last_max_scroll = max_scroll;
    let scroll_vrow = app.scroll.min(max_scroll);
    let at_bottom = scroll_vrow >= max_scroll;

    // ── Slice selection ───────────────────────────────────────────────────────
    let start_idx = if n == 0 {
        0
    } else {
        vcum.partition_point(|&c| c <= scroll_vrow)
            .saturating_sub(1)
            .min(n - 1)
    };
    let row_offset = scroll_vrow.saturating_sub(vcum[start_idx]);

    let need_end = scroll_vrow + visible_h + vrow.get(start_idx).copied().unwrap_or(1);
    let end_idx = vcum.partition_point(|&c| c < need_end).min(n);

    let slice = if n == 0 {
        &all[..]
    } else {
        &all[start_idx..end_idx]
    };

    // ── Streaming cursor ──────────────────────────────────────────────────────
    // Show a blinking dot at the end of the last streaming line.
    // Guard: if the last line is already at (or near) full width, appending " ·"
    // would cause ratatui's Wrap to push it onto a new line, looking disconnected.
    // In that case we push the cursor as a dedicated new line instead.
    let ci = slice.len().saturating_sub(1);
    let mut render: Vec<Line<'static>> =
        if streaming && app.cursor_visible && at_bottom && !slice.is_empty() {
            let last_line_chars: usize = slice[ci]
                .spans
                .iter()
                .map(|s| s.content.chars().count())
                .sum();
            let cursor_fits = content_width > 2 && last_line_chars + 2 < content_width;
            if cursor_fits {
                let mut last = slice[ci].clone();
                last.spans
                    .push(Span::styled(" ·", Style::default().fg(C_PURPLE)));
                let mut v = slice[..ci].to_vec();
                v.push(last);
                v
            } else {
                // Cursor on its own line — avoids ratatui Wrap pushing it to unexpected position
                let mut v = slice.to_vec();
                v.push(Line::from(Span::styled(
                    "  ·",
                    Style::default().fg(C_PURPLE),
                )));
                v
            }
        } else {
            slice.to_vec()
        };

    // ── Thinking animation ────────────────────────────────────────────────────
    // When the AI hasn't started streaming yet (Thinking state), show a spinner
    // indicator in the chat area below the last message.  This gives immediate
    // visual feedback inside the conversation — the status-bar spinner alone is
    // too small and easy to miss.
    // The indicator animates because spinner_tick changes every 500 ms (blink timer).
    if thinking && at_bottom {
        let spin = spinner_frame(app.spinner_tick);
        let model_s: String = app.model_name.chars().take(18).collect();
        render.push(Line::from(vec![
            Span::styled(
                "  ◆ ",
                Style::default().fg(AI_ACCENT).add_modifier(Modifier::BOLD),
            ),
            Span::styled(model_s, Style::default().fg(AI_ACCENT)),
            Span::styled(format!("  {spin}"), Style::default().fg(C_PURPLE)),
        ]));
    }

    // ── Scroll indicator ──────────────────────────────────────────────────────
    let title = if max_scroll > 0 {
        let pct = ((scroll_vrow as u32 * 100) / (max_scroll as u32 + 1)).min(99) as u16;
        let ind = if scroll_vrow == 0 {
            "top".to_string()
        } else if at_bottom {
            "end".to_string()
        } else {
            format!("{pct}%")
        };
        Span::styled(format!(" Messages  {ind} "), Style::default().fg(FG_DIM))
    } else {
        Span::styled(" Messages ", Style::default().fg(FG_DIM))
    };

    let block = Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(FG_BORDER))
        .style(Style::default().bg(BG_BASE))
        .title(title);

    f.render_widget(
        Paragraph::new(render)
            .block(block)
            .wrap(Wrap { trim: false })
            .scroll((row_offset.min(u16::MAX as usize) as u16, 0)),
        area,
    );
}

// ═══════════════════════════════════════════════════════════════════════════════
// MESSAGE RENDERER — Claude Code 风格：简洁，无重型边框
//
// 视觉层级：
//   ▌ You                    ← 彩色 gutter 标识发言者
//     Message text here      ← 两空格缩进，干净
//
//   ◆ model-name · 1.2k      ← AI标识 + token信息
//     Response text here      ← 两空格缩进
//     ╭─ rust ────────────   ← 代码块：有边框（系统动作）
//     │  fn example() { }    ← 代码内容，特殊背景
//     ╰───────────────────
//
//   ✧ fs.read "src/main.rs" ← 工具调用：简洁 icon + 描述
// ═══════════════════════════════════════════════════════════════════════════════
fn build_all_lines(
    messages: &[ChatMessage],
    content_width: usize,
    mode: LayoutMode,
) -> Vec<Line<'static>> {
    let mut out: Vec<Line<'static>> = Vec::with_capacity(messages.len() * 8);
    let sep_w = content_width.saturating_sub(4); // Width for separators

    for msg in messages {
        match msg {
            // ── User message ──────────────────────────────────────────────
            ChatMessage::User(text) => {
                // Thin separator above user message
                out.push(Line::from(vec![
                    Span::styled("  ", Style::default()),
                    Span::styled("─".repeat(sep_w.min(40)), Style::default().fg(FG_BORDER)),
                ]));
                out.push(Line::from(vec![
                    Span::styled(
                        "  ▌ ",
                        Style::default()
                            .fg(USER_ACCENT)
                            .add_modifier(Modifier::BOLD),
                    ),
                    Span::styled(
                        "You",
                        Style::default()
                            .fg(USER_ACCENT)
                            .add_modifier(Modifier::BOLD),
                    ),
                ]));
                for raw in text.lines() {
                    out.push(Line::from(vec![
                        Span::raw("    "),
                        Span::raw(raw.to_string()),
                    ]));
                }
                out.push(Line::from(""));
            }

            // ── AI response ───────────────────────────────────────────────
            ChatMessage::Assistant {
                content,
                model,
                tokens,
            } => {
                let model_s: String = if matches!(mode, LayoutMode::Compact | LayoutMode::Tiny) {
                    "AI".into()
                } else {
                    model.chars().take(18).collect()
                };
                // Show real token count from cost_update (not streaming chunk count).
                // Format: "·3.2k" for output-only, or just blank during streaming.
                let tok_s = if *tokens > 0 && !matches!(mode, LayoutMode::Tiny) {
                    format!("  ·  {}↓", fmt_tokens(*tokens))
                } else {
                    String::new()
                };

                out.push(Line::from(vec![
                    Span::styled(
                        "  ◆ ",
                        Style::default().fg(AI_ACCENT).add_modifier(Modifier::BOLD),
                    ),
                    Span::styled(
                        model_s,
                        Style::default().fg(AI_ACCENT).add_modifier(Modifier::BOLD),
                    ),
                    Span::styled(tok_s, Style::default().fg(FG_DIM)),
                ]));

                let mut in_diff = false;
                let mut in_code = false;
                let mut code_lang = String::new();

                for raw in content.lines() {
                    let lines = render_ai_line(
                        raw,
                        &mut in_diff,
                        &mut in_code,
                        &mut code_lang,
                        content_width,
                        sep_w,
                    );
                    out.extend(lines);
                }
                // Close unclosed code block
                if in_code {
                    out.push(Line::from(vec![
                        Span::raw("    "),
                        Span::styled(
                            "╰".to_string() + &"─".repeat(sep_w.min(40)),
                            Style::default().fg(FG_SEP),
                        ),
                    ]));
                }
                out.push(Line::from(""));
            }

            // ── System ────────────────────────────────────────────────────
            ChatMessage::System(text) => {
                // Split by newlines AND word-wrap long lines so multiline system
                // messages (e.g. /help output, error blocks) render correctly.
                let wrap_w = content_width.saturating_sub(4); // 4 = "  ─ " prefix width
                let mut printed = false;
                let mut first_visual = true;
                for raw_line in text.lines() {
                    for segment in word_wrap(raw_line, wrap_w) {
                        let prefix = if first_visual {
                            first_visual = false;
                            Span::styled("  ─ ", Style::default().fg(FG_DIM))
                        } else {
                            Span::styled("    ", Style::default())
                        };
                        out.push(Line::from(vec![
                            prefix,
                            Span::styled(segment, Style::default().fg(FG_DIM)),
                        ]));
                        printed = true;
                    }
                }
                // Empty system message — still render the dash so it's not invisible.
                if !printed {
                    out.push(Line::from(vec![Span::styled(
                        "  ─ ",
                        Style::default().fg(FG_DIM),
                    )]));
                }
                out.push(Line::from(""));
            }

            // ── Tool call — compact badge (Claude Code 风格) ──────────────
            ChatMessage::ToolCall { display, ok, .. } => {
                let (icon, accent) = match ok {
                    None => ("  ↻ ", C_ORANGE),
                    Some(true) => ("  ✓ ", C_GREEN),
                    Some(false) => ("  ✗ ", C_RED),
                };
                let max_d = content_width.saturating_sub(8);
                let d: String = display.chars().take(max_d).collect();
                let ell = if display.chars().count() > max_d {
                    "…"
                } else {
                    ""
                };
                out.push(Line::from(vec![
                    Span::styled(icon, Style::default().fg(accent)),
                    Span::styled(
                        format!("{d}{ell}"),
                        Style::default().fg(if ok.is_none() { FG_SUBTEXT } else { FG_DIM }),
                    ),
                ]));
            }

            // ── Skill result ──────────────────────────────────────────────
            ChatMessage::SkillResult {
                skill_id,
                ok,
                steps,
                summary,
            } => {
                let (icon, color) = if *ok {
                    ("  ✓ ", C_GREEN)
                } else {
                    ("  ✗ ", C_RED)
                };
                out.push(Line::from(vec![
                    Span::styled(
                        icon,
                        Style::default().fg(color).add_modifier(Modifier::BOLD),
                    ),
                    Span::styled(
                        format!(
                            "Skill '{}' {}  ({} steps)",
                            skill_id,
                            if *ok { "done" } else { "failed" },
                            steps
                        ),
                        Style::default().fg(color),
                    ),
                ]));
                if !summary.is_empty() {
                    out.push(Line::from(vec![
                        Span::raw("    "),
                        Span::styled(summary.clone(), Style::default().fg(FG_DIM)),
                    ]));
                }
                out.push(Line::from(""));
            }

            // ── Call chain ────────────────────────────────────────────────
            ChatMessage::CallChain {
                symbol,
                file,
                line,
                incoming,
                outgoing,
            } => {
                let max_w = content_width.saturating_sub(12);
                out.push(Line::from(vec![
                    Span::styled("  ⌕ ", Style::default().fg(C_CYAN)),
                    Span::styled(
                        symbol.clone(),
                        Style::default().fg(FG_TEXT).add_modifier(Modifier::BOLD),
                    ),
                    Span::styled(format!("  {file}:{line}"), Style::default().fg(FG_DIM)),
                ]));
                out.push(Line::from(Span::styled(
                    format!("    ▲ callers ({})", incoming.len()),
                    Style::default().fg(C_YELLOW),
                )));
                if incoming.is_empty() {
                    out.push(Line::from(Span::styled(
                        "      (none)",
                        Style::default().fg(FG_DIM),
                    )));
                } else {
                    for (j, c) in incoming.iter().enumerate() {
                        let p = if j == incoming.len() - 1 {
                            "      └ "
                        } else {
                            "      ├ "
                        };
                        let cd: String = c.chars().take(max_w).collect();
                        out.push(Line::from(Span::styled(
                            format!("{p}{cd}"),
                            Style::default().fg(C_YELLOW),
                        )));
                    }
                }
                out.push(Line::from(Span::styled(
                    format!("    ▼ callees ({})", outgoing.len()),
                    Style::default().fg(C_CYAN),
                )));
                if outgoing.is_empty() {
                    out.push(Line::from(Span::styled(
                        "      (none)",
                        Style::default().fg(FG_DIM),
                    )));
                } else {
                    for (j, c) in outgoing.iter().enumerate() {
                        let p = if j == outgoing.len() - 1 {
                            "      └ "
                        } else {
                            "      ├ "
                        };
                        let cd: String = c.chars().take(max_w).collect();
                        out.push(Line::from(Span::styled(
                            format!("{p}{cd}"),
                            Style::default().fg(C_CYAN),
                        )));
                    }
                }
                out.push(Line::from(""));
            }
        }
    }
    out
}

// ── AI message line renderer ──────────────────────────────────────────────────
/// Renders one source line from an AI message into one or more `Line<'static>`.
/// Returns `Vec` so long plain-text lines can be word-wrapped without breaking
/// the cache-based virtual scroll (Line count == visual row count).
fn render_ai_line(
    line: &str,
    in_diff: &mut bool,
    in_code: &mut bool,
    code_lang: &mut String,
    content_width: usize,
    sep_w: usize,
) -> Vec<Line<'static>> {
    // Helper: single-line shortcut
    macro_rules! one {
        ($l:expr) => {
            vec![$l]
        };
    }

    // Code fence open/close
    if line.starts_with("```") {
        if *in_code {
            *in_code = false;
            *code_lang = String::new();
            return one!(Line::from(vec![
                Span::raw("    "),
                Span::styled(
                    "╰".to_string() + &"─".repeat(sep_w.min(40)),
                    Style::default().fg(FG_SEP)
                ),
            ]));
        } else {
            *in_code = true;
            let lang = line.trim_start_matches('`').trim().to_string();
            *code_lang = lang.clone();
            let tag = if lang.is_empty() {
                " code ".to_string()
            } else {
                format!(" {lang} ")
            };
            let rest_w = sep_w.min(40).saturating_sub(tag.len() + 2);
            return one!(Line::from(vec![
                Span::raw("    "),
                Span::styled("╭─", Style::default().fg(FG_SEP)),
                Span::styled(
                    tag,
                    Style::default().fg(C_YELLOW).add_modifier(Modifier::BOLD)
                ),
                Span::styled("─".repeat(rest_w), Style::default().fg(FG_SEP)),
            ]));
        }
    }

    if *in_code {
        // Code line — BG_CODE background creates visual depth
        return one!(Line::from(vec![
            Span::styled("  │ ", Style::default().fg(FG_SEP)),
            Span::styled(line.to_string(), Style::default().fg(FG_TEXT).bg(BG_CODE)),
        ]));
    }

    // Diff detection
    if line.starts_with("--- ") || line.starts_with("+++ ") || line.starts_with("diff ") {
        *in_diff = true;
    }
    if *in_diff {
        if line.starts_with('+') && !line.starts_with("+++") {
            return one!(Line::from(vec![
                Span::raw("    "),
                Span::styled(
                    "+ ".to_string(),
                    Style::default().fg(C_GREEN).add_modifier(Modifier::BOLD)
                ),
                Span::styled(line[1..].to_string(), Style::default().fg(C_GREEN)),
            ]));
        }
        if line.starts_with('-') && !line.starts_with("---") {
            return one!(Line::from(vec![
                Span::raw("    "),
                Span::styled(
                    "- ".to_string(),
                    Style::default().fg(C_RED).add_modifier(Modifier::BOLD)
                ),
                Span::styled(line[1..].to_string(), Style::default().fg(C_RED)),
            ]));
        }
        if line.starts_with("@@") {
            return one!(Line::from(vec![
                Span::raw("    "),
                Span::styled(line.to_string(), Style::default().fg(C_CYAN))
            ]));
        }
        if line.starts_with("---") || line.starts_with("+++") || line.starts_with("diff ") {
            return one!(Line::from(vec![
                Span::raw("    "),
                Span::styled(
                    line.to_string(),
                    Style::default()
                        .fg(USER_ACCENT)
                        .add_modifier(Modifier::BOLD)
                )
            ]));
        }
    }

    // H1 — solid background block (OpenCode / Claude's big heading style)
    if line.starts_with("# ") {
        let text = &line[2..];
        let w = content_width.saturating_sub(6);
        let padded = format!("{:<w$}", text, w = w.min(text.len() + 2));
        return one!(Line::from(vec![
            Span::raw("  "),
            Span::styled(
                format!(" {padded} "),
                Style::default()
                    .fg(BG_BASE)
                    .bg(C_YELLOW)
                    .add_modifier(Modifier::BOLD)
            )
        ]));
    }
    if line.starts_with("## ") {
        return one!(Line::from(vec![
            Span::raw("    "),
            Span::styled(
                line[3..].to_string(),
                Style::default().fg(C_YELLOW).add_modifier(Modifier::BOLD)
            )
        ]));
    }
    if line.starts_with("### ") {
        return one!(Line::from(vec![
            Span::raw("    "),
            Span::styled(line[4..].to_string(), Style::default().fg(C_YELLOW))
        ]));
    }

    // Bold
    if line.contains("**") {
        return one!(render_bold(line));
    }

    // Inline code
    if line.contains('`') {
        let parts: Vec<&str> = line.split('`').collect();
        if parts.len() > 1 {
            let mut spans = vec![Span::raw("    ")];
            for (j, p) in parts.iter().enumerate() {
                if j % 2 == 0 {
                    if !p.is_empty() {
                        spans.push(Span::raw(p.to_string()));
                    }
                } else {
                    spans.push(Span::styled(
                        p.to_string(),
                        Style::default().fg(C_ORANGE).bg(BG_CODE),
                    ));
                }
            }
            return one!(Line::from(spans));
        }
    }

    // List items
    if line.starts_with("- ") || line.starts_with("* ") {
        return one!(Line::from(vec![
            Span::raw("    "),
            Span::styled("• ", Style::default().fg(USER_ACCENT)),
            Span::raw(line[2..].to_string()),
        ]));
    }
    if line.starts_with("• ") {
        return one!(Line::from(vec![
            Span::raw("    "),
            Span::styled("• ", Style::default().fg(USER_ACCENT)),
            Span::raw(line["• ".len()..].to_string()),
        ]));
    }

    // Numbered list
    let first2: Vec<char> = line.chars().take(4).collect();
    if !first2.is_empty() && first2[0].is_ascii_digit() {
        let rest: String = line.chars().collect();
        if rest.contains(". ") || rest.contains(") ") {
            return one!(Line::from(vec![
                Span::raw("    "),
                Span::raw(line.to_string())
            ]));
        }
    }

    // Horizontal rule
    if line.trim() == "---" || line.trim() == "===" {
        return one!(Line::from(vec![
            Span::raw("    "),
            Span::styled("─".repeat(sep_w.min(36)), Style::default().fg(FG_BORDER))
        ]));
    }

    // Plain text — word-wrap long paragraphs so they don't get truncated.
    if line.is_empty() {
        return one!(Line::from(""));
    }
    let wrap_w = content_width.saturating_sub(4); // 4 = "    " indent
    word_wrap(line, wrap_w)
        .into_iter()
        .map(|segment| Line::from(vec![Span::raw("    "), Span::raw(segment)]))
        .collect()
}

fn render_bold(line: &str) -> Line<'static> {
    let mut spans = vec![Span::raw("    ")];
    let mut rem = line;
    while !rem.is_empty() {
        if let Some(s) = rem.find("**") {
            if s > 0 {
                spans.push(Span::raw(rem[..s].to_string()));
            }
            let after = &rem[s + 2..];
            if let Some(e) = after.find("**") {
                spans.push(Span::styled(
                    after[..e].to_string(),
                    Style::default().fg(FG_TEXT).add_modifier(Modifier::BOLD),
                ));
                rem = &after[e + 2..];
            } else {
                spans.push(Span::raw(rem[s..].to_string()));
                break;
            }
        } else {
            spans.push(Span::raw(rem.to_string()));
            break;
        }
    }
    Line::from(spans)
}

// ═══════════════════════════════════════════════════════════════════════════════
// SKILL PANEL
// ═══════════════════════════════════════════════════════════════════════════════
fn draw_skill_panel(f: &mut Frame, app: &App, area: Rect) {
    let AppState::SkillRunning {
        skill_name,
        current_step,
        total_steps,
        step_name,
        step_status,
        ..
    } = &app.state
    else {
        return;
    };
    let pct = if *total_steps > 0 {
        (*current_step * 100 / total_steps) as u16
    } else {
        0
    };
    let split = if area.width >= 80 {
        [Constraint::Percentage(65), Constraint::Percentage(35)]
    } else {
        [Constraint::Percentage(75), Constraint::Percentage(25)]
    };
    let inner = Layout::default()
        .direction(Direction::Horizontal)
        .constraints(split)
        .split(area);
    let (gc, icon) = match step_status {
        StepStatus::Running => (C_PURPLE, "◐"),
        StepStatus::WaitingApproval => (C_YELLOW, "⏸"),
        StepStatus::Done => (C_GREEN, "✓"),
        StepStatus::Failed(_) => (C_RED, "✗"),
    };
    let nm: String = skill_name
        .chars()
        .take((inner[0].width as usize).saturating_sub(12).max(4))
        .collect();
    let gauge = Gauge::default()
        .block(
            Block::default()
                .borders(Borders::ALL)
                .border_type(BorderType::Rounded)
                .border_style(Style::default().fg(FG_BORDER))
                .style(Style::default().bg(BG_SURFACE))
                .title(Span::styled(
                    format!(" {icon} {nm} "),
                    Style::default().fg(gc).add_modifier(Modifier::BOLD),
                )),
        )
        .gauge_style(Style::default().fg(gc).bg(BG_CODE))
        .percent(pct)
        .label(Span::styled(
            format!("{}/{total_steps}", current_step + 1),
            Style::default().fg(FG_TEXT),
        ));
    f.render_widget(gauge, inner[0]);
    let mw = (inner[1].width as usize).saturating_sub(4).max(4);
    let sd: String = step_name.chars().take(mw).collect();
    let info = vec![
        Line::from(Span::styled(format!(" {sd}"), Style::default().fg(FG_TEXT))),
        if matches!(step_status, StepStatus::WaitingApproval) {
            Line::from(Span::styled(
                " ⏸ approve? y/n",
                Style::default().fg(C_YELLOW),
            ))
        } else if let StepStatus::Failed(e) = step_status {
            Line::from(Span::styled(
                format!(" ✗ {}", safe_prefix(e, mw.saturating_sub(4))),
                Style::default().fg(C_RED),
            ))
        } else {
            Line::from(Span::styled(" working…", Style::default().fg(C_PURPLE)))
        },
    ];
    f.render_widget(
        Paragraph::new(info).block(
            Block::default()
                .borders(Borders::ALL)
                .border_type(BorderType::Rounded)
                .border_style(Style::default().fg(FG_BORDER))
                .style(Style::default().bg(BG_SURFACE)),
        ),
        inner[1],
    );
}

// ═══════════════════════════════════════════════════════════════════════════════
// APPROVAL MODAL
// ═══════════════════════════════════════════════════════════════════════════════
fn draw_approval_modal(f: &mut Frame, message: &str) {
    let area = f.area();
    let ml = message.lines().count().max(1);
    let ph = (ml as u16 + 5)
        .clamp(7, 14)
        .min(area.height.saturating_sub(2));
    let pw = match area.width {
        w if w >= 100 => (w * 60 / 100).clamp(44, 72),
        w if w >= 60 => (w * 70 / 100).clamp(36, 58),
        w => w.saturating_sub(4).max(28),
    };
    let px = (area.width.saturating_sub(pw)) / 2;
    let py = (area.height.saturating_sub(ph)) / 2;
    let pa = Rect {
        x: px,
        y: py,
        width: pw,
        height: ph,
    };
    f.render_widget(Clear, pa);
    let iw = pw.saturating_sub(4) as usize;
    let mut lines: Vec<Line> = vec![Line::from("")];
    for l in message.lines().take(8) {
        let d = if l.chars().count() > iw {
            format!("  {}…", safe_prefix(l, iw.saturating_sub(1)))
        } else {
            format!("  {l}")
        };
        lines.push(Line::from(Span::styled(d, Style::default().fg(FG_TEXT))));
    }
    if ml > 8 {
        lines.push(Line::from(Span::styled("  …", Style::default().fg(FG_DIM))));
    }
    lines.push(Line::from(""));
    lines.push(Line::from(vec![
        Span::styled(
            "  [y] ",
            Style::default().fg(C_GREEN).add_modifier(Modifier::BOLD),
        ),
        Span::styled("Allow      ", Style::default().fg(FG_SUBTEXT)),
        Span::styled(
            "[n] ",
            Style::default().fg(C_RED).add_modifier(Modifier::BOLD),
        ),
        Span::styled("Deny", Style::default().fg(FG_SUBTEXT)),
    ]));
    f.render_widget(
        Paragraph::new(lines)
            .block(
                Block::default()
                    .borders(Borders::ALL)
                    .border_type(BorderType::Rounded)
                    .border_style(Style::default().fg(C_YELLOW))
                    .style(Style::default().bg(BG_SURFACE))
                    .title(Span::styled(
                        " 🔒 Needs approval ",
                        Style::default().fg(C_YELLOW).add_modifier(Modifier::BOLD),
                    )),
            )
            .wrap(Wrap { trim: false }),
        pa,
    );
}

// ═══════════════════════════════════════════════════════════════════════════════
// CHOICE MODAL — prompt.choice: numbered list + optional custom input
//
// Layout (centred):
//   ╭── title ─────────────────────────────────╮
//   │                                           │
//   │  ▶ 1  Option A           ← selected      │
//   │    2  Option B                            │
//   │    3  Option C                            │
//   │    ─────────────────────                  │
//   │  ✎  custom input  (press i/c)            │
//   │                                           │
//   │  [↑↓/1-9] navigate  [Enter] confirm  [Esc] cancel │
//   ╰───────────────────────────────────────────╯
// ═══════════════════════════════════════════════════════════════════════════════
fn draw_choice_modal(f: &mut Frame, app: &App) {
    let AppState::WaitingChoice {
        ref title,
        ref options,
        selected_idx,
        allow_custom,
        ref custom_input,
        custom_mode,
    } = app.state
    else {
        return;
    };

    let area = f.area();

    // Modal sizing: 60% wide, fit content height (min 10, max 24)
    let opt_rows = options.len() as u16;
    let inner_h = 3           // title + blank + blank
        + opt_rows
        + if allow_custom { 2 } else { 0 }  // separator + custom row
        + if custom_mode  { 2 } else { 0 }  // input field + blank
        + 2; // hint line + bottom padding
    let ph = inner_h.clamp(10, 24).min(area.height.saturating_sub(4));
    let pw = match area.width {
        w if w >= 100 => (w * 60 / 100).clamp(50, 80),
        w if w >= 60 => (w * 75 / 100).clamp(40, 60),
        w => w.saturating_sub(4).max(32),
    };
    let px = (area.width.saturating_sub(pw)) / 2;
    let py = (area.height.saturating_sub(ph)) / 2;
    let pa = Rect {
        x: px,
        y: py,
        width: pw,
        height: ph,
    };

    f.render_widget(Clear, pa);

    let iw = pw.saturating_sub(4) as usize;
    let mut lines: Vec<Line> = vec![Line::from("")];

    // Title
    let title_s: String = title.chars().take(iw).collect();
    lines.push(Line::from(Span::styled(
        format!("  {title_s}"),
        Style::default().fg(FG_TEXT).add_modifier(Modifier::BOLD),
    )));
    lines.push(Line::from(""));

    // Option list
    for (i, opt) in options.iter().enumerate() {
        let num = i + 1;
        let sel = i == selected_idx;
        let prefix = if sel { "▶ " } else { "  " };
        let label: String = opt.label.chars().take(iw.saturating_sub(6)).collect();
        let style = if sel {
            Style::default()
                .fg(USER_ACCENT)
                .add_modifier(Modifier::BOLD)
        } else {
            Style::default().fg(FG_SUBTEXT)
        };
        lines.push(Line::from(Span::styled(
            format!("  {prefix}{num:<2} {label}"),
            style,
        )));
    }

    // Custom input option
    if allow_custom {
        lines.push(Line::from(Span::styled(
            format!("  {}", "─".repeat(iw.min(36))),
            Style::default().fg(FG_SEP),
        )));
        if custom_mode {
            // Show active text field
            lines.push(Line::from(Span::styled(
                "  ✎  Custom answer:",
                Style::default().fg(C_YELLOW).add_modifier(Modifier::BOLD),
            )));
            let display: String = custom_input.chars().take(iw.saturating_sub(8)).collect();
            let cursor = if app.cursor_visible { "█" } else { " " };
            lines.push(Line::from(Span::styled(
                format!("  ▶  [{display}{cursor}]"),
                Style::default().fg(C_YELLOW),
            )));
        } else {
            lines.push(Line::from(Span::styled(
                "  ✎  Enter custom answer…  (i / c)",
                Style::default().fg(FG_DIM),
            )));
        }
    }

    lines.push(Line::from(""));

    // Hint line
    let hint = if custom_mode {
        "  [Enter] confirm  [Esc] back to list"
    } else if allow_custom {
        "  [↑↓/1-9] select  [Enter] confirm  [i/c] custom  [Esc] cancel"
    } else {
        "  [↑↓/1-9] select  [Enter] confirm  [Esc] cancel"
    };
    let hint_s: String = hint.chars().take(iw).collect();
    lines.push(Line::from(Span::styled(
        hint_s,
        Style::default().fg(FG_DIM),
    )));

    f.render_widget(
        Paragraph::new(lines)
            .block(
                Block::default()
                    .borders(Borders::ALL)
                    .border_type(BorderType::Rounded)
                    .border_style(Style::default().fg(USER_ACCENT))
                    .style(Style::default().bg(BG_SURFACE))
                    .title(Span::styled(
                        " ⌨  Choose an option ",
                        Style::default()
                            .fg(USER_ACCENT)
                            .add_modifier(Modifier::BOLD),
                    )),
            )
            .wrap(Wrap { trim: false }),
        pa,
    );
}

// ═══════════════════════════════════════════════════════════════════════════════
// DEBUG LOG OVERLAY — F12 toggle
// Shows the last N lines from ~/.evocli/logs/evocli.log in a floating panel.
// Both [SOUL] (Python) and Rust tracing entries are visible here.
// ═══════════════════════════════════════════════════════════════════════════════
fn draw_debug_overlay(f: &mut Frame, app: &App) {
    let area = f.area();

    // Panel geometry: 75% width, 55% height, bottom-right anchor so it overlaps
    // as little chat content as possible.
    let pw = (area.width as f32 * 0.75) as u16;
    let ph = (area.height as f32 * 0.55) as u16;
    let pw = pw.clamp(50, area.width.saturating_sub(2));
    let ph = ph.clamp(8, area.height.saturating_sub(4));
    let px = area.width.saturating_sub(pw).saturating_sub(1);
    let py = area.height.saturating_sub(ph).saturating_sub(1);
    let pa = Rect {
        x: px,
        y: py,
        width: pw,
        height: ph,
    };

    f.render_widget(Clear, pa);

    // Visible line count inside the border (reserve 2 for top/bottom borders + 1 footer)
    let inner_h = ph.saturating_sub(3) as usize;
    let lines_to_show = &app.debug_log_lines;

    // Take the most-recent `inner_h` lines
    let start = lines_to_show.len().saturating_sub(inner_h);
    let visible = &lines_to_show[start..];

    // Color-code by log level keyword
    let rendered: Vec<Line> = visible
        .iter()
        .map(|line| {
            let (style, marker) =
                if line.contains(" ERROR ") || line.contains("[ERROR]") || line.contains("⛔") {
                    (Style::default().fg(C_RED), "")
                } else if line.contains(" WARNING ")
                    || line.contains("[SOUL] WARNING")
                    || line.contains("⚠")
                {
                    (Style::default().fg(C_YELLOW), "")
                } else if line.contains("[SOUL]") {
                    (Style::default().fg(C_CYAN), "")
                } else if line.contains("ERROR") {
                    (Style::default().fg(C_RED), "")
                } else {
                    (Style::default().fg(FG_SUBTEXT), "")
                };
            let _ = marker;
            // Truncate to panel width (minus 2 for border padding)
            let max_chars = pw.saturating_sub(4) as usize;
            let display: String = line.chars().take(max_chars).collect();
            Line::from(Span::styled(display, style))
        })
        .collect();

    // Footer line: log path + refresh hint
    let footer = format!(" {} ", app.debug_log_path);
    let footer_display: String = footer.chars().take(pw.saturating_sub(4) as usize).collect();

    let title_style = Style::default().fg(C_ORANGE).add_modifier(Modifier::BOLD);
    let block = Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(C_ORANGE))
        .style(Style::default().bg(BG_BASE))
        .title(Span::styled(
            " 🪵 Debug Log  (F12:close · F12 again:refresh) ",
            title_style,
        ))
        .title_bottom(Span::styled(footer_display, Style::default().fg(FG_DIM)));

    f.render_widget(
        Paragraph::new(rendered)
            .block(block)
            .wrap(Wrap { trim: true }),
        pa,
    );
}

// ═══════════════════════════════════════════════════════════════════════════════
// NOTIFICATION BAR — 1-line transient alert between chat and input
//
// Appears only when app.notification is Some.  Auto-expires after NOTIF_TTL_SECS.
// Does NOT write into chat history — the layout row is 0-height when empty.
// ═══════════════════════════════════════════════════════════════════════════════
fn draw_notification_bar(f: &mut Frame, app: &App, area: Rect) {
    use crate::app::{NotifLevel, NOTIF_TTL_SECS};
    let Some(ref notif) = app.notification else {
        return;
    };

    // Fade the colour as the notification ages
    let elapsed = notif.born.elapsed().as_secs();
    let (bg, fg) = match notif.level {
        NotifLevel::Error => (Color::Rgb(80, 30, 30), C_RED),
        NotifLevel::Warn => (Color::Rgb(60, 50, 20), C_YELLOW),
        NotifLevel::Info => (BG_SURFACE, FG_SUBTEXT),
    };

    // Countdown hint: shows remaining seconds so the user knows it'll disappear
    let remaining = NOTIF_TTL_SECS.saturating_sub(elapsed);
    let max_msg = (area.width as usize).saturating_sub(14);
    let msg: String = notif.message.chars().take(max_msg).collect();
    let ell = if notif.message.chars().count() > max_msg {
        "…"
    } else {
        ""
    };

    let line = Line::from(vec![
        Span::styled(format!("  {msg}{ell}"), Style::default().fg(fg).bg(bg)),
        Span::styled(
            format!("  {}s  F12 ▸ ", remaining),
            Style::default().fg(FG_DIM).bg(bg),
        ),
    ]);

    f.render_widget(Paragraph::new(line).style(Style::default().bg(bg)), area);
}

fn draw_input_area(f: &mut Frame, textarea: &TextArea<'_>, area: Rect) {
    f.render_widget(textarea, area);
}

// ═══════════════════════════════════════════════════════════════════════════════
// STATUS BAR — clean, keys bright / labels dim
// ═══════════════════════════════════════════════════════════════════════════════
fn draw_status_bar(f: &mut Frame, app: &App, area: Rect, mode: LayoutMode) {
    // Elapsed time since the current request started (shown during Thinking/Streaming).
    let elapsed_secs = app
        .request_start
        .map(|t| t.elapsed().as_secs())
        .unwrap_or(0);
    let elapsed_hint = if elapsed_secs >= 5 {
        format!("  {}s", elapsed_secs)
    } else {
        String::new()
    };

    let (status_text, status_color, spin) = match &app.state {
        AppState::Idle => ("Ready".to_string(), C_GREEN, "●"),
        AppState::Thinking => (
            format!("Thinking{elapsed_hint}"),
            C_PURPLE,
            spinner_frame(app.spinner_tick),
        ),
        AppState::Streaming { tokens_received } => {
            // During streaming, show recent tool calls as compact breadcrumb
            // so users can see what happened while the AI was working.
            let recent_tools: Vec<String> = app
                .messages
                .iter()
                .rev()
                .filter_map(|m| {
                    if let ChatMessage::ToolCall {
                        display,
                        ok: Some(ok),
                        ..
                    } = m
                    {
                        let icon = if *ok { "✓" } else { "✗" };
                        let d: String = display.chars().take(20).collect();
                        Some(format!("{icon} {d}"))
                    } else {
                        None
                    }
                })
                .take(3)
                .collect::<Vec<_>>()
                .into_iter()
                .rev()
                .collect();

            let tool_hint = if !recent_tools.is_empty() {
                format!("  [{}]", recent_tools.join(" → "))
            } else {
                String::new()
            };

            let tok = fmt_tokens(*tokens_received);
            let s = if tok.is_empty() {
                format!("Streaming{tool_hint}{elapsed_hint}")
            } else {
                format!("Streaming  {tok}{tool_hint}{elapsed_hint}")
            };
            (s, C_PURPLE, spinner_frame(app.spinner_tick))
        }
        AppState::CallingTool { display, .. } => {
            let d: String = display.chars().take(28).collect();
            (d, C_ORANGE, spinner_frame(app.spinner_tick))
        }
        AppState::WaitingApproval { .. } => ("Awaiting approval".into(), C_YELLOW, "⏸"),
        AppState::WaitingChoice { .. } => ("⌨ Choose an option".into(), C_YELLOW, "⏸"),
        AppState::SkillRunning {
            skill_name,
            current_step,
            total_steps,
            ..
        } => (
            format!(
                "Skill {}  {}/{total_steps}",
                skill_name.chars().take(12).collect::<String>(),
                current_step + 1
            ),
            C_PURPLE,
            spinner_frame(app.spinner_tick),
        ),
        AppState::Error(m) => {
            let s: String = m.chars().take(36).collect();
            (format!("Error: {s}"), C_RED, "✗")
        }
    };

    let max_s = (area.width as usize).saturating_sub(50).max(6).min(36);
    let sd: String = status_text.chars().take(max_s).collect();
    let ell = if status_text.chars().count() > max_s {
        "…"
    } else {
        ""
    };

    // Helper for key+label pairs
    let kb = |key: &str, lbl: &str| -> Vec<Span<'static>> {
        vec![
            Span::styled(
                key.to_string(),
                Style::default().fg(C_YELLOW).add_modifier(Modifier::BOLD),
            ),
            Span::styled(lbl.to_string(), Style::default().fg(FG_DIM)),
        ]
    };

    let mut spans = vec![
        Span::styled(format!(" {spin} "), Style::default().fg(status_color)),
        Span::styled(format!("{sd}{ell}"), Style::default().fg(status_color)),
        Span::styled("  ", Style::default()),
    ];

    // Queue badge — shown whenever messages are waiting, regardless of layout mode
    let q = app.queued_count();
    if q > 0 {
        spans.push(Span::styled(
            format!("⏎ {} queued  ", q),
            Style::default().fg(C_YELLOW).add_modifier(Modifier::BOLD),
        ));
    }

    match mode {
        LayoutMode::Wide => {
            spans.extend(kb("^C", ":quit "));
            spans.extend(kb("Enter", ":send "));
            spans.extend(kb("S+Enter", ":newline "));
            spans.extend(kb("PgUp/Dn", ":scroll "));
            spans.extend(kb("Home/End", ":top/end "));
            spans.extend(kb("/help", ":cmds "));
            spans.extend(kb("F12", ":log"));
        }
        LayoutMode::Normal => {
            spans.extend(kb("^C", ":quit "));
            spans.extend(kb("Enter", ":send "));
            spans.extend(kb("PgUp/Dn", ":scroll "));
            spans.extend(kb("/help", ":cmds "));
            spans.extend(kb("F12", ":log"));
        }
        LayoutMode::Compact => {
            spans.extend(kb("^C", " "));
            spans.extend(kb("Ret", ":send "));
            spans.extend(kb("Pg", ":scroll"));
        }
        LayoutMode::Tiny => {
            spans.push(Span::styled(
                format!("{spin} {sd}"),
                Style::default().fg(status_color),
            ));
        }
    }

    f.render_widget(
        Paragraph::new(Line::from(spans)).style(Style::default().bg(BG_SURFACE).fg(FG_TEXT)),
        area,
    );
}

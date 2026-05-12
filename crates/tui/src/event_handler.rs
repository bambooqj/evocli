//! Keyboard event handling

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use tui_textarea::TextArea;

use crate::app::{App, AppState, ChatMessage};

fn fmt_k(n: usize) -> String {
    if n >= 1_000_000 { format!("{:.1}M", n as f64 / 1_000_000.0) }
    else if n >= 1_000 { format!("{:.1}k", n as f64 / 1_000.0) }
    else { n.to_string() }
}

/// Actions that the main loop should take after handling a key event
pub enum EventAction {
    None,
    Submit(String),
    Queue(String),
    Quit,
    ChainQuery(String),
    Interrupt,
    ApprovalResponse(bool),
    /// User resolved a prompt.choice — carries the chosen option id or custom text
    ChoiceResponse(soul_bridge::ChoiceResult),
}

pub fn handle_key_event(
    app: &mut App,
    textarea: &mut TextArea<'static>,
    key: KeyEvent,
) -> EventAction {
    // Ctrl+C → 退出（全局快捷键，任何状态下都响应）
    if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('c') {
        app.should_quit = true;
        return EventAction::Quit;
    }

    // F12 → toggle debug log overlay (works in ALL states)
    // Only handle KeyPress to avoid double-toggle from Windows Press+Release events.
    if key.code == KeyCode::F(12) {
        use crossterm::event::KeyEventKind;
        if key.kind == KeyEventKind::Press || key.kind == KeyEventKind::Repeat {
            app.show_debug = !app.show_debug;
            if app.show_debug {
                app.refresh_debug_log(30);
            }
        }
        return EventAction::None;
    }

    // Esc while debug overlay is open → always close it.
    // Acts as a reliable fallback in case F12 double-fires.
    if app.show_debug && key.code == KeyCode::Esc {
        app.show_debug = false;
        return EventAction::None;
    }

    // During WaitingApproval, only accept y/n/Esc
    if matches!(app.state, AppState::WaitingApproval { .. }) {
        return match key.code {
            KeyCode::Char('y') | KeyCode::Char('Y') => EventAction::ApprovalResponse(true),
            KeyCode::Char('n') | KeyCode::Char('N') | KeyCode::Esc => EventAction::ApprovalResponse(false),
            _ => EventAction::None,
        };
    }

    // During WaitingChoice, handle navigation + selection + custom input
    if let AppState::WaitingChoice {
        ref options,
        ref mut selected_idx,
        allow_custom,
        ref mut custom_input,
        ref mut custom_mode,
        ..
    } = app.state {
        let opt_count = options.len();

        if *custom_mode {
            // Custom text input mode
            return match key.code {
                KeyCode::Esc => {
                    *custom_mode = false;
                    EventAction::None
                }
                KeyCode::Enter => {
                    let text = custom_input.trim().to_string();
                    EventAction::ChoiceResponse(soul_bridge::ChoiceResult::Custom(text))
                }
                KeyCode::Backspace => {
                    custom_input.pop();
                    EventAction::None
                }
                KeyCode::Char(c) => {
                    custom_input.push(c);
                    EventAction::None
                }
                _ => EventAction::None,
            };
        }

        // Option list navigation
        return match key.code {
            KeyCode::Esc => {
                EventAction::ChoiceResponse(soul_bridge::ChoiceResult::Cancelled)
            }
            KeyCode::Up => {
                if opt_count > 0 {
                    *selected_idx = selected_idx.checked_sub(1).unwrap_or(opt_count - 1);
                }
                EventAction::None
            }
            KeyCode::Down | KeyCode::Tab => {
                if opt_count > 0 {
                    *selected_idx = (*selected_idx + 1) % opt_count;
                }
                EventAction::None
            }
            KeyCode::Enter => {
                if opt_count > 0 {
                    let id = options[*selected_idx].id.clone();
                    EventAction::ChoiceResponse(soul_bridge::ChoiceResult::Selected(id))
                } else {
                    EventAction::None
                }
            }
            // 1-9 quick-select
            KeyCode::Char(c) if c.is_ascii_digit() && c != '0' => {
                let n = (c as usize) - ('0' as usize);
                if n <= opt_count {
                    let id = options[n - 1].id.clone();
                    EventAction::ChoiceResponse(soul_bridge::ChoiceResult::Selected(id))
                } else {
                    EventAction::None
                }
            }
            // 'i' / 'c' → custom input (only when allow_custom)
            KeyCode::Char('i') | KeyCode::Char('c') if allow_custom => {
                *custom_mode = true;
                EventAction::None
            }
            _ => EventAction::None,
        };
    }

    // During non-idle states, allow scrolling, quit, interrupt,
    // AND typing / queuing the next message.
    if !matches!(app.state, AppState::Idle | AppState::Error(_)) {
        match key.code {
            // ── Scrolling (unchanged) ────────────────────────────────────
            KeyCode::PageUp   => { app.scroll_up();      return EventAction::None; }
            KeyCode::PageDown => { app.scroll_down();    return EventAction::None; }
            KeyCode::Home if key.modifiers.contains(KeyModifiers::CONTROL)
                          => { app.scroll_to_top();    return EventAction::None; }
            KeyCode::End  if key.modifiers.contains(KeyModifiers::CONTROL)
                          => { app.scroll_to_bottom(); return EventAction::None; }
            KeyCode::Home => { app.scroll_to_top();    return EventAction::None; }
            KeyCode::End  => { app.scroll_to_bottom(); return EventAction::None; }
            KeyCode::Up   if key.modifiers.contains(KeyModifiers::ALT)
                          => { app.scroll_fast_up();   return EventAction::None; }
            KeyCode::Down if key.modifiers.contains(KeyModifiers::ALT)
                          => { app.scroll_fast_down(); return EventAction::None; }

            // ── Interrupt ────────────────────────────────────────────────
            KeyCode::Esc => return EventAction::Interrupt,

            // ── Ctrl+Y: copy last AI message (works even during streaming) ──
            KeyCode::Char('y') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                match app.copy_last_message_to_clipboard() {
                    Ok(n)  => app.notify(format!("✓ Copied {n} chars"), crate::app::NotifLevel::Info),
                    Err(e) => app.notify(format!("✗ {e}"), crate::app::NotifLevel::Warn),
                }
                return EventAction::None;
            }

            // ── Enter while busy → queue the message ─────────────────────
            KeyCode::Enter => {
                // Shift+Enter / Alt+Enter → insert newline (multi-line compose)
                if key.modifiers.contains(KeyModifiers::SHIFT)
                    || key.modifiers.contains(KeyModifiers::ALT)
                {
                    textarea.input(key);
                    return EventAction::None;
                }
                let text: String = textarea.lines().join("\n");
                let text = text.trim().to_string();
                if text.is_empty() { return EventAction::None; }
                *textarea = create_textarea();
                // Show the queued message immediately in chat so the user sees it
                app.messages.push(ChatMessage::User(text.clone()));
                app.scroll = usize::MAX;
                app.invalidate_cache();
                return EventAction::Queue(text);
            }

            // ── All other keys: feed into textarea (allow composing) ─────
            _ => {
                textarea.input(key);
                return EventAction::None;
            }
        }
    }

    // ── 命令联想：Tab / 上下箭头 / Esc 在建议列表激活时拦截 ─────────────
    if app.show_suggestions {
        match key.code {
            KeyCode::Tab | KeyCode::Down => {
                app.suggestion_next();
                return EventAction::None;
            }
            KeyCode::Up => {
                app.suggestion_prev();
                return EventAction::None;
            }
            KeyCode::Enter => {
                // 接受建议：用建议命令替换当前输入（保留光标行为）
                if let Some(text) = app.accept_suggestion() {
                    // Clear the current line without resetting textarea state.
                    // Ctrl+U kills from cursor to line start; Ctrl+K kills to line end.
                    // Together they clear the current line's content.
                    // Then insert the suggestion text directly.
                    textarea.move_cursor(tui_textarea::CursorMove::Head);
                    textarea.delete_line_by_end();
                    for ch in text.chars() {
                        textarea.input(crossterm::event::KeyEvent::new(
                            KeyCode::Char(ch),
                            KeyModifiers::NONE,
                        ));
                    }
                    return EventAction::None;
                }
                // 建议列表为空则正常提交
            }
            KeyCode::Esc => {
                app.dismiss_suggestions();
                return EventAction::None;
            }
            _ => {
                // 继续输入：让 textarea 处理，然后更新建议
            }
        }
    }

    match key.code {
        KeyCode::Enter => {
            // Shift+Enter 或 Alt+Enter → 在输入框中插入换行（多行输入）
            if key.modifiers.contains(KeyModifiers::SHIFT) || key.modifiers.contains(KeyModifiers::ALT) {
                textarea.input(crossterm::event::KeyEvent::new(
                    KeyCode::Enter,
                    KeyModifiers::NONE,
                ));
                return EventAction::None;
            }

            let text: String = textarea.lines().join("\n");
            let text = text.trim().to_string();
            if text.is_empty() {
                return EventAction::None;
            }
            app.dismiss_suggestions();
            *textarea = create_textarea();
            app.messages.push(ChatMessage::User(text.clone()));

            // P3-1: /chain <symbol> — call chain lookup
            if text.starts_with("/chain ") {
                let symbol = text[7..].trim().to_string();
                if !symbol.is_empty() {
                    app.state = AppState::Thinking;
                    return EventAction::ChainQuery(symbol);
                }
            }

            // /help — show available commands
            if text == "/help" || text == "/?" {
                app.messages.pop();
                let cmds = crate::app::SLASH_COMMANDS.iter()
                    .map(|(cmd, desc)| format!("  {:<24} {}", cmd, desc))
                    .collect::<Vec<_>>()
                    .join("\n");
                let shortcuts = "\
Keyboard shortcuts:\n\
  PageUp / PageDown        Scroll chat (always works)\n\
  Home / End               Scroll to top / bottom\n\
  Alt+Up / Alt+Down        Scroll 5 rows fast\n\
  Ctrl+Y                   Copy last AI message to clipboard\n\
  Ctrl+C                   Quit\n\
  Ctrl+L                   Clear screen\n\
  F12                      Toggle debug log\n\
  Esc                      Interrupt AI / dismiss input\n\
\n\
Text selection & copy:\n\
  Default mode (enable_mouse=false in config):\n\
    Click + drag            Native terminal text selection\n\
    Ctrl+C / right-click    Copy selected text (terminal native)\n\
  Mouse mode (enable_mouse=true in config):\n\
    Mouse wheel             Scrolls message list\n\
    Ctrl+Y                  Copies last AI message\n\
    Shift+drag              Native selection (Windows Terminal only)\n\
\n\
To switch modes — add to ~/.evocli/config.toml:\n\
  [tui]\n\
  enable_mouse = true    # mouse wheel scroll; native selection disabled\n\
  enable_mouse = false   # native selection/copy; keyboard scroll (default)\n\
\n\
Slash commands:";
                app.messages.push(ChatMessage::System(
                    format!("{shortcuts}\n{cmds}")
                ));
                app.invalidate_cache();  // message list changed
                return EventAction::None;
            }

            // /clear — clear chat history (keep welcome message)
            if text == "/clear" {
                app.messages.pop(); // remove "/clear" user message
                app.messages.retain(|m| matches!(m, ChatMessage::System(_)));
                app.messages.push(ChatMessage::System("Chat cleared.".into()));
                app.invalidate_cache();  // message list changed
                return EventAction::None;
            }

            // /cost — show current session cost
            if text == "/cost" {
                app.messages.pop();
                let cost = if app.session_cost_usd < 0.001 {
                    "$0.000".to_string()
                } else {
                    format!("${:.4}", app.session_cost_usd)
                };
                app.messages.push(ChatMessage::System(format!(
                    "Session cost: {cost}  |  \
                     Context now: ↑{}  Last output: ↓{}  |  \
                     Session total: in={} out={}",
                    fmt_k(app.current_ctx_tokens),
                    fmt_k(app.last_out_tokens),
                    fmt_k(app.tokens_input),
                    fmt_k(app.tokens_output),
                )));
                app.invalidate_cache();  // message list changed
                return EventAction::None;
            }

            // /log [N] — dump last N lines from log file inline
            if text == "/log" || text.starts_with("/log ") {
                app.messages.pop();
                let n: usize = text.strip_prefix("/log ")
                    .and_then(|s| s.trim().parse().ok())
                    .unwrap_or(30)
                    .clamp(1, 200);
                app.refresh_debug_log(n);
                if app.debug_log_lines.is_empty() {
                    app.messages.push(ChatMessage::System(
                        format!("Log file not found: {}", app.debug_log_path)
                    ));
                } else {
                    let content = app.debug_log_lines.join("\n");
                    app.messages.push(ChatMessage::System(
                        format!("📋 Log (last {} lines from {}):\n{}", n, app.debug_log_path, content)
                    ));
                }
                app.invalidate_cache();
                return EventAction::None;
            }

            app.state = AppState::Thinking;
            app.request_start = Some(std::time::Instant::now());
            EventAction::Submit(text)
        }

        // 滚动快捷键（Idle 状态）
        KeyCode::PageUp   => { app.scroll_up();     EventAction::None }
        KeyCode::PageDown => { app.scroll_down();   EventAction::None }

        // Ctrl+Home / Home → 滚动到顶部（查看历史消息）
        KeyCode::Home if key.modifiers.contains(KeyModifiers::CONTROL)
                      => { app.scroll_to_top();    EventAction::None }
        // Ctrl+End / End → 滚动到底部（最新消息）
        KeyCode::End  if key.modifiers.contains(KeyModifiers::CONTROL)
                      => { app.scroll_to_bottom(); EventAction::None }

        // Alt+Up / Alt+Down → 快速滚动（5行）
        KeyCode::Up   if key.modifiers.contains(KeyModifiers::ALT)
                      => { app.scroll_fast_up();   EventAction::None }
        KeyCode::Down if key.modifiers.contains(KeyModifiers::ALT)
                      => { app.scroll_fast_down(); EventAction::None }

        // Ctrl+L → 清屏（等同于 /clear）
        KeyCode::Char('l') if key.modifiers.contains(KeyModifiers::CONTROL) => {
            app.messages.retain(|m| matches!(m, ChatMessage::System(_)));
            app.messages.push(ChatMessage::System("Screen cleared. (Ctrl+L)".into()));
            app.invalidate_cache();  // message list changed
            EventAction::None
        }

        // Ctrl+Y → 复制最后一条 AI 消息到系统剪贴板
        // （Ctrl+C 已绑定退出，所以用 Y = Yank，类似 Vim 惯例）
        // 提示：在大多数终端中，按住 Shift 再拖鼠标可绕过鼠标捕获实现原生文本选择
        KeyCode::Char('y') if key.modifiers.contains(KeyModifiers::CONTROL) => {
            match app.copy_last_message_to_clipboard() {
                Ok(n) => {
                    app.notify(
                        format!("✓ Copied {n} chars to clipboard"),
                        crate::app::NotifLevel::Info,
                    );
                }
                Err(e) => {
                    app.notify(
                        format!("✗ {e}"),
                        crate::app::NotifLevel::Warn,
                    );
                }
            }
            EventAction::None
        }

        KeyCode::Esc => {
            app.dismiss_suggestions();
            *textarea = create_textarea();
            EventAction::None
        }

        _ => {
            textarea.input(key);
            // 实时更新命令联想（根据当前输入内容）
            let current = textarea.lines().join("\n");
            app.update_suggestions(&current);
            EventAction::None
        }
    }
}

/// 根据 AppState + 队列长度更新输入框的边框样式和标题
/// 关键：只在状态转换时调用（非每帧），避免打断用户输入
pub fn apply_input_style(textarea: &mut TextArea<'static>, state: &AppState, queued: usize) {
    use ratatui::{style::{Style, Color, Modifier}, text::Span,
                  widgets::{Block, Borders, BorderType}};

    let queue_hint = if queued == 0 {
        String::new()
    } else {
        format!(" · {} queued", queued)
    };

    let (title, border_color): (String, Color) = match state {
        AppState::Idle | AppState::Error(_) => (
            " ❯  Message  (Enter:send · S+Enter:newline · Tab:/cmd · Esc:clear) ".into(),
            Color::Rgb(138, 173, 244),
        ),
        AppState::Thinking => (
            format!(" ↻  Connecting…  (Enter:queue{queue_hint} · Esc:cancel) "),
            Color::Rgb(110, 115, 141),
        ),
        AppState::Streaming { .. } => (
            format!(" ↻  AI responding…  (Enter:queue{queue_hint} · Esc:interrupt) "),
            Color::Rgb(198, 160, 246),
        ),
        AppState::CallingTool { .. } => (
            format!(" ↻  Using tool…  (Enter:queue{queue_hint} · Esc:interrupt) "),
            Color::Rgb(245, 169, 127),
        ),
        AppState::WaitingApproval { .. } => (
            " 🔒  Press  y  allow  /  n  deny ".into(),
            Color::Rgb(238, 212, 159),
        ),
        AppState::WaitingChoice { .. } => (
            " ⌨  Choose: ↑↓/1-9 navigate  Enter confirm  Esc cancel ".into(),
            Color::Rgb(138, 173, 244),
        ),
        AppState::SkillRunning { .. } => (
            format!(" ↻  Skill running…  (Enter:queue{queue_hint} · Esc:interrupt) "),
            Color::Rgb(198, 160, 246),
        ),
    };

    textarea.set_block(
        Block::default()
            .borders(Borders::ALL)
            .border_type(BorderType::Rounded)
            .title(Span::styled(title, Style::default().fg(border_color)))
            .border_style(Style::default().fg(border_color))
            .style(Style::default().bg(Color::Rgb(30, 32, 48))),
    );
    textarea.set_cursor_line_style(Style::default());
    textarea.set_cursor_style(Style::default()
        .fg(Color::Rgb(198, 160, 246))
        .add_modifier(Modifier::REVERSED));
}

/// 创建带现代样式的输入 textarea (Gemini CLI 风格 — 实心背景色)
pub fn create_textarea() -> TextArea<'static> {
    let mut textarea = TextArea::default();
    apply_input_style(&mut textarea, &AppState::Idle, 0);
    textarea
}

pub fn create_textarea_for_width(width: u16) -> TextArea<'static> {
    let mut textarea = TextArea::default();
    apply_input_style(&mut textarea, &AppState::Idle, 0);
    // 窄屏时调整标题
    if width < 90 {
        use ratatui::{style::{Style, Color}, text::Span,
                      widgets::{Block, Borders, BorderType}};
        let title = match width {
            w if w >= 60 => " ❯  Message  (Enter:send · S+Enter:wrap · Tab:cmd) ",
            w if w >= 40 => " ❯  Input  (Enter · Tab) ",
            _            => " ❯  Input ",
        };
        textarea.set_block(
            Block::default()
                .borders(Borders::ALL)
                .border_type(BorderType::Rounded)
                .title(Span::styled(title, Style::default().fg(Color::Rgb(138, 173, 244))))
                .border_style(Style::default().fg(Color::Rgb(138, 173, 244)))
                .style(Style::default().bg(Color::Rgb(30, 32, 48))),
        );
    }
    textarea
}

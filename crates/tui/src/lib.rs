//! EvoCLI TUI — Terminal User Interface
//!
//! Full-screen chat TUI built with ratatui + crossterm.

pub mod app;
pub mod event_handler;
pub mod ui;

use std::io;
use std::sync::Arc;
use std::time::Duration;

use anyhow::Result;
use crossterm::{
    event::{DisableBracketedPaste, DisableMouseCapture, EnableBracketedPaste, EnableMouseCapture},
    execute,
    terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen},
};
use ratatui::backend::CrosstermBackend;
use ratatui::Terminal;
use tokio::sync::mpsc;

use app::{App, AppState, ChatMessage, StepStatus};
use event_handler::EventAction;
use soul_bridge::{SoulBridge, StreamChunk};

/// Cleanup guard — restores terminal on drop (even on panic / early return).
/// Tracks whether mouse capture was enabled so cleanup matches setup.
struct CleanupGuard {
    mouse_enabled: bool,
}

impl Drop for CleanupGuard {
    fn drop(&mut self) {
        let _ = disable_raw_mode();
        if self.mouse_enabled {
            let _ = execute!(
                io::stdout(),
                DisableMouseCapture,
                DisableBracketedPaste,
                LeaveAlternateScreen,
            );
        } else {
            let _ = execute!(io::stdout(), DisableBracketedPaste, LeaveAlternateScreen,);
        }
    }
}

/// Run the full-screen TUI event loop.
///
/// `bridge` is the IPC connection to the Python Soul.
/// `model_name` is displayed in the title bar.
/// `resume_session` — if Some(session_id), inject a resume message on startup.
/// `first_chunk_timeout_s` — how long to wait for the first stream chunk before showing error.
/// `enable_mouse` — if true, capture mouse events (wheel scroll); if false, native terminal
///   text selection/copy works but mouse wheel is inactive (use PageUp/Down keyboard shortcuts).
pub async fn run(
    bridge: Arc<SoulBridge>,
    model_name: &str,
    resume_session: Option<&str>,
    max_context_tokens: usize,
    first_chunk_timeout_s: u64,
    enable_mouse: bool,
) -> Result<()> {
    // ── Setup terminal ──────────────────────────────────
    enable_raw_mode()?;
    let mut stdout = io::stdout();
    if enable_mouse {
        execute!(
            stdout,
            EnterAlternateScreen,
            EnableMouseCapture, // mouse scroll; disables native text selection
            EnableBracketedPaste,
        )?;
    } else {
        execute!(
            stdout,
            EnterAlternateScreen,
            // No EnableMouseCapture → terminal handles text selection natively.
            // Users can click+drag to select text and use Ctrl+C/right-click to copy.
            // Scrolling uses PageUp/Down/Home/End keyboard shortcuts.
            EnableBracketedPaste,
        )?;
    }
    let backend = CrosstermBackend::new(stdout);
    let mut terminal = Terminal::new(backend)?;
    let _guard = CleanupGuard {
        mouse_enabled: enable_mouse,
    };

    // ── App state + textarea ────────────────────────────
    let mut app = App::new(model_name.to_string(), max_context_tokens);
    // 根据初始终端宽度选择合适的 textarea 标题
    let initial_width = terminal.size().map(|s| s.width).unwrap_or(80);
    let mut textarea = event_handler::create_textarea_for_width(initial_width);

    // P2-1: Session resume — inject a context-restore message
    // AND store the resume ID so all submit paths use it instead of cwd_*
    if let Some(sid) = resume_session {
        app.messages.push(app::ChatMessage::System(format!(
            "↩ Resuming session {} — previous context loaded",
            &sid[..sid.len().min(16)]
        )));
        app.invalidate_cache();
        // Store override: all agent.stream calls in this TUI session will use this ID
        app.override_session_id = Some(sid.to_string());
    }

    // ── Keyboard event channel ──────────────────────────
    let (event_tx, mut event_rx) = mpsc::channel::<crossterm::event::Event>(100);
    // Shutdown signal for the keyboard polling thread: sent on Quit.
    let (kb_shutdown_tx, kb_shutdown_rx) = tokio::sync::oneshot::channel::<()>();
    tokio::task::spawn_blocking(move || {
        // Use a Cell to allow checking the oneshot without async.
        let mut kb_shutdown_rx = kb_shutdown_rx;
        loop {
            // Non-blocking shutdown check via try_recv (works in blocking context).
            if kb_shutdown_rx.try_recv().is_ok() {
                break;
            }
            if crossterm::event::poll(Duration::from_millis(50)).unwrap_or(false) {
                if let Ok(ev) = crossterm::event::read() {
                    if event_tx.blocking_send(ev).is_err() {
                        break;
                    }
                }
            }
        }
    });

    // ── Stream chunk channel + chain result channel + interrupt ─
    let (chunk_tx, mut chunk_rx) = mpsc::unbounded_channel::<StreamChunk>();
    let (chain_tx, mut chain_rx) = mpsc::unbounded_channel::<ChatMessage>();
    // FIX-B: interrupt token — sent when user presses Escape
    let (interrupt_tx, _interrupt_rx) = tokio::sync::watch::channel(false);

    // ── Cursor blink timer ──────────────────────────────
    let mut blink = tokio::time::interval(Duration::from_millis(500));

    // ── 输入框状态追踪 — 只在状态改变时才更新 set_block ─────────────
    // 关键：不能每帧都调用 textarea.set_block()，否则每次按键都会重置
    // tui-textarea 的内部状态（光标位置、视口偏移），产生"打断输入"的感觉
    #[derive(PartialEq, Clone)]
    enum InputMode {
        Active,
        Waiting,
        Responding,
        UsingTool,
        NeedApproval,
        SkillRun,
    }

    fn get_input_mode(state: &AppState) -> InputMode {
        match state {
            AppState::Idle | AppState::Error(_) => InputMode::Active,
            AppState::Thinking => InputMode::Waiting,
            AppState::Streaming { .. } => InputMode::Responding,
            AppState::CallingTool { .. } => InputMode::UsingTool,
            AppState::WaitingApproval { .. } => InputMode::NeedApproval,
            AppState::WaitingChoice { .. } => InputMode::NeedApproval,
            AppState::SkillRunning { .. } => InputMode::SkillRun,
        }
    }

    let mut last_input_mode = get_input_mode(&app.state);
    // 初始化输入框样式
    event_handler::apply_input_style(&mut textarea, &app.state, app.queued_count(), &app.thinking_label);

    // Pre-compute default session_id ONCE before the main loop.
    // Avoids calling std::env::current_dir() on every Submit/Queue event
    // (which races with directory changes and duplicates logic).
    // FNV-1a hash of CWD → stable per-project session bucket across TUI restarts.
    let default_session_id: String = app.override_session_id.clone().unwrap_or_else(|| {
        let cwd = std::env::current_dir()
            .unwrap_or_default()
            .to_string_lossy()
            .to_string();
        let hash: u64 = cwd
            .bytes()
            .fold(14_695_981_039_346_656_037_u64, |acc, b| {
                acc.wrapping_mul(1_099_511_628_211) ^ b as u64
            });
        format!("cwd_{:012x}", hash & 0xFFFF_FFFF_FFFF)
    });

    // ── Main event loop ─────────────────────────────────
    loop {
        // 仅当状态类别改变时才更新输入框样式
        // 频率：状态转换时（每次对话约4-5次），而非每帧（60fps）
        let current_mode = get_input_mode(&app.state);
        if current_mode != last_input_mode {
            event_handler::apply_input_style(&mut textarea, &app.state, app.queued_count(), &app.thinking_label);
            last_input_mode = current_mode;
        }

        // Draw — event-driven, with one exception: during streaming we only draw
        // when the blink timer fires (every 500ms) OR when a chunk batch completes.
        // This prevents burning CPU at token-emission rate (~100+ draws/sec).
        // All other events (key press, resize, soul events) draw unconditionally.
        let is_streaming = matches!(app.state, AppState::Streaming { .. });
        if !is_streaming || app.cache_dirty {
            terminal.draw(|f| ui::draw(f, &mut app, &textarea))?;
        }

        // Wait for next event
        tokio::select! {
            Some(event) = event_rx.recv() => {
                match event {
                    crossterm::event::Event::Key(key) => {
                        let action = event_handler::handle_key_event(&mut app, &mut textarea, key);
                        match action {
                            EventAction::Quit => {
                                // Signal the keyboard polling thread to stop cleanly.
                                let _ = kb_shutdown_tx.send(());
                                break;
                            }
                            // FIX-B: Escape = soft interrupt, cancel current generation
                            EventAction::Interrupt => {
                                let _ = interrupt_tx.send(true);
                                app.messages.push(ChatMessage::System("⏸ Interrupted".into()));
                                app.invalidate_cache();
                                app.state = AppState::Idle;
                                app.request_start = None;  // clear timer
                                let _ = interrupt_tx.send(false); // reset for next request
                            }
                            // TUI Approval: user responded y/n to approval modal
                            EventAction::ApprovalResponse(approved) => {
                                bridge.resolve_approval(approved).await;
                                let msg = if approved { "✓ Action approved" } else { "✗ Action rejected" };
                                app.messages.push(ChatMessage::System(msg.into()));
                                app.invalidate_cache();
                                app.state = AppState::Idle;
                                terminal.draw(|f| ui::draw(f, &mut app, &textarea))?;
                            }
                            // TUI Choice: user picked an option or entered custom text
                            EventAction::ChoiceResponse(result) => {
                                use soul_bridge::ChoiceResult;
                                let summary = match &result {
                                    ChoiceResult::Selected(id) => format!("✓ Selected: {}", id),
                                    ChoiceResult::Custom(txt)  => format!("✓ Custom: {}", txt),
                                    ChoiceResult::Cancelled     => "✗ Cancelled".into(),
                                };
                                bridge.resolve_choice(result).await;
                                app.messages.push(ChatMessage::System(summary));
                                app.invalidate_cache();
                                app.state = AppState::Idle;
                                terminal.draw(|f| ui::draw(f, &mut app, &textarea))?;
                            }
                            EventAction::Submit(text) => {
                                let _ = interrupt_tx.send(false); // reset interrupt flag
                                // CRITICAL: Draw immediately to show "Thinking..." status.
                                // Without this, the TUI freezes visually for 15-20s while
                                // bridge.call_stream() awaits the Soul (fastembed + LLM setup).
                                // app.state was already set to Thinking by handle_key_event.
                                app.thinking_label.clear(); // reset any stale label from prior turn
                                terminal.draw(|f| ui::draw(f, &mut app, &textarea))?;
                                // Use pre-computed session_id (computed once before main loop,
                                // not on every keypress — avoids live current_dir() calls).
                                let stream = bridge.call_stream(
                                    "agent.stream",
                                    serde_json::json!({ "prompt": text, "session_id": default_session_id }),
                                ).await?;
                                app.start_streaming();
                                let tx = chunk_tx.clone();
                                let mut irx = interrupt_tx.subscribe();
                                tokio::spawn(async move {
                                    let mut stream = stream;
                                    // First-chunk timeout: if the Python Soul doesn't send ANY
                                    // chunk within first_chunk_timeout_s, declare the stream dead.
                                    // Context building (RepoMap, memory search) can take 30-120s
                                    // on large projects — configurable via [agent] first_chunk_timeout_s.
                                    let first_chunk_deadline =
                                        tokio::time::sleep(std::time::Duration::from_secs(first_chunk_timeout_s));
                                    tokio::pin!(first_chunk_deadline);
                                    let mut got_first_chunk = false;

                                    loop {
                                        tokio::select! {
                                            Some(chunk) = stream.recv() => {
                                                got_first_chunk = true;
                                                let done = chunk.done;
                                                let _ = tx.send(chunk);
                                                if done { break; }
                                            }
                                            Ok(()) = irx.changed() => {
                                                if *irx.borrow() { break; } // interrupted
                                            }
                                            // Only fire when no chunks have been received yet.
                                            _ = &mut first_chunk_deadline, if !got_first_chunk => {
                                                let _ = tx.send(soul_bridge::StreamChunk {
                                                    id:   String::new(),
                                                    text: concat!(
                                                        "\n\n⏱ **No response from AI (60s timeout)**\n",
                                                        "Possible causes:\n",
                                                        "  • API key missing — run `evocli init` to configure\n",
                                                        "  • Network issue — check your internet connection\n",
                                                        "  • API rate limit — wait a moment and try again\n",
                                                        "\nPress **Esc** to cancel and try again."
                                                    ).to_string(),
                                                    done: true,
                                                });
                                                break;
                                            }
                                            else => break,
                                        }
                                    }
                                });
                            }
                            // P3-1: /chain <symbol> — 异步查询调用链，不阻塞 TUI
                            EventAction::ChainQuery(symbol) => {
                                app.messages.push(ChatMessage::System(
                                    format!("Looking up call chain for '{}'...", symbol)
                                ));
                                app.invalidate_cache();
                                // 使用 Arc clone 将 bridge 传入 tokio::spawn，不阻塞主循环
                                let btx = chain_tx.clone();
                                let bridge_clone = Arc::clone(&bridge);
                                let sym = symbol.clone();
                                tokio::spawn(async move {
                                    let chain_msg = fetch_call_chain(&bridge_clone, &sym).await;
                                    let _ = btx.send(chain_msg);
                                });
                            }
                            EventAction::None => {}
                            // User typed while AI was busy → push to queue.
                            // The User message was already added to app.messages
                            // by handle_key_event so it shows immediately in chat.
                            EventAction::Queue(text) => {
                                app.message_queue.push_back(text);
                                // Refresh input style to reflect new queue count.
                                event_handler::apply_input_style(
                                    &mut textarea, &app.state, app.queued_count(), &app.thinking_label
                                );
                            }
                        }
                    }
                    // 终端尺寸改变事件：重新创建自适应 textarea（O(lines) 不是 O(chars)）
                    crossterm::event::Event::Resize(new_width, _new_height) => {
                        // Preserve text by re-using existing lines directly.
                        // tui-textarea supports From<Vec<String>> — O(lines) not O(chars).
                        let current_lines: Vec<String> = textarea.lines().to_vec();
                        textarea = event_handler::create_textarea_for_width(new_width);
                        if !current_lines.is_empty() && current_lines != [""] {
                            // Replay using From<Vec<String>> trait — much faster than char replay
                            use tui_textarea::TextArea;
                            let mut new_ta = TextArea::from(current_lines);
                            // Move cursor to end
                            new_ta.move_cursor(tui_textarea::CursorMove::End);
                            // Reapply style
                            event_handler::apply_input_style(&mut new_ta, &app.state, app.queued_count(), &app.thinking_label);
                            textarea = new_ta;
                        }
                        // Invalidate cache: width changed, all line wrapping recalculated
                        app.cache_dirty = true;
                        terminal.draw(|f| ui::draw(f, &mut app, &textarea))?;
                    }

                    // Mouse scroll wheel — only active when enable_mouse=true.
                    // When enable_mouse=false, mouse events are never sent by the terminal,
                    // so this branch is unreachable. Guarded here for clarity.
                    crossterm::event::Event::Mouse(mouse) if enable_mouse => {
                        use crossterm::event::MouseEventKind;
                        let scrolled = match mouse.kind {
                            MouseEventKind::ScrollUp   => { app.scroll_up_n(5);   true }
                            MouseEventKind::ScrollDown => { app.scroll_down_n(5); true }
                            _ => false,
                        };
                        if scrolled {
                            app.cache_dirty = true;
                            terminal.draw(|f| ui::draw(f, &mut app, &textarea))?;
                        }
                    }

                    // Bracketed paste — the entire pasted block arrives as one event.
                    // Without this, every pasted character triggered a separate Key event
                    // and a full terminal.draw() call, causing visible lag on large pastes.
                    crossterm::event::Event::Paste(pasted_text) => {
                        // Insert pasted text into textarea as a single operation
                        for ch in pasted_text.chars() {
                            if ch == '\n' {
                                textarea.input(crossterm::event::KeyEvent::new(
                                    crossterm::event::KeyCode::Enter,
                                    crossterm::event::KeyModifiers::NONE,
                                ));
                            } else {
                                textarea.input(crossterm::event::KeyEvent::new(
                                    crossterm::event::KeyCode::Char(ch),
                                    crossterm::event::KeyModifiers::NONE,
                                ));
                            }
                        }
                        // Single draw after the entire paste — not one per character
                        terminal.draw(|f| ui::draw(f, &mut app, &textarea))?;
                    }

                    _ => {} // 其他事件忽略
                }
            }
            // AI stream chunks — batch process to reduce redundant redraws.
            // During fast streaming (100+ tokens/sec), many chunks may arrive
            // between event loop iterations. Processing them all in one pass
            // means one draw per batch instead of one draw per token.
            Some(chunk) = chunk_rx.recv() => {
                if chunk.done {
                    if !chunk.text.is_empty() {
                        app.append_token(&chunk.text);
                    }
                    let tokens = match app.state {
                        AppState::Streaming { tokens_received } => tokens_received,
                        _ => 0,
                    };
                    app.finish_streaming(tokens);
                    event_handler::apply_input_style(&mut textarea, &app.state, app.queued_count(), &app.thinking_label);
                    terminal.draw(|f| ui::draw(f, &mut app, &textarea))?;

                    // ── Auto-drain message queue ──────────────────────────
                    // If the user typed messages while the AI was responding,
                    // automatically submit the next one now.
                    if let Some(next_text) = app.message_queue.pop_front() {
                        let _ = interrupt_tx.send(false);
                        // Note: the User message was already pushed to app.messages
                        // when the user pressed Enter (in handle_key_event), so we
                        // just need to set state + start the stream.
                        app.state = AppState::Thinking;
                        event_handler::apply_input_style(
                            &mut textarea, &app.state, app.queued_count(), &app.thinking_label
                        );
                        terminal.draw(|f| ui::draw(f, &mut app, &textarea))?;
                        // Derive the same project-scoped session_id as the primary submit path
                        // Reuse the pre-computed session_id — same project bucket.
                        let stream = bridge.call_stream(
                            "agent.stream",
                            serde_json::json!({ "prompt": next_text, "session_id": default_session_id }),
                        ).await?;
                        app.start_streaming();
                        let tx = chunk_tx.clone();
                        let mut irx = interrupt_tx.subscribe();
                        tokio::spawn(async move {
                            let mut stream = stream;
                            let first_chunk_deadline =
                                tokio::time::sleep(std::time::Duration::from_secs(first_chunk_timeout_s));
                            tokio::pin!(first_chunk_deadline);
                            let mut got_first_chunk = false;
                            loop {
                                tokio::select! {
                                    Some(chunk) = stream.recv() => {
                                        got_first_chunk = true;
                                        let done = chunk.done;
                                        let _ = tx.send(chunk);
                                        if done { break; }
                                    }
                                    Ok(()) = irx.changed() => {
                                        if *irx.borrow() { break; }
                                    }
                                    _ = &mut first_chunk_deadline, if !got_first_chunk => {
                                        let _ = tx.send(soul_bridge::StreamChunk {
                                            id:   String::new(),
                                            text: "\n\n⏱ No response from AI (60s timeout). Press Esc to cancel.".to_string(),
                                            done: true,
                                        });
                                        break;
                                    }
                                    else => break,
                                }
                            }
                        });
                    }
                } else {
                    // Non-final chunk: append text and drain any already-queued chunks
                    // in one pass. This batches multiple tokens into a single draw.
                    app.append_token(&chunk.text);
                    let mut did_finish = false;
                    while let Ok(extra) = chunk_rx.try_recv() {
                        if extra.done {
                            if !extra.text.is_empty() { app.append_token(&extra.text); }
                            let tokens = match app.state {
                                AppState::Streaming { tokens_received } => tokens_received,
                                _ => 0,
                            };
                            app.finish_streaming(tokens);
                            event_handler::apply_input_style(&mut textarea, &app.state, app.queued_count(), &app.thinking_label);
                            did_finish = true;
                            break;
                        }
                        app.append_token(&extra.text);
                    }
                    if did_finish {
                        terminal.draw(|f| ui::draw(f, &mut app, &textarea))?;
                    }
                    // Non-final: draw is handled at the TOP of the event loop.
                }
            }
            // P3-1: Call chain results (via tokio::spawn, non-blocking)
            Some(msg) = chain_rx.recv() => {
                // Replace the "Looking up..." placeholder with actual result
                if let Some(last) = app.messages.last_mut() {
                    if matches!(last, ChatMessage::System(s) if s.contains("Looking up call chain")) {
                        *last = msg;
                        app.invalidate_cache();  // message content changed (was missing!)
                    } else {
                        app.messages.push(msg);
                                app.invalidate_cache();
                    }
                } else {
                    app.messages.push(msg);
                                app.invalidate_cache();
                }
                app.state = AppState::Idle;
            }
            // P2-5: 处理来自 Python Soul 的 event.emit 通知
            // Drain up to 20 events per select! iteration.  Draining ALL events
            // without a cap could starve the keyboard branch if the Soul floods
            // events (e.g. rapid skill_step bursts), causing input lag.
            _ = async {
                let mut drained = 0u8;
                while drained < 20 {
                    if let Some(ev) = bridge.next_event().await {
                        handle_soul_event(&mut app, ev);
                        drained += 1;
                    } else {
                        break;
                    }
                }
                tokio::task::yield_now().await;
            } => {}
            _ = blink.tick() => {
                app.cursor_visible = !app.cursor_visible;
                app.spinner_tick = app.spinner_tick.wrapping_add(1);
                app.tick_notification();  // auto-expire transient notifications
                // Mark dirty so streaming draw at loop top fires for spinner/cursor update
                app.cache_dirty = true;
            }
        }

        // TUI Approval: bridge.get_pending_approval() 在 select! 外检查
        if !matches!(app.state, AppState::WaitingApproval { .. }) {
            if let Some(msg) = bridge.get_pending_approval().await {
                app.state = AppState::WaitingApproval { message: msg };
            }
        }

        // TUI Choice: poll for pending prompt.choice requests
        if !matches!(app.state, AppState::WaitingChoice { .. }) {
            if let Some(req) = bridge.get_pending_choice().await {
                app.state = AppState::WaitingChoice {
                    title: req.title,
                    options: req
                        .options
                        .into_iter()
                        .map(|o| app::ChoiceItem {
                            id: o.id,
                            label: o.label,
                        })
                        .collect(),
                    selected_idx: 0,
                    allow_custom: req.allow_custom,
                    custom_input: String::new(),
                    custom_mode: false,
                };
            }
        }

        if app.should_quit {
            break;
        }
    }

    Ok(())
}

/// P3-1: 查询调用链，返回 CallChain 消息（或错误 System 消息）
async fn fetch_call_chain(bridge: &SoulBridge, symbol: &str) -> ChatMessage {
    // Step 1: symbol.lookup
    let lookup = bridge
        .call("symbol.lookup", serde_json::json!({ "name": symbol }))
        .await;
    let (file, line, symbol_id) = match lookup {
        Ok(v) => {
            // symbol.lookup returns {"found": bool, "symbols": [...], "did_you_mean": []}.
            // Fall back gracefully for legacy array responses.
            let first = v
                .get("symbols")
                .and_then(|s| s.as_array())
                .and_then(|a| a.first())
                .unwrap_or(&v);
            let sym_id = first["id"]
                .as_str()
                .or_else(|| v["id"].as_str())
                .unwrap_or(symbol)
                .to_string();
            let f = first["file"]
                .as_str()
                .or_else(|| v["file"].as_str())
                .unwrap_or("unknown")
                .to_string();
            let l = first["line"]
                .as_u64()
                .or_else(|| v["line"].as_u64())
                .unwrap_or(0) as u32;
            (f, l, sym_id)
        }
        Err(_) => (String::from("(not indexed)"), 0, symbol.to_string()),
    };

    // Step 2: incoming + outgoing calls (parallel)
    let (in_result, out_result) = tokio::join!(
        bridge.call(
            "code_intel.incoming_calls",
            serde_json::json!({ "symbol_id": symbol_id })
        ),
        bridge.call(
            "code_intel.outgoing_calls",
            serde_json::json!({ "symbol_id": symbol_id })
        ),
    );

    let incoming = parse_call_list(in_result.unwrap_or(serde_json::Value::Array(vec![])));
    let outgoing = parse_call_list(out_result.unwrap_or(serde_json::Value::Array(vec![])));

    ChatMessage::CallChain {
        symbol: symbol.to_string(),
        file,
        line,
        incoming,
        outgoing,
    }
}

/// 从 code_intel 返回的调用列表中提取可读字符串
fn parse_call_list(val: serde_json::Value) -> Vec<String> {
    let arr = match val.as_array() {
        Some(a) => a.clone(),
        None => return vec![],
    };
    arr.iter()
        .filter_map(|item| {
            let name = item["name"].as_str().unwrap_or("");
            let file = item["file"]
                .as_str()
                .map(|f| {
                    // 只显示文件名，不显示完整路径
                    std::path::Path::new(f)
                        .file_name()
                        .and_then(|n| n.to_str())
                        .unwrap_or(f)
                        .to_string()
                })
                .unwrap_or_default();
            let line = item["line"].as_u64().unwrap_or(0);
            if name.is_empty() {
                None
            } else if file.is_empty() {
                Some(name.to_string())
            } else {
                Some(format!("{} ({}:{})", name, file, line))
            }
        })
        .take(10)
        .collect()
}

/// P2-5: 处理 Python Soul 发来的事件，更新 TUI 状态
fn handle_soul_event(app: &mut App, event: serde_json::Value) {
    let event_type = event["type"].as_str().unwrap_or("");
    match event_type {
        // FIX-E: litellm 计算的精确成本（via Python Soul cost_update event）
        "cost_update" => {
            let cost = event["cost_usd"].as_f64().unwrap_or(0.0);
            let in_tok = event["input_tokens"].as_u64().unwrap_or(0) as usize;
            let out_tok = event["output_tokens"].as_u64().unwrap_or(0) as usize;

            // Session-level accumulators (for /cost command: total $ spent, total tokens processed)
            app.session_cost_usd += cost;
            app.tokens_input += in_tok;
            app.tokens_output += out_tok;
            app.tokens_used = app.tokens_input + app.tokens_output;

            // Current-turn values: SET (not accumulated) so the token bar
            // shows "how full is the context window RIGHT NOW?" not a growing sum.
            // in_tok = full prompt sent this turn (system + history + user) = current context size.
            app.current_ctx_tokens = in_tok;
            app.last_out_tokens = out_tok;

            // Update the LAST assistant message's token display with real output count.
            if out_tok > 0 {
                for msg in app.messages.iter_mut().rev() {
                    if let ChatMessage::Assistant { tokens, .. } = msg {
                        *tokens = out_tok;
                        app.cache_dirty = true;
                        break;
                    }
                }
            }
        }
        "tool_call_start" => {
            let tool = event["tool"].as_str().unwrap_or("").to_string();
            let display = event["display"].as_str().unwrap_or(&tool).to_string();

            // Insert the ToolCall BEFORE the empty Assistant placeholder (if one exists at the
            // end of messages). start_streaming() pushes an empty Assistant{} immediately when
            // the user submits, but tool calls actually execute BEFORE the model generates text.
            // Without this fix, ToolCall badges appear after the response text — visually wrong
            // and easily missed. Inserting before the placeholder preserves the correct order:
            //   ↻ tool_a …
            //   ✓ tool_a …
            //   ◆ model   <response text>
            let insert_at = match app.messages.last() {
                Some(ChatMessage::Assistant { content, .. }) if content.is_empty() => {
                    app.messages.len().saturating_sub(1)
                }
                _ => app.messages.len(),
            };
            app.messages.insert(
                insert_at,
                ChatMessage::ToolCall {
                    tool: tool.clone(),
                    display: display.clone(),
                    ok: None,
                },
            );
            app.invalidate_cache();
            app.state = AppState::CallingTool { tool, display };
        }
        "tool_call_done" => {
            let ok = event["ok"].as_bool().unwrap_or(true);
            // 更新最后一条 ToolCall 消息的状态（⟳ → ✓/✗）
            for msg in app.messages.iter_mut().rev() {
                if let ChatMessage::ToolCall {
                    ok: ref mut status, ..
                } = msg
                {
                    if status.is_none() {
                        *status = Some(ok);
                        break;
                    }
                }
            }
            // BUGFIX: Invalidate cache so the ✓/✗ icon actually renders.
            // Without this, the tool call stays showing ⟳ even after completion.
            app.invalidate_cache();
            // 恢复到流式状态（继续等待 AI 响应）
            if matches!(app.state, AppState::CallingTool { .. }) {
                app.state = AppState::Streaming { tokens_received: 0 };
            }
        }
        "skill_started" => {
            let skill_id = event["skill_id"].as_str().unwrap_or("").to_string();
            let skill_name = event["skill_name"]
                .as_str()
                .unwrap_or(&skill_id)
                .to_string();
            let total_steps = event["total_steps"].as_u64().unwrap_or(1) as usize;
            app.start_skill(&skill_id, &skill_name, total_steps);
        }
        "skill_step" => {
            let step_idx = event["step_idx"].as_u64().unwrap_or(0) as usize;
            let step_name = event["step_name"].as_str().unwrap_or("running").to_string();
            let status = match event["status"].as_str() {
                Some("waiting_approval") => StepStatus::WaitingApproval,
                Some("done") => StepStatus::Done,
                Some("failed") => {
                    StepStatus::Failed(event["error"].as_str().unwrap_or("").to_string())
                }
                _ => StepStatus::Running,
            };
            app.update_skill_step(step_idx, &step_name, status);
        }
        "skill_finished" => {
            let skill_id = event["skill_id"].as_str().unwrap_or("").to_string();
            let ok = event["ok"].as_bool().unwrap_or(false);
            let steps_done = event["steps"].as_u64().unwrap_or(0) as usize;
            let summary = event["summary"].as_str().unwrap_or("").to_string();
            app.finish_skill(&skill_id, ok, steps_done, &summary);
        }
        // Python WARNING / ERROR log lines.
        // Push a transient notification instead of adding to chat history.
        // The full details are in evocli.log (F12 to view).
        "log" => {
            let level = event["level"].as_str().unwrap_or("info");
            let message = event["message"].as_str().unwrap_or("");
            let notif_level = match level {
                "error" | "critical" => app::NotifLevel::Error,
                "warning" | "warn" => app::NotifLevel::Warn,
                _ => return, // INFO → silent
            };
            let icon = if notif_level == app::NotifLevel::Error {
                "⛔"
            } else {
                "⚠"
            };
            app.notify(format!("{icon} {message}  ·  F12 for details"), notif_level);
        }

        // Soul status — routing strategy:
        //   "loading" → transient notification bar (not permanent chat message)
        //               Reason: "⏳ Building context..." must NOT persist after
        //               the response arrives. Previously it stayed forever, making
        //               users think the AI was still working.
        //   "ready"   → transient notification (brief confirmation, auto-expires)
        //               Exception: Memory/model ready messages ARE shown in chat
        //               on first startup (they're meaningful system events).
        //   "error"   → transient notification (prominent)
        "soul_status" => {
            let status = event["status"].as_str().unwrap_or("info");
            let message = event["message"].as_str().unwrap_or("");
            match status {
                "loading" => {
                    // Update thinking_label so input bar border shows real progress.
                    // e.g. "Loading context…" → "Calling LLM…" as stages advance.
                    app.thinking_label = message.to_string();
                    // Transient: show in notification bar, auto-expires in 8s.
                    // Never pushed to permanent messages to avoid "stuck loading" UX.
                    app.notify(format!("⏳ {message}"), app::NotifLevel::Info);
                }
                "ready" => {
                    // Clear thinking_label — we're no longer in a loading stage.
                    app.thinking_label.clear();
                    // "Memory ready" and similar startup messages → chat (one-time info)
                    // but only if they look like startup completion messages.
                    if message.contains("ready")
                        || message.contains("✅")
                        || message.contains("loaded")
                    {
                        app.messages
                            .push(ChatMessage::System(format!("✅ {message}")));
                        app.invalidate_cache();
                    } else {
                        // Other ready messages (e.g. restart confirmation) → transient
                        app.notify(format!("✅ {message}"), app::NotifLevel::Info);
                    }
                }
                "error" => {
                    app.thinking_label.clear();
                    app.notify(
                        format!("⛔ {message}  ·  F12 for details"),
                        app::NotifLevel::Error,
                    );
                }
                _ => {}
            }
        }

        _ => {} // 其他事件忽略（soul_ready 等）
    }
}

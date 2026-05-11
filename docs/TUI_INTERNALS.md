# TUI Internals

Developer guide for understanding and extending the terminal interface. Built with [ratatui](https://github.com/ratatui/ratatui) 0.29 + [crossterm](https://github.com/crossterm-rs/crossterm) 0.28.

---

## File Structure

```
crates/tui/src/
├── app.rs          Application state machine
├── event_handler.rs Keyboard → EventAction translation
├── lib.rs          Async event loop (run() entry point)
└── ui.rs           All rendering functions
```

---

## App State (`app.rs`)

`App` is the single source of truth for all UI state:

```rust
pub struct App {
    // Messages
    pub messages:       Vec<ChatMessage>,   // full conversation history
    pub message_queue:  VecDeque<String>,   // typed while AI is busy
    pub state:          AppState,

    // Scroll
    pub scroll:         usize,   // in VISUAL ROWS (not line indices)

    // Rendering cache
    pub line_cache:     Option<(u16, Vec<Line<'static>>)>,
    pub cache_dirty:    bool,

    // Streaming
    pub stream_skip_frames: u8,

    // Spinner / cursor
    pub spinner_tick:   u8,
    pub cursor_visible: bool,

    // Token tracking
    pub tokens_used:    usize,
    pub tokens_input:   usize,
    pub tokens_output:  usize,
    pub session_cost_usd: f64,
    pub max_context_tokens: usize,

    // Request timer (for elapsed display)
    pub request_start:  Option<std::time::Instant>,

    // Notifications (transient, auto-expire)
    pub notification:   Option<Notification>,

    // Debug overlay
    pub show_debug:     bool,
    pub debug_log_lines: Vec<String>,

    // Slash command suggestions
    pub suggestions:    Vec<(&'static str, &'static str)>,
    pub show_suggestions: bool,

    // Misc
    pub model_name:     String,
    pub project_dir:    String,
    pub should_quit:    bool,
}
```

### `AppState` Enum

```rust
pub enum AppState {
    Idle,
    Thinking,                           // waiting for call_stream() to return
    Streaming { tokens_received: usize }, // receiving chunks
    CallingTool { tool: String, display: String },
    WaitingApproval { message: String },
    WaitingChoice {
        title:        String,
        options:      Vec<ChoiceItem>,
        selected_idx: usize,
        allow_custom: bool,
        custom_input: String,
        custom_mode:  bool,
    },
    SkillRunning {
        skill_id: String, skill_name: String,
        current_step: usize, total_steps: usize,
        step_name: String, step_status: StepStatus,
    },
    Error(String),
}
```

### `ChatMessage` Enum

```rust
pub enum ChatMessage {
    User(String),
    Assistant { content: String, model: String, tokens: usize },
    System(String),          // notifications, loading messages
    ToolCall { tool: String, display: String, ok: Option<bool> },
    SkillResult { skill_id: String, ok: bool, steps: usize, summary: String },
    CallChain { symbol: String, file: String, line: u32,
                incoming: Vec<String>, outgoing: Vec<String> },
}
```

---

## Event Handler (`event_handler.rs`)

Converts `crossterm::KeyEvent` into `EventAction`. The handler checks state in priority order:

```
Ctrl+C         → EventAction::Quit              (always)
F12            → toggle debug overlay             (always, KeyPress only)
Esc (debug on) → close debug overlay              (always)
WaitingApproval → ApprovalResponse(bool)
WaitingChoice  → ChoiceResponse(ChoiceResult)    (↑↓/1-9/Enter/Esc/i)
non-Idle       → scroll / Interrupt / Queue      (Streaming, Thinking, etc.)
show_suggestions → suggestion navigation
default        → submit / scroll / textarea input
```

### Adding a New Key Binding

Add a new arm in `handle_key_event` before the final `match key.code` block:

```rust
// Global shortcut — works in all states
if key.code == KeyCode::F(5) && key.kind == KeyEventKind::Press {
    app.some_action();
    return EventAction::None;
}
```

For state-specific handling, add to the appropriate guard block.

### Adding a New `EventAction`

```rust
// 1. Add variant to EventAction enum in event_handler.rs:
pub enum EventAction {
    // ... existing ...
    MyNewAction(String),
}

// 2. Handle it in lib.rs inside the keyboard event match:
EventAction::MyNewAction(data) => {
    // do something with data
    app.messages.push(ChatMessage::System(format!("Action: {}", data)));
    app.invalidate_cache();
}
```

---

## Event Loop (`lib.rs`)

The main loop uses `tokio::select!` to handle multiple async event sources concurrently:

```rust
loop {
    // ── Draw every iteration ──────────────────────────────────────────
    terminal.draw(|f| ui::draw(f, &mut app, &textarea))?;

    // ── Wait for any event ────────────────────────────────────────────
    tokio::select! {
        // Keyboard input
        Some(event) = event_rx.recv() => { /* ... */ }

        // AI stream chunks
        Some(chunk) = chunk_rx.recv() => { /* ... */ }

        // Call chain results
        Some(msg) = chain_rx.recv() => { /* ... */ }

        // Python Soul events (drain up to 20 per tick)
        _ = async { /* bridge.next_event() loop */ } => {}

        // Blink timer (500ms)
        _ = blink.tick() => {
            app.cursor_visible = !app.cursor_visible;
            app.spinner_tick = app.spinner_tick.wrapping_add(1);
            app.tick_notification();
        }
    }

    // ── Check approval/choice pending (outside select to avoid borrow issues) ──
    if !matches!(app.state, AppState::WaitingApproval {..}) {
        if let Some(msg) = bridge.get_pending_approval().await {
            app.state = AppState::WaitingApproval { message: msg };
        }
    }
    // ... similar for WaitingChoice
}
```

**Draw rate**: driven by events, not a timer. Minimum ~2 Hz from blink timer.

---

## Renderer (`ui.rs`)

### Layout

```
area (full terminal)
├── chunks[0]  Length(2)    title bar
├── chunks[1]  Min(6)       chat area
├── chunks[2]  Length(0/1)  notification bar (0 when no notification)
├── chunks[3]  Length(4)    input area (textarea)
└── chunks[4]  Length(1)    status bar
```

Overlays (rendered on top via `Clear` widget):
- Suggestions popup (above input area)
- Approval modal (centered)
- Choice modal (centered)
- Debug log overlay (bottom-right, 75%×55%)

### Adding a New Widget

1. Add state to `App` struct in `app.rs`
2. Add layout constraint in `draw()` in `ui.rs` if you need dedicated space
3. Write a `draw_my_widget(f: &mut Frame, app: &App, area: Rect)` function
4. Call it from `draw()` at the appropriate position

For overlays (no dedicated layout space):
```rust
fn draw_my_overlay(f: &mut Frame, app: &App) {
    let area = f.area();
    // Calculate overlay rect
    let pa = Rect { x: ..., y: ..., width: ..., height: ... };
    f.render_widget(Clear, pa);    // clear background
    f.render_widget(my_widget, pa);
}
// In draw():
if app.show_my_overlay {
    draw_my_overlay(f, app);
}
```

### Color System

All colors are defined as constants at the top of `ui.rs`:

```rust
// Backgrounds
const BG_BASE:    Color = Color::Rgb(24, 25, 38);    // main background
const BG_SURFACE: Color = Color::Rgb(30, 32, 48);    // panels, input
const BG_CODE:    Color = Color::Rgb(36, 38, 58);    // code blocks

// Text hierarchy
const FG_TEXT:    Color = Color::Rgb(202, 211, 245); // primary text
const FG_SUBTEXT: Color = Color::Rgb(166, 173, 200); // secondary
const FG_DIM:     Color = Color::Rgb(110, 115, 141); // timestamps, hints

// Semantic
const C_PURPLE: Color = Color::Rgb(198, 160, 246); // thinking/streaming
const C_ORANGE: Color = Color::Rgb(245, 169, 127); // tool calls
const C_RED:    Color = Color::Rgb(237, 135, 150); // errors
const C_GREEN:  Color = Color::Rgb(166, 218, 149); // success
const C_YELLOW: Color = Color::Rgb(238, 212, 159); // warnings/headings
const C_TEAL:   Color = Color::Rgb(139, 213, 202); // tokens/cost
```

---

## Virtual Scrolling

The chat area uses a custom virtual scroll to handle large histories efficiently.

**Cache**: `build_all_lines()` renders all messages into `Vec<Line<'static>>` once, then slices the visible window. The cache is invalidated (`cache_dirty = true`) whenever `app.messages` changes.

**Scroll units**: `app.scroll` is in **visual rows** (not line indices). This correctly handles lines that wrap to multiple visual rows.

```rust
// Visual row count for a cache line
let vrow = if content_width == 0 || chars == 0 { 1 }
           else { (chars + content_width - 1) / content_width };
```

**Scroll calculation**:
```
vcum[i] = cumulative visual rows before line i
start_idx = largest i where vcum[i] <= scroll_vrow
row_offset = scroll_vrow - vcum[start_idx]

Paragraph::new(slice[start_idx..end_idx])
    .wrap(Wrap { trim: false })
    .scroll((row_offset, 0))
```

**Auto-scroll to bottom**: `app.scroll = usize::MAX` — clamped to `max_scroll` in `draw_chat_area`.

---

## Streaming Chunk Handling

Chunks arrive via `mpsc::unbounded_channel` and are appended in `app.append_token()`:

```rust
pub fn append_token(&mut self, text: &str) {
    // Use rev().find() because soul_status events may push System messages
    // AFTER start_streaming() creates the Assistant placeholder.
    // last_mut() would find the System message, not the Assistant.
    if let Some(ChatMessage::Assistant { content, .. }) = self.messages
        .iter_mut().rev()
        .find(|m| matches!(m, ChatMessage::Assistant { .. }))
    {
        content.push_str(text);
    }
    // ...
}
```

**Throttle**: redraws happen every `STREAM_REDRAW_EVERY` (3) tokens during streaming to reduce CPU load.

---

## Testing the TUI

The TUI is difficult to unit-test directly. The recommended approach:

1. **Unit test `App` state methods** — they have no rendering dependencies
2. **Unit test `build_all_lines`** — pure function, easy to test
3. **Integration test via tracer** — use `scripts/run_tracer.ps1` to exercise the full pipeline

```rust
// Example App state test
#[test]
fn test_append_token_with_system_message() {
    let mut app = App::new("gpt-4o".into(), 128_000);
    app.start_streaming();
    // Simulate soul_status event pushing a System message during streaming
    app.messages.push(ChatMessage::System("⏳ Building context...".into()));
    // Token should still go to the Assistant message
    app.append_token("Hello");
    if let Some(ChatMessage::Assistant { content, .. }) = app.messages.iter()
        .find(|m| matches!(m, ChatMessage::Assistant { .. }))
    {
        assert_eq!(content, "Hello");
    }
}
```

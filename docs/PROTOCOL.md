# JSON-RPC Protocol

EvoCLI communicates between the Rust Host and Python Soul via JSON-RPC 2.0 over stdin/stdout. Understanding this protocol is essential for adding new capabilities or debugging IPC issues.

---

## Transport

- **Channel**: `stdin` (Rust → Python) and `stdout` (Python → Rust)
- **Framing**: newline-delimited JSON (one JSON object per line)
- **Encoding**: UTF-8 enforced on both sides
- **Concurrency**: multiple requests can be in-flight; matched by `id`

```
Rust Host                           Python Soul
    │                                    │
    │── {"id":"1","method":"tool.call"} ──▶│
    │                                    │
    │◀── {"id":"1","result": {...}} ──────│
    │                                    │
    │◀── {"method":"stream.chunk",...} ──│   (no id = notification)
    │◀── {"method":"event.emit",...} ────│
```

---

## Message Types

### 1. Tool Call (Rust → Python)

The Rust Host dispatches Python-side RPC methods:

```json
{
  "id":     "550e8400-e29b-41d4-a716-446655440000",
  "method": "tool.call",
  "params": {
    "tool": "agent.stream",
    "args": { "prompt": "Fix the auth bug" }
  }
}
```

### 2. Response (Python → Rust)

```json
{
  "id":     "550e8400-e29b-41d4-a716-446655440000",
  "result": { "text": "I found the bug..." },
  "error":  null
}
```

Error response:
```json
{
  "id":    "550e8400-e29b-41d4-a716-446655440000",
  "result": null,
  "error": { "code": -32603, "message": "No API key configured" }
}
```

Standard error codes:
| Code | Meaning |
|---|---|
| `-32700` | Parse error |
| `-32600` | Invalid request |
| `-32601` | Method not found |
| `-32602` | Invalid params |
| `-32603` | Internal error |
| `-32000` | Application-specific error |

### 3. Stream Chunk (Python → Rust)

Used for streaming LLM responses to the TUI:

```json
{
  "method": "stream.chunk",
  "params": {
    "id":   "stream-uuid-matches-original-request",
    "text": "Hello, here is ",
    "done": false
  }
}
```

Final chunk (marks stream complete):
```json
{
  "method": "stream.chunk",
  "params": { "id": "stream-uuid", "text": "", "done": true }
}
```

### 4. Event Notification (Python → Rust)

One-way notifications for TUI updates. No `id`, no response expected.

```json
{
  "method": "event.emit",
  "params": {
    "type":    "soul_status",
    "status":  "loading",
    "message": "Loading embedding model..."
  }
}
```

### 5. Reverse Tool Call (Python → Rust)

Python Soul requests the Rust Host to execute a tool:

```json
{
  "id":     "tool-call-uuid",
  "method": "tool.call",
  "params": {
    "tool": "shell.run",
    "args": { "cmd": "cargo check 2>&1" }
  }
}
```

Rust replies with a normal response (same format as #2).

---

## Event Types

Events emitted by the Python Soul via `event.emit`:

### `soul_status`
User-facing progress notifications (shown in TUI chat).
```json
{
  "type":    "soul_status",
  "status":  "loading",    // "loading" | "ready" | "error"
  "message": "Loading embedding model..."
}
```

### `log`
Developer-facing log entries (shown in F12 debug panel).
```json
{
  "type":    "log",
  "level":   "warning",   // "debug" | "info" | "warning" | "error" | "critical"
  "logger":  "evocli.agent",
  "message": "Primary stream failed, retrying...",
  "exc":     null         // traceback string if exception
}
```

### `tool_call_start`
AI is calling a tool (shown inline in chat).
```json
{
  "type":    "tool_call_start",
  "tool":    "shell.run",
  "display": "$ cargo check"
}
```

### `tool_call_done`
Tool call completed.
```json
{
  "type": "tool_call_done",
  "ok":   true
}
```

### `skill_started` / `skill_step` / `skill_finished`
Skill execution progress (shown in TUI skill panel).
```json
// skill_started
{ "type": "skill_started", "skill_id": "fix_compile_error",
  "skill_name": "Fix Compile Error", "total_steps": 3 }

// skill_step
{ "type": "skill_step", "step_idx": 1, "step_name": "Analyzing errors",
  "status": "running" }  // "running" | "done" | "failed" | "waiting_approval"

// skill_finished
{ "type": "skill_finished", "skill_id": "fix_compile_error",
  "ok": true, "steps": 3, "summary": "Fixed 2 compile errors" }
```

### `cost_update`
LLM cost update (precise cost from litellm's `completion_cost`).
```json
{
  "type":          "cost_update",
  "cost_usd":      0.0023,
  "input_tokens":  1200,
  "output_tokens": 450
}
```

---

## Emitting Events from Python

```python
from evocli_soul.rpc import emit_event

# In any async context:
await emit_event("soul_status", {
    "status":  "loading",
    "message": "Processing your request..."
})

await emit_event("tool_call_start", {
    "tool":    "shell.run",
    "display": "$ cargo check"
})
```

---

## Writing a New RPC Handler

```python
# evocli-soul/evocli_soul/handlers/mymodule.py
import logging
from evocli_soul.rpc import emit_event

log = logging.getLogger("evocli.handlers.mymodule")


def register(router) -> None:
    router.add("mymod.do_something",  handle_do_something)
    router.add("mymod.stream_result", handle_stream_result)


async def handle_do_something(req_id: str, params: dict, send, state) -> None:
    """Handle a simple request-response call."""
    input_text = params.get("input", "")
    try:
        result = {"output": input_text.upper()}
        await send.response(req_id, result)
    except Exception as e:
        log.exception("do_something failed")
        await send.error(req_id, -32603, str(e))


async def handle_stream_result(req_id: str, params: dict, send, state) -> None:
    """Handle a streaming response."""
    prompt = params.get("prompt", "")
    try:
        await emit_event("soul_status", {"status": "loading", "message": "Generating..."})

        llm = state.get_llm_client()
        async for chunk in llm.stream(prompt):
            await send.stream_chunk(req_id, chunk, done=False)

        await send.stream_chunk(req_id, "", done=True)
    except Exception as e:
        log.error("stream_result failed: %s", e, exc_info=True)
        await send.stream_chunk(req_id, f"Error: {e}", done=True)
```

Register in `evocli-soul/evocli_soul/handlers/__init__.py`:
```python
from . import mymodule

def register_all(router):
    # ... existing ...
    mymodule.register(router)
```

---

## Debugging IPC

Run the Python Soul in isolation to test RPC methods:

```bash
# Ping-pong test
echo '{"id":"1","method":"tool.call","params":{"tool":"tracer.ping","args":{}}}' | \
  python evocli-soul/evocli_soul/main.py

# Stream test
echo '{"id":"1","method":"tool.call","params":{"tool":"tracer.llm_stream","args":{"prompt":"hello"}}}' | \
  python evocli-soul/evocli_soul/main.py
```

Enable debug logging:
```bash
python evocli-soul/evocli_soul/main.py --debug < input.jsonl
```

View live logs in TUI:
- Press `F12` to open the debug log panel
- Or run `evocli debug trace` for detailed IPC traces

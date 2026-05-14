# pyright: reportMissingTypeArgument=false, reportMissingImports=false
"""全局状态懒初始化 — 唯一职责：管理 Soul 内所有单例实例。"""
from __future__ import annotations
import os
import threading
from typing import Optional


def normalize_project_id(project_id: str | None) -> str:
    """Normalize project_id to a consistent absolute path.

    Different callers use ".", "global", None, or an actual path.
    This function maps all these to a single canonical form so memory
    lookups, code-index keying, and context injection always use the same key.

    Mapping rules:
    - None / "" / "." / "global" → SESSION_PROJECT_ROOT (frozen at startup)
    - relative path → os.path.abspath(path)
    - absolute path → as-is (normalized separators)
    """
    if not project_id or project_id in (".", "global", ""):
        return get_session_root()  # Use frozen root — not live os.getcwd()
    return os.path.abspath(project_id)


# ── Session project root (frozen at startup) ─────────────────────────────
# Set ONCE by main.py before any other initialization.
# All callers must use get_session_root() instead of os.getcwd() to prevent
# directory drift when shell commands or tool calls change the working directory.
#
# Continue.dev pattern: workspaceFolders[0].fsPath captured at session init,
# passed as invariant cwd to every tool call.
_session_project_root: str = ""


def set_session_root(path: str) -> None:
    """Freeze the session project root. Call ONCE at process startup.

    Must be called before any state initialization (memory, agent, etc.)
    so that normalize_project_id() and get_session_root() return the correct value.
    """
    global _session_project_root
    import pathlib as _pl
    _session_project_root = str(_pl.Path(path).resolve())


def get_session_root() -> str:
    """Return the frozen session project root.

    Always use this instead of os.getcwd() to prevent directory drift.
    If set_session_root() was not yet called (should not happen in normal flow),
    falls back to the current CWD with a warning.
    """
    if _session_project_root:
        return _session_project_root
    import logging as _log
    _log.getLogger("evocli.state").warning(
        "get_session_root() called before set_session_root() — "
        "falling back to os.getcwd(). Call set_session_root() at startup."
    )
    return os.path.abspath(os.getcwd())

_bridge: Optional[object]       = None
_memories: dict[str, object]    = {}   # project_id → EvoCLIMemory
_skill_engine: Optional[object] = None
_llm_client: Optional[object]   = None
_agent: Optional[object]        = None
_orchestrator: Optional[object] = None
_config: Optional[dict]         = None
_active_subagents: dict[str, object] = {}

# ── Session LRU cache — prevents unbounded memory growth ─────────────────────
# All session-keyed dicts use _LRUSessionCache to automatically evict the
# oldest sessions when the process runs for a long time.
# Default capacity = 100 sessions. Override via config: [system] max_sessions = N

class _LRUSessionCache(dict):
    """
    A dict subclass that evicts the oldest entry when capacity is exceeded.
    Drop-in replacement for `dict` for session_id-keyed state.
    Thread-safe: GIL protects dict operations in CPython.
    """
    def __init__(self, maxsize: int = 100):
        super().__init__()
        self._maxsize = maxsize
        self._order: list = []  # insertion order for LRU eviction

    def __setitem__(self, key, value):
        if key in self:
            self._order.remove(key)
        elif len(self) >= self._maxsize:
            # Evict oldest entry
            oldest = self._order.pop(0)
            super().__delitem__(oldest)
        self._order.append(key)
        super().__setitem__(key, value)

    def __delitem__(self, key):
        if key in self._order:
            self._order.remove(key)
        super().__delitem__(key)

    def pop(self, key, *args):
        if key in self._order:
            self._order.remove(key)
        return super().pop(key, *args)


def _make_session_cache() -> _LRUSessionCache:
    """Create a session cache with capacity from config (default 100)."""
    try:
        from evocli_soul.config_defaults import cfg_int
        cap = cfg_int("system.max_sessions") or 100
    except Exception:
        cap = 100
    return _LRUSessionCache(maxsize=cap)


# ── Todo list (keyed by session_id) — OpenCode TodoWrite pattern ─────────────
_todos: _LRUSessionCache = _make_session_cache()


def set_todos(todos: list[dict], session_id: str = "default") -> None:
    _todos[session_id] = todos


def get_todos(session_id: str = "default") -> list[dict]:
    return list(_todos.get(session_id, []))


def update_todo_status(todo_id: str, status: str, session_id: str = "default") -> bool:
    todos = _todos.get(session_id, [])
    for item in todos:
        if item.get("id") == todo_id:
            item["status"] = status
            return True
    return False


# ── Last terminal output (for @terminal mention) ─────────────────────────────
_terminal_output: str = ""  # last N lines of terminal output


def set_terminal_output(output: str) -> None:
    """Store the most recent terminal output (for @terminal mention context)."""
    global _terminal_output
    # Keep last 200 lines to avoid bloating context
    lines = output.splitlines()
    _terminal_output = "\n".join(lines[-200:])


def get_terminal_output() -> str:
    """Return the most recent terminal output."""
    return _terminal_output

# ── Circuit breaker — Cline consecutiveMistakeCount pattern ─────────────────
# Tracks consecutive tool errors within a single _run_litellm turn.
# When count reaches threshold → inject "stop and report" message to prevent
# the AI from looping endlessly on a broken tool.
#
# Separate from _consecutive_no_tools (which tracks text-only turns at the loop level).
# This tracks individual tool failures within one LLM call cycle.
_tool_failure_counts: _LRUSessionCache = _make_session_cache()  # session_id → consecutive failure count
_CIRCUIT_BREAKER_THRESHOLD = 3  # configurable via config [agent] max_consecutive_failures


def increment_tool_failure(session_id: str) -> int:
    """Increment consecutive tool failure count. Returns new count."""
    _tool_failure_counts[session_id] = _tool_failure_counts.get(session_id, 0) + 1
    return _tool_failure_counts[session_id]


def reset_tool_failure(session_id: str) -> None:
    """Reset failure count on successful tool call."""
    _tool_failure_counts.pop(session_id, None)


def get_tool_failure_count(session_id: str) -> int:
    """Return current consecutive failure count."""
    return _tool_failure_counts.get(session_id, 0)


# ── Doom loop detection (OpenCode DOOM_LOOP_THRESHOLD pattern) ────────────────

_recent_tool_calls: dict[str, list[dict[str, object]]] = {}  # session_id → [{tool, args_hash, ts}]


# ── Cancellation flag (keyed by session_id) ──────────────────────────────────
# Set externally (Ctrl+C handler or cancel RPC) to signal the autonomous loop
# should abort at its next iteration boundary.
_cancelled: _LRUSessionCache = _make_session_cache()


def cancel_session(session_id: str) -> None:
    """Signal that the running task for this session should abort."""
    _cancelled[session_id] = True


def clear_cancel(session_id: str) -> None:
    """Clear the cancel flag. Called at the start of every new request."""
    _cancelled.pop(session_id, None)


def is_cancelled(session_id: str) -> bool:
    """Return True if cancel_session() has been called for this session."""
    return bool(_cancelled.get(session_id, False))


def record_tool_call(tool: str, args: dict[str, object], session_id: str = "default") -> None:
    """Record a tool call for doom loop detection."""
    import json
    import time

    calls = _recent_tool_calls.setdefault(session_id, [])
    # keep only last 15 calls
    if len(calls) >= 15:
        calls.pop(0)
    try:
        args_hash = hash(json.dumps(args, sort_keys=True, default=str))
    except Exception:
        args_hash = 0
    calls.append({"tool": tool, "args_hash": args_hash, "ts": time.time()})


def is_doom_loop(tool: str, args: dict[str, object], session_id: str = "default", threshold: int = 3) -> bool:
    """
    Return True if the exact same tool+args has been called >= threshold times recently.
    Based on OpenCode's DOOM_LOOP_THRESHOLD=3.
    """
    import json

    calls = _recent_tool_calls.get(session_id, [])
    try:
        args_hash = hash(json.dumps(args, sort_keys=True, default=str))
    except Exception:
        args_hash = 0
    count = sum(1 for c in calls[-10:] if c["tool"] == tool and c["args_hash"] == args_hash)
    return count >= threshold


def clear_doom_loop_state(session_id: str = "default") -> None:
    """Reset doom loop tracking for a session (call at task start)."""
    _recent_tool_calls.pop(session_id, None)


# ── task_complete signal (Cline attempt_completion / Gemini complete_task pattern) ──
# The AI calls task_complete tool to signal it believes the task is done.
# The autonomous loop in handlers/agent.py polls this to decide when to stop.
#
# Design:
#   - set_task_complete()   → called by task_complete tool in agent.py
#   - get_task_complete()   → polled by autonomous loop each iteration
#   - clear_task_complete() → called at start of each new request + after loop exits
#   - _task_double_checked  → Cline's "double-check" pattern: first attempt_completion
#                             is rejected with a re-verify prompt; only second passes
_task_complete: _LRUSessionCache = _make_session_cache()       # session_id → {result, command, ts}
_task_double_checked: _LRUSessionCache = _make_session_cache() # session_id → True once re-verified
# Per-iteration tool-call counter (reset each autonomous loop iteration)
_iteration_tool_counts: _LRUSessionCache = _make_session_cache()  # session_id → count


def set_task_complete(session_id: str, result: str, command: str = "") -> None:
    """Signal that the AI believes the task is done."""
    import time as _time
    _task_complete[session_id] = {
        "result":  result,
        "command": command,
        "ts":      _time.time(),
    }


def get_task_complete(session_id: str) -> "dict | None":
    """Return the task_complete signal if set, else None."""
    return _task_complete.get(session_id)


def clear_task_complete(session_id: str) -> None:
    """Clear the task_complete signal and double-check state for a new request."""
    _task_complete.pop(session_id, None)
    _task_double_checked.pop(session_id, None)
    _iteration_tool_counts.pop(session_id, None)


def is_task_double_checked(session_id: str) -> bool:
    """Return True if the AI already did the Cline double-check re-verify step."""
    return _task_double_checked.get(session_id, False)


def mark_task_double_checked(session_id: str) -> None:
    """Mark that the AI has re-verified its work (allows next task_complete to pass)."""
    _task_double_checked[session_id] = True


def increment_iteration_tool_count(session_id: str) -> int:
    """Increment tool call counter for current iteration. Returns new count."""
    _iteration_tool_counts[session_id] = _iteration_tool_counts.get(session_id, 0) + 1
    return _iteration_tool_counts[session_id]


def reset_iteration_tool_count(session_id: str) -> None:
    """Reset tool call counter at the start of each autonomous loop iteration."""
    _iteration_tool_counts[session_id] = 0


def get_iteration_tool_count(session_id: str) -> int:
    """Return how many tools were called in the current iteration."""
    return _iteration_tool_counts.get(session_id, 0)


# GAP-3: Per-session event accumulator for memory distillation.
# FIXED: was a global list shared across all sessions — caused concurrent session corruption.
# Now keyed by session_id so each session accumulates its own events.
_session_events: _LRUSessionCache = _make_session_cache()  # session_id → [event, ...]

# ── Multi-turn conversation history (keyed by session_id) ──────────────────
# Implements Aider/Claude Code pattern: persist history server-side so Rust TUI
# doesn't need to send it back. Each entry: {"role": "user"|"assistant", "content": str}
# Tool messages are NOT stored (they bloat history without adding recall value).
# Key: session_id (str); Value: list of message dicts
_conversation_histories: _LRUSessionCache = _make_session_cache()

# ── Session-level context cache (keyed by session_id) ─────────────────────
# Caches expensive computation (RepoMap, memory search results) across turns.
# Invalidated when goal fingerprint OR current file hash changes.
# Keys per session: "goal_fingerprint", "current_file_hash", "repomap_text",
#                   "memory_results", "turn"
_context_caches: _LRUSessionCache = _make_session_cache()

# ── Anchored summary store (keyed by session_id) ──────────────────────────
# When history grows too large, it gets compressed to an Anchored Summary.
# The summary is injected at the front of the next LLM conversation.
_anchored_summaries: _LRUSessionCache = _make_session_cache()

# ── File read tracker (keyed by session_id) ───────────────────────────────
# Cline pattern: if a file is read multiple times in a session, annotate
# subsequent reads with "also read in turn N" to reduce redundant large content.
# Keys: path → turn_number of first read
_files_read: _LRUSessionCache = _make_session_cache()  # session_id -> {path: turn}

# ── Current turn counter (keyed by session_id) ────────────────────────────
_current_turns: _LRUSessionCache = _make_session_cache()  # session_id -> turn_number

# ── Explicitly added files (keyed by session_id) ──────────────────────────
# Aider /add pattern: files pinned by user persist for the whole session.
# They're injected into every turn's context automatically.
_added_files: _LRUSessionCache = _make_session_cache()  # session_id → [path, ...]

_init_lock = threading.Lock()

# ── Added files API ───────────────────────────────────────────────────────

def add_file(path: str, session_id: str = "default") -> None:
    """Pin a file to session context (Aider /add pattern)."""
    if session_id not in _added_files:
        _added_files[session_id] = []
    if path not in _added_files[session_id]:
        _added_files[session_id].append(path)


def get_added_files(session_id: str = "default") -> list[str]:
    """Return all pinned files for a session."""
    return list(_added_files.get(session_id, []))


def remove_added_files(session_id: str, paths: list[str]) -> list[str]:
    """Remove specific files from pinned context. Returns actually removed paths."""
    existing = _added_files.get(session_id, [])
    removed = [p for p in paths if p in existing]
    _added_files[session_id] = [p for p in existing if p not in paths]
    return removed


def clear_added_files(session_id: str = "default") -> None:
    _added_files.pop(session_id, None)

# ── History API ───────────────────────────────────────────────────────────

# History persistence directory: ~/.evocli/history/{session_id}.json
# Written after every append, read lazily on first get_history() call.
_HISTORY_DIR = None  # resolved lazily

def _history_path(session_id: str):
    """Return the on-disk path for a session's conversation history.

    session_id is sanitized to prevent path traversal attacks AND file-name collisions:
    - Only alphanumeric chars, hyphens, underscores, and dots are kept as-is
    - Characters outside the safe set are replaced with '_' in the readable prefix
    - A short SHA-256 suffix is appended to avoid collisions between session IDs
      that differ only in unsafe chars (e.g. "a/b" vs "a?b" both sanitize to "a_b")
    - Total filename length capped at 140 chars + ".json"
    """
    from pathlib import Path
    import re
    import hashlib
    sid_str = str(session_id)
    # Readable prefix (safe chars only)
    safe_prefix = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', sid_str)[:80]
    # Collision-resistant suffix: first 12 hex chars of SHA-256
    suffix = hashlib.sha256(sid_str.encode()).hexdigest()[:12]
    safe_name = f"{safe_prefix}_{suffix}" if safe_prefix else suffix
    return Path.home() / ".evocli" / "history" / f"{safe_name}.json"


def _load_history_from_disk(session_id: str) -> list[dict]:
    """Load history from disk. Returns [] on any error (safe degradation)."""
    import json
    try:
        path = _history_path(session_id)
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


def _save_history_to_disk(session_id: str, history: list[dict]) -> None:
    """Persist history to disk atomically (best-effort — never raises).

    Uses write-to-temp + os.replace() so a crash mid-write cannot produce
    a truncated/corrupt history file (Cline atomic write pattern).
    os.replace() is atomic on POSIX and same-volume Windows.
    """
    import json
    try:
        path = _history_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_path, path)
    except Exception:
        pass


def get_history(session_id: str = "default") -> list[dict]:
    """Return conversation history for a session (safe copy).
    
    Loads from disk on first access so history persists across process restarts.
    This enables true session resume: user restarts evocli and picks up where they left off.
    """
    if session_id not in _conversation_histories:
        # First access: try to load from disk (cross-restart continuity)
        loaded = _load_history_from_disk(session_id)
        if loaded:
            _conversation_histories[session_id] = loaded
    return list(_conversation_histories.get(session_id, []))


def append_history(messages: list[dict], session_id: str = "default") -> None:
    """Append user+assistant messages to session history.

    Only call with user/assistant role messages — not tool messages.

    Large content (e.g. assistant replies with embedded code blocks) is
    automatically summarised to keep history lean:
    - user messages    > TOOL_RESULT_PRUNE_CHARS: truncated to first 400 chars
    - assistant messages > TOOL_RESULT_PRUNE_CHARS: kept but tail truncated
    This prevents multi-turn history from ballooning with prior file reads
    that the model has already processed (Cline deduplication pattern).
    """
    _TOOL_RESULT_PRUNE_CHARS = 2000  # ~500 tokens — above this we summarise
    if session_id not in _conversation_histories:
        _conversation_histories[session_id] = []

    pruned: list[dict] = []
    for msg in messages:
        role    = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, str) and len(content) > _TOOL_RESULT_PRUNE_CHARS:
            if role == "user":
                # User messages that are too long are usually context-injected file reads.
                # Keep the first 400 chars (the actual user question) + a note.
                truncated = content[:400].rstrip()
                note = f"\n\n[Note: message truncated from {len(content)} chars — original content visible to AI in that turn only]"
                pruned.append({**msg, "content": truncated + note})
            elif role == "assistant":
                # Assistant replies with large code generation: keep first + last sections
                head = content[:600]
                tail = content[-300:]
                note = f"\n[... {len(content) - 900} chars omitted from history ...]\n"
                pruned.append({**msg, "content": head + note + tail})
            else:
                pruned.append(msg)
        else:
            pruned.append(msg)

    _conversation_histories[session_id].extend(pruned)
    # Persist to disk for cross-restart continuity (best-effort)
    _save_history_to_disk(session_id, _conversation_histories[session_id])


def set_history(messages: list[dict], session_id: str = "default") -> None:
    """Replace entire conversation history for a session (used by /undo)."""
    _conversation_histories[session_id] = list(messages)
    # Persist to disk for cross-restart continuity (best-effort)
    _save_history_to_disk(session_id, _conversation_histories[session_id])


def clear_history(session_id: str = "default") -> None:
    """Clear raw message history for a session while PRESERVING the anchored summary.

    The anchored summary is the whole point of /compress — it must survive the clear.
    Only the raw message list is cleared; the summary acts as the new compact "memory"
    of what happened before.

    Also removes (or truncates) the on-disk history file so cleared history cannot
    accidentally resurrect after a process restart (e.g., `evocli session resume`).
    """
    _conversation_histories.pop(session_id, None)
    _context_caches.pop(session_id, None)
    # _anchored_summaries intentionally NOT cleared — survives /compress
    # The summary IS the session context after compression.
    _files_read.pop(session_id, None)
    _current_turns.pop(session_id, None)
    # Note: _added_files intentionally NOT cleared — user's /add persists across /compress

    # Remove on-disk history so cleared state survives restarts.
    # Anchored summary is NOT persisted here — it's only needed within a running session.
    # When a session resumes after /compress: the raw history is [] (disk deleted),
    # and the model starts fresh. The summary is gone after restart by design — users
    # must /compress again in the new session if they want a compact history.
    try:
        path = _history_path(session_id)
        if path.exists():
            path.unlink(missing_ok=True)
    except Exception:
        pass


def get_history_token_estimate(session_id: str = "default") -> int:
    """Rough token count of stored history (char // 4 heuristic, no ML needed)."""
    history = _conversation_histories.get(session_id, [])
    return sum(len(str(m.get("content", ""))) for m in history) // 4


# ── Context cache API ─────────────────────────────────────────────────────

def get_context_cache(session_id: str = "default") -> dict:
    """Return the session context cache dict (mutable reference)."""
    if session_id not in _context_caches:
        _context_caches[session_id] = {}
    return _context_caches[session_id]


def update_context_cache(updates: dict, session_id: str = "default") -> None:
    """Merge updates into the session context cache."""
    if session_id not in _context_caches:
        _context_caches[session_id] = {}
    _context_caches[session_id].update(updates)


# ── Anchored summary API ──────────────────────────────────────────────────

def get_anchored_summary(session_id: str = "default") -> str:
    return _anchored_summaries.get(session_id, "")


def set_anchored_summary(text: str, session_id: str = "default") -> None:
    _anchored_summaries[session_id] = text


# ── File read tracker API ─────────────────────────────────────────────────

def record_file_read(path: str, session_id: str = "default") -> int:
    """Record that a file was read. Returns 1 (first read) or 2+ (repeat read).
    
    Cline pattern: caller can annotate repeat reads so history doesn't re-bloat.
    """
    turn = get_current_turn(session_id)
    if session_id not in _files_read:
        _files_read[session_id] = {}
    if path not in _files_read[session_id]:
        _files_read[session_id][path] = turn
        return 1
    return 2  # already read in a prior turn


def get_file_first_read_turn(path: str, session_id: str = "default") -> "Optional[int]":
    """Return the turn number when path was first read, or None."""
    return _files_read.get(session_id, {}).get(path)


def get_files_read_this_session(session_id: str = "default") -> set:
    """Return the set of file paths read in this session (for env details injection)."""
    return set(_files_read.get(session_id, {}).keys())


# ── Turn counter API ──────────────────────────────────────────────────────

def increment_turn(session_id: str = "default") -> int:
    """Increment and return the current turn number for a session."""
    _current_turns[session_id] = _current_turns.get(session_id, 0) + 1
    return _current_turns[session_id]


def get_current_turn(session_id: str = "default") -> int:
    return _current_turns.get(session_id, 0)


# ── Session event buffer (GAP-3: memory distillation) ─────────────────────

# ── Gemini-style tool scratchpad ─────────────────────────────────────────────
# Tracks the sequence of tools called in a session as a compact breadcrumb trail.
# Injected into environment_details per turn so AI knows "what was already tried".
# Format: "tool1 → tool2 | tool3 → tool4" (pipes = iteration boundaries)
_tool_scratchpads: _LRUSessionCache = _make_session_cache()  # session_id → [[turn_tools], ...]


def record_tool_in_scratchpad(tool_name: str, session_id: str = "default") -> None:
    """Append a tool call to the current iteration's scratchpad."""
    if session_id not in _tool_scratchpads:
        _tool_scratchpads[session_id] = [[]]
    if not _tool_scratchpads[session_id]:
        _tool_scratchpads[session_id].append([])
    # Compact: extract base name only (shell_run("cargo test") → "shell_run")
    _tool_scratchpads[session_id][-1].append(tool_name)


def new_scratchpad_iteration(session_id: str = "default") -> None:
    """Start a new iteration boundary in the scratchpad."""
    if session_id not in _tool_scratchpads:
        _tool_scratchpads[session_id] = [[]]
    else:
        _tool_scratchpads[session_id].append([])


def get_scratchpad_summary(session_id: str = "default", max_iters: int = 5) -> str:
    """Return compact breadcrumb: 'fs_read → fs_write | test_and_capture → task_complete'"""
    pads = _tool_scratchpads.get(session_id, [])
    if not pads:
        return ""
    # Show last max_iters iterations
    recent = pads[-max_iters:]
    iter_strs = [" → ".join(t for t in itr if t) for itr in recent if itr]
    return " | ".join(iter_strs)


def append_session_event(event: dict, session_id: str = "default") -> None:
    """Append a tool/action event to a session's event buffer.

    Called from _execute_tool() and Python-native tool closures in agent.py.
    The accumulated events are consumed by MemoryDistiller at session end (GAP-3).
    Each session has its own buffer — concurrent sessions are isolated.
    """
    if session_id not in _session_events:
        _session_events[session_id] = []
    _session_events[session_id].append(event)


def drain_session_events(session_id: str = "default") -> list[dict]:
    """Return all accumulated session events and clear the buffer.

    Called once per session end by _distill_session() in handlers/agent.py.
    Only drains the specified session's events — other sessions unaffected.
    """
    events = list(_session_events.get(session_id, []))
    _session_events.pop(session_id, None)
    return events


def get_config() -> dict:
    """
    Load and cache config.toml with project-local override.

    Merge order (highest priority wins):
      1. {cwd}/.evocli/config.toml  — project-local overrides
      2. ~/.evocli/config.toml      — global defaults

    Mirrors Rust host config.rs merge logic so Python handlers see the same
    effective configuration as the host.
    Falls back to empty dict if neither file is found or readable.
    """
    global _config
    if _config is None:
        with _init_lock:
            if _config is None:
                try:
                    try:
                        import tomllib
                    except ImportError:
                        import tomli as tomllib  # type: ignore[no-redef]
                    from pathlib import Path

                    def _read(p: Path) -> dict:
                        if p.exists():
                            try:
                                with open(p, "rb") as f:
                                    return tomllib.load(f)
                            except Exception:
                                pass
                        return {}

                    global_cfg  = _read(Path.home() / ".evocli" / "config.toml")
                    project_cfg = _read(Path.cwd() / ".evocli" / "config.toml")

                    def _deep_merge(base: dict, override: dict) -> dict:
                        result = dict(base)
                        for k, v in override.items():
                            if k in result and isinstance(result[k], dict) and isinstance(v, dict):
                                result[k] = _deep_merge(result[k], v)
                            else:
                                result[k] = v
                        return result

                    _config = _deep_merge(global_cfg, project_cfg)
                except Exception as e:
                    import logging
                    logging.getLogger("evocli.state").debug("Config load failed: %s", e)
                    _config = {}
    return _config
    return _config


def get_orchestrator():
    global _orchestrator
    if _orchestrator is None:
        with _init_lock:
            if _orchestrator is None:  # double-check after acquiring lock
                try:
                    from evocli_soul.orchestrator import Orchestrator
                    _orchestrator = Orchestrator(get_bridge(), get_memory())
                except Exception as e:
                    import logging
                    logging.getLogger("evocli.state").debug("Orchestrator init failed: %s", e)
    return _orchestrator


def register_subagent(session_id: str, agent_info: dict) -> None:
    _active_subagents[session_id] = agent_info


def get_active_subagents() -> dict:
    return dict(_active_subagents)


def unregister_subagent(session_id: str) -> None:
    _active_subagents.pop(session_id, None)


def get_bridge():
    global _bridge
    if _bridge is None:
        with _init_lock:
            if _bridge is None:  # double-check after acquiring lock
                from evocli_soul.host_bridge import HostBridge
                _bridge = HostBridge()
    return _bridge


def set_bridge(bridge) -> None:
    global _bridge
    _bridge = bridge


def get_memory(project_id: str | None = None):
    """获取（或创建）指定项目的 EvoCLIMemory 实例。

    project_id 默认为当前工作目录路径（cwd），保证每个项目写入各自的
    LanceDB 行，不再出现多项目 project_id 标签混淆问题。

    向后兼容：所有无参数的 get_memory() 调用自动使用 cwd，
    行为与之前相同（每次从同一目录运行 evocli），
    但现在支持同进程内切换项目。
    """
    global _memories
    pid = normalize_project_id(project_id)  # canonical key: always absolute path
    if pid not in _memories:
        with _init_lock:
            if pid not in _memories:
                from evocli_soul.memory_client import EvoCLIMemory
                _memories[pid] = EvoCLIMemory(project_id=pid)
    return _memories[pid]


def get_memory_if_ready(project_id: str | None = None):
    """Return the memory singleton **without blocking**.

    Returns the already-initialised EvoCLIMemory instance if it's ready,
    or ``None`` if initialisation hasn't finished yet (e.g. still loading
    the fastembed model in the background pre-warm task).

    Reading a module-level reference is atomic under the GIL, so no lock
    is needed for this check-only path.
    """
    pid = normalize_project_id(project_id)  # same canonical key as get_memory()
    return _memories.get(pid)


def get_skill_engine():
    global _skill_engine
    if _skill_engine is None:
        with _init_lock:
            if _skill_engine is None:  # double-check after acquiring lock
                from evocli_soul.skill_engine import SkillEngine
                _skill_engine = SkillEngine(get_bridge())
    return _skill_engine


def get_llm_client(config: dict | None = None):
    global _llm_client
    if _llm_client is None:
        with _init_lock:
            if _llm_client is None:  # double-check after acquiring lock
                from evocli_soul.llm_client import LLMClient
                _llm_client = LLMClient(config or {})
    return _llm_client


def get_agent(config: dict | None = None):
    global _agent
    if _agent is None:
        with _init_lock:
            if _agent is None:  # double-check after acquiring lock
                from evocli_soul.agent import EvoCLIAgent
                # Use actual config from disk if not provided.
                # Previously defaulted to {} which caused pydantic-ai to fail
                # (defaulted to provider="anthropic" for openai endpoint).
                effective_config = config or get_config()
                _agent = EvoCLIAgent(get_bridge(), get_memory(), effective_config)
    return _agent


def reset_all() -> None:
    """测试用：重置所有单例。"""
    global _bridge, _memory, _skill_engine, _llm_client, _agent, _orchestrator, _config, _active_subagents
    # Reset old-style single-instance vars (kept for backwards compat with any lingering refs)
    _bridge = _memory = _skill_engine = _llm_client = _agent = _orchestrator = _config = None
    # Reset new-style per-project memory dict (the actual store since H1 unification)
    _memories.clear()
    _active_subagents.clear()
    _session_events.clear()
    _conversation_histories.clear()
    _context_caches.clear()
    _anchored_summaries.clear()
    _files_read.clear()
    _current_turns.clear()
    _added_files.clear()
    _recent_tool_calls.clear()
    _cancelled.clear()

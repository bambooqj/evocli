"""全局状态懒初始化 — 唯一职责：管理 Soul 内所有单例实例。"""
from __future__ import annotations
import threading
from typing import Optional

_bridge: Optional[object]       = None
_memory: Optional[object]       = None
_skill_engine: Optional[object] = None
_llm_client: Optional[object]   = None
_agent: Optional[object]        = None
_orchestrator: Optional[object] = None
_config: Optional[dict]         = None  # Cached config from ~/.evocli/config.toml
_active_subagents: dict[str, object] = {}  # session_id -> SubAgentSession

# GAP-3: Per-session event accumulator for memory distillation.
# Events are appended during tool execution and drained at session end.
# Thread-safe: GIL protects list.append() and list.clear() in CPython.
_session_events: list[dict] = []

# ── Multi-turn conversation history (keyed by session_id) ──────────────────
# Implements Aider/Claude Code pattern: persist history server-side so Rust TUI
# doesn't need to send it back. Each entry: {"role": "user"|"assistant", "content": str}
# Tool messages are NOT stored (they bloat history without adding recall value).
# Key: session_id (str); Value: list of message dicts
_conversation_histories: dict[str, list[dict]] = {}

# ── Session-level context cache (keyed by session_id) ─────────────────────
# Caches expensive computation (RepoMap, memory search results) across turns.
# Invalidated when goal fingerprint OR current file hash changes.
# Keys per session: "goal_fingerprint", "current_file_hash", "repomap_text",
#                   "memory_results", "turn"
_context_caches: dict[str, dict] = {}

# ── Anchored summary store (keyed by session_id) ──────────────────────────
# When history grows too large, it gets compressed to an Anchored Summary.
# The summary is injected at the front of the next LLM conversation.
_anchored_summaries: dict[str, str] = {}

# ── File read tracker (keyed by session_id) ───────────────────────────────
# Cline pattern: if a file is read multiple times in a session, annotate
# subsequent reads with "also read in turn N" to reduce redundant large content.
# Keys: path → turn_number of first read
_files_read: dict[str, dict[str, int]] = {}  # session_id -> {path: turn}

# ── Current turn counter (keyed by session_id) ────────────────────────────
_current_turns: dict[str, int] = {}  # session_id -> turn_number

# ── Explicitly added files (keyed by session_id) ──────────────────────────
# Aider /add pattern: files pinned by user persist for the whole session.
# They're injected into every turn's context automatically.
_added_files: dict[str, list[str]] = {}  # session_id → [path, ...]

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

def get_history(session_id: str = "default") -> list[dict]:
    """Return conversation history for a session (safe copy)."""
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


def clear_history(session_id: str = "default") -> None:
    """Clear raw message history for a session while PRESERVING the anchored summary.

    The anchored summary is the whole point of /compress — it must survive the clear.
    Only the raw message list is cleared; the summary acts as the new compact "memory"
    of what happened before.
    """
    _conversation_histories.pop(session_id, None)
    _context_caches.pop(session_id, None)
    # _anchored_summaries intentionally NOT cleared — survives /compress
    # The summary IS the session context after compression.
    _files_read.pop(session_id, None)
    _current_turns.pop(session_id, None)
    # Note: _added_files intentionally NOT cleared — user's /add persists across /compress


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


def get_file_first_read_turn(path: str, session_id: str = "default") -> Optional[int]:
    """Return the turn number when path was first read, or None."""
    return _files_read.get(session_id, {}).get(path)


# ── Turn counter API ──────────────────────────────────────────────────────

def increment_turn(session_id: str = "default") -> int:
    """Increment and return the current turn number for a session."""
    _current_turns[session_id] = _current_turns.get(session_id, 0) + 1
    return _current_turns[session_id]


def get_current_turn(session_id: str = "default") -> int:
    return _current_turns.get(session_id, 0)


# ── Session event buffer (GAP-3: memory distillation) ─────────────────────

def append_session_event(event: dict) -> None:
    """Append a tool/action event to the current session's event buffer.

    Called from _execute_tool() and Python-native tool closures in agent.py.
    The accumulated events are consumed by MemoryDistiller at session end (GAP-3).
    """
    _session_events.append(event)


def drain_session_events() -> list[dict]:
    """Return all accumulated session events and clear the buffer.

    Called once per session end by _distill_session() in handlers/agent.py.
    """
    events = list(_session_events)
    _session_events.clear()
    return events


def get_config() -> dict:
    """
    Load and cache ~/.evocli/config.toml.
    Returns the full config dict so handlers can pass it to EvoCLIAgent.
    Falls back to empty dict if config is not found or unreadable.
    """
    global _config
    if _config is None:
        with _init_lock:
            if _config is None:
                try:
                    import tomllib
                    from pathlib import Path
                    cfg_path = Path.home() / ".evocli" / "config.toml"
                    if cfg_path.exists():
                        with open(cfg_path, "rb") as f:
                            _config = tomllib.load(f)
                    else:
                        _config = {}
                except Exception as e:
                    import logging
                    logging.getLogger("evocli.state").debug("Config load failed: %s", e)
                    _config = {}
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


def get_memory():
    global _memory
    if _memory is None:
        with _init_lock:
            if _memory is None:  # double-check after acquiring lock
                from evocli_soul.memory_client import EvoCLIMemory
                _memory = EvoCLIMemory()
    return _memory


def get_memory_if_ready():
    """Return the memory singleton **without blocking**.

    Returns the already-initialised EvoCLIMemory instance if it's ready,
    or ``None`` if initialisation hasn't finished yet (e.g. still loading
    the fastembed model in the background pre-warm task).

    Reading a module-level reference is atomic under the GIL, so no lock
    is needed for this check-only path.
    """
    return _memory


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
    _bridge = _memory = _skill_engine = _llm_client = _agent = _orchestrator = _config = None
    _active_subagents.clear()
    _session_events.clear()
    _conversation_histories.clear()
    _context_caches.clear()
    _anchored_summaries.clear()
    _files_read.clear()
    _current_turns.clear()
    _added_files.clear()

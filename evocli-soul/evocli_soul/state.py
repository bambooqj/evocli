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

# threading.Lock is intentionally used (not asyncio.Lock) because:
# 1. asyncio runs in a single thread — no true preemption between tasks.
# 2. The double-checked locking pattern prevents redundant initialization.
# 3. The critical section only runs pure-Python imports/construction (no awaitable I/O).
# 4. If initialization were ever made async (e.g., await LanceDB.connect()), this MUST
#    be changed to asyncio.Lock to avoid event-loop blocking.
_init_lock = threading.Lock()


def append_session_event(event: dict) -> None:
    """Append a tool/action event to the current session's event buffer.
    
    Called from _execute_tool() and Python-native tool closures in agent.py.
    The accumulated events are consumed by MemoryDistiller at session end (GAP-3).
    """
    _session_events.append(event)


def drain_session_events() -> list[dict]:
    """Return all accumulated session events and clear the buffer.
    
    Called once per session end by _distill_session() in handlers/agent.py.
    Not thread-safe for concurrent drain calls, but session end is always
    triggered from a single async handler so this is safe in practice.
    """
    events = list(_session_events)
    _session_events.clear()
    return events
# 1. asyncio runs in a single thread — no true preemption between tasks.
# 2. The double-checked locking pattern prevents redundant initialization.
# 3. The critical section only runs pure-Python imports/construction (no awaitable I/O).
# 4. If initialization were ever made async (e.g., await LanceDB.connect()), this MUST
#    be changed to asyncio.Lock to avoid event-loop blocking.
_init_lock = threading.Lock()


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

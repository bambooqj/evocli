"""
agent_tools.py — Pydantic-AI tool registration orchestrator

Atomized from a 1322-line file into 3 focused sub-modules:
  - agent_tools_fs.py    : FS + git + memory + shell_run/grep (~474 lines)
  - agent_tools_code.py  : Code + task + MCP tools (~541 lines)
  - agent_tools_shell.py : Shell convenience + analysis tools (~297 lines)

Also loads user-defined tools from ~/.evocli/tools/*.py and {project}/.evocli/tools/*.py
"""
from __future__ import annotations


def register_tools(
    agent,
    bridge,
    sid: str,
    sc_fn,
    call_handler_fn,
    config=None,
    memory=None,
) -> None:
    """
    Register all pydantic-ai tools on the given agent instance.

    Delegates to 3 focused sub-modules, then loads user-defined tools.
    """
    import json as _json

    _sc           = sc_fn
    _call_handler = call_handler_fn
    _sid          = sid

    _kwargs = dict(
        _sc=_sc, _call_handler=_call_handler, _sid=_sid, _json=_json,
        bridge=bridge, config=config, memory=memory,
    )

    from evocli_soul.agent_tools_fs    import register as _reg_fs
    from evocli_soul.agent_tools_code  import register as _reg_code
    from evocli_soul.agent_tools_shell import register as _reg_shell

    _reg_fs(agent,    **_kwargs)
    _reg_code(agent,  **_kwargs)
    _reg_shell(agent, **_kwargs)

    # ── User-defined tools (L3-1: no code change needed) ─────────────────────
    # Loads ~/.evocli/tools/*.py and {project}/.evocli/tools/*.py automatically.
    # Users drop a .py file with a register() function and restart evocli.
    try:
        from evocli_soul.user_tool_loader import load_user_tools
        from evocli_soul.state import get_session_root
        load_user_tools(
            agent, bridge, _sid, _sc, _call_handler,
            config=config, memory=memory,
            project_dir=get_session_root(),
        )
    except Exception as _ute:
        import logging as _log_ute
        _log_ute.getLogger("evocli.agent.tools").warning(
            "user_tool_loader failed (custom tools not loaded): %s", _ute
        )


"""agent_context.py - Context building and injection mixin
Extracted from agent_execution.py.
"""
from __future__ import annotations
import logging
log = logging.getLogger('evocli.agent.context')


class AgentContextMixin:
    """Mixin: _build_context and _inject_context for EvoCLIAgent."""

    async def _build_context(self, user_input: str, context_params: dict | None = None,
                              history: list[dict] | None = None,
                              session_id: str = "default") -> dict:
        """Build context via ContextEngine."""
        try:
            from evocli_soul.context_engine import ContextEngine
            ctx_engine = ContextEngine(self.bridge)
    
            # Inject /add-ed files into context params (Aider /add pattern)
            try:
                import evocli_soul.state as _st_add
                added_files = _st_add.get_added_files(session_id)
                if added_files:
                    # Build a "@file:path" prefix for each added file so context_engine
                    # picks them up as @mention providers (highest priority context)
                    add_prefix = " ".join(f"@file:{f}" for f in added_files[:5])  # max 5
                    enriched_goal = f"{add_prefix}\n\n{user_input}"
                    log.debug("_build_context: injecting %d /add-ed files", len(added_files))
                else:
                    enriched_goal = user_input
            except Exception:
                enriched_goal = user_input
    
            # Load anchored summary (preserved across /compress — this is the compact session memory)
            try:
                import evocli_soul.state as _st_anchor
                anchored_summary = _st_anchor.get_anchored_summary(session_id)
            except Exception:
                anchored_summary = ""
    
            # Extract model_id/provider_id from config for per-model env block in system prompt
            _llm_cfg    = (self.config or {}).get("llm", {}) if self.config else {}
            _model_id   = _llm_cfg.get("tiers", {}).get("fast", "")
            _provider   = _llm_cfg.get("provider", "")
    
            return await ctx_engine.build({
                "goal":             enriched_goal,
                "project_id":       (context_params or {}).get("project_id", "."),
                "current_file":     (context_params or {}).get("current_file"),
                "git_diff":         (context_params or {}).get("git_diff", ""),
                "history":          history or [],
                "active_tools":     list(self._TOOL_TO_RPC.keys()),
                "session_id":       session_id,
                "anchored_summary": anchored_summary,
                "read_only":        self.read_only,
                "model_id":         _model_id,    # for per-model prompt specialization
                "provider_id":      _provider,    # for env block
            })
        except Exception as e:
            log.debug("Context build failed: %s", e)
            return {}
    
    async def _inject_context(self, user_input: str, ctx: dict) -> str:
        """Prefix user input with context from ContextEngine + per-turn environment details.
    
        注入策略（避免双重注入）：
        - env_details（每轮动态：已读文件列表、OS、目录）→ 注入 user message 开头（Cline pattern）
        - user_context（当前文件内容、git diff、对话历史）→ 注入 user message
        - system_prompt（约束、记忆、RepoMap）→ 由各 LLM 路径的 system message 处理
        不在此处注入 system_prompt 以避免 LiteLLM 路径的 token 双重消耗。
        """
        parts = []
    
        # ── Per-turn environment details (Cline pattern) ──────────────────────
        # Injected at the top of every user message so the AI always knows:
        # - which files have been read this session (for prior-read awareness)
        # - current working directory (prevents path confusion)
        # - session tool usage stats
        try:
            import os as _os_env
            import platform as _plat_env
            import time as _time_env
            from evocli_soul.state import get_session_root as _gsr_env, get_added_files as _gaf_env
            _cwd_env = _gsr_env()
            _is_git  = _os_env.path.exists(_os_env.path.join(_cwd_env, ".git"))
            _pinned  = _gaf_env(self._session_id)
    
            env_lines = [
                "<environment_details>",
                f"Working directory: {_cwd_env}",
                f"Is git repo: {'yes' if _is_git else 'no'}",
                f"Platform: {_plat_env.system().lower()}",
                f"Time: {_time_env.strftime('%Y-%m-%d %H:%M')}",
            ]
            if _pinned:
                env_lines.append(f"Pinned files (/add): {', '.join(_pinned[:5])}")
            # Files read this session (for prior-read awareness)
            try:
                from evocli_soul.state import get_files_read_this_session as _gfrs
                _read_files = _gfrs(self._session_id)
                if _read_files:
                    env_lines.append(f"Files read this session: {', '.join(list(_read_files)[:8])}")
            except Exception:
                pass
            # Gemini scratchpad: compact tool sequence breadcrumb
            try:
                from evocli_soul.state import get_scratchpad_summary as _gss
                _scratch = _gss(self._session_id)
                if _scratch:
                    env_lines.append(f"Tool history (this session): {_scratch}")
            except Exception:
                pass
            env_lines.append("</environment_details>")
            parts.append("\n".join(env_lines))
        except Exception:
            pass  # Never let env details break context injection
    
        # ── user_context（当前文件内容、git diff、对话历史）────────────────────
        if ctx.get("user_context"):
            parts.append(ctx["user_context"])
    
        parts.append(user_input)
        return "\n\n---\n".join(p for p in parts if p.strip()) if len(parts) > 1 else user_input
    

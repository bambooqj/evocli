"""
EvoCLI Soul — 入口

唯一职责：启动 JSON-RPC 服务循环并注册所有 handler。
业务逻辑全部在 handlers/ 和各功能模块中。
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading

# ── CRITICAL: Fix evocli_soul package path BEFORE any submodule imports ───────
#
# Problem (Windows-specific): `python -m evocli_soul.main` causes Python to
# initialize the `evocli_soul` package BEFORE main.py code runs. If a stale
# editable install in ~/.evocli/venv/site-packages has evocli_soul registered
# (pointing to an old location without llm_client.py etc.), then:
#   sys.modules['evocli_soul'].__path__ → old/wrong location
#   sys.modules['evocli_soul.llm_client'] → ModuleNotFoundError
#
# Also: pathlib.resolve() on Windows adds \\?\ prefix which doesn't match
# the raw path in sys.path, so 'if path not in sys.path' always inserts BUT
# the \\?\ version may not be on the __path__ correctly.
#
# Fix: After startup, FORCE evocli_soul.__path__ to point to the exact
# directory containing THIS file (the dist's evocli_soul/). This overrides
# any stale editable install pointer cached in sys.modules.
import pathlib as _pathlib

# The directory containing this file IS evocli_soul/
_this_pkg_dir = _pathlib.Path(__file__).parent
# Use str() directly (no resolve() to avoid \\?\ prefix on Windows)
_this_pkg_str = str(_this_pkg_dir)
# Parent is the evocli-soul/ directory that should be on sys.path
_soul_dir_str = str(_this_pkg_dir.parent)

# 1. Ensure the soul root is first in sys.path (both \\?\ and normal form)
for _p in [_soul_dir_str, _soul_dir_str.lstrip('\\\\?\\')]:
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)

# 2. Force-update the evocli_soul package's __path__ to point HERE.
#    This fixes the case where sys.modules['evocli_soul'] was already set
#    by the venv's editable install pointing to a stale location.
if 'evocli_soul' in sys.modules:
    _pkg = sys.modules['evocli_soul']
    if hasattr(_pkg, '__path__'):
        _pkg.__path__ = [_this_pkg_str]
    if hasattr(_pkg, '__file__') and _pkg.__file__:
        _pkg.__file__ = str(_this_pkg_dir / '__init__.py')
    del _pkg

del _pathlib, _this_pkg_dir, _this_pkg_str, _soul_dir_str
# Clean up loop variable if it exists
try: del _p
except NameError: pass

# Windows GBK 修复：强制 stdout/stderr/stdin UTF-8（subprocess pipe 需要显式包装）
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", line_buffering=True)
    # stdin 也需要包装：未包装时 Windows pipe 模式下 readline() 可能等到 EOF 才返回
    sys.stdin  = io.TextIOWrapper(sys.stdin.buffer,  encoding="utf-8", line_buffering=True)


def _setup_logging() -> None:
    from evocli_soul.soul_logging import setup_logging  # 修复：logging.py 已重命名为 soul_logging.py
    debug = "--debug" in sys.argv
    setup_logging(debug)


def _build_router():
    import evocli_soul.state as state
    from evocli_soul.router import Router
    from evocli_soul.handlers import register_all

    router = Router(state)
    register_all(router)
    return router


async def _serve(router) -> None:
    """主循环：读 stdin → dispatch → 写 stdout。"""
    from evocli_soul.rpc import emit_event

    await emit_event("soul_ready")
    log = logging.getLogger("evocli.soul")
    log.info("EvoCLI Soul ready")

    # 跨平台 stdin 读取（线程 + asyncio.Queue）
    # asyncio.get_running_loop() is the correct API in Python 3.10+.
    # get_event_loop() is deprecated and raises DeprecationWarning in 3.10+,
    # RuntimeError in some 3.12+ contexts when there is no current event loop.
    loop = asyncio.get_running_loop()
    q: asyncio.Queue[str] = asyncio.Queue()

    def _read_stdin() -> None:
        try:
            for line in sys.stdin:
                line = line.strip()
                if line:
                    loop.call_soon_threadsafe(q.put_nowait, line)
        except Exception as e:
            log.warning("stdin closed: %s", e)
        finally:
            # Guard against calling into a closed loop during shutdown.
            if not loop.is_closed():
                loop.call_soon_threadsafe(q.put_nowait, "")  # EOF sentinel

    threading.Thread(target=_read_stdin, daemon=True).start()

    # ── Graceful SIGTERM handler ──────────────────────────────────────────────
    # When Rust host sends SIGTERM (e.g., on Ctrl+C), flush pending work and exit
    # cleanly so background distillation tasks complete.
    import signal as _signal

    def _handle_sigterm(signum, frame) -> None:
        log = logging.getLogger("evocli.soul")
        log.info("SIGTERM received — Soul shutting down gracefully")
        # Push EOF sentinel into the queue to break the main loop
        if not loop.is_closed():
            loop.call_soon_threadsafe(q.put_nowait, "")

    try:
        _signal.signal(_signal.SIGTERM, _handle_sigterm)
    except (OSError, ValueError):
        pass  # Not available on all platforms (Windows signal constraints)

    while True:
        line = await q.get()
        if not line:
            log.info("stdin EOF — Soul exiting")
            break
        try:
            msg     = json.loads(line)
            req_id  = msg.get("id", "")
            method  = msg.get("method", "")
            params  = msg.get("params", {})

            # 响应消息（Rust → Soul 的 tool call 响应）
            # result/error 字段存在 = 这是对 bridge.call() 的回复
            if "result" in msg or "error" in msg:
                import evocli_soul.state as state
                bridge = state.get_bridge()
                await bridge.handle_response(msg)
                continue

            # Attach error callback so handler exceptions are logged, not silently swallowed.
            # Without this, a crash in router.dispatch() is only visible as a GC warning.
            task = asyncio.create_task(router.dispatch(req_id, method, params))
            task.add_done_callback(_log_task_exception)
        except json.JSONDecodeError as e:
            log.warning("JSON parse error: %s | %r", e, line[:120])
        except Exception as e:
            log.exception("Main loop error: %s", e)


def _log_task_exception(task: asyncio.Task) -> None:
    """Background task error callback — logs unhandled exceptions."""
    if not task.cancelled() and task.exception():
        log = logging.getLogger("evocli.soul")
        log.error("Background task %s failed: %s", task.get_name(), task.exception())


async def main() -> None:
    # ── CRITICAL: Freeze project root BEFORE any other initialization ──────────
    # Captures the CWD at process start as an immutable session constant.
    # All tools must call state.get_session_root() instead of os.getcwd() so that
    # shell commands or subprocess cwd changes don't cause directory drift.
    # Continue.dev pattern: workspaceFolders[0].fsPath captured once at init.
    import os as _os_early
    import evocli_soul.state as _state_early
    _state_early.set_session_root(_os_early.getcwd())

    _setup_logging()

    # 显式初始化 bridge 单例（在任何 background task 启动前，消除竞态）
    import evocli_soul.state as _state
    from evocli_soul.host_bridge import HostBridge
    _state.set_bridge(HostBridge())

    router = _build_router()

    log = logging.getLogger("evocli.soul")

    # 模型 Context Window 预热（后台，不阻塞启动）
    async def _warmup_model_context():
        """在后台探测模型 context window，解决新模型名称问题。
        通过 bridge.call("config.get") 获取非敏感配置（api_key 已脱敏），
        API key 从 keyring 或环境变量读取（config.get 不包含 key 以防泄露）。
        """
        try:
            import os
            import evocli_soul.state as _state
            bridge = _state.get_bridge()
            cfg = await bridge.call("config.get", {})
            llm = cfg.get("llm", {}) if isinstance(cfg, dict) else {}
            base_url = llm.get("base_url")
            tiers    = llm.get("tiers", {})
            models   = list({tiers.get("fast"), tiers.get("smart")} - {None})
            # api_key: config.get redacts it for security. Read from env or keyring instead.
            api_key = (
                os.environ.get("OPENAI_API_KEY")
                or os.environ.get("ANTHROPIC_API_KEY")
                or os.environ.get("DEEPSEEK_API_KEY")
                or os.environ.get("GROQ_API_KEY")
            )
            if not api_key:
                try:
                    import keyring as _kr
                    for _provider in ("openai", "anthropic", "deepseek", "groq"):
                        _v = _kr.get_password("evocli", _provider)
                        if _v:
                            api_key = _v
                            break
                except Exception:
                    pass
            if models and base_url and api_key:
                from evocli_soul.model_context import warmup
                await warmup(models, base_url, api_key)
        except Exception as e:
            log.debug("Model context warmup skipped: %s", e)

    # P2-3: 启动 Daemon Workers（memory_distill 每5分钟，evolution_scan 每10分钟）
    async def _start_daemons():
        try:
            from evocli_soul.multi_agent import get_daemon_manager
            import evocli_soul.state as state
            mgr = get_daemon_manager(state.get_bridge())
            # DaemonWorkerManager.start() is currently a no-op:
            # background scheduling has moved to evolution/scheduler.py (asyncio-based).
            # Kept here as a hook for future daemon worker registration.
            # mgr.start()  # NOOP — safe to remove after v3 daemon design is finalized
            log.info("Daemon manager initialized (start() deferred — scheduling via evolution/scheduler.py)")
        except Exception as e:
            log.warning("Daemon workers init failed (non-fatal): %s", e)

    # Fix: 启动 Evolution 后台调度器（每10分钟读取真实事件，检测重复模式，生成 Skill 草案）
    # 根因：start_background_scheduler() 从未被调用，Evolution 系统完全处于休眠状态。
    async def _start_evolution_scheduler():
        try:
            import evocli_soul.state as _st
            from evocli_soul.evolution import EvolutionEngine
            engine = EvolutionEngine(_st.get_bridge())
            engine.start_background_scheduler()
            log.info("Evolution background scheduler started (reads events.db every 10m)")
        except Exception as e:
            log.warning("Evolution scheduler init failed (non-fatal): %s", e)

    task1 = asyncio.create_task(_start_daemons())   # 包含 memory_distill(5m) + evolution_scan(10m)
    task1.add_done_callback(_log_task_exception)
    task_evo = asyncio.create_task(_start_evolution_scheduler())   # Fix: Evolution 调度器
    task_evo.add_done_callback(_log_task_exception)
    task2 = asyncio.create_task(_warmup_model_context())  # 后台预热 context window
    task2.add_done_callback(_log_task_exception)

    # 后台预热 memory + fastembed 模型（100MB+，首次加载需要 30–120 秒）
    # 目的：让首次请求不等模型加载，直接用 None 降级跳过 constraints；
    # 预热完成后所有后续请求自动获得完整的向量记忆功能。
    async def _prewarm_memory():
        from evocli_soul.rpc import emit_event as _emit
        import time
        import evocli_soul.state as _st
        loop = asyncio.get_running_loop()

        # If memory is already cached in this process, skip silently — no user-facing
        # messages needed since there's nothing to wait for.
        already_ready = _st.get_memory_if_ready() is not None
        if already_ready:
            log.info("Memory already initialised — skipping pre-warm notification")
            return

        # Announce loading so the user knows something is happening.
        await _emit("soul_status", {
            "status":  "loading",
            "message": "Loading memory & embedding models… "
                       "Responses work now, but memory context will activate shortly.",
        })

        t0 = time.monotonic()
        try:
            await loop.run_in_executor(None, _st.get_memory)
            elapsed = time.monotonic() - t0
            # Always confirm completion — the user saw "⏳ Loading…" and needs to know
            # it finished (or failed).  No elapsed-time gate: even a 0.1s cache hit
            # deserves a "✅ ready" so the loading message doesn't hang unresolved.
            await _emit("soul_status", {
                "status":  "ready",
                "message": f"Memory ready ✓  (loaded in {elapsed:.1f}s)",
            })
            log.info("Memory/embeddings pre-warm complete (%.1fs)", elapsed)
        except Exception as e:
            log.error("Memory pre-warm failed: %s", e, exc_info=True)
            await _emit("soul_status", {
                "status":  "error",
                "message": (
                    f"Memory unavailable: {e}. "
                    "Responses work without memory context. "
                    "Run `evocli doctor` to diagnose."
                ),
            })

    task4 = asyncio.create_task(_prewarm_memory())
    task4.add_done_callback(_log_task_exception)

    # Load MCP tools if any servers are registered
    try:
        from evocli_soul.handlers.mcp_bridge import initialize_mcp_tools
        task3 = asyncio.create_task(initialize_mcp_tools())
        task3.add_done_callback(_log_task_exception)
    except Exception as e:
        log.warning("MCP tools init failed: %s", e)

    await _serve(router)


if __name__ == "__main__":
    asyncio.run(main())

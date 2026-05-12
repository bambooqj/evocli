"""
Watch mode handler — Aider --watch 文件监控 RPC

注册: watch.start / watch.stop
"""
from __future__ import annotations
import logging

log = logging.getLogger("evocli.handlers.watch")

_watcher = None


def register(router) -> None:
    router.add("watch.start", handle_watch_start)
    router.add("watch.stop",  handle_watch_stop)


async def handle_watch_start(req_id: str, params: dict, send, state) -> None:
    """Start Aider-style watch mode (// AI! triggers)."""
    global _watcher
    root = params.get("root", ".")
    try:
        from evocli_soul.watch_mode import WatchMode, _WATCHFILES_AVAILABLE
        if not _WATCHFILES_AVAILABLE:
            await send.response(req_id, {"ok": False, "reason": "watchfiles not installed"})
            return
        # Stop any existing watcher before creating a new one.
        # Without this, calling watch.start twice creates an orphaned background task
        # that continues consuming resources and watching files indefinitely.
        if _watcher is not None:
            _watcher.stop()
            log.debug("watch.start: stopped existing watcher before restarting")
        _watcher = WatchMode(root=root)
        _watcher.start_background()
        await send.response(req_id, {"ok": True, "root": root, "status": "watching"})
    except Exception as e:
        log.exception("watch.start failed")
        await send.error(req_id, -32603, str(e))


async def handle_watch_stop(req_id: str, params: dict, send, state) -> None:
    """Stop watch mode."""
    global _watcher
    if _watcher:
        _watcher.stop()
        _watcher = None
    await send.response(req_id, {"ok": True, "status": "stopped"})

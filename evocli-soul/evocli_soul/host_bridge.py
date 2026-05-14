"""HostBridge — Python Soul 与 Rust Host 的 JSON-RPC 通信层"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from typing import Any


class HostBridge:
    """
    所有工具调用都经过这里。Soul 无法绕过 HostBridge 直接操作系统。

    协议：
      请求  → stdout（Rust Host 读取）
      响应  ← stdin（Rust Host 写入）
      事件  → stdout（单向通知）
    """

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def call(self, tool: str, args: dict[str, Any], timeout: float = 0.0) -> Any:
        """
        向 Rust Host 发起工具调用请求，等待响应。
        timeout=0 表示使用 config_defaults["system.bridge_timeout_s"]。
        """
        if timeout <= 0:
            try:
                from evocli_soul.config_defaults import cfg_float
                timeout = cfg_float("system.bridge_timeout_s")
            except Exception:
                timeout = 30.0
        req_id = str(uuid.uuid4())
        request = {
            "id":     req_id,
            "method": "tool.call",
            "params": {"tool": tool, "args": args},
        }
        # asyncio.get_running_loop() is correct inside an async function (Python 3.7+).
        # get_event_loop() is deprecated in 3.10+ and may raise RuntimeError in 3.12+.
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future

        await self._send(request)
        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            # 超时清理：移除 pending 条目，防止 Rust 迟到响应触发 InvalidStateError
            self._pending.pop(req_id, None)
            raise

    async def call_long(self, tool: str, args: dict[str, Any]) -> Any:
        """发起长时间运行的工具调用请求（超时 300 秒）。"""
        return await self.call(tool, args, timeout=300.0)

    # ── Interactive prompts ────────────────────────────────────────────────

    async def prompt_choice(
        self,
        title: str,
        options: list[dict[str, str]],
        allow_custom: bool = False,
        timeout: float = 120.0,
    ) -> dict[str, str]:
        """Show the user a numbered choice modal and wait for their selection.

        Args:
            title:        Question / context shown at the top of the modal.
            options:      List of {"id": str, "label": str} dicts.
            allow_custom: If True, user can type a custom text answer.
            timeout:      Seconds to wait before auto-cancelling (default 120).

        Returns one of:
            {"type": "selected", "id": "<option id>"}
            {"type": "custom",   "text": "<user text>"}  # only if allow_custom
            {"type": "cancelled"}                         # user pressed Esc or timed out

        Example::
            result = await bridge.prompt_choice(
                title="How should I fix the type error?",
                options=[
                    {"id": "string",  "label": "Change type to String"},
                    {"id": "tostr",   "label": "Add .to_string() call"},
                    {"id": "skip",    "label": "Skip this error"},
                ],
                allow_custom=True,
            )
            if result["type"] == "selected":
                fix_id = result["id"]
            elif result["type"] == "custom":
                custom_text = result["text"]
        """
        return await self.call(
            "prompt.choice",
            {"title": title, "options": options, "allow_custom": allow_custom},
            timeout=timeout,
        )

    async def emit_event(self, event: dict[str, Any]) -> None:
        """发送单向事件通知（无需响应）"""
        notification = {"method": "event.emit", "params": event}
        await self._send(notification)

    async def handle_response(self, response: dict[str, Any]) -> None:
        """Rust Host 返回响应时调用"""
        req_id = response.get("id")
        if req_id and req_id in self._pending:
            future = self._pending.pop(req_id)
            # 防御性检查：超时后 future 可能已被 cancel，跳过设置结果
            if future.done():
                return
            if response.get("error"):
                future.set_exception(RpcError(response["error"]))
            else:
                future.set_result(response.get("result"))

    async def _send(self, msg: dict[str, Any]) -> None:
        line = json.dumps(msg, ensure_ascii=False) + "\n"
        sys.stdout.write(line)
        sys.stdout.flush()


class RpcError(Exception):
    def __init__(self, error: dict[str, Any]) -> None:
        self.code    = error.get("code", -1)
        self.message = error.get("message", "unknown error")
        super().__init__(f"[{self.code}] {self.message}")

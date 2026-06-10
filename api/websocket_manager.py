from __future__ import annotations

import asyncio
import json

from fastapi import WebSocket


class WebSocketManager:
    """WebSocket 连接管理器，支持同步广播（供执行线程回调时使用）。"""

    def __init__(self):
        self.connections: set[WebSocket] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self.connections.discard(websocket)

    async def send(self, websocket: WebSocket, message: dict) -> None:
        await websocket.send_json(message)

    async def broadcast(self, message: dict) -> None:
        stale: list[WebSocket] = []
        for websocket in self.connections:
            try:
                await websocket.send_json(message)
            except Exception:
                stale.append(websocket)
        for websocket in stale:
            self.disconnect(websocket)

    def broadcast_sync(self, event_type: str, payload: dict) -> None:
        """同步广播事件 — 供同步执行流（ChatRunner/ReportBuilder）调用。

        将事件包装为 ``{"type": event_type, "content": payload}`` 并通过
        当前运行的 asyncio event loop 异步广播到所有已连接的 WebSocket。
        """
        message = {"type": event_type, "content": payload}
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.broadcast(message))
            return
        except RuntimeError:
            pass
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self.broadcast(message), self._loop)

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
        try:
            await websocket.send_json(message)
        except RuntimeError:
            # WebSocket already closed (e.g. client disconnected mid-execution)
            self.disconnect(websocket)

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
        """同步广播事件 — 供任意线程调用，自动适配 asyncio 跨线程调度。"""
        message = {"type": event_type, "content": payload}
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.broadcast(message))
            return
        except RuntimeError:
            pass
        # 非 async 线程 → 用绑定的 loop 或全局 event loop
        loop = self._loop or asyncio.get_event_loop()
        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(self.broadcast(message), loop)

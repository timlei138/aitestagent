from __future__ import annotations

import asyncio
import json
import logging

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class WebSocketManager:
    """WebSocket 连接管理器，支持同步广播（供执行线程回调时使用）。"""

    def __init__(self):
        self.connections: set[WebSocket] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        logger.info(
            "[stop-debug] ws_manager.bind_loop loop_id=%s running=%s",
            id(loop),
            loop.is_running(),
        )

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.connections.add(websocket)
        logger.info(
            "[stop-debug] ws_manager.connect total=%d",
            len(self.connections),
        )

    def disconnect(self, websocket: WebSocket) -> None:
        self.connections.discard(websocket)
        logger.info(
            "[stop-debug] ws_manager.disconnect total=%d",
            len(self.connections),
        )

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
        # DEBUG(stop 排查)：每次广播都打，看到底走哪条路径、有没有真的送到客户端
        try:
            running_loop = asyncio.get_running_loop()
            running_loop_id = id(running_loop)
            running_loop_running = running_loop.is_running()
        except RuntimeError:
            running_loop_id = None
            running_loop_running = None
        bound_loop_id = id(self._loop) if self._loop else None
        bound_loop_running = self._loop.is_running() if self._loop else None
        logger.info(
            "[stop-debug] broadcast_sync type=%s running_loop=%s(running=%s) bound_loop=%s(running=%s) connections=%d",
            event_type,
            running_loop_id,
            running_loop_running,
            bound_loop_id,
            bound_loop_running,
            len(self.connections),
        )
        # 路径 1：当前线程已有 running loop → 直接 create_task（最常见，asyncio.to_thread 内不会走这条）
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.broadcast(message))
            return
        except RuntimeError:
            pass
        # 路径 2：跨线程（worker thread / 同步执行路径），用绑定的 loop 或全局 loop
        loop = self._loop or asyncio.get_event_loop()
        if loop and loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(self.broadcast(message), loop)
            logger.info(
                "[stop-debug] broadcast_sync -> run_coroutine_threadsafe type=%s fut=%s",
                event_type,
                fut,
            )
        else:
            logger.warning(
                "[stop-debug] broadcast_sync FAILED: no running loop bound. type=%s bound_loop=%s",
                event_type,
                self._loop,
            )

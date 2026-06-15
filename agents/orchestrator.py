from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, AsyncIterator, Callable

from langgraph.types import Command

from config import TestConfig
from agents.graph import build_graph
from agents.state import TestState

logger = logging.getLogger(__name__)


class TestOrchestrator:
    """测试编排器 — 对外唯一入口。

    封装 LangGraph StateGraph 的生命周期：创建 → 执行/流式 → 中断 → 恢复。
    """

    def __init__(self, config: TestConfig):
        self.config = config
        self.graph = build_graph(config)
        self._event_callback: Callable[[str, dict[str, Any]], None] | None = None
        self._state_cache: dict[str, dict[str, Any]] = {}
        self._resume_in_flight: set[str] = set()  # 防重：同一 thread_id 不能同时 resume 两次

    def set_event_callback(self, callback: Callable[[str, dict[str, Any]], None]) -> None:
        self._event_callback = callback

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._event_callback:
            try:
                self._event_callback(event_type, payload)
            except Exception:
                pass

    # ── 同步执行 ──

    def start(
        self, user_request: str, app_package: str = "", app_name: str = "",
        thread_id: str = "",
    ) -> dict[str, Any]:
        """启动测试执行（同步）。设备未连接时直接返回错误。"""
        # 设备连接前置检查
        try:
            from tools import get_tool_context
            ctx = get_tool_context()
            if getattr(ctx, "device", None) is None:
                msg = "Android 设备未连接，请检查 USB/ADB 连接后重试"
                self._emit("error", {"message": msg})
                return {
                    "thread_id": thread_id or "", "status": "device_offline",
                    "mode": "run", "conclusion": msg, "steps": [],
                }
        except Exception:
            pass

        if not thread_id:
            thread_id = f"test-{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        initial_state: TestState = {
            "user_request": user_request, "app_package": app_package,
            "app_name": app_name, "test_plan": [], "current_step_index": 0,
            "last_action": "", "last_observation": "", "step_history": [],
            "anomalies": [], "reviewer_decision": "", "human_question": "",
            "retry_count": 0, "conclusion": "", "report_path": "", "status": "",
            "pending_identities": [],
        }

        config_ctx = {"configurable": {"thread_id": thread_id, "test_config": self.config}}

        self._emit("status", f"开始执行: {user_request}")

        try:
            final_state = self.graph.invoke(initial_state, config_ctx)
            self._state_cache[thread_id] = final_state

            # invoke() 不会抛 GraphInterrupt — 通过 get_state 检测中断
            snapshot = self.graph.get_state(config_ctx)
            if snapshot and snapshot.interrupts:
                # 图被 interrupt() 暂停，提取中断数据
                interrupt_data = snapshot.interrupts[0].value if snapshot.interrupts else {}
                if isinstance(interrupt_data, dict):
                    self._emit(interrupt_data.get("type", "need_human_approval"), interrupt_data)
                    return {
                        "thread_id": thread_id, "status": "need_human", "mode": "run",
                        "interrupt": interrupt_data, "conclusion": "", "steps": [],
                    }
            return self._build_result(thread_id, final_state)
        except Exception as exc:
            return self._handle_exception(thread_id, exc)

    # ── 流式执行 (LangGraph astream_events) ──

    async def start_stream(
        self, user_request: str, app_package: str = "", app_name: str = "",
        thread_id: str = "",
    ) -> AsyncIterator[dict[str, Any]]:
        """流式执行测试 — 通过 astream_events 实时推送每个事件。"""
        if not thread_id:
            thread_id = f"test-{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        initial_state: TestState = {
            "user_request": user_request, "app_package": app_package,
            "app_name": app_name, "test_plan": [], "current_step_index": 0,
            "last_action": "", "last_observation": "", "step_history": [],
            "anomalies": [], "reviewer_decision": "", "human_question": "",
            "retry_count": 0, "conclusion": "", "report_path": "", "status": "",
            "pending_identities": [],
        }

        config_ctx = {"configurable": {"thread_id": thread_id, "test_config": self.config}}

        yield {"type": "status", "content": f"开始执行: {user_request}"}

        try:
            async for event in self.graph.astream_events(initial_state, config_ctx, version="v2"):
                kind = event.get("event", "")

                if kind == "on_chat_model_stream":
                    chunk = event.get("data", {}).get("chunk")
                    if chunk and hasattr(chunk, "content") and chunk.content:
                        yield {"type": "stream_token", "content": str(chunk.content)}

                elif kind == "on_tool_start":
                    yield {
                        "type": "tool_start",
                        "content": {"name": event.get("name", ""), "input": event.get("data", {}).get("input", {})},
                    }

                elif kind == "on_tool_end":
                    yield {
                        "type": "tool_end",
                        "content": {"name": event.get("name", ""), "output": str(event.get("data", {}).get("output", ""))[:500]},
                    }

                elif kind == "on_custom_event":
                    evt_type, payload = event.get("data", ("custom", {}))
                    yield {"type": evt_type, "content": payload}

                elif kind == "on_chain_end" and "planner" in str(event.get("name", "")):
                    output = event.get("data", {}).get("output", {})
                    if isinstance(output, dict) and output.get("test_plan"):
                        yield {"type": "plan_ready", "content": {"steps": len(output["test_plan"])}}

            # 获取最终状态
            final_state = self.graph.get_state(config_ctx)
            if final_state and final_state.values:
                yield {
                    "type": "result",
                    "content": {
                        "status": final_state.values.get("status", "success"),
                        "conclusion": final_state.values.get("conclusion", ""),
                    },
                }

        except Exception as exc:
            exc_msg = str(exc)
            if "GraphInterrupt" in type(exc).__name__:
                interrupt_info = _extract_interrupt_info(exc)
                itype = interrupt_info.get("type", "need_human_approval")
                yield {"type": itype, "content": interrupt_info}
            else:
                logger.exception("Stream execution failed")
                yield {"type": "error", "content": exc_msg}

    # ── 恢复执行 ──

    def resume(self, thread_id: str, decision: Any) -> dict[str, Any]:
        """中断后恢复执行。decision: str (人工确认) 或 dict (计划编辑)。"""
        if thread_id in self._resume_in_flight:
            logger.warning("Resume already in flight for thread %s, ignoring duplicate", thread_id)
            return self._build_result(thread_id, self._state_cache.get(thread_id, {}))
        self._resume_in_flight.add(thread_id)
        config_ctx = {"configurable": {"thread_id": thread_id, "test_config": self.config}}
        self._emit("status",
                   f"恢复执行: {decision if isinstance(decision, str) else decision.get('action', 'confirm')}")

        resume_value = decision
        # 如果是计划编辑结果，包装为标准格式
        if isinstance(decision, dict):
            plan = decision.get("plan")
            action = decision.get("action", "confirm")
            if action == "cancel":
                resume_value = "cancel"
            elif plan and isinstance(plan, list):
                resume_value = {"plan": plan, "action": "confirm"}
            else:
                resume_value = "confirm"

        try:
            final_state = self.graph.invoke(Command(resume=resume_value), config_ctx)
            self._state_cache[thread_id] = final_state
            return self._build_result(thread_id, final_state)
        except Exception as exc:
            return self._handle_exception(thread_id, exc)
        finally:
            self._resume_in_flight.discard(thread_id)

    async def resume_stream(self, thread_id: str, decision: Any) -> AsyncIterator[dict[str, Any]]:
        """流式恢复执行。decision: str 或 dict。"""
        if thread_id in self._resume_in_flight:
            logger.warning("Resume already in flight for thread %s, ignoring duplicate", thread_id)
            yield {"type": "error", "content": "执行已在恢复中，请勿重复操作"}
            return
        self._resume_in_flight.add(thread_id)
        config_ctx = {"configurable": {"thread_id": thread_id, "test_config": self.config}}
        label = decision if isinstance(decision, str) else decision.get("action", "confirm")
        yield {"type": "status", "content": f"恢复执行: {label}"}

        resume_value = decision
        if isinstance(decision, dict):
            plan = decision.get("plan")
            action = decision.get("action", "confirm")
            if action == "cancel":
                resume_value = "cancel"
            elif plan and isinstance(plan, list):
                resume_value = {"plan": plan, "action": "confirm"}
            else:
                resume_value = "confirm"

        try:
            try:
                async for event in self.graph.astream_events(Command(resume=resume_value), config_ctx, version="v2"):
                    kind = event.get("event", "")
                    if kind == "on_chat_model_stream":
                        chunk = event.get("data", {}).get("chunk")
                        if chunk and hasattr(chunk, "content") and chunk.content:
                            yield {"type": "stream_token", "content": str(chunk.content)}
                    elif kind == "on_tool_start":
                        yield {"type": "tool_start", "content": {"name": event.get("name", "")}}
                    elif kind == "on_tool_end":
                        yield {"type": "tool_end", "content": {"name": event.get("name", "")}}

                final_state = self.graph.get_state(config_ctx)
                if final_state and final_state.values:
                    yield {"type": "result", "content": {
                        "status": final_state.values.get("status", "success"),
                        "conclusion": final_state.values.get("conclusion", ""),
                    }}
            except Exception as exc:
                yield {"type": "error", "content": str(exc)}
        finally:
            self._resume_in_flight.discard(thread_id)

    # ── 内部 ──

    def _build_result(self, thread_id: str, state: dict) -> dict[str, Any]:
        result = {
            "thread_id": thread_id,
            "status": state.get("status", "success"),
            "mode": "run",
            "conclusion": state.get("conclusion", ""),
            "steps": state.get("step_history", []),
            "test_plan": state.get("test_plan", []),
            "pending_identities": state.get("pending_identities", []),
        }
        return result

    def _handle_exception(self, thread_id: str, exc: Exception) -> dict[str, Any]:
        exc_msg = str(exc)
        logger.info("Graph exception: %s", exc_msg)
        if "GraphInterrupt" in type(exc).__name__:
            info = _extract_interrupt_info(exc)
            self._emit(info.get("type", "need_human_approval"), info)
            return {"thread_id": thread_id, "status": "need_human", "mode": "run",
                    "interrupt": info, "conclusion": "", "steps": []}
        self._emit("result", {"status": "error", "conclusion": exc_msg})
        return {"thread_id": thread_id, "status": "error", "mode": "run",
                "conclusion": exc_msg, "steps": []}


def _extract_interrupt_info(exc: Exception) -> dict[str, Any]:
    """从 GraphInterrupt 异常中提取中断数据。"""
    try:
        # LangGraph GraphInterrupt: args[0] 是 Interrupt 对象，.value 是实际数据
        from langgraph.errors import GraphInterrupt
        if isinstance(exc, GraphInterrupt):
            interrupts = getattr(exc, "args", [])
            if interrupts:
                first = interrupts[0]
                # Interrupt 对象有 .value 属性
                value = getattr(first, "value", None)
                if isinstance(value, dict):
                    return value
                # 也可能是裸 dict
                if isinstance(first, dict):
                    return first
    except Exception:
        pass
    return {"type": "need_human_approval", "question": str(exc), "options": ["允许", "跳过", "终止"]}

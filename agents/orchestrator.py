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
        from tools import get_tool_context
        ctx = get_tool_context()
        if ctx and hasattr(ctx, '_verifications'):
            ctx._verifications.clear()
        if ctx and hasattr(ctx, '_tool_calls_log'):
            ctx._tool_calls_log.clear()
        if getattr(ctx, "device", None) is None:
            msg = "Android 设备未连接，请检查 USB/ADB 连接后重试"
            self._emit("error", {"message": msg})
            return {
                "thread_id": thread_id or "", "status": "device_offline",
                "execution_status": "device_offline", "test_verdict": "inconclusive",
                "verification_results": [],
                "mode": "run", "conclusion": msg, "steps": [],
            }

        if not thread_id:
            thread_id = f"test-{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        initial_state: TestState = {
            "user_request": user_request, "app_package": app_package,
            "app_name": app_name, "goal_description": {},
            "step_history": [], "messages": [],
            "conclusion": "", "status": "",
        }

        config_ctx = {"configurable": {"thread_id": thread_id, "test_config": self.config}}

        self._emit("status", f"开始执行: {user_request}")

        # 为本次运行创建独立日志
        from config import start_run_log
        run_log = start_run_log(thread_id)

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
                        "execution_status": "", "test_verdict": "inconclusive",
                        "verification_results": [],
                    }
            return self._build_result(thread_id, final_state)
        except Exception as exc:
            return self._handle_exception(thread_id, exc)
        finally:
            run_log["cleanup"]()

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
            "app_name": app_name, "goal_description": {},
            "step_history": [], "messages": [],
            "conclusion": "", "status": "",
        }

        config_ctx = {"configurable": {"thread_id": thread_id, "test_config": self.config}}

        yield {"type": "status", "content": f"开始执行: {user_request}"}

        # 清空上次测试的验证结果
        from tools import get_tool_context as _gtc
        _sctx = _gtc()
        if _sctx and hasattr(_sctx, '_verifications'):
            _sctx._verifications.clear()
        if _sctx and hasattr(_sctx, '_tool_calls_log'):
            _sctx._tool_calls_log.clear()

        from config import start_run_log
        run_log = start_run_log(thread_id)
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
                    goal = output.get("goal_description", {})
                    if isinstance(goal, dict) and goal:
                        yield {"type": "plan_ready", "content": {"goal": goal.get("goal", ""), "pages": goal.get("target_pages", [])}}

            # 获取最终状态
            final_state = self.graph.get_state(config_ctx)
            if final_state and final_state.values:
                yield {
                    "type": "result",
                    "content": {
                        "status": final_state.values.get("status", "success"),
                        "execution_status": final_state.values.get("execution_status", ""),
                        "test_verdict": final_state.values.get("test_verdict", ""),
                        "verification_results": final_state.values.get("verification_results", []),
                        "conclusion": final_state.values.get("conclusion", ""),
                        "steps": final_state.values.get("step_history", []),
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
        finally:
            run_log["cleanup"]()

    # ── 恢复执行 ──

    def resume(self, thread_id: str, decision: Any) -> dict[str, Any]:
        """中断后恢复执行。decision: str (人工确认) 或 dict (计划编辑)。"""
        if thread_id in self._resume_in_flight:
            logger.warning("Resume already in flight for thread %s, ignoring duplicate", thread_id)
            return self._build_result(thread_id, self._state_cache.get(thread_id, {}))
        self._resume_in_flight.add(thread_id)
        # 清空上次验证结果
        from tools import get_tool_context as _gtc
        _sctx = _gtc()
        if _sctx and hasattr(_sctx, '_verifications'):
            _sctx._verifications.clear()
        if _sctx and hasattr(_sctx, '_tool_calls_log'):
            _sctx._tool_calls_log.clear()
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

        from config import start_run_log
        run_log = start_run_log(f"{thread_id}_resume")
        try:
            final_state = self.graph.invoke(Command(resume=resume_value), config_ctx)
            self._state_cache[thread_id] = final_state
            return self._build_result(thread_id, final_state)
        except Exception as exc:
            return self._handle_exception(thread_id, exc)
        finally:
            run_log["cleanup"]()
            self._resume_in_flight.discard(thread_id)

    async def resume_stream(self, thread_id: str, decision: Any) -> AsyncIterator[dict[str, Any]]:
        """流式恢复执行。decision: str 或 dict。"""
        if thread_id in self._resume_in_flight:
            logger.warning("Resume already in flight for thread %s, ignoring duplicate", thread_id)
            yield {"type": "error", "content": "执行已在恢复中，请勿重复操作"}
            return
        self._resume_in_flight.add(thread_id)
        # 清空上次验证结果
        from tools import get_tool_context as _gtc
        _sctx = _gtc()
        if _sctx and hasattr(_sctx, '_verifications'):
            _sctx._verifications.clear()
        if _sctx and hasattr(_sctx, '_tool_calls_log'):
            _sctx._tool_calls_log.clear()
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

        from config import start_run_log
        run_log = start_run_log(f"{thread_id}_resume")
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
                        "execution_status": final_state.values.get("execution_status", ""),
                        "test_verdict": final_state.values.get("test_verdict", ""),
                        "verification_results": final_state.values.get("verification_results", []),
                        "conclusion": final_state.values.get("conclusion", ""),
                        "steps": final_state.values.get("step_history", []),
                    }}
            except Exception as exc:
                yield {"type": "error", "content": str(exc)}
        finally:
            run_log["cleanup"]()
            self._resume_in_flight.discard(thread_id)

    # ── 内部 ──

    def _build_result(self, thread_id: str, state: dict) -> dict[str, Any]:
        display_steps = _build_display_steps(state.get("step_history", []))
        result = {
            "thread_id": thread_id,
            "status": state.get("status", "success"),
            "execution_status": state.get("execution_status", ""),
            "test_verdict": state.get("test_verdict", ""),
            "verification_results": state.get("verification_results", []),
            "mode": "run",
            "conclusion": state.get("conclusion", ""),
            "steps": display_steps,
            "goal_description": state.get("goal_description", {}),
        }
        return result

    def _handle_exception(self, thread_id: str, exc: Exception) -> dict[str, Any]:
        exc_msg = str(exc)
        logger.info("Graph exception: %s", exc_msg)
        if "GraphInterrupt" in type(exc).__name__:
            info = _extract_interrupt_info(exc)
            self._emit(info.get("type", "need_human_approval"), info)
            return {"thread_id": thread_id, "status": "need_human", "mode": "run",
                    "interrupt": info, "conclusion": "", "steps": [],
                    "execution_status": "", "test_verdict": "", "verification_results": []}
        self._emit("result", {"status": "error", "conclusion": exc_msg})
        return {"thread_id": thread_id, "status": "error", "mode": "run",
                "conclusion": exc_msg, "steps": [],
                "execution_status": "error", "test_verdict": "inconclusive",
                "verification_results": []}


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


def _build_display_steps(history: list) -> list[dict]:
    """从 ToolContext._tool_calls_log 读取工具调用生成展示用细粒度步骤。
    不影响 graph 路由/去重逻辑，仅用于前端展示。"""
    from tools import get_tool_context
    ctx = get_tool_context()
    tool_calls_raw = getattr(ctx, '_tool_calls_log', []) if ctx else []

    # 收集工具调用（已过滤感知类）
    tool_steps = [{"action_type": t["name"], "target": t.get("target", "")} for t in tool_calls_raw]

    # 合并连续重复
    merged = []
    for ts in tool_steps:
        if merged and merged[-1]["action_type"] == ts["action_type"] and merged[-1]["target"] == ts["target"]:
            continue
        merged.append(ts)

    # 构建展示步骤
    result = []
    idx = 0
    for ts in merged:
        idx += 1
        result.append({
            "index": idx,
            "intent": f"{ts['action_type']}('{ts['target']}')" if ts["target"] else ts["action_type"],
            "action_type": ts["action_type"],
            "target": ts["target"],
            "page_from": "", "page_to": "", "duration_ms": 0,
            "status": "continue",
            "observation": "", "raw_observation": "",
            "screenshot_path": "", "anomaly": None,
        })

    # 追加原始 step_history 中的 Agent 结论步骤
    for s in history:
        idx += 1
        result.append({**s, "index": idx})

    return result if result else history

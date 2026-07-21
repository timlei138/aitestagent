from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime
from typing import Any, AsyncIterator, Callable

from langgraph.types import Command

from config import TestConfig
from agents.graph import build_graph
from agents.nodes import RunStopped
from agents.state import TestState

logger = logging.getLogger(__name__)


def _reset_run_scoped(ctx) -> None:
    """重置 run 级累加器：验证结果 / M4 确定性核实 / O1 token 统计。
    每次新执行前调用，避免跨 run 数据串扰。绝不抛异常。"""
    if not ctx:
        return
    try:
        if isinstance(getattr(ctx, "_verifications", None), list):
            ctx._verifications.clear()
        if isinstance(getattr(ctx, "_deterministic_checks", None), list):
            ctx._deterministic_checks.clear()
        tu = getattr(ctx, "_token_usage", None)
        if isinstance(tu, dict):
            for k in list(tu.keys()):
                tu[k] = 0
    except Exception:
        pass


class TestOrchestrator:
    """测试编排器 — 对外唯一入口。

    封装 LangGraph StateGraph 的生命周期：创建 → 执行/流式 → 中断 → 恢复。
    """

    def __init__(self, config: TestConfig):
        self.config = config
        self.graph = build_graph(config)
        self._event_callback: Callable[[str, dict[str, Any]], None] | None = None
        self._state_cache: dict[str, dict[str, Any]] = {}
        self._resume_in_flight: set[str] = (
            set()
        )  # 防重：同一 thread_id 不能同时 resume 两次
        self._log_file_paths: dict[str, str] = {}  # thread_id → log 文件路径
        # 用户手动停止：thread_id → threading.Event
        # 用 threading.Event 而非 asyncio.Event：start() 同步路径跑在
        # asyncio.to_thread 的工作线程上，Event.is_set() 是 O(1) 原子读。
        self._stop_flags: dict[str, threading.Event] = {}
        self._active_runs: dict[str, dict[str, Any]] = {}  # tid → {started_at, mode}

    def set_event_callback(
        self, callback: Callable[[str, dict[str, Any]], None]
    ) -> None:
        self._event_callback = callback

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        # DEBUG(stop 排查)：每个 emit 都打，方便看后端事件流
        logger.info(
            "[stop-debug] _emit type=%s tid=%s payload=%s",
            event_type,
            payload.get("thread_id", "-") if isinstance(payload, dict) else "-",
            str(payload)[:200],
        )
        if self._event_callback:
            try:
                self._event_callback(event_type, payload)
            except Exception as exc:
                logger.warning("[stop-debug] _emit callback raised: %s", exc)
                pass

    # ── 手动停止（用户主动中断） ──

    def _register_run(self, thread_id: str) -> threading.Event:
        """为一次 run 注册独立的 stop flag。返回 Event 供调用方挂到 ToolContext。"""
        ev = threading.Event()
        self._stop_flags[thread_id] = ev
        self._active_runs[thread_id] = {
            "started_at": datetime.now().isoformat(),
            "mode": "run",
        }
        logger.info("[stop-debug] _register_run tid=%s active_runs=%d", thread_id, len(self._active_runs))
        return ev

    def _cleanup_run(self, thread_id: str) -> None:
        """run 结束后清理 stop flag。绝不抛异常。

        立即清 _stop_flags / _active_runs——race 修复由
        `request_stop` 的 ctx._stop_event fallback 覆盖：
        resume 期间 ctx 上仍持有 Event 引用，request_stop 找不到 dict
        时会直接 set ctx._stop_event，下个 LLM 节点 stop 检查能命中。
        """
        self._stop_flags.pop(thread_id, None)
        self._active_runs.pop(thread_id, None)
        logger.info("[stop-debug] _cleanup_run tid=%s active_runs=%d", thread_id, len(self._active_runs))

    def _attach_stop_event(self, ctx, ev: threading.Event) -> None:
        """把 Event 挂到 ToolContext._stop_event，让节点快速检查。"""
        if ctx is None:
            return
        try:
            ctx._stop_event = ev
        except Exception:
            pass

    def request_stop(self, thread_id: str, reason: str = "user_requested") -> bool:
        """幂等：置位指定 thread 的停止标志。

        返回 True 表示确实置位了新信号；False 表示无活跃 run 或信号已置位。

        Race 修复：dict 找不到时（delayed_cleanup 已清掉），
        仍尝试 set ctx._stop_event — resume 期间 ctx 还持有 Event 引用，
        下一个 LLM 节点入口检查能命中。
        """
        logger.info(
            "[stop-debug] request_stop entry tid=%s reason=%s flags=%s",
            thread_id,
            reason,
            list(self._stop_flags.keys()),
        )
        ev = self._stop_flags.get(thread_id)
        if ev is None:
            # dict 里没了（grace 已被 daemon 清）— 但 ctx 上可能还挂着一个 Event
            # (resume 期间 ctx._stop_event 仍指向该 Event 的 python 引用)
            try:
                from tools import get_tool_context
                ctx = get_tool_context()
                ctx_ev = getattr(ctx, "_stop_event", None)
            except Exception:
                ctx_ev = None
            if ctx_ev is not None:
                logger.info(
                    "[stop-debug] request_stop: dict empty but ctx has Event for tid=%s, "
                    "set it directly",
                    thread_id,
                )
                if not ctx_ev.is_set():
                    ctx_ev.set()
                    self._emit(
                        "status",
                        {"type": "stopping", "thread_id": thread_id, "reason": reason},
                    )
                return True
            logger.warning(
                "[stop-debug] request_stop: NO active flag for tid=%s! "
                "Already cleaned up? Front-end may have sent stop too late. "
                "Known tids: %s",
                thread_id,
                list(self._stop_flags.keys()),
            )
            return False
        if not ev.is_set():
            ev.set()
            self._emit(
                "status",
                {"type": "stopping", "thread_id": thread_id, "reason": reason},
            )
            logger.info("[stop-debug] request_stop: STOP FLAG SET for tid=%s", thread_id)
        else:
            logger.info("[stop-debug] request_stop: flag already set, ignore. tid=%s", thread_id)
        return True

    # ── 同步执行 ──

    def start(
        self,
        user_request: str,
        app_package: str = "",
        app_name: str = "",
        thread_id: str = "",
    ) -> dict[str, Any]:
        """启动测试执行（同步）。设备未连接时直接返回错误。"""
        logger.info(
            "[stop-debug] start() entry thread_id_in=%r user_request=%r",
            thread_id,
            user_request[:60],
        )
        # 设备连接前置检查
        from tools import get_tool_context

        ctx = get_tool_context()
        _reset_run_scoped(ctx)
        if getattr(ctx, "device", None) is None:
            msg = "Android 设备未连接，请检查 USB/ADB 连接后重试"
            self._emit("error", {"message": msg})
            return {
                "thread_id": thread_id or "",
                "status": "device_offline",
                "execution_status": "device_offline",
                "test_verdict": "inconclusive",
                "verification_results": [],
                "mode": "run",
                "conclusion": msg,
                "steps": [],
            }

        if not thread_id:
            thread_id = f"test-{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        logger.info("[stop-debug] start() resolved thread_id=%s", thread_id)

        # 注册 stop flag + 挂到 ctx（让节点 / _run_agent 子图能检查）
        _stop_ev = self._register_run(thread_id)
        self._attach_stop_event(ctx, _stop_ev)

        # 广播 run_started（让前端能拿到 thread_id 来停止）
        logger.info("[stop-debug] start() emitting run_started tid=%s", thread_id)
        self._emit(
            "run_started",
            {"thread_id": thread_id, "started_at": datetime.now().isoformat()},
        )

        initial_state: TestState = {
            "user_request": user_request,
            "app_package": app_package,
            "app_name": app_name,
            "goal_description": {},
            "step_history": [],
            "messages": [],
            "conclusion": "",
            "status": "",
            "_stop_requested": False,
        }

        config_ctx = {
            "configurable": {"thread_id": thread_id, "test_config": self.config}
        }

        self._emit("status", f"开始执行: {user_request}")

        # 为本次运行创建独立日志
        from config import start_run_log

        run_log = start_run_log(thread_id)
        self._log_file_paths[thread_id] = run_log["langchain_file"]

        _stopped = False
        try:
            final_state = self.graph.invoke(initial_state, config_ctx)
            self._state_cache[thread_id] = final_state
            # 检测本次是否由 stop 触发：ctx._stop_event 已 set 或 state._stop_requested
            _stopped = bool(
                _stop_ev.is_set()
                or (
                    isinstance(final_state, dict)
                    and bool(final_state.get("_stop_requested", False))
                )
            )

            # invoke() 不会抛 GraphInterrupt — 通过 get_state 检测中断
            snapshot = self.graph.get_state(config_ctx)
            if snapshot and snapshot.interrupts:
                # 图被 interrupt() 暂停，提取中断数据
                interrupt_data = (
                    snapshot.interrupts[0].value if snapshot.interrupts else {}
                )
                if isinstance(interrupt_data, dict):
                    self._emit(
                        interrupt_data.get("type", "need_human_approval"),
                        interrupt_data,
                    )
                    return {
                        "thread_id": thread_id,
                        "status": "need_human",
                        "mode": "run",
                        "interrupt": interrupt_data,
                        "conclusion": "",
                        "steps": [],
                        "execution_status": "",
                        "test_verdict": "inconclusive",
                        "verification_results": [],
                    }
            result = self._build_result(thread_id, final_state)
            if _stopped:
                self._emit(
                    "run_stopped",
                    {"thread_id": thread_id, "reason": "user_requested"},
                )
            return result
        except RunStopped:
            # 兜底：子图内 stop 检查没命中，start 在最外层兜住
            self._emit(
                "run_stopped",
                {"thread_id": thread_id, "reason": "user_requested"},
            )
            return {
                "thread_id": thread_id,
                "status": "cancelled",
                "mode": "run",
                "conclusion": "ABORT: USER_STOPPED — 用户手动停止当前运行",
                "steps": [],
                "execution_status": "cancelled",
                "test_verdict": "inconclusive",
                "verification_results": [],
            }
        except Exception as exc:
            return self._handle_exception(thread_id, exc)
        finally:
            self._cleanup_run(thread_id)
            # 清 ctx 上的 event 引用，避免下个 run 看到上一次的 flag
            if ctx is not None:
                try:
                    ctx._stop_event = None
                except Exception:
                    pass
            run_log["cleanup"]()

    # ── 流式执行 (LangGraph astream_events) ──

    async def start_stream(
        self,
        user_request: str,
        app_package: str = "",
        app_name: str = "",
        thread_id: str = "",
    ) -> AsyncIterator[dict[str, Any]]:
        """流式执行测试 — 通过 astream_events 实时推送每个事件。"""
        if not thread_id:
            thread_id = f"test-{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        logger.info("[stop-debug] start_stream() entry tid=%s", thread_id)

        # 清空上次测试的验证结果 + 注册 stop flag
        from tools import get_tool_context as _gtc

        _sctx = _gtc()
        _reset_run_scoped(_sctx)
        _stop_ev = self._register_run(thread_id)
        self._attach_stop_event(_sctx, _stop_ev)

        initial_state: TestState = {
            "user_request": user_request,
            "app_package": app_package,
            "app_name": app_name,
            "goal_description": {},
            "step_history": [],
            "messages": [],
            "conclusion": "",
            "status": "",
            "_stop_requested": False,
        }

        config_ctx = {
            "configurable": {"thread_id": thread_id, "test_config": self.config}
        }

        logger.info("[stop-debug] start_stream() yielding run_started tid=%s", thread_id)
        yield {
            "type": "run_started",
            "content": {
                "thread_id": thread_id,
                "started_at": datetime.now().isoformat(),
            },
        }
        yield {"type": "status", "content": f"开始执行: {user_request}"}

        from config import start_run_log

        run_log = start_run_log(thread_id)
        self._log_file_paths[thread_id] = run_log["langchain_file"]
        try:
            async for event in self.graph.astream_events(
                initial_state, config_ctx, version="v2"
            ):
                # 关键：每个事件之间检查 stop，命中就主动退出流。
                # 节点入口 / _run_agent 子图会负责把 _stop_requested 写到 state，
                # 但 astream 还在跑——必须主动 cancel 当前 async generator。
                if _stop_ev.is_set():
                    logger.info(
                        "start_stream: stop detected mid-stream for %s", thread_id
                    )
                    self._emit(
                        "run_stopped",
                        {"thread_id": thread_id, "reason": "user_requested"},
                    )
                    yield {
                        "type": "run_stopped",
                        "content": {
                            "thread_id": thread_id,
                            "reason": "user_requested",
                        },
                    }
                    return

                kind = event.get("event", "")

                if kind == "on_chat_model_stream":
                    chunk = event.get("data", {}).get("chunk")
                    if chunk and hasattr(chunk, "content") and chunk.content:
                        yield {"type": "stream_token", "content": str(chunk.content)}

                elif kind == "on_tool_start":
                    yield {
                        "type": "tool_start",
                        "content": {
                            "name": event.get("name", ""),
                            "input": event.get("data", {}).get("input", {}),
                        },
                    }

                elif kind == "on_tool_end":
                    yield {
                        "type": "tool_end",
                        "content": {
                            "name": event.get("name", ""),
                            "output": str(event.get("data", {}).get("output", ""))[
                                :500
                            ],
                        },
                    }

                elif kind == "on_custom_event":
                    evt_type, payload = event.get("data", ("custom", {}))
                    yield {"type": evt_type, "content": payload}

                elif kind == "on_chain_end" and "planner" in str(event.get("name", "")):
                    output = event.get("data", {}).get("output", {})
                    goal = output.get("goal_description", {})
                    if isinstance(goal, dict) and goal:
                        yield {
                            "type": "plan_ready",
                            "content": {
                                "goal": goal.get("goal", ""),
                                "pages": goal.get("target_pages", []),
                            },
                        }

            # 获取最终状态
            final_state = self.graph.get_state(config_ctx)
            if final_state and final_state.values:
                yield {
                    "type": "result",
                    "content": {
                        "status": final_state.values.get("status", "success"),
                        "execution_status": final_state.values.get(
                            "execution_status", ""
                        ),
                        "test_verdict": final_state.values.get("test_verdict", ""),
                        "verification_results": final_state.values.get(
                            "verification_results", []
                        ),
                        "budget_violation_count": final_state.values.get(
                            "budget_violation_count", 0
                        ),
                        "llm_call_count": final_state.values.get("llm_call_count", 0),
                        "tool_call_400_count": final_state.values.get(
                            "tool_call_400_count", 0
                        ),
                        "tool_call_400_rate": final_state.values.get(
                            "tool_call_400_rate", 0.0
                        ),
                        "conclusion": final_state.values.get("conclusion", ""),
                        "steps": final_state.values.get("step_history", []),
                    },
                }
            if _stop_ev.is_set():
                self._emit(
                    "run_stopped",
                    {"thread_id": thread_id, "reason": "user_requested"},
                )
                yield {
                    "type": "run_stopped",
                    "content": {"thread_id": thread_id, "reason": "user_requested"},
                }

        except Exception as exc:
            exc_msg = str(exc)
            if "GraphInterrupt" in type(exc).__name__:
                interrupt_info = _extract_interrupt_info(exc)
                interrupt_info["thread_id"] = thread_id
                itype = interrupt_info.get("type", "need_human_approval")
                yield {"type": itype, "content": interrupt_info}
            else:
                logger.exception("Stream execution failed")
                yield {"type": "error", "content": exc_msg}
        finally:
            self._cleanup_run(thread_id)
            if _sctx is not None:
                try:
                    _sctx._stop_event = None
                except Exception:
                    pass
            run_log["cleanup"]()

    # ── 恢复执行 ──

    def resume(self, thread_id: str, decision: Any) -> dict[str, Any]:
        """中断后恢复执行。decision: str (人工确认) 或 dict (计划编辑)。"""
        logger.info("[stop-debug] resume() entry tid=%s", thread_id)
        if thread_id in self._resume_in_flight:
            logger.warning(
                "Resume already in flight for thread %s, ignoring duplicate", thread_id
            )
            return self._build_result(thread_id, self._state_cache.get(thread_id, {}))
        self._resume_in_flight.add(thread_id)
        # 清空上次验证结果
        from tools import get_tool_context as _gtc

        _sctx = _gtc()
        _reset_run_scoped(_sctx)
        # resume 同样支持 stop：复用同 thread_id 的 flag（若仍存在），
        # 不存在则新建一个，让用户能中断刚点完「确认」后继续运行的图。
        _stop_ev = self._stop_flags.get(thread_id) or self._register_run(thread_id)
        self._attach_stop_event(_sctx, _stop_ev)
        config_ctx = {
            "configurable": {"thread_id": thread_id, "test_config": self.config}
        }
        self._emit(
            "status",
            f"恢复执行: {decision if isinstance(decision, str) else decision.get('action', 'confirm')}",
        )

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

        from config import start_run_log, append_run_log

        _log_path = self._log_file_paths.get(thread_id, "")
        if _log_path:
            run_log = append_run_log(_log_path)
        else:
            run_log = start_run_log(thread_id)
            self._log_file_paths[thread_id] = run_log["langchain_file"]
        try:
            final_state = self.graph.invoke(Command(resume=resume_value), config_ctx)
            self._state_cache[thread_id] = final_state
            return self._build_result(thread_id, final_state)
        except Exception as exc:
            return self._handle_exception(thread_id, exc)
        finally:
            self._cleanup_run(thread_id)
            if _sctx is not None:
                try:
                    _sctx._stop_event = None
                except Exception:
                    pass
            run_log["cleanup"]()
            self._resume_in_flight.discard(thread_id)

    async def resume_stream(
        self, thread_id: str, decision: Any
    ) -> AsyncIterator[dict[str, Any]]:
        """流式恢复执行。decision: str 或 dict。"""
        logger.info("[stop-debug] resume_stream() entry tid=%s", thread_id)
        if thread_id in self._resume_in_flight:
            logger.warning(
                "Resume already in flight for thread %s, ignoring duplicate", thread_id
            )
            yield {"type": "error", "content": "执行已在恢复中，请勿重复操作"}
            return
        self._resume_in_flight.add(thread_id)
        # 清空上次验证结果
        from tools import get_tool_context as _gtc

        _sctx = _gtc()
        _reset_run_scoped(_sctx)
        _stop_ev = self._stop_flags.get(thread_id) or self._register_run(thread_id)
        self._attach_stop_event(_sctx, _stop_ev)
        config_ctx = {
            "configurable": {"thread_id": thread_id, "test_config": self.config}
        }
        label = (
            decision if isinstance(decision, str) else decision.get("action", "confirm")
        )
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

        from config import start_run_log, append_run_log

        _log_path = self._log_file_paths.get(thread_id, "")
        if _log_path:
            run_log = append_run_log(_log_path)
        else:
            run_log = start_run_log(thread_id)
            self._log_file_paths[thread_id] = run_log["langchain_file"]
        try:
            try:
                async for event in self.graph.astream_events(
                    Command(resume=resume_value), config_ctx, version="v2"
                ):
                    # resume 路径同样支持 stop
                    if _stop_ev.is_set():
                        logger.info(
                            "resume_stream: stop detected mid-stream for %s", thread_id
                        )
                        self._emit(
                            "run_stopped",
                            {"thread_id": thread_id, "reason": "user_requested"},
                        )
                        yield {
                            "type": "run_stopped",
                            "content": {
                                "thread_id": thread_id,
                                "reason": "user_requested",
                            },
                        }
                        return
                    kind = event.get("event", "")
                    if kind == "on_chat_model_stream":
                        chunk = event.get("data", {}).get("chunk")
                        if chunk and hasattr(chunk, "content") and chunk.content:
                            yield {
                                "type": "stream_token",
                                "content": str(chunk.content),
                            }
                    elif kind == "on_tool_start":
                        yield {
                            "type": "tool_start",
                            "content": {"name": event.get("name", "")},
                        }
                    elif kind == "on_tool_end":
                        yield {
                            "type": "tool_end",
                            "content": {"name": event.get("name", "")},
                        }

                final_state = self.graph.get_state(config_ctx)
                if final_state and final_state.values:
                    yield {
                        "type": "result",
                        "content": {
                            "status": final_state.values.get("status", "success"),
                            "execution_status": final_state.values.get(
                                "execution_status", ""
                            ),
                            "test_verdict": final_state.values.get("test_verdict", ""),
                            "verification_results": final_state.values.get(
                                "verification_results", []
                            ),
                            "budget_violation_count": final_state.values.get(
                                "budget_violation_count", 0
                            ),
                            "llm_call_count": final_state.values.get(
                                "llm_call_count", 0
                            ),
                            "tool_call_400_count": final_state.values.get(
                                "tool_call_400_count", 0
                            ),
                            "tool_call_400_rate": final_state.values.get(
                                "tool_call_400_rate", 0.0
                            ),
                            "conclusion": final_state.values.get("conclusion", ""),
                            "steps": final_state.values.get("step_history", []),
                        },
                    }
            except Exception as exc:
                yield {"type": "error", "content": str(exc)}
        finally:
            self._cleanup_run(thread_id)
            if _sctx is not None:
                try:
                    _sctx._stop_event = None
                except Exception:
                    pass
            run_log["cleanup"]()
            self._resume_in_flight.discard(thread_id)

    # ── 内部 ──

    def _build_result(self, thread_id: str, state: dict) -> dict[str, Any]:
        display_steps = _build_display_steps(
            state.get("step_history", []),
            state.get("_tool_calls_log", []),
        )
        conclusion = state.get("conclusion", "")
        # Command 更新不传播新增 key，从 conclusion 文本推断
        import re

        _m = re.search(
            r"^(?:#{1,3}\s*)?(DONE|ABORT)\s*[:：]",
            conclusion,
            re.IGNORECASE | re.MULTILINE,
        )
        is_done = bool(_m and _m.group(1).upper() == "DONE")
        is_abort = bool(_m and _m.group(1).upper() == "ABORT")
        exec_status = state.get("execution_status", "")
        verdict = state.get("test_verdict", "")
        if not exec_status:
            # 用户手动停止：reporter 写入的 execution_status 是 Command 新增 key，
            # LangGraph 不传播，必须从 _stop_requested 兜底识别。
            if bool(state.get("_stop_requested", False)):
                exec_status = "cancelled"
            elif is_done:
                exec_status = "completed"
            elif is_abort and ("MAX_TURNS" in conclusion or "MAX_TOOL_CALLS" in conclusion):
                exec_status = "exhausted"
            elif is_abort:
                exec_status = "completed"
        if not verdict:
            # 停止时不强制 passed（即便 DONE 也按 inconclusive 记，避免把「中途报告完成」误判为通过）
            if bool(state.get("_stop_requested", False)):
                verdict = "inconclusive"
            else:
                verdict = "passed" if is_done else "inconclusive"

        result = {
            "thread_id": thread_id,
            "status": state.get("status", "success"),
            "execution_status": exec_status,
            "test_verdict": verdict,
            "verification_results": state.get("verification_results", []),
            "budget_violation_count": state.get("budget_violation_count", 0),
            "llm_call_count": state.get("llm_call_count", 0),
            "tool_call_400_count": state.get("tool_call_400_count", 0),
            "tool_call_400_rate": state.get("tool_call_400_rate", 0.0),
            "mode": "run",
            "conclusion": conclusion,
            "steps": display_steps,
            "goal_description": state.get("goal_description", {}),
        }
        return result

    def _handle_exception(self, thread_id: str, exc: Exception) -> dict[str, Any]:
        exc_msg = str(exc)
        logger.info("Graph exception: %s", exc_msg)
        if "GraphInterrupt" in type(exc).__name__:
            info = _extract_interrupt_info(exc)
            info["thread_id"] = thread_id
            self._emit(info.get("type", "need_human_approval"), info)
            return {
                "thread_id": thread_id,
                "status": "need_human",
                "mode": "run",
                "interrupt": info,
                "conclusion": "",
                "steps": [],
                "execution_status": "",
                "test_verdict": "",
                "verification_results": [],
            }
        self._emit("result", {"status": "error", "conclusion": exc_msg})
        return {
            "thread_id": thread_id,
            "status": "error",
            "mode": "run",
            "conclusion": exc_msg,
            "steps": [],
            "execution_status": "error",
            "test_verdict": "inconclusive",
            "verification_results": [],
        }


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
    return {
        "type": "need_human_approval",
        "question": str(exc),
        "options": ["允许", "跳过", "终止"],
    }


def _build_display_steps(history: list, tool_calls_log: list) -> list[dict]:
    """从工具调用日志生成展示步骤。不再依赖全局 ToolContext。"""
    # 不再做去重 merge — 每次工具调用都可见
    result = []
    idx = 0
    for t in tool_calls_log:
        idx += 1
        result.append(
            {
                "index": idx,
                "intent": (
                    f"{t['name']}('{t.get('target', '')}')"
                    if t.get("target")
                    else t["name"]
                ),
                "intent_text": t.get("intent_text", ""),
                "action_type": t["name"],
                "target": t.get("target", ""),
                "page_from": "",
                "page_to": "",
                "duration_ms": 0,
                "status": "continue",
                "observation": t.get("observation", ""),
                "raw_observation": t.get("observation", ""),
                "screenshot_path": t.get("screenshot_path", ""),
                "anomaly": None,
            }
        )
    # 追加原始 step_history 中的 Agent 结论步骤
    for s in history:
        idx += 1
        result.append({**s, "index": idx})
    return result if result else history

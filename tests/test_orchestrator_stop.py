"""手动停止（user-requested stop）相关单测。

覆盖：
- request_stop 幂等性 / 未知 thread_id 静默
- 节点入口 stop 检查（_stop_or_continue 命中时收敛）
- reporter 检测到 stop 时强制 execution_status="cancelled"、
  test_verdict="inconclusive"，**不**触发 V1 全过归正
- orchestrator._register_run / _cleanup_run / ctx 挂载生命周期
- 多 thread_id 隔离
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from langgraph.types import Command

import tools
import tools.context
from agents import graph, nodes, orchestrator as orch_mod
from agents.orchestrator import TestOrchestrator
from config import TestConfig as AppTestConfig
from tools.context import ToolContext


# ── 工具：安装 fake ctx 到 get_tool_context ──


def _install_fake_ctx(monkeypatch, fake_ctx) -> None:
    """把 fake_ctx 同时注入 3 个 get_tool_context 引用点：

    - tools.context.get_tool_context（真函数定义点）
    - tools.get_tool_context（从 tools/__init__.py 再导出）
    - agents.nodes.get_tool_context（顶层 import 拷贝的引用）
    - agents.verification（延迟 import，从 tools 拿）
    """
    monkeypatch.setattr(tools.context, "get_tool_context", lambda: fake_ctx)
    monkeypatch.setattr(tools, "get_tool_context", lambda: fake_ctx)
    monkeypatch.setattr(nodes, "get_tool_context", lambda: fake_ctx)


# ── orchestrator.request_stop 基础行为 ──


def test_request_stop_sets_flag():
    """_register_run + request_stop 后，flag is_set() == True。"""
    orch = TestOrchestrator(AppTestConfig())
    tid = "test-stop-1"
    ev = orch._register_run(tid)
    assert not ev.is_set()
    ok = orch.request_stop(tid, reason="user_requested")
    assert ok is True
    assert ev.is_set()
    orch._cleanup_run(tid)
    assert tid not in orch._stop_flags


def test_request_stop_idempotent():
    """重复 stop 不抛异常，第二次仍返回 True（信号已置位视为成功）。"""
    orch = TestOrchestrator(AppTestConfig())
    tid = "test-stop-idem"
    orch._register_run(tid)
    a = orch.request_stop(tid)
    b = orch.request_stop(tid)
    assert a is True
    assert b is True  # 已置位，仍返回 True 表示"操作成功"
    orch._cleanup_run(tid)


def test_request_stop_unknown_thread_silent():
    """对未知 thread_id stop → 静默返回 False，不抛错。"""
    orch = TestOrchestrator(AppTestConfig())
    ok = orch.request_stop("never-registered")
    assert ok is False
    assert "never-registered" not in orch._stop_flags


def test_cleanup_run_removes_event_and_active():
    """_cleanup_run 必须同时清 stop_flags 和 active_runs。"""
    orch = TestOrchestrator(AppTestConfig())
    tid = "test-cleanup"
    orch._register_run(tid)
    assert tid in orch._stop_flags
    assert tid in orch._active_runs
    orch._cleanup_run(tid)
    assert tid not in orch._stop_flags
    assert tid not in orch._active_runs


def test_attach_stop_event_writes_to_ctx():
    """_attach_stop_event 把 Event 挂到 ctx._stop_event。"""
    orch = TestOrchestrator(AppTestConfig())
    ev = threading.Event()
    ctx = ToolContext(device=None, perceiver=None)
    assert ctx._stop_event is None
    orch._attach_stop_event(ctx, ev)
    assert ctx._stop_event is ev


def test_concurrent_runs_independent():
    """两个并发 run 各自持有独立 flag，stop 一个不影响另一个。"""
    orch = TestOrchestrator(AppTestConfig())
    tid_a = "test-A"
    tid_b = "test-B"
    ev_a = orch._register_run(tid_a)
    ev_b = orch._register_run(tid_b)
    assert ev_a is not ev_b

    orch.request_stop(tid_a)
    assert ev_a.is_set()
    assert not ev_b.is_set()  # B 完全不受影响

    orch._cleanup_run(tid_a)
    orch._cleanup_run(tid_b)


# ── 节点入口 stop 拦截 ──


def test_stop_or_continue_returns_command_when_flag_set(monkeypatch):
    """_stop_or_continue 命中 stop 时返回带 status="stopped" 的 Command。"""
    ctx = ToolContext(device=None, perceiver=None)
    ev = threading.Event()
    ev.set()
    orch_mod.TestOrchestrator._attach_stop_event = (
        lambda self, c, e: setattr(c, "_stop_event", e)
    )
    ctx._stop_event = ev  # 直接挂

    state = {"step_history": [], "messages": [], "conclusion": ""}
    cmd = nodes._stop_or_continue(state, ctx)
    assert isinstance(cmd, Command)
    assert cmd.update["status"] == "stopped"
    assert cmd.update["_stop_requested"] is True
    assert "USER_STOPPED" in cmd.update["conclusion"]
    assert len(cmd.update["step_history"]) == 1
    assert cmd.update["step_history"][0]["action_type"] == "user_stop"


def test_stop_or_continue_returns_none_when_no_flag():
    """未命中 stop 时返回 None，节点继续走原逻辑。"""
    ctx = ToolContext(device=None, perceiver=None)
    ctx._stop_event = None
    state = {"step_history": [], "messages": []}
    assert nodes._stop_or_continue(state, ctx) is None


def test_stop_or_continue_tolerates_missing_ctx():
    """ctx 为 None 时返回 None，不抛错。"""
    state = {"step_history": []}
    assert nodes._stop_or_continue(state, None) is None


# ── reporter 取消判定（V1 全过归正不能覆盖 stop）──


def test_reporter_stop_overrides_v1_passed_to_completed(monkeypatch):
    """即使全部验证项通过，stop 仍强制 execution_status="cancelled"。"""
    fake_ctx = SimpleNamespace(
        _verifications=[
            {"item": "v1", "result": "passed", "detail": "", "screenshot": ""},
            {"item": "v2", "result": "passed", "detail": "", "screenshot": ""},
        ],
        _verification_key_map={},
        _verification_items_by_key={},
    )
    ev = threading.Event()
    ev.set()
    fake_ctx._stop_event = ev
    _install_fake_ctx(monkeypatch, fake_ctx)

    state = {
        "status": "stopped",
        "conclusion": "ABORT: USER_STOPPED — 用户手动停止当前运行",
        "goal_description": {"verification": ["v1", "v2"]},
        "step_history": [{"index": 1}],
        "messages": [],
        "budget_violation_count": 0,
        "_stop_requested": True,
    }
    cmd = graph.reporter_node(
        state, {"configurable": {"test_config": AppTestConfig(write_run_trace=False)}}
    )
    # 核心断言：stop 优先于 V1 全过归正
    assert cmd.update["execution_status"] == "cancelled"
    assert cmd.update["test_verdict"] == "inconclusive"


def test_reporter_stop_marks_inconclusive_even_if_partial(monkeypatch):
    """stop 时即便只有部分验证项通过，也标 cancelled + inconclusive。"""
    fake_ctx = SimpleNamespace(
        _verifications=[
            {"item": "v1", "result": "passed", "detail": "", "screenshot": ""},
        ],
        _verification_key_map={},
        _verification_items_by_key={},
    )
    ev = threading.Event()
    ev.set()
    fake_ctx._stop_event = ev
    _install_fake_ctx(monkeypatch, fake_ctx)

    state = {
        "status": "stopped",
        "conclusion": "ABORT: USER_STOPPED",
        "goal_description": {"verification": ["v1", "v2"]},
        "step_history": [{"index": 1}],
        "messages": [],
        "budget_violation_count": 0,
        "_stop_requested": True,
    }
    cmd = graph.reporter_node(
        state, {"configurable": {"test_config": AppTestConfig(write_run_trace=False)}}
    )
    assert cmd.update["execution_status"] == "cancelled"
    assert cmd.update["test_verdict"] == "inconclusive"


def test_reporter_normal_v1_passed_to_completed_still_works(monkeypatch):
    """无 stop 信号时，V1 全过归正仍正常工作（回归保护）。"""
    fake_ctx = SimpleNamespace(
        _verifications=[
            {"item": "v1", "result": "passed", "detail": "", "screenshot": ""},
            {"item": "v2", "result": "passed", "detail": "", "screenshot": ""},
        ],
        _verification_key_map={},
        _verification_items_by_key={},
    )
    fake_ctx._stop_event = None
    _install_fake_ctx(monkeypatch, fake_ctx)

    state = {
        "status": "fail",
        "conclusion": "ABORT: MAX_TURNS_EXHAUSTED",
        "goal_description": {"verification": ["v1", "v2"]},
        "step_history": [{"index": i} for i in range(6)],
        "messages": [],
        "budget_violation_count": 0,
    }
    cmd = graph.reporter_node(
        state, {"configurable": {"test_config": AppTestConfig(write_run_trace=False)}}
    )
    # 走原 V1 路径：exhausted + 全过 → completed
    assert cmd.update["execution_status"] == "completed"
    assert cmd.update["test_verdict"] == "passed"


# ── llm_runtime USER_STOPPED 分支 ──


def test_check_stop_helper():
    """_check_stop 永远不抛异常。"""
    from agents.nodes import _check_stop

    assert _check_stop(None) is False
    ctx = ToolContext(device=None, perceiver=None)
    assert _check_stop(ctx) is False  # 没 _stop_event
    ctx._stop_event = None
    assert _check_stop(ctx) is False
    ev = threading.Event()
    ctx._stop_event = ev
    assert _check_stop(ctx) is False
    ev.set()
    assert _check_stop(ctx) is True


def test_stop_or_continue_no_goto_when_not_hit():
    """未命中 stop 时必须返回 None（不污染 routing）——节点继续走原逻辑。"""
    ctx = ToolContext(device=None, perceiver=None)
    ctx._stop_event = None
    state = {"step_history": [{"index": 1}], "messages": []}
    assert nodes._stop_or_continue(state, ctx) is None


def test_stop_command_has_goto_reporter():
    """命中 stop 时 Command 必须有 goto='reporter'，否则会走错路由（详见 review §P0）。"""
    from langgraph.types import Command

    ctx = ToolContext(device=None, perceiver=None)
    ev = threading.Event()
    ev.set()
    ctx._stop_event = ev
    state = {"step_history": [], "messages": []}
    cmd = nodes._stop_or_continue(state, ctx)
    assert isinstance(cmd, Command)
    # Command.goto 是单值（节点名）或 list；这里必须是字符串 "reporter"
    goto = cmd.goto
    if isinstance(goto, list):
        assert "reporter" in goto
    else:
        assert goto == "reporter"


def test_run_completes_normally_without_stop():
    """不命中 stop 时，节点入口返回 None，图按既有 routing 正常推进。

    验证 stop 设施**不**污染正常路径：
    - ctx._stop_event 为 None → _stop_or_continue 返回 None
    - ctx._stop_event 是未 set 的 Event → 同样返回 None
    """
    from agents.nodes import _stop_or_continue

    state = {"step_history": [], "messages": []}

    # Case 1: ctx._stop_event 为 None
    ctx1 = ToolContext(device=None, perceiver=None)
    assert ctx1._stop_event is None
    assert _stop_or_continue(state, ctx1) is None

    # Case 2: ctx._stop_event 是未 set 的 Event
    ctx2 = ToolContext(device=None, perceiver=None)
    ev = threading.Event()
    ctx2._stop_event = ev
    assert not ev.is_set()
    assert _stop_or_continue(state, ctx2) is None

    # Case 3: _stop_or_continue 不写任何 state（纯净返回）
    ctx3 = ToolContext(device=None, perceiver=None)
    state_with_history = {"step_history": [{"index": 1}, {"index": 2}], "messages": []}
    assert _stop_or_continue(state_with_history, ctx3) is None
    # state 引用应未被修改
    assert len(state_with_history["step_history"]) == 2


def test_route_after_agent_treats_stopped_as_terminal():
    """route_after_agent 必须把 'stopped' 视作终止态（防御 _stop_or_continue 的 goto 被忽略）。"""
    # 全过 + status=stopped → reporter
    state = {
        "status": "stopped",
        "conclusion": "ABORT: USER_STOPPED",
        "goal_description": {"target_pages": ["p1"], "verification": ["v1"]},
        "step_history": [{"index": i} for i in range(2)],
    }
    assert graph.route_after_agent(state) == "reporter"
    # 不全过 + status=stopped + 未到 max → 仍走 reporter（不走 agent 死循环）
    state_partial = {
        "status": "stopped",
        "conclusion": "ABORT: USER_STOPPED",
        "goal_description": {"target_pages": ["p1"], "verification": ["v1"]},
        "step_history": [{"index": 1}],
    }
    assert graph.route_after_agent(state_partial) == "reporter"


def test_plan_review_node_stops_when_flag_set(monkeypatch):
    """plan_review_node 入口必须检查 stop，命中时直接 return Command(goto='reporter')，
    不调 interrupt()——否则会弹出计划确认对话框，违背用户停止意图。

    验证逻辑：ctx._stop_event.set() 时 plan_review_node 在调 interrupt() 之前
    就已经 return Command——只要返回的 cmd 是 stop 收敛 Command、且 langgraph.types.interrupt
    计数器为 0，就证明了入口 stop 检查生效。
    """
    from langgraph.types import Command

    fake_ctx = ToolContext(device=None, perceiver=None)
    ev = threading.Event()
    ev.set()
    fake_ctx._stop_event = ev
    _install_fake_ctx(monkeypatch, fake_ctx)

    # 防御：如果 stop 检查失效、走到了 interrupt()，这里的 spy 会被触发
    import langgraph.types as _lg_types
    interrupt_called = {"v": False}

    def _spy_interrupt(*args, **kwargs):
        interrupt_called["v"] = True
        return {"action": "confirm"}

    monkeypatch.setattr(_lg_types, "interrupt", _spy_interrupt)

    cmd = nodes.plan_review_node(
        {"goal_description": {"goal": "test"}, "step_history": []},
        {"configurable": {"test_config": AppTestConfig(write_run_trace=False)}},
    )
    assert isinstance(cmd, Command)
    assert cmd.update["_stop_requested"] is True
    assert "USER_STOPPED" in cmd.update["conclusion"]
    # 关键断言：interrupt() **不**应被调用
    assert interrupt_called["v"] is False
    # goto 应指向 reporter
    goto = cmd.goto
    if isinstance(goto, list):
        assert "reporter" in goto
    else:
        assert goto == "reporter"

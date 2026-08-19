from __future__ import annotations

from types import SimpleNamespace

import pytest

from config import TestConfig as AppTestConfig
from agents import graph
from agents import nodes
from agents.llm_runtime import _call_retry_should_retry
import tools as tools_module


def test_calc_budget_values():
    budget = graph._calc_budget(  # type: ignore[attr-defined]
        {"target_pages": ["p1", "p2", "p3"], "verification": ["v1", "v2"]}
    )
    # 3 页 2 验证 → 48+60+36=144，max(7,10)=10，min(144,120)=120。
    assert budget["max_tool_calls_total"] == 144
    assert budget["max_agent_iterations"] == 10
    assert budget["max_turns_per_iteration"] == 120


def test_determine_execution_status_uses_dynamic_iteration_budget():
    # 验证“执行状态依据目标动态算出的迭代预算”判定为 exhausted。
    # 用当前预算公式算出阈值，避免与 _calc_budget 脱节（公式重构后
    # max_agent_iterations 多了 8 的下限，固定 6 条历史会误判为 error）。
    goal = {"target_pages": ["p1"], "verification": ["v1"]}
    budget = graph._calc_budget_from_state(  # type: ignore[attr-defined]
        {"goal_description": goal}
    )
    threshold = budget["max_agent_iterations"]
    state = {
        "status": "continue",
        "conclusion": "",
        "goal_description": goal,
        "step_history": [{"index": i} for i in range(threshold)],
    }
    assert graph._determine_execution_status(state) == "exhausted"  # type: ignore[attr-defined]


def test_determine_execution_status_marks_tool_budget_abort_as_exhausted():
    state = {
        "status": "fail",
        "conclusion": "ABORT: MAX_TOOL_CALLS_EXHAUSTED (60/59)",
        "goal_description": {"target_pages": ["p1"], "verification": ["v1"]},
        "step_history": [],
    }
    assert graph._determine_execution_status(state) == "exhausted"  # type: ignore[attr-defined]


def test_reporter_marks_exhausted_run_inconclusive_when_contract_is_unresolved(
    monkeypatch,
):
    fake_ctx = SimpleNamespace(
        _evidence_events=[
            {
                "verification_key": "v0",
                "clause_id": "v0.0",
                "channel": "ui_text",
                "status": "PASS",
            }
        ]
    )
    monkeypatch.setattr(nodes, "get_tool_context", lambda: fake_ctx)
    monkeypatch.setattr(tools_module, "get_tool_context", lambda: fake_ctx)
    state = {
        "status": "fail",
        "conclusion": "ABORT: MAX_TURNS_EXHAUSTED",
        "goal_description": {"verification": ["验证1"]},
        "verification_contract": {
            "status": "approved",
            "verifications": [
                {
                    "key": "v0",
                    "statement": "验证1",
                    "clauses": [{"id": "v0.0", "channels": ["ui_text"]}],
                }
            ],
        },
        "step_history": [{"index": i} for i in range(6)],
        "messages": [],
        "budget_violation_count": 0,
    }
    cmd = graph.reporter_node(
        state, {"configurable": {"test_config": AppTestConfig(write_run_trace=False)}}
    )
    assert cmd.update["execution_status"] == "exhausted"
    assert cmd.update["test_verdict"] == "inconclusive"
    assert len(cmd.update["verification_results"]) == 1
    assert cmd.update["verification_results"][0]["item"] == "验证1"


def test_route_after_evaluator_prefers_reporter_when_all_verifications_passed(monkeypatch):
    fake_ctx = SimpleNamespace(
        _evidence_events=[
            {
                "verification_key": "v0",
                "clause_id": "v0.0",
                "channel": "ui_text",
                "status": "PASS",
            }
        ]
    )
    monkeypatch.setattr(graph, "get_tool_context", lambda: fake_ctx)
    monkeypatch.setattr(nodes, "get_tool_context", lambda: fake_ctx)
    state = {
        "status": "continue",
        "goal_description": {"verification": ["验证1", "验证2"]},
        "step_history": [],
    }
    evaluation = nodes.evaluator_node(
        {
            **state,
            "verification_contract": {
                "status": "approved",
                "verifications": [
                    {
                        "key": "v0",
                        "clauses": [{"id": "v0.0", "channels": ["ui_text"]}],
                    }
                ],
            },
        },
        {},
    )
    assert evaluation.update["clause_state"]["verdict"] == "passed"
    assert (
        graph.route_after_evaluator(
            {**state, "clause_state": evaluation.update["clause_state"]}
        )
        == "reporter"
    )


def test_route_after_evaluator_routes_direct_to_agent_after_downgrade():
    state = {
        "status": "continue",
        "execution_mode": "direct",
        "_direct_downgrade_count": 1,
        "goal_description": {"verification": ["验证1"]},
        "verification_contract": {
            "status": "approved",
            "verifications": [
                {"key": "v0", "clauses": [{"id": "v0.0", "channels": ["ui_text"]}]}
            ],
        },
        "clause_state": {"verdict": "unknown"},
    }
    assert graph.route_after_evaluator(state) == "agent"


def test_should_downgrade_guided_on_timeout_and_not_found():
    for status in ("TIMEOUT", "NOT_FOUND"):
        state = {
            "execution_mode": "guided",
            "_guided_downgrade_count": 0,
            "step_history": [{"execution_mode": "guided"}],
            "_tool_calls_log": [{"status_code": status}],
        }
        assert graph._should_downgrade_guided(state) is True


def test_agent_node_accumulates_llm_call_metrics(monkeypatch):
    fake_ctx = SimpleNamespace(
        perceiver=None,
        knowledge_base=None,
        device=None,
    )
    monkeypatch.setattr(nodes, "get_tool_context", lambda: fake_ctx)
    monkeypatch.setattr(
        nodes, "_ensure_device_alive", lambda max_retries=2, wait_sec=5.0: True
    )
    monkeypatch.setattr(
        nodes,
        "_run_agent",
        lambda *args, **kwargs: (
            "CONTINUE",
            [],
            {
                "loop_detected": False,
                "loop_pattern": "",
                "loop_break_action": "",
                "llm_call_count": 5,
                "tool_call_400_count": 1,
            },
            "",
        ),
    )
    state = {
        "user_request": "u",
        "app_package": "pkg",
        "app_name": "app",
        "goal_description": {"verification": ["验证1"]},
        "step_history": [],
        "messages": [],
        "budget_violation_count": 0,
        "llm_call_count": 10,
        "tool_call_400_count": 2,
        "tool_call_400_rate": 0.2,
    }
    cmd = graph.agent_node(
        state, {"configurable": {"test_config": AppTestConfig(), "thread_id": "t"}}
    )
    # reducer 通道：节点只上报本次迭代的增量（delta），由通道累加。
    assert cmd.update["llm_call_count"] == 5
    assert cmd.update["tool_call_400_count"] == 1
    # 派生比率基于（累计 + 本次增量）估算：3/15 == 0.2
    assert cmd.update["tool_call_400_rate"] == 0.2


def test_call_retry_should_retry_triggers_on_error_callback():
    captured = []
    err = ValueError(
        "An assistant message with 'tool_calls' must be followed by tool messages"
    )
    should_retry = _call_retry_should_retry(
        err, on_error=lambda e: captured.append(str(e))
    )
    assert should_retry is True
    assert len(captured) == 1


def test_agent_node_routes_evaluator_terminal_passed_as_success(monkeypatch):
    """evaluator 提前返回 passed 时，_run_agent 应产出 DONE 结论，agent_node 路由到 reporter。"""
    fake_ctx = SimpleNamespace(
        perceiver=None,
        knowledge_base=None,
        device=None,
    )
    monkeypatch.setattr(nodes, "get_tool_context", lambda: fake_ctx)
    monkeypatch.setattr(
        nodes, "_ensure_device_alive", lambda max_retries=2, wait_sec=5.0: True
    )
    monkeypatch.setattr(
        nodes,
        "_run_agent",
        lambda *args, **kwargs: (
            "DONE: EVALUATOR_TERMINAL: passed",
            [],
            {
                "loop_detected": False,
                "loop_pattern": "EVALUATOR_TERMINAL: passed",
                "loop_break_action": "evaluator_terminal",
                "llm_call_count": 2,
                "tool_call_400_count": 0,
            },
            "passed",
        ),
    )
    state = {
        "user_request": "u",
        "app_package": "pkg",
        "app_name": "app",
        "goal_description": {"verification": ["验证1"]},
        "step_history": [],
        "messages": [],
        "budget_violation_count": 0,
    }
    cmd = graph.agent_node(
        state, {"configurable": {"test_config": AppTestConfig(), "thread_id": "t"}}
    )
    assert cmd.update["status"] == "success"
    assert cmd.update["conclusion"].startswith("DONE: EVALUATOR_TERMINAL: passed")
    assert cmd.update["_terminal_verdict"] == "passed"


def test_agent_node_routes_evaluator_terminal_failed_as_fail(monkeypatch):
    """evaluator 提前返回 failed 时，agent_node 应标记为 fail 并附带 ABORT 结论。"""
    fake_ctx = SimpleNamespace(
        perceiver=None,
        knowledge_base=None,
        device=None,
    )
    monkeypatch.setattr(nodes, "get_tool_context", lambda: fake_ctx)
    monkeypatch.setattr(
        nodes, "_ensure_device_alive", lambda max_retries=2, wait_sec=5.0: True
    )
    monkeypatch.setattr(
        nodes,
        "_run_agent",
        lambda *args, **kwargs: (
            "ABORT: EVALUATOR_TERMINAL: failed",
            [],
            {
                "loop_detected": False,
                "loop_pattern": "EVALUATOR_TERMINAL: failed",
                "loop_break_action": "evaluator_terminal",
                "llm_call_count": 2,
                "tool_call_400_count": 0,
            },
            "failed",
        ),
    )
    state = {
        "user_request": "u",
        "app_package": "pkg",
        "app_name": "app",
        "goal_description": {"verification": ["验证1"]},
        "step_history": [],
        "messages": [],
        "budget_violation_count": 0,
    }
    cmd = graph.agent_node(
        state, {"configurable": {"test_config": AppTestConfig(), "thread_id": "t"}}
    )
    assert cmd.update["status"] == "fail"
    assert cmd.update["conclusion"].startswith("ABORT: EVALUATOR_TERMINAL: failed")
    assert cmd.update["_terminal_verdict"] == "failed"


def test_agent_node_structural_verdict_overrides_plain_text(monkeypatch):
    """Agent 自然语言结论未带 DONE/ABORT 时，_terminal_verdict 决定最终状态。"""
    fake_ctx = SimpleNamespace(
        perceiver=None,
        knowledge_base=None,
        device=None,
    )
    monkeypatch.setattr(nodes, "get_tool_context", lambda: fake_ctx)
    monkeypatch.setattr(
        nodes, "_ensure_device_alive", lambda max_retries=2, wait_sec=5.0: True
    )
    monkeypatch.setattr(
        nodes,
        "_run_agent",
        lambda *args, **kwargs: (
            "任务已完成，所有验证通过。",
            [],
            {
                "loop_detected": False,
                "loop_pattern": "",
                "loop_break_action": "",
                "llm_call_count": 3,
                "tool_call_400_count": 0,
            },
            "passed",
        ),
    )
    state = {
        "user_request": "u",
        "app_package": "pkg",
        "app_name": "app",
        "goal_description": {"verification": ["验证1"]},
        "step_history": [],
        "messages": [],
        "budget_violation_count": 0,
    }
    cmd = graph.agent_node(
        state, {"configurable": {"test_config": AppTestConfig(), "thread_id": "t"}}
    )
    assert cmd.update["status"] == "success"
    assert cmd.update["_terminal_verdict"] == "passed"


def test_reporter_persists_metrics(monkeypatch):
    captured = {}

    class FakeDB:
        def record_execution_run(self, **kwargs):
            captured["run"] = kwargs

        def record_evidence_events(self, run_id, events):
            captured["evidence"] = {"run_id": run_id, "events": events}

    fake_ctx = SimpleNamespace(
        _evidence_events=[
            {
                "verification_key": "v0",
                "clause_id": "v0.0",
                "channel": "ui_text",
                "status": "PASS",
            }
        ]
    )
    monkeypatch.setattr(nodes, "get_tool_context", lambda: fake_ctx)
    monkeypatch.setattr(tools_module, "get_tool_context", lambda: fake_ctx)
    monkeypatch.setattr(graph, "_relational_db", FakeDB())
    state = {
        "status": "success",
        "conclusion": "DONE: ok",
        "goal_description": {"verification": ["验证1"]},
        "verification_contract": {
            "status": "approved",
            "verifications": [
                {
                    "key": "v0",
                    "statement": "验证1",
                    "clauses": [{"id": "v0.0", "channels": ["ui_text"]}],
                }
            ],
        },
        "step_history": [],
        "messages": [],
        "budget_violation_count": 0,
        "llm_call_count": 11,
        "_tool_calls_log": [],
        "user_request": "u",
        "app_package": "pkg",
        "app_name": "app",
    }
    graph.reporter_node(
        state,
        {
            "configurable": {
                "test_config": AppTestConfig(write_run_trace=False),
                "thread_id": "rid",
            }
        },
    )
    assert captured["run"]["verdict"] == "passed"
    assert captured["run"]["execution_mode"] == "explore"
    assert captured["evidence"]["run_id"] == "rid"
    assert len(captured["evidence"]["events"]) == 1


def test_should_include_rag_on_first_iteration():
    state = {"step_history": [], "_rag_injected_once": False}
    assert graph._should_include_rag(state, "com.test.app") is True  # type: ignore[attr-defined]


def test_should_include_rag_skips_stable_repeated_iterations():
    state = {
        "step_history": [
            {"status": "continue", "observation": "ok", "loop_detected": False}
        ],
        "_rag_injected_once": True,
        "_rag_last_app_package": "com.test.app",
    }
    assert graph._should_include_rag(state, "com.test.app") is False  # type: ignore[attr-defined]


def test_should_include_rag_when_recent_loop_detected():
    state = {
        "step_history": [
            {"status": "continue", "observation": "x", "loop_detected": True}
        ],
        "_rag_injected_once": True,
        "_rag_last_app_package": "com.test.app",
    }
    assert graph._should_include_rag(state, "com.test.app") is True  # type: ignore[attr-defined]


def test_should_force_request_knowledge_on_risky_no_rag():
    state = {
        "step_history": [
            {
                "status": "continue",
                "observation": "NO_PROGRESS warning",
                "loop_detected": False,
            }
        ]
    }
    assert (
        graph._should_force_request_knowledge(state, include_rag=False, rag_summary="")  # type: ignore[attr-defined]
        is True
    )


def test_should_not_force_query_when_rag_already_available():
    state = {
        "step_history": [{"status": "fail", "observation": "x", "loop_detected": False}]
    }
    assert (
        graph._should_force_request_knowledge(state, include_rag=True, rag_summary="## 人工知识")  # type: ignore[attr-defined]
        is False
    )


def test_plan_review_ignores_frontend_bad_contract(monkeypatch):
    """锁 bug：plan_review_node 必须忽略前端回传的坏 verification_contract，
    一律由后端 build_verification_contract 重建（修复 context_spans + channels 漂移）。

    前端坏 contract 特征：status=contract_pending_review、context_spans=[]、channels 简化。
    """
    # 1) 跳过 stop 拦截 + 提供 fake ctx（plan_review_node 直接读 get_tool_context）
    monkeypatch.setattr(nodes, "_stop_or_continue", lambda state, ctx: None)
    monkeypatch.setattr(nodes, "get_tool_context", lambda: SimpleNamespace())
    # 2) 替换 interrupt 为返回前端坏 contract 的 confirm 决策
    bad_contract = {
        "status": "contract_pending_review",
        "context_spans": [],
        "verifications": [
            {
                "key": "v0",
                "statement": "页面切换到设置页并且开关被打开",
                "channels": ["ui_text", "vision_verify", "click_and_check", "behavior_effect"],
            }
        ],
    }
    fake_decision = {
        "action": "confirm",
        "goal": "切到设置页并打开开关",
        "target_pages": ["设置页"],
        "verification": ["页面切换到设置页并且开关被打开"],
        "hints": [],
        "verification_contract": bad_contract,
    }
    monkeypatch.setattr(
        "langgraph.types.interrupt", lambda payload: fake_decision
    )

    state = {
        "goal_description": {
            "goal": "切到设置页并打开开关",
            "target_pages": ["设置页"],
            "verification": ["页面切换到设置页并且开关被打开"],
        },
        "user_request": "请验证设置页切换和开关",
        "verification_contract": bad_contract,
    }
    cmd = nodes.plan_review_node(
        state, {"configurable": {"test_config": AppTestConfig(), "thread_id": "t"}}
    )
    contract = cmd.update["verification_contract"]
    # 后端重建后 status 必须 approved（build->validate 恒 valid）
    assert contract["status"] == "approved"
    # context_spans 必须非空（前端坏 contract 的 [] 已被后端重建覆盖）
    assert contract["verifications"][0]["context_spans"]
    # channels 必须含 page_state / element_state（前端简化的漂移被修好）
    assert "page_state" in contract["verifications"][0]["clauses"][0]["channels"]
    assert "element_state" in contract["verifications"][0]["clauses"][0]["channels"]


def test_route_after_mode_selection_unapproved_goes_reporter():
    """锁 bug：verification_contract 未 approved 时，mode_selection 路由必须返回 reporter，
    避免 agent 空跑一轮再被 evaluator 判 inconclusive（旧 conditional edge 只认
    direct/agent，把 mode_selection_node 的 goto='reporter' 死代码覆盖掉）。"""
    # 未 approved：contract 缺 status
    state_pending = {
        "verification_contract": {"status": "contract_pending_review"},
        "execution_mode": "agent",
    }
    assert graph.route_after_mode_selection(state_pending) == "reporter"

    # 非 dict contract
    state_none = {"verification_contract": None, "execution_mode": "agent"}
    assert graph.route_after_mode_selection(state_none) == "reporter"

    # 已 approved -> agent
    state_agent = {
        "verification_contract": {"status": "approved"},
        "execution_mode": "agent",
    }
    assert graph.route_after_mode_selection(state_agent) == "agent"

    # 已 approved -> direct
    state_direct = {
        "verification_contract": {"status": "approved"},
        "execution_mode": "direct",
    }
    assert graph.route_after_mode_selection(state_direct) == "direct"

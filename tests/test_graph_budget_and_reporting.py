from __future__ import annotations

from types import SimpleNamespace

from config import TestConfig as AppTestConfig
from agents import graph
import tools as tools_module


def test_calc_budget_values():
    budget = graph._calc_budget(  # type: ignore[attr-defined]
        {"target_pages": ["p1", "p2", "p3"], "verification": ["v1", "v2"]}
    )
    assert budget["max_tool_calls_total"] == 59
    assert budget["max_agent_iterations"] == 7
    assert budget["max_turns_per_iteration"] == 9


def test_determine_execution_status_uses_dynamic_iteration_budget():
    state = {
        "status": "continue",
        "conclusion": "",
        "goal_description": {"target_pages": ["p1"], "verification": ["v1"]},
        "step_history": [{"index": i} for i in range(6)],
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


def test_reporter_keeps_verification_results_for_exhausted(monkeypatch):
    fake_ctx = SimpleNamespace(
        _verifications=[
            {
                "item": "验证1",
                "result": "passed",
                "detail": "证据充足",
                "screenshot": "",
            }
        ]
    )
    monkeypatch.setattr(graph, "get_tool_context", lambda: fake_ctx)
    state = {
        "status": "fail",
        "conclusion": "ABORT: MAX_TURNS_EXHAUSTED",
        "goal_description": {"verification": ["验证1"]},
        "step_history": [{"index": i} for i in range(6)],
        "messages": [],
        "budget_violation_count": 0,
    }
    cmd = graph.reporter_node(state, {"configurable": {"test_config": AppTestConfig()}})
    assert cmd.update["execution_status"] == "exhausted"
    assert cmd.update["test_verdict"] == "inconclusive"
    assert len(cmd.update["verification_results"]) == 1
    assert cmd.update["verification_results"][0]["item"] == "验证1"


def test_assert_verification_detail_retry_fallback(monkeypatch):
    fake_ctx = SimpleNamespace(
        _verifications=[],
        verification_auto_vision=False,
        device=None,
        _last_screenshot_path="",
    )
    monkeypatch.setattr(tools_module, "get_tool_context", lambda: fake_ctx)

    r1 = tools_module.assert_verification.invoke(
        {"condition": "验证A", "result": "passed", "detail": ""}
    )
    r2 = tools_module.assert_verification.invoke(
        {"condition": "验证A", "result": "passed", "detail": ""}
    )
    r3 = tools_module.assert_verification.invoke(
        {"condition": "验证A", "result": "passed", "detail": ""}
    )

    assert "detail is required" in r1
    assert "detail is required" in r2
    assert "记录完成" in r3
    assert fake_ctx._verifications[0]["detail"] == "detail unavailable after retries"

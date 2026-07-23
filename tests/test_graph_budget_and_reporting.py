from __future__ import annotations

from types import SimpleNamespace

from config import TestConfig as AppTestConfig
from agents import graph
from agents import nodes
import tools as tools_module


def test_calc_budget_values():
    budget = graph._calc_budget(  # type: ignore[attr-defined]
        {"target_pages": ["p1", "p2", "p3"], "verification": ["v1", "v2"]}
    )
    assert budget["max_tool_calls_total"] == 59
    assert budget["max_agent_iterations"] == 7
    assert budget["max_turns_per_iteration"] == 50


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
    monkeypatch.setattr(nodes, "get_tool_context", lambda: fake_ctx)
    state = {
        "status": "fail",
        "conclusion": "ABORT: MAX_TURNS_EXHAUSTED",
        "goal_description": {"verification": ["验证1"]},
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


def test_assert_verification_dedupes_by_stable_key(monkeypatch):
    fake_ctx = SimpleNamespace(
        _verifications=[],
        _verification_key_map={
            "计算器界面显示结果为20": "v0",
            "计算器结果显示20": "v0",
        },
        verification_auto_vision=False,
        device=None,
        _last_screenshot_path="",
    )
    monkeypatch.setattr(tools_module, "get_tool_context", lambda: fake_ctx)

    r1 = tools_module.assert_verification.invoke(
        {
            "condition": "计算器界面显示结果为20",
            "result": "passed",
            "detail": "首轮通过",
        }
    )
    r2 = tools_module.assert_verification.invoke(
        {"condition": "计算器结果显示20", "result": "passed", "detail": "重复上报"}
    )

    assert "记录完成" in r1
    assert "DUPLICATE_IGNORED" in r2
    assert len(fake_ctx._verifications) == 1
    assert fake_ctx._verifications[0]["key"] == "v0"


def test_route_after_agent_prefers_reporter_when_all_verifications_passed(monkeypatch):
    fake_ctx = SimpleNamespace(
        _verifications=[
            {"key": "v0", "item": "验证1", "result": "passed"},
            {"key": "v1", "item": "验证2", "result": "passed"},
        ]
    )
    monkeypatch.setattr(graph, "get_tool_context", lambda: fake_ctx)
    state = {
        "status": "continue",
        "goal_description": {"verification": ["验证1", "验证2"]},
        "step_history": [],
    }
    assert graph.route_after_agent(state) == "reporter"


def test_agent_node_accumulates_llm_call_metrics(monkeypatch):
    fake_ctx = SimpleNamespace(
        perceiver=None,
        knowledge_base=None,
        device=None,
        _verifications=[],
    )
    monkeypatch.setattr(nodes, "get_tool_context", lambda: fake_ctx)
    monkeypatch.setattr(nodes, "_ensure_device_alive", lambda max_retries=2, wait_sec=5.0: True)
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
    cmd = graph.agent_node(state, {"configurable": {"test_config": AppTestConfig(), "thread_id": "t"}})
    assert cmd.update["llm_call_count"] == 15
    assert cmd.update["tool_call_400_count"] == 3
    assert cmd.update["tool_call_400_rate"] == 0.2


def test_call_retry_should_retry_triggers_on_error_callback():
    captured = []
    err = ValueError("An assistant message with 'tool_calls' must be followed by tool messages")
    should_retry = graph._call_retry_should_retry("openai", err, on_error=lambda e: captured.append(str(e)))  # type: ignore[attr-defined]
    assert should_retry is True
    assert len(captured) == 1


def test_reporter_persists_metrics(monkeypatch):
    captured = {}

    class FakeDB:
        def record_test_run(self, **kwargs):
            captured.update(kwargs)

    fake_ctx = SimpleNamespace(_verifications=[{"key": "v0", "item": "验证1", "result": "passed"}])
    monkeypatch.setattr(nodes, "get_tool_context", lambda: fake_ctx)
    monkeypatch.setattr(graph, "_relational_db", FakeDB())
    state = {
        "status": "success",
        "conclusion": "DONE: ok",
        "goal_description": {"verification": ["验证1"]},
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
    assert captured["llm_call_count"] == 11
    assert "exact_click_rate" in captured
    assert "input_tokens" in captured


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


def test_should_force_query_app_knowledge_on_risky_no_rag():
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
        graph._should_force_query_app_knowledge(state, include_rag=False, rag_summary="")  # type: ignore[attr-defined]
        is True
    )


def test_should_not_force_query_when_rag_already_available():
    state = {
        "step_history": [{"status": "fail", "observation": "x", "loop_detected": False}]
    }
    assert (
        graph._should_force_query_app_knowledge(state, include_rag=True, rag_summary="## 人工知识")  # type: ignore[attr-defined]
        is False
    )


def test_assert_verification_explicit_key_keeps_paraphrased_evidence_on_goal_item(monkeypatch):
    fake_ctx = SimpleNamespace(
        _verifications=[],
        _verification_key_map={"点击取消或弹框外空白处弹框关闭": "v5"},
        verification_auto_vision=False,
        device=None,
        _last_screenshot_path="",
    )
    monkeypatch.setattr(tools_module, "get_tool_context", lambda: fake_ctx)

    out = tools_module.assert_verification.invoke(
        {
            "condition": "已通过关闭按钮返回课程表页面",
            "result": "passed",
            "detail": "当前 Activity 已返回课程表",
            "verification_key": "v5",
        }
    )

    assert "verification_key=v5" in out
    assert fake_ctx._verifications[0]["key"] == "v5"


def _verification_element(
    *,
    label="",
    rid="",
    bounds=(0, 0, 100, 100),
    clickable=False,
    enabled=True,
    safe_to_click=True,
    path="",
):
    return SimpleNamespace(
        label=label,
        associated_label="",
        resource_id=rid,
        class_name="android.widget.LinearLayout",
        context_path=path,
        bounds=bounds,
        region="main_content",
        clickable=clickable,
        enabled=enabled,
        safe_to_click=safe_to_click,
    )


def test_unknown_verification_returns_related_current_page_interactive_facts_once(
    monkeypatch,
):
    disabled = _verification_element(
        label="课程背景色",
        rid="disabledColor",
        clickable=True,
        enabled=False,
    )
    color_row = _verification_element(
        label="课程背景色",
        rid="com.zui.calendar:id/llCourseColor",
        bounds=(560, 1023, 2000, 1173),
        clickable=True,
        path="content > curriculum_scroll_view > llCourseColor",
    )
    understanding = SimpleNamespace(
        activity="com.zui.calendar.EditCourseActivity",
        page_title="新建课程",
        elements=[disabled, color_row],
    )
    fake_ctx = SimpleNamespace(
        _verifications=[],
        _verification_key_map={},
        _verification_items_by_key={
            "v4": "弹出编辑弹窗，包含课程背景色(10种)"
        },
        verification_auto_vision=False,
        device=None,
        perceiver=SimpleNamespace(perceive=lambda: understanding),
        _last_screenshot_path="",
    )
    monkeypatch.setattr(tools_module, "get_tool_context", lambda: fake_ctx)
    detail = "字段存在，但背景色具体颜色数量需展开后确认"
    args = {
        "condition": "编辑课程字段",
        "result": "unknown",
        "detail": detail,
        "verification_key": "v4",
    }

    first = tools_module.assert_verification.invoke(args)
    second = tools_module.assert_verification.invoke(args)

    assert "[当前页面可交互事实]" in first
    assert "[1] '课程背景色'" in first
    assert "llCourseColor" in first
    assert "disabledColor" not in first
    assert "related_interactive_count=1" in first
    assert "[当前页面可交互事实]" not in second
    assert fake_ctx._verifications[0]["detail"] == detail


def test_unknown_verification_promotes_matching_child_to_clickable_parent(monkeypatch):
    parent = _verification_element(
        label="",
        rid="com.example:id/colorRow",
        bounds=(100, 100, 500, 300),
        clickable=True,
        path="content > colorRow",
    )
    child = _verification_element(
        label="背景色指示器",
        rid="com.example:id/viewColorIndicator",
        bounds=(420, 160, 460, 200),
        clickable=False,
        path="content > colorRow",
    )
    understanding = SimpleNamespace(
        activity="com.example.EditActivity",
        page_title="编辑",
        elements=[parent, child],
    )
    fake_ctx = SimpleNamespace(
        _verifications=[],
        _verification_key_map={},
        _verification_items_by_key={"v0": "背景色有10种"},
        verification_auto_vision=False,
        device=None,
        perceiver=SimpleNamespace(perceive=lambda: understanding),
        _last_screenshot_path="",
    )
    monkeypatch.setattr(tools_module, "get_tool_context", lambda: fake_ctx)

    out = tools_module.assert_verification.invoke(
        {
            "condition": "背景色数量",
            "result": "unknown",
            "detail": "背景色指示器存在，但选项数量无法确认",
            "verification_key": "v0",
        }
    )

    assert "[0] '<无文本>'" in out
    assert "colorRow" in out
    assert "child:背景色指示器" in out
    assert "viewColorIndicator" not in out.split("related_interactives=", 1)[0]

from __future__ import annotations

from types import SimpleNamespace

from agents import graph, nodes
from agents.budget import _calc_mode_phase_budget
from agents.plan_extractor import verification_fingerprint


def _state() -> dict:
    return {
        "app_package": "com.example.app",
        "user_request": "创建课程表",
        "verification_contract": {"status": "approved"},
    }


def test_mode_selection_uses_explore_without_matching_plan(monkeypatch):
    expected_fp = verification_fingerprint({"status": "approved"})

    class FakeDatabase:
        def find_matching_execution_plan(
            self, app_package, user_request="", verification_fingerprint="", environment_key="", task_signature=None
        ):
            assert (app_package, user_request) == ("com.example.app", "创建课程表")
            assert verification_fingerprint == expected_fp
            return None

    monkeypatch.setattr(graph, "_relational_db", FakeDatabase())
    monkeypatch.setattr(nodes, "get_tool_context", lambda: None)

    command = nodes.mode_selection_node(_state(), {})

    assert command.update["execution_mode"] == "explore"
    assert command.update["lifecycle_state"] == "Bootstrapping"
    assert command.update["mode_selection_reason"] == "no_matching_plan"


def test_mode_selection_uses_guided_for_matching_candidate(monkeypatch):
    expected_fp = verification_fingerprint({"status": "approved"})

    class FakeDatabase:
        def find_matching_execution_plan(
            self, app_package, user_request="", verification_fingerprint="", environment_key="", task_signature=None
        ):
            assert verification_fingerprint == expected_fp
            return {
                "plan_id": "plan-1",
                "plan_trust": "candidate",
                "direct_approved": 1,
                "actions": [{"tool_name": "click", "action_index": 0}],
            }

    monkeypatch.setattr(graph, "_relational_db", FakeDatabase())
    monkeypatch.setattr(nodes, "get_tool_context", lambda: None)

    command = nodes.mode_selection_node(_state(), {})

    assert command.update["execution_mode"] == "guided"
    assert command.update["lifecycle_state"] == "Guided"
    assert command.update["plan_id"] == "plan-1"
    assert command.update["selected_plan_actions"] == [
        {"tool_name": "click", "action_index": 0}
    ]


def test_guided_failure_downgrades_once_to_explore():
    state = {
        "execution_mode": "guided",
        "step_history": [{"index": 2, "status": "fail", "loop_detected": True}],
        "_guided_downgrade_count": 0,
    }

    assert graph._should_downgrade_guided(state) is True
    command = nodes.mode_transition_node(state, {})

    assert command.update["execution_mode"] == "explore"
    assert command.update["lifecycle_state"] == "Explore"
    assert command.update["mode_transition_events"] == [
        {
            "from": "guided",
            "to": "explore",
            "reason": "guided_loop_detected",
            "step_index": 2,
            "plan_id": "",
            "action_id": "",
            "phase_budget": _calc_mode_phase_budget(state, "guided"),
        }
    ]
    assert command.update["messages"] == []
    assert (
        graph._should_downgrade_guided({**state, "_guided_downgrade_count": 1}) is False
    )


def test_guided_phase_budget_downgrades_to_explore():
    state = {
        "execution_mode": "guided",
        "goal_description": {"verification": ["a"]},
        "step_history": [
            {"index": index, "execution_mode": "guided"} for index in range(10)
        ],
        "_guided_downgrade_count": 0,
    }

    assert graph._should_downgrade_guided(state) is True
    command = nodes.mode_transition_node(state, {})
    assert command.update["mode_selection_reason"] == "guided_phase_budget_exhausted"


def test_direct_actions_exhausted_downgrade_to_guided(monkeypatch):
    monkeypatch.setattr(nodes, "get_tool_context", lambda: None)
    command = nodes.direct_node(
        {"selected_plan_actions": [], "_direct_action_cursor": 0}, {}
    )

    assert command.update["execution_mode"] == "guided"
    assert command.update["mode_transition_events"] == [
        {
            "from": "direct",
            "to": "guided",
            "reason": "direct_actions_exhausted",
            "step_index": 0,
            "plan_id": "",
            "action_id": "",
            "phase_budget": _calc_mode_phase_budget(
                {"selected_plan_actions": [], "_direct_action_cursor": 0}, "direct"
            ),
        }
    ]


def test_direct_action_advances_cursor_when_postcondition_matches(monkeypatch):
    class FakeTool:
        name = "click"

        def invoke(self, tool_input):
            assert tool_input == {"label": "save"}
            return "OK: clicked"

    device = SimpleNamespace(
        current_app=lambda: {"package": "com.example.app", "activity": "Saved"}
    )
    context = SimpleNamespace(device=device, _action_events=[])
    monkeypatch.setattr(nodes, "get_tool_context", lambda: context)
    monkeypatch.setattr(nodes, "AGENT_TOOLS", [FakeTool()])

    command = nodes.direct_node(
        {
            "selected_plan_actions": [
                {
                    "tool_name": "click",
                    "tool_input_json": '{"label":"save"}',
                    "precondition_json": '{"package":"com.example.app","activity":"Saved"}',
                    "postcondition_json": '{"package":"com.example.app","activity":"Saved"}',
                    "locator_json": '{"rid":"save"}',
                }
            ],
            "_direct_action_cursor": 0,
            "goal_description": {"verification": ["saved"]},
        },
        {},
    )

    assert command.update["_direct_action_cursor"] == 1
    assert context._action_events[0]["execution_mode"] == "direct"


def test_direct_postcondition_drift_downgrades_to_guided(monkeypatch):
    class FakeTool:
        name = "click"

        def invoke(self, tool_input):
            return "OK: clicked"

    device = SimpleNamespace(
        current_app=lambda: {"package": "com.example.app", "activity": "Before"}
    )
    context = SimpleNamespace(device=device, _action_events=[])
    monkeypatch.setattr(nodes, "get_tool_context", lambda: context)
    monkeypatch.setattr(nodes, "AGENT_TOOLS", [FakeTool()])

    command = nodes.direct_node(
        {
            "selected_plan_actions": [
                {
                    "tool_name": "click",
                    "tool_input_json": "{}",
                    "precondition_json": '{"activity":"Before"}',
                    "postcondition_json": '{"activity":"After"}',
                    "locator_json": "{}",
                }
            ],
            "_direct_action_cursor": 0,
            "goal_description": {"verification": ["saved"]},
        },
        {},
    )

    assert command.update["execution_mode"] == "guided"
    assert command.update["mode_selection_reason"] == "direct_postcondition_failed"


def test_mode_selection_terminates_to_reporter_when_contract_not_approved():
    state = {
        "app_package": "com.example.app",
        "user_request": "创建课程表",
        "verification_contract": {"status": "contract_pending_review"},
    }

    command = nodes.mode_selection_node(state, {})

    # mode_selection_node 不再设 goto（路由交由 graph conditional edge 接管），
    # 节点只负责写好 fail 状态；真正跳 reporter 的是 route_after_mode_selection。
    assert command.goto == ()
    assert command.update["status"] == "fail"
    assert "CONTRACT_REVIEW_REQUIRED" in command.update["conclusion"]
    assert command.update["mode_selection_reason"] == "verification_contract_not_approved"
    # 真正生效的路由：未 approved -> reporter（合并节点 update 后）
    routed_state = {**state, "verification_contract": command.update.get("verification_contract", state["verification_contract"])}
    assert graph.route_after_mode_selection(routed_state) == "reporter"


def test_mode_selection_terminates_to_reporter_when_span_validation_fails():
    state = {
        "app_package": "com.example.app",
        "user_request": "创建课程表",
        "verification_contract": {
            "status": "approved",
            "verifications": [
                {
                    "key": "v0",
                    "statement": "time is red and save is blocked",
                    "request_source_span": [0, 10],
                    "goal_source_span": [0, 33],
                    # Only one clause, leaving a gap from "and" onward.
                    "clauses": [
                        {
                            "id": "v0.0",
                            "claim": "time is red",
                            "goal_source_span": [0, 11],
                            "channels": ["vision_verify"],
                        }
                    ],
                }
            ],
        },
    }

    command = nodes.mode_selection_node(state, {})

    assert command.goto == ()
    assert command.update["status"] == "fail"
    assert command.update["mode_selection_reason"] == "verification_contract_span_invalid"
    # 真正生效的路由：节点把 contract status 回退为非 approved，
    # graph conditional edge 据此路由到 reporter（模拟 state 合并 update 后）
    routed_state = {**state, "verification_contract": command.update["verification_contract"]}
    assert graph.route_after_mode_selection(routed_state) == "reporter"


def test_direct_requires_full_environment_score(monkeypatch):
    expected_fp = verification_fingerprint({"status": "approved"})

    class FakeDatabase:
        def find_matching_execution_plan(
            self, app_package, user_request="", verification_fingerprint="", environment_key="", task_signature=None
        ):
            return {
                "plan_id": "plan-direct",
                "plan_trust": "candidate",
                "direct_approved": 1,
                # Plan 4.1 准入闸门：累计成功运行、平均质量、动作 postcondition 通过率
                "success_count": 5,
                "quality_score": 0.9,
                "environment_key": environment_key,
                "environment_compatibility_score": 1.0,
                "actions": [
                    {
                        "tool_name": "click",
                        "action_index": 0,
                        "execution_eligibility": "direct_eligible",
                        "attempt_count": 5,
                        "postcondition_pass_count": 5,
                    }
                ],
            }

    monkeypatch.setattr(graph, "_relational_db", FakeDatabase())
    monkeypatch.setattr(nodes, "get_tool_context", lambda: None)

    command = nodes.mode_selection_node(_state(), {})
    assert command.update["execution_mode"] == "direct"
    assert command.update["mode_selection_reason"] == "direct_approved_full_environment_match"


def test_direct_blocked_without_runs_or_quality(monkeypatch):
    """Plan 4.1：即使人工批准 + 全环境匹配，缺少连续 N 次成功/质量/对齐闸门也不得进入 direct。"""

    class FakeDatabase:
        def find_matching_execution_plan(
            self, app_package, user_request="", verification_fingerprint="", environment_key="", task_signature=None
        ):
            return {
                "plan_id": "plan-direct",
                "plan_trust": "candidate",
                "direct_approved": 1,
                "success_count": 0,
                "quality_score": 0.0,
                "environment_key": environment_key,
                "environment_compatibility_score": 1.0,
                "actions": [
                    {
                        "tool_name": "click",
                        "action_index": 0,
                        "execution_eligibility": "direct_eligible",
                        "attempt_count": 0,
                        "postcondition_pass_count": 0,
                    }
                ],
            }

    monkeypatch.setattr(graph, "_relational_db", FakeDatabase())
    monkeypatch.setattr(nodes, "get_tool_context", lambda: None)

    command = nodes.mode_selection_node(_state(), {})
    assert command.update["execution_mode"] == "guided"
    assert command.update["mode_selection_reason"] == "guided_from_approved_no_runs"


def test_minor_environment_drift_selects_guided(monkeypatch):
    expected_fp = verification_fingerprint({"status": "approved"})

    class FakeDatabase:
        def find_matching_execution_plan(
            self, app_package, user_request="", verification_fingerprint="", environment_key="", task_signature=None
        ):
            return {
                "plan_id": "plan-guided",
                "plan_trust": "candidate",
                "direct_approved": 1,
                "environment_key": environment_key,
                "environment_compatibility_score": 0.8,
                "actions": [
                    {"tool_name": "click", "action_index": 0, "execution_eligibility": "direct_eligible"}
                ],
            }

    monkeypatch.setattr(graph, "_relational_db", FakeDatabase())
    monkeypatch.setattr(nodes, "get_tool_context", lambda: None)

    command = nodes.mode_selection_node(_state(), {})
    assert command.update["execution_mode"] == "guided"
    assert command.update["mode_selection_reason"] == "matching_plan_guided_partial_environment"


def test_direct_downgrade_count_prevents_re_entry(monkeypatch):
    monkeypatch.setattr(nodes, "get_tool_context", lambda: None)
    command = nodes.direct_node(
        {
            "selected_plan_actions": [],
            "_direct_action_cursor": 0,
            "_direct_downgrade_count": 1,
        },
        {},
    )
    assert command.update["execution_mode"] == "guided"
    assert command.update["mode_selection_reason"] == "direct_reentry_guard"


def test_guided_downgrade_count_prevents_second_explore_transition():
    command = nodes.mode_transition_node(
        {"_guided_downgrade_count": 1, "step_history": []}, {}
    )
    assert command.update["execution_mode"] == "explore"
    assert command.update["mode_selection_reason"] == "guided_reentry_guard"


def test_direct_node_records_timeout_status(monkeypatch):
    class FakeTool:
        name = "click"

        def invoke(self, tool_input):
            return "TIMEOUT: tool did not respond"

    device = SimpleNamespace(current_app=lambda: {"package": "com.example.app", "activity": "Main"})
    context = SimpleNamespace(device=device, _action_events=[], screen_size=(1080, 2400))
    monkeypatch.setattr(nodes, "get_tool_context", lambda: context)
    monkeypatch.setattr(nodes, "AGENT_TOOLS", [FakeTool()])

    command = nodes.direct_node(
        {
            "selected_plan_actions": [
                {
                    "tool_name": "click",
                    "tool_input_json": '{"label":"save"}',
                    "precondition_json": '{"package":"com.example.app","activity":"Main"}',
                    "postcondition_json": '{"package":"com.example.app","activity":"Main"}',
                    "locator_json": '{"rid":"save"}',
                }
            ],
            "_direct_action_cursor": 0,
            "goal_description": {"verification": ["saved"]},
        },
        {},
    )
    assert command.update["execution_mode"] == "guided"
    assert command.update["mode_selection_reason"] == "direct_action_timeout"
    assert context._action_events[0]["status"] == "TIMEOUT"

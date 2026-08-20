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


def test_reporter_emits_discrepancy_for_authoritative_failure_m2():
    """M2（Plan §7 差异报告）：authoritative FAIL 的 clause 应附带 discrepancy（期望 vs 实际），
    且 conclusion 内含 discrepancies 区块；差异非 bug 定性（只呈现实测矛盾）。"""
    fake_ctx = SimpleNamespace(
        _evidence_events=[
            {
                "verification_key": "v0",
                "clause_id": "v0.0",
                "channel": "behavior_effect",
                "status": "FAIL",
                "authoritative": True,
                "fact": {
                    "anchor": "完成",
                    "resource_id": "com.xxx:id/done",
                    "bounds": "[0,0][100,48]",
                    "enabled": True,
                    "expected_disabled": True,
                },
            }
        ]
    )

    def _fake_ctx():
        return fake_ctx

    import unittest.mock as mock

    with mock.patch.object(nodes, "get_tool_context", _fake_ctx), mock.patch.object(
        tools_module, "get_tool_context", _fake_ctx
    ):
        cmd = graph.reporter_node(
            {
                "status": "fail",
                "conclusion": "ABORT: EVALUATOR_TERMINAL: failed",
                "goal_description": {"verification": ["完成后按钮置灰"]},
                "verification_contract": {
                    "status": "approved",
                    "verifications": [
                        {
                            "key": "v0",
                            "statement": "完成后按钮置灰",
                            "clauses": [{"id": "v0.0", "channels": ["behavior_effect"]}],
                        }
                    ],
                },
                "step_history": [{"index": 0}],
                "messages": [],
                "budget_violation_count": 0,
            },
            {"configurable": {"test_config": AppTestConfig(write_run_trace=False)}},
        )
    results = cmd.update["verification_results"]
    assert len(results) == 1
    clause = results[0]["clauses"][0]
    # 差异条目存在（failed + 权威反证）
    disc = clause["discrepancy"]
    assert disc is not None
    assert disc["expected"] == {"disabled": True}
    assert disc["actual"] == {"enabled": True}
    assert disc["authoritative"] is True
    # conclusion 含 discrepancies 区块（可审计）
    assert "discrepancies" in cmd.update["conclusion"]
    # 差异报告不自动定性 bug（不含 report_bug 字样）
    assert "report_bug" not in cmd.update["conclusion"]


def test_reporter_discrepancy_not_missed_by_deciding_evidence_m2():
    """M2 漏报回归（Review 问题3）：v5 场景 agent 先调非权威 PASS、后调权威 FAIL，
    _deciding_evidence 指向先来的非权威 PASS，per-clause discrepancy 不应因此漏报。
    修复后 _build_discrepancy 直接遍历 evidence 找该 clause 的 authoritative FAIL。"""
    fake_ctx = SimpleNamespace(
        _evidence_events=[
            {
                # 先来：非权威 PASS（不应决定 discrepancy）
                "verification_key": "v0",
                "clause_id": "v0.0",
                "channel": "ui_text",
                "status": "PASS",
                "authoritative": False,
                "fact": {"text": "完成"},
            },
            {
                # 后来：权威 FAIL（disabled 谓词）——应被提取为 discrepancy
                "verification_key": "v0",
                "clause_id": "v0.0",
                "channel": "behavior_effect",
                "status": "FAIL",
                "authoritative": True,
                "fact": {
                    "anchor": "完成",
                    "resource_id": "com.xxx:id/done",
                    "bounds": "[0,0][100,48]",
                    "enabled": True,
                    "expected_disabled": True,
                },
            },
        ]
    )

    def _fake_ctx():
        return fake_ctx

    import unittest.mock as mock

    with mock.patch.object(nodes, "get_tool_context", _fake_ctx), mock.patch.object(
        tools_module, "get_tool_context", _fake_ctx
    ):
        cmd = graph.reporter_node(
            {
                "status": "fail",
                "conclusion": "ABORT: EVALUATOR_TERMINAL: failed",
                "goal_description": {"verification": ["完成后按钮置灰"]},
                "verification_contract": {
                    "status": "approved",
                    "verifications": [
                        {
                            "key": "v0",
                            "statement": "完成后按钮置灰",
                            "clauses": [{"id": "v0.0", "channels": ["behavior_effect"]}],
                        }
                    ],
                },
                "step_history": [{"index": 0}],
                "messages": [],
                "budget_violation_count": 0,
            },
            {"configurable": {"test_config": AppTestConfig(write_run_trace=False)}},
        )
    clause = cmd.update["verification_results"][0]["clauses"][0]
    disc = clause["discrepancy"]
    # 关键断言：即使非权威 PASS 先到，discrepancy 不能漏报
    assert disc is not None, "per-clause discrepancy 不应被先到的非权威 PASS 漏报"
    assert disc["authoritative"] is True
    assert disc["expected"] == {"disabled": True}
    assert disc["actual"] == {"enabled": True}


def test_is_explore_blocked_tool_guard_m3():
    """M3（Plan §8 工具层硬护栏）：explore_restricted 时拒绝 click_and_check（契约外探索），
    但不拦截 click / assert_behavior_effect 等补验证动作。"""
    from agents.llm_runtime import _is_explore_blocked

    # 未限制：任何动作都放行
    assert _is_explore_blocked("click_and_check", False) is False
    assert _is_explore_blocked("click", False) is False
    # 已限制：契约外探索（click 序列 / click_and_check）被硬拦，re-import 被堵
    assert _is_explore_blocked("click_and_check", True) is True
    assert _is_explore_blocked("click", True) is True
    # 补验证类断言/读取动作仍可继续执行
    assert _is_explore_blocked("assert_behavior_effect", True) is False
    assert _is_explore_blocked("visual_check", True) is False
    assert _is_explore_blocked("vision_verify", True) is False


def test_agent_node_consumes_explore_restricted_loop_meta_m3(monkeypatch):
    """Bug A 回归（Review 死代码）：agent_node 必须消费 loop_meta['loop_pattern']=='EXPLORE_RESTRICTED'
    （而非不存在的 loop_break_reason），走定制 ABORT 收敛路径。覆盖 agent_node 对工具层护栏结果的消费。"""
    import unittest.mock as mock

    from agents.llm_runtime import _run_agent

    # mock 工具上下文：explore_restricted 已置位（与 evaluator_node 一致）
    fake_ctx = SimpleNamespace(
        _evidence_events=[],
        perceiver=None,
        _execution_mode="explore",
        knowledge_base=None,
    )

    def _fake_ctx():
        return fake_ctx

    # mock _run_agent：返回工具层触发的 EXPLORE_RESTRICTED 收敛
    def _fake_run_agent(*a, **k):
        return (
            "（agent 试图 re-import）",
            [],
            {
                "loop_detected": True,
                "loop_pattern": "EXPLORE_RESTRICTED",
                "loop_break_action": "end_subgraph",
                "llm_call_count": 1,
                "tool_call_400_count": 0,
            },
            "failed",
        )

    monkeypatch.setattr(nodes, "get_tool_context", _fake_ctx)
    monkeypatch.setattr(tools_module, "get_tool_context", _fake_ctx)
    monkeypatch.setattr(nodes, "_ensure_device_alive", lambda *a, **k: True)
    monkeypatch.setattr(nodes, "_run_agent", _fake_run_agent)

    cmd = graph.agent_node(
        {
            "execution_mode": "explore",
            "goal_description": {"verification": ["x"]},
            "verification_contract": {"status": "approved", "verifications": []},
            "step_history": [],
            "messages": [],
            "_explore_restricted": True,
            "budget_violation_count": 0,
        },
        {"configurable": {"test_config": AppTestConfig(write_run_trace=False)}},
    )
    # 定制 ABORT 消息生效（证明死代码已修复，loop_pattern 被正确消费）
    assert "ABORT: EXPLORE_RESTRICTED" in cmd.update["conclusion"]
    # abort → 本轮 fail 收敛（不再回 agent 死循环，路由由 route_after_agent 决定）
    assert cmd.update["status"] == "fail"


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


def test_unknown_route_limit_forces_reporter_m2():
    """M2（Plan §7）：inconclusive 路由累计达上限后，route_after_evaluator 强制 → reporter，
    治「问题2-RootA 图不终止」（不再吃满整个 agent 预算）。"""
    state = {
        "status": "continue",
        "execution_mode": "explore",
        "goal_description": {"verification": ["验证1"]},
        "verification_contract": {
            "status": "approved",
            "verifications": [{"key": "v0", "clauses": [{"id": "v0.0", "channels": ["ui_text"]}]}],
        },
        "clause_state": {"verdict": "inconclusive"},
        # 已达到 UNKNOWN_ROUTE_LIMIT（3）
        "_unknown_route_count": graph.UNKNOWN_ROUTE_LIMIT,
        "_unknown_exhausted": True,
    }
    assert graph.route_after_evaluator(state) == "reporter"


def test_unknown_route_limit_only_triggers_at_limit_m2():
    """M2：未达上限时 inconclusive 仍走探索循环（不提前强制终）。
    注：explore 模式在 route_after_evaluator 中落到末尾 return 'agent'
    （explore 是 agent 的子模式），故预期 'agent' 而非 'explore'。"""
    state = {
        "status": "continue",
        "execution_mode": "explore",
        "goal_description": {"verification": ["验证1"]},
        "verification_contract": {
            "status": "approved",
            "verifications": [{"key": "v0", "clauses": [{"id": "v0.0", "channels": ["ui_text"]}]}],
        },
        "clause_state": {"verdict": "inconclusive"},
        "_unknown_route_count": graph.UNKNOWN_ROUTE_LIMIT - 1,
    }
    assert graph.route_after_evaluator(state) == "agent"


def test_evaluator_node_sets_explore_restricted_after_threshold_m3():
    """M3（Plan §8）：evaluator_node 在 inconclusive 达阈值时置位 _explore_restricted，
    治 RootB agent 惯性（限制 re-import / launch_app 重开）。"""
    fake_ctx = SimpleNamespace(_evidence_events=[])

    state = {
        "status": "continue",
        "goal_description": {"verification": ["验证1"]},
        "verification_contract": {
            "status": "approved",
            "verifications": [{"key": "v0", "clauses": [{"id": "v0.0", "channels": ["ui_text"]}]}],
        },
        # 已累计 2 次 inconclusive（达 UNKNOWN_RESTRICT_AFTER）
        "_unknown_route_count": nodes.UNKNOWN_RESTRICT_AFTER - 1,
    }
    import agents.graph as _g
    import unittest.mock as mock

    def _fake_ctx():
        return fake_ctx

    with mock.patch.object(nodes, "get_tool_context", _fake_ctx), mock.patch.object(
        _g, "get_tool_context", _fake_ctx
    ):
        cmd = nodes.evaluator_node(state, {})
    assert cmd.update["clause_state"]["verdict"] == "inconclusive"
    # 累加 reducer：prior(2) + 1 = 3 >= UNKNOWN_RESTRICT_AFTER(2) → 置位
    assert cmd.update.get("_explore_restricted") is True
    assert cmd.update.get("_unknown_route_count") == 1  # 累加增量


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


def test_layer2_pending_list_injected_with_claim_and_unknown_only(monkeypatch):
    """证据驱动终止 · 第 2 层：agent_node 必须把 `缺证据`（clause status=="unknown"）的 clause
    带**精确身份** [verification_key::clause_id | channels:...] + claim 文本注入「待验证清单」，
    且只列 unknown、不列 failed。这是修复「agent 把所有 assert 打同一个 clause_id（如 v0.0）导致
    证据错配、其余 clause 全 unknown（前端『未验证』）」的关键：必须给 agent 每条 clause 的真实身份。

    构造：v0 已 passed（v0.0）、v1/v3/v4 仍 unknown（各含子 clause）、v2 failed（确定性矛盾）。
    预期：待验证清单含 v1.0/v3.0/v4.0 的精确身份与 claim；不含 failed 的 v2.x；且每条带 channels。
    """
    claim_by_clause = {
        "v0.0": "课程表页面成功打开",
        "v1.0": "进入课程表基本信息编辑页面",
        "v2.0": "编辑后返回课程表主页",
        "v3.0": "课程表新增条目显示正确",
        "v4.0": "删除课程表条目生效",
    }

    def _mk(key, status, cid):
        return {
            "key": key,
            "result": status,
            "clauses": [
                {
                    "id": cid,
                    "status": status,
                    "claim": claim_by_clause[cid],
                    "channels": ["ui_text"],
                }
            ],
        }

    fake_ctx = SimpleNamespace(
        perceiver=None,
        knowledge_base=None,
        device=None,
        _clause_state={
            "verifications": [
                _mk("v0", "passed", "v0.0"),
                _mk("v1", "unknown", "v1.0"),
                _mk("v2", "failed", "v2.0"),
                _mk("v3", "unknown", "v3.0"),
                _mk("v4", "unknown", "v4.0"),
            ]
        },
    )

    captured = {}

    def _fake_run_agent(msgs, *args, **kwargs):
        captured["msgs"] = msgs
        return (
            "CONTINUE",
            [],
            {
                "loop_detected": False,
                "loop_pattern": "",
                "loop_break_action": "",
                "llm_call_count": 1,
                "tool_call_400_count": 0,
            },
            "",
        )

    monkeypatch.setattr(nodes, "get_tool_context", lambda: fake_ctx)
    monkeypatch.setattr(nodes, "_ensure_device_alive", lambda *a, **k: True)
    monkeypatch.setattr(nodes, "_run_agent", _fake_run_agent)

    state = {
        "user_request": "u",
        "app_package": "pkg",
        "app_name": "app",
        "goal_description": {"verification": list(claim_by_clause.values())},
        "verification_contract": {
            "status": "approved",
            "verifications": [
                {"key": k, "statement": claim_by_clause[f"{k}.0"], "clauses": [{"id": f"{k}.0"}]}
                for k in ("v0", "v1", "v2", "v3", "v4")
            ],
        },
        "step_history": [],
        "messages": [],
        "budget_violation_count": 0,
    }
    graph.agent_node(
        state, {"configurable": {"test_config": AppTestConfig(), "thread_id": "t"}}
    )

    full = "\n".join(str(m) for m in captured["msgs"])
    # 标题存在
    assert "待验证清单" in full
    # 每条 unknown clause 以精确身份 [vN::vN.0 | channels:ui_text] 出现
    for cid in ("v1.0", "v3.0", "v4.0"):
        vkey = cid.split(".")[0]
        assert f"[{vkey}::{cid} | channels:ui_text] {claim_by_clause[cid]}" in full
    # 已通过验证段含 v0.0 身份
    assert "[v0::v0.0 | channels:ui_text]" in full
    # 只列 unknown：failed 的 clause（v2.0）绝不以「[v2::v2.0 ...]」出现在待验证清单中。
    assert "[v2::v2.0" not in full
    # 严禁全打同一个 clause_id 的提示存在（防止证据错配）
    assert "不得把所有 assert 都打同一个 clause_id" in full
    # 身份必须逐条不同（v1.0 / v3.0 / v4.0 都出现，而非统一 v0.0）
    assert "v1.0" in full and "v3.0" in full and "v4.0" in full
    assert full.count("v1.0") >= 1 and full.count("v3.0") >= 1 and full.count("v4.0") >= 1


def test_layer3_done_loopback_routes_agent(monkeypatch):
    """证据驱动终止 · 第 3 层：agent 声明 DONE（status=='success'）但 verdict 仍
    inconclusive（有 unknown、无 failed）时，route_after_evaluator 必须回环到 agent。
    提示靠 agent_node 每轮注入的「待验证清单」+「若你上轮已 report_done 但清单非空则不得重申 DONE」，
    不需要跨层 flag（路由函数的 state mutation 不传播）。
    failed 已在 verdict in {passed,failed} 分支被 reporter 截走，故此处只判 success+inconclusive。
    """
    fake_ctx = SimpleNamespace(_evidence_events=[])
    monkeypatch.setattr(graph, "get_tool_context", lambda: fake_ctx)
    monkeypatch.setattr(nodes, "get_tool_context", lambda: fake_ctx)
    state = {
        "status": "success",  # agent 已 DONE
        "goal_description": {"verification": ["v1", "v3", "v4"]},
        "verification_contract": {
            "status": "approved",
            "verifications": [{"key": "v0", "clauses": [{"id": "v0.0", "channels": ["ui_text"]}]}],
        },
        "clause_state": {"verdict": "inconclusive"},
    }
    # 真实行为：路由函数只返回路由串（不靠跨层 flag）。success+inconclusive 必须回环到 agent。
    # 注意：路由函数对 state 的 mutation 不会传播（LangGraph 快照），故不在此断言任何 state 变化。
    decision = graph.route_after_evaluator(state)
    assert decision == "agent"


def test_layer3_loopback_blocked_by_unknown_limit_m2():
    """第 3 层回环不绕过 M2 兜底：若 _unknown_route_count 已达上限，即便 status=success+inconclusive，
    仍强制收敛到 reporter（防死循环）。"""
    state = {
        "status": "success",
        "goal_description": {"verification": ["v1"]},
        "verification_contract": {
            "status": "approved",
            "verifications": [{"key": "v0", "clauses": [{"id": "v0.0", "channels": ["ui_text"]}]}],
        },
        "clause_state": {"verdict": "inconclusive"},
        "_unknown_route_count": graph.UNKNOWN_ROUTE_LIMIT,
        "_unknown_exhausted": True,
    }
    assert graph.route_after_evaluator(state) == "reporter"


def test_agent_node_emits_dedicated_loopback_hint(monkeypatch):
    """第 3 层回环提示（flag-free）：只要有待验证清单（pending 非空），agent_node 必须注入
    「若你上轮已 report_done 但清单仍非空，则不得原样重申 DONE」的约束——无需跨层 flag，
    因为 pending list 每轮都注入，且 agent 在历史里能看到自己上轮 DONE 被驳回。

    验证真实行为：提示出现 + 点破 DONE 被驳回 + 带精确 clause 身份 + 不得全打同一 clause_id。
    （不再断言任何 state flag 变化——路由函数的 state mutation 不传播，flag 已删除。）"""
    claim_by_key = {"v1": "进入课程表基本信息编辑页面", "v3": "课程表新增条目显示正确"}

    fake_ctx = SimpleNamespace(
        perceiver=None,
        knowledge_base=None,
        device=None,
        _clause_state={
            "verifications": [
                {
                    "key": "v1",
                    "result": "unknown",
                    "clauses": [
                        {"id": "v1.0", "status": "unknown", "claim": claim_by_key["v1"], "channels": ["ui_text"]}
                    ],
                },
                {
                    "key": "v3",
                    "result": "unknown",
                    "clauses": [
                        {"id": "v3.0", "status": "unknown", "claim": claim_by_key["v3"], "channels": ["ui_text"]}
                    ],
                },
            ]
        },
    )
    captured = {}

    def _fake_run_agent(msgs, *args, **kwargs):
        captured["msgs"] = msgs
        return (
            "CONTINUE",
            [],
            {
                "loop_detected": False,
                "loop_pattern": "",
                "loop_break_action": "",
                "llm_call_count": 1,
                "tool_call_400_count": 0,
            },
            "",
        )

    monkeypatch.setattr(nodes, "get_tool_context", lambda: fake_ctx)
    monkeypatch.setattr(nodes, "_ensure_device_alive", lambda *a, **k: True)
    monkeypatch.setattr(nodes, "_run_agent", _fake_run_agent)

    state = {
        "user_request": "u",
        "app_package": "pkg",
        "app_name": "app",
        "goal_description": {"verification": [claim_by_key[k] for k in ("v1", "v3")]},
        "verification_contract": {
            "status": "approved",
            "verifications": [
                {"key": k, "statement": claim_by_key[k], "clauses": [{"id": f"{k}.0"}]}
                for k in ("v1", "v3")
            ],
        },
        "step_history": [],
        "messages": [],
        "budget_violation_count": 0,
    }
    graph.agent_node(
        state, {"configurable": {"test_config": AppTestConfig(), "thread_id": "t"}}
    )
    full = "\n".join(str(m) for m in captured["msgs"])
    # 待验证清单非空时，回环约束提示出现，点破 DONE 被驳回
    assert "若你上轮已 report_done" in full
    assert "不得原样再重申 DONE" in full
    assert "terminate_run" in full
    # 提示必须带精确 clause 身份（让 agent 知道每条该打哪个 clause_id，不再全打 v0.0）
    assert "[v1::v1.0 | channels:ui_text]" in full
    assert "[v3::v3.0 | channels:ui_text]" in full
    assert "不得把所有 assert 都打同一个 clause_id" in full


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

from __future__ import annotations

"""P2 验收：执行模式状态机字段在 trace 中可见（Plan §2）。

契约收敛验证（不新增任何模式决策逻辑，只验证观测透出）：
- build_run_trace 在顶层产出 execution 块，含 mode / lifecycle_state /
  plan_id / plan_trust / mode_selection_reason / mode_transition_events。
- 缺省时 mode 回退为 explore，其余字段为空，不抛错。
"""

from agents.run_trace import build_run_trace


def test_plan_review_wait_survives_resume_rerun(monkeypatch):
    """plan_review 等待段：恢复重跑不得覆盖进入时刻（§13）。

    8/24 真机两跑实测等待段恒 0.0s——根因是恢复路径节点从头重跑，
    无条件重写 ctx._plan_review_entered_at 把首轮打点冲掉。修复后：
    只在未打点时写入，消费后清空。
    """
    from datetime import datetime, timedelta
    from types import SimpleNamespace

    import langgraph.types as lg_types

    from agents import nodes

    ctx = SimpleNamespace(_plan_review_entered_at="", _plan_review_wait_seconds=0.0)
    # 模拟首轮 interrupt 前已打点（ctx 跨 interrupt 存活）：进入在 5 秒前
    ctx._plan_review_entered_at = (datetime.now() - timedelta(seconds=5)).isoformat()
    monkeypatch.setattr(nodes, "get_tool_context", lambda: ctx)

    def _fake_interrupt(payload):  # 模拟恢复路径：interrupt 直接返回待定决策
        assert payload["type"] == "plan_review"
        return "cancel"

    monkeypatch.setattr(lg_types, "interrupt", _fake_interrupt)
    command = nodes.plan_review_node(
        {"verification_contract": None, "goal_description": {}}, {}
    )
    assert command.update == {"status": "cancelled"}
    # 等待段按「首轮进入时刻」计算，而非重跑时刻（留 1s 容差防时钟抖动）
    assert ctx._plan_review_wait_seconds >= 4.0
    # 消费后清空，不污染下一轮 run
    assert ctx._plan_review_entered_at == ""


def _minimal_trace(**overrides) -> dict:
    base = dict(
        run_id="r1",
        user_request="打开 WLAN",
        app_package="com.android.settings",
        app_name="Settings",
        execution_status="completed",
        test_verdict="passed",
        duration_seconds=1.0,
        tool_log=[],
        verification_results=[],
        token_usage={},
        metrics={},
    )
    base.update(overrides)
    return build_run_trace(**base)


def test_execution_block_present_and_defaults_to_explore():
    """缺省入参：execution.mode 回退 explore，其余字段为空，结构完整不抛错。"""
    trace = _minimal_trace()
    assert "execution" in trace
    exec_block = trace["execution"]
    assert exec_block["mode"] == "explore"
    assert exec_block["lifecycle_state"] == ""
    assert exec_block["plan_id"] == ""
    assert exec_block["plan_trust"] == ""
    assert exec_block["mode_selection_reason"] == ""
    assert exec_block["mode_transition_events"] == []


def test_execution_fields_transmitted():
    """传入执行模式字段后，trace.execution 如实透出（纯观测，不改动值）。"""
    transitions = [
        {"from": "explore", "to": "guided", "reason": "plan match"},
    ]
    trace = _minimal_trace(
        execution_mode="guided",
        lifecycle_state="executing_plan",
        plan_id="plan-abc",
        plan_trust="high",
        mode_selection_reason="high_compat_match",
        mode_transition_events=transitions,
    )
    exec_block = trace["execution"]
    assert exec_block["mode"] == "guided"
    assert exec_block["lifecycle_state"] == "executing_plan"
    assert exec_block["plan_id"] == "plan-abc"
    assert exec_block["plan_trust"] == "high"
    assert exec_block["mode_selection_reason"] == "high_compat_match"
    assert exec_block["mode_transition_events"] == transitions


def test_execution_mode_direct_is_observable():
    """对照 direct 路径：mode=direct 也可透出（供验收「至少有一次进入 direct」）。"""
    trace = _minimal_trace(execution_mode="direct")
    assert trace["execution"]["mode"] == "direct"


def test_time_segments_in_result():
    """时间账三段拆分（plan §13）：三段独立透出，缺省为 0，之和 == duration。"""
    trace = _minimal_trace(
        duration_seconds=120.0,
        planner_elapsed_seconds=55.9,
        plan_review_wait_seconds=60.0,
        execution_elapsed_seconds=4.1,
    )
    result = trace["result"]
    assert result["planner_elapsed_seconds"] == 55.9
    assert result["plan_review_wait_seconds"] == 60.0
    assert result["execution_elapsed_seconds"] == 4.1
    segments_sum = (
        result["planner_elapsed_seconds"]
        + result["plan_review_wait_seconds"]
        + result["execution_elapsed_seconds"]
    )
    assert abs(segments_sum - result["duration_seconds"]) < 1e-6

    # 缺省：三段为 0，不抛错
    default_trace = _minimal_trace()
    assert default_trace["result"]["planner_elapsed_seconds"] == 0.0

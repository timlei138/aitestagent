from __future__ import annotations

"""P2 验收：执行模式状态机字段在 trace 中可见（Plan §2）。

契约收敛验证（不新增任何模式决策逻辑，只验证观测透出）：
- build_run_trace 在顶层产出 execution 块，含 mode / lifecycle_state /
  plan_id / plan_trust / mode_selection_reason / mode_transition_events。
- 缺省时 mode 回退为 explore，其余字段为空，不抛错。
"""

from agents.run_trace import build_run_trace


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

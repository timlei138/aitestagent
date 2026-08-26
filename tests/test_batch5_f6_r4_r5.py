"""第五批单测（agent_evolution_plan §8 / §12）：F6① 状态兜底收窄、R4② direct_node
RPC 3→2 与 precondition activity 短名宽松匹配、R5 沉淀绕路剪枝。"""

from __future__ import annotations

from types import SimpleNamespace

from agents import graph, nodes
from agents.plan_extractor import _prune_detour_actions

P_MAIN = {"package": "com.example.app", "activity": "com.example.app.MainActivity"}
P_LIST = {"package": "com.example.app", "activity": "com.example.app.ListActivity"}
P_SHORT_MAIN = {"package": "com.example.app", "activity": ".MainActivity"}


# ── F6①：纯失败 3 步崩溃不再洗白成 completed ──


def test_status_fallback_pure_fail_three_steps_stays_error():
    state = {
        "status": "",
        "conclusion": "",
        "_terminal_verdict": "",
        "goal_description": {"target_pages": ["p1"], "verification": ["v1"]},
        "step_history": [
            {"index": 1, "status": "fail"},
            {"index": 2, "status": "fail"},
            {"index": 3, "status": "fail"},
        ],
    }
    assert graph._determine_execution_status(state) == "error"  # type: ignore[attr-defined]


def test_status_fallback_with_progress_step_completes():
    state = {
        "status": "",
        "conclusion": "",
        "_terminal_verdict": "",
        "goal_description": {"target_pages": ["p1"], "verification": ["v1"]},
        "step_history": [
            {"index": 1, "status": "continue"},
            {"index": 2, "status": "fail"},
            {"index": 3, "status": "continue"},
        ],
    }
    assert graph._determine_execution_status(state) == "completed"  # type: ignore[attr-defined]


# ── R4②：direct_node 单动作 current_app 3→2 + precondition 短名宽松 ──


class _StubTool:
    """替身导航工具：不触真实设备，隔离 direct_node 自身的 RPC 计数。"""

    name = "stub_nav"

    def invoke(self, args):
        return "OK: stub"


def _direct_action(precondition=None, postcondition=None):
    import json

    return {
        "tool_name": "stub_nav",
        "action_index": 0,
        "tool_input_json": '{"package": "com.example.app"}',
        "precondition_json": json.dumps(precondition) if precondition else "{}",
        "postcondition_json": json.dumps(postcondition) if postcondition else "{}",
        "locator_json": "{}",
    }


def _run_direct_node(monkeypatch, action):
    calls = {"current_app": 0}

    class CountingDevice:
        def current_app(self, refresh=False):
            calls["current_app"] += 1
            return dict(P_MAIN)

    ctx = SimpleNamespace(
        device=CountingDevice(), screen_size=(1080, 2400), _action_events=[]
    )
    monkeypatch.setattr(nodes, "get_tool_context", lambda: ctx)
    monkeypatch.setattr(nodes, "AGENT_TOOLS", [_StubTool()])
    state = {
        "execution_mode": "direct",
        "selected_plan_actions": [action],
        "_direct_action_cursor": 0,
        "plan_id": "p1",
        "mode_transition_events": [],
        "goal_description": {"target_pages": [], "verification": []},
    }
    return nodes.direct_node(state, {}), calls


def test_direct_success_uses_two_current_app_calls(monkeypatch):
    cmd, calls = _run_direct_node(
        monkeypatch, _direct_action(precondition=P_MAIN)  # 空 postcondition → 干净成功
    )
    # R4② 核心断言：before + after 各一次（原实现 after 查两次共 3 次）
    assert calls["current_app"] == 2
    assert cmd.update["_direct_action_cursor"] == 1


def test_direct_precondition_activity_short_name_lenient(monkeypatch):
    # 沉淀侧短名 ".Main" vs 运行侧全名 → 短名相等视为满足
    cmd, calls = _run_direct_node(
        monkeypatch, _direct_action(precondition=P_SHORT_MAIN)
    )
    assert calls["current_app"] == 2
    assert cmd.update["_direct_action_cursor"] == 1


def test_direct_precondition_mismatch_downgrades_without_executing(monkeypatch):
    stored = {"package": "com.example.app", "activity": ".Other"}
    cmd, calls = _run_direct_node(monkeypatch, _direct_action(precondition=stored))
    assert calls["current_app"] == 2  # before + after（事件落盘用），工具未执行
    assert cmd.update["execution_mode"] == "guided"
    assert cmd.update["mode_selection_reason"] == "direct_precondition_failed"


def test_direct_postcondition_mismatch_still_classified_after_merge(monkeypatch):
    cmd, calls = _run_direct_node(
        monkeypatch,
        _direct_action(postcondition={"package": "com.other.app"}),
    )
    assert calls["current_app"] == 2
    assert cmd.update["execution_mode"] == "guided"
    assert cmd.update["mode_selection_reason"] == "direct_postcondition_failed"


# ── R5：沉淀绕路剪枝（保守版：只剪「无位移且有同款重试」）──


def _action(tool, input_dict, before, after):
    return {
        "tool_name": tool,
        "tool_input": input_dict,
        "resolved_locator": {},
        "page_before": before,
        "page_after": after,
    }


def test_r5_prunes_noop_retry_keeps_last_attempt():
    actions = [
        # 点「新建」第一次无位移（试错），第二次进入列表页 → 剪第一次
        _action("click", {"label": "新建"}, P_MAIN, P_MAIN),
        _action("click", {"label": "新建"}, P_MAIN, P_LIST),
    ]
    kept = _prune_detour_actions(actions)
    assert len(kept) == 1
    assert kept[0]["page_after"] == P_LIST


def test_r5_keeps_stationary_action_without_later_retry():
    actions = [
        # 无位移但后续没有同款重试 —— 可能是关弹窗等关键动作，不剪（宁漏剪不误剪）
        _action("click", {"label": "允许"}, P_MAIN, P_MAIN),
        _action("click", {"label": "新建"}, P_MAIN, P_LIST),
    ]
    assert _prune_detour_actions(actions) == actions


def test_r5_keeps_moving_actions_and_missing_page_data():
    actions = [
        _action("click", {"label": "新建"}, P_MAIN, P_LIST),
        # 页面数据缺失 → 视为有位移，不剪
        _action("input_text", {"text": "课程"}, {}, {}),
    ]
    assert _prune_detour_actions(actions) == actions


def test_r5_collapses_repeated_stationary_chain():
    actions = [
        _action("click", {"label": "同步"}, P_MAIN, P_MAIN),
        _action("click", {"label": "同步"}, P_MAIN, P_MAIN),
        _action("click", {"label": "同步"}, P_MAIN, P_MAIN),  # 末次同款保留
        _action("click", {"label": "新建"}, P_MAIN, P_LIST),
    ]
    kept = _prune_detour_actions(actions)
    assert [a["tool_input"]["label"] for a in kept] == ["同步", "新建"]


def test_r5_different_tool_same_page_not_pruned():
    actions = [
        _action("input_text", {"text": "x"}, P_MAIN, P_MAIN),
        _action("click", {"label": "新建"}, P_MAIN, P_LIST),
    ]
    assert _prune_detour_actions(actions) == actions

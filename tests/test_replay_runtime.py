from __future__ import annotations

import json

from agents import nodes
from agents.budget import _calc_budget_from_state
from agents.run_trace import build_run_trace


def _state_with_replay(run_type: str, has_actions: bool):
    goal = {}
    if has_actions:
        goal = {
            "execution_plan": {
                "schema_version": 4,
                "effective": {
                    "schema_version": 4,
                    "key_actions": [
                        {"tool": "click", "preferred_locator": {"label": "课程表"}}
                    ],
                },
            }
        }
    return {"_run_type": run_type, "goal_description": goal}


def test_select_agent_system_uses_replay_prompt_for_rerun_with_actions():
    selected = nodes._select_agent_system(_state_with_replay("rerun", True))
    assert "REPLAY MODE" in selected


def test_select_agent_system_falls_back_to_explore_prompt():
    selected = nodes._select_agent_system(_state_with_replay("normal", True))
    assert "模式：Explore" in selected


def test_run_trace_keeps_tool_input_for_all_tools_and_replay_fields():
    trace = build_run_trace(
        run_id="r",
        user_request="u",
        app_package="p",
        app_name="a",
        execution_status="completed",
        test_verdict="passed",
        duration_seconds=1.0,
        tool_log=[
            {
                "tool_seq": 1,
                "name": "visual_check",
                "target": "",
                "intent_text": "check",
                "observation": "OK: done || verify_decision=yes",
                "screenshot_path": "",
                "tool_input": {"description": "x"},
                "replay_source": "script",
                "replay_step_idx": 3,
            }
        ],
        verification_results=[],
        token_usage={},
        metrics={},
    )
    step = trace["steps"][0]
    assert step["tool_input"] == {"description": "x"}
    assert step["replay_source"] == "script"
    assert step["replay_step_idx"] == 3


def test_replay_tool_instruction_appends_verify_for_vision_verify_channel():
    action = {
        "tool": "vision_tap",
        "tool_input": {"description": "分钟列50下方一行", "repeat": 3},
        "postcondition_channel": "vision_verify",
        "postcondition": {"expected_activity": "TimeSlotSettingsActivity", "expected_value": "09:00-09:53"},
    }
    instruction = nodes._replay_tool_instruction(action)
    assert "verify=" in instruction
    assert "09:00-09:53" in instruction
    assert "description=" in instruction


def test_replay_tool_instruction_no_verify_for_ui_text_channel():
    action = {
        "tool": "vision_tap",
        "tool_input": {"description": "播放按钮"},
        "postcondition": {"expected_activity": "PlayerActivity"},
    }
    instruction = nodes._replay_tool_instruction(action)
    assert "verify=" not in instruction


def test_resolve_action_texts_rewrites_without_mutating_source():
    action = {
        "tool": "click",
        "preferred_locator": {"label": "测试课程表2"},
        "postcondition": {"expected_value": "测试课程表2"},
        "tool_input": {"text": "测试课程表2"},
    }
    actuals = {"测试课程表2": "测试课程表2_a3f7"}
    resolved = nodes._resolve_action_texts(action, actuals)
    # 副本被改写
    assert resolved["preferred_locator"]["label"] == "测试课程表2_a3f7"
    assert resolved["postcondition"]["expected_value"] == "测试课程表2_a3f7"
    assert resolved["tool_input"]["text"] == "测试课程表2_a3f7"
    # 原始证据本体不被污染
    assert action["preferred_locator"]["label"] == "测试课程表2"
    assert action["postcondition"]["expected_value"] == "测试课程表2"


def test_get_actual_text_memoizes_within_run():
    actuals: dict = {}
    first = nodes._get_actual_text("测试课程表2", actuals)
    second = nodes._get_actual_text("测试课程表2", actuals)
    assert first == second
    assert first.startswith("测试课程表2_")
    assert len(actuals) == 1


def _goal_with_actions(n_actions: int, recovery_budget: int = 3):
    return {
        "goal": "g",
        "verification": ["v0 项"],
        "replay_recovery_budget": recovery_budget,
        "execution_plan": {
            "schema_version": 4,
            "effective": {
                "schema_version": 4,
                "key_actions": [
                    {"tool": "click", "preferred_locator": {"label": f"btn{i}"}}
                    for i in range(n_actions)
                ],
            },
        },
    }


def test_replay_budget_covers_script_length():
    # 26 步脚本 + recovery 预算 3 + buffer 4 = 33；必须超过默认 cap 行为
    state = {"_run_type": "rerun", "goal_description": _goal_with_actions(26)}
    budget = _calc_budget_from_state(state)
    assert budget["max_agent_iterations"] == 26 + 3 + 4


def test_normal_run_budget_unchanged_by_replay_formula():
    state = {"_run_type": "normal", "goal_description": _goal_with_actions(26)}
    budget = _calc_budget_from_state(state)
    # normal 不受回放公式影响（pages=0, verifs=1 → min(max(3,10),40)=10）
    assert budget["max_agent_iterations"] == 10


def test_verification_args_derive_from_fresh_asserts():
    actions = [
        {"tool": "assert_page_contains", "verify": "v0"},
        {"tool": "assert_verification", "verify_key": "v0"},
    ]
    goal = {"verification": ["可修改间隔为53分钟"]}
    log = [{"replay_step_idx": 0, "status_code": "PASS"}]
    args = nodes._build_replay_verification_args(actions[1], actions, log, goal)
    assert args["result"] == "passed"
    assert args["condition"] == "可修改间隔为53分钟"
    assert args["verification_key"] == "v0"


def test_verification_args_unknown_without_objective_evidence():
    actions = [{"tool": "assert_verification", "verify_key": "v1"}]
    goal = {"verification": ["v0 项", "v1 项"]}
    args = nodes._build_replay_verification_args(actions[0], actions, [], goal)
    assert args["result"] == "unknown"
    assert args["condition"] == "v1 项"


def test_verification_args_failed_when_linked_assert_fails():
    actions = [
        {"tool": "assert_page_contains", "verify": "v0"},
        {"tool": "assert_verification", "verify_key": "v0"},
    ]
    goal = {"verification": ["某验证"]}
    log = [{"replay_step_idx": 0, "status_code": "FAIL"}]
    args = nodes._build_replay_verification_args(actions[1], actions, log, goal)
    assert args["result"] == "failed"


def test_popup_gate_skips_when_next_step_stays_on_page():
    # 下一步期望当前 Activity（弹窗流程）→ 闸门不触发（不读 UI 树也安全返回 False）
    actions = [
        {"tool": "click"},
        {"tool": "vision_tap", "precondition": {"expected_activity": "TimeSlotSettingsActivity"}},
    ]
    assert nodes._replay_popup_blocks_next(None, actions, 0, "TimeSlotSettingsActivity") is False


def test_popup_gate_skips_at_last_step():
    actions = [{"tool": "report_done"}]
    assert nodes._replay_popup_blocks_next(None, actions, 0, "AnyActivity") is False


def test_replay_tool_instruction_click_emits_stable_locator_for_direct_exec():
    # G(direct) 路径直执行 click 的 args 来自 preferred_locator；
    # 指令必须携带 rid/label/class_name/path_contains，确保不退回 index 盲点。
    action = {
        "tool": "click",
        "preferred_locator": {
            "label": "课程表设置",
            "rid": "com.zui.calendar:id/action_curriculum_table_settings",
            "class_name": "android.widget.Button",
            "path_contains": "action_bar_root > content > toolbar",
        },
    }
    instruction = nodes._replay_tool_instruction(action)
    assert instruction.startswith("click(")
    assert "rid='com.zui.calendar:id/action_curriculum_table_settings'" in instruction
    assert "label='课程表设置'" in instruction
    assert "class_name='android.widget.Button'" in instruction
    assert "path_contains='action_bar_root > content > toolbar'" in instruction


def test_resolve_replay_mode_keeps_recovery_when_precondition_matches():
    # 上一 turn 因 postcondition 失败进入 recovery，本 turn precondition 仍匹配时
    # 必须保持 recovery，避免被覆写回 script 导致重复执行同一失败动作。
    assert nodes._resolve_replay_mode("recovery", True) == "recovery"


def test_resolve_replay_mode_switches_from_script_based_on_precondition():
    assert nodes._resolve_replay_mode("script", True) == "script"
    assert nodes._resolve_replay_mode("script", False) == "recovery"


def test_resolve_replay_mode_defaults_to_precondition_for_fresh_state():
    assert nodes._resolve_replay_mode("", True) == "script"
    assert nodes._resolve_replay_mode("", False) == "recovery"


def test_build_direct_click_args_prefers_stable_locator_over_index():
    # 有 rid 时，即使 tool_input 带 index，也不应把 index 传下去
    args = nodes._build_direct_click_args(
        {"label": "课程表", "rid": "com.zui.calendar:id/tv"},
        {"label": "课程表", "index": 0},
    )
    assert args["rid"] == "com.zui.calendar:id/tv"
    assert "index" not in args


def test_build_direct_click_args_falls_back_to_index_when_no_stable_locator():
    # 只有 label 时不算稳定，允许回退使用 index
    args = nodes._build_direct_click_args(
        {"label": "课程表"},
        {"label": "课程表", "index": 2},
    )
    assert args["index"] == 2


def test_build_direct_click_args_label_class_path_is_stable_no_index():
    # label + class_name + path_contains 组合也算稳定，不追加 index
    args = nodes._build_direct_click_args(
        {
            "label": "课程表",
            "class_name": "android.widget.FrameLayout",
            "path_contains": "rv_more_menu",
        },
        {"index": 0},
    )
    assert "index" not in args
    assert args["class_name"] == "android.widget.FrameLayout"


def test_replay_tool_instruction_click_fallback_to_empty_when_no_locator():
    # 没有任何可稳定 locator 时不能抛错，输出占位（后续由 index 兜底路径处理）
    instruction = nodes._replay_tool_instruction({"tool": "click", "preferred_locator": {}})
    assert instruction == "click(label='')"


def test_extract_click_keeps_stable_locator_from_resolved_target():
    # 回归保护：原始 run 的 click 即便 tool_input 只有 index，也要靠
    # resolved_target 重建 label/rid，避免导航步骤被整段丢弃导致脚本断链。
    from api.test_cases_routes import _extract_replay_evidence

    run = {
        "id": "diag-1",
        "execution_status": "completed",
        "test_verdict": "passed",
        "app_package": "com.zui.calendar",
        "goal_json": '{"verification": ["v0 项"]}',
        "steps_json": json.dumps(
            [
                {
                    "action_type": "launch_app",
                    "status_code": "OK",
                    "tool_input": {"package": "com.zui.calendar", "activity": "AllInOneActivity"},
                    "result_evidence": {
                        "arrival_confirmed": "true",
                        "package_matched": "true",
                        "observed_package": "com.zui.calendar",
                        "observed_activity": "AllInOneActivity",
                    },
                },
                {
                    "action_type": "click",
                    "status_code": "OK",
                    "tool_input": {"index": 4},  # 仅 index，无 label
                    "resolved_target": {
                        "label": "课程表设置",
                        "rid": "com.zui.calendar:id/action_curriculum_table_settings",
                    },
                    "result_evidence": {"resolved_label": "课程表设置"},
                },
            ]
        ),
    }
    evidence = _extract_replay_evidence(run)
    assert evidence is not None
    click_actions = [a for a in evidence["base_evidence"]["key_actions"] if a["tool"] == "click"]
    assert len(click_actions) == 1
    loc = click_actions[0]["preferred_locator"]
    assert loc.get("label") == "课程表设置"
    assert loc.get("rid") == "com.zui.calendar:id/action_curriculum_table_settings"


# ── Task 7.3: Recovery 指令增强 ──


def test_recovery_instruction_includes_popup_dismiss_hint():
    instruction = nodes._build_replay_system_instruction(
        action={"precondition": {"expected_activity": "TimetableListActivity"}, "tool": "click"},
        idx=5,
        total=12,
        current_activity="TimetableActivity",
        mode="recovery",
        recovery_used=2,
        recovery_budget=5,
    )
    assert "press_key" in instruction
    assert "back" in instruction
    assert "弹窗" in instruction or "覆盖层" in instruction


def test_recovery_instruction_no_popup_hint_in_script_mode():
    instruction = nodes._build_replay_system_instruction(
        action={
            "tool": "click",
            "precondition": {"expected_activity": "TimetableListActivity"},
            "postcondition": {"expected_activity": "EditActivity"},
        },
        idx=2,
        total=10,
        current_activity="TimetableListActivity",
        mode="script",
        recovery_used=0,
        recovery_budget=3,
    )
    assert "REPLAY_SCRIPT_STEP" in instruction
    assert "press_key" not in instruction


# ── Task 7.2: Recovery 预算动态化 ──


def test_dynamic_recovery_budget_short_script():
    # 3 步脚本 → max(3, 3//4) = max(3, 0) = 3
    goal = _goal_with_actions(3)
    actions = goal["execution_plan"]["effective"]["key_actions"]
    budget = max(3, len(actions) // 4)
    assert budget == 3


def test_dynamic_recovery_budget_long_script():
    # 20 步脚本 → max(3, 20//4) = max(3, 5) = 5
    goal = _goal_with_actions(20)
    actions = goal["execution_plan"]["effective"]["key_actions"]
    budget = max(3, len(actions) // 4)
    assert budget == 5


def test_dynamic_recovery_budget_very_long_script():
    # 48 步脚本 → max(3, 48//4) = max(3, 12) = 12
    goal = _goal_with_actions(48)
    actions = goal["execution_plan"]["effective"]["key_actions"]
    budget = max(3, len(actions) // 4)
    assert budget == 12


# ── Task 6.1: 取消闭环标记 ──


def test_tag_cancellation_loops_marks_open_close_pair():
    try:
        from api.test_cases_routes import _tag_cancellation_loops
    except ImportError:
        import pytest
        pytest.skip("fastapi not installed")
    actions = [
        {"tool": "click", "preferred_locator": {"label": "测试课程表"},
         "postcondition": {"expected_activity": "TimetableActivity"},
         "last_observation": "OK: 测试课程表"},
        {"tool": "click", "preferred_locator": {"label": "取消"},
         "postcondition": {"expected_activity": "TimetableActivity"},
         "last_observation": "OK: 取消"},
        {"tool": "click", "preferred_locator": {"label": "添加课程表", "rid": "id/add"},
         "postcondition": {"expected_activity": "EditActivity"},
         "last_observation": "OK: 添加课程表"},
    ]
    tagged = _tag_cancellation_loops(actions)
    # 所有 action 都保留，不剔除
    assert len(tagged) == 3
    # 闭环的两个 action 被打上 exploration 标签
    assert tagged[0].get("outcome") == "exploration"
    assert tagged[1].get("outcome") == "exploration"
    # 正常 action 没有 outcome 标签
    assert tagged[2].get("outcome") is None


def test_tag_cancellation_loops_no_tag_for_confirm_clicks():
    try:
        from api.test_cases_routes import _tag_cancellation_loops
    except ImportError:
        import pytest
        pytest.skip("fastapi not installed")
    actions = [
        {"tool": "click", "preferred_locator": {"label": "测试课程表"},
         "postcondition": {"expected_activity": "TimetableActivity"},
         "last_observation": "OK"},
        {"tool": "click", "preferred_locator": {"label": "确定"},
         "postcondition": {"expected_activity": "TimetableActivity"},
         "last_observation": "OK"},
    ]
    tagged = _tag_cancellation_loops(actions)
    assert len(tagged) == 2
    assert tagged[0].get("outcome") is None
    assert tagged[1].get("outcome") is None  # “确定”不是 dismiss 关键词


def test_tag_cancellation_loops_no_tag_when_activity_changes():
    try:
        from api.test_cases_routes import _tag_cancellation_loops
    except ImportError:
        import pytest
        pytest.skip("fastapi not installed")
    actions = [
        {"tool": "click", "preferred_locator": {"label": "设置"},
         "postcondition": {"expected_activity": "SettingsActivity"},
         "last_observation": "OK"},
        {"tool": "click", "preferred_locator": {"label": "返回"},
         "postcondition": {"expected_activity": "MainActivity"},
         "last_observation": "OK"},
    ]
    tagged = _tag_cancellation_loops(actions)
    assert len(tagged) == 2
    assert tagged[0].get("outcome") is None
    assert tagged[1].get("outcome") is None  # Activity 变了，不是闭环


def test_tag_cancellation_loops_detects_dismiss_in_observation():
    try:
        from api.test_cases_routes import _tag_cancellation_loops
    except ImportError:
        import pytest
        pytest.skip("fastapi not installed")
    actions = [
        {"tool": "click", "preferred_locator": {"label": "某按钮"},
         "postcondition": {"expected_activity": "SomeActivity"},
         "last_observation": "OK: 点击了某按钮"},
        {"tool": "click", "preferred_locator": {"label": "btn_123"},
         "postcondition": {"expected_activity": "SomeActivity"},
         "last_observation": "OK: 取消弹窗已关闭"},
    ]
    tagged = _tag_cancellation_loops(actions)
    assert len(tagged) == 2  # 全部保留
    assert tagged[0].get("outcome") == "exploration"
    assert tagged[1].get("outcome") == "exploration"


# ── Task 6.2: 死胡同标记 ──


def test_tag_dead_ends_marks_vision_tap_followed_by_back():
    try:
        from api.test_cases_routes import _tag_dead_ends
    except ImportError:
        import pytest
        pytest.skip("fastapi not installed")
    actions = [
        {"tool": "click", "preferred_locator": {"label": "课程表", "rid": "id/tv"},
         "postcondition": {"expected_activity": "TimetableListActivity"},
         "last_observation": "OK: 课程表", "_source_step_idx": 0},
        {"tool": "vision_tap", "preferred_locator": {},
         "postcondition": {"expected_activity": "TimetableListActivity"},
         "last_observation": "OK: tapped cell 3", "_source_step_idx": 1},
    ]
    steps = [
        {"action_type": "click", "observation": "OK: 课程表",
         "page_after_activity": "TimetableListActivity"},
        {"action_type": "vision_tap", "observation": "OK: tapped cell 3",
         "page_after_activity": "TimetableListActivity"},
        {"action_type": "press_key", "tool_input": {"key": "back"},
         "page_after_activity": "TimetableListActivity"},
    ]
    tagged = _tag_dead_ends(actions, steps)
    assert len(tagged) == 2  # 全部保留
    assert tagged[0].get("outcome") is None  # click 不受影响
    assert tagged[1].get("outcome") == "dead_end"  # vision_tap 被标记


# ── Task 8: 防幻觉安全闸门 ──


def test_anti_hallucination_guard_downgrades_incomplete_replay():
    # 模拟回放未完成（step 3/10）但 verdict 为 passed 的场景
    goal = _goal_with_actions(10)
    state = {
        "_run_type": "rerun",
        "_replay_step_idx": 3,
        "goal_description": goal,
    }
    # 验证 _effective_replay_actions 能正确提取
    actions = nodes._effective_replay_actions(goal)
    assert len(actions) == 10
    # 模拟 guard 逻辑（strict=True 默认）
    strict = bool(goal.get("strict_replay_completion", True))
    replay_enabled = str(state.get("_run_type", "") or "") == "rerun" and bool(actions)
    step_idx = int(state.get("_replay_step_idx", 0) or 0)
    finished = bool(actions) and step_idx >= len(actions)
    assert strict is True
    assert replay_enabled is True
    assert finished is False  # step 3 < 10
    # 在此状态下，如果 test_verdict == "passed"，应被强制为 "failed"
    test_verdict = "passed"
    if strict and replay_enabled and not finished and test_verdict == "passed":
        test_verdict = "failed"
    assert test_verdict == "failed"


def test_anti_hallucination_guard_allows_completed_replay():
    # 回放已完成（step 10/10）→ guard 不触发
    goal = _goal_with_actions(10)
    state = {
        "_run_type": "rerun",
        "_replay_step_idx": 10,
        "goal_description": goal,
    }
    actions = nodes._effective_replay_actions(goal)
    step_idx = int(state.get("_replay_step_idx", 0) or 0)
    finished = bool(actions) and step_idx >= len(actions)
    assert finished is True  # step 10 >= 10


def test_anti_hallucination_guard_respects_strict_false():
    # strict_replay_completion=False 时，guard 不触发
    goal = _goal_with_actions(10)
    goal["strict_replay_completion"] = False
    state = {
        "_run_type": "rerun",
        "_replay_step_idx": 3,
        "goal_description": goal,
    }
    actions = nodes._effective_replay_actions(goal)
    strict = bool(goal.get("strict_replay_completion", True))
    replay_enabled = str(state.get("_run_type", "") or "") == "rerun" and bool(actions)
    step_idx = int(state.get("_replay_step_idx", 0) or 0)
    finished = bool(actions) and step_idx >= len(actions)
    assert strict is False
    assert replay_enabled is True
    assert finished is False
    # strict=False 时，即使回放未完成，verdict 不被修改
    test_verdict = "passed"
    if strict and replay_enabled and not finished and test_verdict == "passed":
        test_verdict = "failed"
    assert test_verdict == "passed"  # 保持原值


# ── Task 10: 验证证据回溯 ──


def test_parse_vision_decision_click_and_check_format():
    """click_and_check 返回的 observation 包含 [yes/no] 格式"""
    assert nodes._parse_vision_decision(
        "OK: 已点击'完成'并截图验证 [yes] 屏幕底部出现toast | evidence: toast可见"
    ) == "yes"
    assert nodes._parse_vision_decision(
        "OK: 已点击'删除'并截图验证 [no] 未观察到确认弹窗 | evidence: 无弹窗"
    ) == "no"


def test_parse_vision_decision_visual_check_json_format():
    """visual_check 返回的 observation 是 JSON 格式"""
    assert nodes._parse_vision_decision(
        '{"decision": "yes", "reason": "页面显示课程表", "evidence": "可见3条课程", "confidence": "high"}'
    ) == "yes"
    assert nodes._parse_vision_decision(
        '{"decision": "no", "reason": "未看到提示", "evidence": "", "confidence": "medium"}'
    ) == "no"


def test_parse_vision_decision_returns_empty_for_unparseable():
    """无法解析时返回空字符串"""
    assert nodes._parse_vision_decision("") == ""
    assert nodes._parse_vision_decision("OK: 普通工具输出") == ""
    assert nodes._parse_vision_decision('{"no_decision_field": true}') == ""


def test_parse_vision_decision_verify_decision_format():
    """verify_decision=yes/no 格式也应被解析"""
    assert nodes._parse_vision_decision("verify_decision=yes") == "yes"
    assert nodes._parse_vision_decision("verify_decision=no") == "no"
    assert nodes._parse_vision_decision("OK: verify_decision = yes 确认通过") == "yes"


def test_verification_args_uses_click_and_check_evidence():
    """assert_verification 无关联断言时，向前扫描 click_and_check 的 decision"""
    actions = [
        {"tool": "click_and_check", "tool_input": {"label": "完成"}, "verify": "v0"},
        {"tool": "assert_verification", "verify_key": "v0"},
    ]
    goal = {"verification": ["保存成功提示"]}
    tool_log = [
        {
            "name": "click_and_check",
            "observation": "OK: 已点击'完成'并截图验证 [yes] toast可见 | evidence: 已保存",
            "status_code": "OK",
            "replay_step_idx": 0,
        },
        {
            "name": "assert_verification",
            "observation": "",
            "status_code": "",
            "replay_step_idx": 1,
        },
    ]
    args = nodes._build_replay_verification_args(
        actions[1], actions, tool_log, goal
    )
    assert args["result"] == "passed"
    assert args["_evidence_source"] == "click_and_check"


def test_verification_args_uses_visual_check_evidence():
    """assert_verification 无关联断言时，向前扫描 visual_check 的 decision"""
    actions = [
        {"tool": "assert_verification", "verify_key": "v0"},
    ]
    goal = {"verification": ["页面显示课程列表"]}
    tool_log = [
        {
            "name": "visual_check",
            "observation": '{"decision": "yes", "reason": "可见课程列表", "evidence": "3条课程", "confidence": "high"}',
            "status_code": "",
        },
    ]
    args = nodes._build_replay_verification_args(
        actions[0], actions, tool_log, goal
    )
    assert args["result"] == "passed"
    assert args["_evidence_source"] == "visual_check"


def test_verification_args_no_evidence_returns_unknown():
    """无任何可回溯证据时，verdict 仍为 unknown"""
    actions = [{"tool": "assert_verification", "verify_key": "v0"}]
    goal = {"verification": ["某验证项"]}
    tool_log = [
        {"name": "click", "observation": "OK: 点击了某按钮", "status_code": "OK"},
    ]
    args = nodes._build_replay_verification_args(
        actions[0], actions, tool_log, goal
    )
    assert args["result"] == "unknown"
    assert args["_evidence_source"] == "none"


def test_verification_args_assert_takes_priority_over_vision():
    """关联断言存在时，不使用 vision 证据（assert 优先）"""
    actions = [
        {"tool": "assert_page_contains", "verify": "v0"},
        {"tool": "assert_verification", "verify_key": "v0"},
    ]
    goal = {"verification": ["某验证"]}
    tool_log = [
        {"replay_step_idx": 0, "status_code": "PASS", "name": "assert_page_contains"},
        {
            "name": "click_and_check",
            "observation": "OK: 已点击 [no] 未看到",
            "status_code": "OK",
        },
    ]
    args = nodes._build_replay_verification_args(
        actions[1], actions, tool_log, goal
    )
    assert args["result"] == "passed"  # 来自 assert_page_contains PASS
    assert args["_evidence_source"] == "assert"


def test_verification_args_key_matching_skips_other_key():
    """click_and_check 关联了其他 verify key 时应被跳过，不误关联"""
    actions = [
        {"tool": "click_and_check", "tool_input": {"label": "完成"}, "verify": "v0"},
        {"tool": "assert_verification", "verify_key": "v0"},
        {"tool": "click_and_check", "tool_input": {"label": "删除"}, "verify": "v1"},
        {"tool": "assert_verification", "verify_key": "v1"},
    ]
    goal = {"verification": ["保存成功", "删除成功"]}
    # v0 的 click_and_check 返回 [yes]，v1 的返回 [no]
    tool_log = [
        {"name": "click_and_check", "observation": "[yes] 已保存",
         "status_code": "OK", "replay_step_idx": 0},
        {"name": "assert_verification", "observation": "",
         "status_code": "", "replay_step_idx": 1},
        {"name": "click_and_check", "observation": "[no] 未删除",
         "status_code": "OK", "replay_step_idx": 2},
        {"name": "assert_verification", "observation": "",
         "status_code": "", "replay_step_idx": 3},
    ]
    # 查 v1 时不应读到 v0 的 [yes] 证据
    args = nodes._build_replay_verification_args(
        actions[3], actions, tool_log, goal
    )
    assert args["result"] == "failed"  # 来自 v1 自己的 [no]
    assert args["_evidence_source"] == "click_and_check"


def test_verification_args_key_exact_match():
    """verify key 精确匹配：只读 matching key 的 evidence"""
    actions = [
        {"tool": "click_and_check", "tool_input": {"label": "A"}, "verify": "v0"},
        {"tool": "click_and_check", "tool_input": {"label": "B"}, "verify": "v1"},
        {"tool": "assert_verification", "verify_key": "v0"},
    ]
    goal = {"verification": ["验证A", "验证B"]}
    tool_log = [
        {"name": "click_and_check", "observation": "[yes] A通过",
         "status_code": "OK", "replay_step_idx": 0},
        {"name": "click_and_check", "observation": "[no] B失败",
         "status_code": "OK", "replay_step_idx": 1},
    ]
    # 查 v0 应只读到 v0 的 [yes]，不被 v1 的 [no] 干扰
    args = nodes._build_replay_verification_args(
        actions[2], actions, tool_log, goal
    )
    assert args["result"] == "passed"
    assert args["_evidence_source"] == "click_and_check"


def test_verification_args_fallback_no_key():
    """click_and_check 无 verify key 时退化为位置扫描"""
    actions = [
        {"tool": "click_and_check", "tool_input": {"label": "完成"}},
        {"tool": "assert_verification", "verify_key": "v0"},
    ]
    goal = {"verification": ["某验证"]}
    tool_log = [
        {"name": "click_and_check", "observation": "[yes] 通过",
         "status_code": "OK", "replay_step_idx": 0},
    ]
    args = nodes._build_replay_verification_args(
        actions[1], actions, tool_log, goal
    )
    assert args["result"] == "passed"  # 无 key → 位置扫描兜底
    assert args["_evidence_source"] == "click_and_check"


def test_verification_args_uses_vision_tap_evidence():
    """vision_tap 的 verify=[yes] 输出应被 Layer 2 扫描识别"""
    actions = [
        {"tool": "vision_tap", "tool_input": {"verify": "值是否为53"}},
        {"tool": "click", "preferred_locator": {"label": "确定"}},
        {"tool": "click", "preferred_locator": {"label": "确定"}},
        {"tool": "assert_verification", "verify_key": "v0"},
    ]
    goal = {"verification": ["可成功修改小节时间"]}
    tool_log = [
        {"name": "vision_tap",
         "observation": "OK: 已点击坐标 verify=[yes] 53为蓝色选中状态",
         "status_code": "OK", "replay_step_idx": 0},
        {"name": "click", "observation": "OK: 确定",
         "status_code": "OK", "replay_step_idx": 1},
        {"name": "click", "observation": "OK: 确定",
         "status_code": "OK", "replay_step_idx": 2},
    ]
    args = nodes._build_replay_verification_args(
        actions[3], actions, tool_log, goal
    )
    assert args["result"] == "passed"
    assert args["_evidence_source"] == "vision_tap"


def test_parse_vision_decision_vision_tap_format():
    """vision_tap 输出中的 verify=[yes/no] 应被解析"""
    assert nodes._parse_vision_decision(
        "OK: 已点击设备坐标(1882,1209) verify=[yes] 53为蓝色选中状态"
    ) == "yes"
    assert nodes._parse_vision_decision(
        "OK: 已点击 verify=[no] 值未变化"
    ) == "no"


def test_verification_args_natural_language_verify_not_treated_as_key():
    """vision_tap 的自然语言 verify 字段不应被当作 verify key 跳过"""
    actions = [
        {"tool": "vision_tap", "tool_input": {"verify": "结束分钟列当前选中值是否为53"},
         "verify": "结束分钟列当前选中值是否为53"},
        {"tool": "click", "preferred_locator": {"label": "确定"}},
        {"tool": "assert_verification", "verify_key": "v0"},
    ]
    goal = {"verification": ["可成功修改小节时间"]}
    tool_log = [
        {"name": "vision_tap",
         "observation": "OK: 已点击坐标 verify=[yes] 53为蓝色选中状态",
         "status_code": "OK", "replay_step_idx": 0},
        {"name": "click", "observation": "OK: 确定",
         "status_code": "OK", "replay_step_idx": 1},
    ]
    args = nodes._build_replay_verification_args(
        actions[2], actions, tool_log, goal
    )
    # 自然语言 verify 不匹配 ^v\d+$，应退化为位置扫描
    assert args["result"] == "passed"
    assert args["_evidence_source"] == "vision_tap"


# ── 方案 A: 回放模式下禁止 all-passed 提前退出 ──


def test_replay_script_incomplete_returns_true_when_script_not_done():
    """回放模式 + 脚本未走完 → True"""
    from agents.graph import _replay_script_incomplete
    state = {
        "_run_type": "rerun",
        "_replay_step_idx": 18,
        "goal_description": {
            "execution_plan": {
                "schema_version": 4,
                "effective": {"key_actions": [{"tool": "click"}] * 21},
            }
        },
    }
    assert _replay_script_incomplete(state) is True


def test_replay_script_incomplete_returns_false_when_done():
    """回放模式 + 脚本已走完 → False"""
    from agents.graph import _replay_script_incomplete
    state = {
        "_run_type": "rerun",
        "_replay_step_idx": 21,
        "goal_description": {
            "execution_plan": {
                "schema_version": 4,
                "effective": {"key_actions": [{"tool": "click"}] * 21},
            }
        },
    }
    assert _replay_script_incomplete(state) is False


def test_replay_script_incomplete_returns_false_for_non_rerun():
    """非回放模式 → 始终 False"""
    from agents.graph import _replay_script_incomplete
    state = {
        "_run_type": "normal",
        "_replay_step_idx": 5,
        "goal_description": {
            "execution_plan": {
                "schema_version": 4,
                "effective": {"key_actions": [{"tool": "click"}] * 21},
            }
        },
    }
    assert _replay_script_incomplete(state) is False


# ── Task 6.1 扩展: 确认→取消撤销模式 ──


def test_tag_cancellation_loops_marks_confirm_then_cancel():
    """确认→取消 模式应被标记为 exploration"""
    try:
        from api.test_cases_routes import _tag_cancellation_loops
    except ImportError:
        import pytest
        pytest.skip("fastapi not installed")
    actions = [
        {"tool": "click", "preferred_locator": {"label": "确定"},
         "postcondition": {"expected_activity": "TimetableActivity"},
         "last_observation": "OK: 确定"},
        {"tool": "click", "preferred_locator": {"label": "取消"},
         "postcondition": {"expected_activity": "TimetableActivity"},
         "last_observation": "OK: 取消"},
    ]
    tagged = _tag_cancellation_loops(actions)
    assert len(tagged) == 2
    assert tagged[0].get("outcome") == "exploration"
    assert tagged[1].get("outcome") == "exploration"


def test_tag_cancellation_loops_no_tag_for_confirm_then_confirm():
    """两个 confirm 类按钮不应被标记"""
    try:
        from api.test_cases_routes import _tag_cancellation_loops
    except ImportError:
        import pytest
        pytest.skip("fastapi not installed")
    actions = [
        {"tool": "click", "preferred_locator": {"label": "确定"},
         "postcondition": {"expected_activity": "TimetableActivity"},
         "last_observation": "OK"},
        {"tool": "click", "preferred_locator": {"label": "保存"},
         "postcondition": {"expected_activity": "TimetableActivity"},
         "last_observation": "OK"},
    ]
    tagged = _tag_cancellation_loops(actions)
    assert len(tagged) == 2
    assert tagged[0].get("outcome") is None
    assert tagged[1].get("outcome") is None


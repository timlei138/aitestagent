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


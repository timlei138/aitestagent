from __future__ import annotations

import json

from agents.plan_extractor import (
    build_plan_summary_text,
    build_user_request_template,
    environment_compatibility_score,
    environment_fingerprint,
    extract_action_semantics,
    extract_action_semantics_from_request,
    extract_candidate_plan,
    extract_parameter_slots,
    extract_verification_semantics,
    parameter_slots_compatible,
    task_signatures_compatible,
    verification_fingerprint,
    _task_signature_from_goal,
)
from data.relational import SqliteBackend


def test_extract_candidate_plan_uses_only_successful_business_actions(tmp_path):
    db = SqliteBackend(str(tmp_path / "plans.db"))
    contract = {"status": "approved", "verifications": []}
    plan_id = extract_candidate_plan(
        db,
        app_package="com.example.app",
        user_request="保存设置",
        verification_contract=contract,
        action_events=[
            {
                "tool_name": "get_screen_info",
                "status": "OK",
                "page_before": {"signature": "a"},
                "page_after": {"signature": "a"},
            },
            {
                "tool_name": "click",
                "status": "OK",
                "tool_input": {"label": "保存"},
                "resolved_locator": {"rid": "save"},
                "page_before": {"signature": "a"},
                "page_after": {
                    "signature": "b",
                    "package": "com.example.app",
                    "activity": "SavedActivity",
                },
            },
            {
                "tool_name": "click",
                "status": "ERROR",
                "tool_input": {"label": "取消"},
                "page_before": {"signature": "b"},
                "page_after": {"signature": "b"},
            },
        ],
        evidence_events=[
            {
                "verification_key": "v0",
                "clause_id": "v0.0",
                "status": "PASS",
            }
        ],
    )

    assert plan_id
    plan = db.select("execution_plans", {"plan_id": plan_id})[0]
    actions = db.select("plan_actions", {"plan_id": plan_id})
    assert plan["plan_trust"] == "candidate"
    assert plan["direct_approved"] == 0
    assert len(actions) == 1
    assert actions[0]["execution_eligibility"] == "direct_eligible"
    assert json.loads(actions[0]["verification_links_json"]) == [
        {"verification_key": "v0", "clause_id": "v0.0"}
    ]
    db._conn.close()


def test_extractor_requires_approved_contract(tmp_path):
    db = SqliteBackend(str(tmp_path / "plans.db"))
    plan_id = extract_candidate_plan(
        db,
        app_package="com.example.app",
        user_request="保存设置",
        verification_contract={"status": "contract_pending_review"},
        action_events=[{"tool_name": "click", "status": "OK"}],
        evidence_events=[],
    )

    assert plan_id is None
    assert db.count("execution_plans") == 0
    db._conn.close()


def test_plan_match_rejects_changed_verification_contract(tmp_path):
    db = SqliteBackend(str(tmp_path / "verification_match.db"))
    contract = {
        "status": "approved",
        "verifications": [
            {
                "statement": "保存成功",
                "clauses": [{"claim": "显示成功", "channels": ["ui_text"]}],
            }
        ],
    }
    plan_id = extract_candidate_plan(
        db,
        app_package="com.example.app",
        user_request="保存课程",
        verification_contract=contract,
        action_events=[{"tool_name": "click", "status": "OK"}],
        evidence_events=[],
    )

    assert plan_id is not None
    assert (
        db.find_matching_execution_plan(
            "com.example.app", "保存课程", verification_fingerprint(contract)
        )
        is not None
    )
    changed_contract = {
        **contract,
        "verifications": [
            {
                "statement": "保存成功",
                "clauses": [{"claim": "显示成功", "channels": ["vision_verify"]}],
            }
        ],
    }
    assert (
        db.find_matching_execution_plan(
            "com.example.app",
            "保存课程",
            verification_fingerprint(changed_contract),
        )
        is None
    )
    db._conn.close()


def test_plan_match_rejects_changed_environment(tmp_path):
    db = SqliteBackend(str(tmp_path / "environment_match.db"))
    contract = {"status": "approved", "verifications": []}
    plan_id = extract_candidate_plan(
        db,
        app_package="com.example.app",
        user_request="保存课程",
        verification_contract=contract,
        action_events=[
            {
                "tool_name": "click",
                "status": "OK",
                "page_before": {"package": "com.example.app", "activity": "Main"},
            }
        ],
        evidence_events=[],
    )

    assert plan_id is not None
    matching = environment_fingerprint(
        {"package": "com.example.app", "activity": "Main"}
    )
    changed = environment_fingerprint(
        {"package": "com.example.app", "activity": "Other"}
    )
    fingerprint = verification_fingerprint(contract)
    assert (
        db.find_matching_execution_plan(
            "com.example.app", "保存课程", fingerprint, matching
        )
        is not None
    )
    assert (
        db.find_matching_execution_plan(
            "com.example.app", "保存课程", fingerprint, changed
        )
        is None
    )
    db._conn.close()


def test_environment_fingerprint_includes_optional_version_and_fixture():
    base = {"package": "com.example.app", "activity": "Main"}
    versioned = {**base, "app_version": "2.0", "fixture_fingerprint": "clean"}

    assert environment_fingerprint(base) != environment_fingerprint(versioned)


def test_environment_fingerprint_reads_action_screen_profile():
    page = {
        "package": "com.example.app",
        "activity": "Main",
        "screen_profile": "1080x2400",
    }
    assert environment_fingerprint(page) == environment_fingerprint(page, "1080x2400")


def test_extract_parameter_slots_returns_empty_without_planner_slots():
    # Without planner slots, code does not invent parameters.
    slots = extract_parameter_slots("设置时长为50分钟")
    assert slots == []


def test_extract_parameter_slots_normalizes_planner_slots():
    goal = {
        "parameter_slots": [
            {
                "name": "lesson_duration",
                "type": "duration",
                "unit": "minute",
                "value": 50,
                "original": "50分钟",
            }
        ]
    }
    slots = extract_parameter_slots("设置时长为50分钟", goal)
    assert len(slots) == 1
    assert slots[0]["name"] == "lesson_duration"
    assert slots[0]["type"] == "duration"
    assert slots[0]["unit"] == "minute"
    assert slots[0]["value"] == 50
    assert slots[0]["original"] == "50分钟"


def test_extract_parameter_slots_rejects_positional_names():
    # Planner must emit semantic names, not MINUTE / MINUTE_1 style position labels.
    goal = {
        "parameter_slots": [
            {"name": "MINUTE", "type": "duration", "unit": "minute", "value": 50},
            {"name": "MINUTE_1", "type": "duration", "unit": "minute", "value": 10},
        ]
    }
    slots = extract_parameter_slots("设置时长为50分钟、课间休息10分钟", goal)
    assert slots == []


def test_extract_parameter_slots_keeps_semantic_names():
    goal = {
        "parameter_slots": [
            {"name": "LESSON_DURATION", "type": "duration", "unit": "minute", "value": 50},
        ]
    }
    slots = extract_parameter_slots("设置时长为50分钟", goal)
    assert len(slots) == 1
    assert slots[0]["name"] == "LESSON_DURATION"


def test_build_user_request_template_replaces_planner_originals():
    slots = [
        {"name": "lesson_duration", "value": 50, "original": "50分钟"},
    ]
    template = build_user_request_template("设置每节课时长为50分钟", slots)
    assert template == "设置每节课时长为{lesson_duration}"


def test_build_user_request_template_avoids_substring_collisions():
    # 50分钟 inside 150分钟 must not be replaced.
    slots = [{"name": "lesson_duration", "value": 50, "original": "50分钟"}]
    template = build_user_request_template("等待150分钟后设置50分钟", slots)
    assert template == "等待150分钟后设置{lesson_duration}"


def test_parameter_slots_compatible_same_value():
    q = [{"name": "MINUTE", "type": "duration", "unit": "minute", "value": 50}]
    c = [{"name": "MINUTE", "type": "duration", "unit": "minute", "value": 50}]
    assert parameter_slots_compatible(q, c) is True


def test_parameter_slots_reject_different_values():
    q = [{"name": "MINUTE", "type": "duration", "unit": "minute", "value": 50}]
    c = [{"name": "MINUTE", "type": "duration", "unit": "minute", "value": 54}]
    assert parameter_slots_compatible(q, c) is False


def test_parameter_slots_compatible_within_tolerance():
    q = [{"name": "MINUTE", "type": "duration", "unit": "minute", "value": 50}]
    c = [{"name": "MINUTE", "type": "duration", "unit": "minute", "value": 51}]
    assert parameter_slots_compatible(q, c) is True


def test_parameter_slots_reject_mismatched_names():
    q = [{"name": "lesson_duration", "type": "duration", "unit": "minute", "value": 50}]
    c = [{"name": "break_duration", "type": "duration", "unit": "minute", "value": 10}]
    assert parameter_slots_compatible(q, c) is False


def test_parameter_slots_reject_when_one_side_empty():
    q = [{"name": "lesson_duration", "type": "duration", "unit": "minute", "value": 50}]
    assert parameter_slots_compatible(q, []) is False
    assert parameter_slots_compatible([], q) is False
    assert parameter_slots_compatible([], []) is True


def test_extract_action_semantics_from_click():
    events = [
        {"tool_name": "click", "tool_input": {"label": "保存"}},
        {"tool_name": "type_input", "tool_input": {"label": "课程名"}},
    ]
    semantics = extract_action_semantics(events)
    assert "点击保存" in semantics
    assert "输入课程名" in semantics


def test_extract_action_semantics_from_request():
    goal = {"app_package": "com.example.app", "verification": ["保存成功"]}
    semantics = extract_action_semantics_from_request(
        "打开应用后点击保存并输入课程名，验证保存成功", goal
    )
    assert "启动应用com.example.app" in semantics
    assert any(s.startswith("点击") and "保存" in s for s in semantics)
    assert any(s.startswith("输入") and "课程名" in s for s in semantics)
    assert any(s.startswith("验证") for s in semantics)


def test_task_signature_uses_goal_action_semantics_when_no_events():
    goal = {
        "app_package": "com.example.app",
        "action_semantics": ["点击时长", "输入课程名"],
    }
    contract = {"status": "approved"}
    signature = _task_signature_from_goal("设置时长为50分钟", goal, contract, [])
    assert "点击时长" in signature["action_semantics"]
    assert "输入课程名" in signature["action_semantics"]


def test_task_signature_falls_back_to_request_rules():
    goal = {"app_package": "com.example.app"}
    contract = {"status": "approved"}
    signature = _task_signature_from_goal("点击保存按钮", goal, contract, [])
    assert any("保存" in s for s in signature["action_semantics"])


def test_environment_compatibility_score_rejects_package_mismatch():
    expected = environment_fingerprint(
        {"package": "com.a", "activity": "Main", "app_version": "1.0"}, "1080x2400"
    )
    actual = environment_fingerprint(
        {"package": "com.b", "activity": "Main", "app_version": "1.0"}, "1080x2400"
    )
    score = environment_compatibility_score(expected, actual)
    assert score["compatible"] is False
    assert score["score"] == 0.0
    assert "package_mismatch" in score["reasons"]


def test_environment_compatibility_score_penalizes_version_drift():
    expected = environment_fingerprint(
        {"package": "com.a", "activity": "Main", "app_version": "1.0"}, "1080x2400"
    )
    actual = environment_fingerprint(
        {"package": "com.a", "activity": "Main", "app_version": "2.0"}, "1080x2400"
    )
    score = environment_compatibility_score(expected, actual)
    assert score["compatible"] is True
    assert score["score"] == 0.8
    assert "app_version_drift" in score["reasons"]


def test_environment_compatibility_score_ignores_empty_optional_fields():
    expected = environment_fingerprint(
        {"package": "com.a", "activity": "Main"}, "1080x2400"
    )
    actual = environment_fingerprint(
        {"package": "com.a", "activity": "Main", "app_version": "2.0"}, "1080x2400"
    )
    score = environment_compatibility_score(expected, actual)
    assert score["compatible"] is True
    assert score["score"] == 1.0
    assert score["reasons"] == []


def test_extract_verification_semantics():
    contract = {
        "verifications": [
            {
                "statement": "保存成功",
                "clauses": [{"claim": "显示成功", "channels": ["ui_text"]}],
            }
        ]
    }
    semantics = extract_verification_semantics(contract)
    assert "保存成功" in semantics
    assert "显示成功" in semantics


def test_task_signatures_compatible_requires_verification_fingerprint():
    q = {"verification_fingerprint": "abc", "action_semantics": ["点击保存"]}
    c = {"verification_fingerprint": "def", "action_semantics": ["点击保存"]}
    assert task_signatures_compatible(q, c) is False


def test_task_signatures_compatible_rejects_action_mismatch():
    q = {
        "verification_fingerprint": "abc",
        "action_semantics": ["点击保存"],
        "parameter_slots": [],
    }
    c = {
        "verification_fingerprint": "abc",
        "action_semantics": ["滑动列表"],
        "parameter_slots": [],
    }
    assert task_signatures_compatible(q, c) is False


def test_task_signatures_compatible_accepts_matching_signature():
    q = {
        "verification_fingerprint": "abc",
        "action_semantics": ["点击保存", "输入课程名"],
        "parameter_slots": [],
    }
    c = {
        "verification_fingerprint": "abc",
        "action_semantics": ["点击保存", "输入课程名", "点击完成"],
        "parameter_slots": [],
    }
    assert task_signatures_compatible(q, c) is True


def test_build_plan_summary_text():
    text = build_plan_summary_text(
        "com.example.app",
        ["点击保存"],
        ["显示成功"],
        [{"name": "MINUTE", "type": "duration", "unit": "minute"}],
        "设置时长为{MINUTE}分钟",
    )
    assert "com.example.app" in text
    assert "点击保存" in text
    assert "显示成功" in text
    assert "MINUTE=duration(minute)" in text


def test_verification_fingerprint_is_templated_with_parameter_slots():
    contract = {
        "status": "approved",
        "verifications": [
            {
                "statement": "设置每节课50分钟",
                "clauses": [{"claim": "显示50分钟", "channels": ["ui_text"]}],
            }
        ],
    }
    slots = [
        {"name": "lesson_duration", "type": "duration", "unit": "minute", "value": 50, "original": "50分钟"}
    ]
    templated = verification_fingerprint(contract, slots)
    raw = verification_fingerprint(contract)
    assert templated != raw
    # Same semantic contract with a different concrete value should yield the same fingerprint.
    contract_53 = {
        "status": "approved",
        "verifications": [
            {
                "statement": "设置每节课53分钟",
                "clauses": [{"claim": "显示53分钟", "channels": ["ui_text"]}],
            }
        ],
    }
    slots_53 = [
        {"name": "lesson_duration", "type": "duration", "unit": "minute", "value": 53, "original": "53分钟"}
    ]
    assert verification_fingerprint(contract_53, slots_53) == templated


def test_verification_fingerprint_without_slots_uses_raw_text():
    contract = {
        "status": "approved",
        "verifications": [
            {
                "statement": "保存成功",
                "clauses": [{"claim": "显示成功", "channels": ["ui_text"]}],
            }
        ],
    }
    fp1 = verification_fingerprint(contract)
    fp2 = verification_fingerprint(contract, [])
    assert fp1 == fp2


def test_extract_candidate_plan_writes_semantic_task_signature(tmp_path):
    db = SqliteBackend(str(tmp_path / "plans.db"))
    contract = {
        "status": "approved",
        "verifications": [
            {
                "statement": "时长设置生效",
                "clauses": [{"claim": "显示50分钟", "channels": ["ui_text"]}],
            }
        ],
    }
    plan_id = extract_candidate_plan(
        db,
        app_package="com.example.app",
        user_request="设置时长为50分钟",
        goal={
            "goal": "设置时长",
            "verification": ["时长设置生效"],
            "parameter_slots": [
                {
                    "name": "lesson_duration",
                    "type": "duration",
                    "unit": "minute",
                    "value": 50,
                    "original": "50分钟",
                }
            ],
        },
        verification_contract=contract,
        action_events=[
            {
                "tool_name": "click",
                "status": "OK",
                "tool_input": {"label": "时长"},
                "resolved_locator": {"rid": "duration"},
                "page_before": {"package": "com.example.app", "activity": "Main"},
                "page_after": {
                    "package": "com.example.app",
                    "activity": "Main",
                },
            }
        ],
        evidence_events=[],
    )

    assert plan_id
    plan = db.select("execution_plans", {"plan_id": plan_id})[0]
    signature = json.loads(plan["task_signature_json"])
    assert signature["user_request_template"] == "设置时长为{lesson_duration}"
    assert any(
        s["name"] == "lesson_duration" and s["value"] == 50
        for s in signature["parameter_slots"]
    )
    # Verification fingerprint must be computed on templated text so that
    # "50分钟" and "53分钟" share the same semantic fingerprint.
    assert "50" not in signature["verification_fingerprint"]
    assert "lesson_duration" not in signature["verification_fingerprint"]
    raw_fp = verification_fingerprint(contract)
    assert signature["verification_fingerprint"] != raw_fp
    assert "点击时长" in signature["action_semantics"]
    assert "时长设置生效" in signature["verification_semantics"]
    db._conn.close()

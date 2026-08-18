from __future__ import annotations

import json

from agents.plan_extractor import (
    environment_fingerprint,
    extract_candidate_plan,
    verification_fingerprint,
)
from data.relational import SqliteBackend


def test_find_matching_execution_plan_semantic_match_with_parameter_slots(tmp_path):
    db = SqliteBackend(str(tmp_path / "semantic_match.db"))
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
                "page_after": {"package": "com.example.app", "activity": "Main"},
            }
        ],
        evidence_events=[],
    )
    assert plan_id is not None

    # Use the candidate plan's templated fingerprint so that parameter values
    # in verification text do not break matching.
    plan = db.get_full_execution_plan(plan_id)
    templated_fp = plan["task_signature"]["verification_fingerprint"]

    query_signature = {
        "user_request_template": "设置时长为{lesson_duration}",
        "action_semantics": ["点击时长"],
        "verification_semantics": ["时长设置生效", "显示50分钟"],
        "parameter_slots": [
            {
                "name": "lesson_duration",
                "type": "duration",
                "unit": "minute",
                "value": 50,
            }
        ],
        "verification_fingerprint": templated_fp,
    }

    matched = db.find_matching_execution_plan(
        "com.example.app",
        task_signature=query_signature,
    )
    assert matched is not None
    assert matched["plan_id"] == plan_id


def test_find_matching_execution_plan_rejects_incompatible_parameter_value(tmp_path):
    db = SqliteBackend(str(tmp_path / "param_reject.db"))
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
                "page_after": {"package": "com.example.app", "activity": "Main"},
            }
        ],
        evidence_events=[],
    )
    assert plan_id is not None

    plan = db.get_full_execution_plan(plan_id)
    templated_fp = plan["task_signature"]["verification_fingerprint"]

    query_signature = {
        "user_request_template": "设置时长为{lesson_duration}",
        "action_semantics": ["点击时长"],
        "verification_semantics": ["时长设置生效"],
        "parameter_slots": [
            {
                "name": "lesson_duration",
                "type": "duration",
                "unit": "minute",
                "value": 54,
            }
        ],
        "verification_fingerprint": templated_fp,
    }

    matched = db.find_matching_execution_plan(
        "com.example.app",
        task_signature=query_signature,
    )
    assert matched is None


def test_find_matching_execution_plan_accepts_compatible_parameter_value(tmp_path):
    """A plan recorded with 50分钟 should be reusable for a 53分钟 request
    because the verification fingerprint is computed on templated text and
    parameter_slots_compatible allows values within the tolerance window."""
    db = SqliteBackend(str(tmp_path / "param_compatible.db"))
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
                "page_after": {"package": "com.example.app", "activity": "Main"},
            }
        ],
        evidence_events=[],
    )
    assert plan_id is not None

    plan = db.get_full_execution_plan(plan_id)
    templated_fp = plan["task_signature"]["verification_fingerprint"]

    query_signature = {
        "user_request_template": "设置时长为{lesson_duration}",
        "action_semantics": ["点击时长"],
        "verification_semantics": ["时长设置生效"],
        "parameter_slots": [
            {
                "name": "lesson_duration",
                "type": "duration",
                "unit": "minute",
                "value": 53,
            }
        ],
        "verification_fingerprint": templated_fp,
    }

    matched = db.find_matching_execution_plan(
        "com.example.app",
        task_signature=query_signature,
    )
    assert matched is not None
    assert matched["plan_id"] == plan_id


def test_find_matching_execution_plan_legacy_exact_match_fallback(tmp_path):
    db = SqliteBackend(str(tmp_path / "legacy_match.db"))
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
                "page_after": {"package": "com.example.app", "activity": "Main"},
            }
        ],
        evidence_events=[],
    )
    assert plan_id is not None

    matched = db.find_matching_execution_plan(
        "com.example.app", "保存课程", verification_fingerprint(contract)
    )
    assert matched is not None
    assert matched["plan_id"] == plan_id


def test_get_full_execution_plan_reads_actions(tmp_path):
    db = SqliteBackend(str(tmp_path / "full_plan.db"))
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
                "tool_input": {"label": "保存"},
                "resolved_locator": {"rid": "save"},
                "page_before": {"package": "com.example.app", "activity": "Main"},
                "page_after": {"package": "com.example.app", "activity": "Saved"},
            }
        ],
        evidence_events=[],
    )

    full = db.get_full_execution_plan(plan_id)
    assert full is not None
    assert full["plan_id"] == plan_id
    assert "task_signature" in full
    assert len(full["actions"]) == 1
    assert json.loads(full["actions"][0]["tool_input_json"]).get("label") == "保存"

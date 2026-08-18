from __future__ import annotations

import json

from data.relational import SqliteBackend, evaluate_action_alignment


def test_phase_one_schema_is_created(tmp_path):
    db = SqliteBackend(str(tmp_path / "phase_one.db"))
    tables = {
        row["name"]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert {
        "execution_plans",
        "plan_actions",
        "locator_knowledge",
        "execution_runs",
        "action_events",
        "evidence_events",
        "mode_transition_events",
        "plan_action_alignment_events",
        "curated_rule_migration_audit",
    } <= tables
    db._conn.close()


def test_read_and_delete_execution_report(tmp_path):
    db = SqliteBackend(str(tmp_path / "execution_report.db"))
    run_id = "run-report-1"
    db.record_execution_run(
        run_id=run_id,
        user_request="验证提交结果",
        app_package="com.example.app",
        goal={"goal": "提交", "verification": ["页面显示完成"]},
        verification_contract={"status": "approved", "clauses": []},
        execution_mode="explore",
        verdict="passed",
        terminal_reason="contract passed",
    )
    db.record_action_events(
        run_id,
        [{"action_index": 0, "tool_name": "click", "status": "OK"}],
    )
    db.record_evidence_events(
        run_id,
        [{"verification_key": "v0", "status": "PASS", "fact": {"text": "完成"}}],
    )

    assert db.list_execution_runs(limit=5)[0]["run_id"] == run_id
    report = db.get_execution_run(run_id)
    assert report is not None
    assert report["goal"]["goal"] == "提交"
    assert report["actions"][0]["tool_name"] == "click"
    assert report["evidence"][0]["fact"] == {"text": "完成"}
    assert db.delete_execution_run(run_id) is True
    assert db.get_execution_run(run_id) is None


def test_record_execution_run_and_evidence_events(tmp_path):
    db = SqliteBackend(str(tmp_path / "execution.db"))
    db.record_execution_run(
        run_id="run-v2-1",
        user_request="验证当前页面",
        app_package="com.example.app",
        goal={"verification": ["页面包含完成"]},
        verification_contract={"status": "approved", "verifications": []},
        verdict="passed",
        terminal_reason="DONE: contract passed",
    )
    db.record_evidence_events(
        "run-v2-1",
        [
            {
                "verification_key": "v0",
                "clause_id": "v0.0",
                "channel": "ui_text",
                "status": "PASS",
                "fact": {"text": "完成"},
            }
        ],
    )
    db.record_action_events(
        "run-v2-1",
        [
            {
                "action_index": 0,
                "tool_name": "click",
                "tool_input": {"label": "完成"},
                "resolved_locator": {"rid": "submit"},
                "page_before": {"signature": "before"},
                "page_after": {"signature": "after"},
                "status": "OK",
                "execution_mode": "explore",
            }
        ],
    )
    db.record_mode_transition_events(
        "run-v2-1",
        [
            {
                "from": "guided",
                "to": "explore",
                "reason": "guided_action_failed",
                "step_index": 3,
            }
        ],
    )

    run = db.select("execution_runs", {"run_id": "run-v2-1"})[0]
    evidence = db.select("evidence_events", {"run_id": "run-v2-1"})[0]
    action = db.select("action_events", {"run_id": "run-v2-1"})[0]
    assert run["execution_mode"] == "explore"
    assert run["verdict"] == "passed"
    assert json.loads(evidence["fact_json"]) == {"text": "完成"}
    assert json.loads(action["tool_input_json"]) == {"label": "完成"}
    assert json.loads(action["page_after_json"]) == {"signature": "after"}
    report = db.get_execution_run("run-v2-1")
    assert report is not None
    assert report["mode_transitions"] == [
        {
            "from_mode": "guided",
            "to_mode": "explore",
            "reason": "guided_action_failed",
            "step_index": 3,
            "created_at": report["mode_transitions"][0]["created_at"],
        }
    ]
    db._conn.close()


def test_get_execution_run_action_intent_and_screenshot(tmp_path):
    db = SqliteBackend(str(tmp_path / "action-intent.db"))
    db.record_execution_run(
        run_id="run-action-1",
        user_request="验证",
        app_package="com.example.app",
        goal={},
        verification_contract={},
        verdict="passed",
    )
    db.record_action_events(
        "run-action-1",
        [
            {
                "action_index": 0,
                "tool_name": "click",
                "tool_input": {"label": "WLAN 开关"},
                "resolved_locator": {"rid": "wifi_switch"},
                "page_before": {},
                "page_after": {},
                "status": "success",
                "intent_text": "打开 WLAN 开关",
                "screenshot_path": "screenshots/run-action-1/step_0.png",
                "execution_mode": "direct",
            }
        ],
    )
    report = db.get_execution_run("run-action-1")
    assert report["actions"][0]["intent"] == "打开 WLAN 开关"
    assert report["actions"][0]["screenshot"] == "screenshots/run-action-1/step_0.png"
    db._conn.close()


def test_record_evidence_events_channel_roundtrip(tmp_path):
    db = SqliteBackend(str(tmp_path / "ev-channel.db"))
    db.record_execution_run(
        run_id="run-channel-1",
        user_request="验证",
        app_package="com.example.app",
        goal={},
        verification_contract={},
        verdict="passed",
    )
    db.record_evidence_events(
        "run-channel-1",
        [
            {"verification_key": "v0", "clause_id": "v0.0", "channel": "ui_text", "status": "PASS"},
            {"verification_key": "v0", "clause_id": "v0.0", "channel": "vision_verify", "status": "YES"},
            {"verification_key": "v1", "clause_id": "v1.0", "channel": "element_state", "status": "PASS"},
        ],
    )
    report = db.get_execution_run("run-channel-1")
    channels = [e["channel"] for e in report["evidence"]]
    assert set(channels) == {"ui_text", "vision_verify", "element_state"}
    db._conn.close()


def test_locator_knowledge_prefers_page_and_falls_back_by_alias(tmp_path):
    db = SqliteBackend(str(tmp_path / "locator_knowledge.db"))
    common = {"app_package": "com.example.app", "screen_profile": "1080x2400"}
    db.save_locator_knowledge(
        **common,
        page_signature="settings",
        alias="Wi-Fi",
        locator={"rid": "wifi_toggle"},
        identity={"resource_id": "wifi_toggle", "role": "switch", "region": "top"},
    )
    db.save_locator_knowledge(
        **common,
        page_signature="home",
        alias="Search",
        locator={"rid": "search"},
        identity={"resource_id": "search", "role": "button", "region": "top"},
    )

    same_page = db.query_locator_knowledge(
        "com.example.app", alias="Wi-Fi", page_signature="settings"
    )
    fallback = db.query_locator_knowledge(
        "com.example.app", alias="Search", page_signature="settings"
    )

    assert same_page[0]["resource_id"] == "wifi_toggle"
    assert same_page[0]["page_signature"] == "settings"
    assert fallback[0]["resource_id"] == "search"
    assert fallback[0]["page_signature"] == "home"
    db._conn.close()


def test_plan_action_outcomes_update_only_aligned_actions(tmp_path):
    db = SqliteBackend(str(tmp_path / "action_quality.db"))
    now = "2026-08-13T00:00:00"
    db.insert(
        "plan_actions",
        {
            "action_id": "action-1",
            "plan_id": "plan-1",
            "action_index": 0,
            "tool_name": "click",
            "precondition_json": '{"package":"com.example.app","activity":"Before"}',
            "postcondition_json": '{"package":"com.example.app","activity":"After"}',
            "created_at": now,
            "updated_at": now,
        },
    )
    db.record_plan_action_outcomes(
        "plan-1",
        [
            {
                "action_index": 0,
                "tool_name": "click",
                "status": "OK",
                "page_before": {"package": "com.example.app", "activity": "Before"},
                "page_after": {"package": "com.example.app", "activity": "After"},
            },
            {"action_index": 1, "tool_name": "click", "status": "NOT_FOUND"},
        ],
    )

    action = db.select("plan_actions", {"action_id": "action-1"})[0]
    assert action["attempt_count"] == 1
    assert action["success_count"] == 1
    assert action["not_found_count"] == 0
    assert action["postcondition_pass_count"] == 1
    # Plan 4.4 Laplace smoothing: one success yields moderate confidence (~0.244),
    # not the pre-smoothing 0.444 that assumed zero-failure rates with no prior.
    assert 0.2 < action["quality_score"] < 0.3
    db._conn.close()


def test_plan_promotes_to_trusted_after_two_quality_successes(tmp_path):
    db = SqliteBackend(str(tmp_path / "plan_trust.db"))
    now = "2026-08-13T00:00:00"
    db.insert(
        "execution_plans",
        {
            "plan_id": "plan-1",
            "app_package": "com.example.app",
            "task_signature_json": "{}",
            "created_at": now,
            "updated_at": now,
        },
    )
    db.insert(
        "plan_actions",
        {
            "action_id": "action-1",
            "plan_id": "plan-1",
            "action_index": 0,
            "tool_name": "click",
            "quality_score": 0.8,
            "created_at": now,
            "updated_at": now,
        },
    )

    db.record_execution_run(
        run_id="run-1",
        user_request="save",
        app_package="com.example.app",
        goal={},
        verification_contract={},
        plan_id="plan-1",
        verdict="passed",
    )
    db.record_execution_plan_outcome("plan-1", "passed", "run-1")
    db.record_execution_plan_outcome("plan-1", "passed", "run-1")
    assert (
        db.select("execution_plans", {"plan_id": "plan-1"})[0]["plan_trust"]
        == "candidate"
    )
    db.record_execution_run(
        run_id="run-2",
        user_request="save",
        app_package="com.example.app",
        goal={},
        verification_contract={},
        plan_id="plan-1",
        verdict="passed",
    )
    db.record_execution_plan_outcome("plan-1", "passed", "run-2")
    plan = db.select("execution_plans", {"plan_id": "plan-1"})[0]
    assert plan["success_count"] == 2
    assert plan["plan_trust"] == "trusted"
    assert plan["direct_approved"] == 0
    db._conn.close()


def test_plan_alignment_records_deviation_reason(tmp_path):
    db = SqliteBackend(str(tmp_path / "plan_alignment.db"))
    now = "2026-08-14T00:00:00"
    db.insert(
        "plan_actions",
        {
            "action_id": "action-1",
            "plan_id": "plan-1",
            "action_index": 0,
            "tool_name": "click",
            "tool_input_json": '{"label":"save"}',
            "created_at": now,
            "updated_at": now,
        },
    )
    db.record_plan_action_alignment_events(
        "run-1",
        "plan-1",
        [{"action_index": 0, "tool_name": "click", "tool_input": {"label": "cancel"}}],
    )

    event = db.select("plan_action_alignment_events", {"run_id": "run-1"})[0]
    assert event["alignment"] == "deviated"
    assert event["deviation_reason"] == "tool_input_mismatch"
    db._conn.close()


def test_postcondition_failure_count_is_tracked(tmp_path):
    db = SqliteBackend(str(tmp_path / "postcondition_failure.db"))
    now = "2026-08-13T00:00:00"
    db.insert(
        "plan_actions",
        {
            "action_id": "action-1",
            "plan_id": "plan-1",
            "action_index": 0,
            "tool_name": "click",
            "precondition_json": '{"package":"com.example.app","activity":"Before"}',
            "postcondition_json": '{"package":"com.example.app","activity":"After"}',
            "created_at": now,
            "updated_at": now,
        },
    )
    db.record_plan_action_outcomes(
        "plan-1",
        [
            {
                "action_index": 0,
                "tool_name": "click",
                "status": "ERROR",
                "page_before": {"package": "com.example.app", "activity": "Before"},
                "page_after": {"package": "com.example.app", "activity": "Before"},
            }
        ],
    )
    action = db.select("plan_actions", {"action_id": "action-1"})[0]
    assert action["postcondition_failure_count"] == 1
    db._conn.close()


def test_quality_score_applies_age_decay(tmp_path):
    db = SqliteBackend(str(tmp_path / "age_decay.db"))
    now = "2026-08-13T00:00:00"
    old_success = "2026-01-01T00:00:00"
    db.insert(
        "plan_actions",
        {
            "action_id": "action-1",
            "plan_id": "plan-1",
            "action_index": 0,
            "tool_name": "click",
            "precondition_json": '{"package":"com.example.app","activity":"Before"}',
            "postcondition_json": '{"package":"com.example.app","activity":"After"}',
            "last_success_at": old_success,
            "created_at": now,
            "updated_at": now,
        },
    )
    db.record_plan_action_outcomes(
        "plan-1",
        [
            {
                "action_index": 0,
                "tool_name": "click",
                "status": "NOT_FOUND",
                "page_before": {"package": "com.example.app", "activity": "Before"},
                "page_after": {"package": "com.example.app", "activity": "Before"},
            }
        ],
        decay_lambda=0.01,
    )
    action = db.select("plan_actions", {"action_id": "action-1"})[0]
    # last_success_at stays old, so age decay applies to the stale success.
    assert action["last_success_at"] == old_success
    # Without age decay, quality would be ~0.111. With ~220 days of decay it should be near zero.
    assert action["quality_score"] < 0.05
    db._conn.close()


def test_timeout_not_found_update_quality(tmp_path):
    db = SqliteBackend(str(tmp_path / "timeout_quality.db"))
    now = "2026-08-13T00:00:00"
    db.insert(
        "plan_actions",
        {
            "action_id": "action-1",
            "plan_id": "plan-1",
            "action_index": 0,
            "tool_name": "click",
            "precondition_json": '{"package":"com.example.app","activity":"Before"}',
            "postcondition_json": '{"package":"com.example.app","activity":"After"}',
            "created_at": now,
            "updated_at": now,
        },
    )
    db.record_plan_action_outcomes(
        "plan-1",
        [
            {
                "action_index": 0,
                "tool_name": "click",
                "status": "TIMEOUT",
                "page_before": {"package": "com.example.app", "activity": "Before"},
                "page_after": {"package": "com.example.app", "activity": "Before"},
            }
        ],
    )
    action = db.select("plan_actions", {"action_id": "action-1"})[0]
    assert action["timeout_count"] == 1
    assert action["quality_score"] < 0.2
    db._conn.close()


def test_direct_approval_cleared_on_low_quality(tmp_path):
    db = SqliteBackend(str(tmp_path / "direct_kill_switch.db"))
    now = "2026-08-13T00:00:00"
    db.insert(
        "execution_plans",
        {
            "plan_id": "plan-1",
            "app_package": "com.example.app",
            "task_signature_json": "{}",
            "direct_approved": 1,
            "created_at": now,
            "updated_at": now,
        },
    )
    db.insert(
        "plan_actions",
        {
            "action_id": "action-1",
            "plan_id": "plan-1",
            "action_index": 0,
            "tool_name": "click",
            "quality_score": 0.3,
            "created_at": now,
            "updated_at": now,
        },
    )
    db.record_execution_run(
        run_id="run-1",
        user_request="save",
        app_package="com.example.app",
        goal={},
        verification_contract={},
        plan_id="plan-1",
        verdict="passed",
    )
    db.record_execution_plan_outcome(
        "plan-1", "passed", "run-1", direct_quality_threshold=0.5
    )
    plan = db.select("execution_plans", {"plan_id": "plan-1"})[0]
    assert plan["direct_approved"] == 0
    db._conn.close()


def test_missing_page_facts_are_unknown_not_postcondition_pass(tmp_path):
    db = SqliteBackend(str(tmp_path / "missing_page_facts.db"))
    now = "2026-08-14T00:00:00"
    db.insert(
        "plan_actions",
        {
            "action_id": "action-1",
            "plan_id": "plan-1",
            "action_index": 0,
            "tool_name": "click",
            "precondition_json": "{}",
            "postcondition_json": "{}",
            "created_at": now,
            "updated_at": now,
        },
    )
    event = {"action_index": 0, "tool_name": "click", "status": "OK"}
    db.record_plan_action_outcomes("plan-1", [event])
    db.record_plan_action_alignment_events("run-1", "plan-1", [event])

    action = db.select("plan_actions", {"action_id": "action-1"})[0]
    alignment = db.select("plan_action_alignment_events", {"run_id": "run-1"})[0]
    assert action["postcondition_pass_count"] == 0
    assert alignment["alignment"] == "unknown"
    assert alignment["deviation_reason"] == "missing_precondition_facts"
    db._conn.close()


def test_evaluate_action_alignment_has_three_states():
    action = {
        "tool_name": "click",
        "tool_input_json": '{"label":"save"}',
        "precondition_json": '{"package":"com.example.app","activity":"Main"}',
        "locator_json": '{"rid":"save"}',
    }
    event = {
        "tool_name": "click",
        "tool_input": {"label": "save"},
        "page_before": {"package": "com.example.app", "activity": "Main"},
        "resolved_locator": {"rid": "save"},
    }
    assert evaluate_action_alignment(action, event)["alignment"] == "aligned"
    assert (
        evaluate_action_alignment({**action, "precondition_json": "{}"}, event)[
            "alignment"
        ]
        == "unknown"
    )
    assert (
        evaluate_action_alignment(
            action, {**event, "resolved_locator": {"rid": "other"}}
        )["reason"]
        == "locator_mismatch"
    )


def test_direct_approval_requires_explicit_backend_call(tmp_path):
    db = SqliteBackend(str(tmp_path / "direct_approval.db"))
    now = "2026-08-14T00:00:00"
    db.insert(
        "execution_plans",
        {
            "plan_id": "plan-1",
            "app_package": "com.example.app",
            "task_signature_json": "{}",
            "created_at": now,
            "updated_at": now,
        },
    )
    assert db.set_direct_approval("missing", True) is False
    assert db.set_direct_approval("plan-1", True) is True
    assert (
        db.select("execution_plans", {"plan_id": "plan-1"})[0]["direct_approved"] == 1
    )
    db._conn.close()

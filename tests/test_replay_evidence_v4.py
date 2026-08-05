from __future__ import annotations

import copy
import json

from api import test_cases_routes as routes
from data.relational import SqliteBackend


def _base() -> dict:
    return {
        "base_revision": 1,
        "entry": None,
        "pre_entry": None,
        "key_actions": [{"tool": "click", "preferred_locator": {"label": "Old"}}],
        "verification_evidence": {},
    }


def _goal() -> dict:
    base = _base()
    return {
        "goal": "test",
        "execution_plan": {
            "schema_version": 4,
            "base_evidence": base,
            "override": {
                "revision": 0,
                "patch": [],
                "changed_paths": [],
                "evidence_stale": False,
            },
            "effective": routes.derive_effective_plan(base, [], effective_revision=1),
        },
    }


def _db_case(db: SqliteBackend) -> str:
    return db.create_test_case(
        name="v4", source_run_id="source-run", goal_json=json.dumps(_goal())
    )


def test_test_runs_v4_ddl_and_persistence(tmp_path):
    db = SqliteBackend(str(tmp_path / "db.sqlite"))
    columns = {row[1] for row in db._conn.execute("PRAGMA table_info(test_runs)")}
    assert {"source_case_id", "execution_plan_revision"} <= columns
    assert "environment_json" not in columns
    db.record_test_run(
        "r",
        "u",
        "p",
        "a",
        "success",
        "DONE",
        [],
        source_run_id="origin",
        source_case_id="case",
        execution_plan_revision=7,
    )
    assert db.get_test_run("r")["source_case_id"] == "case"
    assert db.list_test_runs()[0]["execution_plan_revision"] == 7


def test_extract_and_create_materializes_effective(tmp_path):
    db = SqliteBackend(str(tmp_path / "db.sqlite"))
    routes.set_backends(None, db)
    run_id = "source"
    structured = [
        {
            "action_type": "click",
            "status_code": "OK",
            "tool_input": {"label": "WLAN"},
            "resolved_target": {"label": "WLAN"},
            "page_before_activity": "A",
            "page_after_activity": "B",
            "observation": "clicked",
        }
    ]
    db.record_test_run(
        run_id,
        "request",
        "pkg",
        "app",
        "success",
        "DONE",
        structured,
        execution_status="completed",
        test_verdict="passed",
        goal_json=json.dumps({"verification": []}),
    )
    response = routes.create_test_case({"run_id": run_id})
    case = db.get_test_case(response["data"]["id"])
    plan = json.loads(case["goal_json"])["execution_plan"]
    assert response["data"]["has_replay_evidence"] is True
    assert plan["effective"]["effective_revision"] == 1
    assert plan["effective"]["key_actions"][0]["preferred_locator"]["label"] == "WLAN"


def test_patch_valid_invalid_and_compare_and_swap(tmp_path):
    db = SqliteBackend(str(tmp_path / "db.sqlite"))
    routes.set_backends(None, db)
    case_id = _db_case(db)
    payload = {
        "expected_effective_revision": 1,
        "override_patch": [
            {
                "op": "replace",
                "path": "/key_actions/0/preferred_locator/label",
                "value": "New",
            }
        ],
        "changed_paths": ["key_actions[0].preferred_locator.label"],
        "edited_by": "tester",
    }
    response = routes.patch_test_case(case_id, copy.deepcopy(payload))
    assert response["status"] == "ok"
    plan = json.loads(db.get_test_case(case_id)["goal_json"])["execution_plan"]
    assert (
        plan["base_evidence"]["key_actions"][0]["preferred_locator"]["label"] == "Old"
    )
    assert plan["effective"]["key_actions"][0]["preferred_locator"]["label"] == "New"
    assert plan["effective"]["effective_revision"] == 2
    assert routes.patch_test_case(case_id, payload)["status"] == "conflict"
    bad = copy.deepcopy(payload)
    bad["expected_effective_revision"] = 2
    bad["override_patch"] = [{"op": "replace", "path": "/execution_plan/x", "value": 1}]
    assert routes.patch_test_case(case_id, bad)["status"] == "error"
    forbidden = copy.deepcopy(payload)
    forbidden["expected_effective_revision"] = 2
    forbidden["goal_json"] = {"execution_plan": {}}
    assert routes.patch_test_case(case_id, forbidden)["status"] == "error"


def test_json_pointer_rejects_invalid_type_and_supports_array_append():
    target = {"key_actions": []}
    routes.apply_json_patch(
        target, [{"op": "add", "path": "/key_actions/-", "value": {"tool": "click"}}]
    )
    assert target["key_actions"][0]["tool"] == "click"
    for patch in (
        [{"op": "replace", "path": "/key_actions/1", "value": {}}],
        [{"op": "remove", "path": "/key_actions/-"}],
        [{"op": "add", "path": "/key_actions//x", "value": 1}],
        [{"op": "replace", "path": "/unknown/x", "value": 1}],
    ):
        try:
            routes.apply_json_patch(copy.deepcopy(target), patch)
        except ValueError:
            pass
        else:
            raise AssertionError(f"patch should fail: {patch}")


def test_resolver_rejects_malformed_v4_and_returns_typed_lineage():
    case = {"id": "case", "source_run_id": "origin", "goal_json": json.dumps(_goal())}
    entry = routes._resolve_run_entry(case)
    assert entry["source_run_id"] == "origin"
    assert entry["source_case_id"] == "case"
    assert entry["execution_plan_revision"] == 1
    broken = _goal()
    del broken["execution_plan"]["effective"]
    try:
        routes._resolve_run_entry({"id": "case", "goal_json": json.dumps(broken)})
    except ValueError:
        pass
    else:
        raise AssertionError("malformed v4 plan must not be runnable")


def test_display_steps_preserves_structured_tool_evidence():
    from agents.orchestrator import _build_display_steps

    steps = _build_display_steps(
        [],
        [
            {
                "name": "click",
                "target": "WLAN",
                "tool_input": {"label": "WLAN"},
                "status_code": "OK",
                "result_evidence": {"resolved_label": "WLAN"},
                "page_before_activity": "A",
                "page_after_activity": "B",
                "page_before_package": "p",
                "page_after_package": "p",
                "page_before_signature": "before",
                "page_after_signature": "after",
                "resolved_target": {"label": "WLAN"},
                "tool_seq": 3,
            }
        ],
    )
    assert steps[0]["tool_input"] == {"label": "WLAN"}
    assert steps[0]["status_code"] == "OK"
    assert steps[0]["page_before_activity"] == "A"
    assert steps[0]["resolved_target"]["label"] == "WLAN"


def test_replay_renderer_uses_v4_effective_and_keeps_index_separate():
    from agents.nodes import _render_replay_evidence_block

    goal = _goal()
    goal["execution_plan"]["effective"]["key_actions"][0]["observed_index"] = 2
    rendered = _render_replay_evidence_block(goal)
    assert "not a forced script" in rendered
    assert "never combine observed_index" in rendered
    broken = _goal()
    del broken["execution_plan"]["effective"]
    assert _render_replay_evidence_block(broken) == ""


def test_report_done_abort_is_kept_as_accepted_structured_evidence():
    run = {
        "id": "r",
        "execution_status": "completed",
        "test_verdict": "passed",
        "app_package": "pkg",
        "goal_json": "{}",
        "verification_json": "[]",
        "steps": [
            {
                "action_type": "click",
                "status_code": "OK",
                "tool_input": {"label": "x"},
                "observation": "OK",
            },
            {
                "action_type": "report_done",
                "status_code": "OK",
                "tool_input": {"status": "abort"},
                "result_evidence": {"terminal_status": "abort"},
                "observation": "ERROR: legacy abort",
            },
        ],
    }
    evidence = routes._extract_replay_evidence(run)
    assert any(
        item["tool"] == "report_done" and item["last_result"] == "abort"
        for item in evidence["base_evidence"]["key_actions"]
    )


def test_case_capabilities_hide_storage_version_and_supply_managed_evidence(tmp_path):
    db = SqliteBackend(str(tmp_path / "db.sqlite"))
    routes.set_backends(None, db)
    _db_case(db)

    response = routes.list_test_cases()
    row = response["data"][0]
    capabilities = row["plan_capabilities"]

    assert capabilities["evidence_management"] == "server_managed"
    assert capabilities["can_run"] is True
    assert capabilities["can_replace_plan"] is False
    assert capabilities["can_patch_evidence"] is True
    assert capabilities["replay_evidence"]["effective_revision"] == 1
    assert "schema_version" not in capabilities


def test_unknown_execution_plan_format_is_not_runnable():
    goal = _goal()
    goal["execution_plan"]["schema_version"] = 5
    try:
        routes._resolve_run_entry({"id": "case", "goal_json": json.dumps(goal)})
    except ValueError as exc:
        assert "unsupported execution plan format" in str(exc)
    else:
        raise AssertionError("unknown execution-plan formats must not be runnable")


def test_report_rerun_entry_backfills_execution_plan_when_missing():
    run = {
        "id": "r1",
        "execution_status": "completed",
        "test_verdict": "passed",
        "app_package": "pkg",
        "goal_json": json.dumps({"goal": "rerun", "verification": ["页面包含 WLAN"]}),
        "verification_results": [
            {
                "key": "v0",
                "item": "页面包含 WLAN",
                "result": "passed",
                "detail": "deterministic pass",
                "screenshot": "screenshots/verify.png",
            }
        ],
        "steps": [
            {
                "action_type": "click",
                "status_code": "OK",
                "tool_input": {"label": "WLAN", "rid": "id/wlan"},
                "resolved_target": {"label": "WLAN", "rid": "id/wlan"},
                "page_before_activity": "A",
                "page_after_activity": "B",
                "observation": "clicked",
            },
            {
                "action_type": "assert_page_contains",
                "status_code": "PASS",
                "tool_input": {"text": "WLAN"},
                "page_before_activity": "B",
                "page_after_activity": "B",
                "observation": "PASS",
            },
        ],
    }

    entry = routes.resolve_report_rerun_entry(run)
    plan = entry["goal"]["execution_plan"]

    assert entry["execution_plan_revision"] == 1
    assert plan["effective"]["key_actions"]
    assert (
        plan["effective"]["verification_evidence"]["v0"]["subjective"]["result"]
        == "passed"
    )
    assert (
        "screenshot"
        not in plan["effective"]["verification_evidence"]["v0"]["subjective"]
    )


def test_extract_replay_evidence_includes_non_click_tools_and_expected_value_hint():
    run = {
        "id": "r2",
        "execution_status": "completed",
        "test_verdict": "passed",
        "app_package": "pkg",
        "goal_json": json.dumps({"verification": []}),
        "verification_results": [],
        "steps": [
            {
                "action_type": "type_input",
                "status_code": "UNSPECIFIED",
                "tool_input": {"text": "测试课程表2"},
                "page_before_activity": "EditTimetableActivity",
                "page_after_activity": "EditTimetableActivity",
                "observation": "typed",
            },
            {
                "action_type": "assert_page_contains",
                "status_code": "PASS",
                "tool_input": {"text": "测试课程表2"},
                "page_before_activity": "EditTimetableActivity",
                "page_after_activity": "EditTimetableActivity",
                "observation": "PASS",
            },
            {
                "action_type": "vision_tap",
                "status_code": "UNSPECIFIED",
                "tool_input": {"description": "右侧分钟列", "repeat": 3},
                "page_before_activity": "TimeSlotSettingsActivity",
                "page_after_activity": "TimeSlotSettingsActivity",
                "observation": "vision tapped",
            },
            {
                "action_type": "set_permission_intent",
                "status_code": "UNSPECIFIED",
                "tool_input": {"action": "deny"},
                "page_before_activity": "GrantPermissionsActivity",
                "page_after_activity": "GrantPermissionsActivity",
                "observation": "intent set",
            },
            {
                "action_type": "click_and_check",
                "status_code": "UNSPECIFIED",
                "tool_input": {"label": "保存"},
                "page_before_activity": "EditTimetableActivity",
                "page_after_activity": "TimetableActivity",
                "observation": "saved",
            },
        ],
    }

    evidence = routes._extract_replay_evidence(run)
    assert evidence is not None
    key_actions = evidence["base_evidence"]["key_actions"]
    tools = [item["tool"] for item in key_actions]

    assert "type_input" in tools
    assert "vision_tap" in tools
    assert "set_permission_intent" in tools
    assert "click_and_check" in tools

    type_action = next(item for item in key_actions if item["tool"] == "type_input")
    assert type_action["tool_input"]["text"] == "测试课程表2"
    assert type_action.get("inject_random_suffix") is True
    assert type_action["postcondition"].get("expected_value") == "测试课程表2"

def test_extract_replay_evidence_enriches_label_only_click_from_resolved_target():
    """原始 run 用 label 点击但无 rid 时，resolved_target 的 class/path 应被补进 locator。"""
    run = {
        "id": "r-label-only",
        "execution_status": "completed",
        "test_verdict": "passed",
        "app_package": "pkg",
        "goal_json": '{"verification": []}',
        "verification_results": [],
        "steps": [
            {
                "action_type": "click",
                "status_code": "OK",
                "tool_input": {"label": "课程表", "index": 0},
                "resolved_target": {
                    "label": "课程表",
                    "role": "list_entry",
                    "class_name": "android.widget.FrameLayout",
                    "path": "rv_more_menu",
                },
                "page_before_activity": "AllInOneActivity",
                "page_after_activity": "TimetableActivity",
                "observation": "clicked",
            }
        ],
    }
    evidence = routes._extract_replay_evidence(run)
    locator = evidence["base_evidence"]["key_actions"][0]["preferred_locator"]
    assert locator["label"] == "课程表"
    assert locator["class_name"] == "android.widget.FrameLayout"
    assert locator["path_contains"] == "rv_more_menu"


def test_extract_replay_evidence_keeps_rid_only_click_without_label():
    """无可见文本的图标（如 iv_more）只有 rid 时也应当保留，rid 是稳定定位器。"""
    run = {
        "id": "r-rid-only",
        "execution_status": "completed",
        "test_verdict": "passed",
        "app_package": "pkg",
        "goal_json": '{"verification": []}',
        "verification_results": [],
        "steps": [
            {
                "action_type": "click",
                "status_code": "OK",
                "tool_input": {"index": 3},
                "resolved_target": {
                    "label": "",
                    "role": "image",
                    "rid": "com.zui.calendar:id/iv_more",
                    "class_name": "android.widget.ImageView",
                    "path": "action_bar",
                },
                "page_before_activity": "AllInOneActivity",
                "page_after_activity": "AllInOneActivity",
                "observation": "clicked",
            }
        ],
    }
    evidence = routes._extract_replay_evidence(run)
    actions = evidence["base_evidence"]["key_actions"]
    assert len(actions) == 1
    locator = actions[0]["preferred_locator"]
    assert locator["rid"] == "com.zui.calendar:id/iv_more"
    assert locator.get("label") is None
    assert locator["class_name"] == "android.widget.ImageView"
    assert locator["path_contains"] == "action_bar"
    assert actions[0]["step"] == "click_rid_com_zui_calendar:id/iv_more"


def test_extract_replay_evidence_rebuilds_locator_for_index_clicks(tmp_path):
    """index 盲点：tool_input 只有 index 时，用 resolved_target 重建稳定 locator。"""
    db = SqliteBackend(str(tmp_path / "db.sqlite"))
    routes.set_backends(None, db)
    run = {
        "id": "r-idx",
        "execution_status": "completed",
        "test_verdict": "passed",
        "app_package": "pkg",
        "goal_json": json.dumps({"verification": []}),
        "verification_results": [],
        "steps": [
            {
                "action_type": "click",
                "status_code": "OK",
                "tool_input": {"index": 3},
                "resolved_target": {"label": "课程表", "role": "list_entry", "rid": "com.zui.calendar:id/tv"},
                "page_before_activity": "AllInOneActivity",
                "page_after_activity": "TimetableActivity",
                "observation": "clicked",
            },
            {
                "action_type": "click",
                "status_code": "OK",
                "tool_input": {"index": 5},
                "resolved_target": {"label": "", "role": "", "rid": ""},
                "page_before_activity": "TimetableActivity",
                "page_after_activity": "TimetableActivity",
                "observation": "clicked anonymous icon",
            },
        ],
    }
    evidence = routes._extract_replay_evidence(run)
    actions = evidence["base_evidence"]["key_actions"]
    # 有 resolved label 的 index 点击被保留并重建 locator；无 label 的仍丢弃
    assert len(actions) == 1
    locator = actions[0]["preferred_locator"]
    assert locator["label"] == "课程表"
    assert locator["rid"] == "com.zui.calendar:id/tv"


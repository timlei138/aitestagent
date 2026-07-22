"""Replay Evidence v4 test-case CRUD, patching, and validated run resolution."""
from __future__ import annotations

import copy
import json
import logging
from datetime import datetime as _dt
from typing import Any

from fastapi import APIRouter

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/test_cases", tags=["test_cases"])
_orchestrator = None
_relational_db = None

_ALLOWED_PATCH_ROOTS = {
    "entry", "pre_entry", "key_actions", "verification_evidence",
}
_PATCH_FORBIDDEN_NAMES = {
    "goal_json", "execution_plan", "base_evidence", "effective",
}
_BAD_STATUS_CODES = {"NOT_FOUND", "AMBIGUOUS", "NEEDS_HUMAN", "ERROR", "UNSPECIFIED"}


def set_backends(orchestrator_, relational_db_):
    global _orchestrator, _relational_db
    _orchestrator, _relational_db = orchestrator_, relational_db_


def _decode_json_pointer(path: Any) -> list[str]:
    if not isinstance(path, str) or not path.startswith("/") or path == "/" or "//" in path:
        raise ValueError("invalid JSON Pointer")
    parts: list[str] = []
    for token in path[1:].split("/"):
        out, i = "", 0
        while i < len(token):
            if token[i] == "~":
                if i + 1 >= len(token) or token[i + 1] not in "01":
                    raise ValueError("invalid JSON Pointer escape")
                out += "~" if token[i + 1] == "0" else "/"
                i += 2
            else:
                out += token[i]
                i += 1
        parts.append(out)
    if not parts or parts[0] not in _ALLOWED_PATCH_ROOTS:
        raise ValueError("patch path is outside allowed roots")
    return parts


def _list_index(token: str, length: int, *, allow_end: bool = False) -> int:
    if not token.isdigit() or (token != "0" and token.startswith("0")):
        raise ValueError("array index is invalid")
    index = int(token)
    if index > length or (index == length and not allow_end):
        raise ValueError("array index does not exist")
    return index


def apply_json_patch(target: dict[str, Any], patch: Any) -> None:
    """Apply the supported add/replace/remove RFC-6902 subset in place."""
    if not isinstance(patch, list):
        raise ValueError("override_patch must be a list")
    for operation in patch:
        if not isinstance(operation, dict) or set(operation) - {"op", "path", "value"}:
            raise ValueError("patch operation is invalid")
        kind = operation.get("op")
        if kind not in {"add", "replace", "remove"}:
            raise ValueError("unsupported patch operation")
        if kind in {"add", "replace"} and "value" not in operation:
            raise ValueError("patch value is required")
        if kind == "remove" and "value" in operation:
            raise ValueError("remove must not have a value")
        parts = _decode_json_pointer(operation.get("path"))
        node: Any = target
        for token in parts[:-1]:
            if isinstance(node, dict):
                if token not in node:
                    raise ValueError("target path does not exist")
                node = node[token]
            elif isinstance(node, list):
                node = node[_list_index(token, len(node))]
            else:
                raise ValueError("target parent has incompatible type")
        last = parts[-1]
        if isinstance(node, dict):
            if kind == "add":
                node[last] = copy.deepcopy(operation["value"])
            elif kind == "replace":
                if last not in node:
                    raise ValueError("replace target does not exist")
                node[last] = copy.deepcopy(operation["value"])
            else:
                if last not in node:
                    raise ValueError("remove target does not exist")
                del node[last]
        elif isinstance(node, list):
            if last == "-":
                if kind != "add":
                    raise ValueError("'-' is valid only for add")
                node.append(copy.deepcopy(operation["value"]))
            else:
                index = _list_index(last, len(node), allow_end=(kind == "add"))
                if kind == "add":
                    node.insert(index, copy.deepcopy(operation["value"]))
                elif kind == "replace":
                    node[index] = copy.deepcopy(operation["value"])
                else:
                    del node[index]
        else:
            raise ValueError("target parent has incompatible type")


def _materialize_current_effective_plan(
    base_evidence: dict[str, Any],
    patch: list[dict[str, Any]],
    *,
    effective_revision: int,
) -> dict[str, Any]:
    if not isinstance(base_evidence, dict) or not isinstance(effective_revision, int) or effective_revision <= 0:
        raise ValueError("invalid base evidence or effective revision")
    merged = copy.deepcopy(base_evidence)
    apply_json_patch(merged, patch)
    effective = {
        "schema_version": 4,
        "effective_revision": effective_revision,
        "applied_at": _dt.now().isoformat(),
    }
    for key in (
        "entry",
        "pre_entry",
        "key_actions",
        "verification_evidence",
        "extracted_from_run_id",
        "extracted_at",
        "base_revision",
    ):
        if key in merged:
            effective[key] = merged[key]
    return effective


def derive_effective_plan(
    base_evidence: dict[str, Any], patch: list[dict[str, Any]], *, effective_revision: int
) -> dict[str, Any]:
    return _materialize_current_effective_plan(
        base_evidence,
        patch,
        effective_revision=effective_revision,
    )


def validate_v4_execution_plan(goal: dict[str, Any]) -> None:
    """Reject v4 plans whose effective evidence is absent, malformed, or not derived."""
    if not isinstance(goal, dict):
        raise ValueError("goal must be an object")
    plan = goal.get("execution_plan")
    if not isinstance(plan, dict) or plan.get("schema_version") != 4:
        raise ValueError("invalid v4 execution plan")
    base, override, effective = plan.get("base_evidence"), plan.get("override"), plan.get("effective")
    if not all(isinstance(value, dict) for value in (base, override, effective)):
        raise ValueError("v4 plan must contain base_evidence, override, and effective")
    base_revision = base.get("base_revision")
    revision = effective.get("effective_revision")
    if not isinstance(base_revision, int) or base_revision <= 0 or not isinstance(revision, int) or revision <= 0:
        raise ValueError("v4 revisions must be positive integers")
    if effective.get("schema_version") != 4 or not isinstance(base.get("key_actions"), list):
        raise ValueError("v4 evidence schema is invalid")
    patch = override.get("patch")
    if not isinstance(patch, list) or not isinstance(override.get("changed_paths", []), list):
        raise ValueError("v4 override is invalid")
    expected = _materialize_current_effective_plan(
        base,
        patch,
        effective_revision=revision,
    )
    expected.pop("applied_at", None)
    actual = copy.deepcopy(effective)
    actual.pop("applied_at", None)
    if actual != expected:
        raise ValueError("effective evidence is not derived from base and override")


def _derive_plan_capabilities(goal: dict[str, Any]) -> dict[str, Any]:
    """Adapt stored plan formats to one UI-facing, semantic capability contract."""
    defaults = {
        "evidence_management": "client_editable",
        "can_run": False,
        "can_edit_metadata": True,
        "can_replace_plan": True,
        "can_patch_evidence": False,
        "has_replay_evidence": False,
        "has_verified_entry": False,
        "has_stable_locator": False,
        "index_only_locator": False,
        "has_objective_evidence": False,
        "evidence_stale": False,
        "effective_revision": 0,
        "replay_evidence": None,
    }
    if not isinstance(goal, dict):
        return {**defaults, "evidence_management": "invalid", "plan_error": "goal must be an object"}

    plan = goal.get("execution_plan")
    if plan is None:
        return {
            **defaults,
            "can_run": bool(goal.get("goal") or goal.get("target_pages") or goal.get("verification")),
        }
    if not isinstance(plan, dict):
        return {**defaults, "evidence_management": "invalid", "plan_error": "execution plan must be an object"}

    schema_version = plan.get("schema_version")
    if schema_version == 4:
        try:
            validate_v4_execution_plan(goal)
        except ValueError as exc:
            return {**defaults, "evidence_management": "invalid", "plan_error": str(exc)}
        effective = plan["effective"]
        override = plan.get("override") or {}
        management = "server_managed"
    elif schema_version in (None, 3) and isinstance(plan.get("key_actions"), list):
        # Compatibility-only adapter for the former flat evidence representation.
        effective = plan
        override = {}
        management = "client_editable"
    else:
        return {**defaults, "evidence_management": "invalid", "plan_error": "unsupported execution plan format"}

    entry = effective.get("entry") or {}
    actions = effective.get("key_actions") or []
    stable_locator = False
    index_only_locator = False
    for action in actions:
        if not isinstance(action, dict) or action.get("tool") != "click":
            continue
        locator = action.get("preferred_locator") or {}
        stable = bool(locator.get("rid") or (locator.get("class_name") and locator.get("path_contains") and locator.get("label")))
        stable_locator = stable_locator or stable
        index_only_locator = index_only_locator or (action.get("observed_index") is not None and not stable)
    verification = effective.get("verification_evidence") or {}
    has_objective = any(
        isinstance(item, dict) and bool(item.get("objective"))
        for item in verification.values()
    ) if isinstance(verification, dict) else False
    arrival = (entry.get("postcondition") or {}).get("arrival_confirmed") if isinstance(entry, dict) else False
    has_verified_entry = bool(isinstance(entry, dict) and entry.get("launch_app_args") and (entry.get("launch_app_args") or {}).get("activity") and arrival in (True, "true"))
    return {
        "evidence_management": management,
        "can_run": bool(goal.get("goal") or goal.get("target_pages") or goal.get("verification") or entry or actions),
        "can_edit_metadata": True,
        "can_replace_plan": management != "server_managed",
        "can_patch_evidence": management == "server_managed",
        "has_replay_evidence": bool(actions),
        "has_verified_entry": has_verified_entry,
        "has_stable_locator": stable_locator,
        "index_only_locator": index_only_locator,
        "has_objective_evidence": has_objective,
        "evidence_stale": bool(override.get("evidence_stale", False)),
        "effective_revision": int(effective.get("effective_revision", 0) or 0),
        "replay_evidence": effective if management == "server_managed" else None,
    }


def _step_condition(step: dict[str, Any], prefix: str) -> dict[str, Any]:
    activity = str(step.get(f"page_{prefix}_activity", "") or "")
    return {
        "expected_activity": activity.split(".")[-1] if activity else "",
        "required_anchors": [],
        "soft_page_signature": step.get(f"page_{prefix}_signature", "") or "",
        f"page_{prefix}_signature": step.get(f"page_{prefix}_signature", "") or "",
    }


def _verification_evidence(items: list[Any], reported: list[Any], actions: list[dict[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for index, item in enumerate(items):
        key = f"v{index}"
        subjective = {"result": "unknown", "detail": "historical run has no structured verification", "review_required": True}
        for record in reported:
            if isinstance(record, dict) and (record.get("key") == key or record.get("item") == item):
                subjective = {"result": record.get("result", "unknown"), "detail": record.get("detail", ""), "review_required": bool(record.get("review_required", False))}
                break
        objective = [
            {"kind": action["tool"], "args": action.get("args", {}), "result": action.get("last_result", "")}
            for action in actions
            if action.get("verify") == key and action.get("tool") in {"assert_page_contains", "assert_element_exists"}
        ]
        output[key] = {"item": item, "subjective": subjective, "objective": objective}
    return output


def _extract_replay_evidence(run: dict[str, Any]) -> dict[str, Any] | None:
    """Extract immutable base evidence only from a completed, passing structured run."""
    if run.get("execution_status") != "completed" or run.get("test_verdict") != "passed":
        return None
    raw_steps = run.get("steps", run.get("steps_json", []))
    try:
        steps = json.loads(raw_steps) if isinstance(raw_steps, str) else list(raw_steps or [])
        goal = json.loads(run.get("goal_json") or "{}")
        reported = json.loads(run.get("verification_json") or "[]")
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(goal, dict) or not isinstance(reported, list):
        return None
    actions: list[dict[str, Any]] = []
    verification = [item for item in goal.get("verification", []) if str(item or "").strip()]
    verify_index = 0
    entry = pre_entry = None
    business_indexes = [i for i, step in enumerate(steps) if step.get("action_type") in {"click", "assert_verification", "assert_page_contains", "assert_element_exists"}]
    if business_indexes:
        for step in reversed(steps[:business_indexes[0]]):
            if step.get("action_type") != "launch_app" or step.get("status_code") != "OK":
                continue
            args, evidence = step.get("tool_input") or {}, step.get("result_evidence") or {}
            package = str(args.get("package", "") or "")
            if package and package == run.get("app_package", "") and evidence.get("arrival_confirmed") == "true" and evidence.get("package_matched") == "true":
                entry = {"launch_app_args": {"package": package, "activity": args.get("activity", "")}, "postcondition": {"status_code": "OK", "observed_package": evidence.get("observed_package", ""), "observed_activity": evidence.get("observed_activity", ""), "arrival_confirmed": True}}
                pre_entry = _step_condition(step, "before")
                break
    for step in steps:
        tool, status = step.get("action_type"), step.get("status_code", "UNSPECIFIED")
        observation = str(step.get("observation", "") or "")
        # Structured status is authoritative. Observation text is only a legacy
        # fallback for navigation actions, because verification text may itself
        # legitimately mention terms such as "ERROR".
        if status in _BAD_STATUS_CODES or (
            tool in {"click", "launch_app"}
            and any(token in observation for token in _BAD_STATUS_CODES)
        ):
            continue
        pre, post = _step_condition(step, "before"), _step_condition(step, "after")
        args = step.get("tool_input") or {}
        if tool == "click":
            locator = {key: args[key] for key in ("label", "rid", "class_name", "path_contains", "alternatives") if args.get(key) not in (None, "")}
            if not locator.get("label"):
                continue
            resolved = dict(step.get("resolved_target") or {})
            if any(resolved.get(key) for key in ("label", "role", "rid")):
                pre["required_anchors"] = [{key: resolved.get(key, "") for key in ("label", "role", "rid", "class_name", "path")}]
            raw_index = args.get("index")
            actions.append({"step": f"click_{str(locator['label']).lower().replace(' ', '_')}", "tool": "click", "precondition": pre, "preferred_locator": locator, "observed_index": raw_index if isinstance(raw_index, int) and raw_index >= 0 else None, "resolved_target": {k: v for k, v in resolved.items() if v}, "postcondition": post, "last_observation": observation[:200], "last_result": status, "verify": f"v{verify_index}" if verify_index < len(verification) else None})
        elif tool == "assert_verification" and verify_index < len(verification):
            actions.append({"step": f"verify_v{verify_index}", "tool": tool, "precondition": pre, "postcondition": post, "verify_key": f"v{verify_index}", "last_observation": observation[:200], "last_result": (step.get("result_evidence") or {}).get("reported_result", "passed")})
            verify_index += 1
        elif tool in {"assert_page_contains", "assert_element_exists"}:
            arg_name = "text" if tool == "assert_page_contains" else "label"
            actions.append({"step": f"assert_{tool}_{len(actions)}", "tool": tool, "precondition": pre, "postcondition": post, "args": {arg_name: args.get(arg_name, "")}, "last_observation": observation[:200], "last_result": status, "verify": f"v{verify_index}" if verify_index < len(verification) else None})
        elif tool == "report_done":
            actions.append({"step": "done", "tool": tool, "precondition": pre, "postcondition": post, "args": {"status": args.get("status", "done"), "summary": args.get("summary", "")}, "last_observation": observation[:200], "last_result": (step.get("result_evidence") or {}).get("terminal_status", "done")})
    if not actions:
        return None
    base = {
        "extracted_from_run_id": run.get("id"),
        "extracted_at": _dt.now().isoformat(),
        "base_revision": 1,
        "entry": entry,
        "pre_entry": pre_entry,
        "key_actions": actions,
        "verification_evidence": _verification_evidence(
            verification, reported, actions
        ),
    }
    return {"schema_version": 4, "base_evidence": base, "override": {"revision": 0, "patch": [], "changed_paths": [], "evidence_stale": False, "edited_at": None, "edited_by": None}}


def _reject_forbidden_fields(value: Any) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in _PATCH_FORBIDDEN_NAMES:
                raise ValueError(f"field is not allowed in v4 PATCH: {key}")
            _reject_forbidden_fields(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_forbidden_fields(nested)


def _resolve_execution_plan_revision(goal: dict[str, Any]) -> int:
    """Validate only supported stored plan formats before an orchestration run."""
    plan = goal.get("execution_plan") if isinstance(goal, dict) else None
    if plan is None:
        return 0
    if not isinstance(plan, dict):
        raise ValueError("execution plan must be an object")
    schema_version = plan.get("schema_version")
    if schema_version == 4:
        validate_v4_execution_plan(goal)
        return plan["effective"]["effective_revision"]
    if schema_version in (None, 3) and isinstance(plan.get("key_actions"), list):
        return 0
    raise ValueError("unsupported execution plan format")


def _resolve_run_entry(case: dict[str, Any]) -> dict[str, Any]:
    """Return canonical validated goal plus typed lineage for a stored case."""
    try:
        goal = json.loads(case.get("goal_json") or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("case plan data is damaged") from exc
    revision = _resolve_execution_plan_revision(goal)
    return {"goal": goal, "source_run_id": case.get("source_run_id") or None, "source_case_id": case["id"], "execution_plan_revision": revision}


def resolve_report_rerun_entry(run: dict[str, Any]) -> dict[str, Any]:
    try:
        goal = json.loads(run.get("goal_json") or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("report plan data is damaged") from exc
    revision = _resolve_execution_plan_revision(goal)
    return {"goal": goal, "source_run_id": run["id"], "source_case_id": None, "execution_plan_revision": revision}


@router.get("")
def list_test_cases(q: str = ""):
    if not _relational_db:
        return {"status": "error", "message": "数据库未初始化"}
    rows = _relational_db.list_test_cases(q=q) if q else _relational_db.list_test_cases()
    for row in rows:
        try:
            row["plan_capabilities"] = _derive_plan_capabilities(json.loads(row.get("goal_json") or "{}"))
        except (TypeError, json.JSONDecodeError):
            row["plan_capabilities"] = _derive_plan_capabilities({})
    return {"status": "ok", "data": rows}


@router.post("")
def create_test_case(body: dict[str, Any]):
    if not _relational_db:
        return {"status": "error", "message": "数据库未初始化"}
    if body.get("run_id"):
        run = _relational_db.get_test_run(body["run_id"])
        if not run:
            return {"status": "error", "message": "报告不存在"}
        try:
            goal = json.loads(run.get("goal_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            return {"status": "error", "message": "报告计划数据损坏"}
        evidence = _extract_replay_evidence(run)
        if evidence:
            evidence["effective"] = derive_effective_plan(evidence["base_evidence"], [], effective_revision=1)
            goal["execution_plan"] = evidence
        case_id = _relational_db.create_test_case(name=body.get("name") or (run.get("user_request") or "未命名")[:40], source_run_id=run["id"], user_request=run.get("user_request", ""), app_package=run.get("app_package", ""), app_name=run.get("app_name", ""), goal_json=json.dumps(goal, ensure_ascii=False))
        return {"status": "ok", "data": {"id": case_id, "has_replay_evidence": bool(evidence)}}
    goal_json = body.get("goal_json") or {}
    encoded = json.dumps(goal_json, ensure_ascii=False) if isinstance(goal_json, dict) else goal_json
    case_id = _relational_db.create_test_case(name=body.get("name", "未命名"), user_request=body.get("user_request", ""), app_package=body.get("app_package", ""), app_name=body.get("app_name", ""), goal_json=encoded)
    return {"status": "ok", "data": {"id": case_id}}


@router.put("/{case_id}")
def update_test_case(case_id: str, body: dict[str, Any]):
    if not _relational_db:
        return {"status": "error", "message": "数据库未初始化"}
    case = _relational_db.get_test_case(case_id)
    if not case:
        return {"status": "error", "message": "用例不存在"}
    try:
        goal = json.loads(case.get("goal_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        goal = {}
    if isinstance(goal.get("execution_plan"), dict) and goal["execution_plan"].get("schema_version") == 4 and "goal_json" in body:
        return {"status": "error", "message": "v4 用例不允许通过 PUT 覆盖 goal_json"}
    ok = _relational_db.update_test_case(case_id, body)
    return {"status": "ok" if ok else "error", "message": "" if ok else "用例不存在或无可更新字段"}


@router.patch("/{case_id}")
def patch_test_case(case_id: str, body: dict[str, Any]):
    if not _relational_db:
        return {"status": "error", "message": "数据库未初始化"}
    try:
        _reject_forbidden_fields(body)
        expected = set(body)
        allowed = {"expected_effective_revision", "override_patch", "changed_paths", "edited_by"}
        if expected != allowed:
            raise ValueError("v4 PATCH only accepts expected_effective_revision, override_patch, changed_paths, edited_by")
        if not isinstance(body["expected_effective_revision"], int) or body["expected_effective_revision"] <= 0:
            raise ValueError("expected_effective_revision must be a positive integer")
        if not isinstance(body["override_patch"], list) or not isinstance(body["changed_paths"], list) or not isinstance(body["edited_by"], str):
            raise ValueError("v4 PATCH field types are invalid")
        # Dry run before starting the DB transaction gives deterministic client errors.
        case = _relational_db.get_test_case(case_id)
        if not case:
            return {"status": "error", "message": "用例不存在"}
        goal = json.loads(case.get("goal_json") or "{}")
        plan = goal.get("execution_plan") or {}
        base = plan.get("base_evidence")
        if not isinstance(base, dict):
            raise ValueError("case has no editable v4 base evidence")
        apply_json_patch(copy.deepcopy(base), body["override_patch"])
        ok, reason, updated = _relational_db.update_case_override_if_revision(case_id, body["expected_effective_revision"], body["override_patch"], body["changed_paths"], body["edited_by"])
        if not ok:
            message = "用例已被其他编辑更新，请刷新后重试" if reason == "revision_conflict" else f"无法更新用例: {reason}"
            return {"status": "conflict" if reason == "revision_conflict" else "error", "message": message}
        return {"status": "ok", "data": updated}
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return {"status": "error", "message": f"patch 非法: {exc}"}


@router.delete("/{case_id}")
def delete_test_case(case_id: str):
    if not _relational_db:
        return {"status": "error", "message": "数据库未初始化"}
    return {"status": "ok" if _relational_db.delete_test_case(case_id) else "error"}


@router.post("/batch_delete")
def batch_delete_test_cases(body: dict[str, Any]):
    if not _relational_db:
        return {"status": "error", "message": "数据库未初始化"}
    return {"status": "ok", "deleted": _relational_db.batch_delete_test_cases(body.get("ids") or [])}


@router.post("/{case_id}/run")
def run_test_case(case_id: str):
    if not _relational_db or not _orchestrator:
        return {"status": "error", "message": "后台服务未就绪"}
    case = _relational_db.get_test_case(case_id)
    if not case:
        return {"status": "error", "message": "用例不存在"}
    try:
        entry = _resolve_run_entry(case)
    except ValueError as exc:
        return {"status": "error", "message": f"用例计划数据损坏: {exc}"}
    result = _orchestrator.start(user_request=case.get("user_request", ""), app_package=case.get("app_package", ""), app_name=case.get("app_name", ""), goal_description=entry["goal"], reuse_plan=True, run_type="rerun", source_run_id=entry["source_run_id"], source_case_id=entry["source_case_id"], execution_plan_revision=entry["execution_plan_revision"])
    if result.get("status") != "busy":
        _relational_db.record_case_run(case_id, f"{result.get('execution_status', 'error')}/{result.get('test_verdict', 'inconclusive')}", _dt.now().isoformat())
    return {"status": "ok", "thread_id": result.get("thread_id", "")}

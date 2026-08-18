from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from typing import Any

from data.relational import RelationalBackend

_READ_ONLY_TOOLS = {"get_screen_info", "check_page_health", "request_knowledge"}
_SUCCESS_STATUSES = {"OK", "PASS", "YES"}

# Parameter slot tolerance for compatibility checks.
# Code owns the contract of "how close is close enough"; the LLM (planner) owns
# identifying which parameters exist and what they mean.
_DEFAULT_SLOT_TOLERANCE = {
    "duration": {"minute": 3, "hour": 1, "second": 30, "day": 1},
    "count": {"count": 1, "item": 1},
    "number": {"item": 1},
}


def _normalize_planner_slots(slots: Any) -> list[dict[str, Any]]:
    """Validate and normalize parameter slots produced by the planner (LLM).

    Code does not invent slots; it only enforces the slot contract:
    - name must be a semantic identifier (not empty, not a position label)
    - value must be present
    - type/unit/original are kept as provided by the planner
    """
    if not isinstance(slots, list):
        return []
    result: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        name = str(slot.get("name", "") or "").strip()
        value = slot.get("value")
        if not name or value is None:
            continue
        # Reject position-based ASCII names that the planner should never emit.
        # Examples: MINUTE, MINUTE_1, DURATION_2.  Semantic names like
        # lesson_duration or 课时 are allowed.
        if (
            name.isascii()
            and name.upper() == name
            and name.replace("_", "").isalnum()
            and not name[0].isdigit()
        ):
            if "_" not in name or name.split("_")[-1].isdigit():
                continue
        if name in seen_names:
            continue
        seen_names.add(name)
        result.append(
            {
                "name": name,
                "type": str(slot.get("type", "") or "").strip(),
                "unit": str(slot.get("unit", "") or "").strip(),
                "value": value,
                "original": str(slot.get("original", "") or "").strip(),
                "source": str(slot.get("source", "") or "user_request").strip(),
            }
        )
    return result


def extract_parameter_slots(user_request: str, goal: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return the parameter slots produced by the planner, normalized for downstream use.

    Parameter extraction is a semantic understanding task and is owned by the LLM
    (planner). Code only validates the slot contract and removes malformed entries.
    """
    return _normalize_planner_slots((goal or {}).get("parameter_slots"))


def build_user_request_template(user_request: str, slots: list[dict[str, Any]]) -> str:
    """Replace concrete parameter fragments with {NAME} placeholders.

    Uses the ``original`` text provided by the planner. The replacement refuses to
    match when the fragment is immediately preceded by a digit, avoiding collisions
    like ``50分钟`` inside ``150分钟``.
    """
    template = str(user_request or "")
    for slot in slots or []:
        if not isinstance(slot, dict):
            continue
        name = str(slot.get("name", "") or "").strip()
        original = str(slot.get("original", "") or "").strip()
        if not name or not original:
            continue
        placeholder = f"{{{name}}}"
        template = re.sub(
            r"(?<!\d)" + re.escape(original),
            placeholder,
            template,
            count=1,
        )
    return template


def extract_action_semantics_from_request(
    user_request: str, goal: dict[str, Any] | None = None
) -> list[str]:
    """Extract action semantics from user request text when no action events exist.

    This is a deterministic fallback.  The planner should still emit action_semantics
    when possible; code only fills the gap so that task_signatures_compatible can use
    the action-overlap dimension during mode selection.
    """
    text = str(user_request or "")
    if not text:
        return []
    goal = goal or {}
    semantics: list[str] = []

    app_package = str(goal.get("app_package", "") or "").strip()

    # Launch / open app.
    if re.search(r"打开|启动|进入", text) and app_package:
        semantics.append(f"启动应用{app_package}")

    # Click-like actions.
    click_targets = re.findall(r"(?:点击|选择|按|打开)\s*[""'']?([^""''，。；\n]{1,20})[""'']?", text)
    for target in click_targets:
        target = target.strip()
        if target and len(target) >= 1:
            semantics.append(f"点击{target}")

    # Type / fill actions.
    type_targets = re.findall(r"(?:输入|填写)\s*[""'']?([^""''，。；\n]{1,20})[""'']?", text)
    for target in type_targets:
        target = target.strip()
        if target and len(target) >= 1:
            semantics.append(f"输入{target}")

    # Scroll-and-click actions.
    if re.search(r"滚动|滑动查找", text):
        semantics.append("滚动查找并点击")

    # Long press.
    if re.search(r"长按", text):
        semantics.append("long_press")

    # Verification / check intent.
    if re.search(r"验证|检查|确认|应该|断言", text):
        for statement in (goal.get("verification") or []):
            stmt = str(statement or "").strip()
            if stmt:
                semantics.append(f"验证{stmt}")

    # Deduplicate while preserving order.
    seen: set[str] = set()
    result: list[str] = []
    for s in semantics:
        if s not in seen:
            seen.add(s)
            result.append(s)
    return result


def environment_compatibility_score(
    expected_key: str, actual_key: str
) -> dict[str, Any]:
    """Return compatibility score between two environment fingerprints.

    Critical fields (package, activity, screen_profile) must match exactly for
    any compatibility.  Optional fields (app_version, fixture_fingerprint) incur
    penalties only when both sides are present and differ.
    """
    try:
        expected = json.loads(expected_key) if expected_key else {}
    except Exception:
        expected = {}
    try:
        actual = json.loads(actual_key) if actual_key else {}
    except Exception:
        actual = {}

    if not isinstance(expected, dict):
        expected = {}
    if not isinstance(actual, dict):
        actual = {}

    reasons: list[str] = []

    def _value(d: dict[str, Any], key: str) -> str:
        return str(d.get(key, "") or "").strip()

    critical_fields = ("package", "activity", "screen_profile")
    for field in critical_fields:
        exp = _value(expected, field)
        act = _value(actual, field)
        if exp and act and exp != act:
            reasons.append(f"{field}_mismatch")

    if reasons:
        return {
            "compatible": False,
            "score": 0.0,
            "critical_match": False,
            "reasons": reasons,
        }

    score = 1.0
    optional_penalties = {
        "app_version": 0.2,
        "fixture_fingerprint": 0.3,
    }
    for field, penalty in optional_penalties.items():
        exp = _value(expected, field)
        act = _value(actual, field)
        if exp and act and exp != act:
            score -= penalty
            reasons.append(f"{field}_drift")

    return {
        "compatible": score > 0.0,
        "score": max(0.0, score),
        "critical_match": True,
        "reasons": reasons,
    }


def _infer_action_semantics(
    user_request: str,
    goal: dict[str, Any] | None,
    action_events: list[dict[str, Any]],
) -> list[str]:
    """Pick the best available action semantics source."""
    if action_events:
        return extract_action_semantics(action_events)
    goal_obj = goal or {}
    planner_semantics = goal_obj.get("action_semantics")
    if isinstance(planner_semantics, list) and planner_semantics:
        return [str(s) for s in planner_semantics if s]
    return extract_action_semantics_from_request(user_request, goal)


def extract_action_semantics(action_events: list[dict[str, Any]]) -> list[str]:
    """Summarize action sequence into human-readable action semantics."""
    semantics: list[str] = []
    for event in action_events or []:
        if not isinstance(event, dict):
            continue
        tool_name = str(event.get("tool_name", "") or "")
        tool_input = event.get("tool_input", {}) or {}
        label = str(tool_input.get("label", "") or tool_input.get("target", "") or "")
        if tool_name == "click" and label:
            semantics.append(f"点击{label}")
        elif tool_name == "type_input" and label:
            semantics.append(f"输入{label}")
        elif tool_name == "scroll_find_and_click" and label:
            semantics.append(f"滚动查找并点击{label}")
        elif tool_name == "launch_app":
            pkg = str(tool_input.get("package", "") or "")
            semantics.append(f"启动应用{pkg}")
        elif tool_name == "press_key":
            key = str(tool_input.get("key", "") or "")
            semantics.append(f"按键{key}")
        elif tool_name:
            semantics.append(tool_name)
    return semantics


def extract_verification_semantics(verification_contract: dict[str, Any]) -> list[str]:
    """Extract verification statements and clause claims as semantics."""
    semantics: list[str] = []
    for verification in (verification_contract or {}).get("verifications", []) or []:
        if not isinstance(verification, dict):
            continue
        statement = str(verification.get("statement", "") or "")
        if statement:
            semantics.append(statement)
        for clause in verification.get("clauses", []) or []:
            if isinstance(clause, dict):
                claim = str(clause.get("claim", "") or "")
                if claim:
                    semantics.append(claim)
    return semantics


def parameter_slots_compatible(
    query_slots: list[dict[str, Any]],
    candidate_slots: list[dict[str, Any]],
    tolerances: dict[str, dict[str, float]] | None = None,
) -> bool:
    """Return True if candidate parameter slots are compatible with query slots.

    Both sides must agree on the set of semantic parameter names. A task with
    parameters is never compatible with a parameter-less plan (and vice versa),
    because that would mean reusing a plan whose behavior depends on values that
    the current request does not specify.
    """
    q = [
        s
        for s in query_slots or []
        if isinstance(s, dict) and str(s.get("name", "") or "").strip()
    ]
    c = [
        s
        for s in candidate_slots or []
        if isinstance(s, dict) and str(s.get("name", "") or "").strip()
    ]
    if not q and not c:
        return True
    if not q or not c:
        return False

    q_by_name = {str(s["name"]): s for s in q}
    c_by_name = {str(s["name"]): s for s in c}
    if set(q_by_name.keys()) != set(c_by_name.keys()):
        return False

    tolerances = tolerances or _DEFAULT_SLOT_TOLERANCE
    for name, q_slot in q_by_name.items():
        c_slot = c_by_name[name]
        q_type = str(q_slot.get("type", "") or "")
        c_type = str(c_slot.get("type", "") or "")
        if q_type and c_type and q_type != c_type:
            return False
        q_unit = str(q_slot.get("unit", "") or "")
        c_unit = str(c_slot.get("unit", "") or "")
        if q_unit and c_unit and q_unit != c_unit:
            return False
        q_value = q_slot.get("value")
        c_value = c_slot.get("value")
        if q_value is None or c_value is None:
            continue
        try:
            qv = float(q_value)
            cv = float(c_value)
        except (TypeError, ValueError):
            if q_value != c_value:
                return False
            continue
        tol = tolerances.get(q_type, {}).get(q_unit, 0.0)
        if abs(qv - cv) > tol:
            return False
    return True


def task_signatures_compatible(
    query_signature: dict[str, Any], candidate_signature: dict[str, Any]
) -> bool:
    """High-level compatibility check for semantic task signatures."""
    if not isinstance(query_signature, dict) or not isinstance(candidate_signature, dict):
        return False

    # Verification semantics must match exactly (this is the safety boundary).
    q_vf = str(query_signature.get("verification_fingerprint", "") or "")
    c_vf = str(candidate_signature.get("verification_fingerprint", "") or "")
    if q_vf and c_vf and q_vf != c_vf:
        return False

    # Parameter slots must be compatible.
    if not parameter_slots_compatible(
        query_signature.get("parameter_slots", []) or [],
        candidate_signature.get("parameter_slots", []) or [],
    ):
        return False

    # Action semantics: candidate should overlap significantly with query.
    q_actions = set(query_signature.get("action_semantics", []) or [])
    c_actions = set(candidate_signature.get("action_semantics", []) or [])
    if q_actions and c_actions:
        overlap = len(q_actions & c_actions)
        if overlap == 0:
            return False
        # Require at least 50% overlap (or 1 if query is tiny).
        if len(q_actions) > 1 and overlap / len(q_actions) < 0.5:
            return False

    return True


def build_plan_summary_text(
    app_package: str,
    action_semantics: list[str],
    verification_semantics: list[str],
    parameter_slots: list[dict[str, Any]],
    user_request_template: str = "",
) -> str:
    """Build a dense text summary for vector indexing of task plans."""
    parts = [f"app={app_package}"]
    if user_request_template:
        parts.append(user_request_template)
    if action_semantics:
        parts.append("动作:" + ";".join(action_semantics[:10]))
    if verification_semantics:
        parts.append("验证:" + ";".join(verification_semantics[:10]))
    if parameter_slots:
        slot_desc = ",".join(
            f"{s.get('name','')}={s.get('type','')}({s.get('unit','')})"
            for s in parameter_slots
        )
        parts.append(f"参数:[{slot_desc}]")
    return " | ".join(parts)


def _task_signature_from_goal(
    user_request: str,
    goal: dict[str, Any] | None,
    verification_contract: dict[str, Any],
    action_events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the semantic task signature used for plan matching."""
    slots = extract_parameter_slots(user_request, goal)
    template = build_user_request_template(user_request, slots)
    return {
        "user_request": user_request,
        "user_request_template": template,
        "action_semantics": _infer_action_semantics(user_request, goal, action_events),
        "verification_semantics": extract_verification_semantics(verification_contract),
        "parameter_slots": slots,
        "verification_fingerprint": verification_fingerprint(verification_contract, slots),
    }


def environment_fingerprint(page: dict[str, Any], screen_profile: str = "") -> str:
    """Build the minimal stable environment key used for plan applicability."""
    payload = {
        "package": str(page.get("package", "") or ""),
        "activity": str(page.get("activity", "") or ""),
        "screen_profile": screen_profile or str(page.get("screen_profile", "") or ""),
        "app_version": str(page.get("app_version", "") or ""),
        "fixture_fingerprint": str(page.get("fixture_fingerprint", "") or ""),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def verification_fingerprint(
    contract: dict[str, Any],
    parameter_slots: list[dict[str, Any]] | None = None,
) -> str:
    """Return a stable identity for the reviewed verification semantics.

    When ``parameter_slots`` are provided, concrete parameter fragments in
    verification statements and clause claims are replaced with their semantic
    placeholders. This allows tasks like "50分钟" and "53分钟" to share the same
    fingerprint and be differentiated only by ``parameter_slots_compatible``.
    """
    slots = [
        s
        for s in parameter_slots or []
        if isinstance(s, dict)
        and str(s.get("name", "") or "").strip()
        and str(s.get("original", "") or "").strip()
    ]
    replacements = sorted(
        [
            (str(s["original"]), str(s["name"]))
            for s in slots
        ],
        key=lambda item: len(item[0]),
        reverse=True,
    )

    def _template_text(text: str) -> str:
        for original, name in replacements:
            if original in text:
                text = text.replace(original, f"{{{name}}}")
        return text

    verifications = []
    for verification in contract.get("verifications", []) or []:
        if not isinstance(verification, dict):
            continue
        statement = _template_text(str(verification.get("statement", "") or ""))
        clauses = [
            {
                "claim": _template_text(str(clause.get("claim", "") or "")),
                "channels": sorted(
                    str(channel) for channel in clause.get("channels", []) or []
                ),
            }
            for clause in verification.get("clauses", []) or []
            if isinstance(clause, dict)
        ]
        verifications.append(
            {
                "statement": statement,
                "clauses": clauses,
            }
        )
    encoded = json.dumps(verifications, ensure_ascii=False, sort_keys=True)
    return __import__("hashlib").sha256(encoded.encode("utf-8")).hexdigest()


def extract_candidate_plan(
    db: RelationalBackend,
    *,
    app_package: str,
    user_request: str,
    goal: dict[str, Any] | None = None,
    verification_contract: dict[str, Any],
    action_events: list[dict[str, Any]],
    evidence_events: list[dict[str, Any]],
    knowledge_base: Any | None = None,
) -> str | None:
    """Create a guided candidate plan from current-run facts after a passed contract.

    If ``knowledge_base`` is provided, a task-plan summary is also written to the
    vector store for semantic retrieval.
    """
    if verification_contract.get("status") != "approved":
        return None
    actions = [
        event
        for event in action_events
        if isinstance(event, dict)
        and str(event.get("tool_name", "") or "") not in _READ_ONLY_TOOLS
        and str(event.get("status", "") or "").upper() in _SUCCESS_STATUSES
    ]
    if not actions:
        return None

    task_signature = _task_signature_from_goal(
        user_request, goal, verification_contract, actions
    )

    now = datetime.now().isoformat()
    plan_id = str(uuid.uuid4())
    db.insert(
        "execution_plans",
        {
            "plan_id": plan_id,
            "app_package": app_package,
            "task_signature_json": json.dumps(task_signature, ensure_ascii=False),
            "entry_contract_json": json.dumps(
                actions[0].get("page_before", {}) or {}, ensure_ascii=False
            ),
            "verification_contract_json": json.dumps(
                verification_contract, ensure_ascii=False
            ),
            "contract_status": "approved",
            "plan_trust": "candidate",
            "direct_approved": 0,
            "environment_key": environment_fingerprint(
                actions[0].get("page_before", {}) or {}
            ),
            "attempt_count": 0,
            "success_count": 0,
            "quality_score": 0.0,
            "created_at": now,
            "updated_at": now,
        },
    )

    evidence_links = _evidence_links(evidence_events)
    for index, event in enumerate(actions):
        locator = event.get("resolved_locator", {}) or {}
        page_before = event.get("page_before", {}) or {}
        page_after = event.get("page_after", {}) or {}
        has_stable_locator = bool(locator.get("rid") or locator.get("path"))
        has_stable_postcondition = bool(
            page_after.get("package") or page_after.get("activity")
        )
        eligibility = (
            "direct_eligible"
            if has_stable_locator and has_stable_postcondition
            else "guided_only"
        )
        db.insert(
            "plan_actions",
            {
                "action_id": str(uuid.uuid4()),
                "plan_id": plan_id,
                "action_index": index,
                "tool_name": str(event.get("tool_name", "") or ""),
                "tool_input_json": json.dumps(
                    event.get("tool_input", {}) or {}, ensure_ascii=False
                ),
                "precondition_json": json.dumps(page_before, ensure_ascii=False),
                "locator_json": json.dumps(locator, ensure_ascii=False),
                "postcondition_json": json.dumps(page_after, ensure_ascii=False),
                "verification_links_json": json.dumps(
                    evidence_links, ensure_ascii=False
                ),
                "execution_eligibility": eligibility,
                "attempt_count": 0,
                "success_count": 0,
                "postcondition_pass_count": 0,
                "timeout_count": 0,
                "not_found_count": 0,
                "quality_score": 0.0,
                "last_failed_at": None,
                "created_at": now,
                "updated_at": now,
            },
        )

    if knowledge_base is not None:
        try:
            summary = build_plan_summary_text(
                app_package,
                task_signature.get("action_semantics", []),
                task_signature.get("verification_semantics", []),
                task_signature.get("parameter_slots", []),
                task_signature.get("user_request_template", ""),
            )
            knowledge_base.save_task_plan_summary(
                app_package=app_package,
                plan_id=plan_id,
                summary=summary,
                verification_fingerprint=task_signature.get(
                    "verification_fingerprint", ""
                ),
                user_request_template=task_signature.get(
                    "user_request_template", ""
                ),
                parameter_slots=task_signature.get("parameter_slots", []),
                environment_key=environment_fingerprint(
                    actions[0].get("page_before", {}) or {}
                ),
            )
        except Exception:
            # Vector indexing must not block relational plan creation.
            pass

    return plan_id


def _evidence_links(events: list[dict[str, Any]]) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        if str(event.get("status", "") or "").upper() not in {"PASS", "YES"}:
            continue
        key = str(event.get("verification_key", "") or "")
        clause_id = str(event.get("clause_id", "") or "")
        if key and clause_id:
            links.append({"verification_key": key, "clause_id": clause_id})
    return links

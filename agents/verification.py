"""验证项归一化 / key 映射 / 结果合并 / 执行状态判定。

从 agents/graph.py 拆出（重构 G3），仅移动代码、不改逻辑。
"""

from __future__ import annotations

import re
from typing import Any

from agents.budget import _calc_budget_from_state
from agents.loop_control import _detect_termination

_VISUAL_CLAIM_MARKERS = ("颜色", "红色", "黑色", "布局", "图标", "样式", "视觉")
_TEXT_CLAIM_MARKERS = ("文字", "文本", "提示", "toast", "显示")
_STATE_CLAIM_MARKERS = (
    "页面", "activity", "状态", "开启", "关闭", "勾选", "选中", "打开", "开关",
)
_CLAUSE_BOUNDARY = re.compile(r"[，,；;]+|(?:并且|同时|以及|且)")


def _default_channels_for_claim(claim: str) -> list[str]:
    """Choose the evidence channels for a generated claim.

    - Visual claims rely on vision_verify (screenshot + VLM).
    - Text claims rely on ui_text / click_and_check (OCR / UI-tree text).
    - State/switch claims use behavior_effect (did the action produce the expected
      result) and vision_verify (does the screenshot show the switch on/off), and
      also accept page_state / element_state: a page_state PASS (e.g. landed on the
      expected activity) or element_state PASS (target element exists) is genuine
      positive evidence and must be recognized by the evaluator, otherwise the run
      is wrongly marked inconclusive while the frontend (which shows any PASS) looks
      green. The original concern about Accessibility ``checked`` unreliability only
      applied to the producer side (agent loop of open/close to collect evidence),
      not to the evaluator accepting an observed state.
    - The fallback lists channels that have a real producer so a claim never lands
      in a dead channel.
    """
    normalized = str(claim or "").lower()
    if any(marker in normalized for marker in _VISUAL_CLAIM_MARKERS):
        return ["vision_verify", "page_state", "element_state"]
    if any(marker in normalized for marker in _TEXT_CLAIM_MARKERS):
        return ["ui_text", "click_and_check", "page_state", "element_state"]
    if any(marker in normalized for marker in _STATE_CLAIM_MARKERS):
        # UI Tree 能判定的状态/页面类断言优先走 UI Tree；视觉仅作为兜底。
        return ["page_state", "element_state", "behavior_effect", "vision_verify"]
    return [
        "ui_text",
        "vision_verify",
        "click_and_check",
        "behavior_effect",
        "page_state",
        "element_state",
    ]


def _split_claims(statement: str) -> list[str]:
    """Split only explicit conjunction boundaries; review can refine remaining ambiguity."""
    return [
        part.strip(" ，,；;")
        for part in _CLAUSE_BOUNDARY.split(str(statement or ""))
        if part.strip(" ，,；;")
    ] or [str(statement or "").strip()]


def evaluate_verification(
    contract: dict[str, Any], events: list[dict[str, Any]]
) -> dict[str, Any]:
    """Evaluate current-run typed evidence without consulting LLM assertions or history.

    A clause is passed only by a PASS/YES event from one of its declared channels.
    A clause is failed only by an explicitly authoritative FAIL/NO event; all other
    missing or negative observations remain unknown to preserve the asymmetric UI
    evidence policy for text, icons, and canvas content.
    """
    verification_results: list[dict[str, Any]] = []
    for verification in contract.get("verifications", []) or []:
        if not isinstance(verification, dict):
            continue
        key = str(verification.get("key", "") or "")
        clauses: list[dict[str, Any]] = []
        for clause in verification.get("clauses", []) or []:
            if not isinstance(clause, dict):
                continue
            clause_id = str(clause.get("id", "") or "")
            channels = {str(channel) for channel in clause.get("channels", []) or []}
            matching = [
                event
                for event in events or []
                if isinstance(event, dict)
                and str(event.get("verification_key", "") or "") == key
                and str(event.get("clause_id", "") or "") == clause_id
                and str(event.get("channel", "") or "") in channels
            ]
            positive = any(
                str(event.get("status", "") or "").upper() in {"PASS", "YES"}
                for event in matching
            )
            authoritative_failure = any(
                bool(event.get("authoritative", False))
                and str(event.get("status", "") or "").upper() in {"FAIL", "NO"}
                for event in matching
            )
            status = (
                "passed"
                if positive
                else "failed" if authoritative_failure else "unknown"
            )
            clauses.append(
                {
                    "id": clause_id,
                    "claim": str(clause.get("claim", "") or ""),
                    "status": status,
                    "evidence_count": len(matching),
                }
            )
        result = (
            "failed"
            if any(clause["status"] == "failed" for clause in clauses)
            else (
                "passed"
                if clauses and all(clause["status"] == "passed" for clause in clauses)
                else "unknown"
            )
        )
        verification_results.append({"key": key, "result": result, "clauses": clauses})

    verdict = (
        "failed"
        if any(item["result"] == "failed" for item in verification_results)
        else (
            "passed"
            if verification_results
            and all(item["result"] == "passed" for item in verification_results)
            else "inconclusive"
        )
    )
    failed_clauses = [
        {"verification_key": item["key"], "clause_id": clause["id"]}
        for item in verification_results
        for clause in item["clauses"]
        if clause["status"] == "failed"
    ]
    pending_clauses = [
        {"verification_key": item["key"], "clause_id": clause["id"]}
        for item in verification_results
        for clause in item["clauses"]
        if clause["status"] == "unknown"
    ]
    return {
        "verdict": verdict,
        "verifications": verification_results,
        "terminated_on_authoritative_failure": bool(failed_clauses),
        "failed_clauses": failed_clauses,
        "pending_clauses": pending_clauses,
    }


def build_verification_contract(
    goal: dict[str, Any], user_request: str = ""
) -> dict[str, Any]:
    """Create the smallest review-gated contract for an existing planner goal.

    Planner output remains editable in plan review. The whole request is retained as
    provenance until the review UI can collect finer spans and clause decomposition.
    Each verification records its span inside the user request and its span inside the
    goal statement; clauses record their spans inside the goal statement so that gap
    and overlap detection can be performed deterministically.
    """
    request = str(user_request or "")
    verifications = []
    for index, item in enumerate(_goal_verification_items(goal)):
        key = f"v{index}"
        claims = _split_claims(item)
        cursor = 0
        clauses = []
        for clause_index, claim in enumerate(claims):
            claim_offset = item.find(claim, cursor)
            if claim_offset < 0:
                claim_offset = cursor
            cursor = claim_offset + len(claim)
            clauses.append(
                {
                    "id": f"{key}.{clause_index}",
                    "claim": claim,
                    "goal_source_span": [claim_offset, cursor],
                    "channels": _default_channels_for_claim(claim),
                }
            )
        # Gaps between clauses (connectors/punctuation) are context, not condition.
        context_spans: list[list[int]] = []
        sorted_clauses = sorted(clauses, key=lambda c: c["goal_source_span"][0])
        prev_end = 0
        for clause in sorted_clauses:
            start, end = clause["goal_source_span"]
            if start > prev_end:
                context_spans.append([prev_end, start])
            prev_end = max(prev_end, end)
        if prev_end < len(item):
            context_spans.append([prev_end, len(item)])
        verifications.append(
            {
                "key": key,
                "statement": item,
                "request_source_span": [0, len(request)],
                "goal_source_span": [0, len(item)],
                "context_spans": context_spans,
                "clauses": clauses,
            }
        )
    contract = {
        "status": "contract_pending_review",
        "user_request": request,
        "verifications": verifications,
    }
    contract["coverage_map"] = build_coverage_map(contract)
    return contract


def build_coverage_map(contract: dict[str, Any]) -> dict[str, Any]:
    """Build a deterministic coverage map from request -> goals -> clauses.

    The map is used by the plan review UI and by validate_contract_spans to prove
    that every condition span at one layer is fully covered by the layer below it.
    """
    if not isinstance(contract, dict):
        return {}
    request = str(contract.get("user_request", "") or "")
    goal_spans: dict[str, list[int]] = {}
    condition_spans: list[list[int]] = []
    goals_coverage: dict[str, Any] = {}

    for verification in contract.get("verifications", []) or []:
        if not isinstance(verification, dict):
            continue
        key = str(verification.get("key", "") or "")
        req_span = verification.get("request_source_span", [])
        if _is_valid_span(req_span, 0, len(request)):
            condition_spans.append(list(req_span))
            goal_spans[key] = list(req_span)

        statement = str(verification.get("statement", "") or "")
        goal_source_span = verification.get("goal_source_span", [0, len(statement)])
        if not _is_valid_span(goal_source_span, 0, len(statement)):
            goal_source_span = [0, len(statement)]

        clause_spans = []
        for clause in verification.get("clauses", []) or []:
            if not isinstance(clause, dict):
                continue
            clause_span = clause.get("goal_source_span", [])
            if _is_valid_span(clause_span, 0, len(statement)):
                clause_spans.append(
                    {"id": str(clause.get("id", "") or ""), "span": list(clause_span)}
                )

        context_spans = [
            list(s)
            for s in verification.get("context_spans", []) or []
            if _is_valid_span(s, 0, len(statement))
        ]

        goals_coverage[key] = {
            "goal_source_span": list(goal_source_span),
            "clause_spans": clause_spans,
            "context_spans": context_spans,
        }

    return {
        "request": {
            "condition_spans": _merge_sorted_spans(condition_spans),
            "goal_spans": goal_spans,
        },
        "goals": goals_coverage,
    }


def _is_valid_span(span: Any, min_val: int, max_val: int) -> bool:
    """Check that span is a [start, end) integer pair within bounds."""
    if not isinstance(span, (list, tuple)) or len(span) != 2:
        return False
    start, end = span
    if not isinstance(start, int) or not isinstance(end, int):
        return False
    return min_val <= start < end <= max_val


def validate_contract_spans(contract: dict[str, Any]) -> dict[str, Any]:
    """Validate that condition spans have no gaps and no illegal overlaps.

    For Phase 1, every goal statement must be fully covered by its clauses.
    Context spans are tracked but not required to cover anything. Request-layer
    validation currently only checks that goal spans do not overlap; request
    condition parsing (context vs condition) will be added when the planner can
    emit finer request_source_spans.
    """
    if not isinstance(contract, dict):
        return {"valid": False, "gaps": [], "overlaps": []}

    gaps: list[dict[str, Any]] = []
    overlaps: list[dict[str, Any]] = []

    for verification in contract.get("verifications", []) or []:
        if not isinstance(verification, dict):
            continue
        key = str(verification.get("key", "") or "")
        statement = str(verification.get("statement", "") or "")
        goal_span = verification.get("goal_source_span", [0, len(statement)])
        if not _is_valid_span(goal_span, 0, len(statement)):
            goal_span = [0, len(statement)]

        context_spans = [
            list(s)
            for s in verification.get("context_spans", []) or []
            if _is_valid_span(s, 0, len(statement))
        ]

        clause_spans: list[tuple[list[int], str]] = []
        for clause in verification.get("clauses", []) or []:
            if not isinstance(clause, dict):
                continue
            clause_span = clause.get("goal_source_span", [])
            clause_id = str(clause.get("id", "") or "")
            if _is_valid_span(clause_span, 0, len(statement)):
                clause_spans.append((list(clause_span), clause_id))
            else:
                gaps.append(
                    {
                        "layer": "goal",
                        "key": key,
                        "start": goal_span[0],
                        "end": goal_span[1],
                        "reason": f"clause {clause_id} has invalid span",
                    }
                )

        # Detect overlaps between clause spans (context spans do not count)
        for i in range(len(clause_spans)):
            for j in range(i + 1, len(clause_spans)):
                span_a, id_a = clause_spans[i]
                span_b, id_b = clause_spans[j]
                if _spans_overlap(span_a, span_b):
                    overlaps.append(
                        {
                            "layer": "goal",
                            "key": key,
                            "span_a": {"id": id_a, "span": span_a},
                            "span_b": {"id": id_b, "span": span_b},
                        }
                    )

        # Detect gaps: goal statement must be covered by clauses, excluding context
        coverage = _subtract_spans([goal_span], context_spans)
        for c_start, c_end in coverage:
            covered = False
            for clause_span, _ in clause_spans:
                if clause_span[0] <= c_start and clause_span[1] >= c_end:
                    covered = True
                    break
            if not covered:
                # Find the exact gap region not covered by any clause
                gap_start = c_start
                while gap_start < c_end:
                    next_end = c_end
                    for clause_span, _ in clause_spans:
                        if clause_span[0] <= gap_start < clause_span[1]:
                            gap_start = clause_span[1]
                            break
                        if gap_start < clause_span[0] < next_end:
                            next_end = clause_span[0]
                    else:
                        gaps.append(
                            {
                                "layer": "goal",
                                "key": key,
                                "start": gap_start,
                                "end": next_end,
                                "reason": "clause coverage gap",
                            }
                        )
                        gap_start = next_end
                        break

    # Request-layer overlap detection between verification goal spans.
    # Currently request_source_span is a coarse placeholder ([0, len(request)])
    # produced by build_verification_contract. Skip overlap detection when all
    # spans are identical full-request placeholders; enable it once the planner
    # can emit finer request-level condition/context spans.
    request = str(contract.get("user_request", "") or "")
    request_spans: list[tuple[list[int], str]] = []
    for verification in contract.get("verifications", []) or []:
        if not isinstance(verification, dict):
            continue
        key = str(verification.get("key", "") or "")
        req_span = verification.get("request_source_span", [])
        if _is_valid_span(req_span, 0, len(request)):
            request_spans.append((list(req_span), key))

    all_full_request = (
        len(request_spans) > 0
        and all(span == [0, len(request)] for span, _ in request_spans)
    )
    if not all_full_request:
        for i in range(len(request_spans)):
            for j in range(i + 1, len(request_spans)):
                span_a, key_a = request_spans[i]
                span_b, key_b = request_spans[j]
                if _spans_overlap(span_a, span_b):
                    overlaps.append(
                        {
                            "layer": "request",
                            "span_a": {"key": key_a, "span": span_a},
                            "span_b": {"key": key_b, "span": span_b},
                        }
                    )

    return {"valid": not gaps and not overlaps, "gaps": gaps, "overlaps": overlaps}


def _spans_overlap(a: list[int], b: list[int]) -> bool:
    """Return True if two [start, end) spans intersect."""
    return a[0] < b[1] and b[0] < a[1]


def _subtract_spans(
    base_spans: list[list[int]], subtract_spans: list[list[int]]
) -> list[list[int]]:
    """Subtract a list of spans from a list of base spans, returning remaining intervals."""
    if not subtract_spans:
        return [list(s) for s in base_spans]
    result: list[list[int]] = []
    for base in base_spans:
        remaining = [list(base)]
        for sub in subtract_spans:
            new_remaining: list[list[int]] = []
            for seg in remaining:
                if seg[1] <= sub[0] or seg[0] >= sub[1]:
                    new_remaining.append(seg)
                else:
                    if seg[0] < sub[0]:
                        new_remaining.append([seg[0], sub[0]])
                    if seg[1] > sub[1]:
                        new_remaining.append([sub[1], seg[1]])
            remaining = new_remaining
        result.extend(remaining)
    return result


# UI Tree 已能证明的断言应直接跳过视觉通道，避免不必要的 VLM 调用。
_UI_TREE_CHANNELS = {"page_state", "element_state", "behavior_effect"}


def ui_tree_evidence_already_passes(ctx: Any, clause_id: str) -> bool:
    """若 clause_id 已存在 UI Tree 类 PASS/YES 证据，则视觉通道可短路。"""
    if not clause_id:
        return False
    for ev in getattr(ctx, "_evidence_events", []) or []:
        if not isinstance(ev, dict):
            continue
        if str(ev.get("clause_id", "") or "") != clause_id:
            continue
        if ev.get("channel") in _UI_TREE_CHANNELS and ev.get("status") in (
            "PASS",
            "YES",
        ):
            return True
    return False


def _merge_sorted_spans(spans: list[list[int]]) -> list[list[int]]:
    """Merge overlapping or adjacent spans and return sorted intervals."""
    if not spans:
        return []
    sorted_spans = sorted(spans, key=lambda s: (s[0], s[1]))
    merged: list[list[int]] = [list(sorted_spans[0])]
    for current in sorted_spans[1:]:
        last = merged[-1]
        if current[0] <= last[1]:
            last[1] = max(last[1], current[1])
        else:
            merged.append(list(current))
    return merged


def _normalize_verification_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    return re.sub(r"\s+", "", text)


def _goal_verification_items(goal: dict) -> list[str]:
    raw_items = goal.get("verification", []) if isinstance(goal, dict) else []
    return [str(item or "").strip() for item in raw_items if str(item or "").strip()]


def _build_verification_key_maps(goal: dict) -> tuple[dict[str, str], dict[str, str]]:
    key_lookup: dict[str, str] = {}
    key_to_item: dict[str, str] = {}
    for i, item in enumerate(_goal_verification_items(goal)):
        key = f"v{i}"
        key_to_item[key] = item
        key_lookup[item] = key
        normalized = _normalize_verification_text(item)
        if normalized:
            key_lookup[normalized] = key
    return key_lookup, key_to_item


def _determine_execution_status(state: dict) -> str:
    """判定执行状态：completed / exhausted / error / cancelled / device_offline。"""
    s = state.get("status", "")
    conclusion = state.get("conclusion", "")
    terminal_verdict = state.get("_terminal_verdict", "")
    if s == "cancelled":
        return "cancelled"
    if s == "device_offline":
        return "device_offline"
    # 结构化 verdict 优先：避免 Agent 漏写 DONE:/ABORT: 前缀导致误判
    if terminal_verdict == "passed":
        return "completed"
    if terminal_verdict == "failed":
        return "completed"
    done, abort = _detect_termination(conclusion)
    if done:
        return "completed"
    if abort:
        if "MAX_TURNS" in conclusion or "MAX_TOOL_CALLS" in conclusion:
            return "exhausted"
        # Agent 主动 ABORT 仍算 completed，verdict 由 test_verdict 决定
        return "completed"
    budget = _calc_budget_from_state(state)
    history = state.get("step_history", [])
    if len(history) >= budget["max_agent_iterations"]:
        return "exhausted"
    # 兜底：若 Agent 已实际推进了较多步骤（自然结束但 conclusion 无标准前缀，
    # 例如中途遇到可恢复的权限/系统弹窗被绕过后正常走完），不应误判为 error。
    # 仅当几乎未推进（step 极少，疑似一进来就崩）才保留 error。
    if len(history) >= 3:
        return "completed"
    return "error"


# End of verification helpers.

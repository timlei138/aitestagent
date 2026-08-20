"""验证项归一化 / key 映射 / 结果合并 / 执行状态判定。

从 agents/graph.py 拆出（重构 G3），仅移动代码、不改逻辑。
"""

from __future__ import annotations

import re
from typing import Any

from agents.budget import _calc_budget_from_state
from agents.loop_control import _detect_termination

_CLAUSE_BOUNDARY = re.compile(r"[，,；;]+|(?:并且|同时|以及|且)")

# M1-4: 删除关键词通道推断（discrepancy_detection_core_plan Phase 0）。
# 原 _default_channels_for_claim 依据 claim 关键词（"状态"/"提示"/"显示"...）猜测
# 证据通道，是 v4 通道漂移与 v5 漏判的根因之一。Plan §6 要点4：channel 由 spec 谓词
# 决定（Phase 1）；M1 阶段 spec:null 的 claim 回退到全通道（含 behavior_effect），
# 由 authoritative 标志承接确定性判定，不再靠关键词白名单过滤。
# 全通道顺序：UI Tree 类（page_state/element_state/behavior_effect）优先，其余视觉/
# 文本通道兜底——与 Plan §6 要点4 回退一致。
_ALL_CHANNELS_FALLBACK = (
    "page_state",
    "element_state",
    "behavior_effect",
    "ui_text",
    "vision_verify",
    "click_and_check",
)


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
            in_scope = [
                event
                for event in events or []
                if isinstance(event, dict)
                and str(event.get("verification_key", "") or "") == key
                and str(event.get("clause_id", "") or "") == clause_id
            ]
            # M1-2: authoritative 证据无视 channels 过滤，直接进匹配集。
            # 非 authoritative 证据仍按 channels 过滤（保留 test_contract_ignores_
            # undeclared_or_free_text_evidence 的语义）。
            matching = [
                event
                for event in in_scope
                if bool(event.get("authoritative", False))
                or str(event.get("channel", "") or "") in channels
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
            # M1-1 + M1-2 优先级链（discrepancy_detection_core_plan §6 要点①）：
            # authoritative FAIL > 任意 PASS(权威/非权威) > unknown。
            # authoritative FAIL 压住一切 positive（根治 v5：positive 命中仍 failed）；
            # 非 authoritative PASS 不得压过 authoritative FAIL。
            # 注意：authoritative PASS 是 positive 的子集，无需单独变量；
            # 优先级仅两层（FAIL 优先 / 否则 PASS / 否则 unknown）。
            status = (
                "failed"
                if authoritative_failure
                else "passed" if positive else "unknown"
            )
            # M2（Plan §7 增强）：零证据 unknown 标记 unverified。区分「agent 从
            # 未对此 clause 调用验证工具（漏验）」与「调用过但证据不足/通道不匹配」。
            # 两者都判 unknown + review_required，但 unverified 用于前端/报告暴露
            # agent 幻觉式跳过验证（如口头声称「v0-v4 已通过」却零工具证据）。
            unverified = status == "unknown" and len(matching) == 0
            clauses.append(
                {
                    "id": clause_id,
                    "claim": str(clause.get("claim", "") or ""),
                    "status": status,
                    "evidence_count": len(matching),
                    "unverified": unverified,
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
    # M4a (M1): verification 项支持对象形式 {"claim":..., "spec":{...}} 或
    # 旧字符串形式。spec 描述 clause 的结构化期望，由自动匹配器(auto_record_evidence)
    # 在 perceive 后落成证据；spec:null 的 clause 回退 M1 手动 verify。
    raw_items = goal.get("verification", []) if isinstance(goal, dict) else []
    parsed_items: list[tuple[str, dict | None]] = []
    for raw in raw_items:
        if isinstance(raw, dict):
            parsed_items.append(
                (str(raw.get("claim", "") or "").strip(), raw.get("spec"))
            )
        else:
            parsed_items.append((str(raw or "").strip(), None))

    verifications = []
    for index, (item, item_spec) in enumerate(parsed_items):
        if not item:
            continue
        key = f"v{index}"
        claims = _split_claims(item)
        cursor = 0
        clauses = []
        for clause_index, claim in enumerate(claims):
            claim_offset = item.find(claim, cursor)
            if claim_offset < 0:
                claim_offset = cursor
            cursor = claim_offset + len(claim)
            # spec 只挂首条 clause（代表整条 verification 的终态期望），
            # 其余子 clause 保持 spec=None 走手动。
            spec = item_spec if clause_index == 0 else None
            clauses.append(
                {
                    "id": f"{key}.{clause_index}",
                    "claim": claim,
                    "spec": spec,
                    "goal_source_span": [claim_offset, cursor],
                    # M1-4: 删关键词推断（Plan Phase 0）。M1 阶段 spec:null 回退全通道
                    # （含 behavior_effect），与 §6 要点4 一致；M4 后由 spec 谓词决定。
                    "channels": list(_ALL_CHANNELS_FALLBACK),
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


def _render_checklist_view(
    clause_state: dict[str, Any] | None,
    contract: dict[str, Any] | None,
    evidence_events: list[dict[str, Any]] | None = None,
) -> str:
    """Render the real-time verification checklist (✓/✗/○) for the agent.

    Called from the inner `_llm` node every LLM turn, using the freshly
    recomputed `_ctx._clause_state` (from evaluate_verification) so the agent
    sees up-to-date progress and stops re-touching passed clauses. This is a
    soft constraint (tell the agent), not a code-enforced interception.

    Returns "" when there is nothing to render (no clause_state / no clauses).
    """
    if not isinstance(clause_state, dict):
        return ""
    verifications = clause_state.get("verifications", []) or []
    if not isinstance(verifications, list) or not verifications:
        return ""

    # key -> clause_id -> [ (status, artifact_ref) ]  from evidence events,
    # so we can attach the latest screenshot path to each clause line.
    evidence_index: dict[tuple[str, str], tuple[str, str]] = {}
    for ev in evidence_events or []:
        if not isinstance(ev, dict):
            continue
        key = str(ev.get("verification_key", "") or "")
        cid = str(ev.get("clause_id", "") or "")
        if not key or not cid:
            continue
        status = str(ev.get("status", "") or "").upper()
        artifact = str(ev.get("artifact_ref", "") or "")
        prev = evidence_index.get((key, cid))
        # PASS/YES wins for display; otherwise keep first seen.
        if prev is None or (status in {"PASS", "YES"} and prev[0] not in {"PASS", "YES"}):
            evidence_index[(key, cid)] = (status, artifact)

    # contract clauses carry `channels` (declared evidence channels).
    contract_clause_channels: dict[tuple[str, str], list[str]] = {}
    for v in (contract or {}).get("verifications", []) or []:
        if not isinstance(v, dict):
            continue
        vkey = str(v.get("key", "") or "")
        for c in v.get("clauses", []) or []:
            if not isinstance(c, dict):
                continue
            cid = str(c.get("id", "") or "")
            chs = c.get("channels", []) or []
            contract_clause_channels[(vkey, cid)] = [
                str(ch) for ch in chs
            ] or ["any"]

    lines: list[str] = ["检查项清单（✓=已通过 ✗=已失败 ○=待验证）:"]
    for v in verifications:
        if not isinstance(v, dict):
            continue
        key = str(v.get("key", "") or "")
        clauses = v.get("clauses", []) or []
        for c in clauses:
            if not isinstance(c, dict):
                continue
            cid = str(c.get("id", "") or "")
            claim = str(c.get("claim", "") or "")
            status = str(c.get("status", "") or "")
            if status == "passed":
                mark = "✓"
            elif status == "failed":
                mark = "✗"
            else:
                mark = "○"
            chs = contract_clause_channels.get((key, cid), ["any"])
            ch_str = ",".join(chs) if chs != ["any"] else "any"
            ev = evidence_index.get((key, cid))
            if ev and ev[1]:
                ev_part = f" | 证据: {ev[0]} 截图 {ev[1]}"
            elif ev and ev[0]:
                ev_part = f" | 证据: {ev[0]}"
            else:
                ev_part = f" | 通道: {ch_str}"
            lines.append(f" [{mark}] {key}::{cid} {claim}{ev_part}")
    lines.append(
        "规则：○ 项结束前必须补齐证据；✓/✗ 项禁止再 open 编辑器微调或重复 assert。"
    )
    return "\n".join(lines)


# ── M4a: 自动证据匹配器 ────────────────────────────────────────────────

# M4a (M2): 谓词词汇表。每个谓词声明「找不到元素时是 unknown 还是 PASS/FAIL」。
# 不对称设计（见 m4_auto_evidence_plan §3）：element_exists 找不到=unknown，
# element_absent 找不到=PASS，避免把「元素不在当前可见区」误判成矛盾。
_PREDICATES: dict[str, dict[str, str]] = {
    "page_is": {"source": "activity", "not_found": "unknown"},
    "page_contains": {"source": "text", "not_found": "unknown"},
    "element_exists": {"source": "element", "not_found": "unknown"},
    "element_absent": {"source": "element", "not_found": "pass"},
    "element_enabled": {"source": "element", "not_found": "unknown"},
    "element_disabled": {"source": "element", "not_found": "unknown"},
    "element_checked": {"source": "element", "not_found": "unknown"},
    "list_count": {"source": "element", "not_found": "unknown"},
}


def _activity_short(activity: str) -> str:
    return (activity or "").split(".")[-1]


def _element_matches(el: Any, target: str) -> bool:
    """Match a UIElement against a spec target by resource-id leaf or label/text."""
    if not isinstance(el, object) or not hasattr(el, "resource_id"):
        return False
    t = str(target or "").strip().lower()
    if not t:
        return False
    rid = str(getattr(el, "resource_id", "") or "")
    rid_leaf = rid.split("/")[-1].split(".")[-1].lower()
    label = str(getattr(el, "label", "") or "").lower()
    text = str(getattr(el, "text", "") or "").lower()
    desc = str(getattr(el, "content_desc", "") or "").lower()
    return t == rid_leaf or t in label or t in text or t in desc


def _find_elements(u: Any, target: str) -> list[Any]:
    elements = getattr(u, "elements", []) or []
    return [e for e in elements if _element_matches(e, target)]


def _match_spec(
    spec: dict[str, Any], u: Any, current_app: dict[str, Any]
) -> dict[str, Any] | None:
    """M4a (M2): 纯代码 spec 匹配。

    Returns None (无法判定/未知) | {"status":"PASS"/"FAIL","authoritative":bool,"fact":dict}.
    保守原则：拿不准（元素不唯一/找不到且谓词非 absent）就不写。
    """
    if not isinstance(spec, dict):
        return None
    predicate = str(spec.get("predicate", "") or "").strip().lower()
    if predicate not in _PREDICATES:
        return None
    target = str(spec.get("target", "") or "")
    expected = spec.get("expected")
    activity = _activity_short(str((current_app or {}).get("activity", "") or ""))
    page_text = " ".join(
        [
            str(getattr(u, "page_title", "") or ""),
            *[
                str(getattr(e, "text", "") or "")
                + " "
                + str(getattr(e, "content_desc", "") or "")
                + " "
                + str(getattr(e, "resource_id", "") or "")
                for e in getattr(u, "elements", []) or []
            ],
        ]
    ).lower()

    if predicate == "page_is":
        if not target or not activity:
            return None
        if target.lower() in activity.lower() or activity.lower() in target.lower():
            return {
                "status": "PASS",
                "authoritative": False,
                "fact": {"predicate": predicate, "activity": activity, "target": target},
            }
        return None  # 页面对不上不算矛盾，只是还没到

    if predicate == "page_contains":
        if not target:
            return None
        if target.lower() in page_text:
            return {
                "status": "PASS",
                "authoritative": False,
                "fact": {"predicate": predicate, "target": target},
            }
        return None

    # 以下谓词都依赖元素匹配
    matched = _find_elements(u, target) if target else []
    n = len(matched)

    if predicate == "element_exists":
        if n >= 1:
            return {
                "status": "PASS",
                "authoritative": False,
                "fact": {
                    "predicate": predicate,
                    "target": target,
                    "matched_count": n,
                },
            }
        return None

    if predicate == "element_absent":
        if n == 0:
            return {
                "status": "PASS",
                "authoritative": False,
                "fact": {"predicate": predicate, "target": target},
            }
        # 找到元素 = 矛盾（确定性 FAIL）
        el = matched[0]
        return {
            "status": "FAIL",
            "authoritative": True,
            "fact": {
                "predicate": predicate,
                "target": target,
                "matched_count": n,
                "rid": str(getattr(el, "resource_id", "") or ""),
            },
        }

    # enabled/disabled/checked/list_count 需要唯一元素，避免误判
    if predicate in ("element_enabled", "element_disabled", "element_checked"):
        if n != 1:
            return None  # 多匹配/无匹配 → 不写（避免歧义误判）
        el = matched[0]
        if predicate == "element_enabled":
            ok = bool(getattr(el, "enabled", False))
            return {
                "status": "PASS" if ok else "FAIL",
                "authoritative": not ok,
                "fact": {
                    "predicate": predicate,
                    "target": target,
                    "enabled": ok,
                    "rid": str(getattr(el, "resource_id", "") or ""),
                },
            }
        if predicate == "element_disabled":
            ok = not bool(getattr(el, "enabled", False))
            return {
                "status": "PASS" if ok else "FAIL",
                "authoritative": bool(getattr(el, "enabled", False)),
                "fact": {
                    "predicate": predicate,
                    "target": target,
                    "enabled": bool(getattr(el, "enabled", False)),
                    "rid": str(getattr(el, "resource_id", "") or ""),
                },
            }
        if predicate == "element_checked":
            actual = getattr(el, "checked", None)
            if actual is None:
                return None
            ok = bool(actual) == bool(expected)
            # element_checked 读的是 AccessibilityNodeInfo.isChecked()（实时 checked
            # 过渡态），与 assert_behavior_effect 的 toggled 谓词同源 —— 历史已知在部分
            # ROM 上「抖动/误读」，属当前态检查而非确定性 before/after 反证。故 FAIL
            # 一律 authoritative=False（与 toggled 口径一致，见 verify.py toggled 分支），
            # 不触发 fail-fast；真·失败交由 visual_check 高置信 FAIL 做权威确认。
            # 注意：element_enabled/element_disabled 读 View.isEnabled()（可靠稳定），
            # 仍保留 authoritative=True，不在本规则覆盖范围内。
            return {
                "status": "PASS" if ok else "FAIL",
                "authoritative": False,
                "fact": {
                    "predicate": predicate,
                    "target": target,
                    "checked": bool(actual),
                    "expected": bool(expected),
                    "rid": str(getattr(el, "resource_id", "") or ""),
                },
            }

    if predicate == "list_count":
        if expected is None:
            return None
        try:
            want = int(expected)
        except (TypeError, ValueError):
            return None
        if n == 0:
            return None  # anchor 无匹配 → 无法判定
        ok = n == want
        return {
            "status": "PASS" if ok else "FAIL",
            "authoritative": not ok,
            "fact": {
                "predicate": predicate,
                "anchor": target,
                "count": n,
                "expected": want,
            },
        }

    return None


def _spec_channel(predicate: str) -> str:
    """M4a: 写侧 channel 由 spec 谓词决定（m4_auto_evidence_plan §5 要点）。"""
    if predicate in ("page_is", "page_contains"):
        return "page_state"
    return "element_state"


def _has_same_evidence(
    events: list[dict[str, Any]], key: str, clause_id: str, channel: str, status: str
) -> bool:
    """M4a (M3 去重): 已存在相同 (key,clause_id,channel,status) 的非权威证据则跳过。"""
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        if (
            str(ev.get("verification_key", "") or "") == key
            and str(ev.get("clause_id", "") or "") == clause_id
            and str(ev.get("channel", "") or "") == channel
            and str(ev.get("status", "") or "").upper() == status.upper()
            and not bool(ev.get("authoritative", False))
        ):
            return True
    return False


def auto_record_evidence(
    ctx: Any,
    contract: dict[str, Any],
    u: Any,
    current_app: dict[str, Any],
) -> int:
    """M4a (M3): perceive() 之后调用，把当前页面确定性事实自动落成证据。

    Returns the number of evidence events written. spec:null 的 clause 跳過
    （留给 LLM 手动 verify）。保守原则：_match_spec 返回 None 不写；已存在相同
    非权威证据去重；authoritative FAIL 例外（总写入，触发 fail-fast）。
    """
    if not isinstance(contract, dict) or not hasattr(ctx, "_evidence_events"):
        return 0
    written = 0
    # 注意：不能用 `getattr(ctx, "_evidence_events", []) or []` —— 当属性本身是
    # 空列表 [] 时，`[] or []` 会返回一个新的空列表，append 不会反映到 ctx 上。
    events = getattr(ctx, "_evidence_events", None)
    if events is None:
        events = []
        ctx._evidence_events = events
    for v in contract.get("verifications", []) or []:
        if not isinstance(v, dict):
            continue
        key = str(v.get("key", "") or "")
        for clause in v.get("clauses", []) or []:
            if not isinstance(clause, dict):
                continue
            spec = clause.get("spec")
            if not isinstance(spec, dict):
                continue  # spec:null → 手动 verify
            r = _match_spec(spec, u, current_app)
            if r is None:
                continue  # 无法判定 → 不写
            channel = _spec_channel(str(spec.get("predicate", "") or ""))
            status = str(r.get("status", "") or "").upper()
            # M3 去重：非权威证据若已存在相同记录则跳过（避免每次 perceive 重复写）
            if not bool(r.get("authoritative", False)) and _has_same_evidence(
                events, key, str(clause.get("id", "")), channel, status
            ):
                continue
            events.append(
                {
                    "verification_key": key,
                    "clause_id": str(clause.get("id", "")),
                    "channel": channel,
                    "status": status,
                    "authoritative": bool(r.get("authoritative", False)),
                    "fact": r.get("fact", {}),
                    "auto": True,  # 标记自动证据，区别于 LLM 手动 assert
                }
            )
            written += 1
    return written


# End of verification helpers.

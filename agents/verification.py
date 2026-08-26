"""验证项归一化 / key 映射 / 结果合并 / 执行状态判定。

从 agents/graph.py 拆出（重构 G3），仅移动代码、不改逻辑。
"""

from __future__ import annotations

import difflib
import logging
import re
from typing import Any

from agents.budget import _calc_budget_from_state
from agents.loop_control import _detect_termination

logger = logging.getLogger(__name__)

# F3（agent_evolution_plan §5）：去掉 ASCII `,` 边界——枚举逗号不拆
# （「周一,周三,周五」是一个判定点，不是三个）；中文全角分隔符仍拆。
# 该机械切分已降级为仅处理旧字符串项的 fallback；新格式由 planner 在
# 规划期以 {"claim", "clauses":[...]} 对象显式给出语义合并结果。
_CLAUSE_BOUNDARY = re.compile(r"[，；;]+|(?:并且|同时|以及|且)")

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
    """Split only explicit conjunction boundaries; review can refine remaining ambiguity.

    F3 后仅作旧字符串项的机械兜底：planner 已升级为在规划期以嵌套对象
    （{"claim", "clauses":[...]}）显式给出语义合并后的 clause，不再依赖此处切分。
    """
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


def _parse_explicit_clauses(raw_clauses: Any) -> list[dict] | None:
    """F3：解析 planner 显式给出的语义 clause 列表（agent_evolution_plan §5）。

    每项为 {"claim":..., "spec":{...}|null}；容忍纯字符串项（spec=null）。
    无有效 clause 时返回 None，调用方回退 _split_claims 机械兜底。
    """
    if not isinstance(raw_clauses, list):
        return None
    clauses: list[dict] = []
    for entry in raw_clauses:
        if isinstance(entry, dict):
            claim = str(entry.get("claim", "") or "").strip()
            if not claim:
                continue
            spec = entry.get("spec")
            clauses.append(
                {"claim": claim, "spec": spec if isinstance(spec, dict) else None}
            )
        else:
            claim = str(entry or "").strip()
            if claim:
                clauses.append({"claim": claim, "spec": None})
    return clauses or None


def _explicit_clause_spans(item: str, claims: list[str]) -> list[list[int]]:
    """F3：planner 语义 clause 的 goal_source_span。

    全部逐字子串（按序可定位）→ 用实际位置；任一条被 planner 改写措辞
    （非逐字子串）→ 整组按声明长度占比顺序占位。占位保证无重叠、完整覆盖
    statement——span 此时是近似溯源而非精确对位（与既有 find 失败时
    cursor 兜底的口径一致），validate_contract_spans 不产生 gap/overlap 误报。
    """
    n = len(item)
    found: list[list[int]] = []
    cursor = 0
    all_literal = bool(claims)
    for claim in claims:
        offset = item.find(claim, cursor)
        if offset < 0:
            all_literal = False
            break
        found.append([offset, offset + len(claim)])
        cursor = offset + len(claim)
    if all_literal:
        return found

    weights = [max(1, len(claim)) for claim in claims]
    total_w = sum(weights)
    spans: list[list[int]] = []
    pos = 0
    for i, weight in enumerate(weights):
        start = pos
        if i == len(weights) - 1:
            end = n
        else:
            ideal = pos + max(1, round(n * weight / total_w))
            # 每条后续 clause 至少留 1 字符，避免末条零宽/倒退
            upper = n - (len(weights) - 1 - i)
            end = min(max(ideal, start + 1), max(upper, start + 1))
        spans.append([start, end])
        pos = end
    return spans


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
    # M4a (M1): verification 项支持对象形式 {"claim":..., "spec":{...}}、
    # F3 嵌套对象形式 {"claim":..., "clauses":[...]} 或旧字符串形式。
    # spec 描述 clause 的结构化期望，由自动匹配器(auto_record_evidence)
    # 在 perceive 后落成证据；spec:null 的 clause 回退 M1 手动 verify。
    raw_items = goal.get("verification", []) if isinstance(goal, dict) else []
    parsed_items: list[tuple[str, dict | None, list[dict] | None]] = []
    for raw in raw_items:
        if isinstance(raw, dict):
            parsed_items.append(
                (
                    str(raw.get("claim", "") or "").strip(),
                    raw.get("spec"),
                    _parse_explicit_clauses(raw.get("clauses")),
                )
            )
        else:
            parsed_items.append((str(raw or "").strip(), None, None))

    verifications = []
    for index, (item, item_spec, explicit_clauses) in enumerate(parsed_items):
        if not item:
            continue
        key = f"v{index}"
        # F3：planner 显式给出语义 clause 时直接采用（规划期完成语义合并，
        # 对应「人类一个操作顺带验多个点」）；仅旧字符串项走 _split_claims 机械兜底。
        prepared: list[tuple[str, dict | None, list[int]]] = []
        if explicit_clauses:
            claims = [
                (
                    c["claim"],
                    c["spec"] if c["spec"] is not None else (item_spec if j == 0 else None),
                )
                for j, c in enumerate(explicit_clauses)
            ]
            spans = _explicit_clause_spans(item, [claim for claim, _ in claims])
            prepared = [
                (claim, spec, span) for (claim, spec), span in zip(claims, spans)
            ]
        else:
            cursor = 0
            for clause_index, claim in enumerate(_split_claims(item)):
                claim_offset = item.find(claim, cursor)
                if claim_offset < 0:
                    claim_offset = cursor
                end = claim_offset + len(claim)
                cursor = end
                # spec 只挂首条 clause（代表整条 verification 的终态期望），
                # 其余子 clause 保持 spec=None 走手动。
                spec = item_spec if clause_index == 0 else None
                prepared.append((claim, spec, [claim_offset, end]))
        clauses = []
        for clause_index, (claim, spec, span) in enumerate(prepared):
            clauses.append(
                {
                    "id": f"{key}.{clause_index}",
                    "claim": claim,
                    "spec": spec,
                    "goal_source_span": span,
                    # M1-4: 删关键词推断（Plan Phase 0）。spec:null 保持全通道回退。
                    # F1 已开闸（agent_evolution_plan §3）：带 spec 的 clause 只认
                    # 其确定性证据通道（写读同源 _spec_channel）——UI 树可判定的事实
                    # 不再被 vision PASS 兜底放行。开闸依据：2026-08-24 真机采样
                    # 分桶，谓词/结构不支持类占比 20% ≤ 30% 硬阈值。
                    "channels": (
                        [_spec_channel(str(spec.get("predicate", "") or ""))]
                        if isinstance(spec, dict)
                        else list(_ALL_CHANNELS_FALLBACK)
                    ),
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
    """verification 项的纯文本形式（供 key 映射 / agent 提示）。

    F4/F3 对象项（{"claim":...} / {"claim":..., "clauses":[...]}）取其 claim 文本，
    避免 str(dict) 把 Python repr 当验证文本喂给 agent。
    """
    raw_items = goal.get("verification", []) if isinstance(goal, dict) else []
    items: list[str] = []
    for raw in raw_items:
        if isinstance(raw, dict):
            text = str(raw.get("claim", "") or "").strip()
        else:
            text = str(raw or "").strip()
        if text:
            items.append(text)
    return items


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


def _clause_evidence_hints(
    merged_verifications: list[dict[str, Any]],
    key_to_item: dict[str, str],
) -> tuple[list[str], list[str]]:
    """生成 agent 历史注入的「已通过 / 待验证」clause 身份标签列表。

    返回 ``(passed_tags, pending_tags)``。pending 只收 unknown 且证据尝试 <3 的
    clause——已有 ≥3 条匹配证据仍 unknown 视为「尽力仍不可证」（典型：规划期拆出
    了系统给不出权威 PASS 的手段性 clause），不再列入待验证清单、不再据此驳回
    DONE，避免不可满足 clause 把 run 拖入回环（用例 168 实测：agent 从已到达的
    目标页被逼导航回起点反复补证直至被取消）。最终报告仍如实 unknown +
    review_required；F1「零证据漏验」判定不受影响（unverified 仅在零证据时标记）。
    """
    def _tag(vkey: str, clause: dict) -> str:
        """生成「clause 身份」标签，供 agent 原样填进 assert 的 verification_key/clause_id。

        关键：agent 之前把所有 assert 都打上 v0.0，导致证据错配、其余 clause 全部
        unknown（即前端「未验证」）。这里把 evaluator 契约里**精确的 clause_id + 所需
        channel** 直接给出来，让 agent 知道每次 assert 该 stamp 什么身份、用哪个通道工具。
        """
        cid = str(clause.get("id", "") or "")
        chs = clause.get("channels") or []
        ch_str = ",".join(str(c) for c in chs) if chs else "any"
        claim = str(clause.get("claim", "") or key_to_item.get(vkey, "") or "")
        return f"[{vkey}::{cid} | channels:{ch_str}] {claim}"

    passed: list[str] = []
    pending: list[str] = []
    for entry in merged_verifications or []:
        if not isinstance(entry, dict):
            continue
        vkey = str(entry.get("key", "") or "")
        # failed 整项不进 pending：确定性矛盾走 authoritative FAIL 终止，
        # 不该让 agent 去「补」。
        entry_failed = str(entry.get("result", "") or "") == "failed"
        for clause in entry.get("clauses", []) or []:
            if not isinstance(clause, dict):
                continue
            status = str(clause.get("status", "") or "")
            if status == "passed":
                passed.append(_tag(vkey, clause))
            elif (
                status == "unknown"
                and not entry_failed
                and int(clause.get("evidence_count", 0) or 0) < 3
            ):
                pending.append(_tag(vkey, clause))
    return passed, pending


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
    # F6①（plan §8）收窄：必须有真实推进（至少一步 success/continue）才算
    # completed——纯失败的 3 步崩溃不再因步数达标被洗白成 completed。
    if len(history) >= 3 and any(
        isinstance(s, dict)
        and str(s.get("status", "") or "") in ("success", "continue")
        for s in history
    ):
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


def _fuzzy_candidates(u: Any, target: str) -> list[str]:
    """埋点辅助：给 spec target 找页面上最接近的真实控件名（同义改写检测线索）。

    仅用于 None 归因日志，不参与判定；cutoff 0.6 下「课程名称 vs 课程名」类
    近似词能被召回，帮助人工区分「同义改写落空」与「真没走到该页面」。
    """
    t = str(target or "").strip().lower()
    if not t:
        return []
    vocab: set[str] = set()
    for e in getattr(u, "elements", []) or []:
        for attr in ("label", "text", "content_desc"):
            val = str(getattr(e, attr, "") or "").strip()
            if val:
                vocab.add(val)
        rid_leaf = str(getattr(e, "resource_id", "") or "").split("/")[-1].split(".")[-1]
        if rid_leaf:
            vocab.add(rid_leaf)
    if not vocab:
        return []
    return difflib.get_close_matches(t, sorted(vocab), n=3, cutoff=0.6)


def _match_none_reason(
    spec: dict[str, Any], u: Any, current_app: dict[str, Any]
) -> tuple[str, list[str]]:
    """埋点辅助：分类 `_match_spec` 返回 None 的原因（只归因，不改判定行为）。

    分桶口径见 agent_evolution_plan §3：`page_not_arrived` / `element_not_found`
    属「控件缺失类」（用例没走到那步，不计入 F1 开闸分母）；其余归「谓词或结构
    不支持类」。code 无法语义区分「同义改写落空」与「真不在页面上」——由
    fuzzy hints 字段供人工/聚合脚本判别。
    """
    predicate = str(spec.get("predicate", "") or "").strip().lower()
    if predicate not in _PREDICATES:
        return "unsupported_predicate", []
    target = str(spec.get("target", "") or "")
    activity = _activity_short(str((current_app or {}).get("activity", "") or ""))
    if predicate == "page_is":
        if not target or not activity:
            return "missing_target", []
        return "page_not_arrived", []
    if predicate == "page_contains":
        if not target:
            return "missing_target", []
        return "text_missing", _fuzzy_candidates(u, target)
    if not target:
        # 元素类谓词缺 target 属结构问题（planner 漏填），非页面缺失
        return "missing_target", []
    matched = _find_elements(u, target)
    n = len(matched)
    if predicate == "list_count":
        try:
            int(str(spec.get("expected")))  # noqa: B007 — 只验可解析性
        except (TypeError, ValueError):
            return "missing_expected", []
        if n >= 2:
            return "element_ambiguous", []
        if n == 0:
            return "element_not_found", _fuzzy_candidates(u, target)
        return "unclassified", []  # n==1 理应出判定，理论不可达
    if predicate in ("element_enabled", "element_disabled"):
        # n==1 必出 PASS/FAIL 判定，None 只可能来自 n!=1
        if n >= 2:
            return "element_ambiguous", []
        return "element_not_found", _fuzzy_candidates(u, target)
    if predicate == "element_checked":
        if n >= 2:
            return "element_ambiguous", list(_fuzzy_candidates(u, target))
        if n == 1:
            # 唯一元素 checked 态读不到（AccessibilityNodeInfo 抖动）→ None
            return "checked_unreadable", []
        return "element_not_found", _fuzzy_candidates(u, target)
    # element_exists / element_absent：absent 有 n==0→PASS 兜底，落到这里即 exists n==0
    return "element_not_found", _fuzzy_candidates(u, target)


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
    # label 兜底链含 associated_label（兄弟 TextView 文本）——只拼 text/desc 会漏掉
    # 字段标签类元素（2026-08-24 run103147 实证），与手动 verify 的匹配口径保持一致。
    page_text = " ".join(
        [
            str(getattr(u, "page_title", "") or ""),
            *[
                str(getattr(e, "text", "") or "")
                + " "
                + str(getattr(e, "content_desc", "") or "")
                + " "
                + str(getattr(e, "resource_id", "") or "")
                + " "
                + str(getattr(e, "label", "") or "")
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
                # expected_disabled 供 reporter 差异报告 _extract_expected_actual
                # 提取「期望 vs 实际」（自动证据与手动 verify 的 fact 同构）。
                "fact": {
                    "predicate": predicate,
                    "target": target,
                    "expected_disabled": True,
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


def _log_none_outcome(
    key: str, clause: dict[str, Any], spec: dict[str, Any], u: Any, current_app: dict[str, Any]
) -> None:
    """埋点（plan §6 最小埋点）：_match_spec 返回 None 的归因分桶日志。

    分桶码见 `_match_none_reason`；hints 为页面上与 target 近似的真实控件名，
    用于区分「同义改写落空」（如「课程名称」vs「课程名」）与「真没走到该页面」。
    """
    reason, hints = _match_none_reason(spec, u, current_app)
    logger.info(
        "[auto-evidence] outcome=none key=%s clause=%s pred=%s target=%r reason=%s hints=%s",
        key,
        str(clause.get("id", "") or ""),
        str(spec.get("predicate", "") or ""),
        str(spec.get("target", "") or ""),
        reason,
        "|".join(hints) or "-",
    )


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
    if not isinstance(contract, dict):
        # 注意不要用 `not hasattr(ctx, "_evidence_events")` 早退：该属性由首个手动
        # verify 才创建，早退会使之前所有 perceive 的自动匹配静默空转（2026-08-24
        # run103147 实证）。缺属性时由下方惰性初始化补上。
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
                _log_none_outcome(key, clause, spec, u, current_app)
                continue  # 无法判定 → 不写
            channel = _spec_channel(str(spec.get("predicate", "") or ""))
            status = str(r.get("status", "") or "").upper()
            # M3 去重：非权威证据若已存在相同记录则跳过（避免每次 perceive 重复写）
            if not bool(r.get("authoritative", False)) and _has_same_evidence(
                events, key, str(clause.get("id", "")), channel, status
            ):
                logger.info(
                    "[auto-evidence] outcome=dedup_skip key=%s clause=%s pred=%s",
                    key,
                    str(clause.get("id", "")),
                    str(spec.get("predicate", "")),
                )
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
            logger.info(
                "[auto-evidence] outcome=hit key=%s clause=%s pred=%s status=%s auth=%s",
                key,
                str(clause.get("id", "")),
                str(spec.get("predicate", "")),
                status,
                bool(r.get("authoritative", False)),
            )
            written += 1
    return written


# End of verification helpers.

"""验证项归一化 / key 映射 / 结果合并 / 执行状态判定。

从 agents/graph.py 拆出（重构 G3），仅移动代码、不改逻辑。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from agents.budget import _calc_budget_from_state
from agents.loop_control import _detect_termination


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


def _resolve_verification_key(
    assertion: dict[str, Any], key_lookup: dict[str, str]
) -> str:
    explicit = str(assertion.get("key", "") or "").strip()
    if explicit:
        return explicit
    item = str(assertion.get("item", "") or "").strip()
    if item and item in key_lookup:
        return key_lookup[item]
    normalized = _normalize_verification_text(item)
    if normalized and normalized in key_lookup:
        return key_lookup[normalized]
    if item.startswith("v") and item[1:].isdigit():
        return item
    if normalized:
        digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:8]
        return f"dyn_{digest}"
    return "dyn_unknown"


def _merge_goal_verification_results(
    goal: dict, assertions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    verification_items = _goal_verification_items(goal)
    if not verification_items:
        return []
    key_lookup, key_to_item = _build_verification_key_maps(goal)
    merged: dict[str, dict[str, Any]] = {
        key: {
            "key": key,
            "item": item,
            "result": "unknown",
            "detail": "需要人工复核：未获得该验证项的自动化证据",
            "screenshot": "",
            "review_required": True,
        }
        for key, item in key_to_item.items()
    }
    for assertion in assertions or []:
        if not isinstance(assertion, dict):
            continue
        key = _resolve_verification_key(assertion, key_lookup)
        if key not in merged:
            continue
        incoming_result = str(assertion.get("result", "unknown") or "unknown")
        incoming = {
            "key": key,
            "item": key_to_item[key],
            "result": incoming_result,
            "detail": str(assertion.get("detail", "") or ""),
            "screenshot": str(assertion.get("screenshot", "") or ""),
            "review_required": (
                bool(assertion.get("review_required", False))
                or incoming_result == "unknown"
            ),
        }
        current = merged[key]
        current_result = str(current.get("result", "unknown") or "unknown")
        if incoming_result == "passed":
            merged[key] = incoming
        elif incoming_result == "failed":
            if current_result != "passed":
                merged[key] = incoming
        elif current_result == "unknown":
            merged[key] = incoming
    return [merged[f"v{i}"] for i in range(len(verification_items))]


def _determine_execution_status(state: dict) -> str:
    """判定执行状态：completed / exhausted / error / cancelled / device_offline。"""
    s = state.get("status", "")
    conclusion = state.get("conclusion", "")
    if s == "cancelled":
        return "cancelled"
    if s == "device_offline":
        return "device_offline"
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
    return "error"


def _collect_verification_results(goal: dict) -> tuple[str, list]:
    """从 ToolContext._verifications 收集 assert_verification 的结构化结果。"""
    from tools import get_tool_context  # 延迟 import：避免循环并兼容测试 monkeypatch

    ctx = get_tool_context()
    assertions = getattr(ctx, "_verifications", []) if ctx else []
    verification_items = _goal_verification_items(goal)
    if verification_items:
        merged = _merge_goal_verification_results(goal, assertions)
        failed = sum(1 for a in merged if a.get("result") == "failed")
        passed = sum(1 for a in merged if a.get("result") == "passed")
        if failed > 0:
            verdict = "failed"
        elif passed == len(merged):
            verdict = "passed"
        else:
            verdict = "inconclusive"
        return verdict, merged
    if not assertions:
        return "passed", []
    normalized_assertions = []
    for assertion in assertions:
        if not isinstance(assertion, dict):
            continue
        normalized_assertions.append(
            {
                "key": str(assertion.get("key", "") or ""),
                "item": str(assertion.get("item", "") or ""),
                "result": str(assertion.get("result", "unknown") or "unknown"),
                "detail": str(assertion.get("detail", "") or ""),
                "screenshot": str(assertion.get("screenshot", "") or ""),
            }
        )
    failed = sum(1 for a in normalized_assertions if a.get("result") == "failed")
    passed = sum(1 for a in normalized_assertions if a.get("result") == "passed")
    if failed > 0:
        verdict = "failed"
    elif normalized_assertions and passed == len(normalized_assertions):
        verdict = "passed"
    else:
        verdict = "inconclusive"
    return verdict, normalized_assertions

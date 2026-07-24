"""预算与 token 估算（纯函数）。

从 agents/graph.py 拆出（重构 G2），仅移动代码、不改逻辑。
"""

from __future__ import annotations

import re


def _estimate_tokens(text: str) -> int:
    """粗略 token 估算：CJK 单字 + 英文词。"""
    if not text:
        return 0
    cjk = re.findall(r"[\u4e00-\u9fff]", text)
    words = re.findall(r"[A-Za-z0-9_]+", text)
    punct = re.findall(r"[^\sA-Za-z0-9_\u4e00-\u9fff]", text)
    return len(cjk) + len(words) + max(1, len(punct) // 2)


def _clip_to_token_budget(text: str, max_tokens: int) -> tuple[str, bool]:
    if _estimate_tokens(text) <= max_tokens:
        return text, False
    chars = list(text)
    lo, hi = 0, len(chars)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _estimate_tokens("".join(chars[:mid])) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    clipped = "".join(chars[:lo]).rstrip() + "\n...[truncated by token budget]"
    return clipped, True


def _safe_len(value) -> int:
    if isinstance(value, list):
        return len(value)
    return 0


def _calc_budget(goal: dict) -> dict[str, int]:
    pages = _safe_len(goal.get("target_pages", [])) if isinstance(goal, dict) else 0
    verifications = (
        _safe_len(goal.get("verification", [])) if isinstance(goal, dict) else 0
    )
    max_tool_calls_total = 36 + pages * 12 + verifications * 10
    max_agent_iterations = min(max(2 + pages + verifications, 8), 24)
    # 每轮子图预算作为断路器，不应过小导致在关键动作前被截断。
    # 迭代层(route)负责主导结束；这里取较宽上限，避免"即将点击关键元素时 __end__"。
    max_turns_per_iteration = min(max(max_tool_calls_total, 10), 64)
    return {
        "max_tool_calls_total": max_tool_calls_total,
        "max_agent_iterations": max_agent_iterations,
        "max_turns_per_iteration": max_turns_per_iteration,
    }


def _calc_budget_from_state(state: dict) -> dict[str, int]:
    return _calc_budget(state.get("goal_description", {}) or {})

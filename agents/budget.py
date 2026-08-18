"""预算与 token 估算（纯函数）。

从 agents/graph.py 拆出（重构 G2），仅移动代码、不改逻辑。
"""

from __future__ import annotations

import re
from collections.abc import Mapping


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
    # T9: 提高预算上限。原公式（36 + pages*12 + verifications*10）对多子目标
    # 任务偏紧——例如「设为当前(确定/取消)+删除全部+验证空状态」(3 页/4 验证)
    # 仅 112 次，agent 在跑完所有验证、evaluator 尚未来得及收敛时就被
    # MAX_TOOL_CALLS 掐断，被判失败。系数整体上调约 1.6x，并放宽两个 cap，
    # 给复杂任务留足收尾余量（T8 已消除禁用按钮空转，放宽不会 reintroduce 死循环）。
    max_tool_calls_total = 48 + pages * 20 + verifications * 18
    max_agent_iterations = min(max(2 + pages + verifications, 10), 40)
    # 每轮子图预算作为断路器，不应过小导致在关键动作前被截断。
    # 迭代层(route)负责主导结束；这里取较宽上限，避免"即将点击关键元素时 __end__"。
    max_turns_per_iteration = min(max(max_tool_calls_total, 10), 120)
    return {
        "max_tool_calls_total": max_tool_calls_total,
        "max_agent_iterations": max_agent_iterations,
        "max_turns_per_iteration": max_turns_per_iteration,
    }


def _calc_budget_from_state(state: Mapping[str, object]) -> dict[str, int]:
    goal = state.get("goal_description", {}) or {}
    return _calc_budget(goal)


def _calc_mode_phase_budget(state: Mapping[str, object], mode: str) -> int:
    """Return the bounded per-phase iteration allowance for degradable modes."""
    global_budget = _calc_budget_from_state(state)["max_agent_iterations"]
    if mode == "guided":
        return max(3, global_budget // 2)
    if mode == "direct":
        return max(2, global_budget // 3)
    return global_budget

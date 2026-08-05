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
    # T9: 提高预算上限。原公式（36 + pages*12 + verifications*10）对多子目标
    # 任务偏紧——例如「设为当前(确定/取消)+删除全部+验证空状态」(3 页/4 验证)
    # 仅 112 次，agent 在跑完所有验证、还没来得及调用 report_done 收尾时就被
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


def _replay_key_actions(goal: dict) -> list:
    """取 v4/v3 execution_plan 的 key_actions；无则空列表。"""
    if not isinstance(goal, dict):
        return []
    plan = goal.get("execution_plan")
    if not isinstance(plan, dict):
        return []
    if plan.get("schema_version") == 4:
        effective = plan.get("effective")
        actions = (effective or {}).get("key_actions") if isinstance(effective, dict) else []
    else:
        actions = plan.get("key_actions")
    return [a for a in (actions or []) if isinstance(a, dict)]


def _calc_budget_from_state(state: dict) -> dict[str, int]:
    goal = state.get("goal_description", {}) or {}
    budget = _calc_budget(goal)
    # 回放模式：主图每 iteration 只推进 1 个脚本步骤（one_step / 直执），
    # 迭代预算必须覆盖脚本长度 + recovery 预算 + entry 对齐/收尾余量，
    # 否则 26 步脚本会在默认 cap(≤40) 内被 route_after_agent 提前收敛。
    if str(state.get("_run_type", "") or "") == "rerun":
        actions = _replay_key_actions(goal)
        if actions:
            recovery_budget = int(goal.get("replay_recovery_budget", 3) or 3)
            budget["max_agent_iterations"] = min(
                len(actions) + max(1, recovery_budget) + 4, 80
            )
    return budget

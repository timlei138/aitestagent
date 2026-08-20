# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import logging
import os
import re
import hashlib
from datetime import datetime
from typing import Any, Annotated

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

from config import TestConfig, UNKNOWN_ROUTE_LIMIT, UNKNOWN_RESTRICT_AFTER
from agents.state import TestState
from agents.budget import _calc_budget, _calc_budget_from_state, _calc_mode_phase_budget
from agents.rag_context import (
    _should_force_request_knowledge,
    _should_include_rag,
)
from agents.verification import (
    _determine_execution_status,
    evaluate_verification,
)

# M2（Plan §7）：unknown（inconclusive）路由上限。达到后 route_after_evaluator 强制
# 收敛到 reporter，治「问题2-RootA 图不终止」。常量统一定义于 config.py。
from tools import get_tool_context
from agents.nodes import (
    agent_node,
    direct_node,
    evaluator_node,
    mode_selection_node,
    mode_transition_node,
    plan_review_node,
    planner_node,
    reporter_node,
)

logger = logging.getLogger(__name__)

_relational_db = None


def set_relational_db(db) -> None:
    global _relational_db
    _relational_db = db


# ═══ WebSocket 实时事件回调 ═══
_ws_emit_callback = None


def set_ws_emit_callback(callback) -> None:
    global _ws_emit_callback
    _ws_emit_callback = callback


def route_after_evaluator(state: TestState) -> str:
    try:
        ctx = get_tool_context()
    except Exception:
        ctx = None
    contract = state.get("verification_contract", {}) if isinstance(state, dict) else {}
    if not isinstance(contract, dict) or contract.get("status") != "approved":
        logger.error("Route: reporter (verification contract is not approved)")
        return "reporter"
    evaluation = state.get("clause_state", {})
    if not isinstance(evaluation, dict):
        evaluation = evaluate_verification(
            contract, getattr(ctx, "_evidence_events", []) if ctx else []
        )
    if evaluation["verdict"] in {"passed", "failed"}:
        logger.info("Route: reporter (contract verdict=%s)", evaluation["verdict"])
        return "reporter"

    # M2（Plan §7）：unknown（inconclusive）路由上限。达到上限后强制收敛到 reporter，
    # 治「问题2-RootA 图不终止」——避免 inconclusive 一直吃满 agent 预算、期间做契约外探索。
    # 计数与耗尽标志由 evaluator_node 经累加 reducer 维护，独立于 max_agent_iterations。
    if state.get("_unknown_exhausted") or int(
        state.get("_unknown_route_count", 0) or 0
    ) >= UNKNOWN_ROUTE_LIMIT:
        logger.warning(
            "Route: reporter (unknown routes exhausted: %d)",
            int(state.get("_unknown_route_count", 0) or 0),
        )
        return "reporter"

    # 证据驱动终止机制 · 第 3 层：M2 兜底（死循环护栏）已在上方优先判定，此处是「正常范围内」
    # 的强制回环。agent 已声明 DONE（status=="success"），但 verdict 仍为 inconclusive
    # （有 unknown、无 failed；failed 已在最上方 verdict 分支被 reporter 截走）。
    # 此时「结束只能靠证据判定」的契约必须由代码强制：不放过 DONE，把图回环到 agent 节点。
    # 专门提示不依赖跨层 flag——agent 在历史中能看到自己上轮的 report_done 被驳回，且 agent_node
    # 每轮都会重新注入「待验证清单」+「若你上轮已 DONE 但清单非空则不得重申 DONE」提示。
    # 注意：每次回环都会经过 evaluator_node 累加 _unknown_route_count，故 M2 上限仍能兜底防死循环。
    if state.get("status") == "success" and evaluation["verdict"] == "inconclusive":
        logger.warning(
            "Route: agent (DONE loopback: status=success but verdict=inconclusive)"
        )
        return "agent"
    if state.get("execution_mode") == "direct":
        # If direct already degraded once, do not route back to direct.
        if int(state.get("_direct_downgrade_count", 0) or 0) >= 1:
            logger.info("Route: agent (direct downgrade already consumed)")
            return "agent"
        return "direct"
    if _should_downgrade_guided(state):
        logger.info("Route: mode transition (guided -> explore)")
        return "mode_transition"
    n = len(state.get("step_history", []))
    budget = _calc_budget_from_state(state)
    if state.get("status") in ("success", "fail", "stopped"):
        # "stopped" 是 _stop_or_continue 写入的 stop 收敛态。_stop_or_continue
        # 已显式 goto="reporter"，正常路径不会走到这里；保留这条分支作为防御——
        # 万一 LangGraph 未来忽略 Command.goto，路由仍能正确收敛（避免在
        # agent ↔ agent 之间死循环到 max_iterations）。
        logger.info("Route: reporter (status=%s, steps=%d)", state.get("status"), n)
        return "reporter"
    if n >= budget["max_agent_iterations"]:
        logger.warning("Route: reporter (max iterations %d)", n)
        return "reporter"
    logger.info("Route: agent (iteration %d)", n + 1)
    return "agent"


def _should_downgrade_guided(state: TestState) -> bool:
    if state.get("execution_mode") != "guided":
        return False
    if int(state.get("_guided_downgrade_count", 0) or 0) >= 1:
        return False
    history = state.get("step_history", []) or []
    if not history:
        return False
    last_step = history[-1] if isinstance(history[-1], dict) else {}
    guided_steps = sum(
        1
        for step in history
        if isinstance(step, dict) and step.get("execution_mode") == "guided"
    )
    if guided_steps >= _calc_mode_phase_budget(state, "guided"):
        return True
    if bool(last_step.get("loop_detected", False)):
        return True
    action_log = state.get("_tool_calls_log", []) or []
    if not action_log:
        return False
    last_action = action_log[-1] if isinstance(action_log[-1], dict) else {}
    return str(last_action.get("status_code", "") or "").upper() in {
        "ERROR",
        "FAIL",
        "TIMEOUT",
        "NOT_FOUND",
    }


# ═══ GRAPH ═══


def route_after_plan_review(state: TestState) -> str:
    if state.get("status") == "cancelled":
        return "reporter"
    return "mode_selection"


def route_after_mode_selection(state: TestState) -> str:
    """Route after mode_selection_node.

    契约收敛：verification_contract 未 approved 时直接进 reporter（避免 agent
    空跑一轮再被 evaluator 判 inconclusive）。approved 后按 execution_mode 选
    direct / agent。此函数替代原匿名 lambda，便于测试直接断言。
    """
    contract = state.get("verification_contract", {})
    if not isinstance(contract, dict) or contract.get("status") != "approved":
        return "reporter"
    return "direct" if state.get("execution_mode") == "direct" else "agent"


def route_start(state: TestState) -> str:
    """Every run creates and reviews a current contract before execution."""
    return "planner"


def build_graph(config: TestConfig) -> StateGraph:
    g = StateGraph(TestState)
    g.add_node("planner", planner_node)
    g.add_node("plan_review", plan_review_node)
    g.add_node("mode_selection", mode_selection_node)
    g.add_node("mode_transition", mode_transition_node)
    g.add_node("direct", direct_node)
    g.add_node("agent", agent_node)
    g.add_node("evaluator", evaluator_node)
    g.add_node("reporter", reporter_node)
    g.add_conditional_edges(
        START, route_start, {"planner": "planner", "agent": "agent"}
    )
    g.add_edge("planner", "plan_review")
    g.add_conditional_edges(
        "plan_review",
        route_after_plan_review,
        {"mode_selection": "mode_selection", "reporter": "reporter"},
    )
    g.add_conditional_edges(
        "mode_selection",
        route_after_mode_selection,
        {"direct": "direct", "agent": "agent", "reporter": "reporter"},
    )
    g.add_edge("direct", "evaluator")
    g.add_conditional_edges(
        "agent", lambda state: "evaluator", {"evaluator": "evaluator"}
    )
    g.add_conditional_edges(
        "evaluator",
        route_after_evaluator,
        {
            "agent": "agent",
            "direct": "direct",
            "mode_transition": "mode_transition",
            "reporter": "reporter",
        },
    )
    g.add_edge("mode_transition", "agent")
    g.add_edge("reporter", END)
    return g.compile(checkpointer=MemorySaver())


# ═══ HELPERS ═══

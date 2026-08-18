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

from config import TestConfig
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
        lambda state: "direct" if state.get("execution_mode") == "direct" else "agent",
        {"direct": "direct", "agent": "agent"},
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

# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import logging
import os
import re
import hashlib
from datetime import datetime
from typing import Any, Annotated

from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage
from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from langchain_core.runnables import RunnableConfig

from config import TestConfig
from llm.clients import (
    create_llm_client,
    _call_with_retry,
    _is_rate_limit_error,
    _default_should_retry,
)
from agents.state import TestState
from agents.budget import (
    _calc_budget,
    _calc_budget_from_state,
    _clip_to_token_budget,
    _estimate_tokens,
    _safe_len,
)
from agents.loop_control import (
    _DONE_PATTERN,
    _build_call_signature,
    _build_page_signature,
    _cooldown_group,
    _detect_termination,
    _output_has_page_change,
    _resolve_click_fallback,
    _resolve_click_match_mode,
)
from agents.rag_context import (
    _apply_click_preferences,
    _rag_ctx,
    _should_force_query_app_knowledge,
    _should_include_rag,
)
from agents.verification import (
    _build_verification_key_maps,
    _collect_verification_results,
    _determine_execution_status,
    _goal_verification_items,
    _merge_goal_verification_results,
    _normalize_verification_text,
    _resolve_verification_key,
)
from agents.llm_runtime import (
    _FINALIZATION_REMAINING_TOOL_BUDGET,
    _build_tool_target,
    _call_retry,
    _call_retry_should_retry,
    _ensure_device_alive,
    _llm_cfg,
    _run_agent,
)
from tools import AGENT_TOOLS, get_tool_context, _extract_click_preferences_from_rag
from agents.nodes import (
    agent_node,
    plan_review_node,
    planner_node,
    reporter_node,
)

import app_paths

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


def route_after_agent(state: TestState) -> str:
    try:
        ctx = get_tool_context()
    except Exception:
        ctx = None
    goal = state.get("goal_description", {}) if isinstance(state, dict) else {}
    merged = _merge_goal_verification_results(
        goal if isinstance(goal, dict) else {},
        getattr(ctx, "_verifications", []) if ctx else [],
    )
    if merged and all(str(item.get("result", "") or "") == "passed" for item in merged):
        logger.info("Route: reporter (all verifications passed)")
        return "reporter"
    n = len(state.get("step_history", []))
    budget = _calc_budget_from_state(state)
    if state.get("status") in ("success", "fail"):
        logger.info("Route: reporter (status=%s, steps=%d)", state.get("status"), n)
        return "reporter"
    if n >= budget["max_agent_iterations"]:
        logger.warning("Route: reporter (max iterations %d)", n)
        return "reporter"
    logger.info("Route: agent (iteration %d)", n + 1)
    return "agent"


# ═══ GRAPH ═══


def route_after_plan_review(state: TestState) -> str:
    if state.get("status") == "cancelled":
        return "reporter"
    return "agent"


def build_graph(config: TestConfig) -> StateGraph:
    g = StateGraph(TestState)
    g.add_node("planner", planner_node)
    g.add_node("plan_review", plan_review_node)
    g.add_node("agent", agent_node)
    g.add_node("reporter", reporter_node)
    g.add_edge(START, "planner")
    g.add_edge("planner", "plan_review")
    g.add_conditional_edges(
        "plan_review",
        route_after_plan_review,
        {"agent": "agent", "reporter": "reporter"},
    )
    g.add_conditional_edges(
        "agent", route_after_agent, {"agent": "agent", "reporter": "reporter"}
    )
    g.add_edge("reporter", END)
    return g.compile(checkpointer=MemorySaver())


# ═══ HELPERS ═══

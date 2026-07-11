"""RAG 上下文注入与点击偏好应用。

从 agents/graph.py 拆出（重构 G3），仅移动代码、不改逻辑。
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from agents.state import TestState
from tools import _extract_click_preferences_from_rag

logger = logging.getLogger(__name__)


def _rag_ctx(kb, app_package: str, user_request: str = "") -> str:
    """查询 RAG 获取上下文：人工知识 + 操作经验（2 sections）。"""
    if not kb:
        return ""
    parts = []
    # 1. 人工知识（一次查询，Python 侧自动分组为全局知识 + App 操作前提）
    rules = kb.query_curated_rules(app_package, top_k=20)
    if rules:
        parts.append("## 人工知识\n" + rules)
    # 2. 操作经验
    if user_request:
        exp = kb.query_experience(app_package, user_request[:50], top_k=3)
        if exp:
            parts.append("## 操作经验\n" + "\n".join(f"- {e['content']}" for e in exp))
    return "\n\n".join(parts)


def _apply_click_preferences(
    ctx: Any, rag_summary: str, effective_app_package: str = ""
) -> None:
    """将 RAG 文本中的点击偏好解析并缓存到 ToolContext。"""
    if not ctx:
        return
    prefs = _extract_click_preferences_from_rag(rag_summary or "")
    if not prefs:
        return
    try:
        prefs["app_package"] = str(effective_app_package or "")
        prefs["rag_hash"] = hashlib.md5(
            (rag_summary or "").encode("utf-8")
        ).hexdigest()[:12]
        setattr(ctx, "_click_preferences", prefs)
    except Exception:
        logger.debug("apply click preferences failed", exc_info=True)


def _should_include_rag(state: TestState, effective_app_package: str) -> bool:
    """Phase 1: 从"每轮预注入"收敛为"首轮 + 触发式注入"。

    触发条件：
    1) 首轮；
    2) app_package 变化；
    3) 最近一步出现循环/无进展信号或失败。
    """
    history = state.get("step_history", []) or []
    if not history:
        return True

    if not bool(state.get("_rag_injected_once", False)):
        return True

    last_pkg = str(state.get("_rag_last_app_package", "") or "")
    if effective_app_package and effective_app_package != last_pkg:
        return True

    last = history[-1] if history else {}
    if bool(last.get("loop_detected")):
        return True
    if str(last.get("status", "")).lower() == "fail":
        return True
    obs = str(last.get("observation", "") or "")
    if any(k in obs for k in ("NO_PROGRESS", "COOLDOWN", "LOOP_DETECTED")):
        return True
    return False


def _should_force_query_app_knowledge(
    state: TestState, include_rag: bool, rag_summary: str
) -> bool:
    """在高风险轮次给出明确知识查询指令（仅触发时）。"""
    history = state.get("step_history", []) or []
    if not history:
        return False
    last = history[-1]
    obs = str(last.get("observation", "") or "")
    risky = (
        bool(last.get("loop_detected")) or str(last.get("status", "")).lower() == "fail"
    )
    if not risky and not any(
        k in obs for k in ("NO_PROGRESS", "COOLDOWN", "LOOP_DETECTED")
    ):
        return False
    # 仍未得到可用 RAG 时，强提示先查知识
    if include_rag and rag_summary:
        return False
    return True

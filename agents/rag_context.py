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
    """Inject only reviewed v2 semantic knowledge; task plans live in the relational store."""
    if not kb:
        return ""
    parts = []
    del user_request
    headings = {
        "constraint": "## 执行约束",
        "negative_knowledge": "## 已知禁止/失败模式",
        "semantic_hint": "## 语义提示（不得覆盖验收）",
    }
    for knowledge_type, entries in kb.query_semantic_knowledge(
        app_package, top_k=20
    ).items():
        if entries:
            parts.append(
                headings[knowledge_type]
                + "\n"
                + "\n".join(f"- {entry}" for entry in entries)
            )
    return "\n\n".join(parts)


def retrieve_knowledge(
    *,
    app_package: str,
    purpose: str,
    query: str = "",
    page_signature: str = "",
    verification_fingerprint: str = "",
    environment_key: str = "",
    task_signature: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Single structured v2 knowledge entry (Plan 4.3 / 5.6).

    Framework nodes and LLM-facing tools read each knowledge source through this one
    boundary, so the retrieval purpose is explicit and every result carries its source.
    """
    if purpose == "task_plan":
        from agents.graph import _relational_db
        from tools import get_tool_context

        db = _relational_db
        kb = None
        try:
            from tools import get_tool_context

            ctx = get_tool_context()
            kb = getattr(ctx, "knowledge_base", None) if ctx else None
        except RuntimeError:
            kb = None

        if not db:
            return {"purpose": purpose, "source": "none", "items": []}

        # Prefer vector semantic recall + relational filtering when possible.
        if kb and task_signature:
            candidates = kb.query_task_plan_summaries(
                app_package=app_package,
                query=query or task_signature.get("user_request_template", "") or "",
                top_k=5,
            )
            compatible_items: list[dict[str, Any]] = []
            from agents.plan_extractor import (
                environment_compatibility_score,
                task_signatures_compatible,
            )

            for candidate in candidates or []:
                metadata = candidate.get("metadata", {}) or {}
                plan_id = metadata.get("plan_id")
                if not plan_id:
                    continue
                full_plan = db.get_full_execution_plan(str(plan_id))
                if not full_plan:
                    continue
                cand_signature = full_plan.get("task_signature", {}) or {}
                # Verification fingerprint safety gate.
                cand_vf = cand_signature.get("verification_fingerprint", "")
                if (
                    verification_fingerprint
                    and cand_vf
                    and verification_fingerprint != cand_vf
                ):
                    continue
                if not task_signatures_compatible(task_signature, cand_signature):
                    continue
                # Environment compatibility: scored instead of exact equality.
                cand_env = full_plan.get("environment_key", "")
                env_score = environment_compatibility_score(cand_env, environment_key)
                if not env_score["compatible"]:
                    continue
                full_plan["environment_compatibility_score"] = env_score["score"]
                full_plan["environment_compatibility_reasons"] = env_score["reasons"]
                compatible_items.append(full_plan)
            if compatible_items:
                # Prefer highest environment compatibility, then quality.
                compatible_items.sort(
                    key=lambda p: (
                        float(p.get("environment_compatibility_score", 0.0) or 0.0),
                        float(p.get("quality_score", 0.0) or 0.0),
                    ),
                    reverse=True,
                )
                best = compatible_items[0]
                return {
                    "purpose": purpose,
                    "source": "vector_then_relational",
                    "items": [best],
                    "candidates": len(candidates),
                    "environment_compatibility_score": best.get(
                        "environment_compatibility_score"
                    ),
                    "environment_compatibility_reasons": best.get(
                        "environment_compatibility_reasons"
                    ),
                }

        # Fallback to relational exact/semantic match.
        plan = db.find_matching_execution_plan(
            app_package,
            query,
            verification_fingerprint=verification_fingerprint,
            environment_key=environment_key,
            task_signature=task_signature,
        )
        return {
            "purpose": purpose,
            "source": "relational",
            "items": [plan] if plan else [],
            "environment_compatibility_score": plan.get(
                "environment_compatibility_score"
            )
            if plan
            else None,
            "environment_compatibility_reasons": plan.get(
                "environment_compatibility_reasons"
            )
            if plan
            else None,
        }
    if purpose == "locator":
        from agents.graph import _relational_db
        from tools import get_tool_context

        ctx = get_tool_context()
        db = getattr(ctx, "relational_db", None) or _relational_db
        if not db:
            return {"purpose": purpose, "source": "none", "items": []}
        rows = db.query_locator_knowledge(
            app_package, alias=query, page_signature=page_signature
        )
        return {"purpose": purpose, "source": "relational", "items": rows}
    if purpose == "semantic":
        from tools import get_tool_context

        ctx = get_tool_context()
        kb = getattr(ctx, "knowledge_base", None)
        if not kb:
            return {"purpose": purpose, "source": "none", "items": []}
        grouped = kb.query_semantic_knowledge(app_package, top_k=5)
        return {"purpose": purpose, "source": "vector", "items": grouped}
    if purpose == "action":
        # Action knowledge is carried by the selected plan actions in state; there is
        # no independent action-retrieval source yet.
        return {"purpose": purpose, "source": "none", "items": []}
    raise ValueError(f"unknown knowledge purpose: {purpose!r}")


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


def _should_force_request_knowledge(
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

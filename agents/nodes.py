"""LangGraph 节点：planner / agent / reporter / plan_review + prompt 装配。

从 agents/graph.py 拆出（重构 G5），仅移动代码、不改逻辑。
reporter_node 读取 graph 的可变全局 _relational_db，通过函数内延迟 import 获取当前值。
build_graph 也以延迟 import 方式引用本模块，避免加载期循环依赖。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
from datetime import datetime
from typing import Any, Annotated

from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage
from langchain_core.prompts import ChatPromptTemplate
from langgraph.types import Command
from langchain_core.runnables import RunnableConfig

from config import TestConfig
from llm.clients import create_llm_client
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
    _ensure_device_alive,
    _llm_cfg,
    _run_agent,
)
from tools import AGENT_TOOLS, get_tool_context, _extract_click_preferences_from_rag

import app_paths

logger = logging.getLogger(__name__)


def _load_prompt(name: str) -> str:
    _dir = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(_dir, "prompts", name)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


PLANNER_SYSTEM = _load_prompt("planner.txt")
AGENT_SYSTEM = _load_prompt("agent.txt")

PLANNER_TEMPLATE = ChatPromptTemplate.from_messages(
    [
        SystemMessage(content=PLANNER_SYSTEM),
        (
            "user",
            """Create a test goal for:
Request: {user_request}
Target app: {app_name} ({app_package})

{rag_context}""",
        ),
    ]
)


# ═══ Tool calling sub-graph ═══

# ═══ NODES ═══


def planner_node(state: TestState, config: RunnableConfig) -> Command:
    cfg: TestConfig = config["configurable"]["test_config"]
    llm = _llm_cfg(cfg)
    ctx = get_tool_context()
    kb = ctx.knowledge_base if ctx else None
    rag = _rag_ctx(kb, state.get("app_package", ""), state.get("user_request", ""))
    budget_violation_count = int(state.get("budget_violation_count", 0) or 0)
    rag, rag_truncated = _clip_to_token_budget(rag, 500)
    if rag_truncated:
        budget_violation_count += 1
    msgs = PLANNER_TEMPLATE.format_messages(
        user_request=state.get("user_request", ""),
        app_name=state.get("app_name", ""),
        app_package=state.get("app_package", ""),
        rag_context=rag,
    )
    cl = create_llm_client(
        provider=llm["provider"],
        model=llm["model"],
        api_key=llm["api_key"],
        base_url=llm["base_url"],
    )
    import time as _time

    _t0 = _time.time()
    raw = _call_retry(llm["provider"], cl.invoke, msgs) if cl else None
    logger.info("Planner LLM: %.1fs", _time.time() - _t0)
    if raw is None:
        goal = {
            "goal": state.get("user_request", ""),
            "target_pages": [],
            "verification": [],
            "hints": [],
        }
    else:
        text = raw.content if hasattr(raw, "content") else str(raw)
        goal = _parse_goal(text)
    logger.info("Planner: %s", goal.get("goal", "")[:80])
    # 每次新 run 开始时清空 RAG 查询缓存和计数器
    try:
        ctx_cleanup = get_tool_context()
        if ctx_cleanup:
            ctx_cleanup._rag_query_cache = {}
            ctx_cleanup._run_tag = (
                config.get("configurable", {}).get("thread_id", "") or ""
            )
            ctx_cleanup._rag_query_count = 0
            ctx_cleanup._rag_same_app_count = 0
            ctx_cleanup._rag_cross_app_count = 0
            ctx_cleanup._rag_empty_hit_count = 0
    except Exception:
        pass
    return Command(
        update={
            "goal_description": goal,
            "step_history": [],
            "messages": [],
            "started_at": datetime.now().isoformat(),
            "step_times": [],
            "budget_violation_count": budget_violation_count,
            "llm_call_count": 0,
            "tool_call_400_count": 0,
            "tool_call_400_rate": 0.0,
            "_rag_injected_once": False,
            "_rag_last_app_package": "",
            "_knowledge_query_hint_injected": False,
            "_last_page_app_key": "",
            "_last_clickable_count": 0,
        }
    )


def agent_node(state: TestState, config: RunnableConfig) -> Command:
    cfg: TestConfig = config["configurable"]["test_config"]
    llm = _llm_cfg(cfg)

    # ── 设备健康检查：断开时等待重连，重试 2 次仍失败则直接终止 ──
    if not _ensure_device_alive(max_retries=2, wait_sec=5.0):
        history = state.get("step_history", [])
        si = len(history) + 1
        conclusion = "ABORT: 设备在测试过程中断开连接，尝试重连 2 次失败，无法继续"
        logger.warning("Agent #%d: device lost, aborting", si)
        nh = list(history) + [
            {
                "index": si,
                "intent": conclusion[:80],
                "action_type": "device_lost",
                "target": "",
                "page_from": "",
                "page_to": "",
                "duration_ms": 0,
                "status": "fail",
                "observation": conclusion,
                "raw_observation": conclusion,
                "screenshot_path": "",
                "anomaly": None,
            }
        ]
        return Command(
            update={
                "step_history": nh,
                "status": "fail",
                "conclusion": conclusion,
            }
        )

    # Page info
    ctx = get_tool_context()
    page_info = "unknown"
    pid = ""
    current_app_key = ""
    n_clickable = 0
    t0 = 0
    if ctx and ctx.perceiver:
        try:
            import time as _time

            t0 = _time.time()
            u = ctx.perceiver.perceive()
            dt = _time.time() - t0
            act = u.activity.split(".")[-1] if u.activity else "?"
            title = u.page_title or ""
            pid = act + "「" + title + "」" if title else act
            pkg = (
                (ctx.device.current_app() or {}).get("package", "")
                if ctx.device
                else ""
            )
            current_app_key = f"{pkg}:{act}"
            n_clickable = sum(1 for e in u.elements if e.clickable and e.label)
            lines = [
                "page=" + pid,
                (
                    "layout=two_pane（结构分区标签，不保证左右方位）"
                    if u.layout == "two_pane"
                    else "layout=" + u.layout
                ),
                "clickable=" + str(n_clickable),
            ]
            click_idx = 0
            for e in u.elements:
                if e.clickable and e.label:
                    role = e.role or ""
                    rid = (e.resource_id or "").split("/")[-1] if e.resource_id else ""
                    extra = ""
                    if role:
                        extra += " [" + role + "]"
                    if rid:
                        extra += " rid=" + rid
                    lines.append(f"  - [{click_idx}] " + e.label + extra)
                    click_idx += 1
                    if len(lines) > 25:
                        break
            page_info = "\n".join(lines)
            logger.info(
                "Agent perceive: %.1fs page=%s clickable=%d", dt, pid, n_clickable
            )
        except Exception as e:
            page_info = "error: " + str(e)
            logger.warning("Agent perceive failed: %s", e)

    # Goal + history
    goal = state.get("goal_description", {})
    budget = _calc_budget(goal)
    goal_str = json.dumps(goal, ensure_ascii=False, indent=2)
    history = state.get("step_history", [])
    budget_violation_count = int(state.get("budget_violation_count", 0) or 0)
    effective_app_package = goal.get("app_package", "") or state.get("app_package", "")
    include_rag = _should_include_rag(state, effective_app_package)
    rag_summary = ""
    if include_rag:
        rag_summary = _rag_ctx(
            ctx.knowledge_base if ctx else None,
            effective_app_package,
            state.get("user_request", ""),
        )
        rag_summary, rag_truncated = _clip_to_token_budget(rag_summary, 500)
        if rag_truncated:
            budget_violation_count += 1
    if include_rag and rag_summary and ctx:
        _apply_click_preferences(ctx, rag_summary, effective_app_package)
    hist_lines = [
        f"  [{s.get('status','')}] {s.get('intent','')}: {str(s.get('observation',''))[:100]}"
        for s in history[-10:]
    ]
    hist_str = "\n".join(hist_lines) if hist_lines else "(none)"
    key_lookup, key_to_item = _build_verification_key_maps(goal)
    if ctx:
        ctx._verification_key_map = key_lookup
        ctx._verification_items_by_key = key_to_item
        merged_verifications = _merge_goal_verification_results(
            goal, getattr(ctx, "_verifications", []) or []
        )
        passed_items = [
            f"[{entry.get('key', '')}] {entry.get('item', '')}"
            for entry in merged_verifications
            if str(entry.get("result", "") or "") == "passed"
        ]
        if passed_items:
            hist_str += "\n\n已通过验证: " + "; ".join(passed_items)

    # Messages — always include goal + page for context
    msgs = list(state.get("messages", []))
    if not msgs:
        msgs = [SystemMessage(content=AGENT_SYSTEM)]
    used_tool_calls_before = len(state.get("_tool_calls_log", []) or [])
    remaining_tool_budget = budget["max_tool_calls_total"] - used_tool_calls_before
    finalization_hint_injected = bool(state.get("_finalization_hint_injected", False))
    if (
        remaining_tool_budget <= _FINALIZATION_REMAINING_TOOL_BUDGET
        and not finalization_hint_injected
    ):
        msgs.append(
            SystemMessage(
                content=(
                    "FINALIZATION_HINT: 剩余工具预算较低。请优先对可判定项调用 "
                    'assert_verification；若无法继续，请立即 report_done(status="abort")。'
                )
            )
        )
        finalization_hint_injected = True
    force_query_hint = _should_force_query_app_knowledge(
        state, include_rag, rag_summary
    )
    knowledge_query_hint_injected = bool(
        state.get("_knowledge_query_hint_injected", False)
    )
    if force_query_hint and not knowledge_query_hint_injected:
        query_text = (state.get("user_request", "") or "").strip()[:40]
        if not query_text:
            query_text = "当前页面下一步"
        hint_text = (
            "检测到循环或无进展风险，下一步先调用 "
            f'query_app_knowledge(query="{query_text}", app_package="{effective_app_package}") '
            "再执行点击/滑动。"
        )
        msgs.append(SystemMessage(content=("KNOWLEDGE_QUERY_REQUIRED: " + hint_text)))
        if ctx and getattr(ctx, "_ws_emit", None):
            try:
                ctx._ws_emit("knowledge_hint", {"message": hint_text})
            except Exception:
                pass
        knowledge_query_hint_injected = True
    elif not force_query_hint:
        knowledge_query_hint_injected = False

    self_doubt_reasons: list[str] = []
    if pid and len(history) >= 3:
        recent_hist = history[-3:]
        if all(
            str(s.get("page_from", "") or "") == pid and s.get("status") == "continue"
            for s in recent_hist
        ):
            self_doubt_reasons.append("连续 3 步在同一页面无进展")
    target_pages = [
        str(x or "").strip()
        for x in (goal.get("target_pages", []) or [])
        if str(x or "").strip()
    ]
    if pid and target_pages and len(history) >= 3:
        if all(tp not in pid for tp in target_pages):
            self_doubt_reasons.append("当前页面与 Goal 目标页面长期偏离")
    last_clickable_count = int(state.get("_last_clickable_count", 0) or 0)
    if last_clickable_count > 0 and n_clickable > 0:
        if abs(n_clickable - last_clickable_count) >= max(
            8, int(last_clickable_count * 0.6)
        ):
            self_doubt_reasons.append("页面元素数量突变，可能存在弹窗或页面异常")
    if self_doubt_reasons:
        doubt_text = (
            "SELF_DOUBT_HINT: 检测到不确定状态（"
            + "；".join(self_doubt_reasons[:2])
            + "）。下一步先调用 get_screen_info 复核；若仍无法确认路径，请立即 "
            'report_done(status="abort", summary="页面异常，建议人工确认")。'
        )
        msgs.append(SystemMessage(content=doubt_text))

    last_page_app_key = str(state.get("_last_page_app_key", "") or "")
    app_switch_hint = ""
    if current_app_key and current_app_key != last_page_app_key and last_page_app_key:
        app_switch_hint = (
            f"已进入新应用上下文（{current_app_key}），如不确定下一步，优先调用 "
            f'query_app_knowledge(query="当前页面下一步", app_package="{effective_app_package}")。'
        )
        msgs.append(SystemMessage(content="APP_SWITCH_HINT: " + app_switch_hint))
        if ctx and getattr(ctx, "_ws_emit", None):
            try:
                ctx._ws_emit("knowledge_hint", {"message": app_switch_hint})
            except Exception:
                pass
    msgs.append(
        HumanMessage(
            content="Goal:\n"
            + goal_str
            + "\n\nPage:\n"
            + page_info
            + "\n\nHistory:\n"
            + hist_str
            + (
                "\n\nKnowledge Policy:\n默认不预置场景知识。若不确定下一步，"
                "优先调用 query_app_knowledge(query, app_package) 获取当前场景知识。"
                if not include_rag
                else ""
            )
            + ("\n\nRAG:\n" + rag_summary if rag_summary else "")
        )
    )

    result, tool_calls_log, loop_meta = _run_agent(
        msgs,
        AGENT_TOOLS,
        llm["provider"],
        llm["model"],
        llm["api_key"],
        llm["base_url"],
        max_turns=budget["max_turns_per_iteration"],
        run_id=config.get("configurable", {}).get("thread_id", "unknown"),
    )
    prev_llm_call_count = int(state.get("llm_call_count", 0) or 0)
    prev_tool_call_400_count = int(state.get("tool_call_400_count", 0) or 0)
    iter_llm_call_count = int(loop_meta.get("llm_call_count", 0) or 0)
    iter_tool_call_400_count = int(loop_meta.get("tool_call_400_count", 0) or 0)
    llm_call_count = prev_llm_call_count + iter_llm_call_count
    tool_call_400_count = prev_tool_call_400_count + iter_tool_call_400_count
    tool_call_400_rate = (
        round(tool_call_400_count / llm_call_count, 4) if llm_call_count > 0 else 0.0
    )
    # 不再写入 ctx._tool_calls_log（避免 rebuild 丢失），改为存入 state
    logger.info("Agent #%d: %s", len(history) + 1, result[:200])

    done, abort = _detect_termination(result)
    used_tool_calls_total = used_tool_calls_before + len(tool_calls_log)
    if used_tool_calls_total >= budget["max_tool_calls_total"] and not done:
        abort = True
        done = False
        result = (
            result.rstrip()
            + f"\nABORT: MAX_TOOL_CALLS_EXHAUSTED ({used_tool_calls_total}/{budget['max_tool_calls_total']})"
        )
    # 结构化信号优先：tool call 中的 report_done 已被 _run_agent 转为 "DONE: ..." / "ABORT: ..."
    # _detect_termination 已覆盖文本兜底，此处仅记录来源
    _signal_source = "text" if (done or abort) else "none"
    if loop_meta.get("loop_break_action") in ("report_done", "report_abort"):
        _signal_source = "tool_call"
    si = len(history) + 1
    st = "success" if done else ("fail" if abort else "continue")
    logger.info(
        "Agent #%d decision: %s (source=%s)",
        si,
        "DONE" if done else ("ABORT" if abort else "CONTINUE"),
        _signal_source,
    )
    # 从 messages 中提取最后一条工具调用的结构化信息
    _tool_name = "agent"
    _tool_target = ""
    _page_from = pid  # 当前页（已在感知阶段获取）
    _page_to = ""
    for _m in reversed(msgs):
        _tcs = getattr(_m, "tool_calls", None) or []
        if _tcs:
            _last = _tcs[-1]
            _tn = _last.get("name", "")
            if _tn not in (
                "get_screen_info",
                "check_page_health",
                "query_app_knowledge",
            ):
                _tool_name = _tn
                _args = _last.get("args", {}) or {}
                _tool_target = _build_tool_target(_tn, _args)
            break
    # 尝试捕获 page_to（本步骤之后下一次感知的页面）
    try:
        if ctx and ctx.perceiver and (done or abort or si == 1):
            _u2 = ctx.perceiver.perceive()
            _act2 = _u2.activity.split(".")[-1] if _u2.activity else "?"
            _t2 = _u2.page_title or ""
            _page_to = _act2 + "「" + _t2 + "」" if _t2 else _act2
    except Exception:
        pass

    try:
        _step_duration_ms = int((_time.time() - t0) * 1000) if t0 else 0
    except NameError:
        _step_duration_ms = 0

    nh = list(history) + [
        {
            "index": si,
            "intent": result[:80].replace("\n", " "),
            "action_type": _tool_name,
            "target": _tool_target,
            "page_from": _page_from,
            "page_to": _page_to,
            "duration_ms": _step_duration_ms,
            "status": st,
            "observation": result[:500],
            "raw_observation": result,
            "screenshot_path": "",
            "anomaly": None,
            "loop_detected": bool(loop_meta.get("loop_detected")),
            "loop_pattern": str(loop_meta.get("loop_pattern", "")),
            "loop_break_action": str(loop_meta.get("loop_break_action", "")),
        }
    ]
    um: list[Any] = list(state.get("messages", []))
    if not um:
        um = [SystemMessage(content=AGENT_SYSTEM)]
    um.append(AIMessage(content=result))

    # ═══ 关键操作后自动注入当前页面状态（基于工具类型，非硬编码关键词）═══
    if not done and not abort and ctx and ctx.perceiver:
        # Phase 1.5: 检测最近的 tool_calls 是否包含 click 或 scroll_find_and_click
        last_ai_msgs = [m for m in msgs[-4:] if isinstance(m, AIMessage)]
        had_action = False
        for m in last_ai_msgs:
            for tc in getattr(m, "tool_calls", None) or []:
                if tc.get("name") in ("click", "scroll_find_and_click"):
                    had_action = True
                    break
        if had_action:
            try:
                u2 = ctx.perceiver.perceive()
                title2 = u2.page_title or ""
                act2 = u2.activity.split(".")[-1] if u2.activity else "?"
                time_snapshot = act2 + "「" + title2 + "」" if title2 else act2
                verify_items = goal.get("verification", [])
                verify_hint = "；".join(verify_items[:3]) if verify_items else ""
                post_check = (
                    f"\n\n[操作后页面状态]\n当前页面: {time_snapshot}\n"
                    f"验证条件: {verify_hint}\n"
                    '如果页面状态已满足验证条件，请立即调用 report_done(status="done") 报告结果。'
                )
                post_check, violated = _clip_to_token_budget(post_check, 120)
                if violated:
                    budget_violation_count += 1
                um.append(HumanMessage(content=post_check))
            except Exception:
                logger.warning("Post-action perceive failed", exc_info=True)

    # ═══ 重复操作检测：连续相同操作 → 强制提醒 ═══
    if not done and not abort:
        recent_actions = [s.get("observation", "")[:60] for s in nh[-4:]]
        if len(recent_actions) >= 3:
            # 检测最近 3 步是否有重复模式（观察内容高度相似）
            unique = len(set(a[:30] for a in recent_actions[-3:]))
            if unique == 1:
                dup_warning = (
                    "[系统提醒] 你已连续 3 次执行相同的操作，页面可能没有变化。"
                    '请立即调 get_screen_info 检查当前状态，如果目标已达成则调用 report_done(status="done")。'
                )
                dup_warning, violated = _clip_to_token_budget(dup_warning, 100)
                if violated:
                    budget_violation_count += 1
                um.append(HumanMessage(content=dup_warning))
                logger.warning(
                    "Agent duplicate action detected, injecting reminder. Recent: %s",
                    recent_actions[-1][:80],
                )
            elif unique == 2 and len(recent_actions) >= 4:
                # 4 步内只有 2 种操作 → 可能在循环
                dup_warning = (
                    "[系统提醒] 检测到可能的循环模式。"
                    '如果目标已达成，请直接调用 report_done(status="done") 报告结果。'
                )
                dup_warning, violated = _clip_to_token_budget(dup_warning, 100)
                if violated:
                    budget_violation_count += 1
                um.append(HumanMessage(content=dup_warning))

    # Phase 1.4: 裁剪时保留 system prompt + 带 Goal 的消息 + 最近消息
    _prune_messages(um)

    if done or abort:
        return Command(
            update={
                "step_history": nh,
                "messages": um,
                "status": "success" if done else "fail",
                "conclusion": result.strip(),
                "budget_violation_count": budget_violation_count,
                "_finalization_hint_injected": finalization_hint_injected,
                "_knowledge_query_hint_injected": knowledge_query_hint_injected,
                "_last_page_app_key": current_app_key or last_page_app_key,
                "_last_clickable_count": n_clickable or last_clickable_count,
                "_rag_injected_once": bool(state.get("_rag_injected_once", False))
                or bool(rag_summary),
                "_rag_last_app_package": (
                    effective_app_package
                    if rag_summary
                    else str(state.get("_rag_last_app_package", "") or "")
                ),
                "llm_call_count": llm_call_count,
                "tool_call_400_count": tool_call_400_count,
                "tool_call_400_rate": tool_call_400_rate,
                "_tool_calls_log": list(state.get("_tool_calls_log", []))
                + tool_calls_log,
            }
        )
    return Command(
        update={
            "step_history": nh,
            "messages": um,
            "budget_violation_count": budget_violation_count,
            "_finalization_hint_injected": finalization_hint_injected,
            "_knowledge_query_hint_injected": knowledge_query_hint_injected,
            "_last_page_app_key": current_app_key or last_page_app_key,
            "_last_clickable_count": n_clickable or last_clickable_count,
            "_rag_injected_once": bool(state.get("_rag_injected_once", False))
            or bool(rag_summary),
            "_rag_last_app_package": (
                effective_app_package
                if rag_summary
                else str(state.get("_rag_last_app_package", "") or "")
            ),
            "llm_call_count": llm_call_count,
            "tool_call_400_count": tool_call_400_count,
            "tool_call_400_rate": tool_call_400_rate,
            "_tool_calls_log": list(state.get("_tool_calls_log", [])) + tool_calls_log,
        }
    )


def reporter_node(state: TestState, config: RunnableConfig) -> Command:
    cfg: TestConfig = config["configurable"]["test_config"]
    history = state.get("step_history", [])
    conclusion = state.get("conclusion", "")
    status = state.get("status", "") or (
        "success" if _detect_termination(conclusion)[0] else "fail"
    )
    goal = state.get("goal_description", {})

    # Compute duration
    duration = 0.0
    started = state.get("started_at", "")
    if started:
        try:
            duration = round(
                (datetime.now() - datetime.fromisoformat(started)).total_seconds(), 1
            )
        except:
            pass

    # dd 初始化（后面 try 块内会覆盖）
    dd = history

    # 失败/截断时，补充已完成的中间步骤摘要到 conclusion
    if status == "fail" and history:
        step_summaries = []
        for s in history:
            obs = str(s.get("observation", "") or "").strip()
            if obs:
                # 取观察内容的第一行作为摘要
                first_line = obs.split("\n")[0][:100]
                step_summaries.append(f"[{s.get('status','')}] {first_line}")
        if step_summaries:
            progress = "已完成步骤:\n" + "\n".join(step_summaries[-10:])
            conclusion = f"{conclusion}\n\n---\n{progress}" if conclusion else progress

    ctx = get_tool_context()

    # ── 双维度结果判定 ──
    execution_status = _determine_execution_status(state)
    test_verdict, verification_results = _collect_verification_results(goal)
    budget_violation_count = int(state.get("budget_violation_count", 0) or 0)
    llm_call_count = int(state.get("llm_call_count", 0) or 0)
    tool_call_400_count = int(state.get("tool_call_400_count", 0) or 0)
    tool_call_400_rate = float(state.get("tool_call_400_rate", 0.0) or 0.0)
    if execution_status not in ("completed",):
        test_verdict = "inconclusive"
    # 向后兼容 status
    status = (
        "success"
        if (execution_status == "completed" and test_verdict == "passed")
        else status
    )

    # ── 点击质量指标 ──
    _tool_log = state.get("_tool_calls_log", [])
    click_count = sum(1 for s in _tool_log if s.get("name") == "click")
    exact_count = sum(
        1
        for s in _tool_log
        if s.get("name") == "click" and s.get("match_mode") == "exact"
    )
    fuzzy_count = sum(
        1 for s in _tool_log if s.get("name") == "click" and s.get("fallback_used")
    )
    ambiguous_count = sum(
        1
        for s in _tool_log
        if s.get("name") == "click" and s.get("match_mode") == "ambiguous"
    )
    rag_query_count = int(getattr(ctx, "_rag_query_count", 0) or 0)

    # RAG same_app ratio: 统计 query_app_knowledge 调用中 same_app 回应的占比
    _rag_same_app = int(getattr(ctx, "_rag_same_app_count", 0) or 0)
    _rag_cross_app = int(getattr(ctx, "_rag_cross_app_count", 0) or 0)
    _rag_empty = int(getattr(ctx, "_rag_empty_hit_count", 0) or 0)
    rag_total_resolved = _rag_same_app + _rag_cross_app + _rag_empty
    rag_same_app_ratio = round(_rag_same_app / max(rag_total_resolved, 1), 4)
    rag_empty_hit_rate = round(_rag_empty / max(rag_total_resolved, 1), 4)

    # 延迟 import：读取 graph 的可变全局当前值（set_relational_db 会更新它）
    from agents.graph import _relational_db

    if _relational_db:
        try:
            from agents.orchestrator import _build_display_steps

            dd = _build_display_steps(history, _tool_log)
            _relational_db.record_test_run(
                run_id=config.get("configurable", {}).get("thread_id", ""),
                user_request=state.get("user_request", ""),
                app_package=state.get("app_package", ""),
                app_name=state.get("app_name", ""),
                status=status,
                conclusion=str(conclusion),
                steps=dd,
                duration_seconds=duration,
                execution_status=execution_status,
                test_verdict=test_verdict,
                verification_json=json.dumps(verification_results, ensure_ascii=False),
                llm_call_count=llm_call_count,
                tool_call_400_count=tool_call_400_count,
                tool_call_400_rate=tool_call_400_rate,
                click_count=click_count,
                fuzzy_click_count=fuzzy_count,
                ambiguous_count=ambiguous_count,
                exact_click_count=exact_count,
                rag_query_count=rag_query_count,
                rag_same_app_ratio=rag_same_app_ratio,
                rag_empty_hit_rate=rag_empty_hit_rate,
                rag_cross_app_used_count=_rag_cross_app,
            )
        except:
            pass

    # 统计统一基于 dd（实际展示步骤）
    pc = sum(1 for s in dd if s.get("status") in ("success", "continue"))
    fc = sum(1 for s in dd if s.get("status") == "fail")
    cc = sum(1 for s in dd if s.get("status") == "continue")
    logger.info(
        "Reporter: exec=%s verdict=%s display_steps=%d steps(success=%d fail=%d continue=%d) duration=%.1fs budget_violation=%d llm_calls=%d tool_call_400=%d tool_call_400_rate=%.4f click=%d exact=%d fuzzy=%d ambiguous=%d rag_q=%d rag_same=%.2f conclusion=%s",
        execution_status,
        test_verdict,
        len(dd),
        pc,
        fc,
        cc,
        duration,
        budget_violation_count,
        llm_call_count,
        tool_call_400_count,
        tool_call_400_rate,
        click_count,
        exact_count,
        fuzzy_count,
        ambiguous_count,
        rag_query_count,
        rag_same_app_ratio,
        str(conclusion)[:120],
    )
    return Command(
        update={
            "conclusion": str(conclusion),
            "status": status,
            "step_history": history,
            "execution_status": execution_status,
            "test_verdict": test_verdict,
            "verification_results": verification_results,
            "budget_violation_count": budget_violation_count,
            "llm_call_count": llm_call_count,
            "tool_call_400_count": tool_call_400_count,
            "tool_call_400_rate": tool_call_400_rate,
        }
    )


def plan_review_node(state: TestState, config: RunnableConfig) -> Command:
    """Pause to let user confirm (and optionally edit) the generated goal before Agent runs."""
    goal = state.get("goal_description", {})
    from langgraph.types import interrupt

    result = interrupt(
        {
            "type": "plan_review",
            "plan": goal,
            "goal": goal.get("goal", ""),
            "pages": goal.get("target_pages", []),
            "verification": goal.get("verification", []),
        }
    )
    # If user edited the goal, use the edited version
    if isinstance(result, dict) and result.get("action") == "confirm":
        edited = {
            "goal": result.get("goal", goal.get("goal", "")),
            "target_pages": result.get("target_pages", goal.get("target_pages", [])),
            "verification": result.get("verification", goal.get("verification", [])),
            "hints": result.get("hints", goal.get("hints", [])),
            "app_package": goal.get("app_package", ""),
            "app_name": goal.get("app_name", ""),
        }
        logger.info("Plan review: user edited goal")
        return Command(update={"goal_description": edited})
    if result == "cancel" or (
        isinstance(result, dict) and result.get("action") == "cancel"
    ):
        return Command(update={"status": "cancelled"})
    return Command(update={})


# ═══ ROUTING ═══


def _prune_messages(um: list[Any], max_len: int = 16) -> None:
    """Phase 1.4: 裁剪消息列表，保留 system prompt + Goal 上下文 + 最近消息。"""
    if len(um) <= max_len:
        return
    # 找到包含 Goal 的消息（agent_node 注入的 HumanMessage 以 "Goal:\n" 开头）
    goal_msg_indices = [
        i
        for i, m in enumerate(um)
        if isinstance(m, HumanMessage)
        and str(getattr(m, "content", "")).startswith("Goal:\n")
    ]
    # 保留: [0] system prompt, goal message, 最近 (max_len - 2) 条
    keep = {0}
    if goal_msg_indices:
        keep.add(goal_msg_indices[-1])  # 保留最新的 goal 消息
    keep.update(range(max(0, len(um) - (max_len - len(keep))), len(um)))
    preserved = sorted(keep)
    # 确保总数不超过 max_len
    if len(preserved) > max_len:
        preserved = [0] + preserved[-(max_len - 1) :]
    um[:] = [um[i] for i in preserved]


def _parse_goal(text: str) -> dict:
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return {
            "goal": text.strip()[:200],
            "target_pages": [],
            "verification": [],
            "hints": [],
        }
    try:
        r = json.loads(m.group(0))
        if isinstance(r, dict):
            return r
    except json.JSONDecodeError:
        pass
    return {
        "goal": text.strip()[:200],
        "target_pages": [],
        "verification": [],
        "hints": [],
    }

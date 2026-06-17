# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from typing import Any, Annotated

from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
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
from tools import AGENT_TOOLS, get_tool_context

logger = logging.getLogger(__name__)

_relational_db = None


def set_relational_db(db) -> None:
    global _relational_db
    _relational_db = db


# ═══ Prompt ═══


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

from typing import TypedDict as _TD
from langgraph.graph.message import add_messages


class _SubState(_TD):
    messages: Annotated[list, add_messages]
    _turn_count: int


def _run_agent(
    messages, tools, provider, model, api_key, base_url, max_turns=20
) -> str:
    if provider == "zhipu":
        from zhipuai import ZhipuAI

        c = ZhipuAI(max_retries=0, api_key=api_key)
        if base_url:
            c.base_url = base_url
        schemas = _zhipu_schemas(tools)

        def _llm(s: _SubState) -> dict:
            ms = _to_zhipu(s["messages"])
            r = _call_retry(
                "zhipu",
                c.chat.completions.create,
                model=model,
                messages=ms,
                tools=schemas,
                tool_choice="auto",
            )
            if r is None:
                return {"messages": [AIMessage(content="LLM failed")]}
            m = r.choices[0].message
            tcs = [
                {
                    "id": t.id,
                    "name": t.function.name,
                    "args": (
                        json.loads(t.function.arguments)
                        if isinstance(t.function.arguments, str)
                        else (t.function.arguments or {})
                    ),
                    "type": "tool_call",
                }
                for t in (m.tool_calls or [])
            ]
            return {
                "messages": [
                    AIMessage(content=m.content or "", tool_calls=tcs if tcs else None)
                ]
            }

        llm_node = _llm
    else:
        from langchain_openai import ChatOpenAI

        lc = ChatOpenAI(
            model=model, temperature=0.1, api_key=api_key, base_url=base_url
        ).bind_tools(tools)

        def _llm(s: _SubState) -> dict:
            r = _call_retry("openai", lc.invoke, s["messages"])
            return {"messages": [r] if r else [AIMessage(content="LLM failed")]}

        llm_node = _llm

    def _inc(s: _SubState) -> dict:
        return {"_turn_count": s.get("_turn_count", 0) + 1, "messages": []}

    def _limit(s: _SubState) -> str:
        r = tools_condition(s)
        return END if r == "tools" and s.get("_turn_count", 0) >= max_turns else r

    g = StateGraph(_SubState)
    g.add_node("llm", llm_node)
    g.add_node("tools", ToolNode(tools))
    g.add_node("inc", _inc)
    g.add_edge(START, "llm")
    g.add_conditional_edges("llm", _limit, {"tools": "inc", END: END})
    g.add_edge("inc", "tools")
    g.add_edge("tools", "llm")
    result = g.compile().invoke({"messages": list(messages), "_turn_count": 0})
    turn_count = result.get("_turn_count", 0)

    # Phase 1.1: 静默截断检测 —— 当 turn 耗尽时注入明确标记
    if turn_count >= max_turns:
        for m in reversed(result["messages"]):
            c = getattr(m, "content", None)
            if c:
                return str(c) + "\nABORT: MAX_TURNS_EXHAUSTED"
        return "ABORT: MAX_TURNS_EXHAUSTED — 达到最大工具调用次数"

    for m in reversed(result["messages"]):
        c = getattr(m, "content", None)
        if c:
            return str(c)
    return "ABORT: No agent response"


# Phase 1.2: 锚定行首的 DONE/ABORT 检测
_DONE_PATTERN = re.compile(r"^(DONE|ABORT)\s*[:：]", re.IGNORECASE | re.MULTILINE)


def _detect_termination(result: str) -> tuple[bool, bool]:
    """返回 (done, abort) — 取最后一个行首匹配（后追加的标记优先级更高）。"""
    matches = list(_DONE_PATTERN.finditer(result.strip()))
    if not matches:
        return (False, False)
    m = matches[-1]
    return (m.group(1).upper() == "DONE", m.group(1).upper() == "ABORT")


def _call_retry(provider, fn, *a, **kw):
    return _call_with_retry(
        lambda e: (
            _is_rate_limit_error(e) if provider == "zhipu" else _default_should_retry(e)
        ),
        fn,
        *a,
        **kw,
    )


def _zhipu_schemas(tools):
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description or "",
                "parameters": {
                    "type": "object",
                    "properties": {
                        n: {
                            "type": m.get("type", "string"),
                            "description": m.get("description", ""),
                        }
                        for n, m in (getattr(t, "args", {}) or {}).items()
                    },
                    "required": list((getattr(t, "args", {}) or {}).keys()),
                },
            },
        }
        for t in tools
    ]


def _to_zhipu(msgs):
    r = []
    for m in msgs:
        role = getattr(m, "type", "system")
        if role == "human":
            role = "user"
        elif role == "ai":
            role = "assistant"
        e = {"role": role, "content": str(getattr(m, "content", "") or "")}
        if hasattr(m, "tool_calls") and m.tool_calls:
            e["tool_calls"] = [
                {
                    "id": t.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "arguments": json.dumps(t.get("args", {}), ensure_ascii=False),
                    },
                }
                for t in m.tool_calls
            ]
        if tid := getattr(m, "tool_call_id", None):
            e["tool_call_id"] = tid
        r.append(e)
    return r


# ═══ LLM config ═══


def _llm_cfg(cfg: TestConfig):
    return {
        "provider": cfg.llm_provider,
        "model": cfg.model,
        "api_key": cfg.api_key,
        "base_url": cfg.base_url,
    }


def _rag_ctx(kb, app_package: str, user_request: str = "") -> str:
    """查询 RAG 获取 App 上下文：前提条件 + 验证计划 + 导航经验。"""
    if not kb:
        return ""
    parts = []
    # 1. App 前提条件（如"计算器需先清空"）
    pre = kb.query_preconditions(app_package)
    if pre:
        parts.append("## App 操作前提\n" + pre)
    # 2. 历史验证计划（同 App 同需求优先）
    plans = kb.query_verified_plan(app_package, user_request, top_k=2)
    if plans:
        parts.append(
            "## 历史验证计划\n" + "\n".join(f"- {p['content']}" for p in plans)
        )
    # 3. 导航经验（用 user_request 动态查询）
    if user_request:
        nav = kb.query_navigation(app_package, user_request[:50], top_k=2)
        if nav:
            parts.append("## 导航经验\n" + "\n".join(f"- {n['content']}" for n in nav))
    return "\n\n".join(parts)


# ═══ NODES ═══


def planner_node(state: TestState, config: RunnableConfig) -> Command:
    cfg: TestConfig = config["configurable"]["test_config"]
    llm = _llm_cfg(cfg)
    ctx = get_tool_context()
    kb = ctx.knowledge_base if ctx else None
    rag = _rag_ctx(kb, state.get("app_package", ""), state.get("user_request", ""))
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
    return Command(
        update={
            "goal_description": goal,
            "step_history": [],
            "messages": [],
            "started_at": datetime.now().isoformat(),
            "step_times": [],
        }
    )


def agent_node(state: TestState, config: RunnableConfig) -> Command:
    cfg: TestConfig = config["configurable"]["test_config"]
    llm = _llm_cfg(cfg)

    # Page info
    ctx = get_tool_context()
    page_info = "unknown"
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
            n_clickable = sum(1 for e in u.elements if e.clickable and e.label)
            lines = [
                "page=" + pid,
                "layout=" + u.layout,
                "clickable=" + str(n_clickable),
            ]
            for e in u.elements:
                if e.clickable and e.label:
                    role = e.role or ""
                    rid = (e.resource_id or "").split("/")[-1] if e.resource_id else ""
                    extra = ""
                    if role:
                        extra += " [" + role + "]"
                    if rid:
                        extra += " rid=" + rid
                    lines.append("  - " + e.label + extra)
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
    goal_str = json.dumps(goal, ensure_ascii=False, indent=2)
    history = state.get("step_history", [])
    hist_lines = [
        f"  [{s.get('status','')}] {s.get('intent','')}: {str(s.get('observation',''))[:100]}"
        for s in history[-10:]
    ]
    hist_str = "\n".join(hist_lines) if hist_lines else "(none)"

    # Messages — always include goal + page for context
    msgs = list(state.get("messages", []))
    if not msgs:
        msgs = [SystemMessage(content=AGENT_SYSTEM)]
    msgs.append(
        HumanMessage(
            content="Goal:\n"
            + goal_str
            + "\n\nPage:\n"
            + page_info
            + "\n\nHistory:\n"
            + hist_str
        )
    )

    # 根据 Goal 复杂度动态计算 max_turns：基础 6 + 每页面 3 + 每验证项 2，上限 30
    _goal_turns = (
        18
        + len(goal.get("target_pages", [])) * 4
        + len(goal.get("verification", [])) * 4
    )
    _max_turns = min(max(_goal_turns, 8), 30)  # 最少 8，最多 30

    result = _run_agent(
        msgs,
        AGENT_TOOLS,
        llm["provider"],
        llm["model"],
        llm["api_key"],
        llm["base_url"],
        max_turns=_max_turns,
    )
    logger.info("Agent #%d: %s", len(history) + 1, result[:200])

    done, abort = _detect_termination(result)
    si = len(history) + 1
    st = "success" if done else ("fail" if abort else "continue")
    logger.info(
        "Agent #%d decision: %s",
        si,
        "DONE" if done else ("ABORT" if abort else "CONTINUE"),
    )
    nh = list(history) + [
        {
            "index": si,
            "intent": result[:80].replace("\n", " "),
            "action_type": "agent",
            "target": "",
            "status": st,
            "observation": result[:300],
            "screenshot_path": "",
            "anomaly": None,
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
                    "如果页面状态已满足验证条件，请立即输出 DONE: 描述结果。"
                )
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
                    "请立即调 get_screen_info 检查当前状态，如果目标已达成则 DONE。"
                )
                um.append(HumanMessage(content=dup_warning))
                logger.warning(
                    "Agent duplicate action detected, injecting reminder. Recent: %s",
                    recent_actions[-1][:80],
                )
            elif unique == 2 and len(recent_actions) >= 4:
                # 4 步内只有 2 种操作 → 可能在循环
                dup_warning = (
                    "[系统提醒] 检测到可能的循环模式。"
                    "如果目标已达成，请直接输出 DONE，不要再操作。"
                )
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
            }
        )
    return Command(update={"step_history": nh, "messages": um})


def reporter_node(state: TestState, config: RunnableConfig) -> Command:
    cfg: TestConfig = config["configurable"]["test_config"]
    history = state.get("step_history", [])
    conclusion = state.get("conclusion", "")
    status = state.get("status", "") or (
        "success" if _detect_termination(conclusion)[0] else "fail"
    )

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

    # Count: success = "success", fail = "fail" only (not "continue")
    pc = sum(1 for s in history if s.get("status") == "success")
    fc = sum(1 for s in history if s.get("status") == "fail")
    cc = sum(1 for s in history if s.get("status") == "continue")

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
    if ctx and ctx.knowledge_base:
        try:
            ctx.knowledge_base.extract_from_test_result(
                state.get("app_package", ""),
                state.get("user_request", ""),
                [
                    {
                        "page": s.get("target", ""),
                        "action": s.get("action_type", ""),
                        "observation": s.get("observation", ""),
                        "result": s.get("status", ""),
                        "error": (
                            s.get("observation", "")
                            if s.get("status") != "success"
                            else ""
                        ),
                    }
                    for s in history
                ],
                "PASS" if status == "success" else "FAIL",
            )
        except:
            pass

    # P3: 成功后保存 verified_plan 到 RAG
    if status == "success" and ctx and ctx.knowledge_base:
        try:
            ctx.knowledge_base.save_verified_plan(
                app_package=state.get("app_package", ""),
                user_request=state.get("user_request", ""),
                plan=history,
                results=history,
            )
        except Exception:
            pass

    if _relational_db:
        try:
            seen = set()
            dd = []
            for s in history:
                k = (s.get("intent", ""), s.get("action_type", ""), s.get("target", ""))
                if k not in seen:
                    seen.add(k)
                    dd.append(s)
            _relational_db.record_test_run(
                run_id=config.get("configurable", {}).get("thread_id", ""),
                user_request=state.get("user_request", ""),
                app_package=state.get("app_package", ""),
                app_name=state.get("app_name", ""),
                status=status,
                conclusion=str(conclusion),
                steps=dd,
                duration_seconds=duration,
            )
        except:
            pass

    logger.info(
        "Reporter: status=%s success=%d fail=%d continue=%d duration=%.1fs conclusion=%s",
        status,
        pc,
        fc,
        cc,
        duration,
        str(conclusion)[:120],
    )
    return Command(
        update={
            "conclusion": str(conclusion),
            "status": status,
            "step_history": history,
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


def route_after_agent(state: TestState) -> str:
    n = len(state.get("step_history", []))
    if state.get("status") in ("success", "fail"):
        logger.info("Route: reporter (status=%s, steps=%d)", state.get("status"), n)
        return "reporter"
    if n >= 12:
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

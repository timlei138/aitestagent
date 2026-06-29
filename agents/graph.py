# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import logging
import os
import re
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
from tools import AGENT_TOOLS, get_tool_context

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


_SKIP_EMIT = {"get_screen_info", "check_page_health", "query_app_knowledge"}


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
) -> tuple[str, list]:
    try:
        _ctx = get_tool_context()  # 捕获当前 ToolContext 供子图事件发射使用
    except Exception:
        _ctx = None
    if _ctx and _ws_emit_callback:
        _ctx._ws_emit = _ws_emit_callback
    if provider == "zhipu":
        from zhipuai import ZhipuAI

        c = ZhipuAI(max_retries=0, api_key=api_key)
        if base_url:
            c.base_url = base_url
        schemas = _zhipu_schemas(tools)

        def _llm(s: _SubState) -> dict:
            if _ctx and _ctx._ws_emit:
                try: _ctx._ws_emit("stream_token", "thinking")
                except Exception: pass
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
            if _ctx and _ctx._ws_emit:
                try: _ctx._ws_emit("stream_token", "thinking")
                except Exception: pass
            r = _call_retry("openai", lc.invoke, s["messages"])
            return {"messages": [r] if r else [AIMessage(content="LLM failed")]}

        llm_node = _llm

    # ── 自定义工具执行节点（替代 ToolNode，支持实时事件发射）──
    _tool_map = {t.name: t for t in tools}

    def _tools_node(s: _SubState) -> dict:
        last_ai = next(m for m in reversed(s["messages"]) if isinstance(m, AIMessage))
        outputs = []
        for tc in (last_ai.tool_calls or []):
            name = tc["name"]
            args = tc.get("args", {}) or {}
            if name not in _SKIP_EMIT and _ctx and _ctx._ws_emit:
                try: _ctx._ws_emit("tool_start", {"name": name, "input": {"label": args.get("label", "") or args.get("target", "")}})
                except Exception: pass
            t = _tool_map.get(name)
            try:
                output = str(t.invoke(args)) if t else f"UNKNOWN_TOOL: {name}"
            except Exception as e:
                output = f"ERROR: {e}"
            if name not in _SKIP_EMIT and _ctx and _ctx._ws_emit:
                try: _ctx._ws_emit("tool_end", {"name": name, "output": output[:200]})
                except Exception: pass
            outputs.append(ToolMessage(content=output, tool_call_id=tc["id"]))
        return {"messages": outputs}

    def _inc(s: _SubState) -> dict:
        return {"_turn_count": s.get("_turn_count", 0) + 1, "messages": []}

    def _limit(s: _SubState) -> str:
        r = tools_condition(s)
        return END if r == "tools" and s.get("_turn_count", 0) >= max_turns else r

    g = StateGraph(_SubState)
    g.add_node("llm", llm_node)
    g.add_node("tools", _tools_node)
    g.add_node("inc", _inc)
    g.add_edge(START, "llm")
    g.add_conditional_edges("llm", _limit, {"tools": "inc", END: END})
    g.add_edge("inc", "tools")
    g.add_edge("tools", "llm")
    result = g.compile().invoke({"messages": list(messages), "_turn_count": 0})
    turn_count = result.get("_turn_count", 0)

    # 提取内部工具调用信息（供报告展示用）
    _tool_calls_log = []
    for m in result["messages"]:
        tcs = getattr(m, "tool_calls", None) or []
        for tc in tcs:
            name = tc.get("name", "")
            if name not in ("get_screen_info", "check_page_health", "query_app_knowledge"):
                args = tc.get("args", {}) or {}
                _tool_calls_log.append({
                    "name": name,
                    "target": args.get("label", "") or args.get("target", "") or "",
                })

    # Phase 1.1: 静默截断检测 —— 当 turn 耗尽时注入明确标记
    if turn_count >= max_turns:
        for m in reversed(result["messages"]):
            c = getattr(m, "content", None)
            if c:
                return str(c) + "\nABORT: MAX_TURNS_EXHAUSTED", _tool_calls_log
        return "ABORT: MAX_TURNS_EXHAUSTED — 达到最大工具调用次数", _tool_calls_log

    for m in reversed(result["messages"]):
        c = getattr(m, "content", None)
        if c:
            return str(c), _tool_calls_log
    return "ABORT: No agent response", _tool_calls_log


# Phase 1.2: 锚定行首的 DONE/ABORT 检测（兼容 ##/### Markdown 标题前缀）
_DONE_PATTERN = re.compile(r"^(?:#{1,3}\s*)?(DONE|ABORT)\s*[:：]", re.IGNORECASE | re.MULTILINE)


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
    """查询 RAG 获取上下文：人工知识 + 操作经验（2 sections）。"""
    if not kb:
        return ""
    parts = []
    # 1. 人工知识（一次查询，Python 侧自动分组为全局知识 + App 操作前提）
    rules = kb.query_curated_rules(app_package)
    if rules:
        parts.append("## 人工知识\n" + rules)
    # 2. 操作经验
    if user_request:
        exp = kb.query_experience(app_package, user_request[:50], top_k=3)
        if exp:
            parts.append("## 操作经验\n" + "\n".join(f"- {e['content']}" for e in exp))
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
    pid = ""
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

    # 根据 Goal 复杂度动态计算 max_turns：基础 18 + 每页面 4 + 每验证项 4
    _goal_turns = (
        18
        + len(goal.get("target_pages", [])) * 10
        + len(goal.get("verification", [])) * 10
    )
    _max_turns = min(max(_goal_turns, 10), 200)  # 最少 10，最多 200

    result, tool_calls_log = _run_agent(
        msgs,
        AGENT_TOOLS,
        llm["provider"],
        llm["model"],
        llm["api_key"],
        llm["base_url"],
        max_turns=_max_turns,
    )
    # 累积存入 ToolContext 供 _build_display_steps 使用（多轮 Agent 调用需累加）
    if ctx:
        if not hasattr(ctx, '_tool_calls_log'):
            ctx._tool_calls_log = []
        ctx._tool_calls_log.extend(tool_calls_log)
    logger.info("Agent #%d: %s", len(history) + 1, result[:200])

    done, abort = _detect_termination(result)
    si = len(history) + 1
    st = "success" if done else ("fail" if abort else "continue")
    logger.info(
        "Agent #%d decision: %s",
        si,
        "DONE" if done else ("ABORT" if abort else "CONTINUE"),
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
            if _tn not in ("get_screen_info", "check_page_health", "query_app_knowledge"):
                _tool_name = _tn
                _args = _last.get("args", {}) or {}
                _tool_target = _args.get("label", "") or _args.get("target", "") or ""
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


def _determine_execution_status(state: dict) -> str:
    """判定执行状态：completed / exhausted / error / cancelled / device_offline。"""
    s = state.get("status", "")
    conclusion = state.get("conclusion", "")
    if s == "cancelled":
        return "cancelled"
    if s == "device_offline":
        return "device_offline"
    done, abort = _detect_termination(conclusion)
    if done:
        return "completed"
    if abort:
        if "MAX_TURNS" in conclusion:
            return "exhausted"
        # Agent 主动 ABORT 仍算 completed，verdict 由 test_verdict 决定
        return "completed"
    history = state.get("step_history", [])
    if len(history) >= 12:
        return "exhausted"
    return "error"


def _collect_verification_results(goal: dict) -> tuple[str, list]:
    """从 ToolContext._verifications 收集 assert_verification 的结构化结果。"""
    ctx = get_tool_context()
    assertions = getattr(ctx, '_verifications', []) if ctx else []

    if not assertions:
        verification_items = goal.get("verification", [])
        if verification_items:
            assertions = [{"item": v, "result": "unknown"} for v in verification_items]
        else:
            return "passed", []

    passed = sum(1 for a in assertions if a["result"] == "passed")
    failed = sum(1 for a in assertions if a["result"] == "failed")

    if failed > 0:
        verdict = "failed"
    elif passed == len(assertions):
        verdict = "passed"
    else:
        verdict = "inconclusive"

    return verdict, assertions


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

    # ── 双维度结果判定 ──
    execution_status = _determine_execution_status(state)
    test_verdict, verification_results = _collect_verification_results(goal)
    if execution_status not in ("completed",):
        test_verdict = "inconclusive"
        verification_results = []
    # 向后兼容 status
    status = "success" if (execution_status == "completed" and test_verdict == "passed") else status

    if _relational_db:
        try:
            from agents.orchestrator import _build_display_steps
            dd = _build_display_steps(history)
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
            )
        except:
            pass

    logger.info(
        "Reporter: exec=%s verdict=%s steps(success=%d fail=%d continue=%d) duration=%.1fs conclusion=%s",
        execution_status, test_verdict,
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
            "execution_status": execution_status,
            "test_verdict": test_verdict,
            "verification_results": verification_results,
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

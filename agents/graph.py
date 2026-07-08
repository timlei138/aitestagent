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
from tools import AGENT_TOOLS, get_tool_context, _extract_click_preferences_from_rag

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


_SKIP_EMIT = {"get_screen_info", "check_page_health", "query_app_knowledge"}

_SCREENSHOT_ACTIONS = {
    "click",
    "long_press",
    "scroll_find_and_click",
    "launch_app",
    "assert_verification",
    "swipe",
}


def _take_step_screenshot(ctx, run_id: str, tool_seq: int) -> str:
    """截取当前屏幕，返回相对于 DATA_DIR 的路径。
    路径格式：screenshots/{safe_run_id}/{tool_seq}_{ts}.png
    前端通过 /storage 挂载点访问。
    """
    from datetime import datetime as _dt

    # 路径安全清洗：只保留 [a-zA-Z0-9_-]
    safe_run_id = re.sub(r"[^\w\-]", "_", run_id)
    if not safe_run_id:
        safe_run_id = "unknown"
    shot_dir = os.path.join(app_paths.SCREENSHOT_DIR_STR, safe_run_id)
    os.makedirs(shot_dir, exist_ok=True)
    ts = _dt.now().strftime("%Y%m%d_%H%M%S_%f")
    path = os.path.join(shot_dir, f"{tool_seq}_{ts}.png")
    img = ctx.device.screenshot()  # 返回 PIL Image（无参）
    img.save(path)
    # 返回相对路径（相对于 DATA_DIR），使前端 /storage 挂载能正确解析
    try:
        rel = os.path.relpath(path, app_paths.DATA_DIR_STR)
        return rel.replace(os.sep, "/")
    except Exception:
        return path


# ═══ Prompt ═══


def _build_tool_target(name: str, args: dict) -> str:
    """从工具参数中提取可读的目标描述。"""
    if not args:
        return ""
    # 优先使用显式的 label / target 参数
    label = args.get("label", "") or args.get("target", "")
    if label:
        return str(label)
    # 按优先级从其他参数中提取
    for key in (
        "key",
        "text",
        "direction",
        "panel",
        "package",
        "seconds",
        "orientation",
    ):
        val = args.get(key, "")
        if val:
            return str(val)
    return ""


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
    _recent_call_sigs: list[str]
    _recent_action_groups: list[str]
    _loop_break_reason: str
    _no_progress_count: int
    _no_progress_warned: bool
    _cooldown_map: dict[str, int]
    _tool_calls_log: list
    _run_id: str


_LOOP_BREAK_CONSECUTIVE = 3
_NO_PROGRESS_LIMIT = 8
_FINALIZATION_REMAINING_TOOL_BUDGET = 5
_NO_PROGRESS_ACTIONS = {
    "click",
    "scroll_find_and_click",
    "long_press",
    "copy",
    "scroll_panel",
    "type_input",
    "press_key",
    "paste",
    "swipe",
    "open_notification",
    "open_quick_settings",
    "unlock_screen",
    "set_orientation",
    "toggle_auto_rotate",
    "recover_from_anomaly",
}


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


def _build_page_signature(ctx: Any) -> str:
    """页面签名：activity + page_title + visible_labels_hash。"""
    if not ctx or not getattr(ctx, "perceiver", None):
        return "unknown"
    try:
        u = ctx.perceiver.perceive()
        act = u.activity or ""
        title = u.page_title or ""
        labels = sorted(
            (e.label or "").strip().lower()
            for e in (u.elements or [])
            if getattr(e, "clickable", False) and (e.label or "").strip()
        )
        vis = "|".join(labels[:80])
        vis_hash = hashlib.md5(vis.encode("utf-8")).hexdigest()[:12]
        return f"{act}|{title}|{vis_hash}"
    except Exception:
        return "unknown"


def _build_call_signature(name: str, args: dict, page_sig: str) -> str:
    try:
        args_norm = json.dumps(args or {}, ensure_ascii=False, sort_keys=True)
    except Exception:
        args_norm = str(args or {})
    return f"{name}|{args_norm}|{page_sig}"


def _safe_len(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    return 0


def _calc_budget(goal: dict) -> dict[str, int]:
    pages = _safe_len(goal.get("target_pages", [])) if isinstance(goal, dict) else 0
    verifications = (
        _safe_len(goal.get("verification", [])) if isinstance(goal, dict) else 0
    )
    max_tool_calls_total = 16 + pages * 9 + verifications * 8
    max_agent_iterations = min(max(2 + pages + verifications, 6), 18)
    # 每轮子图预算作为断路器，不应过小导致在关键动作前被截断。
    # 迭代层(route)负责主导结束；这里取较宽上限，避免“即将点击关键元素时 __end__”。
    max_turns_per_iteration = min(max(max_tool_calls_total, 10), 50)
    return {
        "max_tool_calls_total": max_tool_calls_total,
        "max_agent_iterations": max_agent_iterations,
        "max_turns_per_iteration": max_turns_per_iteration,
    }


def _calc_budget_from_state(state: dict) -> dict[str, int]:
    return _calc_budget(state.get("goal_description", {}) or {})


def _cooldown_group(name: str, args: dict, target: str = "") -> str:
    if name == "press_key" and str(args.get("key", "")).lower() == "back":
        return "nav_back"
    if name in ("swipe", "scroll_panel"):
        return "browse"
    if name == "click":
        txt = " ".join(
            [
                str(args.get("label", "") or ""),
                str(args.get("target", "") or ""),
                str(args.get("alternatives", "") or ""),
                str(target or ""),
            ]
        )
        if any(k in txt for k in ("应用列表", "所有应用")):
            return "app_entry_retry"
        # “应用”需要精确边界，避免误命中“应用商店”等复合词
        if re.search(r"(^|[^\u4e00-\u9fff])应用([^\u4e00-\u9fff]|$)", txt):
            return "app_entry_retry"
    return ""


def _output_has_page_change(
    output: str, page_sig_before: str = "", page_sig_after: str = ""
) -> bool:
    if page_sig_before and page_sig_after and page_sig_before != page_sig_after:
        return True
    m = re.search(r"页面变化:\s*(.+?)\s*→\s*(.+?)(?:\s*\||$)", output or "")
    if not m:
        return False
    return m.group(1).strip() != m.group(2).strip()


def _should_skip_hotword_element(element: Any) -> bool:
    rid = ((getattr(element, "resource_id", "") or "").split("/")[-1]).lower()
    label = (getattr(element, "label", "") or "").strip()
    if rid == "search_keyword":
        return True
    if rid == "search_bar_bg" and len(label) >= 8:
        return True
    return False


def _run_agent(
    messages,
    tools,
    provider,
    model,
    api_key,
    base_url,
    max_turns=20,
    run_id: str = "",
) -> tuple[str, list, dict[str, Any]]:
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
                try:
                    _ctx._ws_emit("stream_token", "thinking")
                except Exception:
                    pass
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
                try:
                    _ctx._ws_emit("stream_token", "thinking")
                except Exception:
                    pass
            r = _call_retry("openai", lc.invoke, s["messages"])
            return {"messages": [r] if r else [AIMessage(content="LLM failed")]}

        llm_node = _llm

    # ── 自定义工具执行节点（替代 ToolNode，支持实时事件发射）──
    _tool_map = {t.name: t for t in tools}

    def _tools_node(s: _SubState) -> dict:
        last_ai = next(m for m in reversed(s["messages"]) if isinstance(m, AIMessage))
        outputs = []
        recent = list(s.get("_recent_call_sigs", []))
        recent_action_groups = list(s.get("_recent_action_groups", []))
        loop_break_reason = s.get("_loop_break_reason", "")
        no_progress_count = int(s.get("_no_progress_count", 0) or 0)
        no_progress_warned = bool(s.get("_no_progress_warned", False))
        cooldown_map = dict(s.get("_cooldown_map", {}) or {})
        _current_log = list(s.get("_tool_calls_log", []))
        page_sig_once = _build_page_signature(_ctx)
        for tc in last_ai.tool_calls or []:
            name = tc["name"]
            args = tc.get("args", {}) or {}
            target_hint = _build_tool_target(name, args)
            cooldown_group = _cooldown_group(name, args, target_hint)
            if cooldown_group and int(cooldown_map.get(cooldown_group, 0) or 0) > 0:
                cooldown_map[cooldown_group] = int(cooldown_map[cooldown_group]) - 1
                if cooldown_map[cooldown_group] <= 0:
                    cooldown_map.pop(cooldown_group, None)
                outputs.append(
                    ToolMessage(
                        content=(
                            f"COOLDOWN_SKIP: {cooldown_group} cooling down, "
                            "请切换策略并尝试不同操作"
                        ),
                        tool_call_id=tc["id"],
                    )
                )
                continue
            if name not in _SKIP_EMIT and _ctx and _ctx._ws_emit:
                try:
                    _intent_text = (getattr(last_ai, "content", "") or "").strip()
                    _ctx._ws_emit(
                        "tool_start",
                        {
                            "name": name,
                            "input": {"label": _build_tool_target(name, args)},
                            "intent_text": _intent_text[:200],
                        },
                    )
                except Exception:
                    pass
            t = _tool_map.get(name)
            try:
                output = str(t.invoke(args)) if t else f"UNKNOWN_TOOL: {name}"
            except Exception as e:
                output = f"ERROR: {e}"
            page_sig_after = _build_page_signature(_ctx)
            # 设备断开快速终止：工具执行后立即检测，避免继续执行无意义操作
            try:
                _live_ctx = get_tool_context()
                if _live_ctx.device is None:
                    output = "ERROR: 设备已断开连接"
                    outputs.append(ToolMessage(content=output, tool_call_id=tc["id"]))
                    # 为剩余未执行的 tool_calls 补占位 ToolMessage，避免 LangChain 报错
                    _remaining = last_ai.tool_calls or []
                    _idx = _remaining.index(tc) + 1 if tc in _remaining else -1
                    if _idx > 0:
                        for _skipped in _remaining[_idx:]:
                            outputs.append(
                                ToolMessage(
                                    content="SKIPPED: 设备已断开",
                                    tool_call_id=_skipped["id"],
                                )
                            )
                    logger.warning("设备在工具执行中断开，终止剩余工具调用")
                    break
            except Exception:
                pass
            if name not in _SKIP_EMIT and _ctx and _ctx._ws_emit:
                try:
                    _ctx._ws_emit("tool_end", {"name": name, "output": output[:200]})
                except Exception:
                    pass
            # 截图：仅关键操作
            _screenshot_path = ""
            if name in _SCREENSHOT_ACTIONS and _ctx and _ctx.device:
                try:
                    _run_id = s.get("_run_id", "unknown")
                    _tool_seq = len(_current_log) + 1
                    _screenshot_path = _take_step_screenshot(_ctx, _run_id, _tool_seq)
                except Exception as e:
                    logger.warning("Step screenshot failed for %s: %s", name, e)
                    _screenshot_path = ""
            outputs.append(ToolMessage(content=output, tool_call_id=tc["id"]))

            # 最小断路器：连续 N 次未进行 assert_verification 判定为空转。
            if name == "assert_verification":
                no_progress_count = 0
                no_progress_warned = False
            else:
                if name in _NO_PROGRESS_ACTIONS:
                    no_progress_count += 1
                    if no_progress_count >= _NO_PROGRESS_LIMIT:
                        if not no_progress_warned:
                            no_progress_warned = True
                            no_progress_count = 0
                            outputs.append(
                                SystemMessage(
                                    content=(
                                        "NO_PROGRESS_WARNING: 连续动作未提交验证结果。"
                                        "请立即调用 assert_verification(condition, result)"
                                        " 上报当前可验证项；无法确认时上报 failed。"
                                    )
                                )
                            )
                        else:
                            loop_break_reason = (
                                "NO_PROGRESS: no assert_verification "
                                f"for {_NO_PROGRESS_LIMIT} action tool calls"
                            )
                            logger.warning(loop_break_reason)
                            break

            # 内层循环断路器：连续 N 次相同 tool+args+page_signature 立即终止。
            if name not in (
                "get_screen_info",
                "check_page_health",
                "query_app_knowledge",
            ):
                call_sig = _build_call_signature(name, args, page_sig_once)
                recent.append(call_sig)
                if len(recent) > 8:
                    recent = recent[-8:]
                if (
                    len(recent) >= _LOOP_BREAK_CONSECUTIVE
                    and len(set(recent[-_LOOP_BREAK_CONSECUTIVE:])) == 1
                ):
                    loop_break_reason = (
                        "LOOP_DETECTED: repeated tool+args+page_signature "
                        f"for {_LOOP_BREAK_CONSECUTIVE} times ({name})"
                    )
                    logger.warning(loop_break_reason)
                    break

            # 语义冷却：处理近似抖动（与签名断路器互补）
            milestone = name == "assert_verification" or _output_has_page_change(
                output, page_sig_once, page_sig_after
            )
            if milestone:
                recent_action_groups = []
            elif cooldown_group:
                recent_action_groups.append(cooldown_group)
                if len(recent_action_groups) > 6:
                    recent_action_groups = recent_action_groups[-6:]
                if (
                    len(recent_action_groups) >= 4
                    and recent_action_groups.count(cooldown_group) >= 4
                ):
                    cooldown_map[cooldown_group] = 2
                    recent_action_groups = []
                    outputs.append(
                        SystemMessage(
                            content=(
                                f"COOLDOWN_TRIGGERED: {cooldown_group} 连续重复。"
                                "请改用结构化定位并优先完成验证上报。"
                            )
                        )
                    )
            page_sig_once = page_sig_after

            # 实时记录工具调用日志（过滤感知类，不去重）
            if name not in _SKIP_EMIT:
                _current_log.append(
                    {
                        "name": name,
                        "target": target_hint,
                        "intent_text": (getattr(last_ai, "content", "") or "").strip()[
                            :200
                        ],
                        "observation": output[:200],
                        "screenshot_path": _screenshot_path,
                        "tool_seq": len(_current_log),
                    }
                )

            # report_done: 结构化终止信号，立即终止子图
            if name == "report_done":
                _status = (args.get("status", "") or "").lower()
                _summary = args.get("summary", "") or ""
                loop_break_reason = (
                    f"REPORT_{'DONE' if _status == 'done' else 'ABORT'}: {_summary}"
                )
                break

        return {
            "messages": outputs,
            "_recent_call_sigs": recent,
            "_loop_break_reason": loop_break_reason,
            "_recent_action_groups": recent_action_groups,
            "_cooldown_map": cooldown_map,
            "_no_progress_count": no_progress_count,
            "_no_progress_warned": no_progress_warned,
            "_tool_calls_log": _current_log,
        }

    def _inc(s: _SubState) -> dict:
        return {"_turn_count": s.get("_turn_count", 0) + 1, "messages": []}

    def _limit(s: _SubState) -> str:
        r = tools_condition(s)
        return END if r == "tools" and s.get("_turn_count", 0) >= max_turns else r

    def _after_tools(s: _SubState) -> str:
        return END if s.get("_loop_break_reason") else "llm"

    g = StateGraph(_SubState)
    g.add_node("llm", llm_node)
    g.add_node("tools", _tools_node)
    g.add_node("inc", _inc)
    g.add_edge(START, "llm")
    g.add_conditional_edges("llm", _limit, {"tools": "inc", END: END})
    g.add_edge("inc", "tools")
    g.add_conditional_edges("tools", _after_tools, {"llm": "llm", END: END})
    result = g.compile().invoke(
        {
            "messages": list(messages),
            "_turn_count": 0,
            "_recent_call_sigs": [],
            "_recent_action_groups": [],
            "_loop_break_reason": "",
            "_cooldown_map": {},
            "_no_progress_count": 0,
            "_no_progress_warned": False,
            "_tool_calls_log": [],
            "_run_id": run_id,
        }
    )
    turn_count = result.get("_turn_count", 0)
    loop_break_reason = result.get("_loop_break_reason", "")

    # 从子图 state 获取实时工具调用日志（不再事后从 messages 提取）
    _tool_calls_log = result.get("_tool_calls_log", [])

    if loop_break_reason:
        # report_done 结构化终止：提取状态而非当 ABORT 处理
        if loop_break_reason.startswith("REPORT_DONE:"):
            _summary = loop_break_reason[len("REPORT_DONE:") :].strip()
            return (
                f"DONE: {_summary}",
                _tool_calls_log,
                {
                    "loop_detected": False,
                    "loop_pattern": "",
                    "loop_break_action": "report_done",
                },
            )
        if loop_break_reason.startswith("REPORT_ABORT:"):
            _summary = loop_break_reason[len("REPORT_ABORT:") :].strip()
            return (
                f"ABORT: {_summary}",
                _tool_calls_log,
                {
                    "loop_detected": False,
                    "loop_pattern": "",
                    "loop_break_action": "report_abort",
                },
            )
        return (
            f"ABORT: {loop_break_reason}",
            _tool_calls_log,
            {
                "loop_detected": True,
                "loop_pattern": loop_break_reason,
                "loop_break_action": "end_subgraph",
            },
        )

    # Phase 1.1: 静默截断检测 —— 当 turn 耗尽时注入明确标记
    if turn_count >= max_turns:
        for m in reversed(result["messages"]):
            c = getattr(m, "content", None)
            if c:
                return (
                    str(c) + "\nABORT: MAX_TURNS_EXHAUSTED",
                    _tool_calls_log,
                    {
                        "loop_detected": False,
                        "loop_pattern": "",
                        "loop_break_action": "",
                    },
                )
        return (
            "ABORT: MAX_TURNS_EXHAUSTED — 达到最大工具调用次数",
            _tool_calls_log,
            {
                "loop_detected": False,
                "loop_pattern": "",
                "loop_break_action": "",
            },
        )

    for m in reversed(result["messages"]):
        c = getattr(m, "content", None)
        if c:
            return (
                str(c),
                _tool_calls_log,
                {
                    "loop_detected": False,
                    "loop_pattern": "",
                    "loop_break_action": "",
                },
            )
    return (
        "ABORT: No agent response",
        _tool_calls_log,
        {
            "loop_detected": False,
            "loop_pattern": "",
            "loop_break_action": "",
        },
    )


# Phase 1.2: 锚定行首的 DONE/ABORT 检测（兼容 ##/### Markdown 标题 + **/__/bold 前缀）
_DONE_PATTERN = re.compile(
    r"^(?:#{1,3}\s*)?(?:\*{1,2}|_{1,2})?(DONE|ABORT)\s*[:\uff1a]",
    re.IGNORECASE | re.MULTILINE,
)


def _detect_termination(result: str) -> tuple[bool, bool]:
    """返回 (done, abort) — 取最后一个行首匹配（后追加的标记优先级更高）。"""
    matches = list(_DONE_PATTERN.finditer(result.strip()))
    if not matches:
        return (False, False)
    m = matches[-1]
    return (m.group(1).upper() == "DONE", m.group(1).upper() == "ABORT")


def _ensure_device_alive(max_retries: int = 2, wait_sec: float = 5.0) -> bool:
    """检测设备是否存活。断开时等待 USB monitor 自动重连，最多重试 max_retries 次。

    USB monitor（server.py _start_usb_monitor）在检测到 USB 断开时会将
    ToolContext.device 置为 None，重连后自动重建 ToolContext。
    本函数只需轮询 get_tool_context().device 即可。
    """
    import time as _time

    try:
        ctx = get_tool_context()
        if ctx.device is not None:
            return True
    except Exception:
        pass

    for attempt in range(1, max_retries + 1):
        logger.warning("设备已断开，等待自动重连 (%d/%d)...", attempt, max_retries)
        _time.sleep(wait_sec)
        try:
            ctx = get_tool_context()
            if ctx.device is not None:
                logger.info("设备已自动重连 (第 %d 次尝试)", attempt)
                return True
        except Exception:
            pass

    logger.error("设备重连失败，已达最大重试次数 %d", max_retries)
    return False


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
        prefs["rag_hash"] = hashlib.md5((rag_summary or "").encode("utf-8")).hexdigest()[
            :12
        ]
        setattr(ctx, "_click_preferences", prefs)
    except Exception:
        logger.debug("apply click preferences failed", exc_info=True)


def _should_include_rag(state: TestState, effective_app_package: str) -> bool:
    """Phase 1: 从“每轮预注入”收敛为“首轮 + 触发式注入”。

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
    risky = bool(last.get("loop_detected")) or str(last.get("status", "")).lower() == "fail"
    if not risky and not any(k in obs for k in ("NO_PROGRESS", "COOLDOWN", "LOOP_DETECTED")):
        return False
    # 仍未得到可用 RAG 时，强提示先查知识
    if include_rag and rag_summary:
        return False
    return True


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
    return Command(
        update={
            "goal_description": goal,
            "step_history": [],
            "messages": [],
            "started_at": datetime.now().isoformat(),
            "step_times": [],
            "budget_violation_count": budget_violation_count,
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
            pkg = (ctx.device.current_app() or {}).get("package", "") if ctx.device else ""
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
                    "assert_verification；若无法继续，请立即 report_done(status=\"abort\")。"
                )
            )
        )
        finalization_hint_injected = True
    force_query_hint = _should_force_query_app_knowledge(state, include_rag, rag_summary)
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
        msgs.append(
            SystemMessage(
                content=(
                    "KNOWLEDGE_QUERY_REQUIRED: " + hint_text
                )
            )
        )
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
    target_pages = [str(x or "").strip() for x in (goal.get("target_pages", []) or []) if str(x or "").strip()]
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
            "_tool_calls_log": list(state.get("_tool_calls_log", [])) + tool_calls_log,
        }
    )


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
        if "MAX_TURNS" in conclusion or "MAX_TOOL_CALLS" in conclusion:
            return "exhausted"
        # Agent 主动 ABORT 仍算 completed，verdict 由 test_verdict 决定
        return "completed"
    budget = _calc_budget_from_state(state)
    history = state.get("step_history", [])
    if len(history) >= budget["max_agent_iterations"]:
        return "exhausted"
    return "error"


def _collect_verification_results(goal: dict) -> tuple[str, list]:
    """从 ToolContext._verifications 收集 assert_verification 的结构化结果。"""
    ctx = get_tool_context()
    assertions = getattr(ctx, "_verifications", []) if ctx else []

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
    if execution_status not in ("completed",):
        test_verdict = "inconclusive"
    # 向后兼容 status
    status = (
        "success"
        if (execution_status == "completed" and test_verdict == "passed")
        else status
    )

    if _relational_db:
        try:
            from agents.orchestrator import _build_display_steps

            tool_log = state.get("_tool_calls_log", [])
            dd = _build_display_steps(history, tool_log)
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

    # 统计统一基于 dd（实际展示步骤）
    pc = sum(1 for s in dd if s.get("status") in ("success", "continue"))
    fc = sum(1 for s in dd if s.get("status") == "fail")
    cc = sum(1 for s in dd if s.get("status") == "continue")
    logger.info(
        "Reporter: exec=%s verdict=%s display_steps=%d steps(success=%d fail=%d continue=%d) duration=%.1fs budget_violation=%d conclusion=%s",
        execution_status,
        test_verdict,
        len(dd),
        pc,
        fc,
        cc,
        duration,
        budget_violation_count,
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

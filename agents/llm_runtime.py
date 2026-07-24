"""LLM 运行时：内层子图执行 (_run_agent) + provider 适配 + 重试 + 设备保活。

从 agents/graph.py 拆出（重构 G4），仅移动代码、不改逻辑。
_run_agent 读取 graph 的可变全局 _ws_emit_callback，通过函数内延迟 import 获取当前值。
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Annotated

from langchain_core.messages import SystemMessage, AIMessage, ToolMessage
from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import tools_condition

from config import TestConfig
from llm.clients import _call_with_retry, _default_should_retry
from agents.loop_control import (
    _build_call_signature,
    _build_page_signature,
    _cooldown_group,
    _output_has_page_change,
    _resolve_click_fallback,
    _resolve_click_match_mode,
)
from tools import get_tool_context

import app_paths

logger = logging.getLogger(__name__)


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


def _extract_status_code(output: str) -> str:
    """Read the standard tool status without inventing success for legacy text."""
    try:
        from tools.results import parse_status

        return parse_status(output or "") or "UNSPECIFIED"
    except Exception:
        return "UNSPECIFIED"


def _parse_evidence(output: str) -> dict[str, Any]:
    """Return tool evidence as a safe dict for durable step logging."""
    try:
        from tools.results import parse_evidence

        parsed = parse_evidence(output or "")
        return dict(parsed) if isinstance(parsed, dict) else {}
    except Exception:
        return {}


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


from typing import TypedDict as _TD
from langgraph.graph.message import add_messages


class _SubState(_TD):
    # 循环/空转守卫(_no_progress_count/_no_progress_warned/_recent_call_sigs/
    # _recent_action_groups/_cooldown_map) 已迁移到 ctx._loop_guard，跨 _run_agent
    # 调用持久累计；此处仅保留单 subgraph 内的本地状态。
    messages: Annotated[list, add_messages]
    _turn_count: int
    _loop_break_reason: str
    _tool_calls_log: list
    _run_id: str


_LOOP_BREAK_CONSECUTIVE = 3
_NO_PROGRESS_LIMIT = 8
_FINALIZATION_REMAINING_TOOL_BUDGET = 5
# launch_app 守卫：目标 App 已在前台（package 已匹配）却仍 launch 属冗余重开，
# 累计同 package 超过该阈值则下次直接 COOLDOWN_SKIP，逼 LLM 转向 assert/report_done。
_LAUNCH_REDUNDANT_LIMIT = 3
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


def _accumulate_token_usage(ctx, msg) -> None:
    """O1：把一次 LLM 响应的 usage_metadata 累加到 ctx._token_usage（run 级观测）。

    ChatOpenAI 返回的 AIMessage 自带 usage_metadata，包含 input_tokens /
    output_tokens / total_tokens / input_token_details.cache_read。
    绝不抛异常，缺字段按 0 处理。
    """
    if ctx is None or msg is None:
        return

    um = getattr(msg, "usage_metadata", None)
    if not isinstance(um, dict):
        return

    tu = getattr(ctx, "_token_usage", None)
    if not isinstance(tu, dict):
        return

    tu["input_tokens"] = int(tu.get("input_tokens", 0) or 0) + int(
        um.get("input_tokens", 0) or 0
    )
    tu["output_tokens"] = int(tu.get("output_tokens", 0) or 0) + int(
        um.get("output_tokens", 0) or 0
    )
    tu["total_tokens"] = int(tu.get("total_tokens", 0) or 0) + int(
        um.get("total_tokens", 0) or 0
    )
    tu["llm_calls"] = int(tu.get("llm_calls", 0) or 0) + 1

    details = um.get("input_token_details") or {}
    if isinstance(details, dict):
        tu["cached_input_tokens"] = int(tu.get("cached_input_tokens", 0) or 0) + int(
            details.get("cache_read", 0) or 0
        )


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
    # 延迟 import：读取 graph 的可变全局当前值（set_ws_emit_callback 会更新它）
    from agents.graph import _ws_emit_callback

    try:
        _ctx = get_tool_context()  # 捕获当前 ToolContext 供子图事件发射使用
    except Exception:
        _ctx = None
    if _ctx and _ws_emit_callback:
        _ctx._ws_emit = _ws_emit_callback
    # B: 循环/空转守卫（run 级持久）。放在 ctx 上跨 _run_agent 调用累计，
    # 否则每次外层迭代重建 _SubState 会让 no_progress 计数归零，导致长空转逃过熔断。
    if _ctx is not None:
        _guard = getattr(_ctx, "_loop_guard", None)
        if not isinstance(_guard, dict):
            _guard = {
                "_no_progress_count": 0,
                "_no_progress_warned": False,
                "_recent_call_sigs": [],
                "_recent_action_groups": [],
                "_cooldown_map": {},
                "_launch_count_by_pkg": {},
            }
            _ctx._loop_guard = _guard
    else:
        _guard = {
            "_no_progress_count": 0,
            "_no_progress_warned": False,
            "_recent_call_sigs": [],
            "_recent_action_groups": [],
            "_cooldown_map": {},
        }
    llm_call_count = 0
    tool_call_400_count = 0

    def _is_tool_call_400_error(exc: Exception) -> bool:
        text = str(exc or "")
        if "tool_call_id" in text:
            return True
        return (
            "assistant message with 'tool_calls' must be followed by tool messages"
            in text.lower()
        )

    # 统一走 OpenAI 兼容接入（zhipu / 多模态等通过 base_url 指向其 OpenAI 兼容端点）。
    from langchain_openai import ChatOpenAI

    lc = ChatOpenAI(
        model=model, temperature=0.1, api_key=api_key, base_url=base_url
    ).bind_tools(tools)

    def _llm(s: _SubState) -> dict:
        nonlocal llm_call_count, tool_call_400_count
        # 用户手动停止：立刻终止子图，不再发新的 LLM 请求。
        # 把 stop 转成 loop_break_reason 复用既有"循环检测到终止"的 END 收敛路径。
        if _ctx is not None:
            _ev = getattr(_ctx, "_stop_event", None)
            if _ev is not None and _ev.is_set():
                logger.info("_llm: stop flag hit, returning USER_STOPPED")
                return {
                    "messages": [],
                    "_loop_break_reason": "USER_STOPPED",
                }
        llm_call_count += 1
        if _ctx and _ctx._ws_emit:
            try:
                _ctx._ws_emit("stream_token", "thinking")
            except Exception:
                pass
        current_call_has_tool_400 = False

        def _on_llm_error(exc: Exception) -> None:
            nonlocal current_call_has_tool_400, tool_call_400_count
            if _is_tool_call_400_error(exc) and not current_call_has_tool_400:
                tool_call_400_count += 1
                current_call_has_tool_400 = True

        r = _call_retry("openai", lc.invoke, s["messages"], on_error=_on_llm_error)
        # O1：累计本次 LLM 调用的 token 消耗（run 级，存 ToolContext）
        _accumulate_token_usage(_ctx, r)
        return {"messages": [r] if r else [AIMessage(content="LLM failed")]}

    llm_node = _llm

    # ── 自定义工具执行节点（替代 ToolNode，支持实时事件发射）──
    _tool_map = {t.name: t for t in tools}

    def _tools_node(s: _SubState) -> dict:
        # 用户手动停止：立刻终止，不执行后续工具。
        if _ctx is not None:
            _ev = getattr(_ctx, "_stop_event", None)
            if _ev is not None and _ev.is_set():
                logger.info("_tools_node: stop flag hit, returning USER_STOPPED")
                return {
                    "messages": [],
                    "_loop_break_reason": "USER_STOPPED",
                }
        last_ai = next(m for m in reversed(s["messages"]) if isinstance(m, AIMessage))
        outputs = []
        # 循环/空转守卫直接读写 ctx._loop_guard（跨 _run_agent 调用持久）
        recent = list(_guard.get("_recent_call_sigs", []))
        recent_action_groups = list(_guard.get("_recent_action_groups", []))
        loop_break_reason = s.get("_loop_break_reason", "")
        no_progress_count = int(_guard.get("_no_progress_count", 0) or 0)
        no_progress_warned = bool(_guard.get("_no_progress_warned", False))
        cooldown_map = dict(_guard.get("_cooldown_map", {}) or {})
        _current_log = list(s.get("_tool_calls_log", []))
        page_sig_once = _build_page_signature(_ctx)
        for tc in last_ai.tool_calls or []:
            name = tc["name"]
            args = tc.get("args", {}) or {}
            target_hint = _build_tool_target(name, args)
            cooldown_group = _cooldown_group(name, args, target_hint)
            # launch_app 守卫（pre-check）：目标 App 已在前台却仍 launch 属冗余重开。
            # 若同 package 的冗余重开已累计达阈值，直接 COOLDOWN_SKIP 拒绝，
            # 逼 LLM 转向 assert_verification / report_done，而不是反复"重开试试"。
            if (
                name == "launch_app"
                and _ctx is not None
                and getattr(_ctx, "device", None) is not None
            ):
                _req_pkg = (args.get("package") or "").strip()
                _lc = _guard.setdefault("_launch_count_by_pkg", {})
                # 修复：不再要求"launch 前 App 已在前台"才计为冗余。只要对
                # 同一 package 的 launch 调用累计达阈值，即视为病态反复重开
                # （无论中间是否被切走），直接 COOLDOWN_SKIP 拦截，逼 LLM
                # 转向 assert_verification / report_done。
                if _req_pkg and int(_lc.get(_req_pkg, 0) or 0) >= _LAUNCH_REDUNDANT_LIMIT:
                    outputs.append(
                        ToolMessage(
                            content=(
                                f"COOLDOWN_SKIP: launch_app({_req_pkg}) 已多次重开同一应用，"
                                "请勿重复启动；若无法继续请调用 "
                                "report_done(status='abort')"
                            ),
                            tool_call_id=tc["id"],
                        )
                    )
                    continue
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
            # 每个工具调用前检查 stop——避免被长时间工具调用阻塞
            if _ctx is not None:
                _ev = getattr(_ctx, "_stop_event", None)
                if _ev is not None and _ev.is_set():
                    logger.info("_tools_node: stop flag hit before %s, breaking", name)
                    outputs.append(
                        ToolMessage(
                            content="SKIPPED: 用户手动停止",
                            tool_call_id=tc["id"],
                        )
                    )
                    loop_break_reason = "USER_STOPPED"
                    break
            t = _tool_map.get(name)
            # Capture evidence on the actual invocation boundary.  The before
            # values must be read before a launch/click can change the app.
            page_sig_before = page_sig_once
            try:
                before_app = (
                    _ctx.device.current_app() or {}
                    if _ctx is not None and getattr(_ctx, "device", None) is not None
                    else {}
                )
            except Exception:
                before_app = {}
            try:
                output = str(t.invoke(args)) if t else f"UNKNOWN_TOOL: {name}"
            except Exception as e:
                output = f"ERROR: {e}"
            page_sig_after = _build_page_signature(_ctx)
            try:
                after_app = (
                    _ctx.device.current_app() or {}
                    if _ctx is not None and getattr(_ctx, "device", None) is not None
                    else {}
                )
            except Exception:
                after_app = {}
            progress_milestone = (
                name == "assert_verification"
                or _output_has_page_change(output, page_sig_once, page_sig_after)
            )
            # launch_app 守卫（post-update）：冗余重开（App 已在前台）累计计数；
            # 真实启动（package 变化）则重置。冗余重开不重置 no_progress，让熔断器能 arming。
            if name == "launch_app":
                _req_pkg = (args.get("package") or "").strip()
                _lc = _guard.setdefault("_launch_count_by_pkg", {})
                # 修复：对该 requested_package 的 launch 调用累计 +1（不再要求
                # before_pkg == req_pkg），使反复重开同一包能被阈值拦截。
                if _req_pkg:
                    _lc[_req_pkg] = int(_lc.get(_req_pkg, 0) or 0) + 1
            # 设备断开快速终止：工具执行后立即检测，避免继续执行无意义操作
            try:
                _live_ctx = get_tool_context()
                if _live_ctx.device is None:
                    output = "ERROR: 设备已断开连接"
                    outputs.append(
                ToolMessage(content=output, name=name, tool_call_id=tc["id"])
            )
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
            outputs.append(
                ToolMessage(content=output, name=name, tool_call_id=tc["id"])
            )

            # 最小断路器：连续无进展动作判定为空转（assert 或页面变化均算进展）。
            # 计数持久于 ctx._loop_guard，跨 _run_agent 调用累计，避免长空转逃过熔断。
            # 契约：达到 _NO_PROGRESS_LIMIT 发一次警告（不重置），达到 2× 阈值则硬 ABORT。
            if progress_milestone:
                no_progress_count = 0
                no_progress_warned = False
            else:
                if name in _NO_PROGRESS_ACTIONS:
                    no_progress_count += 1
                    if (
                        no_progress_count == _NO_PROGRESS_LIMIT
                        and not no_progress_warned
                    ):
                        no_progress_warned = True
                        outputs.append(
                            SystemMessage(
                                content=(
                                    "NO_PROGRESS_WARNING: 连续动作未提交验证结果。"
                                    "请立即调用 assert_verification(condition, result)"
                                    " 上报当前可验证项；无法确认时上报 failed。"
                                )
                            )
                        )
                    elif no_progress_count >= 2 * _NO_PROGRESS_LIMIT:
                        loop_break_reason = (
                            "NO_PROGRESS: no assert_verification "
                            f"for {2 * _NO_PROGRESS_LIMIT} consecutive action tool calls"
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
            if progress_milestone:
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

            # Persist the full structured event for every executed tool call,
            # including sensing calls that are intentionally omitted from live UI
            # emission. Result parsing deliberately falls back to UNSPECIFIED;
            # an unstructured successful-looking string is not evidence.
            result_evidence = _parse_evidence(output)
            status_code = _extract_status_code(output)
            # report_done(abort) is an accepted terminal report rather than
            # a tool error; retain its terminal state as evidence.
            if name == "report_done":
                status_code = "OK"
                result_evidence.setdefault(
                    "terminal_status", (args.get("status", "") or "done").lower()
                )
            entry: dict[str, Any] = {
                "name": name,
                "target": target_hint,
                "intent_text": (getattr(last_ai, "content", "") or "").strip()[:200],
                "observation": output[:200],
                "screenshot_path": _screenshot_path,
                "tool_seq": len(_current_log),
                "tool_input": dict(args or {}),
                "status_code": status_code,
                "result_evidence": result_evidence,
                "page_before_signature": page_sig_before,
                "page_after_signature": page_sig_after,
                "page_before_activity": str(before_app.get("activity", "") or ""),
                "page_after_activity": str(after_app.get("activity", "") or ""),
                "page_before_package": str(before_app.get("package", "") or ""),
                "page_after_package": str(after_app.get("package", "") or ""),
            }
            if name == "click":
                entry["match_mode"] = _resolve_click_match_mode(name, args, output)
                entry["fallback_used"] = _resolve_click_fallback(output)
                # C: 模糊匹配（搜索词≠实际标签的语义命中）是独立事实，
                # 透传 fuzzy_match 供 fuzzy_click_rate 门禁统计（非 fallback）。
                entry["fuzzy_match"] = bool(result_evidence.get("fuzzy_match", False))
                entry["resolved_target"] = {
                    "label": result_evidence.get("resolved_label", ""),
                    "role": result_evidence.get("resolved_role", ""),
                    "rid": result_evidence.get("resolved_rid", ""),
                    "class_name": result_evidence.get("resolved_class", ""),
                    "path": result_evidence.get("resolved_path", ""),
                }
            _current_log.append(entry)

            # report_done: 结构化终止信号，立即终止子图
            if name == "report_done":
                _status = (args.get("status", "") or "").lower()
                _summary = args.get("summary", "") or ""
                loop_break_reason = (
                    f"REPORT_{'DONE' if _status == 'done' else 'ABORT'}: {_summary}"
                )
                break

        # 写回 run 级守卫（跨 _run_agent 调用持久）
        _guard["_recent_call_sigs"] = recent
        _guard["_recent_action_groups"] = recent_action_groups
        _guard["_cooldown_map"] = cooldown_map
        _guard["_no_progress_count"] = no_progress_count
        _guard["_no_progress_warned"] = no_progress_warned
        return {
            "messages": outputs,
            "_loop_break_reason": loop_break_reason,
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
            "_loop_break_reason": "",
            "_tool_calls_log": [],
            "_run_id": run_id,
        }
    )
    turn_count = result.get("_turn_count", 0)
    loop_break_reason = result.get("_loop_break_reason", "")

    # 从子图 state 获取实时工具调用日志（不再事后从 messages 提取）
    _tool_calls_log = result.get("_tool_calls_log", [])

    if loop_break_reason:
        # 用户手动停止：子图内 _llm/_tools_node 命中 stop，单独返回，
        # 不被 REPORT_ABORT 的 ABORT 兜底覆盖（语义不同）。
        if loop_break_reason == "USER_STOPPED":
            return (
                "ABORT: USER_STOPPED — 用户手动停止当前运行",
                _tool_calls_log,
                {
                    "loop_detected": False,
                    "loop_pattern": "",
                    "loop_break_action": "user_stopped",
                    "llm_call_count": llm_call_count,
                    "tool_call_400_count": tool_call_400_count,
                },
            )
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
                    "llm_call_count": llm_call_count,
                    "tool_call_400_count": tool_call_400_count,
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
                    "llm_call_count": llm_call_count,
                    "tool_call_400_count": tool_call_400_count,
                },
            )
        return (
            f"ABORT: {loop_break_reason}",
            _tool_calls_log,
            {
                "loop_detected": True,
                "loop_pattern": loop_break_reason,
                "loop_break_action": "end_subgraph",
                "llm_call_count": llm_call_count,
                "tool_call_400_count": tool_call_400_count,
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
                        "llm_call_count": llm_call_count,
                        "tool_call_400_count": tool_call_400_count,
                    },
                )
        return (
            "ABORT: MAX_TURNS_EXHAUSTED — 达到最大工具调用次数",
            _tool_calls_log,
            {
                "loop_detected": False,
                "loop_pattern": "",
                "loop_break_action": "",
                "llm_call_count": llm_call_count,
                "tool_call_400_count": tool_call_400_count,
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
                    "llm_call_count": llm_call_count,
                    "tool_call_400_count": tool_call_400_count,
                },
            )
    return (
        "ABORT: No agent response",
        _tool_calls_log,
        {
            "loop_detected": False,
            "loop_pattern": "",
            "loop_break_action": "",
            "llm_call_count": llm_call_count,
            "tool_call_400_count": tool_call_400_count,
        },
    )


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


def _call_retry(provider, fn, *a, on_error=None, **kw):
    return _call_with_retry(
        lambda e: _call_retry_should_retry(provider, e, on_error),
        fn,
        *a,
        **kw,
    )


def _call_retry_should_retry(provider: str, exc: Exception, on_error=None) -> bool:
    if on_error:
        try:
            on_error(exc)
        except Exception:
            logger.debug("on_error callback failed", exc_info=True)
    return _default_should_retry(exc)


# ═══ LLM config ═══


def _llm_cfg(cfg: TestConfig):
    return {
        "provider": cfg.llm_provider,
        "model": cfg.model,
        "api_key": cfg.api_key,
        "base_url": cfg.base_url,
    }

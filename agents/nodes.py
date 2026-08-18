"""LangGraph 节点：planner / agent / reporter / plan_review + prompt 装配。

从 agents/graph.py 拆出（重构 G5），仅移动代码、不改逻辑。
reporter_node 读取 graph 的可变全局 _relational_db，通过函数内延迟 import 获取当前值。
build_graph 也以延迟 import 方式引用本模块，避免加载期循环依赖。
"""

from __future__ import annotations

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
    _should_force_request_knowledge,
    _should_include_rag,
)
from agents.verification import (
    _build_verification_key_maps,
    _determine_execution_status,
    _goal_verification_items,
    _normalize_verification_text,
    evaluate_verification,
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
from agents.run_trace import compute_resolution_metrics

logger = logging.getLogger(__name__)


class RunStopped(Exception):
    """保留作 orchestrator 最外层兜底——`agents/orchestrator.py:start` 的
    `except RunStopped` 分支是「深度防御」：如果未来某次 LangGraph 演进忽略了
    节点内的 stop 检查、或 stop 检查路径被绕过，最外层这里仍能把图收敛到
    cancelled 返回。实际当前 stop 机制**完全**通过 Command(..., goto="reporter")
    + _loop_break_reason="USER_STOPPED" 实现，不走异常路径。
    """


def _check_stop(ctx) -> bool:
    """检查 stop 标志，命中时返回 True。绝不抛异常。"""
    if ctx is None:
        return False
    ev = getattr(ctx, "_stop_event", None)
    if ev is not None and ev.is_set():
        return True
    return False


def _stop_or_continue(state: TestState, ctx) -> Command | None:
    """节点入口 stop 拦截器。命中 stop 时返回带 goto 的 Command；未命中返回 None。

    **必须**显式 `goto="reporter"`——否则图按既有 routing 会：
    - planner_node 走固定边 planner → plan_review，触发 interrupt 弹计划确认
    - agent_node 走 route_after_agent，"stopped" 不在 ("success", "fail") 中，
      不在 max_iterations 时会被路由回 agent_node 死循环，直到 step_history 撞
      max_agent_iterations 才收敛——既浪费回合又污染 step_history。

    返回的 Command 携带：
    - conclusion: 标准 ABORT 前缀，便于 _detect_termination 识别
    - status: "stopped"（与 success/fail/cancelled 并列的语义状态）
    - _stop_requested: True，供 reporter 写入 execution_status="cancelled"
    """
    if not _check_stop(ctx):
        return None
    history = state.get("step_history", []) if isinstance(state, dict) else []
    si = len(history) + 1
    conclusion = "ABORT: USER_STOPPED — 用户手动停止当前运行"
    nh = list(history) + [
        {
            "index": si,
            "intent": "user_stop",
            "action_type": "user_stop",
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
    logger.info("Node entry: stop flag hit, redirecting to reporter (step=%d)", si)
    return Command(
        update={
            "conclusion": conclusion,
            "status": "stopped",
            "step_history": [nh[-1]],
            "_stop_requested": True,
        },
        goto="reporter",
    )


# 注入给 LLM 的 page_info 带文本元素上限。真实全局 index 不受展示顺序影响。
_AGENT_PAGE_INFO_MAX_ELEMENTS = 60
# 额外展示少量无文本代表项，避免课程网格刷屏；混合页面最多输出 60 + 8 项。
_AGENT_PAGE_INFO_UNLABELED_REPRESENTATIVE_LIMIT = 8


def _select_page_info_clickables(
    clickable_elements: list[Any], max_items: int = _AGENT_PAGE_INFO_MAX_ELEMENTS
) -> list[tuple[int, Any]]:
    """Prioritize labeled navigation anchors without changing global click indexes.

    The returned tuples retain indexes from the canonical all-clickable sequence.
    When a page mixes text controls and a large unlabeled grid, reserve a small
    tail for representative grid cells instead of letting raw UI order crowd out
    textual controls that guide immediate agent decisions.
    """
    indexed = list(enumerate(clickable_elements))
    if max_items <= 0 or len(indexed) <= max_items:
        return indexed[:max_items]
    labeled = [
        item for item in indexed if (getattr(item[1], "label", "") or "").strip()
    ]
    unlabeled = [
        item for item in indexed if not (getattr(item[1], "label", "") or "").strip()
    ]
    if not labeled or not unlabeled:
        return indexed[:max_items]
    # 带文本导航/操作锚点最多展示 max_items 个；另追加固定数量的无文本
    # 代表项。两者的全局 index 均来自 canonical 原始序列，不受展示重排影响。
    unlabeled_limit = min(
        _AGENT_PAGE_INFO_UNLABELED_REPRESENTATIVE_LIMIT,
        len(unlabeled),
    )
    return labeled[:max_items] + unlabeled[:unlabeled_limit]


def _load_prompt(name: str) -> str:
    _dir = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(_dir, "prompts", name)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


PLANNER_SYSTEM = _load_prompt("planner.txt")
AGENT_SYSTEM = (
    _load_prompt("agent_common.txt") + "\n" + _load_prompt("agent_explore.txt")
)


def _select_agent_system(state: TestState) -> str:
    return AGENT_SYSTEM


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
    # 入口 stop 检查：在 LLM 调用之前拦截，避免无意义的规划开销
    _stop_cmd = _stop_or_continue(state, ctx)
    if _stop_cmd is not None:
        return _stop_cmd
    kb = ctx.knowledge_base if ctx else None
    rag = _rag_ctx(kb, state.get("app_package", ""), state.get("user_request", ""))
    rag, rag_truncated = _clip_to_token_budget(rag, 500)
    # reducer 通道：只上报本次增量（delta），不要读旧值累加，也不要重置。
    budget_violation_count = 1 if rag_truncated else 0
    msgs = PLANNER_TEMPLATE.format_messages(
        user_request=state.get("user_request", ""),
        app_name=state.get("app_name", ""),
        app_package=state.get("app_package", ""),
        rag_context=rag,
    )
    cl = create_llm_client(
        model=llm["model"],
        api_key=llm["api_key"],
        base_url=llm["base_url"],
    )
    import time as _time

    goal: dict[str, Any] | None = None
    for _attempt in range(3):
        _t0 = _time.time()
        raw = _call_retry(cl.invoke, msgs) if cl else None
        logger.info("Planner LLM: %.1fs", _time.time() - _t0)
        if raw is None:
            break
        text = raw.content if hasattr(raw, "content") else str(raw)
        candidate = _parse_goal(text)
        if _goal_is_usable(candidate):
            goal = candidate
            break
        # 格式不符合要求：把错误反馈回 LLM 让其重新生成，而不是在解析层堆特例。
        msgs = list(msgs) + [
            AIMessage(content=text),
            HumanMessage(
                content=(
                    "输出格式不符合要求：verification 必须是非空字符串数组，"
                    "goal 必须是纯文本（不要再嵌套 JSON）。请只重新输出一个 JSON 对象。"
                )
            ),
        ]
    if goal is None:
        goal = {
            "goal": state.get("user_request", ""),
            "target_pages": [],
            "verification": [],
            "hints": [],
            "parameter_slots": [],
        }
    logger.info("Planner: %s", goal.get("goal", "")[:80])
    from agents.verification import build_verification_contract

    verification_contract = build_verification_contract(
        goal, state.get("user_request", "")
    )
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
            "verification_contract": verification_contract,
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
    ctx = get_tool_context()
    ctx._execution_mode = str(state.get("execution_mode", "explore") or "explore")

    # 入口 stop 检查：优先于设备检查、感知、LLM 调用——命中直接收敛
    _stop_cmd = _stop_or_continue(state, ctx)
    if _stop_cmd is not None:
        return _stop_cmd

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
                "step_history": [nh[-1]],
                "status": "fail",
                "conclusion": conclusion,
            }
        )

    # Page info（ctx 已在函数顶部获取，stop 检查在更早）
    page_info = "unknown"
    pid = ""
    page_activity_short = ""
    page_text_snapshot = ""
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
            page_activity_short = act
            title = u.page_title or ""
            pid = act + "「" + title + "」" if title else act
            pkg = (
                (ctx.device.current_app() or {}).get("package", "")
                if ctx.device
                else ""
            )
            current_app_key = f"{pkg}:{act}"
            # 契约：全局 [n] 与 click(index=n) 覆盖所有真实可点击元素，
            # 无文本课程格等元素不能因 label 为空而从候选池消失。
            clickable_elements = [e for e in u.elements if e.clickable]
            page_text_snapshot = " ".join(
                str(getattr(e, "label", "") or "").strip()
                for e in u.elements
                if str(getattr(e, "label", "") or "").strip()
            )
            n_clickable = len(clickable_elements)
            n_labeled_clickable = sum(
                bool((e.label or "").strip()) for e in clickable_elements
            )
            lines = [
                "page=" + pid,
                "layout=" + u.layout,
                "clickable_total=" + str(n_clickable),
                "labeled_clickable=" + str(n_labeled_clickable),
            ]
            if u.layout == "two_pane":
                lines.append(
                    "panels（动态边界；[n] 始终是全局可点击序号，不是面板内序号）："
                )
                for panel in getattr(u, "regions", []) or []:
                    name = str(panel.get("name", "main_content"))
                    bounds = panel.get("bounds", [])
                    panel_elements = [
                        element for element in u.elements if element.region == name
                    ]
                    panel_clickables = [
                        element for element in panel_elements if element.clickable
                    ]
                    panel_indexes = [
                        index
                        for index, element in enumerate(clickable_elements)
                        if element.region == name
                    ]
                    labeled_count = sum(
                        bool((element.label or "").strip())
                        for element in panel_clickables
                    )
                    unlabeled = [
                        element
                        for element in panel_clickables
                        if not (element.label or "").strip()
                    ]
                    index_text = ",".join(map(str, panel_indexes[:12])) or "无"
                    if len(panel_indexes) > 12:
                        index_text += f",...(+{len(panel_indexes) - 12})"
                    lines.append(
                        f"- {name} bounds={bounds} elements={len(panel_elements)} "
                        f"clickable_total={len(panel_clickables)} "
                        f"labeled_clickable={labeled_count} "
                        f"global_indexes={index_text}"
                    )
                    if name == "right_content" and unlabeled:
                        example = unlabeled[0]
                        example_index = clickable_elements.index(example)
                        example_class = (example.class_name or "").split(".")[-1] or "?"
                        example_path = example.context_path or "?"
                        lines.append(
                            f"  ! 右侧存在 {len(unlabeled)} 个无文本可点击元素；"
                            f"使用全局 [n]，不要使用左侧导航的 [n] 代替。"
                        )
                        lines.append(
                            f"    示例：[{example_index}] <无文本> class={example_class} "
                            f"bounds={example.bounds} path={example_path}"
                        )
                    elif (
                        name == "right_content"
                        and panel_elements
                        and not panel_clickables
                    ):
                        lines.append(
                            "  ! 右侧内容区没有真实可点击元素；不要使用左侧导航的 [n] 代替。"
                        )
            # 仅 page_info 的展示顺序优先保留带文本导航锚点；每项仍携带
            # canonical all-clickable 序列中的真实全局 [n]，不改变 click(index=n)。
            page_info_items = _select_page_info_clickables(clickable_elements)
            for global_index, e in page_info_items:
                role = e.role or ""
                rid = (e.resource_id or "").split("/")[-1] if e.resource_id else ""
                region = e.region or "main_content"
                label_text = (e.label or "").strip() or "<无文本>"
                extra = " [" + region + "/" + (role or "unknown") + "]"
                if rid:
                    extra += " rid=" + rid
                if not (e.label or "").strip():
                    class_name = (e.class_name or "").split(".")[-1] or "?"
                    extra += f" class={class_name} bounds={e.bounds}"
                    if e.context_path:
                        extra += " path=" + e.context_path
                lines.append(f"  - [{global_index}] " + label_text + extra)
            if n_clickable > len(page_info_items):
                lines.append(
                    f"  ...（另有 {n_clickable - len(page_info_items)} 个真实可点击元素未列出；"
                    "用 get_screen_info(mode='clickable', offset=...) 分页查看全局 [n]）"
                )
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
    effective_app_package = goal.get("app_package", "") or state.get("app_package", "")
    include_rag = _should_include_rag(state, effective_app_package)
    rag_summary = ""
    rag_truncated = False
    if include_rag:
        rag_summary = _rag_ctx(
            ctx.knowledge_base if ctx else None,
            effective_app_package,
            state.get("user_request", ""),
        )
        rag_summary, rag_truncated = _clip_to_token_budget(rag_summary, 500)
    # reducer 通道：累计本地增量（rag 截断 + 后置裁剪 + 重复检测），只上报 delta。
    budget_violation_count = 1 if rag_truncated else 0
    if include_rag and rag_summary and ctx:
        _apply_click_preferences(ctx, rag_summary, effective_app_package)
    _cfg_steps = max(1, getattr(cfg, "context_history_steps", 5) or 5)
    hist_lines = [
        f"  [{s.get('status','')}] {s.get('intent','')}: {str(s.get('observation',''))[:100]}"
        for s in history[-_cfg_steps:]
    ]
    hist_str = "\n".join(hist_lines) if hist_lines else "(none)"
    key_lookup, key_to_item = _build_verification_key_maps(goal)
    verification_key_guide = "\n".join(
        f"- {key}: {item}" for key, item in key_to_item.items()
    )
    clause_guide = "\n".join(
        f"- {verification.get('key', '')}/{clause.get('id', '')}: "
        + ", ".join(str(channel) for channel in clause.get("channels", []) or [])
        for verification in (state.get("verification_contract", {}) or {}).get(
            "verifications", []
        )
        or []
        if isinstance(verification, dict)
        for clause in verification.get("clauses", []) or []
        if isinstance(clause, dict)
    )
    if ctx:
        ctx._verification_key_map = key_lookup
        ctx._verification_items_by_key = key_to_item
        ctx._verification_contract = (
            state.get("verification_contract", {})
            if isinstance(state.get("verification_contract", {}), dict)
            else {}
        )
        merged_verifications = (getattr(ctx, "_clause_state", {}) or {}).get(
            "verifications", []
        )
        passed_items = [
            f"[{entry.get('key', '')}] {key_to_item.get(entry.get('key', ''), '')}"
            for entry in merged_verifications
            if str(entry.get("result", "") or "") == "passed"
        ]
        if passed_items:
            hist_str += "\n\n已通过验证: " + "; ".join(passed_items)

    # Messages — always include goal + page for context
    msgs = list(state.get("messages", []))
    if not msgs:
        msgs = [SystemMessage(content=_select_agent_system(state))]
        if state.get("execution_mode") == "guided":
            guided_actions = []
            for action in state.get("selected_plan_actions", []) or []:
                if not isinstance(action, dict):
                    continue
                try:
                    tool_input = json.loads(action.get("tool_input_json") or "{}")
                    precondition = json.loads(action.get("precondition_json") or "{}")
                    postcondition = json.loads(action.get("postcondition_json") or "{}")
                except (TypeError, json.JSONDecodeError):
                    continue
                guided_actions.append(
                    {
                        "tool": action.get("tool_name", ""),
                        "input": tool_input,
                        "precondition": precondition,
                        "postcondition": postcondition,
                    }
                )
            if guided_actions:
                msgs.append(
                    SystemMessage(
                        content=(
                            "GUIDED_PLAN: These are historical candidate actions. "
                            "Use them only when current perception satisfies their "
                            "preconditions; adapt or explore when it does not.\n"
                            + json.dumps(guided_actions, ensure_ascii=False)
                        )
                    )
                )
    used_tool_calls_before = len(state.get("_tool_calls_log", []) or [])
    remaining_tool_budget = budget["max_tool_calls_total"] - used_tool_calls_before
    finalization_hint_injected = bool(state.get("_finalization_hint_injected", False))
    force_query_hint = _should_force_request_knowledge(state, include_rag, rag_summary)
    knowledge_query_hint_injected = bool(
        state.get("_knowledge_query_hint_injected", False)
    )
    # force_query_hint 不再成立时清除标记，使后续再次命中可重新注入
    if not force_query_hint:
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
    last_page_app_key = str(state.get("_last_page_app_key", "") or "")

    # ── M1：收集启发式提示候选，单轮只注入优先级最高 1 条（config 可关）──
    # 元组：(priority[越小越高], kind, content, emit_msg)
    hint_candidates: list[tuple[int, str, str, str]] = []

    if (
        remaining_tool_budget <= _FINALIZATION_REMAINING_TOOL_BUDGET
        and not finalization_hint_injected
    ):
        hint_candidates.append(
            (
                0,
                "finalization",
                "FINALIZATION_HINT: 剩余工具预算较低。请优先对可判定项调用 "
                '验证当前页面；若无法继续，请立即 terminate_run(reason="无法安全继续")。',
                "",
            )
        )
    if force_query_hint and not knowledge_query_hint_injected:
        query_text = (state.get("user_request", "") or "").strip()[:40]
        if not query_text:
            query_text = "当前页面下一步"
        _kq = (
            "检测到循环或无进展风险，下一步先调用 "
            f'request_knowledge(intent="{query_text}") '
            "再执行点击/滑动。"
        )
        hint_candidates.append(
            (1, "knowledge", "KNOWLEDGE_QUERY_REQUIRED: " + _kq, _kq)
        )
    if self_doubt_reasons:
        hint_candidates.append(
            (
                2,
                "self_doubt",
                "SELF_DOUBT_HINT: 检测到不确定状态（"
                + "；".join(self_doubt_reasons[:2])
                + "）。下一步先调用 get_screen_info 复核；若仍无法确认路径，请立即 "
                'terminate_run(reason="页面异常，建议人工确认")。',
                "",
            )
        )
    if current_app_key and current_app_key != last_page_app_key and last_page_app_key:
        _as = (
            f"已进入新应用上下文（{current_app_key}），如不确定下一步，优先调用 "
            'request_knowledge(intent="当前页面下一步")。'
        )
        hint_candidates.append((3, "app_switch", "APP_SWITCH_HINT: " + _as, _as))

    if getattr(cfg, "single_hint_per_turn", True):
        selected = sorted(hint_candidates, key=lambda c: c[0])[:1]
    else:
        selected = hint_candidates
    for _prio, _kind, _content, _emit in selected:
        msgs.append(SystemMessage(content=_content))
        if _kind == "finalization":
            finalization_hint_injected = True
        elif _kind == "knowledge":
            knowledge_query_hint_injected = True
        if _emit and ctx and getattr(ctx, "_ws_emit", None):
            try:
                ctx._ws_emit("knowledge_hint", {"message": _emit})
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
                "\n\nVerification keys（上报时必须传 verification_key，condition 可写当前证据描述）：\n"
                + verification_key_guide
                if verification_key_guide
                else ""
            )
            + (
                "\n\nClause channels（验证工具必须同时传 verification_key 与 clause_id）：\n"
                + clause_guide
                if clause_guide
                else ""
            )
            + (
                "\n\nKnowledge Policy:\n默认不预置场景知识。若不确定下一步，"
                "优先调用 request_knowledge(intent) 获取当前场景知识。"
                if not include_rag
                else ""
            )
            + ("\n\nRAG:\n" + rag_summary if rag_summary else "")
        )
    )

    result, tool_calls_log, loop_meta, terminal_verdict = _run_agent(
        msgs,
        AGENT_TOOLS,
        llm["model"],
        llm["api_key"],
        llm["base_url"],
        max_turns=budget["max_turns_per_iteration"],
        run_id=config.get("configurable", {}).get("thread_id", "unknown"),
    )
    # reducer 通道：只上报本次迭代增量（delta），累计由通道 reducer 完成。
    iter_llm_call_count = int(loop_meta.get("llm_call_count", 0) or 0)
    iter_tool_call_400_count = int(loop_meta.get("tool_call_400_count", 0) or 0)
    llm_call_count = iter_llm_call_count
    tool_call_400_count = iter_tool_call_400_count
    # 派生比率：基于（累计值 + 本次增量）估算，仅用于实时展示，reporter 会重算。
    _acc_llm = int(state.get("llm_call_count", 0) or 0) + iter_llm_call_count
    _acc_400 = int(state.get("tool_call_400_count", 0) or 0) + iter_tool_call_400_count
    tool_call_400_rate = (
        round(_acc_400 / _acc_llm, 4) if _acc_llm > 0 else 0.0
    )
    # 不再写入 ctx._tool_calls_log（避免 rebuild 丢失），改为存入 state
    logger.info("Agent #%d: %s", len(history) + 1, result[:200])

    done, abort = _detect_termination(result)
    # 结构化 verdict 优先：消除对 DONE:/ABORT: 文本前缀的依赖
    if terminal_verdict == "passed":
        done = True
        abort = False
    elif terminal_verdict == "failed":
        done = False
        abort = True
    used_tool_calls_total = used_tool_calls_before + len(tool_calls_log)
    if used_tool_calls_total >= budget["max_tool_calls_total"] and not done:
        abort = True
        done = False
        terminal_verdict = ""  # 资源耗尽不是明确的测试 verdict
        result = (
            result.rstrip()
            + f"\nABORT: MAX_TOOL_CALLS_EXHAUSTED ({used_tool_calls_total}/{budget['max_tool_calls_total']})"
        )
    # 结构化终止请求由 _run_agent 转为 ABORT；成功只能来自 evaluator 的证据 verdict。
    _signal_source = "text" if (done or abort) else "none"
    if loop_meta.get("loop_break_action") == "terminate_run":
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
                "request_knowledge",
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

    tool_calls_log_tagged = [item for item in tool_calls_log if isinstance(item, dict)]

    nh = list(history) + [
        {
            "index": si,
            "execution_mode": str(state.get("execution_mode", "explore") or "explore"),
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
        um = [SystemMessage(content=_select_agent_system(state))]
    um.append(AIMessage(content=result))

    # ═══ 操作后回呈确定性结果事实（契约：动作的事实原样回呈，不限于开关）═══
    if not done and not abort and ctx and ctx.perceiver:
        last_ai_msgs = [m for m in msgs[-4:] if isinstance(m, AIMessage)]
        had_action = False
        for m in last_ai_msgs:
            for tc in getattr(m, "tool_calls", None) or []:
                if tc.get("name") not in (
                    "get_screen_info",
                    "check_page_health",
                    "request_knowledge",
                ):
                    had_action = True
                    break
            if had_action:
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
                )
                # 回呈最近一次动作的确定性结果事实（开关勾选态、权限弹窗等），
                # 让 LLM 总能看到「我刚做的事，程序核实的结果」，把精力留给下一步判断。
                if tool_calls_log:
                    _ev = tool_calls_log[-1].get("result_evidence") or {}
                    _facts = []
                    if "checked" in _ev:
                        _state_cn = "开启" if _ev["checked"] else "关闭"
                        _facts.append(f"开关勾选态={_state_cn}")
                    if _ev.get("permission_dialog"):
                        _facts.append(
                            f"出现权限弹窗(按钮: {_ev.get('permission_buttons', '')})"
                        )
                    if _ev.get("fallback_used"):
                        _facts.append(f"匹配模式={_ev.get('match_mode', '')}(回退)")
                    if _facts:
                        post_check += "操作结果: " + "；".join(_facts) + "\n"
                post_check += (
                    "如果页面状态满足验证条件，继续收集对应 clause 的工具证据。"
                )
                post_check, violated = _clip_to_token_budget(post_check, 160)
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
                    "请立即调 get_screen_info 检查当前状态；目标达成时继续收集 clause 证据。"
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
                    "如果目标已达成，请继续调用对应验证工具收集 clause 证据。"
                )
                dup_warning, violated = _clip_to_token_budget(dup_warning, 100)
                if violated:
                    budget_violation_count += 1
                um.append(HumanMessage(content=dup_warning))

    # Phase 1.4: 裁剪时保留 system prompt + 带 Goal 的消息 + 最近消息
    # O2: 折叠历史 get_screen_info 大输出（config 可关闭）
    _prune_messages(
        um,
        summarize_stale_screens=getattr(cfg, "context_summarize_stale_screens", True),
    )

    if done or abort:
        return Command(
            update={
                "step_history": [nh[-1]],
                "messages": um,
                "status": "success" if done else "fail",
                "conclusion": result.strip(),
                "_terminal_verdict": terminal_verdict,
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
                + tool_calls_log_tagged,
            }
        )
    return Command(
        update={
            "step_history": [nh[-1]],
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
            "_tool_calls_log": list(state.get("_tool_calls_log", []))
            + tool_calls_log_tagged,
        }
    )


def reporter_node(state: TestState, config: RunnableConfig) -> Command:
    cfg: TestConfig = config["configurable"]["test_config"]
    history = state.get("step_history", [])
    conclusion = state.get("conclusion", "")
    terminal_verdict = state.get("_terminal_verdict", "")
    if terminal_verdict == "passed":
        status = "success"
    elif terminal_verdict == "failed":
        status = "fail"
    else:
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
    evidence_events = list(getattr(ctx, "_evidence_events", []) or [])

    # ── Contract-only verdict ──
    execution_status = _determine_execution_status(state)
    verification_contract = state.get("verification_contract", {})
    evaluation = evaluate_verification(
        verification_contract if isinstance(verification_contract, dict) else {},
        evidence_events,
    )
    test_verdict = str(evaluation.get("verdict", "inconclusive"))
    # 结构化 verdict 优先：evidence 已到 passed/failed 且运行是「自然结束但缺 DONE
    # 前缀」导致的 error（如用 visual_check 验证、子循环自然结束）时，判为 completed。
    # 只纠正 error，不动 exhausted/cancelled/device_offline。
    if (
        evaluation.get("verdict") in ("passed", "failed")
        and execution_status == "error"
    ):
        execution_status = "completed"
    if evaluation.get("terminated_on_authoritative_failure"):
        failure_summary = json.dumps(
            {
                "terminated_on_authoritative_failure": True,
                "failed_clauses": evaluation.get("failed_clauses", []),
                "pending_clauses": evaluation.get("pending_clauses", []),
            },
            ensure_ascii=False,
        )
        conclusion = f"{conclusion}\n{failure_summary}".strip()
    statements = (
        {
            str(item.get("key", "") or ""): str(item.get("statement", "") or "")
            for item in verification_contract.get("verifications", [])
            if isinstance(item, dict)
        }
        if isinstance(verification_contract, dict)
        else {}
    )

    # ── 诊断日志：clause channels vs 实际 evidence 匹配情况 ──
    # 用于快速定位「证据是否产生」以及「是否因 channels 配置被过滤」。
    # channels 从原始 contract 读（evaluate_verification 返回的 clause 不含 channels）。
    _contract_clause_meta: dict[tuple[str, str], dict[str, Any]] = {}
    for v in (verification_contract.get("verifications", []) or []):
        key = str(v.get("key", "") or "")
        for c in (v.get("clauses", []) or []):
            _contract_clause_meta[(key, str(c.get("id", "") or ""))] = c

    _diag_lines = ["Evidence matching diagnostics:"]
    for item in evaluation.get("verifications", []) or []:
        key = str(item.get("key", "") or "")
        for clause in item.get("clauses", []) or []:
            cid = str(clause.get("id", "") or "")
            claim = str(clause.get("claim", "") or "")
            meta = _contract_clause_meta.get((key, cid), {})
            channels = meta.get("channels", []) or []
            matched = [
                e
                for e in evidence_events
                if str(e.get("verification_key", "") or "") == key
                and str(e.get("clause_id", "") or "") == cid
            ]
            matched_channels = {str(e.get("channel", "") or "") for e in matched}
            _diag_lines.append(
                f"  {key}/{cid}: claim='{claim}' channels={channels} "
                f"matched_channels={sorted(matched_channels)} status={clause.get('status')}"
            )
    logger.info("\n".join(_diag_lines))

    # 给每个 clause 补充「决定性证据」：第一个 PASS/YES 或 FAIL/NO 事件及其通道，
    # 便于 reporter 展示「为何通过 / 为何失败」。透出 authoritative 字段（Plan §5.3.2/§5.4）：
    # 非权威 FAIL（如 toggled 过渡态）authoritative=False，可在 trace 中观测到它不会触发 fail-fast。
    _deciding_evidence: dict[tuple[str, str], dict[str, Any]] = {}
    for event in evidence_events:
        if not isinstance(event, dict):
            continue
        status = str(event.get("status", "") or "").upper()
        if status not in {"PASS", "YES", "FAIL", "NO"}:
            continue
        key = str(event.get("verification_key", "") or "")
        cid = str(event.get("clause_id", "") or "")
        if (key, cid) not in _deciding_evidence:
            _deciding_evidence[(key, cid)] = {
                "channel": str(event.get("channel", "") or ""),
                "status": status,
                "authoritative": bool(event.get("authoritative", False)),
                "fact": event.get("fact", {}),
            }

    def _enrich_clause(key: str, clause: dict[str, Any]) -> dict[str, Any]:
        cid = str(clause.get("id", "") or "")
        enriched = dict(clause)
        enriched["deciding_evidence"] = _deciding_evidence.get((key, cid))
        return enriched

    verification_results = [
        {
            "key": item.get("key", ""),
            "item": statements.get(str(item.get("key", "") or ""), ""),
            "result": item.get("result", "unknown"),
            "clauses": [
                _enrich_clause(str(item.get("key", "") or ""), c)
                for c in item.get("clauses", [])
            ],
            "review_required": item.get("result") == "unknown",
        }
        for item in evaluation.get("verifications", [])
    ]
    budget_violation_count = int(state.get("budget_violation_count", 0) or 0)
    llm_call_count = int(state.get("llm_call_count", 0) or 0)
    tool_call_400_count = int(state.get("tool_call_400_count", 0) or 0)
    tool_call_400_rate = float(state.get("tool_call_400_rate", 0.0) or 0.0)
    # 用户手动停止：优先级最高，**不**被 V1 全过归正覆盖。
    # 即便所有验证都通过了，用户主动停止也只记 cancelled——停止是一种
    # 主动意图，不是"自然完成"。同时保证 test_verdict 为 inconclusive。
    if _check_stop(ctx) or bool(state.get("_stop_requested", False)):
        logger.info(
            "Reporter: 用户手动停止 → execution_status=cancelled, test_verdict=inconclusive"
        )
        execution_status = "cancelled"
        test_verdict = "inconclusive"
    if execution_status not in ("completed",):
        test_verdict = "inconclusive"
    # 向后兼容 status（仅在无结构化 verdict 时生效）
    if not terminal_verdict:
        status = (
            "success"
            if (execution_status == "completed" and test_verdict == "passed")
            else status
        )

    # ── 点击质量指标 ──
    _tool_log = state.get("_tool_calls_log", [])
    click_count = sum(1 for s in _tool_log if s.get("name") == "click")
    # Phase 4 locator 解析指标（统一走 run_trace 的聚合逻辑，避免分叉）。
    # exact_count / semantic_count 由 resolution_type 得出；旧逻辑误用 match_mode=="exact"
    # （click 的 match_mode 实际为 resource_id/element/...，永不命中），此处纠正。
    _resolution_metrics = compute_resolution_metrics(_tool_log)
    rag_query_count = int(getattr(ctx, "_rag_query_count", 0) or 0)

    # RAG same_app ratio: 统计 request_knowledge 调用中 same_app 回应的占比
    _rag_same_app = int(getattr(ctx, "_rag_same_app_count", 0) or 0)
    _rag_cross_app = int(getattr(ctx, "_rag_cross_app_count", 0) or 0)
    _rag_empty = int(getattr(ctx, "_rag_empty_hit_count", 0) or 0)
    rag_total_resolved = _rag_same_app + _rag_cross_app + _rag_empty
    rag_same_app_ratio = round(_rag_same_app / max(rag_total_resolved, 1), 4)
    rag_empty_hit_rate = round(_rag_empty / max(rag_total_resolved, 1), 4)
    # 将 RAG 检索指标一并并入 resolution_metrics，便于报告接口一并返回
    _resolution_metrics["rag_query_count"] = rag_query_count
    _resolution_metrics["rag_same_app_ratio"] = round(rag_same_app_ratio, 3)
    _resolution_metrics["rag_empty_hit_rate"] = round(rag_empty_hit_rate, 3)
    exact_count = _resolution_metrics["exact_resolution_count"]
    semantic_count = _resolution_metrics["semantic_resolution_count"]
    fuzzy_count = sum(
        1 for s in _tool_log if s.get("name") == "click" and s.get("fuzzy_match")
    )
    ambiguous_count = sum(
        1
        for s in _tool_log
        if s.get("name") == "click" and s.get("match_mode") == "ambiguous"
    )

    # Phase 2/3 执行模式状态机透出（Plan §2）：字段由 mode_selection_node 写入 state，
    # 此处仅读取并透出到 log / trace，不新增任何模式决策逻辑（契约收敛，不堆补丁）。
    _exec_mode = str(state.get("execution_mode", "explore") or "explore")
    _lifecycle = str(state.get("lifecycle_state", "") or "")
    _plan_id = str(state.get("plan_id", "") or "")
    _plan_trust = str(state.get("plan_trust", "") or "")
    _mode_reason = str(state.get("mode_selection_reason", "") or "")
    _mode_transitions = list(state.get("mode_transition_events", []) or [])

    # O1: 单次运行 token 消耗（纯观测）
    token_usage = dict(getattr(ctx, "_token_usage", {}) or {})

    evidence_event_counts: dict[str, int] = {}
    for event in evidence_events:
        if isinstance(event, dict):
            event_type = str(event.get("channel", "") or "unknown")
            evidence_event_counts[event_type] = (
                evidence_event_counts.get(event_type, 0) + 1
            )

    # 延迟 import：读取 graph 的可变全局当前值（set_relational_db 会更新它）
    from agents.graph import _relational_db
    from agents.orchestrator import _build_display_steps

    dd = _build_display_steps(history, _tool_log)
    if _relational_db:
        try:
            action_events = list(getattr(ctx, "_action_events", []) or [])
            generated_plan_id = None
            if (
                test_verdict == "passed"
                and isinstance(verification_contract, dict)
                and verification_contract.get("status") == "approved"
            ):
                from agents.plan_extractor import extract_candidate_plan

                kb = getattr(ctx, "knowledge_base", None) if ctx else None
                # #1 修复：沉淀侧必须把 app_version/fixture 写进 environment_key，
                # 否则评分 optional 档（app_version 漂移 -0.2、fixture 漂移 -0.3）
                # 永远不触发（候选 plan 的 page 只含 package/activity/screen_profile）。
                _app_pkg = state.get("app_package", "")
                _env_app_version = _get_app_version(ctx, _app_pkg)
                _env_fixture = cfg.fixture_profile
                # 注意：action_events 来自 ctx._action_events（reporter 之前可能还持有同一引用），
                # 因此这里对 page 字典做浅拷贝后再注入，避免 in-place 改写污染上层持有的副本。
                for _ev in action_events:
                    if not isinstance(_ev, dict):
                        continue
                    for _page_key in ("page_before", "page_after"):
                        _page = _ev.get(_page_key)
                        if not isinstance(_page, dict):
                            continue
                        _page = dict(_page)
                        if _env_app_version and not _page.get("app_version"):
                            _page["app_version"] = _env_app_version
                        if _env_fixture and not _page.get("fixture_fingerprint"):
                            _page["fixture_fingerprint"] = _env_fixture
                        _ev[_page_key] = _page
                generated_plan_id = extract_candidate_plan(
                    _relational_db,
                    app_package=_app_pkg,
                    user_request=state.get("user_request", ""),
                    goal=state.get("goal_description") or {},
                    verification_contract=verification_contract,
                    action_events=action_events,
                    evidence_events=evidence_events,
                    knowledge_base=kb,
                )
            _relational_db.record_execution_run(
                run_id=config.get("configurable", {}).get("thread_id", ""),
                user_request=state.get("user_request", ""),
                app_package=state.get("app_package", ""),
                goal=state.get("goal_description") or {},
                verification_contract=(
                    verification_contract
                    if isinstance(verification_contract, dict)
                    else {}
                ),
                execution_mode=str(state.get("execution_mode", "explore") or "explore"),
                lifecycle_state="Terminal",
                plan_id=state.get("plan_id") or generated_plan_id,
                plan_trust=str(state.get("plan_trust", "") or ""),
                verdict=test_verdict,
                terminal_reason=str(conclusion)[:2000],
                resolution_metrics=_resolution_metrics,
                duration_seconds=float(duration or 0.0),
                llm_call_count=int(llm_call_count or 0),
                token_usage=token_usage,
            )
            _relational_db.record_evidence_events(
                config.get("configurable", {}).get("thread_id", ""), evidence_events
            )
            _relational_db.record_action_events(
                config.get("configurable", {}).get("thread_id", ""),
                action_events,
            )
            _relational_db.record_mode_transition_events(
                config.get("configurable", {}).get("thread_id", ""),
                list(state.get("mode_transition_events", []) or []),
            )
            _relational_db.record_plan_action_outcomes(
                str(state.get("plan_id", "") or ""),
                action_events,
                decay_lambda=cfg.quality_decay_lambda,
            )
            _relational_db.record_plan_action_alignment_events(
                config.get("configurable", {}).get("thread_id", ""),
                str(state.get("plan_id", "") or ""),
                action_events,
            )
            _relational_db.record_execution_plan_outcome(
                str(state.get("plan_id", "") or ""),
                test_verdict,
                config.get("configurable", {}).get("thread_id", ""),
                direct_quality_threshold=cfg.direct_quality_threshold,
            )
        except Exception:
            logger.exception(
                "Failed to persist execution run %s",
                config.get("configurable", {}).get("thread_id", ""),
            )

    # 统计统一基于 dd（实际展示步骤）
    pc = sum(1 for s in dd if s.get("status") in ("success", "continue"))
    fc = sum(1 for s in dd if s.get("status") == "fail")
    cc = sum(1 for s in dd if s.get("status") == "continue")
    logger.info(
        "Reporter: exec=%s verdict=%s display_steps=%d steps(success=%d fail=%d continue=%d) duration=%.1fs budget_violation=%d llm_calls=%d tool_call_400=%d tool_call_400_rate=%.4f click=%d exact=%d semantic=%d fuzzy=%d ambiguous=%d rag_q=%d rag_same=%.2f tokens(in=%d out=%d total=%d cached=%d calls=%d) conclusion=%s",
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
        semantic_count,
        fuzzy_count,
        ambiguous_count,
        rag_query_count,
        rag_same_app_ratio,
        int(token_usage.get("input_tokens", 0) or 0),
        int(token_usage.get("output_tokens", 0) or 0),
        int(token_usage.get("total_tokens", 0) or 0),
        int(token_usage.get("cached_input_tokens", 0) or 0),
        int(token_usage.get("llm_calls", 0) or 0),
        str(conclusion)[:120],
    )
    # 执行模式状态机观测（Plan §2 P2 验收用）：不放在长行里，单独打印保证清晰度。
    logger.info(
        "Reporter[mode]: execution_mode=%s lifecycle_state=%s plan_id=%s "
        "plan_trust=%s mode_selection_reason=%s mode_transition_events=%d",
        _exec_mode,
        _lifecycle,
        _plan_id or "-",
        _plan_trust or "-",
        _mode_reason or "-",
        len(_mode_transitions),
    )
    # 本地逐轮 trace 落盘（离线可观测；config 可关；绝不影响主流程）
    if getattr(cfg, "write_run_trace", True):
        try:
            from agents.run_trace import (
                build_run_trace,
                write_run_trace,
            )

            _trace = build_run_trace(
                run_id=config.get("configurable", {}).get("thread_id", ""),
                user_request=state.get("user_request", ""),
                app_package=state.get("app_package", ""),
                app_name=state.get("app_name", ""),
                execution_status=execution_status,
                test_verdict=test_verdict,
                duration_seconds=duration,
                tool_log=_tool_log,
                verification_results=verification_results,
                token_usage=token_usage,
                metrics={
                    "llm_call_count": llm_call_count,
                    "tool_call_400_count": tool_call_400_count,
                    "tool_call_400_rate": tool_call_400_rate,
                    "click_count": click_count,
                    "exact_click_count": exact_count,
                    "semantic_click_count": semantic_count,
                    "fuzzy_click_count": fuzzy_count,
                    "ambiguous_count": ambiguous_count,
                    "rag_query_count": rag_query_count,
                    "rag_same_app_ratio": rag_same_app_ratio,
                    "rag_empty_hit_rate": rag_empty_hit_rate,
                    "evidence_event_counts": evidence_event_counts,
                },
                execution_mode=_exec_mode,
                lifecycle_state=_lifecycle,
                plan_id=_plan_id,
                plan_trust=_plan_trust,
                mode_selection_reason=_mode_reason,
                mode_transition_events=_mode_transitions,
            )
            _trace_path = write_run_trace(_trace)
            if _trace_path:
                logger.info("Run trace written: %s", _trace_path)
        except Exception as exc:
            logger.warning("run trace skipped: %s", exc)

    return Command(
        update={
            "conclusion": str(conclusion),
            "status": status,
            "execution_status": execution_status,
            "test_verdict": test_verdict,
            "verification_results": verification_results,
            "tool_call_400_rate": tool_call_400_rate,
            "token_usage": token_usage,
        }
    )


def plan_review_node(state: TestState, config: RunnableConfig) -> Command:
    """Pause to let user confirm (and optionally edit) the generated goal before Agent runs."""
    # 用户手动停止：在调 interrupt() 之前拦截，避免「点了停止却弹出计划确认」
    # 命中时直接 goto reporter，与其他节点的 stop 收敛路径一致。
    _ctx = get_tool_context()
    _stop_cmd = _stop_or_continue(state, _ctx)
    if _stop_cmd is not None:
        return _stop_cmd
    goal = state.get("goal_description", {})
    verification_contract = state.get("verification_contract", {})
    from langgraph.types import interrupt

    result = interrupt(
        {
            "type": "plan_review",
            "plan": goal,
            "goal": goal.get("goal", ""),
            "pages": goal.get("target_pages", []),
            "verification": goal.get("verification", []),
            "user_request": state.get("user_request", ""),
            "verification_contract": verification_contract,
        }
    )
    # If user edited the goal, use the edited version
    if isinstance(result, dict) and result.get("action") == "confirm":
        # 在原计划基础上覆盖用户编辑的字段，保留 execution_plan 等其余字段
        edited = dict(goal)
        edited["goal"] = result.get("goal", goal.get("goal", ""))
        edited["target_pages"] = result.get(
            "target_pages", goal.get("target_pages", [])
        )
        edited["verification"] = result.get(
            "verification", goal.get("verification", [])
        )
        edited["hints"] = result.get("hints", goal.get("hints", []))
        logger.info("Plan review: user edited goal")
        from agents.verification import (
            build_verification_contract,
            validate_contract_spans,
        )

        reviewed_contract = result.get("verification_contract")
        contract = (
            reviewed_contract
            if isinstance(reviewed_contract, dict)
            else build_verification_contract(edited, state.get("user_request", ""))
        )
        contract = dict(contract)
        span_validation = validate_contract_spans(contract)
        if span_validation["valid"]:
            contract["status"] = "approved"
            contract["span_validation_error"] = None
        else:
            contract["status"] = "contract_pending_review"
            contract["span_validation_error"] = span_validation
            logger.warning(
                "Plan review: span validation failed, keeping contract pending: %s",
                span_validation,
            )
        return Command(
            update={"goal_description": edited, "verification_contract": contract}
        )
    if result == "cancel" or (
        isinstance(result, dict) and result.get("action") == "cancel"
    ):
        return Command(update={"status": "cancelled"})
    if verification_contract:
        from agents.verification import validate_contract_spans

        approved = dict(verification_contract)
        span_validation = validate_contract_spans(approved)
        if span_validation["valid"]:
            approved["status"] = "approved"
            approved["span_validation_error"] = None
        else:
            approved["status"] = "contract_pending_review"
            approved["span_validation_error"] = span_validation
            logger.warning(
                "Plan review: span validation failed on simple confirm: %s",
                span_validation,
            )
        return Command(update={"verification_contract": approved})
    return Command(update={})


def _get_app_version(ctx: Any, app_package: str) -> str:
    """Query the device for the installed app version, cached per run."""
    if not ctx or not app_package:
        return ""
    cache_key = f"_app_version_{app_package}"
    cached = getattr(ctx, cache_key, None)
    if cached is not None:
        return cached
    version = ""
    try:
        if ctx.device and hasattr(ctx.device, "shell"):
            output = ctx.device.shell(["dumpsys", "package", app_package])
            text = str(output or "")
            for line in text.splitlines():
                if "versionName=" in line:
                    parts = line.split("versionName=")
                    if len(parts) > 1:
                        candidate = parts[1].split()[0].strip()
                        if candidate:
                            version = candidate
                            break
    except Exception:
        version = ""
    setattr(ctx, cache_key, version)
    return version


def _action_postcond_rate(action: dict[str, Any]) -> float:
    """postcondition 通过率 = pass / attempt（attempt 为 0 时视为 0，未对齐）。"""
    try:
        attempts = int(action.get("attempt_count", 0) or 0)
        if attempts <= 0:
            return 0.0
        passes = int(action.get("postcondition_pass_count", 0) or 0)
        return passes / attempts
    except Exception:
        return 0.0


def mode_selection_node(state: TestState, config: RunnableConfig) -> Command:
    """Select a one-way v2 execution mode from an approved task plan."""
    from agents.verification import validate_contract_spans

    cfg: TestConfig = (
        config.get("configurable", {}).get("test_config")
        if isinstance(config, dict) and config.get("configurable")
        else TestConfig()
    )

    contract = state.get("verification_contract", {})
    if not isinstance(contract, dict) or contract.get("status") != "approved":
        return Command(
            update={
                "status": "fail",
                "conclusion": "CONTRACT_REVIEW_REQUIRED: verification contract is not approved",
                "execution_mode": "explore",
                "lifecycle_state": "Terminal",
                "mode_selection_reason": "verification_contract_not_approved",
                "selected_plan_actions": [],
            },
            goto="reporter",
        )

    span_validation = validate_contract_spans(contract)
    if not span_validation["valid"]:
        contract = dict(contract)
        contract["span_validation_error"] = span_validation
        return Command(
            update={
                "status": "fail",
                "conclusion": (
                    "CONTRACT_REVIEW_REQUIRED: verification contract span validation failed: "
                    f"gaps={len(span_validation.get('gaps', []))} "
                    f"overlaps={len(span_validation.get('overlaps', []))}"
                ),
                "execution_mode": "explore",
                "lifecycle_state": "Terminal",
                "mode_selection_reason": "verification_contract_span_invalid",
                "verification_contract": contract,
                "selected_plan_actions": [],
            },
            goto="reporter",
        )

    from agents.plan_extractor import (
        environment_fingerprint,
        _task_signature_from_goal,
    )
    from agents.rag_context import retrieve_knowledge

    environment_key = ""
    actual_environment_key = ""
    try:
        ctx = get_tool_context()
        current_app = ctx.device.current_app() if ctx and ctx.device else {}
        screen_size = ctx.screen_size if ctx else (0, 0)
        screen_profile = "x".join(str(value) for value in screen_size)
        app_version = _get_app_version(ctx, state.get("app_package", ""))
        fixture_fingerprint = cfg.fixture_profile
        # 语义说明：environment_key 由「执行时前台 app 的 package/activity」+「目标 app 的
        # app_version/fixture」混合组成。find_matching_execution_plan 已按 app_package 过滤，
        # package/activity 是弱信号不会误判；严格对齐 Plan 4.1 时应改用「目标 App 启动后首屏
        # activity」而非当前前台 app，但当前侧与沉淀侧口径一致，匹配自洽。
        page_payload = {
            "package": (current_app or {}).get("package", ""),
            "activity": (current_app or {}).get("activity", ""),
            "app_version": app_version,
            "fixture_fingerprint": fixture_fingerprint,
        }
        actual_environment_key = environment_fingerprint(page_payload, screen_profile)
        # For matching, use the same key (candidate plans store their own page facts).
        environment_key = actual_environment_key
    except Exception:
        environment_key = ""
        actual_environment_key = ""

    task_signature = _task_signature_from_goal(
        state.get("user_request", ""),
        state.get("goal_description", {}),
        contract,
        [],
    )

    plan_result = retrieve_knowledge(
        app_package=state.get("app_package", ""),
        purpose="task_plan",
        query=state.get("user_request", ""),
        verification_fingerprint=task_signature["verification_fingerprint"],
        environment_key=environment_key,
        task_signature=task_signature,
    )
    plan = plan_result["items"][0] if plan_result["items"] else None
    env_score = float(plan_result.get("environment_compatibility_score", 0.0) or 0.0)
    env_reasons = list(plan_result.get("environment_compatibility_reasons") or [])

    # Fallback: if the retrieval backend did not attach a score, compute it here.
    if plan and env_score == 0.0 and not env_reasons:
        from agents.plan_extractor import environment_compatibility_score

        env_score_result = environment_compatibility_score(
            plan.get("environment_key", ""), environment_key
        )
        env_score = float(env_score_result.get("score", 0.0) or 0.0)
        env_reasons = list(env_score_result.get("reasons") or [])

    base_update = {
        "actual_environment_key": actual_environment_key,
        "environment_compatibility_score": env_score,
        "environment_compatibility_reasons": env_reasons,
    }

    if not plan:
        return Command(
            update={
                **base_update,
                "execution_mode": "explore",
                "lifecycle_state": "Bootstrapping",
                "mode_selection_reason": "no_matching_plan",
                "selected_plan_actions": [],
            }
        )

    actions = list(plan.get("actions", []) or [])
    all_direct_eligible = bool(actions) and all(
        action.get("execution_eligibility") == "direct_eligible"
        for action in actions
        if isinstance(action, dict)
    )

    # Plan 4.1 direct 准入闸门（码级强制，除人工批准外还需满足）：
    # (a) 同一兼容键下累计成功运行 >= direct_min_runs（连续 N 次 guided 成功近似）；
    # (b) 每个动作 postcondition 通过率 >= direct_postcond_rate_threshold（全动作对齐）；
    # (c) plan 平均质量 >= direct_quality_threshold。
    _success_runs = int(plan.get("success_count", 0) or 0)
    _min_runs_ok = _success_runs >= max(1, int(cfg.direct_min_runs))
    _actions_aligned = bool(actions) and all(
        _action_postcond_rate(action) >= float(cfg.direct_postcond_rate_threshold)
        for action in actions
        if isinstance(action, dict)
    )
    _plan_quality = float(plan.get("quality_score", 0.0) or 0.0)
    _quality_ok = _plan_quality >= float(cfg.direct_quality_threshold)

    direct_ready = (
        bool(plan.get("direct_approved", False))
        and all_direct_eligible
        and env_score == 1.0
        and _min_runs_ok
        and _actions_aligned
        and _quality_ok
    )
    _approved_but_blocked = (
        bool(plan.get("direct_approved", False))
        and all_direct_eligible
        and env_score == 1.0
        and not direct_ready
    )
    if direct_ready:
        return Command(
            update={
                **base_update,
                "execution_mode": "direct",
                "lifecycle_state": "Direct",
                "plan_id": str(plan["plan_id"]),
                "plan_trust": str(plan.get("plan_trust", "") or "candidate"),
                "mode_selection_reason": "direct_approved_full_environment_match",
                "selected_plan_actions": actions,
                "_direct_action_cursor": 0,
            }
        )

    guided_threshold = cfg.environment_guided_threshold
    if env_score >= guided_threshold:
        return Command(
            update={
                **base_update,
                "execution_mode": "guided",
                "lifecycle_state": "Guided",
                "plan_id": str(plan["plan_id"]),
                "plan_trust": str(plan.get("plan_trust", "") or "candidate"),
                "mode_selection_reason": (
                    "guided_from_approved_no_runs"
                    if _approved_but_blocked
                    else (
                        "matching_plan_guided_partial_environment"
                        if env_score < 1.0
                        else "matching_plan_guided"
                    )
                ),
                "selected_plan_actions": actions,
            }
        )

    return Command(
        update={
            **base_update,
            "execution_mode": "explore",
            "lifecycle_state": "Explore",
            "mode_selection_reason": "matching_plan_environment_incompatible",
            "selected_plan_actions": [],
        }
    )


def direct_node(state: TestState, config: RunnableConfig) -> Command:
    """Execute one stable plan action without an LLM decision."""
    from agents.budget import _calc_mode_phase_budget
    from tools.results import parse_status

    cfg: TestConfig = (
        config.get("configurable", {}).get("test_config")
        if isinstance(config, dict) and config.get("configurable")
        else TestConfig()
    )

    _direct_plan_id = str(state.get("plan_id", "") or "")
    _direct_phase_budget = _calc_mode_phase_budget(state, "direct")

    if int(state.get("_direct_downgrade_count", 0) or 0) >= 1:
        transitions = list(state.get("mode_transition_events", []) or [])
        transitions.append(
            {
                "from": "direct",
                "to": "guided",
                "reason": "direct_reentry_guard",
                "step_index": int(state.get("_direct_action_cursor", 0) or 0),
                "plan_id": _direct_plan_id,
                "action_id": "",
                "phase_budget": _direct_phase_budget,
            }
        )
        return Command(
            update={
                "execution_mode": "guided",
                "lifecycle_state": "Guided",
                "mode_selection_reason": "direct_reentry_guard",
                "mode_transition_events": transitions,
            }
        )

    cursor = int(state.get("_direct_action_cursor", 0) or 0)
    actions = list(state.get("selected_plan_actions", []) or [])
    if cursor >= len(actions) or cursor >= _calc_mode_phase_budget(state, "direct"):
        transitions = list(state.get("mode_transition_events", []) or [])
        transitions.append(
            {
                "from": "direct",
                "to": "guided",
                "reason": (
                    "direct_phase_budget_exhausted"
                    if cursor < len(actions)
                    else "direct_actions_exhausted"
                ),
                "step_index": cursor,
                "plan_id": _direct_plan_id,
                "action_id": "",
                "phase_budget": _direct_phase_budget,
            }
        )
        return Command(
            update={
                "execution_mode": "guided",
                "lifecycle_state": "Guided",
                "mode_selection_reason": transitions[-1]["reason"],
                "_direct_downgrade_count": 1,
                "mode_transition_events": transitions,
            }
        )
    action = actions[cursor]
    ctx = get_tool_context()
    tool_input: dict[str, Any] = {}
    before_app: dict[str, Any] = {}
    output = ""
    try:
        tool_input = json.loads(action.get("tool_input_json") or "{}")
        precondition = json.loads(action.get("precondition_json") or "{}")
        before_app = ctx.device.current_app() if ctx and ctx.device else {}
        if (
            precondition.get("package")
            and precondition.get("package") != before_app.get("package")
        ) or (
            precondition.get("activity")
            and precondition.get("activity") != before_app.get("activity")
        ):
            raise RuntimeError("direct precondition mismatch")
        tool = next(
            tool for tool in AGENT_TOOLS if tool.name == action.get("tool_name")
        )
        output = str(tool.invoke(tool_input))
        postcondition = json.loads(action.get("postcondition_json") or "{}")
        after_app = ctx.device.current_app() if ctx and ctx.device else {}
        if (
            postcondition.get("package")
            and postcondition.get("package") != after_app.get("package")
        ) or (
            postcondition.get("activity")
            and postcondition.get("activity") != after_app.get("activity")
        ):
            raise RuntimeError("direct postcondition mismatch")
    except Exception as exc:
        output = f"ERROR: {exc}"

    parsed_status = parse_status(output)
    if parsed_status:
        status = parsed_status
    else:
        status = "OK" if output.startswith(("OK", "PASS")) else "ERROR"

    after_app = ctx.device.current_app() if ctx and ctx.device else {}
    if ctx:
        screen_profile = ""
        try:
            screen_profile = "x".join(str(value) for value in ctx.screen_size)
        except Exception:
            pass
        ctx._action_events.append(
            {
                "action_index": cursor,
                "tool_name": str(action.get("tool_name", "") or ""),
                "tool_input": tool_input if "tool_input" in locals() else {},
                "intent_text": "",
                "screenshot_path": "",
                "resolved_locator": json.loads(action.get("locator_json") or "{}"),
                "page_before": {
                    **dict(before_app or {}),
                    "screen_profile": screen_profile,
                },
                "page_after": {
                    **dict(after_app or {}),
                    "screen_profile": screen_profile,
                },
                "status": status,
                "execution_mode": "direct",
            }
        )
    if status not in {"OK", "PASS", "YES"}:
        transitions = list(state.get("mode_transition_events", []) or [])
        reason = "direct_action_failed"
        if "precondition mismatch" in output:
            reason = "direct_precondition_failed"
        elif "postcondition mismatch" in output:
            reason = "direct_postcondition_failed"
        elif status == "TIMEOUT":
            reason = "direct_action_timeout"
        elif status == "NOT_FOUND":
            reason = "direct_action_not_found"
        transitions.append(
            {
                "from": "direct",
                "to": "guided",
                "reason": reason,
                "step_index": cursor,
                "plan_id": _direct_plan_id,
                "action_id": str(action.get("action_id", "") or action.get("action_index", "") or ""),
                "phase_budget": _direct_phase_budget,
            }
        )
        return Command(
            update={
                "execution_mode": "guided",
                "lifecycle_state": "Guided",
                "mode_selection_reason": reason,
                "_direct_downgrade_count": 1,
                "mode_transition_events": transitions,
            }
        )
    return Command(update={"_direct_action_cursor": cursor + 1})


def mode_transition_node(state: TestState, config: RunnableConfig) -> Command:
    """Perform the only currently supported degradation: guided to explore."""
    if int(state.get("_guided_downgrade_count", 0) or 0) >= 1:
        return Command(
            update={
                "execution_mode": "explore",
                "lifecycle_state": "Explore",
                "mode_selection_reason": "guided_reentry_guard",
                "selected_plan_actions": [],
            }
        )

    history = list(state.get("step_history", []) or [])
    last_step = history[-1] if history else {}
    from agents.budget import _calc_mode_phase_budget

    guided_steps = sum(
        1
        for step in history
        if isinstance(step, dict) and step.get("execution_mode") == "guided"
    )
    reason = (
        "guided_loop_detected"
        if bool(last_step.get("loop_detected", False))
        else (
            "guided_phase_budget_exhausted"
            if guided_steps >= _calc_mode_phase_budget(state, "guided")
            else "guided_action_failed"
        )
    )
    transitions = list(state.get("mode_transition_events", []) or [])
    transitions.append(
        {
            "from": "guided",
            "to": "explore",
            "reason": reason,
            "step_index": last_step.get("index"),
            "plan_id": str(state.get("plan_id", "") or ""),
            "action_id": str(last_step.get("action_id", "") or ""),
            "phase_budget": _calc_mode_phase_budget(state, "guided"),
        }
    )
    return Command(
        update={
            "execution_mode": "explore",
            "lifecycle_state": "Explore",
            "mode_selection_reason": reason,
            "selected_plan_actions": [],
            "mode_transition_events": transitions,
            "_guided_downgrade_count": 1,
            "status": "",
            "conclusion": "",
            "messages": [],
        }
    )


def evaluator_node(state: TestState, config: RunnableConfig) -> Command:
    """Materialize the current-run typed-evidence verdict without invoking an LLM."""
    contract = state.get("verification_contract", {})
    try:
        ctx = get_tool_context()
    except Exception:
        ctx = None
    evaluation = evaluate_verification(
        contract if isinstance(contract, dict) else {},
        list(getattr(ctx, "_evidence_events", []) or []) if ctx else [],
    )
    if ctx:
        ctx._clause_state = evaluation
    return Command(update={"clause_state": evaluation})


# ═══ ROUTING ═══


_STALE_SCREEN_TOOLS = ("get_screen_info",)
_STALE_SCREEN_PLACEHOLDER_PREFIX = "[历史页面已折叠]"


def _summarize_stale_screen_dumps(um: list[Any]) -> None:
    """O2：把历史消息中除最新一次外的 get_screen_info 大输出折叠为占位符，
    抑制上下文/token 膨胀。最新一份保留全量；折叠时保留 tool_call_id 以维持
    与 AIMessage tool_calls 的配对（OpenAI 协议要求）。就地替换，绝不抛异常。"""
    try:
        idxs = [
            i for i, m in enumerate(um) if getattr(m, "name", "") in _STALE_SCREEN_TOOLS
        ]
        for i in idxs[:-1]:  # 保留最后一份全量
            m = um[i]
            content = getattr(m, "content", "")
            if not isinstance(content, str) or content.startswith(
                _STALE_SCREEN_PLACEHOLDER_PREFIX
            ):
                continue
            first_line = content.split("\n", 1)[0]
            um[i] = ToolMessage(
                content=(
                    f"{_STALE_SCREEN_PLACEHOLDER_PREFIX} {first_line}"
                    "（页面可能已变化，如需当前状态请重新调用 get_screen_info）"
                ),
                name=getattr(m, "name", None),
                tool_call_id=getattr(m, "tool_call_id", ""),
            )
    except Exception:
        pass


def _prune_messages(
    um: list[Any], max_len: int = 14, summarize_stale_screens: bool = True
) -> None:
    """Phase 1.4: 裁剪消息列表，保留 system prompt + Goal 上下文 + 最近消息。
    T5: max_len 16→14，仅微调跨轮累积的 AIMessage/ToolMessage 体积。
    O2: summarize_stale_screens=True 时先折叠历史 get_screen_info 大输出。"""
    if summarize_stale_screens:
        _summarize_stale_screen_dumps(um)
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


def _goal_is_usable(goal: dict) -> bool:
    """A planner goal is usable only when it has a plain-text goal and a
    non-empty verification list — otherwise the verification contract would be
    empty and the run can never reach a terminal verdict."""
    if not isinstance(goal, dict):
        return False
    goal_text = goal.get("goal", "")
    if not isinstance(goal_text, str) or goal_text.strip().startswith("{"):
        return False
    verification = goal.get("verification", [])
    return isinstance(verification, list) and any(
        str(v or "").strip() for v in verification
    )


def _parse_goal(text: str) -> dict:
    m = re.search(r"\{[\s\S]*\}", text)
    fallback = {
        "goal": text.strip()[:200],
        "target_pages": [],
        "verification": [],
        "hints": [],
        "parameter_slots": [],
    }
    if not m:
        return fallback
    try:
        r = json.loads(m.group(0))
        if isinstance(r, dict):
            # Normalize parameter_slots to plain dicts for downstream compatibility.
            slots = r.get("parameter_slots", [])
            normalized_slots: list[dict[str, Any]] = []
            for slot in slots or []:
                if isinstance(slot, dict):
                    normalized_slots.append(
                        {
                            "name": str(slot.get("name", "") or ""),
                            "type": str(slot.get("type", "") or ""),
                            "unit": str(slot.get("unit", "") or ""),
                            "value": slot.get("value"),
                            "original": str(slot.get("original", "") or ""),
                            "source": str(slot.get("source", "") or "user_request"),
                        }
                    )
            r["parameter_slots"] = normalized_slots
            return r
    except json.JSONDecodeError:
        pass
    return fallback

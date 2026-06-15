# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Any, Annotated

from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import interrupt, Command
from langgraph.config import get_stream_writer
from langchain_core.runnables import RunnableConfig

from config import TestConfig
from llm.clients import create_llm_client, supports_structured_output, LLMFatalError, _call_with_retry, _is_rate_limit_error, _default_should_retry
from agents.state import TestState, TestPlanOutput
from tools import (
    PLANNER_TOOLS, EXECUTOR_TOOLS, REVIEWER_TOOLS,
    get_tool_context,
    _executor_tools_summary, _reviewer_tools_summary,
)

logger = logging.getLogger(__name__)

# ── 重试参数 ──
_MAX_RETRIES = 3
_RETRY_WAIT_SECONDS = 5


def _should_retry_llm(provider: str, exc: Exception) -> bool:
    """graph.py 中根据 provider 判断 LLM 调用是否可重试。

    智谱：仅 429 限流可重试；其他 provider：默认策略（大部分可重试，欠费/认证除外）。
    """
    if provider == "zhipu":
        return _is_rate_limit_error(exc)
    return _default_should_retry(exc)


def _call_llm_with_retry(provider: str, fn, *args, **kwargs):
    """graph.py 中的 LLM 调用重试：根据 provider 判断是否可重试。

    Returns: fn 的返回值，或 None（所有重试耗尽后降级）
    Raises:  LLMFatalError: 不可重试的错误
    """
    return _call_with_retry(lambda exc: _should_retry_llm(provider, exc), fn, *args, **kwargs)

# ── 关系型数据库（模块级单例，build_graph 时注入）──
_relational_db = None


def set_relational_db(db) -> None:
    global _relational_db
    _relational_db = db


# ── LangSmith 追踪（可选，设置 LANGSMITH_API_KEY 后自动启用）──
if os.environ.get("LANGSMITH_API_KEY"):
    os.environ.setdefault("LANGSMITH_TRACING", "true")

# ═══════════════════════════════════════════
#  Prompt 加载
# ═══════════════════════════════════════════

def _load_prompt(name: str) -> str:
    """加载 prompts 目录下的提示词文件，基于当前文件所在目录解析。"""
    # 基于当前文件 (agents/graph.py) 所在目录解析
    _dir = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(_dir, "prompts", name)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        logger.warning("Failed to load prompt: %s", path)
        return ""

PLANNER_SYSTEM = _load_prompt("planner.txt")
EXECUTOR_SYSTEM = _load_prompt("executor.txt")
REVIEWER_SYSTEM = _load_prompt("reviewer.txt")

PLANNER_TEMPLATE = ChatPromptTemplate.from_messages([
    SystemMessage(content=PLANNER_SYSTEM),
    ("user", """Create a test plan for:
Request: {user_request}
Target app: {app_name} ({app_package})

{rag_context}"""),
])

EXECUTOR_TEMPLATE = ChatPromptTemplate.from_messages([
    SystemMessage(content=EXECUTOR_SYSTEM),
    MessagesPlaceholder("recent_history", optional=True),
    ("user", """\u8bf7\u6267\u884c\u4ee5\u4e0b\u6b65\u9aa4:\n\n\u6b65\u9aa4 {step_index}/{total_steps}: {intent}\n\u64cd\u4f5c\u7c7b\u578b: {action_type}\n\u64cd\u4f5c\u76ee\u6807: {target}\n\u5907\u9009\u76ee\u6807: {alternatives}\n\u9884\u671f\u7ed3\u679c: {expected}\n\n\u8bf7\u8c03\u7528\u5bf9\u5e94\u5de5\u5177\u6267\u884c\u8fd9\u4e00\u6b65\u9aa4, \u8fd4\u56de\u7b80\u6d01\u7684 OK/FAIL \u7ed3\u679c."""),
])

REVIEWER_TEMPLATE = ChatPromptTemplate.from_messages([
    SystemMessage(content=REVIEWER_SYSTEM),
    MessagesPlaceholder("recent_history", optional=True),
    ("user", """\u8bf7\u5ba1\u67e5\u5f53\u524d\u6b65\u9aa4\u7684\u6267\u884c\u7ed3\u679c\uff1a\n\n\u6b65\u9aa4 {step_index}/{total_steps}: {intent}\n\u64cd\u4f5c\u7c7b\u578b: {action_type}\n\u76ee\u6807: {target}\n\u64cd\u4f5c: {last_action}\n\u7ed3\u679c: {last_observation}\n\u9884\u671f: {expected}\n\u5df2\u91cd\u8bd5: {retry_count} \u6b21\n\n\u8bf7\u5224\u65ad\u9875\u9762\u5065\u5eb7\u72b6\u6001, \u7136\u540e\u7ed9\u51fa\u51b3\u7b56 (continue/retry/skip/abort/ask_human/done).\n\u5982\u679c\u8fd9\u662f\u6700\u540e\u4e00\u4e2a\u6b65\u9aa4\u4e14\u6267\u884c\u6210\u529f, \u51b3\u7b56\u4e3a done."""),
])

REPORTER_TEMPLATE = ChatPromptTemplate.from_messages([
    SystemMessage(content="\u4f60\u662f\u6d4b\u8bd5\u62a5\u544a\u751f\u6210\u4e13\u5bb6\u3002"),
    ("user", """\u6d4b\u8bd5\u9700\u6c42: {user_request}\n\u8ba1\u5212\u6b65\u9aa4\u6570: {total_steps}\n\u6210\u529f: {pass_count}, \u5931\u8d25: {fail_count}\n\n\u6b65\u9aa4\u8bb0\u5f55:\n{step_records}\n\n\u8bf7\u8f93\u51fa: PASS \u6216 FAIL, \u4ee5\u53ca\u7b80\u8981\u539f\u56e0."""),
])


# ═══════════════════════════════════════════
#  ToolNode 驱动的 Agent 子图
# ═══════════════════════════════════════════

from typing import TypedDict
from langgraph.graph.message import add_messages


class _AgentSubState(TypedDict):
    """ToolNode 子图的内部状态（模块级别，避免闭包类型解析问题）。"""
    messages: Annotated[list, add_messages]
    _turn_count: int


def _run_agent_with_tools(
    messages: list[Any],
    tools: list[Any],
    provider: str,
    model: str,
    api_key: str,
    base_url: str | None,
    max_turns: int = 10,
    temperature: float = 0.1,
) -> str:
    """用 LangGraph ToolNode + tools_condition 驱动工具调用循环。"""

    # 创建 LLM client
    if provider == "zhipu":
        from zhipuai import ZhipuAI
        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        _zhipu_client = ZhipuAI(max_retries=0, **kwargs)
        _zhipu_model = model
        _zhipu_temp = temperature
        _zhipu_tool_schemas = _build_openai_tool_schemas(tools)

        def _call_zhipu(state: _AgentSubState) -> dict:
            msgs = _convert_messages_to_zhipu(state["messages"])
            resp = _call_llm_with_retry(
                "zhipu", _zhipu_client.chat.completions.create,
                model=_zhipu_model, messages=msgs, tools=_zhipu_tool_schemas,
                tool_choice="auto", temperature=_zhipu_temp,
            )
            if resp is None:
                return {"messages": [AIMessage(content="LLM 调用失败，请稍后重试")]}
            msg = resp.choices[0].message
            content = msg.content or ""
            tool_calls = []
            for tc in (msg.tool_calls or []):
                tool_calls.append({
                    "id": tc.id, "name": tc.function.name,
                    "args": json.loads(tc.function.arguments) if isinstance(tc.function.arguments, str) else (tc.function.arguments or {}),
                    "type": "tool_call",
                })
            ai_msg = AIMessage(content=content, tool_calls=tool_calls if tool_calls else None)
            return {"messages": [ai_msg]}

        llm_node = _call_zhipu
    else:
        from langchain_openai import ChatOpenAI
        _llm = ChatOpenAI(model=model, temperature=temperature, api_key=api_key, base_url=base_url)
        _llm_with_tools = _llm.bind_tools(tools)

        def _call_openai(state: _AgentSubState) -> dict:
            response = _call_llm_with_retry("openai", _llm_with_tools.invoke, state["messages"])
            if response is None:
                return {"messages": [AIMessage(content="LLM 调用失败，请稍后重试")]}
            return {"messages": [response]}

        llm_node = _call_openai

    # 构建子图（带 turn 计数器）
    _max_cycles = max(1, max_turns)

    def _inc_turn(state: _AgentSubState) -> dict:
        return {"_turn_count": state.get("_turn_count", 0) + 1, "messages": []}

    def _limited_tools_condition(state: _AgentSubState) -> str:
        from langgraph.prebuilt import tools_condition as _tc
        route = _tc(state)
        if route == "tools" and state.get("_turn_count", 0) >= _max_cycles:
            return END
        return route

    sub = StateGraph(_AgentSubState)
    sub.add_node("llm", llm_node)
    sub.add_node("tools", ToolNode(tools))
    sub.add_node("inc_turn", _inc_turn)
    sub.add_edge(START, "llm")
    sub.add_conditional_edges("llm", _limited_tools_condition, {"tools": "inc_turn", END: END})
    sub.add_edge("inc_turn", "tools")
    sub.add_edge("tools", "llm")

    compiled = sub.compile()

    result = compiled.invoke({"messages": list(messages), "_turn_count": 0})
    final_messages = result["messages"]
    # 从后往前找第一个有实际内容的 msg（tool_calls 截断时最后一条可能 content=""）
    for msg in reversed(final_messages):
        content = getattr(msg, "content", None)
        if content:
            return str(content)
    return "未生成有效结果"


def _build_openai_tool_schemas(tools: list[Any]) -> list[dict[str, Any]]:
    """仅 Zhipu provider 需要手动构建 tool schemas。"""
    schemas: list[dict[str, Any]] = []
    for t in tools:
        props = {}
        for name, meta in (getattr(t, "args", {}) or {}).items():
            prop: dict[str, Any] = {"type": meta.get("type", "string")}
            if meta.get("description"):
                prop["description"] = meta["description"]
            props[name] = prop
        schemas.append({
            "type": "function",
            "function": {
                "name": t.name,
                "description": getattr(t, "description", "") or "",
                "parameters": {"type": "object", "properties": props},
            },
        })
    return schemas


def _convert_messages_to_zhipu(messages: list[Any]) -> list[dict[str, Any]]:
    """将 LangChain messages 转为 Zhipu SDK 格式。"""
    result = []
    for m in messages:
        role = getattr(m, "type", None) or getattr(m, "role", None) or "user"
        if role in ("ai", "assistant"):
            role = "assistant"
        elif role == "human":
            role = "user"
        entry: dict[str, Any] = {"role": role, "content": getattr(m, "content", "") or ""}
        tool_calls = getattr(m, "tool_calls", None)
        if tool_calls:
            entry["tool_calls"] = [{
                "id": tc.get("id", ""),
                "type": "function",
                "function": {"name": tc["name"], "arguments": json.dumps(tc["args"], ensure_ascii=False)},
            } for tc in tool_calls]
        if role == "tool":
            entry["tool_call_id"] = getattr(m, "tool_call_id", "")
        result.append(entry)
    return result


# ═══════════════════════════════════════════
#  摘要记忆
# ═══════════════════════════════════════════

_SUMMARY_CACHE: dict[str, str] = {}
MAX_RECENT_STEPS = 3
SUMMARY_THRESHOLD = 8


def _summarize_history(state: TestState, role: str) -> list[Any]:
    history = state.get("step_history", [])
    if len(history) <= SUMMARY_THRESHOLD:
        return _format_recent_steps(history, -MAX_RECENT_STEPS)

    cache_key = f"{state.get('user_request', '')}_{len(history)}"
    if cache_key not in _SUMMARY_CACHE:
        _SUMMARY_CACHE[cache_key] = _generate_summary(state)

    return [
        AIMessage(content=f"[历史摘要] {_SUMMARY_CACHE[cache_key]}"),
    ] + _format_recent_steps(history, -MAX_RECENT_STEPS)


def _generate_summary(state: TestState) -> str:
    history = state.get("step_history", [])
    older = history[:-MAX_RECENT_STEPS]
    if not older:
        return ""
    lines = [f"  {'OK' if s.get('status') == 'success' else 'FAIL'} 步骤{s.get('index', '?')}: {s.get('intent', '')[:60]}" for s in older]
    return "已完成的步骤:\n" + "\n".join(lines)


def _format_recent_steps(history: list[dict[str, Any]], tail: int) -> list[Any]:
    recent = history[tail:] if tail < 0 else history[:tail]
    if not recent:
        return []
    text = "最近已完成的步骤:\n" + "\n".join(
        f"  步骤{s.get('index', '?')}: [{s.get('status', '?')}] {s.get('intent', '')[:80]}"
        for s in recent
    )
    return [AIMessage(content=text)]


# ═══════════════════════════════════════════
#  Graph Nodes
# ═══════════════════════════════════════════

def _get_llm_config(config: TestConfig, role: str) -> dict[str, Any]:
    return config.agent_config(role)


def _get_rag_context() -> str:
    ctx = get_tool_context()
    if ctx.knowledge_base:
        return ctx.knowledge_base.load_memory_context("页面结构 导航路径 操作经验", "")
    return ""


def _emit_step_event(state: TestState, event_type: str, content: Any) -> None:
    """通过 stream_writer 推送事件。"""
    try:
        writer = get_stream_writer()
        writer((event_type, {"step": state.get("current_step_index", 0) + 1, "content": content}))
    except Exception:
        pass


def planner_node(state: TestState, config: RunnableConfig) -> Command:
    """Planner: 两阶段 — Stage1 ToolNode 收集屏幕/知识信息，Stage2 structured_output 生成 JSON 计划。"""
    cfg: TestConfig = config["configurable"]["test_config"]
    llm = _get_llm_config(cfg, "planner")

    _emit_step_event(state, "status", "Planner 正在收集页面信息...")

    # ── Stage 1: ToolNode 收集屏幕信息 + 知识库 ──
    info_messages = [
        SystemMessage(content=(
            "You are gathering information for a test plan. "
            "Call get_screen_info and query_app_knowledge ONCE each, then STOP. "
            "Do NOT output any plan or JSON — just collect info."
        )),
        HumanMessage(content=(
            f"Collect info for test:\n"
            f"Request: {state.get('user_request', '')}\n"
            f"App: {state.get('app_name', '')} ({state.get('app_package', '')})"
        )),
    ]
    info_text = _run_agent_with_tools(
        info_messages, PLANNER_TOOLS, llm["provider"], llm["model"],
        llm["api_key"], llm["base_url"], max_turns=1,
    )
    logger.info("Planner Stage1 info collected: %d chars", len(info_text))

    # ── Stage 2: 用 structured_output / 严格 prompt 生成 JSON 计划 ──
    _emit_step_event(state, "status", "Planner 正在制定测试计划...")

    rag_context = _get_rag_context()
    plan_messages = PLANNER_TEMPLATE.format_messages(
        user_request=state.get("user_request", ""),
        app_name=state.get("app_name", ""),
        app_package=state.get("app_package", ""),
        rag_context=rag_context,
    )
    # 把 Stage1 收集的信息注入 prompt
    plan_messages.append(
        SystemMessage(content=(
            f"\n=== Current Screen Info (from get_screen_info) ===\n{info_text}\n"
            f"\n=== RAG Context ===\n{rag_context}\n\n"
            "You now have ALL the information you need. "
            "Output the test plan as JSON ONLY. Do NOT call any tools. Do NOT output markdown."
        ))
    )

    result_text = ""
    plan: dict[str, Any] = {}
    use_structured = supports_structured_output(llm["provider"], llm.get("base_url"))
    if use_structured:
        try:
            from langchain_openai import ChatOpenAI
            structured_llm = ChatOpenAI(
                model=llm["model"], temperature=0.1,
                api_key=llm["api_key"], base_url=llm["base_url"],
            ).with_structured_output(TestPlanOutput)
            plan_obj = _call_llm_with_retry(llm["provider"], structured_llm.invoke, plan_messages)
            plan = plan_obj.model_dump() if plan_obj else {}
        except Exception as exc:
            logger.warning("Structured output failed: %s, falling back to direct invoke", exc)
            use_structured = False

    if not use_structured:
        # 直接 invoke（不经过 ToolNode），prompt 已包含所有信息
        try:
            from langchain_openai import ChatOpenAI
            direct_llm = ChatOpenAI(
                model=llm["model"], temperature=0.1,
                api_key=llm["api_key"], base_url=llm["base_url"],
            )
            response = _call_llm_with_retry(llm["provider"], direct_llm.invoke, plan_messages)
            result_text = str(response.content) if response else ""
        except Exception as exc:
            logger.error("Planner direct invoke failed: %s", exc)
            result_text = ""
        plan = _parse_json_block(result_text) or {}

    steps = plan.get("steps", []) if isinstance(plan, dict) else []
    # 兜底：LLM 输出 JSON 数组 [{...},{...}] 而非 {"steps":[...]}
    if not steps and isinstance(plan, list):
        steps = plan
    # 校验步骤字段完整性
    steps = [_normalize_step(s, i + 1) for i, s in enumerate(steps)]
    if not steps and result_text:
        steps = _extract_steps_from_text(result_text, state.get("app_package", ""))
    if not steps:
        steps = [
            {"index": 1, "intent": "启动应用", "action_type": "launch_app",
             "target": state.get("app_package", ""), "alternatives": [], "expected": "进入应用首页"},
        ]

    _save_plan(cfg, plan)
    _SUMMARY_CACHE.clear()

    logger.info("Planner generated %d steps", len(steps))
    return Command(update={
        "test_plan": steps,
        "current_step_index": 0,
        "step_history": [],
        "retry_count": 0,
        "started_at": datetime.now().isoformat(),
        "step_times": [],
    })


def plan_review_node(state: TestState, config: RunnableConfig) -> Command:
    """计划审阅节点：展示 Planner 生成的计划，等待用户确认/编辑/取消。"""
    plan = state.get("test_plan", [])

    # 推送 plan_ready 事件（含完整计划数据，供前端展示编辑）
    _emit_step_event(state, "plan_ready", {"steps": len(plan), "plan": plan})

    logger.info("Plan review: %d steps ready, waiting for user confirmation", len(plan))
    # interrupt 暂停，等待用户确认
    result = interrupt({
        "type": "plan_review",
        "plan": plan,
    })

    # result 可能是: "confirm" | "cancel" | {"plan": [...], "action": "confirm"}
    if result is None or result == "cancel":
        logger.info("Plan review cancelled by user")
        return Command(update={"reviewer_decision": "abort", "status": "cancelled"})

    # 用户可能编辑了计划
    if isinstance(result, dict):
        edited_plan = result.get("plan")
        if edited_plan and isinstance(edited_plan, list):
            logger.info("Plan edited by user: %d steps", len(edited_plan))
            return Command(update={"test_plan": edited_plan, "current_step_index": 0})

    logger.info("Plan confirmed, proceeding to executor")
    return Command(update={"current_step_index": 0})


def executor_node(state: TestState, config: RunnableConfig) -> Command:
    """Executor: 用 ToolNode 子图执行当前步骤。"""
    cfg: TestConfig = config["configurable"]["test_config"]
    llm = _get_llm_config(cfg, "executor")

    plan = state.get("test_plan", [])
    idx = state.get("current_step_index", 0)

    if idx >= len(plan):
        return Command(update={"last_observation": "所有步骤已完成"})

    step = plan[idx]
    recent = _summarize_history(state, "executor")

    messages = EXECUTOR_TEMPLATE.format_messages(
        step_index=idx + 1, total_steps=len(plan),
        intent=step.get("intent", ""),
        action_type=step.get("action_type", ""),
        target=step.get("target", ""),
        alternatives=", ".join(step.get("alternatives", [])) or "无",
        expected=step.get("expected", ""),
        recent_history=recent,
    )
    messages.append(SystemMessage(content=f"Available tools:\n{_executor_tools_summary()}"))

    _emit_step_event(state, "step_start", f"步骤{idx + 1}: {step.get('intent', '')}")

    observation = _run_agent_with_tools(
        messages, EXECUTOR_TOOLS, llm["provider"], llm["model"],
        llm["api_key"], llm["base_url"], max_turns=3,
    )

    logger.info("Executor step %d/%d result=%s", idx + 1, len(plan), observation[:200])
    # 记录步骤开始时间
    step_times = list(state.get("step_times", []))
    step_times.append({"step_index": idx + 1, "started_at": datetime.now().isoformat()})
    return Command(update={
        "last_action": f"{step.get('action_type', '')}: {step.get('target', '')}",
        "last_observation": observation,
        "step_times": step_times,
    })


def reviewer_node(state: TestState, config: RunnableConfig) -> Command:
    """Reviewer: 用 ToolNode 子图检查执行结果。"""
    cfg: TestConfig = config["configurable"]["test_config"]
    llm = _get_llm_config(cfg, "reviewer")

    plan = state.get("test_plan", [])
    idx = state.get("current_step_index", 0)
    step = plan[idx] if idx < len(plan) else {}

    recent = _summarize_history(state, "reviewer")
    messages = REVIEWER_TEMPLATE.format_messages(
        step_index=idx + 1, total_steps=len(plan),
        intent=step.get("intent", "?"),
        action_type=step.get("action_type", ""),
        target=step.get("target", ""),
        last_action=state.get("last_action", ""),
        last_observation=state.get("last_observation", ""),
        expected=step.get("expected", ""),
        retry_count=state.get("retry_count", 0),
        recent_history=recent,
    )
    messages.append(SystemMessage(content=f"Available tools:\n{_reviewer_tools_summary()}"))

    review_text = _run_agent_with_tools(
        messages, REVIEWER_TOOLS, llm["provider"], llm["model"],
        llm["api_key"], llm["base_url"], max_turns=2,
    )

    decision = _parse_decision(review_text)
    human_question = _parse_human_question(review_text)
    # retry 超过 2 次（第 3 次）强制跳过，防止同一步无限循环
    if decision == "retry" and state.get("retry_count", 0) >= 2:
        logger.warning("Retry limit (3) exceeded for step %d, forcing skip", idx + 1)
        decision = "skip"
    new_retry = state.get("retry_count", 0) + 1 if decision == "retry" else 0
    new_idx = idx
    if decision in ("continue", "skip"):
        new_idx = idx + 1
    elif decision == "done":
        new_idx = len(plan)

    new_history = list(state.get("step_history", [])) + [{
        "index": idx + 1, "intent": step.get("intent", ""),
        "action_type": step.get("action_type", ""),
        "target": step.get("target", ""),
        "status": "success" if decision in ("continue", "done") else "fail",
        "observation": state.get("last_observation", ""),
        "anomaly": None,
    }]

    _emit_step_event(state, "step_end", f"步骤{idx + 1}: {decision}")

    # ── 元素身份跟踪 ──
    pending = list(state.get("pending_identities", []))
    step_success = decision in ("continue", "done")
    step_is_click = step.get("action_type", "") in ("click", "navigate_tab")
    observation = state.get("last_observation", "")
    # 从观察中提取 find_element 候选数
    cand_match = re.search(r"(\d+) candidate\(s\)", observation)
    candidates_count = int(cand_match.group(1)) if cand_match else 0

    if step_success and step_is_click and candidates_count > 0:
        target = step.get("target", "")
        # 提取 find_element 返回的具体元素信息
        el_match = re.search(
            r'\[(\w+/)?(\w+)\] "([^"]*)" rid=(\S+) class=(\S+) bounds=\(([^)]+)\)',
            observation
        )
        pending.append({
            "target": target,
            "candidates_count": candidates_count,
            "resource_id": el_match.group(4) if el_match else "",
            "class_name": el_match.group(5) if el_match else "",
            "role": el_match.group(2) if el_match else "",
            "bounds": el_match.group(6) if el_match else "",
            "assert_verified": False,  # Reporter will check
            "level": 2 if candidates_count > 1 else 1,
        })

    logger.info("Reviewer decision=%s step=%d/%d", decision, idx + 1, len(plan))
    return Command(update={
        "step_history": new_history,
        "current_step_index": new_idx,
        "retry_count": new_retry,
        "pending_identities": pending,
        "reviewer_decision": decision,
        "human_question": human_question if decision == "ask_human" else "",
    })


def human_approval_node(state: TestState, config: RunnableConfig) -> Command:
    """人工确认节点。"""
    question = state.get("human_question", "是否继续？")
    decision = interrupt({
        "type": "need_human_approval",
        "question": question,
        "options": ["允许执行", "跳过此步", "终止测试"],
        "step": state.get("current_step_index", 0) + 1,
        "action": state.get("last_action", ""),
    })

    choice = str(decision).strip() if decision else "跳过此步"
    if "允许" in choice or "approve" in choice.lower():
        result = {"reviewer_decision": "continue",
                  "current_step_index": state.get("current_step_index", 0) + 1}
    elif "终止" in choice or "abort" in choice.lower():
        result = {"reviewer_decision": "abort"}
    else:
        result = {"reviewer_decision": "skip",
                  "current_step_index": state.get("current_step_index", 0) + 1}

    logger.info("Human decision=%s", choice)
    return Command(update=result)


# 区域/角色 → 中文
_REGION_CN: dict[str, str] = {
    "left_navigation": "左侧导航栏", "right_content": "右侧内容区",
    "top_bar": "顶部栏", "bottom_bar": "底部栏",
    "dialog": "弹窗", "unknown": "未知区域",
}
_ROLE_CN: dict[str, str] = {
    "text": "文本", "button": "按钮", "switch": "开关", "toggle": "切换",
    "list_entry": "列表项", "settings_entry": "设置项", "tab": "选项卡",
    "navigation_item": "导航项", "input": "输入框", "checkbox": "复选框",
    "container": "容器", "unknown": "未知",
}


def _clean_observation(obs: str) -> str:
    """将原始 observation 翻译为测试人员可读的中文摘要。"""
    if not obs:
        return ""
    # find_element 输出 → 翻译区域/角色
    if "candidate(s) for" in obs:
        lines = obs.split("\n")
        what = lines[0].split(" for ", 1)[-1].strip().strip("'\"")
        clean_lines = [f"搜索 \"{what}\"，找到以下候选元素："]
        cand_count = 0
        for line in lines[1:]:
            stripped = line.strip()
            if not stripped or not stripped.startswith("score="):
                continue
            cand_count += 1
            # 解析 score=1 pri=9 [region/role] "label"
            m = re.search(r'score=(\d+)\s+pri=(\d+)\s+\[(\w+)/(\w+)\]\s*"([^"]*)"', stripped)
            if m and cand_count <= 5:
                region = _REGION_CN.get(m.group(3), m.group(3))
                role = _REGION_CN.get(m.group(4), m.group(4)) if m.group(4) in _REGION_CN else _ROLE_CN.get(m.group(4), m.group(4))
                label = m.group(5)
                # 去掉纯资源 ID 的 label（不好读）
                if label and ":" in label and "/" not in label:
                    label = f"(资源ID: {label})"
                clean_lines.append(f"  {cand_count}. {region}的{role} \"{label}\" (匹配度={m.group(1)})")
        if cand_count == 0:
            clean_lines.append("  (无匹配结果)")
        elif cand_count > 5:
            clean_lines.append(f"  ...（共 {cand_count} 个候选，点击展开查看全部）")
        return "\n".join(clean_lines)
    # get_screen_info 输出 → 提取摘要
    if obs.startswith("layout=") or "\nlayout=" in obs:
        return "当前页面信息已获取，点击展开查看详情"
    # OK/FAIL/ERROR
    if obs.startswith(("OK:", "FAIL:", "ERROR:")):
        return obs.strip()
    # 太长
    if len(obs) > 200:
        return obs[:200].replace("\n", " ").strip() + "..."
    return obs.replace("\n", " ").strip()


def reporter_node(state: TestState, config: RunnableConfig) -> Command:
    """Reporter: 生成最终报告。"""
    cfg: TestConfig = config["configurable"]["test_config"]
    llm = _get_llm_config(cfg, "planner")

    history = state.get("step_history", [])
    plan = state.get("test_plan", [])
    pass_count = sum(1 for s in history if s.get("status") == "success")
    fail_count = len(history) - pass_count

    # 保存原始 observation 到 raw_observation，observation 替换为中文摘要
    for s in history:
        s["raw_observation"] = s.get("observation", "")
        s["observation"] = _clean_observation(s.get("observation", ""))

    step_records = "\n".join(
        f"  步骤{s['index']}: [{s['status']}] {s['intent']} -> {s.get('observation', '')[:200]}"
        for s in history
    )

    messages = REPORTER_TEMPLATE.format_messages(
        user_request=state.get("user_request", ""),
        total_steps=len(plan), pass_count=pass_count, fail_count=fail_count,
        step_records=step_records,
    )

    text_llm = create_llm_client(
        provider=llm["provider"], model=llm["model"],
        api_key=llm["api_key"], base_url=llm["base_url"],
    )

    if text_llm:
        conclusion = _call_llm_with_retry(llm["provider"], text_llm.invoke, messages)
        if conclusion is None:
            conclusion = f"测试完成。成功 {pass_count}/{len(history)} 步。" + (
                "" if fail_count == 0 else f" 失败 {fail_count} 步。")
    else:
        conclusion = f"测试完成。成功 {pass_count}/{len(history)} 步。" + (
            "" if fail_count == 0 else f" 失败 {fail_count} 步。")

    status = "success" if "FAIL" not in str(conclusion).upper() else "fail"

    # RAG 回写
    ctx = get_tool_context()
    if ctx.knowledge_base:
        try:
            ctx.knowledge_base.extract_from_test_result(
                state.get("app_package", "unknown"), state.get("user_request", ""),
                [{"page": s.get("target", ""), "action": s.get("action_type", ""),
                  "observation": s.get("observation", ""), "result": s.get("status", ""),
                  "error": s.get("observation", "") if s.get("status") != "success" else ""}
                 for s in history],
                "PASS" if status == "success" else "FAIL",
            )
        except Exception as exc:
            logger.warning("Knowledge extraction failed: %s", exc)

    # ── 元素身份：Level1 自动写入 ──
    pending = state.get("pending_identities", [])
    if status == "success" and pending and _relational_db:
        app_package = state.get("app_package", "")
        sig = ""
        try:
            sig = ctx.perceiver.screen_signature()[:16] if ctx.perceiver else ""
        except Exception:
            pass
        for entry in pending:
            if entry.get("level") == 1:  # Level1: 只有1个候选，自动写入
                try:
                    _relational_db.save_element_identity(
                        app_package=app_package, page_signature=sig,
                        alias=entry.get("target", ""),
                        resource_id=entry.get("resource_id", ""),
                        class_name=entry.get("class_name", ""),
                        role=entry.get("role", ""),
                        candidates_count=entry.get("candidates_count", 1),
                    )
                except Exception as exc:
                    logger.warning("Element identity save failed: %s", exc)

    # ── SQLite 写入测试执行记录 ──
    # 计算耗时
    started_at_str = state.get("started_at", "")
    duration = 0.0
    if started_at_str:
        try:
            started_at = datetime.fromisoformat(started_at_str)
            duration = round((datetime.now() - started_at).total_seconds(), 2)
        except Exception:
            pass

    # 补步骤耗时到 step_history
    step_times = state.get("step_times", [])
    time_map: dict[int, str] = {}
    for t in step_times:
        time_map[t.get("step_index", 0)] = t.get("started_at", "")
    for s in history:
        si = s.get("index", 0)
        if si in time_map:
            s["started_at"] = time_map[si]
        s.setdefault("started_at", "")

    if _relational_db:
        try:
            thread_id = config.get("configurable", {}).get("thread_id", "")
            _relational_db.record_test_run(
                run_id=thread_id,
                user_request=state.get("user_request", ""),
                app_package=state.get("app_package", ""),
                app_name=state.get("app_name", ""),
                status=status,
                conclusion=str(conclusion),
                steps=history,
                duration_seconds=duration,
            )
        except Exception as exc:
            logger.warning("Failed to record test run: %s", exc)

    logger.info("Reporter done status=%s", status)
    return Command(update={
        "conclusion": str(conclusion),
        "status": status,
        "step_history": history,  # 确保清理后的 observation 写入状态
    })


# ═══════════════════════════════════════════
#  条件路由
# ═══════════════════════════════════════════

def route_after_reviewer(state: TestState) -> str:
    decision = state.get("reviewer_decision", "continue")
    plan = state.get("test_plan", [])
    idx = state.get("current_step_index", 0)
    if decision == "done" or idx >= len(plan) or decision == "abort":
        return "reporter"
    if decision == "ask_human":
        return "human_approval"
    return "executor"


def route_after_human(state: TestState) -> str:
    return "reporter" if state.get("reviewer_decision") == "abort" else "executor"


def route_after_plan_review(state: TestState) -> str:
    """计划审阅后的路由：取消 → reporter，确认 → executor。"""
    if state.get("reviewer_decision") == "abort" or state.get("status") == "cancelled":
        return "reporter"
    return "executor"


# ═══════════════════════════════════════════
#  Graph 构建
# ═══════════════════════════════════════════

def build_graph(config: TestConfig) -> StateGraph:
    """构建 LangGraph StateGraph（ToolNode + MemorySaver + streaming）。"""

    graph = StateGraph(TestState)

    graph.add_node("planner", planner_node)
    graph.add_node("plan_review", plan_review_node)
    graph.add_node("executor", executor_node)
    graph.add_node("reviewer", reviewer_node)
    graph.add_node("human_approval", human_approval_node)
    graph.add_node("reporter", reporter_node)

    graph.set_entry_point("planner")
    graph.add_edge("planner", "plan_review")
    graph.add_conditional_edges("plan_review", route_after_plan_review, {
        "executor": "executor",
        "reporter": "reporter",
    })
    graph.add_edge("executor", "reviewer")

    graph.add_conditional_edges("reviewer", route_after_reviewer, {
        "executor": "executor", "reporter": "reporter", "human_approval": "human_approval",
    })
    graph.add_conditional_edges("human_approval", route_after_human, {
        "executor": "executor", "reporter": "reporter",
    })
    graph.add_edge("reporter", END)

    return graph.compile(checkpointer=MemorySaver())


# ═══════════════════════════════════════════
#  辅助函数
# ═══════════════════════════════════════════


def _parse_json_block(text: str) -> dict[str, Any] | None:
    """从文本中提取 JSON 块。"""
    cleaned = str(text).strip()
    if "```" in cleaned:
        parts = cleaned.split("```")
        if len(parts) >= 2:
            cleaned = parts[1].replace("json", "", 1).strip()
    try:
        return json.loads(cleaned)
    except Exception:
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if match:
            try:
                return json.loads(match.group())
            except Exception:
                pass
    return None


def _normalize_step(step: dict[str, Any], idx: int) -> dict[str, Any]:
    """确保单步字段完整且合法（过滤 emoji / 单字 target / 字段名标准化）。"""
    s = dict(step)  # 浅拷贝
    s.setdefault("index", idx)
    # 字段名标准化：LLM 可能输出非标准字段名
    if "action_type" not in s and "action" in s:
        s["action_type"] = s.pop("action")
    if "intent" not in s and "description" in s:
        s["intent"] = s.pop("description")
    # intent: 去掉 emoji 前缀，保证有意义
    intent = str(s.get("intent", "")).strip()
    intent = re.sub(r"^[✅❌⚠️🟢🔴🟡]+\s*", "", intent)
    if len(intent) < 2:
        intent = f"Step {idx}"
    s["intent"] = intent
    # target: 过滤单字 / emoji / 无意义值
    target = str(s.get("target", "")).strip()
    target = re.sub(r"^[✅❌⚠️🟢🔴🟡]+\s*", "", target)
    if len(target) <= 1 and s.get("action_type") not in ("launch_app", "wait"):
        # 单字 target 对 click/navigate 无意义，用 intent 中的关键词代替
        target = intent[:50]
    s["target"] = target
    # action_type: 必须是合法枚举
    valid_actions = {"launch_app", "click", "navigate_tab", "type_text", "swipe",
                     "press_key", "wait", "assert"}
    at = s.get("action_type", "")
    # 常见非标准 action_type 映射
    _action_aliases = {"observe": "assert", "observation": "assert",
                       "verify": "assert", "check": "assert",
                       "open": "launch_app", "navigate": "navigate_tab",
                       "tap": "click", "toggle": "click"}
    if at not in valid_actions:
        at = _action_aliases.get(at.lower() if isinstance(at, str) else "", "click")
    if at not in valid_actions:
        at = "click"
    s["action_type"] = at
    # wait 类型：提取 duration 字段作为 target
    if at == "wait" and "duration" in s and not s.get("target"):
        s["target"] = str(s["duration"])
    s.setdefault("alternatives", [])
    s.setdefault("expected", "")
    return s


def _extract_steps_from_text(text: str, app_package: str = "") -> list[dict[str, Any]]:
    """从 LLM 自然语言输出中提取步骤（JSON 解析失败时的兆底）。"""
    if not text:
        return []
    steps: list[dict[str, Any]] = []
    # 过滤 emoji 前缀
    _emoji_re = re.compile(r"[✅❌⚠️🟢🔴🟡]+\s*")
    # 匹配中文/英文编号的步骤行：Step 1、步骤1、1.、**Step 1** 等
    pattern = re.compile(
        r"(?:Step\s*\d+|步骤\s*\d+|\d+[\.\)])\s*[:：]?\s*(.+)",
        re.IGNORECASE,
    )
    # 非步骤关键词（观察点、预期结果、异常分支等表格行不算步骤）
    _skip_keywords = {"观察", "预期", "异常", "分支", "备注", "判定", "采集", "注意",
                      "obser", "expect", "except", "note", "remark", "anomal", "branch"}
    action_map: dict[str, tuple[str, str]] = {
        "启动": ("launch_app", "com.android.settings"),
        "打开": ("launch_app", "com.android.settings"),
        "点击": ("click", ""),
        "单击": ("click", ""),
        "输入": ("type_text", ""),
        "等待": ("wait", "3"),
        "验证": ("assert", ""),
        "断言": ("assert", ""),
        "检查": ("assert", ""),
        "滑动": ("swipe", "up"),
        "查找": ("click", ""),
        "找到": ("click", ""),
        "按下": ("press_key", "back"),
        "导航": ("navigate_tab", ""),
        "切换": ("navigate_tab", ""),
        # English keywords
        "launch": ("launch_app", ""),
        "open": ("launch_app", ""),
        "navigate": ("navigate_tab", ""),
        "switch": ("navigate_tab", ""),
        "tap": ("click", ""),
        "click": ("click", ""),
        "type": ("type_text", ""),
        "enter": ("type_text", ""),
        "wait": ("wait", "3"),
        "verify": ("assert", ""),
        "check": ("assert", ""),
        "assert": ("assert", ""),
        "swipe": ("swipe", "up"),
        "scroll": ("swipe", "up"),
        "press": ("press_key", "back"),
    }
    lines = text.split("\n")
    idx = 0
    for line in lines:
        m = pattern.match(line.strip())
        if m:
            content = m.group(1).strip()
            # 去掉 markdown 标记
            content = re.sub(r"\*\*|__", "", content)
            # 过滤 emoji
            content = _emoji_re.sub("", content).strip()
            # 表格行: 提取 |...|...| 中的第一个非空单元格作为描述
            if "|" in content:
                cells = [c.strip() for c in content.split("|") if c.strip()]
                if cells:
                    content = cells[0]
            # 跳过非步骤行（观察点/异常分支等）
            content_lower = content.lower()
            if any(kw in content_lower for kw in _skip_keywords):
                continue
            idx += 1
            # 推断 action_type 和 target
            atype, target = "click", ""
            content_lower = content.lower()
            for kw, (a, t) in action_map.items():
                if kw in content_lower:
                    atype, target = a, t
                    if a in ("launch_app", "wait", "swipe", "press_key"):
                        break
            # 提取引号或中文书名号中的内容作为 target
            quoted = re.findall(r"[\"\'](.+?)[\"\']", content)
            if not quoted:
                quoted = re.findall(r"《(.+?)》", content)
            if quoted:
                target = quoted[0] if len(quoted[0]) > 1 else quoted[-1]
            elif not target:
                # 去掉操作词后的剩余文字作为 target
                remainder = content
                for kw in action_map:
                    remainder = re.sub(rf"^{kw}\s*", "", remainder, flags=re.IGNORECASE)
                target = remainder[:50].strip()
            # wait 类型：从内容中提取数字
            if atype == "wait":
                num_match = re.search(r"(\d+)\s*(s|sec|秒)", content, re.IGNORECASE)
                if num_match:
                    target = num_match.group(1)
            # target 不能是单字（除非是 wait/launch_app）
            if len(target) <= 1 and atype not in ("launch_app", "wait"):
                target = content[:50].strip()
            steps.append({
                "index": idx,
                "intent": content[:80],
                "action_type": atype,
                "target": target,
                "alternatives": [],
                "expected": "",
            })
    if not steps and app_package:
        steps = [
            {"index": 1, "intent": "启动应用", "action_type": "launch_app",
             "target": app_package, "alternatives": [], "expected": "进入应用首页"},
        ]
    return steps


def _parse_decision(text: str) -> str:
    match = re.search(r"DECISION:\s*(\w+)", text, re.I)
    if match:
        decision = match.group(1).lower()
        if decision in {"continue", "retry", "skip", "abort", "ask_human", "done"}:
            return decision
    return "done" if "done" in text.lower() else "continue"


def _parse_human_question(text: str) -> str:
    match = re.search(r"QUESTION:\s*(.+?)(?:\n|$)", text, re.I)
    return match.group(1).strip() if match else "检测到危险操作，是否继续？"


def _save_plan(config: TestConfig, plan: dict[str, Any]) -> None:
    if not plan or not isinstance(plan, dict):
        return
    import yaml
    name = plan.get("name", "test_plan")
    safe_name = re.sub(r"[^0-9a-zA-Z_\-一-龥]+", "_", str(name)).strip("_") or "test_plan"
    case_dir = getattr(config, "case_dir", "test_cases")
    os.makedirs(case_dir, exist_ok=True)
    path = os.path.join(case_dir, f"{safe_name}.yaml")
    plan["created_at"] = plan.get("created_at", datetime.now().isoformat())
    try:
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(plan, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
        logger.info("Plan saved to %s", path)
    except Exception as exc:
        logger.warning("Failed to save plan: %s", exc)

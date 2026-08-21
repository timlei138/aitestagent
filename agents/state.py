from __future__ import annotations

import operator
from typing import Any, Annotated, TypedDict

from pydantic import BaseModel, Field


def _last_value(prev: Any, new: Any) -> Any:
    """后写覆盖（last-value-wins）：用于派生/标量字段，避免并发写触发 InvalidUpdateError。"""
    return new

# ═══════════════════════════════════════════
#  Planner 结构化输出
# ═══════════════════════════════════════════


class TestGoalOutput(BaseModel):
    goal: str = ""
    app_package: str = ""
    app_name: str = ""
    target_pages: list[str] = Field(default_factory=list)
    verification: list[str] = Field(default_factory=list)
    hints: list[str] = Field(default_factory=list)
    parameter_slots: list[dict[str, Any]] = Field(default_factory=list)
    action_semantics: list[str] = Field(default_factory=list)


class ParameterSlot(BaseModel):
    """Structured parameter slot extracted from user request / goal."""

    name: str = ""
    type: str = ""  # duration, number, text, time, count, enum
    unit: str = ""  # minute, hour, px, item, ...
    value: Any = None
    original: str = ""  # 原始文本，如 "50分钟"
    source: str = ""  # "user_request" | "goal" | "verification"


# Backward-compatible helper: convert ParameterSlot or plain dict to dict.
def _slot_to_dict(slot: Any) -> dict[str, Any]:
    if isinstance(slot, ParameterSlot):
        return {
            "name": slot.name,
            "type": slot.type,
            "unit": slot.unit,
            "value": slot.value,
            "original": slot.original,
            "source": slot.source,
        }
    if isinstance(slot, dict):
        return dict(slot)
    return {}


def _slots_to_dicts(slots: list[Any]) -> list[dict[str, Any]]:
    return [_slot_to_dict(s) for s in slots or [] if s]


# ═══════════════════════════════════════════
#  Graph state
# ═══════════════════════════════════════════


class TestState(TypedDict, total=False):
    user_request: str
    app_package: str
    app_name: str
    goal_description: dict[str, Any]
    # 自动批准计划：CLI run --auto-approve 时置 True，plan_review_node 跳过
    # interrupt() 直接 approve，用于无人值守的一次性真机验证。
    auto_approve: bool
    verification_contract: dict[str, Any]
    clause_state: dict[str, Any]
    step_history: Annotated[list[dict[str, Any]], operator.add]
    messages: list[dict[str, Any]]
    # 标量派生字段：多节点（_stop_or_continue / mode_selection 的 goto="reporter" 路径）
    # 会在同一步与 reporter 一起写 status/conclusion，用 _last_value（后写覆盖）
    # 避免 LangGraph InvalidUpdateError。
    conclusion: Annotated[str, _last_value]
    status: Annotated[str, _last_value]
    started_at: str
    step_times: list[dict[str, Any]]
    # V2: 双维度结果
    execution_status: str  # completed / exhausted / error / cancelled / device_offline
    test_verdict: str  # passed / failed / inconclusive
    verification_results: (
        list  # [{"item": "...", "result": "passed|failed|unknown", "screenshot": ""}]
    )
    # 累加型计数器：用 reducer（operator.add）避免多节点同一步写入触发
    # LangGraph InvalidUpdateError。各节点只上报自身本次增量（delta）。
    budget_violation_count: Annotated[int, operator.add]  # P0.4: token budget violations
    llm_call_count: Annotated[int, operator.add]
    tool_call_400_count: Annotated[int, operator.add]
    llm_elapsed_ms: Annotated[float, operator.add]  # 方案 5 回合级 LLM 耗时（D 类最大隐藏成本）
    tool_call_400_rate: Annotated[float, _last_value]  # 派生比率，后写覆盖
    token_usage: (
        dict  # O1: 单次运行 token 消耗汇总（input/output/total/cached/llm_calls）
    )
    execution_mode: str  # direct / guided / explore
    lifecycle_state: str  # Bootstrapping / Direct / Guided / Explore / Terminal
    plan_id: str
    plan_trust: str
    mode_selection_reason: str
    selected_plan_actions: list[dict[str, Any]]
    mode_transition_events: list[dict[str, Any]]
    actual_environment_key: str
    environment_compatibility_score: float
    environment_compatibility_reasons: list[str]
    _guided_downgrade_count: int
    _direct_action_cursor: int
    _direct_downgrade_count: int
    _tool_calls_log: list  # 工具调用实时日志（存入 state，不依赖 ctx）
    _finalization_hint_injected: bool
    _rag_injected_once: bool
    _rag_last_app_package: str
    _knowledge_query_hint_injected: bool
    _last_page_app_key: str
    _last_clickable_count: int
    # 用户手动停止标志（由 orchestrator.request_stop 置位，节点入口检查）。
    # 命中时让图收敛到 reporter 写 cancelled，不影响其他状态的正常流转。
    _stop_requested: bool
    # M2（discrepancy_detection_core_plan §7）：inconclusive（unknown）路由累计次数。
    # evaluator 每次判定 inconclusive 时 +1（累加 reducer），route_after_evaluator
    # 超过上限后强制收敛到 reporter，治「问题2-RootA 图不终止」。
    _unknown_route_count: Annotated[int, operator.add]
    _unknown_exhausted: bool
    # M3（Plan §8 代码护栏）：连续 inconclusive 达到阈值后置位，提示 agent_node
    # 限制契约外探索（如 re-import / launch_app 重开），治 RootB agent 惯性。
    _explore_restricted: bool

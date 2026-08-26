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
    # R2（agent_evolution_plan §10）：显式回放意图（前端「回放」按钮）。配合
    # auto_approved_reason=reuse_hit 在 mode_selection 解锁 direct 准入。
    replay: bool
    # §9 验收2：plan_review 审阅态区分提案来源（reused_plan=历史复用 / 空=new plan）。
    # R1 复用命中（含 env 漂移落人工审的分支）置 reused_plan，正常 planner 路径留空。
    proposal_source: str
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
    # 时间账三段拆分（agent_evolution_plan §13）：planner 段由 planner_node 累计，
    # review 等待段存 tool ctx（interrupt 节点更新不提交，state 存不住进入时刻），
    # 执行段 = duration − 前两者（reporter 反推），三段之和 == duration_seconds。
    planner_elapsed_seconds: float
    plan_review_wait_seconds: float
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
    # R1（agent_evolution_plan §9）：复用命中自动过审的审计标记（"reuse_hit"），
    # 空串表示走人工审。仅作 trace 透出，不参与任何模式决策。
    auto_approved_reason: str
    selected_plan_actions: list[dict[str, Any]]
    mode_transition_events: list[dict[str, Any]]
    actual_environment_key: str
    environment_compatibility_score: float
    environment_compatibility_reasons: list[str]
    _guided_downgrade_count: int
    _direct_action_cursor: int
    _direct_downgrade_count: int
    # R3（agent_evolution_plan §11）：direct 导航动作耗尽收口标志。进入收尾分支时
    # 即刻置位（不等 evaluator verdict），与 unknown 回环互斥——置位后路由不再回
    # direct 重放动作，unknown 只送 agent 补验。
    _direct_exhausted: bool
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

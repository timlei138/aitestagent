from __future__ import annotations

from typing import Any, TypedDict

from pydantic import BaseModel, Field


# ═══════════════════════════════════════════
#  Planner 结构化输出
# ═══════════════════════════════════════════

class StepDefModel(BaseModel):
    index: int
    intent: str = ""
    action_type: str = ""
    target: str = ""
    alternatives: list[str] = Field(default_factory=list)
    expected: str = ""


class TestPlanOutput(BaseModel):
    """Planner Agent 的结构化输出 — 用 with_structured_output 直接生成。"""
    name: str = ""
    description: str = ""
    app_package: str = ""
    app_name: str = ""
    steps: list[StepDefModel] = Field(default_factory=list)
    verification: list[str] = Field(default_factory=list)


# ═══════════════════════════════════════════
#  LangGraph 图状态
# ═══════════════════════════════════════════

class StepDef(TypedDict, total=False):
    index: int
    intent: str
    action_type: str
    target: str
    alternatives: list[str]
    expected: str


class StepRecord(TypedDict, total=False):
    index: int
    intent: str
    action_type: str
    target: str
    status: str
    observation: str
    anomaly: dict[str, Any] | None


class TestState(TypedDict, total=False):
    # ── 用户输入 ──
    user_request: str
    app_package: str
    app_name: str

    # ── 计划 ──
    test_plan: list[dict[str, Any]]
    current_step_index: int

    # ── 执行上下文 ──
    last_action: str
    last_observation: str
    step_history: list[dict[str, Any]]
    pending_identities: list[dict[str, Any]]    # 待确认的元素身份映射

    # ── 工具调用用 messages（LangGraph ToolNode 兼容） ──
    messages: list[dict[str, Any]]

    # ── Reviewer 的判断 ──
    anomalies: list[dict[str, Any]]
    reviewer_decision: str
    human_question: str
    retry_count: int

    # ── 最终结果 ──
    conclusion: str
    report_path: str
    status: str

    # ── 时间追踪 ──
    started_at: str
    step_times: list[dict[str, Any]]  # [{step_index, started_at, finished_at}]

    # ── 内部 ──
    _interrupt_reason: str

from __future__ import annotations

from typing import Any, TypedDict

from pydantic import BaseModel, Field


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


# ═══════════════════════════════════════════
#  Graph state
# ═══════════════════════════════════════════

class TestState(TypedDict, total=False):
    user_request: str
    app_package: str
    app_name: str
    goal_description: dict[str, Any]
    step_history: list[dict[str, Any]]
    messages: list[dict[str, Any]]
    conclusion: str
    status: str
    started_at: str
    step_times: list[dict[str, Any]]
    # V2: 双维度结果
    execution_status: str       # completed / exhausted / error / cancelled / device_offline
    test_verdict: str           # passed / failed / inconclusive
    verification_results: list  # [{"item": "...", "result": "passed|failed|unknown", "screenshot": ""}]

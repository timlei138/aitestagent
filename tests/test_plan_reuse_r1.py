"""R1 检索前移单测（agent_evolution_plan §9）。

覆盖：
- _try_plan_reuse：逐字匹配命中 / 措辞不同跳过 / 未审批或无动作跳过 / 无 KB 兜底
  / env 漂移只收回 auto_approve 不丢命中。
- _goal_from_stored_contract：契约原文重建 claim/spec + slots/semantics 原样透传。
- planner_node 命中路径：LLM 客户端一旦被构造即失败（验收：planner LLM 调用为 0），
  auto_approve=True 且 reason=reuse_hit、planner 段耗时为 0。
- plan_review_node auto_approve 分支：复用契约 span 校验通过后直接置 approved（无 interrupt）。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from agents import graph, nodes
from config import TestConfig as AppTestConfig

REQUEST = "创建一个50分钟的课程表"
STATEMENT = "课程表页面显示新建的课程"

SLOTS = [
    {
        "name": "duration",
        "type": "duration",
        "unit": "minute",
        "value": 50,
        "original": "50分钟",
        "source": "user_request",
    }
]
SEMANTICS = ["create_timetable"]

APPROVED_CONTRACT = {
    "status": "approved",
    "verifications": [
        {
            "key": "v0",
            "statement": STATEMENT,
            "goal_source_span": [0, len(STATEMENT)],
            "clauses": [
                {
                    "id": "v0.0",
                    "claim": STATEMENT,
                    "channels": ["ui_text"],
                    "spec": {"page_is": "TimetableActivity"},
                    "goal_source_span": [0, len(STATEMENT)],
                }
            ],
        }
    ],
}


def _full_plan(
    *,
    user_request: str = REQUEST,
    contract_status: str = "approved",
    contract: dict | None = None,
    actions: list | None = None,
    environment_key: str = "",
) -> dict:
    return {
        "plan_id": "plan-r1",
        "contract_status": contract_status,
        "verification_contract_json": json.dumps(
            contract if contract is not None else APPROVED_CONTRACT,
            ensure_ascii=False,
        ),
        "environment_key": environment_key,
        "task_signature": {
            "user_request": user_request,
            "parameter_slots": SLOTS,
            "action_semantics": SEMANTICS,
        },
        "actions": (
            actions
            if actions is not None
            else [{"tool_name": "click", "tool_input_json": "{}"}]
        ),
    }


class _FakeKB:
    def __init__(self, plan_ids: list[str]):
        self._plan_ids = plan_ids

    def query_task_plan_summaries(self, app_package, query="", top_k=5, **kwargs):
        return [{"metadata": {"plan_id": pid}} for pid in self._plan_ids]

    def query_semantic_knowledge(self, app_package, top_k=20):
        return {}


class _FakeDB:
    def __init__(self, plans: dict):
        self._plans = plans

    def get_full_execution_plan(self, plan_id: str):
        return self._plans.get(plan_id)


def _patch(monkeypatch, plans: dict, device=None) -> None:
    monkeypatch.setattr(graph, "_relational_db", _FakeDB(plans))
    monkeypatch.setattr(
        nodes,
        "get_tool_context",
        lambda: SimpleNamespace(
            knowledge_base=_FakeKB(list(plans.keys())),
            device=device,
            screen_size=(0, 0),
        ),
    )


def _state() -> dict:
    return {"user_request": REQUEST, "app_package": "com.example.app"}


def test_reuse_hits_on_verbatim_request(monkeypatch):
    _patch(monkeypatch, {"plan-r1": _full_plan()})
    hit = nodes._try_plan_reuse(_state())  # type: ignore[attr-defined]
    assert hit is not None
    assert hit["plan_id"] == "plan-r1"
    assert hit["contract"] == APPROVED_CONTRACT
    assert hit["parameter_slots"] == SLOTS
    assert hit["action_semantics"] == SEMANTICS
    # device=None → 弱环境键全空 vs 存储键无冲突关键字段 ⇒ 兼容
    assert hit["env_compatible"] is True


def test_reuse_skips_different_wording(monkeypatch):
    _patch(
        monkeypatch,
        {"plan-r1": _full_plan(user_request="创建一个60分钟的课程表")},
    )
    assert nodes._try_plan_reuse(_state()) is None  # type: ignore[attr-defined]


def test_reuse_skips_unapproved_or_actionless(monkeypatch):
    _patch(monkeypatch, {"plan-r1": _full_plan(contract_status="draft")})
    assert nodes._try_plan_reuse(_state()) is None  # type: ignore[attr-defined]

    _patch(monkeypatch, {"plan-r1": _full_plan(actions=[])})
    assert nodes._try_plan_reuse(_state()) is None  # type: ignore[attr-defined]


def test_reuse_returns_none_without_kb(monkeypatch):
    monkeypatch.setattr(graph, "_relational_db", _FakeDB({}))
    monkeypatch.setattr(
        nodes, "get_tool_context", lambda: SimpleNamespace(knowledge_base=None)
    )
    assert nodes._try_plan_reuse(_state()) is None  # type: ignore[attr-defined]


def test_reuse_env_drift_withdraws_auto_approve_only(monkeypatch):
    # R6 口径：真在目标 App 内但 activity 与沉淀侧不同（双侧非空且不同）→
    # critical mismatch；命中保留（env_compatible=False），由调用方收回
    # auto_approve 落人工确认。（桌面/其它 App 前景已改为「未知不误杀」，
    # 见 test_reuse_env_compatible_when_device_on_launcher。）
    stored = _full_plan(
        environment_key='{"package": "com.example.app", "activity": ".Main"}'
    )
    device = SimpleNamespace(
        current_app=lambda: {"package": "com.example.app", "activity": ".Settings"}
    )
    _patch(monkeypatch, {"plan-r1": stored}, device=device)
    hit = nodes._try_plan_reuse(_state())  # type: ignore[attr-defined]
    assert hit is not None
    assert hit["env_compatible"] is False


def test_goal_from_stored_contract_rebuilds_claim_spec_and_passthrough():
    goal = nodes._goal_from_stored_contract(  # type: ignore[attr-defined]
        APPROVED_CONTRACT, REQUEST, SLOTS, SEMANTICS
    )
    assert goal["goal"] == REQUEST
    assert goal["verification"] == [
        {"claim": STATEMENT, "spec": {"page_is": "TimetableActivity"}}
    ]
    assert goal["parameter_slots"] == SLOTS
    assert goal["action_semantics"] == SEMANTICS


def test_planner_node_reuse_skips_llm_and_sets_auto_approve(monkeypatch):
    _patch(monkeypatch, {"plan-r1": _full_plan()})

    def _boom(**kwargs):  # LLM 一旦被构造即失败：验收「planner LLM 调用为 0」
        raise AssertionError("planner LLM must not run on reuse hit")

    monkeypatch.setattr(nodes, "create_llm_client", _boom)
    command = nodes.planner_node(
        _state(),
        {"configurable": {"test_config": AppTestConfig(), "thread_id": "t-r1"}},
    )
    upd = command.update
    assert upd["auto_approve"] is True
    assert upd["auto_approved_reason"] == "reuse_hit"
    assert upd["planner_elapsed_seconds"] == 0.0
    assert upd["verification_contract"] == APPROVED_CONTRACT
    assert upd["goal_description"]["action_semantics"] == SEMANTICS
    assert upd["budget_violation_count"] == 0


def test_planner_reuse_env_drift_keeps_proposal_but_no_auto_approve(monkeypatch):
    # §9③：复用命中但 env 不兼容（R6 口径：目标包内 activity 漂移）→ 契约仍作为
    # 提案复用（跳过 planner LLM），但收回自动过审落人工确认。
    stored = _full_plan(
        environment_key='{"package": "com.example.app", "activity": ".Main"}'
    )
    device = SimpleNamespace(
        current_app=lambda: {"package": "com.example.app", "activity": ".Settings"}
    )
    _patch(monkeypatch, {"plan-r1": stored}, device=device)

    def _boom(**kwargs):
        raise AssertionError("planner LLM must not run on reuse hit")

    monkeypatch.setattr(nodes, "create_llm_client", _boom)
    command = nodes.planner_node(
        _state(),
        {"configurable": {"test_config": AppTestConfig(), "thread_id": "t-r1-env"}},
    )
    upd = command.update
    assert upd["verification_contract"] == APPROVED_CONTRACT  # 提案仍是历史契约
    assert upd["auto_approve"] is False
    assert upd["auto_approved_reason"] == ""
    # §9 验收2：落人工审时带来源标记，前端据此标注「来源=reused plan」
    assert upd.get("proposal_source") == "reused_plan"


def test_plan_review_auto_approves_reuse_contract(monkeypatch):
    monkeypatch.setattr(nodes, "get_tool_context", lambda: None)
    state = {
        "auto_approve": True,
        "verification_contract": json.loads(json.dumps(APPROVED_CONTRACT)),
    }
    command = nodes.plan_review_node(state, {})
    approved = command.update["verification_contract"]
    assert approved["status"] == "approved"
    assert approved.get("span_validation_error") is None


def test_reuse_env_compatible_when_device_on_launcher(monkeypatch):
    """R6 后：回放起点在桌面（launcher）→ 目标 App 状态未知置空 → 不再误判
    incompatible（验收「换桌面 launcher 不影响匹配」）。"""
    stored = _full_plan(
        environment_key='{"package": "com.example.app", "activity": ".Main"}'
    )
    device = SimpleNamespace(
        current_app=lambda: {"package": "com.miui.home", "activity": "Launcher"}
    )
    _patch(monkeypatch, {"plan-r1": stored}, device=device)
    hit = nodes._try_plan_reuse(_state())  # type: ignore[attr-defined]
    assert hit is not None
    assert hit["env_compatible"] is True


def test_reuse_env_incompatible_when_inside_app_activity_differs(monkeypatch):
    """R6 后：真在目标 App 内但 activity 不同（双侧非空且不同）→ 仍如实判不兼容。"""
    stored = _full_plan(
        environment_key='{"package": "com.example.app", "activity": ".Main"}'
    )
    device = SimpleNamespace(
        current_app=lambda: {"package": "com.example.app", "activity": ".Settings"}
    )
    _patch(monkeypatch, {"plan-r1": stored}, device=device)
    hit = nodes._try_plan_reuse(_state())  # type: ignore[attr-defined]
    assert hit is not None
    assert hit["env_compatible"] is False


def test_reuse_prefers_env_compatible_over_stale_dirty_key_plan(monkeypatch):
    """R6 转型期：同一请求同时存在旧口径脏键 plan（先沉淀）与 R6 后净键 plan
    时，复用优先取 env 兼容者——召回顺序不再让旧沉淀遮蔽新沉淀（与
    mode_selection 真实检索按 env_score 择优对齐）。"""
    device = SimpleNamespace(
        current_app=lambda: {"package": "com.example.app", "activity": ".Settings"}
    )
    stale = _full_plan(
        environment_key='{"package": "com.example.app", "activity": ".Main"}'
    )
    stale["plan_id"] = "plan-stale"
    clean = _full_plan(
        environment_key='{"package": "com.example.app", "activity": ".Settings"}'
    )
    clean["plan_id"] = "plan-clean"
    # 召回顺序：脏键在前 —— 修复前首个逐字命中即返回，会拿到 plan-stale
    _patch(monkeypatch, {"plan-stale": stale, "plan-clean": clean}, device=device)
    hit = nodes._try_plan_reuse(_state())  # type: ignore[attr-defined]
    assert hit is not None
    assert hit["plan_id"] == "plan-clean"
    assert hit["env_compatible"] is True


def test_reuse_all_incompatible_falls_back_to_first_hit(monkeypatch):
    """候选全不兼容时回落首个命中：契约照常提案、仅收回自动过审落人工确认
    （§9③ 自愈路径不受影响）。"""
    device = SimpleNamespace(
        current_app=lambda: {"package": "com.example.app", "activity": ".Settings"}
    )
    first = _full_plan(
        environment_key='{"package": "com.example.app", "activity": ".Main"}'
    )
    first["plan_id"] = "plan-first"
    second = _full_plan(
        environment_key='{"package": "com.example.app", "activity": ".Other"}'
    )
    second["plan_id"] = "plan-second"
    _patch(monkeypatch, {"plan-first": first, "plan-second": second}, device=device)
    hit = nodes._try_plan_reuse(_state())  # type: ignore[attr-defined]
    assert hit is not None
    assert hit["plan_id"] == "plan-first"
    assert hit["env_compatible"] is False

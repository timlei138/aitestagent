"""R2 显式回放解锁 direct 单测（agent_evolution_plan §10）。

覆盖：
- replay=true + reuse_hit + env 兼容 → 首跑即 direct，且只重演导航动作
  （assert_*/vision_tap/click_and_check 被过滤）。
- 非 replay 重跑 / 无 reuse_hit / env 不达标 / 全是验证类动作 → 维持现有保守阶梯。
- _is_verification_action 分类边界。
"""

from __future__ import annotations

import agents.rag_context as rag_context
from agents import nodes
from config import TestConfig

STATEMENT = "课程表页面显示新建的课程"

# 与 test_plan_reuse_r1 同款：span 校验可通过的最小 approved 契约
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


def _plan(plan_id: str = "plan-r2", **overrides) -> dict:
    plan = {
        "plan_id": plan_id,
        "plan_trust": "candidate",
        # direct_approved=False：证明 replay 分支绕过的是「人工批准 + 统计门槛」，
        # 而不是依赖既有 direct_ready 条件。
        "direct_approved": False,
        "quality_score": 0.0,
        "success_count": 0,
        "environment_key": "",
        "actions": [
            {"tool_name": "click", "action_index": 0},
            {"tool_name": "launch_app", "action_index": 1},
            {"tool_name": "long_press", "action_index": 2},
            {"tool_name": "swipe", "action_index": 3},
            {"tool_name": "assert_page_contains", "action_index": 4},
            {"tool_name": "vision_tap", "action_index": 5},
            {"tool_name": "click_and_check", "action_index": 6},
        ],
    }
    plan.update(overrides)
    return plan


def _patch_retrieval(
    monkeypatch, plan: dict | None, env_score: float = 1.0
):
    # score=0 时附带非空 reasons，避免触发 mode_selection 的「无评分则本地重算」
    # 兜底（空键 vs 空键会重算出 1.0，掩盖被测阈值分支）。
    env_reasons = [] if env_score else ["test_controlled_mismatch"]

    def _fake_retrieve(**kwargs):
        return {
            "purpose": "task_plan",
            "source": "vector_then_relational",
            "items": [plan] if plan else [],
            "environment_compatibility_score": env_score if plan else None,
            "environment_compatibility_reasons": env_reasons,
        }

    monkeypatch.setattr(rag_context, "retrieve_knowledge", _fake_retrieve)
    monkeypatch.setattr(nodes, "get_tool_context", lambda: None)


def _state(replay: bool, reason: str = "reuse_hit") -> dict:
    return {
        "app_package": "com.example.app",
        "user_request": "创建一个50分钟的课程表",
        "verification_contract": APPROVED_CONTRACT,
        "replay": replay,
        "auto_approved_reason": reason,
    }


CFG = TestConfig()


def test_verification_action_classifier_boundaries():
    for name in (
        "assert_page_contains",
        "assert_element_exists",
        "vision_tap",
        "click_and_check",
    ):
        assert nodes._is_verification_action(name)
    for name in ("click", "long_press", "swipe", "launch_app", ""):
        assert not nodes._is_verification_action(name)


def test_replay_unlocks_direct_and_filters_verification_actions(monkeypatch):
    _patch_retrieval(monkeypatch, _plan())
    command = nodes.mode_selection_node(_state(replay=True), {})
    upd = command.update
    assert upd["execution_mode"] == "direct"
    assert upd["mode_selection_reason"] == "replay_direct_reuse_hit"
    assert upd["plan_id"] == "plan-r2"
    # 只重演导航动作；验证类交 R3 收尾自动判定
    assert [a["tool_name"] for a in upd["selected_plan_actions"]] == [
        "click",
        "launch_app",
        "long_press",
        "swipe",
    ]


def test_non_replay_rerun_keeps_conservative_ladder(monkeypatch):
    _patch_retrieval(monkeypatch, _plan())
    command = nodes.mode_selection_node(_state(replay=False), {})
    upd = command.update
    # 未达 direct_ready（无人工批准/统计门槛），env=1.0 ≥ guided 阈值 → guided
    assert upd["execution_mode"] == "guided"
    assert upd["mode_selection_reason"] != "replay_direct_reuse_hit"


def test_replay_without_reuse_hit_stays_on_ladder(monkeypatch):
    _patch_retrieval(monkeypatch, _plan())
    command = nodes.mode_selection_node(
        _state(replay=True, reason=""), {}
    )
    assert command.update["execution_mode"] != "direct"


def test_replay_env_below_threshold_stays_off_direct(monkeypatch):
    _patch_retrieval(monkeypatch, _plan(), env_score=0.0)
    command = nodes.mode_selection_node(_state(replay=True), {})
    upd = command.update
    assert upd["execution_mode"] != "direct"
    assert upd["mode_selection_reason"] == "matching_plan_environment_incompatible"


def test_replay_with_only_verification_actions_stays_off_direct(monkeypatch):
    only_verify = _plan(
        actions=[
            {"tool_name": "assert_page_contains", "action_index": 0},
            {"tool_name": "vision_tap", "action_index": 1},
        ]
    )
    _patch_retrieval(monkeypatch, only_verify)
    command = nodes.mode_selection_node(_state(replay=True), {})
    upd = command.update
    assert upd["execution_mode"] != "direct"

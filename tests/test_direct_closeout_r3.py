"""R3 direct 收尾零 LLM 收口单测（agent_evolution_plan §11）。

覆盖：
- 导航动作真耗尽 → 耗尽标志即刻置位、一次 perceive + auto_record_evidence、
  不降级 guided（路由交 static edge 进 evaluator 纯代码判定）。
- 证据采集失败不影响标志置位（诚实兜底）。
- phase 预算耗尽仍走原 guided 降级护栏（行为保留）。
- 路由守卫：置位后 route_after_evaluator 绝不再返回 direct（与 unknown 回环互斥，
  单测锁定「不重放动作」）。
"""

from __future__ import annotations

from types import SimpleNamespace

import agents.verification as verification_module
from agents import graph, nodes


def _state(cursor: int, n_actions: int = 2, **overrides) -> dict:
    state = {
        "plan_id": "plan-r3",
        "execution_mode": "direct",
        "_direct_action_cursor": cursor,
        "_direct_downgrade_count": 0,
        "selected_plan_actions": [
            {"tool_name": "click", "tool_input_json": "{}"} for _ in range(n_actions)
        ],
        "verification_contract": {"status": "approved"},
        "mode_transition_events": [],
        # 固定预算：max_agent_iterations=10 → direct phase budget=3
        "goal_description": {
            "target_pages": ["p1", "p2", "p3"],
            "verification": ["v1", "v2"],
        },
    }
    state.update(overrides)
    return state


def _patch_ctx(monkeypatch):
    """Fake perceiver/device + 记录型 auto_record_evidence。"""
    recorded: dict = {}

    def _fake_auto_record(ctx, contract, u, current_app):
        recorded["contract"] = contract
        recorded["u"] = u
        recorded["current_app"] = current_app

    monkeypatch.setattr(verification_module, "auto_record_evidence", _fake_auto_record)

    snapshot = object()

    class _Perceiver:
        def __init__(self):
            self.calls = 0

        def perceive(self):
            self.calls += 1
            return snapshot

    perceiver = _Perceiver()
    ctx = SimpleNamespace(
        perceiver=perceiver,
        device=SimpleNamespace(current_app=lambda: {}),
    )
    monkeypatch.setattr(nodes, "get_tool_context", lambda: ctx)
    return perceiver, recorded, snapshot


def test_exhaustion_sets_flag_immediately_and_records_evidence(monkeypatch):
    perceiver, recorded, snapshot = _patch_ctx(monkeypatch)
    command = nodes.direct_node(_state(cursor=2, n_actions=2), {})
    upd = command.update
    # 标志即刻置位，不等 evaluator verdict
    assert upd["_direct_exhausted"] is True
    # 不降级 guided —— 保持 direct 由 static edge 进入 evaluator 判定
    assert upd["execution_mode"] == "direct"
    assert upd["lifecycle_state"] == "Direct"
    assert upd["mode_selection_reason"] == "direct_actions_exhausted_closeout"
    # 恰好一次 perceive（零 LLM）+ 契约证据落盘
    assert perceiver.calls == 1
    assert recorded["u"] is snapshot
    assert recorded["contract"]["status"] == "approved"
    # 转移事件如实记录去向 evaluator
    assert upd["mode_transition_events"][-1]["reason"] == "direct_actions_exhausted"
    assert upd["mode_transition_events"][-1]["to"] == "evaluator"


def test_exhaustion_flag_survives_evidence_failure(monkeypatch):
    perceiver, _recorded, _snap = _patch_ctx(monkeypatch)

    def _boom(*args, **kwargs):
        raise RuntimeError("evidence backend down")

    monkeypatch.setattr(verification_module, "auto_record_evidence", _boom)
    command = nodes.direct_node(_state(cursor=2, n_actions=2), {})
    upd = command.update
    assert upd["_direct_exhausted"] is True
    assert upd["execution_mode"] == "direct"
    assert perceiver.calls == 1


def test_no_perceiver_still_sets_flag(monkeypatch):
    _perceiver, _recorded, _snap = _patch_ctx(monkeypatch)
    monkeypatch.setattr(
        nodes,
        "get_tool_context",
        lambda: SimpleNamespace(perceiver=None, device=None),
    )
    command = nodes.direct_node(_state(cursor=2, n_actions=2), {})
    upd = command.update
    assert upd["_direct_exhausted"] is True


def test_phase_budget_exhaustion_still_downgrades_to_guided(monkeypatch):
    perceiver, _recorded, _snap = _patch_ctx(monkeypatch)
    # cursor=3 >= phase budget(3) 但 < len(actions)=5 → 资源护栏，维持 guided 降级
    command = nodes.direct_node(_state(cursor=3, n_actions=5), {})
    upd = command.update
    assert "_direct_exhausted" not in upd
    assert upd["execution_mode"] == "guided"
    assert upd["mode_selection_reason"] == "direct_phase_budget_exhausted"
    assert perceiver.calls == 0  # 不做收口证据采集


def test_router_never_returns_direct_after_closeout():
    """互斥单测锁定：置位后 unknown 回环绝不再把图送回 direct 重放动作。"""
    base = {
        "verification_contract": {"status": "approved"},
        "clause_state": {"verdict": "inconclusive"},
        "execution_mode": "direct",
        "_direct_exhausted": True,
        "_unknown_route_count": 0,
    }
    # 无 status（纯 direct 回放从未经过 agent）也必须送 agent 补验
    assert graph.route_after_evaluator(dict(base)) == "agent"
    # 即便 agent 曾声明 DONE（status=success）同样只送 agent，不回 direct
    done_loop = dict(base)
    done_loop["status"] = "success"
    assert graph.route_after_evaluator(done_loop) == "agent"


def test_router_keeps_direct_when_not_exhausted():
    """未置位时守卫不得误伤正常 direct 连续执行。"""
    state = {
        "verification_contract": {"status": "approved"},
        "clause_state": {"verdict": "inconclusive"},
        "execution_mode": "direct",
        "_direct_exhausted": False,
        "_direct_downgrade_count": 0,
        "_unknown_route_count": 0,
    }
    assert graph.route_after_evaluator(state) == "direct"

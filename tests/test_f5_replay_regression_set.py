"""F5 录制回放回归集（agent_evolution_plan §7 最小版）。

三条手写场景 + bug 注入反证（P3：结构化 diff 断言，不 diff 整个 state）：
- 高频：direct 全链零 LLM 通过 —— verdict=passed / mode=direct / llm_call_count==0；
- 恢复：save_btn enabled=True → 权威 FAIL → failed，不降级、零 LLM；
- 歧义：save_btn 缺失 → v0.1 unknown → R3 收口送 agent 补验（脚本 LLM DONE）
  → inconclusive；
- 反证：注入「匹配器恒 None」回归后高频场景期望被打破，证明回放集能变红。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from replay_harness import (
    APP_PACKAGE,
    ReplayScenario,
    make_element,
    run_replay_scenario,
)

_USER_REQUEST = "打开课程表页面，确认保存按钮置灰不可点"

_GOAL = {
    "goal": "进入课程表页面并确认保存按钮置灰不可点",
    "target_pages": [],
    # F3 嵌套契约：一个验证项两个 clause，各带结构化 spec
    "verification": [
        {
            "claim": "已进入课程表页面且保存按钮置灰不可点",
            "clauses": [
                {
                    "claim": "已进入课程表页面",
                    "spec": {"predicate": "page_is", "target": "TimetableActivity"},
                },
                {
                    "claim": "保存按钮置灰不可点",
                    "spec": {"predicate": "element_disabled", "target": "save_btn"},
                },
            ],
        }
    ],
    "hints": [],
}


def _scenario(case_id: str, category: str, elements: list) -> ReplayScenario:
    return ReplayScenario(
        case_id=case_id,
        category=category,
        user_request=_USER_REQUEST,
        goal=_GOAL,
        snapshot_elements=elements,
    )


def _clause_statuses(r: dict) -> dict:
    """从 verification_results 提取 {clause id: status}。"""
    statuses = {}
    for item in r["result"]["verification_results"]:
        for clause in item["clauses"]:
            statuses[clause["id"]] = clause["status"]
    return statuses


def _transition_pairs(state: dict) -> set[tuple[str, str]]:
    return {
        (t.get("from"), t.get("to"))
        for t in state.get("mode_transition_events", [])
        if isinstance(t, dict)
    }


# ── 场景① 高频：direct 全链零 LLM 通过 ──


def test_high_freq_direct_full_pass(monkeypatch):
    elements = [
        make_element(rid="com.example.app:id/save_btn", label="保存", enabled=False),
        make_element(rid="com.example.app:id/title_text", label="课程表"),
    ]
    r = run_replay_scenario(monkeypatch, _scenario("high-freq", "高频", elements))

    assert r["result"]["test_verdict"] == "passed"
    assert r["state"].get("execution_mode") == "direct"
    assert r["state"].get("auto_approved_reason") == "reuse_hit"
    assert (
        r["state"].get("mode_selection_reason")
        == "direct_actions_exhausted_closeout"
    )
    # 零 LLM：planner 复用跳过 + direct 直通 + 收口纯代码判定
    assert r["result"]["llm_call_count"] == 0
    statuses = _clause_statuses(r)
    assert statuses.get("v0.0") == "passed"  # page_is TimetableActivity
    assert statuses.get("v0.1") == "passed"  # element_disabled save_btn
    # R3：动作耗尽 → direct→evaluator 收口迁移，无降级 guided
    assert ("direct", "evaluator") in _transition_pairs(r["state"])
    assert r["ctx"].device.started == [(APP_PACKAGE, ".TimetableActivity")]


# ── 场景② 恢复：权威反证 → failed，不降级、零 LLM ──


def test_recovery_authoritative_fail(monkeypatch):
    elements = [
        # 沉淀后按钮被修复为可点（enabled=True）→ element_disabled 权威 FAIL
        make_element(rid="com.example.app:id/save_btn", label="保存", enabled=True),
        make_element(rid="com.example.app:id/title_text", label="课程表"),
    ]
    r = run_replay_scenario(monkeypatch, _scenario("recovery", "恢复", elements))

    assert r["result"]["test_verdict"] == "failed"
    assert r["result"]["llm_call_count"] == 0
    statuses = _clause_statuses(r)
    assert statuses.get("v0.0") == "passed"  # 页面到了
    assert statuses.get("v0.1") == "failed"  # 权威反证
    assert ("direct", "evaluator") in _transition_pairs(r["state"])
    # 差异报告：期望 disabled vs 实际 enabled
    clause = next(
        c
        for item in r["result"]["verification_results"]
        for c in item["clauses"]
        if c["id"] == "v0.1"
    )
    assert clause["discrepancy"]["expected"] == {"disabled": True}
    assert clause["discrepancy"]["actual"] == {"enabled": True}


# ── 场景③ 歧义：unknown → R3 收口送 agent 补验 → inconclusive ──


def test_ambiguous_downgrades_to_agent_supplement(monkeypatch):
    elements = [
        # save_btn 不在页面上（n==0 → element_disabled 返回 None，诚实不写）
        make_element(rid="com.example.app:id/title_text", label="课程表"),
    ]
    scenario = _scenario("ambiguous", "歧义", elements)
    scenario.agent_responses = [AIMessage(content="DONE: 目标元素未出现，需人工复核")]
    r = run_replay_scenario(monkeypatch, scenario)

    assert r["result"]["test_verdict"] == "inconclusive"
    statuses = _clause_statuses(r)
    assert statuses.get("v0.0") == "passed"
    assert statuses.get("v0.1") == "unknown"
    # R3 收口后 unknown 送 agent 补验：至少一次脚本 LLM 调用
    assert r["result"]["llm_call_count"] >= 1
    assert r["state"].get("_direct_exhausted") is True
    assert ("direct", "evaluator") in _transition_pairs(r["state"])


# ── bug 注入反证：匹配器回归必须让回放集变红 ──


def test_mutation_matcher_always_none_turns_set_red(monkeypatch):
    import agents.verification as verification

    elements = [
        make_element(rid="com.example.app:id/save_btn", label="保存", enabled=False),
        make_element(rid="com.example.app:id/title_text", label="课程表"),
    ]
    monkeypatch.setattr(
        verification, "_match_spec", lambda spec, u, current_app: None
    )
    r = run_replay_scenario(monkeypatch, _scenario("mutation", "反证", elements))

    # 匹配器失效 → 高频场景的「零 LLM 通过」期望被打破
    assert r["result"]["test_verdict"] != "passed"

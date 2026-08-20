"""#5 回归：验证契约 clause_id 护栏。

agent 误填孤儿 clause_id（如第10节→v2.1，但契约只有 v0.0/v1.0/v2.0）时，
证据不应写入 _evidence_events（否则 evaluate_verification 精确匹配不到导致漏判），
工具返回应提示合法取值范围，让 LLM 自纠。

fixture 使用真实的 v0/v1/v2 分离结构（每条 verification 挂自己名下的 clause），
避免把跨 key 错配无意中锁成合法。跨 key 错配（v1::v2.0）明确为当前拒绝行为。
"""
from types import SimpleNamespace

import tools.verify as verify_tools
from tools.context import ToolContext, set_tool_context


def _make_ctx(contract):
    class _Device:
        def current_app(self):
            return {"package": "com.example", "activity": ".MainActivity"}

        def snapshot(self):
            return SimpleNamespace(width=1080, height=2400)

    class _Perceiver:
        def perceive(self):
            return SimpleNamespace(
                activity=".MainActivity",
                page_title="",
                summary="",
                layout="",
                primary_paths=[],
                elements=[SimpleNamespace(label="添加", resource_id="btn_add", class_name="android.widget.Button", associated_label="", context_path="", role="list_entry", region="main_content", bounds=(0, 0, 10, 10), clickable=True, text="添加", checked=None)],
            )

    ctx = ToolContext(device=_Device(), perceiver=_Perceiver())
    ctx._verification_contract = contract
    ctx._evidence_events = []
    return ctx


# 真实结构：v0/v1/v2 各挂各名下的 clause
CONTRACT = {
    "verifications": [
        {"key": "v0", "clauses": [{"id": "v0.0"}]},
        {"key": "v1", "clauses": [{"id": "v1.0"}]},
        {"key": "v2", "clauses": [{"id": "v2.0"}]},
    ]
}


def test_assert_page_state_rejects_orphan_clause_id():
    ctx = _make_ctx(CONTRACT)
    set_tool_context(ctx)
    out = verify_tools.assert_page_state.invoke(
        {"package": "com.example", "verification_key": "v1", "clause_id": "v2.1"}
    )
    assert "归因被拒" in out
    assert "v2.1" in out
    # 孤儿证据不得写入
    assert len(ctx._evidence_events) == 0


def test_assert_page_state_accepts_legal_clause_id():
    ctx = _make_ctx(CONTRACT)
    set_tool_context(ctx)
    out = verify_tools.assert_page_state.invoke(
        {"package": "com.example", "verification_key": "v1", "clause_id": "v1.0"}
    )
    assert "PASS" in out
    assert len(ctx._evidence_events) == 1
    assert ctx._evidence_events[0]["clause_id"] == "v1.0"
    assert ctx._evidence_events[0]["verification_key"] == "v1"


def test_assert_page_state_rejects_cross_key_mismatch():
    # v2.0 属于 v2，却用 v1 归因 → 跨 key 错配，应拒绝
    ctx = _make_ctx(CONTRACT)
    set_tool_context(ctx)
    out = verify_tools.assert_page_state.invoke(
        {"package": "com.example", "verification_key": "v1", "clause_id": "v2.0"}
    )
    assert "归因被拒" in out
    assert "跨 key 错配" in out
    assert len(ctx._evidence_events) == 0


def test_assert_behavior_effect_rejects_orphan_clause_id():
    ctx = _make_ctx(CONTRACT)
    set_tool_context(ctx)
    out = verify_tools.assert_behavior_effect.invoke(
        {"expected": "element_present(添加)", "verification_key": "v1", "clause_id": "v9.9"}
    )
    assert "归因被拒" in out
    assert "v9.9" in out
    assert len(ctx._evidence_events) == 0


def test_assert_behavior_effect_accepts_legal_clause_id():
    ctx = _make_ctx(CONTRACT)
    set_tool_context(ctx)
    out = verify_tools.assert_behavior_effect.invoke(
        {"expected": "element_present(添加)", "verification_key": "v1", "clause_id": "v1.0"}
    )
    assert "PASS" in out
    assert len(ctx._evidence_events) == 1
    assert ctx._evidence_events[0]["clause_id"] == "v1.0"


def test_assert_page_contains_rejects_orphan_and_returns_hint():
    # 🟡1 闭环：最常用的 assert 工具也要把孤儿提示回传给 LLM
    ctx = _make_ctx(CONTRACT)
    set_tool_context(ctx)
    out = verify_tools.assert_page_contains.invoke(
        {"text": "添加", "verification_key": "v1", "clause_id": "v2.1"}
    )
    assert "PASS" in out  # 查询本身命中
    assert "归因被拒" in out  # 但归因提示被带回
    assert "v2.1" in out
    assert len(ctx._evidence_events) == 0


def test_assert_element_exists_rejects_orphan_and_returns_hint():
    # 🟡1 闭环：assert_element_exists 是 trace 里误填孤儿 id 的工具，必须回传提示。
    # orphan 时预校验拦截，不查设备、不写证据，返回 FAIL + 归因被拒（提示带回，agent 可自纠）。
    ctx = _make_ctx(CONTRACT)
    set_tool_context(ctx)
    out = verify_tools.assert_element_exists.invoke(
        {"label": "添加", "verification_key": "v1", "clause_id": "v2.1"}
    )
    assert "FAIL" in out  # 孤儿归因被拒，查询未完成
    assert "归因被拒" in out  # 提示必须回传给 LLM
    assert "v2.1" in out
    assert len(ctx._evidence_events) == 0

"""Checklist（C1/C2）单测：实时勾选清单渲染 + 内层 _llm 注入 SystemMessage。

覆盖 Plan §4 Phase A 的 C1（_render_checklist_view）与 C2（_build_checklist_message /
_llm 节点注入）。M4a 相关测试在 tests/test_m4a_auto_evidence.py（后续里程碑）。
"""

from types import SimpleNamespace

from agents.verification import _render_checklist_view
from agents.llm_runtime import _build_checklist_message


# ── C1：_render_checklist_view 渲染 ──────────────────────────────────────

def _sample_clause_state():
    return {
        "verdict": "inconclusive",
        "verifications": [
            {
                "key": "v0",
                "result": "passed",
                "clauses": [
                    {
                        "id": "v0.0",
                        "claim": "默认每节课45分钟",
                        "status": "passed",
                        "evidence_count": 1,
                        "unverified": False,
                    }
                ],
            },
            {
                "key": "v1",
                "result": "failed",
                "clauses": [
                    {
                        "id": "v1.0",
                        "claim": "第1节08:00-08:50",
                        "status": "failed",
                        "evidence_count": 1,
                        "unverified": False,
                    }
                ],
            },
            {
                "key": "v2",
                "result": "unknown",
                "clauses": [
                    {
                        "id": "v2.0",
                        "claim": "可添加至10节",
                        "status": "unknown",
                        "evidence_count": 0,
                        "unverified": True,
                    }
                ],
            },
        ],
    }


def _sample_contract():
    return {
        "status": "approved",
        "verifications": [
            {
                "key": "v0",
                "clauses": [
                    {"id": "v0.0", "claim": "默认每节课45分钟", "channels": ["ui_text"]}
                ],
            },
            {
                "key": "v1",
                "clauses": [
                    {"id": "v1.0", "claim": "第1节08:00-08:50", "channels": ["ui_text"]}
                ],
            },
            {
                "key": "v2",
                "clauses": [
                    {
                        "id": "v2.0",
                        "claim": "可添加至10节",
                        "channels": ["behavior_effect"],
                    }
                ],
            },
        ],
    }


def _sample_evidence():
    return [
        {
            "verification_key": "v0",
            "clause_id": "v0.0",
            "status": "PASS",
            "channel": "ui_text",
            "artifact_ref": "screenshots/r1/evidence_v0_2.png",
        },
        {
            "verification_key": "v1",
            "clause_id": "v1.0",
            "status": "FAIL",
            "channel": "ui_text",
            "artifact_ref": "screenshots/r1/evidence_v1_1.png",
        },
    ]


def test_render_checklist_view_marks_status_and_evidence():
    out = _render_checklist_view(
        _sample_clause_state(), _sample_contract(), _sample_evidence()
    )
    assert "[✓] v0::v0.0 默认每节课45分钟" in out
    assert "evidence_v0_2.png" in out
    assert "[✗] v1::v1.0 第1节08:00-08:50" in out
    assert "evidence_v1_1.png" in out
    assert "[○] v2::v2.0 可添加至10节" in out
    # 硬规则必须出现
    assert "禁止再 open 编辑器微调或重复 assert" in out
    # 通道展示（v2 是 behavior_effect）
    assert "通道: behavior_effect" in out


def test_render_checklist_view_empty_when_no_clause_state():
    assert _render_checklist_view(None, _sample_contract()) == ""
    assert _render_checklist_view({"verifications": []}, _sample_contract()) == ""


def test_render_checklist_view_no_evidence_shows_channel():
    # v0 无 evidence 时退化为通道展示
    out = _render_checklist_view(_sample_clause_state(), _sample_contract(), [])
    assert "[✓] v0::v0.0" in out
    assert "通道: ui_text" in out


# ── C2：_build_checklist_message 注入 ────────────────────────────────────

def test_build_checklist_message_injects_with_real_ctx():
    fake_ctx = SimpleNamespace(
        _clause_state=_sample_clause_state(),
        _verification_contract=_sample_contract(),
        _evidence_events=_sample_evidence(),
    )
    out = _build_checklist_message(fake_ctx)
    assert "[✓]" in out and "[✗]" in out and "[○]" in out
    assert "evidence_v0_2.png" in out


def test_build_checklist_message_returns_empty_without_clause_state():
    fake_ctx = SimpleNamespace(
        _clause_state={},
        _verification_contract=_sample_contract(),
        _evidence_events=[],
    )
    assert _build_checklist_message(fake_ctx) == ""


def test_build_checklist_message_never_raises_on_bad_ctx():
    # 即使 _ctx 缺属性也应安全返回 ""，不抛异常（避免阻断主流程）
    class _BadCtx:
        def __getattr__(self, name):
            raise RuntimeError("boom")

    assert _build_checklist_message(_BadCtx()) == ""

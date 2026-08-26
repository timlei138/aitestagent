"""_clause_evidence_hints 单测（用例 168 回归：不可满足 clause 的诚实兜底）。

背景：planner 曾为「图库导入入口可点击」拆出系统给不出权威 PASS 的 clause，
agent 3 次补证无果仍被待验证清单驳回 DONE，从目标页被逼导航回起点直至取消。
本组测试锁住：≥3 次证据尝试仍 unknown 的 clause 从待验证清单释放（不再驳回
DONE），零证据/少证据 unknown 仍在清单内（F1 漏验语义不变）。
"""

from __future__ import annotations

from agents.verification import _clause_evidence_hints

_KEY_TO_ITEM = {"v0": "可打开课程表页面", "v1": "图库导入入口可点击"}


def _entry(key: str, result: str, clauses: list[dict]) -> dict:
    return {"key": key, "result": result, "clauses": clauses}


def _clause(cid: str, status: str, evidence_count: int = 0) -> dict:
    return {
        "id": cid,
        "claim": f"clause-{cid}",
        "status": status,
        "evidence_count": evidence_count,
        "channels": ["page_state"],
    }


def test_unknown_with_three_attempts_released_from_pending():
    merged = [
        _entry("v0", "passed", [_clause("v0.0", "passed", 1)]),
        _entry(
            "v1",
            "unknown",
            [
                _clause("v1.0", "passed", 1),
                # 用例 168 的 v1.1：3 条匹配证据（含 disabled FAIL）仍 unknown
                _clause("v1.1", "unknown", evidence_count=3),
            ],
        ),
    ]
    passed, pending = _clause_evidence_hints(merged, _KEY_TO_ITEM)
    assert any("v0::v0.0" in t for t in passed)
    # 已通过 clause 即使所在整项为 unknown 也列入「勿重复验证」（原实现漏列 v1.0）
    assert any("v1::v1.0" in t for t in passed)
    assert pending == []  # 尽力不可证 → 不再驳回 DONE


def test_zero_and_low_evidence_unknown_stay_pending():
    merged = [
        _entry(
            "v1",
            "unknown",
            [
                _clause("v1.0", "unknown", evidence_count=0),  # 零证据漏验候选
                _clause("v1.1", "unknown", evidence_count=2),  # 还有余量
            ],
        )
    ]
    _, pending = _clause_evidence_hints(merged, _KEY_TO_ITEM)
    assert len(pending) == 2
    assert any("v1::v1.0" in t for t in pending)


def test_failed_entry_clauses_never_pending():
    merged = [
        _entry(
            "v0",
            "failed",
            [_clause("v0.0", "failed", 1), _clause("v0.1", "unknown", 1)],
        )
    ]
    passed, pending = _clause_evidence_hints(merged, _KEY_TO_ITEM)
    assert passed == []
    assert pending == []  # 确定性矛盾走 authoritative FAIL 终止，不进待验证清单


def test_tag_carries_clause_id_and_channel():
    merged = [_entry("v0", "unknown", [_clause("v0.0", "unknown", 0)])]
    _, pending = _clause_evidence_hints(merged, _KEY_TO_ITEM)
    assert pending == ["[v0::v0.0 | channels:page_state] clause-v0.0"]

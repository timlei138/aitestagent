from __future__ import annotations

from agents.verification import (
    _default_channels_for_claim,
    build_coverage_map,
    build_verification_contract,
    evaluate_verification,
    validate_contract_spans,
)
from tools.verify import (
    _record_deterministic_check,
    assert_behavior_effect,
    assert_page_state,
)

CONTRACT = {
    "verifications": [
        {
            "key": "v0",
            "clauses": [
                {"id": "v0.red", "claim": "time is red", "channels": ["vision_verify"]},
                {
                    "id": "v0.saved",
                    "claim": "save is blocked",
                    "channels": ["behavior_effect"],
                },
            ],
        }
    ]
}


def test_contract_requires_evidence_for_every_clause():
    result = evaluate_verification(
        CONTRACT,
        [
            {
                "verification_key": "v0",
                "clause_id": "v0.red",
                "channel": "vision_verify",
                "status": "YES",
            }
        ],
    )

    assert result["verdict"] == "inconclusive"
    assert result["verifications"][0]["clauses"][1]["status"] == "unknown"


def test_contract_ignores_undeclared_or_free_text_evidence():
    result = evaluate_verification(
        CONTRACT,
        [
            {
                "verification_key": "v0",
                "clause_id": "v0.red",
                "channel": "agent_assertion",
                "status": "PASS",
            }
        ],
    )

    assert result["verdict"] == "inconclusive"


def test_default_channels_for_switch_state_claims():
    """状态/开关类 claim 优先用行为 + 视觉验证，避免 element_state/page_state。

    element_state/page_state 依赖 Accessibility ``checked`` 属性，在不同 ROM 上不可靠，
    且容易诱导 agent 反复开关同一个控件去「补证据」。因此 state claim 的默认通道只保留
    behavior_effect（操作是否产生预期结果）和 vision_verify（视觉状态）。
    """
    switch_claims = [
        "Wi-Fi开关可正常打开",
        "开关是开启的",
        "勾选用户协议",
        "选中第一个选项",
        "按钮状态为关闭",
        "WLAN 打开后状态变为开启",
    ]
    for claim in switch_claims:
        channels = _default_channels_for_claim(claim)
        assert "vision_verify" in channels, f"{claim!r} 缺少 vision_verify"
        assert "behavior_effect" in channels, f"{claim!r} 缺少 behavior_effect"
        assert "element_state" not in channels, f"{claim!r} 不应包含 element_state"
        assert "page_state" not in channels, f"{claim!r} 不应包含 page_state"


def test_default_channels_for_text_claims():
    channels = _default_channels_for_claim("页面提示文字为保存成功")
    assert "ui_text" in channels
    assert "click_and_check" in channels
    assert "vision_verify" not in channels


def test_default_fallback_channels_have_producers():
    """完全无 marker 命中的 claim，fallback 只能包含有生产者的通道。"""
    channels = _default_channels_for_claim("something totally unknown xyz")
    assert set(channels) == {"ui_text", "vision_verify", "click_and_check", "behavior_effect"}
    assert "element_state" not in channels
    assert "page_state" not in channels


def test_contract_authoritative_failure_is_failed():
    result = evaluate_verification(
        CONTRACT,
        [
            {
                "verification_key": "v0",
                "clause_id": "v0.red",
                "channel": "vision_verify",
                "status": "NO",
                "authoritative": True,
            }
        ],
    )

    assert result["verdict"] == "failed"
    assert result["terminated_on_authoritative_failure"] is True
    assert result["failed_clauses"] == [
        {"verification_key": "v0", "clause_id": "v0.red"}
    ]
    assert result["pending_clauses"] == [
        {"verification_key": "v0", "clause_id": "v0.saved"}
    ]


def test_deterministic_check_needs_explicit_clause_association(monkeypatch):
    class Context:
        _deterministic_checks: list[dict] = []
        _evidence_events: list[dict] = []

    context = Context()
    monkeypatch.setattr("tools.verify.get_tool_context", lambda: context)

    _record_deterministic_check("red", "page_contains", True)
    _record_deterministic_check("red", "page_contains", True, "v0", "v0.red", "ui_text")

    assert len(context._evidence_events) == 1
    assert context._evidence_events[0]["authoritative"] is False


def test_generated_contract_requires_review_before_execution():
    request = "set a conflict and verify time is red"
    contract = build_verification_contract({"verification": ["time is red"]}, request)

    assert contract["status"] == "contract_pending_review"
    assert contract["verifications"][0]["request_source_span"] == [0, len(request)]
    assert contract["verifications"][0]["clauses"][0]["channels"]


def test_visual_claim_requires_vision_evidence():
    contract = build_verification_contract({"verification": ["时间文本为红色"]})
    clause = contract["verifications"][0]["clauses"][0]
    assert clause["channels"] == ["vision_verify"]

    result = evaluate_verification(
        {**contract, "status": "approved"},
        [
            {
                "verification_key": "v0",
                "clause_id": "v0.0",
                "channel": "ui_text",
                "status": "PASS",
            }
        ],
    )
    assert result["verdict"] == "inconclusive"


def test_text_claim_excludes_vision_only_evidence():
    contract = build_verification_contract({"verification": ["页面显示保存成功提示"]})
    clause = contract["verifications"][0]["clauses"][0]
    assert "vision_verify" not in clause["channels"]


def test_composite_claim_requires_each_subclaim_evidence():
    contract = build_verification_contract(
        {"verification": ["小节数和时间显示为红色，点击完成后toast提示时间冲突"]}
    )
    clauses = contract["verifications"][0]["clauses"]
    assert len(clauses) == 2
    assert clauses[0]["channels"] == ["vision_verify"]
    assert "click_and_check" in clauses[1]["channels"]

    result = evaluate_verification(
        {**contract, "status": "approved"},
        [
            {
                "verification_key": "v0",
                "clause_id": "v0.0",
                "channel": "vision_verify",
                "status": "YES",
            }
        ],
    )
    assert result["verdict"] == "inconclusive"
    assert result["pending_clauses"] == [
        {"verification_key": "v0", "clause_id": "v0.1"}
    ]


def test_evidence_evaluation_becomes_terminal_when_all_clauses_pass():
    result = evaluate_verification(
        CONTRACT,
        [
            {
                "verification_key": "v0",
                "clause_id": "v0.red",
                "channel": "vision_verify",
                "status": "YES",
            },
            {
                "verification_key": "v0",
                "clause_id": "v0.saved",
                "channel": "behavior_effect",
                "status": "PASS",
            },
        ],
    )

    assert result["verdict"] == "passed"


def test_build_coverage_map_structure():
    request = "set a conflict and verify time is red"
    contract = build_verification_contract(
        {"verification": ["time is red，save is blocked"]}, request
    )
    coverage = contract.get("coverage_map", {})
    assert "request" in coverage
    assert "goals" in coverage
    assert coverage["request"]["goal_spans"]["v0"] == [0, len(request)]
    goal_coverage = coverage["goals"]["v0"]
    assert goal_coverage["goal_source_span"] == [0, len("time is red，save is blocked")]
    assert len(goal_coverage["clause_spans"]) == 2
    assert goal_coverage["clause_spans"][0]["id"] == "v0.0"
    assert goal_coverage["clause_spans"][1]["id"] == "v0.1"


def test_validate_contract_spans_passes_for_multiple_verifications():
    contract = build_verification_contract(
        {"verification": ["time is red", "save is blocked"]},
        "set conflict and verify time is red and save is blocked",
    )
    assert len(contract["verifications"]) == 2
    result = validate_contract_spans(contract)
    assert result["valid"] is True, result
    assert result["overlaps"] == []
    assert result["gaps"] == []


def test_validate_contract_spans_detects_gap():
    contract = build_verification_contract(
        {"verification": ["time is red，save is blocked"]},
        "verify behavior",
    )
    # Artificially remove the second clause to create a gap.
    contract["verifications"][0]["clauses"] = [contract["verifications"][0]["clauses"][0]]
    contract["coverage_map"] = build_coverage_map(contract)
    result = validate_contract_spans(contract)
    assert result["valid"] is False
    assert any(g["layer"] == "goal" and g["reason"] == "clause coverage gap" for g in result["gaps"])


def test_validate_contract_spans_detects_overlap():
    contract = build_verification_contract(
        {"verification": ["time is red，save is blocked"]},
        "verify behavior",
    )
    # Artificially expand the first clause to overlap the second.
    first = contract["verifications"][0]["clauses"][0]
    second = contract["verifications"][0]["clauses"][1]
    first["goal_source_span"] = [0, second["goal_source_span"][1] - 1]
    contract["coverage_map"] = build_coverage_map(contract)
    result = validate_contract_spans(contract)
    assert result["valid"] is False
    assert len(result["overlaps"]) > 0


def test_validate_contract_spans_allows_context_spans():
    contract = build_verification_contract(
        {"verification": ["time is red，save is blocked"]}, "check time"
    )
    # Mark the second half of the goal statement as context; the first clause
    # does not cover the connector punctuation that follows it, so a gap remains.
    contract["verifications"][0]["context_spans"] = [[12, len("time is red，save is blocked")]]
    contract["coverage_map"] = build_coverage_map(contract)
    result = validate_contract_spans(contract)
    assert result["valid"] is False
    assert any(g["start"] == 11 for g in result["gaps"])


class FakeElement:
    def __init__(self, rid, label, checked=None, enabled=None):
        self.resource_id = rid
        self.label = label
        self.checked = checked
        self.enabled = enabled


class FakePerceiver:
    def __init__(self, elements):
        self._elements = elements

    def perceive(self):
        class Understanding:
            pass
        u = Understanding()
        u.elements = self._elements
        return u


class FakeDevice:
    def current_app(self):
        return {}


def _make_context_with_elements(elements, monkeypatch):
    class Context:
        _evidence_events: list[dict] = []
        _deterministic_checks: list[dict] = []
        perceiver = FakePerceiver(elements)
        device = FakeDevice()

    context = Context()
    monkeypatch.setattr("tools.verify.get_tool_context", lambda: context)
    return context


def test_assert_behavior_effect_unsupported_predicate_lists_supported(monkeypatch):
    """不支持的谓词报错应列出所有支持的谓词，避免 Agent 盲猜。"""
    _make_context_with_elements([], monkeypatch)

    result = assert_behavior_effect.invoke(
        {
            "expected": "wifi_switch_on()",
            "verification_key": "v1",
            "clause_id": "v1.0",
        }
    )
    assert result.startswith("ERROR")
    assert "supported predicates" in result.lower()
    assert "toggled" in result
    assert "element_state" not in result


def test_assert_behavior_effect_toggled_on_passes_when_switch_is_on(monkeypatch):
    """toggled(WLAN,on) 应在开关已切到 on 时 PASS。"""
    context = _make_context_with_elements(
        [FakeElement("rid_wifi", "WLAN", checked=True)], monkeypatch
    )

    result = assert_behavior_effect.invoke(
        {
            "expected": "toggled(WLAN,on)",
            "verification_key": "v0",
            "clause_id": "v0.0",
        }
    )
    assert result.startswith("PASS"), result


def test_assert_behavior_effect_toggled_on_fails_when_switch_still_off(monkeypatch):
    """toggled(WLAN,on) 在开关仍未切到 on（点击未生效）时应 FAIL，暴露真实失败。"""
    context = _make_context_with_elements(
        [FakeElement("rid_wifi", "WLAN", checked=False)], monkeypatch
    )

    result = assert_behavior_effect.invoke(
        {
            "expected": "toggled(WLAN,on)",
            "verification_key": "v0",
            "clause_id": "v0.0",
        }
    )
    assert result.startswith("FAIL"), result


def test_assert_behavior_effect_toggled_off_passes(monkeypatch):
    """toggled(WLAN,off) 在开关为 off 时应 PASS。"""
    context = _make_context_with_elements(
        [FakeElement("rid_wifi", "WLAN", checked=False)], monkeypatch
    )

    result = assert_behavior_effect.invoke(
        {
            "expected": "toggled(WLAN,off)",
            "verification_key": "v0",
            "clause_id": "v0.0",
        }
    )
    assert result.startswith("PASS"), result


def test_assert_behavior_effect_toggled_emits_behavior_effect_channel(monkeypatch):
    """toggled() 产出 behavior_effect 通道证据（状态类 claim 的默认通道），而非 element_state。"""
    context = _make_context_with_elements(
        [FakeElement("rid_wifi", "WLAN", checked=True)], monkeypatch
    )

    assert_behavior_effect.invoke(
        {
            "expected": "toggled(WLAN,on)",
            "verification_key": "v0",
            "clause_id": "v0.0",
        }
    )
    assert len(context._evidence_events) == 1
    assert context._evidence_events[0]["channel"] == "behavior_effect"
    assert context._evidence_events[0]["authoritative"] is True


def test_assert_page_state_matches_simple_activity_name(monkeypatch):
    """agent 传入短 Activity 名时应匹配设备返回的完整内部类名。"""

    class FakePerceiverWithActivity:
        def perceive(self):
            class U:
                pass
            u = U()
            u.elements = []
            return u

    class FakeDeviceWithActivity:
        def current_app(self):
            return {"package": "com.android.settings", "activity": ".Settings$WifiSettingsActivity"}

    class Context:
        _evidence_events: list[dict] = []
        _deterministic_checks: list[dict] = []
        perceiver = FakePerceiverWithActivity()
        device = FakeDeviceWithActivity()

    context = Context()
    monkeypatch.setattr("tools.verify.get_tool_context", lambda: context)

    result = assert_page_state.invoke(
        {
            "package": "com.android.settings",
            "activity": "WifiSettingsActivity",
            "verification_key": "v0",
            "clause_id": "v0.0",
        }
    )
    assert result.startswith("PASS"), result
    assert len(context._evidence_events) == 1
    assert context._evidence_events[0]["status"] == "PASS"


def test_assert_page_state_exact_match_still_works(monkeypatch):
    """完整 Activity 名精确匹配仍应通过。"""

    class FakePerceiverWithActivity:
        def perceive(self):
            class U:
                pass
            u = U()
            u.elements = []
            return u

    class FakeDeviceWithActivity:
        def current_app(self):
            return {"package": "com.android.settings", "activity": ".Settings$WifiSettingsActivity"}

    class Context:
        _evidence_events: list[dict] = []
        _deterministic_checks: list[dict] = []
        perceiver = FakePerceiverWithActivity()
        device = FakeDeviceWithActivity()

    context = Context()
    monkeypatch.setattr("tools.verify.get_tool_context", lambda: context)

    result = assert_page_state.invoke(
        {
            "package": "com.android.settings",
            "activity": ".Settings$WifiSettingsActivity",
            "verification_key": "v0",
            "clause_id": "v0.0",
        }
    )
    assert result.startswith("PASS"), result


def test_assert_behavior_effect_still_on_activity_matches_simple_name(monkeypatch):
    """still_on_activity 谓词同样支持短 Activity 名匹配。"""

    class FakePerceiverWithActivity:
        def perceive(self):
            class U:
                pass
            u = U()
            u.elements = []
            return u

    class FakeDeviceWithActivity:
        def current_app(self):
            return {"package": "com.android.settings", "activity": ".Settings$WifiSettingsActivity"}

    class Context:
        _evidence_events: list[dict] = []
        _deterministic_checks: list[dict] = []
        perceiver = FakePerceiverWithActivity()
        device = FakeDeviceWithActivity()

    context = Context()
    monkeypatch.setattr("tools.verify.get_tool_context", lambda: context)

    result = assert_behavior_effect.invoke(
        {
            "expected": "still_on_activity(WifiSettingsActivity)",
            "verification_key": "v0",
            "clause_id": "v0.0",
        }
    )
    assert result.startswith("PASS"), result

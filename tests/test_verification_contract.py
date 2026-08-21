from __future__ import annotations

from agents.verification import (
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


def test_contract_marks_zero_evidence_unknown_as_unverified():
    """Plan §7 增强：agent 仅验证部分 clause 时，零证据 unknown clause 应标记
    unverified=True（暴露幻觉式漏验，如口头声称 v0-v4 已通过却零工具证据）。"""
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
    clauses = {c["id"]: c for c in result["verifications"][0]["clauses"]}
    # v0.red 有证据 → 非 unverified
    assert clauses["v0.red"]["status"] == "passed"
    assert clauses["v0.red"].get("unverified") is not True
    # v0.saved 零证据 → unknown + unverified
    assert clauses["v0.saved"]["status"] == "unknown"
    assert clauses["v0.saved"]["unverified"] is True


def test_contract_unverified_false_when_evidence_present_but_undecided():
    """对照：clause 有匹配通道的证据事件，但状态为 NO（非 YES/PASS）→ 仍判
    unknown，然而 evidence_count>0 → unverified=False。以此区分「验证过但证据
    不足/未决」与「从未调用验证工具（零证据）」两类 unknown。"""
    result = evaluate_verification(
        CONTRACT,
        [
            {
                # behavior_effect 通道匹配，但状态为 NO（未决）→ unknown 但非漏验
                "verification_key": "v0",
                "clause_id": "v0.saved",
                "channel": "behavior_effect",
                "status": "NO",
            }
        ],
    )
    clause = result["verifications"][0]["clauses"][1]
    assert clause["status"] == "unknown"
    assert clause["evidence_count"] > 0
    assert clause["unverified"] is False


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


def test_contract_clauses_use_all_channel_fallback():
    """M1-3/M1-4: 删除关键词通道推断后，clause 的 channels 恒为全通道回退
    （含 behavior_effect），由 authoritative 标志承接确定性判定（Plan §6 要点4）。
    """
    contract = build_verification_contract({"verification": ["页面显示保存成功提示"]})
    clause = contract["verifications"][0]["clauses"][0]
    assert set(clause["channels"]) == {
        "page_state",
        "element_state",
        "behavior_effect",
        "ui_text",
        "vision_verify",
        "click_and_check",
    }


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


def test_authoritative_failure_beats_positive_v5_scenario():
    """M1-1 盲区（根治 v5）：当某 clause 同时存在 authoritative FAIL 与非权威 PASS 证据时，
    判定必须为 failed——不得让非权威 PASS 压过 authoritative FAIL。

    对应真实场景：期望"完成"按钮置灰（disabled），实际 enabled=True 且点击弹 Toast
    （click_and_check PASS 合理化通过），但元素 enabled=True 是确定性矛盾（authoritative FAIL）。
    """
    result = evaluate_verification(
        CONTRACT,
        [
            # 非权威 PASS：click_and_check 认为点击有反应（Toast 拦截）
            {
                "verification_key": "v0",
                "clause_id": "v0.red",
                "channel": "click_and_check",
                "status": "PASS",
            },
            # authoritative FAIL：元素 enabled=True 与期望 disabled=True 矛盾
            {
                "verification_key": "v0",
                "clause_id": "v0.red",
                "channel": "behavior_effect",
                "status": "NO",
                "authoritative": True,
            },
        ],
    )

    assert result["verdict"] == "failed"
    assert result["terminated_on_authoritative_failure"] is True
    assert {"verification_key": "v0", "clause_id": "v0.red"} in result["failed_clauses"]


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


def test_visual_claim_uses_all_channel_fallback():
    """M1-3/M1-4: visual claim 不再特例化为 vision-only 通道，
    统一走全通道回退（Plan §6 要点4）。"""
    contract = build_verification_contract({"verification": ["时间文本为红色"]})
    clause = contract["verifications"][0]["clauses"][0]
    assert set(clause["channels"]) == {
        "page_state",
        "element_state",
        "behavior_effect",
        "ui_text",
        "vision_verify",
        "click_and_check",
    }

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
    # M1-4 全通道回退：ui_text 现在属于 clause 通道，PASS 即 passed
    # （不再像原视觉特例那样排除 ui_text 导致 inconclusive）。
    assert result["verdict"] == "passed"


def test_text_claim_uses_all_channel_fallback():
    """M1-3/M1-4: text claim 同样走全通道回退，不再排除 vision_verify。"""
    contract = build_verification_contract({"verification": ["页面显示保存成功提示"]})
    clause = contract["verifications"][0]["clauses"][0]
    assert "vision_verify" in clause["channels"]


def test_composite_claim_requires_each_subclaim_evidence():
    contract = build_verification_contract(
        {"verification": ["小节数和时间显示为红色，点击完成后toast提示时间冲突"]}
    )
    clauses = contract["verifications"][0]["clauses"]
    assert len(clauses) == 2
    # M1-3/M1-4: 各子句统一走全通道回退
    assert set(clauses[0]["channels"]) == {
        "page_state",
        "element_state",
        "behavior_effect",
        "ui_text",
        "vision_verify",
        "click_and_check",
    }
    assert set(clauses[1]["channels"]) == set(clauses[0]["channels"])

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


def test_assert_behavior_effect_toggled_bad_format_gives_example(monkeypatch):
    """P0 第 2 点补强：以 toggled 开头但格式不对（裸 toggled / 缺 on|off）时，
    返回完整格式示例（toggled(label,on|off)），而非只走通用 unsupported 报错。
    防止换了模型又把裸 toggled 传进来。"""
    _make_context_with_elements([], monkeypatch)

    for bad in ("toggled", "toggled(WLAN)", "toggled(WLAN,on,extra)", "toggled WLAN on"):
        result = assert_behavior_effect.invoke(
            {
                "expected": bad,
                "verification_key": "v1",
                "clause_id": "v1.0",
            }
        )
        assert result.startswith("ERROR"), f"{bad} 应报错: {result}"
        assert "toggled(label,on|off)" in result, f"{bad} 应给出完整格式示例: {result}"
        assert "例如" in result, f"{bad} 应含示例: {result}"


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
    """toggled() 产出 behavior_effect 通道证据（状态类 claim 的默认通道），而非 element_state。

    注意：toggled 读实时 checked，属当前态检查，其证据 authoritative=False（见 Plan §5.3.2
    权威不变量）——本测试只验证通道归属，不再断言 authoritative=True。
    """
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
    assert context._evidence_events[0]["authoritative"] is False


def test_assert_behavior_effect_without_clause_id_drops_evidence(monkeypatch):
    """回归：assert_behavior_effect 与 assert_page_contains 一样，必须使用方传入
    verification_key + clause_id 才会落证据（tools/verify.py:519 的 `if verification_key
    and clause_id` 守卫）。若 agent 像真实跑批里那样只传 `toggled(周末有课,on)` 而不带
    clause 身份，证据被静默丢弃 → 该条开关类 clause 收不到证据 → evaluator 判 unknown
    → 前端「未验证」。这正是某次跑批『未验证项特别多』的根因（UI 文本类 clause 正常，
    开关类 clause 全未验证）。修复在 prompt 侧（common/explore 已要求 behavior_effect
    也必须带 clause 身份）；本测试锁定该不变量：缺身份则不落证据。
    """
    context = _make_context_with_elements(
        [FakeElement("rid_wifi", "WLAN", checked=True)], monkeypatch
    )
    # 只传 expected，不传 verification_key / clause_id —— 复现真实跑批的缺身份调用
    assert_behavior_effect.invoke({"expected": "toggled(WLAN,on)"})
    assert len(context._evidence_events) == 0

    # 带上身份后必须落证据
    assert_behavior_effect.invoke(
        {"expected": "toggled(WLAN,on)", "verification_key": "v0", "clause_id": "v0.0"}
    )
    assert len(context._evidence_events) == 1
    assert context._evidence_events[0]["clause_id"] == "v0.0"


def test_assert_behavior_effect_disabled_fails_when_enabled_is_true(monkeypatch):
    """F2（决策反转，非纯 bug fix）：disabled(完成) 在元素 enabled=True（实际未置灰）
    时应 FAIL，但证据 authoritative=False——enabled=True 只证明「App 没调 setEnabled(
    false)」，推不出「用户可选中它」（不可选还能用 selected/自绘/OnClickListener 直接
    return/父容器拦截表达）。代码不替 LLM 下它证不了的结论，非权威 FAIL 让 clause 保持
    unknown 可重试，不再触发 llm_runtime 的 mid-batch break。

    代价（必须记账）：「真该置灰却没置灰」从 fail-fast 降级为 unknown + 靠 LLM 取证，
    这是有意反转。正向能力不丢：enabled=False 仍是权威 PASS（见下个测试）。
    """
    context = _make_context_with_elements(
        [FakeElement("rid_done", "完成", enabled=True)], monkeypatch
    )

    result = assert_behavior_effect.invoke(
        {
            "expected": "disabled(完成)",
            "verification_key": "v0",
            "clause_id": "v0.0",
        }
    )
    assert result.startswith("FAIL"), result
    assert len(context._evidence_events) == 1
    event = context._evidence_events[0]
    # _record_deterministic_check 将 FAIL 记为 "FAIL"（evaluate_verification 同时认 FAIL/NO）。
    assert event["status"] == "FAIL"
    assert event["authoritative"] is False
    assert event["channel"] == "behavior_effect"
    assert event["fact"]["enabled"] is True
    assert event["fact"]["expected_disabled"] is True


def test_assert_behavior_effect_disabled_passes_when_enabled_is_false(monkeypatch):
    """disabled(完成) 在元素 enabled=False（真实置灰）时应 PASS，且 authoritative=True。"""
    context = _make_context_with_elements(
        [FakeElement("rid_done", "完成", enabled=False)], monkeypatch
    )

    result = assert_behavior_effect.invoke(
        {
            "expected": "disabled(完成)",
            "verification_key": "v0",
            "clause_id": "v0.0",
        }
    )
    assert result.startswith("PASS"), result
    assert context._evidence_events[0]["authoritative"] is True
    assert context._evidence_events[0]["fact"]["enabled"] is False


def test_assert_behavior_effect_disabled_respects_only_enabled_not_checked(monkeypatch):
    """M1-5 nuance：disabled 只按 View.isEnabled() 判定，不混入 checked（避免过渡态抖动）。
    元素 enabled=False 但 checked=True 时仍判 PASS（置灰态下 checked 不可靠）。
    """
    context = _make_context_with_elements(
        [FakeElement("rid_done", "完成", checked=True, enabled=False)], monkeypatch
    )

    result = assert_behavior_effect.invoke(
        {
            "expected": "disabled(完成)",
            "verification_key": "v0",
            "clause_id": "v0.0",
        }
    )
    assert result.startswith("PASS"), result
    assert context._evidence_events[0]["authoritative"] is True


def test_assert_behavior_effect_disabled_missing_anchor_errors(monkeypatch):
    """disabled(label) 找不到锚点元素时应报错，而非误判。"""
    context = _make_context_with_elements([], monkeypatch)

    result = assert_behavior_effect.invoke(
        {
            "expected": "disabled(不存在的按钮)",
            "verification_key": "v0",
            "clause_id": "v0.0",
        }
    )
    assert result.startswith("ERROR"), result


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


# ---------------------------------------------------------------------------
# authoritative 语义回归（Plan P0 第 3 点 / §5.3.2 权威不变量）
#   当前态检查谓词（element_present/element_absent/toggled）FAIL → authoritative=False
#   (unknown, 可重试, 不触发 fail-fast)
#   确定性 before/after 谓词（still_on_activity/no_page_change/list_count_unchanged）FAIL
#   → authoritative=True（保留 §5.4 fail-fast 保障）
# ---------------------------------------------------------------------------


class FakeDeviceWithActivity:
    def current_app(self):
        return {"package": "com.android.settings", "activity": ".Settings$WifiSettingsActivity"}


def _context_with_activity(elements, monkeypatch):
    class FakePerceiverWithActivity:
        def perceive(self):
            class U:
                pass
            u = U()
            u.elements = elements
            return u

    class Context:
        _evidence_events: list[dict] = []
        _deterministic_checks: list[dict] = []
        perceiver = FakePerceiverWithActivity()
        device = FakeDeviceWithActivity()

    context = Context()
    monkeypatch.setattr("tools.verify.get_tool_context", lambda: context)
    return context


def test_behavior_effect_toggled_failure_is_non_authoritative(monkeypatch):
    """toggled(WLAN,on) 在开关仍为 off（过渡态）时 FAIL，但其 FAIL 必须 authoritative=False，
    不得触发 fail-fast（由 evaluate_verification 判 unknown）。"""
    context = _context_with_activity(
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
    assert context._evidence_events, "应写入 evidence event"
    ev = context._evidence_events[-1]
    assert ev["channel"] == "behavior_effect"
    assert ev["status"] == "FAIL"
    assert ev["authoritative"] is False, "toggled 是当前态检查，FAIL 必须非权威"


def test_behavior_effect_element_present_failure_is_non_authoritative(monkeypatch):
    """element_present(label) 缺失时 FAIL，必须 authoritative=False（当前态检查）。"""
    context = _make_context_with_elements([], monkeypatch)
    result = assert_behavior_effect.invoke(
        {
            "expected": "element_present(missing_label)",
            "verification_key": "v1",
            "clause_id": "v1.0",
        }
    )
    assert result.startswith("FAIL"), result
    ev = context._evidence_events[-1]
    assert ev["status"] == "FAIL"
    assert ev["authoritative"] is False, "element_present 是当前态检查，FAIL 必须非权威"


def test_behavior_effect_element_absent_failure_is_non_authoritative(monkeypatch):
    """element_absent(label) 仍存在时 FAIL，必须 authoritative=False（当前态检查）。"""
    context = _make_context_with_elements(
        [FakeElement("rid_x", "present_label")], monkeypatch
    )
    result = assert_behavior_effect.invoke(
        {
            "expected": "element_absent(present_label)",
            "verification_key": "v1",
            "clause_id": "v1.0",
        }
    )
    assert result.startswith("FAIL"), result
    ev = context._evidence_events[-1]
    assert ev["status"] == "FAIL"
    assert ev["authoritative"] is False, "element_absent 是当前态检查，FAIL 必须非权威"


def test_behavior_effect_still_on_activity_failure_is_authoritative(monkeypatch):
    """still_on_activity 在 Activity 不符时 FAIL，必须 authoritative=True（确定性 before/after）。"""
    context = _context_with_activity(
        [FakeElement("rid_wifi", "WLAN", checked=True)], monkeypatch
    )
    result = assert_behavior_effect.invoke(
        {
            "expected": "still_on_activity(OtherActivity)",
            "verification_key": "v0",
            "clause_id": "v0.0",
        }
    )
    assert result.startswith("FAIL"), result
    ev = context._evidence_events[-1]
    assert ev["status"] == "FAIL"
    assert ev["authoritative"] is True, "still_on_activity 是 before/after 比较，FAIL 必须权威"


def test_behavior_effect_list_count_unchanged_failure_is_authoritative(monkeypatch):
    """list_count_unchanged 在计数不符时 FAIL，必须 authoritative=True（确定性 before/after）。"""
    context = _make_context_with_elements(
        [FakeElement("rid", "Item A"), FakeElement("rid2", "Item B")], monkeypatch
    )
    result = assert_behavior_effect.invoke(
        {
            "expected": "list_count_unchanged(Item,5)",
            "verification_key": "v2",
            "clause_id": "v2.0",
        }
    )
    assert result.startswith("FAIL"), result
    ev = context._evidence_events[-1]
    assert ev["status"] == "FAIL"
    assert ev["authoritative"] is True, "list_count_unchanged 是 before/after 比较，FAIL 必须权威"


def test_behavior_effect_no_page_change_failure_is_authoritative(monkeypatch):
    """no_page_change 在 screen_signature 不符时 FAIL，必须 authoritative=True
    （确定性 before/after 比较，与 still_on_activity/list_count_unchanged 同属权威侧）。
    补齐权威侧三类谓词的单独回归。"""

    class _Perceiver:
        def perceive(self):
            return SimpleNamespace(elements=[])

        def screen_signature(self):
            return "sig_actual_abc"

    class FakeDevice:
        def current_app(self):
            return {
                "package": "com.android.settings",
                "activity": ".Settings",
            }

    class Context:
        _evidence_events: list[dict] = []
        _deterministic_checks: list[dict] = []
        device = FakeDevice()

    context = Context()
    context.perceiver = _Perceiver()
    monkeypatch.setattr("tools.verify.get_tool_context", lambda: context)

    result = assert_behavior_effect.invoke(
        {
            "expected": "no_page_change(sig_expected_xyz)",
            "verification_key": "v3",
            "clause_id": "v3.0",
        }
    )
    assert result.startswith("FAIL"), result
    ev = context._evidence_events[-1]
    assert ev["channel"] == "behavior_effect"
    assert ev["status"] == "FAIL"
    assert ev["authoritative"] is True, "no_page_change 是 before/after 比较，FAIL 必须权威"


# ---------------------------------------------------------------------------
# 集成级回归：过渡态 FAIL 经 evaluate_verification 后 verdict=unknown，
# 不触发 llm_runtime.py:649 的 fail-fast（对照 before/after FAIL → failed 会触发）。
# 直接用真实 evaluate_verification，不 mock，确保端到端契约闭环。
# ---------------------------------------------------------------------------


def _contract_with_behavior_clause(key="v0", clause_id="v0.0"):
    return {
        "status": "approved",
        "verifications": [
            {
                "key": key,
                "clauses": [
                    {
                        "id": clause_id,
                        "claim": "Wi-Fi 开关状态可切换",
                        "channels": ["behavior_effect"],
                    }
                ],
            }
        ],
    }


def test_toggled_transient_failure_is_unknown_not_failed(monkeypatch):
    """toggled 过渡态 FAIL（authoritative=False）→ evaluate_verification 判 inconclusive，
    而非 failed → llm_runtime.py:649 的 {passed, failed} 不命中 → 不触发 fail-fast。

    注：evaluate_verification 顶层 verdict 取值为 failed/passed/inconclusive；
    非权威 FAIL 使 clause status=unknown → verification result=unknown → verdict=inconclusive。
    """
    from agents.verification import evaluate_verification

    contract = _contract_with_behavior_clause()
    events = [
        {
            "verification_key": "v0",
            "clause_id": "v0.0",
            "channel": "behavior_effect",
            "status": "FAIL",
            "authoritative": False,  # toggled 当前态检查，非权威
            "fact": {"expected": "toggled(WLAN,on)", "checked": False},
        }
    ]
    state = evaluate_verification(contract, events)
    assert state["verdict"] == "inconclusive", (
        f"过渡态 FAIL 必须 inconclusive(可重试)，实际={state['verdict']}——"
        f"若变 failed 将误触发 fail-fast"
    )
    # 模拟 llm_runtime.py:649 的 break 条件：inconclusive 不命中 → 不 break
    assert state.get("verdict") not in {"passed", "failed"}


def test_before_after_failure_is_failed_triggers_failfast(monkeypatch):
    """对照：before/after 谓词 FAIL（authoritative=True）→ evaluate_verification 判 failed，
    会触发 llm_runtime.py:649 的 fail-fast（保留 §5.4 保障）。"""
    from agents.verification import evaluate_verification

    contract = _contract_with_behavior_clause()
    events = [
        {
            "verification_key": "v0",
            "clause_id": "v0.0",
            "channel": "behavior_effect",
            "status": "FAIL",
            "authoritative": True,  # still_on_activity / list_count_unchanged 等 before/after 谓词
            "fact": {"expected": "list_count_unchanged(Item,5)", "count": 2},
        }
    ]
    state = evaluate_verification(contract, events)
    assert state["verdict"] == "failed", (
        f"确定性 before/after FAIL 必须 failed(触发 fail-fast)，实际={state['verdict']}"
    )
    # 模拟 llm_runtime.py:649 的 break 条件：failed 命中 → 会 break
    assert state.get("verdict") in {"passed", "failed"}

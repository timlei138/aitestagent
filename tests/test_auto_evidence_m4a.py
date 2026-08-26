"""M4a 自动证据：_match_spec + auto_record_evidence + build_verification_contract(spec)。

覆盖 m4_auto_evidence_plan §10 验收：自动成证据、自动报差异、降级 spec:null、去重。
"""

from types import SimpleNamespace

from agents.verification import (
    _match_spec,
    auto_record_evidence,
    build_verification_contract,
)


def _make_element(label="", rid="", enabled=True, checked=None, text="", desc=""):
    return SimpleNamespace(
        label=label,
        resource_id=rid,
        text=text,
        content_desc=desc,
        enabled=enabled,
        checked=checked,
        clickable=True,
    )


def _make_u(activity="", title="", elements=None):
    return SimpleNamespace(activity=activity, page_title=title, elements=elements or [])


def _make_ctx(events=None):
    return SimpleNamespace(_evidence_events=events if events is not None else [])


# ── M2: _match_spec ─────────────────────────────────────────────────────

def test_match_page_is_pass():
    spec = {"predicate": "page_is", "target": "TimetableActivity"}
    r = _match_spec(spec, _make_u(), {"activity": "com.xxx.TimetableActivity"})
    assert r and r["status"] == "PASS" and r["authoritative"] is False


def test_match_page_is_unknown_when_not_arrived():
    spec = {"predicate": "page_is", "target": "TimetableActivity"}
    r = _match_spec(spec, _make_u(), {"activity": "com.xxx.LauncherActivity"})
    assert r is None  # 页面对不上不算矛盾


def test_match_page_is_unknown_when_no_app():
    spec = {"predicate": "page_is", "target": "TimetableActivity"}
    r = _match_spec(spec, _make_u(activity="com.xxx.TimetableActivity"), {})
    assert r is None  # current_app 缺 activity → 不误判


def test_match_element_exists_pass_and_absent_fail():
    u = _make_u(elements=[_make_element(rid="com.xxx:id/btn_add", label="加号")])
    r1 = _match_spec({"predicate": "element_exists", "target": "btn_add"}, u, {})
    assert r1 and r1["status"] == "PASS"
    # element_absent 找到 = 确定性矛盾
    r2 = _match_spec({"predicate": "element_absent", "target": "btn_add"}, u, {})
    assert r2 and r2["status"] == "FAIL" and r2["authoritative"] is True


def test_match_element_exists_unknown_when_missing():
    u = _make_u(elements=[])
    r = _match_spec({"predicate": "element_exists", "target": "btn_add"}, u, {})
    assert r is None  # 找不到 = unknown（非矛盾）


def test_match_element_disabled_contradiction():
    u = _make_u(elements=[_make_element(rid="btn_done", enabled=True)])
    r = _match_spec({"predicate": "element_disabled", "target": "btn_done"}, u, {})
    assert r and r["status"] == "FAIL" and r["authoritative"] is True


def test_match_element_enabled_pass():
    u = _make_u(elements=[_make_element(rid="btn_done", enabled=True)])
    r = _match_spec({"predicate": "element_enabled", "target": "btn_done"}, u, {})
    assert r and r["status"] == "PASS"


def test_match_element_checked_pass_and_fail():
    u = _make_u(elements=[_make_element(rid="sw", checked=True)])
    r_pass = _match_spec(
        {"predicate": "element_checked", "target": "sw", "expected": True}, u, {}
    )
    assert r_pass and r_pass["status"] == "PASS"
    r_fail = _match_spec(
        {"predicate": "element_checked", "target": "sw", "expected": False}, u, {}
    )
    # element_checked 读 isChecked()（实时 checked 过渡态），与 toggled 同源，
    # 历史已知部分 ROM 上抖动/误读 → FAIL 非权威，不触发 fail-fast。
    assert r_fail and r_fail["status"] == "FAIL" and r_fail["authoritative"] is False


def test_match_ambiguous_element_skipped():
    # 两个匹配元素 → 不唯一 → 不写（避免误判）
    u = _make_u(elements=[_make_element(rid="x"), _make_element(rid="x")])
    r = _match_spec({"predicate": "element_enabled", "target": "x"}, u, {})
    assert r is None


def test_match_list_count():
    u = _make_u(elements=[_make_element(rid="row") for _ in range(3)])
    r = _match_spec({"predicate": "list_count", "target": "row", "expected": 3}, u, {})
    assert r and r["status"] == "PASS"
    r2 = _match_spec({"predicate": "list_count", "target": "row", "expected": 5}, u, {})
    assert r2 and r2["status"] == "FAIL" and r2["authoritative"] is True


def test_match_unknown_predicate_returns_none():
    assert _match_spec({"predicate": "click_then_change", "target": "x"}, _make_u(), {}) is None


def test_match_page_contains_covers_associated_label():
    # 可见文本只在 label（associated_label 兜底链）上、text/desc 为空时，
    # page_contains 也要能命中——锁定 2026-08-24 run103147 漏配修复。
    u = _make_u(elements=[_make_element(label="课程名称", text="", desc="")])
    r = _match_spec({"predicate": "page_contains", "target": "课程名称"}, u, {})
    assert r and r["status"] == "PASS"


# ── M3: auto_record_evidence ────────────────────────────────────────────

def _contract_with_spec(predicate, target=None, expected=None):
    return {
        "verifications": [
            {
                "key": "v0",
                "clauses": [
                    {
                        "id": "v0.0",
                        "claim": "课程表页面成功打开",
                        "spec": (
                            {"predicate": predicate, "target": target, "expected": expected}
                            if predicate
                            else None
                        ),
                    }
                ],
            }
        ]
    }


def test_auto_record_writes_pass_evidence():
    ctx = _make_ctx()
    u = _make_u(activity="com.xxx.TimetableActivity")
    n = auto_record_evidence(
        ctx, _contract_with_spec("page_is", "TimetableActivity"), u,
        {"activity": "com.xxx.TimetableActivity"},
    )
    assert n == 1
    ev = ctx._evidence_events[0]
    assert ev["verification_key"] == "v0" and ev["clause_id"] == "v0.0"
    assert ev["status"] == "PASS" and ev["channel"] == "page_state" and ev["auto"] is True


def test_auto_record_skips_spec_null():
    ctx = _make_ctx()
    n = auto_record_evidence(ctx, _contract_with_spec(None), _make_u(), {})
    assert n == 0 and ctx._evidence_events == []


def test_auto_record_dedup_non_authoritative():
    ctx = _make_ctx()
    u = _make_u(activity="com.xxx.TimetableActivity")
    c = _contract_with_spec("page_is", "TimetableActivity")
    app = {"activity": "com.xxx.TimetableActivity"}
    n1 = auto_record_evidence(ctx, c, u, app)  # 第一次 perceive
    n2 = auto_record_evidence(ctx, c, u, app)  # 第二次 perceive 同页
    assert n1 == 1 and n2 == 0  # 去重：非权威证据不重复写


def test_auto_record_authoritative_fail_not_deduped():
    ctx = _make_ctx()
    u = _make_u(elements=[_make_element(rid="btn_done", enabled=True)])
    c = _contract_with_spec("element_disabled", "btn_done")
    n1 = auto_record_evidence(ctx, c, u, {})
    n2 = auto_record_evidence(ctx, c, u, {})  # 第二次仍应写入（触发 fail-fast）
    assert n1 == 1 and n2 == 1
    assert all(e["authoritative"] for e in ctx._evidence_events)


def test_auto_record_initializes_events_when_attr_missing():
    """ctx 尚无 _evidence_events 属性时不得静默跳过——首个 perceive 即应落证据。

    锁定 2026-08-24 初始化门 bug：该属性由首个手动 verify 才创建，此前若用
    hasattr 早退，之前所有 perceive 的自动匹配全部空转。
    """
    ctx = SimpleNamespace()  # 故意不带 _evidence_events
    u = _make_u(activity="com.xxx.TimetableActivity")
    n = auto_record_evidence(
        ctx, _contract_with_spec("page_is", "TimetableActivity"), u,
        {"activity": "com.xxx.TimetableActivity"},
    )
    assert n == 1
    assert ctx._evidence_events[0]["status"] == "PASS"


# ── M1: build_verification_contract 保留 spec ───────────────────────────

def test_build_contract_keeps_spec_from_object():
    goal = {
        "verification": [
            {
                "claim": "课程表页面成功打开",
                "spec": {"predicate": "page_is", "target": "TimetableActivity"},
            }
        ]
    }
    contract = build_verification_contract(goal)
    clause = contract["verifications"][0]["clauses"][0]
    assert clause["spec"] == {"predicate": "page_is", "target": "TimetableActivity"}


def test_build_contract_string_falls_back_spec_null():
    goal = {"verification": ["课程表页面成功打开"]}
    contract = build_verification_contract(goal)
    clause = contract["verifications"][0]["clauses"][0]
    assert clause["spec"] is None


# ── 埋点（plan §6 第3条）：hit / dedup_skip / none+原因码 ─────────────────

def test_match_none_reason_buckets():
    """None 归因分桶 + 同义改写线索（「课程名称」vs「课程名」应召回）。"""
    from agents.verification import _match_none_reason

    reason, hints = _match_none_reason(
        {"predicate": "foo_bar", "target": "x"}, _make_u(), {}
    )
    assert reason == "unsupported_predicate" and hints == []

    # element_not_found 属「控件缺失类」；fuzzy hint 应召回页面近似控件名
    u = _make_u(elements=[_make_element(label="课程名")])
    reason, hints = _match_none_reason(
        {"predicate": "element_exists", "target": "课程名称"}, u, {}
    )
    recalls = "".join(hints)
    assert reason == "element_not_found"
    assert "课程名" in recalls


def test_match_none_reason_missing_on_page_variants():
    from agents.verification import _match_none_reason

    # 多匹配 → element_ambiguous（拿不准不写）
    u = _make_u(elements=[_make_element(rid="row"), _make_element(rid="row")])
    reason, hints = _match_none_reason(
        {"predicate": "element_enabled", "target": "row"}, u, {}
    )
    assert reason == "element_ambiguous" and hints == []

    # page_is 未到达目标页 → page_not_arrived（控件缺失类）
    reason, hints = _match_none_reason(
        {"predicate": "page_is", "target": "EditActivity"},
        _make_u(),
        {"activity": "com.x.MainActivity"},
    )
    assert reason == "page_not_arrived" and hints == []

    # page_contains 文本不在页面上 → text_missing
    reason, hints = _match_none_reason(
        {"predicate": "page_contains", "target": "保存成功"}, _make_u(), {}
    )
    assert reason == "text_missing" and hints == []


def test_auto_record_logs_outcome_lines(caplog):
    """一次 auto_record_evidence 对每个 spec clause 恰打一行 outcome 日志。"""
    import logging

    caplog.set_level(logging.INFO, logger="agents.verification")

    ctx = _make_ctx()
    contract = _contract_with_spec("page_is", "TimetableActivity")
    app = {"activity": "com.xxx.TimetableActivity"}

    n1 = auto_record_evidence(ctx, contract, _make_u(), app)
    assert n1 == 1
    hit_lines = [r for r in caplog.records if "outcome=hit" in r.getMessage()]
    assert len(hit_lines) == 1

    caplog.clear()
    n2 = auto_record_evidence(ctx, contract, _make_u(), app)
    assert n2 == 0
    dedup_lines = [
        r for r in caplog.records if "outcome=dedup_skip" in r.getMessage()
    ]
    assert len(dedup_lines) == 1

    caplog.clear()
    miss_contract = _contract_with_spec("page_is", "NeverActivity")
    n3 = auto_record_evidence(ctx, miss_contract, _make_u(), app)
    assert n3 == 0
    none_lines = [r for r in caplog.records if "outcome=none" in r.getMessage()]
    assert len(none_lines) == 1
    assert "reason=page_not_arrived" in none_lines[0].getMessage()  # 向后兼容：字符串 → spec:null → 手动 verify

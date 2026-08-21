"""F1/F2/F3 回归：disabled(label) 锚点解析与权威性越界修复。

192037 run 误报根因：disabled("1") 子串命中 ScrollView 容器 label "10"
（"1" in "10"），而不是周 1 的 chip；读到容器 enabled=true → 误判 FAIL 且
authoritative=True → llm_runtime mid-batch break 杀掉整轮。

本文件覆盖：
1. 容器排除（F1）：ScrollView/GridLayout 被 is_container 排除，命中正确 chip。
2. 容器唯一命中 → not found，且不写证据。
3. 层内歧义 → ambiguous，不写证据。
4. 分层顺序：text 精确 > rid 叶子 > 子串。
5. F2 权威性：enabled=False→PASS+权威；enabled=True→FAIL+非权威，fact 含 selected/clickable。
6. F3 [SELECTED] 渲染：selected=True 的元素行含 [SELECTED]。

元素用 SimpleNamespace 显式给全字段，避免改动其他文件的公共 fixture（YAGNI）。
"""

from types import SimpleNamespace

import tools.verify as verify_tools
from tools.context import ToolContext, set_tool_context


def _el(label, **kw):
    """构造一个测试元素。is_container/selected/enabled/text 等按需显式给出。"""
    base = dict(
        label=label,
        text=label,
        resource_id="",
        class_name="android.widget.TextView",
        associated_label="",
        context_path="",
        role="list_entry",
        region="main_content",
        bounds=(0, 0, 10, 10),
        clickable=True,
        enabled=True,
        selected=False,
        checked=None,
        is_container=False,
        has_switch_child=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _make_ctx(elements):
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
                elements=list(elements),
            )

    ctx = ToolContext(device=_Device(), perceiver=_Perceiver())
    ctx._verification_contract = {
        "verifications": [{"key": "v5", "clauses": [{"id": "v5.3"}]}]
    }
    # 关键：必须挂真实 list，不能用 getattr(..., []) or []（空列表会被 or 换成新列表
    # 导致 append 丢失）—— agents/verification.py:892-897 注释已说明。
    ctx._evidence_events = []
    return ctx


# ---------------------------------------------------------------------------
# 1. 回归本 bug：容器排除 + 命中正确 chip
# ---------------------------------------------------------------------------
def test_disabled_excludes_container_and_hits_chip(monkeypatch):
    scroll = _el("10", class_name="android.widget.ScrollView", is_container=True,
                 text="", associated_label="10", resource_id="", enabled=True)
    chip1 = _el("1", bounds=(1006, 732, 1251, 842), resource_id="", selected=False)
    chip10 = _el("10", bounds=(10, 10, 20, 20), selected=True)
    ctx = _make_ctx([scroll, chip1, chip10])
    set_tool_context(ctx)

    result = verify_tools.assert_behavior_effect.invoke(
        {"expected": "disabled(1)", "verification_key": "v5", "clause_id": "v5.3"}
    )
    assert result.startswith("FAIL"), result  # chip1 无置灰 → FAIL，但非权威
    assert ctx._evidence_events[0]["fact"]["bounds"] == (1006, 732, 1251, 842)
    assert "ScrollView" not in str(ctx._evidence_events[0]["fact"])


# ---------------------------------------------------------------------------
# 2. 仅容器命中 → not found，且不写证据
# ---------------------------------------------------------------------------
def test_disabled_only_container_match_returns_not_found(monkeypatch):
    scroll = _el("10", class_name="android.widget.ScrollView", is_container=True,
                 text="", associated_label="10", resource_id="", enabled=True)
    ctx = _make_ctx([scroll])
    set_tool_context(ctx)

    result = verify_tools.assert_behavior_effect.invoke(
        {"expected": "disabled(1)", "verification_key": "v5", "clause_id": "v5.3"}
    )
    assert result.startswith("ERROR"), result
    assert "not found" in result
    assert len(ctx._evidence_events) == 0


# ---------------------------------------------------------------------------
# 3. 层内歧义 → ambiguous + 候选列表，且不写证据
# ---------------------------------------------------------------------------
def test_disabled_ambiguous_when_two_leaf_candidates(monkeypatch):
    a = _el("1", bounds=(1, 1, 2, 2), resource_id="foo_1")
    b = _el("1", bounds=(3, 3, 4, 4), resource_id="bar_1")
    ctx = _make_ctx([a, b])
    set_tool_context(ctx)

    result = verify_tools.assert_behavior_effect.invoke(
        {"expected": "disabled(1)", "verification_key": "v5", "clause_id": "v5.3"}
    )
    assert result.startswith("ERROR"), result
    assert "ambiguous" in result
    assert "foo_1" in result and "bar_1" in result
    assert len(ctx._evidence_events) == 0


# ---------------------------------------------------------------------------
# 4. 分层顺序：层 1(text/label) > 层 2(rid 叶子) > 层 3(子串)
#    本用例专门让层 2 真被命中（rid 叶子名 == 查询词），层 1 无竞争者。
# ---------------------------------------------------------------------------
def test_disabled_layer_priority_rid_over_substring(monkeypatch):
    # 层 2 命中：rid 叶子="完成"（label 非 "完成"，故层 1 不中）
    by_rid = _el("其他", bounds=(3, 3, 4, 4), resource_id="com.example:id/完成")
    # 层 3 命中：label 含 "完成"（子串）
    by_sub = _el("未完成", bounds=(5, 5, 6, 6))
    ctx = _make_ctx([by_rid, by_sub])
    set_tool_context(ctx)

    result = verify_tools.assert_behavior_effect.invoke(
        {"expected": "disabled(完成)", "verification_key": "v5", "clause_id": "v5.3"}
    )
    # 无层 1 候选 → 层 2 rid 叶子精确命中，不应落到层 3 子串
    assert ctx._evidence_events[0]["fact"]["bounds"] == (3, 3, 4, 4)


def test_disabled_layer_ambiguous_when_repeated_label_suppressed(monkeypatch):
    """F-4 承重场景：同页 3 个「删除」按钮，perceiver 把重复 label 抑制为 ""。
    旧实现/无 el.text 回退会静默 not found（误报元素不存在）；
    带 el.text 回退应得 ambiguous + 候选清单，准确且可行动。
    """
    del_a = _el("删除", bounds=(1, 1, 2, 2), suppress_label=True)
    del_b = _el("删除", bounds=(3, 3, 4, 4), suppress_label=True)
    del_c = _el("删除", bounds=(5, 5, 6, 6), suppress_label=True)
    # suppress_label 时 perceiver 让 label 返回 ""，但 text 仍保留
    for d in (del_a, del_b, del_c):
        d.label = ""
    ctx = _make_ctx([del_a, del_b, del_c])
    set_tool_context(ctx)

    result = verify_tools.assert_behavior_effect.invoke(
        {"expected": "disabled(删除)", "verification_key": "v5", "clause_id": "v5.3"}
    )
    assert result.startswith("ERROR"), result
    assert "ambiguous" in result
    # 应列全 3 个候选（不静默截断）
    assert "3 个候选" in result
    assert len(ctx._evidence_events) == 0


# ---------------------------------------------------------------------------
# 5. F2 权威性：enabled=False→PASS+权威；enabled=True→FAIL+非权威，fact 含 selected/clickable
# ---------------------------------------------------------------------------
def test_disabled_authority_by_enabled(monkeypatch):
    # enabled=False → PASS + authoritative=True
    ctx_off = _make_ctx([_el("完成", enabled=False, selected=False, clickable=False)])
    set_tool_context(ctx_off)
    res_off = verify_tools.assert_behavior_effect.invoke(
        {"expected": "disabled(完成)", "verification_key": "v5", "clause_id": "v5.3"}
    )
    assert res_off.startswith("PASS"), res_off
    ev_off = ctx_off._evidence_events[0]
    assert ev_off["authoritative"] is True
    assert ev_off["fact"]["enabled"] is False

    # enabled=True → FAIL + authoritative=False，fact 含 selected/clickable/checked
    ctx_on = _make_ctx([_el("完成", enabled=True, selected=False, clickable=True, checked=None)])
    set_tool_context(ctx_on)
    res_on = verify_tools.assert_behavior_effect.invoke(
        {"expected": "disabled(完成)", "verification_key": "v5", "clause_id": "v5.3"}
    )
    assert res_on.startswith("FAIL"), res_on
    ev_on = ctx_on._evidence_events[0]
    assert ev_on["authoritative"] is False
    assert ev_on["fact"]["enabled"] is True
    assert "selected" in ev_on["fact"]
    assert "clickable" in ev_on["fact"]
    # checked 保留 None 语义（非可勾选类型），不被 bool() 吞
    assert ev_on["fact"]["checked"] is None


# ---------------------------------------------------------------------------
# 6. F3 [SELECTED] 渲染：selected=True 元素行含 [SELECTED]
# ---------------------------------------------------------------------------
def test_format_element_line_renders_selected(monkeypatch):
    from tools import _format_element_line

    sel = _el("周一", region="main_content", role="tab", selected=True, bounds=(0, 0, 1, 1))
    unsel = _el("周二", region="main_content", role="tab", selected=False, bounds=(0, 0, 1, 1))

    line_sel = _format_element_line(sel)
    line_unsel = _format_element_line(unsel)
    assert "[SELECTED]" in line_sel
    assert "[SELECTED]" not in line_unsel

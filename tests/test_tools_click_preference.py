from __future__ import annotations

from types import SimpleNamespace

import tools as tools_module
from tools.context import ToolContext


def _el(
    *,
    label: str,
    rid: str = "",
    cls: str = "android.widget.TextView",
    role: str = "list_entry",
    path: str = "",
    bounds: tuple[int, int, int, int] = (0, 0, 100, 100),
    clickable: bool = True,
    text: str = "",
):
    return SimpleNamespace(
        label=label,
        resource_id=rid,
        class_name=cls,
        associated_label="",
        context_path=path,
        role=role,
        region="main_content",
        bounds=bounds,
        clickable=clickable,
        text=text,
        checked=None,
    )


def test_extract_click_preferences_from_rag():
    rag = (
        "优先匹配：role=list_entry 且 label 命中应用列表且 class 为 TextView，"
        "path 包含 taskbar_container > taskbar_view"
    )
    prefs = tools_module._extract_click_preferences_from_rag(rag)
    assert "应用列表" in prefs.get("label_contains", [])
    assert "list_entry" in prefs.get("role_prefer", [])
    assert "textview" in prefs.get("class_prefer", [])
    assert "taskbar_container > taskbar_view" in prefs.get("path_contains", [])


def test_disambiguate_container_is_gated_by_prefs():
    container = _el(
        label="应用列表",
        rid="com.zui.launcher:id/taskbar_view",
        cls="android.widget.FrameLayout",
        role="list_entry",
        path="taskbar_container",
        bounds=(0, 2400, 3840, 2560),
    )
    child_text = _el(
        label="应用列表",
        cls="android.widget.TextView",
        role="list_entry",
        path="taskbar_container > taskbar_view",
        bounds=(1417, 2428, 1530, 2541),
    )
    all_elements = [container, child_text]
    # prefs 为空时，list_entry 子项不参与替换
    assert (
        tools_module._disambiguate_container(container, all_elements, {}, "应用列表")
        is None
    )
    prefs = tools_module._extract_click_preferences_from_rag(
        "role=list_entry label=应用列表 class=TextView path=taskbar_container > taskbar_view"
    )
    assert (
        tools_module._disambiguate_container(container, all_elements, prefs, "应用列表")
        is child_text
    )


def test_rag_weight_prefers_textview_over_container():
    container = _el(
        label="应用列表",
        rid="com.zui.launcher:id/taskbar_view",
        cls="android.widget.FrameLayout",
        role="list_entry",
        path="taskbar_container",
        bounds=(0, 2400, 3840, 2560),
    )
    child_text = _el(
        label="应用列表",
        cls="android.widget.TextView",
        role="list_entry",
        path="taskbar_container > taskbar_view",
        bounds=(1417, 2428, 1530, 2541),
    )
    words = ["应用列表"]
    base_container = tools_module._score_element(container, words)
    base_child = tools_module._score_element(child_text, words)
    assert base_container >= base_child
    prefs = tools_module._extract_click_preferences_from_rag(
        "role=list_entry label=应用列表 class=TextView path=taskbar_container > taskbar_view"
    )
    pref_container = tools_module._score_element(container, words, prefs, "应用列表")
    pref_child = tools_module._score_element(child_text, words, prefs, "应用列表")
    assert pref_child > pref_container


def test_click_no_longer_auto_fallbacks_to_next_candidate(monkeypatch):
    class _Device:
        def __init__(self):
            self.page = "CustomModeLauncher"
            self.rid_click_count = 0
            self.bounds_click_count = 0

        def current_app(self):
            act = (
                "com.zui.launcher.MainActivity"
                if self.page == "MainActivity"
                else "com.zui.launcher.CustomModeLauncher"
            )
            return {"package": "com.zui.launcher", "activity": act}

        def click_resource_id(self, rid: str):
            if rid == "com.zui.launcher:id/taskbar_view":
                self.rid_click_count += 1
                return True
            return False

        def click_text(self, _text: str):
            return False

        def click_bounds(self, _bounds):
            self.bounds_click_count += 1
            self.page = "MainActivity"
            return True

        def snapshot(self):
            return SimpleNamespace(width=3840, height=2560)

    class _Perceiver:
        def __init__(self, device):
            self.device = device

        def perceive(self):
            container = _el(
                label="应用列表",
                rid="com.zui.launcher:id/taskbar_view",
                cls="android.widget.FrameLayout",
                role="list_entry",
                path="taskbar_container",
                bounds=(0, 2400, 3840, 2560),
            )
            child_text = _el(
                label="应用列表",
                cls="android.widget.TextView",
                role="list_entry",
                path="taskbar_container > taskbar_view",
                bounds=(1417, 2428, 1530, 2541),
            )
            activity = (
                "com.zui.launcher.MainActivity"
                if self.device.page == "MainActivity"
                else "com.zui.launcher.CustomModeLauncher"
            )
            return SimpleNamespace(
                activity=activity,
                page_title="16:53",
                primary_paths=[],
                elements=[container, child_text],
            )

    device = _Device()
    perceiver = _Perceiver(device)
    ctx = ToolContext(device=device, perceiver=perceiver)
    # 该用例验证“回退在同一次 click 内完成”，故不注入 prefs，让首击先命中容器。
    tools_module.set_tool_context(ctx)
    monkeypatch.setattr(
        tools_module,
        "check_dangerous",
        lambda _label: SimpleNamespace(allowed=True, reason=""),
    )

    out = tools_module.click.invoke({"label": "应用列表", "alternatives": ""})
    assert "fallback=next_candidate" not in out
    assert "已点击" in out
    assert device.rid_click_count == 1
    assert device.bounds_click_count == 0


def test_click_returns_ambiguous_when_unique_rid_semantics_mismatch(monkeypatch):
    class _Device:
        def __init__(self):
            self.rid_click_count = 0

        def current_app(self):
            return {
                "package": "com.zui.calculator",
                "activity": "com.zui.calculator.Calculator",
            }

        def click_resource_id(self, rid: str):
            if rid == "com.zui.calculator:id/op_fact":
                self.rid_click_count += 1
                return True
            return False

        def click_text(self, _text: str):
            return False

        def click_bounds(self, _bounds):
            return True

        def snapshot(self):
            return SimpleNamespace(width=1200, height=2000)

    class _Perceiver:
        def perceive(self):
            return SimpleNamespace(
                activity="com.zui.calculator.Calculator",
                page_title="16:53",
                primary_paths=[],
                elements=[
                    _el(
                        label="x!",
                        rid="com.zui.calculator:id/op_fact",
                        cls="android.widget.Button",
                        role="list_entry",
                        path="content > root_layout > content_layout > pad_layout",
                    )
                ],
            )

    device = _Device()
    tools_module.set_tool_context(ToolContext(device=device, perceiver=_Perceiver()))
    monkeypatch.setattr(
        tools_module,
        "check_dangerous",
        lambda _label: SimpleNamespace(allowed=True, reason=""),
    )
    out = tools_module.click.invoke(
        {"label": "AC", "rid": "com.zui.calculator:id/op_fact"}
    )
    assert "AMBIGUOUS:" in out
    assert "请用 index/class 精确定位" in out
    assert device.rid_click_count == 0


def test_get_screen_info_contains_click_indexes():
    class _Device:
        def current_app(self):
            return {
                "package": "com.zui.launcher",
                "activity": "com.zui.launcher.MainActivity",
            }

        def snapshot(self):
            return SimpleNamespace(width=3840, height=2560)

    class _Perceiver:
        def perceive(self):
            return SimpleNamespace(
                activity="com.zui.launcher.MainActivity",
                page_title="16:53",
                layout="two_pane",
                summary="summary",
                primary_paths=[],
                elements=[
                    _el(label="应用列表", cls="android.widget.TextView"),
                    _el(label="搜索", cls="android.widget.TextView"),
                ],
            )

    tools_module.set_tool_context(ToolContext(device=_Device(), perceiver=_Perceiver()))
    out = tools_module.get_screen_info.invoke({"mode": "full"})
    assert "- [0]" in out
    assert "- [1]" in out


def test_click_exact_mode_reports_ambiguous_matches(monkeypatch):
    class _Device:
        def current_app(self):
            return {
                "package": "com.zui.launcher",
                "activity": "com.zui.launcher.MainActivity",
            }

        def snapshot(self):
            return SimpleNamespace(width=3840, height=2560)

    class _Perceiver:
        def perceive(self):
            return SimpleNamespace(
                activity="com.zui.launcher.MainActivity",
                page_title="16:53",
                primary_paths=[],
                elements=[
                    # 两个同名同类元素 → 即便按 label 收窄仍无法区分，应报 AMBIGUOUS
                    _el(label="应用列表", cls="android.widget.TextView", path="a"),
                    _el(label="应用列表", cls="android.widget.TextView", path="b"),
                ],
            )

    tools_module.set_tool_context(ToolContext(device=_Device(), perceiver=_Perceiver()))
    monkeypatch.setattr(
        tools_module,
        "check_dangerous",
        lambda _label: SimpleNamespace(allowed=True, reason=""),
    )
    out = tools_module.click.invoke({"label": "应用列表", "class_name": "textview"})
    assert "候选匹配" in out


def test_click_exact_mode_with_index_clicks_direct_target(monkeypatch):
    class _Device:
        def __init__(self):
            self.rid_click_count = 0
            self.clicked_bounds = 0

        def current_app(self):
            return {
                "package": "com.zui.launcher",
                "activity": "com.zui.launcher.MainActivity",
            }

        def click_resource_id(self, rid: str):
            if rid == "com.zui.launcher:id/app_list":
                self.rid_click_count += 1
                return True
            return False

        def click_text(self, _text: str):
            return False

        def click_bounds(self, _bounds):
            self.clicked_bounds += 1
            return True

        def snapshot(self):
            return SimpleNamespace(width=3840, height=2560)

    class _Perceiver:
        def perceive(self):
            return SimpleNamespace(
                activity="com.zui.launcher.MainActivity",
                page_title="16:53",
                primary_paths=[],
                elements=[
                    _el(label="搜索", rid="com.zui.launcher:id/search"),
                    _el(
                        label="应用列表",
                        rid="com.zui.launcher:id/app_list",
                        path="taskbar_container > taskbar_view",
                    ),
                ],
            )

    d = _Device()
    tools_module.set_tool_context(ToolContext(device=d, perceiver=_Perceiver()))
    monkeypatch.setattr(
        tools_module,
        "check_dangerous",
        lambda _label: SimpleNamespace(allowed=True, reason=""),
    )
    out = tools_module.click.invoke({"label": "应用列表", "index": 1})
    assert "fallback=next_candidate" not in out
    assert d.rid_click_count == 1


def test_extract_curated_rule_label_for_conflict_match():
    assert (
        tools_module._extract_curated_rule_label(
            "在MainActivity点击“应用列表”时，优先匹配 class=textview"
        )
        == "应用列表"
    )
    assert (
        tools_module._extract_curated_rule_label(
            '在MainActivity点击"应用"时，优先匹配 class=textview'
        )
        == "应用"
    )
    assert (
        tools_module._extract_curated_rule_label("普通人工规则：先等待页面稳定") == ""
    )

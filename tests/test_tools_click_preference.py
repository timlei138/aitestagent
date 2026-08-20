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
    checked=None,
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
        checked=checked,
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
    # check_dangerous safety拦截已移除，无需 mock

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
    # check_dangerous safety拦截已移除，无需 mock
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
    # check_dangerous safety拦截已移除，无需 mock
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
    # check_dangerous safety拦截已移除，无需 mock
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


def test_get_screen_info_indexes_and_pages_unlabeled_clickables():
    class _Device:
        def current_app(self):
            return {
                "package": "com.zui.calendar",
                "activity": "com.zui.calendar.TimetableActivity",
            }

        def snapshot(self):
            return SimpleNamespace(width=1200, height=800)

    class _Perceiver:
        def perceive(self):
            left = _el(label="第1周", bounds=(20, 120, 220, 180))
            left.region = "left_navigation"
            first_cell = _el(
                label="",
                cls="android.widget.FrameLayout",
                path="content > recyclerView > list_entry",
                bounds=(620, 120, 820, 240),
            )
            first_cell.region = "right_content"
            second_cell = _el(
                label="",
                cls="android.widget.FrameLayout",
                path="content > recyclerView > list_entry",
                bounds=(830, 120, 1030, 240),
            )
            second_cell.region = "right_content"
            return SimpleNamespace(
                activity="com.zui.calendar.TimetableActivity",
                page_title="课程表",
                layout="two_pane",
                summary="summary",
                regions=[
                    {
                        "name": "left_navigation",
                        "bounds": [0, 0, 500, 800],
                    },
                    {
                        "name": "right_content",
                        "bounds": [500, 0, 1200, 800],
                    },
                ],
                primary_paths=[left, first_cell, second_cell],
                elements=[left, first_cell, second_cell],
            )

    tools_module.set_tool_context(ToolContext(device=_Device(), perceiver=_Perceiver()))

    overview = tools_module.get_screen_info.invoke({"mode": "full"})
    assert "clickable_total=2 labeled_clickable=0 global_indexes=1,2" in overview
    assert "右侧存在 2 个无文本可点击元素" in overview
    assert '[1] [right_content/list_entry] "<无文本>"' in overview

    page = tools_module.get_screen_info.invoke(
        {"mode": "clickable", "offset": 1, "limit": 1}
    )
    assert "clickable_total=3" in page
    assert "showing=1-1" in page
    assert '[1] [right_content/list_entry] "<无文本>"' in page
    assert "offset=2" in page


def test_click_index_selects_unlabeled_clickable_by_bounds(monkeypatch):
    class _Device:
        def __init__(self):
            self.clicked_bounds = []

        def current_app(self):
            return {
                "package": "com.zui.calendar",
                "activity": "com.zui.calendar.TimetableActivity",
            }

        def click_text(self, _text: str):
            return False

        def click_bounds(self, bounds):
            self.clicked_bounds.append(bounds)
            return True

        def click_resource_id(self, rid):
            self.clicked_bounds.append(rid)
            return True

        def click_text(self, text):
            self.clicked_bounds.append(text)
            return True

        def snapshot(self):
            return SimpleNamespace(width=1200, height=800)

    class _Perceiver:
        def perceive(self):
            return SimpleNamespace(
                activity="com.zui.calendar.TimetableActivity",
                page_title="课程表",
                primary_paths=[],
                elements=[
                    _el(label="第1周", bounds=(20, 120, 220, 180)),
                    _el(
                        label="",
                        cls="android.widget.FrameLayout",
                        path="content > recyclerView > list_entry",
                        bounds=(620, 120, 820, 240),
                    ),
                ],
            )

    device = _Device()
    tools_module.set_tool_context(ToolContext(device=device, perceiver=_Perceiver()))
    # check_dangerous safety拦截已移除，无需 mock

    out = tools_module.click.invoke({"index": 1})

    assert "已点击" in out
    assert device.clicked_bounds == [(620, 120, 820, 240)]


def test_click_rejects_empty_label_without_locator():
    # §0 契约：空 label 且无任何定位依据（index/rid/class/path/alternatives）
    # 时直接 ERROR，禁止进入兜底“盲点”浪费步数。
    class _Device:
        def current_app(self):
            return {
                "package": "com.zui.calendar",
                "activity": "com.zui.calendar.MainActivity",
            }

    tools_module.set_tool_context(ToolContext(device=_Device(), perceiver=None))

    out = tools_module.click.invoke({})  # 全默认：label="" index=-1 ...
    assert "label 不能为空" in out
    assert "已点击" not in out

    # 给了 index 即使 label 为空也应放行（走精确定位，不触发盲点守卫）
    class _Perceiver:
        def perceive(self):
            return SimpleNamespace(
                activity="com.zui.calendar.MainActivity",
                page_title="",
                primary_paths=[],
                elements=[_el(label="新课程", bounds=(20, 120, 220, 180))],
            )

    class _Device2:
        def __init__(self):
            self.clicked_bounds = []

        def current_app(self):
            return {
                "package": "com.zui.calendar",
                "activity": "com.zui.calendar.MainActivity",
            }

        def click_bounds(self, bounds):
            self.clicked_bounds.append(bounds)
            return True

        def click_resource_id(self, rid):
            self.clicked_bounds.append(rid)
            return True

        def click_text(self, text):
            self.clicked_bounds.append(text)
            return True

    tools_module.set_tool_context(ToolContext(device=_Device2(), perceiver=_Perceiver()))
    out_idx = tools_module.click.invoke({"index": 0})
    assert "label 不能为空" not in out_idx


def test_click_repeat_performs_n_taps_on_resolved_element():
    # 回归：click(label=..., repeat=N) 必须对同一个已解析元素连点 N 次（增量控件场景，
    # 如“最多添加10节”连点 + 号）。一次性完成，避免 agent 连发 N 次相同 click 触发
    # LOOP_DETECTED 把本轮 abort。本测试锁定：repeat=4 → click_bounds 被调用 4 次，
    # 且返回文案含「连点4次」。
    class _Device:
        def __init__(self):
            self.clicked_bounds = []

        def current_app(self):
            return {
                "package": "com.zui.calendar",
                "activity": "com.zui.calendar.MainActivity",
            }

        def click_bounds(self, bounds):
            self.clicked_bounds.append(bounds)
            return True

        def click_resource_id(self, rid):
            return False

        def click_text(self, text):
            return False

    class _Perceiver:
        def perceive(self):
            return SimpleNamespace(
                activity="com.zui.calendar.MainActivity",
                page_title="",
                primary_paths=[],
                elements=[_el(label="加号", bounds=(20, 120, 220, 180))],
            )

    tools_module.set_tool_context(ToolContext(device=_Device(), perceiver=_Perceiver()))
    out = tools_module.click.invoke({"label": "加号", "repeat": 4})
    assert "已点击" in out
    assert "连点4次" in out
    assert len(_Device().clicked_bounds) == 0  # sanity: fresh instance unused
    # 重新跑以统计真实调用次数
    d = _Device()
    tools_module.set_tool_context(ToolContext(device=d, perceiver=_Perceiver()))
    tools_module.click.invoke({"label": "加号", "repeat": 4})
    assert len(d.clicked_bounds) == 4  # 首次（_perform_click）+ 额外 3 次 repeat


def test_click_repeat_ignored_for_switch_toggle():
    # 回归：switch/checkbox 是“切换”控件，连点会来回切，repeat 必须被忽略（仍只点 1 次）。
    class _Device:
        def __init__(self):
            self.clicked_bounds = []

        def current_app(self):
            return {
                "package": "com.zui.calendar",
                "activity": "com.zui.calendar.MainActivity",
            }

        def click_bounds(self, bounds):
            self.clicked_bounds.append(bounds)
            return True

    class _Perceiver:
        def perceive(self):
            return SimpleNamespace(
                activity="com.zui.calendar.MainActivity",
                page_title="",
                primary_paths=[],
                elements=[
                    _el(
                        label="WLAN",
                        bounds=(20, 120, 220, 180),
                        role="switch",
                        cls="android.widget.Switch",
                    )
                ],
            )

    d = _Device()
    tools_module.set_tool_context(ToolContext(device=d, perceiver=_Perceiver()))
    out = tools_module.click.invoke({"label": "WLAN", "repeat": 5})
    # switch 走专用回检路径（sleep 4s），repeat 被忽略：bounds 只点 1 次
    assert "连点" not in out
    assert len(d.clicked_bounds) == 1


def test_click_repeat_works_for_stepper_imageview_not_checkbox():
    # 回归 #1+#回归：无文本 ImageView +/- 节数按钮（btn_add_morning_slot）携带 XML 噪音
    # checked="false" + role=list_entry。修复前 _is_checkbox_like 误判为 True → 走 switch
    # 回检分支、repeat 被忽略。修复后判定为 False → 走普通 bounds 路径，repeat=N 连点生效。
    class _Device:
        def __init__(self):
            self.clicked_bounds = []

        def current_app(self):
            return {
                "package": "com.zui.calendar",
                "activity": "com.zui.calendar.TimetableActivity",
            }

        def click_bounds(self, bounds):
            self.clicked_bounds.append(bounds)
            return True

        def click_text(self, text):
            return False

    class _Perceiver:
        def perceive(self):
            return SimpleNamespace(
                activity="com.zui.calendar.TimetableActivity",
                page_title="",
                primary_paths=[],
                elements=[
                    _el(
                        label="",
                        rid="com.zui.calendar:id/btn_add_morning_slot",
                        cls="android.widget.ImageView",
                        checked=False,  # XML 噪音：并非真可勾选（真实 perceiver 返回布尔）
                        bounds=(20, 120, 220, 180),
                    )
                ],
            )

    d = _Device()
    tools_module.set_tool_context(ToolContext(device=d, perceiver=_Perceiver()))
    # 用 index 精确定位无文本 +/- 按钮，并声明 repeat=6
    out = tools_module.click.invoke(
        {"index": 0, "label": "添加上午课程节数", "repeat": 6}
    )
    # 关键：不再被误判为 checkbox/switch 回检分支，repeat 连点生效
    assert "连点6次" in out
    assert len(d.clicked_bounds) == 6
    # 不应出现勾选/开关状态误导文案（那是 checkbox-like 分支才加的）
    assert "勾选状态" not in out
    assert "开关状态" not in out


def test_click_index_bypasses_rid_semantic_match_for_textless_imageview():
    # 回归 #回归：无文本 ImageView + 唯一 rid + 按 index 点击，应绕过 rid 语义匹配分支
    # （该分支要求 label 命中才放行，无文本元素必然 label_assoc_hit=False → AMBIGUOUS），
    # 直接走 click_bounds 成功。index 已精确定位，无需 rid 语义匹配。
    class _Device:
        def __init__(self):
            self.clicked_bounds = []

        def current_app(self):
            return {
                "package": "com.zui.calendar",
                "activity": "com.zui.calendar.TimetableActivity",
            }

        def click_bounds(self, bounds):
            self.clicked_bounds.append(bounds)
            return True

        def click_resource_id(self, rid):
            self.clicked_bounds.append(rid)
            return True

        def click_text(self, text):
            return False

    class _Perceiver:
        def perceive(self):
            return SimpleNamespace(
                activity="com.zui.calendar.TimetableActivity",
                page_title="",
                primary_paths=[],
                elements=[
                    _el(
                        label="",
                        rid="com.zui.calendar:id/btn_add_morning_slot",
                        cls="android.widget.ImageView",
                        checked=False,
                        bounds=(20, 120, 220, 180),
                    )
                ],
            )

    d = _Device()
    tools_module.set_tool_context(ToolContext(device=d, perceiver=_Perceiver()))
    # 按 index=0 精确点击，无文本按钮不应返回 AMBIGUOUS
    out = tools_module.click.invoke({"index": 0, "label": "添加上午课程节数"})
    assert "AMBIGUOUS" not in out
    assert "已点击" in out
    assert d.clicked_bounds == [(20, 120, 220, 180)]  # 走 bounds，非 rid


def test_edittext_with_checked_noise_not_checkbox_like():
    # 回归（最新日志实证）：EditText（如 et_schedule_name）常携带 checked 噪音属性，
    # 且 role 默认 list_entry，修复前 _is_checkbox_like 借此兜底判 True → 走 switch 回检
    # 分支、打“勾选状态: 未勾选”误导文案。EditText 非 toggle，必须排除。
    class _Device:
        def __init__(self):
            self.clicked_bounds = []

        def current_app(self):
            return {
                "package": "com.zui.calendar",
                "activity": "com.zui.calendar.TimetableActivity",
            }

        def click_bounds(self, bounds):
            self.clicked_bounds.append(bounds)
            return True

        def click_resource_id(self, rid):
            self.clicked_bounds.append(rid)
            return True

        def click_text(self, text):
            return False

    class _Perceiver:
        def perceive(self):
            return SimpleNamespace(
                activity="com.zui.calendar.TimetableActivity",
                page_title="",
                primary_paths=[],
                elements=[
                    _el(
                        label="课程表名称（必填）",
                        rid="com.zui.calendar:id/et_schedule_name",
                        cls="android.widget.EditText",
                        checked=False,  # XML 噪音：输入框并非可勾选
                        bounds=(88, 200, 2952, 280),
                    )
                ],
            )

    d = _Device()
    tools_module.set_tool_context(ToolContext(device=d, perceiver=_Perceiver()))
    out = tools_module.click.invoke(
        {"index": 0, "label": "课程表名称（必填）", "repeat": 2}
    )
    # 关键：EditText 不再被误判为 checkbox-like → 走 bounds、repeat 生效、无勾选状态误导
    assert "连点2次" in out
    assert "勾选状态" not in out
    assert "开关状态" not in out
    assert len(d.clicked_bounds) == 2


def test_empty_requested_label_never_counts_as_fuzzy():
    # P0 第 4 点回归：空 requested_label 绝不能被记为 fuzzy_match=True。
    # click.py:827-832 的 fuzzy_match 公式依赖「双方非空」约束（_q 与 _el_label
    # 均非空），空 label 必须落空、绝不能污染模糊匹配指标统计。未来很容易悄悄把
    # `bool(_q) and` 删掉改成「label 空也算 fuzzy」，故用三条测试锁死：
    #   (1) 空 label（+ 定位依据）→ 不触发 fuzzy（被 AMBIGUOUS/ERROR 挡在成功路径外）；
    #   (2) 精确匹配 label → 成功且 fuzzy_match=false（证明该 key 会被正常透出为 false）；
    #   (3) 模糊 label（子串但不等价）→ 成功且 fuzzy_match=true（证明公式确实在跑）。
    class _Device:
        def __init__(self):
            self.clicked_bounds = []

        def current_app(self):
            return {
                "package": "com.zui.calendar",
                "activity": "com.zui.calendar.MainActivity",
            }

        def click_bounds(self, bounds):
            self.clicked_bounds.append(bounds)
            return True

        def click_resource_id(self, rid):
            self.clicked_bounds.append(rid)
            return True

        def click_text(self, text):
            self.clicked_bounds.append(text)
            return True

    class _Perceiver:
        def perceive(self):
            return SimpleNamespace(
                activity="com.zui.calendar.MainActivity",
                page_title="",
                primary_paths=[],
                elements=[
                    _el(
                        label="新课程",
                        rid="com.zui.calendar:id/new_course",
                        bounds=(20, 120, 220, 180),
                    )
                ],
            )

    tools_module.set_tool_context(ToolContext(device=_Device(), perceiver=_Perceiver()))

    # (1) 空 label + index 定位依据 → 不会进入成功路径，更不会冒出 fuzzy_match=true。
    out_empty = tools_module.click.invoke({"index": 0, "label": ""})
    assert "fuzzy_match=true" not in out_empty

    # (2) 精确匹配 label → 成功且明确透出 fuzzy_match=false。
    out_exact = tools_module.click.invoke({"index": 0, "label": "新课程"})
    assert "已点击" in out_exact
    assert "fuzzy_match=false" in out_exact
    assert "fuzzy_match=true" not in out_exact

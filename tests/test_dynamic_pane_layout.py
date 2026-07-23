from __future__ import annotations

from device.perceiver import SmartPerceiver, UIElement


def _element(label: str, bounds: tuple[int, int, int, int], *, clickable: bool = True):
    return UIElement(text=label, bounds=bounds, clickable=clickable)


def test_dynamic_panes_are_disjoint_and_classify_shared_chrome_as_main_content():
    perceiver = SmartPerceiver(device=None)
    elements = [
        _element("菜单", (30, 120, 240, 180)),
        _element("第1周", (30, 280, 240, 340)),
        _element("设置", (30, 460, 240, 520)),
        _element("课程格", (620, 120, 1120, 260), clickable=False),
        _element("课程表", (620, 300, 1120, 440), clickable=False),
        _element("时间轴", (620, 500, 1120, 700), clickable=False),
        _element("共享标题", (0, 0, 1200, 72)),
    ]

    regions = perceiver._detect_pane_regions(elements, width=1200, height=800)

    assert [region["name"] for region in regions] == [
        "left_navigation",
        "right_content",
    ]
    split_x = regions[0]["bounds"][2]
    assert regions[1]["bounds"][0] == split_x
    assert 240 < split_x < 620

    for element in elements:
        perceiver._classify_element(element, 1200, 800, regions)

    assert elements[1].region == "left_navigation"
    assert elements[3].region == "right_content"
    assert elements[-1].region == "main_content"


def test_dynamic_panes_reject_a_wide_left_navigation_split():
    perceiver = SmartPerceiver(device=None)
    elements = [
        _element("菜单", (300, 120, 400, 180)),
        _element("第1周", (350, 280, 450, 340)),
        _element("设置", (400, 460, 500, 520)),
        _element("课程格1", (950, 120, 1050, 260), clickable=False),
        _element("课程格2", (1000, 300, 1100, 440), clickable=False),
        _element("课程格3", (1050, 500, 1150, 700), clickable=False),
    ]

    regions = perceiver._detect_pane_regions(elements, width=1200, height=800)

    assert regions == []


def test_heuristic_understand_excludes_elements_completely_outside_viewport():
    perceiver = SmartPerceiver(device=None)
    visible = _element("当前课程格", (620, 120, 820, 240))
    partially_visible = _element("底部露出", (620, 760, 820, 840))
    off_right = _element("屏外右侧", (1200, 120, 1320, 240))
    off_bottom = _element("屏外底部", (620, 800, 820, 920))

    understanding = perceiver._heuristic_understand(
        [visible, partially_visible, off_right, off_bottom],
        package="com.example",
        activity="com.example.MainActivity",
        page_title="",
        width=1200,
        height=800,
    )

    assert [element.label for element in understanding.elements] == [
        "当前课程格",
        "底部露出",
    ]


def test_course_grid_columns_are_not_misclassified_as_left_navigation():
    perceiver = SmartPerceiver(device=None)
    elements = [
        _element("返回", (36, 95, 166, 239)),
        _element("我的课程表", (166, 128, 436, 206)),
        _element("第1周", (459, 143, 576, 190)),
        _element("", (232, 396, 697, 666)),
        _element("", (697, 396, 1162, 666)),
        _element("", (1162, 396, 1627, 666)),
        _element("", (1627, 396, 2092, 666)),
        _element("", (2092, 396, 2557, 666)),
        _element("", (232, 666, 697, 936)),
        _element("", (1162, 666, 1627, 936)),
    ]

    regions = perceiver._detect_pane_regions(elements, width=2560, height=1600)

    assert regions == []

from __future__ import annotations

from types import SimpleNamespace

from agents.nodes import _select_page_info_clickables


def _clickable(label: str):
    return SimpleNamespace(label=label, clickable=True)


def test_page_info_prioritizes_labeled_anchors_with_real_global_indexes():
    elements = [
        *[_clickable("") for _ in range(10)],
        _clickable("第1周"),
        _clickable("设置"),
        *[_clickable("") for _ in range(10)],
    ]

    selected = _select_page_info_clickables(elements, max_items=6)

    assert [index for index, _ in selected] == [10, 11, 0, 1, 2, 3, 4, 5, 6, 7]
    assert [element.label for _, element in selected] == [
        "第1周",
        "设置",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
    ]

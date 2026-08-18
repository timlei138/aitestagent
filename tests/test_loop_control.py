from __future__ import annotations

import pytest

from agents.loop_control import (
    _cooldown_group,
    _cooldown_group_from_evidence,
    _detect_toggle_loop,
)


def test_cooldown_group_click_returns_normalized_key_with_page_sig():
    group = _cooldown_group(
        "click",
        {"label": "保存按钮", "target": "", "alternatives": ""},
        "保存",
        "MainActivity||abc123",
    )
    assert group == "click|保存按钮|MainActivity||abc123"


def test_cooldown_group_click_empty_when_page_sig_unknown():
    group = _cooldown_group(
        "click", {"label": "保存"}, "保存", "unknown"
    )
    assert group == ""


def test_cooldown_group_click_empty_when_no_target():
    group = _cooldown_group("click", {}, "", "MainActivity||abc123")
    assert group == ""


def test_cooldown_group_scroll_find_and_click():
    group = _cooldown_group(
        "scroll_find_and_click",
        {"label": "课程表设置"},
        "",
        "TimetableActivity||def456",
    )
    assert group == "scroll_find_and_click|课程表设置|TimetableActivity||def456"


def test_cooldown_group_type_input():
    group = _cooldown_group(
        "type_input", {"label": "课程名称"}, "", "EditTimetableActivity||ghi789"
    )
    assert group == "type_input|课程名称|EditTimetableActivity||ghi789"


def test_cooldown_group_nav_back_and_browse_unchanged():
    assert _cooldown_group("press_key", {"key": "back"}, "", "A||1") == "nav_back"
    assert _cooldown_group("swipe", {}, "", "A||1") == "browse"
    assert _cooldown_group("scroll_panel", {}, "", "A||1") == "browse"


def test_cooldown_group_from_evidence_fuzzy_click():
    group = _cooldown_group_from_evidence(
        "click",
        {"label": "保存"},
        "保存",
        "MainActivity||abc123",
        {"fuzzy_match": True},
    )
    assert group == "fuzzy_click|保存|MainActivity||abc123"


def test_cooldown_group_from_evidence_not_fuzzy_returns_empty():
    group = _cooldown_group_from_evidence(
        "click", {"label": "保存"}, "保存", "MainActivity||abc123", {"fuzzy_match": False}
    )
    assert group == ""


def test_cooldown_group_from_evidence_unknown_page_sig_returns_empty():
    group = _cooldown_group_from_evidence(
        "click", {"label": "保存"}, "保存", "unknown", {"fuzzy_match": True}
    )
    assert group == ""


def _click_entry(label: str, page_sig: str) -> dict:
    return {
        "tool_name": "click",
        "tool_input": {"label": label},
        "page_after": {"signature": page_sig},
    }


def test_detect_toggle_loop_triggers_on_same_target_three_times():
    """同一元素在同一页面被点击 3 次应识别为破坏性切换环路。"""
    history = [
        _click_entry("WLAN", "Settings||a"),
        {"tool_name": "assert_page_contains", "tool_input": {}},
        _click_entry("WLAN", "Settings||a"),
        {"tool_name": "assert_page_contains", "tool_input": {}},
        _click_entry("WLAN", "Settings||a"),
    ]
    detected, key = _detect_toggle_loop(history)
    assert detected is True
    assert "wlan" in key


def test_detect_toggle_loop_ignores_different_targets():
    history = [
        _click_entry("WLAN", "Settings||a"),
        _click_entry("蓝牙", "Settings||a"),
        _click_entry("WLAN", "Settings||a"),
    ]
    detected, _ = _detect_toggle_loop(history)
    assert detected is False


def test_detect_toggle_loop_respects_page_signature():
    """不同页面上的同名按钮不应被算作同一个切换环路。"""
    history = [
        _click_entry("WLAN", "Settings||a"),
        _click_entry("WLAN", "Settings||a"),
        _click_entry("WLAN", "QuickPanel||b"),
    ]
    detected, _ = _detect_toggle_loop(history)
    assert detected is False

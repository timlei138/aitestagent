from __future__ import annotations

"""click(permission_hint) 自动处理路径的单元测试。

对应 docs/权限双分支测试问题与解决方案_20260724.md 的「步骤 4」验证要求：
- 非 hint 路径行为不变（回归 #6，布尔 permission_dialog）
- 按钮多变体 + 最小权限优先（下标最小匹配）
- 媒体范围 / 设置按钮不误触发
- 证据回写（handled / fallback_match / fallback_error）
- 弹窗延迟出现时 _detect_permission_popup 有界轮询可捕捉
"""

from types import SimpleNamespace
from unittest.mock import patch

import tools.perceive_tools as pt
from tools.perceive_tools import _match_permission_button
from tools.click import _maybe_auto_handle_permission


# ── 构造工具 ──────────────────────────────────────────────

def _b(x1: int, y1: int, x2: int, y2: int) -> str:
    """生成 [x1,y1][x2,y2] 形式的 bounds 字符串。"""
    return f"{x1},{y1}][{x2},{y2}"


def _xml(*buttons):
    """buttons: (text, bounds_str) 序列，生成可解析的 hierarchy XML。"""
    nodes = "".join(
        f'<node index="0" text="{t}" resource-id="" '
        f'class="android.widget.Button" clickable="true" bounds="[{b}]"/>'
        for t, b in buttons
    )
    return (
        f'<hierarchy rotation="0">'
        f'<node index="0" text="" clickable="false">{nodes}</node>'
        f'</hierarchy>'
    )


class _Device:
    """立即返回权限弹窗的 device（marker activity + 给定可点击按钮）。"""

    def __init__(self, buttons, activity="com.android.permissioncontroller.PermissionActivity"):
        self.activity = activity
        self.hierarchy = _xml(*buttons)
        self.click_bounds_calls: list[tuple[int, int, int, int]] = []

    def current_app(self, refresh=True):
        return {"package": "com.demo", "activity": self.activity}

    def dump_hierarchy(self):
        return self.hierarchy

    def click_bounds(self, bounds):
        self.click_bounds_calls.append(tuple(bounds))
        return True


class _ErrDevice(_Device):
    """click_bounds 始终抛错，用于验证 fallback_error 降级。"""

    def click_bounds(self, bounds):
        raise RuntimeError("adb failed")


class _SeqDevice:
    """按轮询次数依次返回不同 (activity, buttons) 状态。

    current_app 在每次调用时推进轮询下标（首轮之后），dump_hierarchy
    读取同一轮询下标 —— 与真实设备「先 current_app 再 dump_hierarchy」一致。
    """

    def __init__(self, states):
        self.states = [(a, _xml(*b)) for a, b in states]
        self._idx = 0
        self._poll = 0
        self.click_bounds_calls: list[tuple[int, int, int, int]] = []

    def current_app(self, refresh=True):
        self._idx = min(self._poll, len(self.states) - 1)
        if self._poll < len(self.states) - 1:
            self._poll += 1
        return {"package": "com.demo", "activity": self.states[self._idx][0]}

    def dump_hierarchy(self):
        return self.states[self._idx][1]

    def click_bounds(self, bounds):
        self.click_bounds_calls.append(tuple(bounds))
        return True


def _ctx(device):
    return SimpleNamespace(device=device)


# ── _match_permission_button：纯函数 + 最小权限优先 ──────────

def test_match_grant_picks_min_index_not_first_hit():
    # 「始终允许」先于「仅在使用中允许」出现，但下标更小者优先
    controls = [("始终允许", (0, 0, 1, 1)), ("仅在使用中允许", (0, 0, 2, 2))]
    assert _match_permission_button(controls, "grant") == ("仅在使用中允许", (0, 0, 2, 2))


def test_match_deny_picks_min_index():
    controls = [("不允许", (0, 0, 1, 1)), ("拒绝", (0, 0, 2, 2))]
    assert _match_permission_button(controls, "deny") == ("拒绝", (0, 0, 2, 2))


def test_match_skips_media_buttons():
    controls = [
        ("只允许访问所选照片", (0, 0, 1, 1)),
        ("允许访问所有照片", (0, 0, 2, 2)),
    ]
    assert _match_permission_button(controls, "grant") is None


def test_match_skips_settings_button():
    controls = [("前往设置", (0, 0, 1, 1))]
    assert _match_permission_button(controls, "grant") is None


def test_match_strips_whitespace():
    controls = [("  允许  ", (0, 0, 1, 1))]
    assert _match_permission_button(controls, "grant") == ("允许", (0, 0, 1, 1))


def test_match_no_hit_returns_none():
    controls = [("确定", (0, 0, 1, 1))]
    assert _match_permission_button(controls, "grant") is None


def test_match_empty_controls_returns_none():
    assert _match_permission_button([], "grant") is None


# ── _permission_popup_buttons：activity gate + 解析 ────────

def test_permission_popup_returns_none_when_not_permission_activity():
    # 即使有可点击「允许」，非权限 activity 也应直接返回 None
    device = _Device([("允许", _b(0, 0, 2, 2))], activity="com.demo.MainActivity")
    assert pt._permission_popup_buttons(_ctx(device)) is None


def test_permission_popup_parses_clickable():
    device = _Device([("拒绝", _b(0, 0, 2, 2)), ("允许", _b(0, 5, 2, 7))])
    info = pt._permission_popup_buttons(_ctx(device))
    assert info is not None
    activity, controls = info
    assert "permissioncontroller" in activity.lower()
    assert ("拒绝", (0, 0, 2, 2)) in controls
    assert ("允许", (0, 5, 2, 7)) in controls


def test_permission_popup_no_clickable_text_returns_empty():
    device = _Device([("", _b(0, 0, 2, 2))])  # 有 bounds 但无 text
    activity, controls = pt._permission_popup_buttons(_ctx(device))
    assert activity
    assert controls == []


# ── _detect_permission_popup：有界轮询捕捉延迟弹窗 ────────

def test_detect_finds_dialog_after_delay():
    # poll1 非权限页 → None；poll2 权限弹窗出现 → 命中
    device = _SeqDevice([
        ("com.demo.MainActivity", [("允许", _b(0, 0, 2, 2))]),
        ("com.android.permissioncontroller.PermissionActivity", [("允许", _b(0, 0, 2, 2))]),
    ])
    with patch("tools.perceive_tools.time.sleep", lambda *a, **k: None):
        info = pt._detect_permission_popup(_ctx(device), timeout=1.0)
    assert info is not None
    activity, controls = info
    assert "permissioncontroller" in activity.lower()
    assert ("允许", (0, 0, 2, 2)) in controls


def test_detect_returns_none_on_timeout():
    device = _SeqDevice([("com.demo.MainActivity", [("允许", _b(0, 0, 2, 2))])])
    with patch("tools.perceive_tools.time.sleep", lambda *a, **k: None):
        info = pt._detect_permission_popup(_ctx(device), timeout=0.3)
    assert info is None


# ── _maybe_auto_handle_permission：hint 路径 ───────────────

def test_auto_grant_clicks_min_permission_button():
    device = _Device([
        ("始终允许", _b(0, 0, 1, 1)),
        ("仅在使用中允许", _b(0, 0, 2, 2)),
    ])
    result = _maybe_auto_handle_permission(_ctx(device), "grant", "测试点击")
    assert result is not None
    assert result["permission_auto_handled"] == "grant"
    assert result["permission_auto_button"] == "仅在使用中允许"
    assert result["permission_auto_result"] == "handled"
    assert result["permission_auto_trigger"] == "测试点击"
    assert result["permission_dialog"] == "true"
    assert result["permission_state"] == "awaiting_response"
    assert device.click_bounds_calls == [((0, 0, 2, 2))]


def test_auto_deny_clicks_reject():
    device = _Device([
        ("仅在使用中允许", _b(0, 0, 1, 1)),
        ("拒绝", _b(0, 0, 2, 2)),
    ])
    result = _maybe_auto_handle_permission(_ctx(device), "deny", "测试点击")
    assert result is not None
    assert result["permission_auto_handled"] == "deny"
    assert result["permission_auto_button"] == "拒绝"
    assert device.click_bounds_calls == [((0, 0, 2, 2))]


def test_auto_fallback_match_no_click():
    # 弹窗出现但无 grant/deny 匹配按钮 → 不自动点，回第二层
    device = _Device([("确定", _b(0, 0, 2, 2))])
    result = _maybe_auto_handle_permission(_ctx(device), "grant", "测试点击")
    assert result is not None
    assert result["permission_auto_result"] == "fallback_match"
    assert "permission_auto_handled" not in result
    assert device.click_bounds_calls == []


def test_auto_fallback_error_on_click_failure():
    device = _ErrDevice([("允许", _b(0, 0, 2, 2))])
    result = _maybe_auto_handle_permission(_ctx(device), "grant", "测试点击")
    assert result is not None
    assert result["permission_auto_result"] == "fallback_error"
    assert "permission_auto_handled" not in result


def test_auto_no_dialog_returns_none():
    # 弹窗始终不出现 → 返回 None，且不调用 click_bounds
    device = _SeqDevice([("com.demo.MainActivity", [("允许", _b(0, 0, 2, 2))])])
    clock = {"t": 0.0}

    def _fake_monotonic():
        clock["t"] += 5.0
        return clock["t"]

    with patch("tools.perceive_tools.time.monotonic", _fake_monotonic), \
         patch("tools.perceive_tools.time.sleep", lambda *a, **k: None):
        result = _maybe_auto_handle_permission(_ctx(device), "grant", "测试点击")
    assert result is None
    assert device.click_bounds_calls == []


def test_intent_auto_handles_permission():
    """intent 路径：ctx 有 _permission_intent 时自动监听并处理。"""
    import time as _time
    device = _Device([("拒绝", _b(0, 0, 2, 2)), ("允许", _b(0, 5, 2, 7))])
    ctx = SimpleNamespace(
        device=device,
        _permission_intent={"permission": "camera", "action": "deny", "set_time": _time.monotonic()},
    )
    result = _maybe_auto_handle_permission(ctx, "", "拍照导入")
    assert result is not None
    assert result["permission_auto_handled"] == "deny"
    assert result["permission_auto_button"] == "拒绝"
    assert result["permission_auto_trigger"] == "拍照导入"
    assert result["permission_intent_type"] == "camera"
    assert device.click_bounds_calls == [((0, 0, 2, 2))]


def test_intent_ttl_expired():
    """intent 超过 120s TTL 后视为未设置。"""
    import time as _time
    device = _Device([("拒绝", _b(0, 0, 2, 2))])
    ctx = SimpleNamespace(
        device=device,
        _permission_intent={"permission": "camera", "action": "deny", "set_time": _time.monotonic() - 200.0},
    )
    # intent 过期 → 走默认路径（单次检测、布尔字段、不自动点）
    result = _maybe_auto_handle_permission(ctx, "", "拍照导入")
    assert result is not None
    assert result["permission_dialog"] is True  # 布尔字段，默认路径
    assert "permission_auto_handled" not in result
    assert device.click_bounds_calls == []


# ── _maybe_auto_handle_permission：非 hint 路径（回归 #6）──

def test_nonhint_dialog_reports_boolean_and_no_click():
    # 改动前行为：单次检测、布尔 permission_dialog、不自动点
    device = _Device([("允许", _b(0, 0, 2, 2))])
    result = _maybe_auto_handle_permission(_ctx(device), "", "测试点击")
    assert result is not None
    assert result["permission_dialog"] is True  # 布尔，保持改动前
    assert result["permission_activity"] == "com.android.permissioncontroller.PermissionActivity"
    assert result["permission_buttons"] == "允许"
    assert "permission_auto_handled" not in result
    assert device.click_bounds_calls == []


def test_nonhint_no_dialog_returns_none():
    device = _Device([], activity="com.demo.MainActivity")
    assert _maybe_auto_handle_permission(_ctx(device), "", "测试点击") is None

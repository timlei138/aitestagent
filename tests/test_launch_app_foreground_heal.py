"""launch_app 前台归位自愈回归（2026-08-25 用例168 回放复盘）。

场景：上轮遗留的系统照片选择器（他包窗口）盖在屏幕顶层，force_fresh 只杀目标包
杀不掉它——启动后前台仍是他包，到达契约误判失败，direct 回放首动作即降级。
修复：检测到「他包在前台」时按 HOME 归位并重试启动一次；仍不达才如实 ERROR。
"""

from __future__ import annotations

from types import SimpleNamespace

import tools.device_ops as device_ops

HOME = ("shell", ("input", "keyevent", "KEYCODE_HOME"))


class _CoveredDevice:
    """启动后前台被他包占据；HOME 归位重试后（或永不，视 healable）到达目标包。"""

    def __init__(self, healable: bool = True):
        self.calls: list[tuple] = []
        self._healable = healable
        self._home_pressed = False

    def app_stop(self, package: str) -> None:
        self.calls.append(("stop", package))

    def app_start(self, package: str, activity: str = "") -> None:
        self.calls.append(("start", package, activity))

    def shell(self, cmd) -> None:
        self.calls.append(("shell", tuple(cmd)))
        if tuple(cmd) == ("input", "keyevent", "KEYCODE_HOME"):
            self._home_pressed = True

    def current_app(self, refresh: bool = False) -> dict:
        if self._healable and self._home_pressed:
            return {
                "package": "com.zui.calendar",
                "activity": ".AllInOneActivity",
            }
        return {
            "package": "com.android.providers.media.module",
            "activity": "PhotoPickerGetContentActivity",
        }


def _run(monkeypatch, device: _CoveredDevice) -> str:
    monkeypatch.setattr(
        device_ops, "get_tool_context", lambda: SimpleNamespace(device=device)
    )
    return device_ops.launch_app.invoke(
        {"package": "com.zui.calendar", "force_fresh": True}
    )


def test_heals_foreign_package_on_top_and_arrives(monkeypatch):
    device = _CoveredDevice(healable=True)
    out = _run(monkeypatch, device)
    assert out.startswith("OK:")
    assert HOME in device.calls  # 检测到他包盖顶 → 按 HOME 归位
    # 归位后重试了一次启动
    assert sum(1 for c in device.calls if c[0] == "start") == 2


def test_reports_error_honestly_when_heal_fails(monkeypatch):
    device = _CoveredDevice(healable=False)
    out = _run(monkeypatch, device)
    assert out.startswith("ERROR:")
    assert HOME in device.calls  # 自愈尝试过但不奏效
    assert "foreground_healed=True" in out or "foreground_healed" in out


def test_no_heal_when_target_already_on_top(monkeypatch):
    class _CleanDevice(_CoveredDevice):
        def current_app(self, refresh: bool = False) -> dict:
            return {"package": "com.zui.calendar", "activity": ".AllInOneActivity"}

    device = _CleanDevice()
    out = _run(monkeypatch, device)
    assert out.startswith("OK:")
    assert HOME not in device.calls  # 起点干净 → 不触发归位

from __future__ import annotations

from device.controller import DeviceController


class _ShellResult:
    def __init__(self, output: str):
        self.output = output


class _FakeDevice:
    """Fake uiautomator2 device，只模拟 shell 返回（当前 current_app 只走 dumpsys）。"""

    def __init__(self, shell_output: str = ""):
        self._shell_output = shell_output

    def shell(self, cmd):
        return _ShellResult(self._shell_output)


def _controller_with(fake: _FakeDevice) -> DeviceController:
    controller = DeviceController.__new__(DeviceController)
    controller.device = fake
    return controller


def test_current_app_parses_topResumedActivity():
    """dumpsys 输出含 topResumedActivity 时，应解析为当前前台应用。"""
    fake = _FakeDevice(
        shell_output=(
            "  topResumedActivity=ActivityRecord{abc u0 com.android.settings/.Settings ...}\n"
            "  mResumedActivity: ActivityRecord{def u0 com.tblenovo.center/.Splash ...}"
        ),
    )
    controller = _controller_with(fake)
    current = controller.current_app()
    assert current["package"] == "com.android.settings"
    assert current["activity"] == ".Settings"


def test_current_app_falls_back_to_mCurrentFocus():
    """topResumedActivity / mResumedActivity 都没匹配时，回退到 mCurrentFocus。"""
    fake = _FakeDevice(
        shell_output="mCurrentFocus=Window{42 u0 com.android.settings/.Settings}",
    )
    controller = _controller_with(fake)
    current = controller.current_app()
    assert current["package"] == "com.android.settings"
    assert current["activity"] == ".Settings"


def test_current_app_returns_empty_on_no_match():
    """dumpsys 输出无法解析时，返回空 package。"""
    fake = _FakeDevice(shell_output="some unrelated output")
    controller = _controller_with(fake)
    current = controller.current_app()
    assert current["package"] == ""
    assert current["activity"] == ""

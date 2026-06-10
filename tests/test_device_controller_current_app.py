from __future__ import annotations

from core.device_controller import DeviceController


class _ShellResult:
    def __init__(self, output: str):
        self.output = output


class _FakeDevice:
    def __init__(self, app_current_payload=None, shell_output=""):
        self._app_current_payload = app_current_payload
        self._shell_output = shell_output

    def app_current(self):
        if isinstance(self._app_current_payload, Exception):
            raise self._app_current_payload
        return self._app_current_payload

    def shell(self, cmd: str):
        return _ShellResult(self._shell_output)


def _controller_with(fake: _FakeDevice) -> DeviceController:
    controller = DeviceController.__new__(DeviceController)
    controller.device = fake
    return controller


def test_current_app_prefers_app_current_when_available():
    fake = _FakeDevice(
        app_current_payload={
            "package": "com.android.settings",
            "activity": ".Settings",
        },
        shell_output="mCurrentFocus=Window{ u0 com.tblenovo.center/.SplashActivity}",
    )
    controller = _controller_with(fake)
    current = controller.current_app()
    assert current["package"] == "com.android.settings"


def test_current_app_falls_back_to_dumpsys_when_app_current_empty():
    fake = _FakeDevice(
        app_current_payload={"package": "", "activity": ""},
        shell_output="mCurrentFocus=Window{42 u0 com.android.settings/.Settings}",
    )
    controller = _controller_with(fake)
    current = controller.current_app()
    assert current["package"] == "com.android.settings"
    assert current["activity"] == ".Settings"

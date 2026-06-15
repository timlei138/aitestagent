from __future__ import annotations

from device.controller import DeviceController


class _ShellResult:
    def __init__(self, output: str):
        self.output = output


class _FakeU2Device:
    def __init__(self):
        self.started: list[tuple[str, str | None]] = []

    def shell(self, cmd: str):
        if "query-activities" in cmd:
            return _ShellResult(
                "\n".join(
                    [
                        "com.demo.app/.LeakLauncherActivity",
                        "com.demo.app/.SplashActivity",
                    ]
                )
            )
        if "resolve-activity" in cmd:
            return _ShellResult("com.demo.app/.LeakLauncherActivity")
        return _ShellResult("")

    def app_start(self, package: str, activity: str | None = None):
        self.started.append((package, activity))


def _controller_with(fake: _FakeU2Device) -> DeviceController:
    controller = DeviceController.__new__(DeviceController)
    controller.device = fake
    return controller


def test_app_start_auto_avoids_leak_launcher():
    fake = _FakeU2Device()
    controller = _controller_with(fake)
    controller.app_start("com.demo.app")
    assert fake.started == [("com.demo.app", ".SplashActivity")]


def test_app_start_with_explicit_activity_kept():
    fake = _FakeU2Device()
    controller = _controller_with(fake)
    controller.app_start("com.demo.app", activity=".MainActivity")
    assert fake.started == [("com.demo.app", ".MainActivity")]


def test_app_start_explicit_leak_activity_is_overridden():
    fake = _FakeU2Device()
    controller = _controller_with(fake)
    controller.app_start("com.demo.app", activity=".LeakLauncherActivity")
    assert fake.started == [("com.demo.app", ".SplashActivity")]

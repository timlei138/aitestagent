from __future__ import annotations

import json

from core.tool_context import ToolContext
from core.tools import get_primary_launch_activity, launch_app, set_tool_context


class FakeDevice:
    def __init__(self, resolved: str | None):
        self.resolved = resolved
        self.calls: list[tuple[str, str | None]] = []
        self.last_excluded: list[str] = []

    def resolve_launch_activity(
        self, package: str, excluded_keywords: list[str] | None = None
    ) -> str | None:
        self.last_excluded = excluded_keywords or []
        return self.resolved

    def list_launcher_activities(self, package: str) -> list[str]:
        return [".LeakLauncherActivity", ".SplashActivity"]

    def app_start(self, package: str, activity: str | None = None):
        self.calls.append((package, activity))


def _set_ctx(device: FakeDevice) -> None:
    set_tool_context(
        ToolContext(
            device=device,
            perceiver=None,
            baseline_store=None,
            anomaly_detector=None,
        )
    )


def test_launch_app_uses_resolved_activity():
    device = FakeDevice(".SplashActivity")
    _set_ctx(device)
    message = launch_app.invoke({"package": "com.demo.app"})
    assert device.calls == [("com.demo.app", ".SplashActivity")]
    assert "com.demo.app/.SplashActivity" in message
    assert "LeakLauncherActivity" in ",".join(device.last_excluded)


def test_launch_app_falls_back_when_no_activity():
    device = FakeDevice(None)
    _set_ctx(device)
    message = launch_app.invoke({"package": "com.demo.app"})
    assert device.calls == [("com.demo.app", None)]
    assert message == "已启动: com.demo.app"


def test_get_primary_launch_activity_returns_json_payload():
    device = FakeDevice(".SplashActivity")
    _set_ctx(device)
    payload = get_primary_launch_activity.invoke({"package": "com.demo.app"})
    data = json.loads(payload)
    assert data["package"] == "com.demo.app"
    assert data["activity"] == ".SplashActivity"
    assert ".LeakLauncherActivity" in data["candidates"]

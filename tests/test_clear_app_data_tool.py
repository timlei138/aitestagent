"""clear_app_data 工具回归：注册在位 + 防误触确认 + 设备调用透传。

背景（用例 168 复盘）：prompt（agent_common「冷启动意图识别」节）指示 agent 调用
clear_app_data，但该工具从未注册进 AGENT_TOOLS——前提「清空APP数据」被静默降级
为 launch_app(force_fresh)，清空语义从未执行。本组测试锁住注册与调用契约。
"""

from __future__ import annotations

from types import SimpleNamespace

from tools import AGENT_TOOLS
import tools.device_ops as device_ops


class _FakeDevice:
    def __init__(self, result: str = "Success", exc: Exception | None = None):
        self._result = result
        self._exc = exc
        self.calls: list[str] = []

    def clear_app_data(self, package: str) -> str:
        self.calls.append(package)
        if self._exc:
            raise self._exc
        return self._result


def _patch_ctx(monkeypatch, device) -> None:
    monkeypatch.setattr(
        device_ops, "get_tool_context", lambda: SimpleNamespace(device=device)
    )


def test_clear_app_data_is_registered_agent_tool():
    # prompt（agent_common.txt「清理 APP 数据」节）引用的工具必须真实可绑定，
    # 否则就是又一个「prompt 承诺、工具箱不兑现」的脱节。
    assert "clear_app_data" in {t.name for t in AGENT_TOOLS}


def test_clear_app_data_requires_exact_confirmation(monkeypatch):
    device = _FakeDevice()
    _patch_ctx(monkeypatch, device)
    out = device_ops.clear_app_data.invoke(
        {"package": "com.zui.calendar", "confirmation": "CLEAR_DATA:com.other"}
    )
    assert out.startswith("NEEDS_HUMAN:")
    assert device.calls == []  # 确认不符时不得触碰设备


def test_clear_app_data_calls_device_when_confirmed(monkeypatch):
    device = _FakeDevice(result="Success")
    _patch_ctx(monkeypatch, device)
    out = device_ops.clear_app_data.invoke(
        {"package": "com.zui.calendar", "confirmation": "CLEAR_DATA:com.zui.calendar"}
    )
    assert out.startswith("OK:")
    assert "Success" in out
    assert device.calls == ["com.zui.calendar"]


def test_clear_app_data_reports_device_failure(monkeypatch):
    device = _FakeDevice(exc=RuntimeError("pm clear 执行失败: boom"))
    _patch_ctx(monkeypatch, device)
    out = device_ops.clear_app_data.invoke(
        {"package": "com.zui.calendar", "confirmation": "CLEAR_DATA:com.zui.calendar"}
    )
    assert out.startswith("ERROR:")

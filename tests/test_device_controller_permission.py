from __future__ import annotations

from device.controller import DeviceController


class _ShellResult:
    def __init__(self, output: str = "", exit_code: int | None = 0):
        self.output = output
        self.exit_code = exit_code


class _FakeU2Device:
    """记录 shell 调用，可按命令前缀模拟 adb 返回。"""

    def __init__(self, responses: dict[str, _ShellResult] | None = None):
        self.responses = responses or {}
        self.calls: list[list[str]] = []

    def shell(self, cmd):
        self.calls.append(list(cmd))
        key = " ".join(cmd)
        for prefix, res in self.responses.items():
            if key.startswith(prefix):
                return res
        return _ShellResult("")


def _controller_with(fake: _FakeU2Device) -> DeviceController:
    controller = DeviceController.__new__(DeviceController)
    controller.device = fake
    return controller


def test_grant_permission_resolves_alias_and_calls_pm_grant():
    fake = _FakeU2Device({"pm grant": _ShellResult("granted")})
    ctl = _controller_with(fake)
    out = ctl.grant_permission("com.demo.app", "camera")
    assert out == "granted"
    assert fake.calls[-1] == [
        "pm",
        "grant",
        "com.demo.app",
        "android.permission.CAMERA",
    ]


def test_revoke_permission_calls_pm_revoke():
    fake = _FakeU2Device({"pm revoke": _ShellResult("revoked")})
    ctl = _controller_with(fake)
    out = ctl.revoke_permission("com.demo.app", "camera")
    assert out == "revoked"
    assert fake.calls[-1] == [
        "pm",
        "revoke",
        "com.demo.app",
        "android.permission.CAMERA",
    ]


def test_grant_falls_back_to_appops_for_special_permission():
    # pm grant 对特殊权限返回 "not a changeable permission" → 回退 cmd appops
    fake = _FakeU2Device(
        {
            "pm grant": _ShellResult("not a changeable permission", exit_code=0),
            "cmd appops": _ShellResult("appops ok"),
        }
    )
    ctl = _controller_with(fake)
    out = ctl.grant_permission("com.demo.app", "android.permission.SYSTEM_ALERT_WINDOW")
    assert "appops" in out
    assert fake.calls[-1] == [
        "cmd",
        "appops",
        "set",
        "com.demo.app",
        "SYSTEM_ALERT_WINDOW",
        "allow",
    ]


def test_invalid_package_rejected():
    fake = _FakeU2Device()
    ctl = _controller_with(fake)
    try:
        ctl.grant_permission("not a package", "camera")
        assert False, "应抛出 ValueError"
    except ValueError:
        pass


def test_set_runtime_permission_tool_grants_common():
    from tools.perceive_tools import set_runtime_permission
    from tools.context import ToolContext

    fake = _FakeU2Device({"pm grant": _ShellResult("granted")})
    ctl = _controller_with(fake)
    ctx = ToolContext(device=ctl, perceiver=None)
    import tools.perceive_tools as pt

    pt.get_tool_context = lambda: ctx  # 临时注入上下文（测试替身）

    result = set_runtime_permission.func("com.demo.app", include_common=True)
    # 11 个常用别名，其中 storage/calendar/contacts 各展开为读写 2 项 → 14 项全成功 → OK
    assert "已处理 14/14" in result
    assert "OK" in result


def test_grant_media_permission_resolves_alias_via_pm_grant():
    # 媒体权限是普通运行时权限，pm grant 本就支持，直接走 pm grant（不经 appops 回退）
    fake = _FakeU2Device({"pm grant": _ShellResult("granted")})
    ctl = _controller_with(fake)

    # 选择照片
    ctl.grant_permission("com.demo.app", "photos")
    assert fake.calls[-1] == [
        "pm", "grant", "com.demo.app", "android.permission.READ_MEDIA_IMAGES",
    ]
    # 选择视频
    ctl.grant_permission("com.demo.app", "select_videos")
    assert fake.calls[-1] == [
        "pm", "grant", "com.demo.app", "android.permission.READ_MEDIA_VIDEO",
    ]
    # 音频
    ctl.grant_permission("com.demo.app", "audio")
    assert fake.calls[-1] == [
        "pm", "grant", "com.demo.app", "android.permission.READ_MEDIA_AUDIO",
    ]
    # 部分媒体访问（Android 14「仅所选」）
    ctl.grant_permission("com.demo.app", "visual_selected")
    assert fake.calls[-1] == [
        "pm", "grant", "com.demo.app",
        "android.permission.READ_MEDIA_VISUAL_USER_SELECTED",
    ]


def test_media_permission_pm_failure_raises_no_appops_fallback():
    # 回归保护：媒体权限 pm grant 失败时应直接抛错，不回退 cmd appops
    fake = _FakeU2Device(
        {"pm grant": _ShellResult("not a changeable permission", exit_code=1)}
    )
    ctl = _controller_with(fake)
    try:
        ctl.grant_permission("com.demo.app", "photos")
        assert False, "媒体权限 pm 失败后不应回退 appops"
    except RuntimeError:
        pass


def test_revoke_media_permission_calls_pm_revoke():
    # 撤销媒体权限同样直接走 pm revoke（不经 appops 回退）
    fake = _FakeU2Device({"pm revoke": _ShellResult("revoked")})
    ctl = _controller_with(fake)
    ctl.revoke_permission("com.demo.app", "videos")
    assert fake.calls[-1] == [
        "pm", "revoke", "com.demo.app", "android.permission.READ_MEDIA_VIDEO",
    ]


def test_set_runtime_permission_tool_reports_partial_failure():
    from tools.perceive_tools import set_runtime_permission
    from tools.context import ToolContext

    # body_sensors 无 appops 回退；其 pm grant 失败时整个权限应 FAIL，
    # 而 camera pm grant 成功 → 部分失败（1/2 + FAIL 明细）。
    class _FlakyDevice(_FakeU2Device):
        def shell(self, cmd):
            self.calls.append(list(cmd))
            j = " ".join(cmd)
            if "pm grant" in j and "BODY_SENSORS" in j:
                return _ShellResult("failure", exit_code=1)
            return _ShellResult("granted")

    fake = _FlakyDevice()
    ctl = _controller_with(fake)
    ctx = ToolContext(device=ctl, perceiver=None)
    import tools.perceive_tools as pt

    pt.get_tool_context = lambda: ctx

    result = set_runtime_permission.func(
        "com.demo.app", permissions="camera,body_sensors", action="grant"
    )
    assert "FAIL" in result
    assert "已处理 1/2" in result

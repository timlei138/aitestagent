"""设备动作类工具（copy/paste/输入/按键/滑动/方向/启动 App 等）。

从 tools/__init__.py 拆出（重构 T4），仅移动代码、不改逻辑。
"""

from __future__ import annotations

import time

from tools.context import get_tool_context

try:
    from langchain_core.tools import tool
except Exception:

    def tool(func=None, *args, **kwargs):
        def wrapper(f):
            return f

        return wrapper(func) if func else wrapper


@tool
def copy() -> str:
    """复制当前选中内容到剪贴板。应在 long_press 触发系统弹窗后调用，自动点击弹窗中的"复制"按钮。"""
    ctx = get_tool_context()
    if ctx.device is None:
        return "ERROR: 未连接 Android 设备"
    strategy = ctx.device.copy()
    return f"已复制 (strategy={strategy})"


@tool
def paste() -> str:
    """粘贴剪贴板内容到当前焦点输入框。依次尝试 CTRL+V → KEYCODE_PASTE → 系统弹窗"粘贴"按钮。粘贴后应用 get_screen_info 验证内容是否已成功出现在输入框中。"""
    ctx = get_tool_context()
    if ctx.device is None:
        return "ERROR: 未连接 Android 设备"
    strategy = ctx.device.paste()
    msg = f"已粘贴 (strategy={strategy})"
    if strategy == "paste_ctrl_v":
        msg += ' | 请用 get_screen_info 确认内容已出现在输入框，如未出现则手动 long_press 输入框后 click("粘贴")'
    return msg


@tool
def type_input(text: str) -> str:
    """向当前已聚焦的输入框输入文本。"""
    ctx = get_tool_context()
    if ctx.device is None:
        return "ERROR: 未连接 Android 设备"
    ctx.device.type_text(text)
    return f"已输入: {text}"


@tool
def press_key(key: str) -> str:
    """按系统键。key 可为 back / home / enter / recent / power（电源键，锁屏或亮屏）。"""
    ctx = get_tool_context()
    if ctx.device is None:
        return "ERROR: 未连接 Android 设备"
    ctx.device.press(key)
    return f"已按键: {key}"


@tool
def swipe(direction: str = "up") -> str:
    """滑动屏幕。direction 可为 up / down / left / right。"""
    ctx = get_tool_context()
    if ctx.device is None:
        return "ERROR: 未连接 Android 设备"
    ctx.device.swipe(direction)
    return f"已滑动: {direction}"


@tool
def open_notification() -> str:
    """打开通知栏。系统级操作：横屏时等效于从顶部左侧下滑，竖屏时从顶部下滑。"""
    ctx = get_tool_context()
    if ctx.device is None:
        return "ERROR: 未连接 Android 设备"
    ctx.device.open_notification()
    return "已打开通知栏"


@tool
def open_quick_settings() -> str:
    """打开快速设置面板（Quick Settings / 控制中心）。系统级操作：横屏时等效于从顶部右侧下滑，竖屏时从顶部下滑。"""
    ctx = get_tool_context()
    if ctx.device is None:
        return "ERROR: 未连接 Android 设备"
    ctx.device.open_quick_settings()
    return "已打开快速设置面板"


@tool
def unlock_screen() -> str:
    """解锁屏幕（swipe up 解锁，适用于无密码/图案的测试设备）。亮屏后若处于锁屏界面需调用此工具。"""
    ctx = get_tool_context()
    if ctx.device is None:
        return "ERROR: 未连接 Android 设备"
    ctx.device.unlock()
    return "已解锁屏幕"


@tool
def set_orientation(orientation: str = "portrait") -> str:
    """设置屏幕方向。orientation: portrait（竖屏）/ landscape（横屏）。"""
    ctx = get_tool_context()
    if ctx.device is None:
        return "ERROR: 未连接 Android 设备"
    mapping = {"portrait": "natural", "landscape": "left"}
    raw = mapping.get(orientation, orientation)
    ctx.device.set_orientation(raw)
    return f"已设置屏幕方向: {orientation}"


@tool
def toggle_auto_rotate(enable: bool = True) -> str:
    """启用或禁用屏幕自动旋转（等同于控制中心/Quick Settings 中的"自动旋转"开关）。
    enable=True 开启自动旋转，设备横屏时 App 随重力感应切换布局；
    enable=False 关闭自动旋转，设备方向锁定，App 不会随横屏切换。"""
    ctx = get_tool_context()
    if ctx.device is None:
        return "ERROR: 未连接 Android 设备"
    ctx.device.freeze_rotation(not enable)
    action = "开启" if enable else "关闭"
    return f"已{action}自动旋转"


@tool
def check_desktop_mode(mode: str = "dw") -> str:
    """检查当前是否处于指定桌面模式。mode 可选: dw（无限工作台/CustomModeLauncher，检查 zui_ov_desktop_mode==1）。
    返回当前模式和判断结果，供 Agent 在操作前确认环境状态。"""
    ctx = get_tool_context()
    if ctx.device is None:
        return "ERROR: 未连接 Android 设备"

    key_map = {"dw": "zui_ov_desktop_mode"}
    key = key_map.get(mode, mode)
    val = ctx.device.get_system_setting(key, "system")
    if mode == "dw":
        is_dw = val == "1"
        return f"桌面模式: {'无限工作台' if is_dw else '普通桌面'} (zui_ov_desktop_mode={val or '0'})"
    return f"{key}={val or '(未设置)'}"


@tool
def scroll_panel(panel: str = "left_navigation", direction: str = "down") -> str:
    """在特定面板内滚动（用于导航栏或内容区有折叠项时）。
    panel: left_navigation（左侧导航栏）/ right_content（右侧内容区）
    direction: down（往下翻, 露出下方内容）/ up（往上翻, 露出上方内容）
    """
    ctx = get_tool_context()
    if ctx.device is None:
        return "ERROR: 未连接 Android 设备"
    if ctx.perceiver is None:
        return "ERROR: Perceiver not available"

    understanding = ctx.perceiver.perceive()
    screen_w = understanding.width or 1080
    screen_h = understanding.height or 1920

    if understanding.layout == "two_pane":
        if panel == "left_navigation":
            x_center = int(screen_w * 0.22)
        else:
            x_center = int(screen_w * 0.72)
    else:
        x_center = screen_w // 2

    # 滚动前记录当前可见元素（检测是否已到末尾）
    pre_labels = {e.label for e in understanding.elements if e.label and e.clickable}

    # 方向语义：down=往下翻(露出下方内容)→手指从下往上划
    #           up=往上翻(露出上方内容)→手指从上往下划
    # 滑动距离 = 2/3 屏幕高度
    if direction == "down":
        y_from = int(screen_h * 0.85)
        y_to = int(screen_h * 0.15)
    else:
        y_from = int(screen_h * 0.15)
        y_to = int(screen_h * 0.85)

    try:
        ctx.device.device.swipe(x_center, y_from, x_center, y_to, duration=0.3)
    except Exception as exc:
        return f"ERROR: 滚动{panel}失败: {exc}"

    # 滚动后检查元素是否有变化
    time.sleep(0.5)  # 等 UI 刷新
    try:
        post = ctx.perceiver.perceive()
        post_labels = {e.label for e in post.elements if e.label and e.clickable}
        new_count = len(post_labels - pre_labels)
        if new_count == 0:
            return f"已滚动{panel}: {direction}（已到末尾，无新元素）"
        return f"已滚动{panel}: {direction}（新增 {new_count} 个元素）"
    except Exception:
        return f"已滚动{panel}: {direction}"


@tool
def launch_app(
    package: str,
    activity: str = "",
) -> str:
    """启动指定包名的 App。"""
    # 延迟 import 避免加载期循环依赖（click 相关 helper 仍在 tools/__init__.py）
    from tools import _capture_page_id, _record_page_transition

    ctx = get_tool_context()
    if ctx.device is None:
        return "ERROR: 未连接 Android 设备，无法启动应用"
    _pre_page = _capture_page_id(ctx)
    target_activity = (activity or "").strip()
    if target_activity:
        ctx.device.app_start(package, activity=target_activity)
    else:
        ctx.device.app_start(package)
    # 记录页面跳转
    _record_page_transition(ctx, _pre_page, f"launch_app({package})")
    return (
        f"已启动: {package}/{target_activity}"
        if target_activity
        else f"已启动: {package}"
    )

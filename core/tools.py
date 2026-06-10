from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any

from core.device_controller import DeviceController
from core.smart_perceiver import PerceptionMode
from core.tool_context import ToolContext

logger = logging.getLogger(__name__)

_CONTEXT: ToolContext | None = None


def init_device(serial: str | None = None) -> DeviceController:
    return DeviceController(serial)


def set_tool_context(context: ToolContext) -> None:
    global _CONTEXT
    _CONTEXT = context


def get_tool_context() -> ToolContext:
    if _CONTEXT is None:
        raise RuntimeError("ToolContext 未初始化")
    return _CONTEXT


try:
    from langchain_core.tools import tool
except Exception:

    def tool(func=None, *args, **kwargs):
        def wrapper(f):
            return f

        return wrapper(func) if func else wrapper


@tool
def get_screen_info() -> str:
    """获取当前页面的结构化语义信息。"""
    understanding = get_tool_context().perceiver.perceive()
    lines = [understanding.summary, f"layout={understanding.layout}"]
    for item in understanding.primary_paths[:30]:
        lines.append(f"- [{item.region}/{item.role}] {item.label} bounds={item.bounds}")
    return "\n".join(lines)


@tool
def click(label: str) -> str:
    """点击页面上指定文本、描述或资源 id 的元素。"""
    ctx = get_tool_context()
    if ctx.safety_guard:
        decision = ctx.safety_guard.check_click(label)
        if not decision.allowed:
            return f"已阻止点击: {decision.reason}"
    if ctx.device.click_text(label):
        return f"已点击: {label}"
    if ctx.device.click_resource_id(label):
        return f"已点击资源: {label}"
    return f"未找到可点击元素: {label}"


@tool
def navigate_to(target: str) -> str:
    """切换到指定导航项或 Tab。"""
    return (
        click.invoke({"label": target}) if hasattr(click, "invoke") else click(target)
    )


@tool
def scroll_find_and_click(label: str, max_swipes: int = 5) -> str:
    """滑动查找并点击指定元素。"""
    ctx = get_tool_context()
    for _ in range(max_swipes + 1):
        if ctx.device.click_text(label, timeout=0.5):
            return f"已找到并点击: {label}"
        ctx.device.swipe("up")
    return f"滑动后仍未找到: {label}"


@tool
def type_input(text: str) -> str:
    """向当前输入框输入文本。"""
    get_tool_context().device.type_text(text)
    return f"已输入: {text}"


@tool
def press_key(key: str) -> str:
    """按系统键，如 back、home、enter。"""
    get_tool_context().device.press(key)
    return f"已按键: {key}"


@tool
def swipe(direction: str = "up") -> str:
    """滑动屏幕，direction 可为 up/down/left/right。"""
    get_tool_context().device.swipe(direction)
    return f"已滑动: {direction}"


@tool
def get_primary_launch_activity(
    package: str, exclude_activity_keywords: str = "LeakLauncherActivity"
) -> str:
    """查询包名对应的主启动 Activity，默认排除 LeakLauncherActivity。"""
    ctx = get_tool_context()
    keywords = [
        item.strip()
        for item in (exclude_activity_keywords or "").split(",")
        if item.strip()
    ]
    logger.info(
        "Tool get_primary_launch_activity package=%s exclude_keywords=%s",
        package,
        keywords,
    )
    activity = None
    candidates: list[str] = []
    resolver = getattr(ctx.device, "resolve_launch_activity", None)
    lister = getattr(ctx.device, "list_launcher_activities", None)
    if callable(lister):
        candidates = lister(package)
    if callable(resolver):
        activity = resolver(package, excluded_keywords=keywords)
    logger.info(
        "Tool get_primary_launch_activity result package=%s activity=%s candidates=%s",
        package,
        activity or "",
        candidates,
    )
    result = {"package": package, "activity": activity or "", "candidates": candidates}
    return json.dumps(result, ensure_ascii=False)


@tool
def launch_app(
    package: str,
    activity: str = "",
    exclude_activity_keywords: str = "LeakLauncherActivity",
) -> str:
    """启动指定包名的 App；未显式给 activity 时自动解析并排除异常 Launcher。"""
    ctx = get_tool_context()
    target_activity = (activity or "").strip()
    logger.info(
        "Tool launch_app input package=%s activity=%s exclude_keywords=%s",
        package,
        target_activity or "",
        exclude_activity_keywords,
    )
    if not target_activity:
        keywords = [
            item.strip()
            for item in (exclude_activity_keywords or "").split(",")
            if item.strip()
        ]
        resolver = getattr(ctx.device, "resolve_launch_activity", None)
        if callable(resolver):
            target_activity = resolver(package, excluded_keywords=keywords) or ""
        logger.info(
            "Tool launch_app resolved package=%s activity=%s",
            package,
            target_activity or "",
        )
    if target_activity:
        ctx.device.app_start(package, activity=target_activity)
        return f"已启动: {package}/{target_activity}"
    ctx.device.app_start(package)
    return f"已启动: {package}"


@tool
def get_detailed_screen() -> str:
    """获取当前页面详细语义分析，优先包含 Vision 说明。"""
    understanding = get_tool_context().perceiver.perceive()
    return str(understanding.to_dict())


@tool
def switch_perception_mode(mode: str) -> str:
    """切换感知模式：ui_tree、vision、hybrid。"""
    ctx = get_tool_context()
    if mode not in {
        PerceptionMode.UI_TREE,
        PerceptionMode.VISION,
        PerceptionMode.HYBRID,
    }:
        return f"不支持的感知模式: {mode}"
    ctx.perceiver.mode = mode
    return f"已切换感知模式: {mode}"


@tool
def check_page_health(app_package: str = "") -> str:
    """检测当前页面异常。"""
    ctx = get_tool_context()
    package = app_package or ctx.device.current_app().get("package", "")
    result = ctx.anomaly_detector.detect(package, check_baseline=False)
    return str(result.to_dict())


@tool
def assert_page_contains(text: str) -> str:
    """断言当前页面包含指定文本。"""
    info = (
        get_screen_info.invoke({})
        if hasattr(get_screen_info, "invoke")
        else get_screen_info()
    )
    return "PASS" if text in info else f"FAIL: 页面不包含 {text}"


@tool
def assert_element_exists(label: str) -> str:
    """断言当前页面存在指定元素。"""
    understanding = get_tool_context().perceiver.perceive()
    labels = [element.label for element in understanding.elements]
    return (
        "PASS" if any(label in item for item in labels) else f"FAIL: 元素不存在 {label}"
    )


@tool
def assert_text_in_list(text: str) -> str:
    """断言页面列表或元素集合中存在指定文本。"""
    return (
        assert_element_exists.invoke({"label": text})
        if hasattr(assert_element_exists, "invoke")
        else assert_element_exists(text)
    )


@tool
def check_against_baseline(app_package: str = "", page_key: str = "") -> str:
    """将当前页面与基线对比。"""
    ctx = get_tool_context()
    package = app_package or ctx.device.current_app().get("package", "")
    result = ctx.anomaly_detector.detect(
        package, page_key=page_key, check_baseline=True
    )
    return str(result.to_dict())


@tool
def recover_from_anomaly(app_package: str = "") -> str:
    """从异常状态恢复：处理弹窗、返回或重启目标应用。"""
    ctx = get_tool_context()
    package = app_package or ctx.device.current_app().get("package", "")
    for text in ["允许", "确定", "同意", "OK", "Allow", "关闭", "知道了"]:
        if ctx.device.click_text(text, timeout=0.5):
            return f"已处理弹窗: {text}"
    ctx.device.press("back")
    current = ctx.device.current_app()
    if package and current.get("package") != package:
        ctx.device.app_start(package)
        return f"已重启应用: {package}"
    return "已尝试返回恢复"


@tool
def query_app_knowledge(query: str, app_package: str = "") -> str:
    """查询 APP 历史知识。"""
    ctx = get_tool_context()
    if not ctx.knowledge_base:
        return "未启用知识库"
    package = app_package or ctx.device.current_app().get("package", "")
    return str(ctx.knowledge_base.query(query, app_package=package))


@tool
def save_current_page_knowledge(page_name: str = "当前页面") -> str:
    """保存当前页面结构到知识库。"""
    ctx = get_tool_context()
    if not ctx.knowledge_base:
        return "未启用知识库"
    package = ctx.device.current_app().get("package", "")
    understanding = ctx.perceiver.perceive()
    count = ctx.knowledge_base.save_ui_structure(
        package, page_name, [e.to_dict() for e in understanding.elements]
    )
    return f"已保存 {count} 条页面知识"


@tool
def save_screenshot(name: str = "") -> str:
    """保存当前截图。"""
    ctx = get_tool_context()
    os.makedirs("storage/screenshots", exist_ok=True)
    filename = name or f"screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    path = os.path.join("storage/screenshots", filename)
    ctx.device.screenshot().save(path)
    return path


@tool
def log_step(message: str) -> str:
    """记录测试步骤。"""
    ctx = get_tool_context()
    if ctx.report_logger:
        ctx.report_logger.log_step(message)
    return f"已记录: {message}"


ALL_TOOLS: list[Any] = [
    click,
    type_input,
    swipe,
    press_key,
    navigate_to,
    scroll_find_and_click,
    launch_app,
    get_primary_launch_activity,
    get_screen_info,
    get_detailed_screen,
    switch_perception_mode,
    assert_element_exists,
    assert_text_in_list,
    assert_page_contains,
    check_page_health,
    check_against_baseline,
    recover_from_anomaly,
    query_app_knowledge,
    save_current_page_knowledge,
    save_screenshot,
    log_step,
]

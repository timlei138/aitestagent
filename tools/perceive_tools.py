"""视觉/页面健康类感知工具（视觉判断、弹窗检测、页面健康、异常恢复等）。

从 tools/__init__.py 拆出（重构 T5），仅移动代码、不改逻辑。
注：`_run_multimodal_from_context` / `_has_meaningful_ui_elements` 仍在
tools/__init__.py，采用函数内延迟 import 以避免加载期循环依赖。
（get_screen_info / find_element 因耦合较深，暂留在 tools/__init__.py。）
"""

from __future__ import annotations

import json
import time
import xml.etree.ElementTree as ET
from typing import Any

import numpy as np

from tools.context import get_tool_context

try:
    from langchain_core.tools import tool
except Exception:

    def tool(func=None, *args, **kwargs):
        def wrapper(f):
            return f

        return wrapper(func) if func else wrapper


@tool
def visual_check(description: str) -> str:
    """基于截图进行视觉判断，返回结构化 JSON：decision/reason/evidence/confidence。"""
    from tools import _run_multimodal_from_context  # 延迟 import 避免循环依赖

    ctx = get_tool_context()
    if ctx.device is None:
        return json.dumps(
            {
                "decision": "unknown",
                "reason": "未连接设备",
                "evidence": "",
                "confidence": "low",
            },
            ensure_ascii=False,
        )
    snap = ctx.device.snapshot()
    prompt = (
        "请根据截图判断描述是否成立，并只返回 JSON。"
        "字段: decision(yes/no/unknown), reason, evidence, confidence(high/medium/low)。"
        f"描述: {description}"
    )
    result = _run_multimodal_from_context(
        prompt=prompt,
        image_base64=snap.image_base64,
        purpose="visual_check",
        strict_json=True,
        timeout_sec=12,
    )
    payload = {
        "decision": (
            result.get("decision", "unknown") if result.get("ok") else "unknown"
        ),
        "reason": result.get("reason", "vision unavailable"),
        "evidence": result.get("evidence", ""),
        "confidence": "medium" if result.get("ok") else "low",
    }
    return json.dumps(payload, ensure_ascii=False)


@tool
def detect_overlay() -> str:
    """检测截图中的弹窗/Toast/浮层遮挡，返回结构化 JSON。"""
    from tools import _run_multimodal_from_context  # 延迟 import 避免循环依赖

    ctx = get_tool_context()
    if ctx.device is None:
        return json.dumps(
            {
                "has_overlay": False,
                "overlay_type": "none",
                "reason": "未连接设备",
                "evidence": "",
                "blocking": False,
            },
            ensure_ascii=False,
        )
    snap = ctx.device.snapshot()
    prompt = (
        "请分析截图是否存在遮挡层（toast/dialog/popup/sheet）。"
        "只返回 JSON，字段: has_overlay(boolean), overlay_type(toast/dialog/popup/sheet/unknown/none),"
        " reason, evidence, blocking(boolean)。"
    )
    result = _run_multimodal_from_context(
        prompt=prompt,
        image_base64=snap.image_base64,
        purpose="detect_overlay",
        strict_json=True,
        timeout_sec=12,
    )
    # _mk_result 在 strict_json 成功解析时已将完整 dict 放入 data 字段
    raw_data = result.get("data") or {}
    if not isinstance(raw_data, dict):
        raw_data = {}
    has_overlay = False
    if result.get("ok"):
        raw_has_overlay = raw_data.get("has_overlay")
        if isinstance(raw_has_overlay, bool):
            has_overlay = raw_has_overlay
        else:
            # JSON 非标准或解析缺字段时，回退到 decision 语义
            has_overlay = str(result.get("decision", "unknown")).lower() == "yes"
    overlay_type = raw_data.get("overlay_type")
    if not isinstance(overlay_type, str) or not overlay_type:
        overlay_type = "unknown" if has_overlay else "none"
    blocking = raw_data.get("blocking")
    if not isinstance(blocking, bool):
        blocking = has_overlay
    payload = {
        "has_overlay": has_overlay,
        "overlay_type": overlay_type,
        "reason": result.get("reason", "vision unavailable"),
        "evidence": result.get("evidence", ""),
        "blocking": blocking,
    }
    return json.dumps(payload, ensure_ascii=False)


@tool
def detect_popup() -> str:
    """检测当前页面是否存在系统弹窗（权限请求, 确认对话框等），返回弹窗按钮列表。"""
    ctx = get_tool_context()
    if ctx.device is None:
        return "ERROR: 未连接 Android 设备"
    root = ET.fromstring(ctx.device.dump_hierarchy())
    keywords = [
        "允许",
        "拒绝",
        "确定",
        "取消",
        "同意",
        "关闭",
        "跳过",
        "知道了",
        "Allow",
        "Deny",
        "OK",
        "Cancel",
        "Agree",
        "Dismiss",
    ]
    buttons: list[str] = []
    for node in root.iter():
        text = node.get("text", "")
        if node.get("clickable") == "true" and text in keywords:
            buttons.append(text)
    if buttons:
        return f"检测到弹窗按钮: {', '.join(buttons)}"
    return "未检测到弹窗"


@tool
def dismiss_popup() -> str:
    """尝试关闭当前弹窗（按优先级点击允许/确定/同意/关闭）。"""
    ctx = get_tool_context()
    if ctx.device is None:
        return "ERROR: 未连接 Android 设备"
    for text in ["允许", "确定", "同意", "OK", "Allow", "关闭", "知道了", "Dismiss"]:
        if ctx.device.click_text(text, timeout=0.5):
            time.sleep(0.5)
            return f"已处理弹窗: {text}"
    return "未找到可关闭的弹窗按钮"


@tool
def wait_seconds(seconds: float = 1.0) -> str:
    """等待指定秒数，用于页面加载或动画完成。"""
    time.sleep(float(seconds))
    return f"已等待 {seconds} 秒"


@tool
def switch_perception_mode(mode: str) -> str:
    """切换感知模式：ui_tree / hybrid。"""
    from device.perceiver import PerceptionMode

    ctx = get_tool_context()
    if ctx.perceiver is None:
        return "不支持切换感知模式: perceiver unavailable"
    if mode not in {PerceptionMode.UI_TREE, PerceptionMode.HYBRID}:
        return f"不支持的感知模式: {mode}"
    ctx.perceiver.mode = mode
    return f"已切换感知模式: {mode}"


@tool
def check_page_health(app_package: str = "") -> str:
    """检测当前页面异常：ANR/崩溃弹窗/白屏/黑屏/单色屏/进程丢失。返回健康状态。"""
    from tools import _has_meaningful_ui_elements  # 延迟 import 避免循环依赖

    ctx = get_tool_context()
    device = ctx.device
    if device is None:
        return "ERROR: 未连接 Android 设备"
    package = app_package or device.current_app().get("package", "")

    anomalies: list[dict[str, Any]] = []

    # ── UI 树健康 ──
    try:
        root = ET.fromstring(device.dump_hierarchy())
        texts = [node.get("text", "") for node in root.iter()]
        if any("无响应" in t or "isn't responding" in t or "ANR" in t for t in texts):
            anomalies.append(
                {"type": "anr", "severity": "critical", "desc": "检测到 ANR 弹窗"}
            )
        if any(
            "已停止运行" in t or "keeps stopping" in t or "has stopped" in t
            for t in texts
        ):
            anomalies.append(
                {"type": "crash", "severity": "critical", "desc": "检测到崩溃弹窗"}
            )
    except Exception as exc:
        anomalies.append(
            {
                "type": "unreachable",
                "severity": "critical",
                "desc": f"无法获取 UI 树: {exc}",
            }
        )

    # ── 颜色检测（无 UI 元素时才做）──
    if not anomalies and not _has_meaningful_ui_elements(device):
        try:
            screenshot = device.screenshot()
            arr = np.array(screenshot)
            white_ratio = float(np.mean(np.all(arr > 240, axis=2)))
            black_ratio = float(np.mean(np.all(arr < 15, axis=2)))
            if white_ratio > 0.95:
                anomalies.append(
                    {
                        "type": "white_screen",
                        "severity": "high",
                        "desc": f"白屏 {white_ratio:.1%}",
                    }
                )
            elif black_ratio > 0.95:
                anomalies.append(
                    {
                        "type": "black_screen",
                        "severity": "high",
                        "desc": f"黑屏 {black_ratio:.1%}",
                    }
                )
            unique_colors = int(len(np.unique(arr.reshape(-1, arr.shape[-1]), axis=0)))
            if unique_colors < 10:
                anomalies.append(
                    {
                        "type": "solid_screen",
                        "severity": "medium",
                        "desc": f"疑似单色屏(颜色数{unique_colors})",
                    }
                )
        except Exception:
            pass

    # ── 进程丢失检测 ──
    if package:
        current = device.current_app()
        if current.get("package") and current.get("package") != package:
            time.sleep(0.4)
            stable = device.current_app()
            if stable.get("package") != package:
                anomalies.append(
                    {
                        "type": "process_lost",
                        "severity": "high",
                        "desc": f"前台应用为 {stable.get('package')}，非预期的 {package}",
                    }
                )

    if not anomalies:
        return "页面健康: 正常"
    return json.dumps({"healthy": False, "anomalies": anomalies}, ensure_ascii=False)


@tool
def recover_from_anomaly(app_package: str = "") -> str:
    """从异常页面恢复：关闭弹窗 → 按返回 → 重启应用。"""
    ctx = get_tool_context()
    device = ctx.device
    if device is None:
        return "ERROR: 未连接 Android 设备"
    package = app_package or device.current_app().get("package", "")

    # 1) 弹窗
    for text in ["允许", "确定", "同意", "OK", "Allow", "关闭", "知道了"]:
        if device.click_text(text, timeout=0.5):
            return f"已处理弹窗: {text}"

    # 2) 返回
    device.press("back")
    time.sleep(0.8)

    # 3) 重启
    current = device.current_app()
    if package and current.get("package") != package:
        device.app_start(package)
        return f"已重启应用: {package}"
    return "已按返回键尝试恢复"

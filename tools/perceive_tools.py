"""视觉/页面健康类感知工具（视觉判断、弹窗检测、页面健康、异常恢复等）。

从 tools/__init__.py 拆出（重构 T5），仅移动代码、不改逻辑。
注：`_run_multimodal_from_context` / `_has_meaningful_ui_elements` 仍在
tools/__init__.py，采用函数内延迟 import 以避免加载期循环依赖。
（get_screen_info / find_element 因耦合较深，暂留在 tools/__init__.py。）
"""

from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from typing import Any

import numpy as np

from tools.context import get_tool_context
from tools.results import AMBIGUOUS, ERROR, NOT_FOUND, OK, make_result

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


_PERMISSION_ACTIVITY_MARKERS = ("permissioncontroller", "grantpermissionsactivity")
_PERMISSION_SETTINGS_BUTTONS = ("前往设置", "Go to settings")


def _permission_popup_buttons(
    ctx: Any,
) -> tuple[str, list[tuple[str, tuple[int, int, int, int]]]] | None:
    """读取系统权限弹窗的当前可点击控件，不作任何点击或授权决定。"""
    try:
        try:
            current = ctx.device.current_app(refresh=True)
        except TypeError:
            # 兼容测试替身或尚未升级的设备适配器。
            current = ctx.device.current_app()
        activity = str(current.get("activity", "") or "")
        if not any(marker in activity.lower() for marker in _PERMISSION_ACTIVITY_MARKERS):
            return None
        root = ET.fromstring(ctx.device.dump_hierarchy())
        controls: list[tuple[str, tuple[int, int, int, int]]] = []
        for node in root.iter():
            if node.get("clickable") != "true":
                continue
            text = (node.get("text") or node.get("content-desc") or "").strip()
            raw_bounds = node.get("bounds", "")
            match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", raw_bounds)
            if text and match:
                controls.append((text, tuple(int(value) for value in match.groups())))
        return activity, controls
    except Exception:
        return None


def _permission_evidence(
    activity: str, controls: list[tuple[str, tuple[int, int, int, int]]]
) -> dict[str, str]:
    labels = [text for text, _ in controls]
    return {
        "permission_dialog": "true",
        "permission_activity": activity,
        "permission_buttons": "|".join(labels),
        "permission_state": (
            "settings_required"
            if any(text in _PERMISSION_SETTINGS_BUTTONS for text in labels)
            else "awaiting_response"
        ),
    }


@tool
def detect_popup() -> str:
    """检测当前弹窗；权限弹窗只返回当前事实，不会自动处理。"""
    ctx = get_tool_context()
    if ctx.device is None:
        return make_result(ERROR, "未连接 Android 设备")

    permission_info = _permission_popup_buttons(ctx)
    if permission_info:
        activity, controls = permission_info
        return make_result(
            OK,
            "检测到系统权限弹窗，请显式调用 respond_to_permission_dialog",
            _permission_evidence(activity, controls),
        )

    try:
        root = ET.fromstring(ctx.device.dump_hierarchy())
    except Exception as exc:
        return make_result(ERROR, f"读取弹窗层级失败: {exc}")
    keywords = [
        "允许", "拒绝", "确定", "取消", "同意", "关闭", "跳过", "知道了",
        "前往设置", "Allow", "Deny", "OK", "Cancel", "Agree", "Dismiss",
    ]
    buttons: list[str] = []
    for node in root.iter():
        text = node.get("text", "")
        if node.get("clickable") == "true" and text in keywords:
            buttons.append(text)
    if buttons:
        return make_result(OK, "检测到弹窗按钮", {"buttons": "|".join(buttons)})
    return make_result(NOT_FOUND, "未检测到弹窗")


@tool
def wait_for_permission_dialog(timeout: float = 3.0) -> str:
    """有限轮询系统权限弹窗，只返回当前真实按钮，不执行授权。"""
    ctx = get_tool_context()
    if ctx.device is None:
        return make_result(ERROR, "未连接 Android 设备")
    deadline = time.monotonic() + max(0.0, min(float(timeout), 8.0))
    while True:
        info = _permission_popup_buttons(ctx)
        if info:
            activity, controls = info
            return make_result(
                OK,
                "权限弹窗已出现，请根据测试意图显式选择按钮",
                _permission_evidence(activity, controls),
            )
        if time.monotonic() >= deadline:
            break
        time.sleep(0.2)
    return make_result(NOT_FOUND, "权限弹窗未在等待时间内出现")


@tool
def respond_to_permission_dialog(button: str, timeout: float = 3.0) -> str:
    """显式响应系统权限弹窗。仅点击调用方指定且当前仍可见的按钮。"""
    ctx = get_tool_context()
    if ctx.device is None:
        return make_result(ERROR, "未连接 Android 设备")
    requested = (button or "").strip()
    if not requested:
        return make_result(ERROR, "必须提供当前权限弹窗中可见的 button 文本")

    deadline = time.monotonic() + max(0.0, min(float(timeout), 8.0))
    while True:
        info = _permission_popup_buttons(ctx)
        if info:
            activity, controls = info
            evidence = _permission_evidence(activity, controls)
            for label, bounds in controls:
                if label == requested:
                    ctx.device.click_bounds(bounds)
                    evidence["selected_button"] = label
                    return make_result(OK, "已按显式请求响应系统权限弹窗", evidence)
            return make_result(
                NOT_FOUND,
                f"当前权限弹窗不存在指定按钮: {requested}",
                evidence,
            )
        if time.monotonic() >= deadline:
            break
        time.sleep(0.2)
    return make_result(NOT_FOUND, "权限弹窗未在等待时间内出现")


@tool
def dismiss_popup() -> str:
    """尝试关闭普通业务弹窗；系统权限弹窗必须由显式权限工具处理。"""
    ctx = get_tool_context()
    if ctx.device is None:
        return make_result(ERROR, "未连接 Android 设备")
    permission_info = _permission_popup_buttons(ctx)
    if permission_info:
        activity, controls = permission_info
        return make_result(
            AMBIGUOUS,
            "当前为系统权限弹窗，请调用 respond_to_permission_dialog(button=...) 明确选择",
            _permission_evidence(activity, controls),
        )
    for text in ["确定", "同意", "OK", "关闭", "知道了", "Dismiss"]:
        if ctx.device.click_text(text, timeout=0.5):
            time.sleep(0.3)
            return make_result(OK, f"已关闭普通弹窗: {text}", {"button": text})
    return make_result(NOT_FOUND, "未找到可关闭的普通弹窗按钮")


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

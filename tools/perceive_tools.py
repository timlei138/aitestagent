"""视觉/页面健康类感知工具（视觉判断、弹窗检测、页面健康、异常恢复等）。

从 tools/__init__.py 拆出（重构 T5），仅移动代码、不改逻辑。
注：`_run_multimodal_from_context` / `_has_meaningful_ui_elements` 仍在
tools/__init__.py，采用函数内延迟 import 以避免加载期循环依赖。
（get_screen_info / find_element 因耦合较深，暂留在 tools/__init__.py。）
"""

from __future__ import annotations

import base64
import json
import re
import time
import xml.etree.ElementTree as ET
from io import BytesIO
from typing import Any

from PIL import Image, ImageDraw, ImageFont

import numpy as np

from tools.context import get_tool_context
from tools.results import (
    AMBIGUOUS,
    ERROR,
    NOT_FOUND,
    OK,
    UNSUPPORTED,
    make_result,
)

import logging

import app_paths

logger = logging.getLogger(__name__)

try:
    from langchain_core.tools import tool
except Exception:

    def tool(func=None, *args, **kwargs):
        def wrapper(f):
            return f

        return wrapper(func) if func else wrapper


@tool
def visual_check(
    description: str, verification_key: str = "", clause_id: str = ""
) -> str:
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
    # UI Tree 已能证明该 clause 达成时，跳过视觉通道（UI Tree 优先，避免多余 VLM 调用）。
    if verification_key and getattr(ctx, "llm_vision_enabled", True) is False:
        from agents.verification import ui_tree_evidence_already_passes

        if ui_tree_evidence_already_passes(ctx, clause_id):
            return json.dumps(
                {
                    "decision": "unknown",
                    "reason": "vision_disabled_but_ui_tree_proven",
                    "evidence": "UI Tree 证据已证明该断言，无需视觉校验",
                    "confidence": "high",
                },
                ensure_ascii=False,
            )
    if not getattr(ctx, "llm_vision_enabled", True):
        return json.dumps(
            {
                "decision": "unsupported",
                "reason": "vision 未启用",
                "evidence": "",
                "confidence": "low",
            },
            ensure_ascii=False,
        )
    snap = ctx.device.snapshot_for_vision()
    # 验证证据点显式保存截图（perceiver 已不再自动落盘）。
    _save_perceive_evidence_screenshot(ctx, verification_key)
    prompt = (
        "请根据截图判断以下描述是否成立，并只返回 JSON。"
        "字段: decision(yes/no/unknown), reason, evidence, confidence(high/medium/low)。\n"
        "规则："
        "- 如果描述是陈述句，判断其是否与截图内容一致（yes/no）\n"
        "- 如果描述是疑问句或祈使句（如'XX是什么'、'描述XX'），"
        "提取截图中可观察到的事实作为 evidence，decision 设为 yes\n"
        "- 不要以'这是疑问句无法判断'为由拒绝，始终提取截图中的视觉事实\n"
        f"描述: {description}"
    )
    result = _run_multimodal_from_context(
        prompt=prompt,
        image_base64=snap.image_base64,
        purpose="visual_check",
        strict_json=True,
        timeout_sec=getattr(ctx, "vision_timeout", 60),
    )
    raw_data = result.get("data") or {}
    confidence = str(raw_data.get("confidence", "") or "").lower()
    if confidence not in {"high", "medium", "low"}:
        confidence = "medium" if result.get("ok") else "low"
    payload = {
        "decision": (
            result.get("decision", "unknown") if result.get("ok") else "unknown"
        ),
        "reason": result.get("reason", "vision unavailable"),
        "evidence": result.get("evidence", ""),
        "confidence": confidence,
    }
    if verification_key and clause_id:
        decision = str(payload["decision"] or "unknown").lower()
        ctx._evidence_events.append(
            {
                "verification_key": verification_key,
                "clause_id": clause_id,
                "channel": "vision_verify",
                "status": (
                    "YES"
                    if decision == "yes"
                    else "NO" if decision == "no" else "UNKNOWN"
                ),
                "authoritative": decision == "no" and payload["confidence"] == "high",
                "fact": payload,
                "artifact_ref": getattr(ctx, "_last_screenshot_path", "") or "",
            }
        )
    return json.dumps(payload, ensure_ascii=False)


def _save_perceive_evidence_screenshot(ctx, verification_key: str) -> None:
    """视觉验证证据点显式截图：保存到 screenshots/{run_id}/evidence_{key}_vision.png。"""
    try:
        if ctx.device is None:
            return
        run_id = getattr(ctx, "_run_tag", "") or "unknown"
        shot_dir = app_paths.SCREENSHOT_DIR / str(run_id)
        shot_dir.mkdir(parents=True, exist_ok=True)
        filename = f"evidence_{verification_key or 'v'}_vision.png"
        path = str(shot_dir / filename)
        ctx.device.screenshot().save(path)
        ctx._last_screenshot_path = path
    except Exception as exc:
        logger.warning("perceive evidence screenshot failed: %s", exc)


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
    snap = ctx.device.snapshot_for_vision()
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
        timeout_sec=getattr(ctx, "vision_timeout", 60),
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
        "available": result.get("ok"),
        "reason": (
            result.get("reason", "vision unavailable")
            + "，检测业务弹窗请改用 detect_popup()（基于 UI 树，不依赖 vision）"
            if not result.get("ok")
            else result.get("reason", "")
        ),
        "evidence": result.get("evidence", ""),
        "blocking": blocking,
    }
    return json.dumps(payload, ensure_ascii=False)


_PERMISSION_ACTIVITY_MARKERS = ("permissioncontroller", "grantpermissionsactivity")
_PERMISSION_SETTINGS_BUTTONS = ("前往设置", "Go to settings")


def _permission_popup_buttons(
    ctx: Any,
) -> tuple[str, list[tuple[str, str, tuple[int, int, int, int]]]] | None:
    """读取系统权限弹窗的当前可点击控件，不作任何点击或授权决定。"""
    try:
        try:
            current = ctx.device.current_app(refresh=True)
        except TypeError:
            # 兼容测试替身或尚未升级的设备适配器。
            current = ctx.device.current_app()
        activity = str(current.get("activity", "") or "")
        if not any(
            marker in activity.lower() for marker in _PERMISSION_ACTIVITY_MARKERS
        ):
            return None
        root = ET.fromstring(ctx.device.dump_hierarchy())
        controls: list[tuple[str, str, tuple[int, int, int, int]]] = []
        for node in root.iter():
            if node.get("clickable") != "true":
                continue
            text = (node.get("text") or node.get("content-desc") or "").strip()
            raw_bounds = node.get("bounds", "")
            match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", raw_bounds)
            if text and match:
                rid = node.get("resource-id") or ""
                controls.append(
                    (text, rid, tuple(int(value) for value in match.groups()))
                )
        return activity, controls
    except Exception:
        return None


def _permission_evidence(
    activity: str,
    controls: list[tuple[str, str, tuple[int, int, int, int]]],
) -> dict[str, str]:
    labels = [item[0] for item in controls]
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


# O1: resource-id → 角色映射（标准 Android permissioncontroller 前缀）
# rid 优先于文案，消除 OEM / 语言差异（如 ZUI「全部允许」vs 其它「允许访问所有照片」）
# 注意：permission_deny_and_dont_ask_again_button 永不自动点（避免设 don't-ask-again）
_GRANT_RID_SCORES = {
    "permission_allow_all_button": 1,  # 媒体全量「全部允许」
    "permission_allow_always_button": 1,  # 始终允许
    "permission_allow_foreground_only_button": 2,
    "permission_allow_button": 2,  # 基本允许（单次）
    "permission_allow_one_time_button": 2,  # 仅本次使用
}
_DENY_RID = "permission_deny_button"
# 文案兜底表（兼容非标准 OEM / 自定义 rid 的设备）
_GRANT_BUTTONS = (
    "仅在使用中允许",
    "仅本次使用时允许",
    "始终允许",
    "允许",
    "全部允许",
    "允许访问所有照片",
    "Allow",
)
_DENY_BUTTONS = ("拒绝", "不允许", "Deny", "Don't allow")


def _match_permission_button(
    controls: list[tuple[str, str, tuple[int, int, int, int]]],
    hint: str,
) -> tuple[str, tuple[int, int, int, int]] | None:
    """按 rid 优先、文案兜底，确定性匹配权限按钮。

    返回 (text, bounds) 或 None。grant 优先「全部/始终」(score 最小)，
    deny 只命中 permission_deny_button，绝不命中 dont_ask_again。
    """
    if hint == "deny":
        for text, rid, bounds in controls:
            if rid.endswith(_DENY_RID) and not rid.endswith("dont_ask_again_button"):
                return (text, bounds)
        return _match_by_text(controls, _DENY_BUTTONS)
    best, best_score = None, None
    for text, rid, bounds in controls:
        for key, score in _GRANT_RID_SCORES.items():
            if rid.endswith(key) and (best_score is None or score < best_score):
                best, best_score = (text, bounds), score
                break
    return best if best else _match_by_text(controls, _GRANT_BUTTONS)


def _match_by_text(
    controls: list[tuple[str, str, tuple[int, int, int, int]]],
    table: tuple[str, ...],
) -> tuple[str, tuple[int, int, int, int]] | None:
    """rid 未命中时的文案兜底（最小权限优先）。"""
    best, best_idx = None, None
    for text, _rid, bounds in controls:
        t = text.strip()
        if t in table:
            idx = table.index(t)
            if best_idx is None or idx < best_idx:
                best, best_idx = (t, bounds), idx
    return best


def _detect_permission_popup(
    ctx: Any, timeout: float = 2.0
) -> tuple[str, list[tuple[str, str, tuple[int, int, int, int]]]] | None:
    """有界轮询检测权限弹窗（非单次）。

    覆盖弹窗 100~300ms 渲染延迟，每 200ms 检测一次，最多等 timeout 秒。
    供 click(permission_hint) 和 wait_for_permission_dialog 复用。
    """
    deadline = time.monotonic() + max(0.0, min(float(timeout), 8.0))
    while time.monotonic() < deadline:
        info = _permission_popup_buttons(ctx)
        if info:
            return info
        time.sleep(0.2)
    return None


# detect_popup 关键词：覆盖常见弹窗按钮文案
_POPUP_KEYWORDS = (
    "允许",
    "拒绝",
    "确定",
    "取消",
    "同意",
    "继续",
    "进入",
    "关闭",
    "跳过",
    "知道了",
    "前往设置",
    "Allow",
    "Deny",
    "OK",
    "Cancel",
    "Agree",
    "Continue",
    "Dismiss",
)
_POPUP_RETRY_MAX = 3  # 最多重试次数
_POPUP_RETRY_INTERVAL = 0.5  # 重试间隔（秒），覆盖弹窗 200~500ms 渲染延迟


@tool
def detect_popup() -> str:
    """检测当前弹窗（基于 UI 树，不依赖 vision）。

    权限弹窗由专用检测路径处理；本工具覆盖业务弹窗（确认/取消/允许等）。
    内置重试：弹窗渲染有延迟，会自动重试最多 3 次（间隔 0.5s）。
    """
    ctx = get_tool_context()
    if ctx.device is None:
        return make_result(ERROR, "未连接 Android 设备")

    # 1) 权限弹窗专用检测（确定性事实）
    permission_info = _permission_popup_buttons(ctx)
    if permission_info:
        activity, controls = permission_info
        return make_result(
            OK,
            "检测到系统权限弹窗，请显式调用 respond_to_permission_dialog",
            _permission_evidence(activity, controls),
        )

    # 2) 业务弹窗检测：轮询重试（覆盖渲染延迟）+ text/content-desc 双字段 + strip 后子串匹配
    for attempt in range(_POPUP_RETRY_MAX):
        try:
            root = ET.fromstring(ctx.device.dump_hierarchy())
        except Exception as exc:
            return make_result(ERROR, f"读取弹窗层级失败: {exc}")
        buttons: list[str] = []
        for node in root.iter():
            if node.get("clickable") != "true":
                continue
            # 同时检查 text 和 content-desc（部分 OEM 弹窗按钮只设 content-desc）
            text = (node.get("text") or "").strip()
            desc = (node.get("content-desc") or "").strip()
            for candidate in (text, desc):
                if not candidate:
                    continue
                for kw in _POPUP_KEYWORDS:
                    if kw in candidate:  # 子串匹配：「确定(2)」也能命中「确定」
                        buttons.append(candidate)
                        break
                else:
                    continue
                break  # 一个节点只记一次
        if buttons:
            return make_result(OK, "检测到弹窗按钮", {"buttons": "|".join(buttons)})
        if attempt < _POPUP_RETRY_MAX - 1:
            time.sleep(_POPUP_RETRY_INTERVAL)
    return make_result(NOT_FOUND, "未检测到弹窗")


@tool
def wait_for_permission_dialog(timeout: float = 3.0) -> str:
    """有限轮询系统权限弹窗，只返回当前真实按钮，不执行授权。"""
    ctx = get_tool_context()
    if ctx.device is None:
        return make_result(ERROR, "未连接 Android 设备")
    info = _detect_permission_popup(ctx, timeout=timeout)
    if info:
        activity, controls = info
        return make_result(
            OK,
            "权限弹窗已出现，请根据测试意图显式选择按钮",
            _permission_evidence(activity, controls),
        )
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
            for label, rid, bounds in controls:
                if label == requested:
                    ctx.device.click_bounds(bounds)
                    evidence["selected_button"] = label
                    return make_result(OK, "已按显式请求响应系统权限弹窗", evidence)
            return make_result(
                NOT_FOUND,
                f"当前权限弹窗不存在指定按钮: {requested}；"
                f"当前可见按钮为: {'|'.join(label for label, _, _b in controls)}，"
                f"请用 get_screen_info() 读取真实按钮文案后重试",
                evidence,
            )
        if time.monotonic() >= deadline:
            break
        time.sleep(0.2)
    return make_result(
        NOT_FOUND,
        "权限弹窗未在等待时间内出现（可能已超时 10s 自动消失）；"
        "请先 get_screen_info() 确认弹窗是否仍在，若仍在则用真实按钮文案重试；"
        "若确已消失，可改用 set_runtime_permission(package, permissions, action) 经 adb 授权",
    )


@tool
def set_permission_intent(permission: str = "", action: str = "") -> str:
    """声明本轮权限测试意图。设置后，后续每次 click() 自动监听
    匹配的权限弹窗并按 action 响应，无需在 click 中传 permission_hint。

    permission: 可选权限类型（camera/location/storage 等），仅作日志标记
    action: "grant" 或 "deny"
    传空值清除意图。
    """
    ctx = get_tool_context()
    if not permission and not action:
        ctx._permission_intent = {}
        return make_result(OK, "权限测试意图已清除")
    if action not in ("grant", "deny"):
        return make_result(ERROR, "action 必须是 grant 或 deny")
    # T2: 弹窗在屏 → 不硬报 ERROR（会卡住冷启动/基线建立流程），
    # 而是按 action 自动响应弹窗，再平滑设立基线。
    _popup = _permission_popup_buttons(ctx)
    if _popup:
        _activity, _controls = _popup
        _btns = "|".join(t for t, _, _ in _controls)
        # 按 action 选目标按钮文案：deny 优先「拒绝」（不误点「拒绝并不再询问」
        # 以免永久污染后续用例）；grant 优先「允许」。
        if action == "deny":
            _candidates = ("拒绝并不再询问", "拒绝", "禁止", "Deny")
        else:
            _candidates = ("允许", "始终允许", "同意", "Allow")
        _chosen = None
        for _c in _candidates:
            for _label, _rid, _b in _controls:
                if _label == _c or _c in _label:
                    _chosen = (_label, _b)
                    break
            if _chosen:
                break
        if _chosen:
            _label, _b = _chosen
            ctx.device.click_bounds(_b)
            time.sleep(0.3)
            # 响应成功后设立基线
            ctx._permission_intent = {
                "permission": permission.lower().strip(),
                "action": action.strip(),
                "set_time": time.monotonic(),
            }
            return make_result(
                OK,
                f"检测到权限弹窗({_activity})已按 {action} 响应({_label})，"
                f"并设立权限测试意图: permission={permission}, action={action}。"
                f"后续 click() 将自动监听权限弹窗并按 {action} 响应。"
                f"测试完当前分支后调用 set_permission_intent() 清除。",
                {"permission": permission, "action": action, "popup_responded": _label},
            )
        # 找不到匹配按钮（极端情况）→ 降级提示，由 Agent 显式决策
        return make_result(
            ERROR,
            f"当前屏幕存在权限弹窗({_activity})，可见按钮: {_btns}，"
            f"但未找到匹配 {action} 的按钮；请直接用 "
            f'respond_to_permission_dialog(button="...") 显式响应。',
            _permission_evidence(_activity, _controls),
        )
    ctx._permission_intent = {
        "permission": permission.lower().strip(),
        "action": action.strip(),
        "set_time": time.monotonic(),
    }
    return make_result(
        OK,
        f"已设置权限测试意图: permission={permission}, action={action}。"
        f"后续 click() 将自动监听权限弹窗并按 {action} 响应。"
        f"测试完当前分支后调用 set_permission_intent() 清除。",
        {"permission": permission, "action": action},
    )


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


# 常用危险权限别名集合：弹窗已消失/想提前授权时一键兜底
_COMMON_PERMISSIONS = (
    "camera",
    "location",
    "storage",
    "calendar",
    "contacts",
    "microphone",
    "phone",
    "notifications",
    "photos",
    "videos",
    "audio",
)

# 组合权限别名 → 展开为多个独立权限（工具层处理，不侵入控制器）
# storage 同时需要 READ + WRITE，calendar/contacts 同理；避免只授读不授写导致功能异常
_PERMISSION_EXPAND_MAP: dict[str, list[str]] = {
    "storage": ["read_storage", "write_storage"],
    "calendar": ["read_calendar", "write_calendar"],
    "contacts": ["read_contacts", "write_contacts"],
}


@tool
def set_runtime_permission(
    package: str,
    permissions: str = "",
    action: str = "grant",
    include_common: bool = False,
) -> str:
    """当系统权限弹窗已超时消失（约 10s 自动消失）或需绕过弹窗时，经 adb `pm grant`/`pm revoke` 直接授予或撤销运行时权限。

    package: 目标 App 包名，如 com.zui.calendar。
    permissions: 权限名或别名，多个用逗号分隔。支持别名：
        camera, location/fine_location, coarse_location, storage/read_storage,
        write_storage, calendar/read_calendar, write_calendar, contacts,
        microphone, phone, sms, notifications, body_sensors, bluetooth；
        以及媒体权限（Android 13+「选择照片/视频」类弹窗）：
        photos/images/select_photos(READ_MEDIA_IMAGES)、
        videos/video/select_videos(READ_MEDIA_VIDEO)、
        audio/music/media_audio(READ_MEDIA_AUDIO)、
        visual_selected/selected_media/partial_media(READ_MEDIA_VISUAL_USER_SELECTED，Android 14 部分媒体访问)；
        也可直接给完整权限名（android.permission.XXX）。
    action: "grant" 授予；"deny" 或 "revoke" 撤销（等价于在弹窗点「拒绝」）。
    include_common: True 时忽略 permissions，直接授予一组常用危险权限
        （camera/location/storage/calendar/contacts/microphone/phone/notifications/photos/videos/audio），
        用于「弹窗已消失、想让功能可用」的快速兜底。storage/calendar/contacts 会自动展开为读写双授。

    典型用法：
    - respond_to_permission_dialog 返回 NOT_FOUND「权限弹窗未在等待时间内出现」→
      弹窗已超时消失，本工具经 adb 直接授权，避免空转重试。
    - 想提前授权以规避弹窗竞态：set_runtime_permission(package, include_common=true)。
    - 撤销（验证拒绝路径）：set_runtime_permission(package, permissions='camera', action='deny')。

    pm grant 不支持的特殊权限（如悬浮窗/系统设置）会自动回退 cmd appops。
    返回结构化结果；部分权限失败时状态为 AMBIGUOUS/NOT_FOUND 并列出明细。

    ⚠️ 回调问题：`pm grant` 是在系统层修改权限状态，不会触发 App 的
    `onRequestPermissionsResult` 回调。如果 App 在回调里写了后续逻辑
    （如打开相机、跳转页面），这些逻辑不会被执行。解决方案：授权后
    导航回触发点并重新点击触发操作，App 再次调 `requestPermissions()`
    时 Android 发现权限已 GRANTED，会直接触发回调，App 正常流程才跑通。
    """
    ctx = get_tool_context()
    if ctx.device is None:
        return make_result(ERROR, "未连接 Android 设备")

    action = (action or "grant").strip().lower()
    if action in ("deny", "revoke"):
        grant = False
    elif action == "grant":
        grant = True
    else:
        return make_result(ERROR, f"不支持的 action: {action}（用 grant / deny）")

    if include_common:
        perms = list(_COMMON_PERMISSIONS)
    else:
        perms = [p.strip() for p in (permissions or "").split(",") if p.strip()]
    if not perms:
        return make_result(
            ERROR,
            "permissions 为空且 include_common=False，请提供至少一个权限名/别名",
        )

    results: list[str] = []
    ok = 0
    total = 0
    for raw in perms:
        # 展开组合权限别名（storage → read_storage + write_storage 等）
        sub_perms = _PERMISSION_EXPAND_MAP.get(raw.lower(), [raw])
        for sub in sub_perms:
            total += 1
            try:
                msg = (
                    ctx.device.grant_permission(package, sub)
                    if grant
                    else ctx.device.revoke_permission(package, sub)
                )
                results.append(f"{sub}: OK ({msg})")
                ok += 1
            except Exception as exc:  # 单条失败不影响其他权限
                results.append(f"{sub}: FAIL ({exc})")
    status = OK if ok == total else (NOT_FOUND if ok == 0 else AMBIGUOUS)
    return make_result(
        status,
        f"已处理 {ok}/{total} 个权限（action={action}）",
        {"package": package, "action": action, "details": " || ".join(results)},
    )


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


# ═══ vision_tap：视觉定位点击（Canvas/滚轮等 view tree 无法访问的 UI 元素） ═══

_VISION_TAP_FAIL_STREAK_CAP = 2


def _parse_android_bounds(bounds_str: str) -> tuple[int, int, int, int] | None:
    """解析 Android UI Automator bounds 格式 '[x1,y1][x2,y2]'。"""
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds_str or "")
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))
    return None


def _find_dialog_crop_bounds(hierarchy_xml: str) -> tuple[int, int, int, int] | None:
    """从 view tree XML 中找到弹窗区域的 bounds，用于 vision_tap 智能裁剪。

    策略（按优先级）：
      1. parentPanel rid — 标准 Android 弹窗（含标题+内容）
      2. customPanel rid — 弹窗内容区（仅滚轮/Canvas）
      3. 找不到则返回 None，回退全屏截图
    """
    try:
        root = ET.fromstring(hierarchy_xml)
    except ET.ParseError:
        return None

    # 策略1: parentPanel（含标题+内容+按钮，上下文最完整）
    for node in root.iter("node"):
        rid = node.get("resource-id", "")
        if "parentPanel" in rid:
            bounds = _parse_android_bounds(node.get("bounds", ""))
            if bounds:
                return bounds

    # 策略2: customPanel（仅内容区）
    for node in root.iter("node"):
        rid = node.get("resource-id", "")
        if "customPanel" in rid:
            bounds = _parse_android_bounds(node.get("bounds", ""))
            if bounds:
                return bounds

    return None


# ═══ SoM 网格定位（Set-of-Mark）══════════════════════════════════════


def _draw_som_grid(
    pil_img: Image.Image,
) -> tuple[Image.Image, str, int, int, int, float, float]:
    """在截图上绘制 SoM 网格（列字母 + 行编号），标签放在画布外扩区域。

    Returns:
        (annotated_image, base64_str, cols, rows, margin, cell_w, cell_h)
        cell_w / cell_h 是原图坐标系下的格子尺寸（不含外扩偏移）。
    """
    img_w, img_h = pil_img.size

    # 自适应网格：目标 30~96 格，格子尺寸 ~60-150px
    cols = max(4, min(8, img_w // 60))
    rows = max(4, min(12, img_h // 60))
    cell_w = img_w / cols
    cell_h = img_h / rows

    # 画布外扩 margin px 放标签，避免遮挡 UI 内容
    margin = 30

    if pil_img.mode != "RGBA":
        pil_img = pil_img.convert("RGBA")

    # 扩展画布
    extended = Image.new(
        "RGBA", (img_w + 2 * margin, img_h + 2 * margin), (255, 255, 255, 255)
    )
    extended.paste(pil_img, (margin, margin))

    overlay = Image.new("RGBA", extended.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # 网格线：1px 灰色半透明，不干扰滚轮蓝色高亮
    grid_color = (128, 128, 128, 100)
    for c in range(1, cols):
        x = margin + int(c * cell_w)
        draw.line([(x, margin), (x, margin + img_h)], fill=grid_color, width=1)
    for r in range(1, rows):
        y = margin + int(r * cell_h)
        draw.line([(margin, y), (margin + img_w, y)], fill=grid_color, width=1)

    # 标签：红色加粗 ≥20px，与 UI 的黑/蓝/灰文字明确区分
    font = None
    for font_name in (
        "arial.ttf",
        "DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ):
        try:
            font = ImageFont.truetype(font_name, 20)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()

    # anchor="mm" 仅 TrueType 字体支持，位图字体需手动居中
    _is_truetype = hasattr(font, "getbbox")

    label_color = (255, 0, 0, 255)  # 纯红

    def _draw_centered_label(x: int, y: int, text: str) -> None:
        if _is_truetype:
            draw.text((x, y), text, fill=label_color, font=font, anchor="mm")
        else:
            # 位图字体 fallback：手动居中
            bbox = draw.textbbox((0, 0), text, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            draw.text((x - tw // 2, y - th // 2), text, fill=label_color, font=font)

    # 列标签（A, B, C, ...）画在顶部外扩区
    for c in range(cols):
        label = chr(ord("A") + c)
        cx = margin + int((c + 0.5) * cell_w)
        _draw_centered_label(cx, margin // 2, label)

    # 行标签（1, 2, 3, ...）画在左侧外扩区
    for r in range(rows):
        label = str(r + 1)
        cy = margin + int((r + 0.5) * cell_h)
        _draw_centered_label(margin // 2, cy, label)

    # 合成
    result = Image.alpha_composite(extended, overlay).convert("RGB")

    # 编码 base64
    buf = BytesIO()
    result.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    import logging as _lg

    _lg.getLogger(__name__).info(
        "[SoM] grid=%dx%d cells=%.0fx%.0fpx margin=%d canvas=%dx%d",
        cols,
        rows,
        cell_w,
        cell_h,
        margin,
        img_w + 2 * margin,
        img_h + 2 * margin,
    )

    return result, b64, cols, rows, margin, cell_w, cell_h


def _parse_cell_response(data: dict, cols: int, rows: int) -> tuple[str, int] | None:
    """宽容解析模型返回的格子引用。

    支持格式:
      {"col": "G", "row": 7}
      {"col": "g", "row": "7"}
      {"cell": "G7"}
      reason 字段内包含 "G7" 等
    列字母转大写，校验范围 [0, cols) 和 [1, rows]。
    返回 (col_letter, row_number) 或 None。
    """
    col_raw = str(data.get("col", "")).strip().upper()
    row_raw = str(data.get("row", "")).strip()
    cell_raw = str(data.get("cell", "")).strip().upper()

    col_letter = ""
    row_number = 0

    # 格式 1: col + row 分离（col 必须单字母，否则放行到格式 1.5）
    if col_raw and row_raw and len(col_raw) == 1:
        col_letter = col_raw
        try:
            row_number = int(row_raw)
        except (ValueError, TypeError):
            col_letter = ""

    # 格式 1.5: col 字段含合并值（如 {"col": "G7"} 没给 row）
    if not col_letter and col_raw:
        m = re.match(r"([A-Z])(\d{1,2})$", col_raw)
        if m:
            col_letter = m.group(1)
            row_number = int(m.group(2))

    # 格式 2: cell 合并（如 "G7"）
    if not col_letter and cell_raw:
        m = re.match(r"([A-Z])(\d{1,2})", cell_raw)
        if m:
            col_letter = m.group(1)
            row_number = int(m.group(2))

    # 格式 3: 从 reason 字段提取
    if not col_letter:
        reason = str(data.get("reason", ""))
        m = re.search(r"(?<![A-Za-z0-9])([A-Za-z])(\d{1,2})(?!\d)", reason)
        if m:
            col_letter = m.group(1).upper()
            row_number = int(m.group(2))

    if not col_letter:
        return None

    # 校验列范围: A=0, B=1, ...
    col_idx = ord(col_letter) - ord("A")
    if not (0 <= col_idx < cols):
        return None
    # 校验行范围: 1-based
    if not (1 <= row_number <= rows):
        return None

    return col_letter, row_number


@tool
def vision_tap(
    description: str,
    repeat: int = 1,
    verify: str = "",
    verification_key: str = "",
    clause_id: str = "",
) -> str:
    """基于截图让 vision 模型定位目标区域并点击。

    专用于 Canvas 绘制、滚轮选择器等 view tree 无法访问的 UI 元素。

    description 示例：
      - "上课时长滚轮中显示数字 10 的那一行"
      - "休息时间滚轮中显示数字 5 的那一行"
      - "颜色选择器中紫色色块"
      - "开始时间小时滚轮中当前选中数字下方的那一行"（当需要连续点击多次时）

    repeat 参数：在同一坐标上重复点击的次数（默认 1）。
    时间拾取器高效策略：
      - 时间轴每列显示 3 个值：上一值 / 选中值 / 下一值
      - 点击上方值可选中上一值，点击下方值可选中下一值
      - 设置分钟时，先定位到"下方值"的位置，然后用 repeat=N 连续点击 N 次
        例如：当前分钟 00，目标是 10 → vision_tap("分钟列当前选中值下方的位置", repeat=10)
        避免多次调用 vision_tap 浪费 token 和时间

    verify 参数：点击后用新截图验证结果（可选）。
      - 示例：verify="分钟列当前选中值是否为53"
      - 点击后自动等待 0.3s 动画 → 重新截图 → vision 验证
      - 如果 verify 显示值未变，说明坐标可能偏差，请调整 description 后重试
    """
    from tools import _run_multimodal_from_context  # 延迟 import 避免循环依赖
    import logging as _logging

    _logger = _logging.getLogger(__name__)
    ctx = get_tool_context()

    # 1) 设备可用性
    if ctx.device is None:
        return make_result(ERROR, "未连接 Android 设备")

    # 2) vision 可用性：独立视觉模型优先， 否则回退主模型
    actual_model = ctx.vision_model or ctx.llm_model
    if not (ctx.llm_vision_enabled and actual_model):
        return make_result(
            UNSUPPORTED,
            "vision 未启用，请配置 vision_model 或使用多模态主模型",
        )

    # 3) fail_streak 防卡死：连续 2 次失败 → 提示 LLM 回退 UI-tree
    streak = getattr(ctx, "_vision_tap_fail_streak", 0)
    if streak >= _VISION_TAP_FAIL_STREAK_CAP:
        return make_result(
            ERROR,
            "vision_tap 连续失败，建议回退 UI-tree 路径",
        )

    try:
        # 4) 截图 + view tree，尝试智能裁剪（只发 Canvas 区域给 vision 模型）
        image = ctx.device.screenshot()
        hierarchy = ctx.device.dump_hierarchy()
        dev_w, dev_h = image.width, image.height

        crop_bounds = _find_dialog_crop_bounds(hierarchy)

        if crop_bounds:
            # ── 裁剪模式：从原图裁出弹窗区域，不加压缩 ──
            pad = 30  # 四周留边距，给模型上下文
            cx1 = max(0, crop_bounds[0] - pad)
            cy1 = max(0, crop_bounds[1] - pad)
            cx2 = min(dev_w, crop_bounds[2] + pad)
            cy2 = min(dev_h, crop_bounds[3] + pad)
            cropped = image.crop((cx1, cy1, cx2, cy2))
            img_w, img_h = cropped.width, cropped.height

            # PNG 无损 + RGBA→RGB 转换
            if cropped.mode in ("RGBA", "P"):
                cropped = cropped.convert("RGB")

            # SoM 网格：画网格 + 外扩标签 → 编码
            (
                grid_pil,
                img_base64,
                grid_cols,
                grid_rows,
                grid_margin,
                grid_cell_w,
                grid_cell_h,
            ) = _draw_som_grid(cropped)
            img_w, img_h = grid_pil.size  # 含外扩 margin

            # 坐标映射：device = crop_offset + vision_coord（零缩放，零精度损失）
            offset_x, offset_y = cx1, cy1
            scale_x, scale_y = 1.0, 1.0

            _logger.info(
                "[vision_tap] CROPPED mode: crop=(%d,%d,%d,%d) img=%dx%d "
                "(device=%dx%d) desc=%s",
                cx1,
                cy1,
                cx2,
                cy2,
                img_w,
                img_h,
                dev_w,
                dev_h,
                description,
            )
        else:
            # ── 全屏模式：压缩快照（visual_check 也用这条路径） ──
            snap = ctx.device.snapshot_for_vision()
            img_w, img_h = snap.width, snap.height
            dev_w = snap.original_width or img_w
            dev_h = snap.original_height or img_h
            img_base64 = snap.image_base64

            # 保留原图尺寸（格子坐标系基于原图，scale 分母必须用原图尺寸）
            orig_w, orig_h = snap.width, snap.height

            # SoM 网格：解码压缩快照 → 画网格 → 重新编码
            snap_bytes = base64.b64decode(img_base64)
            snap_pil = Image.open(BytesIO(snap_bytes))
            (
                grid_pil,
                img_base64,
                grid_cols,
                grid_rows,
                grid_margin,
                grid_cell_w,
                grid_cell_h,
            ) = _draw_som_grid(snap_pil)
            img_w, img_h = grid_pil.size  # 含外扩 margin

            # 坐标映射：device = vision_coord * scale
            # 注意：scale 分母必须是原图尺寸（格子坐标是原图空间）
            offset_x, offset_y = 0, 0
            scale_x = dev_w / orig_w if orig_w > 0 else 1.0
            scale_y = dev_h / orig_h if orig_h > 0 else 1.0

            _logger.info(
                "[vision_tap] FULLSCREEN mode: snapshot=%dx%d (device=%dx%d) desc=%s",
                img_w,
                img_h,
                dev_w,
                dev_h,
                description,
            )

        # 5) SoM 网格 prompt：模型只做语义指认（认格子），不做几何估算
        last_col = chr(ord("A") + grid_cols - 1)
        prompt = (
            f"截图已标注 {grid_cols}列(A-{last_col}) × {grid_rows}行(1-{grid_rows}) 的坐标网格。"
            f"红色字母和数字是坐标标注，不是界面内容。"
            f"找到「{description}」所在的格子。"
            f"如果目标跨多个格子，返回目标中心点所在的格子。"
            f'只返回 JSON: {{"col": "列字母", "row": 行号, "reason": str}}。'
            f"列范围 A-{last_col}，行范围 1-{grid_rows}。"
        )

        # 6) 调用 vision
        res = _run_multimodal_from_context(
            prompt,
            img_base64,
            purpose="locate_tap",
            strict_json=True,
            timeout_sec=getattr(ctx, "vision_timeout", 60),
        )
    except Exception as exc:
        _logger.warning("[vision_tap] 工具内部异常: %s", exc, exc_info=True)
        ctx._vision_tap_fail_streak = streak + 1
        return make_result(ERROR, f"工具内部异常 {exc}")

    # 7) 检查 vision 调用结果
    if not res.get("ok"):
        ctx._vision_tap_fail_streak = streak + 1
        return make_result(
            ERROR,
            "vision 调用失败 {error}".format(
                error=res.get("error", res.get("reason", "unknown")),
            ),
        )

    # 8) SoM 解析：提取格子引用 → 计算格子中心像素 → 映射设备坐标
    data = res.get("data") or {}
    cell_ref = _parse_cell_response(data, grid_cols, grid_rows)
    reason = data.get("reason", "")

    if cell_ref is None:
        # 宽容解析全部失败 → 尝试 fallback 像素模式
        _logger.warning(
            "[vision_tap] SoM cell parse failed, data=%s, fallback pixel", data
        )
        if "x" not in data or "y" not in data:
            ctx._vision_tap_fail_streak = streak + 1
            return make_result(ERROR, "vision 返回格式无效（无法解析格子引用或 x/y）")
        try:
            raw_x = int(data["x"])
            raw_y = int(data["y"])
        except (ValueError, TypeError):
            ctx._vision_tap_fail_streak = streak + 1
            return make_result(
                ERROR, f"vision 返回坐标非整数: x={data.get('x')} y={data.get('y')}"
            )
        # fallback x/y 是外扩坐标系，需减去 margin 回到原图空间
        x_img = max(0, min(img_w - 2 * grid_margin, raw_x - grid_margin))
        y_img = max(0, min(img_h - 2 * grid_margin, raw_y - grid_margin))
        cell_note = " [fallback pixel]"
    else:
        col_letter, row_number = cell_ref
        col_idx = ord(col_letter) - ord("A")
        # 格子中心（原图坐标系，不含外扩 margin）
        x_img = int((col_idx + 0.5) * grid_cell_w)
        y_img = int((row_number - 0.5) * grid_cell_h)
        x_img = max(0, min(int(img_w - 2 * grid_margin), x_img))
        y_img = max(0, min(int(img_h - 2 * grid_margin), y_img))
        cell_note = f" [cell {col_letter}{row_number}]"

    # 统一坐标映射：device = offset + vision_coord * scale
    x = int(offset_x + x_img * scale_x)
    y = int(offset_y + y_img * scale_y)

    # 详细日志
    _logger.info(
        "[vision_tap] SoM: img=(%d,%d) → dev=(%d,%d)%s reason=%s",
        x_img,
        y_img,
        x,
        y,
        cell_note,
        reason,
    )

    # click_xy 使用 adb shell input tap，坐标系与截图一致，无需横屏互换
    _logger.info(
        "[vision_tap] 点击坐标: (%d, %d) dev=%dx%d img=%dx%d landscape=%s",
        x,
        y,
        dev_w,
        dev_h,
        img_w,
        img_h,
        dev_w > dev_h,
    )

    # 9) 执行点击（使用设备坐标），支持 repeat 批量连点
    actual_repeat = max(1, int(repeat))
    _logger.info(
        "[vision_tap] 最终点击坐标: (%d, %d) repeat=%d",
        x,
        y,
        actual_repeat,
    )
    for _i in range(actual_repeat):
        ctx.device.click_xy(x, y)
        if _i < actual_repeat - 1:
            time.sleep(0.15)  # 连点间隔，给滚轮动画留时间

    # 成功 → 重置 fail_streak 和上次 evidence
    ctx._vision_tap_fail_streak = 0
    ctx._vision_tap_last_evidence = ""
    mode_note = " [裁剪模式]" if crop_bounds else " [全屏模式]"
    repeat_note = f" 连点{actual_repeat}次" if actual_repeat > 1 else ""
    base_msg = "已点击设备坐标({x},{y}) [图坐标({x_img},{y_img})]{cell}{mode} reason={reason}{repeat}".format(
        x=x,
        y=y,
        x_img=x_img,
        y_img=y_img,
        cell=cell_note,
        mode=mode_note,
        reason=reason,
        repeat=repeat_note,
    )
    verify_decision = ""
    verify_evidence = ""

    # ── verify 闭环：点击后用新截图验证 ──
    if verify:
        time.sleep(0.3)  # 等滚轮动画
        try:
            snap2 = ctx.device.snapshot_for_vision()
            _save_perceive_evidence_screenshot(ctx, verification_key)
            verify_prompt = (
                f"请根据截图判断：{verify}。"
                f'只返回 JSON: {{"decision": "yes/no", "reason": str, "evidence": str}}'
            )
            vr = _run_multimodal_from_context(
                verify_prompt,
                snap2.image_base64,
                purpose="visual_check",
                strict_json=True,
                timeout_sec=getattr(ctx, "vision_timeout", 60),
            )
            if vr.get("ok"):
                vd = vr.get("data") or {}
                v_decision = str(vd.get("decision", "unknown") or "unknown").lower()
                if v_decision not in {"yes", "no", "unknown"}:
                    v_decision = "unknown"
                v_evidence = str(vd.get("evidence", "") or "")
                verify_decision = v_decision
                verify_evidence = v_evidence
                # 缓存 evidence 供下次 vision_tap 参考
                ctx._vision_tap_last_evidence = v_evidence
                base_msg += (
                    f"\nverify=[{v_decision}] {v_evidence}"
                    f"\n⚠️ 如果 verify 显示值未变，说明坐标可能偏差，"
                    f"请调整 description 使其更精确（如指定列号）后重试。"
                )
                _logger.info(
                    "[vision_tap] verify: decision=%s evidence=%s",
                    v_decision,
                    v_evidence,
                )
            else:
                verify_decision = "unknown"
                base_msg += f"\nverify=vision 调用失败: {vr.get('error', 'unknown')}"
        except Exception as exc:
            verify_decision = "unknown"
            base_msg += f"\nverify=截图或验证异常: {exc}"
            _logger.warning("[vision_tap] verify 异常: %s", exc)

    # ── evidence 反馈：附带上次验证结果（如果有） ──
    elif hasattr(ctx, "_vision_tap_last_evidence") and ctx._vision_tap_last_evidence:
        base_msg += (
            f"\n⚠️ 上次验证结果: {ctx._vision_tap_last_evidence}"
            f"\n如果目标值未改变，请调整 description 后重试。"
        )

    evidence = {
        "verify_decision": verify_decision,
        "verify_evidence": verify_evidence,
    }
    if verification_key and clause_id and verify:
        ctx._evidence_events.append(
            {
                "verification_key": verification_key,
                "clause_id": clause_id,
                "channel": "vision_verify",
                "status": (
                    "YES"
                    if verify_decision == "yes"
                    else "NO" if verify_decision == "no" else "UNKNOWN"
                ),
                "authoritative": False,
                "fact": {
                    "description": description,
                    "verify": verify,
                    "decision": verify_decision,
                    "evidence": verify_evidence,
                },
                "artifact_ref": getattr(ctx, "_last_screenshot_path", "") or "",
            }
        )
    return make_result(OK, base_msg, evidence)


# ═══ click_and_check：点击后立即截图验证（捕获 toast 等瞬态 UI） ═══


@tool
def click_and_check(
    label: str,
    check_description: str,
    wait_ms: int = 500,
    verification_key: str = "",
    clause_id: str = "",
) -> str:
    """点击元素后立即截图并用 vision 验证。专用于捕获 toast 等瞬态 UI 提示。

    普通 visual_check 流程太慢（LLM 思考 + vision 推理 >> toast 显示时长），
    此工具将点击和截图绑定为一步：点击 → 等待 wait_ms → 立即截图 → 再送 vision 分析。

    label: 要点击的元素文本，如 "完成"、"确定"
    check_description: 让 vision 验证的内容，如 "屏幕底部是否出现toast提示"
    wait_ms: 点击后等待毫秒数（默认 500ms，toast 通常 1-2 秒内可见）

    示例：
      - click_and_check("完成", "屏幕底部是否出现toast提示")
      - click_and_check("保存", "页面中央是否出现加载动画", wait_ms=300)
    """
    from tools import _run_multimodal_from_context
    import logging as _logging

    _logger = _logging.getLogger(__name__)
    ctx = get_tool_context()

    if ctx.device is None:
        return make_result(ERROR, "未连接 Android 设备")

    actual_model = ctx.vision_model or ctx.llm_model
    if not (ctx.llm_vision_enabled and actual_model):
        return make_result(UNSUPPORTED, "vision 未启用，无法执行 click_and_check")

    # 1) 点击元素
    clicked = ctx.device.click_text(label)
    if not clicked:
        return make_result(NOT_FOUND, f"未找到可点击元素: {label}")
    _logger.info("[click_and_check] clicked '%s', waiting %dms", label, wait_ms)

    # 2) 等待 toast/动画出现
    time.sleep(max(0, wait_ms) / 1000.0)

    # 3) 立即截图（压缩，toast 通常 2 秒内可见）；同时作为验证证据落盘
    try:
        snap = ctx.device.snapshot_for_vision()
        _save_perceive_evidence_screenshot(ctx, verification_key)
    except Exception as exc:
        return make_result(ERROR, f"截图失败: {exc}")

    # 4) 送 vision 模型分析
    prompt = (
        f"观察截图，{check_description}。"
        f'只返回 JSON: {{"decision": "yes/no", "reason": str, "evidence": str}}'
    )
    res = _run_multimodal_from_context(
        prompt,
        snap.image_base64,
        purpose="click_and_check",
        strict_json=True,
        timeout_sec=getattr(ctx, "vision_timeout", 60),
    )

    if not res.get("ok"):
        return make_result(
            ERROR,
            f"已点击'{label}'并截图，但 vision 分析失败: {res.get('error', 'unknown')}",
        )

    data = res.get("data") or {}
    decision = data.get("decision", "unknown")
    reason = data.get("reason", "")
    evidence = data.get("evidence", "")

    if verification_key and clause_id:
        ctx._evidence_events.append(
            {
                "verification_key": verification_key,
                "clause_id": clause_id,
                "channel": "click_and_check",
                "status": "PASS" if decision == "yes" else "UNKNOWN",
                "authoritative": False,
                "fact": {
                    "label": label,
                    "decision": decision,
                    "reason": reason,
                    "evidence": evidence,
                },
                "artifact_ref": getattr(ctx, "_last_screenshot_path", "") or "",
            }
        )

    _logger.info(
        "[click_and_check] '%s' → decision=%s reason=%s",
        label,
        decision,
        reason,
    )
    return make_result(
        OK,
        f"已点击'{label}'并截图验证 [{decision}] {reason} | evidence: {evidence}",
    )

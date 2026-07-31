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
    snap = ctx.device.snapshot_for_vision()
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
        timeout_sec=getattr(ctx, "vision_timeout", 45),
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
        timeout_sec=getattr(ctx, "vision_timeout", 45),
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
        if not any(marker in activity.lower() for marker in _PERMISSION_ACTIVITY_MARKERS):
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
    "permission_allow_all_button": 1,               # 媒体全量「全部允许」
    "permission_allow_always_button": 1,            # 始终允许
    "permission_allow_foreground_only_button": 2,
    "permission_allow_button": 2,                   # 基本允许（单次）
    "permission_allow_one_time_button": 2,          # 仅本次使用
}
_DENY_RID = "permission_deny_button"
# 文案兜底表（兼容非标准 OEM / 自定义 rid 的设备）
_GRANT_BUTTONS = ("仅在使用中允许", "仅本次使用时允许", "始终允许", "允许", "全部允许", "允许访问所有照片", "Allow")
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
    "允许", "拒绝", "确定", "取消", "同意", "继续", "进入", "关闭", "跳过", "知道了",
    "前往设置", "Allow", "Deny", "OK", "Cancel", "Agree", "Continue", "Dismiss",
)
_POPUP_RETRY_MAX = 3       # 最多重试次数
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
    # T2: 弹窗在屏却传空 permission → 结构化错误（确定性事实 + 正确工具名）
    if action and not permission:
        _popup = _permission_popup_buttons(ctx)
        if _popup:
            _activity, _controls = _popup
            _btns = "|".join(t for t, _, _ in _controls)
            return make_result(
                ERROR,
                f"当前屏幕存在权限弹窗({_activity})，请直接用 "
                f"respond_to_permission_dialog(button=\"...\") 响应（可见按钮: {_btns}）；"
                f"或传入具体 permission 类型后再用 click() 自动处理。",
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


@tool
def vision_tap(description: str) -> str:
    """基于截图让 vision 模型定位目标区域并点击。

    专用于 Canvas 绘制、滚轮选择器等 view tree 无法访问的 UI 元素。

    description 示例：
      - "上课时长滚轮中显示数字 10 的那一行"
      - "休息时间滚轮中显示数字 5 的那一行"
      - "颜色选择器中紫色色块"
    """
    from tools import _run_multimodal_from_context  # 延迟 import 避免循环依赖
    import logging as _logging

    _logger = _logging.getLogger(__name__)
    ctx = get_tool_context()

    # 1) 设备可用性
    if ctx.device is None:
        return make_result(ERROR, "未连接 Android 设备")

    # 2) vision 可用性：独立视觉模型优先，否则回退主模型
    actual_model = ctx.vision_model or ctx.llm_model
    if not (ctx.llm_vision_enabled and actual_model):
        return make_result(
            ERROR,
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
        # 4) 截图 + 坐标系（使用压缩快照节省 token）
        snap = ctx.device.snapshot_for_vision()
        # 压缩图尺寸（vision 模型看到的尺寸）
        img_w, img_h = snap.width, snap.height
        # 原始设备分辨率（用于坐标映射）
        dev_w = snap.original_width
        dev_h = snap.original_height
        # 防御：如果 original 字段未正确填写（为 0），回退恒等映射
        if dev_w <= 0 or dev_h <= 0:
            _logger.warning(
                "[vision_tap] original_width/height 无效 (%d, %d)，回退恒等映射",
                dev_w, dev_h,
            )
            dev_w, dev_h = img_w, img_h
        _logger.info(
            "[vision_tap] snapshot=%dx%d (device=%dx%d) desc=%s",
            img_w, img_h, dev_w, dev_h, description,
        )

        # 5) 构造 prompt（让 vision 模型基于压缩图输出坐标，我们再映射回设备空间）
        prompt = (
            f"截图尺寸 {img_w}x{img_h}，坐标原点左上角。"
            f"找到「{description}」所在位置。"
            f'只返回 JSON: {{"x": int, "y": int, "reason": str}}，'
            f"x in [0,{img_w}]，y in [0,{img_h}]。"
        )

        # 6) 调用 vision
        res = _run_multimodal_from_context(
            prompt,
            snap.image_base64,
            purpose="locate_tap",
            strict_json=True,
            timeout_sec=getattr(ctx, "vision_timeout", 45),
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

    # 8) 解析 + 边界裁剪
    data = res.get("data") or {}
    if "x" not in data or "y" not in data:
        ctx._vision_tap_fail_streak = streak + 1
        return make_result(ERROR, "vision 返回格式无效（缺少 x/y 字段）")

    try:
        raw_x = int(data["x"])
        raw_y = int(data["y"])
    except (ValueError, TypeError):
        ctx._vision_tap_fail_streak = streak + 1
        return make_result(
            ERROR,
            "vision 返回坐标非整数: x={x} y={y}".format(
                x=data.get("x"), y=data.get("y"),
            ),
        )

    # 先裁剪到压缩图边界（vision 模型输出的是压缩图坐标）
    x_img = max(0, min(img_w, raw_x))
    y_img = max(0, min(img_h, raw_y))
    clipped = (x_img != raw_x or y_img != raw_y)
    # 映射回原始设备坐标
    x = int(x_img * dev_w / img_w)
    y = int(y_img * dev_h / img_h)

    # 详细日志：打印完整坐标变换链路
    _logger.info(
        "[vision_tap] 坐标变换: raw=(%d,%d) → clip=(%d,%d) → "
        "dev=(%d,%d) [img=%dx%d dev=%dx%d]%s",
        raw_x, raw_y, x_img, y_img,
        x, y, img_w, img_h, dev_w, dev_h,
        " CLIPPED" if clipped else "",
    )

    # 横屏纠正：uiautomator2 在横屏时截图使用物理方向坐标，
    # 但点击坐标仍基于自然方向（竖屏），导致 X/Y 轴互换。
    # 检测：截图宽度 > 高度（横屏截图特征）
    landscape = dev_w > dev_h
    if landscape:
        _logger.warning(
            "[vision_tap] ★ 横屏模式：坐标轴互换 (%d,%d) -> (%d,%d)",
            x, y, y, x,
        )
        x, y = y, x
    else:
        _logger.info(
            "[vision_tap] 竖屏模式 (dev_w=%d <= dev_h=%d)，坐标不互换",
            dev_w, dev_h,
        )

    # 9) 执行点击（使用设备坐标）
    _logger.info("[vision_tap] 最终点击坐标: (%d, %d) landscape=%s", x, y, landscape)
    ctx.device.click_xy(x, y)

    # 成功 → 重置 fail_streak
    ctx._vision_tap_fail_streak = 0
    reason = data.get("reason", "")
    clip_note = " (已裁剪)" if clipped else ""
    orient_note = " (横屏互换)" if landscape else ""
    return make_result(
        OK,
        "已点击设备坐标({x},{y}) [压缩图坐标({x_img},{y_img})] reason={reason}{clip}{orient}".format(
            x=x, y=y, x_img=x_img, y_img=y_img,
            reason=reason, clip=clip_note, orient=orient_note,
        ),
    )

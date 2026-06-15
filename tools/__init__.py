from __future__ import annotations

import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any

import numpy as np

from tools.context import ToolContext
from llm.safety import check_dangerous

logger = logging.getLogger(__name__)

_CONTEXT: ToolContext | None = None


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


# ═══════════════════════════════════════════
#  Planner Agent 工具
# ═══════════════════════════════════════════


@tool
def get_screen_info() -> str:
    """获取当前页面的结构化语义信息。返回页面布局, 导航区和所有可交互元素（含 resource_id, class, bounds, associated_label, context_path）。"""
    ctx = get_tool_context()
    if ctx.perceiver is None:
        return "Perceiver not available - no Android device connected"
    understanding = ctx.perceiver.perceive()
    lines = [understanding.summary, f"layout={understanding.layout}"]
    for item in understanding.primary_paths[:30]:
        rid = item.resource_id or ""
        cls = item.class_name or ""
        assoc = getattr(item, "associated_label", "") or ""
        ctx_path = getattr(item, "context_path", "") or ""
        has_switch = getattr(item, "has_switch_child", False)
        checked = item.checked
        extra = f" rid={rid}" if rid else ""
        extra += f" class={cls.split('.')[-1]}" if cls else ""
        if assoc:
            extra += f" assoc='{assoc}'"
        if has_switch:
            state = "on" if checked is True else ("off" if checked is False else "?")
            extra += f" switch_state={state}"
        if ctx_path:
            extra += f" path='{ctx_path}'"
        lines.append(
            f'- [{item.region}/{item.role}] "{item.label}"{extra} bounds={item.bounds}'
        )
    logger.debug(f"get_screen_info {lines}")
    return "\n".join(lines)


@tool
def find_element(description: str) -> str:
    """Find UI elements matching description. NOTE: Usually NOT needed — use click() directly, it auto-searches. Only use find_element when you need to inspect candidates before acting."""
    ctx = get_tool_context()
    if ctx.perceiver is None:
        return "NOT_FOUND: perceiver unavailable"

    prev_mode = getattr(ctx.perceiver, "mode", "ui_tree")

    # Phase 1: UI_TREE (fast, milliseconds)
    try:
        ctx.perceiver.mode = "ui_tree"
        understanding = ctx.perceiver.perceive()
        result = _search_elements(understanding, description)
        if result:
            return result
    finally:
        ctx.perceiver.mode = prev_mode

    # Phase 2: Vision (slow, seconds. Fallback when UI tree misses)
    try:
        ctx.perceiver.mode = "vision"
        understanding = ctx.perceiver.perceive()
    finally:
        ctx.perceiver.mode = prev_mode
    return _search_elements(understanding, description)


# 中文控件类型词 → class_name 关键词 映射
_ZH_CONTROL_TOKENS: dict[str, tuple[str, ...]] = {
    "开关": ("switch", "togglebutton"),
    "按钮": ("button",),
    "切换": ("toggle", "switch"),
    "复选框": ("checkbox",),
    "单选框": ("radiobutton",),
    "输入框": ("edittext",),
    "列表": ("recyclerview", "listview"),
    "选项卡": ("tab",),
    "项": ("item",),
}

# role 优先级（数值越小越优，同分时优先选择“点击安全 + 区域合理”的）
_ROLE_PRIORITY: dict[str, int] = {
    "switch_row": 1,  # 包裹 Switch 的 clickable 容器（点击区域大，最优）
    "switch": 2,  # 裸 Switch 控件
    "settings_entry": 3,  # 设置项入口
    "tab": 4,
    "list_entry": 5,
    "button": 6,
    "navigation_item": 7,  # 导航跳转会离开当前页，优先级较低
    "text": 8,
    "container": 9,
}


def _expand_zh_keywords(words: list[str]) -> list[str]:
    """将中文控件词扩展为英文 class 关键词。
    例: ['wlan', '开关'] -> ['wlan', '开关', 'switch', 'togglebutton']
    """
    # 保留原词 + 同义映射
    extras: list[str] = []
    for w in words:
        for zh, en_list in _ZH_CONTROL_TOKENS.items():
            if zh in w:
                extras.extend(en_list)
    return list(dict.fromkeys(words + extras))  # 去重保顺序


def _score_element(el: Any, words: list[str]) -> int:
    """为单个元素计算匹配得分。字段权重：label=3, assoc=2, rid=2, cls=1, ctx_path=1。"""
    score = 0
    label = (el.label or "").lower()
    rid = (el.resource_id or "").lower()
    cls = (el.class_name or "").lower()
    assoc = (getattr(el, "associated_label", "") or "").lower()
    ctx_path = (getattr(el, "context_path", "") or "").lower()
    for w in words:
        if not w:
            continue
        if w in label:
            score += 3
        if w in assoc:
            score += 2
        if w in rid:
            score += 2
        if w in cls:
            score += 1
        if w in ctx_path:
            score += 1
    return score


def _search_elements(understanding: Any, description: str) -> str:
    """Core element search across primary_paths + elements.
    Enhanced:
    - matches label / associated_label / resource_id / class / context_path
    - expands Chinese control words (开关 → switch)
    - sorts candidates by (score desc, role priority asc) so switch_row beats navigation_item
    """
    if understanding is None:
        return ""
    all_elements = list(understanding.primary_paths) + [
        e for e in understanding.elements if e not in understanding.primary_paths
    ]
    desc_lower = description.lower().strip()
    raw_words = [w for w in re.split(r"\s+", desc_lower) if w]
    if not raw_words:
        raw_words = [desc_lower]
    # 过滤太短的英文词，保留中文字（单个中文字也是有意义词）
    words = [w for w in raw_words if (len(w) > 1 or _has_cjk(w))]
    if not words:
        words = raw_words
    expanded_words = _expand_zh_keywords(words)

    candidates: list[tuple[int, int, Any]] = []
    for el in all_elements:
        score = _score_element(el, expanded_words)
        if score <= 0:
            continue
        # role 优先级（数值越小越优）
        role_pri = _ROLE_PRIORITY.get(getattr(el, "role", ""), 50)
        candidates.append((score, role_pri, el))

    if not candidates:
        return ""

    # 排序：得分 desc → role 优先级 asc → 位置（上到下，左到右）
    candidates.sort(key=lambda x: (-x[0], x[1], x[2].bounds[1], x[2].bounds[0]))

    lines = [f"{len(candidates)} candidate(s) for '{description}':"]
    for score, role_pri, el in candidates[:10]:
        assoc = getattr(el, "associated_label", "") or ""
        ctx_path = getattr(el, "context_path", "") or ""
        has_switch = getattr(el, "has_switch_child", False)
        extras = []
        if assoc:
            extras.append(f"assoc='{assoc}'")
        if has_switch:
            state = (
                "on" if el.checked is True else ("off" if el.checked is False else "?")
            )
            extras.append(f"switch_state={state}")
        if ctx_path:
            extras.append(f"path='{ctx_path}'")
        extra_str = (" " + " ".join(extras)) if extras else ""
        lines.append(
            f'  score={score} pri={role_pri} [{el.region}/{el.role}] "{el.label}" '
            f"rid={el.resource_id} class={el.class_name} bounds={el.bounds}{extra_str}"
        )
    return "\n".join(lines)


def _has_cjk(text: str) -> bool:
    """检测字符串中是否含 CJK 中日韩字符。"""
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _find_best_element(understanding: Any, description: str) -> Any | None:
    """返回最佳匹配元素对象（为 click 工具提供，不输出字符串）。"""
    if understanding is None:
        return None
    all_elements = list(understanding.primary_paths) + [
        e for e in understanding.elements if e not in understanding.primary_paths
    ]
    desc_lower = description.lower().strip()
    raw_words = [w for w in re.split(r"\s+", desc_lower) if w]
    if not raw_words:
        raw_words = [desc_lower]
    words = [w for w in raw_words if (len(w) > 1 or _has_cjk(w))]
    if not words:
        words = raw_words
    expanded_words = _expand_zh_keywords(words)

    best: tuple[int, int, Any] | None = None
    for el in all_elements:
        if not el.clickable:
            continue
        score = _score_element(el, expanded_words)
        if score <= 0:
            continue
        role_pri = _ROLE_PRIORITY.get(getattr(el, "role", ""), 50)
        if best is None or (score, -role_pri) > (best[0], -best[1]):
            best = (score, role_pri, el)
    return best[2] if best else None


@tool
def query_app_knowledge(query: str, app_package: str = "") -> str:
    """Query app page structure, navigation paths, and test experience."""
    ctx = get_tool_context()
    if not ctx.knowledge_base:
        return "未启用知识库"
    package = app_package or ctx.device.current_app().get("package", "")
    return str(ctx.knowledge_base.query(query, app_package=package))


@tool
def query_element_identity(alias: str, app_package: str = "") -> str:
    """Query stored element identities for given alias. Returns resource_id, class, role, region from previous successful clicks."""
    ctx = get_tool_context()
    package = app_package or ctx.device.current_app().get("package", "")
    sig = ""
    try:
        sig = ctx.perceiver.screen_signature()[:16] if ctx.perceiver else ""
    except Exception:
        pass
    from data import create_relational_db
    from config import TestConfig

    try:
        cfg = TestConfig()
        db = create_relational_db(cfg)
        rows = db.query_element_identity(package, alias, sig)
        if not rows:
            return f"No known identity for '{alias}' on {package}"
        lines = [f"Known identities for '{alias}' on {package}:"]
        for r in rows:
            lines.append(
                f"  rid={r['resource_id']} class={r['class_name']} "
                f"role={r['role']} region={r['region']} "
                f"clicks={r['click_count']} candidates={r['candidates_count']}"
            )
        return chr(10).join(lines)
    except Exception as exc:
        return f"Element identity query failed: {exc}"


# ═══════════════════════════════════════════
#  Executor Agent 工具
# ═══════════════════════════════════════════


@tool
def click(label: str) -> str:
    """点击页面上指定文本, 描述, 资源 id 或关联标签的元素。

    点击策略（v2）：
    1. 先通过 SmartPerceiver 的语义搜索找到最佳匹配元素（考虑 role 优先级、中文控件词）。
    2. 根据元素类型选执行策略：
       - switch / switch_row → 直接用 bounds 点击（避免 click_text 误点到同名导航项）
       - 其他→ 先 click_text → click_resource_id → bounds 兑底
    3. 记录时输出语义信息（label/rid/role/context_path）供知识库沉淀，不记录原始坐标。
    """
    ctx = get_tool_context()
    decision = check_dangerous(label)
    if not decision.allowed:
        return f"NEEDS_HUMAN: {decision.reason}"
    if ctx.device is None:
        return "ERROR: 未连接 Android 设备"

    # 语义搜索 → 最佳元素
    best_el = None
    if ctx.perceiver is not None:
        try:
            understanding = ctx.perceiver.perceive()
            best_el = _find_best_element(understanding, label)
        except Exception as exc:
            logger.warning("click: perceive failed | %s", exc)

    if best_el is not None:
        role = getattr(best_el, "role", "")
        # 开关类控件直接用 bounds 点击（避免 click_text 误点到导航项）
        if role in ("switch", "switch_row"):
            old_checked = getattr(best_el, "checked", None)
            ctx.device.click_bounds(best_el.bounds)
            # 点击后等待短暂时间再验证开关状态
            time.sleep(1.0)
            new_checked = _check_switch_state(ctx, best_el)
            if new_checked is not None:
                state_cn = "开启" if new_checked else "关闭"
                return _format_click_log(label, best_el, strategy="bounds") + f" | 开关状态: {state_cn}"
            return _format_click_log(label, best_el, strategy="bounds")
        # 其他可点击元素 — 先试语义字段点击，失败后兑底 bounds
        if best_el.text and ctx.device.click_text(best_el.text):
            return _format_click_log(label, best_el, strategy="text")
        if best_el.resource_id and ctx.device.click_resource_id(best_el.resource_id):
            return _format_click_log(label, best_el, strategy="resource_id")
        if best_el.bounds != (0, 0, 0, 0):
            ctx.device.click_bounds(best_el.bounds)
            return _format_click_log(label, best_el, strategy="bounds")

    # 兑底：未找到语义匹配，回退到原始文本/资源点击
    if ctx.device.click_text(label):
        return f"已点击: {label} (strategy=text-fallback)"
    if ctx.device.click_resource_id(label):
        return f"已点击资源: {label} (strategy=rid-fallback)"
    return f"未找到可点击元素: {label}"


def _check_switch_state(ctx: Any, target_el: Any) -> bool | None:
    """点击开关后重新解析 UI 树，查找目标元素的 checked 状态。"""
    try:
        if ctx.perceiver is None:
            return None
        # 清除缓存，强制重新解析
        ctx.perceiver._cache_sig = ""
        understanding = ctx.perceiver.perceive()
        # 按 resource_id 或 bounds 匹配目标元素
        target_rid = getattr(target_el, "resource_id", "") or ""
        target_bounds = getattr(target_el, "bounds", (0, 0, 0, 0))
        for el in understanding.elements:
            if target_rid and getattr(el, "resource_id", "") == target_rid:
                return getattr(el, "checked", None)
            if el.bounds == target_bounds and el.bounds != (0, 0, 0, 0):
                return getattr(el, "checked", None)
        return None
    except Exception:
        return None


def _format_click_log(query: str, el: Any, strategy: str) -> str:
    """生成点击完成后的语义化记录（供知识库沉淀）。不输出原始坐标。"""
    parts = [f"已点击: {query}"]
    if strategy:
        parts.append(f"strategy={strategy}")
    if el.role:
        parts.append(f"role={el.role}")
    if el.region:
        parts.append(f"region={el.region}")
    if el.label:
        parts.append(f"label='{el.label}'")
    if el.resource_id:
        parts.append(f"rid={el.resource_id}")
    assoc = getattr(el, "associated_label", "") or ""
    if assoc and assoc != el.label:
        parts.append(f"assoc='{assoc}'")
    ctx_path = getattr(el, "context_path", "") or ""
    if ctx_path:
        parts.append(f"path='{ctx_path}'")
    return " | ".join(parts)


@tool
def navigate_to(target: str) -> str:
    """切换到指定导航项或 Tab。"""
    ctx = get_tool_context()
    if ctx.device is None:
        return "ERROR: 未连接 Android 设备"
    return (
        click.invoke({"label": target}) if hasattr(click, "invoke") else click(target)
    )


@tool
def scroll_find_and_click(label: str, max_swipes: int = 3) -> str:
    """滑动查找并点击指定元素。最多滑动 max_swipes 次。"""
    ctx = get_tool_context()
    if ctx.device is None:
        return "ERROR: 未连接 Android 设备"
    for _ in range(max_swipes + 1):
        if ctx.device.click_text(label, timeout=0.5):
            return f"已找到并点击: {label}"
        ctx.device.swipe("up")
    return f"滑动后仍未找到: {label}"


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
    """按系统键。key 可为 back / home / enter / recent。"""
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
def launch_app(
    package: str,
    activity: str = "",
) -> str:
    """启动指定包名的 App。"""
    ctx = get_tool_context()
    if ctx.device is None:
        return "ERROR: 未连接 Android 设备，无法启动应用"
    target_activity = (activity or "").strip()
    if target_activity:
        ctx.device.app_start(package, activity=target_activity)
        return f"已启动: {package}/{target_activity}"
    ctx.device.app_start(package)
    return f"已启动: {package}"


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


# ═══════════════════════════════════════════
#  Reviewer Agent 工具
# ═══════════════════════════════════════════


@tool
def switch_perception_mode(mode: str) -> str:
    """切换感知模式：ui_tree / vision / hybrid。"""
    from device.perceiver import PerceptionMode

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
    """检测当前页面异常：ANR/崩溃弹窗/白屏/黑屏/单色屏/进程丢失。返回健康状态。"""
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


@tool
def assert_page_contains(text: str) -> str:
    """断言当前页面包含指定文本。返回 PASS 或 FAIL。"""
    info = (
        get_screen_info.invoke({})
        if hasattr(get_screen_info, "invoke")
        else get_screen_info()
    )
    return "PASS" if text in info else f"FAIL: 页面不包含 {text}"


@tool
def assert_element_exists(label: str) -> str:
    """断言当前页面存在指定元素（按 text / content_desc / resource_id 匹配）。"""
    ctx = get_tool_context()
    if ctx.perceiver is None:
        return "FAIL: Perceiver not available - no device"
    understanding = ctx.perceiver.perceive()
    for element in understanding.elements:
        if label in (element.label or ""):
            return "PASS"
    return f"FAIL: 元素不存在 {label}"


# ═══════════════════════════════════════════
#  辅助工具（多个 Agent 共用）
# ═══════════════════════════════════════════


@tool
def log_step(message: str) -> str:
    """记录一条测试步骤到报告中。"""
    ctx = get_tool_context()
    if ctx.report_logger:
        ctx.report_logger.log_step(message)
    return f"已记录: {message}"


@tool
def save_screenshot(name: str = "") -> str:
    """保存当前截图到磁盘。"""
    ctx = get_tool_context()
    if ctx.device is None:
        return "ERROR: 未连接 Android 设备"
    os.makedirs("storage/screenshots", exist_ok=True)
    filename = name or f"screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    if not filename.endswith(".png"):
        filename += ".png"
    path = os.path.join("storage/screenshots", filename)
    ctx.device.screenshot().save(path)
    return path


# ═══════════════════════════════════════════
#  工具分组
# ═══════════════════════════════════════════

PLANNER_TOOLS: list[Any] = [
    get_screen_info,
    query_app_knowledge,
]

EXECUTOR_TOOLS: list[Any] = [
    query_element_identity,
    find_element,
    click,
    navigate_to,
    scroll_find_and_click,
    type_input,
    press_key,
    swipe,
    launch_app,
    detect_popup,
    dismiss_popup,
    wait_seconds,
    log_step,
]

REVIEWER_TOOLS: list[Any] = [
    find_element,
    get_screen_info,
    switch_perception_mode,
    check_page_health,
    recover_from_anomaly,
    assert_page_contains,
    assert_element_exists,
    log_step,
    save_screenshot,
]

ALL_TOOLS: list[Any] = list(
    {id(t): t for t in PLANNER_TOOLS + EXECUTOR_TOOLS + REVIEWER_TOOLS}.values()
)

# 用于 Planner 的操作类 tool（排除辅助类: log_step, save_screenshot, detect_popup, dismiss_popup）
_ACTION_TOOLS = [
    click,
    type_input,
    swipe,
    press_key,
    navigate_to,
    scroll_find_and_click,
    launch_app,
    wait_seconds,
]


def _action_tools_summary() -> str:
    """动态生成 Planner 提示词中的 action_type 列表，始终与 tools 同步。"""
    return _tools_summary(_ACTION_TOOLS)


def _executor_tools_summary() -> str:
    """动态生成 Executor 提示词中的工具列表。"""
    return _tools_summary(EXECUTOR_TOOLS)


def _reviewer_tools_summary() -> str:
    """动态生成 Reviewer 提示词中的工具列表。"""
    return _tools_summary(REVIEWER_TOOLS)


def _tools_summary(tools: list[Any]) -> str:
    lines: list[str] = []
    for t in tools:
        desc = (getattr(t, "description", "") or "").strip()
        args = getattr(t, "args", {}) or {}
        arg_str = ", ".join(args.keys()) if args else "none"
        lines.append(f"  {t.name}({arg_str}): {desc}")
    return "\n".join(lines)


# ── 内部辅助 ──


def _try_click_by_associated_label(ctx: ToolContext, label: str) -> str | None:
    """通过 find_element 查找与 label 匹配的元素，再用 bounds 进行坐标点击。
    适用于 Switch/ToggleButton 等没有直接 text 的控件。"""
    if ctx.perceiver is None:
        return None

    understanding = ctx.perceiver.perceive()
    all_elements = understanding.primary_paths + understanding.elements
    label_lower = label.lower().strip()

    best_el = None
    best_score = 0
    for el in all_elements:
        score = 0
        el_label = (el.label or "").lower()
        assoc = (getattr(el, "associated_label", "") or "").lower()
        rid = (el.resource_id or "").lower()
        if label_lower in el_label:
            score += 3
        if label_lower in assoc:
            score += 3
        if label_lower in rid:
            score += 2
        # 必须是可点击元素
        if not el.clickable:
            continue
        if score > best_score:
            best_score = score
            best_el = el

    if best_el and best_el.clickable and best_el.bounds != (0, 0, 0, 0):
        ctx.device.click_bounds(best_el.bounds)
        assoc_tag = (
            f" (关联标签: {best_el.associated_label})"
            if getattr(best_el, "associated_label", "")
            else ""
        )
        return f"已通过坐标点击: {label}{assoc_tag} bounds={best_el.bounds}"
    return None


def _has_meaningful_ui_elements(device) -> bool:
    try:
        root = ET.fromstring(device.dump_hierarchy())
        for node in root.iter("node"):
            text = (node.get("text", "") or "").strip()
            desc = (node.get("content-desc", "") or "").strip()
            rid = (node.get("resource-id", "") or "").strip()
            clickable = (node.get("clickable", "") or "").lower() == "true"
            class_name = (node.get("class", "") or "").strip().lower()
            if text or desc or rid:
                return True
            if clickable and class_name and class_name != "android.view.view":
                return True
        return False
    except Exception:
        return False

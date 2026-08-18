"""验证与终止类工具（页面/元素断言、验证结果上报、报告完成）。

从 tools/__init__.py 拆出（重构 T5），仅移动代码、不改逻辑。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from datetime import datetime
from typing import Any

import app_paths
import logging

from tools.context import ToolContext, get_tool_context

logger = logging.getLogger(__name__)

try:
    from langchain_core.tools import tool
except Exception:

    def tool(func=None, *args, **kwargs):
        def wrapper(f):
            return f

        return wrapper(func) if func else wrapper


def _simple_activity_match(expected: str, ual: str) -> bool:
    """按简单类名匹配 Activity，兼容包名前缀与内部类 $ 分隔。"""
    exp = expected.strip().lstrip(".")
    act = ual.strip().lstrip(".")
    if not exp or not act:
        return False
    exp_simple = exp.rsplit("$", 1)[-1].rsplit(".", 1)[-1]
    act_simple = act.rsplit("$", 1)[-1].rsplit(".", 1)[-1]
    return exp_simple == act_simple


def _save_evidence_screenshot(ctx, verification_key: str, seq: int) -> None:
    """验证证据点显式截图：保存到 screenshots/{run_id}/evidence_{key}_{seq}.png
    并写回 ctx._last_screenshot_path，便于 artifact_ref 引用。

    perceiver 已不再自动落盘，因此所有验证证据截图必须由本函数显式产生。
    """
    try:
        if ctx.device is None:
            return
        run_id = getattr(ctx, "_run_tag", "") or "unknown"
        shot_dir = app_paths.SCREENSHOT_DIR / str(run_id)
        shot_dir.mkdir(parents=True, exist_ok=True)
        filename = f"evidence_{verification_key or 'v'}_{seq}.png"
        path = str(shot_dir / filename)
        ctx.device.screenshot().save(path)
        ctx._last_screenshot_path = path
    except Exception as exc:  # 截图失败不应中断验证流程
        logger.warning("evidence screenshot failed: %s", exc)


_evidence_seq = 0


def verification_seq() -> int:
    """递增生成验证证据截图序号，避免同 run 内文件名冲突。"""
    global _evidence_seq
    _evidence_seq += 1
    return _evidence_seq


def _record_deterministic_check(
    text: str,
    kind: str,
    passed: bool,
    verification_key: str = "",
    clause_id: str = "",
    channel: str = "",
) -> None:
    """Record deterministic current-run evidence for a declared clause."""
    try:
        ctx = get_tool_context()
    except Exception:
        return
    if ctx is None:
        return
    if not verification_key or not clause_id or not channel:
        return
    if not hasattr(ctx, "_evidence_events"):
        ctx._evidence_events = []
    ctx._evidence_events.append(
        {
            "verification_key": verification_key,
            "clause_id": clause_id,
            "channel": channel,
            "status": "PASS" if passed else "FAIL",
            # Text/element lookup FAIL is intentionally non-authoritative.
            "authoritative": False,
            "fact": {"kind": kind, "text": str(text or "")},
            "artifact_ref": getattr(ctx, "_last_screenshot_path", "") or "",
        }
    )


@tool
def assert_page_contains(
    text: str,
    pattern: bool = False,
    verification_key: str = "",
    clause_id: str = "",
) -> str:
    """断言当前页面包含指定文本或匹配正则模式。

    - pattern=False（默认）: 检查页面是否包含 text 子串
    - pattern=True: text 作为正则表达式匹配，例: text="\\\\d{2}/\\\\d{2}/\\\\d{4}" 匹配日期格式
      注意: 传入时需双反斜杠转义
    返回: PASS 或 FAIL: <原因>
    """
    _result = _assert_page_contains_impl(text, pattern)
    if verification_key and clause_id:
        _save_evidence_screenshot(
            get_tool_context(), verification_key, verification_seq()
        )
    _record_deterministic_check(
        text,
        "page_contains",
        _result.startswith("PASS"),
        verification_key,
        clause_id,
        "ui_text",
    )
    return _result


def _assert_page_contains_impl(text: str, pattern: bool = False) -> str:
    from tools import get_screen_info  # 延迟 import 避免加载期循环依赖

    ctx = get_tool_context()
    info = (
        get_screen_info.invoke({"mode": "full"})
        if hasattr(get_screen_info, "invoke")
        else get_screen_info(mode="full")
    )

    # 兼容旧行为：没有 perceiver 时仅在 get_screen_info 文本中匹配
    if ctx.perceiver is None:
        if pattern:
            try:
                if re.search(text, info):
                    return f"PASS: 页面匹配模式 /{text}/"
                return f"FAIL: 页面不匹配模式 /{text}/"
            except re.error as e:
                return f"FAIL: 正则错误 - {e}"
        return "PASS" if text in info else f"FAIL: 页面不包含 {text}"

    def _norm(s: str) -> str:
        return re.sub(r"\s+", "", (s or "").lower())

    understanding = ctx.perceiver.perceive()
    all_elements = list(understanding.primary_paths) + [
        e for e in understanding.elements if e not in understanding.primary_paths
    ]
    element_lines: list[str] = []
    for el in all_elements:
        element_lines.append(
            " | ".join(
                [
                    el.label or "",
                    getattr(el, "associated_label", "") or "",
                    el.resource_id or "",
                    el.class_name or "",
                    getattr(el, "context_path", "") or "",
                ]
            )
        )

    haystack = "\n".join([info] + element_lines)
    if pattern:
        try:
            if re.search(text, haystack):
                return f"PASS: 页面匹配模式 /{text}/"
            return f"FAIL: 页面不匹配模式 /{text}/"
        except re.error as e:
            return f"FAIL: 正则错误 - {e}"

    needle = text or ""
    needle_norm = _norm(needle)

    # 1) 原样子串匹配（文本、rid、path）
    if needle and needle in haystack:
        return "PASS"

    # 2) 归一化匹配（处理换行/空格/OCR 分段）
    if needle_norm:
        if needle_norm in _norm(haystack):
            return "PASS"
        for line in element_lines:
            if needle_norm in _norm(line):
                return "PASS"

    return f"FAIL: 页面不包含 {text}"


@tool
def assert_element_exists(
    label: str, verification_key: str = "", clause_id: str = ""
) -> str:
    """断言当前页面存在指定元素（按 text / content_desc / resource_id 匹配）。"""
    ctx = get_tool_context()
    if ctx.perceiver is None:
        return "FAIL: Perceiver not available - no device"
    understanding = ctx.perceiver.perceive()
    matched = any(label in (element.label or "") for element in understanding.elements)
    if verification_key and clause_id:
        _save_evidence_screenshot(ctx, verification_key, verification_seq())
    for element in understanding.elements:
        if label in (element.label or ""):
            _record_deterministic_check(
                label,
                "element_exists",
                True,
                verification_key,
                clause_id,
                "element_state",
            )
            return "PASS"
    _record_deterministic_check(
        label,
        "element_exists",
        False,
        verification_key,
        clause_id,
        "element_state",
    )
    return f"FAIL: 元素不存在 {label}"


@tool
def assert_page_state(
    package: str = "",
    activity: str = "",
    verification_key: str = "",
    clause_id: str = "",
) -> str:
    """Assert the current application package and/or activity deterministically."""
    ctx = get_tool_context()
    current = ctx.device.current_app() if ctx.device else {}
    actual_package = str(current.get("package", "") or "")
    actual_activity = str(current.get("activity", "") or "")
    # Activity 名前导点归一化：设备返回的 activity 常带前导点
    # （如 ".Settings$WifiSettingsActivity"），agent 传入时可能漏掉，
    # strip 前导点后再比较，避免多余一次往返。
    expected_activity = (activity or "").strip().lstrip(".")
    actual_activity_norm = actual_activity.strip().lstrip(".")
    passed = (not package or package == actual_package) and (
        not expected_activity
        or expected_activity == actual_activity_norm
        or _simple_activity_match(expected_activity, actual_activity_norm)
    )
    fact = {"package": actual_package, "activity": actual_activity}
    if verification_key and clause_id:
        _save_evidence_screenshot(ctx, verification_key, verification_seq())
        ctx._evidence_events.append(
            {
                "verification_key": verification_key,
                "clause_id": clause_id,
                "channel": "page_state",
                "status": "PASS" if passed else "FAIL",
                "authoritative": bool(package or activity) and not passed,
                "fact": fact,
                "artifact_ref": getattr(ctx, "_last_screenshot_path", "") or "",
                }
        )
    return "PASS" if passed else f"FAIL: page state {fact}"


def _infer_state_from_text(text: str) -> str | None:
    """从元素文本 / content-desc 推断开关状态（on/off），作为 checked 缺失时的兜底。"""
    text_lower = str(text or "").lower()
    on_markers = ("开启", "打开", "启用", "选中", "勾选", "on", "yes", "true", "已连接")
    off_markers = ("关闭", "禁用", "未选中", "未勾选", "off", "no", "false", "未连接")
    if any(m in text_lower for m in on_markers):
        return "on"
    if any(m in text_lower for m in off_markers):
        return "off"
    return None


@tool
def assert_behavior_effect(
    expected: str, verification_key: str = "", clause_id: str = ""
) -> str:
    """Assert a deterministic page behavior using a restricted predicate DSL."""
    ctx = get_tool_context()
    expected = str(expected or "").strip()
    passed = False
    channel = "behavior_effect"
    # authoritative 默认 False，按谓词在下方分支赋值（契约收敛，见 Plan §5.3.2/§5.4）：
    # 仅确定性 before/after 反证才 True；当前态检查（element_present/element_absent/toggled）一律 False。
    authoritative = False
    fact: dict[str, Any] = {"expected": expected}
    logger.debug(
        "[verify] assert_behavior_effect enter: expected=%r channel=%s",
        expected,
        channel,
    )
    try:
        current_app = ctx.device.current_app() if ctx.device else {}
        if expected.startswith("still_on_activity(") and expected.endswith(")"):
            activity = expected[18:-1]
            actual_activity = str(current_app.get("activity", "") or "")
            passed = actual_activity == activity or _simple_activity_match(
                activity, actual_activity
            )
            fact["activity"] = current_app.get("activity", "")
            # still_on_activity = 确定性 before/after 比较 → 权威反证
            authoritative = True
        elif expected.startswith("no_page_change(") and expected.endswith(")"):
            signature = expected[15:-1]
            actual_signature = (
                str(ctx.perceiver.screen_signature() or "") if ctx.perceiver else ""
            )
            passed = bool(actual_signature) and actual_signature == signature
            fact["signature"] = actual_signature
            # no_page_change = 确定性 before/after 比较 → 权威反证
            authoritative = True
        elif expected.startswith("list_count_unchanged(") and expected.endswith(")"):
            anchor, separator, expected_count = expected[21:-1].rpartition(",")
            if not separator:
                return f"ERROR: list_count_unchanged requires anchor,count: {expected}"
            understanding = ctx.perceiver.perceive() if ctx.perceiver else None
            try:
                count = sum(
                    1
                    for element in (understanding.elements if understanding else [])
                    if anchor in (element.label or "")
                )
                passed = count == int(expected_count)
                fact.update({"anchor": anchor, "count": count})
                # list_count_unchanged = 确定性 before/after 比较 → 权威反证
                authoritative = True
            except ValueError:
                return f"ERROR: invalid list count: {expected}"
        elif expected.startswith("element_present(") and expected.endswith(")"):
            label = expected[16:-1]
            understanding = ctx.perceiver.perceive() if ctx.perceiver else None
            passed = bool(
                understanding
                and any(
                    label in (element.label or "") for element in understanding.elements
                )
            )
            fact["label"] = label
            # element_present = 当前态检查（瞬态）→ 非权威，FAIL 默认 unknown 可重试
            authoritative = False
        elif expected.startswith("element_absent(") and expected.endswith(")"):
            label = expected[15:-1]
            understanding = ctx.perceiver.perceive() if ctx.perceiver else None
            passed = bool(
                understanding
                and not any(
                    label in (element.label or "") for element in understanding.elements
                )
            )
            fact["label"] = label
            # element_absent = 当前态检查（瞬态）→ 非权威，FAIL 默认 unknown 可重试
            authoritative = False
        elif expected.startswith("toggled(") and expected.endswith(")"):
            # 格式校验（P0 第 2 点补强）：以 toggled( 开头但整体不匹配
            # toggled(label,on|off)（裸 toggled / 缺 on|off / 多了参数）→ 直接返回
            # 完整格式示例，不进入解析。既兜底 planner.txt 的 prompt 层禁止，也防
            # 换模型后再次传错（契约收敛：显式示例，不自动补全）。
            if not re.match(r"^toggled\([^,]+,(on|off)\)$", expected):
                return (
                    f"ERROR: toggled 格式不正确: {expected}. "
                    "正确格式为 toggled(label,on|off)，例如 toggled(WLAN,on) "
                    "或 toggled(蓝牙开关,off)。"
                )
            # toggled(label, on|off): 点击某开关后它应变为 on/off。
            # 与 element_state 的区别是语义更明确，且为切换类操作提供直接断言。
            anchor, separator, state_spec = expected[8:-1].rpartition(",")
            if not separator:
                return (
                    f"ERROR: toggled 格式不正确: {expected}. "
                    "正确格式为 toggled(label,on|off)，例如 toggled(WLAN,on) "
                    "或 toggled(蓝牙开关,off)。"
                )
            anchor = anchor.strip().strip('"').strip("'")
            state_spec = state_spec.strip().lower()
            if "=" in state_spec:
                _, expected_value = state_spec.split("=", 1)
            else:
                expected_value = state_spec
            expected_checked = expected_value.strip() in ("true", "on", "yes", "1")
            understanding = ctx.perceiver.perceive() if ctx.perceiver else None
            matched = None
            for element in understanding.elements if understanding else []:
                if anchor and (
                    anchor in (element.label or "")
                    or anchor in (element.resource_id or "")
                ):
                    # 优先匹配真正的开关节点（role=switch 或含 switch 子控件的父行），
                    # 跳过结构容器——它们可能残留硬编码的 checked="false"，导致误读。
                    if (
                        getattr(element, "has_switch_child", False)
                        or getattr(element, "role", "") == "switch"
                    ):
                        matched = element
                        break
                    if matched is None:
                        matched = element
            if matched is None:
                return f"ERROR: toggled anchor not found: {anchor}"
            if matched.checked is not None:
                actual_checked = bool(matched.checked)
                inferred = None
            else:
                inferred = _infer_state_from_text(matched.label) or _infer_state_from_text(
                    getattr(matched, "associated_label", "")
                )
                actual_checked = inferred == "on" if inferred is not None else False
            fact.update({"anchor": anchor, "checked": actual_checked})
            if inferred:
                fact["inferred_from_text"] = inferred
            passed = actual_checked == expected_checked
            # toggled 是「切换行为」断言，产出 behavior_effect 通道，与状态/开关类
            # claim 的默认通道一致。但读的是实时 checked（过渡态 3-4s 不可靠），
            # 故当前态检查 → 非权威，FAIL 默认 unknown 可重试，不触发 fail-fast。
            # 真·失败由 visual_check 高置信 FAIL 做权威确认（见 Plan P0 第 3 点）。
            channel = "behavior_effect"
            authoritative = False
            logger.debug(
                "[verify] toggled realtime checked: anchor=%r expected_checked=%s "
                "actual_checked=%s inferred=%s → passed=%s "
                "(authoritative=False, 过渡态易误报)",
                anchor,
                expected_checked,
                actual_checked,
                inferred,
                passed,
            )
        else:
            # 格式校验（P0 第 2 点补强）：以 toggled 开头但格式不对（裸 toggled /
            # 缺 on|off / 多了括号）时，明确提示完整格式而非只走通用 unsupported。
            # 即便 planner.txt 已从 prompt 层禁止裸 toggled，这里仍兜底，防止换了
            # 模型又传错（契约收敛：显式示例，不自动补全）。
            if expected.startswith("toggled"):
                if not re.match(r"^toggled\([^,]+,(on|off)\)$", expected):
                    return (
                        f"ERROR: toggled 格式不正确: {expected}. "
                        "正确格式为 toggled(label,on|off)，例如 toggled(WLAN,on) "
                        "或 toggled(蓝牙开关,off)。"
                    )
            return (
                f"ERROR: unsupported behavior predicate: {expected}. "
                "supported predicates: still_on_activity(activity), "
                "no_page_change(signature), list_count_unchanged(anchor,count), "
                "element_present(label), element_absent(label), "
                "toggled(label,on|off)"
            )
    except Exception as exc:
        return f"ERROR: behavior effect check failed: {exc}"
    if verification_key and clause_id:
        _save_evidence_screenshot(ctx, verification_key, verification_seq())
        ctx._evidence_events.append(
            {
                "verification_key": verification_key,
                "clause_id": clause_id,
                "channel": channel,
                "status": "PASS" if passed else "FAIL",
                "authoritative": authoritative,
                "fact": fact,
                "artifact_ref": getattr(ctx, "_last_screenshot_path", "") or "",
                }
        )
    logger.debug(
        "[verify] assert_behavior_effect result: expected=%r status=%s "
        "authoritative=%s channel=%s",
        expected,
        "PASS" if passed else "FAIL",
        authoritative,
        channel,
    )
    return "PASS" if passed else f"FAIL: behavior effect not observed: {expected}"


@tool
def terminate_run(reason: str) -> str:
    """Request an evaluator-confirmed abort when no safe path remains."""
    from tools.results import OK, make_result

    return make_result(
        OK,
        "已请求终止运行",
        evidence={
            "agent_abort_requested": True,
            "reason": str(reason or ""),
        },
    )


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
    app_paths.SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    filename = name or f"screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    if not filename.endswith(".png"):
        filename += ".png"
    path = str(app_paths.SCREENSHOT_DIR / filename)
    ctx.device.screenshot().save(path)
    return path

"""验证与终止类工具（页面/元素断言、验证结果上报、报告完成）。

从 tools/__init__.py 拆出（重构 T5），仅移动代码、不改逻辑。
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
from datetime import datetime
from typing import Any

import app_paths

from tools.context import ToolContext, get_tool_context

try:
    from langchain_core.tools import tool
except Exception:

    def tool(func=None, *args, **kwargs):
        def wrapper(f):
            return f

        return wrapper(func) if func else wrapper


def _record_deterministic_check(text: str, kind: str, passed: bool) -> None:
    """M4：记录一次确定性断言（page_contains/element_exists）的结果，
    供 assert_verification 反查作为 ground truth。仅追加、绝不抛异常。"""
    try:
        ctx = get_tool_context()
    except Exception:
        return
    if ctx is None:
        return
    if not getattr(ctx, "_deterministic_checks", None):
        ctx._deterministic_checks = []
    ctx._deterministic_checks.append(
        {
            "text": str(text or ""),
            "kind": kind,
            "result": "pass" if passed else "fail",
        }
    )


def _lookup_deterministic_ground_truth(ctx: ToolContext, condition: str):
    """M4：在已记录的确定性断言中，反查与验证项 condition 匹配的最近一条结果。
    采用保守的「子串包含」匹配（断言文本 ⊆ 验证项，或反之），最近的优先。
    返回 "pass" / "fail" / None。"""
    checks = getattr(ctx, "_deterministic_checks", None) or []
    cond_norm = _normalize_verification_text(condition)
    if not cond_norm:
        return None
    for check in reversed(checks):
        text_norm = _normalize_verification_text(check.get("text", ""))
        if not text_norm:
            continue
        if text_norm in cond_norm or cond_norm in text_norm:
            return check.get("result")
    return None


@tool
def assert_page_contains(text: str, pattern: bool = False) -> str:
    """断言当前页面包含指定文本或匹配正则模式。

    - pattern=False（默认）: 检查页面是否包含 text 子串
    - pattern=True: text 作为正则表达式匹配，例: text="\\\\d{2}/\\\\d{2}/\\\\d{4}" 匹配日期格式
      注意: 传入时需双反斜杠转义
    返回: PASS 或 FAIL: <原因>
    """
    _result = _assert_page_contains_impl(text, pattern)
    # M4：记录确定性核实结果，供 assert_verification 反查 ground truth
    _record_deterministic_check(text, "page_contains", _result.startswith("PASS"))
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
def assert_element_exists(label: str) -> str:
    """断言当前页面存在指定元素（按 text / content_desc / resource_id 匹配）。"""
    ctx = get_tool_context()
    if ctx.perceiver is None:
        return "FAIL: Perceiver not available - no device"
    understanding = ctx.perceiver.perceive()
    for element in understanding.elements:
        if label in (element.label or ""):
            _record_deterministic_check(label, "element_exists", True)
            return "PASS"
    _record_deterministic_check(label, "element_exists", False)
    return f"FAIL: 元素不存在 {label}"


def _normalize_verification_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    return re.sub(r"\s+", "", text)


def _resolve_verification_key(ctx: ToolContext, condition: str) -> str:
    raw = str(condition or "").strip()
    normalized = _normalize_verification_text(raw)
    key_map = getattr(ctx, "_verification_key_map", {}) or {}
    if raw and raw in key_map:
        return str(key_map[raw])
    if normalized and normalized in key_map:
        return str(key_map[normalized])
    if raw.startswith("v") and raw[1:].isdigit():
        return raw
    if normalized:
        digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:8]
        return f"dyn_{digest}"
    return f"dyn_{len(getattr(ctx, '_verifications', [])) + 1}"


@tool
def assert_verification(condition: str, result: str, detail: str = "") -> str:
    """逐条报告验证条件的结果。condition 对应 goal.verification 中的验证项，
    result 为 "passed" 或 "failed"，detail 可选补充说明。
    截图策略：每个验证点都实时截图，保证验证清单中每条记录对应独立证据。"""
    # 延迟 import：避免加载期循环依赖；同时让测试对 tools.get_tool_context 的
    # monkeypatch 生效（局部名遮蔽模块级 import）。
    from tools import _run_multimodal_from_context, get_tool_context

    ctx = get_tool_context()
    if ctx:
        if not hasattr(ctx, "_verifications"):
            ctx._verifications = []
        if not hasattr(ctx, "_verification_detail_retries"):
            ctx._verification_detail_retries = {}
        if not hasattr(ctx, "_duplicate_assert_count"):
            ctx._duplicate_assert_count = 0
        verification_key = _resolve_verification_key(ctx, condition)
        normalized = result if result in ("passed", "failed") else "unknown"
        if normalized == "passed":
            for index, existing in enumerate(ctx._verifications):
                if (
                    str(existing.get("key", "") or "") == verification_key
                    and str(existing.get("result", "") or "") == "passed"
                ):
                    ctx._duplicate_assert_count = (
                        int(ctx._duplicate_assert_count or 0) + 1
                    )
                    return (
                        "DUPLICATE_IGNORED: "
                        + verification_key
                        + f" already passed at step={index + 1}"
                    )
        if normalized in ("passed", "failed") and not (detail or "").strip():
            retries = int(
                ctx._verification_detail_retries.get(verification_key, 0) or 0
            )
            if retries < 2:
                ctx._verification_detail_retries[verification_key] = retries + 1
                return f"ERROR: detail is required for assert_verification (attempt {retries + 1}/2)"
            detail = "detail unavailable after retries"
        else:
            ctx._verification_detail_retries.pop(verification_key, None)
        shot_path = ""  # 相对路径（供前端 /storage 挂载解析）
        shot_abs_path = ""  # 绝对路径（供本地文件操作）

        # 每个验证点都尝试实时截图，失败时再回退到最近缓存截图。
        if ctx.device:
            try:
                app_paths.SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
                safe_cond = re.sub(r"[^\w一-鿿-]", "_", condition[:30])
                verify_index = len(getattr(ctx, "_verifications", [])) + 1
                new_path = str(
                    app_paths.SCREENSHOT_DIR
                    / f"verify_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{verify_index:02d}_{safe_cond}.png"
                )
                ctx.device.screenshot().save(new_path)
                shot_abs_path = new_path
                # 转为相对于 DATA_DIR 的路径，使前端 /storage 挂载能解析
                try:
                    shot_path = os.path.relpath(
                        new_path, app_paths.DATA_DIR_STR
                    ).replace("\\", "/")
                except Exception:
                    shot_path = new_path.replace("\\", "/")
            except Exception:
                shot_abs_path = getattr(ctx, "_last_screenshot_path", "") or ""
                try:
                    shot_path = (
                        os.path.relpath(shot_abs_path, app_paths.DATA_DIR_STR).replace(
                            "\\", "/"
                        )
                        if shot_abs_path
                        else ""
                    )
                except Exception:
                    shot_path = shot_abs_path
        else:
            shot_abs_path = getattr(ctx, "_last_screenshot_path", "") or ""
            try:
                shot_path = (
                    os.path.relpath(shot_abs_path, app_paths.DATA_DIR_STR).replace(
                        "\\", "/"
                    )
                    if shot_abs_path
                    else ""
                )
            except Exception:
                shot_path = shot_abs_path

        if shot_path:
            shot_path = shot_path.replace("\\", "/")
        if shot_abs_path:
            shot_abs_path = shot_abs_path.replace("\\", "/")

        # failed 时追加视觉分析（短超时，不阻塞主流程）
        if (
            normalized == "failed"
            and ctx.verification_auto_vision
            and shot_abs_path
            and os.path.exists(shot_abs_path)
        ):
            try:
                with open(shot_abs_path, "rb") as fh:
                    image_b64 = base64.b64encode(fh.read()).decode("utf-8")
                prompt = (
                    "请分析该失败截图，说明此验证项失败的可能原因。"
                    "只返回 JSON，字段: decision(yes/no/unknown), reason, evidence。"
                    f"验证项: {condition}"
                )
                vres = _run_multimodal_from_context(
                    prompt=prompt,
                    image_base64=image_b64,
                    purpose="verification_fail_analyze",
                    strict_json=True,
                    timeout_sec=10,
                )
                if vres.get("ok"):
                    vis = f"vision={vres.get('decision', 'unknown')}: {vres.get('reason', '')}"
                    detail = f"{detail} | {vis}" if detail else vis
            except Exception:
                pass

        # M4（方案A）：确定性断言参与核实，但 PASS 与 FAIL **不对称**——
        # PASS = 权威 ground truth（文字/元素确实存在，可确认甚至 override）；
        # FAIL = **不权威**（文字未匹配 ≠ 元素不存在，如图标/canvas 绘制/无 content-desc），
        #        只作弱证据，绝不否定模型、绝不 override，避免对图标类 UI 误判为冲突。
        code_gt = _lookup_deterministic_ground_truth(ctx, condition)
        if code_gt == "pass" and normalized in ("passed", "failed"):
            if normalized == "passed":
                tag = "[代码核实=PASS]"
            elif getattr(ctx, "deterministic_verification_override", False):
                normalized = "passed"
                tag = f"[已按代码核实修正为PASS(模型原判定={result})]"
            else:
                tag = f"[⚠️代码核实=PASS，与模型判定({result})冲突]"
            detail = f"{detail} | {tag}" if detail else tag
        elif code_gt == "fail" and normalized == "failed":
            # 模型也判 failed，文字未匹配与之一致 → 作弱佐证（不改判定）。
            # 注意：model=passed 时故意不加任何标记（FAIL 不足以否定，可能是图标）。
            tag = "[代码核实=文字未匹配(与判定一致)]"
            detail = f"{detail} | {tag}" if detail else tag

        ctx._verifications.append(
            {
                "key": verification_key,
                "item": condition,
                "result": normalized,
                "detail": detail,
                "screenshot": shot_path,
            }
        )
    return f"记录完成: {condition} → {result}"


@tool
def report_done(status: str, summary: str = "") -> str:
    """报告测试完成或无法继续。所有验证完成后必须调用此工具。

    Args:
        status: "done" 表示所有验证条件已完成，"abort" 表示无法继续执行
        summary: 简要描述验证结果或无法继续的原因
    """
    return f"REPORTED: {status} | {summary}"


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

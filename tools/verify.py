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


def _save_evidence_screenshot(ctx, verification_key: str) -> None:
    """验证证据点显式截图：保存到 screenshots/{run_id}/evidence_{key}.png
    并写回 ctx._last_screenshot_path，便于 artifact_ref 引用。

    P1 截图去重：文件名按 verification_key 去重（去掉 seq 后缀、同 key 覆盖），
    对齐 _save_perceive_evidence_screenshot 的 evidence_{key}_vision.png 命名。
    语义：同一验证条件只保留一张证据图（同页连续 assert 连拍不再重复落盘）。
    perceiver 已不再自动落盘，因此所有验证证据截图必须由本函数显式产生。
    """
    try:
        if ctx.device is None:
            return
        run_id = getattr(ctx, "_run_tag", "") or "unknown"
        shot_dir = app_paths.SCREENSHOT_DIR / str(run_id)
        shot_dir.mkdir(parents=True, exist_ok=True)
        filename = f"evidence_{verification_key or 'v'}.png"
        path = str(shot_dir / filename)
        ctx.device.screenshot().save(path)
        ctx._last_screenshot_path = path
    except Exception as exc:  # 截图失败不应中断验证流程
        logger.warning("evidence screenshot failed: %s", exc)


def _legal_clause_refs(ctx) -> tuple[set[str], set[str], dict[str, set[str]]]:
    """从当前验证契约收集合法的 verification_key 集合、clause_id 集合，以及 key→clause 映射。

    clause_id 仅接受契约中真实存在的 `v{index}.{clause_index}`（如 v0.0 / v1.0 / v2.1
    需契约确有该子句）。key_to_clauses 用于跨 key 配对校验（防 v1::v2.0 这类错配）。
    """
    valid_keys: set[str] = set()
    valid_clauses: set[str] = set()
    key_to_clauses: dict[str, set[str]] = {}
    contract = getattr(ctx, "_verification_contract", None) or {}
    for v in contract.get("verifications", []) or []:
        if not isinstance(v, dict):
            continue
        key = str(v.get("key", "") or "")
        if key:
            valid_keys.add(key)
            key_to_clauses.setdefault(key, set())
        for c in v.get("clauses", []) or []:
            if not isinstance(c, dict):
                continue
            cid = str(c.get("id", "") or "")
            if cid:
                valid_clauses.add(cid)
                if key:
                    key_to_clauses[key].add(cid)
    return valid_keys, valid_clauses, key_to_clauses


def _check_clause_ref(ctx, verification_key: str, clause_id: str) -> tuple[bool, str]:
    """校验 agent 传入的 (verification_key, clause_id) 是否来自当前契约。

    两层校验：
    1. 孤儿 clause_id（契约中不存在）直接判非法，不写入证据（避免污染 evaluate_verification
       的精确匹配），提示合法候选，让 LLM 自纠。
    2. 跨 key 错配（clause_id 不属于该 verification_key，如 v1::v2.0）判非法——
       孤儿是"id 不存在"，错配是"id 存在但挂错 key"，护栏能抓。
    设计边界：仅传 clause_id 不传 key、或语义错配（v1.0 是 max=10 却拿"默认第4节"当证据）
    这类非确定性问题抓不到，只能靠模型质量（#6）解决，记录在案。
    """
    if not verification_key and not clause_id:
        return True, ""  # 未携带归因信息，放行（由调用方决定是否记录）
    valid_keys, valid_clauses, key_to_clauses = _legal_clause_refs(ctx)
    # 无任何契约上下文（如单元测试未注入 contract）时宽松放行，不阻断。
    if not valid_keys and not valid_clauses:
        return True, ""
    if clause_id and clause_id not in valid_clauses:
        hint = (
            f"clause_id={clause_id!r} 不在当前验证契约，证据未计入。"
            f" 合法 clause_id: {sorted(valid_clauses) or '无'}；"
            f" 合法 verification_key: {sorted(valid_keys) or '无'}。"
            " 请使用契约中的 clause_id（或仅传合法的 verification_key）。"
        )
        return False, hint
    if verification_key and verification_key not in valid_keys:
        hint = (
            f"verification_key={verification_key!r} 不在当前验证契约，证据未计入。"
            f" 合法 verification_key: {sorted(valid_keys) or '无'}。"
        )
        return False, hint
    # 跨 key 配对校验：clause_id 必须归属于该 verification_key（如 v2.0 属于 v2）
    if verification_key and clause_id and key_to_clauses.get(verification_key) is not None:
        if clause_id not in key_to_clauses[verification_key]:
            hint = (
                f"clause_id={clause_id!r} 不属于 verification_key={verification_key!r}"
                f"（跨 key 错配），证据未计入。该 key 合法的 clause_id: "
                f"{sorted(key_to_clauses[verification_key]) or '无'}。"
            )
            return False, hint
    return True, ""


def _record_deterministic_check(
    text: str,
    kind: str,
    passed: bool,
    verification_key: str = "",
    clause_id: str = "",
    channel: str = "",
) -> str:
    """Record deterministic current-run evidence for a declared clause.

    返回 "" 表示证据已记录；返回非空字符串表示校验未通过（孤儿 clause_id），
    该字符串为给 LLM 的提示，调用方可拼接到工具返回值。
    """
    try:
        ctx = get_tool_context()
    except Exception:
        return ""
    if ctx is None:
        return ""
    if not verification_key or not clause_id or not channel:
        return ""
    if not hasattr(ctx, "_evidence_events"):
        ctx._evidence_events = []
    valid, hint = _check_clause_ref(ctx, verification_key, clause_id)
    if not valid:
        logger.warning("verify skip orphan clause ref: %s", hint)
        return hint  # 孤儿 clause_id：不写入证据，返回提示让 LLM 自纠
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
    return ""


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
    _hint = ""
    if verification_key and clause_id:
        _save_evidence_screenshot(
            get_tool_context(), verification_key
        )
        _hint = _record_deterministic_check(
            text,
            "page_contains",
            _result.startswith("PASS"),
            verification_key,
            clause_id,
            "ui_text",
        )
    return _result + ((" | 归因被拒：" + _hint) if _hint else "")


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
    # 先校验归因：孤儿 clause_id 直接提示并拒绝，避免静默丢证据
    _hint = ""
    if verification_key and clause_id:
        valid, _hint = _check_clause_ref(ctx, verification_key, clause_id)
        if not valid:
            logger.warning("assert_element_exists skip orphan clause ref: %s", _hint)
            return f"FAIL: 元素 {label} 查询未完成归因 | 归因被拒：" + _hint
    understanding = ctx.perceiver.perceive()
    matched = any(label in (element.label or "") for element in understanding.elements)
    if verification_key and clause_id and valid:
        _save_evidence_screenshot(ctx, verification_key)
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
            return "PASS" + ((" | 归因被拒：" + _hint) if _hint else "")
    _record_deterministic_check(
        label,
        "element_exists",
        False,
        verification_key,
        clause_id,
        "element_state",
    )
    return (f"FAIL: 元素不存在 {label}") + ((" | 归因被拒：" + _hint) if _hint else "")


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
        valid, hint = _check_clause_ref(ctx, verification_key, clause_id)
        if not valid:
            logger.warning("assert_page_state skip orphan clause ref: %s", hint)
            return ("PASS" if passed else f"FAIL: page state {fact}") + " | 归因被拒：" + hint
        _save_evidence_screenshot(ctx, verification_key)
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


def _resolve_anchor(
    understanding: Any, label: str, predicate: str = "disabled"
) -> tuple[Any | None, str]:
    """断言类谓词的统一锚点解析。返回 (element, err)；err 非空时 element 为 None。

    predicate 是调用方谓词名（如 "disabled"），只用于错误串文案，绝不硬编码——
    将来 toggled 走此函数也要传入它的 predicate，否则 LLM 会收到谓词名错的提示
    （这正是本次修掉的 enabled(label) 文案漂移同类问题）。

    分层精确优先 + 排除结构容器 + 层内唯一性检查（F1）：

    第 0 步：排除 is_container 控件（layout/recyclerview/scroll/viewgroup 等，
    见 perceiver.py:375）。这些容器可能通过 associated_label 携带数字/文本，
    子串命中会把周 chip 之类的叶子控件误判到 ScrollView 上。

    承重依赖：perceiver.py:404 把容器排除在「同页重复 label 抑制」判定之外——
    否则 ScrollView/GridLayout/chip 三个 "10" 会互相抑制使 chip 的 label 变空，
    层 1/层 3 都拿不到它。本函数依赖这个排除，才有意义。

    层 1：label == el.label 或 el.text（label = text or content_desc or
          associated_label；同页重复 label 被 suppress 时 el.label 返回 ""，
          此时回退到 el.text 仍能命中或落到 ambiguous，不静默 not found）
    层 2：label == rid 叶子名（rid.split("/")[-1].split(".")[-1].lower()）
    层 3：子串命中 el.label 或 el.resource_id（保留既有行为，不回退能力）

    某层非空即停；层内 >1 候选判歧义（ambiguous）。
    所有字段访问必须 getattr 带默认（FakeElement 等测试替身字段不全）。
    """
    if not label:
        return None, f"ERROR: {predicate} anchor not found: {label}"

    elements = getattr(understanding, "elements", []) or []

    def _rid_leaf(el: Any) -> str:
        rid = getattr(el, "resource_id", "") or ""
        return rid.split("/")[-1].split(".")[-1].lower()

    # 第 0 步：排除容器
    leaves = [el for el in elements if not getattr(el, "is_container", False)]

    # 层 1：label 或 text 精确（覆盖同页重复 label 被 suppress 为 "" 的情况）
    layer1 = [
        el for el in leaves
        if label == (getattr(el, "label", "") or "").strip()
        or label == (getattr(el, "text", "") or "").strip()
    ]
    if len(layer1) == 1:
        return layer1[0], ""
    if len(layer1) > 1:
        return None, _ambiguous_err(label, layer1, predicate)

    # 层 2：rid 叶子精确
    layer2 = [el for el in leaves if label == _rid_leaf(el)]
    if len(layer2) == 1:
        return layer2[0], ""
    if len(layer2) > 1:
        return None, _ambiguous_err(label, layer2, predicate)

    # 层 3：子串（保留既有行为）
    layer3 = [
        el for el in leaves
        if label in (getattr(el, "label", "") or "")
        or label in (getattr(el, "resource_id", "") or "")
    ]
    if len(layer3) == 1:
        return layer3[0], ""
    if len(layer3) > 1:
        return None, _ambiguous_err(label, layer3, predicate)

    return None, f"ERROR: {predicate} anchor not found: {label}"


def _ambiguous_err(label: str, cands: list[Any], predicate: str = "disabled") -> str:
    """层内多候选 → 歧义，附带候选清单让 LLM 换更精确锚点（不写证据、不标权威）。

    头部报总数（不静默截断），正文最多列前 5 个，符合项目「不做静默截断」原则。
    predicate 同 _resolve_anchor，不硬编码谓词名。
    """
    lines = [
        f"ERROR: {predicate} anchor ambiguous: {label} "
        f"（共 {len(cands)} 个候选，显示前 5 个）"
    ]
    for el in cands[:5]:
        rid = getattr(el, "resource_id", "") or ""
        cls = (getattr(el, "class_name", "") or "").split(".")[-1]
        bounds = getattr(el, "bounds", None)
        lines.append(
            f"  - label={getattr(el, 'label', '')!r} rid={rid!r} "
            f"class={cls} bounds={bounds}"
        )
    return "\n".join(lines)


@tool
def assert_behavior_effect(
    expected: str, verification_key: str = "", clause_id: str = ""
) -> str:
    """Assert a deterministic page behavior using a restricted predicate DSL.

    Supported predicates:
    - still_on_activity(activity)            -> before/after 比较, authoritative=True
    - no_page_change(signature)              -> before/after 比较, authoritative=True
    - list_count_unchanged(anchor,count)     -> before/after 比较, authoritative=True
    - element_present(label)                 -> 当前态检查, authoritative=False
    - element_absent(label)                  -> 当前态检查, authoritative=False
    - toggled(label,on|off)                  -> 当前态(checked 过渡态), authoritative=False
    - disabled(label)                        -> 读 View.isEnabled(), 置灰矛盾 authoritative=True
    """
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
                # 方案 3a（Plan §4）：chip/TextView 等无 checkbox 子控件的元素，其选中态
                # 真实来源是 element.selected（已通过 F3 渲染为 [SELECTED]），而非 on/off 文本。
                # 优先读 selected，避免把"已选中的 chip"误判为未选中→FAIL→逼 LLM 走 visual_check。
                _selected = getattr(matched, "selected", None)
                if _selected is True:
                    actual_checked = True
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
        elif expected.startswith("disabled(") and expected.endswith(")"):
            # disabled(label): 断言某元素处于置灰/不可交互状态（enabled=False）。
            #
            # 权威性契约（F2，决策反转，非纯 bug fix）：
            #   enabled == False → PASS 且 authoritative=True。
            #     「确实调用了 setEnabled(false)」是可核实事实，代码可主张权威。
            #   enabled == True  → FAIL 但 authoritative=False（收紧）。
            #     enabled=True 只能证明「App 没调 setEnabled(false)」，推不出
            #     「用户可选中它」——不可选还能用 selected、自绘、OnClickListener
            #     直接 return、父容器拦截等方式表达。代码在此不替 LLM 下它证不了
            #     的结论。非权威 FAIL 落在 verification.py 优先级链第三档 → clause
            #     保持 unknown 可重试，不再触发 llm_runtime 的 mid-batch break。
            #
            # 代价（必须记账）：「真该置灰却没置灰」会从 fail-fast 降级为 unknown
            # + 靠 LLM 取证。这是有意反转，不是 bug。正向能力不丢：enabled=False
            # 仍是权威 PASS。缓解靠 F3（[SELECTED] 渲染 + 全字段 fact 上报），那是
            # 「更全的信息 + LLM 判断」，不是「代码硬保证」。
            #
            # 锚点解析（F1）：用 _resolve_anchor 统一分层精确优先 + 排除容器 +
            # 唯一性检查，替代原来「子串 + 首个命中即停 + 不过滤容器」的坏匹配器
            # （192037 误把 ScrollView「10」当周「1」）。
            label = expected[9:-1].strip().strip('"').strip("'")
            if not label:
                return f"ERROR: disabled requires a label: {expected}"
            understanding = ctx.perceiver.perceive() if ctx.perceiver else None
            # 层内候选在此收敛，toggled 统一时可在此返回 candidates 供 tie-break。
            matched, anchor_err = _resolve_anchor(understanding, label, predicate="disabled")
            if anchor_err:
                # not-found / ambiguous：不写证据、不标权威（err 串保持 ERROR 前缀，
                # 既有测试锁定该词表）。
                return anchor_err
            actual_enabled = bool(getattr(matched, "enabled", True))
            fact.update(
                {
                    "anchor": label,
                    "resource_id": getattr(matched, "resource_id", None),
                    "class_name": getattr(matched, "class_name", None),
                    "bounds": getattr(matched, "bounds", None),
                    "enabled": actual_enabled,
                    "clickable": getattr(matched, "clickable", False),
                    "selected": bool(getattr(matched, "selected", False)),
                    # checked 是 bool|None：None=非可勾选类型，False=可勾选但未勾。
                    # 原样带出，bool() 会抹平这个区分（F2 细节 1）。
                    "checked": getattr(matched, "checked", None),
                    "expected_disabled": True,
                }
            )
            passed = not actual_enabled
            channel = "behavior_effect"
            if actual_enabled:
                # enabled=True 只证明「没调 setEnabled」，非权威反证。
                authoritative = False
            else:
                # enabled=False 是「确实置灰」的可核实事实，权威 PASS。
                authoritative = True
            logger.debug(
                "[verify] disabled state: anchor=%r enabled=%s expected_disabled=True "
                "→ passed=%s (authoritative=%s)",
                label,
                actual_enabled,
                passed,
                authoritative,
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
                "toggled(label,on|off), disabled(label)"
            )
    except Exception as exc:
        return f"ERROR: behavior effect check failed: {exc}"
    if verification_key and clause_id:
        valid, hint = _check_clause_ref(ctx, verification_key, clause_id)
        if not valid:
            logger.warning("assert_behavior_effect skip orphan clause ref: %s", hint)
            return ("PASS" if passed else f"FAIL: behavior effect not observed: {expected}") + " | 归因被拒：" + hint
        _save_evidence_screenshot(ctx, verification_key)
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

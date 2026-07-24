from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any

import numpy as np

from llm.multimodal import multimodal_vision_call
from tools.context import ToolContext, get_tool_context, set_tool_context
from tools.text_utils import (
    _ZH_CONTROL_TOKENS,
    _cjk_char_overlap,
    _expand_zh_keywords,
    _has_cjk,
    _normalize_text,
)
from tools.element_match import (
    _CLICK_PREF_DEFAULT_WEIGHTS,
    _ROLE_PRIORITY,
    _disambiguate_container,
    _extract_click_preferences_from_rag,
    _find_best_element_with_known,
    _pref_bonus_for_element,
    _prefs_active_for_description,
    _promote_to_clickable_parent,
    _rank_click_candidates,
    _score_element,
    _search_elements,
)
from tools.device_ops import (
    check_desktop_mode,
    clear_app_data,
    copy,
    launch_app,
    open_app_permission_settings,
    open_notification,
    open_quick_settings,
    paste,
    press_key,
    scroll_panel,
    set_orientation,
    swipe,
    toggle_auto_rotate,
    type_input,
    unlock_screen,
)
from tools.knowledge_tools import (
    _experience_relevance,
    query_app_knowledge,
    query_element_identity,
)
from tools.verify import (
    _normalize_verification_text,
    _resolve_verification_key,
    assert_element_exists,
    assert_page_contains,
    assert_verification,
    log_step,
    report_done,
    save_screenshot,
)
from tools.perceive_tools import (
    check_page_health,
    detect_overlay,
    detect_popup,
    dismiss_popup,
    recover_from_anomaly,
    respond_to_permission_dialog,
    switch_perception_mode,
    visual_check,
    wait_for_permission_dialog,
    wait_seconds,
)
from tools.click import (
    _capture_page_id,
    _check_switch_state,
    _compute_page_signature,
    _exact_clickable_candidates,
    _extract_curated_rule_label,
    _format_click_log,
    _has_meaningful_ui_elements,
    _is_expected_destination,
    _is_target_consistent,
    _maybe_promote_exact_rule,
    _post_click_snapshot,
    _query_known_by_rid,
    _query_known_identities,
    _record_page_transition,
    _rid_matches,
    _save_click_identity,
    _score_known_identity,
    click,
    long_press,
    navigate_to,
    reset_session_click_ids,
    scroll_find_and_click,
)
import app_paths

logger = logging.getLogger(__name__)


def _run_multimodal_from_context(
    prompt: str,
    image_base64: str,
    purpose: str,
    strict_json: bool = True,
    timeout_sec: int = 12,
) -> dict[str, Any]:
    ctx = get_tool_context()
    return multimodal_vision_call(
        prompt=prompt,
        image_base64=image_base64,
        purpose=purpose,
        strict_json=strict_json,
        provider=ctx.llm_provider,
        model=ctx.llm_model,
        api_key=ctx.llm_api_key,
        base_url=ctx.llm_base_url,
        vision_enabled=ctx.llm_vision_enabled,
        timeout_sec=timeout_sec,
    )


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
def get_screen_info(mode: str = "full", offset: int = 0, limit: int = 50) -> str:
    """获取当前页面的结构化语义信息。

    mode 参数：
    - "full"（默认）: 返回主要路径和元素概览，适合规划和分析。
    - "clickable": 分页返回可点击元素及其全局 [n]；可用 offset/limit 查看后续项。
    """
    ctx = get_tool_context()
    if ctx.perceiver is None:
        return "Perceiver not available - no Android device connected"
    understanding = ctx.perceiver.perceive()
    # 契约：所有真实可点击元素均有全局 index，且与 click(index=n) 一致。
    indexed_clickables = [
        e for e in understanding.elements if getattr(e, "clickable", False)
    ]
    clickable_index_map = {id(e): i for i, e in enumerate(indexed_clickables)}
    # 页面身份: activity + 标题
    act = understanding.activity.split(".")[-1] if understanding.activity else "?"
    title = understanding.page_title or ""
    page_id = f"{act}" if not title else f"{act}「{title}」"
    lines = [
        f"page={page_id}",
        understanding.summary,
        f"layout={understanding.layout}",
    ]
    _append_panel_summary(lines, understanding, indexed_clickables)

    if mode == "clickable":
        # 对有文本项按 label 去重；无文本项必须逐一保留，bounds/class/path
        # 是它们的唯一性事实，不能合并或丢弃。
        seen_labels: set[str] = set()
        display_items: list[Any] = []
        for element in indexed_clickables:
            key = (element.label or "").strip()
            if key:
                if key in seen_labels:
                    continue
                seen_labels.add(key)
            display_items.append(element)
        page_offset = max(0, int(offset))
        page_limit = min(max(1, int(limit)), 100)
        page_items = display_items[page_offset : page_offset + page_limit]
        end = page_offset + len(page_items)
        lines.append(
            f"clickable_total={len(indexed_clickables)} "
            f"display_items={len(display_items)} showing={page_offset}-{max(page_offset, end - 1)}"
        )
        for item in page_items:
            lines.append(_format_element_line(item, clickable_index_map.get(id(item))))
        if end < len(display_items):
            lines.append(
                f"...（还有 {len(display_items) - end} 项；"
                f"使用 get_screen_info(mode='clickable', offset={end}) 查看后续全局 [n]）"
            )
    else:
        # 全量概览：导航项 + 所有元素。大型页面请改用 clickable 分页查看 index。
        lines.append(f"primary_paths={len(understanding.primary_paths)}")
        for item in understanding.primary_paths[:40]:
            lines.append(_format_element_line(item, clickable_index_map.get(id(item))))
        lines.append(f"all_elements={len(understanding.elements)}")
        for item in understanding.elements[:60]:
            lines.append(_format_element_line(item, clickable_index_map.get(id(item))))

    return "\n".join(lines)


def _append_panel_summary(
    lines: list[str], understanding: Any, indexed_clickables: list[Any]
) -> None:
    """Expose pane facts using the same all-clickable index contract as click()."""
    if getattr(understanding, "layout", "") != "two_pane":
        return
    regions = getattr(understanding, "regions", []) or []
    if not regions:
        return
    lines.append("panels（动态边界；[n] 为全局真实可点击序号）：")
    for panel in regions:
        name = str(panel.get("name", "main_content"))
        bounds = panel.get("bounds", [])
        panel_elements = [
            element
            for element in (getattr(understanding, "elements", []) or [])
            if getattr(element, "region", "main_content") == name
        ]
        panel_clickables = [
            element for element in panel_elements if getattr(element, "clickable", False)
        ]
        panel_indexes = [
            index
            for index, element in enumerate(indexed_clickables)
            if getattr(element, "region", "main_content") == name
        ]
        labeled_count = sum(
            bool((getattr(element, "label", "") or "").strip())
            for element in panel_clickables
        )
        unlabeled = [
            element
            for element in panel_clickables
            if not (getattr(element, "label", "") or "").strip()
        ]
        index_text = ",".join(map(str, panel_indexes[:12])) or "无"
        if len(panel_indexes) > 12:
            index_text += f",...(+{len(panel_indexes) - 12})"
        lines.append(
            f"- {name} bounds={bounds} elements={len(panel_elements)} "
            f"clickable_total={len(panel_clickables)} "
            f"labeled_clickable={labeled_count} global_indexes={index_text}"
        )
        if name == "right_content" and unlabeled:
            example = unlabeled[0]
            example_index = indexed_clickables.index(example)
            example_class = (
                (getattr(example, "class_name", "") or "").split(".")[-1] or "?"
            )
            example_path = getattr(example, "context_path", "") or "?"
            lines.append(
                f"  ! 右侧存在 {len(unlabeled)} 个无文本可点击元素；"
                "使用其全局 [n]，不要改用左侧导航的 [n]。"
            )
            lines.append(
                f"    示例：[{example_index}] <无文本> class={example_class} "
                f"bounds={getattr(example, 'bounds', ())} path={example_path}"
            )
        elif name == "right_content" and panel_elements and not panel_clickables:
            lines.append(
                "  ! 右侧内容区没有真实可点击元素；不要改用左侧导航的 [n]。"
            )


def _format_element_line(item: Any, clickable_index: int | None = None) -> str:
    """格式化单个元素为一行。"""
    rid = item.resource_id or ""
    cls = item.class_name or ""
    assoc = getattr(item, "associated_label", "") or ""
    ctx_path = getattr(item, "context_path", "") or ""
    has_switch = getattr(item, "has_switch_child", False)
    checked = item.checked
    clickable_mark = " [CLICKABLE]" if getattr(item, "clickable", False) else ""
    extra = f" rid={rid}" if rid else ""
    extra += f" class={cls.split('.')[-1]}" if cls else ""
    if assoc and assoc != item.label:
        extra += f" assoc='{assoc}'"
    if has_switch:
        state = "on" if checked is True else ("off" if checked is False else "?")
        extra += f" switch_state={state}"
    if ctx_path:
        extra += f" path='{ctx_path}'"
    rag_hint = getattr(item, "rag_hint", "") or ""
    if rag_hint and rag_hint != item.label:
        extra += f" (经验推断:{rag_hint})"
    idx_prefix = f"[{clickable_index}] " if clickable_index is not None else ""
    label = (getattr(item, "label", "") or "").strip() or "<无文本>"
    return (
        f'- {idx_prefix}[{item.region}/{item.role}] "{label}"{extra}'
        f" bounds={item.bounds}{clickable_mark}"
    )


@tool
def find_element(description: str) -> str:
    """Find UI elements matching description. NOTE: Usually NOT needed — use click() directly, it auto-searches. Only use find_element when you need to inspect candidates before acting."""
    ctx = get_tool_context()
    if ctx.perceiver is None:
        return "NOT_FOUND: perceiver unavailable"

    # Phase 1: UI_TREE (fast, milliseconds)
    t0 = time.time()
    understanding = ctx.perceiver.perceive()
    result = _search_elements(understanding, description)
    if result:
        logger.info(
            "find_element[ui_tree] found in %.2fs: %r", time.time() - t0, description
        )
        return result
    logger.info(
        "find_element[ui_tree] MISS in %.2fs: %r (elements=%d paths=%d)",
        time.time() - t0,
        description,
        len(understanding.elements) if understanding else 0,
        len(understanding.primary_paths) if understanding else 0,
    )

    # Phase 2: Vision-augmented hybrid fallback (slow, seconds)
    t1 = time.time()
    understanding = ctx.perceiver.perceive(force_vision=True)
    result = _search_elements(understanding, description)
    logger.info(
        "find_element[vision] %s in %.2fs: %r (%d candidates)",
        "HIT" if result else "MISS",
        time.time() - t1,
        description,
        result.count("candidate") if result else 0,
    )
    return result


# ═══════════════════════════════════════════
#  工具分组
# ═══════════════════════════════════════════

PLANNER_TOOLS: list[Any] = [
    get_screen_info,
    query_app_knowledge,
]

AGENT_TOOLS: list[Any] = [
    get_screen_info,
    query_app_knowledge,
    query_element_identity,
    click,
    navigate_to,
    scroll_find_and_click,
    long_press,
    copy,
    scroll_panel,
    type_input,
    press_key,
    paste,
    swipe,
    open_notification,
    open_quick_settings,
    unlock_screen,
    set_orientation,
    toggle_auto_rotate,
    check_desktop_mode,
    clear_app_data,
    launch_app,
    open_app_permission_settings,
    visual_check,
    detect_overlay,
    detect_popup,
    dismiss_popup,
    wait_seconds,
    wait_for_permission_dialog,
    respond_to_permission_dialog,
    check_page_health,
    recover_from_anomaly,
    assert_page_contains,
    assert_element_exists,
    assert_verification,
    report_done,
]

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

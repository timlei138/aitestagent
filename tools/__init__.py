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
from tools.context import ToolContext
from llm.safety import check_dangerous

import app_paths

logger = logging.getLogger(__name__)

_CONTEXT: ToolContext | None = None


def set_tool_context(context: ToolContext) -> None:
    global _CONTEXT
    _CONTEXT = context
    reset_session_click_ids()  # 每次新执行时重置 session 去重


def get_tool_context() -> ToolContext:
    if _CONTEXT is None:
        raise RuntimeError("ToolContext 未初始化")
    return _CONTEXT


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
def get_screen_info(mode: str = "full") -> str:
    """获取当前页面的结构化语义信息。

    mode 参数：
    - "full"（默认）: 返回全部元素（导航项 + 所有可交互元素），适合规划和分析
    - "clickable": 仅返回可点击的元素，适合执行 click 前快速查找目标
    """
    ctx = get_tool_context()
    if ctx.perceiver is None:
        return "Perceiver not available - no Android device connected"
    understanding = ctx.perceiver.perceive()
    indexed_clickables = [
        e
        for e in understanding.elements
        if getattr(e, "clickable", False) and (e.label or "")
    ]
    clickable_index_map = {id(e): i for i, e in enumerate(indexed_clickables)}
    # 页面身份: activity + 标题
    act = understanding.activity.split(".")[-1] if understanding.activity else "?"
    title = understanding.page_title or ""
    page_id = f"{act}" if not title else f"{act}「{title}」"
    lines = [
        f"page={page_id}",
        understanding.summary,
        (
            "layout=two_pane（结构分区标签，不保证左右方位）"
            if understanding.layout == "two_pane"
            else f"layout={understanding.layout}"
        ),
    ]

    if mode == "clickable":
        # 仅可点击元素（按 role 优先级排序）
        clickable = [e for e in understanding.elements if e.clickable]
        # 去重：按 label 去重，保留第一个
        seen_labels: set[str] = set()
        unique: list[Any] = []
        for e in clickable:
            key = (e.label or "").strip()
            if key and key not in seen_labels:
                seen_labels.add(key)
                unique.append(e)
        lines.append(f"clickable_elements={len(unique)}")
        for item in unique[:50]:
            lines.append(_format_element_line(item, clickable_index_map.get(id(item))))
    else:
        # 全量：导航项 + 所有元素
        lines.append(f"primary_paths={len(understanding.primary_paths)}")
        for item in understanding.primary_paths[:40]:
            lines.append(_format_element_line(item, clickable_index_map.get(id(item))))
        lines.append(f"all_elements={len(understanding.elements)}")
        for item in understanding.elements[:60]:
            lines.append(_format_element_line(item, clickable_index_map.get(id(item))))

    return "\n".join(lines)


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
    idx_prefix = f"[{clickable_index}] " if clickable_index is not None else ""
    return (
        f'- {idx_prefix}[{item.region}/{item.role}] "{item.label}"{extra}'
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

_CLICK_PREF_DEFAULT_WEIGHTS = {
    "textview": 4,
    "path": 4,
    "label_role": 3,
    "avoid_class": 3,
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


def _normalize_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _extract_click_preferences_from_rag(rag_summary: str) -> dict[str, Any]:
    """从 RAG 文本中提取 click 候选偏好（轻量规则解析）。"""
    text = _normalize_text(rag_summary)
    if not text:
        return {}
    prefs: dict[str, Any] = {
        "label_contains": [],
        "role_prefer": [],
        "class_prefer": [],
        "path_contains": [],
        "avoid_class": [],
        "weights": dict(_CLICK_PREF_DEFAULT_WEIGHTS),
    }

    if "应用列表" in text:
        prefs["label_contains"].append("应用列表")
    if "role=list_entry" in text or "role 为 list_entry" in text:
        prefs["role_prefer"].append("list_entry")
    if "textview" in text or "class为textview" in text or "class 是 textview" in text:
        prefs["class_prefer"].append("textview")
    if "taskbar_container > taskbar_view" in text:
        prefs["path_contains"].append("taskbar_container > taskbar_view")
    if "framelayout" in text and ("避免" in text or "不要" in text or "降级" in text):
        prefs["avoid_class"].append("framelayout")

    if not any(
        prefs.get(k)
        for k in ("label_contains", "role_prefer", "class_prefer", "path_contains")
    ):
        return {}
    return prefs


def _prefs_active_for_description(
    prefs: dict[str, Any] | None, description: str
) -> bool:
    if not prefs:
        return False
    labels = [str(x).strip().lower() for x in (prefs.get("label_contains") or []) if x]
    if not labels:
        return True
    desc = _normalize_text(description)
    return any(lbl in desc for lbl in labels)


def _pref_bonus_for_element(
    el: Any, prefs: dict[str, Any] | None, description: str
) -> int:
    if not _prefs_active_for_description(prefs, description):
        return 0
    weights = dict(_CLICK_PREF_DEFAULT_WEIGHTS)
    if isinstance((prefs or {}).get("weights"), dict):
        weights.update(prefs.get("weights") or {})

    bonus = 0
    cls = _normalize_text(getattr(el, "class_name", "")).split(".")[-1]
    role = _normalize_text(getattr(el, "role", ""))
    label = _normalize_text(getattr(el, "label", ""))
    path = _normalize_text(getattr(el, "context_path", ""))

    if cls and cls in [str(v).lower() for v in (prefs or {}).get("class_prefer", [])]:
        bonus += int(weights["textview"])
    if any(
        str(v).strip().lower() in path
        for v in (prefs or {}).get("path_contains", [])
        if v
    ):
        bonus += int(weights["path"])
    if any(
        str(v).strip().lower() in label
        for v in (prefs or {}).get("label_contains", [])
        if v
    ) and (
        not (prefs or {}).get("role_prefer")
        or role in [str(v).lower() for v in (prefs or {}).get("role_prefer", [])]
    ):
        bonus += int(weights["label_role"])
    if cls and cls in [str(v).lower() for v in (prefs or {}).get("avoid_class", [])]:
        bonus -= int(weights["avoid_class"])
    return bonus


def _score_element(
    el: Any,
    words: list[str],
    prefs: dict[str, Any] | None = None,
    description: str = "",
) -> int:
    """为单个元素计算匹配得分。字段权重：label=3, rid=3, assoc=2, cls=1, ctx_path=1。
    CJK 词精确匹配失败时，退化为字符级重叠计分（阈值 >= 0.67）。
    搜索栏 / 输入框降权，避免被误匹配为导航项。"""
    score = 0
    label = (el.label or "").lower()
    rid = (el.resource_id or "").lower()
    cls = (el.class_name or "").lower()
    assoc = (getattr(el, "associated_label", "") or "").lower()
    ctx_path = (getattr(el, "context_path", "") or "").lower()

    cjk_fallback: list[tuple[str, int]] = []  # (query_word, field_weight)

    for w in words:
        if not w:
            continue
        matched = False
        if w in label:
            score += 3
            matched = True
        if w in assoc:
            score += 2
            matched = True
        if w in rid:
            score += 3
            matched = True
        if w in cls:
            score += 1
            matched = True
        if w in ctx_path:
            score += 1
            matched = True
        # 精确匹配失败 + 含 CJK → 登记为候选 fallback
        if not matched and _has_cjk(w):
            cjk_fallback.append((w, 0))  # weight 稍后统一处理

    # ── 搜索栏 / 输入框降权 ──
    rid_lower = rid.lower()
    if any(kw in rid_lower for kw in ("search", "search_bar", "edittext", "input")):
        score = max(0, score - 2)

    # ── CJK 字符重叠 fallback ──
    if score == 0 and cjk_fallback:
        for w, _ in cjk_fallback:
            for field_text, weight in [
                (label, 3),
                (rid, 3),
                (assoc, 2),
                (cls, 1),
                (ctx_path, 1),
            ]:
                if not field_text or not _has_cjk(field_text):
                    continue
                overlap = _cjk_char_overlap(w, field_text)
                # 阈值 0.67: "通用设置" vs "搜索设置" = 2/4=0.5 → 拒绝
                #             "日期与时间" vs "日期和时间" = 4/5=0.8 → 通过
                if overlap >= 0.67:
                    score += max(1, int(weight * overlap))
                    break

    score += _pref_bonus_for_element(el, prefs, description)
    return score


def _cjk_char_overlap(query_word: str, target: str) -> float:
    """计算两个 CJK 字符串的字符集重叠率（Jaccard-like）。
    例: "日期与时间" vs "日期和时间" → 重叠 {"日","期","时","间"} / 5 = 0.8
    """
    q_chars = {c for c in query_word if "一" <= c <= "鿿"}
    t_chars = {c for c in target if "一" <= c <= "鿿"}
    if not q_chars or not t_chars:
        return 0.0
    overlap = q_chars & t_chars
    return len(overlap) / max(len(q_chars), len(t_chars))


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

    logger.debug(
        "_search_elements: desc=%r words=%s total_elements=%d",
        description,
        expanded_words,
        len(all_elements),
    )

    candidates: list[tuple[int, int, Any]] = []
    for el in all_elements:
        score = _score_element(el, expanded_words)
        if score <= 0:
            continue
        # role 优先级（数值越小越优）
        role_pri = _ROLE_PRIORITY.get(getattr(el, "role", ""), 50)
        candidates.append((score, role_pri, el))

    if not candidates:
        logger.info(
            "_search_elements: NO candidates for %r (searched %d elements, words=%s)",
            description,
            len(all_elements),
            expanded_words,
        )
        return ""

    # 排序：得分 desc → label 长度 asc(短优先=更精确) → role 优先级 asc → 位置
    candidates.sort(
        key=lambda x: (
            -x[0],
            len(x[2].label or ""),
            x[1],
            x[2].bounds[1],
            x[2].bounds[0],
        )
    )

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


def _disambiguate_container(
    best_el: Any,
    all_elements: list[Any],
    prefs: dict[str, Any] | None = None,
    description: str = "",
) -> Any | None:
    """当最佳匹配是大型容器（list_entry/container）且存在同 label 的 navigation_item
    子元素时，返回子元素替代容器。解决 taskbar_view 等容器遮盖子控件的问题。"""
    best_role = getattr(best_el, "role", "")
    if best_role not in ("list_entry", "container"):
        return None
    best_label = (best_el.label or "").strip()
    if not best_label or len(best_label) < 2:
        return None
    bb = best_el.bounds
    if not bb or len(bb) != 4:
        return None
    best_area = (bb[2] - bb[0]) * (bb[3] - bb[1])
    if best_area <= 0:
        return None

    allow_list_entry = _prefs_active_for_description(
        prefs, description
    ) and "list_entry" in [str(v).lower() for v in (prefs or {}).get("role_prefer", [])]
    allowed_roles = {"navigation_item", "button", "tab"}
    if allow_list_entry:
        allowed_roles.add("list_entry")

    picked = None
    picked_key: tuple[int, int] | None = None
    for el in all_elements:
        if not el.clickable or el is best_el:
            continue
        el_label = (el.label or "").strip()
        if el_label != best_label:
            continue
        el_role = getattr(el, "role", "")
        if el_role not in allowed_roles:
            continue
        eb = el.bounds
        if not eb or len(eb) != 4:
            continue
        el_area = (eb[2] - eb[0]) * (eb[3] - eb[1])
        if not (el_area > 0 and el_area < best_area * 0.5):
            continue
        pref_bonus = _pref_bonus_for_element(el, prefs, description)
        key = (pref_bonus, -el_area)
        if picked is None or (picked_key is not None and key > picked_key):
            picked = el
            picked_key = key
    if picked is not None:
        logger.info(
            "_disambiguate: role=%s label=%r → role=%s class=%s",
            best_role,
            best_label,
            getattr(picked, "role", ""),
            getattr(picked, "class_name", ""),
        )
    return picked


def _rank_click_candidates(
    understanding: Any,
    description: str,
    known_ids: list[dict[str, Any]] | None = None,
    prefs: dict[str, Any] | None = None,
) -> list[tuple[int, int, Any]]:
    if understanding is None:
        return []
    all_elements = list(understanding.primary_paths) + [
        e for e in understanding.elements if e not in understanding.primary_paths
    ]
    desc_lower = description.lower().strip()
    raw_words = [w for w in re.split(r"\s+", desc_lower) if w] or [desc_lower]
    words = [w for w in raw_words if (len(w) > 1 or _has_cjk(w))] or raw_words
    expanded_words = _expand_zh_keywords(words)

    candidates: list[tuple[int, int, Any]] = []
    for el in all_elements:
        if not el.clickable:
            continue
        score = _score_element(el, expanded_words, prefs, description)
        if score <= 0:
            continue
        if known_ids:
            score += _score_known_identity(el, known_ids)
        role_pri = _ROLE_PRIORITY.get(getattr(el, "role", ""), 50)
        candidates.append((score, role_pri, el))
    candidates.sort(
        key=lambda x: (
            -x[0],
            len(x[2].label or ""),
            x[1],
            x[2].bounds[1],
            x[2].bounds[0],
        )
    )
    return candidates


def _find_best_element_with_known(
    understanding: Any,
    description: str,
    prefs: dict[str, Any] | None = None,
) -> tuple[Any | None, list[dict[str, Any]]]:
    """定位最佳元素并返回经验库查询结果（供 click 兜底复用，避免重复 DB 查询）。

    定位优先级:
    1. 经验库可靠记录(click_count >= 2)的 resource_id → 直接在 understanding 中验证
    2. 语义搜索 + 历史身份加分
    3. 返回 None，由 click 函数用百分比 bounds 兜底

    Returns:
        (best_element, known_ids): best_element 为 None 时由调用方处理兜底
    """
    # 无论 understanding 是否为 None，都先查经验库（兜底需要 known_ids）
    known_ids = _query_known_identities(description)

    if understanding is None:
        return None, known_ids

    all_elements = list(understanding.primary_paths) + [
        e for e in understanding.elements if e not in understanding.primary_paths
    ]

    # ── 快速路径: 经验库 click_count >= 2 的 resource_id ──
    for known in known_ids:
        if known.get("click_count", 0) >= 2:
            rid = known.get("resource_id", "")
            if rid:
                for el in all_elements:
                    if getattr(el, "resource_id", "") == rid and el.clickable:
                        # 若当前 query 命中了 RAG 偏好，避免容器 rid 抢占。
                        if _prefs_active_for_description(prefs, description):
                            if _pref_bonus_for_element(el, prefs, description) <= 0:
                                continue
                        logger.info(
                            "Experience hit: rid=%s (click_count=%d)",
                            rid,
                            known["click_count"],
                        )
                        return el, known_ids

    # ── 正常路径: 语义搜索 + 历史身份加分 ──
    best: tuple[int, int, Any] | None = None
    ranked = _rank_click_candidates(understanding, description, known_ids, prefs)
    scanned = len([e for e in all_elements if getattr(e, "clickable", False)])
    if ranked:
        best = ranked[0]

    if best is not None:
        best_el = best[2]
        child = _disambiguate_container(best_el, all_elements, prefs, description)
        if child is not None:
            best = (best[0], best[1], child)
            best_el = child
        logger.info(
            "_find_best: desc=%r best_score=%d role=%s label=%r (scanned %d clickable)",
            description,
            best[0],
            getattr(best_el, "role", ""),
            getattr(best_el, "label", ""),
            scanned,
        )
    else:
        logger.info(
            "_find_best: desc=%r NO match (scanned %d clickable, words=%s)",
            description,
            scanned,
            _expand_zh_keywords(
                [w for w in re.split(r"\s+", description.lower().strip()) if w]
                or [description.lower().strip()]
            ),
        )
    return (best[2] if best else None), known_ids


def _promote_to_clickable_parent(best_el: Any, understanding: Any) -> Any | None:
    """当命中的元素本身不可点击时，提升到包含它的最小可点击父容器。

    通用机制：不依赖特定 label 模式（如时长标签），适用于所有
    "文本/图标覆盖在可点击容器上"的场景。

    匹配策略：
    1. best_el 本身 clickable → 无需提升，返回 None
    2. 在所有元素中查找 bounds 包含 best_el 的可点击元素
    3. 选面积最小的那个（最贴近的父容器）
    """
    if best_el is None or understanding is None:
        return None

    # 本身可点击 → 无需提升
    if getattr(best_el, "clickable", False):
        return None

    bb = getattr(best_el, "bounds", None)
    if not bb or len(bb) != 4:
        return None

    all_elements = list(understanding.primary_paths) + [
        e for e in understanding.elements if e not in understanding.primary_paths
    ]

    def _contains(parent_bb: tuple, child_bb: tuple) -> bool:
        return (
            parent_bb[0] <= child_bb[0]
            and parent_bb[1] <= child_bb[1]
            and parent_bb[2] >= child_bb[2]
            and parent_bb[3] >= child_bb[3]
        )

    def _area(bounds: tuple) -> int:
        return max(0, bounds[2] - bounds[0]) * max(0, bounds[3] - bounds[1])

    best_parent = None
    best_area = float("inf")

    for el in all_elements:
        if el is best_el or not getattr(el, "clickable", False):
            continue
        el_bb = getattr(el, "bounds", None)
        if not el_bb or len(el_bb) != 4 or el_bb == bb:
            continue
        if not _contains(el_bb, bb):
            continue
        area = _area(el_bb)
        if area < best_area:
            best_area = area
            best_parent = el

    if best_parent is not None:
        logger.info(
            "promote: non-clickable -> clickable parent %r -> %r (rid=%s)",
            getattr(best_el, "label", ""),
            getattr(best_parent, "label", ""),
            getattr(best_parent, "resource_id", ""),
        )
    return best_parent


def _query_known_identities(description: str) -> list[dict[str, Any]]:
    """查询 SQLite 中已确认的元素身份（设备无关属性: rid/role/region + bounds）。
    通过 ToolContext 获取已有的 DB 实例, 不重复创建连接。
    屏幕尺寸使用 ToolContext 缓存，避免每次 snapshot() 的性能开销。
    """
    try:
        ctx = get_tool_context()
        if not ctx or not ctx.device:
            return []
        # 优先从 ToolContext 取已有的 DB 实例
        db = getattr(ctx, "relational_db", None)
        if db is None:
            from data import create_relational_db
            from config import TestConfig

            cfg = TestConfig()
            db = create_relational_db(cfg)
        package = ctx.device.current_app().get("package", "")
        # 使用缓存的屏幕尺寸（设备分辨率运行期间不变）
        screen_w, screen_h = ctx.screen_size
        return (
            db.query_element_identity(
                package, description, target_screen=(screen_w, screen_h)
            )
            or []
        )
    except Exception:
        return []


def _score_known_identity(el: Any, known_ids: list[dict[str, Any]]) -> int:
    """已知身份加分: resource_id 匹配 +5, role 匹配 +3, region 匹配 +2。
    只用设备无关属性加分, 不用 bounds。
    """
    bonus = 0
    rid = (getattr(el, "resource_id", "") or "").lower()
    role = (getattr(el, "role", "") or "").lower()
    region = (getattr(el, "region", "") or "").lower()
    for known in known_ids:
        k_rid = (known.get("resource_id", "") or "").lower()
        k_role = (known.get("role", "") or "").lower()
        k_region = (known.get("region", "") or "").lower()
        if k_rid and rid == k_rid:
            bonus += 5
        if k_role and role == k_role:
            bonus += 3
        if k_region and region == k_region:
            bonus += 2
    return bonus


# ═══════════════════════════════════════════
#  元素身份自动记忆（SQLite，LLM 无感）
# ═══════════════════════════════════════════

_session_click_ids: set[str] = set()  # 同一次执行中已记录的 alias，用于 session 去重
_page_transition_seen: set[str] = (
    set()
)  # 同一次执行中已记录的 (page, action, to_page)，避免重复写入


def reset_session_click_ids() -> None:
    """重置 session 去重集合。每次测试执行开始时调用。"""
    _session_click_ids.clear()
    _page_transition_seen.clear()


def _compute_page_signature(understanding: Any) -> str:
    """计算页面签名: Activity + 可点击元素指纹。

    同页面不同内容 → 指纹稳定（按钮标签不变）
    同 Activity 不同 Fragment → 指纹不同（按钮集不同）
    """
    act = (understanding.activity or "").split(".")[-1] or "Unknown"
    clickable = [e for e in understanding.elements if e.clickable]
    stable = [
        e
        for e in clickable
        if e.label
        and len(e.label) > 1
        and len(e.label) < 10
        and not e.label.isdigit()
        and not e.label.startswith("--")
    ]
    # 按 label 排序，取前 5 个最稳定的
    stable.sort(key=lambda e: len(e.label or ""))
    fingerprint = [e.label for e in stable[:5]]
    return f"{act}「{','.join(fingerprint)}」" if fingerprint else act


def _query_known_by_rid(resource_id: str) -> list[dict[str, Any]]:
    """反向查询: 通过 resource_id 查找历史身份。

    用于场景: click("左下角应用按钮") → 语义搜索找到 el, rid=taskbar_view
    → 反查 SQLite 发现 alias='应用列表' click_count=3 → 加分
    """
    if not resource_id:
        return []
    try:
        ctx = get_tool_context()
        if not ctx or not ctx.device:
            return []
        db = getattr(ctx, "relational_db", None)
        if db is None:
            from data import create_relational_db
            from config import TestConfig

            db = create_relational_db(TestConfig())
        package = ctx.device.current_app().get("package", "")
        rows = db.select(
            "element_identities",
            {
                "app_package": package,
                "resource_id": resource_id,
            },
            order_by="click_count DESC",
            limit=3,
        )
        return [dict(r) for r in rows]
    except Exception:
        return []


def _save_click_identity(
    ctx: Any, label: str, best_el: Any, understanding: Any
) -> None:
    """click 成功后自动保存元素身份到 SQLite。

    session 去重: 同一次执行中同一个 alias 的 click_count 只 +1。
    """
    try:
        if not ctx or not ctx.device or not best_el:
            return
        db = getattr(ctx, "relational_db", None)
        if db is None:
            return
        # Session 去重
        alias_key = f"{ctx.device.current_app().get('package', '')}:{label}"
        if alias_key in _session_click_ids:
            return
        _session_click_ids.add(alias_key)

        page_sig = _compute_page_signature(understanding) if understanding else ""
        resource_id = getattr(best_el, "resource_id", "") or ""
        bounds = getattr(best_el, "bounds", (0, 0, 0, 0))
        bounds_json = ""
        if bounds and bounds != (0, 0, 0, 0):
            bounds_json = json.dumps(
                {"x1": bounds[0], "y1": bounds[1], "x2": bounds[2], "y2": bounds[3]}
            )
        screen_w, screen_h = ctx.screen_size
        db.save_element_identity(
            app_package=ctx.device.current_app().get("package", ""),
            page_signature=page_sig,
            alias=label,
            resource_id=resource_id,
            class_name=getattr(best_el, "class_name", "") or "",
            role=getattr(best_el, "role", "") or "",
            region=getattr(best_el, "region", "") or "",
            text_hint=getattr(best_el, "text", "") or "",
            bounds_json=bounds_json,
            screen_width=screen_w,
            screen_height=screen_h,
        )
        logger.info(
            "Saved identity: alias=%r rid=%s page_sig=%s", label, resource_id, page_sig
        )
    except Exception as exc:
        logger.debug("_save_click_identity failed: %s", exc)


@tool
def query_app_knowledge(query: str, app_package: str = "") -> str:
    """Query operation experience and curated rules for the given app."""
    ctx = get_tool_context()
    if not ctx.knowledge_base:
        return "未启用知识库"
    package = app_package or ctx.device.current_app().get("package", "")

    exp_results = ctx.knowledge_base.query(
        query, app_package=package, knowledge_type="experience", top_k=5
    )
    rule_text = ctx.knowledge_base.query_curated_rules(package, top_k=3)

    parts = []
    if exp_results:
        parts.append("## 操作经验")
        parts.extend(f"- {r['content']}" for r in exp_results)
    if rule_text:
        parts.append("## 人工知识")
        parts.append(rule_text)

    return "\n".join(parts) if parts else f"未找到 '{query}' 的相关知识"


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
    # P6.3: 延迟导入避免循环依赖 (tools -> graph -> tools)
    db = None
    try:
        from agents.graph import _relational_db as _gdb

        db = _gdb
    except ImportError:
        pass
    if db is None:
        from data import create_relational_db
        from config import TestConfig

        db = create_relational_db(TestConfig())

    try:
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


def _is_target_consistent(candidate, original_label: str, click_prefs: dict) -> bool:
    """回退候选必须与原始目标语义一致，防止乱点到无关元素。"""
    cand_label = (candidate.label or "").strip()
    target_words = [w.strip() for w in original_label.split(" ") if w.strip()]
    if any(w in cand_label for w in target_words if len(w) >= 2):
        return True
    if click_prefs:
        for kw in click_prefs.get("label_contains", []) or []:
            if kw in cand_label:
                return True
    return False


def _is_expected_destination(
    ctx: Any, post_page_id: str, pre_page_id: str, original_label: str
) -> bool:
    """页面变化后检查是否到达预期目标（后置特征检测，非关键词泛匹配）。"""
    if not post_page_id or not pre_page_id or post_page_id == pre_page_id:
        return False
    try:
        u = ctx.perceiver.perceive() if ctx.perceiver else None
        if u is None:
            return True
        labels_lower = [(e.label or "").strip().lower() for e in (u.elements or [])]
        t = original_label.strip().lower()
        # 全部应用/应用列表 → 必须出现搜索框（rid=search_input_all_apps）或应用网格
        if any(k in t for k in ("应用列表", "全部应用", "所有应用")):
            return any(
                "search_input_all_apps" in (getattr(e, "resource_id", "") or "").lower()
                for e in (u.elements or [])
            )
        return True
    except Exception:
        return True


def _rid_matches(actual_rid: str, expected_rid: str) -> bool:
    actual = _normalize_text(actual_rid)
    expected = _normalize_text(expected_rid)
    if not actual or not expected:
        return False
    if actual == expected:
        return True
    return actual.endswith("/" + expected) or actual.split("/")[-1] == expected


def _exact_clickable_candidates(
    understanding: Any,
    *,
    rid: str = "",
    class_name: str = "",
    path_contains: str = "",
    index: int = -1,
) -> tuple[list[Any], str]:
    if understanding is None:
        return [], "ERROR: 页面信息不可用，无法执行精确点击"
    clickables = [
        e
        for e in (understanding.elements or [])
        if e.clickable and (e.label or "").strip()
    ]
    if index >= 0:
        if index >= len(clickables):
            return (
                [],
                f"ERROR: index={index} 越界（当前可点击元素 {len(clickables)} 个）",
            )
        candidates = [clickables[index]]
    else:
        candidates = list(clickables)

    rid_filter = (rid or "").strip()
    cls_filter = _normalize_text(class_name).split(".")[-1]
    path_filter = _normalize_text(path_contains)

    if rid_filter:
        candidates = [
            e
            for e in candidates
            if _rid_matches(getattr(e, "resource_id", "") or "", rid_filter)
        ]
    if cls_filter:
        candidates = [
            e
            for e in candidates
            if _normalize_text(getattr(e, "class_name", "")).split(".")[-1]
            == cls_filter
        ]
    if path_filter:
        candidates = [
            e
            for e in candidates
            if path_filter in _normalize_text(getattr(e, "context_path", ""))
        ]

    if not candidates:
        return [], "ERROR: 未找到匹配元素，请调整 rid/class_name/path_contains/index"
    if len(candidates) > 1:
        labels = []
        for e in candidates[:6]:
            name = (getattr(e, "label", "") or "").strip() or "?"
            labels.append(name)
        return (
            [],
            f"ERROR: {len(candidates)} 个候选匹配（{'/'.join(labels)}），请追加 path_contains 或 rid 缩小范围",
        )
    return candidates, ""


def _extract_curated_rule_label(content: str) -> str:
    text = str(content or "")
    m = re.search(r'点击[“"\']([^”"\']+)[”"\']', text)
    if m:
        return (m.group(1) or "").strip()
    return ""


def _maybe_promote_exact_rule(
    ctx: Any,
    *,
    label: str,
    pre_page: str,
    matched_el: Any,
) -> None:
    kb = getattr(ctx, "knowledge_base", None)
    if not kb or matched_el is None:
        return
    app_pkg = (ctx.device.current_app() or {}).get("package", "")
    if not app_pkg:
        return

    rid = getattr(matched_el, "resource_id", "") or ""
    cls = _normalize_text(getattr(matched_el, "class_name", "")).split(".")[-1]
    path = getattr(matched_el, "context_path", "") or ""
    post_page = _capture_page_id(ctx)
    action = f'click_exact("{label}")'
    if rid:
        action += f" rid={rid}"
    if cls:
        action += f" class={cls}"
    if path:
        action += f" path={path}"

    kb.save_experience(
        app_package=app_pkg,
        page=pre_page or "",
        action=action,
        to_page=post_page or "",
        outcome="成功",
        detail="exact_click",
        signal_type="exact_click",
        quality_score=1.0,
        action_semantic=action,
        page_stability="stable",
    )
    evidence = 0
    try:
        exp_rows = kb.backend.get_by_metadata(
            {"app_package": app_pkg, "knowledge_type": "experience"},
            limit=100,
        )
        for row in exp_rows:
            meta = row.get("metadata", {}) or {}
            if str(meta.get("action", "") or "") != action:
                continue
            evidence = max(evidence, int(meta.get("success_count", 1) or 1))
    except Exception:
        logger.debug("read exact experience counters failed", exc_info=True)
    if evidence < 3:
        return

    page_name = (pre_page or "").split("「")[0] or "当前页面"
    rule = f"在{page_name}点击“{label}”时，优先匹配"
    rule_parts = []
    if cls:
        rule_parts.append(f"class={cls}")
    if path:
        rule_parts.append(f"path={path}")
    if rid:
        rule_parts.append(f"rid={rid.split('/')[-1]}")
    if not rule_parts:
        return
    rule += " 且 ".join(rule_parts)
    # 冲突检测：若同 label 存在人工 curated（非 auto_exact_click）且内容不同，不自动覆盖
    try:
        curated = kb.backend.get_by_metadata(
            {"app_package": app_pkg, "knowledge_type": "curated_rule"},
            limit=50,
        )
        for row in curated:
            meta = row.get("metadata", {}) or {}
            content = str(row.get("content", "") or "")
            scenario = str(meta.get("scenario", "") or "")
            if scenario == "auto_exact_click":
                continue
            manual_label = _extract_curated_rule_label(content)
            if manual_label and manual_label == label and content != rule:
                logger.warning(
                    "Skip auto curated due to manual conflict: label=%r manual=%r auto=%r",
                    label,
                    content[:80],
                    rule[:80],
                )
                return
    except Exception:
        logger.debug("curated conflict check failed", exc_info=True)
    existing = kb.query_curated_rules(app_pkg, top_k=20)
    if rule in existing:
        return
    kb.save_curated_rule(
        app_package=app_pkg,
        content=rule,
        scope="app",
        scenario="auto_exact_click",
        quality_score=0.8,
    )


@tool
def click(
    label: str,
    alternatives: str = "",
    rid: str = "",
    class_name: str = "",
    path_contains: str = "",
    index: int = -1,
) -> str:
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

    # 记录点击前页面标题（用于操作后状态对比）
    _pre_page = _capture_page_id(ctx)
    _pre_title = _pre_page  # 保存给 _post_click_snapshot 用

    # 收集所有待搜索的目标（label + alternatives 逗号分隔）
    alt_list = [a.strip() for a in (alternatives or "").split(",") if a.strip()]
    search_targets = [label] + alt_list
    exact_mode = bool(
        (rid or "").strip()
        or (class_name or "").strip()
        or (path_contains or "").strip()
        or index >= 0
    )
    click_prefs = {}
    try:
        click_prefs = dict(getattr(ctx, "_click_preferences", {}) or {})
    except Exception:
        click_prefs = {}

    # 语义搜索/精确搜索 → 最佳元素（内部已查询经验库，复用 known_ids）
    best_el, known_ids, matched_label = None, [], label
    ranked_candidates: list[tuple[int, int, Any]] = []
    if ctx.perceiver is not None:
        try:
            understanding = ctx.perceiver.perceive()
            # 更新 _pre_title 为更精确的页面标识
            act = (
                understanding.activity.split(".")[-1] if understanding.activity else "?"
            )
            _pre_title = (
                act + "「" + (understanding.page_title or "") + "」"
                if understanding.page_title
                else act
            )
        except Exception as exc:
            logger.warning("click: perceive failed | %s", exc)
            understanding = None

        if exact_mode:
            exact_candidates, err = _exact_clickable_candidates(
                understanding,
                rid=rid,
                class_name=class_name,
                path_contains=path_contains,
                index=index,
            )
            if err:
                return err
            best_el = exact_candidates[0]
            matched_label = label
            logger.info(
                "click exact: label=%r rid=%r class=%r path=%r index=%s -> %r",
                label,
                rid,
                class_name,
                path_contains,
                index,
                getattr(best_el, "label", ""),
            )
        else:
            for target in search_targets:
                try:
                    best_el, known_ids = _find_best_element_with_known(
                        understanding, target, click_prefs
                    )
                    if best_el is not None:
                        matched_label = target
                        ranked_candidates = _rank_click_candidates(
                            understanding, target, known_ids, click_prefs
                        )
                        logger.info(
                            "click: hit target=%r (primary=%r) role=%s label=%r",
                            target,
                            label,
                            getattr(best_el, "role", ""),
                            getattr(best_el, "label", ""),
                        )
                        break
                    logger.info("click: miss target=%r (alt for %r)", target, label)
                except Exception as exc:
                    logger.warning("click: search failed for %r | %s", target, exc)
    else:
        understanding = None

    def _is_container_like(el: Any) -> bool:
        cls = _normalize_text(getattr(el, "class_name", "")).split(".")[-1]
        role = _normalize_text(getattr(el, "role", ""))
        return cls in ("framelayout", "viewgroup", "linearlayout") or role in (
            "container",
            "list_entry",
        )

    def _should_skip_rid_fastpath(el: Any, description: str) -> bool:
        if not _prefs_active_for_description(click_prefs, description):
            return False
        rid = _normalize_text(getattr(el, "resource_id", ""))
        cls = _normalize_text(getattr(el, "class_name", "")).split(".")[-1]
        label_text = _normalize_text(getattr(el, "label", ""))
        if "taskbar_view" in rid and cls == "framelayout":
            return True
        if cls == "framelayout" and any(
            t in label_text for t in (click_prefs.get("label_contains") or [])
        ):
            return True
        return False

    def _perform_click_on_element(el: Any, desc: str) -> tuple[bool, str]:
        role = getattr(el, "role", "")
        rid = getattr(el, "resource_id", "") or ""
        rid_is_unique = False
        if rid and understanding is not None:
            rid_count = sum(
                1
                for e in (understanding.elements or [])
                if (e.resource_id or "") == rid
            )
            rid_is_unique = rid_count <= 1
        if role in ("switch", "switch_row"):
            ctx.device.click_bounds(el.bounds)
            time.sleep(1.0)
            new_checked = _check_switch_state(ctx, el)
            if new_checked is not None:
                state_cn = "开启" if new_checked else "关闭"
                return (
                    True,
                    _format_click_log(desc, el, strategy="bounds")
                    + f" | 开关状态: {state_cn}",
                )
            return True, _format_click_log(desc, el, strategy="bounds")
        if (
            rid_is_unique
            and rid
            and (exact_mode or (not _should_skip_rid_fastpath(el, desc)))
            and ctx.device.click_resource_id(rid)
        ):
            return True, _format_click_log(desc, el, strategy="resource_id")
        if getattr(el, "text", "") and ctx.device.click_text(el.text):
            return True, _format_click_log(desc, el, strategy="text")
        if getattr(el, "bounds", (0, 0, 0, 0)) != (0, 0, 0, 0):
            ctx.device.click_bounds(el.bounds)
            return True, _format_click_log(desc, el, strategy="bounds")
        return False, ""

    def _build_click_context(
        strategy: str, element: Any | None = None
    ) -> dict[str, Any]:
        return {
            "exact_mode": exact_mode,
            "index": index,
            "rid": (rid or "").strip() or (getattr(element, "resource_id", "") or ""),
            "class_name": (class_name or "").strip()
            or _normalize_text(getattr(element, "class_name", "")).split(".")[-1],
            "path_contains": (path_contains or "").strip()
            or (getattr(element, "context_path", "") or ""),
            "strategy": strategy,
        }

    def _with_snapshot(base_result: str, clicked_el: Any) -> str:
        """为成功的点击结果追加操作后页面状态。"""
        _save_click_identity(ctx, label, clicked_el, understanding)
        snap = _post_click_snapshot(ctx, _pre_title, label)
        return f"{base_result} | {snap}" if snap else base_result

    if best_el is not None:
        promoted = _promote_to_clickable_parent(best_el, understanding)
        if promoted is not None:
            best_el = promoted

        ok, result = _perform_click_on_element(best_el, matched_label)
        if ok:
            strategy = re.search(r"strategy=([A-Za-z0-9_-]+)", result or "")
            _record_page_transition(
                ctx,
                _pre_page,
                label,
                click_context=_build_click_context(
                    strategy.group(1) if strategy else "", best_el
                ),
            )
            if exact_mode:
                _maybe_promote_exact_rule(
                    ctx,
                    label=label,
                    pre_page=_pre_page,
                    matched_el=best_el,
                )
                return _with_snapshot(result, best_el)
            post_page = _capture_page_id(ctx)
            if (
                post_page
                and _pre_page
                and post_page == _pre_page
                and _is_container_like(best_el)
                and len(ranked_candidates) >= 2
            ):
                for _, _, candidate in ranked_candidates[1:4]:
                    if candidate is best_el:
                        continue
                    # 硬门槛：回退候选必须与原始目标语义一致
                    if not _is_target_consistent(candidate, matched_label, click_prefs):
                        continue
                    ok2, result2 = _perform_click_on_element(candidate, matched_label)
                    if not ok2:
                        continue
                    strategy2 = re.search(r"strategy=([A-Za-z0-9_-]+)", result2 or "")
                    _record_page_transition(
                        ctx,
                        _pre_page,
                        label,
                        click_context=_build_click_context(
                            strategy2.group(1) if strategy2 else "", candidate
                        ),
                    )
                    post_page2 = _capture_page_id(ctx)
                    # 成功条件：RAG prefs 命中时走严格后置特征检查，否则页面变化即成功
                    if click_prefs:
                        if not _is_expected_destination(
                            ctx, post_page2, _pre_page, label
                        ):
                            continue
                    elif not post_page2 or post_page2 == _pre_page:
                        continue
                    return _with_snapshot(
                        result2 + " | fallback=next_candidate", candidate
                    )
            return _with_snapshot(result, best_el)

    # 兆底：未找到语义匹配，回退到原始文本/资源点击
    if ctx.device.click_text(label):
        _save_click_identity(ctx, label, None, understanding)
        _record_page_transition(
            ctx,
            _pre_page,
            label,
            click_context=_build_click_context("text-fallback", None),
        )
        return _with_snapshot(f"已点击: {label} (strategy=text-fallback)", None)
    # 历史身份兜底
    if not known_ids:
        known_ids = _query_known_identities(label)
    for known in known_ids:
        rid = known.get("resource_id", "")
        if rid and ctx.device.click_resource_id(rid):
            _save_click_identity(ctx, label, None, understanding)
            _record_page_transition(
                ctx,
                _pre_page,
                label,
                click_context=_build_click_context("known-rid-fallback", None),
            )
            return _with_snapshot(
                f"已点击历史资源: {label} rid={rid} (strategy=known-rid-fallback)",
                None,
            )
    # 百分比 bounds 兜底
    for known in known_ids:
        converted = known.get("bounds_converted")
        confidence = known.get("bounds_confidence", "low")
        if confidence == "low":
            continue
        if converted and all(k in converted for k in ("x1", "y1", "x2", "y2")):
            bounds = (
                converted["x1"],
                converted["y1"],
                converted["x2"],
                converted["y2"],
            )
            screen_w, screen_h = ctx.screen_size
            if (
                screen_w > 0
                and screen_h > 0
                and 0 <= bounds[0] < screen_w
                and 0 <= bounds[1] < screen_h
                and bounds[2] <= screen_w
                and bounds[3] <= screen_h
            ):
                ctx.device.click_bounds(bounds)
                _save_click_identity(ctx, label, None, understanding)
                _clicked = True
                _record_page_transition(
                    ctx,
                    _pre_page,
                    label,
                    click_context=_build_click_context("pct-bounds-fallback", None),
                )
                return _with_snapshot(
                    f"已点击历史坐标: {label} "
                    f"bounds=({bounds[0]},{bounds[1]},{bounds[2]},{bounds[3]}) "
                    f"(strategy=pct-bounds-fallback)",
                    None,
                )
    if ctx.device.click_resource_id(label):
        _save_click_identity(ctx, label, None, understanding)
        _record_page_transition(
            ctx,
            _pre_page,
            label,
            click_context=_build_click_context("rid-fallback", None),
        )
        return _with_snapshot(f"已点击资源: {label} (strategy=rid-fallback)", None)
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
    """生成点击完成后的语义化记录（供知识库沉淀）。不输出原始坐标。
    当搜索词与实际元素名不匹配时（CJK 模糊匹配），标记 WARNING。"""
    el_label = (el.label or "").strip()
    q = query.strip().lower()
    # 检测模糊匹配：搜索词 != 实际标签，或者嵌在长标签里 (如 "时区" 嵌在 "自动确定时区")
    mismatch = (
        el_label
        and q != el_label.lower()
        and (q not in el_label or len(q) < len(el_label) * 0.5)
    )
    prefix = "已点击(WARNING: 模糊匹配): " if mismatch else "已点击: "
    parts = [f"{prefix}{query}"]
    if strategy:
        parts.append(f"strategy={strategy}")
    if mismatch:
        parts.append(f"actual_label='{el_label}'")
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


# 已知挥发性 label 模式：时间、日期、电量、通知、热词等动态内容
_VOLATILE_LABEL_PATTERNS = [
    re.compile(r"^\d{1,2}:\d{2}$"),  # 时间
    re.compile(r"^\d{1,2}月\d{1,2}日"),  # 日期
    re.compile(r"^(周一|周二|周三|周四|周五|周六|周日)$"),  # 星期
    re.compile(r"^\d+%$"),  # 电量百分比
    re.compile(r"^(正在|已).*(充电|USB)"),  # 充电状态
    re.compile(r"^\d+ (分钟|小时|天)前$"),  # 相对时间
]


def _is_volatile_label(label: str) -> bool:
    """判断 label 是否为挥发性动态内容。"""
    s = (label or "").strip()
    if len(s) >= 12:
        return False  # 长文本通常不是动态的
    return any(p.search(s) for p in _VOLATILE_LABEL_PATTERNS)


def _capture_page_id(ctx: Any) -> str:
    """捕获当前页面身份标识（activity + 页面标题 + 稳定可见元素签名）。
    过滤挥发性 label（时间、电量等），避免假页面变化干扰回退判定。"""
    try:
        app = ctx.device.current_app()
        act = (app.get("activity", "") or "").split(".")[-1]
        title = ""
        vis_hash = ""
        if ctx.perceiver:
            understanding = ctx.perceiver.perceive()
            title = getattr(understanding, "page_title", "") or ""
            labels = sorted(
                (e.label or "").strip().lower()
                for e in (understanding.elements or [])
                if getattr(e, "clickable", False)
                and (e.label or "").strip()
                and not _is_volatile_label(e.label)
            )
            if labels:
                vis_hash = hashlib.md5(
                    "|".join(labels[:80]).encode("utf-8")
                ).hexdigest()[:12]
        base = f"{act}「{title}」" if title else act
        return f"{base}#{vis_hash}" if vis_hash else base
    except Exception:
        return ""


def _post_click_snapshot(ctx: Any, pre_title: str, label: str) -> str:
    """点击后快速感知页面变化，返回简洁状态变化描述。"""
    if ctx.perceiver is None:
        return ""
    try:
        # 短暂等待让 UI 刷新完成
        time.sleep(0.3)
        u = ctx.perceiver.perceive()
        act = u.activity.split(".")[-1] if u.activity else "?"
        post_title = act + "「" + (u.page_title or "") + "」" if u.page_title else act
        lines = [f"操作后页面: {post_title}"]
        if pre_title and post_title != pre_title:
            lines.append(f"页面变化: {pre_title} → {post_title}")
        # 收集可点击元素 + 含数字的叶子节点（计算器显示、设置值等）
        clickables = [e for e in u.elements if e.clickable and e.label]
        text_leaves = [
            e
            for e in u.elements
            if not e.clickable
            and e.text
            and any(c.isdigit() for c in (e.text or ""))
            and e.role not in ("container", "text")
            and len(e.text or "") < 20
        ]
        nearby = (clickables + text_leaves)[:15]
        value_hints = []
        for e in nearby:
            txt = e.text or ""
            if txt and any(c.isdigit() for c in txt) and len(txt) < 20:
                value_hints.append(f"{e.label or ''}:{txt}".strip(":"))
        if value_hints:
            lines.append(f"关键值: {', '.join(value_hints[:5])}")
        return " | ".join(lines)
    except Exception:
        return ""


def _record_page_transition(
    ctx: Any, pre_page: str, label: str, *, click_context: dict[str, Any] | None = None
) -> None:
    """记录页面流转到知识库（异步，失败不阻塞）。

    组合去重: 同一次执行中同一 (page, action, to_page) 只写入一次。
    """
    if not pre_page:
        return
    try:
        post_page = _capture_page_id(ctx)
        if post_page and post_page != pre_page:
            # 组合去重
            cc = dict(click_context or {})
            strategy = str(cc.get("strategy", "") or "")
            exact_mode = bool(cc.get("exact_mode", False))
            rid = str(cc.get("rid", "") or "")
            class_name = str(cc.get("class_name", "") or "")
            path_contains = str(cc.get("path_contains", "") or "")
            index = int(cc.get("index", -1) or -1)

            action_parts = [f"click(label={label})"]
            if class_name:
                action_parts.append(f"class={class_name}")
            if path_contains:
                action_parts.append(f"path={path_contains}")
            if rid:
                action_parts.append(f"rid={rid}")
            if index >= 0:
                action_parts.append(f"index={index}")
            action_semantic = ",".join(action_parts)
            action = f'click_exact("{label}")' if exact_mode else f"click({label})"
            if rid:
                action += f" rid={rid}"
            if class_name:
                action += f" class={class_name}"
            if path_contains:
                action += f" path={path_contains}"

            quality = 0.35
            if exact_mode:
                quality += 0.35
            if index >= 0:
                quality += 0.10
            if rid:
                quality += 0.25
            if path_contains and ">" in path_contains:
                quality += 0.20
            if class_name:
                quality += 0.10
            if "fallback" in strategy or "bounds" in strategy:
                quality -= 0.25
            signal_type = (
                "exact_click"
                if exact_mode
                else ("fallback_click" if "fallback" in strategy else "semantic_click")
            )
            page_stability = "stable"
            if re.search(
                r"\d+\.\d+\s*[KMG]?[Bb]/s|\d{1,2}:\d{2}(:\d{2})?|\d+%", pre_page
            ):
                page_stability = "volatile"
                quality -= 0.30
            quality = max(0.0, min(1.0, quality))
            if quality < 0.75:
                logger.info(
                    "Skip low-quality experience: label=%r quality=%.2f strategy=%s",
                    label,
                    quality,
                    strategy,
                )
                return
            combo_key = f"{pre_page}|{action}|{post_page}"
            if combo_key in _page_transition_seen:
                return
            _page_transition_seen.add(combo_key)

            kb = ctx.knowledge_base
            if kb:
                app_pkg = ctx.device.current_app().get("package", "")
                kb.save_experience(
                    app_package=app_pkg,
                    page=pre_page,
                    action=action,
                    to_page=post_page,
                    outcome="成功",
                    signal_type=signal_type,
                    quality_score=quality,
                    action_semantic=action_semantic,
                    page_stability=page_stability,
                )
                logger.info(
                    "KB page transition: %s → %s (click %r)", pre_page, post_page, label
                )
    except Exception as exc:
        logger.debug("Page transition recording skipped: %s", exc)


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
def scroll_find_and_click(label: str, max_swipes: int = 3, panel: str = "") -> str:
    """滑动查找并点击指定元素。最多滑动 max_swipes 次。
    panel: 可选 left_navigation / right_content，指定滑动哪个面板（不指定则全屏滑动）。"""
    ctx = get_tool_context()
    if ctx.device is None:
        return "ERROR: 未连接 Android 设备"

    pre_labels: set[str] = set()
    for attempt in range(max_swipes + 1):
        if ctx.perceiver is not None:
            try:
                understanding = ctx.perceiver.perceive()
                best_el, _ = _find_best_element_with_known(understanding, label)
                if best_el is not None:
                    _save_click_identity(ctx, label, best_el, understanding)
                    if best_el.text and ctx.device.click_text(best_el.text):
                        logger.info(
                            "scroll_find[%d]: semantic hit label=%r text=%r",
                            attempt,
                            label,
                            best_el.text,
                        )
                        return _format_click_log(
                            label, best_el, strategy="scroll-semantic-text"
                        )
                    if best_el.resource_id and ctx.device.click_resource_id(
                        best_el.resource_id
                    ):
                        logger.info(
                            "scroll_find[%d]: semantic hit label=%r rid=%r",
                            attempt,
                            label,
                            best_el.resource_id,
                        )
                        return _format_click_log(
                            label, best_el, strategy="scroll-semantic-rid"
                        )
                    ctx.device.click_bounds(best_el.bounds)
                    logger.info(
                        "scroll_find[%d]: semantic hit label=%r bounds=%s",
                        attempt,
                        label,
                        best_el.bounds,
                    )
                    return _format_click_log(
                        label, best_el, strategy="scroll-semantic-bounds"
                    )
                # 检测元素是否无变化（已到末尾）
                cur_labels = {
                    e.label for e in understanding.elements if e.label and e.clickable
                }
                if attempt > 0 and cur_labels == pre_labels:
                    return f"滑动后仍未找到: {label}（已到末尾，无新元素出现）"
                pre_labels = cur_labels
            except Exception as exc:
                logger.warning("scroll_find[%d]: perceive failed | %s", attempt, exc)

        if ctx.device.click_text(label, timeout=0.5):
            _save_click_identity(ctx, label, None, understanding)
            logger.info("scroll_find[%d]: text hit label=%r", attempt, label)
            return f"已找到并点击: {label}"

        if attempt < max_swipes:
            if panel:
                if hasattr(scroll_panel, "invoke"):
                    scroll_panel.invoke({"panel": panel, "direction": "down"})
                else:
                    ctx.device.swipe("up")
            else:
                ctx.device.swipe("up")

    logger.warning(
        "scroll_find: exhausted %d swipes, NOT found label=%r", max_swipes, label
    )
    return f"滑动后仍未找到: {label}"


@tool
def long_press(label: str, duration: float = 0.8) -> str:
    """长按指定元素。先搜索匹配 label 的元素，再对其执行长按。适用于拖拽、上下文菜单、删除等场景。
    duration: 长按持续时间（秒），默认 0.8。"""
    ctx = get_tool_context()
    if ctx.device is None:
        return "ERROR: 未连接 Android 设备"

    if ctx.perceiver is not None:
        understanding = ctx.perceiver.perceive()
        best_el, _ = _find_best_element_with_known(understanding, label)
        if best_el is not None:
            ctx.device.long_click_bounds(best_el.bounds, duration)
            _save_click_identity(ctx, label, best_el, understanding)
            msg = _format_click_log(label, best_el, strategy="long_press")
            return msg + f" | 长按 {duration}s"
        # 扩充搜索：含非 clickable 元素（历史记录行、文本标签等 clickable=false 但支持长按）
        label_lower = label.lower().strip()
        for el in understanding.elements:
            el_label = (el.label or "").lower()
            el_rid = (el.resource_id or "").lower()
            if (label_lower in el_label or label_lower in el_rid) and el.bounds != (
                0,
                0,
                0,
                0,
            ):
                ctx.device.long_click_bounds(el.bounds, duration)
                _save_click_identity(ctx, label, el, understanding)
                msg = _format_click_log(label, el, strategy="long_press_nonclickable")
                return msg + f" | 长按 {duration}s"
        logger.info("long_press: miss target=%r", label)

    # 兜底：用 resource_id 长按
    if ctx.device.device(resourceId=label).exists(timeout=2.0):
        ctx.device.device(resourceId=label).long_click()
        return f"已长按: {label}"
    return f"long_press: 未找到 '{label}'"


@tool
def copy() -> str:
    """复制当前选中内容到剪贴板。应在 long_press 触发系统弹窗后调用，自动点击弹窗中的"复制"按钮。"""
    ctx = get_tool_context()
    if ctx.device is None:
        return "ERROR: 未连接 Android 设备"
    strategy = ctx.device.copy()
    return f"已复制 (strategy={strategy})"


@tool
def paste() -> str:
    """粘贴剪贴板内容到当前焦点输入框。依次尝试 CTRL+V → KEYCODE_PASTE → 系统弹窗"粘贴"按钮。粘贴后应用 get_screen_info 验证内容是否已成功出现在输入框中。"""
    ctx = get_tool_context()
    if ctx.device is None:
        return "ERROR: 未连接 Android 设备"
    strategy = ctx.device.paste()
    msg = f"已粘贴 (strategy={strategy})"
    if strategy == "paste_ctrl_v":
        msg += ' | 请用 get_screen_info 确认内容已出现在输入框，如未出现则手动 long_press 输入框后 click("粘贴")'
    return msg


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
    """按系统键。key 可为 back / home / enter / recent / power（电源键，锁屏或亮屏）。"""
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
def open_notification() -> str:
    """打开通知栏。系统级操作：横屏时等效于从顶部左侧下滑，竖屏时从顶部下滑。"""
    ctx = get_tool_context()
    if ctx.device is None:
        return "ERROR: 未连接 Android 设备"
    ctx.device.open_notification()
    return "已打开通知栏"


@tool
def open_quick_settings() -> str:
    """打开快速设置面板（Quick Settings / 控制中心）。系统级操作：横屏时等效于从顶部右侧下滑，竖屏时从顶部下滑。"""
    ctx = get_tool_context()
    if ctx.device is None:
        return "ERROR: 未连接 Android 设备"
    ctx.device.open_quick_settings()
    return "已打开快速设置面板"


@tool
def unlock_screen() -> str:
    """解锁屏幕（swipe up 解锁，适用于无密码/图案的测试设备）。亮屏后若处于锁屏界面需调用此工具。"""
    ctx = get_tool_context()
    if ctx.device is None:
        return "ERROR: 未连接 Android 设备"
    ctx.device.unlock()
    return "已解锁屏幕"


@tool
def set_orientation(orientation: str = "portrait") -> str:
    """设置屏幕方向。orientation: portrait（竖屏）/ landscape（横屏）。"""
    ctx = get_tool_context()
    if ctx.device is None:
        return "ERROR: 未连接 Android 设备"
    mapping = {"portrait": "natural", "landscape": "left"}
    raw = mapping.get(orientation, orientation)
    ctx.device.set_orientation(raw)
    return f"已设置屏幕方向: {orientation}"


@tool
def toggle_auto_rotate(enable: bool = True) -> str:
    """启用或禁用屏幕自动旋转（等同于控制中心/Quick Settings 中的"自动旋转"开关）。
    enable=True 开启自动旋转，设备横屏时 App 随重力感应切换布局；
    enable=False 关闭自动旋转，设备方向锁定，App 不会随横屏切换。"""
    ctx = get_tool_context()
    if ctx.device is None:
        return "ERROR: 未连接 Android 设备"
    ctx.device.freeze_rotation(not enable)
    action = "开启" if enable else "关闭"
    return f"已{action}自动旋转"


@tool
def check_desktop_mode(mode: str = "dw") -> str:
    """检查当前是否处于指定桌面模式。mode 可选: dw（无限工作台/CustomModeLauncher，检查 zui_ov_desktop_mode==1）。
    返回当前模式和判断结果，供 Agent 在操作前确认环境状态。"""
    ctx = get_tool_context()
    if ctx.device is None:
        return "ERROR: 未连接 Android 设备"

    key_map = {"dw": "zui_ov_desktop_mode"}
    key = key_map.get(mode, mode)
    val = ctx.device.get_system_setting(key, "system")
    if mode == "dw":
        is_dw = val == "1"
        return f"桌面模式: {'无限工作台' if is_dw else '普通桌面'} (zui_ov_desktop_mode={val or '0'})"
    return f"{key}={val or '(未设置)'}"


@tool
def scroll_panel(panel: str = "left_navigation", direction: str = "down") -> str:
    """在特定面板内滚动（用于导航栏或内容区有折叠项时）。
    panel: left_navigation（左侧导航栏）/ right_content（右侧内容区）
    direction: down（往下翻, 露出下方内容）/ up（往上翻, 露出上方内容）
    """
    ctx = get_tool_context()
    if ctx.device is None:
        return "ERROR: 未连接 Android 设备"
    if ctx.perceiver is None:
        return "ERROR: Perceiver not available"

    understanding = ctx.perceiver.perceive()
    screen_w = understanding.width or 1080
    screen_h = understanding.height or 1920

    if understanding.layout == "two_pane":
        if panel == "left_navigation":
            x_center = int(screen_w * 0.22)
        else:
            x_center = int(screen_w * 0.72)
    else:
        x_center = screen_w // 2

    # 滚动前记录当前可见元素（检测是否已到末尾）
    pre_labels = {e.label for e in understanding.elements if e.label and e.clickable}

    # 方向语义：down=往下翻(露出下方内容)→手指从下往上划
    #           up=往上翻(露出上方内容)→手指从上往下划
    # 滑动距离 = 2/3 屏幕高度
    if direction == "down":
        y_from = int(screen_h * 0.85)
        y_to = int(screen_h * 0.15)
    else:
        y_from = int(screen_h * 0.15)
        y_to = int(screen_h * 0.85)

    try:
        ctx.device.device.swipe(x_center, y_from, x_center, y_to, duration=0.3)
    except Exception as exc:
        return f"ERROR: 滚动{panel}失败: {exc}"

    # 滚动后检查元素是否有变化
    time.sleep(0.5)  # 等 UI 刷新
    try:
        post = ctx.perceiver.perceive()
        post_labels = {e.label for e in post.elements if e.label and e.clickable}
        new_count = len(post_labels - pre_labels)
        if new_count == 0:
            return f"已滚动{panel}: {direction}（已到末尾，无新元素）"
        return f"已滚动{panel}: {direction}（新增 {new_count} 个元素）"
    except Exception:
        return f"已滚动{panel}: {direction}"


@tool
def launch_app(
    package: str,
    activity: str = "",
) -> str:
    """启动指定包名的 App。"""
    ctx = get_tool_context()
    if ctx.device is None:
        return "ERROR: 未连接 Android 设备，无法启动应用"
    _pre_page = _capture_page_id(ctx)
    target_activity = (activity or "").strip()
    if target_activity:
        ctx.device.app_start(package, activity=target_activity)
    else:
        ctx.device.app_start(package)
    # 记录页面跳转
    _record_page_transition(ctx, _pre_page, f"launch_app({package})")
    return (
        f"已启动: {package}/{target_activity}"
        if target_activity
        else f"已启动: {package}"
    )


@tool
def visual_check(description: str) -> str:
    """基于截图进行视觉判断，返回结构化 JSON：decision/reason/evidence/confidence。"""
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


# ═══════════════════════════════════════════
#  Reviewer Agent 工具
# ═══════════════════════════════════════════


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
def assert_page_contains(text: str, pattern: bool = False) -> str:
    """断言当前页面包含指定文本或匹配正则模式。

    - pattern=False（默认）: 检查页面是否包含 text 子串
    - pattern=True: text 作为正则表达式匹配，例: text="\\\\d{2}/\\\\d{2}/\\\\d{4}" 匹配日期格式
      注意: 传入时需双反斜杠转义
    返回: PASS 或 FAIL: <原因>
    """
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
            return "PASS"
    return f"FAIL: 元素不存在 {label}"


# ═══════════════════════════════════════════
# ═══════════════════════════════════════════
#  验证工具
# ═══════════════════════════════════════════


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
    app_paths.SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    filename = name or f"screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    if not filename.endswith(".png"):
        filename += ".png"
    path = str(app_paths.SCREENSHOT_DIR / filename)
    ctx.device.screenshot().save(path)
    return path


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
    launch_app,
    visual_check,
    detect_overlay,
    detect_popup,
    dismiss_popup,
    wait_seconds,
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

"""元素匹配 / 打分 / 排序（legacy 启发式簇）。

从 tools/__init__.py 拆出（重构 T3），仅移动代码、不改逻辑。
本模块集中了「模糊/启发式」的候选打分与排序逻辑，便于后续 LLM-Native
迁移时整体下线（见 docs/llm_native_architecture_migration_20260709.md）。

注：`_rank_click_candidates` / `_find_best_element_with_known` 依赖
`_score_known_identity` / `_query_known_identities`（元素身份查询，仍在
tools/__init__.py 中），采用函数内延迟 import 以避免加载期循环依赖。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from tools.text_utils import (
    _cjk_char_overlap,
    _expand_zh_keywords,
    _has_cjk,
    _normalize_text,
)

logger = logging.getLogger(__name__)


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
    from tools import _score_known_identity  # 延迟 import 避免加载期循环依赖

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
    from tools import _query_known_identities  # 延迟 import 避免加载期循环依赖

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

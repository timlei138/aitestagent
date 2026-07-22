"""点击及其辅助（元素定位 / 点击执行 / 页面跳转 / 元素身份 / 长按等）。

从 tools/__init__.py 拆出（重构 T6），仅移动代码、不改逻辑。
element_match 会在运行期反向调用本模块的 _score_known_identity /
_query_known_identities（经 tools 命名空间的延迟 import），故无加载期循环依赖。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from typing import Any

from llm.safety import check_dangerous
from tools.context import ToolContext, get_tool_context
from tools.results import AMBIGUOUS, ERROR, NOT_FOUND, make_result, parse_status
from tools.text_utils import _has_cjk, _normalize_text
from tools.element_match import (
    _extract_click_preferences_from_rag,
    _find_best_element_with_known,
    _prefs_active_for_description,
    _promote_to_clickable_parent,
    _rank_click_candidates,
)

try:
    from langchain_core.tools import tool
except Exception:  # pragma: no cover

    def tool(func=None, *args, **kwargs):
        def wrapper(f):
            return f

        return wrapper(func) if func else wrapper


logger = logging.getLogger(__name__)

# R3: 精确定位「未找到」时的有限重试（等待页面/元素延迟出现后重感知再试）
_CLICK_LOCATE_RETRY_MAX = 2
_CLICK_LOCATE_RETRY_WAIT = 0.5


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
    label: str = "",
) -> tuple[list[Any], str]:
    if understanding is None:
        return [], "ERROR: 页面信息不可用，无法执行精确点击"
    clickables = [
        e
        for e in (understanding.elements or [])
        if e.clickable and (e.label or "").strip()
    ]
    rid_filter = (rid or "").strip()
    cls_filter = _normalize_text(class_name).split(".")[-1]
    path_filter = _normalize_text(path_contains)

    # 契约：index 与 page_info 的 [n] 一一对应。给了 index 就按全局位置直接选位——
    # 这是最直接、模型最常用的方式（clickables 的顺序与 page_info [n] 一致）。
    # 同名/同类兄弟（如弹窗多个图标）也各有独立 [n]，用各自的 index 即可精确区分。
    if index >= 0:
        if index >= len(clickables):
            return (
                [],
                f"ERROR: index={index} 越界（当前可点击元素 {len(clickables)} 个）",
            )
        return [clickables[index]], ""

    # 无 index：按稳定属性（rid/class/path）过滤
    candidates = list(clickables)
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
        return [], make_result(
            NOT_FOUND, "未找到匹配元素，请调整 rid/class_name/path_contains/index"
        )
    # 修复B：结构过滤命中多个时，若同时给了 label，则用 label 收窄——
    # 避免 path_contains 指向共享容器路径时把多个兄弟元素全算歧义。
    # 有 label 命中就取命中的；无命中则保持原候选（退回 AMBIGUOUS，不会更差）。
    if len(candidates) > 1 and (label or "").strip():
        _lab = _normalize_text(label)
        if _lab:
            narrowed = [
                e
                for e in candidates
                if _lab in _normalize_text(getattr(e, "label", "") or "")
                or _lab in _normalize_text(getattr(e, "associated_label", "") or "")
            ]
            if narrowed:
                candidates = narrowed
    if len(candidates) > 1:
        labels = []
        for e in candidates[:6]:
            name = (getattr(e, "label", "") or "").strip() or "?"
            labels.append(name)
        # 同名兄弟（label 全相同）无法靠 rid/path 区分 → 提示用 page_info 里各自的 [n]（index）。
        if len(set(labels)) == 1:
            hint = "这些候选 label 相同，请用 page_info 中它们各自的 [n] 作为 index 精确选中"
        else:
            hint = "请追加 path_contains 或 rid 缩小范围"
        return (
            [],
            make_result(
                AMBIGUOUS,
                f"{len(candidates)} 个候选匹配（{'/'.join(labels)}），{hint}",
            ),
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
    # NOTE(2026-07-10): 点击偏好已通过 save_experience 保存为操作经验，
    # _extract_click_preferences_from_rag 在 RAG 查询时自动提取。
    # 不再自动提升为 curated_rule —— 人工知识应仅由测试人员手动维护。


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
                label=label,
            )
            # R3: "未找到匹配元素" 可能是页面尚未稳定/元素延迟出现 → 等待+重感知+重试
            # （不重试 AMBIGUOUS「N 个候选」和 index 越界——那是参数问题，应透传给 LLM）
            _retry = 0
            while (
                err
                and parse_status(err) == NOT_FOUND
                and ctx.perceiver is not None
                and _retry < _CLICK_LOCATE_RETRY_MAX
            ):
                _retry += 1
                time.sleep(_CLICK_LOCATE_RETRY_WAIT)
                try:
                    understanding = ctx.perceiver.perceive()
                except Exception:
                    break
                exact_candidates, err = _exact_clickable_candidates(
                    understanding,
                    rid=rid,
                    class_name=class_name,
                    path_contains=path_contains,
                    index=index,
                    label=label,
                )
                if not err:
                    logger.info("click exact: 重试第 %d 次后命中", _retry)
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
            # L3 kill switch: native_strict 模式下无精确参数 → 直接返回 AMBIGUOUS，
            # 不进入语义搜索/fallback 兜底，强制 LLM 下发精确参数（index/rid/class/path）。
            if getattr(ctx, "click_mode", "legacy") == "native_strict":
                return make_result(
                    AMBIGUOUS,
                    "未提供精确参数（index/rid/class_name/path_contains），"
                    "请在 page_info 中选择后重试",
                )
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
        label_text = (getattr(el, "label", "") or "").lower()
        assoc_text = (getattr(el, "associated_label", "") or "").lower()
        desc_lower = (desc or "").lower().strip()
        raw_words = [w for w in re.split(r"\s+", desc_lower) if w]
        score_words = [w for w in raw_words if (len(w) > 1 or _has_cjk(w))]
        if not score_words and desc_lower:
            score_words = [desc_lower]
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
        if rid_is_unique and rid and (exact_mode or (not _should_skip_rid_fastpath(el, desc))):
            label_assoc_hit = any(
                w and (w in label_text or w in assoc_text) for w in score_words
            )
            # 语义匹配：label 或 associated_label 命中即放行（不做业务惩罚，不做 rid 子串）
            if label_assoc_hit:
                if ctx.device.click_resource_id(rid):
                    return True, _format_click_log(desc, el, strategy="resource_id")
            return (
                False,
                f"AMBIGUOUS: rid={rid} label={getattr(el, 'label', '')} "
                f"与目标 '{desc}' 不匹配，请用 index/class 精确定位",
            )
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

    def _make_click_success(
        message: str,
        resolved: dict[str, str],
        *,
        match_mode: str,
        fallback_used: bool,
        clicked_el: Any | None,
    ) -> str:
        """Return every successful click through the shared result contract.

        The message retains the legacy click log and post-click page facts for people,
        while the evidence block contains only stable, directly parseable facts.
        """
        from tools.perceive_tools import _permission_popup_buttons

        _save_click_identity(ctx, label, clicked_el, understanding)
        parts = [message]
        snap = _post_click_snapshot(ctx, _pre_title, label)
        if snap:
            parts.append(snap)

        evidence: dict[str, Any] = {
            "match_mode": match_mode,
            "fallback_used": fallback_used,
            "resolved_label": resolved.get("label", ""),
            "resolved_role": resolved.get("role", ""),
            "resolved_rid": resolved.get("rid", ""),
            "resolved_class": resolved.get("class_name", ""),
            "resolved_path": resolved.get("path", ""),
        }
        permission_info = _permission_popup_buttons(ctx)
        if permission_info:
            activity, controls = permission_info
            evidence.update(
                {
                    "permission_dialog": True,
                    "permission_activity": activity,
                    "permission_buttons": "|".join(text for text, _ in controls),
                }
            )
        return make_result("OK", " | ".join(parts), evidence)

    def _resolved_from_element(element: Any | None) -> dict[str, str]:
        if element is None:
            return {"label": label}
        return {
            "label": getattr(element, "label", "") or label,
            "role": getattr(element, "role", "") or "",
            "rid": getattr(element, "resource_id", "") or "",
            "class_name": getattr(element, "class_name", "") or "",
            "path": getattr(element, "context_path", "") or "",
        }

    if best_el is not None:
        promoted = _promote_to_clickable_parent(best_el, understanding)
        if promoted is not None:
            best_el = promoted

        ok, result = _perform_click_on_element(best_el, matched_label)
        if not ok and "AMBIGUOUS:" in (result or ""):
            return result  # 歧义直接透传给 LLM，不走 fallback
        if ok:
            strategy_match = re.search(r"strategy=([A-Za-z0-9_-]+)", result or "")
            match_mode = strategy_match.group(1) if strategy_match else "element"
            _record_page_transition(
                ctx,
                _pre_page,
                label,
                click_context=_build_click_context(match_mode, best_el),
            )
            if exact_mode:
                _maybe_promote_exact_rule(
                    ctx,
                    label=label,
                    pre_page=_pre_page,
                    matched_el=best_el,
                )
            return _make_click_success(
                result,
                _resolved_from_element(best_el),
                match_mode=match_mode,
                fallback_used=False,
                clicked_el=best_el,
            )
        # 不在工具内自动猜测次优候选；直接透传给 LLM 做下一步精确决策
        return result or make_result(
            ERROR, f"未能点击目标元素: {label}，请用 index/class/rid 精确定位"
        )

    # 兆底：未找到语义匹配，回退到原始文本/资源点击
    if ctx.device.click_text(label):
        _record_page_transition(
            ctx,
            _pre_page,
            label,
            click_context=_build_click_context("text-fallback", None),
        )
        return _make_click_success(
            f"已点击: {label} (strategy=text-fallback)",
            {"label": label},
            match_mode="text-fallback",
            fallback_used=True,
            clicked_el=None,
        )
    # 历史身份兜底
    if not known_ids:
        known_ids = _query_known_identities(label)
    for known in known_ids:
        known_rid = known.get("resource_id", "")
        if known_rid and ctx.device.click_resource_id(known_rid):
            _record_page_transition(
                ctx,
                _pre_page,
                label,
                click_context=_build_click_context("known-rid-fallback", None),
            )
            return _make_click_success(
                f"已点击历史资源: {label} rid={known_rid} "
                "(strategy=known-rid-fallback)",
                {"label": label, "rid": known_rid},
                match_mode="known-rid-fallback",
                fallback_used=True,
                clicked_el=None,
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
                _record_page_transition(
                    ctx,
                    _pre_page,
                    label,
                    click_context=_build_click_context("pct-bounds-fallback", None),
                )
                return _make_click_success(
                    f"已点击历史坐标: {label} "
                    f"bounds=({bounds[0]},{bounds[1]},{bounds[2]},{bounds[3]}) "
                    "(strategy=pct-bounds-fallback)",
                    {"label": label, "rid": known.get("resource_id", "")},
                    match_mode="pct-bounds-fallback",
                    fallback_used=True,
                    clicked_el=None,
                )
    if ctx.device.click_resource_id(label):
        _record_page_transition(
            ctx,
            _pre_page,
            label,
            click_context=_build_click_context("rid-fallback", None),
        )
        return _make_click_success(
            f"已点击资源: {label} (strategy=rid-fallback)",
            {"label": label, "rid": label},
            match_mode="rid-fallback",
            fallback_used=True,
            clicked_el=None,
        )
    return make_result(NOT_FOUND, f"未找到可点击元素: {label}")


def _check_switch_state(ctx: Any, target_el: Any) -> bool | None:
    """点击开关后重新解析 UI 树，查找目标元素的 checked 状态。

    优先读 Switch 子控件的原生 checked（若有），其次读目标元素自身的 checked。
    """
    try:
        if ctx.perceiver is None:
            return None
        # 清除缓存，强制重新解析
        ctx.perceiver._cache_sig = ""
        understanding = ctx.perceiver.perceive()
        target_rid = getattr(target_el, "resource_id", "") or ""
        target_bounds = getattr(target_el, "bounds", (0, 0, 0, 0))
        has_child = getattr(target_el, "has_switch_child", False)

        # 第一优先：目标有 Switch 子控件 → 直接读子的原生 checked
        if has_child:
            for el in understanding.elements:
                if el.role == "switch" and _bounds_overlap(target_bounds, el.bounds):
                    checked = getattr(el, "checked", None)
                    if checked is not None:
                        return checked

        # 第二优先：按 resource_id 或 bounds 匹配目标自身
        for el in understanding.elements:
            if target_rid and getattr(el, "resource_id", "") == target_rid:
                return getattr(el, "checked", None)
            if el.bounds == target_bounds and el.bounds != (0, 0, 0, 0):
                return getattr(el, "checked", None)
        return None
    except Exception:
        return None


def _bounds_overlap(a: tuple, b: tuple) -> bool:
    """b 的边界是否在 a 内部（含容差）。"""
    if len(a) < 4 or len(b) < 4:
        return False
    margin = 4
    return (
        b[0] >= a[0] - margin
        and b[1] >= a[1] - margin
        and b[2] <= a[2] + margin
        and b[3] <= a[3] + margin
    )


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
                and not (
                    # 内联 _is_volatile_label 逻辑
                    len(str(e.label or "").strip()) < 12
                    and any(
                        p.search(str(e.label or "").strip()) for p in _VOLATILE_LABEL_PATTERNS
                    )
                )
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
            # §5.2: 同页同按钮不重复写 experience。跨页导航才写入。
            pre_activity = (pre_page or "").split("「")[0].split("#")[0].strip()
            post_activity = (post_page or "").split("「")[0].split("#")[0].strip()
            if exact_mode and pre_activity == post_activity:
                return  # 同 activity 内的精确点击（如计算器按键）不写 experience

            combo_key = f"{pre_page}|{action}|{post_page}"
            if combo_key in _page_transition_seen:
                return
            _page_transition_seen.add(combo_key)

            kb = ctx.knowledge_base
            if kb:
                app_pkg = ctx.device.current_app().get("package", "")
                # §5.3: content 精简 — page 归一化去 hash，action 传语义化摘要
                rid_tail = (rid or "").split("/")[-1] if rid else ""
                norm_pre = re.sub(r"#\w{6,}", "", (pre_page or "").split("「")[0])
                norm_post = re.sub(r"#\w{6,}", "", (post_page or "").split("「")[0])
                kb.save_experience(
                    app_package=app_pkg,
                    page=norm_pre,
                    action=action,
                    to_page=norm_post,
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
                    return make_result(
                        NOT_FOUND, f"滑动后仍未找到: {label}（已到末尾，无新元素出现）"
                    )
                pre_labels = cur_labels
            except Exception as exc:
                logger.warning("scroll_find[%d]: perceive failed | %s", attempt, exc)

        if ctx.device.click_text(label, timeout=0.5):
            _save_click_identity(ctx, label, None, understanding)
            logger.info("scroll_find[%d]: text hit label=%r", attempt, label)
            return f"已找到并点击: {label}"

        if attempt < max_swipes:
            if panel:
                from tools import scroll_panel  # 延迟 import 避免加载期循环依赖

                if hasattr(scroll_panel, "invoke"):
                    scroll_panel.invoke({"panel": panel, "direction": "down"})
                else:
                    ctx.device.swipe("up")
            else:
                ctx.device.swipe("up")

    logger.warning(
        "scroll_find: exhausted %d swipes, NOT found label=%r", max_swipes, label
    )
    return make_result(NOT_FOUND, f"滑动后仍未找到: {label}")


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
    return make_result(NOT_FOUND, f"long_press: 未找到 '{label}'")


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

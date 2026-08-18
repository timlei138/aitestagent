"""循环检测 / 页面签名 / 冷却分组 / 终止识别（纯函数）。

从 agents/graph.py 拆出（重构 G2），仅移动代码、不改逻辑。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from typing import Any


def _build_page_signature(ctx: Any) -> str:
    """页面签名：activity + page_title + visible_labels_hash。"""
    if not ctx or not getattr(ctx, "perceiver", None):
        return "unknown"
    try:
        u = ctx.perceiver.perceive()
        act = u.activity or ""
        title = u.page_title or ""
        labels = sorted(
            (e.label or "").strip().lower()
            for e in (u.elements or [])
            if getattr(e, "clickable", False) and (e.label or "").strip()
        )
        vis = "|".join(labels[:80])
        vis_hash = hashlib.md5(vis.encode("utf-8")).hexdigest()[:12]
        return f"{act}|{title}|{vis_hash}"
    except Exception:
        return "unknown"


def _build_call_signature(name: str, args: dict, page_sig: str) -> str:
    try:
        args_norm = json.dumps(args or {}, ensure_ascii=False, sort_keys=True)
    except Exception:
        args_norm = str(args or {})
    return f"{name}|{args_norm}|{page_sig}"


def _cooldown_group(name: str, args: dict, target: str = "", page_sig: str = "") -> str:
    """Return a cooldown group key for semantically repeating actions.

    The group key is ``tool|normalized_target|page_sig``. When ``page_sig`` is
    ``unknown`` we return an empty group so that actions on different pages are
    not accidentally collapsed into the same cooldown bucket.
    """
    if page_sig == "unknown":
        return ""
    if name == "press_key" and str(args.get("key", "")).lower() == "back":
        return "nav_back"
    if name in ("swipe", "scroll_panel"):
        return "browse"

    def _norm_text(value: Any) -> str:
        return re.sub(r"\s+", "", str(value or "").lower())

    if name == "click":
        parts = [
            _norm_text(args.get(k, "") or "")
            for k in ("label", "target", "alternatives")
        ]
        txt = "|".join(p for p in parts if p) or _norm_text(target)
        if txt:
            return f"click|{txt}|{page_sig}"
        return ""

    if name == "scroll_find_and_click":
        txt = _norm_text(args.get("label", "") or args.get("target", "") or target)
        if txt:
            return f"scroll_find_and_click|{txt}|{page_sig}"
        return ""

    if name == "type_input":
        txt = _norm_text(args.get("label", "") or args.get("target", "") or target)
        if txt:
            return f"type_input|{txt}|{page_sig}"
        return ""

    return ""


def _cooldown_group_from_evidence(
    name: str, args: dict, target: str, page_sig: str, evidence: dict[str, Any]
) -> str:
    """Post-invocation cooldown group for actions whose fuzzy/semantic nature
    is only known after execution (e.g. click fuzzy_match).
    """
    if name == "click" and evidence.get("fuzzy_match"):
        parts = [
            re.sub(r"\s+", "", str(args.get(k, "") or "").lower())
            for k in ("label", "target", "alternatives")
        ]
        txt = "|".join(p for p in parts if p) or re.sub(
            r"\s+", "", str(target or "").lower()
        )
        if txt and page_sig != "unknown":
            return f"fuzzy_click|{txt}|{page_sig}"
    return ""


def _resolve_click_match_mode(name: str, args: dict, output: str) -> str:
    """从 click 参数和输出推断 match_mode：exact / semantic / ambiguous。
    L1：优先读规范状态码（AMBIGUOUS），旧格式回退到子串启发式。"""
    from tools.results import parse_status, AMBIGUOUS

    if parse_status(output) == AMBIGUOUS or "ambiguous" in (output or "").lower():
        return "ambiguous"
    index_val = args.get("index", -1)
    if (
        (isinstance(index_val, int) and index_val >= 0)
        or (args.get("rid") or "").strip()
        or (args.get("class_name") or "").strip()
        or (args.get("path_contains") or "").strip()
    ):
        return "exact"
    return "semantic"


def _resolve_click_fallback(output: str) -> bool:
    """从 click 输出判断是否走了兜底路径。L1：按 strategy= 枚举判定，
    旧格式回退到 'fallback' 子串启发式（见 tools.results.is_fallback_output）。"""
    from tools.results import is_fallback_output

    return is_fallback_output(output)


def _output_has_page_change(
    output: str, page_sig_before: str = "", page_sig_after: str = ""
) -> bool:
    if page_sig_before and page_sig_after and page_sig_before != page_sig_after:
        return True
    m = re.search(r"页面变化:\s*(.+?)\s*→\s*(.+?)(?:\s*\||$)", output or "")
    if not m:
        return False
    return m.group(1).strip() != m.group(2).strip()


# Phase 1.2: 锚定行首的 DONE/ABORT 检测（兼容 ##/### Markdown 标题 + **/__/bold 前缀）
_DONE_PATTERN = re.compile(
    r"^(?:#{1,3}\s*)?(?:\*{1,2}|_{1,2})?(DONE|ABORT)\s*[:\uff1a]",
    re.IGNORECASE | re.MULTILINE,
)


def _detect_toggle_loop(
    action_history: list[dict[str, Any]], window: int = 8, threshold: int = 3
) -> tuple[bool, str]:
    """Detect destructive toggle loops such as open/close/open on the same switch.

    Unlike the consecutive-signature detector, this looks at the recent ``window``
    actions and flags any target that is clicked ``threshold`` or more times on the
    same page. Interleaved asserts/sensing do not hide the pattern.
    """
    recent = action_history[-window:] if action_history else []
    keys: list[str] = []
    for entry in recent:
        name = entry.get("name") or entry.get("tool_name") or ""
        if name != "click":
            continue
        args = entry.get("tool_input") or entry.get("args") or {}
        page_sig = (
            entry.get("page_after_signature")
            or entry.get("page_after", {}).get("signature")
            or ""
        )
        parts = [
            re.sub(r"\s+", "", str(args.get(k, "") or "").lower())
            for k in ("label", "target", "alternatives")
        ]
        txt = "|".join(p for p in parts if p)
        if txt:
            keys.append(f"click|{txt}|{page_sig}")
    if not keys:
        return (False, "")
    counts = Counter(keys)
    most_common = counts.most_common(1)[0]
    if most_common[1] >= threshold:
        return (True, most_common[0])
    return (False, "")


def _detect_termination(result: str) -> tuple[bool, bool]:
    """返回 (done, abort) — 取最后一个行首匹配（后追加的标记优先级更高）。"""
    matches = list(_DONE_PATTERN.finditer(result.strip()))
    if not matches:
        return (False, False)
    m = matches[-1]
    return (m.group(1).upper() == "DONE", m.group(1).upper() == "ABORT")

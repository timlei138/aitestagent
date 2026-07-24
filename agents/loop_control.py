"""循环检测 / 页面签名 / 冷却分组 / 终止识别（纯函数）。

从 agents/graph.py 拆出（重构 G2），仅移动代码、不改逻辑。
"""

from __future__ import annotations

import hashlib
import json
import re
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


def _cooldown_group(name: str, args: dict, target: str = "") -> str:
    if name == "press_key" and str(args.get("key", "")).lower() == "back":
        return "nav_back"
    if name in ("swipe", "scroll_panel"):
        return "browse"
    if name == "click":
        txt = " ".join(
            [
                str(args.get("label", "") or ""),
                str(args.get("target", "") or ""),
                str(args.get("alternatives", "") or ""),
                str(target or ""),
            ]
        )
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


def _detect_termination(result: str) -> tuple[bool, bool]:
    """返回 (done, abort) — 取最后一个行首匹配（后追加的标记优先级更高）。"""
    matches = list(_DONE_PATTERN.finditer(result.strip()))
    if not matches:
        return (False, False)
    m = matches[-1]
    return (m.group(1).upper() == "DONE", m.group(1).upper() == "ABORT")

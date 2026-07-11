"""纯文本工具函数（无外部依赖，供各 tools 子模块复用）。

本模块从 tools/__init__.py 拆出（重构 T1），仅移动代码、不改逻辑。
"""

from __future__ import annotations

from typing import Any

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


def _has_cjk(text: str) -> bool:
    """检测字符串中是否含 CJK 中日韩字符。"""
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)

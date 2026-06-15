from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DANGEROUS_KEYWORDS = [
    "删除", "移除", "清空", "提交", "发送", "支付", "购买", "下单",
    "拨打", "注销", "退出登录", "恢复出厂", "重置", "格式化",
    "delete", "remove", "clear", "submit", "send", "pay", "buy",
    "call", "logout", "reset",
]


@dataclass
class SafetyDecision:
    allowed: bool
    reason: str = ""


def check_dangerous(label: str) -> SafetyDecision:
    """检查操作标签是否命中危险关键词。"""
    text = (label or "").lower()
    for kw in DANGEROUS_KEYWORDS:
        if kw in text:
            return SafetyDecision(False, f"命中危险操作关键词: {kw}")
    return SafetyDecision(True)

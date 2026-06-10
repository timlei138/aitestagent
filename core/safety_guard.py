from __future__ import annotations

from dataclasses import dataclass


DANGEROUS_KEYWORDS = [
    "删除", "移除", "清空", "提交", "发送", "支付", "购买", "下单", "拨打", "注销", "退出登录",
    "恢复出厂", "重置", "格式化",
    "delete", "remove", "clear", "submit", "send", "pay", "buy", "call", "logout", "reset",
]


@dataclass
class SafetyDecision:
    allowed: bool
    reason: str = ""


class SafetyGuard:
    def __init__(self, level: str = "strict", dangerous_keywords: list[str] | None = None):
        self.level = level
        self.dangerous_keywords = [k.lower() for k in (dangerous_keywords or DANGEROUS_KEYWORDS)]

    def check_click(self, label: str) -> SafetyDecision:
        text = (label or "").lower()
        matched = next((k for k in self.dangerous_keywords if k in text), "")
        if matched and self.level in {"strict", "normal"}:
            return SafetyDecision(False, f"命中危险操作关键词: {matched}")
        return SafetyDecision(True)


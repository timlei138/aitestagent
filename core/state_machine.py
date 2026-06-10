from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from enum import Enum


class TestState(str, Enum):
    RUNNING = "running"
    POPUP = "popup"
    STUCK = "stuck"
    CRASHED = "crashed"
    LOST = "lost"
    COMPLETED = "completed"
    FAILED = "failed"


class StateMachine:
    """执行恢复状态机：处理弹窗、卡住、崩溃和跳出目标 App。"""

    def __init__(self, device, max_recovery: int = 5):
        self.device = device
        self.max_recovery = max_recovery
        self.recovery_count = 0
        self.state = TestState.RUNNING

    def detect_popup(self) -> list[str]:
        root = ET.fromstring(self.device.dump_hierarchy())
        keywords = ["允许", "拒绝", "确定", "取消", "同意", "关闭", "跳过", "知道了", "Allow", "Deny", "OK", "Cancel"]
        buttons = []
        for node in root.iter():
            text = node.get("text", "")
            if node.get("clickable") == "true" and text in keywords:
                buttons.append(text)
        if buttons:
            self.state = TestState.POPUP
        return buttons

    def recover_popup(self) -> bool:
        if self.recovery_count >= self.max_recovery:
            return False
        self.recovery_count += 1
        for text in ["允许", "确定", "同意", "OK", "Allow", "关闭", "知道了"]:
            if self.device.click_text(text, timeout=0.5):
                self.state = TestState.RUNNING
                time.sleep(0.5)
                return True
        return False

    def recover_stuck(self) -> bool:
        if self.recovery_count >= self.max_recovery:
            return False
        self.recovery_count += 1
        self.device.swipe("up")
        self.state = TestState.RUNNING
        time.sleep(0.5)
        return True

    def recover_app(self, package: str) -> bool:
        if self.recovery_count >= self.max_recovery:
            return False
        self.recovery_count += 1
        self.device.app_start(package)
        self.state = TestState.RUNNING
        time.sleep(1.5)
        return True

    def reset(self) -> None:
        self.recovery_count = 0
        self.state = TestState.RUNNING

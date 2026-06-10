from __future__ import annotations

import base64
import hashlib
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from io import BytesIO
from typing import Any

from core.model_clients import VLMClient


class PerceptionMode:
    UI_TREE = "ui_tree"
    VISION = "vision"
    HYBRID = "hybrid"


@dataclass
class UIElement:
    text: str = ""
    content_desc: str = ""
    resource_id: str = ""
    class_name: str = ""
    package: str = ""
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)
    clickable: bool = False
    enabled: bool = True
    selected: bool = False
    checked: bool | None = None
    role: str = "unknown"
    region: str = "unknown"
    priority: int = 100
    safe_to_click: bool = True

    @property
    def label(self) -> str:
        return self.text or self.content_desc or self.resource_id

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["bounds"] = list(self.bounds)
        data["label"] = self.label
        return data


@dataclass
class PageUnderstanding:
    layout: str
    summary: str
    package: str = ""
    activity: str = ""
    width: int = 0
    height: int = 0
    regions: list[dict[str, Any]] = field(default_factory=list)
    elements: list[UIElement] = field(default_factory=list)
    primary_paths: list[UIElement] = field(default_factory=list)
    risky_actions: list[UIElement] = field(default_factory=list)
    raw_vision: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "layout": self.layout,
            "summary": self.summary,
            "package": self.package,
            "activity": self.activity,
            "width": self.width,
            "height": self.height,
            "regions": self.regions,
            "elements": [e.to_dict() for e in self.elements],
            "primary_paths": [e.to_dict() for e in self.primary_paths],
            "risky_actions": [e.to_dict() for e in self.risky_actions],
            "raw_vision": self.raw_vision,
        }


class SmartPerceiver:
    """UI 树 + 可选 Vision 的页面语义理解器，UI树卡住时自动切换到 Vision。"""

    def __init__(
        self,
        device,
        llm_client: VLMClient | None = None,
        mode: str = PerceptionMode.HYBRID,
        auto_switch: bool = True,
        stuck_threshold: int = 2,
    ):
        self.device = device
        self.vlm = llm_client
        self.mode = mode
        self.auto_switch = auto_switch
        self.stuck_threshold = stuck_threshold
        self._last_hash: str = ""
        self._stuck_count: int = 0
        self._vision_calls: int = 0
        self.last_vision_log: dict[str, Any] = {}
        self.logger = logging.getLogger(__name__)

    def perceive(self) -> PageUnderstanding:
        xml = self.device.dump_hierarchy()
        elements = self.parse_elements(xml)
        snapshot = self.device.snapshot()
        understanding = self._heuristic_understand(
            elements=elements,
            package=snapshot.package,
            activity=snapshot.activity,
            width=snapshot.width,
            height=snapshot.height,
        )
        if self.mode in {PerceptionMode.VISION, PerceptionMode.HYBRID} and self.vlm:
            self._vision_calls += 1
            understanding.raw_vision = self._vision_describe(
                snapshot.image_base64, understanding
            )

        # 检测是否卡在相同页面，自动切换模式
        if self.auto_switch and self.mode != PerceptionMode.VISION:
            self._update_stuck(understanding.summary)
        return understanding

    def screen_signature(self) -> str:
        xml = self.device.dump_hierarchy()
        app = self.device.current_app()
        compact = re.sub(r"\s+", "", xml)
        return hashlib.md5(
            f"{app.get('package')}|{app.get('activity')}|{compact}".encode()
        ).hexdigest()

    def parse_elements(self, xml: str) -> list[UIElement]:
        root = ET.fromstring(xml)
        elements: list[UIElement] = []
        for node in root.iter():
            text = node.get("text", "") or ""
            desc = node.get("content-desc", "") or ""
            rid = node.get("resource-id", "") or ""
            if not (text or desc or rid):
                continue
            elements.append(
                UIElement(
                    text=text,
                    content_desc=desc,
                    resource_id=rid,
                    class_name=node.get("class", "") or "",
                    package=node.get("package", "") or "",
                    bounds=self._parse_bounds(node.get("bounds", "")),
                    clickable=node.get("clickable", "false") == "true",
                    enabled=node.get("enabled", "true") == "true",
                    selected=node.get("selected", "false") == "true",
                    checked=self._parse_checked(node.get("checked")),
                )
            )
        return elements

    def _heuristic_understand(
        self,
        elements: list[UIElement],
        package: str,
        activity: str,
        width: int,
        height: int,
    ) -> PageUnderstanding:
        left_clickables = [
            e for e in elements if e.clickable and e.bounds[2] <= width * 0.45
        ]
        right_elements = [e for e in elements if e.bounds[0] >= width * 0.35]
        has_two_pane = len(left_clickables) >= 3 and len(right_elements) >= 3

        layout = "two_pane" if has_two_pane else "single_pane"
        regions = []
        if has_two_pane:
            regions = [
                {
                    "name": "left_navigation",
                    "role": "navigation",
                    "bounds": [0, 0, int(width * 0.45), height],
                },
                {
                    "name": "right_content",
                    "role": "content",
                    "bounds": [int(width * 0.35), 0, width, height],
                },
            ]
        else:
            regions = [
                {
                    "name": "main_content",
                    "role": "content",
                    "bounds": [0, 0, width, height],
                }
            ]

        risky: list[UIElement] = []
        primary: list[UIElement] = []
        for e in elements:
            self._classify_element(e, width, height, has_two_pane)
            if not e.safe_to_click:
                risky.append(e)
            if (
                e.clickable
                and e.safe_to_click
                and e.role in {"navigation_item", "tab", "list_entry", "settings_entry"}
            ):
                primary.append(e)

        primary.sort(
            key=lambda item: (item.priority, item.bounds[1], item.bounds[0], item.label)
        )
        return PageUnderstanding(
            layout=layout,
            summary=f"{layout} 页面，识别到 {len(primary)} 个主要路径入口",
            package=package,
            activity=activity,
            width=width,
            height=height,
            regions=regions,
            elements=elements,
            primary_paths=primary[:80],
            risky_actions=risky,
        )

    def _classify_element(
        self, e: UIElement, width: int, height: int, has_two_pane: bool
    ) -> None:
        label = e.label.lower()
        if any(
            k in label
            for k in [
                "删除",
                "提交",
                "发送",
                "支付",
                "购买",
                "重置",
                "注销",
                "退出",
                "delete",
                "submit",
                "send",
                "pay",
                "reset",
            ]
        ):
            e.safe_to_click = False
        left, top, right, bottom = e.bounds
        class_name = e.class_name.lower()
        if has_two_pane and right <= width * 0.45 and e.clickable:
            e.role = "navigation_item"
            e.region = "left_navigation"
            e.priority = 1
        elif e.selected and e.clickable:
            e.role = "tab"
            e.region = "navigation"
            e.priority = 2
        elif "switch" in class_name:
            e.role = "switch"
            e.region = "content"
            e.priority = 20
        elif e.clickable and (bottom - top) >= 32:
            e.role = (
                "settings_entry"
                if any(
                    k in label
                    for k in [
                        "wi-fi",
                        "wifi",
                        "蓝牙",
                        "显示",
                        "声音",
                        "网络",
                        "setting",
                    ]
                )
                else "list_entry"
            )
            e.region = "right_content" if left >= width * 0.35 else "main_content"
            e.priority = 5
        elif e.clickable:
            e.role = "button"
            e.region = "content"
            e.priority = 30
        else:
            e.role = "text"
            e.region = "content"
            e.priority = 100

    def switch_mode(self, mode: str) -> None:
        """手动切换感知模式，重置卡住计数。"""
        if mode in {
            PerceptionMode.UI_TREE,
            PerceptionMode.VISION,
            PerceptionMode.HYBRID,
        }:
            self.mode = mode
        self._stuck_count = 0

    @property
    def stats(self) -> dict:
        return {"current_mode": self.mode, "vision_calls": self._vision_calls}

    def _update_stuck(self, current_result: str) -> None:
        """检测是否卡在相同页面，自动从 UI_TREE 切换到 VISION。"""
        import hashlib

        result_hash = hashlib.md5(current_result.encode()).hexdigest()[:8]
        if result_hash == self._last_hash:
            self._stuck_count += 1
            if (
                self._stuck_count >= self.stuck_threshold
                and self.mode == PerceptionMode.UI_TREE
            ):
                self.mode = PerceptionMode.VISION
        else:
            # 页面变化，如果之前在 VISION 模式且卡住过，切回 UI_TREE
            if self.mode == PerceptionMode.VISION and self._stuck_count > 0:
                self.mode = PerceptionMode.UI_TREE
            self._stuck_count = 0
        self._last_hash = result_hash

    def _vision_describe(self, image_base64: str, base: PageUnderstanding) -> str:
        """调用 Vision LLM 对截图进行语义描述。"""
        prompt = (
            "你是 Android UI 自动化测试的页面理解器。请根据截图说明页面布局、导航区域、"
            "主要可探索入口、危险按钮和状态控件。输出简短中文说明。"
        )
        context = f"UI树初步判断: {base.summary}"
        try:
            answer = self.vlm.describe(
                prompt=prompt,
                image_base64=image_base64,
                context=context,
            )
            self.last_vision_log = {
                "prompt": prompt,
                "context": context,
                "response": answer,
            }
            self.logger.info(
                "vision_llm ok | context=%s | response=%s",
                context,
                (answer or "")[:500],
            )
            return answer
        except Exception as exc:
            self.last_vision_log = {
                "prompt": prompt,
                "context": context,
                "error": str(exc),
            }
            self.logger.warning("vision_llm failed | context=%s | error=%s", context, exc)
            return f"Vision 解析失败: {exc}"

    def _parse_bounds(self, raw: str) -> tuple[int, int, int, int]:
        nums = [int(n) for n in re.findall(r"\d+", raw or "")]
        if len(nums) == 4:
            return nums[0], nums[1], nums[2], nums[3]
        return 0, 0, 0, 0

    def _parse_checked(self, value: str | None) -> bool | None:
        if value == "true":
            return True
        if value == "false":
            return False
        return None

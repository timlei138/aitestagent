from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from io import BytesIO
from typing import Any, Callable

import app_paths


class PerceptionMode:
    UI_TREE = "ui_tree"
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
    associated_label: str = ""  # 关联文本标签（来自兄弟 TextView 或 Vision 语义标注）
    context_path: str = ""  # 上下文路径，例如 'right_content > WLAN > toggle_switch'
    is_container: bool = False  # 是否为结构性容器（LinearLayout/ViewGroup 等）
    has_switch_child: bool = False  # 是否包裹 Switch 类子控件（合并标记）

    @property
    def label(self) -> str:
        return (
            self.text or self.content_desc or self.associated_label or self.resource_id
        )

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
    page_title: str = ""  # 从 UI 树 Toolbar/标题栏提取的页面标题
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
    """UI 树 + 按需视觉补充的页面语义理解器。"""

    def __init__(
        self,
        device,
        vision_call: Callable[[str, str, str, bool], dict[str, Any]] | None = None,
        screenshot_sink: Callable[[str], None] | None = None,
        mode: str = PerceptionMode.HYBRID,
        auto_switch: bool = True,
        stuck_threshold: int = 2,
    ):
        self.device = device
        self._vision_call = vision_call
        self._screenshot_sink = screenshot_sink
        self.mode = mode
        self.auto_switch = auto_switch
        self.stuck_threshold = stuck_threshold
        self._last_hash: str = ""
        self._stuck_count: int = 0
        self._vision_calls: int = 0
        self.last_vision_log: dict[str, Any] = {}
        self.logger = logging.getLogger(__name__)
        # 短时缓存：同页面 3 秒内复用
        self._cache_sig: str = ""
        self._cache_ts: float = 0.0
        self._cache_result: PageUnderstanding | None = None
        # 视觉补充缓存：截图不变时不重复调用
        self._vision_cache_img_hash: str = ""
        self._vision_cache_text: str = ""

    def perceive(self, force_vision: bool = False) -> PageUnderstanding:
        # 短时缓存：相同页面+相同mode + 3秒内直接返回
        xml = self.device.dump_hierarchy()
        sig = hashlib.md5(f"{xml}|{self.mode}".encode()).hexdigest()
        now = time.monotonic()
        if (
            sig == self._cache_sig
            and (now - self._cache_ts) < 5.0
            and self._cache_result is not None
        ):
            self.logger.debug("Perceive cache hit sig=%s", sig[:8])
            return self._cache_result
        elements = self.parse_elements(xml)
        page_title = self._extract_page_title(xml)
        snapshot = self.device.snapshot()
        # ── 截图存盘：perceive cache miss 时顺带存磁盘，供 assert_verification 复用（零额外截图调用）
        try:
            if snapshot.image_base64:
                app_paths.SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
                from datetime import datetime as _dt

                _shot_path = str(
                    app_paths.SCREENSHOT_DIR
                    / f"perceive_{_dt.now().strftime('%Y%m%d_%H%M%S_%f')}.png"
                )
                with open(_shot_path, "wb") as _f:
                    _f.write(base64.b64decode(snapshot.image_base64))
                if self._screenshot_sink is not None:
                    # 发送绝对路径，消费方按需转换为相对路径
                    self._screenshot_sink(_shot_path)
        except Exception:
            pass
        understanding = self._heuristic_understand(
            elements=elements,
            package=snapshot.package,
            activity=snapshot.activity,
            page_title=page_title,
            width=snapshot.width,
            height=snapshot.height,
        )
        should_use_vision = self._vision_call is not None and (
            force_vision
            or (
                self.mode == PerceptionMode.HYBRID
                and self._stuck_count >= self.stuck_threshold
            )
        )
        if should_use_vision:
            indexed_elements = understanding.primary_paths[:20]
            # 视觉缓存：截图不变时复用上次视觉结果（图像 hash）
            import hashlib as _hashlib

            img_hash = (
                _hashlib.md5(snapshot.image_base64.encode()).hexdigest()
                if snapshot.image_base64
                else ""
            )
            if (
                img_hash
                and img_hash == self._vision_cache_img_hash
                and self._vision_cache_text
            ):
                self.logger.info("Vision cache hit (img sig=%s)", img_hash[:8])
                understanding.raw_vision = self._vision_cache_text
                self._apply_vision_annotations(
                    indexed_elements, self._vision_cache_text
                )
            else:
                self._vision_calls += 1
                understanding.raw_vision = self._vision_describe(
                    snapshot.image_base64, understanding
                )
                self._vision_cache_img_hash = img_hash
                self._vision_cache_text = understanding.raw_vision

        # 检测是否卡在相同页面，供 hybrid 模式按需触发视觉补充
        if self.auto_switch:
            self._update_stuck(understanding.summary)
        # 存入缓存
        self._cache_sig = sig
        self._cache_ts = now
        self._cache_result = understanding
        return understanding

    def screen_signature(self) -> str:
        xml = self.device.dump_hierarchy()
        app = self.device.current_app()
        compact = re.sub(r"\s+", "", xml)
        return hashlib.md5(
            f"{app.get('package')}|{app.get('activity')}|{compact}".encode()
        ).hexdigest()

    def parse_elements(self, xml: str) -> list[UIElement]:
        """解析 UI 树 XML，结构化保留策略 + 关联标签 + 上下文路径 + 父子去重。

        保留规则（用户方案 A — 结构容器全保留）:
        1. 有 text/desc/rid 的常规控件
        2. 可点击节点（交互入口，包括没有文本的 clickable 容器）
        3. Switch/Toggle/CheckBox 等开关类控件（即使没文本）
        4. 结构性容器（layout/recyclerview/scroll/viewgroup/cardview）
        """
        root = ET.fromstring(xml)

        # 构建 parent 映射（ET 不提供直接 parent 引用）
        parent_map: dict[ET.Element, ET.Element] = {}
        for parent in root.iter():
            for child in parent:
                parent_map[child] = parent

        # 第 1 阶段：节点 → UIElement（含 context_path）
        node_to_el: dict[ET.Element, UIElement] = {}
        elements: list[UIElement] = []

        switch_keywords = (
            "switch",
            "togglebutton",
            "checkbox",
            "radiobutton",
            "compoundbutton",
        )
        structural_keywords = (
            "layout",
            "recyclerview",
            "scroll",
            "viewgroup",
            "cardview",
            "listview",
            "gridview",
        )

        for node in root.iter():
            # 跳过不可见元素
            visibility = (node.get("visibility") or "").lower()
            if visibility in ("gone", "invisible"):
                continue
            text = node.get("text", "") or ""
            desc = node.get("content-desc", "") or ""
            rid = node.get("resource-id", "") or ""
            clickable = node.get("clickable", "false") == "true"
            class_name = node.get("class", "") or ""
            cls_lower = class_name.lower()

            is_switch_like = any(kw in cls_lower for kw in switch_keywords)
            is_structural = any(kw in cls_lower for kw in structural_keywords)

            should_keep = False
            if text or desc or rid:
                should_keep = True  # 规则 1：有名控件
            elif clickable:
                should_keep = True  # 规则 2：可点击容器
            elif is_switch_like:
                should_keep = True  # 规则 3：开关类控件
            elif is_structural:
                should_keep = True  # 规则 4：结构容器（用户选 A，全保留）

            if not should_keep:
                continue

            bounds = self._parse_bounds(node.get("bounds", ""))
            # 跳过零面积节点（无意义）
            if (
                bounds == (0, 0, 0, 0)
                or (bounds[2] - bounds[0]) <= 0
                or (bounds[3] - bounds[1]) <= 0
            ):
                continue

            el = UIElement(
                text=text,
                content_desc=desc,
                resource_id=rid,
                class_name=class_name,
                package=node.get("package", "") or "",
                bounds=bounds,
                clickable=clickable,
                enabled=node.get("enabled", "true") == "true",
                selected=node.get("selected", "false") == "true",
                checked=self._parse_checked(node.get("checked")),
                is_container=is_structural and not (text or desc),
            )

            # 为没有 text/desc 的控件（Switch、clickable 容器、或系统弹窗等结构性容器）建立关联标签
            if not text and not desc and (clickable or is_structural):
                associated = self._find_associated_label(node, parent_map)
                if associated:
                    el.associated_label = associated

            # 上下文路径（仅用 text/desc/rid 有名节点构建）
            el.context_path = self._build_context_path(node, parent_map)

            node_to_el[node] = el
            elements.append(el)

        # 第 2 阶段：父子去重 — clickable 父 + 唯一 Switch 子 → 合并到父
        elements = self._merge_parent_with_switch_child(
            elements, node_to_el, parent_map
        )
        return elements

    def _extract_page_title(self, xml: str) -> str:
        """从 UI 树中提取页面标题。

        策略:
        1. 找 Toolbar/ActionBar 内的 TextView 文本
        2. 找 resource_id 包含 'title' 的元素
        3. 兜底: 屏幕顶部区域的第一个有文本的非导航元素
        """
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            return ""

        # 策略 1: Toolbar 内的 TextView
        for node in root.iter():
            cls = (node.get("class", "") or "").lower()
            if "toolbar" in cls or "actionbar" in cls or "action_bar" in cls:
                texts = []
                for child in node.iter():
                    child_cls = (child.get("class", "") or "").lower()
                    if "textview" in child_cls or "edittext" in child_cls:
                        t = (child.get("text", "") or "").strip()
                        if t and len(t) < 50:
                            texts.append(t)
                if texts:
                    return texts[0]  # 第一个文本通常是标题

        # 策略 2: resource_id 含 title
        for node in root.iter():
            rid = node.get("resource_id", "") or ""
            if "title" in rid.lower() and not rid.endswith("_title"):
                continue
            if "title" in rid.lower():
                t = (node.get("text", "") or "").strip()
                if t and 2 <= len(t) <= 40:
                    return t

        # 策略 3: 顶部区域有文本的非导航元素
        top_texts = []
        for node in root.iter():
            bounds_str = node.get("bounds", "") or ""
            m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds_str)
            if not m:
                continue
            y1 = int(m.group(2))
            y2 = int(m.group(4))
            # 屏幕顶部 20% 区域
            if y1 < 400 and y2 < 700:
                t = (node.get("text", "") or "").strip()
                cls = (node.get("class", "") or "").lower()
                if t and 2 <= len(t) <= 40 and "textview" in cls:
                    rid = node.get("resource_id", "") or ""
                    # 排除导航按钮
                    if "back" not in rid.lower() and "home" not in rid.lower():
                        top_texts.append((y1, t))
        if top_texts:
            top_texts.sort()
            return top_texts[0][1]

        return ""

    def _heuristic_understand(
        self,
        elements: list[UIElement],
        package: str,
        activity: str,
        page_title: str,
        width: int,
        height: int,
    ) -> PageUnderstanding:
        # 用更严格的 left 判定（必须完全在左 45% 内 + 高度合理）来识别 two_pane
        left_clickables = [
            e
            for e in elements
            if e.clickable
            and e.bounds[2] <= width * 0.45
            and (e.bounds[3] - e.bounds[1]) >= 32
        ]
        right_elements = [
            e
            for e in elements
            if e.bounds[0] >= width * 0.35
            and (e.bounds[2] - e.bounds[0]) > 0
            and (e.bounds[3] - e.bounds[1]) > 0
        ]
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
                and e.role
                in {
                    "navigation_item",
                    "tab",
                    "list_entry",
                    "settings_entry",
                    "switch",
                    "switch_row",
                    "button",
                }
            ):
                primary.append(e)

        primary.sort(
            key=lambda item: (item.priority, item.bounds[1], item.bounds[0], item.label)
        )
        # 构建带页面标题的 summary
        title_part = f"「{page_title}」" if page_title else ""
        act_short = activity.split(".")[-1] if activity else ""
        breadcrumb = f"{title_part}" if title_part else act_short
        return PageUnderstanding(
            layout=layout,
            summary=(
                f"{breadcrumb} — {layout} 页面（结构分区标签，不保证左右方位），识别到 {len(primary)} 个主要路径入口"
                if breadcrumb
                else f"{layout} 页面（结构分区标签，不保证左右方位），识别到 {len(primary)} 个主要路径入口"
            ),
            package=package,
            activity=activity,
            page_title=page_title,
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
        """为 UIElement 赋 role / region / priority。

        关键修复（v2）:
        - 左侧导航判定增加硬约束：必须 right <= width*0.45 AND left < width*0.30
        - 区分 settings_entry（右侧二级页入口）vs list_entry（普通列表项）
        - 引入 switch_row（包裹 Switch 的 clickable 容器，优先级最高）
        """
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
        in_left_pane = (
            has_two_pane
            and right <= width * 0.45
            and left < width * 0.30  # 硬约束：起点必须在左 30% 内
        )
        in_right_pane = has_two_pane and left >= width * 0.35

        # 区域归属（不论是否可点击都标注，便于检索）
        if in_left_pane:
            e.region = "left_navigation"
        elif in_right_pane:
            e.region = "right_content"
        else:
            e.region = "main_content"

        # 角色判定 — 按优先级匹配
        if e.has_switch_child and e.clickable:
            # 包裹 Switch 的 clickable 容器：最优点击目标
            e.role = "switch_row"
            e.priority = 3
        elif "switch" in class_name or "togglebutton" in class_name:
            e.role = "switch"
            e.priority = 4
        elif in_left_pane and e.clickable and (bottom - top) >= 32:
            e.role = "navigation_item"
            e.priority = 1
        elif e.selected and e.clickable:
            e.role = "tab"
            e.priority = 2
        elif e.clickable and (bottom - top) >= 32 and (right - left) >= 32:
            is_settings_kw = any(
                k in label
                for k in [
                    "wi-fi",
                    "wifi",
                    "wlan",
                    "蓝牙",
                    "显示",
                    "声音",
                    "网络",
                    "setting",
                ]
            )
            e.role = "settings_entry" if is_settings_kw else "list_entry"
            e.priority = 5 if is_settings_kw else 6
        elif e.clickable:
            e.role = "button"
            e.priority = 30
        elif e.is_container:
            e.role = "container"
            e.priority = 90
        else:
            e.role = "text"
            e.priority = 100

    def switch_mode(self, mode: str) -> None:
        """手动切换感知模式（ui_tree/hybrid），重置卡住计数。"""
        if mode in {PerceptionMode.UI_TREE, PerceptionMode.HYBRID}:
            self.mode = mode
        self._stuck_count = 0

    @property
    def stats(self) -> dict:
        return {"current_mode": self.mode, "vision_calls": self._vision_calls}

    def _update_stuck(self, current_result: str) -> None:
        """检测是否卡在相同页面，维护卡住计数给 hybrid 触发条件使用。"""
        import hashlib

        result_hash = hashlib.md5(current_result.encode()).hexdigest()[:8]
        if result_hash == self._last_hash:
            self._stuck_count += 1
        else:
            self._stuck_count = 0
        self._last_hash = result_hash

    def _vision_describe(self, image_base64: str, base: PageUnderstanding) -> str:
        """调用统一视觉 helper 对截图进行语义描述。"""
        if not self._vision_call:
            return ""
        # 构建 UI 树元素列表作为 context（含 bounds，Vision 无法输出坐标但可以引用序号）
        indexed_elements = base.primary_paths[:20]
        el_lines = ["UI tree elements (with exact bounds):"]
        for i, el in enumerate(indexed_elements):
            extra = f" clickable text='{el.label}'" if el.label else ""
            extra += f" class={el.class_name.split('.')[-1]}" if el.class_name else ""
            extra += f" role={el.role}" if el.role else ""
            extra += (
                f" bounds=({el.bounds[0]},{el.bounds[1]},{el.bounds[2]},{el.bounds[3]})"
            )
            el_lines.append(f"  [{i}] {extra}")
        element_context = "\n".join(el_lines)

        prompt = (
            "You are an Android UI analyzer. Below are UI tree elements with exact pixel bounds. "
            "Look at the screenshot and annotate each element with its ACTUAL meaning.\n\n"
            "Key rules:\n"
            "- Switch/Toggle controls often have empty text — identify them by their visual position\n"
            "- Navigation items in the left panel have their own text labels\n"
            "- If an element has no visible text, describe what it IS based on the screenshot\n"
            "- Return results indexed by the element number [0], [1], etc.\n"
            "- Format: [N] semantic_label | notes\n\n"
            f"{element_context}"
        )
        try:
            result = self._vision_call(
                prompt,
                image_base64,
                "perceiver_annotate",
                False,
            )
            answer = str(result.get("raw", "") or result.get("reason", ""))
            if not result.get("ok", False):
                self.logger.warning(
                    "vision helper failed | reason=%s", result.get("reason", "")
                )
                return answer or f"Vision 解析失败: {result.get('error', '')}"
            # 将 Vision 语义标注映射回 UI 元素
            self._apply_vision_annotations(indexed_elements, answer)

            self.last_vision_log = {
                "prompt": prompt,
                "context": element_context,
                "response": answer,
            }
            self.logger.info(
                "vision_llm ok | elements=%d | response=%s",
                len(indexed_elements),
                (answer or "")[:500],
            )
            return answer
        except Exception as exc:
            self.last_vision_log = {
                "prompt": prompt,
                "context": element_context,
                "error": str(exc),
            }
            self.logger.warning("vision_llm failed | error=%s", exc)
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

    # ── Switch 关联标签辅助方法 ──

    def _find_associated_label(self, switch_node: ET.Element, parent_map: dict) -> str:
        """为 Switch 类控件查找关联的文本标签。

        策略:
        1. 直接兄弟节点中找垂直位置重叠最大的有 text 的节点
        2. 向上一层（祖父节点）继续找
        """
        switch_bounds = self._parse_bounds(switch_node.get("bounds", ""))

        # 策略 0: 子节点中的文本（列表项常用: LinearLayout > TextView）
        for child in switch_node.iter():
            if child is switch_node:
                continue
            text = child.get("text", "") or ""
            desc = child.get("content-desc", "") or ""
            label = text or desc
            if label and len(label) >= 2:
                cls = (child.get("class", "") or "").lower()
                if "textview" in cls or "edittext" in cls:
                    return label

        # 策略 1: 直接兄弟
        parent = parent_map.get(switch_node)
        if parent is not None:
            label = self._best_label_in_container(switch_node, parent, switch_bounds)
            if label:
                return label

        # 策略 2: 祖父层（父节点的兄弟节点中的文本）
        if parent is not None:
            grandparent = parent_map.get(parent)
            if grandparent is not None:
                label = self._best_label_in_container(
                    parent, grandparent, switch_bounds
                )
                if label:
                    return label

        return ""

    def _best_label_in_container(
        self,
        target_node: ET.Element,
        container: ET.Element,
        target_bounds: tuple[int, int, int, int],
    ) -> str:
        """在容器节点中查找与目标控件位置最关联的文本标签。

        选择 y 轴重叠最大的兄弟文本节点作为关联标签（同一行的标签和开关 y 重叠最大）。"""
        best_label = ""
        best_overlap = 0

        for child in container:
            if child is target_node:
                continue

            visibility = (child.get("visibility") or "").lower()
            if visibility in ("gone", "invisible"):
                continue

            text = child.get("text", "") or ""
            desc = child.get("content-desc", "") or ""
            label = text or desc
            if not label:
                continue

            # 排除 Switch 类自身
            cls = (child.get("class", "") or "").lower()
            if any(
                kw in cls
                for kw in ("switch", "togglebutton", "checkbox", "radiobutton")
            ):
                continue

            child_bounds = self._parse_bounds(child.get("bounds", ""))

            # 计算 y 轴重叠（同行元素 y 范围重叠大）
            overlap = min(target_bounds[3], child_bounds[3]) - max(
                target_bounds[1], child_bounds[1]
            )
            if overlap > best_overlap:
                best_overlap = overlap
                best_label = label

        # 要求至少 30% 的 y 轴重叠
        height = target_bounds[3] - target_bounds[1]
        if height > 0 and best_overlap >= height * 0.3:
            return best_label

        return ""

    def _apply_vision_annotations(
        self, elements: list[UIElement], vision_text: str
    ) -> None:
        """将 Vision LLM 返回的语义标注映射回 UI 元素的 associated_label。

        Vision 返回格式示例: [0] WLAN 开关 | Wi-Fi toggle control
        匹配 [N] 标注格式，为没有 text 的元素补充语义标签。"""
        if not vision_text:
            return
        pattern = re.compile(r"\[(\d+)\]\s*(.+?)(?:\s*[|｜]\s*(.*))?\s*$")
        for line in vision_text.split("\n"):
            m = pattern.match(line.strip())
            if not m:
                continue
            idx = int(m.group(1))
            semantic_label = m.group(2).strip()
            if idx < len(elements):
                el = elements[idx]
                # 只为没有自身文本标签的元素补充 Vision 语义标签
                if not el.text and not el.content_desc:
                    el.associated_label = semantic_label
                    self.logger.info(
                        "vision_annotate | idx=%d | label='%s' → element %s",
                        idx,
                        semantic_label,
                        el.resource_id or el.class_name,
                    )

    # ── 上下文路径 / 父子去重 辅助方法 ──

    _CONTAINER_CLASSES = (
        "layout",
        "recyclerview",
        "scroll",
        "viewgroup",
        "cardview",
        "listview",
        "gridview",
    )

    _SWITCH_CLASSES = (
        "switch",
        "togglebutton",
        "checkbox",
        "radiobutton",
        "compoundbutton",
    )

    def _build_context_path(
        self, node: ET.Element, parent_map: dict[ET.Element, ET.Element]
    ) -> str:
        """沿父节点链构建上下文路径。

        仅使用有名节点（有 text/content-desc/resource-id 后缀），避免混入无意义 wrapper。
        例： 'right_content > WLAN > toggle_switch'
        """
        parts: list[str] = []
        cursor: ET.Element | None = parent_map.get(node)
        max_depth = 6
        while cursor is not None and max_depth > 0:
            text = (cursor.get("text") or "").strip()
            desc = (cursor.get("content-desc") or "").strip()
            rid = (cursor.get("resource-id") or "").strip()
            tag: str = ""
            if text:
                tag = text[:20]
            elif desc:
                tag = desc[:20]
            elif rid:
                # 取 resource-id 的 "id/xxx" 后缀部分
                tag = rid.split("/")[-1] if "/" in rid else rid
            if tag:
                parts.append(tag)
            cursor = parent_map.get(cursor)
            max_depth -= 1
        if not parts:
            return ""
        # 从顶层根 → 当前节点
        return " > ".join(reversed(parts))

    def _merge_parent_with_switch_child(
        self,
        elements: list[UIElement],
        node_to_el: dict[ET.Element, UIElement],
        parent_map: dict[ET.Element, ET.Element],
    ) -> list[UIElement]:
        """父子去重: clickable 父容器 + 唯一可点击 Switch 子 → 合并为父。

        场景：WLAN Item 整行（LinearLayout clickable=true）里包装一个 Switch（clickable=true）。
        合并后仅保留父（点击区域大、更稳定），同时保留 Switch 的 associated_label、
        checked 状态。子 Switch 从列表中移除。
        """
        # 反向查询: el -> node
        el_to_node: dict[int, ET.Element] = {id(el): n for n, el in node_to_el.items()}

        # 阅历所有 clickable 容器，查看子节点中是否有唯一可点击 Switch
        to_remove: set[int] = set()
        for parent_el in elements:
            if not parent_el.clickable:
                continue
            if parent_el.text or parent_el.content_desc:
                continue  # 父本身有文本，不需要从子提升
            parent_node = el_to_node.get(id(parent_el))
            if parent_node is None:
                continue

            # 递归查找子树中的 Switch 控件
            switch_children: list[tuple[ET.Element, UIElement]] = []
            other_clickable_children: list[ET.Element] = []
            for descendant in parent_node.iter():
                if descendant is parent_node:
                    continue
                cls_lower = (descendant.get("class", "") or "").lower()
                is_switch = any(kw in cls_lower for kw in self._SWITCH_CLASSES)
                is_clickable_desc = descendant.get("clickable", "false") == "true"
                desc_el = node_to_el.get(descendant)
                if is_switch and desc_el is not None:
                    switch_children.append((descendant, desc_el))
                elif is_clickable_desc:
                    other_clickable_children.append(descendant)

            # 仅合并“唯一 Switch 子 + 无其他 clickable 后代”场景
            if len(switch_children) == 1 and not other_clickable_children:
                _, switch_el = switch_children[0]
                # 提升语义信息到父
                if not parent_el.associated_label and switch_el.associated_label:
                    parent_el.associated_label = switch_el.associated_label
                if parent_el.checked is None and switch_el.checked is not None:
                    parent_el.checked = switch_el.checked
                parent_el.has_switch_child = True
                # 移除子 Switch
                to_remove.add(id(switch_el))

        if not to_remove:
            return elements
        return [e for e in elements if id(e) not in to_remove]

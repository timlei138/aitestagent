from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from enum import Enum

import numpy as np

logger = logging.getLogger(__name__)


class AnomalyType(str, Enum):
    WHITE_SCREEN = "white_screen"
    BLACK_SCREEN = "black_screen"
    SOLID_SCREEN = "solid_screen"
    INCOMPLETE_DISPLAY = "incomplete_display"
    ANR = "anr"
    CRASH = "crash"
    PROCESS_LOST = "process_lost"
    LAYOUT_MISMATCH = "layout_mismatch"
    TEXT_MISMATCH = "text_mismatch"


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class Anomaly:
    type: AnomalyType
    severity: Severity
    description: str
    confidence: float = 1.0
    details: dict = field(default_factory=dict)

    def to_dict(self):
        data = asdict(self)
        data["type"] = self.type.value
        data["severity"] = self.severity.value
        return data


@dataclass
class DetectionResult:
    is_healthy: bool
    anomalies: list[Anomaly] = field(default_factory=list)
    page_key: str = ""
    detection_time_ms: float = 0.0

    @property
    def has_critical(self) -> bool:
        return any(a.severity == Severity.CRITICAL for a in self.anomalies)

    def to_dict(self):
        return {
            "is_healthy": self.is_healthy,
            "anomalies": [a.to_dict() for a in self.anomalies],
            "page_key": self.page_key,
            "detection_time_ms": self.detection_time_ms,
        }


class AnomalyDetector:
    def __init__(self, device, baseline_store=None, config=None):
        self.device = device
        self.baseline_store = baseline_store
        self.config = config

    def detect(
        self,
        app_package: str,
        page_key: str = "",
        screenshot=None,
        check_baseline: bool = True,
    ) -> DetectionResult:
        start = time.time()
        anomalies: list[Anomaly] = []
        screenshot = screenshot or self.device.screenshot()
        anomalies.extend(self._check_health(app_package))
        if not any(a.severity == Severity.CRITICAL for a in anomalies):
            # 先看是否有可识别 UI 元素；仅在元素缺失时再做白/黑屏判定，减少误报。
            if not self._has_meaningful_ui_elements():
                anomalies.extend(self._check_color(screenshot))
            if check_baseline and self.baseline_store:
                baseline = self._find_baseline(app_package, page_key, screenshot)
                if baseline:
                    anomalies.extend(self._check_baseline(screenshot, baseline))
        return DetectionResult(
            is_healthy=len(anomalies) == 0,
            anomalies=anomalies,
            page_key=page_key,
            detection_time_ms=(time.time() - start) * 1000,
        )

    def _check_health(self, app_package: str) -> list[Anomaly]:
        anomalies: list[Anomaly] = []
        try:
            current = self.device.current_app()
            expected_package = str(app_package or "").strip() or str(
                current.get("package", "") or ""
            ).strip()
            root = ET.fromstring(self.device.dump_hierarchy())
            texts = [node.get("text", "") for node in root.iter()]
            if any(
                any(k in t for k in ["无响应", "isn't responding", "ANR"])
                for t in texts
            ):
                anomalies.append(
                    Anomaly(AnomalyType.ANR, Severity.CRITICAL, "检测到 ANR 弹窗", 0.95)
                )
            if any(
                any(k in t for k in ["已停止运行", "keeps stopping", "has stopped"])
                for t in texts
            ):
                anomalies.append(
                    Anomaly(
                        AnomalyType.CRASH, Severity.CRITICAL, "检测到崩溃弹窗", 0.95
                    )
                )
            logger.info(
                "Anomaly health check expected_package=%s current_package=%s current_activity=%s hierarchy_package=%s",
                expected_package,
                current.get("package", ""),
                current.get("activity", ""),
                self._infer_foreground_package_from_hierarchy(root),
            )
            if (
                expected_package
                and current.get("package")
                and current.get("package") != expected_package
            ):
                recheck_delay = float(
                    getattr(self.config, "process_lost_recheck_delay", 0.4)
                )
                time.sleep(max(0.0, recheck_delay))
                stable = self.device.current_app()
                logger.info(
                    "Anomaly process_lost check expected=%s first=%s/%s stable=%s/%s hierarchy=%s recheck_delay=%.2f",
                    expected_package,
                    current.get("package", ""),
                    current.get("activity", ""),
                    stable.get("package", ""),
                    stable.get("activity", ""),
                    self._infer_foreground_package_from_hierarchy(root),
                    recheck_delay,
                )
                if stable.get("package") == expected_package:
                    return anomalies
                hierarchy_package = self._infer_foreground_package_from_hierarchy(root)
                if hierarchy_package and hierarchy_package == expected_package:
                    logger.info(
                        "Anomaly process_lost ignored by hierarchy package expected=%s hierarchy=%s",
                        expected_package,
                        hierarchy_package,
                    )
                    return anomalies
                anomalies.append(
                    Anomaly(
                        AnomalyType.PROCESS_LOST,
                        Severity.HIGH,
                        f"当前前台应用为 {stable.get('package') or current.get('package')}",
                        details={
                            "expected_package": expected_package,
                            "first_package": current.get("package", ""),
                            "first_activity": current.get("activity", ""),
                            "stable_package": stable.get("package", ""),
                            "stable_activity": stable.get("activity", ""),
                            "hierarchy_package": hierarchy_package or "",
                        },
                    )
                )
        except Exception as exc:
            anomalies.append(
                Anomaly(
                    AnomalyType.ANR, Severity.CRITICAL, f"无法获取 UI 树: {exc}", 0.7
                )
            )
        return anomalies

    def _check_color(self, screenshot) -> list[Anomaly]:
        white_threshold = getattr(self.config, "white_screen_threshold", 0.95)
        black_threshold = getattr(self.config, "black_screen_threshold", 0.95)
        arr = np.array(screenshot)
        white_ratio = float(np.mean(np.all(arr > 240, axis=2)))
        black_ratio = float(np.mean(np.all(arr < 15, axis=2)))
        anomalies: list[Anomaly] = []
        if white_ratio > white_threshold:
            anomalies.append(
                Anomaly(
                    AnomalyType.WHITE_SCREEN,
                    Severity.HIGH,
                    f"白屏比例 {white_ratio:.1%}",
                    white_ratio,
                )
            )
        if black_ratio > black_threshold:
            anomalies.append(
                Anomaly(
                    AnomalyType.BLACK_SCREEN,
                    Severity.HIGH,
                    f"黑屏比例 {black_ratio:.1%}",
                    black_ratio,
                )
            )
        if white_ratio <= white_threshold and black_ratio <= black_threshold:
            unique_colors = int(len(np.unique(arr.reshape(-1, arr.shape[-1]), axis=0)))
            if unique_colors < 10:
                anomalies.append(
                    Anomaly(
                        AnomalyType.SOLID_SCREEN,
                        Severity.MEDIUM,
                        f"疑似单色屏，颜色数 {unique_colors}",
                        0.8,
                    )
                )
        return anomalies

    def _find_baseline(self, app_package: str, page_key: str, screenshot):
        if page_key:
            baseline = self.baseline_store.load_page(app_package, page_key)
            if baseline:
                return baseline
        return self.baseline_store.find_best_match(
            app_package,
            screenshot,
            threshold=getattr(self.config, "phash_distance_low", 15),
        )

    def _check_baseline(self, screenshot, baseline) -> list[Anomaly]:
        anomalies: list[Anomaly] = []
        current_count = self._count_elements()
        if baseline.element_count:
            ratio = current_count / baseline.element_count
            if ratio < getattr(self.config, "critical_incomplete_ratio", 0.3):
                anomalies.append(
                    Anomaly(
                        AnomalyType.INCOMPLETE_DISPLAY,
                        Severity.HIGH,
                        f"元素严重减少 {current_count}/{baseline.element_count}",
                    )
                )
            elif ratio < getattr(self.config, "incomplete_display_ratio", 0.5):
                anomalies.append(
                    Anomaly(
                        AnomalyType.INCOMPLETE_DISPLAY,
                        Severity.MEDIUM,
                        f"元素减少 {current_count}/{baseline.element_count}",
                    )
                )

        # pHash 布局差异
        try:
            from imagehash import hex_to_hash, phash

            distance = phash(screenshot) - hex_to_hash(baseline.screenshot_phash)
            if distance > getattr(self.config, "phash_distance_medium", 20):
                anomalies.append(
                    Anomaly(
                        AnomalyType.LAYOUT_MISMATCH,
                        Severity.MEDIUM,
                        f"布局差异较大 pHash={distance}",
                    )
                )
        except Exception:
            pass

        # TEXT_MISMATCH: 检测关键文本缺失或变化
        anomalies.extend(self._check_text_mismatch(baseline))
        return anomalies

    def _check_text_mismatch(self, baseline) -> list[Anomaly]:
        """检测关键文本是否在基线和当前页面之间发生变化。"""
        anomalies: list[Anomaly] = []
        try:
            root = ET.fromstring(self.device.dump_hierarchy())
            current_texts: set[str] = set()
            for node in root.iter():
                text = node.get("text", "")
                desc = node.get("content-desc", "")
                if text:
                    current_texts.add(text.lower())
                if desc:
                    current_texts.add(desc.lower())

            # 收集基线中的关键可点击文本
            baseline_texts: set[str] = set()
            for el in baseline.elements:
                label = (
                    el.get("label") or el.get("text") or el.get("content_desc") or ""
                ).lower()
                if label and el.get("clickable"):
                    baseline_texts.add(label)

            # 基线中存在但当前缺失的关键文本
            missing = baseline_texts - current_texts
            key_missing = [t for t in missing if len(t) > 1]
            if len(key_missing) >= 3:
                anomalies.append(
                    Anomaly(
                        AnomalyType.TEXT_MISMATCH,
                        Severity.MEDIUM,
                        f"关键文本缺失: {key_missing[:5]}",
                        confidence=0.7,
                        details={"missing": key_missing},
                    )
                )
        except Exception:
            pass
        return anomalies

    def _count_elements(self) -> int:
        try:
            root = ET.fromstring(self.device.dump_hierarchy())
            return sum(
                1 for n in root.iter() if n.get("text", "") or n.get("content-desc", "")
            )
        except Exception:
            return 0

    def _has_meaningful_ui_elements(self) -> bool:
        try:
            root = ET.fromstring(self.device.dump_hierarchy())
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

    def _infer_foreground_package_from_hierarchy(self, root: ET.Element) -> str:
        counts: dict[str, int] = {}
        for node in root.iter("node"):
            pkg = str(node.get("package", "") or "").strip()
            if not pkg:
                continue
            counts[pkg] = counts.get(pkg, 0) + 1
        if not counts:
            return ""
        return sorted(counts.items(), key=lambda item: item[1], reverse=True)[0][0]

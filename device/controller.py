from __future__ import annotations

import base64
import logging
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from io import BytesIO
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_EXCLUDED_LAUNCHER_KEYWORDS = ("LeakLauncherActivity",)


class DeviceUnavailableError(RuntimeError):
    pass


@dataclass
class DeviceSnapshot:
    package: str
    activity: str
    width: int
    height: int
    image_base64: str
    hierarchy_xml: str


class DeviceController:
    def __init__(self, serial: str | None = None, auto_init: bool = True):
        try:
            import uiautomator2 as u2
        except ImportError as exc:
            raise DeviceUnavailableError(
                "uiautomator2 未安装，请先安装 requirements.txt"
            ) from exc

        self.device = u2.connect(serial) if serial else u2.connect()
        logger.info("Connected to device: %s", self.device)

        if auto_init:
            self._ensure_atx()

        try:
            _ = self.device.info
        except Exception as exc:
            logger.warning("Device info call failed (device may still be usable): %s", exc)
        self._last_current_app: dict[str, str] = {"package": "", "activity": ""}
        self._last_current_app_ts: float = 0.0
        self._last_current_app_log_sig: str = ""
        self._last_current_app_log_ts: float = 0.0
        # current_app 高频调用（预览/扫描/健康检测）时，短时间内复用结果，减少 adb dumpsys 压力。
        self._current_app_cache_ttl_sec: float = 2.0

    def _ensure_atx(self) -> None:
        """检测 ATX 服务是否运行，未安装则自动初始化。"""
        # 轻量探活：检查 atx-agent 是否在监听
        try:
            result = self.device.shell("curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:7912/version 2>/dev/null || echo 000")
            if "200" in str(result.output):
                logger.info("ATX agent is running (HTTP 200)")
                return
        except Exception:
            pass

        # 备选探活：ps 检查 atx-agent 进程
        try:
            result = self.device.shell("ps -A 2>/dev/null | grep atx-agent || ps 2>/dev/null | grep atx-agent || echo NOT_FOUND")
            if "atx-agent" in str(getattr(result, "output", result)):
                logger.info("ATX agent process found")
                return
        except Exception:
            pass

        logger.info("ATX agent not detected, auto-installing...")
        try:
            subprocess.run(
                [sys.executable, "-m", "uiautomator2", "init"],
                capture_output=True, text=True, timeout=120,
            )
            logger.info("ATX init completed — waiting for agent to start...")
            time.sleep(3)
        except Exception as exc:
            logger.warning("ATX auto-init failed: %s — device may still work if ATX is already present", exc)

    def app_start(
        self,
        package: str,
        activity: str | None = None,
        excluded_activity_keywords: list[str] | None = None,
    ) -> None:
        if not package:
            logger.warning("Device app_start skipped: empty package")
            return
        target_activity = (activity or "").strip()
        excluded = excluded_activity_keywords or list(
            DEFAULT_EXCLUDED_LAUNCHER_KEYWORDS
        )
        excluded_lower = [item.lower() for item in excluded if item]
        if target_activity and any(
            token in target_activity.lower() for token in excluded_lower
        ):
            logger.warning(
                "Device app_start override excluded explicit activity package=%s activity=%s excluded_keywords=%s",
                package,
                target_activity,
                excluded,
            )
            target_activity = ""
        if not target_activity:
            target_activity = (
                self.resolve_launch_activity(package, excluded_keywords=excluded) or ""
            )
        logger.info(
            "Device app_start package=%s activity=%s excluded_keywords=%s",
            package,
            target_activity or "<default>",
            excluded,
        )
        if target_activity:
            self.device.app_start(package, activity=target_activity)
            return
        self.device.app_start(package)

    def list_launcher_activities(self, package: str) -> list[str]:
        if not package:
            return []
        commands = [
            f"cmd package query-activities --brief -a android.intent.action.MAIN -c android.intent.category.LAUNCHER {package}",
            f"cmd package resolve-activity --brief {package}",
        ]
        matches: list[str] = []
        for cmd in commands:
            try:
                shell_result = self.device.shell(cmd)
                out = getattr(shell_result, "output", shell_result) or ""
            except Exception:
                continue
            for line in str(out).splitlines():
                line = line.strip()
                if "/" not in line:
                    continue
                token = line.split()[0]
                if not token.startswith(package + "/"):
                    continue
                component = token.split("/", 1)[1].strip()
                if component:
                    matches.append(component)
        # dumpsys 作为兜底（部分 ROM 不支持 query-activities）
        if not matches:
            try:
                shell_result = self.device.shell(f"dumpsys package {package}")
                out = getattr(shell_result, "output", shell_result) or ""
            except Exception:
                out = ""
            if out:
                pattern = re.compile(rf"{re.escape(package)}/([A-Za-z0-9_.$]+)")
                for item in pattern.findall(str(out)):
                    if item:
                        matches.append(item)
        unique: list[str] = []
        seen: set[str] = set()
        for item in matches:
            key = item.lower()
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        logger.info(
            "Device launcher activities package=%s candidates=%s", package, unique
        )
        return unique

    def resolve_launch_activity(
        self, package: str, excluded_keywords: list[str] | None = None
    ) -> str | None:
        candidates = self.list_launcher_activities(package)
        if not candidates:
            logger.info(
                "Device resolve launch activity package=%s selected=<none>", package
            )
            return None
        excludes = [
            k.lower()
            for k in (excluded_keywords or list(DEFAULT_EXCLUDED_LAUNCHER_KEYWORDS))
            if k
        ]
        if excludes:
            for activity in candidates:
                lowered = activity.lower()
                if any(token in lowered for token in excludes):
                    continue
                logger.info(
                    "Device resolve launch activity package=%s selected=%s excluded=%s",
                    package,
                    activity,
                    excludes,
                )
                return activity
        logger.info(
            "Device resolve launch activity package=%s selected=%s excluded=%s",
            package,
            candidates[0],
            excludes,
        )
        return candidates[0]

    def app_stop(self, package: str) -> None:
        if not package:
            return
        try:
            self.device.shell(f"am force-stop {package}")
        except Exception:
            self.device.app_stop(package)

    def current_app(self) -> dict[str, Any]:
        """
        获取当前前台应用
        优先读取 app_current，必要时回退 dumpsys；带短时缓存，避免高频调用与刷屏日志
        """
        now = time.monotonic()
        cache_ttl = float(getattr(self, "_current_app_cache_ttl_sec", 0.4) or 0.4)
        last_ts = float(getattr(self, "_last_current_app_ts", 0.0) or 0.0)
        if now - last_ts <= max(0.0, cache_ttl):
            return dict(
                getattr(self, "_last_current_app", {"package": "", "activity": ""})
            )

        package = ""
        activity = ""
        try:
            current = self.device.app_current() or {}
            package = str(current.get("package", "") or "").strip()
            activity = str(current.get("activity", "") or "").strip()
        except Exception as exc:
            logger.warning("current_app app_current failed: %s", exc)

        if not package:
            result = self._current_app_from_dumpsys()
            package = str(result.get("package", "") or "").strip()
            activity = str(result.get("activity", "") or "").strip()

        current_app = {"package": package, "activity": activity}
        self._last_current_app = current_app
        self._last_current_app_ts = now

        sig = f"{package}/{activity}"
        last_sig = str(getattr(self, "_last_current_app_log_sig", "") or "")
        last_log_ts = float(getattr(self, "_last_current_app_log_ts", 0.0) or 0.0)
        if sig != last_sig or (now - last_log_ts) >= 10.0:
            logger.info("current top app package=%s activity=%s", package, activity)
            self._last_current_app_log_sig = sig
            self._last_current_app_log_ts = now
        return current_app

    def _current_app_from_dumpsys(self) -> dict[str, str]:
        """
        通过 adb shell dumpsys 获取当前前台应用
        优先匹配 topResumedActivity，其次匹配 mResumedActivity
        """
        try:
            # 执行 dumpsys activity activities (数据最全，比 activity top 更稳定)
            output = self.device.shell(["dumpsys", "activity", "activities"]).output

            # 策略 1: 匹配 topResumedActivity (最精准，直接指向当前前台)
            # 示例: topResumedActivity=ActivityRecord{... u0 com.android.settings/.SubSettings ...}
            match = re.search(
                r"topResumedActivity=.*?u0 (?P<package>[^/]+)/(?P<activity>[^\s,]+)",
                output,
            )
            if match:
                return {
                    "package": match.group("package"),
                    "activity": match.group("activity"),
                }

            # 策略 2: 匹配 mResumedActivity (备选)
            # 示例: mResumedActivity: ActivityRecord{... u0 com.android.settings/.SubSettings ...}
            match = re.search(
                r"mResumedActivity:.*?u0 (?P<package>[^/]+)/(?P<activity>[^\s,]+)",
                output,
            )
            if match:
                return {
                    "package": match.group("package"),
                    "activity": match.group("activity"),
                }

            # 策略 3: 匹配 mCurrentFocus / mFocusedApp（窗口焦点）
            # 示例: mCurrentFocus=Window{42 u0 com.android.settings/.Settings}
            match = re.search(
                r"m(CurrentFocus|FocusedApp).*?u0 (?P<package>[^/]+)/(?P<activity>[^\s,}]+)",
                output,
            )
            if match:
                return {
                    "package": match.group("package"),
                    "activity": match.group("activity"),
                }

        except Exception as exc:
            logger.warning("Failed to get current app from dumpsys: %s", exc)

        return {"package": "", "activity": ""}

    def dump_hierarchy(self) -> str:
        return self.device.dump_hierarchy()

    def screenshot(self):
        return self.device.screenshot()

    def snapshot(self) -> DeviceSnapshot:
        image = self.screenshot()
        buf = BytesIO()
        image.save(buf, format="PNG")
        app = self.current_app()
        return DeviceSnapshot(
            package=app.get("package", ""),
            activity=app.get("activity", ""),
            width=image.width,
            height=image.height,
            image_base64=base64.b64encode(buf.getvalue()).decode("ascii"),
            hierarchy_xml=self.dump_hierarchy(),
        )

    def click_bounds(self, bounds: tuple[int, int, int, int]) -> None:
        left, top, right, bottom = bounds
        self.device.click((left + right) // 2, (top + bottom) // 2)

    def click_text(self, text: str, timeout: float = 2.0) -> bool:
        if self.device(text=text).exists(timeout=timeout):
            self.device(text=text).click()
            return True
        if self.device(description=text).exists(timeout=timeout):
            self.device(description=text).click()
            return True
        return False

    def click_resource_id(self, resource_id: str, timeout: float = 2.0) -> bool:
        if self.device(resourceId=resource_id).exists(timeout=timeout):
            self.device(resourceId=resource_id).click()
            return True
        return False

    def type_text(self, text: str) -> None:
        self.device.send_keys(text, clear=True)

    def press(self, key: str) -> None:
        self.device.press(key)

    def swipe(self, direction: str = "up", scale: float = 0.75) -> None:
        self.device.swipe_ext(direction, scale=scale)

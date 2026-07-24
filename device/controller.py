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

        try:
            self.device = u2.connect(serial) if serial else u2.connect()
        except Exception as exc:
            raise DeviceUnavailableError(f"ADB 设备未连接: {exc}") from exc
        logger.info("Connected to device: %s", self.device)

        if auto_init:
            self._ensure_atx()

        try:
            _ = self.device.info
        except Exception as exc:
            logger.warning(
                "Device info call failed (device may still be usable): %s", exc
            )
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
            result = self.device.shell(
                "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:7912/version 2>/dev/null || echo 000"
            )
            if "200" in str(result.output):
                logger.info("ATX agent is running (HTTP 200)")
                return
        except Exception:
            pass

        # 备选探活：ps 检查 atx-agent 进程
        try:
            result = self.device.shell(
                "ps -A 2>/dev/null | grep atx-agent || ps 2>/dev/null | grep atx-agent || echo NOT_FOUND"
            )
            if "atx-agent" in str(getattr(result, "output", result)):
                logger.info("ATX agent process found")
                return
        except Exception:
            pass

        # 打包模式下 sys.executable 指向 exe，无法运行 python -m
        import app_paths as _ap
        if _ap.FROZEN:
            logger.warning("Frozen mode: ATX agent not detected, skipping auto-init (use `python -m uiautomator2 init` manually)")
            return

        logger.info("ATX agent not detected, auto-installing...")
        try:
            subprocess.run(
                [sys.executable, "-m", "uiautomator2", "init"],
                capture_output=True,
                text=True,
                timeout=120,
            )
            logger.info("ATX init completed — waiting for agent to start...")
            time.sleep(3)
        except Exception as exc:
            logger.warning(
                "ATX auto-init failed: %s — device may still work if ATX is already present",
                exc,
            )

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

    def clear_app_data(self, package: str) -> str:
        """清理指定 App 的用户数据，等价于 ``adb shell pm clear <package>``。"""
        package = (package or "").strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+", package):
            raise ValueError(f"无效的 Android 包名: {package or '<empty>'}")

        result = self.device.shell(["pm", "clear", package])
        output = str(getattr(result, "output", result) or "").strip()
        exit_code = getattr(result, "exit_code", None)
        if exit_code not in (None, 0) or output.lower() != "success":
            detail = output or f"exit_code={exit_code}"
            raise RuntimeError(f"pm clear 执行失败: {detail}")
        logger.info("Device app data cleared package=%s", package)
        return output

    def open_app_permission_settings(self, package: str) -> str:
        """打开指定 App 的系统详情页，供人工或 Agent 继续处理权限。"""
        package = (package or "").strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+", package):
            raise ValueError(f"无效的 Android 包名: {package or '<empty>'}")

        result = self.device.shell(
            [
                "am",
                "start",
                "-a",
                "android.settings.APPLICATION_DETAILS_SETTINGS",
                "-d",
                f"package:{package}",
            ]
        )
        output = str(getattr(result, "output", result) or "").strip()
        exit_code = getattr(result, "exit_code", None)
        if exit_code not in (None, 0):
            detail = output or f"exit_code={exit_code}"
            raise RuntimeError(f"打开应用权限设置失败: {detail}")
        logger.info("Opened app settings package=%s", package)
        return output

    # ── 运行时权限经 adb 直接授予/撤销（绕过系统弹窗，解决弹窗 10s 超时自动消失）──
    _PERMISSION_ALIAS_MAP = {
        "camera": "android.permission.CAMERA",
        "fine_location": "android.permission.ACCESS_FINE_LOCATION",
        "location": "android.permission.ACCESS_FINE_LOCATION",
        "coarse_location": "android.permission.ACCESS_COARSE_LOCATION",
        "read_storage": "android.permission.READ_EXTERNAL_STORAGE",
        "write_storage": "android.permission.WRITE_EXTERNAL_STORAGE",
        "storage": "android.permission.READ_EXTERNAL_STORAGE",
        "read_calendar": "android.permission.READ_CALENDAR",
        "write_calendar": "android.permission.WRITE_CALENDAR",
        "calendar": "android.permission.READ_CALENDAR",
        "read_contacts": "android.permission.READ_CONTACTS",
        "write_contacts": "android.permission.WRITE_CONTACTS",
        "contacts": "android.permission.READ_CONTACTS",
        "microphone": "android.permission.RECORD_AUDIO",
        "phone": "android.permission.READ_PHONE_STATE",
        "sms": "android.permission.SEND_SMS",
        "notifications": "android.permission.POST_NOTIFICATIONS",
        "body_sensors": "android.permission.BODY_SENSORS",
        "bluetooth": "android.permission.BLUETOOTH_CONNECT",
        # Android 13+ 媒体权限（「选择照片 / 选择视频」类弹窗）
        "photos": "android.permission.READ_MEDIA_IMAGES",
        "images": "android.permission.READ_MEDIA_IMAGES",
        "media_images": "android.permission.READ_MEDIA_IMAGES",
        "select_photos": "android.permission.READ_MEDIA_IMAGES",
        "videos": "android.permission.READ_MEDIA_VIDEO",
        "video": "android.permission.READ_MEDIA_VIDEO",
        "media_video": "android.permission.READ_MEDIA_VIDEO",
        "select_videos": "android.permission.READ_MEDIA_VIDEO",
        "media_audio": "android.permission.READ_MEDIA_AUDIO",
        "audio": "android.permission.READ_MEDIA_AUDIO",
        "music": "android.permission.READ_MEDIA_AUDIO",
        # Android 14「仅允许访问所选照片/视频」（部分媒体访问）
        "visual_selected": "android.permission.READ_MEDIA_VISUAL_USER_SELECTED",
        "selected_media": "android.permission.READ_MEDIA_VISUAL_USER_SELECTED",
        "partial_media": "android.permission.READ_MEDIA_VISUAL_USER_SELECTED",
    }

    # pm grant 不支持的特殊权限（如悬浮窗/系统设置）→ 回退 cmd appops
    # 注意：只列 pm grant 确实不支持的特殊权限，普通运行时权限（CAMERA/CALENDAR 等）不在此列
    _APPOPS_FOR_PERMISSION = {
        "android.permission.SYSTEM_ALERT_WINDOW": "SYSTEM_ALERT_WINDOW",
        "android.permission.WRITE_SETTINGS": "WRITE_SETTINGS",
        "android.permission.PACKAGE_USAGE_STATS": "PACKAGE_USAGE_STATS",
    }

    _PKG_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+")

    def _resolve_permission_name(self, perm: str) -> str:
        """权限别名 → 完整权限名；已给完整名或短名则原样/补前缀。"""
        perm = (perm or "").strip()
        if not perm:
            raise ValueError("权限名不能为空")
        if perm in self._PERMISSION_ALIAS_MAP:
            return self._PERMISSION_ALIAS_MAP[perm]
        if perm.startswith("android.permission.") and "." in perm:
            return perm
        # 形如 READ_CALENDAR 的短名补前缀
        return f"android.permission.{perm}"

    def _set_permission_via_adb(
        self, package: str, permission: str, grant: bool
    ) -> str:
        """经 adb 授予/撤销运行时权限：先试 pm grant/revoke，失败回退 cmd appops。"""
        package = (package or "").strip()
        if not self._PKG_RE.fullmatch(package):
            raise ValueError(f"无效的 Android 包名: {package or '<empty>'}")
        perm = self._resolve_permission_name(permission)
        verb = "grant" if grant else "revoke"
        result = self.device.shell(["pm", verb, package, perm])
        output = str(getattr(result, "output", result) or "").strip()
        exit_code = getattr(result, "exit_code", None)
        pm_failed = (
            exit_code not in (None, 0)
            or "error" in output.lower()
            or "not a changeable permission" in output
        )
        if not pm_failed:
            return output or f"ok: pm {verb} {perm}"
        # pm 不支持（特殊权限/永久拒绝态）→ 回退 appops
        op = self._APPOPS_FOR_PERMISSION.get(perm)
        if op:
            mode = "allow" if grant else "deny"
            r2 = self.device.shell(["cmd", "appops", "set", package, op, mode])
            out2 = str(getattr(r2, "output", r2) or "").strip()
            if getattr(r2, "exit_code", None) in (None, 0):
                return out2 or f"ok: appops set {op} {mode}"
            raise RuntimeError(f"pm {verb} 失败({output}); appops 失败({out2})")
        raise RuntimeError(
            f"pm {verb} 失败: {output or 'exit_code=' + str(exit_code)}"
        )

    def grant_permission(self, package: str, permission: str) -> str:
        """经 adb `pm grant` 授予运行时权限（绕过系统弹窗）。"""
        msg = self._set_permission_via_adb(package, permission, grant=True)
        logger.info("Granted permission package=%s perm=%s", package, permission)
        return msg

    def revoke_permission(self, package: str, permission: str) -> str:
        """经 adb `pm revoke` 撤销运行时权限（等价于在弹窗点拒绝）。"""
        msg = self._set_permission_via_adb(package, permission, grant=False)
        logger.info("Revoked permission package=%s perm=%s", package, permission)
        return msg

    def current_app(self, refresh: bool = False) -> dict[str, Any]:
        """
        获取当前前台应用
        优先读取 app_current，必要时回退 dumpsys；带短时缓存，避免高频调用与刷屏日志。
        refresh=True 时绕过缓存，适用于短生命周期的系统弹窗检测。
        """
        now = time.monotonic()
        cache_ttl = float(getattr(self, "_current_app_cache_ttl_sec", 0.4) or 0.4)
        last_ts = float(getattr(self, "_last_current_app_ts", 0.0) or 0.0)
        if not refresh and now - last_ts <= max(0.0, cache_ttl):
            return dict(
                getattr(self, "_last_current_app", {"package": "", "activity": ""})
            )

        package = ""
        activity = ""
        try:
            result = self._current_app_from_dumpsys()
            package = str(result.get("package", "") or "").strip()
            activity = str(result.get("activity", "") or "").strip()
        except Exception as exc:
            logger.warning("current_app app_current failed: %s", exc)

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

    def long_click_bounds(self, bounds, duration: float = 0.8) -> None:
        """长按元素（swipe 同点模拟）。"""
        x, y = (bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2
        self.device.swipe(x, y, x, y, duration)

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

    def copy(self) -> str:
        """触发复制操作：查找并点击系统弹窗中的'复制'按钮。"""
        try:
            if self.device(text="复制").exists(timeout=1):
                self.device(text="复制").click()
                return "copy_popup"
        except Exception:
            pass
        # 兜底：KEYCODE_COPY (278)
        try:
            self.device.shell("input keyevent 278")
            return "copy_keyevent"
        except Exception:
            pass
        return "copy_unknown"

    def paste(self) -> str:
        """粘贴剪贴板内容。先尝试 CTRL+V，失败则通过长按触发系统弹窗点击'粘贴'。"""
        # 方案1: CTRL+V
        self.device.press("v", meta=True)
        # 方案2: KEYCODE_PASTE (279)
        try:
            self.device.shell("input keyevent 279")
        except Exception:
            pass
        # 方案3: 查找并点击系统弹窗中的"粘贴"按钮
        try:
            if self.device(text="粘贴").exists(timeout=1):
                self.device(text="粘贴").click()
                return "paste_popup"
        except Exception:
            pass
        return "paste_ctrl_v"

    def get_system_setting(self, key: str, namespace: str = "system") -> str:
        """读取 Android 系统设置（Settings.System / Secure / Global）。"""
        resp = self.device.shell(f"settings get {namespace} {key}")
        raw = (resp.output or "").strip() if hasattr(resp, "output") else str(resp).strip()
        return raw if raw and raw != "null" else ""

    def press(self, key: str) -> None:
        self.device.press(key)

    def swipe(self, direction: str = "up", scale: float = 0.75) -> None:
        self.device.swipe_ext(direction, scale=scale)

    def unlock(self) -> None:
        """解锁设备（swipe up 解锁，无密码时有效）。"""
        self.device.unlock()

    def screen_off(self) -> None:
        """关闭屏幕（等同于按电源键锁屏）。"""
        self.device.screen_off()

    def screen_on(self) -> None:
        """点亮屏幕（等同于按电源键亮屏）。"""
        self.device.screen_on()

    def set_orientation(self, orientation: str) -> None:
        """设置屏幕方向。orientation: natural(竖屏) / left(左横屏) / right(右横屏)。"""
        self.device.set_orientation(orientation)

    def freeze_rotation(self, freeze: bool) -> None:
        """冻结/解冻屏幕旋转。freeze=True 锁定当前方向。"""
        self.device.freeze_rotation(freeze)

    def open_notification(self) -> None:
        """打开通知栏（系统级操作，横屏时从顶部左侧下拉获取通知）。"""
        self.device.open_notification()

    def open_quick_settings(self) -> None:
        """打开快速设置面板（系统级操作，横屏时从顶部右侧下拉，竖屏时与通知栏同面板）。"""
        self.device.open_quick_settings()

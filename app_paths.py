"""
统一路径管理模块。

开发模式：数据目录 = 项目根目录下的 storage/、logs/
打包模式：数据目录 = %LOCALAPPDATA%\\AiAgentTest\\data、logs
          配置目录 = %LOCALAPPDATA%\\AiAgentTest\\config
          资源目录 = PyInstaller _MEIPASS 内的 frontend/dist、agents/prompts
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# ── 运行模式检测 ──
FROZEN = getattr(sys, "frozen", False)
MEIPASS = Path(getattr(sys, "_MEIPASS", "")) if FROZEN else None

if FROZEN and MEIPASS:
    BUNDLE_DIR = MEIPASS
    EXE_DIR = Path(sys.executable).parent
    APP_NAME = "AiAgentTest"
    APP_DATA = Path(os.environ["LOCALAPPDATA"]) / APP_NAME
else:
    BUNDLE_DIR = Path(__file__).resolve().parent
    EXE_DIR = BUNDLE_DIR
    APP_DATA = BUNDLE_DIR

# ── 数据目录（用户运行时产生）──
DATA_DIR = APP_DATA / "data" if FROZEN else APP_DATA / "storage"
LOG_DIR = APP_DATA / "logs" if FROZEN else APP_DATA / "logs"
CONFIG_DIR = APP_DATA / "config" if FROZEN else APP_DATA

# ── 数据子目录 ──
SCREENSHOT_DIR = DATA_DIR / "screenshots"
KNOWLEDGE_DIR = DATA_DIR / "knowledge"
DB_PATH = DATA_DIR / "test_history.db"
APPS_YAML = DATA_DIR / "apps.yaml"

# ── 日志子目录 ──
LOG_RUN_DIR = LOG_DIR / "runs"
SERVICE_LOG = LOG_DIR / "service.log"

# ── 配置文件 ──
CONFIG_YAML = CONFIG_DIR / "config.yaml"
CONFIG_LOCAL_YAML = CONFIG_DIR / "config.local.yaml"

# ── 打包资源路径（只读）──
FRONTEND_DIST_DIR = BUNDLE_DIR / "frontend" / "dist"
PROMPTS_DIR = BUNDLE_DIR / "agents" / "prompts"


def ensure_dirs() -> None:
    """确保所有运行时目录存在。启动时调用一次。"""
    for d in (DATA_DIR, LOG_DIR, LOG_RUN_DIR, SCREENSHOT_DIR, KNOWLEDGE_DIR):
        d.mkdir(parents=True, exist_ok=True)
    if FROZEN:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def get_config_yaml_path() -> str:
    """返回 config.yaml 的实际路径。"""
    if CONFIG_YAML.exists():
        return str(CONFIG_YAML)
    bundle_cfg = BUNDLE_DIR / "config.yaml"
    if bundle_cfg.exists():
        return str(bundle_cfg)
    return str(CONFIG_YAML)


def get_config_local_yaml_path() -> str:
    """返回 config.local.yaml 的实际路径。"""
    if CONFIG_LOCAL_YAML.exists():
        return str(CONFIG_LOCAL_YAML)
    bundle_local = BUNDLE_DIR / "config.local.yaml"
    if bundle_local.exists():
        return str(bundle_local)
    return str(CONFIG_LOCAL_YAML)


def get_apps_yaml_path() -> str:
    """返回 apps.yaml 的实际路径。"""
    if APPS_YAML.exists():
        return str(APPS_YAML)
    bundle_apps = BUNDLE_DIR / "storage" / "apps.yaml"
    if bundle_apps.exists():
        return str(bundle_apps)
    return str(APPS_YAML)


# ── 兼容旧代码的字符串路径 ──
STORAGE_DIR_STR = str(DATA_DIR)
DATA_DIR_STR = str(DATA_DIR)
LOG_DIR_STR = str(LOG_DIR)
LOG_RUN_DIR_STR = str(LOG_RUN_DIR)
SCREENSHOT_DIR_STR = str(SCREENSHOT_DIR)
KNOWLEDGE_DIR_STR = str(KNOWLEDGE_DIR)
DB_PATH_STR = str(DB_PATH)


# ── ADB 路径检测 ──
def setup_adb_path() -> str | None:
    """查找 adb.exe 并将其目录加入 PATH。

    搜索顺序:
    1. 已在 PATH 中
    2. 常见 Android SDK 目录
    3. 环境变量 ANDROID_HOME / ANDROID_SDK_ROOT

    返回 adb 完整路径，未找到返回 None。
    """
    import shutil

    # 1. 已在 PATH
    found = shutil.which("adb")
    if found:
        return found

    # 2. 常见位置
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Android" / "Sdk" / "platform-tools",
        Path(os.environ.get("USERPROFILE", "")) / "AppData" / "Local" / "Android" / "Sdk" / "platform-tools",
        Path("D:\\AndroidSdk") / "platform-tools",
        Path("C:\\AndroidSdk") / "platform-tools",
        Path("C:\\android-sdk") / "platform-tools",
    ]

    # 3. 环境变量
    for env_key in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        val = os.environ.get(env_key)
        if val:
            candidates.insert(0, Path(val) / "platform-tools")

    for p in candidates:
        adb = p / "adb.exe"
        if adb.is_file():
            adb_dir = str(p)
            adb_str = str(adb)
            os.environ["PATH"] = adb_dir + os.pathsep + os.environ.get("PATH", "")
            # uiautomator2 / adbutils 依赖这些变量
            os.environ.setdefault("ADBUTILS_ADB_PATH", adb_str)
            sdk_root = str(p.parent)  # platform-tools 的父目录即 SDK root
            os.environ.setdefault("ANDROID_HOME", sdk_root)
            os.environ.setdefault("ANDROID_SDK_ROOT", sdk_root)
            return adb_str

    return None

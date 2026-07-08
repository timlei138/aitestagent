"""
桌面应用入口：启动 FastAPI 服务 + 打开 pywebview 原生窗口。

打包后双击 .exe 即可运行，无需手动启动浏览器。
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time

# ── console=False 时 sys.stdout/stderr 为 None，需重定向避免崩溃 ──
import os as _os
if sys.stdout is None:
    sys.stdout = open(_os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(_os.devnull, "w")

# ── 全局 subprocess 补丁：console=False 时阻止子进程弹出控制台窗口 ──
if sys.platform == "win32":
    _CREATE_NO_WINDOW = 0x08000000
    _orig_Popen = subprocess.Popen

    class _SilentPopen(_orig_Popen):
        def __init__(self, *args, **kwargs):
            if kwargs.get("creationflags", 0) & _CREATE_NO_WINDOW == 0:
                kwargs["creationflags"] = kwargs.get("creationflags", 0) | _CREATE_NO_WINDOW
            super().__init__(*args, **kwargs)

    subprocess.Popen = _SilentPopen

import app_paths

# ── 调试输出：确认路径模式 ──
print(f"[app_paths] FROZEN={app_paths.FROZEN}, MEIPASS={app_paths.MEIPASS}")
print(f"[app_paths] BUNDLE_DIR={app_paths.BUNDLE_DIR}")
print(f"[app_paths] APP_DATA={app_paths.APP_DATA}")
print(f"[app_paths] DATA_DIR={app_paths.DATA_DIR}")
print(f"[app_paths] LOG_DIR={app_paths.LOG_DIR}")

# 在导入任何 ML 库之前设置离线模式（模型已缓存到 ~/.cache/huggingface）
import os as _os
_cache_dir = _os.path.join(
    _os.path.expanduser("~"), ".cache", "huggingface", "hub",
    "models--BAAI--bge-large-zh-v1.5"
)
if _os.path.isdir(_cache_dir):
    _os.environ["HF_HUB_OFFLINE"] = "1"
    _os.environ["TRANSFORMERS_OFFLINE"] = "1"

# 查找 adb 并加入 PATH
_adb = app_paths.setup_adb_path()
if _adb:
    print(f"[app_entry] ADB found: {_adb}")
    print(f"[app_entry] ANDROID_HOME={_os.environ.get('ANDROID_HOME', 'NOT SET')}")
else:
    print("[app_entry] WARNING: adb not found, device features will not work")

# 文件日志：写入 AppData，即使无控制台也可见
app_paths.ensure_dirs()
_file_handler = logging.FileHandler(
    str(app_paths.LOG_DIR / "app_entry.log"), encoding="utf-8", mode="w"
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
logging.getLogger().addHandler(_file_handler)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("app_entry")

HOST = "127.0.0.1"
PORT = 8080
TITLE = "AI 自动化测试 Agent"
WINDOW_WIDTH = 1400
WINDOW_HEIGHT = 900


def _start_server():
    """在子线程中启动 FastAPI 服务。"""
    import uvicorn
    logger.info("正在导入 server 模块…")
    try:
        from api.server import app
    except Exception:
        logger.exception("server 模块导入失败")
        return
    logger.info("server 模块导入完成，正在启动 uvicorn…")

    app_paths.ensure_dirs()
    logger.info("Starting FastAPI server on %s:%d", HOST, PORT)
    try:
        uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
    except Exception:
        logger.exception("uvicorn 启动失败")


def _wait_for_server(timeout: float = 120.0) -> bool:
    """等待服务就绪。"""
    import urllib.request

    url = f"http://{HOST}:{PORT}/api/health"
    start = time.time()
    while time.time() - start < timeout:
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except Exception:
            time.sleep(0.3)
    logger.error("Server did not start within %ds", timeout)
    return False


def main():
    app_paths.ensure_dirs()
    logger.info("启动服务线程…")

    server_thread = threading.Thread(target=_start_server, daemon=True)
    server_thread.start()

    logger.info("等待服务就绪 (timeout=120s)...")
    if not _wait_for_server():
        logger.error("无法启动服务，请检查端口 %d 是否被占用", PORT)
        try:
            input("按 Enter 退出...")
        except (RuntimeError, EOFError):
            pass
        sys.exit(1)

    logger.info("服务已就绪，正在创建窗口…")
    try:
        import webview
        logger.info("pywebview 导入成功，创建窗口…")

        window = webview.create_window(
            TITLE,
            url=f"http://{HOST}:{PORT}",
            width=WINDOW_WIDTH,
            height=WINDOW_HEIGHT,
            min_size=(800, 600),
            resizable=True,
            text_select=True,
        )
        logger.info("窗口已创建，启动 webview 事件循环…")
        webview.start(debug=False)
        logger.info("webview 事件循环结束")
        # 窗口关闭后清理 adb 子进程
        try:
            subprocess.run(["adb", "kill-server"], capture_output=True, timeout=5)
            logger.info("adb server killed")
        except Exception:
            pass
    except ImportError:
        logger.warning("pywebview 未安装，自动打开浏览器")
        import webbrowser

        webbrowser.open(f"http://{HOST}:{PORT}")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    except Exception:
        logger.exception("窗口启动失败")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        logger.exception("Fatal error in main")
        # 也写入文件
        try:
            with open(str(app_paths.LOG_DIR / "app_entry_crash.log"), "w", encoding="utf-8") as f:
                traceback.print_exc(file=f)
        except Exception:
            pass
        # 有控制台时显示
        try:
            traceback.print_exc()
            input("发生错误，按 Enter 退出...")
        except Exception:
            pass

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

# ── 关键：切换工作目录到安全位置，避免子进程继承 PyInstaller 临时目录 ──
# console=False 时子进程脱离控制台，不会被系统自动清理，若 CWD 指向
# _MEIxxxxx 临时目录，会导致目录被锁住无法删除、弹出"adb 占用"提示。
if getattr(sys, "frozen", False):
    _safe_cwd = _os.path.expanduser("~")
    try:
        _os.chdir(_safe_cwd)
        print(f"[app_entry] CWD changed to {_safe_cwd}")
    except Exception:
        pass

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

# 文件日志：写入 AppData + stderr，即使无控制台也可见
app_paths.ensure_dirs()
_log_fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")

# 文件 handler（行缓冲，确保立即写入）
_log_path = str(app_paths.LOG_DIR / "app_entry.log")
_file_handler = logging.FileHandler(_log_path, encoding="utf-8", mode="w")
_file_handler.setFormatter(_log_fmt)
logging.getLogger().addHandler(_file_handler)

# stderr handler（控制台即时可见）
_stderr_handler = logging.StreamHandler(sys.stderr)
_stderr_handler.setFormatter(_log_fmt)
logging.getLogger().addHandler(_stderr_handler)

logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger("app_entry")

HOST = "127.0.0.1"
PORT = 8080
TITLE = "AI 自动化测试 Agent"
WINDOW_WIDTH = 1400
WINDOW_HEIGHT = 900
APP_URL = f"http://{HOST}:{PORT}"
HEALTH_URL = f"{APP_URL}/api/health"

# ── 启动加载页：纯静态，由 Python 侧轮询服务就绪后跳转 ──
# 注意：不能靠 JS fetch 轮询，WebView2 中 html= 加载的页面 origin 为 null，
# 对 localhost 的请求会被跨域拦截。
_LOADING_HTML = r"""
<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    display: flex; justify-content: center; align-items: center;
    height: 100vh; background: #1a1a2e; color: #e0e0e0;
    font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
  }
  .splash { text-align: center; }
  .spinner {
    width: 48px; height: 48px; margin: 0 auto 24px;
    border: 4px solid rgba(255,255,255,.15);
    border-top-color: #4fc3f7; border-radius: 50%;
    animation: spin .8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  h2 { font-size: 20px; font-weight: 500; margin-bottom: 8px; }
  p { font-size: 14px; color: #999; }
  .progress { margin-top: 32px; width: 180px; height: 3px; background: rgba(255,255,255,.1); border-radius: 2px; overflow: hidden; }
  .progress-bar { height: 100%; width: 0%; background: #4fc3f7; border-radius: 2px; transition: width .5s ease; }
  #error { color: #ef5350; display: none; margin-top: 16px; font-size: 13px; }
</style>
</head>
<body>
<div class="splash">
  <div class="spinner"></div>
  <h2>AI 自动化测试 Agent</h2>
  <p id="status">正在初始化服务...</p>
  <div class="progress"><div class="progress-bar" id="bar"></div></div>
  <p id="error">服务启动超时，请重启应用</p>
</div>
</body>
</html>
"""


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


def _make_poll_server():
    """返回一个后台轮询函数，服务就绪后自动跳转到应用页面。"""
    import urllib.request
    import webview as _wv

    MAX_RETRIES = 400       # 400 × 0.3s ≈ 120s
    INTERVAL = 0.3

    def _poll():
        logger.info("后台轮询服务就绪 (timeout=%.0fs)...", MAX_RETRIES * INTERVAL)
        for i in range(MAX_RETRIES):
            try:
                urllib.request.urlopen(HEALTH_URL, timeout=1)
                logger.info("服务已就绪 (第 %d 次轮询)，跳转到 %s", i + 1, APP_URL)
                _wv.windows[0].load_url(APP_URL)
                return
            except Exception:
                pass

            # 非线性进度：平方根曲线，前期快后期慢，感知更流畅
            ratio = (i + 1) / MAX_RETRIES
            pct = int(min(95, ratio ** 0.35 * 100))
            try:
                _wv.windows[0].evaluate_js(
                    f'document.getElementById("bar").style.width="{pct}%";'
                )
            except Exception:
                pass
            time.sleep(INTERVAL)

        # 超时
        logger.error("服务启动超时 (%ds)", int(MAX_RETRIES * INTERVAL))
        try:
            _wv.windows[0].evaluate_js(
                'document.getElementById("status").textContent="服务启动超时";'
                'document.getElementById("error").style.display="block";'
            )
        except Exception:
            pass

    return _poll


def main():
    app_paths.ensure_dirs()
    logger.info("启动服务线程…")

    server_thread = threading.Thread(target=_start_server, daemon=True)
    server_thread.start()

    try:
        import webview
    except ImportError:
        logger.warning("pywebview 未安装，自动打开浏览器")
        import webbrowser
        webbrowser.open(APP_URL)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        return

    logger.info("pywebview 导入成功，立即显示加载窗口（服务后台初始化中）…")
    _t0 = _t1 = _t2 = 0.0
    try:
        webview.create_window(
            TITLE,
            html=_LOADING_HTML,
            width=WINDOW_WIDTH,
            height=WINDOW_HEIGHT,
            min_size=(800, 600),
            resizable=True,
            text_select=True,
        )
        logger.info("窗口已创建，启动 webview 事件循环…")
        webview.start(func=_make_poll_server(), debug=False)
        _t0 = time.time()
        logger.info("webview 事件循环结束")

        # ── 计时：定位退出延迟 ──
        _t1 = time.time()
        logger.info("[shutdown] 开始清理 (webview.start 返回耗时 %.1fs)", _t1 - _t0)

        try:
            from api.server import shutdown_adb
            shutdown_adb()
        except Exception:
            logger.exception("shutdown_adb 失败")

        _t2 = time.time()
        logger.info("[shutdown] 清理完成 (耗时 %.1fs, 总计 %.1fs)", _t2 - _t1, _t2 - _t0)
    except Exception:
        logger.exception("窗口启动失败")

    logger.info("[shutdown] main() 即将返回 (%.1fs since window close)", time.time() - _t0)
    # 强制刷盘
    for _h in logging.getLogger().handlers:
        try:
            _h.flush()
        except Exception:
            pass
    # 跳过 Python 退出阶段的非 daemon 线程等待（uvicorn 线程池等会阻塞 ~5s）
    _os._exit(0)


if __name__ == "__main__":
    _main_t0 = time.time()
    try:
        main()
        _main_t1 = time.time()
        logger.info("[shutdown] main() 已返回 (耗时 %.1fs)", _main_t1 - _main_t0)
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
    # 确保所有日志刷盘
    logging.shutdown()

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import threading
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.types import ASGIApp, Scope, Receive, Send
from starlette.responses import JSONResponse

from api.device_routes import router as device_router
from api.device_routes import set_runner as set_device_runner
from api.device_routes import _get_device as _api_get_device
from api.apps_routes import router as apps_router
from api.apps_routes import resolve_app as _resolve_app_from_yaml
from api.knowledge_routes import router as knowledge_router
from api.config_routes import router as config_router
from api.test_cases_routes import router as test_cases_router
from api.test_cases_routes import (
    set_backends as _set_tc_backends,
    _resolve_run_entry,
    resolve_report_rerun_entry,
)
from api.knowledge_routes import set_knowledge_base as _set_kb_for_routes
from api.websocket_manager import WebSocketManager
from config import TestConfig, resolve_perception_mode
from data import create_vector_store, create_relational_db
from device.controller import DeviceController, DeviceUnavailableError
from agents.graph import set_relational_db, set_ws_emit_callback
from data.knowledge import KnowledgeBase
from agents.orchestrator import TestOrchestrator
from device.perceiver import SmartPerceiver
from llm.multimodal import multimodal_vision_call, reset_vision_capability_state
from tools.context import ToolContext
from tools import set_tool_context

import app_paths

BASE_DIR = app_paths.DATA_DIR
FRONTEND_DIST_DIR = app_paths.FRONTEND_DIST_DIR
INDEX_FILE = FRONTEND_DIST_DIR / "index.html"
PROJECT_ROOT = app_paths.BUNDLE_DIR

app = FastAPI(title="AI 自动化测试 Agent")

# ── 安全：持久化 Token，服务重启后复用，避免浏览器旧 cookie 被 403 ──
_TOKEN_FILE = app_paths.DATA_DIR / ".auth_token"
if _TOKEN_FILE.exists():
    AUTH_TOKEN = _TOKEN_FILE.read_text(encoding="utf-8").strip()
else:
    AUTH_TOKEN = secrets.token_hex(32)
    app_paths.ensure_dirs()
    _TOKEN_FILE.write_text(AUTH_TOKEN, encoding="utf-8")
AUTH_COOKIE_NAME = "auth_token"

# ── CORS：仅允许同源请求（浏览器跨域防护）──
_APP_PORT = os.environ.get("APP_PORT", "8080")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[f"http://127.0.0.1:{_APP_PORT}"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Auth 中间件：验证 Cookie Token，拦截未授权请求 ──
class _AuthMiddleware:
    """纯 ASGI 中间件，兼容 SSE 流式响应和静态文件。"""

    __slots__ = ("app",)

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")

        # 白名单：首页（设置 Cookie）和健康检查
        if path in ("/", "/api/health"):
            await self.app(scope, receive, send)
            return

        # 解析 Cookie
        cookies: dict[str, str] = {}
        for header_name, header_value in scope.get("headers", []):
            if header_name == b"cookie":
                for part in header_value.decode("latin-1").split("; "):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        cookies[k] = v
                break

        if cookies.get(AUTH_COOKIE_NAME) != AUTH_TOKEN:
            resp = JSONResponse({"detail": "Unauthorized"}, status_code=401)
            await resp(scope, receive, send)
            return

        await self.app(scope, receive, send)


app.add_middleware(_AuthMiddleware)

config = TestConfig.from_yaml(app_paths.get_config_yaml_path())
ws_manager = WebSocketManager()

# ── 初始化工具上下文 ──
_device: DeviceController | None = None
_perceiver: SmartPerceiver | None = None
_kb: KnowledgeBase | None = None
_ctx: ToolContext | None = None


def _build_vision_call(cfg: TestConfig):
    def _vision_call(prompt: str, image_base64: str, purpose: str, strict_json: bool):
        return multimodal_vision_call(
            prompt=prompt,
            image_base64=image_base64,
            purpose=purpose,
            strict_json=strict_json,
            provider=cfg.llm_provider,
            model=cfg.model,
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            vision_enabled=cfg.vision_enabled,
            timeout_sec=12,
        )

    return _vision_call


def _bind_last_screenshot_path(path: str) -> None:
    global _ctx
    if _ctx is not None:
        _ctx._last_screenshot_path = path


def rebuild_perceiver() -> dict:
    """仅按当前配置重建 perceiver/context，不主动重连设备。"""
    global _perceiver

    if _device is None:
        _perceiver = None
        _rebuild_tool_context()
        return {"rebuilt": False, "detail": "device offline"}

    try:
        reset_vision_capability_state()
        mode, auto_switch = resolve_perception_mode(config)
        _perceiver = SmartPerceiver(
            _device,
            vision_call=_build_vision_call(config),
            screenshot_sink=_bind_last_screenshot_path,
            mode=mode,
            auto_switch=auto_switch,
        )
        logging.getLogger(__name__).info(
            "SmartPerceiver rebuilt (mode=%s)", _perceiver.mode
        )
    except Exception as exc:
        _perceiver = None
        logging.getLogger(__name__).warning("SmartPerceiver rebuild failed: %s", exc)

    _rebuild_tool_context()
    return {"rebuilt": _perceiver is not None, "detail": "ok"}


# 1) 设备连接
try:
    _device = DeviceController()
    logging.getLogger(__name__).info("Android device connected")
except (DeviceUnavailableError, Exception) as exc:
    logging.getLogger(__name__).warning("Android device NOT connected: %s", exc)

# 2) 感知器（设备在线时才创建）
if _device is not None:
    try:
        reset_vision_capability_state()
        mode, auto_switch = resolve_perception_mode(config)
        _perceiver = SmartPerceiver(
            _device,
            vision_call=_build_vision_call(config),
            screenshot_sink=_bind_last_screenshot_path,
            mode=mode,
            auto_switch=auto_switch,
        )
        logging.getLogger(__name__).info(
            "SmartPerceiver initialized (mode=%s)", _perceiver.mode
        )
    except Exception as exc:
        logging.getLogger(__name__).warning("SmartPerceiver init failed: %s", exc)

# 3) 知识库（始终可用）
_kb = KnowledgeBase(create_vector_store(config))

_ctx = ToolContext(
    device=_device,
    perceiver=_perceiver,
    knowledge_base=_kb,
    safety_level=config.safety_level,
    llm_provider=config.llm_provider,
    llm_model=config.model,
    llm_api_key=config.api_key,
    llm_base_url=config.base_url,
    llm_vision_enabled=config.vision_enabled,
    click_mode=config.click_mode,
)
set_tool_context(_ctx)


def _cleanup_old_screenshots(keep_runs: int = 20):
    """保留最近 N 个 run 的截图目录（按 mtime 排序），删除更早的。"""
    import os
    import shutil

    base = app_paths.SCREENSHOT_DIR_STR
    if not os.path.isdir(base):
        return
    # 只清理符合 run_id 命名规则的目录（必须含连字符，排除纯名称手工目录）
    _run_dir_re = re.compile(r"^[A-Za-z0-9_\-]*-[A-Za-z0-9_\-]*$")
    dirs = [
        (d, os.path.getmtime(os.path.join(base, d)))
        for d in os.listdir(base)
        if os.path.isdir(os.path.join(base, d)) and _run_dir_re.match(d)
    ]
    dirs.sort(key=lambda x: x[1], reverse=True)
    for old_dir, _ in dirs[keep_runs:]:
        shutil.rmtree(os.path.join(base, old_dir), ignore_errors=True)


# 启动时确保目录存在 + 清理旧截图
app_paths.ensure_dirs()
_cleanup_old_screenshots()


def _get_relational_db():
    """获取关系型数据库实例。"""
    from agents.graph import _relational_db

    return _relational_db


def _rebuild_tool_context() -> None:
    """重新构建 ToolContext 并更新全局引用。"""
    global _ctx
    _ctx = ToolContext(
        device=_device,
        perceiver=_perceiver,
        knowledge_base=_kb,
        safety_level=config.safety_level,
        llm_provider=config.llm_provider,
        llm_model=config.model,
        llm_api_key=config.api_key,
        llm_base_url=config.base_url,
        llm_vision_enabled=config.vision_enabled,
        click_mode=config.click_mode,
    )
    set_tool_context(_ctx)


def reconnect_device() -> dict:
    """尝试重连设备并重建感知器，返回状态字典。"""
    global _device, _perceiver
    try:
        _device = DeviceController()  # auto_init=True 已自动检测并安装 ATX
        logging.getLogger(__name__).info("Device reconnected")
    except DeviceUnavailableError as exc:
        _device = None
        _perceiver = None
        _rebuild_tool_context()
        return {"connected": False, "detail": str(exc)}

    try:
        reset_vision_capability_state()
        mode, auto_switch = resolve_perception_mode(config)
        _perceiver = SmartPerceiver(
            _device,
            vision_call=_build_vision_call(config),
            screenshot_sink=_bind_last_screenshot_path,
            mode=mode,
            auto_switch=auto_switch,
        )
        logging.getLogger(__name__).info(
            "SmartPerceiver reinitialized (mode=%s)", _perceiver.mode
        )
    except Exception as exc:
        _perceiver = None
        logging.getLogger(__name__).warning(
            "SmartPerceiver init failed during reconnect: %s", exc
        )

    _rebuild_tool_context()
    return {"connected": True, "detail": "设备重连成功"}


# ── 编排器 ──
orchestrator = TestOrchestrator(config)
set_device_runner(orchestrator)

# ── 关系型数据库 ──
_db = create_relational_db(config)
set_relational_db(_db)
_set_tc_backends(orchestrator, _db)

# ── 事件广播 ──
orchestrator.set_event_callback(
    lambda event_type, payload: ws_manager.broadcast_sync(event_type, payload)
)
set_ws_emit_callback(lambda t, p: ws_manager.broadcast_sync(t, p))


# ── USB 热插拔监听 ──
_usb_monitor_proc: subprocess.Popen | None = None
_usb_monitor_stop = threading.Event()
_shutdown_done = False


def shutdown_adb() -> None:
    """应用退出前清理所有 ADB 相关资源。可重复调用，只执行一次。"""
    import subprocess
    import time as _time

    global _shutdown_done
    if _shutdown_done:
        return
    _shutdown_done = True

    _log = logging.getLogger(__name__)
    _t0 = _time.time()

    # 0) 设置停止标志，阻止 USB 监控线程重启 adb
    _usb_monitor_stop.set()
    _log.info("[shutdown] step0 stop_flag set (%.2fs)", _time.time() - _t0)

    # 1) 释放设备引用（不调 close()，避免 TCP 关闭等待超时）
    global _device
    _device = None
    _log.info("[shutdown] step1 device released (%.2fs)", _time.time() - _t0)

    # 2) 终止 adb track-devices 监控子进程
    global _usb_monitor_proc
    if _usb_monitor_proc is not None:
        try:
            _usb_monitor_proc.kill()
        except Exception:
            pass
        _usb_monitor_proc = None
    _log.info("[shutdown] step2 monitor killed (%.2fs)", _time.time() - _t0)

    # 3) Windows: taskkill 强杀所有 adb.exe
    import sys as _sys
    if _sys.platform == "win32":
        try:
            subprocess.Popen(  # fire-and-forget，不等结果
                ["taskkill", "/F", "/IM", "adb.exe"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass
    else:
        try:
            subprocess.run(
                ["adb", "kill-server"],
                capture_output=True,
                timeout=2,
            )
        except Exception:
            pass
    _log.info("[shutdown] step3 taskkill fired (%.2fs total)", _time.time() - _t0)


def _start_usb_monitor() -> None:
    """后台线程：adb track-devices 实时监听 USB 插拔，毫秒级响应。"""
    import subprocess
    import atexit

    atexit.register(shutdown_adb)

    def _monitor():
        global _device, _perceiver, _usb_monitor_proc
        logger = logging.getLogger(__name__)
        prev_has_device = _device is not None
        try:
            _usb_monitor_proc = subprocess.Popen(
                ["adb", "track-devices"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
            for line in _usb_monitor_proc.stdout:
                if _usb_monitor_stop.is_set():
                    break
                line = line.strip()
                if not line:
                    continue
                has_device = "\tdevice" in line and "offline" not in line
                if has_device != prev_has_device:
                    prev_has_device = has_device
                    if has_device:
                        logger.info("USB device connected via ADB track-devices")
                        reconnect_device()
                    else:
                        logger.info("USB device disconnected via ADB track-devices")
                        _device = None
                        _perceiver = None
                        _rebuild_tool_context()
                    try:
                        ws_manager.broadcast_sync(
                            "device_status_change",
                            {"connected": has_device},
                        )
                    except Exception:
                        pass
        except Exception as exc:
            logger.warning("adb track-devices error: %s", exc)

    t = threading.Thread(target=_monitor, daemon=True, name="usb-monitor")
    t.start()


_start_usb_monitor()

app.include_router(device_router)
app.include_router(apps_router)
app.include_router(knowledge_router)
app.include_router(config_router)
app.include_router(test_cases_router)
_set_kb_for_routes(_kb)


class RunRequest(BaseModel):
    message: str
    session_id: str = "default"


class HumanDecisionRequest(BaseModel):
    thread_id: str
    decision: Any


class StopRunRequest(BaseModel):
    thread_id: str


class IdentityConfirmRequest(BaseModel):
    identities: list[dict]  # [{target, resource_id, class_name, role, ...}]


@app.post("/api/run")
async def run_test(request: RunRequest):
    """一步式执行（自动解析意图 + 执行）。"""
    logging.getLogger(__name__).info(
        "[stop-debug] HTTP /api/run hit, msg=%r", request.message[:60]
    )
    # 设备连接前置检查
    if _device is None:
        return {
            "status": "device_offline",
            "message": "Android 设备未连接，请检查 USB/ADB 连接后重试",
        }
    ws_manager.bind_loop(asyncio.get_running_loop())
    logging.getLogger(__name__).info(
        "[stop-debug] HTTP /api/run bind_loop done, -> orchestrator.start"
    )

    # 解析 app_package
    app_package, app_name = _quick_resolve_app(request.message)

    result = await asyncio.to_thread(
        orchestrator.start,
        user_request=request.message,
        app_package=app_package,
        app_name=app_name,
    )
    logging.getLogger(__name__).info(
        "[stop-debug] HTTP /api/run orchestrator.start returned, status=%s tid=%s",
        result.get("status"),
        result.get("thread_id"),
    )
    return {"status": result.get("status", "error"), "data": result}


@app.post("/api/run/stream")
async def run_test_stream(request: RunRequest):
    """流式执行 — Server-Sent Events。"""
    if _device is None:

        async def offline_stream():
            yield f"data: {json.dumps({'type': 'error', 'content': 'Android 设备未连接，请检查 USB/ADB 连接后重试'}, ensure_ascii=False)}\n\n"

        return StreamingResponse(offline_stream(), media_type="text/event-stream")
    ws_manager.bind_loop(asyncio.get_running_loop())
    app_package, app_name = _quick_resolve_app(request.message)

    async def event_generator():
        async for event in orchestrator.start_stream(
            user_request=request.message,
            app_package=app_package,
            app_name=app_name,
        ):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post("/api/human_decision")
async def human_decision(request: HumanDecisionRequest):
    """人工确认后恢复执行。"""
    ws_manager.bind_loop(asyncio.get_running_loop())
    if not request.thread_id:
        return {"status": "error", "message": "缺少 thread_id"}

    result = await asyncio.to_thread(
        orchestrator.resume,
        thread_id=request.thread_id,
        decision=request.decision,
    )
    return {"status": result.get("status", "error"), "data": result}


@app.post("/api/run/stop")
async def stop_run(request: StopRunRequest):
    """手动停止一个正在执行的 run（HTTP 路径）。

    幂等：未知 thread_id 或已结束的 run 都会返回 noop，不抛错。
    """
    logging.getLogger(__name__).info(
        "[stop-debug] HTTP /api/run/stop hit, thread_id=%s", request.thread_id
    )
    if not request.thread_id:
        return {"status": "error", "message": "缺少 thread_id"}
    ok = orchestrator.request_stop(request.thread_id, reason="http_stop")
    logging.getLogger(__name__).info(
        "[stop-debug] HTTP /api/run/stop -> ok=%s", ok
    )
    return {
        "status": "ok" if ok else "noop",
        "thread_id": request.thread_id,
    }


@app.post("/api/element_identities/confirm")
async def confirm_element_identities(request: IdentityConfirmRequest):
    """确认 Level2 元素身份映射，写入 SQLite。"""
    from data import create_relational_db
    from agents.graph import set_relational_db, _relational_db

    db = _relational_db
    if not db:
        from config import TestConfig

        db = create_relational_db(TestConfig())
    count = 0
    for ident in request.identities:
        try:
            db.save_element_identity(
                app_package=ident.get("app_package", ""),
                page_signature=ident.get("page_signature", ""),
                alias=ident.get("target", ""),
                resource_id=ident.get("resource_id", ""),
                class_name=ident.get("class_name", ""),
                role=ident.get("role", ""),
                candidates_count=ident.get("candidates_count", 2),
            )
            count += 1
        except Exception:
            pass
    return {"status": "success", "confirmed": count}


@app.get("/api/runs/active")
async def get_active_runs():
    """供前端页面刷新时恢复 executing 状态（v3 R16）。"""
    return {"active": bool(orchestrator._active_runs)}


@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    """WebSocket: 接收 run / human_decision / stop_run 消息。

    关键设计：stop_run 走独立 task，永远不让 asyncio.to_thread 阻塞 receive 循环。
    修复背景：run/human_decision 通过 asyncio.to_thread 跑同步 graph（耗时可达数分钟），
    原实现顺序 await 整个 task，导致 stop_run 消息进 buffer 但没人读，
    现象是「点了停止要等 run 自然结束才停」。
    """
    # 本地回环地址直接放行；外部连接验证 auth cookie
    client_host = (websocket.client.host if websocket.client else "") or ""
    if client_host not in ("127.0.0.1", "localhost", "::1"):
        ws_cookies = websocket.cookies
        if ws_cookies.get(AUTH_COOKIE_NAME) != AUTH_TOKEN:
            await websocket.close(code=4001, reason="Unauthorized")
            return

    ws_manager.bind_loop(asyncio.get_running_loop())
    await ws_manager.connect(websocket)
    _ws_log = logging.getLogger(__name__)

    async def _handle_stop_run_async(tid: str) -> None:
        """独立 task 处理 stop_run：request_stop 是 O(1) 同步快路径，但包装在 task
        里保证主 receive 循环不被任何 await 阻塞。"""
        _ws_log.info(
            "[stop-debug] WS stop_run [independent task] -> orchestrator.request_stop tid=%s",
            tid,
        )
        try:
            ok = orchestrator.request_stop(tid, reason="ws_stop")
        except Exception as exc:
            _ws_log.warning("[stop-debug] request_stop raised: %s", exc)
            ok = False
        _ws_log.info(
            "[stop-debug] WS stop_run [independent task] -> request_stop returned ok=%s",
            ok,
        )
        try:
            await ws_manager.send(
                websocket,
                {"type": "stop_ack", "content": {"ok": ok, "thread_id": tid}},
            )
        except (RuntimeError, Exception) as exc:
            _ws_log.debug("[stop-debug] stop_ack send failed: %s", exc)

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type", "")
            _ws_log.info(
                "[stop-debug] WS RECV type=%s thread_id=%s payload=%s",
                msg_type,
                data.get("thread_id", "-"),
                str(data)[:200],
            )

            # ── stop_run 走独立 task，绝不阻塞 receive 循环 ──
            if msg_type == "stop_run":
                tid = data.get("thread_id", "")
                asyncio.create_task(_handle_stop_run_async(tid))
                continue

            if msg_type == "run":
                user_input = data.get("message", "")
                # 设备连接前置检查
                if _device is None:
                    await ws_manager.send(
                        websocket,
                        {
                            "type": "error",
                            "content": "Android 设备未连接，请检查 USB/ADB 连接后重试",
                        },
                    )
                    continue
                app_package, app_name = _quick_resolve_app(user_input)
                _ws_log.info("[stop-debug] WS run -> orchestrator.start (to_thread)")
                result = await asyncio.to_thread(
                    orchestrator.start,
                    user_request=user_input,
                    app_package=app_package,
                    app_name=app_name,
                )
                _ws_log.info(
                    "[stop-debug] WS run -> orchestrator.start returned, status=%s tid=%s",
                    result.get("status"),
                    result.get("thread_id"),
                )
                try:
                    await ws_manager.send(
                        websocket, {"type": "result", "content": result}
                    )
                except RuntimeError:
                    pass

            elif msg_type == "human_decision":
                thread_id = data.get("thread_id", "")
                decision = data.get("decision", "跳过")
                _ws_log.info(
                    "[stop-debug] WS human_decision -> orchestrator.resume tid=%s",
                    thread_id,
                )
                result = await asyncio.to_thread(
                    orchestrator.resume,
                    thread_id=thread_id,
                    decision=decision,
                )
                try:
                    await ws_manager.send(
                        websocket, {"type": "result", "content": result}
                    )
                except RuntimeError:
                    pass

            # ── v3: 复跑 / 用例运行 ──
            elif msg_type == "rerun":
                run_id = data.get("run_id", "")
                run = _db.get_test_run(run_id) if _db and run_id else None
                if not run:
                    await ws_manager.send(websocket, {"type": "error", "content": f"报告不存在: {run_id}"})
                    continue
                try:
                    entry = resolve_report_rerun_entry(run)
                except ValueError as exc:
                    await ws_manager.send(websocket, {"type": "error", "content": f"报告计划数据损坏: {exc}"})
                    continue
                result = await asyncio.to_thread(
                    orchestrator.start,
                    user_request=run.get("user_request", ""),
                    app_package=run.get("app_package", ""),
                    app_name=run.get("app_name", ""),
                    goal_description=entry["goal"],
                    reuse_plan=True,
                    run_type="rerun",
                    source_run_id=entry["source_run_id"],
                    source_case_id=None,
                    execution_plan_revision=entry["execution_plan_revision"],
                )
                try:
                    await ws_manager.send(websocket, {"type": "result", "content": result})
                except RuntimeError:
                    pass

            elif msg_type == "run_case":
                case_id = data.get("case_id", "")
                case = _db.get_test_case(case_id) if _db and case_id else None
                if not case:
                    await ws_manager.send(websocket, {"type": "error", "content": f"用例不存在: {case_id}"})
                    continue
                try:
                    entry = _resolve_run_entry(case)
                except ValueError as exc:
                    await ws_manager.send(websocket, {"type": "error", "content": f"用例计划数据损坏: {exc}"})
                    continue
                result = await asyncio.to_thread(
                    orchestrator.start,
                    user_request=case.get("user_request", ""),
                    app_package=case.get("app_package", ""),
                    app_name=case.get("app_name", ""),
                    goal_description=entry["goal"],
                    reuse_plan=True,
                    run_type="rerun",
                    source_run_id=entry["source_run_id"],
                    source_case_id=entry["source_case_id"],
                    execution_plan_revision=entry["execution_plan_revision"],
                )
                if result.get("status") != "busy":
                    st = result.get("execution_status", "error")
                    vd = result.get("test_verdict", "inconclusive")
                    _db.record_case_run(case_id, f"{st}/{vd}", datetime.now().isoformat())
                try:
                    await ws_manager.send(websocket, {"type": "result", "content": result})
                except RuntimeError:
                    pass

    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)


# ── 报告 API ──


@app.get("/api/reports/list")
async def list_reports(limit: int = 30):
    db = _get_relational_db()
    if db:
        try:
            items = db.list_test_runs(limit)
            return {"status": "success", "items": items}
        except Exception:
            logging.getLogger(__name__).exception("Failed to list test reports")
    return {"status": "success", "items": []}


@app.get("/api/reports/{run_id}")
async def get_report(run_id: str):
    db = _get_relational_db()
    if db:
        try:
            report = db.get_test_run(run_id)
            if report:
                return {"status": "success", "report": report}
        except Exception:
            pass
    return {"status": "error", "message": f"报告不存在: {run_id}"}


def _safe_unlink(path: Path) -> bool:
    try:
        if path.exists() and path.is_file():
            path.unlink()
            return True
    except Exception as exc:
        logging.getLogger(__name__).warning("Failed to delete file %s: %s", path, exc)
    return False


@app.delete("/api/reports/{run_id}")
async def delete_report(run_id: str):
    db = _get_relational_db()
    if not db:
        return {"status": "error", "message": "数据库未初始化"}

    try:
        report = db.get_test_run(run_id)
    except Exception:
        report = None
    if not report:
        return {"status": "error", "message": f"报告不存在: {run_id}"}

    deleted_images = 0
    deleted_logs = 0

    image_paths: set[Path] = set()
    for step in report.get("steps", []) or []:
        p = str(step.get("screenshot_path", "") or "").strip()
        if p:
            _pp = Path(p.replace("/", "\\"))
            image_paths.add(_pp if _pp.is_absolute() else PROJECT_ROOT / _pp)
    for item in report.get("verification_results", []) or []:
        p = str(item.get("screenshot", "") or "").strip()
        if p:
            _pp = Path(p.replace("/", "\\"))
            image_paths.add(_pp if _pp.is_absolute() else PROJECT_ROOT / _pp)

    run_shot_dir = app_paths.SCREENSHOT_DIR / run_id
    if run_shot_dir.exists() and run_shot_dir.is_dir():
        for f in run_shot_dir.rglob("*"):
            if f.is_file() and _safe_unlink(f):
                deleted_images += 1
        try:
            run_shot_dir.rmdir()
        except Exception:
            pass

    for p in image_paths:
        try:
            resolved = p.resolve()
            if (
                PROJECT_ROOT not in resolved.parents
                and app_paths.DATA_DIR not in resolved.parents
            ):
                continue
        except Exception:
            continue
        if _safe_unlink(p):
            deleted_images += 1

    logs_dir = app_paths.LOG_RUN_DIR
    if logs_dir.exists() and logs_dir.is_dir():
        for lf in logs_dir.glob(f"*{run_id}*langchain.log"):
            if _safe_unlink(lf):
                deleted_logs += 1

    deleted_db = False
    try:
        deleted_db = db.delete_test_run(run_id)
    except Exception as exc:
        return {"status": "error", "message": f"删除数据库记录失败: {exc}"}
    if not deleted_db:
        return {"status": "error", "message": f"报告不存在: {run_id}"}

    return {
        "status": "success",
        "deleted": {
            "db_records": 1,
            "images": deleted_images,
            "logs": deleted_logs,
        },
    }


@app.get("/api/health")
async def health():
    return {"status": "ok"}


# ── 静态文件 ──

app.mount("/storage", StaticFiles(directory=str(app_paths.DATA_DIR)), name="storage")
app.mount(
    "/static",
    StaticFiles(directory=str(app_paths.FRONTEND_DIST_DIR.parent)),
    name="static",
)


@app.get("/")
async def index():
    resp = FileResponse(
        str(INDEX_FILE),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )
    # 设置 auth cookie，浏览器/WebView 后续请求自动携带
    resp.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=AUTH_TOKEN,
        httponly=True,
        samesite="strict",
        path="/",
    )
    return resp


# ── 辅助 ──


def _quick_resolve_app(text: str) -> tuple[str, str]:
    """从用户输入中解析 (package, name)，优先读取 storage/apps.yaml。"""
    return _resolve_app_from_yaml(text)

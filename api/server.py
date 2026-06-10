from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from api.device_routes import router as device_router
from api.device_routes import set_runner as set_device_runner
from api.websocket_manager import WebSocketManager
from config import TestConfig
from core.chat_runner import ChatRunner

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
FRONTEND_DIST_DIR = FRONTEND_DIR / "dist"
INDEX_FILE = FRONTEND_DIST_DIR / "index.html"

app = FastAPI(title="AI 自动化测试 Agent")
config = TestConfig.from_yaml("config.yaml")
runner = ChatRunner(config)
set_device_runner(runner)
ws_manager = WebSocketManager()
pending_intents: dict[str, dict] = {}
app.include_router(device_router)

# 将 WebSocket 广播能力注入 ChatRunner，实现执行过程实时推送
runner.set_event_callback(lambda event_type, payload: ws_manager.broadcast_sync(event_type, payload))


class ParseRequest(BaseModel):
    message: str
    session_id: str = "default"


class ConfirmRequest(BaseModel):
    session_id: str = "default"
    intent: dict
    confirmed: bool = True


class CaseContentRequest(BaseModel):
    case_file: str
    content: str


@app.post("/api/parse")
async def parse_intent(request: ParseRequest):
    intent = runner.parse(request.message)
    pending_intents[request.session_id] = intent
    return {
        "status": "pending_confirmation",
        "intent": intent,
        "editable_fields": editable_fields(),
    }


@app.post("/api/confirm")
async def confirm_intent(request: ConfirmRequest):
    ws_manager.bind_loop(asyncio.get_running_loop())
    if not request.confirmed:
        pending_intents.pop(request.session_id, None)
        return {"status": "cancelled", "message": "已取消"}

    # 推送执行开始事件
    await ws_manager.broadcast({"type": "status", "content": "开始执行..."})

    result = await asyncio.to_thread(runner.run_with_intent, request.intent)
    pending_intents.pop(request.session_id, None)

    return {
        "status": result.get("status", "error"),
        "message": result.get("conclusion") or result.get("message", ""),
        "data": result,
    }


@app.post("/api/chat")
async def chat(request: ParseRequest):
    await ws_manager.broadcast({"type": "status", "content": "开始执行..."})
    result = runner.run(request.message)
    return {"status": result.get("status", "error"), "data": result}


@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    ws_manager.bind_loop(asyncio.get_running_loop())
    await ws_manager.connect(websocket)
    session_id = str(id(websocket))
    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type", "")

            if msg_type == "parse":
                # 解析意图
                user_input = data.get("message", "")
                await ws_manager.send(
                    websocket,
                    {"type": "status", "content": f"正在解析: {user_input}"},
                )
                intent = runner.parse(user_input)
                pending_intents[session_id] = intent
                await ws_manager.send(
                    websocket,
                    {
                        "type": "intent",
                        "content": intent,
                        "editable_fields": editable_fields(),
                    },
                )

            elif msg_type == "confirm":
                confirmed = data.get("confirmed", False)
                if not confirmed:
                    pending_intents.pop(session_id, None)
                    await ws_manager.send(
                        websocket, {"type": "cancelled", "content": "已取消"}
                    )
                    continue

                await ws_manager.send(
                    websocket, {"type": "status", "content": "开始执行..."}
                )

                # 执行过程中，ReportBuilder 会通过 event_callback 自动广播
                # step_start / step_end / anomaly / snapshot / result 事件
                result = await asyncio.to_thread(
                    runner.run_with_intent, data.get("intent", {})
                )
                pending_intents.pop(session_id, None)

                # 最终结果也推一次，确保客户端收到
                await ws_manager.send(
                    websocket,
                    {
                        "type": "result",
                        "content": {
                            "status": result.get("status"),
                            "mode": result.get("mode"),
                            "conclusion": result.get("conclusion"),
                            "report_path": result.get("report_path"),
                            "message": result.get("message", ""),
                            "case_file": result.get("case_file", ""),
                            "intent": result.get("intent", {}),
                        },
                    },
                )

    except WebSocketDisconnect:
        pending_intents.pop(session_id, None)
        ws_manager.disconnect(websocket)


def editable_fields():
    return {
        "intent": {
            "label": "模式",
            "type": "select",
            "options": ["traverse", "run", "replay", "run_case", "generate_case"],
        },
        "app_name": {"label": "应用名称", "type": "text"},
        "app_package": {"label": "包名", "type": "text"},
        "task_description": {"label": "任务描述", "type": "textarea"},
        "case_file": {"label": "用例文件", "type": "text"},
        "traversal_max_depth": {"label": "扫描深度", "type": "number"},
        "traversal_max_pages": {"label": "最大页面", "type": "number"},
    }


def _safe_resolve_under(base: Path, raw_path: str) -> Path:
    raw = str(raw_path or "").strip().replace("/", "\\")
    if not raw:
        raise ValueError("路径为空")
    path = Path(raw)
    if not path.is_absolute():
        path = (BASE_DIR / path).resolve()
    else:
        path = path.resolve()
    base_resolved = base.resolve()
    try:
        path.relative_to(base_resolved)
    except ValueError as exc:
        raise ValueError("路径不在允许目录内") from exc
    return path


@app.get("/api/cases/content")
async def get_case_content(case_file: str):
    case_base = BASE_DIR / "test_cases"
    try:
        target = _safe_resolve_under(case_base, case_file)
        if not target.exists():
            return {"status": "error", "message": f"用例不存在: {case_file}"}
        return {
            "status": "success",
            "case_file": str(target.relative_to(BASE_DIR)).replace("/", "\\"),
            "content": target.read_text(encoding="utf-8"),
        }
    except Exception as exc:
        return {"status": "error", "message": f"读取用例失败: {exc}"}


@app.post("/api/cases/content")
async def save_case_content(request: CaseContentRequest):
    case_base = BASE_DIR / "test_cases"
    try:
        target = _safe_resolve_under(case_base, request.case_file)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(request.content or ""), encoding="utf-8")
        return {
            "status": "success",
            "case_file": str(target.relative_to(BASE_DIR)).replace("/", "\\"),
        }
    except Exception as exc:
        return {"status": "error", "message": f"保存用例失败: {exc}"}


@app.get("/api/reports/list")
async def list_reports(limit: int = 30):
    report_base = BASE_DIR / "reports"
    report_base.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for item in sorted(
        report_base.glob("report_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[: max(1, min(limit, 200))]:
        try:
            data = json.loads(item.read_text(encoding="utf-8"))
            rows.append(
                {
                    "name": data.get("name") or item.stem,
                    "mode": data.get("mode", ""),
                    "status": data.get("status", ""),
                    "app_package": data.get("app_package", ""),
                    "created_at": data.get("created_at", ""),
                    "duration_seconds": round(float(data.get("duration_seconds", 0) or 0), 2),
                    "report_path": str(item.relative_to(BASE_DIR)).replace("/", "\\"),
                }
            )
        except Exception:
            continue
    return {"status": "success", "items": rows}


@app.get("/api/reports/content")
async def get_report_content(report_path: str):
    report_base = BASE_DIR / "reports"
    try:
        target = _safe_resolve_under(report_base, report_path)
        if not target.exists():
            return {"status": "error", "message": f"报告不存在: {report_path}"}
        data = json.loads(target.read_text(encoding="utf-8"))
        return {
            "status": "success",
            "report_path": str(target.relative_to(BASE_DIR)).replace("/", "\\"),
            "report": data,
        }
    except Exception as exc:
        return {"status": "error", "message": f"读取报告失败: {exc}"}


app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(
        str(INDEX_FILE),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/api/device", tags=["device"])
_orchestrator = None


def set_runner(orchestrator) -> None:
    global _orchestrator
    _orchestrator = orchestrator


def get_orchestrator():
    if _orchestrator is None:
        raise RuntimeError("orchestrator 未初始化")
    return _orchestrator


class ClickRequest(BaseModel):
    label: str = ""
    bounds: list[int] | None = None


class InputRequest(BaseModel):
    text: str


class KeyRequest(BaseModel):
    key: str


def _get_device():
    """安全获取设备实例，未连接时返回 None。"""
    try:
        from tools import get_tool_context
        ctx = get_tool_context()
        return getattr(ctx, "device", None)
    except Exception:
        return None


def _get_perceiver():
    """安全获取感知器实例。"""
    try:
        from tools import get_tool_context
        ctx = get_tool_context()
        return getattr(ctx, "perceiver", None)
    except Exception:
        return None


def _device_required(dev):
    """设备未连接时抛出统一错误。"""
    if dev is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="Android 设备未连接，请检查 USB/ADB")


# ── 设备状态 ──

@router.get("/status")
async def device_status():
    """查询设备连接状态。离线时自动尝试重连。"""
    dev = _get_device()
    if dev is None:
        # 自动尝试重连（设备可能在服务器启动后才插入）
        from api.server import reconnect_device
        result = reconnect_device()
        if result.get("connected"):
            dev = _get_device()
        else:
            return {"connected": False, "detail": "Android 设备未连接，请检查 USB/ADB 连接后重试"}
    try:
        app = dev.current_app()
        return {
            "connected": True,
            "package": app.get("package", ""),
            "activity": app.get("activity", ""),
        }
    except Exception as exc:
        return {"connected": False, "detail": f"设备通信异常: {exc}"}


@router.post("/reconnect")
async def device_reconnect():
    """重连 Android 设备。"""
    from api.server import reconnect_device
    return reconnect_device()


# ── 快照 ──

@router.get("/snapshot")
async def snapshot(include_vision: bool = False):
    dev = _get_device()
    _device_required(dev)

    from device.perceiver import PerceptionMode

    snapshot_obj = dev.snapshot()
    perceiver = _get_perceiver()

    if perceiver:
        previous_mode = getattr(perceiver, "mode", PerceptionMode.UI_TREE)
        try:
            perceiver.switch_mode(PerceptionMode.HYBRID if include_vision else PerceptionMode.UI_TREE)
            understanding = perceiver.perceive()
        finally:
            perceiver.switch_mode(previous_mode)
    else:
        understanding = None

    result: dict = {
        "package": snapshot_obj.package,
        "activity": snapshot_obj.activity,
        "screen": {
            "width": snapshot_obj.width,
            "height": snapshot_obj.height,
            "image_base64": snapshot_obj.image_base64,
        },
    }
    if understanding:
        result["understanding"] = understanding.to_dict()
    else:
        result["understanding"] = {"summary": "感知器未初始化"}

    return result


# ── 当前应用 ──

@router.get("/current")
async def current():
    dev = _get_device()
    _device_required(dev)
    return dev.current_app()


# ── 点击 ──

@router.post("/click")
async def click(request: ClickRequest):
    dev = _get_device()
    _device_required(dev)
    if request.bounds and len(request.bounds) == 4:
        dev.click_bounds(tuple(request.bounds))
        return {"status": "success", "message": "clicked bounds"}
    if request.label and dev.click_text(request.label):
        return {"status": "success", "message": f"clicked {request.label}"}
    if request.label and dev.click_resource_id(request.label):
        return {"status": "success", "message": f"clicked resource {request.label}"}
    return {"status": "error", "message": "target not found"}


# ── 输入 ──

@router.post("/input")
async def input_text(request: InputRequest):
    dev = _get_device()
    _device_required(dev)
    dev.type_text(request.text)
    return {"status": "success"}


# ── 按键 ──

@router.post("/key")
async def key(request: KeyRequest):
    dev = _get_device()
    _device_required(dev)
    dev.press(request.key)
    return {"status": "success"}

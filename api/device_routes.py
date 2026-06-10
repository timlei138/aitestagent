from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/api/device", tags=["device"])
_runner = None


def set_runner(runner) -> None:
    global _runner
    _runner = runner


def get_runner():
    if _runner is None:
        raise RuntimeError("device runner 未初始化")
    return _runner


class ClickRequest(BaseModel):
    label: str = ""
    bounds: list[int] | None = None


class InputRequest(BaseModel):
    text: str


class KeyRequest(BaseModel):
    key: str


@router.get("/snapshot")
async def snapshot(include_vision: bool = False):
    try:
        return get_runner().snapshot(include_vision=include_vision)
    except Exception as exc:
        return {"status": "error", "message": f"设备未连接或服务未就绪: {exc}"}


@router.get("/current")
async def current():
    try:
        ctx = get_runner()._ensure_context()
        return ctx.device.current_app()
    except Exception as exc:
        return {"status": "error", "message": f"设备未连接: {exc}"}


@router.post("/click")
async def click(request: ClickRequest):
    ctx = get_runner()._ensure_context()
    if request.bounds and len(request.bounds) == 4:
        ctx.device.click_bounds(tuple(request.bounds))
        return {"status": "success", "message": "clicked bounds"}
    if request.label and ctx.device.click_text(request.label):
        return {"status": "success", "message": f"clicked {request.label}"}
    if request.label and ctx.device.click_resource_id(request.label):
        return {"status": "success", "message": f"clicked resource {request.label}"}
    return {"status": "error", "message": "target not found"}


@router.post("/input")
async def input_text(request: InputRequest):
    ctx = get_runner()._ensure_context()
    ctx.device.type_text(request.text)
    return {"status": "success"}


@router.post("/key")
async def key(request: KeyRequest):
    ctx = get_runner()._ensure_context()
    ctx.device.press(request.key)
    return {"status": "success"}

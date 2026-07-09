# -*- coding: utf-8 -*-
"""测试用例计划 API：保存 planner 生成物 + 一键复跑。"""
from __future__ import annotations

import json
import logging
import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from data import plans as plans_data

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/plans", tags=["plans"])

_orchestrator = None
_device_getter = None


def set_orchestrator(orchestrator) -> None:
    global _orchestrator
    _orchestrator = orchestrator


def set_device_getter(fn) -> None:
    """注入获取当前设备连接状态的回调（返回 DeviceController 或 None）。"""
    global _device_getter
    _device_getter = fn


def _device_online() -> bool:
    if _device_getter is None:
        return True
    try:
        return _device_getter() is not None
    except Exception:
        return True


# ── 请求模型 ──


class SavePlanRequest(BaseModel):
    name: str
    plan: dict[str, Any]


class BatchRunRequest(BaseModel):
    names: list[str]


# ── API ──


@router.post("")
def save_plan(req: SavePlanRequest):
    """保存一条测试用例计划（planner 生成物）。"""
    try:
        record = plans_data.save_plan(req.name, req.plan)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"status": "success", "plan": record}


@router.get("")
def list_plans():
    """列出全部保存的测试用例计划。"""
    try:
        items = plans_data.list_plans()
    except Exception:
        items = []
    return {"status": "success", "items": items}


@router.get("/{name}")
def get_plan(name: str):
    """读取单条计划（含 YAML 正文）。"""
    plan = plans_data.get_plan(name)
    if not plan:
        raise HTTPException(status_code=404, detail=f"计划不存在: {name}")
    return {"status": "success", "plan": plan}


@router.delete("/{name}")
def delete_plan(name: str):
    """删除单条计划。"""
    ok = plans_data.delete_plan(name)
    if not ok:
        raise HTTPException(status_code=404, detail=f"计划不存在: {name}")
    return {"status": "success", "deleted": True}


@router.post("/{name}/run")
async def run_plan(name: str):
    """复跑已保存计划（同步，返回最终结果）。"""
    if not _device_online():
        return {
            "status": "device_offline",
            "message": "Android 设备未连接，请检查 USB/ADB 连接后重试",
        }
    if _orchestrator is None:
        raise HTTPException(status_code=503, detail="编排器未初始化")
    record = plans_data.get_plan(name)
    if not record:
        raise HTTPException(status_code=404, detail=f"计划不存在: {name}")
    plan = record.get("plan", {}) or {}
    result = await _run_plan_to_result(name, plan)
    return {"status": result.get("status", "error"), "data": result}


@router.post("/run/batch")
async def run_plans_batch(req: BatchRunRequest):
    """按选择顺序批量复跑已保存计划。"""
    names = [n.strip() for n in (req.names or []) if n and n.strip()]
    if not names:
        raise HTTPException(status_code=400, detail="请选择要复跑的计划")
    if not _device_online():
        return {
            "status": "device_offline",
            "message": "Android 设备未连接，请检查 USB/ADB 连接后重试",
            "items": [],
        }
    if _orchestrator is None:
        raise HTTPException(status_code=503, detail="编排器未初始化")

    items = []
    for name in names:
        record = plans_data.get_plan(name)
        if not record:
            items.append({"name": name, "status": "not_found", "message": f"计划不存在: {name}"})
            continue
        plan = record.get("plan", {}) or {}
        try:
            result = await _run_plan_to_result(name, plan)
            items.append({"name": name, "status": result.get("status", "error"), "data": result})
        except Exception as exc:
            logger.exception("批量复跑失败: %s", name)
            items.append({"name": name, "status": "error", "message": str(exc)})
    return {"status": "success", "items": items}


@router.post("/{name}/run/stream")
async def run_plan_stream(name: str):
    """复跑已保存计划（流式 SSE）。"""
    if not _device_online():
        async def offline_stream():
            yield f"data: {json.dumps({'type': 'error', 'content': 'Android 设备未连接，请检查 USB/ADB 连接后重试'}, ensure_ascii=False)}\n\n"
        return StreamingResponse(offline_stream(), media_type="text/event-stream")
    if _orchestrator is None:
        async def err_stream():
            yield f"data: {json.dumps({'type': 'error', 'content': '编排器未初始化'}, ensure_ascii=False)}\n\n"
        return StreamingResponse(err_stream(), media_type="text/event-stream")
    record = plans_data.get_plan(name)
    if not record:
        async def nf_stream():
            yield f"data: {json.dumps({'type': 'error', 'content': f'计划不存在: {name}'}, ensure_ascii=False)}\n\n"
        return StreamingResponse(nf_stream(), media_type="text/event-stream")
    plan = record.get("plan", {}) or {}

    async def event_generator():
        async for event in _orchestrator.start_stream(
            user_request=plan.get("goal", "") or name,
            app_package=plan.get("app_package", ""),
            app_name=plan.get("app_name", ""),
            goal_override=plan,
        ):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


async def _run_plan_to_result(name: str, plan: dict[str, Any]) -> dict[str, Any]:
    return await asyncio.to_thread(
        _orchestrator.start,
        user_request=plan.get("goal", "") or name,
        app_package=plan.get("app_package", ""),
        app_name=plan.get("app_name", ""),
        goal_override=plan,
    )

"""v3: 用例管理 CRUD + 单条运行 + 批量删除。"""

from __future__ import annotations

import json
import logging
from datetime import datetime as _dt

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/test_cases", tags=["test_cases"])

# 由 server.py 在 startup 设置，避免循环导入
_orchestrator = None
_relational_db = None


def set_backends(orchestrator_, relational_db_):
    global _orchestrator, _relational_db
    _orchestrator = orchestrator_
    _relational_db = relational_db_


# ═══ CRUD ═══


@router.get("")
def list_test_cases(q: str = ""):
    if not _relational_db:
        return {"status": "error", "message": "数据库未初始化"}
    return {
        "status": "ok",
        "data": _relational_db.list_test_cases(q=q) if q else _relational_db.list_test_cases(),
    }


@router.post("")
def create_test_case(body: dict):
    if not _relational_db:
        return {"status": "error", "message": "数据库未初始化"}

    # 两模式统一入口：带 run_id 从报告复制；否则用 body 完整计划
    if body.get("run_id"):
        run = _relational_db.get_test_run(body["run_id"])
        if not run:
            return {"status": "error", "message": "报告不存在"}
        try:
            goal = json.loads(run.get("goal_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            return {"status": "error", "message": "报告计划数据损坏"}
        case_id = _relational_db.create_test_case(
            name=body.get("name") or (run.get("user_request") or "未命名")[:40],
            source_run_id=run["id"],
            user_request=run.get("user_request", ""),
            app_package=run.get("app_package", ""),
            app_name=run.get("app_name", ""),
            goal_json=json.dumps(goal, ensure_ascii=False),
        )
        return {"status": "ok", "data": {"id": case_id}}

    # 从零新建：body 带完整计划
    goal_json = body.get("goal_json") or {}
    if isinstance(goal_json, dict):
        goal_json = json.dumps(goal_json, ensure_ascii=False)
    case_id = _relational_db.create_test_case(
        name=body.get("name", "未命名"),
        user_request=body.get("user_request", ""),
        app_package=body.get("app_package", ""),
        app_name=body.get("app_name", ""),
        goal_json=goal_json,
    )
    return {"status": "ok", "data": {"id": case_id}}


@router.put("/{case_id}")
def update_test_case(case_id: str, body: dict):
    if not _relational_db:
        return {"status": "error", "message": "数据库未初始化"}
    ok = _relational_db.update_test_case(case_id, body)
    return {
        "status": "ok" if ok else "error",
        "message": "" if ok else "用例不存在或无可更新字段",
    }


@router.delete("/{case_id}")
def delete_test_case(case_id: str):
    if not _relational_db:
        return {"status": "error", "message": "数据库未初始化"}
    ok = _relational_db.delete_test_case(case_id)
    return {"status": "ok" if ok else "error", "message": "" if ok else "用例不存在"}


@router.post("/batch_delete")
def batch_delete_test_cases(body: dict):
    if not _relational_db:
        return {"status": "error", "message": "数据库未初始化"}
    ids: list[str] = body.get("ids") or []
    n = _relational_db.batch_delete_test_cases(ids)
    return {"status": "ok", "deleted": n}


# ═══ 运行 ═══


@router.post("/{case_id}/run")
def run_test_case(case_id: str):
    """单条复跑（HTTP 兜底路径）。"""
    if not _relational_db or not _orchestrator:
        return {"status": "error", "message": "后台服务未就绪"}
    case = _relational_db.get_test_case(case_id)
    if not case:
        return {"status": "error", "message": "用例不存在"}

    try:
        goal = json.loads(case.get("goal_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        return {"status": "error", "message": "用例计划数据损坏"}

    result = _orchestrator.start(
        user_request=case.get("user_request", ""),
        app_package=case.get("app_package", ""),
        app_name=case.get("app_name", ""),
        goal_description=goal,
        reuse_plan=True,
        run_type="rerun",
        source_run_id=case_id,
    )

    # 仅实际启动时更新用例状态
    if result.get("status") != "busy":
        status = result.get("execution_status", "error")
        verdict = result.get("test_verdict", "inconclusive")
        case_status = f"{status}/{verdict}"
        _relational_db.record_case_run(case_id, case_status, _dt.now().isoformat())

    return {"status": "ok", "thread_id": result.get("thread_id", "")}

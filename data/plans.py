# -*- coding: utf-8 -*-
"""测试用例计划：将 planner 生成物持久化为 YAML 并登记到关系库。

保存格式（与 agents.state.TestGoalOutput 对齐）：
    goal:         测试目标（一句话）
    app_package:  目标应用包名
    app_name:     目标应用名
    target_pages: 需到达的页面列表
    verification: 验证条件列表
    hints:        给 Agent 的导航提示列表
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

import yaml

import app_paths

logger = logging.getLogger(__name__)

_PLAN_FIELDS = ("goal", "app_package", "app_name", "target_pages", "verification", "hints")


def _get_db():
    """复用 agents.graph 中的关系库单例。"""
    from agents.graph import _relational_db

    return _relational_db


def _sanitize_filename(name: str) -> str:
    """将计划名清洗为安全的文件名（保留原 name 作为数据库唯一键）。"""
    safe = re.sub(r"[^\w\-]+", "_", name).strip("_")
    return safe or "plan"


def save_plan(name: str, plan: dict[str, Any]) -> dict[str, Any]:
    """保存一条测试用例计划：写 YAML + 登记关系库。返回保存后的记录。"""
    name = (name or "").strip()
    if not name:
        raise ValueError("计划名称不能为空")
    if not isinstance(plan, dict) or not plan.get("goal"):
        raise ValueError("计划内容缺少 goal 字段")

    db = _get_db()
    if db is None:
        raise RuntimeError("关系型数据库未初始化")

    app_paths.CASE_DIR.mkdir(parents=True, exist_ok=True)
    safe = _sanitize_filename(name)
    yaml_path = str(app_paths.CASE_DIR / f"{safe}.yaml")

    record = {k: plan.get(k, [] if k in ("target_pages", "verification", "hints") else "") for k in _PLAN_FIELDS}
    with open(yaml_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(record, f, allow_unicode=True, sort_keys=False)

    steps_count = len(record.get("target_pages", []) or []) + len(
        record.get("verification", []) or []
    ) + len(record.get("hints", []) or [])
    db.save_test_plan(
        name=name,
        app_package=record.get("app_package", "") or "",
        yaml_path=yaml_path,
        steps_count=steps_count,
    )
    row = db.get_test_plan(name) or {}
    return {**row, "plan": record}


def list_plans() -> list[dict[str, Any]]:
    """列出全部测试用例计划（不含 YAML 正文）。"""
    db = _get_db()
    if db is None:
        return []
    return db.list_test_plans()


def get_plan(name: str) -> dict[str, Any] | None:
    """读取单条计划，包含 YAML 正文（plan 字段）。"""
    db = _get_db()
    if db is None:
        return None
    row = db.get_test_plan(name)
    if not row:
        return None
    yaml_path = row.get("yaml_path", "")
    plan: dict[str, Any] = {}
    try:
        if yaml_path:
            with open(yaml_path, "r", encoding="utf-8") as f:
                plan = yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.warning("计划 YAML 缺失: %s", yaml_path)
    except Exception as exc:
        logger.warning("读取计划 YAML 失败: %s", exc)
    return {**row, "plan": plan}


def delete_plan(name: str) -> bool:
    """删除单条计划：删数据库记录 + 删除 YAML 文件。"""
    db = _get_db()
    if db is None:
        return False
    row = db.get_test_plan(name)
    if not row:
        return False
    yaml_path = row.get("yaml_path", "")
    if yaml_path:
        try:
            from pathlib import Path

            p = Path(yaml_path)
            if p.exists():
                p.unlink()
        except Exception as exc:
            logger.warning("删除计划 YAML 失败: %s", exc)
    return db.delete_test_plan(name)

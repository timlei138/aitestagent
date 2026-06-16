from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/apps", tags=["apps"])

# ── 存储路径 ──
_APPS_YAML = Path("storage/apps.yaml")
_lock = threading.Lock()


# ── 数据模型 ──

class AppEntry(BaseModel):
    name: str
    package: str
    keywords: list[str] = []


# ── 内部读写 ──

def _load() -> list[dict[str, Any]]:
    """从 YAML 文件加载应用列表。"""
    if not _APPS_YAML.exists():
        return []
    with open(_APPS_YAML, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("apps", []) if isinstance(data, dict) else []


def _save(apps: list[dict[str, Any]]) -> None:
    """将应用列表写回 YAML 文件。"""
    _APPS_YAML.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "# 应用管理配置 - 由 APP管理 页面维护\n"
        "# 字段说明:\n"
        "#   name: 应用显示名称\n"
        "#   package: Android 包名\n"
        "#   keywords: 触发关键词（用户输入含任意关键词即可匹配该应用）\n\n"
    )
    with open(_APPS_YAML, "w", encoding="utf-8") as f:
        f.write(content)
        yaml.dump({"apps": apps}, f, allow_unicode=True,
                  default_flow_style=False, sort_keys=False)


# ── 公开读接口（供 server.py / main.py 使用）──

def load_app_list() -> list[dict[str, Any]]:
    """返回应用列表，线程安全。"""
    with _lock:
        return _load()


def resolve_app(text: str) -> tuple[str, str]:
    """从用户输入文本中解析 (package, name)。
    先按关键词匹配 YAML 中的应用，找不到则正则提取包名。
    """
    import re
    lowered = text.lower()
    apps = load_app_list()
    for app in apps:
        for kw in (app.get("keywords") or []):
            if kw.lower() in lowered:
                return app.get("package", ""), app.get("name", "")
    # 兜底：正则提取包名
    m = re.search(r"\b[a-zA-Z][\w]*(?:\.[\w]+){2,}\b", text)
    if m:
        return m.group(0), ""
    return "", ""


# ── REST API ──

@router.get("")
async def list_apps():
    """获取全部应用。"""
    with _lock:
        apps = _load()
    return {"status": "success", "apps": apps}


@router.post("")
async def add_app(entry: AppEntry):
    """新增应用。package 不可重复。"""
    with _lock:
        apps = _load()
        if any(a.get("package") == entry.package for a in apps):
            raise HTTPException(status_code=409, detail=f"包名已存在: {entry.package}")
        apps.append({"name": entry.name, "package": entry.package,
                      "keywords": entry.keywords})
        _save(apps)
    return {"status": "success", "message": f"已添加: {entry.name}"}


@router.put("/{package:path}")
async def update_app(package: str, entry: AppEntry):
    """修改应用（按 package 查找）。"""
    with _lock:
        apps = _load()
        idx = next((i for i, a in enumerate(apps) if a.get("package") == package), None)
        if idx is None:
            raise HTTPException(status_code=404, detail=f"未找到包名: {package}")
        apps[idx] = {"name": entry.name, "package": entry.package,
                     "keywords": entry.keywords}
        _save(apps)
    return {"status": "success", "message": f"已更新: {entry.name}"}


@router.delete("/{package:path}")
async def delete_app(package: str):
    """删除应用（按 package 查找）。"""
    with _lock:
        apps = _load()
        before = len(apps)
        apps = [a for a in apps if a.get("package") != package]
        if len(apps) == before:
            raise HTTPException(status_code=404, detail=f"未找到包名: {package}")
        _save(apps)
    return {"status": "success", "message": f"已删除: {package}"}


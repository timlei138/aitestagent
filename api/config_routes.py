from __future__ import annotations

import logging
from pathlib import Path

import yaml
from fastapi import APIRouter
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/config", tags=["config"])

_CONFIG_PATH = "config.yaml"
_LOCAL_CONFIG_PATH = "config.local.yaml"

# 可通过前端修改的字段白名单
_EDITABLE_FIELDS = (
    "llm_provider",
    "model",
    "api_key",
    "base_url",
    "embedding_provider",
    "embedding_model",
    "embedding_api_key",
    "embedding_base_url",
    "perception_mode",
    "safety_level",
)

_SECRET_FIELDS = ("api_key", "embedding_api_key")


def _get_config():
    """延迟获取全局 config 对象，避免循环导入。"""
    from api.server import config

    return config


def _mask(value: str | None) -> str:
    """脱敏 API Key：保留前4后4，中间用 *** 替代。"""
    if not value:
        return ""
    s = str(value)
    if len(s) <= 8:
        return "***"
    return f"{s[:4]}***{s[-4:]}"


# ── GET：读取当前配置 ──


@router.get("")
async def get_config():
    cfg = _get_config()
    data = {}
    for field in _EDITABLE_FIELDS:
        val = getattr(cfg, field, None)
        if field in _SECRET_FIELDS:
            data[field] = _mask(val)
        else:
            data[field] = val if val is not None else ""
    return data


# ── PUT：更新配置 ──


class ConfigUpdateRequest(BaseModel):
    llm_provider: str | None = None
    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    embedding_provider: str | None = None
    embedding_model: str | None = None
    embedding_api_key: str | None = None
    embedding_base_url: str | None = None
    perception_mode: str | None = None
    safety_level: str | None = None


@router.put("")
async def update_config(req: ConfigUpdateRequest):
    cfg = _get_config()
    updates = req.model_dump(exclude_none=True)

    changed = {}
    need_rebuild_perceiver = False

    for field, new_val in updates.items():
        if field not in _EDITABLE_FIELDS:
            continue
        # API key 脱敏值回传 → 保留原值
        if field in _SECRET_FIELDS and new_val and "***" in new_val:
            continue
        old_val = getattr(cfg, field, None)
        if str(old_val or "") == str(new_val or ""):
            continue
        setattr(cfg, field, new_val or None)
        changed[field] = True
        # perception_mode 或主 LLM 凭证变更需要重建 perceiver/context
        if field in ("perception_mode", "llm_provider", "model", "api_key", "base_url"):
            need_rebuild_perceiver = True

    # 写回 YAML（敏感字段写 config.local.yaml，非敏感写 config.yaml）
    _save_yaml(updates)

    # 热更新 perceiver
    if need_rebuild_perceiver:
        try:
            from api.server import reconnect_device

            reconnect_device()
            logger.info("Perceiver rebuilt after config change")
        except Exception as exc:
            logger.warning("Failed to rebuild perceiver: %s", exc)

    return {
        "status": "success",
        "changed": list(changed.keys()),
        "perceiver_rebuilt": need_rebuild_perceiver,
    }


def _save_yaml(updates: dict) -> None:
    """将更新写回配置文件。
    - 敏感字段（API Key）→ config.local.yaml（已 gitignore）
    - 非敏感字段 → config.yaml
    """
    valid_updates = {k: v for k, v in updates.items() if k in _EDITABLE_FIELDS}

    # ── 分离敏感与非敏感字段 ──
    local_updates = {k: v for k, v in valid_updates.items() if k in _SECRET_FIELDS}
    public_updates = {k: v for k, v in valid_updates.items() if k not in _SECRET_FIELDS}

    # ── 非敏感 → config.yaml ──
    if public_updates:
        path = Path(_CONFIG_PATH)
        existing: dict = {}
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                existing = yaml.safe_load(f) or {}
        existing.update(public_updates)
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(
                existing,
                f,
                allow_unicode=True,
                default_flow_style=False,
                sort_keys=False,
            )
        logger.info("config.yaml saved: %s", list(public_updates.keys()))

    # ── 敏感 → config.local.yaml ──
    if local_updates:
        local_path = Path(_LOCAL_CONFIG_PATH)
        local_existing: dict = {}
        if local_path.exists():
            with open(local_path, "r", encoding="utf-8") as f:
                local_existing = yaml.safe_load(f) or {}
        local_existing.update(local_updates)
        with open(local_path, "w", encoding="utf-8") as f:
            yaml.dump(
                local_existing,
                f,
                allow_unicode=True,
                default_flow_style=False,
                sort_keys=False,
            )
        logger.info("config.local.yaml saved: %s", list(local_updates.keys()))

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, SecretStr, model_validator

import app_paths

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/config", tags=["config"])

# 可通过前端修改的字段白名单
_EDITABLE_FIELDS = (
    "model",
    "api_key",
    "base_url",
    "perception_mode",
    "safety_level",
    # ── 视觉备用模型 ──
    "vision_model",
    "vision_api_key",
    "vision_base_url",
    "vision_timeout",
    "llm_vision_capable",
    # ── 上下文历史步数 ──
    "context_history_steps",
)

# 敏感字段保存到 config.local.yaml，避免写入已 git 跟踪的 config.yaml
_SECRET_FIELDS = ("api_key", "vision_api_key")


def _get_config():
    """延迟获取全局 config 对象，避免循环导入。"""
    from api.server import config

    return config


def _get_onnx_model_status() -> dict[str, object]:
    """返回固定 ONNX 模型目录的就绪状态，供设置页展示。"""
    model_dir = app_paths.ONNX_MODEL_DIR
    required_files = ("model.onnx", "tokenizer.json")
    missing_files = [
        name for name in required_files if not (model_dir / name).is_file()
    ]
    return {
        "ready": not missing_files,
        "path": str(model_dir),
        "missing_files": missing_files,
    }


# ── GET：读取当前配置 ──


@router.get("")
async def get_config():
    cfg = _get_config()
    # 注意：bool/int/float 字段不能用 `or ""` 兜底——False/0/0.0 会被 coerce 成 ""，
    # 前端保存时整包 PUT 回后端，Pydantic bool/int 字段收到 "" → 422 Unprocessable Entity。
    # 仅字符串字段用 "" 兜底（避免 null 在表单里不好显示），标量字段保持原值/None。
    _STRING_FIELDS = {"model", "api_key", "base_url", "perception_mode", "safety_level",
                      "vision_model", "vision_api_key", "vision_base_url"}
    payload = {}
    for field in _EDITABLE_FIELDS:
        val = getattr(cfg, field, None)
        if field in _STRING_FIELDS:
            payload[field] = val or ""
        else:
            payload[field] = val
    payload["onnx_model_status"] = _get_onnx_model_status()
    return payload


# ── PUT：更新配置 ──


class ConfigUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")  # 前端整包 PUT，忽略非配置字段，避免 422

    model: str | None =  None
    api_key: str | None =  None
    base_url: str | None =  None
    perception_mode: str | None =  None
    safety_level: str | None =  None
    vision_model: str | None =  None
    vision_api_key: str | None =  None
    vision_base_url: str | None =  None
    vision_timeout: int | None =  None
    llm_vision_capable: bool | None =  None
    context_history_steps: int | None =  None

    # 容错：前端偶发把标量字段传空串（如 checkbox 未勾选 / 数字重置），
    # 接收侧归一为 None，避免 Pydantic 类型校验 422。
    @model_validator(mode="before")
    @classmethod
    def _coerce_blank(cls, data: object) -> object:
        if isinstance(data, dict):
            for f in ("vision_timeout", "context_history_steps", "llm_vision_capable"):
                if data.get(f) == "":
                    data[f] = None
        return data


class ModelTestRequest(BaseModel):
    model: str = ""
    api_key: str = ""
    base_url: str = ""


def _safe_error_message(exc: Exception, api_key: str) -> str:
    message = str(exc).strip() or type(exc).__name__
    if api_key:
        message = message.replace(api_key, "***")
    return message[:500]


@router.post("/test-model")
async def test_model_connection(req: ModelTestRequest):
    """使用未保存的模型配置发送最小请求，验证连接、鉴权和模型可用性。"""
    model = req.model.strip()
    api_key = req.api_key.strip()
    base_url = req.base_url.strip() or None
    if not model or not api_key:
        raise HTTPException(status_code=400, detail="请填写模型名称和 API Key")

    try:
        from langchain_openai import ChatOpenAI

        client = ChatOpenAI(
            model=model,
            api_key=SecretStr(api_key),
            base_url=base_url,
            temperature=0,
            timeout=15,
            max_retries=0,
        )
        response = client.invoke("Reply with OK.")
        content = str(getattr(response, "content", "") or "").strip()
        return {"status": "success", "message": content[:200] or "模型已响应"}
    except Exception as exc:
        logger.info(
            "Model connection test failed for model=%s: %s", model, type(exc).__name__
        )
        raise HTTPException(
            status_code=400,
            detail=_safe_error_message(exc, api_key),
        ) from exc


@router.put("")
async def update_config(req: ConfigUpdateRequest):
    cfg = _get_config()
    updates = req.model_dump(exclude_none=True)

    changed = {}
    changed_values: dict[str, object] = {}
    need_rebuild_perceiver = False

    for field, new_val in updates.items():
        if field not in _EDITABLE_FIELDS:
            continue

        normalized_val = new_val
        if isinstance(normalized_val, str):
            normalized_val = normalized_val.strip() or None

        old_val = getattr(cfg, field, None)
        if old_val == normalized_val:
            continue
        setattr(cfg, field, normalized_val)
        changed[field] = True
        changed_values[field] = normalized_val
        # perception_mode 或主 LLM / 视觉 凭证变更需要重建 perceiver/context
        if field in (
            "perception_mode",
            "model",
            "api_key",
            "base_url",
            "vision_model",
            "vision_api_key",
            "vision_base_url",
            "llm_vision_capable",
        ):
            need_rebuild_perceiver = True

    # 写回 YAML（敏感字段写 config.local.yaml，非敏感写 config.yaml）
    _save_yaml(changed_values)

    # vision_timeout 只改 ctx 字段，无需重建 perceiver
    if "vision_timeout" in changed_values:
        try:
            from tools.context import get_tool_context

            ctx = get_tool_context()
            if ctx is not None:
                ctx.vision_timeout = changed_values["vision_timeout"]
                logger.info(
                    "vision_timeout hot-updated to %s", changed_values["vision_timeout"]
                )
        except Exception as exc:
            logger.warning("Failed to hot-update vision_timeout on ctx: %s", exc)

    # 热更新 perceiver
    if need_rebuild_perceiver:
        try:
            from api.server import rebuild_perceiver

            rebuild_perceiver()
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
        # 读取：优先 AppData，其次 bundle
        read_path = Path(app_paths.get_config_yaml_path())
        # 写入：始终写 AppData
        write_path = app_paths.CONFIG_YAML
        write_path.parent.mkdir(parents=True, exist_ok=True)
        existing: dict = {}
        if read_path.exists():
            with open(read_path, "r", encoding="utf-8") as f:
                existing = yaml.safe_load(f) or {}
        existing.update(public_updates)
        with open(write_path, "w", encoding="utf-8") as f:
            yaml.dump(
                existing,
                f,
                allow_unicode=True,
                default_flow_style=False,
                sort_keys=False,
            )
        logger.info(
            "config.yaml saved to %s: %s", write_path, list(public_updates.keys())
        )

    # ── 敏感 → config.local.yaml ──
    if local_updates:
        # 读取：优先 AppData，其次 bundle
        read_local = Path(app_paths.get_config_local_yaml_path())
        # 写入：始终写 AppData
        write_local = app_paths.CONFIG_LOCAL_YAML
        write_local.parent.mkdir(parents=True, exist_ok=True)
        local_existing: dict = {}
        if read_local.exists():
            with open(read_local, "r", encoding="utf-8") as f:
                local_existing = yaml.safe_load(f) or {}
        local_existing.update(local_updates)
        with open(write_local, "w", encoding="utf-8") as f:
            yaml.dump(
                local_existing,
                f,
                allow_unicode=True,
                default_flow_style=False,
                sort_keys=False,
            )
        logger.info(
            "config.local.yaml saved to %s: %s", write_local, list(local_updates.keys())
        )

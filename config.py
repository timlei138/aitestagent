from __future__ import annotations

import os
import logging
from dataclasses import dataclass
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass
class TestConfig:
    # Chat 模型（纯文本，用于 Agent 推理 / Intent 解析）
    model: str = "gpt-4o"
    llm_provider: str = "openai"
    api_key: str | None = None
    base_url: str | None = None

    # Vision 模型（多模态，用于 SmartPerceiver 截图分析）
    vision_model: str = "gpt-4o"
    vision_provider: str = "openai"
    vision_api_key: str | None = None
    vision_base_url: str | None = None

    # 非 OpenAI 兼容提供方（如智谱）可使用独立密钥
    zhipu_api_key: str | None = None
    zhipu_base_url: str | None = None

    # Embedding 模型（用于 RAG 向量化）
    embedding_model: str = "text-embedding-3-small"

    device_serial: str | None = None
    launch_activity: str | None = None

    enable_vision: bool = True
    enable_rag: bool = False
    auto_switch_perception: bool = True
    stuck_threshold: int = 2

    baseline_dir: str = "storage/baselines"
    screenshot_dir: str = "storage/screenshots"
    result_dir: str = "storage/results"
    rag_persist_dir: str = "storage/knowledge"
    report_dir: str = "reports"

    traversal_max_depth: int = 5
    traversal_max_pages: int = 50
    traversal_max_clicks: int = 300

    white_screen_threshold: float = 0.95
    black_screen_threshold: float = 0.95
    incomplete_display_ratio: float = 0.5
    critical_incomplete_ratio: float = 0.3
    phash_distance_low: int = 15
    phash_distance_medium: int = 20

    safety_level: str = "strict"
    langchain_debug: bool = True

    @classmethod
    def from_yaml(cls, path: str = "config.yaml") -> "TestConfig":
        if not logging.getLogger().handlers:
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            )
        data: dict[str, Any] = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        config = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

        # Chat 凭证（OpenAI 兼容）
        config.api_key = config.api_key or os.getenv("OPENAI_API_KEY")
        config.base_url = config.base_url or os.getenv("OPENAI_BASE_URL")

        # Vision 独立环境变量（暂不立即和 Chat 互相回退，先看 provider）
        vision_api_key_env = os.getenv("VISION_API_KEY")
        vision_base_url_env = os.getenv("VISION_BASE_URL")

        # 智谱凭证
        config.zhipu_api_key = config.zhipu_api_key or os.getenv("ZHIPU_API_KEY")
        config.zhipu_base_url = config.zhipu_base_url or os.getenv("ZHIPU_BASE_URL")

        # 按 provider 兜底填充凭证，避免跨 provider 混用 base_url
        if config.llm_provider.lower() == "zhipu" and not config.api_key:
            config.api_key = config.zhipu_api_key
        if config.llm_provider.lower() == "zhipu":
            config.base_url = config.base_url or config.zhipu_base_url

        if config.vision_provider.lower() == "zhipu":
            config.vision_api_key = config.vision_api_key or config.zhipu_api_key
            config.vision_base_url = config.vision_base_url or config.zhipu_base_url
        else:
            config.vision_api_key = (
                config.vision_api_key or vision_api_key_env or config.api_key
            )
            config.vision_base_url = (
                config.vision_base_url or vision_base_url_env or config.base_url
            )

        config.device_serial = config.device_serial or os.getenv("ANDROID_SERIAL")
        config.langchain_debug = str(
            os.getenv("LANGCHAIN_DEBUG", str(config.langchain_debug))
        ).lower() in {"1", "true", "yes", "on"}
        cls._enable_langchain_debug(config.langchain_debug)
        cls._log_provider_summary(config)
        return config

    @staticmethod
    def _enable_langchain_debug(enabled: bool) -> None:
        if not enabled:
            return
        os.environ["LANGCHAIN_DEBUG"] = "true"
        logging.getLogger("langchain").setLevel(logging.DEBUG)
        logging.getLogger("langgraph").setLevel(logging.DEBUG)
        try:
            from langchain.globals import set_debug, set_verbose

            set_debug(True)
            set_verbose(True)
            logger.info("LangChain debug enabled")
        except Exception as exc:
            logger.warning("Failed to enable LangChain debug: %s", exc)

    @staticmethod
    def _mask_secret(value: str | None) -> str:
        if not value:
            return "<empty>"
        value = str(value)
        if len(value) <= 8:
            return "***"
        return f"{value[:4]}***{value[-4:]}"

    @classmethod
    def _log_provider_summary(cls, config: "TestConfig") -> None:
        logger.info(
            "LLM provider=%s model=%s base_url=%s api_key=%s",
            config.llm_provider,
            config.model,
            config.base_url or "<default>",
            cls._mask_secret(config.api_key),
        )
        logger.info(
            "VLM provider=%s model=%s base_url=%s api_key=%s",
            config.vision_provider,
            config.vision_model,
            config.vision_base_url or "<default>",
            cls._mask_secret(config.vision_api_key),
        )

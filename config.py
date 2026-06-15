from __future__ import annotations

import os
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass
class TestConfig:
    # ── 默认 LLM（Planner / Reporter 及未覆盖时的回退）──
    llm_provider: str = "openai"
    model: str = "gpt-4o"
    api_key: str | None = None
    base_url: str | None = None

    # ── 各 Agent 独立模型（空字符串 = 使用默认 model / provider）──
    planner_model: str = ""
    planner_provider: str = ""
    planner_api_key: str | None = None
    planner_base_url: str | None = None

    executor_model: str = ""
    executor_provider: str = ""
    executor_api_key: str | None = None
    executor_base_url: str | None = None

    reviewer_model: str = ""
    reviewer_provider: str = ""
    reviewer_api_key: str | None = None
    reviewer_base_url: str | None = None

    # ── Vision 模型（SmartPerceiver 截图分析）──
    vision_model: str = "gpt-4o"
    vision_provider: str = "openai"
    vision_api_key: str | None = None
    vision_base_url: str | None = None

    # ── 非 OpenAI 兼容提供方（如智谱）──
    zhipu_api_key: str | None = None
    zhipu_base_url: str | None = None

    # ── Embedding ──
    embedding_model: str = "text-embedding-3-small"

    # ── 设备 ──

    # ── 感知 ──
    enable_vision: bool = True
    auto_switch_perception: bool = True
    stuck_threshold: int = 2

    # ── RAG ──
    enable_rag: bool = False
    rag_persist_dir: str = "storage/knowledge"

    # ── 存储 ──
    screenshot_dir: str = "storage/screenshots"
    report_dir: str = "reports"
    db_path: str = "storage/test_history.db"

    # ── 安全 / Debug ──
    safety_level: str = "strict"
    langchain_debug: bool = True

    # ── 测试计划持久化 ──
    case_dir: str = "test_cases"

    # ── 内部缓存 ──
    _agent_configs: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)

    # ──────────────── Agent 配置解析 ────────────────

    def agent_config(self, role: str) -> dict[str, Any]:
        """返回指定 Agent 角色的 (provider, model, api_key, base_url)。"""
        if role in self._agent_configs:
            return self._agent_configs[role]

        provider = (getattr(self, f"{role}_provider", "") or "").strip()
        model = (getattr(self, f"{role}_model", "") or "").strip()
        api_key = getattr(self, f"{role}_api_key", None)
        base_url = getattr(self, f"{role}_base_url", None)

        cfg = {
            "provider": provider or self.llm_provider,
            "model": model or self.model,
            "api_key": api_key or self.api_key,
            "base_url": base_url or self.base_url,
        }
        self._agent_configs[role] = cfg
        return cfg

    # ──────────────── YAML 加载 ────────────────

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

        # 只取 dataclass 中定义的字段
        config = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

        # ── 凭证回退链 ──
        config.api_key = config.api_key or os.getenv("OPENAI_API_KEY")
        config.base_url = config.base_url or os.getenv("OPENAI_BASE_URL")

        config.zhipu_api_key = config.zhipu_api_key or os.getenv("ZHIPU_API_KEY")
        config.zhipu_base_url = config.zhipu_base_url or os.getenv("ZHIPU_BASE_URL")

        # 默认 LLM → zhipu
        if config.llm_provider.lower() == "zhipu" and not config.api_key:
            config.api_key = config.zhipu_api_key
        if config.llm_provider.lower() == "zhipu":
            config.base_url = config.base_url or config.zhipu_base_url

        # Vision
        vision_api_key_env = os.getenv("VISION_API_KEY")
        vision_base_url_env = os.getenv("VISION_BASE_URL")
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

        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = log_dir / f"langchain_{timestamp}.log"

        # LangChain [chain/end] 等日志通过 print() 直接输出，不走 logging 模块
        # 所以需要 tee stdout 到文件才能捕获
        # utf-8-sig 写入 BOM，防止编辑器（如 VSCode）误判编码导致中文乱码
        _stdout_log = open(log_file, "w", encoding="utf-8-sig")

        _orig_stdout = sys.stdout
        _orig_encoding = getattr(_orig_stdout, "encoding", "utf-8") or "utf-8"

        class _Tee:
            def __init__(self, *files):
                self.files = files

            @property
            def encoding(self):
                return _orig_encoding

            def write(self, obj):
                for f in self.files:
                    f.write(obj)
                    f.flush()

            def flush(self):
                for f in self.files:
                    f.flush()

        sys.stdout = _Tee(_orig_stdout, _stdout_log)

        # logging 模块也加上 langchain / langgraph 的 handler（捕获少数走 logger 的消息）
        for name in ("langchain", "langchain_core", "langgraph"):
            lg = logging.getLogger(name)
            lg.setLevel(logging.DEBUG)

        try:
            from langchain_core.globals import set_debug, set_verbose

            set_debug(True)
            set_verbose(True)
        except Exception:
            pass

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
        for role in ("planner", "executor", "reviewer"):
            cfg = config.agent_config(role)
            logger.info(
                "[%s] provider=%s model=%s base_url=%s api_key=%s",
                role,
                cfg["provider"],
                cfg["model"],
                cfg["base_url"] or "<default>",
                cls._mask_secret(cfg["api_key"]),
            )
        logger.info(
            "[vision] provider=%s model=%s base_url=%s api_key=%s",
            config.vision_provider,
            config.vision_model,
            config.vision_base_url or "<default>",
            cls._mask_secret(config.vision_api_key),
        )

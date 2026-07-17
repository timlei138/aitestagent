from __future__ import annotations

import os
import logging
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

import app_paths

logger = logging.getLogger(__name__)


@dataclass
class TestConfig:
    # ── LLM ──
    llm_provider: str = "openai"
    model: str = "gpt-4o"
    api_key: str | None = None
    base_url: str | None = None

    # ── 非 OpenAI 兼容提供方（如智谱）──
    zhipu_api_key: str | None = None
    zhipu_base_url: str | None = None

    # ── Embedding ──
    embedding_provider: str = "huggingface"
    embedding_model: str = "BAAI/bge-large-zh-v1.5"
    embedding_api_key: str | None = None
    embedding_base_url: str | None = None

    # ── 感知模式: "hybrid" | "ui_tree" ──
    perception_mode: str = "hybrid"

    # ── RAG ──
    rag_persist_dir: str = ""

    # ── 存储 ──
    db_path: str = ""

    # ── 安全 / Debug ──
    safety_level: str = "strict"
    langchain_debug: bool = True
    vision_enabled: bool = True

    # ── 上下文优化 (O2) ──
    # 历史消息中，除最新一次外的 get_screen_info 大输出折叠为占位符，
    # 抑制上下文/token 膨胀（最新一份仍保留全量）。可在 config.yaml 关闭。
    context_summarize_stale_screens: bool = True

    # ── 降低模型依赖 (M1) ──
    # 单轮启发式软提示（FINALIZATION/KNOWLEDGE_QUERY/SELF_DOUBT/APP_SWITCH）
    # 多条命中时只注入优先级最高的 1 条，避免弱模型被多条提示淹没。
    # 关闭则回退旧行为（全部注入）。
    single_hint_per_turn: bool = True

    # ── 本地可观测 ──
    # 每次运行结束把「逐轮动作 + 状态码 + match_mode/fallback + token + 验证」
    # 串成结构化 JSON 落盘到 logs/runs/*_trace.json（离线，替代 LangSmith 云）。
    write_run_trace: bool = True

    # ── 点击策略 (L3 kill switch) ──
    # legacy: 精确参数不存在时走语义搜索 + fallback 兜底（当前默认）。
    # native_strict: 精确参数不存在时直接返回 AMBIGUOUS，强制 LLM 下精确参数；
    #   精确参数存在时行为与 legacy 一致。用于验证 LLM 能否脱离语义搜索独立工作，
    #   是下线 ~500 行 legacy 语义匹配代码的前置开关。
    click_mode: str = "legacy"

    # ──────────────── YAML 加载 ────────────────

    @classmethod
    def from_yaml(cls, path: str = "") -> "TestConfig":
        if not path:
            path = app_paths.get_config_yaml_path()
        if not logging.getLogger().handlers:
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            )
        cls._ensure_service_log_handler()

        data: dict[str, Any] = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}

        # ── 加载本地敏感配置（API Key 等，已在 .gitignore 中）──
        local_path = app_paths.get_config_local_yaml_path()
        if os.path.exists(local_path):
            with open(local_path, "r", encoding="utf-8") as f:
                local_data = yaml.safe_load(f) or {}
            data.update(local_data)
            logger.info("Loaded local overrides from %s", local_path)

        # 只取 dataclass 中定义的字段
        config = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

        # 回退到 app_paths 默认路径
        if not config.rag_persist_dir:
            config.rag_persist_dir = app_paths.KNOWLEDGE_DIR_STR
        elif not os.path.isabs(config.rag_persist_dir):
            # 相对路径 → 转为 AppData 下的绝对路径
            config.rag_persist_dir = str(app_paths.DATA_DIR / config.rag_persist_dir)
        if not config.db_path:
            config.db_path = app_paths.DB_PATH_STR
        elif not os.path.isabs(config.db_path):
            config.db_path = str(app_paths.DATA_DIR / config.db_path)

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

        # LangChain 的逐次运行日志由 start_run_log() tee 到 logs/runs/*_langchain.log。
        # 这里仅打开调试开关，不在进程启动时创建单独 boot 日志文件。
        for name in ("langchain", "langgraph"):
            lg = logging.getLogger(name)
            lg.setLevel(logging.WARNING)
        # langchain_core callbacks 会产生 KeyError('input') 冗余警告，抑制到 ERROR
        logging.getLogger("langchain_core.callbacks.manager").setLevel(logging.ERROR)
        logging.getLogger("langchain_core").setLevel(logging.WARNING)

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
        logger.info(
            "[llm] provider=%s model=%s base_url=%s api_key=%s",
            config.llm_provider,
            config.model,
            config.base_url or "<default>",
            cls._mask_secret(config.api_key),
        )

    @classmethod
    def _ensure_service_log_handler(cls) -> None:
        """确保服务状态日志仅写入 logs/service.log，且避免重复注册 handler。"""
        app_paths.ensure_dirs()
        root = logging.getLogger()

        for h in root.handlers:
            if getattr(h, "_service_log_handler", False):
                return

        class _ExcludeLangchainLogs(logging.Filter):
            def filter(self, record: logging.LogRecord) -> bool:
                return not (
                    record.name.startswith("langchain")
                    or record.name.startswith("langgraph")
                )

        fh = logging.FileHandler(app_paths.SERVICE_LOG, encoding="utf-8")
        fh._service_log_handler = True
        fh.setLevel(logging.INFO)
        fh.addFilter(_ExcludeLangchainLogs())
        fh.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
        )
        root.addHandler(fh)
        logger.info("Service log file: %s", app_paths.SERVICE_LOG)


def resolve_perception_mode(config: TestConfig) -> tuple[str, bool]:
    """根据 perception_mode 配置解析 Perceiver 参数。

    Returns: (mode, auto_switch)
    """
    from device.perceiver import PerceptionMode

    mode = config.perception_mode.lower()
    if mode == "ui_tree":
        return (PerceptionMode.UI_TREE, False)
    if mode not in {"hybrid", "ui_tree"}:
        logger.warning("Unknown perception_mode=%s, fallback to hybrid", mode)
    return (PerceptionMode.HYBRID, True)


# ── 单次运行日志 ──


def start_run_log(run_id: str) -> dict:
    """为单次测试运行创建独立 langchain 日志文件。"""
    import re

    app_paths.ensure_dirs()
    run_dir = app_paths.LOG_RUN_DIR
    run_dir.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[<>:\"/\\|?* ]", "_", run_id)[:60]
    ts = datetime.now().strftime("%H%M%S")

    # ── langchain 日志 ──
    lc_path = run_dir / f"{ts}_{safe_id}_langchain.log"
    lc_file = open(lc_path, "w", encoding="utf-8-sig")

    _orig_stdout = sys.stdout
    _orig_encoding = getattr(_orig_stdout, "encoding", "utf-8") or "utf-8"

    class _Tee:
        def write(self, s):
            _orig_stdout.write(s)
            if lc_file and not lc_file.closed:
                try:
                    lc_file.write(s)
                except Exception:
                    pass

        def flush(self):
            _orig_stdout.flush()
            if lc_file and not lc_file.closed:
                try:
                    lc_file.flush()
                except Exception:
                    pass

        @property
        def encoding(self):
            return _orig_encoding

    sys.stdout = _Tee()

    def cleanup():
        sys.stdout = _orig_stdout
        lc_file.close()

    return {"langchain_file": lc_path, "cleanup": cleanup}

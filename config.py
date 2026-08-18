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
    model: str = "gpt-4o"
    api_key: str | None = None
    base_url: str | None = None

    # ── 感知模式: "hybrid" | "ui_tree" ──
    perception_mode: str = "hybrid"

    # ── RAG ──
    rag_persist_dir: str = ""

    # ── 存储 ──
    db_path: str = ""

    # ── 安全 / Debug ──
    safety_level: str = "strict"
    langchain_debug: bool = True

    # ── 视觉备用模型（可选）──
    # 当主模型不是多模态时，配置此处可让视觉能力走独立模型；
    # 若主模型本身支持视觉，可开启下方的 llm_vision_capable。
    # ⚠️ 若将 vision_api_key 加入 config_routes._EDITABLE_FIELDS，
    #    必须同时加入 _SECRET_FIELDS，否则密钥会写入 config.yaml（已 git 跟踪）
    vision_model: str | None = None
    vision_api_key: str | None = None
    vision_base_url: str | None = None
    # 主模型本身是否支持多模态/视觉。开启后即使不配置 vision_model，
    # 也会尝试用主模型处理图片；关闭则必须配置 vision_model 才启用视觉。
    llm_vision_capable: bool = False
    # 视觉调用超时秒数（visual_check / detect_overlay / vision_tap）
    # DashScope qwen3.7-flash 实测响应 5-20s，偶发排队超 30s，默认 60s 留余量。
    vision_timeout: int = 60

    @property
    def vision_enabled(self) -> bool:
        """视觉是否启用：配置了独立视觉模型，或主模型本身支持视觉。"""
        return bool(self.vision_model or self.llm_vision_capable)

    # ── 上下文历史步数（摘要层）──
    # agent 每轮注入的 step_history 摘要条数（原硬编码 10）
    context_history_steps: int = 5

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

    # ── Phase 3: 计划执行与环境兼容 ──
    # fixture 指纹占位符；真实 fixture 生命周期指纹接入前使用稳定配置值。
    fixture_profile: str = "default"
    # 动作质量每日衰减系数（0 表示不衰减）。
    quality_decay_lambda: float = 0.01
    # plan action 平均质量低于此值时撤销 direct 准入。
    direct_quality_threshold: float = 0.5
    # 环境兼容分不低于此值时才允许进入 guided（1.0 等价于精确匹配）。
    environment_guided_threshold: float = 0.7
    # 同一兼容键下至少累计成功运行 N 次，才允许进入 direct（Plan 4.1 连续 N 次准入）。
    direct_min_runs: int = 2
    # 每个动作 postcondition 通过率不低于此值时，才满足 direct 全动作对齐闸门。
    direct_postcond_rate_threshold: float = 0.8

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
            config.rag_persist_dir = app_paths.KNOWLEDGE_V2_DIR_STR
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

        # ── 视觉模型凭证回退链 ──
        config.vision_api_key = config.vision_api_key or os.getenv("VISION_API_KEY")
        config.vision_base_url = config.vision_base_url or os.getenv("VISION_BASE_URL")

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
            "[llm] model=%s base_url=%s api_key=%s",
            config.model,
            config.base_url or "<default>",
            cls._mask_secret(config.api_key),
        )
        if config.vision_model or config.llm_vision_capable:
            v_model, v_api_key, v_base_url = resolve_vision_credentials(
                llm_model=config.model,
                llm_api_key=config.api_key,
                llm_base_url=config.base_url,
                vision_model=config.vision_model,
                vision_api_key=config.vision_api_key,
                vision_base_url=config.vision_base_url,
            )
            logger.info(
                "[vision] model=%s base_url=%s api_key=%s",
                v_model,
                v_base_url or "<default>",
                cls._mask_secret(v_api_key),
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


def resolve_vision_credentials(
    *,
    llm_model: str | None,
    llm_api_key: str | None,
    llm_base_url: str | None,
    vision_model: str | None,
    vision_api_key: str | None,
    vision_base_url: str | None,
) -> tuple[str | None, str | None, str | None]:
    """解析视觉调用真正要使用的 model/api_key/base_url。

    规则：
    - model：vision_model 优先，否则回退主模型 model。
    - api_key / base_url：
      - 如果 vision_base_url 显式设置且与 llm_base_url 不同，视为独立视觉端点，
        仅使用 vision_api_key / vision_base_url，不回退主模型凭证。
      - 否则视为同一端点，vision_api_key / vision_base_url 可回退到主模型。
    """
    effective_model = vision_model or llm_model

    separate_endpoint = bool(vision_base_url and vision_base_url != llm_base_url)

    if separate_endpoint:
        effective_api_key = vision_api_key
        effective_base_url = vision_base_url
    else:
        effective_api_key = vision_api_key or llm_api_key
        effective_base_url = vision_base_url or llm_base_url

    return effective_model, effective_api_key, effective_base_url


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

    return {"langchain_file": str(lc_path), "cleanup": cleanup}


def append_run_log(file_path: str) -> dict:
    """续写到已有的 langchain 日志文件（resume 时使用）。"""
    import os as _os

    lc_file = open(file_path, "a", encoding="utf-8-sig")
    _orig_stdout = sys.stdout
    _orig_encoding = getattr(_orig_stdout, "encoding", "utf-8") or "utf-8"

    # 写入分隔标记，方便区分 run 和 resume 阶段
    _ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lc_file.write(f"\n{'='*60}\n")
    lc_file.write(f"=== RESUME {_ts}\n")
    lc_file.write(f"{'='*60}\n\n")
    lc_file.flush()

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

    return {"cleanup": cleanup}

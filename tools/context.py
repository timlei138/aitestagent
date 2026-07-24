from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolContext:
    """统一工具上下文 — 所有 Tool 通过此对象获取依赖。"""

    device: Any
    perceiver: Any
    report_logger: Any | None = None
    knowledge_base: Any | None = None
    relational_db: Any = None  # V2: 注入已有的 SqliteBackend 实例，避免重复创建连接
    safety_level: str = "strict"
    llm_provider: str = ""
    llm_model: str = ""
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    llm_vision_enabled: bool = True
    verification_auto_vision: bool = True
    # M4：确定性断言（assert_page_contains/assert_element_exists）作为 ground truth
    # 参与 assert_verification 结果核实。默认「仅证据」（annotate 不改判定）；
    # 置 True 时开启「硬核实」——代码核实与模型判定冲突时按代码结果修正。
    deterministic_verification_override: bool = False
    # L3 kill switch：点击策略分流
    # legacy: 精确参数不存在时走语义搜索；native_strict: 精确参数不存在→AMBIGUOUS
    click_mode: str = "legacy"
    _screen_size: tuple[int, int] | None = field(default=None, repr=False)
    _ws_emit: Any = field(
        default=None, repr=False
    )  # WebSocket 实时事件回调 (type, payload) -> None
    _click_preferences: dict[str, Any] = field(
        default_factory=dict, repr=False
    )  # RAG 解析出的点击偏好（仅当前 run）
    _last_screenshot_path: str = (
        ""  # perceive() cache miss 时自动存盘的截图路径，assert_verification 失败时回退
    )
    # RAG 查询缓存与观测计数器
    _rag_query_cache: dict[str, str] = field(default_factory=dict, repr=False)
    _rag_query_count: int = 0
    _rag_same_app_count: int = 0
    _rag_cross_app_count: int = 0
    _rag_empty_hit_count: int = 0
    _run_tag: str = ""  # 当前 run 标识，用于缓存键隔离
    # Reactive Permission Intent：声明式权限测试意图，跨 click() 调用持久。
    # 格式：{"permission": "camera", "action": "deny", "set_time": <monotonic>}
    # 空 dict = 未设置；TTL 120s 自动过期（click.py 内部检查）。
    _permission_intent: dict = field(default_factory=dict, repr=False)
    # M4：确定性断言结果记录（{"text","kind","result": "pass"/"fail"}），
    # assert_verification 反查最近一条与验证项匹配的确定性核实作为 ground truth。
    _deterministic_checks: list = field(default_factory=list, repr=False)
    # unknown 验证返回过的当前页相关可交互事实签名，避免同页重复提示。
    _verification_interactive_facts_seen: set[str] = field(
        default_factory=set, repr=False
    )
    # O1：单次运行 token 消耗累计（纯观测）。每次 LLM 调用累加 usage_metadata。
    _token_usage: dict = field(
        default_factory=lambda: {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cached_input_tokens": 0,
            "llm_calls": 0,
        },
        repr=False,
    )
    # 手动停止事件（threading.Event 而非 asyncio.Event：start() 同步路径
    # 跑在 asyncio.to_thread 的工作线程上，Event.is_set() 是 O(1) 原子读）。
    # orchestrator.request_stop 会 set；节点入口 / _run_agent 主循环检查。
    # run 结束（finally）清回 None，避免下个 run 看到上一次的 flag。
    _stop_event: threading.Event | None = field(default=None, repr=False)
    # B：循环/空转守卫（run 级持久，跨 _run_agent 调用累计）。含 no_progress
    # 计数 / 内层循环签名 / 语义冷却映射，确保长空转（跨多次图迭代）也能被熔断。
    # 每次 run 由 orchestrator._reset_run_scoped 重置。
    _loop_guard: dict = field(
        default_factory=lambda: {
            "_no_progress_count": 0,
            "_no_progress_warned": False,
            "_recent_call_sigs": [],
            "_recent_action_groups": [],
            "_cooldown_map": {},
        },
        repr=False,
    )


    @property
    def screen_size(self) -> tuple[int, int]:
        """懒加载并缓存屏幕分辨率。设备运行期分辨率不变，只需 snapshot() 一次。
        _query_known_identities 和 click 兜底都通过此属性获取当前屏幕尺寸，
        用于 query_element_identity(target_screen=...) 的 bounds 百分比换算。
        """
        if self._screen_size is None:
            try:
                snap = self.device.snapshot()
                self._screen_size = (snap.width, snap.height)
            except Exception:
                self._screen_size = (0, 0)
        return self._screen_size


# ── 进程级唯一 ToolContext（所有 Tool 通过 get_tool_context() 获取依赖）──
# 从 tools/__init__.py 拆出（重构 T2），使各 tools 子模块可直接
# `from tools.context import get_tool_context` 而不产生循环依赖。
_CONTEXT: "ToolContext | None" = None


def set_tool_context(context: "ToolContext") -> None:
    global _CONTEXT
    _CONTEXT = context
    # 延迟 import 避免加载期循环依赖（reset_session_click_ids 仍在 tools/__init__.py）
    from tools import reset_session_click_ids

    reset_session_click_ids()  # 每次新执行时重置 session 去重


def get_tool_context() -> "ToolContext":
    if _CONTEXT is None:
        raise RuntimeError("ToolContext 未初始化")
    return _CONTEXT

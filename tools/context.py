from __future__ import annotations

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

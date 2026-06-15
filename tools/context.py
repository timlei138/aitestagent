from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ToolContext:
    """统一工具上下文 — 所有 Tool 通过此对象获取依赖。"""

    device: object
    perceiver: object
    report_logger: object | None = None
    knowledge_base: object | None = None
    safety_level: str = "strict"

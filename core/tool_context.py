from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ToolContext:
    """统一工具上下文 — 所有 Tool 通过此对象获取依赖。

    避免多个模块各自持有不同 device/perceiver 实例导致状态不一致。
    """

    device: object
    perceiver: object
    baseline_store: object
    anomaly_detector: object
    safety_guard: object | None = None
    report_logger: object | None = None
    knowledge_base: object | None = None
    state_machine: object | None = None

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable


@dataclass
class ReportBuilder:
    """测试报告生成器，支持 WebSocket 实时事件广播。

    Attributes:
        event_callback: 可选的事件回调，签名为 callback(event_type: str, payload: dict)。
            用于向 WebSocket 推送执行进度。
    """

    name: str
    mode: str
    app_package: str = ""
    report_dir: str = "reports"
    steps: list[dict[str, Any]] = field(default_factory=list)
    anomalies: list[dict[str, Any]] = field(default_factory=list)
    started_at: datetime = field(default_factory=datetime.now)
    _event_callback: Callable[[str, dict[str, Any]], None] | None = None

    def set_event_callback(
        self, callback: Callable[[str, dict[str, Any]], None]
    ) -> None:
        """设置事件广播回调，用于实时推送执行进度。"""
        self._event_callback = callback

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._event_callback:
            try:
                self._event_callback(event_type, payload)
            except Exception:
                pass

    def log_step(
        self,
        intent: str,
        action: str = "",
        target: str = "",
        status: str = "success",
        **extra,
    ) -> None:
        step_index = len(self.steps) + 1
        entry: dict[str, Any] = {
            "index": step_index,
            "intent": intent,
            "action": action,
            "target": target,
            "status": status,
            "timestamp": datetime.now().isoformat(),
            **extra,
        }
        self.steps.append(entry)

        # 实时推送 step 事件
        self._emit(
            "step_end",
            {"step": step_index, "intent": intent, "status": status, "content": intent},
        )

    def log_step_start(self, step: int, content: str) -> None:
        """推送步骤开始事件（由调用方在步骤执行前调用）。"""
        self._emit("step_start", {"step": step, "content": content})

    def log_anomaly(self, anomaly: dict[str, Any]) -> None:
        self.anomalies.append(anomaly)
        severity = anomaly.get("severity", "medium")
        message = anomaly.get("description", str(anomaly))
        self._emit(
            "anomaly", {"severity": severity, "message": message, "detail": anomaly}
        )

    def log_snapshot(
        self, image_base64: str = "", elements: list[dict[str, Any]] | None = None
    ) -> None:
        """推送截图快照事件。"""
        self._emit("snapshot", {"image": image_base64, "elements": elements or []})

    def log_stream(self, event_type: str, payload: dict[str, Any]) -> None:
        """推送自定义流式事件。"""
        self._emit(event_type, payload)

    def save(
        self,
        conclusion: str = "",
        status: str = "success",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        os.makedirs(self.report_dir, exist_ok=True)
        duration = (datetime.now() - self.started_at).total_seconds()
        report: dict[str, Any] = {
            "name": self.name,
            "mode": self.mode,
            "app_package": self.app_package,
            "status": status,
            "conclusion": conclusion,
            "duration_seconds": duration,
            "steps": self.steps,
            "anomalies": self.anomalies,
            "created_at": datetime.now().isoformat(),
            **(extra or {}),
        }
        path = os.path.join(
            self.report_dir,
            f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
        )
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        report["report_path"] = path

        # 推送结果事件
        self._emit(
            "result", {"status": status, "conclusion": conclusion, "report_path": path}
        )
        return report

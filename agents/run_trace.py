"""本地逐轮运行 trace（离线可观测，替代 LangSmith 云）。

把单次运行的动作日志 + 结果 + 指标 + token 串成一个结构化 JSON，落盘到
logs/runs/*_trace.json，用于 turn-by-turn 定位「模型决策 / 工具契约 / 感知」
哪一步出问题——不外发任何数据，契合本地 exe 部署。

数据全部来自运行期已有产物（`_tool_calls_log` / verification_results / token_usage
/ reporter 指标），本模块只做汇总与落盘，不改变任何执行行为。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any

import app_paths
from tools.results import parse_status

logger = logging.getLogger(__name__)


def build_run_trace(
    *,
    run_id: str,
    user_request: str,
    app_package: str,
    app_name: str,
    execution_status: str,
    test_verdict: str,
    duration_seconds: float,
    tool_log: list[dict[str, Any]] | None,
    verification_results: list[dict[str, Any]] | None,
    token_usage: dict[str, Any] | None,
    metrics: dict[str, Any] | None,
) -> dict[str, Any]:
    """把运行期产物汇总为一份结构化 trace（纯数据转换，绝不抛异常）。"""
    steps: list[dict[str, Any]] = []
    for e in tool_log or []:
        if not isinstance(e, dict):
            continue
        obs = str(e.get("observation", "") or "")
        step: dict[str, Any] = {
            "seq": e.get("tool_seq"),
            "tool": e.get("name", ""),
            "target": e.get("target", ""),
            # L1 契约状态码（OK/NOT_FOUND/AMBIGUOUS/ERROR/...）；旧格式为空串
            "status": parse_status(obs),
            "intent": e.get("intent_text", ""),
            "observation": obs,
            "screenshot": e.get("screenshot_path", ""),
        }
        if e.get("name") == "click":
            step["match_mode"] = e.get("match_mode", "")
            step["fallback_used"] = bool(e.get("fallback_used", False))
            step["tool_input"] = e.get("tool_input", {})
        steps.append(step)

    return {
        "run_id": run_id,
        "created_at": datetime.now().isoformat(),
        "request": user_request,
        "app": {"package": app_package, "name": app_name},
        "result": {
            "execution_status": execution_status,
            "test_verdict": test_verdict,
            "duration_seconds": round(float(duration_seconds or 0), 2),
        },
        "metrics": metrics or {},
        "token_usage": token_usage or {},
        "verifications": verification_results or [],
        "step_count": len(steps),
        "steps": steps,
    }


def write_run_trace(trace: dict[str, Any]) -> str:
    """把 trace 落盘到 logs/runs/{ts}_{run_id}_trace.json，返回路径（失败返回 ""）。"""
    try:
        app_paths.ensure_dirs()
        run_dir = app_paths.LOG_RUN_DIR
        run_dir.mkdir(parents=True, exist_ok=True)
        safe_id = re.sub(r'[<>:"/\\|?* ]', "_", str(trace.get("run_id", "") or ""))[:60]
        if not safe_id:
            safe_id = "run"
        ts = datetime.now().strftime("%H%M%S")
        path = run_dir / f"{ts}_{safe_id}_trace.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(trace, f, ensure_ascii=False, indent=2)
        return str(path)
    except Exception as exc:  # 观测功能绝不影响主流程
        logger.warning("write_run_trace failed: %s", exc)
        return ""

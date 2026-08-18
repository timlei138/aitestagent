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


def compute_resolution_metrics(tool_log: list[dict[str, Any]] | None) -> dict[str, int]:
    """Phase 4 locator 解析指标：从工具日志汇总 exact/semantic 解析次数 + 标签错配次数。

    - resolution_type == "exact"    → exact_resolution_count
    - resolution_type == "semantic"  → semantic_resolution_count
    - fuzzy_match (标签错配) 且请求/命中 label 都非空 → label_mismatch_count

    空 label 不计入 mismatch（修复「空 label 被当作 mismatch」的统计偏差；
    click 工具已保证双方非空才置 fuzzy_match）。
    """
    exact_resolution_count = 0
    semantic_resolution_count = 0
    label_mismatch_count = 0
    for e in tool_log or []:
        if not isinstance(e, dict) or e.get("name") != "click":
            continue
        rtype = e.get("resolution_type", "")
        if rtype == "exact":
            exact_resolution_count += 1
        elif rtype == "semantic":
            semantic_resolution_count += 1
        # 标签错配：仅当请求 label 与命中 label 都非空且不一致。
        req = e.get("requested_label", "") or ""
        res = e.get("resolved_label", "") or ""
        if req and res and req != res:
            label_mismatch_count += 1
    return {
        "exact_resolution_count": exact_resolution_count,
        "semantic_resolution_count": semantic_resolution_count,
        "label_mismatch_count": label_mismatch_count,
    }


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
    # Phase 2/3 执行模式状态机透出（Plan §2）：字段由 mode_selection_node 写入 state，
    # 这里只做观测层透出，不新增任何模式决策逻辑（契约收敛，不堆补丁）。
    execution_mode: str = "",
    lifecycle_state: str = "",
    plan_id: str = "",
    plan_trust: str = "",
    mode_selection_reason: str = "",
    mode_transition_events: list[dict[str, Any]] | None = None,
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
            "tool_input": e.get("tool_input", {}),
        }
        if e.get("name") == "click":
            step["match_mode"] = e.get("match_mode", "")
            step["fallback_used"] = bool(e.get("fallback_used", False))
            step["resolution_type"] = e.get("resolution_type", "")
            step["requested_label"] = e.get("requested_label", "")
            step["resolved_label"] = e.get("resolved_label", "")
            step["fuzzy_match"] = bool(e.get("fuzzy_match", False))
        steps.append(step)

    # Phase 4 locator 解析指标：exact/semantic 解析次数 + 标签错配次数。
    # 复用 compute_resolution_metrics（reporter 持久化同一份，避免逻辑分叉）。
    resolution_metrics = compute_resolution_metrics(tool_log)
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
        # 执行模式状态机（Plan §2）：仅观测透出，供 Gap Plan P2 验收「trace 中可见
        # execution_mode 且至少有一次 run 进入 direct/guided」使用。
        "execution": {
            "mode": str(execution_mode or "explore"),
            "lifecycle_state": str(lifecycle_state or ""),
            "plan_id": str(plan_id or ""),
            "plan_trust": str(plan_trust or ""),
            "mode_selection_reason": str(mode_selection_reason or ""),
            "mode_transition_events": list(mode_transition_events or []),
        },
        "metrics": metrics or {},
        "resolution_metrics": resolution_metrics,
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

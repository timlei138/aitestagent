from __future__ import annotations

import os
import re
import time
import logging
from typing import Any

import yaml
from core.smart_perceiver import PerceptionMode

logger = logging.getLogger(__name__)


class ReplayRunner:
    """YAML 用例回放执行器。

    支持 baseline 对比模式（check_baseline=True）和 StateMachine 异常恢复。
    """

    def __init__(self, context, report_builder=None, check_baseline: bool = False):
        self.context = context
        self.report = report_builder
        self.check_baseline = check_baseline
        self._collected_steps: list[dict[str, Any]] = []

    def run_case_file(self, case_file: str) -> dict[str, Any]:
        logger.info("Replay run_case_file case=%s", case_file)
        if not os.path.exists(case_file):
            return {"status": "error", "message": f"用例文件不存在: {case_file}"}
        with open(case_file, "r", encoding="utf-8") as f:
            case = yaml.safe_load(f) or {}
        return self.run_case(case)

    def run_case(self, case: dict[str, Any]) -> dict[str, Any]:
        package = case.get("app_package", "")
        self._collected_steps = []
        logger.info("Replay run_case name=%s package=%s", case.get("name", ""), package)

        if package:
            logger.info("Replay prelaunch package=%s", package)
            self.context.device.app_start(package)
            time.sleep(2)
            current = self.context.device.current_app()
            logger.info(
                "Replay prelaunch done current=%s/%s",
                current.get("package", ""),
                current.get("activity", ""),
            )

        # ── Baseline 预检 ──
        if self.check_baseline and package:
            pages = self.context.baseline_store.list_pages(package)
            if not pages:
                return {
                    "status": "error",
                    "message": f"未找到 {package} 的基线数据，请先 traverse",
                }

        # ── StateMachine 重置 ──
        if self.context.state_machine:
            self.context.state_machine.reset()

        failures: list[str] = []
        baseline_diffs: list[dict[str, Any]] = []

        pending_health_check = False
        previous_step_index = 0
        previous_step_intent = ""
        for i, step in enumerate(case.get("steps", []), 1):
            if package and pending_health_check:
                logger.info(
                    "Replay health check before next step next_idx=%s prev_idx=%s prev_intent=%s package=%s",
                    i,
                    previous_step_index,
                    previous_step_intent,
                    package,
                )
                detection = self.context.anomaly_detector.detect(
                    package, check_baseline=False
                )
                if not detection.is_healthy:
                    anomaly_str = str(detection.to_dict())
                    logger.warning(
                        "Replay health check failed before step idx=%s prev_idx=%s msg=%s",
                        i,
                        previous_step_index,
                        anomaly_str,
                    )
                    if self.report:
                        self.report.log_anomaly(
                            {
                                "severity": "high",
                                "description": anomaly_str,
                            }
                        )
                    failures.append(anomaly_str)
                    if self.context.state_machine:
                        self.context.state_machine.recover_app(package)
                    break
            logger.info(
                "Replay step start idx=%s type=%s intent=%s target=%s",
                i,
                step.get("type", ""),
                step.get("intent", ""),
                step.get("target") or step.get("tab_name") or step.get("text") or "",
            )
            # ── StateMachine 弹窗检测 ──
            if self.context.state_machine:
                popup_buttons = self.context.state_machine.detect_popup()
                if popup_buttons:
                    self.context.state_machine.recover_popup()

            # 推送步骤开始事件
            if self.report:
                self.report.log_step_start(i, step.get("intent", ""))

            ok, msg = self._execute_step(step)
            step_record: dict[str, Any] = {
                "index": i,
                "intent": step.get("intent", ""),
                "action": step.get("type", ""),
                "target": step.get("target")
                or step.get("tab_name")
                or step.get("text")
                or "",
                "status": "success" if ok else "fail",
                "message": msg,
            }
            self._collected_steps.append(step_record)

            if self.report:
                self.report.log_step(
                    step.get("intent", ""),
                    action=step.get("type", ""),
                    target=step.get("target")
                    or step.get("tab_name")
                    or step.get("text")
                    or "",
                    status="success" if ok else "fail",
                    message=msg,
                )

            if not ok:
                logger.warning("Replay step failed idx=%s msg=%s", i, msg)
                failures.append(msg)
                pending_health_check = False
                # 尝试恢复
                if self.context.state_machine and package:
                    self.context.state_machine.recover_app(package)
                break
            logger.info("Replay step success idx=%s msg=%s", i, msg)
            pending_health_check = bool(package)
            previous_step_index = i
            previous_step_intent = str(step.get("intent", "") or "")

            # ── Baseline 对比 ──
            if self.check_baseline and package:
                current_key = self._current_page_key()
                screenshot = self.context.device.screenshot()
                baseline = self.context.baseline_store.find_best_match(
                    package,
                    screenshot,
                    threshold=getattr(
                        getattr(self.context, "anomaly_detector", None),
                        "config",
                        None,
                    )
                    or 15,
                )
                if baseline:
                    detection = self.context.anomaly_detector.detect(
                        package, page_key=baseline.page_key, check_baseline=True
                    )
                    if not detection.is_healthy:
                        diff = detection.to_dict()
                        diff["step"] = i
                        diff["step_intent"] = step.get("intent", "")
                        baseline_diffs.append(diff)
                        if self.report:
                            self.report.log_anomaly(
                                {
                                    "severity": "medium",
                                    "description": f"步骤{i}与基线差异: {diff}",
                                }
                            )

        conclusion = "PASS" if not failures else f"FAIL: {failures[0]}"
        logger.info("Replay finished status=%s conclusion=%s", "success" if not failures else "fail", conclusion)
        return {
            "status": "success" if not failures else "fail",
            "name": case.get("name", ""),
            "app_package": case.get("app_package", ""),
            "conclusion": conclusion,
            "failures": failures,
            "baseline_diffs": baseline_diffs,
            "steps": self._collected_steps,
        }

    def _execute_step(self, step: dict[str, Any]) -> tuple[bool, str]:
        step_type = step.get("type", "")
        target = (
            step.get("target")
            or step.get("tab_name")
            or step.get("text")
            or step.get("key")
            or ""
        )
        try:
            if step_type == "launch_app":
                launch_package = step.get("package") or step.get("app_package", "")
                launch_activity = step.get("activity") or step.get("launch_activity", "")
                logger.info(
                    "Replay launch_app step package=%s activity=%s",
                    launch_package,
                    launch_activity,
                )
                if launch_activity:
                    self.context.device.app_start(
                        launch_package,
                        activity=launch_activity,
                    )
                else:
                    self.context.device.app_start(launch_package)

            elif step_type in {"click", "navigate_tab"}:
                if step_type == "navigate_tab":
                    if not self._click_navigation_tab(step.get("tab_name", target)):
                        return False, f"未找到导航项: {target}"
                else:
                    if not self._click_target(target):
                        return False, f"未找到元素: {target}"

            elif step_type in {"type_text", "search"}:
                text = step.get("text", "")
                if step_type == "search":
                    # 搜索：先点击搜索框
                    self.context.device.click_text(
                        "搜索", timeout=0.5
                    ) or self.context.device.click_resource_id("search", timeout=0.5)
                    time.sleep(0.3)
                else:
                    focus_hint = (
                        step.get("target")
                        or step.get("field")
                        or step.get("intent")
                        or ""
                    )
                    self._focus_input_if_possible(focus_hint)
                self.context.device.type_text(text)

            elif step_type == "press_key":
                self.context.device.press(step.get("key", "back"))

            elif step_type == "wait":
                seconds = step.get("seconds", step.get("duration", 1))
                time.sleep(float(seconds))

            elif step_type == "swipe":
                direction = step.get("direction", "up")
                self.context.device.swipe(direction)

            elif step_type == "assert":
                text = step.get("text") or step.get("condition", "")
                if text and not self._assert_contains(text):
                    return False, f"断言失败: {text}"

            # ── 扩展类型 ──
            elif step_type == "conditional":
                # 条件分支：检查条件后在分支中尝试匹配操作
                condition = step.get("condition", "")
                branches = step.get("branches", [])
                screen_text = str(self._perceive(allow_vision=False).to_dict())
                matched = (
                    any(kw in screen_text for kw in condition.split("|"))
                    if condition
                    else False
                )
                if matched and branches:
                    return self._execute_step(branches[0])
                return True, "conditional: 无匹配分支，跳过"

            elif step_type == "traverse_tabs":
                understanding = self._perceive(allow_vision=False)
                tabs = [e for e in understanding.primary_paths if e.role == "tab"]
                for tab in tabs[:10]:
                    self.context.device.click_text(tab.label, timeout=0.5)
                    time.sleep(1)
                return True, f"已遍历 {len(tabs)} 个Tab"

            elif step_type == "feedback_submit":
                # 复合动作：导航到反馈 → 输入 → 提交
                for t in ["反馈", "Feedback", "意见反馈"]:
                    if self.context.device.click_text(t, timeout=1):
                        break
                time.sleep(0.5)
                text = step.get("text", "auto ai testing submit")
                self.context.device.type_text(text)
                time.sleep(0.3)
                for t in ["提交", "Submit", "发送"]:
                    if self.context.device.click_text(t, timeout=1):
                        return True, "反馈提交完成"
                return False, "未找到提交按钮"

            else:
                # fallback: 尝试按意图文本点击
                intent = step.get("intent", target)
                if intent and self.context.device.click_text(intent, timeout=0.5):
                    return True, f"点击: {intent}"
                return False, f"暂不支持步骤类型: {step_type}"

            return True, "ok"
        except Exception as exc:
            return False, str(exc)

    def _current_page_key(self) -> str:
        current = self.context.device.current_app()
        sig = self.context.perceiver.screen_signature()
        return f"{current.get('package', '')}|{current.get('activity', '')}|{sig[:10]}"

    def _click_navigation_tab(self, tab_name: str) -> bool:
        if not tab_name:
            return False
        understanding = self._perceive(allow_vision=False)
        matched = []
        for element in understanding.elements:
            if not getattr(element, "clickable", False):
                continue
            region = str(getattr(element, "region", "") or "").lower()
            if region not in {"left_navigation", "navigation"}:
                continue
            label = str(getattr(element, "label", "") or "")
            if self._text_match(label, tab_name):
                matched.append(element)
        if matched:
            matched.sort(
                key=lambda e: (
                    getattr(e, "priority", 99),
                    getattr(e, "bounds", (0, 0, 0, 0))[1],
                )
            )
            for element in matched:
                bounds = getattr(element, "bounds", (0, 0, 0, 0))
                if bounds != (0, 0, 0, 0):
                    self.context.device.click_bounds(bounds)
                    return True
        return self._click_target(tab_name)

    def _click_target(self, target: str) -> bool:
        if not target:
            return False
        if self.context.device.click_text(target, timeout=0.8):
            return True
        if self.context.device.click_resource_id(target, timeout=0.6):
            return True
        understanding = self._perceive(allow_vision=False)
        candidates = self._find_clickable_candidates(understanding, target)
        if not candidates:
            # 仅在 UI 树未命中时，降级触发一次 Vision/HYBRID 感知。
            logger.info("Replay vision fallback for click target=%s", target)
            understanding = self._perceive(allow_vision=True)
            candidates = self._find_clickable_candidates(understanding, target)
        candidates.sort(
            key=lambda e: (
                getattr(e, "priority", 99),
                getattr(e, "bounds", (0, 0, 0, 0))[1],
            )
        )
        for element in candidates:
            bounds = getattr(element, "bounds", (0, 0, 0, 0))
            if bounds != (0, 0, 0, 0):
                self.context.device.click_bounds(bounds)
                return True
        return False

    def _find_clickable_candidates(self, understanding, target: str) -> list[Any]:
        candidates: list[Any] = []
        for element in understanding.elements:
            if not getattr(element, "clickable", False):
                continue
            label = str(getattr(element, "label", "") or "")
            rid = str(getattr(element, "resource_id", "") or "")
            if self._text_match(label, target) or self._text_match(rid, target):
                candidates.append(element)
        return candidates

    def _focus_input_if_possible(self, hint: str = "") -> None:
        understanding = self._perceive(allow_vision=False)
        if self._focus_input_from_understanding(understanding, hint):
            return
        logger.info("Replay vision fallback for focusing input")
        understanding = self._perceive(allow_vision=True)
        self._focus_input_from_understanding(understanding, hint)

    def _focus_input_from_understanding(self, understanding, hint: str = "") -> bool:
        normalized_hint = self._normalize_text(hint)
        prefer_search = "搜索" in hint or "search" in normalized_hint
        candidates: list[tuple[int, int, Any]] = []
        for element in understanding.elements:
            class_name = str(getattr(element, "class_name", "") or "").lower()
            role = str(getattr(element, "role", "") or "").lower()
            if "edittext" not in class_name and role != "input":
                continue
            bounds = getattr(element, "bounds", (0, 0, 0, 0))
            if bounds == (0, 0, 0, 0):
                continue
            region = str(getattr(element, "region", "") or "").lower()
            fields = [
                str(getattr(element, "label", "") or ""),
                str(getattr(element, "text", "") or ""),
                str(getattr(element, "content_desc", "") or ""),
                str(getattr(element, "resource_id", "") or ""),
            ]
            normalized_fields = self._normalize_text(" ".join(fields))
            score = 0
            if normalized_hint and normalized_hint in normalized_fields:
                score -= 50
            if not prefer_search and (
                "search" in normalized_fields or "搜索" in "".join(fields)
            ):
                score += 40
            if region in {"left_navigation", "navigation", "sidebar"}:
                score += 30
            if region in {"main_content", "content", "right_content", "right_panel"}:
                score -= 20
            top = bounds[1]
            candidates.append((score, top, element))
        if not candidates:
            return False
        candidates.sort(key=lambda item: (item[0], item[1]))
        best = candidates[0][2]
        bounds = getattr(best, "bounds", (0, 0, 0, 0))
        self.context.device.click_bounds(bounds)
        time.sleep(0.2)
        return True

    def _assert_contains(self, text: str) -> bool:
        understanding = self._perceive(allow_vision=False)
        target = self._normalize_text(text)
        if self._match_text_in_understanding(understanding, target):
            return True
        logger.info("Replay vision fallback for assert text=%s", text)
        understanding = self._perceive(allow_vision=True)
        return self._match_text_in_understanding(understanding, target)

    def _match_text_in_understanding(self, understanding, normalized_target: str) -> bool:
        for element in understanding.elements:
            fields = [
                str(getattr(element, "label", "") or ""),
                str(getattr(element, "text", "") or ""),
                str(getattr(element, "content_desc", "") or ""),
                str(getattr(element, "resource_id", "") or ""),
            ]
            for field in fields:
                if normalized_target and normalized_target in self._normalize_text(field):
                    return True
        return normalized_target in self._normalize_text(str(understanding.to_dict()))

    def _perceive(self, allow_vision: bool = False):
        perceiver = self.context.perceiver
        switch_mode = getattr(perceiver, "switch_mode", None)
        previous_mode = getattr(perceiver, "mode", PerceptionMode.UI_TREE)
        if not callable(switch_mode):
            return perceiver.perceive()
        try:
            switch_mode(PerceptionMode.HYBRID if allow_vision else PerceptionMode.UI_TREE)
            return perceiver.perceive()
        finally:
            switch_mode(previous_mode)

    def _text_match(self, candidate: str, target: str) -> bool:
        c = self._normalize_text(candidate)
        t = self._normalize_text(target)
        if not c or not t:
            return False
        return t in c or c in t

    def _normalize_text(self, value: str) -> str:
        return re.sub(r"\s+", "", str(value or "").lower())

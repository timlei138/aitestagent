from __future__ import annotations

import json
import logging
import os
import re
import time
from hashlib import sha1
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import uuid4


@dataclass
class ScanNode:
    page_key: str
    page_name: str
    depth: int
    parent_key: str = ""
    action_to_reach: str = ""
    retry_count: int = 0


@dataclass
class ScanResult:
    app_package: str
    total_pages: int
    visited_keys: list[str]
    path_tree: dict[str, Any]
    duration_seconds: float
    errors: list[dict[str, str]] = field(default_factory=list)
    traverse_summary: dict[str, Any] = field(default_factory=dict)


class SemanticScanner:
    """语义路径扫描器：先理解布局和入口，再按有意义路径探索。

    接入 StateMachine（弹窗处理/恢复）和 ReportBuilder（实时事件推送）。
    """

    def __init__(
        self,
        device,
        perceiver,
        baseline_store,
        anomaly_detector=None,
        safety_guard=None,
        state_machine=None,
        report_logger=None,
        max_depth: int = 5,
        max_pages: int = 50,
        max_clicks: int = 300,
        click_wait: float = 1.2,
        back_wait: float = 0.8,
        launch_activity: str | None = None,
        max_recover_retries: int = 3,
        planner_llm=None,
    ):
        self.device = device
        self.perceiver = perceiver
        self.baseline_store = baseline_store
        self.anomaly_detector = anomaly_detector
        self.safety_guard = safety_guard
        self.state_machine = state_machine
        self.report = report_logger
        self.max_depth = max_depth
        self.max_pages = max_pages
        self.max_clicks = max_clicks
        self.click_wait = click_wait
        self.back_wait = back_wait
        self.launch_activity = launch_activity
        self.max_recover_retries = max_recover_retries
        self.planner_llm = planner_llm
        self.visited: set[str] = set()
        self.errors: list[dict[str, str]] = []
        self.click_count = 0
        self.key_nodes: list[dict[str, Any]] = []
        self.scan_logs: list[dict[str, Any]] = []
        self._explored_actions: set[str] = set()
        self._visited_surfaces: set[str] = set()
        self._live_key_nodes_paths: list[str] = []
        self._live_run_log_paths: list[str] = []
        self._planner_cache: dict[str, dict[str, Any]] = {}
        self._active_root_for_route: dict[str, str] = {}
        self._no_transition_actions: dict[str, int] = {}
        self._understanding_cache: dict[str, Any] = {}
        self._root_paths: dict[str, set[str]] = {}
        self._roots_seen: set[str] = set()
        self._roots_clicked: set[str] = set()
        self._scan_started_at: datetime | None = None
        self.run_id: str = ""
        self.logger = logging.getLogger(__name__)
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            handler.setLevel(logging.INFO)
            handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
                )
            )
            self.logger.addHandler(handler)
        self.logger.propagate = False
        self.logger.setLevel(logging.INFO)
        self._ignore_activity_keywords = [
            "leakcanary.internal.activity.LeakLauncherActivity".lower(),
        ]
        self._ignore_packages = {
            "com.android.systemui",
        }

    def scan(self, app_package: str, start_page_name: str = "首页") -> ScanResult:
        start = time.time()
        self.visited = set()
        self.errors = []
        self.click_count = 0
        self._scan_started_at = datetime.now()
        self.key_nodes = []
        self.scan_logs = []
        self._explored_actions = set()
        self._visited_surfaces = set()
        self._live_key_nodes_paths = []
        self._live_run_log_paths = []
        self._planner_cache = {}
        self._active_root_for_route = {}
        self._no_transition_actions = {}
        self._understanding_cache = {}
        self._root_paths = {}
        self._roots_seen = set()
        self._roots_clicked = set()
        self.run_id = uuid4().hex
        set_run_func = getattr(self.baseline_store, "set_active_run", None)
        if callable(set_run_func):
            self.run_id = set_run_func(app_package, self.run_id) or self.run_id
        self._prepare_live_log_paths(app_package)
        self._log_key_node("scan_start", app_package=app_package, page=start_page_name)

        purge_func = getattr(self.baseline_store, "remove_foreign_pages", None)
        if callable(purge_func):
            removed_foreign = purge_func(app_package, list(self._ignore_packages))
            if removed_foreign:
                self._log_key_node("baseline_purge_foreign", removed_pages=removed_foreign)

        # 清理历史基线中已知的辅助 Activity 页面（如 LeakCanary）
        cleanup_func = getattr(self.baseline_store, "remove_pages_by_activity_keywords", None)
        if callable(cleanup_func):
            removed = cleanup_func(app_package, self._ignore_activity_keywords)
            if removed:
                self._log_key_node("baseline_cleanup", removed_pages=len(removed))
        sanitize_func = getattr(self.baseline_store, "sanitize_page_elements_by_package", None)
        if callable(sanitize_func):
            sanitized = sanitize_func(app_package, list(self._ignore_packages))
            if sanitized:
                self._log_key_node("baseline_sanitize", updated_pages=sanitized)

        self._start_target_app(app_package)
        time.sleep(2)

        if self.state_machine:
            self.state_machine.reset()

        if self.report:
            self.report.log_step_start(0, f"开始扫描 {app_package}")

        root_key = self._page_key()
        queue = deque([ScanNode(root_key, start_page_name, 0)])
        tree: dict[str, Any] = {}

        while (
            queue
            and len(self.visited) < self.max_pages
            and self.click_count < self.max_clicks
        ):
            node = queue.popleft()
            if node.page_key in self.visited or node.depth > self.max_depth:
                continue

            # ── StateMachine 弹窗检测 ──
            if self.state_machine:
                popup_buttons = self.state_machine.detect_popup()
                if popup_buttons:
                    self._emit_anomaly("medium", f"检测到弹窗: {popup_buttons}")
                    self.state_machine.recover_popup()

            try:
                current = self.device.current_app()
                current_package = current.get("package", "")
                current_activity = current.get("activity", "")
                if not self._is_target_app_foreground(current_package, app_package):
                    self._log_key_node(
                        "skip_foreign_package",
                        package=current_package,
                        reason="not_target_app",
                    )
                    self._recover_and_retry_node(queue, node, app_package, "foreign_package")
                    continue
                if self._is_auxiliary_activity(current_activity):
                    self._log_key_node(
                        "skip_aux_activity",
                        activity=current_activity,
                        reason="ignore_aux_launcher",
                    )
                    self._recover_and_retry_node(queue, node, app_package, "aux_activity")
                    continue

                route_key = self._current_route_key()
                page_key = self._page_key()
                understanding = self._get_understanding(page_key)
                self._log_key_node(
                    "layout_detected",
                    layout=understanding.layout,
                    summary=understanding.summary,
                    primary_count=len(understanding.primary_paths),
                    route=route_key,
                )
                plan = self._get_or_build_plan(node, understanding, route_key)
                surface_key = self._build_surface_key(route_key, understanding, plan)
                self._log_key_node(
                    "planner_decision",
                    route=route_key,
                    surface=surface_key,
                    layout=plan.get("layout", understanding.layout),
                    root_zone=plan.get("root_zone", ""),
                    content_zones=plan.get("content_zones", []),
                    strategy=plan.get("strategy", "heuristic"),
                    root_first=bool(plan.get("root_zone")),
                )
                vision_log = getattr(self.perceiver, "last_vision_log", None)
                if vision_log:
                    self._log_key_node(
                        "vision_llm_log",
                        context=str(vision_log.get("context", ""))[:400],
                        response=str(vision_log.get("response", vision_log.get("error", "")))[:800],
                    )
                if surface_key in self._visited_surfaces:
                    self._log_key_node(
                        "skip_visited_surface",
                        route=route_key,
                        surface=surface_key,
                        page_name=node.page_name,
                    )
                    continue
                node.page_key = page_key
                self._capture(app_package, page_key, node, understanding)
                self._log_key_node(
                    "page_captured",
                    page_key=page_key,
                    page_name=node.page_name,
                    depth=node.depth,
                )

                # ── 事件：新页面快照 ──
                if self.report:
                    self.report.log_step_start(
                        len(self.visited) + 1,
                        f"深度{node.depth} | {node.page_name}",
                    )

                self.visited.add(page_key)
                self._visited_surfaces.add(surface_key)

                # ── 异常检测 ──
                if self.anomaly_detector:
                    detection = self.anomaly_detector.detect(app_package, page_key)
                    if not detection.is_healthy:
                        for a in detection.anomalies:
                            self._emit_anomaly(
                                (
                                    a.severity.value
                                    if hasattr(a.severity, "value")
                                    else "medium"
                                ),
                                a.description,
                            )
                    if detection.has_critical:
                        self._log_key_node(
                            "critical_anomaly",
                            page_key=page_key,
                            anomalies=detection.to_dict(),
                        )
                        tree[page_key] = {
                            "name": node.page_name,
                            "children": [],
                            "anomalies": detection.to_dict(),
                        }
                        if self.state_machine:
                            self.state_machine.recover_app(app_package)
                        continue

                children = self._discover(
                    app_package, node, understanding, plan, surface_key
                )
                tree[page_key] = {
                    "name": node.page_name,
                    "parent": node.parent_key,
                    "action_to_reach": node.action_to_reach,
                    "layout": understanding.layout,
                    "summary": understanding.summary,
                    "children": [child.page_key for child in children],
                }

                if self.report:
                    self.report.log_step(
                        f"已收录 {node.page_name}",
                        action="capture",
                        target=node.page_name,
                        status="success",
                        page_key=page_key,
                        children_count=len(children),
                    )

                for child in children:
                    if child.page_key not in self.visited:
                        queue.append(child)

            except Exception as exc:
                self.errors.append({"page": node.page_key, "error": str(exc)})
                self._log_key_node("page_exception", page=node.page_key, error=str(exc))
                self._emit_anomaly("medium", f"页面探索异常: {exc}")
                self._recover_to_app(app_package)

        elapsed = time.time() - start
        traverse_summary = self._build_traverse_summary()
        if self.report:
            self.report.log_step(
                f"扫描完成: {len(self.visited)} 页 / {self.click_count} 次点击",
                action="complete",
                status="success",
            )
        self._log_key_node(
            "scan_complete",
            total_pages=len(self.visited),
            click_count=self.click_count,
            duration_seconds=elapsed,
            root_coverage=traverse_summary.get("root_coverage", {}),
        )
        self._persist_scan_records(app_package, elapsed, traverse_summary)

        return ScanResult(
            app_package=app_package,
            total_pages=len(self.visited),
            visited_keys=sorted(self.visited),
            path_tree=tree,
            duration_seconds=elapsed,
            errors=self.errors,
            traverse_summary=traverse_summary,
        )

    def _discover(
        self,
        app_package: str,
        node: ScanNode,
        understanding,
        plan: dict[str, Any],
        surface_key: str,
    ) -> list[ScanNode]:
        children: list[ScanNode] = []
        candidates = self._select_semantic_candidates(node, understanding, plan)
        self._log_key_node(
            "candidates_selected",
            total=len(candidates),
            labels=[
                str(getattr(e, "label", "") or "")[:40] for e in (candidates or [])[:15]
            ],
            regions=[
                str(getattr(e, "region", "") or "") for e in (candidates or [])[:15]
            ],
        )
        if not candidates:
            fallback = self._fallback_candidates(understanding)
            self._log_key_node(
                "candidate_empty",
                layout=understanding.layout,
                primary_paths=len(understanding.primary_paths or []),
                fallback_count=len(fallback),
            )
            candidates = fallback
        current_key = self._page_key()
        for element in candidates:
            if self.click_count >= self.max_clicks:
                break
            if not self._is_allowed_element_package(
                getattr(element, "package", ""), app_package
            ):
                continue
            if self._is_auxiliary_element(element):
                continue
            label = element.label
            if not label or not element.safe_to_click:
                continue
            if self._should_skip_noise(element, label):
                self._log_key_node("skip_noise_label", label=label)
                continue
            if self._is_input_element(element):
                self._log_key_node("skip_input_action", label=label)
                continue
            action_key = f"{surface_key}|{self._element_identity(element)}"
            if action_key in self._explored_actions:
                self._log_key_node(
                    "skip_duplicate_action",
                    action_key=action_key,
                    label=label,
                )
                continue
            semantic_key = f"{surface_key}|{self._semantic_action_key(element)}"
            if self._no_transition_actions.get(semantic_key, 0) >= 1:
                self._log_key_node(
                    "skip_no_transition_action",
                    action_key=semantic_key,
                    label=label,
                )
                continue
            self._explored_actions.add(action_key)
            if self.safety_guard:
                decision = self.safety_guard.check_click(label)
                if not decision.allowed:
                    self._log_key_node("skip_unsafe_action", label=label)
                    continue

            # ── StateMachine 每次点击前检测弹窗 ──
            if self.state_machine:
                popup_buttons = self.state_machine.detect_popup()
                if popup_buttons:
                    self.state_machine.recover_popup()

            try:
                before_app = self.device.current_app()
                before = self.perceiver.screen_signature()
                before_semantic = understanding
                click_kind = self._candidate_kind_by_plan(element, plan)
                if click_kind == "root":
                    root_name = self._normalize_root_name(label)
                    self._roots_clicked.add(root_name)
                    self._log_key_node(
                        "root_selected",
                        label=root_name,
                        region=getattr(element, "region", ""),
                        role=getattr(element, "role", ""),
                    )
                clicked = self._click_element(element)
                if not clicked:
                    self._log_key_node("click_not_found", label=label)
                    continue
                self.click_count += 1
                self._log_key_node(
                    "click",
                    label=label,
                    role=element.role,
                    region=element.region,
                    depth=node.depth,
                )
                time.sleep(self.click_wait)
                after_key = self._page_key()
                after_sig = self.perceiver.screen_signature()
                after_app = self.device.current_app()
                after_semantic = self._get_understanding(after_key)
                before_package = before_app.get("package", "")
                before_activity = before_app.get("activity", "")
                after_package = after_app.get("package", "")
                after_activity = after_app.get("activity", "")
                semantic_changed = self._semantic_region_changed(
                    before_semantic,
                    after_semantic,
                    plan.get("content_zones", []),
                )
                changed_page = (
                    before_package != after_package
                    or before_activity != after_activity
                    or semantic_changed
                )

                if changed_page:
                    next_package = after_package
                    next_activity = after_activity
                    if not self._is_target_app_foreground(next_package, app_package):
                        self._log_key_node(
                            "skip_foreign_package_after_click",
                            package=next_package,
                            label=label,
                        )
                        self._go_back_or_restart(app_package, current_key)
                        continue
                    if self._is_auxiliary_activity(next_activity):
                        self._log_key_node(
                            "skip_aux_activity_after_click",
                            activity=next_activity,
                            label=label,
                        )
                        self._go_back_or_restart(app_package, current_key)
                        continue
                    child = ScanNode(
                        page_key=after_key,
                        page_name=label,
                        depth=node.depth + 1,
                        parent_key=current_key,
                        action_to_reach=f"点击 {element.region}/{element.role}: {label}",
                    )
                    children.append(child)
                    if click_kind == "root":
                        route = self._current_route_key()
                        root_name = self._normalize_root_name(label)
                        self._active_root_for_route[route] = root_name
                        self._log_key_node(
                            "subtree_enter",
                            route=route,
                            root_label=root_name,
                        )
                    self._log_key_node(
                        "edge_discovered",
                        from_page=current_key,
                        to_page=after_key,
                        action=child.action_to_reach,
                        semantic_changed=semantic_changed,
                    )
                    active_root = self._active_root_for_route.get(
                        self._current_route_key(), "unassigned"
                    )
                    self._root_paths.setdefault(active_root, set()).add(after_key)
                    self._go_back_or_restart(app_package, current_key)
                    if click_kind == "root":
                        self._log_key_node(
                            "subtree_exit",
                            route=self._current_route_key(),
                            root_label=self._normalize_root_name(label),
                        )
                else:
                    self._no_transition_actions[semantic_key] = (
                        self._no_transition_actions.get(semantic_key, 0) + 1
                    )
                    self._log_key_node(
                        "click_no_transition",
                        label=label,
                        activity=after_activity,
                        signature_changed=(after_sig != before),
                        semantic_changed=semantic_changed,
                    )
            except Exception as exc:
                self.errors.append(
                    {"page": current_key, "action": f"点击 {label}", "error": str(exc)}
                )
                self._log_key_node(
                    "click_exception", page=current_key, label=label, error=str(exc)
                )
                self._go_back_or_restart(app_package, current_key)

        return children

    def _capture(
        self, app_package: str, page_key: str, node: ScanNode, understanding=None
    ) -> None:
        screenshot = self.device.screenshot()
        xml = self.device.dump_hierarchy()
        if understanding is None:
            understanding = self._get_understanding(page_key)
        filtered_elements = self._filter_elements_for_app(understanding.elements, app_package)
        filtered_primary = self._filter_elements_for_app(understanding.primary_paths, app_package)
        self.baseline_store.save_page(
            app_package=app_package,
            page_key=page_key,
            page_name=node.page_name,
            screenshot=screenshot,
            ui_tree_xml=xml,
            elements=[e.to_dict() for e in filtered_elements],
            activity_name=self.device.current_app().get("activity", ""),
            parent_page=node.parent_key,
            action_to_reach=node.action_to_reach,
            traversal_depth=node.depth,
            run_id=self.run_id,
        )

        # ── 推送快照事件 ──
        if self.report:
            try:
                import base64
                from io import BytesIO

                buf = BytesIO()
                screenshot.save(buf, format="PNG")
                image_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
                self.report.log_snapshot(
                    image_base64=image_b64,
                    elements=[e.to_dict() for e in filtered_primary[:30]],
                )
            except Exception:
                pass

    def _click_element(self, element) -> bool:
        if element.text and self.device.click_text(element.text, timeout=0.5):
            return True
        if element.content_desc and self.device.click_text(
            element.content_desc, timeout=0.5
        ):
            return True
        if element.resource_id and self.device.click_resource_id(
            element.resource_id, timeout=0.5
        ):
            return True
        if element.bounds != (0, 0, 0, 0):
            self.device.click_bounds(element.bounds)
            return True
        return False

    def _page_key(self) -> str:
        current = self.device.current_app()
        return f"{current.get('package', '')}|{current.get('activity', '')}|{self.perceiver.screen_signature()[:10]}"

    def _go_back_or_restart(self, app_package: str, expected_key: str) -> None:
        current = self.device.current_app()
        if not self._is_target_app_foreground(current.get("package", ""), app_package):
            self._recover_to_app(app_package)
            return
        current_key = self._page_key()
        if self._is_same_page(expected_key, current_key):
            self._log_key_node(
                "skip_back_same_page",
                expected=expected_key,
                current_route=self._current_route_key(),
                current_page=current_key,
            )
            return
        self._log_key_node(
            "press_back",
            expected=expected_key,
            current_page=current_key,
        )
        self.device.press("back")
        time.sleep(self.back_wait)
        current = self.device.current_app()
        if not self._is_target_app_foreground(current.get("package", ""), app_package):
            self._recover_to_app(app_package)

    def _start_target_app(self, app_package: str) -> None:
        before = self.device.current_app()
        self._log_key_node(
            "app_start_attempt",
            package=app_package,
            launch_activity=self.launch_activity or "",
            before_package=before.get("package", ""),
            before_activity=before.get("activity", ""),
        )
        try:
            self.device.app_start(app_package, activity=self.launch_activity)
        except TypeError:
            # 兼容测试桩/旧签名
            self.device.app_start(app_package)
        except Exception:
            self.logger.exception(
                "app_start with launch_activity failed, fallback to package start"
            )
            self.device.app_start(app_package)
        time.sleep(0.8)
        after = self.device.current_app()
        self._log_key_node(
            "app_start_done",
            package=app_package,
            after_package=after.get("package", ""),
            after_activity=after.get("activity", ""),
            started_target=self._is_target_app_foreground(
                after.get("package", ""), app_package
            ),
        )

    def _recover_to_app(self, app_package: str) -> None:
        try:
            self._start_target_app(app_package)
            time.sleep(1.5)
        except Exception:
            self.logger.exception("recover_to_app failed")

    def _recover_and_retry_node(
        self,
        queue: deque[ScanNode],
        node: ScanNode,
        app_package: str,
        reason: str,
    ) -> None:
        self._recover_to_app(app_package)
        node.retry_count += 1
        if node.retry_count <= self.max_recover_retries:
            queue.appendleft(node)
            self._log_key_node(
                "retry_node",
                reason=reason,
                page=node.page_name,
                retry=node.retry_count,
            )
            return
        self.errors.append(
            {
                "page": node.page_key,
                "error": f"恢复重试超限: {reason}",
            }
        )
        self._log_key_node(
            "drop_node_after_retries",
            reason=reason,
            page=node.page_name,
            retry=node.retry_count,
        )

    def _emit_anomaly(self, severity: str, message: str) -> None:
        if self.report:
            self.report.log_anomaly({"severity": severity, "description": message})

    def _is_auxiliary_activity(self, activity: str) -> bool:
        lowered = (activity or "").lower()
        return any(k in lowered for k in self._ignore_activity_keywords)

    def _is_target_app_foreground(self, current_package: str, app_package: str) -> bool:
        pkg = (current_package or "").strip().lower()
        target = (app_package or "").strip().lower()
        if not pkg or not target:
            return True
        if pkg == target:
            return True
        return pkg.startswith(f"{target}.")

    def _is_allowed_element_package(self, element_package: str, app_package: str) -> bool:
        pkg = (element_package or "").strip().lower()
        target = (app_package or "").strip().lower()
        if not pkg:
            return True
        if pkg in self._ignore_packages:
            return False
        if not target:
            return True
        return pkg == target or pkg.startswith(f"{target}.")

    def _filter_elements_for_app(self, elements: list[Any], app_package: str) -> list[Any]:
        return [
            element
            for element in elements
            if self._is_allowed_element_package(
                getattr(element, "package", ""), app_package
            )
            and not self._is_auxiliary_element(element)
        ]

    def _is_auxiliary_element(self, element: Any) -> bool:
        fields = [
            getattr(element, "text", ""),
            getattr(element, "content_desc", ""),
            getattr(element, "resource_id", ""),
            getattr(element, "class_name", ""),
        ]
        lowered = " ".join(str(v or "").lower() for v in fields)
        return "leakcanary" in lowered or "leaklauncher" in lowered

    def _is_input_element(self, element: Any) -> bool:
        class_name = str(getattr(element, "class_name", "") or "").lower()
        rid = str(getattr(element, "resource_id", "") or "").lower()
        text = str(getattr(element, "text", "") or "").lower()
        desc = str(getattr(element, "content_desc", "") or "").lower()
        role = str(getattr(element, "role", "") or "").lower()
        if any(k in class_name for k in ("edittext", "textinput", "textfield")):
            return True
        if role == "input":
            return True
        return any(k in f"{rid} {text} {desc}" for k in ("input", "edit", "search_src_text"))

    def _select_semantic_candidates(
        self, node: ScanNode, understanding, plan: dict[str, Any]
    ) -> list[Any]:
        candidates = understanding.primary_paths or []
        root_zone = str(plan.get("root_zone", "") or "")
        content_zones = [str(z) for z in (plan.get("content_zones", []) or [])]
        from_root = self._came_from_root_entry(node, root_zone)
        if not root_zone:
            ordered = self._order_candidates(
                self._filter_by_zones(candidates, content_zones) or candidates,
                "",
            )
            self._log_key_node(
                "candidate_policy",
                policy="no_root_zone_layered",
                count=len(ordered),
                content_zones=content_zones,
            )
            return ordered

        roots = [e for e in candidates if self._is_in_zone(e, root_zone)]
        content = self._filter_by_zones(candidates, content_zones)
        roots = self._expand_root_candidates(roots, understanding, root_zone)
        self._log_key_node(
            "root_pool_built",
            root_zone=root_zone,
            root_count=len(roots),
            sample=[str(getattr(e, "label", "") or "")[:40] for e in roots[:12]],
        )
        active_root_key, active_root_label = self._detect_active_root(roots)
        if active_root_key and content:
            ordered = self._order_candidates(content, "")
            remain_roots = [
                r
                for r in roots
                if self._semantic_action_key(r) != active_root_key
            ]
            ordered_roots = self._order_candidates(remain_roots, root_zone)
            merged = ordered + [
                r
                for r in ordered_roots
                if self._element_identity(r) not in {self._element_identity(c) for c in ordered}
            ]
            self._log_key_node(
                "candidate_policy",
                policy="active_root_content_then_roots",
                count=len(merged),
                active_root=active_root_label,
                content_count=len(ordered),
                root_count=len(ordered_roots),
            )
            return merged
        if from_root and content:
            ordered = self._order_candidates(content, "")
            self._log_key_node(
                "candidate_policy", policy="root_then_content", count=len(ordered)
            )
            return ordered
        if roots:
            ordered_roots = self._order_candidates(roots, root_zone)
            ordered_content = self._order_candidates(content, "")
            root_ids = {self._element_identity(r) for r in ordered_roots}
            merged = ordered_roots + [
                c for c in ordered_content if self._element_identity(c) not in root_ids
            ]
            self._log_key_node(
                "candidate_policy",
                policy="root_first_then_content",
                count=len(merged),
                root_zone=root_zone,
            )
            return merged
        if content:
            ordered = self._order_candidates(content, "")
            self._log_key_node(
                "candidate_policy", policy="content_fallback", count=len(ordered)
            )
            return ordered
        self._log_key_node(
            "candidate_policy", policy="candidate_empty", count=0, root_zone=root_zone
        )
        return candidates

    def _fallback_candidates(self, understanding) -> list[Any]:
        pool = understanding.elements or []
        picked: list[Any] = []
        for e in pool:
            if not getattr(e, "clickable", False):
                continue
            label = getattr(e, "label", "")
            if not label:
                continue
            if self._should_skip_noise(e, label):
                continue
            if self._is_input_element(e):
                continue
            if self._is_auxiliary_element(e):
                continue
            if not getattr(e, "safe_to_click", True):
                continue
            picked.append(e)
        picked.sort(
            key=lambda item: (
                getattr(item, "priority", 99),
                getattr(item, "region", ""),
                getattr(item, "label", ""),
            )
        )
        return picked[:50]

    def _get_or_build_plan(
        self, node: ScanNode, understanding, route_key: str
    ) -> dict[str, Any]:
        planner_key = self._planner_cache_key(route_key, understanding)
        cached = self._planner_cache.get(planner_key)
        if cached:
            return cached
        heuristic = self._build_heuristic_plan(understanding)
        planned = self._build_llm_plan(understanding, heuristic)
        plan = planned or heuristic
        self._planner_cache[planner_key] = plan
        return plan

    def _planner_cache_key(self, route_key: str, understanding) -> str:
        layout = str(getattr(understanding, "layout", "") or "").lower()
        semantic = self._semantic_signature(understanding, [])
        digest = sha1(semantic.encode("utf-8")).hexdigest()[:12] if semantic else "empty"
        return f"{route_key}|{layout}|{digest}"

    def _build_surface_key(self, route_key: str, understanding, plan: dict[str, Any]) -> str:
        zones = [str(z) for z in (plan.get("content_zones", []) or []) if z]
        semantic = self._semantic_signature(understanding, zones)
        if not semantic:
            semantic = self._semantic_signature(understanding, [])
        digest = sha1(semantic.encode("utf-8")).hexdigest()[:12] if semantic else "empty"
        return f"{route_key}|{digest}"

    def _get_understanding(self, page_key: str):
        cached = self._understanding_cache.get(page_key)
        if cached is not None:
            self._log_key_node("understanding_cache_hit", page_key=page_key)
            return cached
        understanding = self.perceiver.perceive()
        self._understanding_cache[page_key] = understanding
        return understanding

    def _normalize_root_name(self, label: str) -> str:
        raw = (label or "").strip()
        if not raw:
            return "unknown_root"
        lowered = raw.lower()
        if lowered in {"collapse", "expand", "返回", "back"}:
            return f"toggle:{lowered}"
        return raw

    def _build_heuristic_plan(self, understanding) -> dict[str, Any]:
        layout = (understanding.layout or "single_flow").lower()
        normalized = layout
        root_zone = ""
        content_zones: list[str] = []
        if layout == "two_pane":
            root_zone = "left_navigation"
            content_zones = ["right_content", "main_content", "content"]
        elif layout in {"bottom_tab", "drawer"}:
            root_zone = "navigation"
            content_zones = ["main_content", "content", "right_content"]
        elif layout in {"single_pane", "single_flow"}:
            normalized = "single_flow"
            content_zones = ["main_content", "content", "right_content"]
        else:
            normalized = "mixed"
            content_zones = ["main_content", "content", "right_content", "navigation"]
        return {
            "layout": normalized,
            "root_zone": root_zone,
            "content_zones": content_zones,
            "strategy": "heuristic",
        }

    def _build_llm_plan(self, understanding, heuristic: dict[str, Any]) -> dict[str, Any] | None:
        if not self.planner_llm:
            return None
        try:
            sample = [
                {
                    "label": getattr(e, "label", ""),
                    "role": getattr(e, "role", ""),
                    "region": getattr(e, "region", ""),
                    "clickable": bool(getattr(e, "clickable", False)),
                }
                for e in (understanding.primary_paths or [])[:30]
            ]
            prompt = (
                "你是移动端UI遍历规划器。根据页面布局和元素，输出JSON: "
                "{\"layout\":\"two_pane|bottom_tab|drawer|single_flow|mixed\","
                "\"root_zone\":\"left_navigation|navigation|\","
                "\"content_zones\":[...],"
                "\"strategy\":\"llm_plan\"}。只输出JSON。"
            )
            content = self.planner_llm.invoke(
                [
                    {"role": "system", "content": prompt},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "layout": understanding.layout,
                                "summary": understanding.summary,
                                "heuristic": heuristic,
                                "elements": sample,
                            },
                            ensure_ascii=False,
                        ),
                    },
                ]
            )
            cleaned = str(content).strip()
            if "```" in cleaned:
                cleaned = cleaned.split("```")[1].replace("json", "", 1).strip()
            data = json.loads(cleaned)
            root_zone = str(data.get("root_zone", "") or "")
            content_zones = data.get("content_zones", heuristic.get("content_zones", []))
            if not isinstance(content_zones, list):
                content_zones = heuristic.get("content_zones", [])
            plan = {
                "layout": str(data.get("layout", heuristic.get("layout", "mixed"))),
                "root_zone": root_zone,
                "content_zones": [str(z) for z in content_zones if z],
                "strategy": "llm_plan",
            }
            return plan
        except Exception as exc:
            self._log_key_node("planner_llm_failed", error=str(exc)[:400])
            return None

    def _came_from_root_entry(self, node: ScanNode, root_zone: str) -> bool:
        action = (node.action_to_reach or "").lower()
        if not action or not root_zone:
            return False
        zone = root_zone.lower()
        if f"{zone}/" in action:
            return True
        return any(k in action for k in ("/navigation_item:", "/tab:"))

    def _filter_by_zones(self, elements: list[Any], zones: list[str]) -> list[Any]:
        if not zones:
            return []
        zone_set = {z.lower() for z in zones}
        return [
            e for e in elements if str(getattr(e, "region", "") or "").lower() in zone_set
        ]

    def _expand_root_candidates(
        self, roots: list[Any], understanding, root_zone: str
    ) -> list[Any]:
        if not root_zone:
            return roots
        root_zone_lower = root_zone.lower()
        pool = list(roots)
        for e in (understanding.elements or []):
            region = str(getattr(e, "region", "") or "").lower()
            if region != root_zone_lower:
                continue
            role = str(getattr(e, "role", "") or "").lower()
            label = str(getattr(e, "label", "") or "").strip()
            if not label:
                continue
            if self._is_input_element(e):
                continue
            if role not in {"navigation_item", "tab"} and not getattr(e, "clickable", False):
                continue
            if getattr(e, "bounds", (0, 0, 0, 0)) == (0, 0, 0, 0):
                continue
            pool.append(e)
        deduped: list[Any] = []
        seen: set[str] = set()
        for e in self._order_candidates(pool, root_zone):
            key = self._semantic_action_key(e)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(e)
            self._roots_seen.add(self._normalize_root_name(str(getattr(e, "label", "") or "")))
        return deduped

    def _is_in_zone(self, element: Any, zone: str) -> bool:
        return str(getattr(element, "region", "") or "").lower() == (zone or "").lower()

    def _candidate_kind_by_plan(self, element: Any, plan: dict[str, Any]) -> str:
        root_zone = str(plan.get("root_zone", "") or "")
        if root_zone and self._is_in_zone(element, root_zone):
            return "root"
        return "content"

    def _detect_active_root(self, roots: list[Any]) -> tuple[str, str]:
        for e in roots:
            if bool(getattr(e, "selected", False)) or bool(getattr(e, "checked", False)):
                return (
                    self._semantic_action_key(e),
                    str(getattr(e, "label", "") or ""),
                )
        return ("", "")

    def _order_candidates(self, elements: list[Any], root_zone: str) -> list[Any]:
        return sorted(
            elements,
            key=lambda item: self._candidate_rank(item, root_zone),
        )

    def _candidate_rank(self, element: Any, root_zone: str) -> tuple[int, int, str]:
        label = str(getattr(element, "label", "") or "").strip()
        lowered = label.lower()
        region = str(getattr(element, "region", "") or "").lower()
        role = str(getattr(element, "role", "") or "").lower()
        priority = int(getattr(element, "priority", 99) or 99)
        penalty = 0
        if not label:
            penalty += 80
        if ":id/" in lowered:
            penalty += 40
        if self._is_noise_label(lowered):
            penalty += 80
        if root_zone and region == root_zone.lower():
            if lowered in {"collapse", "expand", "menu", "back", "返回"}:
                penalty += 30
            if role not in {"navigation_item", "tab"}:
                penalty += 15
        return (penalty, priority, lowered)

    def _is_noise_label(self, label: str) -> bool:
        lowered = (label or "").strip().lower()
        if not lowered:
            return True
        noise_keys = {
            "grouplayout",
            "linearlayout",
            "framelayout",
            "relativelayout",
            "constraintlayout",
            "recyclerview",
            "viewgroup",
        }
        if lowered in noise_keys:
            return True
        for key in noise_keys:
            if lowered.endswith(f":id/{key}") or lowered.endswith(f"/{key}"):
                return True
        return False

    def _should_skip_noise(self, element: Any, label: str) -> bool:
        if not self._is_noise_label(label):
            return False
        role = str(getattr(element, "role", "") or "").lower()
        region = str(getattr(element, "region", "") or "").lower()
        if role in {"navigation_item", "tab"} and region in {"left_navigation", "navigation"}:
            # 左侧导航很多真实可点项只暴露为 groupLayout 容器，不能一刀切跳过
            return False
        if self._is_list_item_candidate(element):
            return False
        return True

    def _semantic_region_changed(
        self, before_understanding, after_understanding, zones: list[str]
    ) -> bool:
        before_sig = self._semantic_signature(before_understanding, zones)
        after_sig = self._semantic_signature(after_understanding, zones)
        return before_sig != after_sig

    def _semantic_signature(self, understanding, zones: list[str]) -> str:
        zone_set = {str(z).lower() for z in (zones or []) if z}
        lines: list[str] = []
        for e in (understanding.primary_paths or [])[:120]:
            region = str(getattr(e, "region", "") or "").lower()
            if zone_set and region not in zone_set:
                continue
            lines.append(
                "|".join(
                    [
                        str(getattr(e, "role", "") or ""),
                        region,
                        str(getattr(e, "label", "") or ""),
                        str(getattr(e, "bounds", (0, 0, 0, 0))),
                    ]
                )
            )
        raw = "\n".join(lines)
        return re.sub(r"\s+", " ", raw).strip()

    def _is_list_item_candidate(self, element: Any) -> bool:
        role = str(getattr(element, "role", "") or "").lower()
        region = str(getattr(element, "region", "") or "").lower()
        class_name = str(getattr(element, "class_name", "") or "").lower()
        rid = str(getattr(element, "resource_id", "") or "").lower()
        label = str(getattr(element, "label", "") or "").lower()
        if role in {"list_entry", "settings_entry"}:
            return True
        if region in {"right_content", "main_content", "content"} and (
            "item" in rid or "item" in label
        ):
            return True
        return any(k in f"{class_name} {rid}" for k in ("recyclerview", "listview", "gridview"))

    def _element_identity(self, element: Any) -> str:
        rid = getattr(element, "resource_id", "") or ""
        text = getattr(element, "text", "") or ""
        desc = getattr(element, "content_desc", "") or ""
        role = getattr(element, "role", "") or ""
        bounds = getattr(element, "bounds", (0, 0, 0, 0))
        return f"{rid}|{text}|{desc}|{role}|{bounds}"

    def _semantic_action_key(self, element: Any) -> str:
        label = str(getattr(element, "label", "") or "").strip().lower()
        rid = str(getattr(element, "resource_id", "") or "").strip().lower()
        role = str(getattr(element, "role", "") or "").strip().lower()
        region = str(getattr(element, "region", "") or "").strip().lower()
        text = label or rid
        return f"{region}|{role}|{text}"

    def _current_route_key(self) -> str:
        app = self.device.current_app()
        return f"{app.get('package', '')}|{app.get('activity', '')}"

    def _is_same_page(self, expected_key: str, current_key: str) -> bool:
        return (expected_key or "") == (current_key or "")

    def _log_key_node(self, event: str, **payload: Any) -> None:
        entry = {
            "time": datetime.now().isoformat(),
            "event": event,
            "run_id": self.run_id,
            **payload,
        }
        self.key_nodes.append(entry)
        self.scan_logs.append(
            {
                "time": entry["time"],
                "level": "INFO",
                "event": event,
                "message": json.dumps(payload, ensure_ascii=False),
            }
        )
        self.logger.info("[scan:%s] %s %s", self.run_id or "-", event, payload)
        if self.report and hasattr(self.report, "log_stream"):
            self.report.log_stream("scan_log", entry)
        self._append_live_log(entry)

    def _prepare_live_log_paths(self, app_package: str) -> None:
        get_run_dir = getattr(self.baseline_store, "get_run_dir", None)
        if callable(get_run_dir):
            app_dir = get_run_dir(app_package, self.run_id)
        else:
            base_dir = getattr(self.baseline_store, "storage_dir", "storage/baselines")
            app_dir = os.path.join(base_dir, app_package or "unknown", self.run_id or "")
            os.makedirs(app_dir, exist_ok=True)
        app_root = os.path.dirname(app_dir)
        self._live_key_nodes_paths = [
            os.path.join(app_dir, "traverse_keynodes.jsonl"),
            os.path.join(app_root, "traverse_keynodes.jsonl"),
        ]
        self._live_run_log_paths = [
            os.path.join(app_dir, "traverse.log.jsonl"),
            os.path.join(app_root, "traverse.log.jsonl"),
        ]
        for path in self._live_key_nodes_paths + self._live_run_log_paths:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write("")

    def _append_live_log(self, entry: dict[str, Any]) -> None:
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        for path in self._live_key_nodes_paths:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
        mapped = {
            "time": entry.get("time"),
            "level": "INFO",
            "event": entry.get("event"),
            "message": json.dumps(
                {k: v for k, v in entry.items() if k not in {"time", "event", "run_id"}},
                ensure_ascii=False,
            ),
            "run_id": entry.get("run_id", ""),
        }
        run_line = json.dumps(mapped, ensure_ascii=False) + "\n"
        for path in self._live_run_log_paths:
            with open(path, "a", encoding="utf-8") as f:
                f.write(run_line)
                f.flush()

    def _persist_scan_records(
        self, app_package: str, duration_seconds: float, traverse_summary: dict[str, Any]
    ) -> None:
        get_run_dir = getattr(self.baseline_store, "get_run_dir", None)
        if callable(get_run_dir):
            app_dir = get_run_dir(app_package, self.run_id)
        else:
            base_dir = getattr(self.baseline_store, "storage_dir", "storage/baselines")
            app_dir = os.path.join(base_dir, app_package or "unknown", self.run_id or "")
        os.makedirs(app_dir, exist_ok=True)

        # 1) 关键节点日志（每次遍历单独文件）
        key_nodes_path = os.path.join(app_dir, "traverse_keynodes.jsonl")
        with open(key_nodes_path, "w", encoding="utf-8") as f:
            for node in self.key_nodes:
                f.write(json.dumps(node, ensure_ascii=False) + "\n")

        # 2) 本次遍历日志记录
        run_log_path = os.path.join(app_dir, "traverse.log.jsonl")
        with open(run_log_path, "w", encoding="utf-8") as f:
            for item in self.scan_logs:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")

        # 3) 遍历运行记录（按 app 追加）
        history_path = os.path.join(os.path.dirname(app_dir), "traverse_history.jsonl")
        record = {
            "time": datetime.now().isoformat(),
            "run_id": self.run_id,
            "app_package": app_package,
            "total_pages": len(self.visited),
            "click_count": self.click_count,
            "duration_seconds": round(duration_seconds, 3),
            "error_count": len(self.errors),
            "key_nodes_file": key_nodes_path,
            "log_file": run_log_path,
            "traverse_summary": traverse_summary,
            "logs": self.scan_logs[-200:],
        }
        with open(history_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        # 4) 在 app 根目录保留 latest 文件，便于直接查看
        app_root = os.path.dirname(app_dir)
        latest_key_nodes = os.path.join(app_root, "traverse_keynodes.jsonl")
        with open(latest_key_nodes, "w", encoding="utf-8") as f:
            for node in self.key_nodes:
                f.write(json.dumps(node, ensure_ascii=False) + "\n")
        latest_run_log = os.path.join(app_root, "traverse.log.jsonl")
        with open(latest_run_log, "w", encoding="utf-8") as f:
            for item in self.scan_logs:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")

    def _build_traverse_summary(self) -> dict[str, Any]:
        per_root = []
        for root, paths in sorted(self._root_paths.items(), key=lambda item: item[0]):
            per_root.append(
                {
                    "root": root,
                    "path_count": len(paths),
                }
            )
        seen_count = len(self._roots_seen)
        clicked_count = len(self._roots_clicked)
        return {
            "main_nav_count": seen_count,
            "clicked_nav_count": clicked_count,
            "per_root_paths": per_root,
            "root_coverage": {
                "seen": seen_count,
                "clicked": clicked_count,
                "ratio": round((clicked_count / seen_count), 3) if seen_count else 0.0,
            },
        }

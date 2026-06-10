from __future__ import annotations

import json
from datetime import datetime
import os
import re
import logging
from pathlib import Path
from typing import Any, Callable

import yaml

from config import TestConfig
from core.agent import create_test_agent
from core.anomaly_detector import AnomalyDetector
from core.baseline_store import BaselineStore
from core.device_controller import DeviceController, DeviceUnavailableError
from core.intent_parser import IntentParser
from core.knowledge_base import KnowledgeBase
from core.model_clients import create_llm_client, create_vlm_client
from core.replay_runner import ReplayRunner
from core.report_builder import ReportBuilder
from core.safety_guard import SafetyGuard
from core.semantic_scanner import SemanticScanner
from core.smart_perceiver import PerceptionMode, SmartPerceiver
from core.state_machine import StateMachine
from core.tool_context import ToolContext
from core.tools import set_tool_context

logger = logging.getLogger(__name__)


class ChatRunner:
    """统一调度器：Intent -> 语义扫描 / 意图测试 / 回放。"""

    def __init__(self, config: TestConfig):
        self.config = config
        # 使用抽象 LLMClient 创建 IntentParser，兼容 OpenAI / Zhipu 等任意 provider
        text_llm = create_llm_client(
            provider=config.llm_provider,
            model=config.model,
            api_key=config.api_key,
            base_url=config.base_url,
        )
        self.intent_parser = IntentParser(llm_client=text_llm)
        self._context: ToolContext | None = None
        self._event_callback: Callable[[str, dict[str, Any]], None] | None = None
        self._successful_case_cache_path = Path("storage") / "successful_cases.json"

    def set_event_callback(
        self, callback: Callable[[str, dict[str, Any]], None]
    ) -> None:
        """设置事件广播回调，用于 WebSocket 实时推送执行进度。"""
        self._event_callback = callback

    def parse(self, message: str) -> dict[str, Any]:
        logger.info("ChatRunner parse message=%s", str(message)[:500])
        parse_context = {"successful_cases": self._load_successful_case_summaries(limit=20)}
        try:
            intent = self.intent_parser.parse(message, extra_context=parse_context)
        except TypeError:
            # 兼容测试中 monkeypatch 的旧签名 parse(message)。
            intent = self.intent_parser.parse(message)
        intent = self._prefer_successful_case(message, intent)
        intent = self._enrich_case_intent_with_llm(message, intent)
        if intent.get("intent") == "run_case" and not intent.get("case_file"):
            candidates = self._find_case_candidates(intent)
            if len(candidates) == 1:
                intent["case_file"] = candidates[0]
            elif candidates:
                intent["case_candidates"] = candidates
                intent["case_file"] = candidates[0]
        intent["missing_fields"] = self._recompute_missing_fields(intent)
        return intent

    def run(self, message: str) -> dict[str, Any]:
        return self.run_with_intent(self.parse(message))

    def run_with_intent(self, intent: dict[str, Any]) -> dict[str, Any]:
        logger.info(
            "ChatRunner run_with_intent intent=%s app=%s case=%s",
            intent.get("intent", ""),
            intent.get("app_package", ""),
            intent.get("case_file", ""),
        )
        try:
            action = intent.get("intent", "run")
            if action == "generate_case":
                return self._generate_case(intent)
            if action == "run_case" and not intent.get("case_file"):
                candidates = self._find_case_candidates(intent)
                if len(candidates) == 1:
                    intent["case_file"] = candidates[0]
                elif candidates:
                    intent = {**intent, "case_candidates": candidates, "case_file": candidates[0]}
                    return {
                        "status": "need_input",
                        "message": "匹配到多个用例，请确认 case_file 后再执行",
                        "intent": intent,
                    }
                else:
                    return {
                        "status": "need_input",
                        "message": "未找到可执行用例，请先生成用例或指定 case_file",
                        "intent": intent,
                    }
            intent["missing_fields"] = self._recompute_missing_fields(intent)
            if intent.get("missing_fields"):
                return {
                    "status": "need_input",
                    "message": f"缺少字段: {', '.join(intent['missing_fields'])}",
                    "intent": intent,
                }
            self._restart_app_on_confirm(intent, action)
            if action == "traverse":
                return self._run_traverse(intent)
            if action == "replay":
                return self._run_replay(intent)
            if action == "run_case":
                primary_result = self._run_case(intent)
                if (
                    primary_result.get("status") != "success"
                    and bool(intent.get("_from_success_cache"))
                ):
                    logger.info(
                        "Run case failed on success-cache shortcut, fallback to matcher detection"
                    )
                    fallback_intent = dict(intent)
                    fallback_intent["_from_success_cache"] = False
                    fallback_intent.pop("case_file", None)
                    fallback_intent = self._enrich_case_intent_with_llm(
                        str(intent.get("task_description", "") or ""),
                        fallback_intent,
                    )
                    fallback_case = str(fallback_intent.get("case_file", "") or "")
                    if fallback_case:
                        primary_case = str(intent.get("case_file", "") or "")
                        if fallback_case.replace("/", "\\") != primary_case.replace("/", "\\"):
                            return self._run_case(fallback_intent)
                return primary_result
            return self._run_natural(intent)
        except DeviceUnavailableError as exc:
            logger.error("ChatRunner device unavailable: %s", exc)
            return {"status": "error", "message": str(exc), "intent": intent}

    def _restart_app_on_confirm(self, intent: dict[str, Any], action: str) -> None:
        package = (intent.get("app_package") or "").strip()
        if not package:
            return
        if action in {"run_case", "replay"} and intent.get("case_file"):
            return
        ctx = self._ensure_context(intent.get("safety_level", self.config.safety_level))
        launch_activity = intent.get("launch_activity") or self.config.launch_activity
        before = ctx.device.current_app()
        self._emit_status(
            f"[restart] force-stop/start {package} | before={before.get('package','')}/{before.get('activity','')}"
        )
        # 确认执行后统一做一次强制重启，避免沿用旧前台状态
        ctx.device.app_stop(package)
        mid = ctx.device.current_app()
        self._emit_status(
            f"[restart] stopped {package} | current={mid.get('package','')}/{mid.get('activity','')}"
        )
        self._start_app(ctx.device, package, launch_activity)
        after = ctx.device.current_app()
        self._emit_status(
            f"[restart] started {package} | after={after.get('package','')}/{after.get('activity','')}"
        )

    def snapshot(self, include_vision: bool = False) -> dict[str, Any]:
        ctx = self._ensure_context()
        snapshot = ctx.device.snapshot()
        perceiver = ctx.perceiver
        previous_mode = getattr(perceiver, "mode", PerceptionMode.UI_TREE)
        try:
            if hasattr(perceiver, "switch_mode"):
                if include_vision:
                    perceiver.switch_mode(PerceptionMode.HYBRID)
                else:
                    # 预览阶段默认不触发 Vision LLM，避免未执行就产生视觉调用。
                    perceiver.switch_mode(PerceptionMode.UI_TREE)
            understanding = perceiver.perceive()
        finally:
            if hasattr(perceiver, "switch_mode"):
                perceiver.switch_mode(previous_mode)
        return {
            "package": snapshot.package,
            "activity": snapshot.activity,
            "screen": {
                "width": snapshot.width,
                "height": snapshot.height,
                "image_base64": snapshot.image_base64,
            },
            "understanding": understanding.to_dict(),
        }

    # ── 遍历 ──

    def _run_traverse(self, intent: dict[str, Any]) -> dict[str, Any]:
        logger.info("Traverse start app=%s", intent.get("app_package", ""))
        ctx = self._ensure_context(intent.get("safety_level", self.config.safety_level))
        package = intent.get("app_package", "")
        launch_activity = intent.get("launch_activity") or self.config.launch_activity

        # 1. 确保在目标 App 内
        self._start_app(ctx.device, package, launch_activity)
        import time

        time.sleep(2)

        # 2. 重置状态机
        ctx.state_machine.reset()

        # 3. 创建报告，接入 WebSocket 事件流
        report = ReportBuilder(
            intent.get("task_description", "语义路径扫描"),
            "traverse",
            package,
            self.config.report_dir,
        )
        if self._event_callback:
            report.set_event_callback(self._event_callback)
        ctx.report_logger = report
        set_tool_context(ctx)

        # 4. 启动扫描
        scanner = SemanticScanner(
            device=ctx.device,
            perceiver=ctx.perceiver,
            baseline_store=ctx.baseline_store,
            anomaly_detector=ctx.anomaly_detector,
            safety_guard=ctx.safety_guard,
            state_machine=ctx.state_machine,
            report_logger=report,
            planner_llm=self.intent_parser.llm,
            max_depth=int(
                intent.get("traversal_max_depth", self.config.traversal_max_depth)
            ),
            max_pages=int(
                intent.get("traversal_max_pages", self.config.traversal_max_pages)
            ),
            max_clicks=self.config.traversal_max_clicks,
            launch_activity=launch_activity,
        )
        result = scanner.scan(package, intent.get("app_name") or "首页")
        logger.info(
            "Traverse done app=%s pages=%s clicks=%s",
            package,
            result.total_pages,
            scanner.click_count,
        )

        # 5. 知识提取
        if ctx.knowledge_base and result.total_pages > 0:
            self._extract_traverse_knowledge(ctx, package, scanner, result)

        report_data = report.save(
            conclusion=(
                f"扫描完成，共访问 {result.total_pages} 个页面，"
                f"导航覆盖 {result.traverse_summary.get('root_coverage', {}).get('clicked', 0)}"
                f"/{result.traverse_summary.get('root_coverage', {}).get('seen', 0)}"
            ),
            extra={
                "path_tree": result.path_tree,
                "visited_keys": result.visited_keys,
                "errors": result.errors,
                "traverse_summary": result.traverse_summary,
            },
        )
        return {
            "status": "success",
            "mode": "traverse",
            "total_pages": result.total_pages,
            "report_path": report_data["report_path"],
            "data": result.__dict__,
        }

    def _extract_traverse_knowledge(self, ctx, package, scanner, result):
        """从扫描结果中构建简化执行日志并存入知识库。"""
        log: list[dict[str, Any]] = []
        for key, node in result.path_tree.items():
            log.append(
                {
                    "page": node.get("name", "?"),
                    "action": node.get("action_to_reach", "入口"),
                    "observation": node.get("summary", ""),
                    "result": "success" if key in result.visited_keys else "fail",
                }
            )
        if log:
            ctx.knowledge_base.extract_from_test_result(
                package, f"traverse_{package}", log, "PASS"
            )

    # ── 回放（复用 run_case） ──

    def _run_replay(self, intent: dict[str, Any]) -> dict[str, Any]:
        case_file = intent.get("case_file", "")
        if not case_file:
            return {"status": "error", "message": "未指定回放用例文件"}
        return self._run_case(
            {"case_file": case_file, "intent": "run_case", "check_baseline": True}
        )

    # ── YAML 用例执行 ──

    def _run_case(self, intent: dict[str, Any]) -> dict[str, Any]:
        logger.info("Run case start case=%s", intent.get("case_file", ""))
        ctx = self._ensure_context(intent.get("safety_level", self.config.safety_level))
        case_file = intent.get("case_file", "")
        resolved_case_file = self._resolve_case_file_path(case_file)
        if not resolved_case_file:
            return {
                "status": "error",
                "message": f"用例文件不存在: {case_file}",
            }
        case_file = resolved_case_file
        check_baseline = intent.get("check_baseline", False)

        if check_baseline:
            import yaml, os

            if os.path.exists(case_file):
                with open(case_file, "r", encoding="utf-8") as f:
                    case = yaml.safe_load(f) or {}
                pkg = case.get("app_package", "")
                pages = ctx.baseline_store.list_pages(pkg)
                if not pages:
                    return {
                        "status": "error",
                        "message": f"未找到 {pkg} 的基线数据，请先执行遍历建基线（traverse）",
                    }

        ctx.state_machine.reset()
        report = ReportBuilder(
            case_file or "YAML 用例",
            "replay" if check_baseline else "run_case",
            intent.get("app_package", ""),
            self.config.report_dir,
        )
        if self._event_callback:
            report.set_event_callback(self._event_callback)
        ctx.report_logger = report
        set_tool_context(ctx)

        runner = ReplayRunner(ctx, report, check_baseline=check_baseline)
        result = runner.run_case_file(case_file)
        logger.info("Run case done status=%s case=%s", result.get("status"), case_file)
        if result.get("status") == "success":
            self._record_successful_case(case_file, result)

        if ctx.knowledge_base and result.get("status") != "error":
            self._extract_replay_knowledge(ctx, runner, result)

        saved = report.save(
            result.get("conclusion", ""), result.get("status", "error"), result
        )
        result["report_path"] = saved["report_path"]
        return result

    def _prefer_successful_case(self, message: str, intent: dict[str, Any]) -> dict[str, Any]:
        action = str(intent.get("intent", "") or "")
        if action not in {"run_case", "replay"}:
            return intent
        if str(intent.get("case_file", "") or "").strip():
            return intent
        selected = self._match_successful_case(message, intent)
        if not selected:
            return intent
        merged = dict(intent)
        merged["case_file"] = selected.get("case_file", "")
        if not merged.get("app_package") and selected.get("app_package"):
            merged["app_package"] = selected.get("app_package", "")
        if not merged.get("app_name") and selected.get("app_name"):
            merged["app_name"] = selected.get("app_name", "")
        merged["_from_success_cache"] = True
        logger.info("Case shortcut from successful cache case=%s", merged.get("case_file", ""))
        return merged

    def _generate_case(self, intent: dict[str, Any]) -> dict[str, Any]:
        logger.info("Generate case start task=%s", str(intent.get("task_description", ""))[:400])
        app_package = intent.get("app_package", "")
        app_name = intent.get("app_name", "")
        task = intent.get("task_description", "")
        case_name = (intent.get("case_name") or "").strip() or "auto_generated_case"
        safe_name = re.sub(r"[^0-9a-zA-Z_\-\u4e00-\u9fa5]+", "_", case_name).strip("_")
        if not safe_name:
            safe_name = f"case_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        case_path = os.path.join("test_cases", f"{safe_name}.yaml")
        os.makedirs("test_cases", exist_ok=True)

        case_obj = self._build_case_with_llm(task, app_package, app_name) or self._build_case_fallback(
            task, app_package, app_name
        )
        case_obj = self._normalize_generated_case(case_obj, app_package, app_name, safe_name)
        if not case_obj.get("name"):
            case_obj["name"] = safe_name
        if app_package and not case_obj.get("app_package"):
            case_obj["app_package"] = app_package
        if app_name and not case_obj.get("app_name"):
            case_obj["app_name"] = app_name
        yaml_text = yaml.safe_dump(
            case_obj,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
        with open(case_path, "w", encoding="utf-8") as f:
            f.write(yaml_text)
        logger.info("Generate case done file=%s", case_path)
        return {
            "status": "success",
            "mode": "generate_case",
            "message": f"已生成测试脚本: {case_path}",
            "case_file": case_path,
            "data": case_obj,
        }

    def _build_case_with_llm(
        self, task_description: str, app_package: str, app_name: str
    ) -> dict[str, Any] | None:
        llm = self.intent_parser.llm
        if not llm:
            return None
        prompt = (
            "你是移动端测试用例生成器。请把用户描述转换成 YAML 用例结构。"
            "仅输出 YAML。字段必须包含: name, description, app_package, app_name, steps, verification。"
            "steps 中 type 仅允许: launch_app, navigate_tab, type_text, click, wait, assert。"
            "每个 step 必须有 intent。"
            "字段要求: launch_app 使用 app_package; navigate_tab 使用 tab_name; click 使用 target;"
            "type_text/assert 使用 text; wait 使用 seconds(整数)。verification 必须是字符串数组。"
            "示例格式:\n"
            "name: \"反馈提交流程测试\"\n"
            "description: \"测试服务与反馈APP的反馈提交和验证流程\"\n"
            "app_package: \"com.tblenovo.center\"\n"
            "app_name: \"服务与反馈\"\n"
            "steps:\n"
            "  - intent: \"启动服务与反馈APP\"\n"
            "    type: \"launch_app\"\n"
            "    app_package: \"com.tblenovo.center\"\n"
            "  - intent: \"点击左侧导航栏的'反馈'选项\"\n"
            "    type: \"navigate_tab\"\n"
            "    tab_name: \"反馈\"\n"
            "  - intent: \"在右侧输入框中输入反馈内容\"\n"
            "    type: \"type_text\"\n"
            "    text: \"auto ai testing submit\"\n"
            "  - intent: \"点击提交按钮\"\n"
            "    type: \"click\"\n"
            "    target: \"提交\"\n"
            "  - intent: \"等待提交完成\"\n"
            "    type: \"wait\"\n"
            "    seconds: 2\n"
            "  - intent: \"检查反馈列表中是否存在刚提交的反馈\"\n"
            "    type: \"assert\"\n"
            "    text: \"auto ai testing submit\"\n"
            "verification:\n"
            "  - \"反馈列表中可以看到包含 'auto ai testing submit' 的记录\"\n"
            "  - \"页面无报错信息\"\n"
        )
        content = llm.invoke(
            [
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": str(
                        {
                            "task_description": task_description,
                            "app_package": app_package,
                            "app_name": app_name,
                        }
                    ),
                },
            ]
        )
        logger.info("Generate case LLM raw output=%s", str(content)[:1500])
        cleaned = str(content).strip()
        if "```" in cleaned:
            parts = cleaned.split("```")
            if len(parts) >= 2:
                cleaned = parts[1].replace("yaml", "", 1).strip()
        try:
            data = yaml.safe_load(cleaned) or {}
            if isinstance(data, dict) and isinstance(data.get("steps", []), list):
                return data
            return None
        except Exception:
            return None

    def _build_case_fallback(
        self, task_description: str, app_package: str, app_name: str
    ) -> dict[str, Any]:
        return {
            "name": "自动生成测试",
            "description": task_description or "由自然语言自动生成",
            "app_package": app_package,
            "app_name": app_name,
            "steps": [
                {
                    "intent": "启动应用",
                    "type": "launch_app",
                    "app_package": app_package,
                },
                {
                    "intent": "根据任务描述导航并操作",
                    "type": "navigate_tab",
                    "tab_name": "反馈",
                },
                {
                    "intent": "输入默认文本",
                    "type": "type_text",
                    "text": "auto ai testing submit",
                },
                {"intent": "点击提交", "type": "click", "target": "提交"},
                {"intent": "等待", "type": "wait", "seconds": 2},
                {
                    "intent": "断言提交文本出现",
                    "type": "assert",
                    "text": "auto ai testing submit",
                },
            ],
            "verification": ["页面无报错信息"],
        }

    def _normalize_generated_case(
        self,
        case_obj: dict[str, Any],
        app_package: str,
        app_name: str,
        default_name: str,
    ) -> dict[str, Any]:
        data = dict(case_obj or {})
        steps = data.get("steps", [])
        normalized_steps: list[dict[str, Any]] = []
        if not isinstance(steps, list):
            steps = []
        for idx, raw in enumerate(steps, 1):
            if not isinstance(raw, dict):
                continue
            step_type = str(raw.get("type", "") or "").strip()
            if step_type not in {"launch_app", "navigate_tab", "type_text", "click", "wait", "assert"}:
                continue
            intent = str(raw.get("intent", "") or "").strip() or f"执行步骤{idx}"
            normalized: dict[str, Any] = {"intent": intent, "type": step_type}
            if step_type == "launch_app":
                normalized["app_package"] = raw.get("app_package") or raw.get("package") or app_package
            elif step_type == "navigate_tab":
                normalized["tab_name"] = raw.get("tab_name") or raw.get("target") or raw.get("text") or ""
            elif step_type == "type_text":
                normalized["text"] = raw.get("text") or ""
            elif step_type == "click":
                normalized["target"] = raw.get("target") or raw.get("text") or ""
            elif step_type == "wait":
                secs = raw.get("seconds", raw.get("duration", raw.get("wait", 1)))
                try:
                    normalized["seconds"] = int(float(secs))
                except Exception:
                    normalized["seconds"] = 1
            elif step_type == "assert":
                normalized["text"] = raw.get("text") or raw.get("condition") or ""
            normalized_steps.append(normalized)
        if not normalized_steps:
            normalized_steps = self._build_case_fallback("", app_package, app_name).get("steps", [])
        verification = data.get("verification", [])
        if isinstance(verification, str):
            verification = [verification]
        if not isinstance(verification, list):
            verification = []
        verification = [str(item) for item in verification if str(item).strip()]
        if not verification:
            verification = ["页面无报错信息"]
        return {
            "name": str(data.get("name", "") or default_name),
            "description": str(data.get("description", "") or "由自然语言自动生成"),
            "app_package": str(data.get("app_package", "") or app_package),
            "app_name": str(data.get("app_name", "") or app_name),
            "steps": normalized_steps,
            "verification": verification,
        }

    def _resolve_case_file_path(self, case_file: str) -> str:
        raw = (case_file or "").strip()
        if not raw:
            return ""
        extracted = re.search(r"([0-9A-Za-z_\-./\\]+\.(?:ya?ml))", raw, flags=re.I)
        extracted_name = extracted.group(1) if extracted else ""
        raw_with_ext = raw if raw.lower().endswith((".yaml", ".yml")) else f"{raw}.yaml"
        candidates = [raw]
        candidates.append(raw_with_ext)
        if extracted_name:
            candidates.append(extracted_name)
        normalized = raw.replace("/", os.sep).replace("\\", os.sep)
        normalized_with_ext = (
            normalized if normalized.lower().endswith((".yaml", ".yml")) else f"{normalized}.yaml"
        )
        if normalized != raw:
            candidates.append(normalized)
        candidates.append(normalized_with_ext)
        if extracted_name:
            extracted_norm = extracted_name.replace("/", os.sep).replace("\\", os.sep)
            candidates.append(extracted_norm)
        if not os.path.isabs(normalized):
            candidates.append(os.path.join("test_cases", os.path.basename(normalized)))
            candidates.append(os.path.join("test_cases", normalized))
            candidates.append(os.path.join("test_cases", os.path.basename(normalized_with_ext)))
            candidates.append(os.path.join("test_cases", normalized_with_ext))
        if extracted_name:
            extracted_norm = extracted_name.replace("/", os.sep).replace("\\", os.sep)
            candidates.append(os.path.join("test_cases", os.path.basename(extracted_norm)))
            candidates.append(os.path.join("test_cases", extracted_norm))
        # 去重并去掉明显噪声前后缀
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in candidates:
            path = str(item or "").strip().strip("\"'`，。")
            if not path or path in seen:
                continue
            seen.add(path)
            cleaned.append(path)
        for path in cleaned:
            if path and os.path.exists(path):
                return path
        return ""

    def _load_successful_case_cache(self) -> list[dict[str, Any]]:
        path = self._successful_case_cache_path
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Load successful case cache failed: %s", exc)
            return []
        if not isinstance(data, list):
            return []
        rows: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            case_file = str(item.get("case_file", "") or "").strip()
            if not case_file:
                continue
            rows.append(
                {
                    "case_file": case_file.replace("/", "\\"),
                    "name": str(item.get("name", "") or ""),
                    "app_package": str(item.get("app_package", "") or ""),
                    "app_name": str(item.get("app_name", "") or ""),
                    "last_success_at": str(item.get("last_success_at", "") or ""),
                }
            )
        rows.sort(key=lambda row: row.get("last_success_at", ""), reverse=True)
        return rows

    def _save_successful_case_cache(self, rows: list[dict[str, Any]]) -> None:
        path = self._successful_case_cache_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(rows[:200], ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            logger.warning("Save successful case cache failed: %s", exc)

    def _load_successful_case_summaries(self, limit: int = 20) -> list[dict[str, str]]:
        rows = self._load_successful_case_cache()
        output: list[dict[str, str]] = []
        for row in rows[: max(limit, 0)]:
            output.append(
                {
                    "file": str(row.get("case_file", "") or ""),
                    "name": str(row.get("name", "") or ""),
                    "app_name": str(row.get("app_name", "") or ""),
                    "app_package": str(row.get("app_package", "") or ""),
                }
            )
        return output

    def _record_successful_case(self, case_file: str, result: dict[str, Any]) -> None:
        resolved = self._resolve_case_file_path(case_file) or case_file
        entry = {
            "case_file": str(resolved).replace("/", "\\"),
            "name": str(result.get("name", "") or ""),
            "app_package": str(result.get("app_package", "") or ""),
            "app_name": "",
            "last_success_at": datetime.now().isoformat(timespec="seconds"),
        }
        try:
            _, meta = self._load_case_text_and_meta(resolved)
            if meta.get("app_name"):
                entry["app_name"] = str(meta.get("app_name", "") or "")
        except Exception:
            pass
        rows = self._load_successful_case_cache()
        dedup: list[dict[str, Any]] = [entry]
        target_file = entry["case_file"].lower()
        for row in rows:
            if str(row.get("case_file", "") or "").lower() == target_file:
                continue
            dedup.append(row)
        self._save_successful_case_cache(dedup)

    def _match_successful_case(
        self, message: str, intent: dict[str, Any]
    ) -> dict[str, Any] | None:
        rows = self._load_successful_case_cache()
        if not rows:
            return None
        task = str(intent.get("task_description", "") or message or "")
        case_name = str(intent.get("case_name", "") or "")
        combined = f"{task} {case_name}".strip()
        lowered = combined.lower()
        keywords = [
            k.lower()
            for k in re.findall(r"[\u4e00-\u9fa5a-zA-Z0-9_]+", combined)
            if len(k) >= 2
        ]
        if not keywords and not lowered:
            return None
        scored: list[tuple[int, dict[str, Any]]] = []
        for row in rows:
            path = str(row.get("case_file", "") or "")
            if not path:
                continue
            resolved = self._resolve_case_file_path(path)
            if not resolved:
                continue
            stem = Path(resolved).stem.lower()
            base = Path(resolved).name.lower()
            name = str(row.get("name", "") or "").lower()
            app_name = str(row.get("app_name", "") or "").lower()
            text = " ".join([stem, base, name, app_name])
            score = 0
            if stem and stem in lowered:
                score += 8
            if base and base in lowered:
                score += 8
            for kw in keywords:
                if kw in text:
                    score += 2
            if score > 0:
                selected = dict(row)
                selected["case_file"] = resolved.replace("/", "\\")
                scored.append((score, selected))
        if not scored:
            return None
        scored.sort(
            key=lambda item: (item[0], str(item[1].get("last_success_at", ""))),
            reverse=True,
        )
        return scored[0][1]

    def _find_case_candidates(self, intent: dict[str, Any]) -> list[str]:
        case_root = Path("test_cases")
        if not case_root.exists():
            return []
        app_package = str(intent.get("app_package", "") or "").lower()
        app_name = str(intent.get("app_name", "") or "").lower()
        task = str(intent.get("task_description", "") or "").lower()
        case_name = str(intent.get("case_name", "") or "").lower()
        keywords = [
            k
            for k in re.findall(r"[\u4e00-\u9fa5a-zA-Z0-9_]+", f"{app_name} {task} {case_name}")
            if len(k) >= 2
        ]
        scored: list[tuple[int, str]] = []
        for path in list(case_root.rglob("*.yaml")) + list(case_root.rglob("*.yml")):
            try:
                text = path.read_text(encoding="utf-8")
            except Exception:
                continue
            lowered = text.lower()
            score = 0
            if app_package and app_package in lowered:
                score += 5
            if app_name and app_name in lowered:
                score += 3
            name_lower = path.name.lower()
            for kw in keywords:
                kwl = kw.lower()
                if kwl in name_lower:
                    score += 2
                if kwl in lowered:
                    score += 1
            if score > 0:
                scored.append((score, str(path).replace("/", "\\")))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [p for _, p in scored[:8]]

    def _enrich_case_intent_with_llm(self, message: str, intent: dict[str, Any]) -> dict[str, Any]:
        action = str(intent.get("intent", "") or "")
        if action not in {"run_case", "replay"}:
            return intent
        llm = self.intent_parser.llm
        if not llm:
            return intent

        case_index = self._build_case_index()
        if not case_index:
            return intent

        selected = self._llm_select_case_file(message, intent, case_index, llm)
        if not selected:
            return intent
        resolved = self._resolve_case_file_path(selected)
        if not resolved:
            logger.info("Case select by LLM but file missing selected=%s", selected)
            return intent

        case_text, case_meta = self._load_case_text_and_meta(resolved)
        finalized = self._llm_finalize_intent_with_case(message, intent, resolved, case_text, llm)

        merged = dict(intent)
        merged["case_file"] = resolved.replace("/", "\\")
        if isinstance(finalized, dict):
            merged.update({k: v for k, v in finalized.items() if v is not None})
        merged["case_file"] = resolved.replace("/", "\\")
        if not merged.get("app_package") and case_meta.get("app_package"):
            merged["app_package"] = case_meta.get("app_package", "")
        if not merged.get("app_name") and case_meta.get("app_name"):
            merged["app_name"] = case_meta.get("app_name", "")

        logger.info(
            "Case intent enriched by LLM case=%s app=%s",
            merged.get("case_file", ""),
            merged.get("app_package", ""),
        )
        return merged

    def _build_case_index(self) -> list[dict[str, str]]:
        case_root = Path("test_cases")
        if not case_root.exists():
            return []
        rows: list[dict[str, str]] = []
        for path in list(case_root.rglob("*.yaml")) + list(case_root.rglob("*.yml")):
            try:
                _, meta = self._load_case_text_and_meta(str(path))
            except Exception:
                continue
            rows.append(
                {
                    "file": str(path).replace("/", "\\"),
                    "name": str(meta.get("name", "") or ""),
                    "description": str(meta.get("description", "") or ""),
                    "app_name": str(meta.get("app_name", "") or ""),
                    "app_package": str(meta.get("app_package", "") or ""),
                }
            )
        return rows

    def _llm_select_case_file(
        self,
        message: str,
        intent: dict[str, Any],
        case_index: list[dict[str, str]],
        llm,
    ) -> str:
        prompt = (
            "你是测试用例匹配器。根据用户输入和用例索引，选择最匹配的 case_file。"
            "只输出 JSON: {\"case_file\":\"...\",\"reason\":\"...\"}。"
            "若无法确定，case_file 置空字符串。"
        )
        payload = {
            "user_input": message,
            "intent": intent,
            "case_index": case_index[:60],
        }
        try:
            content = llm.invoke(
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ]
            )
            logger.info("Case match LLM raw output=%s", str(content)[:1200])
            data = self._parse_json_content(content)
            return str((data or {}).get("case_file", "") or "").strip()
        except Exception as exc:
            logger.warning("Case match LLM failed: %s", exc)
            return ""

    def _llm_finalize_intent_with_case(
        self,
        message: str,
        intent: dict[str, Any],
        case_file: str,
        case_text: str,
        llm,
    ) -> dict[str, Any] | None:
        prompt = (
            "你是 Android 自动化测试意图整合器。"
            "根据用户输入、当前意图、目标用例内容，输出最终 JSON。"
            "字段: intent, app_package, app_name, task_description, case_file, case_name, "
            "scope, target_pages, extra_context, traversal_max_depth, traversal_max_pages, "
            "confidence, need_confirmation。只输出 JSON。"
        )
        payload = {
            "user_input": message,
            "current_intent": intent,
            "selected_case_file": case_file,
            "selected_case_content": case_text[:6000],
        }
        try:
            content = llm.invoke(
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ]
            )
            logger.info("Case finalize LLM raw output=%s", str(content)[:1200])
            data = self._parse_json_content(content)
            return data if isinstance(data, dict) else None
        except Exception as exc:
            logger.warning("Case finalize LLM failed: %s", exc)
            return None

    def _parse_json_content(self, content: Any) -> dict[str, Any] | None:
        text = str(content or "").strip()
        if "```" in text:
            parts = text.split("```")
            if len(parts) >= 2:
                text = parts[1].replace("json", "", 1).strip()
        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _load_case_text_and_meta(self, case_file: str) -> tuple[str, dict[str, Any]]:
        with open(case_file, "r", encoding="utf-8") as f:
            text = f.read()
        data = yaml.safe_load(text) or {}
        if not isinstance(data, dict):
            data = {}
        return text, data

    def _recompute_missing_fields(self, intent: dict[str, Any]) -> list[str]:
        action = str(intent.get("intent", "") or "")
        app_package = str(intent.get("app_package", "") or "").strip()
        case_file = str(intent.get("case_file", "") or "").strip()
        missing: list[str] = []
        if action in {"traverse", "run"} and not app_package:
            missing.append("app_package")
        if action in {"run_case", "replay"} and not case_file:
            missing.append("case_file")
        return missing

    def _extract_replay_knowledge(self, ctx, runner, result):
        """从回放结果中提取知识。"""
        log: list[dict[str, Any]] = []
        for step in runner._collected_steps:
            log.append(
                {
                    "page": step.get("target", "?"),
                    "action": step.get("action", step.get("intent", "?")),
                    "observation": step.get("message", ""),
                    "result": "success" if step.get("status") == "success" else "fail",
                    "error": (
                        step.get("message", "")
                        if step.get("status") != "success"
                        else ""
                    ),
                }
            )
        if log:
            pkg = result.get("app_package", "") or self.config.baseline_dir
            ctx.knowledge_base.extract_from_test_result(
                pkg,
                result.get("name", "replay"),
                log,
                "PASS" if result.get("status") == "success" else "FAIL",
            )

    # ── 自然语言执行 ──

    def _run_natural(self, intent: dict[str, Any]) -> dict[str, Any]:
        ctx = self._ensure_context(intent.get("safety_level", self.config.safety_level))
        package = intent.get("app_package", "")
        if package:
            launch_activity = intent.get("launch_activity") or self.config.launch_activity
            self._start_app(ctx.device, package, launch_activity)
            import time

            time.sleep(2)

        ctx.state_machine.reset()
        report = ReportBuilder(
            intent.get("task_description", "自然语言测试"),
            "run",
            package,
            self.config.report_dir,
        )
        if self._event_callback:
            report.set_event_callback(self._event_callback)
        ctx.report_logger = report
        set_tool_context(ctx)

        task = self._build_task(intent)
        # 使用 provider 参数，适配 OpenAI / Zhipu 等不同厂商
        agent = create_test_agent(
            provider=self.config.llm_provider,
            model=self.config.model,
            api_key=self.config.api_key,
            base_url=self.config.base_url,
            knowledge_base=ctx.knowledge_base,
            app_package=package,
        )
        result = agent.invoke(
            {"messages": [{"role": "user", "content": task}]},
            config={
                "configurable": {
                    "thread_id": f"run-{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                }
            },
        )
        final = result["messages"][-1]
        final_message = (
            final.get("content", "") if isinstance(final, dict) else final.content
        )
        status = "success" if "FAIL" not in final_message.upper() else "fail"

        # 知识提取
        if ctx.knowledge_base and package:
            log_file = "storage/results/test_log.jsonl"
            if __import__("os").path.exists(log_file):
                import json

                log = [json.loads(l) for l in open(log_file, encoding="utf-8")]
                ctx.knowledge_base.extract_from_test_result(
                    package,
                    intent.get("task_description", "natural"),
                    log,
                    "PASS" if status == "success" else "FAIL",
                )

        saved = report.save(final_message, status)
        return {
            "status": saved["status"],
            "mode": "run",
            "conclusion": final_message,
            "report_path": saved["report_path"],
        }

    def _start_app(self, device, package: str, activity: str | None = None) -> None:
        try:
            device.app_start(package, activity=activity)
        except TypeError:
            device.app_start(package)

    def _emit_status(self, message: str) -> None:
        if self._event_callback:
            self._event_callback("status", message)

    def _build_task(self, intent: dict[str, Any]) -> str:
        return f"""测试任务: {intent.get('task_description', '')}
目标应用: {intent.get('app_name', '')} ({intent.get('app_package', '')})

执行要求:
1. 先理解当前页面布局和主要入口。
2. 如果是 Wi-Fi 开关测试，需要分别验证关闭和打开后的 UI 状态。
3. 每一步都调用页面健康检查。
4. 操作和断言必须基于当前页面实际状态。
5. 输出 PASS/FAIL 和原因。
"""

    # ── 上下文初始化（单例） ──

    def _ensure_context(self, safety_level: str | None = None) -> ToolContext:
        if self._context is not None:
            return self._context

        device = DeviceController(self.config.device_serial)

        # Vision: 使用抽象 VLMClient 工厂，兼容 OpenAI / Zhipu / 其他多模态 provider
        vlm_client = create_vlm_client(
            provider=self.config.vision_provider,
            model=self.config.vision_model,
            api_key=self.config.vision_api_key,
            base_url=self.config.vision_base_url,
        )

        # RAG 知识库
        kb: KnowledgeBase | None = None
        if self.config.enable_rag:
            kb = KnowledgeBase(
                persist_dir=self.config.rag_persist_dir,
                embedding_model=self.config.embedding_model,
                api_key=self.config.api_key,
                base_url=self.config.base_url,
            )

        baseline_store = BaselineStore(self.config.baseline_dir)
        # SmartPerceiver 接受 VLMClient 接口（而非裸 ChatOpenAI）
        perceiver = SmartPerceiver(
            device, llm_client=vlm_client, mode=PerceptionMode.HYBRID
        )
        detector = AnomalyDetector(device, baseline_store, self.config)
        sm = StateMachine(device, max_recovery=5)

        context = ToolContext(
            device=device,
            perceiver=perceiver,
            baseline_store=baseline_store,
            anomaly_detector=detector,
            safety_guard=SafetyGuard(safety_level or self.config.safety_level),
            knowledge_base=kb,
            state_machine=sm,
        )
        set_tool_context(context)
        self._context = context
        return context

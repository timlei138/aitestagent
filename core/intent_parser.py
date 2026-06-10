from __future__ import annotations

import json
import logging
import re
from typing import Any

from core.model_clients import LLMClient

logger = logging.getLogger(__name__)

APP_NAME_MAP = {
    "settings": "com.android.settings",
    "setting": "com.android.settings",
    "设置": "com.android.settings",
    "联系人": "com.android.contacts",
    "相机": "com.android.camera",
    "电话": "com.android.dialer",
    "信息": "com.android.mms",
    "浏览器": "com.android.browser",
    "时钟": "com.android.deskclock",
    "计算器": "com.android.calculator2",
    "文件管理": "com.android.filemanager",
    "日历": "com.android.calendar",
    "服务与反馈": "com.tblenovo.center",
}


class IntentParser:
    def __init__(
        self,
        llm_client: LLMClient | None = None,
    ):
        self.llm = llm_client

    def parse(
        self, user_input: str, extra_context: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        logger.info("Intent parse start text=%s", user_input[:300])
        quick_hint = self._quick_parse(user_input)
        if self.llm:
            try:
                result = self._llm_parse(user_input, quick_hint, extra_context)
                logger.info("Intent parse llm-final result=%s", result.get("intent"))
                return result
            except Exception as exc:
                logger.warning("Intent parse llm failed: %s", exc)
        if quick_hint:
            logger.info("Intent parse quick fallback result=%s", quick_hint.get("intent"))
            return quick_hint
        fallback = self._fallback(user_input)
        logger.info("Intent parse fallback result=%s", fallback.get("intent"))
        return fallback

    def _quick_parse(self, text: str) -> dict[str, Any] | None:
        lowered = text.lower()
        case_file = self._extract_case_file(text)
        if self._looks_like_case_description(text):
            return self._intent("generate_case", text, confidence=0.9)
        if any(
            k in lowered
            for k in [
                "生成用例",
                "生成测试脚本",
                "生成yaml",
                "生成 yml",
                "写用例",
                "脚本生成",
                "generate case",
                "generate yaml",
            ]
        ):
            return self._intent("generate_case", text, confidence=0.93)
        if case_file:
            intent = self._intent("run_case", text, case_file=case_file, confidence=0.95)
            intent["need_confirmation"] = not self._is_direct_execute_command(text, case_file)
            return intent
        if any(k in lowered for k in ["执行", "验证", "流程", "testcase", "用例"]) and any(
            k in lowered for k in ["提交", "反馈", "回归", "验证", "流程"]
        ):
            return self._intent("run_case", text, confidence=0.82)
        if any(k in lowered for k in ["回放", "replay", "重放"]):
            return self._intent("replay", text, case_file=case_file, confidence=0.9)
        if any(
            k in lowered
            for k in [
                "全路径",
                "路径扫描",
                "全量扫描",
                "语义扫描",
                "遍历",
                "建基线",
                "traverse",
                "scan",
            ]
        ):
            return self._intent("traverse", text, confidence=0.88)
        if any(k in lowered for k in ["wifi", "wi-fi", "无线"]) and any(
            k in lowered for k in ["开关", "打开", "关闭", "检查", "验证"]
        ):
            intent = self._intent("run", text, confidence=0.9)
            intent["test_type"] = "toggle_check"
            intent["target"] = "Wi-Fi"
            intent["expected_states"] = {
                "off": ["关闭", "Off", "未连接"],
                "on": ["开启", "On", "正在搜索", "已连接"],
            }
            if not intent["app_package"]:
                intent["app_package"] = "com.android.settings"
                intent["app_name"] = "Settings"
            return intent
        return None

    def _llm_parse(
        self,
        user_input: str,
        quick_hint: dict[str, Any] | None = None,
        extra_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        prompt = """你是 Android 自动化测试意图解析器。把用户输入解析为结构化 JSON。
字段: intent(traverse/replay/run/run_case/generate_case), app_package, app_name, task_description, case_file, case_name, scope, target_pages, extra_context, traversal_max_depth, traversal_max_pages, confidence, missing_fields。
规则:
1) 必须返回合法 JSON 对象，不能输出 markdown。
2) task_description 填原始用户输入。
3) 如果识别到 yaml/yml 文件，intent 优先 run_case 且写入 case_file。
4) 如果是“生成步骤脚本”意图，intent=generate_case。
5) 缺字段时写入 missing_fields 数组。
6) 若给到 successful_cases（历史已跑通脚本），优先参考其 file/name 做 run_case 匹配。
"""
        hint_text = json.dumps(quick_hint or {}, ensure_ascii=False)
        context_text = json.dumps(extra_context or {}, ensure_ascii=False)
        content = self.llm.invoke(
            [
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": (
                        f"user_input={user_input}\n"
                        f"quick_hint={hint_text}\n"
                        f"extra_context={context_text}"
                    ),
                },
            ]
        )
        logger.info("Intent LLM raw output=%s", str(content)[:1000])
        text = str(content).strip()
        if "```" in text:
            parts = text.split("```")
            if len(parts) >= 2:
                text = parts[1].replace("json", "", 1).strip()
        data = json.loads(text)
        return self._normalize(data, user_input)

    def _intent(
        self, intent: str, text: str, case_file: str = "", confidence: float = 0.7
    ) -> dict[str, Any]:
        app_name, app_package = self._resolve_app(text)
        data = {
            "intent": intent,
            "confidence": confidence,
            "need_confirmation": True,
            "missing_fields": [],
            "app_package": app_package,
            "app_name": app_name,
            "task_description": text,
            "case_file": case_file,
            "case_name": "",
            "scope": "full",
            "target_pages": [],
            "extra_context": "",
            "traversal_max_depth": 5,
            "traversal_max_pages": 50,
            "safety_level": "strict",
        }
        if intent in {"traverse", "run"} and not app_package:
            data["missing_fields"].append("app_package")
        return data

    def _normalize(self, data: dict[str, Any], text: str) -> dict[str, Any]:
        base = self._intent(data.get("intent", "run"), text)
        base.update({k: v for k, v in data.items() if v is not None})
        if base.get("intent") in {"run_case", "replay"}:
            case_file = str(base.get("case_file", "") or "").strip()
            case_name = str(base.get("case_name", "") or "").strip()
            if not case_file and case_name:
                case_file = case_name
            if case_file and not case_file.lower().endswith((".yaml", ".yml")):
                case_file = f"{case_file}.yaml"
            if case_file:
                base["case_file"] = case_file
        if not base.get("app_package"):
            _, package = self._resolve_app(f"{base.get('app_name', '')} {text}")
            base["app_package"] = package
        base["missing_fields"] = self._recompute_missing_fields(base)
        base["need_confirmation"] = self._recompute_need_confirmation(base, text)
        return base

    def _recompute_missing_fields(self, intent_data: dict[str, Any]) -> list[str]:
        intent_name = str(intent_data.get("intent", "") or "")
        missing: list[str] = []
        app_package = str(intent_data.get("app_package", "") or "").strip()
        case_file = str(intent_data.get("case_file", "") or "").strip()

        if intent_name in {"traverse", "run"} and not app_package:
            missing.append("app_package")
        if intent_name in {"run_case", "replay"} and not case_file:
            missing.append("case_file")
        return missing

    def _recompute_need_confirmation(self, intent_data: dict[str, Any], text: str) -> bool:
        intent_name = str(intent_data.get("intent", "") or "")
        case_file = str(intent_data.get("case_file", "") or "").strip()
        if intent_name in {"run_case", "replay"} and case_file:
            if self._is_direct_execute_command(text, case_file):
                return False
        return bool(intent_data.get("need_confirmation", True))

    def _is_direct_execute_command(self, text: str, case_file: str) -> bool:
        lowered = text.lower().strip()
        file_lower = case_file.lower().strip()
        file_stem = file_lower.rsplit(".", 1)[0] if "." in file_lower else file_lower
        execute_prefixes = ("执行", "run ", "run_case", "回放", "replay")
        has_prefix = lowered.startswith(execute_prefixes)
        has_file = bool(file_lower and file_lower in lowered) or bool(file_stem and file_stem in lowered)
        return has_prefix and has_file

    def _fallback(self, text: str) -> dict[str, Any]:
        return self._intent("run", text, confidence=0.5)

    def _resolve_app(self, text: str) -> tuple[str, str]:
        lowered = text.lower()
        for name, package in APP_NAME_MAP.items():
            if name.lower() in lowered:
                return name, package
        package_match = re.search(r"\b[a-zA-Z][\w]*(?:\.[\w]+){2,}\b", text)
        if package_match:
            return "", package_match.group(0)
        return "", ""

    def _extract_case_file(self, text: str) -> str:
        match = re.search(r"[\w.\\/-]+\.(?:ya?ml)", text, flags=re.I)
        return match.group(0) if match else ""

    def _looks_like_case_description(self, text: str) -> bool:
        lowered = text.lower()
        if any(k in lowered for k in ["生成用例", "生成测试脚本", "generate yaml", "generate case"]):
            return True
        action_keywords = [
            "打开",
            "启动",
            "点击",
            "输入",
            "等待",
            "提交",
            "断言",
            "看到",
            "检查",
            "进入",
            "then",
            "click",
            "input",
            "wait",
            "assert",
        ]
        hit_count = sum(1 for k in action_keywords if k in lowered)
        has_sequence = ("然后" in text) or ("->" in text) or ("后" in text)
        return has_sequence and hit_count >= 3

from __future__ import annotations

import json
from pathlib import Path

from config import TestConfig as AppConfig
from core.chat_runner import ChatRunner


class FakeLLM:
    def __init__(self, responses: list[dict]):
        self._responses = [json.dumps(item, ensure_ascii=False) for item in responses]
        self.calls = 0

    def invoke(self, messages):
        idx = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[idx]


def test_parse_run_case_with_llm_index_and_finalize(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    case_dir = tmp_path / "test_cases"
    case_dir.mkdir(parents=True, exist_ok=True)
    case_path = case_dir / "auto_generated_case.yaml"
    case_path.write_text(
        "\n".join(
            [
                'name: "反馈提交流程测试"',
                'description: "测试服务与反馈APP的反馈提交和验证流程"',
                'app_package: "com.tblenovo.center"',
                'app_name: "服务与反馈"',
                "steps:",
                '  - intent: "启动服务与反馈APP"',
                '    type: "launch_app"',
                '    app_package: "com.tblenovo.center"',
            ]
        ),
        encoding="utf-8",
    )

    runner = ChatRunner(AppConfig(api_key=None, vision_api_key=None))
    runner.intent_parser.parse = lambda _: {
        "intent": "run_case",
        "task_description": "执行 auto_generated_case 脚本",
        "case_file": "",
        "case_name": "",
        "app_package": "",
        "app_name": "",
        "need_confirmation": True,
        "missing_fields": [],
    }
    runner.intent_parser.llm = FakeLLM(
        [
            {"case_file": "auto_generated_case.yaml", "reason": "best"},
            {
                "intent": "run_case",
                "case_file": "auto_generated_case.yaml",
                "app_package": "com.tblenovo.center",
                "app_name": "服务与反馈",
                "need_confirmation": False,
            },
        ]
    )

    result = runner.parse("执行 auto_generated_case 脚本")
    assert result["intent"] == "run_case"
    assert result["case_file"].endswith("test_cases\\auto_generated_case.yaml")
    assert result["app_package"] == "com.tblenovo.center"
    assert result["missing_fields"] == []


def test_parse_prefers_successful_case_cache(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    case_dir = tmp_path / "test_cases"
    case_dir.mkdir(parents=True, exist_ok=True)
    case_path = case_dir / "feedback_case.yaml"
    case_path.write_text(
        "\n".join(
            [
                'name: "反馈脚本"',
                'app_package: "com.tblenovo.center"',
                'app_name: "服务与反馈"',
                "steps: []",
            ]
        ),
        encoding="utf-8",
    )
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir(parents=True, exist_ok=True)
    (storage_dir / "successful_cases.json").write_text(
        json.dumps(
            [
                {
                    "case_file": "test_cases\\feedback_case.yaml",
                    "name": "反馈脚本",
                    "app_package": "com.tblenovo.center",
                    "app_name": "服务与反馈",
                    "last_success_at": "2026-06-07T18:00:00",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    runner = ChatRunner(AppConfig(api_key=None, vision_api_key=None))
    runner.intent_parser.parse = lambda _: {
        "intent": "run_case",
        "task_description": "执行 feedback_case 脚本",
        "case_file": "",
        "case_name": "",
        "app_package": "",
        "app_name": "",
        "need_confirmation": False,
        "missing_fields": [],
    }
    runner.intent_parser.llm = None

    result = runner.parse("执行 feedback_case 脚本")
    assert result["case_file"].endswith("test_cases\\feedback_case.yaml")
    assert result.get("_from_success_cache") is True


def test_run_case_fallback_to_matcher_when_cache_shortcut_failed(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    case_dir = tmp_path / "test_cases"
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "old_case.yaml").write_text(
        'name: "旧脚本"\napp_package: "com.tblenovo.center"\napp_name: "服务与反馈"\nsteps: []\n',
        encoding="utf-8",
    )
    (case_dir / "new_case.yaml").write_text(
        'name: "新脚本"\napp_package: "com.tblenovo.center"\napp_name: "服务与反馈"\nsteps: []\n',
        encoding="utf-8",
    )

    runner = ChatRunner(AppConfig(api_key=None, vision_api_key=None))
    runner.intent_parser.llm = FakeLLM(
        [
            {"case_file": "new_case.yaml", "reason": "fallback"},
            {
                "intent": "run_case",
                "case_file": "new_case.yaml",
                "app_package": "com.tblenovo.center",
                "app_name": "服务与反馈",
            },
        ]
    )
    calls: list[str] = []

    def fake_run_case(intent):
        calls.append(str(intent.get("case_file", "")))
        if len(calls) == 1:
            return {"status": "fail", "conclusion": "FAIL: old"}
        return {"status": "success", "conclusion": "PASS"}

    runner._run_case = fake_run_case  # type: ignore[method-assign]

    result = runner.run_with_intent(
        {
            "intent": "run_case",
            "task_description": "执行反馈脚本",
            "case_file": "test_cases\\old_case.yaml",
            "_from_success_cache": True,
            "missing_fields": [],
            "need_confirmation": False,
            "app_package": "",
            "app_name": "",
        }
    )
    assert result["status"] == "success"
    assert len(calls) == 2
    assert calls[0].endswith("old_case.yaml")
    assert calls[1].endswith("new_case.yaml")

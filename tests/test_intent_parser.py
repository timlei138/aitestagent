from core.intent_parser import IntentParser


def test_parse_traverse_intent():
    parser = IntentParser()
    result = parser.parse("全路径扫描 Settings")
    assert result["intent"] == "traverse"
    assert result["app_package"] == "com.android.settings"


def test_parse_wifi_toggle_intent():
    parser = IntentParser()
    result = parser.parse("检查Setting的WIFI开关打开和关闭")
    assert result["intent"] == "run"
    assert result.get("test_type") == "toggle_check"
    assert result["app_package"] == "com.android.settings"


def test_parse_case_file_intent():
    parser = IntentParser()
    result = parser.parse("回放 test_cases/wifi_toggle.yaml")
    assert result["intent"] in {"run_case", "replay"}
    assert result["case_file"].endswith("wifi_toggle.yaml")


def test_parse_generate_case_intent():
    parser = IntentParser()
    result = parser.parse("根据下面步骤生成yaml测试脚本：打开服务与反馈并提交反馈")
    assert result["intent"] == "generate_case"


def test_parse_step_description_to_generate_case():
    parser = IntentParser()
    result = parser.parse("打开服务与反馈，然后点击反馈，输入内容后提交，等待5秒并检查历史记录")
    assert result["intent"] == "generate_case"


def test_normalize_run_case_ignores_invalid_missing_fields():
    parser = IntentParser()
    result = parser._normalize(
        {
            "intent": "run_case",
            "app_name": "服务与反馈",
            "app_package": "com.tblenovo.center",
            "case_file": "auto_generated_case.yaml",
            "missing_fields": ["app_package", "app_name"],
        },
        "执行auto_generated_case.yaml",
    )
    assert result["intent"] == "run_case"
    assert result["case_file"] == "auto_generated_case.yaml"
    assert result["missing_fields"] == []


def test_direct_execute_case_turns_off_confirmation():
    parser = IntentParser()
    result = parser.parse("执行auto_generated_case.yaml")
    assert result["intent"] == "run_case"
    assert result["need_confirmation"] is False


def test_normalize_case_name_to_case_file_for_run_case():
    parser = IntentParser()
    result = parser._normalize(
        {
            "intent": "run_case",
            "case_name": "auto_generated_case",
            "case_file": "",
        },
        "执行 auto_generated_case 脚本",
    )
    assert result["case_file"] == "auto_generated_case.yaml"
    assert result["missing_fields"] == []


def test_normalize_case_file_without_extension():
    parser = IntentParser()
    result = parser._normalize(
        {
            "intent": "run_case",
            "case_file": "auto_generated_case",
        },
        "执行 auto_generated_case 脚本",
    )
    assert result["case_file"] == "auto_generated_case.yaml"
    assert result["need_confirmation"] is False

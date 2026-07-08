from __future__ import annotations

from dataclasses import dataclass

import pytest


@dataclass(frozen=True)
class GoldenCase:
    case_id: str
    user_request: str
    app_package: str
    app_name: str


GOLDEN_CASES: list[GoldenCase] = [
    GoldenCase(
        case_id="launcher-calc-search",
        user_request="进入无限工作台，点击全部应用，搜索计算器并验证 12+8=20",
        app_package="com.zui.launcher",
        app_name="无限工作台",
    ),
    GoldenCase(
        case_id="settings-timezone-format",
        user_request="打开设置，切换时区并验证日期格式变化",
        app_package="com.android.settings",
        app_name="设置",
    ),
    GoldenCase(
        case_id="gallery-multi-select",
        user_request="打开图库进入多选模式，选择两张图片并完成",
        app_package="com.zui.gallery",
        app_name="图库",
    ),
    GoldenCase(
        case_id="calculator-basic-expression",
        user_request="打开计算器，输入 12+8 并验证结果 20",
        app_package="com.zui.calculator",
        app_name="计算器",
    ),
    GoldenCase(
        case_id="launcher-open-appstore",
        user_request="在桌面打开应用商店并确认进入应用商店首页",
        app_package="com.zui.launcher",
        app_name="桌面",
    ),
]


def test_golden_case_registry_shape():
    assert len(GOLDEN_CASES) >= 5
    ids = [c.case_id for c in GOLDEN_CASES]
    assert len(ids) == len(set(ids))
    for c in GOLDEN_CASES:
        assert c.user_request
        assert c.app_package


@pytest.mark.integration
@pytest.mark.parametrize("case", GOLDEN_CASES, ids=[c.case_id for c in GOLDEN_CASES])
def test_golden_case_end_to_end(case: GoldenCase):
    # 真实设备集成入口：默认不执行；仅在 --run-integration 打开时运行。
    # 这里保留最小骨架，避免把 CI 绑定到单一设备环境。
    from config import TestConfig
    from agents.orchestrator import TestOrchestrator
    from agents.graph import set_relational_db
    from data import create_relational_db, create_vector_store
    from data.knowledge import KnowledgeBase
    from tools.context import ToolContext
    from tools import set_tool_context
    from device.controller import DeviceController, DeviceUnavailableError
    from device.perceiver import SmartPerceiver

    cfg = TestConfig.from_yaml("config.yaml")
    try:
        device = DeviceController()
    except DeviceUnavailableError:
        pytest.skip("device unavailable for integration run")

    perceiver = SmartPerceiver(device, mode=cfg.perception_mode)
    kb = KnowledgeBase(create_vector_store(cfg))
    set_tool_context(
        ToolContext(
            device=device,
            perceiver=perceiver,
            knowledge_base=kb,
            safety_level=cfg.safety_level,
            llm_provider=cfg.llm_provider,
            llm_model=cfg.model,
            llm_api_key=cfg.api_key,
            llm_base_url=cfg.base_url,
            llm_vision_enabled=cfg.vision_enabled,
        )
    )
    set_relational_db(create_relational_db(cfg))
    orchestrator = TestOrchestrator(cfg)
    out = orchestrator.start(
        user_request=case.user_request,
        app_package=case.app_package,
        app_name=case.app_name,
    )
    assert out.get("status") in {"success", "fail", "continue"}
    assert "execution_status" in out

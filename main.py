from __future__ import annotations

import argparse
import json
import logging

from config import TestConfig, resolve_perception_mode
from data import create_vector_store, create_relational_db
from agents.graph import set_relational_db
from agents.orchestrator import TestOrchestrator
from tools.context import ToolContext
from tools import set_tool_context
from device.controller import DeviceController, DeviceUnavailableError
from device.perceiver import SmartPerceiver
from llm.multimodal import multimodal_vision_call, reset_vision_capability_state
from data.knowledge import KnowledgeBase
from api.apps_routes import resolve_app as _resolve_app_from_yaml


def main():
    parser = argparse.ArgumentParser(description="AI 自动化测试 Agent")
    sub = parser.add_subparsers(dest="mode")

    run_parser = sub.add_parser("run", help="自然语言执行测试")
    run_parser.add_argument("message", nargs="+", help="测试需求描述")
    run_parser.add_argument("--config", default="config.yaml")

    server_parser = sub.add_parser("server", help="启动 Web 服务")
    server_parser.add_argument("--config", default="config.yaml")
    server_parser.add_argument("--host", default="127.0.0.1")
    server_parser.add_argument("--port", type=int, default=8080)

    args = parser.parse_args()

    if args.mode == "server":
        import uvicorn

        uvicorn.run("api.server:app", host=args.host, port=args.port, reload=False)
        return

    if args.mode == "run":
        config = TestConfig.from_yaml(getattr(args, "config", "config.yaml"))
        user_request = " ".join(args.message)

        _init_tool_context(config)
        set_relational_db(create_relational_db(config))
        orchestrator = TestOrchestrator(config)

        app_package, app_name = _quick_resolve_app(user_request)
        result = orchestrator.start(
            user_request=user_request,
            app_package=app_package,
            app_name=app_name,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return

    parser.print_help()


def _init_tool_context(config: TestConfig) -> None:
    try:
        device = DeviceController()
        reset_vision_capability_state()
        mode, auto_switch = resolve_perception_mode(config)
        ctx_holder: dict[str, ToolContext | None] = {"ctx": None}

        def _vision_call(
            prompt: str, image_base64: str, purpose: str, strict_json: bool
        ):
            return multimodal_vision_call(
                prompt=prompt,
                image_base64=image_base64,
                purpose=purpose,
                strict_json=strict_json,
                provider=config.llm_provider,
                model=config.model,
                api_key=config.api_key,
                base_url=config.base_url,
                vision_enabled=config.vision_enabled,
                timeout_sec=12,
            )

        def _screenshot_sink(path: str) -> None:
            if ctx_holder["ctx"] is not None:
                ctx_holder["ctx"]._last_screenshot_path = path

        perceiver = SmartPerceiver(
            device,
            vision_call=_vision_call,
            screenshot_sink=_screenshot_sink,
            mode=mode,
            auto_switch=auto_switch,
        )
        kb = KnowledgeBase(create_vector_store(config))
        # 经验推断挂载：按 rid 查知识库给无标签图标补 rag_hint（不污染 label）
        perceiver.attach_knowledge(
            kb, lambda: device.current_app().get("package", "")
        )
        ctx = ToolContext(
            device=device,
            perceiver=perceiver,
            knowledge_base=kb,
            safety_level=config.safety_level,
            llm_provider=config.llm_provider,
            llm_model=config.model,
            llm_api_key=config.api_key,
            llm_base_url=config.base_url,
            llm_vision_enabled=config.vision_enabled,
            click_mode=config.click_mode,
        )
        ctx_holder["ctx"] = ctx
        set_tool_context(ctx)
    except DeviceUnavailableError:
        logging.warning("设备不可用，部分功能受限")


def _quick_resolve_app(text: str) -> tuple[str, str]:
    return _resolve_app_from_yaml(text)


if __name__ == "__main__":
    main()

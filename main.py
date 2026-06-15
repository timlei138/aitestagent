from __future__ import annotations

import argparse
import json
import logging

from config import TestConfig
from data import create_vector_store, create_relational_db
from agents.graph import set_relational_db
from agents.orchestrator import TestOrchestrator
from tools.context import ToolContext
from tools import set_tool_context
from device.controller import DeviceController, DeviceUnavailableError
from device.perceiver import PerceptionMode, SmartPerceiver
from llm.clients import create_vlm_client
from data.knowledge import KnowledgeBase


def main():
    parser = argparse.ArgumentParser(description="AI 自动化测试 Agent")
    sub = parser.add_subparsers(dest="mode")

    run_parser = sub.add_parser("run", help="自然语言执行测试")
    run_parser.add_argument("message", nargs="+", help="测试需求描述")
    run_parser.add_argument("--config", default="config.yaml")

    server_parser = sub.add_parser("server", help="启动 Web 服务")
    server_parser.add_argument("--config", default="config.yaml")
    server_parser.add_argument("--host", default="0.0.0.0")
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
        vlm = create_vlm_client(
            provider=config.vision_provider, model=config.vision_model,
            api_key=config.vision_api_key, base_url=config.vision_base_url,
        )
        perceiver = SmartPerceiver(device, llm_client=vlm, mode=PerceptionMode.HYBRID)
        kb = None
        if config.enable_rag or True:  # MemoryBackend 始终可用
            kb = KnowledgeBase(create_vector_store(config))
        ctx = ToolContext(device=device, perceiver=perceiver, knowledge_base=kb, safety_level=config.safety_level)
        set_tool_context(ctx)
    except DeviceUnavailableError:
        logging.warning("设备不可用，部分功能受限")


_APP_MAP = {
    "settings": ("com.android.settings", "Settings"),
    "设置": ("com.android.settings", "Settings"),
    "服务与反馈": ("com.tblenovo.center", "服务与反馈"),
}


def _quick_resolve_app(text: str) -> tuple[str, str]:
    import re
    lowered = text.lower()
    for name, (pkg, label) in _APP_MAP.items():
        if name.lower() in lowered:
            return pkg, label
    match = re.search(r"\b[a-zA-Z][\w]*(?:\.[\w]+){2,}\b", text)
    if match:
        return match.group(0), ""
    return "", ""


if __name__ == "__main__":
    main()

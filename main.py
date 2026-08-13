from __future__ import annotations

import argparse
import json
import logging
import sys
import traceback

from config import TestConfig, resolve_perception_mode, resolve_vision_credentials
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

    # 无参数时默认启动 server（兼容 PyInstaller 打包后双击运行）
    if not args.mode:
        args.mode = "server"
        args.host = "127.0.0.1"
        args.port = 8080

    if args.mode == "server":
        import uvicorn
        from api.server import app

        host = args.host
        port = args.port
        url = f"http://{'127.0.0.1' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}"

        print(f"服务启动中，请在浏览器打开: {url}")
        uvicorn.run(app, host=host, port=port, reload=False)
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

        # 视觉优先用独立配置；vision_base_url 与主模型不一致时视为独立端点，
        # 禁止回退主模型凭证，避免把 key 发错端点。
        v_model, v_api_key, v_base_url = resolve_vision_credentials(
            llm_model=config.model,
            llm_api_key=config.api_key,
            llm_base_url=config.base_url,
            vision_model=config.vision_model,
            vision_api_key=config.vision_api_key,
            vision_base_url=config.vision_base_url,
        )

        logging.getLogger(__name__).info(
            "[vision-build] model=%s base_url=%s enabled=%s",
            v_model, v_base_url, config.vision_enabled,
        )

        def _vision_call(
            prompt: str, image_base64: str, purpose: str, strict_json: bool
        ):
            return multimodal_vision_call(
                prompt=prompt,
                image_base64=image_base64,
                purpose=purpose,
                strict_json=strict_json,
                model=v_model,
                api_key=v_api_key,
                base_url=v_base_url,
                vision_enabled=config.vision_enabled,
                timeout_sec=30,
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
            llm_model=config.model,
            llm_api_key=config.api_key,
            llm_base_url=config.base_url,
            llm_vision_enabled=config.vision_enabled,
            vision_model=config.vision_model,
            vision_api_key=config.vision_api_key,
            vision_base_url=config.vision_base_url,
            vision_timeout=config.vision_timeout,
            click_mode=config.click_mode,
        )
        ctx_holder["ctx"] = ctx
        set_tool_context(ctx)
    except DeviceUnavailableError:
        logging.warning("设备不可用，部分功能受限")


def _quick_resolve_app(text: str) -> tuple[str, str]:
    return _resolve_app_from_yaml(text)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        if getattr(sys, "frozen", False):
            import app_paths

            app_paths.ensure_dirs()
            crash_log = app_paths.LOG_DIR / "startup_crash.log"
            crash_log.write_text(traceback.format_exc(), encoding="utf-8")
            print(f"\n启动失败日志已写入：{crash_log}")
            try:
                input("按 Enter 退出...")
            except EOFError:
                pass
        raise

from __future__ import annotations

import argparse
import json

from config import TestConfig
from core.chat_runner import ChatRunner


def main():
    parser = argparse.ArgumentParser(description="AI 自动化测试 Agent")
    sub = parser.add_subparsers(dest="mode")

    scan = sub.add_parser("traverse", help="语义路径扫描")
    scan.add_argument("--package", required=True)
    scan.add_argument("--name", default="")
    scan.add_argument("--depth", type=int, default=None)
    scan.add_argument("--pages", type=int, default=None)
    scan.add_argument("--config", default="config.yaml")

    run = sub.add_parser("chat", help="自然语言执行")
    run.add_argument("message")
    run.add_argument("--config", default="config.yaml")

    case = sub.add_parser("run_case", help="执行 YAML 用例")
    case.add_argument("--case", required=True)
    case.add_argument("--config", default="config.yaml")

    replay = sub.add_parser("replay", help="回放 YAML 用例")
    replay.add_argument("--case", required=True)
    replay.add_argument("--config", default="config.yaml")

    server = sub.add_parser("server", help="启动 Web 服务")
    server.add_argument("--config", default="config.yaml")
    server.add_argument("--host", default="0.0.0.0")
    server.add_argument("--port", type=int, default=8080)

    args = parser.parse_args()
    if args.mode == "server":
        import uvicorn

        uvicorn.run("api.server:app", host=args.host, port=args.port, reload=False)
        return

    config = TestConfig.from_yaml(getattr(args, "config", "config.yaml"))
    runner = ChatRunner(config)
    if args.mode == "traverse":
        intent = {
            "intent": "traverse",
            "app_package": args.package,
            "app_name": args.name,
            "task_description": f"语义路径扫描 {args.name or args.package}",
            "traversal_max_depth": args.depth or config.traversal_max_depth,
            "traversal_max_pages": args.pages or config.traversal_max_pages,
            "safety_level": config.safety_level,
        }
        result = runner.run_with_intent(intent)
    elif args.mode in {"run_case", "replay"}:
        result = runner.run_with_intent({"intent": "run_case", "case_file": args.case})
    elif args.mode == "chat":
        result = runner.run(args.message)
    else:
        parser.print_help()
        return
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()


import os, sys, json, subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import main
from data import create_relational_db
from agents.graph import set_relational_db


REQUEST = """首次打开联想日历APP。不授予日历未授予相机/图库权限。
1. 日历点击页面右上角更多按钮，选择课程表，点击 "拍照导入课程表" 按钮
2. 返回课程表设置页，点击 "从图库导入课程表" 按钮
验证
1.弹出权限弹窗，选择允许后打开相机，选择拒绝后提示需要授予相机权限
2.弹出权限弹窗，可选择部分照片或全部允许，选择后可选择照片，选择拒绝后提示需要授予相册权限
（权限弹窗follow系统权限弹窗样式）"""

# 预置 goal_description 让 route_start 直达 agent，跳过 plan_review 的 interrupt（CLI 同步模式无审批）
GOAL_DESC = {
    "goal": "首次打开联想日历APP,不预授权相机/图库权限。验证:1)拍照导入课程表:允许后打开相机,拒绝后提示需要授予相机权限;2)从图库导入课程表:允许(全部允许)后可选择照片,拒绝后提示需要授予相册权限。权限弹窗 follow 系统样式。",
    "app_package": "com.zui.calendar",
    "app_name": "联想日历",
    "target_pages": ["日历主页", "课程表设置页", "拍照导入页", "从图库导入页"],
    "verification": [
        "拍照导入:拒绝权限后提示需要授予相机权限",
        "拍照导入:允许权限后打开相机",
        "从图库导入:拒绝权限后提示需要授予相册权限",
        "从图库导入:允许(全部允许)后可选择照片",
    ],
    "hints": [
        "先用 clear_app_data('com.zui.calendar') 重置 App 到首次打开状态",
        "进入 App 后第一步 set_permission_intent(action='deny') 建立 deny 基线",
        "按 agent 契约:先拒绝后授予,复用 deny 基线不 revoke,禁止走 adb 授权",
    ],
}


def _revoke_all(pkg: str) -> None:
    perms = [
        "android.permission.CAMERA",
        "android.permission.READ_EXTERNAL_STORAGE",
        "android.permission.WRITE_EXTERNAL_STORAGE",
        "android.permission.READ_MEDIA_IMAGES",
        "android.permission.READ_MEDIA_VIDEO",
        "android.permission.READ_MEDIA_AUDIO",
        "android.permission.POST_NOTIFICATIONS",
    ]
    for p in perms:
        try:
            subprocess.run(
                ["adb", "shell", "pm", "revoke", pkg, p],
                capture_output=True, text=True, timeout=15,
            )
        except Exception:
            pass


def main_run() -> None:
    config = main.TestConfig.from_yaml(os.path.join(ROOT, "config.yaml"))
    main._init_tool_context(config)
    set_relational_db(create_relational_db(config))
    orch = main.TestOrchestrator(config)

    pkg, name = main._quick_resolve_app(REQUEST)
    print(f"[regression] resolved app: pkg={pkg} name={name}", flush=True)
    _revoke_all(pkg)
    print("[regression] permissions revoked (best-effort) before run", flush=True)

    result = orch.start(
        user_request=REQUEST, app_package=pkg, app_name=name,
        goal_description=GOAL_DESC,
    )
    print("===RESULT===")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main_run()

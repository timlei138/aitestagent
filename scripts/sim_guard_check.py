"""离线模拟：验证 launch_app 冗余守卫逻辑本身是否正确。

不依赖真机/LLM：用 FakeDevice（始终前台 com.zui.calendar）+ FakeChat（病态反复
launch_app，收到 COOLDOWN_SKIP 后改调 report_done），真调 agents.llm_runtime._run_agent，
对比「守卫启用(LIMIT=3)」与「守卫禁用(LIMIT=1e9)」两种结果。

若启用时 launch 真实执行仅 ~3 次、出现 COOLDOWN_SKIP、且最终 report_done(abort)，
而禁用时 launch 耗满 max_turns、无 SKIP、无 report_done —— 则证明守卫逻辑正确，
真实 log 里 0 SKIP = 运行时未加载新代码（常驻进程需重启）。
"""
from __future__ import annotations

import os
import sys
import types
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_core.messages import SystemMessage, AIMessage

# 运行环境可能在函数内才 import langchain_openai；当前解释器未安装该包时，
# 往 sys.modules 注入一个假的 langchain_openai，使 _run_agent 内部的
# `from langchain_openai import ChatOpenAI` 拿到 FakeChat，无需真实依赖。
import agents.llm_runtime as RT  # noqa: E402  (必须在注入后/或此处不触发 import)

# 延迟注入：先定义 FakeChat，再塞入 sys.modules。
class _FakeLangchainOpenAI:  # placeholder，真正类在下方定义
    pass


def _install_fake_langchain_openai(fake_chat_cls):
    mod = types.ModuleType("langchain_openai")
    mod.ChatOpenAI = fake_chat_cls
    sys.modules["langchain_openai"] = mod


import tools  # noqa: E402
import tools.device_ops as device_ops  # noqa: E402
from tools import AGENT_TOOLS, set_tool_context, ToolContext  # noqa: E402

# 防御性 no-op：屏蔽 token 统计 / settle 等待 / 页面迁移写库，聚焦守卫逻辑。
RT._accumulate_token_usage = lambda *a, **k: None
device_ops._settle_after_action = lambda *a, **k: None
tools._record_page_transition = lambda *a, **k: None


class FakePerceiver:
    """activity 每次变化 —— 复现真实环境 page_signature 每轮不同，
    使 LOOP_DETECTED 不触发（它靠 page_signature 重复判定）。"""

    def __init__(self):
        self._n = 0

    def perceive(self):
        self._n += 1
        return SimpleNamespace(
            activity=f"Act_{self._n}", page_title="", elements=[],
        )


class FakeDevice:
    def __init__(self):
        self.launch_count = 0  # 真实 launch_app 执行次数（=app_start 调用次数）
        self._at_target = False  # consume 模型：launch 后前台仅生效一次
        self._n = 0

    def current_app(self, refresh=False):
        self._n += 1
        if self._at_target:
            # 仅 app_start 之后第一次查询视为"已到达目标前台"，
            # 之后立即视为被切走（复现病态：两次 launch 之间 App 不在前台）。
            self._at_target = False
            return {"package": "com.zui.calendar",
                    "activity": f"com.zui.calendar.AllInOneActivity_{self._n}"}
        # launch 前 / 被切走后：前台是桌面，不是目标包
        return {"package": "com.android.launcher",
                "activity": f"Launcher_{self._n}"}

    def app_start(self, pkg, activity=""):
        self.launch_count += 1
        self._at_target = True
        return True

    def screenshot(self):
        class _Img:
            def save(self, p):
                pass
        return _Img()


class FakeChat:
    skip_seen = 0
    reported = False

    def __init__(self, *a, **k):
        pass

    def bind_tools(self, tools):
        self._tools = tools
        return self

    def invoke(self, messages):
        cnt = sum(
            1 for m in messages
            if hasattr(m, "content")
            and "COOLDOWN_SKIP: launch_app" in str(m.content)
        )
        FakeChat.skip_seen = cnt
        if cnt > 0:
            FakeChat.reported = True
            return AIMessage(
                content="",
                tool_calls=[{"name": "report_done",
                             "args": {"status": "abort", "summary": "guard feedback"},
                             "id": "rd1"}],
            )
        return AIMessage(
            content="",
            tool_calls=[{"name": "launch_app",
                         "args": {"package": "com.zui.calendar"},
                         "id": "la1"}],
        )


def run_once(disable_guard: bool):
    RT._LAUNCH_REDUNDANT_LIMIT = 10 ** 9 if disable_guard else 3
    FakeChat.skip_seen = 0
    FakeChat.reported = False
    dev = FakeDevice()
    set_tool_context(ToolContext(device=dev, perceiver=FakePerceiver()))

    text, _log, metrics = RT._run_agent(
        messages=[SystemMessage(content="sim")],
        tools=AGENT_TOOLS,
        provider="openai", model="x", api_key="x", base_url="x",
        max_turns=30, run_id="sim",
    )
    return {
        "launch_real": dev.launch_count,
        "skip_seen": FakeChat.skip_seen,
        "reported": FakeChat.reported,
        "break_action": metrics.get("loop_break_action", ""),
        "exhausted": "MAX_TURNS_EXHAUSTED" in text,
        "text_head": text[:60],
    }


if __name__ == "__main__":
    _install_fake_langchain_openai(FakeChat)
    print("=== 守卫启用 (LIMIT=3) ===")
    on = run_once(disable_guard=False)
    for k, v in on.items():
        print(f"  {k}: {v}")
    print("\n=== 守卫禁用 (LIMIT=1e9，模拟改动前) ===")
    off = run_once(disable_guard=True)
    for k, v in off.items():
        print(f"  {k}: {v}")
    print("\n=== 结论 ===")
    if on["launch_real"] <= 3 and on["skip_seen"] > 0 and on["reported"]:
        print("  [OK] 守卫逻辑正确：冗余 launch 被拦截并逼出 report_done")
        if off["launch_real"] >= 20 and not off["reported"]:
            print("  [OK] 禁用时退化成耗满预算 — 与「最新 log 64次launch+0 SKIP」一致")
            print("  [NOTE] 真实 log 未生效 = 守卫未抓住病态，见下方诊断")
    else:
        print("  [FAIL] 守卫逻辑未按预期生效，需排查")

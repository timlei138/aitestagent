"""F5 录制回放回归集 — 共享测试基建（agent_evolution_plan §7）。

定位（P4 边界诚实）：mock 回放保的是「决策/判定逻辑回归」（verdict / clause
状态 / mode 迁移 / LLM 调用数），不保设备时序与感知竞态。

驱动方式：整图 orchestrator.start，其中——
- planner：R1 复用命中注入历史契约（planner LLM=0，走真实生产路径）；
- plan_review：auto_approve=True 自动过审；
- mode_selection：monkeypatch rag_context.retrieve_knowledge 种入沉淀 plan；
- 设备/感知：ScriptedDevice + ScriptedPerceiver 停在目标页快照；
- agent 循环 LLM：仅歧义场景需要，patch langchain_openai.ChatOpenAI 为脚本模型。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace

from langchain_core.messages import AIMessage

import agents.rag_context as rag_context
from agents import graph as graph_module
from agents.orchestrator import TestOrchestrator
from agents.verification import build_verification_contract
from config import TestConfig
from tools import set_tool_context
import tools.context as _tools_context
from tools.context import ToolContext

APP_PACKAGE = "com.example.app"
TARGET_ACTIVITY = "com.example.app.TimetableActivity"
SCREEN_PROFILE = "1080x2400"


# ── 感知侧 fakes ──


def make_element(
    rid: str = "",
    label: str = "",
    text: str = "",
    content_desc: str = "",
    enabled: bool | None = None,
    checked: bool | None = None,
    clickable: bool = False,
):
    """与 SmartPerceiver Understanding.elements 同字段的轻量元素。"""
    return SimpleNamespace(
        resource_id=rid,
        label=label,
        text=text,
        content_desc=content_desc,
        enabled=enabled,
        checked=checked,
        clickable=clickable,
        region="main",
    )


def make_snapshot(
    activity: str,
    elements: list,
    page_title: str = "课程表",
    layout: str = "single",
):
    return SimpleNamespace(
        activity=activity,
        page_title=page_title,
        layout=layout,
        elements=elements,
        regions=[],
    )


class ScriptedPerceiver:
    """按序列返回快照；耗尽后停在最后一帧（页面稳定语义）。"""

    def __init__(self, snapshots: list):
        self._snapshots = list(snapshots)
        self._index = 0

    def perceive(self):
        if self._index < len(self._snapshots):
            snapshot = self._snapshots[self._index]
            self._index += 1
            return snapshot
        return self._snapshots[-1] if self._snapshots else make_snapshot("", [])


class ScriptedDevice:
    """最小设备面：前台固定在目标页（与沉淀侧环境键同页，保证 R1/R6 兼容）。
    未覆盖的方法抛 AttributeError 让缺口显式暴露。"""

    def __init__(self, apps: list[dict]):
        self._apps = list(apps)
        self.started: list[tuple] = []

    def current_app(self, refresh: bool = False) -> dict:
        app = self._apps[0]
        return dict(app)

    def app_start(self, package: str, activity: str = "") -> None:
        self.started.append((package, activity))

    def app_stop(self, package: str) -> None:
        pass


class ScriptedChatModel:
    """替身 agent LLM：bind_tools 后按序弹出预设 AIMessage；耗尽后兜底 DONE。"""

    def __init__(self, responses: list[AIMessage]):
        self._responses = list(responses)

    def bind_tools(self, tools, **kwargs):
        return self

    def invoke(self, messages, *args, **kwargs):
        if self._responses:
            return self._responses.pop(0)
        return AIMessage(content="DONE: 脚本响应已耗尽，需人工复核")


# ── 记忆复用种子（R1 检索前移的数据源）──


class FakeKB:
    """知识库替身：task_plan 向量召回 + 语义知识空集（_rag_ctx 需要）。"""

    def __init__(self, plan_ids: list[str]):
        self._plan_ids = plan_ids

    def query_task_plan_summaries(self, app_package, query="", top_k=5, **kwargs):
        return [{"metadata": {"plan_id": pid}} for pid in self._plan_ids]

    def query_semantic_knowledge(self, app_package, top_k=20, **kwargs):
        return {}


class FakePlanDB:
    """沉淀库替身：读走真数据、写全部 no-op（reporter 沉淀路径的持久化消音）。"""

    def __init__(self, plans: dict):
        self._plans = plans

    def get_full_execution_plan(self, plan_id: str):
        return self._plans.get(plan_id)

    def insert(self, *args, **kwargs):
        return None

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def _noop(*args, **kwargs):
            return None

        return _noop


def build_approved_contract(goal: dict, user_request: str) -> dict:
    """用真实生产代码生成契约（span/channels/coverage_map 全部同源），置为已审批。"""
    contract = build_verification_contract(goal, user_request)
    contract["status"] = "approved"
    return contract


def deposited_plan(
    plan_id: str,
    user_request: str,
    contract: dict,
    actions: list[dict],
    environment_key: str,
) -> dict:
    """构造 R1 可命中的沉淀 plan（与 save_task_plan_summary 落库字段同构）。"""
    return {
        "plan_id": plan_id,
        "contract_status": "approved",
        "verification_contract_json": json.dumps(contract, ensure_ascii=False),
        "environment_key": environment_key,
        "task_signature": {
            "user_request": user_request,
            "parameter_slots": [],
            "action_semantics": [],
        },
        "actions": actions,
    }


def navigation_action(
    index: int, package: str = APP_PACKAGE, activity: str = ""
) -> dict:
    payload = {"package": package}
    if activity:
        payload["activity"] = activity
    return {
        "tool_name": "launch_app",
        "action_index": index,
        "execution_eligibility": "direct_eligible",
        "tool_input_json": json.dumps(payload),
        "precondition_json": "{}",
        "postcondition_json": "{}",
        "locator_json": "{}",
    }


# ── 场景定义与执行 ──


@dataclass
class ReplayScenario:
    case_id: str
    category: str  # 高频 / 歧义 / 恢复
    user_request: str
    goal: dict
    snapshot_elements: list
    agent_responses: list = field(default_factory=list)
    plan_environment_key: str = ""


def run_replay_scenario(monkeypatch, scenario: ReplayScenario) -> dict:
    """整图回放一个场景，返回 {result, state, ctx}。"""
    from agents.plan_extractor import environment_fingerprint

    # 沉淀侧环境键用生产 environment_fingerprint 构造（与运行侧同构）：
    # 前台=目标 App 目标页 + 无版本差异 → R1 复用 env_compatible=True。
    stored_env_key = scenario.plan_environment_key or environment_fingerprint(
        {
            "package": APP_PACKAGE,
            "activity": TARGET_ACTIVITY,
            "app_version": "",
            "fixture_fingerprint": "",
        },
        SCREEN_PROFILE,
    )
    contract = build_approved_contract(scenario.goal, scenario.user_request)
    plan = deposited_plan(
        f"plan-{scenario.case_id}",
        scenario.user_request,
        contract,
        [navigation_action(0, activity=".TimetableActivity")],
        environment_key=stored_env_key,
    )
    kb = FakeKB([plan["plan_id"]])
    db = FakePlanDB({plan["plan_id"]: plan})
    device = ScriptedDevice([{"package": APP_PACKAGE, "activity": TARGET_ACTIVITY}])
    perceiver = ScriptedPerceiver(
        [make_snapshot(TARGET_ACTIVITY, scenario.snapshot_elements)]
    )
    ctx = ToolContext(
        device=device,
        perceiver=perceiver,
        knowledge_base=kb,
        safety_level="strict",
        _screen_size=(1080, 2400),
    )
    # 进程级 ToolContext 用后还原：set_tool_context 是全局副作用，
    # 不还原会泄漏给同进程后续测试（实测污染 test_mode_selection）。
    prev_ctx = _tools_context._CONTEXT
    set_tool_context(ctx)

    def _fake_retrieve(**kwargs):
        return {
            "purpose": "task_plan",
            "source": "vector_then_relational",
            "items": [plan],
            "environment_compatibility_score": 1.0,
            "environment_compatibility_reasons": [],
        }

    monkeypatch.setattr(rag_context, "retrieve_knowledge", _fake_retrieve)
    monkeypatch.setattr(graph_module, "_relational_db", db)
    if scenario.agent_responses:

        def _scripted_chat(*args, **kwargs):
            return ScriptedChatModel(list(scenario.agent_responses))

        import langchain_openai

        monkeypatch.setattr(langchain_openai, "ChatOpenAI", _scripted_chat)

    orchestrator = TestOrchestrator(TestConfig())
    tid = f"replay-{scenario.case_id}"
    try:
        result = orchestrator.start(
            user_request=scenario.user_request,
            app_package=APP_PACKAGE,
            app_name="Example",
            thread_id=tid,
            auto_approve=True,
            replay=True,
        )
    finally:
        _tools_context._CONTEXT = prev_ctx
    # mode 迁移等中间决策字段不在 _build_result 里，从 state cache 取全量终态
    return {
        "result": result,
        "state": orchestrator._state_cache.get(tid, {}),
        "ctx": ctx,
    }

"""M4（Plan §4 Phase 1）最小步回归：状态类 claim 的工具选择引导 + 冷启动硬约束。

M4 的完整结构化 spec 落地与否取决于"agent 在真实用例下是否稳定选 disabled"的量化
结果（见 Plan §4 + Review 共识）。此处先锁住低成本高价值的 prompt 引导：
agent_common.txt 必须引导 agent 对置灰/禁用/可点/开关态等状态类 claim 使用
`assert_behavior_effect` 的 disabled/enabled/toggled 谓词，而非 click_and_check/visual_check 猜。

另：每次运行绝对第一步必须是冷启动 App（force_fresh=True），消除残留页面栈脏状态。
该约束以 prompt 软约束形式落地（orchestrator 入口不强制，避免弹窗副作用），故需锁住
文案不被误删/弱化。

该测试只验证引导文案存在（防回归/误删），不依赖 LLM 端到端行为。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_agent_system() -> str:
    from agents.nodes import AGENT_SYSTEM

    return AGENT_SYSTEM


def _load_planner_system() -> str:
    from agents.nodes import PLANNER_SYSTEM

    return PLANNER_SYSTEM


def test_agent_common_guides_state_claims_to_behavior_effect_predicates():
    """状态类 claim 引导存在且明确指向 assert_behavior_effect 的专用谓词。"""
    system = _load_agent_system()
    # 引导段标题存在
    assert "状态类 claim 必须用专用谓词验证" in system
    # 明确列出专用谓词
    assert "disabled(label)" in system
    assert "toggled(" in system
    # 不存在 enabled(label) 谓词（F3：删掉 prompt 里广告但未实现的谓词）
    assert "enabled(label)" not in system
    # 明确禁止用 click_and_check / visual_check 猜状态（这是 M1+根因治理核心）
    assert "click_and_check" in system
    assert "不要用" in system
    # 判定口诀把三类场景区分开（状态/字面/视觉）
    assert "assert_behavior_effect" in system
    assert "assert_page_contains" in system
    assert "visual_check" in system


def test_agent_system_requires_cold_start_as_first_step():
    """agent 运行时系统 prompt 必须强制「每次运行绝对第一步冷启动 App」。

    这是针对 162023 run 漏冷启动（agent 第 0 步直接 click、零 launch_app 调用）
    的根因治理：以 prompt 硬约束要求先 force_fresh 再操作，避免脏页面栈导致漏验。
    """
    system = _load_agent_system()
    # 冷启动作为绝对第一步、不可省略/跳过的硬约束措辞存在
    assert "绝对第一步" in system
    assert "force_fresh=True" in system
    assert "launch_app" in system
    # 明确禁止因"页面看起来对"而跳过冷启动（残留下次运行页面栈）
    assert "跳过冷启动" in system


def test_planner_requires_cold_start_as_first_step():
    """planner 全局初始化同样必须要求冷启动为绝对第一步。"""
    planner = _load_planner_system()
    assert "绝对第一步" in planner
    assert "force_fresh=True" in planner
    assert "launch_app" in planner
    assert "跳过冷启动" in planner


def test_agent_system_schedules_verification_by_page_not_order():
    """agent 应按当前页面就近批量验证，而非机械按列表顺序回溯（减少回退/LLM 调用）。"""
    system = _load_agent_system()
    # 验证调度段明确否定"按列表顺序"
    assert "验证调度" in system
    assert "不按列表顺序" in system
    # 就近批量：到达某页先清空该页可验证 clause
    assert "就地批量" in system or "一次性验完" in system
    # 页面位置驱动，而非列表序号
    assert "页面位置" in system or "以「页面位置」驱动" in system
    # 澄清 hints 顺序只约束导航前置，不约束验证项顺序（避免与上文混淆）
    assert "导航前置依赖" in system

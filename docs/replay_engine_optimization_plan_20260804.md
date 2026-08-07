# 回放引擎优化方案：脚本优先 + AI 兜底

> 日期：2026-08-04
> 作者：Agent + 高级测试开发工程师

## 0. 指导原则（对齐综合评审）

| 类型 | 回放场景中的归属 |
|---|---|
| **按脚本执行点击、校验页面状态** | 代码（确定性事实/契约）→ 不调 LLM |
| **页面偏离脚本、需要恢复** | LLM（理解、决策、判断）→ 接管后恢复 |

> 一句话：**回放时代码负责"稳"（按脚本走、核实页面状态），LLM 负责"活"（偏离时恢复）。**

## 1. 问题陈述

### 1.1 现象

首次 run 通过 → 复跑 → 回放时出现大量"点错"，成功率和 token 消耗未如预期下降。

### 1.2 数据对比（联想日历课程表场景）

| 维度 | 首次 normal run（105801，8/3） | rerun 之一（144130，8/4） | rerun 之二（165126） |
|---|---|---|---|
| run_type | normal | rerun | rerun |
| source_run_id | — | 105801 | 105801 |
| source_case_id | — | NULL | NULL |
| test_verdict | passed | passed | passed |
| SoM 格子定位 | 0 次（像素盲猜） | 2 次（cell G9/F8） | — |
| 累计 input_tokens | — | ~5.9M（91 次 LLM 调用累计） | — |
| REPLAY_EVIDENCE_BLOCK 注入 | — | 否（0 次） | 否（0 次） |

**关键发现：144130 虽然 passed，但 SoM 格子定位生效不是因为回放证据——回放证据（REPLAY_EVIDENCE_BLOCK）在这些 rerun 中从未被注入（计数 = 0）。通过是因为 Agent 恰好走对了路径。**

**rerun verdict 分布**：144130/165126/170827/180433 为 passed，134652 为 inconclusive（被取消）。

## 2. 现状分析

### 2.0 ★ 真正的根因：证据链断裂 — execution_plan 根本没被加载

**这是导致当前回放失败的最直接原因，也是文档最容易遗漏的问题。**

当前回放有两条入口路径，但只有其中一条会注入 evidence：

| 入口 | 调用链 | evidence 注入？ | 当前课程表 rerun 走的哪条？ |
|---|---|---|---|
| **报告 rerun** | `api/server.py:734` → `resolve_report_rerun_entry(run)` → `json.loads(run.goal_json)` | **否** — 只读取 goal_json，不调 `_extract_replay_evidence` | ← **这条** |
| **用例 run** | `api/test_cases_routes.py:515` → `create_test_case(run_id=...)` → `_extract_replay_evidence(run)` → `goal["execution_plan"] = evidence` | **是** | 课程表用例根本没建 case |

**DB 实测证据：**
- DB 中 `test_cases` 只有 1 条用例（b7254c72816f，"首次打开联想APP…不授予权限"）
- 所有课程表 rerun（165126/170827/180433/134652/144130）的 `source_case_id` 全为 NULL
- 144130 trace 中 key_actions / preferred_locator / effective / REPLAY 相关计数 = 0
- 证明 `REPLAY_EVIDENCE_BLOCK` 在这些 rerun 里从未被注入

**→ 结论：这批 rerun 点错、token 不降，不是因为"证据只注入第 1 步"或"约束力不够"，而是因为 execution_plan 压根没被录制到 goal_json 里。回放端拿到的证据恒为空。**

```
报告 rerun 的数据流（当前）：
  api/server.py → resolve_report_rerun_entry(run)
    ↓ json.loads(run.goal_json)  ← goal_json 里没有 execution_plan
    ↓ orchestrator.start(goal_description=goal, run_type="rerun")
    ↓ agent_node → _render_replay_evidence_block(goal_desc)
    ↓ goal_desc.execution_plan 为空 → 返回 "" → 不注入任何回放证据
    ↓ Agent 完全自由探索（与首次 run 无异）
```

### 2.1 当前回放数据流（两条路径）

**路径 A：报告 rerun（当前课程表走的路径，evidence 为空）**
```
api/server.py rerun → resolve_report_rerun_entry
  ↓ 只读取 goal_json（无 execution_plan）
  ↓ Agent 拿不到任何回放证据 → 完全自由探索
```

**路径 B：用例 run（设计意图路径，evidence 有值）**
```
首次 run 通过 → create_test_case(run_id=...) → _extract_replay_evidence(run)
  ↓ evidence 写入 goal_json.execution_plan
  ↓ run_test_case → _resolve_run_entry(case) → goal 含 execution_plan
  ↓ agent_node → _render_replay_evidence_block → 注入 REPLAY_EVIDENCE_BLOCK
```

**当前产品流程缺陷**：报告 rerun 不强制要求先建用例，用户直接从报告页面点"复跑"就走路径 A，evidence 丢失。

### 2.2 当前录制内容（_extract_replay_evidence）

每个 key_action 记录（**仅用例路径 B 会执行此录制**）：
```json
{
  "step": "click_课程表",
  "tool": "click",
  "precondition": {"expected_activity": "AllInOneActivity"},
  "preferred_locator": {"label": "课程表", "rid": "..."},
  "observed_index": 0,
  "resolved_target": {"label": "课程表", "role": "list_entry", "rid": "..."},
  "postcondition": {"expected_activity": "TimetableActivity"},
  "last_observation": "...",
  "last_result": "OK"
}
```

**录制层的两个致命问题：**

**问题 A：非 click 工具整个被丢弃（P3a 的真正根因）**

`_extract_replay_evidence` 主循环（`test_cases_routes.py:322-351`）只处理 5 种 action_type：`click`、`assert_verification`、`assert_page_contains`、`assert_element_exists`、`report_done`。

`type_input`、`vision_tap`、`set_permission_intent`、`click_and_check` 的步骤**连 key_action 都不生成**（被整个丢弃）。实测 105801 被丢弃的 OK 步骤：vision_tap=6、set_permission_intent=2、click_and_check=1。

这不是“参数缺失”问题——`orchestrator.py:941` 对所有工具都记了完整 `tool_input`，数据都在 steps_json 里。问题出在提取层直接跳过了这些工具类型。

→ 修复：在提取主循环新增这些工具的分支。同时更新 `business_indexes`（L311），当前只认 `{click, assert_*}`，新增工具分支后需同步更新，否则 entry 提取会错位。

**问题 B：postcondition 无 value 级断言**

postcondition 仅含 expected_activity，不含 value 级断言（如分钟值从 00→50）。对于同 Activity 内操作（vision_tap 滚轮 / type_input 输入），before_act == after_act，activity_match 恒为 true，毫无区分度——这正是回放失败的重灾区。

### 2.3 当前回放注入（_render_replay_evidence_block）

```
## REPLAY_EVIDENCE_BLOCK (historical facts, not a forced script)
Use current perception as the authority...
1. click reference locator={'label': '课程表'}; observed_index=0; precondition_activity='AllInOneActivity'.
2. click reference locator={'label': '手动创建课程表'}; observed_index=5; precondition_activity='TimetableActivity'.
3. type_input reference; precondition_activity='EditTimetableActivity'.   ← 非 click 只有一行占位
4. vision_tap reference; precondition_activity='TimeSlotSettingsActivity'. ← 同样只有占位
...
```

**6 个结构性问题：**

| # | 问题 | 层级 | 影响 |
|---|---|---|---|
| P0 | **证据链断裂** — 报告 rerun 路径不调 `_extract_replay_evidence`，goal_json 无 execution_plan，evidence 恒为空 | **数据流** | 回放与首次 run 无异，所有回放优化空转 |
| P1 | "historical facts, not a forced script" — Agent 可以无视证据 | prompt | 回放无约束力 |
| P2 | 仅第 1 步注入（`if not msgs`） | 注入时机 | 第 2 步起 Agent 完全自由探索 |
| P3a | **提取层整个丢弃非 click 工具**（type_input/vision_tap/set_permission_intent/click_and_check 连 key_action 都不生成） | 提取（_extract_replay_evidence） | 课程时长用例的核心操作（6 次 vision_tap 滚轮）根本不在脚本里 |
| P3b | **渲染层**对非 click 工具只输出一行占位文字（`{tool} reference`），不渲染任何参数 | 渲染（_render） | 即使录制了参数，回放端也看不到 |
| P4 | 无步骤推进机制 | 状态机 | 不知道"下一步该执行脚本的第几步" |
| P5 | 无偏离-恢复判定逻辑 | 状态机 | 偏离脚本时全靠 LLM 自由发挥 |

## 3. 方案设计

### 3.0 ★ Task 0：打通证据链（P0 前置，最高优先级）

**目标**：确保回放时 goal_json 中一定包含 execution_plan（含 v4 evidence）。不解决 Task 0，后面所有 Task 都是空转。

**方案 A（推荐）：报告 rerun 路径也注入 evidence**

改动 `resolve_report_rerun_entry`，在读取 goal_json 后检查是否已有 execution_plan，如果没有则现场调用 `_extract_replay_evidence`：

```python
def resolve_report_rerun_entry(run: dict[str, Any]) -> dict[str, Any]:
    try:
        goal = json.loads(run.get("goal_json") or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("report plan data is damaged") from exc
    # ★ 补注 evidence（如果 goal 中尚无 execution_plan）
    if not isinstance(goal.get("execution_plan"), dict):
        evidence = _extract_replay_evidence(run)
        if evidence:
            evidence["effective"] = derive_effective_plan(
                evidence["base_evidence"], [], effective_revision=1
            )
            goal["execution_plan"] = evidence
    revision = _resolve_execution_plan_revision(goal)
    return {"goal": goal, "source_run_id": run["id"], "source_case_id": None,
            "execution_plan_revision": revision}
```

**方案 B（产品流程约束）：强制先建用例再复跑**

在前端报告详情页禁用直接的"复跑"按钮，引导用户走"创建用例 → 用例运行"路径。但这样增加用户操作成本，不推荐作为唯一方案。

**建议：方案 A + 方案 B 并行**。方案 A 兜底保证报告 rerun 也有 evidence；方案 B 作为推荐流程引导用户使用更规范的用例管理。

**改动文件**：`api/test_cases_routes.py`（`resolve_report_rerun_entry`、`_extract_replay_evidence`）

**向后兼容**：旧报告的 goal_json 无 execution_plan 时现场提取，有则跳过；`_extract_replay_evidence` 仅对 `test_verdict == "passed"` 的 run 返回非 None，未通过的 run 不受影响。

**方案 A evidence 不落库**：方案 A 提取的 evidence 只在 rerun 运行时临时注入 goal，不写入 test_runs 表。因此不可通过用例 patch API 修改——需要修改 evidence 时，应先建用例（走 `create_test_case(run_id=...)` 路径），再用用例 patch API 修改 base_evidence。

**⚠️ 隐蔽 bug（实施 Task 0 时必须修）**：`_extract_replay_evidence` 通过 `run.get("verification_json")` 获取验证数据，但 `get_test_run`（`data/relational.py:359`）返回的 dict 中该字段已被 pop 并替换为 `verification_results`。结果是：即使 `get_test_run` 的 DB 里有完整验证数据，`_extract_replay_evidence` 拿到的 verification 恒为空列表 → `verification_evidence` 全部为 unknown。

→ 修复：`_extract_replay_evidence` 中改为：
```python
# verification_json 已被 get_test_run pop 并替换为 verification_results
reported = run.get("verification_results") or []
```
注意：`verification_results` 中的 screenshot 字段包含本地路径（如 `screenshots/verify_xxx.png`），会随 evidence 写入 goal_json 导致体积膨胀 + 路径跨机失效。回填前应剔除：
```python
for item in reported:
    if isinstance(item, dict):
        item.pop("screenshot", None)
```

**验收标准**：Task 0 实施后：
- 报告 rerun 的 `goal['execution_plan']['effective']['key_actions']` 必须非空
- `verification_evidence` 中每个 v-key 的 `subjective.result` 与源 run 原始结果一致（如 105801 的 v0/v1/v2 全为 passed，而非仅“不为 unknown”）

### 3.1 架构概览

```
回放模式（run_type == "rerun" && 有 key_actions）:
  ┌─────────────────────────────────────────────────┐
  │                  replay_engine                   │
  │                                                  │
  │  每步进入 agent_node 前：                         │
  │    1. 取脚本当前步 current_step                   │
  │    2. 检查 precondition 是否匹配当前页面          │
  │       ├─ 匹配 → 注入 NEXT_ACTION 指令（强制）     │
  │       │         LLM 按指令执行（参数已给定）       │
  │       └─ 不匹配 → 注入 RECOVERY 指令（降级）      │
  │                  LLM 自由恢复到脚本对齐点          │
  │    3. 执行后检查 postcondition                    │
  │       ├─ 匹配 → 推进到下一步                     │
  │       └─ 不匹配 → 标记偏离，下步走 RECOVERY       │
  └─────────────────────────────────────────────────┘
```

### 3.2 核心改动点

#### Task 1：修复提取层 + 增强录制信息（_extract_replay_evidence）

**目标**：让非 click 工具（type_input、vision_tap、set_permission_intent、click_and_check）也生成 key_action（修复 P3a：当前整个丢弃）；同时补充 value 级断言。

**改动文件**：`api/test_cases_routes.py`

**核心修复**：在 `_extract_replay_evidence` 主循环（L322-351）新增 type_input / vision_tap / set_permission_intent / click_and_check 的分支。同时更新 `business_indexes`（L311），将新增工具类型加入集合，否则 entry 提取会错位。

**⚠️ type_input status_code 处理**：实测 type_input 的 status_code 是 `UNSPECIFIED`（不是 OK），Task 1 新增提取分支时不能按 `status_code=="OK"` 过滤，要把 `UNSPECIFIED` 当成功（仅排除 `_BAD_STATUS_CODES`）。更彻底的做法是顺手在 type_input 工具里补上结构化 status_code。

**Task 1 验收标准**：

用 105801 fixture 提取后：
- `key_actions` 包含 6 次 vision_tap + 2 次 type_input + 2 次 set_permission_intent（而非被丢弃）
- 同 Activity 步骤（vision_tap 滚轮 / type_input 输入）带 `postcondition_channel` 标记
- `verification_evidence` 中每个 v-key 的 `subjective.result` 与源 run 原始结果一致（v0/v1/v2 全 passed）

**新增录制字段**（以 click 为例）：

```json
{
  "step": "click_课程表",
  "tool": "click",
  "precondition": {
    "expected_activity": "AllInOneActivity",
    "page_signature": "14:42"
  },
  "preferred_locator": {"label": "课程表", "rid": "com.zui.calendar:id/..."},
  "tool_input": {"label": "课程表"},            // ← 新增：完整工具参数
  "postcondition": {
    "expected_activity": "TimetableActivity",
    "expected_value": null                       // ← 新增：可选 value 断言
  },
  "last_observation": "...",
  "last_result": "OK"
}
```

对于非 click 工具（type_input、vision_tap 等），提取层当前整个丢弃，需新增分支并录制完整 tool_input：
```json
{
  "step": "type_课程名",
  "tool": "type_input",
  "tool_input": {"text": "测试课程表2"},          // ← 录制时已有，提取层需补分支
  "inject_random_suffix": true,                  // ← 回放时自动追加随机后缀（避免重名冲突）
  "precondition": {"expected_activity": "EditTimetableActivity"},
  "postcondition": {
    "expected_activity": "EditTimetableActivity",
    "expected_value": "测试课程表2"              // ← 同 Activity 内操作，必填 value 断言
  }
}
```

```json
{
  "step": "vision_tap_时间滚轮",
  "tool": "vision_tap",
  "tool_input": {"description": "右侧结束时间分钟列中当前选中值50下方的那一行", "repeat": 3},
  "precondition": {"expected_activity": "TimeSlotSettingsActivity"},  // ← 真实宿主 Activity
  "postcondition": {
    "expected_activity": "TimeSlotSettingsActivity",  // ← 弹窗不是独立 Activity，前后相同
    "expected_value": "09:00-09:53"              // ← 同 Activity 内操作，必填 value 断言
  }
}
```

**expected_value 数据来源定义**：

`expected_value` 无法被 `_extract_replay_evidence` 自动推导（steps_json 中没有"操作后应出现什么文本"）。数据来源分两种情况：

**情况 A：后续有断言步骤（可自动回填）**：把紧随其后的 `assert_page_contains`/`assert_element_exists` 的 args 作为前一个业务步骤的 expected_value。例如 105801 中 assert 步骤就在滚轮操作之后，这个启发式可行。

**情况 B：无后续断言（需人工补充或延迟校验）**：走用例 patch API 人工补充，或用以下两种 postcondition 通道之一：

- **通道 1：vision_tap 自带 verify 参数**：对 canvas 自绘控件（如滚轮），把 expected_value 转成 `verify` 描述传给 vision 模型，执行时自动验证
- **通道 2：延迟到后续 assert 步骤**：assert_page_contains/assert_element_exists 本身已是 key_action，天然是确定性闸门

**⚠️ Task 3 必须区分这两种通道**：

```python
if action.get("postcondition_channel") == "vision_verify":
    # canvas 步骤：用 vision_tap 的 verify 参数执行时验证
    instruction_text += f'  verify="{expected_value}"'
elif action.get("postcondition_channel") == "deferred_assert":
    # 延迟到后续 assert 步骤校验，当前步骤只做 activity_match
    value_match = True  # 不在弹窗期间校验
```

**⚠️ 输入文本随机化（按 run 记忆化）**：

对 `type_input` 类步骤，回放时应自动追加随机后缀（如 `测试课程表_r4k2`），避免与已有数据重名导致保存冲突。

**必须按 run 记忆化，不能逐步随机**：实测 105801，同一文本 "测试课程表2" 在 step 13 和 step 18 被输入了两次。若逐步随机会得到不同名字，改变原脚本语义。

```python
# 在 state 中维护 original_text → actual_text 映射
_replay_input_actuals: dict[str, str]  # 如 {"测试课程表2": "测试课程表2_a3f7"}

# 生成指令时查表
def _get_actual_text(original_text: str) -> str:
    if original_text not in state["_replay_input_actuals"]:
        suffix = "_" + secrets.token_hex(2)
        state["_replay_input_actuals"][original_text] = original_text + suffix
    return state["_replay_input_actuals"][original_text]
```

**下游字段改写（deepcopy，不污染证据本体）**：同一原始文本可能在多个字段出现（tool_input.text、locator.label、expected_value）。注入时通过 `_resolve_action_texts`（定义见 Task 2）对 action 做全文替换，返回副本。不在 Task 1 提取层原地突变。

**⚠️ 同 Activity 步骤的 postcondition 规则**：

vision_tap 滚轮 / type_input 输入等操作在弹窗或页面内完成，操作前后 Activity 完全相同（如 TimeSlotSettingsActivity → TimeSlotSettingsActivity）。因此：
- `activity_match` 恒为 true，毫无区分度
- `expected_value` **不能是可选**，对同 Activity 步骤应**必填**
- 也可用 `detect_popup` 结果做 postcondition（如弹窗消失 = 操作成功）

判定规则：若 `precondition.expected_activity == postcondition.expected_activity`，则 `expected_value` 必填。否则脚本会在滚轮步骤上“假命中”推进——页面对了、值没对。

用于 Task 3 步骤推进时的精确校验，避免“页面对了但值没对”的假命中。

#### Task 2：回放引擎 — 渲染层重构 + 逐步脚本引导

**目标**：解决 P2（仅第 1 步注入）、P3b（渲染层对非 click 只输出占位行）。

**改动文件**：`agents/nodes.py`

**P3b 修复 — 渲染层补全非 click 工具参数**：

当前 `_render_replay_evidence_block`（L309-319）对非 click 工具只渲染 `{tool} reference`。需改为渲染完整参数：

```python
# 当前（缺陷）
else:
    lines.append(f"{index}. {action.get('tool', 'action')} reference; precondition_activity={pre_text!r}.")

# 修复后
else:
    tool_name = action.get('tool', 'action')
    tool_input = action.get('tool_input', {})
    lines.append(f"{index}. {tool_name} params={tool_input!r}; precondition_activity={pre_text!r}.")
```

**逐步注入逻辑**：

在 `agent_node` 中，回放模式下每一步（不仅第一步）：
1. 从 `key_actions` 取出当前待执行步骤 `replay_step_idx`
2. 检查 precondition（当前页面 Activity 是否匹配）
3. 根据匹配结果注入不同指令

**新增 state 字段**：
```python
_replay_step_idx: int              # 当前脚本步骤索引（回放模式专用）
_replay_mode: str                  # "script" | "recovery" | ""（空=非回放）
_replay_input_actuals: dict        # type_input 随机后缀记忆化表（original_text → actual_text）
```

**注入 prompt 设计**：

**匹配时（script 模式）**：
```
## REPLAY_SCRIPT_STEP [3/15] — 按脚本执行
当前页面 AllInOneActivity 与脚本 precondition 匹配。
请执行以下操作（不要自由探索）：
- tool: click
- locator: {"label": "课程表", "rid": "com.zui.calendar:id/..."}
- 使用 click(label="课程表", rid="com.zui.calendar:id/...") 执行点击（rid 非空自动进入 exact 模式）
- 期望结果页面: TimetableActivity
如果执行后页面不符合预期，系统会在下一步自动切换恢复模式。
```

**不匹配时（recovery 模式）**：
```
## REPLAY_RECOVERY — 页面偏离脚本
当前页面: EditTimetableActivity
脚本期望: TimetableActivity（第 5 步）
脚本上下文:
  上一步: click "手动创建课程表" → EditTimetableActivity
  下一步: type_input "测试课程表"
判断当前页面是否已经到达脚本的后续步骤。如果是，跳到对应步骤继续；
如果不是，请导航回脚本路径。优先使用确定性工具（click(label=..., rid=...), get_screen_info）。
```

**type_input 随机后缀执行（按 run 记忆化）**：

对于 `inject_random_suffix: true` 的 type_input 步骤，回放引擎在生成 REPLAY_SCRIPT_STEP 时，通过 `_resolve_action_texts` 对 action 做全文替换（deepcopy，不污染 state 中的证据本体）：

```python
import copy

def _resolve_action_texts(action: dict, actuals: dict[str, str]) -> dict:
    """注入前对任意步骤做全文替换。返回副本，不污染 state 中的证据本体。
    注意：仅做精确匹配（field == original），不命中复合文本（如 assert 的 text="页面包含测试课程表2"）。
    这是有意为之，防止误替换。"""
    resolved = copy.deepcopy(action)
    for original, actual in actuals.items():
        for field_holder, key in (
            (resolved.get("tool_input"), "text"),
            (resolved.get("preferred_locator"), "label"),
            (resolved.get("postcondition"), "expected_value"),
            (resolved.get("args"), "text"),
        ):
            if isinstance(field_holder, dict) and field_holder.get(key) == original:
                field_holder[key] = actual
    return resolved
```

每个步骤注入时都调用，而不是只在 type_input 步骤内处理自己。后续 click(label="测试课程表2") 这类跨步骤引用也会被正确改写。

**⚠️ LangGraph state 更新陷阱**：

LangGraph 节点的 state 更新**必须走 `Command(update={...})` 返回**，原地改节点收到的 state 快照不会可靠持久化（有 checkpointer 时尤其如此）。现有的 run 级可变状态（如 `_vision_tap_fail_streak`）就是存在 ctx 上而非 state。

`_replay_step_idx`、`_replay_mode`、`_replay_input_actuals` 的每次变更都要通过 Command update 提交（或明确改存 ctx），否则状态机推进会"看起来推进了、下一轮又回去了"。

```python
# ❗ 错误：原地赋值不会可靠持久化
state["_replay_input_actuals"][original_text] = actual_text  # 不可靠！

# ✅ 正确：通过 Command update 提交
return Command(update={"_replay_input_actuals": {**state["_replay_input_actuals"], original_text: actual_text}})
```

```python
if action.get("inject_random_suffix"):
    original_text = action["tool_input"]["text"]
    actual_text = _get_actual_text(original_text)  # 查表或生成
    # 更新 actuals 表，后续步骤通过 _resolve_action_texts 自动改写
    # ⚠️ 通过 Command update 提交，不要原地赋值
    new_actuals = {**state["_replay_input_actuals"], original_text: actual_text}
    instruction_text = f'type_input(text={actual_text!r})  [随机后缀，原始: {original_text!r}]'
```
LLM 收到的指令会是：`type_input(text="测试课程表_a3f7")`，避免与已有数据重名。

#### Task 3：步骤推进与偏离检测

**目标**：执行完一步后，判断是否成功推进到下一步，还是偏离了。

**改动文件**：`agents/nodes.py`（agent_node 的 Command update 部分）

**逻辑**：

```python
# 执行后检查
if replay_mode == "script":
    current_action = key_actions[replay_step_idx]
    post = current_action.get("postcondition", {})
    expected_activity = post.get("expected_activity", "")
    expected_value = post.get("expected_value")
    channel = current_action.get("postcondition_channel", "ui_text")
    
    activity_match = expected_activity and expected_activity in current_page_activity
    
    if channel == "vision_verify":
        # canvas 步骤：verify 在 vision_tap 执行时已完成，从结构化 evidence 读结论
        # ⚠️ 必须取"当前脚本步骤对应的工具输出"（按 tool_seq 匹配），不是笼统的 last
        # （如果 LLM 在 vision_tap 之后又调了 get_screen_info，"最后一次工具输出"就不是 vision_tap 的 verify 结果）
        step_output = get_step_tool_output(replay_step_idx)  # 按 tool_seq 匹配
        value_match = parse_evidence(step_output).get("verify_decision") == "yes"
    elif channel == "deferred_assert":
        value_match = True  # 弹窗期间不校验，闸门在后续 assert 步骤
    else:
        # ui_text 通道：拼接当前页所有元素 label
        if expected_value:
            page_text = " ".join(e.label for e in current_elements if e.label)
            value_match = expected_value in page_text
        else:
            value_match = True
    
    if activity_match and value_match:
        # 成功推进
        replay_step_idx += 1
        replay_mode = "script"
    else:
        # 页面或值不匹配，标记偏离
        replay_mode = "recovery"
```

**postcondition_channel 三种通道**：

| 通道值 | 适用场景 | value_match 判定方式 |
|---|---|---|
| `ui_text`（默认） | 普通页面元素 | 拼接元素 label 检查文本 |
| `vision_verify` | canvas 自绘控件（滚轮等） | 从 vision_tap 结构化返回的 `verify_decision` 读取 |
| `deferred_assert` | 弹窗期间无法验证 | 恒 true，由后续 assert 步骤做确定性闸门 |

**vision_tap 结构化返回改造**（Task 1 或 Task 3 子项）：

vision_tap 的 `verify=` 参数当前返回 `verify=[yes]` 文本字符串，违反项目 §0 结构化契约。应改为：
```python
# tools/ 中 vision_tap 的 verify 路径
return make_result(OK, msg, {"verify_decision": v_decision})
# v_decision: "yes" | "no" | "unknown"
```

**verify 文本形态**：`verify="{expected_value}"` 把裸值（如 `09:00-09:53`）直接当验证描述，而 vision_tap 的 verify prompt 是"请根据截图判断：{verify}"——裸值语义不完整。建议提取时存自然语言描述（如"分钟值是否变为53"），或执行时包一层模板：
```python
# 提取时
"verify_prompt": "分钟值是否变为53"

# 或执行时包装
verify_desc = f"{action['tool_input']['description']}执行后，页面是否显示{expected_value}"
```

**偏离容忍策略**：
- 导航步骤（get_screen_info、query_app_knowledge）不计入脚本步骤，回放时 LLM 可自行调用
- 脚本中连续两步在同一个 Activity 时，允许中间有额外的感知步骤
- recovery 模式下最多允许 N 步自由探索，超过则 abort
  - **N 应可配置**（用例级别），默认 3；权限弹窗类用例天然需要更多恢复步，可设为 5-8
  - 配置来源：goal_description 中的 `replay_recovery_budget` 字段，无则取 config 默认值

**entry 对齐容错**：

`_extract_replay_evidence` 中 precondition 取的是 `page_before_activity`，若首帧感知抖动（如 launch_app 后 Activity 还未切换完成），第一步 precondition 就可能不匹配而直接进 recovery。

→ 解决方案：回放引擎在 script 模式启动前，先执行 entry 对齐：
```python
# 在第一个 script 步骤前，确保 entry 已对齐
if replay_step_idx == 0 and entry_action:
    entry_pkg = entry_action.get("launch_app_args", {}).get("package", "")
    entry_act = entry_action.get("launch_app_args", {}).get("activity", "")
    if current_activity != entry_act:
        # 先执行 launch_app 对齐 entry
        inject_launch_app_instruction(entry_pkg, entry_act)
        # 对齐后再开始脚本比对
```

#### Task 4：Prompt 三层拆分（共享契约 + 模式专属）

**目标**：回放模式与探索模式的 system prompt 彻底分离，避免指令互斥共存、token 浪费和维护耦合。

**拆分理由**：

| agent.txt 现有规则 | 回放模式需要的行为 |
|---|---|
| L1「自主导航并验证」 | 按脚本执行，不自主导航 |
| L95「优先调用 query_app_knowledge」 | 回放禁止调 query_app_knowledge（脚本已含知识） |
| L21-25「禁止 click A → click B → click A」 | 回放允许连续 2 次执行同一动作（值未变时重试） |
| L93「超了就 ABORT」 | 回放预算语义不同（脚本步数 + recovery 预算） |

如果往 agent.txt 里追加 REPLAY MODE 段落，会产生：
1. **指令互斥共存**：同一份 prompt 里「自主探索」和「严格按脚本」同时存在，约束力打折
2. **token 目标背道而驰**：回放每轮 LLM 调用都重发 100+ 行用不上的探索规则，白烧 token
3. **维护耦合**：回放状态机规则会频繁迭代，和探索规则混在一起改一边容易误伤另一边

**拆分结构**：

```
agents/prompts/
  agent_common.txt      ← 共享契约层（~60 行）
  agent_explore.txt     ← 探索专属层（~45 行，即当前 agent.txt 的探索部分）
  agent_replay.txt      ← 回放专属层（~25 行，新建）
```

**各层内容归属**：

| 层 | 来源 | 包含内容 |
|---|---|---|
| **common** | agent.txt 现有 | 终止契约（L3-7）、长按/复制/粘贴（L16-19）、元素选择与 index 契约（L42-45）、精确点击（L62-66）、系统权限弹窗（L47-51）、权限双分支（L53-60）、工具返回状态（L68-75）、验证报告（L77-89）、弹窗检测与视觉工具（L97-109） |
| **explore** | agent.txt 现有 | 角色定义（L1）、正确流程（L9-14）、错误流程（L21-25）、启动 App（L27-33）、执行前置检查（L35-40）、规则（L91-96） |
| **replay** | 新建 | 角色定义（回放 Agent）、REPLAY MODE 语义、script/recovery 模式规则、禁止 query_app_knowledge、连续 2 次同动作页面未变才可跳步、recovery 模式的导航能力保留 |

**装配方式（nodes.py）**：

```python
AGENT_SYSTEM = _load_prompt("agent_common.txt") + _load_prompt("agent_explore.txt")
REPLAY_AGENT_SYSTEM = _load_prompt("agent_common.txt") + _load_prompt("agent_replay.txt")

def _select_agent_system(state: dict) -> str:
    # run 级选择：rerun + 有 evidence → replay prompt
    # rerun 但无 evidence（evidence 提取失败）→ 回退到 explore prompt（等价于自由跑）
    if (state.get("_run_type") == "rerun"
            and _has_key_actions(state.get("goal_description", {}))):
        return REPLAY_AGENT_SYSTEM
    return AGENT_SYSTEM
```

**替换点**：`nodes.py:534` 和 `nodes.py:762`（两处都用了 `AGENT_SYSTEM`，不要漏掉第二处）。

**replay prompt 内容设计**（`agent_replay.txt` 草稿）：
```
你是 Android UI 自动化测试 Agent，正在执行一个已验证成功的回放脚本。

## 终止契约
（继承自 common）

## 执行模式
- 每轮你会收到 REPLAY_SCRIPT_STEP 或 REPLAY_RECOVERY 指令。
- REPLAY_SCRIPT_STEP：必须严格按指定工具参数执行，不要自由探索。
- REPLAY_RECOVERY：页面偏离脚本，目标是尽快恢复到脚本路径。
  优先使用确定性工具（click(label=..., rid=...)、get_screen_info）。
  可以使用所有导航工具（scroll_find_and_click 等），但目标是恢复，不是探索。

## 约束规则
- 禁止调用 query_app_knowledge（脚本已包含所有必要知识）。
- 禁止跳过脚本步骤，除非连续 2 次执行同一动作且页面未变化。
- recovery 模式下最多 N 步（由脚本配置），超过则 ABORT。
- 工具调用预算与探索模式不同：脚本步数 + recovery 预算。
```

**设计原则**：
- **静态/动态分离**：`agent_replay.txt` 是静态"模式宪法"；每轮的动态指令（`REPLAY_SCRIPT_STEP` / `REPLAY_RECOVERY`）走 `SystemMessage` 逐轮注入（Task 2 设计），职责清晰
- **recovery 不是纯脚本**：恢复时 Agent 仍需导航能力（get_screen_info、scroll_find_and_click），所以 `agent_replay.txt` 不能写成"只许执行指令"

**测试要求**：
- 在 `tests/` 加一个测试：rerun + 有 evidence → 选 replay prompt 且含 REPLAY 标记
- normal run → 选 explore prompt，行为不变
- rerun + 无 evidence → 回退到 explore prompt

**改动文件**：
- 新增 `agents/prompts/agent_common.txt`、`agent_explore.txt`、`agent_replay.txt`
- 删除原 `agents/prompts/agent.txt`
- `agents/nodes.py`：替换 `AGENT_SYSTEM` 加载逻辑（2 处）
- `tests/`：新增 prompt 选择测试

#### Task 5：回放统计与可观测

**目标**：在 trace 和报告中区分“脚本命中”与“AI 恢复”步骤。

**改动文件**：`agents/nodes.py`（step_history 记录）、`agents/run_trace.py`

**⚠️ run_trace.py 数据不一致修复**：当前 `run_trace.py:55-58` 只对 click 记录 `tool_input`，而 `orchestrator.py:941` 对所有工具都记了 tool_input。回放统计若读 trace JSON，非 click 步骤参数会缺失。Task 5 应顺手统一：所有工具的 tool_input 都写入 trace。

**新增 step_history 字段**：
```python
{
    "index": si,
    ...
    "replay_source": "script" | "recovery" | "",  # ← 新增
    "replay_step_idx": 3,                         # ← 新增：对应脚本第几步
}
```

**报告指标**：
- `script_hit_rate`: 脚本命中率（script 步骤 / 总步骤）
- `recovery_count`: AI 恢复次数
- `recovery_success_rate`: 恢复后成功对齐脚本的比例

## 4. 改动影响评估

| 文件 | 改动类型 | 对应 Task | 影响范围 |
|---|---|---|---|
| `api/test_cases_routes.py` | 补注 evidence（`resolve_report_rerun_entry`）+ 修复 `_extract_replay_evidence` 的 `verification_json` → `verification_results` 字段不匹配 + screenshot 剔除 | Task 0 | **证据链打通** |
| `api/test_cases_routes.py` | `_extract_replay_evidence` 新增 type_input/vision_tap/set_permission_intent/click_and_check 分支 + `business_indexes` 同步更新 + value 断言 | Task 1 | 录制数据格式（向后兼容） |
| `agents/nodes.py` | 渲染层补全 + 逐步注入 + `_resolve_action_texts` deepcopy 改写 + 步骤推进 | Task 2/3 | 回放执行核心 |
| `agents/state.py` | 新增 `_replay_step_idx`、`_replay_mode`、`_replay_input_actuals` | Task 2 | 状态定义 |
| `agents/prompts/agent_common.txt` | 新建：共享契约层 | Task 4 | Agent prompt |
| `agents/prompts/agent_explore.txt` | 新建：探索专属层 | Task 4 | Agent prompt |
| `agents/prompts/agent_replay.txt` | 新建：回放专属层 | Task 4 | Agent prompt |
| `agents/prompts/agent.txt` | 删除（拆为以上 3 个文件） | Task 4 | Agent prompt |
| `tools/click.py` | 更新 L1007 注释（原引用 agent.txt，Task 4 删除后失效） | Task 4 | 纯注释 |
| `agents/run_trace.py` | 新增回放统计字段 + 统一全工具 tool_input 记录 | Task 5 | 可观测性 |
| `agents/orchestrator.py` | 初始化 replay state | Task 2 | 启动逻辑 |
| `tools/` (vision_tap) | `verify=` 返回改为结构化 `{"verify_decision": "yes/no/unknown"}` | Task 3 | vision 工具契约 |

**数据库可重建**：数据库可以重建，不需要写 migration 代码。涉及到数据库的内容不需要考虑向后兼容旧数据。

**向后兼容**：
- 首次 run（非回放）不受影响，`_replay_mode` 为空字符串
- 旧格式 base_evidence（无 tool_input）降级为当前的"参考"模式
- v4 execution_plan 的 base/override/effective 结构不变，只是 key_actions 内每个 action 新增可选字段
- `resolve_report_rerun_entry` 补注 evidence 对旧报告无副作用（`_extract_replay_evidence` 对非 passed run 返回 None）

**前置依赖**：
- SoM 格子定位代码（`_draw_som_grid` / `_parse_cell_response`）已提交并通过 `tests/test_som_grid.py`，确保 vision_tap 步骤的确定性执行基础可用

## 5. 实施优先级

| 优先级 | Task | 预期收益 | 复杂度 |
|---|---|---|---|
| **P0** | **Task 0（打通证据链）** | **不打通则所有回放优化空转** | **低** |
| P0 | Task 2（渲染补全 + 逐步脚本引导） | 解决 P2/P3b，每步都有回放指导 | 中 |
| P0 | Task 3（步骤推进 + 偏离检测 + entry 对齐） | 回放有明确的步骤状态机 | 中 |
| **P0** | Task 1（修复提取层丢弃非 click 工具 + value 断言） | **不修则课程时长用例核心操作（vision_tap 滚轮）不在脚本里** | 低 |
| P1 | Task 4a（prompt 三层拆分 + 选择器） | prompt 分离，省 token + 约束力 | 中 |
| P1 | Task 4b（replay prompt 内容编写） | Agent 行为更可控 | 低 |
| P2 | Task 5（统计可观测） | 回放效果可量化 | 低 |

**建议实施顺序**：Task 0 → Task 1 → Task 2 → Task 3 → Task 4a → Task 4b → Task 5

- Task 0 和 Task 1 都是 P0 且改动最小（同文件 test_cases_routes.py），建议一起实施
- Task 1 不修则课程时长用例核心操作（6 次 vision_tap）不在脚本里，后续优化无意义
- Task 1 和 Task 2+3 可以并行开发（Task 1 改提取端，Task 2+3 改回放端）
- Task 4 放在 Task 2 之后实施：先有逐轮动态指令（Task 2），再收编静态宪法（Task 4）

## 6. 预期效果

| 维度 | 改进前 | 改进后 |
|---|---|---|
| 回放通过率 | rerun 靠 Agent 自由探索碰运气 | 显著高于无 evidence 的自由 rerun |
| token 消耗 | 与首次 run 持平 | rerun / 首次 run input_tokens < 0.5 |
| 乱点率（script 模式） | 高（Agent 自由探索） | < 5%（严格按 locator 执行） |
| 乱点率（recovery 模式） | 高 | 受 recovery budget 约束，可度量 |
| 恢复能力 | 无（全靠自由探索） | 有（recovery 模式 + 可配置 budget） |
| 证据链完整性 | 报告 rerun 无 evidence | 两条路径均有 evidence（Task 0 兜底） |

## 7. 基于 test-20260805_135924 复跑实践的补充优化

> 日期：2026-08-05
> 本节记录 P0/P1/P2 代码修复后的实际 rerun 表现，以及针对"录制脚本含探索噪声"这一新问题的补充 Task。

### 7.1 问题：代码修复已生效，但录制的脚本本身包含探索噪声

#### 7.1.1 已验证的 P0/P1/P2 修复

| 修复点 | 复跑表现 |
|---|---|
| **P1 — `iv_more` 保留** | Step 3 成功点击 `rid=com.zui.calendar:id/iv_more`，没有因无 `label` 被丢弃 |
| **P2 — direct 模式不用 index** | Step 4 点击"课程表"使用 `label+class_name+path_contains`，成功命中；Step 3 `iv_more` 使用 rid，没有带上原始 index |
| **P0 — recovery 不被覆写** | Step 11 "课程表设置" `NOT_FOUND` 后，后续出现 `replay_source=recovery` 的 `get_screen_info`，说明确实进入了 recovery，没有再死循环回 script |

结论：**P0/P1/P2 三个代码修复均已在 test-20260805_135924 复跑中生效。**

#### 7.1.2 复跑仍 `inconclusive` 的根因

复跑失败不是因为回放执行层 bug，而是**录制下来的脚本本身包含了一段探索性/绕路的操作序列**，复跑时机械执行就撞上了状态不匹配：

1. **Step 5–8 在"测试课程表"和"取消"之间空转两轮**  
   首次运行时 agent 点标题只是"想看看切换课表弹窗里有没有新建入口"，然后取消。这段探索被当作关键动作录进了脚本。复跑时它忠实地重复了两遍"点标题 → 取消"，但并没推进任务。

2. **Step 10 点击顶部"+"按钮打开了导入弹层**  
   首次运行 agent 在这里发现"+"其实是"拍照导入课程表 / 图库导入课程表"，不是新建课程表。复跑直接 vision_tap 了这个"+"，于是页面被导入弹层挡住。

3. **Step 11 "课程表设置" `NOT_FOUND`**  
   因为导入弹层没关，页面上找不到"课程表设置"。这里触发了 recovery（P0 生效的证明），但 recovery 预算只有 3 次，且弹层状态下的恢复没有成功。

4. **最终 `report_done` 的结论是假的**  
   Step 16 的 `report_done` 声称三项验证都通过了，但实际上复跑根本没走到验证步骤。这是 LLM 在 recovery 耗尽后产生的幻觉式收尾，导致 verdict 是 `inconclusive` 而非 `fail`。

简言之：**代码层面的 replay 引擎 bug 已修，但录到的"首次运行路径"本身不是一条干净的可回放路径。**

### 7.2 Task 6：提取层脚本净化（Exploration Filtering）

**目标**：让 replay plan 不再是"首次运行的动作切片"，而是"目标达成的最小充分路径"。

首次 run 是探索式成功：agent 绕路、试错、最终找到正确入口。 replay 的价值在于 **用确定性动作复现验证**，不是复刻 agent 的思考过程。所以录制时应做 **"逆向剪枝"**：

```
原始 steps:  [A → B → 探索X → 撤销X → 探索Y → 撤销Y → C → D → 验证V]
replay plan: [A → B → C → D → 验证V]
```

#### 7.2.1 取消闭环过滤（Cancellation Loop Filter）

检测 **"打开弹窗/菜单 → 关闭弹窗/菜单"** 且中间没有 page activity 变化、没有验证推进的闭环，直接剔除。

例如首次运行中的：
- click `测试课程表` → get_screen_info → click `取消` → get_screen_info
- 又 click `测试课程表` → get_screen_info → 又 click `取消` → get_screen_info

**注意**：弹窗本身在 steps_json 里通常不可见（弹窗不是独立 Activity），trace 里只有两次 `get_screen_info` 都显示同一个 Activity。因此不能依赖"弹窗是否打开"这种 UI 树判断。

**识别规则**（基于 observation 信号）：
```python
# dismiss keywords 只匹配关闭/取消类按钮，不匹配"确定"/"OK"等确认类按钮
DISMISS_KEYWORDS = ("取消", "关闭", "Close", "返回")

if action1 is click/vision_tap and action2 is click/press_key
   and action2.observation contains any dismiss keyword
   and action1.page_after == action2.page_after
   and no verification reported between action1 and action2:
       action1.outcome = "exploration"
       action2.outcome = "exploration"
```

这样更鲁棒：不判断“弹窗是否打开”，只利用关闭/取消类动作的 observation 语义和 Activity 未变化两个稳定信号。

**第一阶段实施策略（已更新）**：
- **仅标记，不剔除**：给匹配的 action 打 `outcome=exploration` 标签，保留完整 actions
- 回放引擎当前不读 `outcome` 字段，标记不影响回放行为
- 通过离线分析 ≥10 条历史 run 验证标记准确率 ≥ 90% 后，方可进入自动剔除阶段
- 实现函数：`_tag_cancellation_loops(actions)`（`api/test_cases_routes.py`）

#### 7.2.2 死胡同动作过滤（Dead-end Filter）

如果某个动作在首次运行中 **导致 NOT_FOUND、ERROR、或打开了一个后续必须手动关闭的错误弹层**，则不应进入 replay plan。

例如：
- `vision_tap(+按钮)` 打开了"拍照导入课程表"弹层 → 首次运行后续不得不 `press_key(back)` 关闭
- 这种"打开错误弹层"的动作应标记为 `exploratory_dead_end`，不录制

实现方式：给每个 step 打标签：
```python
step_outcome = "progress" | "exploration" | "dead_end" | "recovery"
```

**第一阶段实施策略（已更新）**：
- **仅标记，不剔除**：给匹配的 action 打 `outcome=dead_end` 标签，保留完整 actions
- `press_key(back)` 判断死胡同有风险（正常导航也常用 back），因此第一阶段只做标记
- 通过离线分析验证标记准确率后再决定是否自动剔除
- 实现函数：`_tag_dead_ends(actions, steps)`（`api/test_cases_routes.py`），通过 `_source_step_idx` 精确关联 action 与 raw step

#### 7.2.3 重复动作合并（Deduplication）

连续多次相同动作（如连续 3 次点 `取消`）合并为一次，或只保留最后一次有 page 变化的那次。

#### 7.2.4 Activity 变迁图剪枝

构建首次运行的 Activity 序列，检测 **回退动作**：如果某个动作让 Activity 回到已访问过的状态，且中间没有产生任何 verification 证据，则标记为 `exploration`。

**通用规则**（不硬编码 Activity 列表，跨 App 可用）：
```python
visited_activities = []
for action in steps:
    after = action.page_after_activity
    if after in visited_activities and no_verification_since(after):
        action.outcome = "exploration"
    visited_activities.append(after)
```

这比硬编码 `critical_path` 更通用，也能正确识别"从 `TimetableActivity` 进入 `TimetableListActivity` 又退回 `TimetableActivity`"这类探索回退。

> 原文此处曾使用硬编码 `critical_path`，是反模式。已修正为通用 visited_activities 规则。

#### 7.2.5 验证回溯关联（保守实现）

给每个 `key_action` 关联它要支撑的 verification：
```python
key_action["verifies"] = ["v0"]  # 该动作是 v0 的前置或证据
```

提取时做逆向回溯，从 `assert_verification` / `assert_page_contains` 倒推，标记直接贡献验证的动作。

**注意**：导航步骤（`launch_app`、点菜单进入子页面等）本身不产生 verification，但对验证是必需的。如果直接剔除"未关联 verification"的动作，会误杀导航链。因此实现上要保守：

1. **只标记，不剔除**：把 `verifies` 作为可信度信号，回放时优先执行有验证关联的步骤
2. **如需剔除，先做 forward reachability**：对被标记的步骤，检查其前置 precondition 依赖的页面是否在剩余步骤中可达；不可达时保留必要的导航步骤

> 不建议在 Task 6 第一阶段就启用自动剔除，避免过度剪枝风险。先完成"标记"即可。

### 7.3 Task 7：回放层弹层容错与恢复增强

即使 plan 还不够干净，回放时也应更能容错。

#### 7.3.1 弹层后置消解

不依赖前置的"弹层检测"（定义什么是弹窗在通用场景很难），而是在 **direct click 返回 NOT_FOUND 后** 做后置消解：

```python
result = direct_click(locator)
if result.status == "NOT_FOUND" and replay_mode == "script":
    popup_elements = detect_overlay_or_dialog_elements()
    if popup_elements:
        dismiss_popup(popup_elements)  # press_key("back") 或 click 取消/关闭
        result = retry_direct_click(locator)
```

后置消解更可靠：
- `NOT_FOUND` 是明确信号——目标元素不在预期的无障碍树位置
- 此时再尝试消解弹层，比提前猜测"有没有弹层"更稳
- 对 `test-20260805_135924` 的场景，Step 11 "课程表设置" NOT_FOUND 时可以先关闭导入弹层，再重试一次

#### 7.3.2 Recovery 预算动态化

当前固定 3 次太紧。可按脚本长度动态：
```python
recovery_budget = max(3, len(key_actions) // 4)
```

#### 7.3.3 Recovery 指令增强

trace 中 recovery LLM 已经有能力调用 `press_key("back")`，但它选择了 `get_screen_info`。问题不在工具库，而在于 recovery prompt 没有明确告诉它"优先关闭意外弹层"。

在 recovery 指令中增加一条：
```
如果当前页面存在与脚本无关的弹窗/覆盖层（如"拍照导入"/"图库导入"），
优先使用 press_key("back") 关闭它，然后重试脚本步骤。
```

这比"扩展 recovery 动作库"更直接有效，改动也更小。

### 7.4 Task 8：结果层防幻觉安全闸门（P0）

这是当前最严重的问题之一：脚本还没走完，agent 就 `report_done(passed)`。

#### 7.4.1 实现方案

在 `reporter_node` 中增加精确检查（所有回放路径最终都经过 reporter_node）：

```python
_strict = bool(goal.get("strict_replay_completion", True))
if _strict and replay_enabled and replay_step_idx < len(key_actions):
    # 脚本还没走完就 verdict=passed —— 一定是幻觉收尾
    collected_vkeys = {
        e.get("result_evidence", {}).get("verification_key", "")
        for e in tool_calls_log
        if e.get("name") == "assert_verification"
    }
    required = {f"v{i}" for i in range(len(verification_items))}
    missing = required - collected_vkeys
    _guard_reason = f"script_incomplete,step={step}/{total}"
    if missing:
        _guard_reason += f",missing_verifications={sorted(missing)}"
    test_verdict = "failed"
```

要点：
- verdict 统一设为 `failed`（而非 abort），附带明确原因
- `strict_replay_completion` 配置（默认 True，可关闭）避免不干净 plan 导致全部 fail
- verification completeness 检查：收集已执行的 verification_key 与 goal 中必需项对比
- guard 触发原因结构化到 `run_trace.metrics`（`replay_guard_triggered` + `replay_guard_reason`）

**注入点说明**：
- 放在 `reporter_node`（而非 `_after_tools`），因为 reporter_node 是所有回放路径的最终汇合点
- direct 模式和 LLM 模式都经过 reporter_node，一处拦截即可

#### 7.4.2 效果

- 直接杜绝 `inconclusive` 变假 `passed`
- recovery 耗尽后 LLM 无法再 hallucinate 一个成功收尾
- verdict 正确落为 `failed`，附带结构化原因
- 用户可通过 `strict_replay_completion: false` 在用例级别关闭闸门

### 7.5 实施优先级调整（基于 Review 反馈修订）

> **实施顺序修订理由**：原顺序 Task 6 在 Task 2/3 之前，但 Task 6（脚本净化）依赖 Task 2/3（回放引擎）才能验证净化效果。修订为 Milestone 制，每个 Milestone 都有可度量的产出。

| Milestone | 包含 Task | 产出目标 | 依赖 |
|---|---|---|---|
| **M1：数据正确性** | Task 0 + Task 1 | 报告 rerun 路径有完整 execution_plan；非 click 工具生成 key_action | 无 |
| **M2：最小状态机 + 验证层** | Task 2 + Task 3 + Task 8 + Task 10 | 回放有逐步引导、步骤推进、防幻觉闸门、验证证据回溯 | M1 |
| **M3：脚本净化 + 容错** | Task 6（**仅标记**） + Task 7 | 探索性动作打标签（不剔除）；弹层容错 + recovery 增强 | M2 |
| **M4：优化 + 可观测** | Task 4（prompt 拆分，P2）+ Task 5（统计可观测） | prompt 分离、回放效果可量化 | M3 |

**建议实施顺序**：M1 (Task 0 + 1) → M2 (Task 2 + 3 + 8 + 10) → M3 (Task 6 + 7) → M4 (Task 4 + 5)

| 优先级 | Task | 预期收益 | 复杂度 | 状态 |
|---|---|---|---|---|
| **P0** | Task 0（打通证据链） | 不打通则所有回放优化空转 | 低 | ✅ 已实现 |
| P0 | Task 1（修复提取层丢弃非 click 工具 + value 断言） | 不修则核心操作不在脚本里 | 低 | ✅ 已实现 |
| **P0** | **Task 8（结果层防幻觉安全闸门）** | 杜绝假 passed，ROI 最高 | 低 | ✅ 已实现（含 `strict_replay_completion` 配置开关） |
| P0 | Task 2（渲染补全 + 逐步脚本引导） | 解决 P2/P3b，每步都有回放指导 | 中 | ✅ 已实现 |
| P0 | Task 3（步骤推进 + 偏离检测 + entry 对齐） | 回放有明确的步骤状态机 | 中 | ✅ 已实现 |
| P0 | Task 6（提取层脚本净化：6.1 + 6.2） | 解决“点标题→取消”空转和死胡同动作 | 低 | ✅ 已实现（**仅标记，不剔除**） |
| P1 | Task 7（回放层弹层容错 7.1 + recovery 预算 7.2 + 指令增强 7.3） | 提升对不完美 plan 的容错 | 中 | ✅ 已实现 |
| P1 | Task 4a（prompt 三层拆分 + 选择器） | prompt 分离，省 token + 约束力 | 中 | ✅ 已完成 |
| P1 | Task 4b（replay prompt 内容编写） | Agent 行为更可控 | 低 | ✅ 已完成 |
| P2 | Task 6.4（Activity 变迁图通用剪枝） | 需要更多验证通用规则稳定性 | 中 | ❗ 待实施 |
| P2 | Task 5（统计可观测） | 回放效果可量化 | 低 | ✅ 已实现（含 `replay_guard_triggered` + `verification_evidence_sources` 指标） |
| **P0** | **Task 10（验证层证据回溯）** | 让直执 assert_verification 能读取 click_and_check/visual_check 的结构化 evidence | 低 | ✅ 已实现 |
| P2 | Task 9（conditional_action） | 处理非确定性步骤（权限弹窗/网络弹层） | 中 | ❗ 未来增强 |

调整理由：
- **Task 8 提前到 P0**：实现简单，收益最高，直接杜绝假 passed
- **Task 6 仅标记不剔除**：先打 `outcome` 标签，通过离线分析验证标记准确率后再决定是否自动剔除。准出标准：≥10 条历史 run 上标记准确率 ≥ 90%
- **Task 8 加 `strict_replay_completion` 配置**：默认 True 可关闭，避免不干净 plan 导致全部 fail
- **Task 1 value 断言简化**：`vision_verify` 通道降级使用简单 verify 提示（如"当前页面是否显示 {expected_value}"），不稳定时由后续 `assert_verification` 兜底，避免 vision 模型对细微文本变化的判定抖动
- **Task 10 加入 M2**：验证层是回放通过率的直接瓶颈，实现轻量（向前扫描 tool_log），与 Task 8（防幻觉闸门）互补

## 7.6 Task 10：验证层证据回溯（直执模式 assert_verification 读取 vision 证据）

### 7.6.1 问题定位

回放执行层已完美工作，但验证层无法自动判定，导致 rerun 拿不到 passed。根因：
- `assert_verification` 在直执模式下只查找关联的 `assert_page_contains` / `assert_element_exists` 结果
- 当脚本使用 `click_and_check` / `visual_check` 进行验证（而非显式 assert），直执模式的 `assert_verification` 找不到关联证据，返回 `unknown`

### 7.6.2 实现方案

在 `_build_replay_verification_args` 中增加两层回溯：

```python
# 第 1 层：关联断言（已有）
linked_idx = {assert_page_contains / assert_element_exists 关联项}
if 全部 PASS → passed
if 任一 FAIL → failed

# 第 2 层（Task 10）：向前扫描 vision 工具
if verdict == "unknown":
    for entry in reversed(tool_log[-5:]):  # 最近 5 步
        if entry.name in ("click_and_check", "visual_check"):
            decision = _parse_vision_decision(entry.observation)
            if decision == "yes" → passed
            if decision == "no" → failed
```

证据解析：
- `click_and_check` 返回 `OK: 已点击.. [yes] ...` → 正则匹配 `[yes/no]`
- `visual_check` 返回 `{"decision": "yes", ...}` → JSON 解析 decision 字段

### 7.6.3 可信度映射

| click_and_check / visual_check 结果 | assert_verification 返回 |
|---|---|
| `verify=[yes]` / `decision=yes` | `passed` |
| `verify=[no]` / `decision=no` | `failed` |
| 无 verify 字段 / unknown / 无法解析 | `unknown` |

### 7.6.4 可观测性

- 每个 assert_verification 的 tool_log entry 包含 `_evidence_source` 字段
- 取值：`"assert"` / `"click_and_check"` / `"visual_check"` / `"none"`
- reporter_node 汇总为 `verification_evidence_sources` 指标写入 run_trace

### 7.6.5 边界处理

- **关联优先级**：assert_page_contains / assert_element_exists 优先于 vision 证据
- **扫描范围**：只扫描当前 verification 之前的最近 5 步（`_VERIFICATION_LOOKBACK = 5`）
- **内部字段隔离**：`_evidence_source` 等 `_` 前缀字段在 `tool.invoke()` 前被剥离，不影响工具执行

## 8. 实验设计

### 8.1 目标定义

回放场景下的成功标准不应是“≥ 首次 run”，而是可量化的相对提升：

| 指标 | 定义 | 目标 |
|---|---|---|
| 回放通过率 | rerun verdict=passed 的占比 | 显著高于无 evidence 的自由 rerun |
| script_hit_rate | 回放脚本步骤占比（基于工具调用数） | ≥ 70% |
| recovery 成功率 | recovery 后回到 script 的占比 | ≥ 50% |
| token 节省比 | rerun input_tokens / 首次 run input_tokens | < 0.5 |
| 乱点率（script 模式） | script 步骤中 NOT_FOUND 的比例 | < 5% |
| 乱点率（recovery 模式） | recovery 步骤中 NOT_FOUND 的比例 | 受 budget 约束 |

### 8.2 失败分类

回放失败应归因为以下四类之一：

| 分类 | 判定条件 | 示例 |
|---|---|---|
| **脚本缺陷** | plan 本身不包含必需步骤 | 提取层丢弃了 type_input |
| **环境漂移** | 页面结构与录制时不同 | Activity 变更、元素 rid 变化 |
| **恢复失败** | recovery 预算耗尽未能回到脚本 | 弹层无法关闭、recovery budget=0 |
| **幻觉收尾** | guard 触发，verdict 被强制为 failed | 脚本未完成时 LLM 假 report_done |

### 8.3 对照组设计

| 组 | 配置 | 目的 |
|---|---|---|
| A：无 evidence 自由 rerun | 不注入 execution_plan | 基线：自由探索的成功率 |
| B：仅 M1（Task 0+1） | 注入完整 key_actions，无状态机 | 验证数据正确性的收益 |
| C：M1+M2（Task 0/1/2/3/8） | 完整状态机 + 闸门 | 验证状态机的收益 |
| D：完整方案（M1+M2+M3） | 状态机 + 标记 + 容错 | 验证容错的收益 |

### 8.4 评估用例集

建议至少 10-20 条不同场景的用例：
- 简单场景：2-3 步（如“打开设置”）
- 中等场景：5-10 步（如“创建课程表”）
- 复杂场景：15+ 步（如“创建多门课程表”）
- 包含弹层的场景：权限弹窗、导入弹窗
- 包含同 Activity 步骤的场景：vision_tap 滚轮、type_input

### 8.5 指标计算方式

- `script_hit_rate` = script 工具调用数 / 总回放工具调用数（基于 `replay_source` 字段）
- `recovery_success_rate` = recovery 后回到 script 的次数 / recovery 总次数（基于相邻 `replay_source` 变化）
- `replay_guard_triggered` = guard 触发的次数 / 总 rerun 次数（基于 `run_trace.metrics`）

## 9. Task 9：conditional_action（未来增强）

录制步骤中有很多非确定性：
- 权限弹窗：首次出现 vs 不再出现
- 网络请求：加载时间、Toast 提示
- 用户数据状态：课程表是否已存在、弹层是否首次展示

当前方案靠 Task 7.1（弹层后置消解）和 recovery 模式处理，但每次都需要 LLM 调用，增加 token 消耗。

### 9.1 conditional_action 设计

给 action 增加 `condition` 字段，回放时由 direct executor 判断：

```json
{
  "tool": "click",
  "locator": {"label": "允许"},
  "condition": {"if_element_exists": "允许"},
  "timeout_ms": 5000
}
```

回放时：
1. 检查 `condition.if_element_exists` 指定的元素是否存在
2. 存在 → 执行 action
3. 不存在 → 跳过，推进到下一步
4. 超时后仍未出现 → 跳过

这样权限弹窗、导入弹窗等非确定性步骤可以在不进入 recovery 的情况下自动处理，减少 LLM 调用。

### 9.2 实施时机

待 M3（Task 6+7）完成且在实际复跑中验证效果后，再评估是否需要 conditional_action。如果 M3 的弹层容错已经足够处理大部分场景，此 Task 可延后。

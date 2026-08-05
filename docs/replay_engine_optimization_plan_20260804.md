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
- SoM 格子定位代码（`_draw_som_grid` / `_parse_cell_response`）当前在工作区未提交，实施前应先提交并回归 `tests/test_som_grid.py`，否则回放端对 vision_tap 步骤的"确定性执行"无从谈起

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
| 回放成功率 | rerun 靠 Agent 自由探索碰运气 | ≥ 首次 run（脚本命中步骤确定性执行） |
| token 消耗 | 与首次 run 持平（每步 LLM 推理 + 108 行探索 prompt） | 显著下降（脚本命中步骤 LLM 只确认参数；prompt 拆分后回放不携带探索规则，每步省 ~45 行 prompt token） |
| 乱点率 | 高（Agent 自由探索） | 接近零（脚本步骤严格按 locator 执行） |
| 恢复能力 | 无（全靠自由探索） | 有（recovery 模式 + 可配置步数限制） |
| 证据链完整性 | 报告 rerun 无 evidence | 两条路径均有 evidence（Task 0 兜底） |

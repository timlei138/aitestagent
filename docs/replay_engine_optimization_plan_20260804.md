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

首次 run 通过 → 创建用例/复跑 → 回放时出现大量"点错"，成功率和 token 消耗未如预期下降。

### 1.2 数据对比（联想日历课程表用例）

| 维度 | 首次 run（144130） | 回放 run（典型） |
|---|---|---|
| test_verdict | passed | 经常 inconclusive/failed |
| steps | 89 | 更多（探索路径更长） |
| vision_tap | 2 次 SoM 格子定位 | 更多次（探索性调用） |
| token | ~5.9M input | 更高（LLM 每步都在重新推理） |

## 2. 现状分析

### 2.1 当前回放数据流

```
首次 run:
  Planner → plan_review → Agent（自由探索）
  ↓ 完成后
  _extract_replay_evidence(run) → base_evidence（key_actions 列表）
  ↓ 存入 goal_json.execution_plan

回放 rerun:
  route_start 检测到 goal_description 有内容 → 跳过 Planner → agent_node
  ↓ agent_node 第一轮
  _render_replay_evidence_block(goal_desc) → REPLAY_EVIDENCE_BLOCK（SystemMessage）
  ↓ 第二轮起
  回放证据消失，Agent 完全自由探索
```

### 2.2 当前录制内容（_extract_replay_evidence）

每个 key_action 记录：
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

**缺失的关键信息：**
- 完整的工具参数（type_input 的文本、vision_tap 的 description、repeat 次数）
- 步骤间的导航步骤（get_screen_info、query_app_knowledge）
- 操作前的页面元素签名（用于精确匹配校验）

### 2.3 当前回放注入（_render_replay_evidence_block）

```
## REPLAY_EVIDENCE_BLOCK (historical facts, not a forced script)
Use current perception as the authority...
1. click reference locator={'label': '课程表'}; observed_index=0; precondition_activity='AllInOneActivity'.
2. click reference locator={'label': '手动创建课程表'}; observed_index=5; precondition_activity='TimetableActivity'.
...
```

**5 个结构性问题：**

| # | 问题 | 影响 |
|---|---|---|
| P1 | "historical facts, not a forced script" — Agent 可以无视证据 | 回放无约束力 |
| P2 | 仅第 1 步注入（`if not msgs`） | 第 2 步起 Agent 完全自由探索 |
| P3 | 不含完整工具参数 | Agent 不知道 type_input 该输入什么文本 |
| P4 | 无步骤推进机制 | 不知道"下一步该执行脚本的第几步" |
| P5 | 无偏离-恢复判定逻辑 | 偏离脚本时全靠 LLM 自由发挥 |

## 3. 方案设计

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

#### Task 1：增强录制信息（_extract_replay_evidence）

**目标**：录制更完整的"操作脚本"，为回放提供充分的执行参数。

**改动文件**：`api/test_cases_routes.py`

**新增录制字段**：

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
  "expected_page_to": "TimetableActivity",       // ← 新增：期望跳转页面
  "postcondition": {"expected_activity": "TimetableActivity"},
  "last_observation": "...",
  "last_result": "OK"
}
```

对于非 click 工具（type_input、vision_tap 等）：
```json
{
  "step": "type_课程名",
  "tool": "type_input",
  "tool_input": {"text": "测试课程表"},          // ← 关键：输入文本
  "precondition": {"expected_activity": "EditTimetableActivity"},
  "postcondition": {"expected_activity": "EditTimetableActivity"}
}
```

```json
{
  "step": "vision_tap_时间滚轮",
  "tool": "vision_tap",
  "tool_input": {"description": "分钟50", "repeat": 3},  // ← 关键：描述 + 重复次数
  "precondition": {"expected_activity": "TimePickerDialog"},
  "postcondition": {"expected_activity": "TimePickerDialog"}
}
```

#### Task 2：回放引擎 — 逐步脚本引导（_render_replay_evidence_block 重构）

**目标**：每步注入"下一步应执行的脚本动作"，而非一次性给出全部历史。

**改动文件**：`agents/nodes.py`

**核心逻辑**：

在 `agent_node` 中，回放模式下每一步：
1. 从 `key_actions` 取出当前待执行步骤 `replay_step_idx`
2. 检查 precondition（当前页面 Activity 是否匹配）
3. 根据匹配结果注入不同指令

**新增 state 字段**：
```python
_replay_step_idx: int      # 当前脚本步骤索引（回放模式专用）
_replay_mode: str          # "script" | "recovery" | ""（空=非回放）
```

**注入 prompt 设计**：

**匹配时（script 模式）**：
```
## REPLAY_SCRIPT_STEP [3/15] — 按脚本执行
当前页面 AllInOneActivity 与脚本 precondition 匹配。
请执行以下操作（不要自由探索）：
- tool: click
- locator: {"label": "课程表", "rid": "com.zui.calendar:id/..."}
- 使用 click_exact(label="课程表", rid="com.zui.calendar:id/...") 执行点击
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
如果不是，请导航回脚本路径。优先使用确定性工具（click_exact, get_screen_info）。
```

#### Task 3：步骤推进与偏离检测

**目标**：执行完一步后，判断是否成功推进到下一步，还是偏离了。

**改动文件**：`agents/nodes.py`（agent_node 的 Command update 部分）

**逻辑**：

```python
# 执行后检查
if replay_mode == "script":
    current_action = key_actions[replay_step_idx]
    expected_page_to = current_action.get("postcondition", {}).get("expected_activity", "")
    
    if expected_page_to and expected_page_to in current_page_activity:
        # 成功推进
        replay_step_idx += 1
        replay_mode = "script"
    else:
        # 页面不匹配，标记偏离
        replay_mode = "recovery"
```

**偏离容忍策略**：
- 导航步骤（get_screen_info、query_app_knowledge）不计入脚本步骤，回放时 LLM 可自行调用
- 脚本中连续两步在同一个 Activity 时，允许中间有额外的感知步骤
- recovery 模式下最多允许 3 步自由探索，超过则 abort

#### Task 4：回放 prompt 约束力升级

**目标**：回放模式下 Agent 的 system prompt 明确区分"脚本执行"和"自由探索"。

**改动文件**：`agents/prompts/agent.txt`（或 nodes.py 中动态注入）

**新增回放专用 system 指令**：
```
## REPLAY MODE
你正在执行一个已验证成功的测试回放脚本。
- 当收到 REPLAY_SCRIPT_STEP 指令时，必须严格按指定工具参数执行，不要自由探索。
- 当收到 REPLAY_RECOVERY 指令时，目标是尽快恢复到脚本路径，优先使用确定性工具。
- 不要调用 query_app_knowledge（脚本已包含所有必要知识）。
- 不要跳过脚本步骤，除非连续 2 次执行同一动作且页面未变化。
```

#### Task 5：回放统计与可观测

**目标**：在 trace 和报告中区分"脚本命中"与"AI 恢复"步骤。

**改动文件**：`agents/nodes.py`（step_history 记录）、`agents/run_trace.py`

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

| 文件 | 改动类型 | 影响范围 |
|---|---|---|
| `api/test_cases_routes.py` | 增强 `_extract_replay_evidence` | 录制数据格式（向后兼容） |
| `agents/nodes.py` | 重构 `_render_replay_evidence_block` + agent_node 注入逻辑 | 回放执行核心 |
| `agents/state.py` | 新增 `_replay_step_idx`、`_replay_mode` | 状态定义 |
| `agents/prompts/agent.txt` | 新增回放模式说明 | Agent prompt |
| `agents/run_trace.py` | 新增回放统计字段 | 可观测性 |
| `agents/orchestrator.py` | 初始化 replay state | 启动逻辑 |

**向后兼容**：
- 首次 run（非回放）不受影响，`_replay_mode` 为空字符串
- 旧格式 base_evidence（无 tool_input）降级为当前的"参考"模式
- v4 execution_plan 的 base/override/effective 结构不变，只是 key_actions 内每个 action 新增可选字段

## 5. 实施优先级

| 优先级 | Task | 预期收益 | 复杂度 |
|---|---|---|---|
| P0 | Task 2（逐步脚本引导） | 解决"只注入第1步"的核心问题 | 中 |
| P0 | Task 3（步骤推进 + 偏离检测） | 回放有明确的步骤状态机 | 中 |
| P1 | Task 1（增强录制信息） | 回放有完整参数可用 | 低 |
| P1 | Task 4（prompt 约束力） | Agent 行为更可控 | 低 |
| P2 | Task 5（统计可观测） | 回放效果可量化 | 低 |

**建议实施顺序**：Task 1 → Task 2 → Task 3 → Task 4 → Task 5

Task 1 和 Task 2+3 可以并行开发（Task 1 改录制端，Task 2+3 改回放端）。

## 6. 预期效果

| 维度 | 改进前 | 改进后 |
|---|---|---|
| 回放成功率 | 低于首次 run | ≥ 首次 run（脚本命中步骤确定性执行） |
| token 消耗 | ≥ 首次 run（每步 LLM 推理） | 显著下降（脚本命中步骤 LLM 只确认参数，不做路径推理） |
| 乱点率 | 高（Agent 自由探索） | 接近零（脚本步骤严格按 locator 执行） |
| 恢复能力 | 无（全靠自由探索） | 有（recovery 模式 + 3 步限制） |

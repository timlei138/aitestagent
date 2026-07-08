# 执行稳定性与可解释性修复计划（2026-07-08）

## 1. 背景
- 在最近一次执行（`test-20260708_110925`）中，已出现以下问题：
  1. 第一个验证项已在执行中 `passed`，但测试报告详情未体现（`verification_json` 为空）。
  2. 存在“点击热词文本（如霍震霆...）”这类语义上看似无关的动作，影响可读性与可靠性。
  3. 存在明显步骤浪费，最终触发 `MAX_TURNS_EXHAUSTED`，导致任务未完整收敛。
- 新增需求：
  1. 验证项通过时，返回“通过理由”。
  2. 前端展示“AI为什么调用该工具/下一步要做什么”的说明文本。

## 2. 目标
1. 保证验证结果“过程记录”和“最终报告”一致，不丢失已通过项。
2. 降低误导性点击与无效循环，提升步数利用率。
3. 增强步骤与报告可解释性（理由、意图、证据）。
4. 在“无限工作台 -> 全部应用”场景增加稳定先验，减少路径偏航。

## 3. 计划范围（已纳入 Review 补充）

### 3.0 已确认遗漏（本次补齐）
- **遗漏A：`route_after_agent` 固定 12 次上限**
  - 现状：存在硬编码 `if n >= 12: return reporter`，与动态 `max_turns` 冲突。
  - 计划：改为统一预算函数计算，避免 magic number 和路由/状态判定不一致。
- **遗漏B：知识注入位置不明确**
  - 现状：RAG 在 HumanMessage 末尾，优先级不足。
  - 计划：将关键导航规则提升到 system 级强约束；RAG 保持补充说明角色。
- **遗漏C：冷却机制与断路器关系不清**
  - 现状：已有 `_LOOP_BREAK_CONSECUTIVE=3`（同签名重复断路）。
  - 计划：新增“语义冷却”仅处理非同签名抖动，不重复实现现有断路器。

### 3.0.1 预算统一设计（新增，P0）
- 新增统一函数：`_calc_budget(goal)`，返回三项预算：
  - `max_tool_calls_total = 16 + pages*9 + verifications*8`
  - `max_agent_iterations = clamp(2 + pages + verifications, min=6, max=18)`
  - `max_turns_per_iteration = clamp(ceil(max_tool_calls_total / max_agent_iterations), min=6, max=12)`
- 三处必须统一调用，不允许各自写常量：
  1. `agent_node` 调 `_run_agent(..., max_turns=max_turns_per_iteration)`
  2. `route_after_agent` 用 `max_agent_iterations` 判定是否继续
  3. `_determine_execution_status` 用同一 `max_agent_iterations` 判定 exhausted
- 预算关系定义（防止两层预算失控）：
  - route（迭代层）为主导：迭代到上限即结束并进 reporter
  - `_run_agent.max_turns` 是每轮子图断路器
  - 软上限：`总工具调用数 <= max_tool_calls_total`，超限直接 exhausted
  - 软上限检查点：放在 `agent_node` 返回前；若 `len(tool_calls_log) >= max_tool_calls_total`，直接置 `status="fail"` 并写入超限原因，触发 `route_after_agent -> reporter`

### 3.1 报告一致性修复（高优先级）
- 修复 `assert_verification` 到 `test_runs.verification_json` 的落盘链路。
- 当执行 `exhausted/abort` 时，保留已记录 verification 项，不再清空为 `[]`。
- 明确状态语义：
  - `execution_status`: `completed / exhausted / aborted`
  - `test_verdict`: `passed / failed / inconclusive`
- 报告页按 verification 明细展示，不因执行中断抹掉已通过项。

### 3.2 全部应用入口策略修复（高优先级）
- 增加“人工经验”规则（无限工作台场景）：
  - 入口别名：`应用 / 应用列表 / 所有应用`
  - 优先特征：`rid=com.zui.launcher:id/taskbar_view`、`role=list_entry`
  - 路径 `taskbar_container > taskbar_view` 作为辅助约束（弱约束）
- **注入位置与优先级**：
  - 写入 `AGENT_SYSTEM`（system prompt）作为强规则；
  - RAG/人工知识继续作为补充（弱规则）；
  - 冲突时以 system 规则优先。
- **System Prompt 追加文本（拟定）**：
  ```text
  ## 元素选择优先级（强约束）
  - 导航动作必须优先依据 rid/role/path 证据，不得仅凭热词/新闻标题文本点击。
  - `search_keyword`、`search_bar_bg` 属于搜索区元素，不得作为“进入页面”导航目标。
  - 在无限工作台进入全部应用时，优先匹配：
    1) rid=com.zui.launcher:id/taskbar_view
    2) role=list_entry 且 label 命中“应用/应用列表/所有应用”
    3) path 含 taskbar_container > taskbar_view（辅助）
  - 若候选冲突，按 rid > role+label > path 的优先级决策。
  ```
- 增加负向规则：
  - 搜索场景下，不把热词/新闻标题作为目标入口（如 `search_keyword` 文本）。
- 点击后立即做“可输入/页面到达”校验，失败则切换候选而非继续错误链路。

### 3.3 布局文案去误导（中优先级）
- 调整 `two_pane` 文案语义，避免被理解为“严格左右布局”。
- 建议输出：
  - `layout=two_pane（结构分区标签，不保证左右方位）`
- 在 agent 提示中强调：导航优先依赖 `clickable/rid/role/path` 证据，不依赖 left/right 字面方位。

### 3.4 步骤预算与防浪费（高优先级）
- 增加“无进展检测”：
  - `NO_PROGRESS` 阈值从 `16` 下调到 `8`（必要时再评估到 `6`）。
- 调整 `_NO_PROGRESS_ACTIONS`（避免误伤）：
  - 从统计集合移除：`launch_app`、`navigate_to`、`dismiss_popup`、`wait_seconds`
  - 保留完整清单（移除后）：`click`、`scroll_find_and_click`、`long_press`、`copy`、`scroll_panel`、`type_input`、`press_key`、`paste`、`swipe`、`open_notification`、`open_quick_settings`、`unlock_screen`、`set_orientation`、`toggle_auto_rotate`、`recover_from_anomaly`
- 增加动作去重/冷却（与现有断路器分工）：
  - 现有 `_LOOP_BREAK_CONSECUTIVE=3` 继续处理“同 tool+args+page_signature 重复”；
  - 新冷却只处理“语义近似抖动”（`back/swipe/scroll_panel/应用列表往返`）。
- **语义冷却算法（拟定）**：
  - 窗口：最近 6 个 action tool calls（不含 get_screen_info/query）
  - 近似分组：
    - `nav_back`: `press_key(back)`
    - `browse`: `swipe` + `scroll_panel`
    - `app_entry_retry`: 命中“应用/应用列表/所有应用”的重复点击
  - 触发条件：窗口内同分组 >= 4 且无里程碑（无 page 变化、无 assert_verification）
  - 冷却行为：阻止该分组继续 2 次调用，并注入 SystemMessage 要求切换策略（结构化定位或上报 failed）
  - 状态存储：在 `_SubState` 新增 `_cooldown_map: dict[str, int]`，按分组记录剩余冷却次数（如 `{"browse": 2}`）
- 增加失败升级策略：
  - `未找到元素` 达阈值后，切换到结构化定位 + 备选入口策略。
- 增加收尾预算保护：
  - 算法定义：
    - `remaining_tool_budget = max_tool_calls_total - used_tool_calls`
    - 触发阈值：`remaining_tool_budget <= 5`（先用绝对值，后续可调）
    - 触发行为：向子图注入一次 SystemMessage（finalization hint），要求优先执行：
      1) 对可判定项立即 `assert_verification`
      2) 无可继续证据时立即 `report_done(status="abort", summary=...)`
    - 本期不在 `_tools_node` 做硬拦截（避免误伤正常收尾动作），先采用提示驱动

### 3.5 可解释性增强（新增功能，中高优先级）
- **验证理由**：
  - `assert_verification` 的 `detail` 作为通过/失败理由必填。
  - 强制手段采用**方案 A**：tool 层校验 `detail` 为空则返回 error（`detail is required`），促使模型重试补齐理由。
  - 兜底策略：同一验证项连续重试 2 次仍无 `detail`，降级接受空值并写入占位说明（如 `detail unavailable after retries`），避免 error→retry 死循环。
  - 报告详情展示：`condition + result + reason(detail) + screenshot`。
- **步骤意图展示**（前端）：
  - 每一步展示三段结构：`LLM意图文本 -> 工具调用 -> 工具结果`。
  - 使用模型 tool-call 前的 `text/content` 作为“为什么这么做”的依据。
  - 默认折叠，支持展开查看完整内容。

## 4. 影响面（预估）
- 后端执行链路：Agent/Reporter/TestRun 持久化与状态汇总。
- 规则与知识：人工经验注入、候选元素评分、负向匹配规则。
- 前端展示：步骤时间线、报告详情字段扩展。
- 数据结构：`verification_json` 写入逻辑与前端读取兼容性。
- 预算联动点：`route_after_agent`、`_determine_execution_status`、`agent_node(_run_agent max_turns)` 三处统一。

## 5. 验收标准（Review 用）
1. 复现“先过第一项后耗尽”场景时：
   - `verification_json` 至少包含第一项 `passed` 明细与理由。
   - 报告详情页可见该通过项，不再显示为空。
2. 在无限工作台中：
   - 进入全部应用优先命中“应用列表”入口，不再点击热词文本作为入口。
3. 执行效率：
   - 总步数与无效动作占比较修复前下降（目标下降 20%+）。
   - 不出现“route 结束但 execution_status 仍按旧阈值误判 exhausted”的冲突。
4. 可解释性：
   - 每个验证项有理由字段；
   - 前端可查看每步“AI意图说明”。

## 6. 实施顺序
1. **P0** 预算统一函数 `_calc_budget(goal)` + 三处联动替换（3.0.1）
2. **P0** 报告一致性修复（3.1）
3. **P1** 入口负向规则 + system 强注入（3.2）
4. **P1** `NO_PROGRESS` 集合修正 + 阈值下调 + 语义冷却（3.4）
5. **P2** 可解释性增强（3.5）
6. **P3** 布局文案去误导（3.3）
7. 回归测试与对比数据输出

## 7. 回归测试建议
- 用例A：本次问题用例（无限工作台搜索计算器并计算 12+8）。
- 用例B：有两条 verification 的常规成功用例（确保双项都能落盘展示）。
- 用例C：故意制造中断/耗尽场景（验证“部分通过可保留”）。
- 单元测试（新增，防 LLM 非确定性）：
  - `_calc_budget(goal)`：不同 pages/verifications 组合的边界与 clamp 结果
  - `_determine_execution_status`：completed/exhausted/aborted 各分支（使用统一预算）
  - 报告汇总：execution_status=exhausted 时 verification 不被清空
  - 语义冷却：触发/不触发边界、冷却期恢复行为
- 观测指标：
  - `execution_status`、`test_verdict`、`verification_json` 内容
  - 步骤数、重复动作数量、误点击次数
  - 前端步骤“意图说明”可见性
- 基线对比：
  - 选一条历史成功 run（`exec=completed verdict=passed`）作为金标准，确保无回归。

## 8. 风险与注意事项
- 历史数据可能存在旧格式（`verification_json=[]`），前端需兼容空值与旧字段。
- 路径/控件特征在不同机型可能变化，`path` 仅做弱约束，避免过拟合。
- 可解释性文本需要节流与折叠，防止日志/页面噪声过大。
- 修改预算后需重点关注“短任务是否被过度放宽”与“复杂任务是否提前截断”两类反向回归。

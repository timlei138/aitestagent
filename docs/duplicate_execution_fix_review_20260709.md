# 重复执行问题修复方案（Review 终版）

## 1) 评审结论（已采纳）

1. **单一事实源**
   - 不新增 `TestState.verification_status`。
   - 统一使用 `ctx._verifications` 作为 verification 状态源。
   - History 注入也从 `ctx._verifications` 读取。

2. **收敛时机调整**
   - 不在 Agent 决策前强制 `report_done`。
   - 改为：在 `route_after_agent` 中优先检查 `all passed`，满足即返回 `"reporter"`。
   - 这样可保证最后一轮 `assert_verification` 证据已写入 `ctx._verifications` 后再收敛。

3. **避免保护逻辑重叠**
   - 不新增“动作序列断路器”。
   - 继续使用现有 `_LOOP_BREAK_CONSECUTIVE + 语义冷却`。
   - Item 5 降级为埋点观测项。

---

## 2) 修订后的改动点

## P0（必须上线）

1. **verification 稳定键 + `assert_verification` 幂等去重**
   - 为 `goal.verification` 分配稳定键（`v0/v1/...`），去重按稳定键而非原始文本。
   - 若同一稳定键已 `passed`，再次上报返回 `duplicate_ignored`。
   - 不重复计数，不影响已完成状态。
   - 返回首通过信息（时间/轮次）便于日志分析。

2. **History 注入“已通过验证项”**
   - 在每轮提示中追加：
   - `已通过验证: [i] condition ...`（来源：`ctx._verifications`）
   - 目的：降低长链路下的记忆漂移与重复执行。

3. **后置收敛路由**
   - 在 `route_after_agent` 顶部检查是否 `all passed`：
     - 是 -> `reporter`
     - 否 -> 继续 `agent`
   - 再走已有 `status` / `iteration budget` 判定分支。

4. **埋点（最小闭环）**
   - `duplicate_assert_count`
   - `verification_progress`（pending/passed/failed）
   - `post_agent_autofinish_count`

---

## P1（建议）

5. **仅加观测，不加新断路**
   - 增加重复动作相关埋点，不引入新控制分支。
   - 避免与现有断路器叠加导致维护复杂。

6. **LLM 400（tool_call 配对）问题按阈值升级**
   - 先统计 `tool_call_id` 未配对导致 400 的 run 占比；
   - 当占比超过阈值（建议 2%~5%）再升级为 P0 修复项；
   - 低频场景先保留观测，不抢占主链路资源。

---

## P2（高级工程建议）

7. **工具状态机回归测试**
   - 新增最小回归集：
     - 重复 `assert_verification` 不重复计数
     - 最后一条 verification 刚通过时，下一跳必须 reporter
     - 模拟 `tool_call_id` 缺失时恢复后不重放已完成项

8. **可观测性看板指标**
   - `duplicate_assert_count`
   - `verification_progress`（pending/passed/failed）
   - `tool_call_400_rate`
   - `post_agent_autofinish_count`

---

## 3) 具体代码修改建议（文件级）

以下为建议改动位置（便于直接开工）：

1. **`tools/__init__.py` -> `assert_verification(...)`**
   - 新增稳定键解析逻辑（优先命中 `ctx._verification_key_map`）。
   - 写入记录结构增加键字段：`{"key": "v0", "item": "...", "result": "...", ...}`。
   - 若同 key 已 `passed`，直接返回：
     - `DUPLICATE_IGNORED: v0 already passed at step=...`

2. **`agents/graph.py` -> `agent_node(...)`**
   - 在组装 prompt 前，从 `ctx._verifications` 读取已通过项，追加到 `hist_str`：
   - `已通过验证: [v0] xxx; [v1] yyy`
   - 在每轮将当前 goal 的 verification 规范化后写入 `ctx._verification_key_map`（若不存在则初始化）。

3. **`agents/graph.py` -> `route_after_agent(state)`**
   - 在现有 `status`/budget 判断前新增：
     - 先调用 `get_tool_context()` 获取 `ctx`，从 `ctx._verifications` + `state.goal_description.verification` 计算 `all_passed`
     - 若 true，直接 `return "reporter"`
   - 其余分支保持不变。
   - 实现注意：
     - `route_after_agent` 当前签名仅有 `state`，必须在函数内显式读取 `ToolContext`；
     - 若遗漏 `get_tool_context` 导入/调用，容易出现 `NameError` 或始终无法命中 `all_passed`。

4. **`agents/graph.py` -> `_collect_verification_results(goal)`**
   - 按稳定键归并结果（同 key 只保留一次最终态，`passed` 优先级最高）。
   - 输出顺序按 `goal.verification` 原始顺序，保证报告稳定。

5. **测试建议**
   - `tests/test_graph_budget_and_reporting.py`
     - 新增：`route_after_agent` 在 all passed 时返回 reporter。
   - `tests/test_graph_budget_and_reporting.py` 或新增 `tests/test_assert_verification_dedupe.py`
     - 新增：同一 key 重复 passed 不重复计数。
     - 新增：condition 文本轻微变化但同 key 时仍可去重。
   - `tests/integration/test_golden_runs.py`
     - 新增：计算器双算式场景不再重复执行已通过 verification。

---

## 4) 最小可上线范围（推荐）

先上线 **P0: 1 + 2 + 3 + 4**（稳定键去重 + History 注入 + route 收敛 + 基础埋点）。  
这是最小改动且能直接压住“重复执行”问题的组合。

---

## 5) 验收标准

- 同一 verification 条件不会重复计入 `passed`；
- 最后一条 verification 通过后，流程在下一跳进入 reporter；
- condition 文本轻微变化时仍能按稳定键去重；
- 重复执行问题可被埋点量化（可观测、可回归）。

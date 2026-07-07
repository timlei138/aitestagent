# RAG执行差距修复计划（基于2026-07-03最新Review）

## 1. 目的与范围
本计划用于闭环最新一轮执行Review中暴露的核心差距，并与既有 `Plan v3` 对齐。

范围内：
- P0.3 断路器有效性（以最小改动拦截空转）
- P1.1 全局知识过滤有效性（从“规则设计正确”升级为“泄漏率达标”）
- max_turns 基础公式收紧（减少无效空转）
- 观测与验收口径补全（确保日志可判定）

范围外：
- 设备驱动稳定性
- 前端UI重构

## 2. 与上次总结的对比（Delta）
一致结论：
- P0.1/P0.2 工作正常，400错误已显著下降，unsupported识别链路生效。
- P0.4 基本达标，`budget_violation_count=0`。
- 主要风险仍在循环收敛与RAG相关性。

新增/更精细结论（本次Review补充）：
- P0.3不是“未接入”，而是“仅能抓精确重复”，对多样化无效操作失效。
- P1.1不是“偶发泄漏”，而是存在高比例无关全局知识泄漏（80%+）。
- 当前 `max_turns=78` 对该类任务过宽，放大了断路器失效成本。

## 3. 根因拆解
### 3.1 断路器失效根因
- 当前签名 `name|args_json|page_sig` 对“不同元素点击”过敏，导致每轮签名不同。
- 仅用 `len(set(recent[-3:])) == 1` 判定重复，无法识别“页面不变但动作在乱跳”的空转。

### 3.2 全局知识泄漏根因
- `_global_rule_relevance` 对泛化词（如“系统”）给分后，低相关规则仍可进入注入链路。
- 过滤阈值与规则质量分层联动不足（缺少硬门槛 + 质量权重叠加）。
- 观测上未输出每条规则的打分明细，难以快速定位误召回来源。

### 3.3 turn预算偏宽根因
- 线性公式系数偏大，导致 MAX_TURNS 兜底过晚。

## 4. 修复方案（按优先级）
## P0A. 最小断路器（轻设计）
### 方案A（实施）
- 保留现有“精确重复检测”逻辑（原 A3，不重写）。
- 新增 `_no_progress_count` 计数器：
  - 每轮工具后若本轮工具不是 `assert_verification`，`_no_progress_count += 1`；
  - 只要调用 `assert_verification`，计数器清零；
  - 当 `_no_progress_count >= N`（建议 N=8）时提前终止。
- 终止时写入 `loop_break_reason=no_progress`。

说明：
- 不再引入 A1/A2 的额外判据，避免继续叠加补丁链。

## P1A. 全局知识过滤收紧
### 方案B2：阈值提升与兜底（先行）
- `_GLOBAL_RULE_MIN_SCORE` 从 2 提升到 3。
- 若全局规则全被过滤，允许回退到“仅app专属 + seed规则”，不强行补全全局规则。

### 方案B3：日志可观测性补齐（与B2同批）
每条被检索规则输出：
- `rule_id`, `source_scope`, `score_detail`, `drop_reason`（若被过滤）

### 方案B1：两段式门控（暂缓）
- 暂不纳入本轮交付。
- 仅当 B2+B3 后仍无法将泄漏率压到目标阈值，再进入下一轮设计。

## P1B. max_turns 收紧
### 方案C1：基础公式收紧（实施）
- 从 `18 + page*10 + verification*10`
- 调整为 `12 + page*7 + verification*6`

## 5. 实施顺序（建议2天）
Day 1（先止血）：
1. 落地最小断路器：`精确重复 + _no_progress_count`（P0A）。
2. 全局规则阈值提升（B2）+ 评分明细日志（B3）。
3. 收紧 max_turns 基础公式（C1）。

Day 2（提质）：
1. 回放图库场景，产出对比报告。
2. 若泄漏率仍不达标，再评估 B1 两段式门控是否立项。

## 6. 验收标准（替换“仅代码完成”）
### 6.1 断路器有效性
- 在同类图库多选任务中：
  - 触发 `loop_break_reason=no_progress` 或精确重复断路的比例 > 0（说明检测生效）；
  - 平均turn较基线下降 >= 35%；
  - 无需等到 MAX_TURNS_EXHAUSTED 才停止的比例 >= 80%。

### 6.2 RAG相关性
- 无关全局规则注入占比 < 20%。
- 每次注入日志中可见每条规则评分明细与过滤原因。

### 6.3 预算与稳定性
- `budget_violation_count=0` 保持。
- 多模态400错误维持低位（较基线下降 >= 95%）。

## 7. 需要新增的日志字段
已存在（上轮已接入 step_history，当前以“补齐语义”为主）：
- `loop_detected`
- `loop_break_reason`
- `loop_break_action`

需新增（本计划新增）：
- `rule_score_detail`
- `rule_drop_reason`

不新增（本轮明确不做）：
- `stall_window` / `last_progress_turn` / `progress_score`
- `turn_budget_before` / `turn_budget_after`

## 8. 回滚与灰度
- 本轮不引入 feature flag。
- 验证失败时直接 `git revert` 回退最小提交。
- 保持单一执行路径，避免组合状态测试爆炸。

## 9. 输出产物
- 代码改动（`agents/graph.py`, `data/knowledge.py`, `agents/state.py`, `agents/orchestrator.py`）
- 运行对比报告（基线 vs 新版，至少10次样本）
- 失败样例清单（用于下一轮P2治理）

---

结论：
本轮按“轻设计优先”收敛为最小可执行修复：先用 `_no_progress_count` 解决空转，再用 B2+B3 解决泄漏，再收紧 C1 控制回合上限；其余增强项延后到有证据再做。

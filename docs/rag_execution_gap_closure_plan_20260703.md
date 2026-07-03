# RAG执行差距修复计划（基于2026-07-03最新Review）

## 1. 目的与范围
本计划用于闭环最新一轮执行Review中暴露的核心差距，并与既有 `Plan v3` 对齐。

范围内：
- P0.3 断路器有效性（从“精确重复检测”升级为“停滞检测+进展检测”）
- P1.1 全局知识过滤有效性（从“规则设计正确”升级为“泄漏率达标”）
- max_turns 动态预算策略（减少无效空转）
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
- 线性公式系数偏大，且无“早停惩罚/停滞惩罚”机制。
- 预算与风险不联动：即使检测到长时间无进展，也不会收紧剩余回合。

## 4. 修复方案（按优先级）
## P0A. 双层断路器（替代单一精确重复）
### 方案A1：页面停滞断路器（新增）
触发条件（建议）：
- 最近 `N=8` 次工具执行 `page_sig` 完全相同；
- 且没有出现以下“进展信号”：
  - 页面签名变化；
  - 命中目标验证工具（`assert_verification`）或DONE；
  - 命中明确导航进展（如从列表页到详情页）。

触发动作：
- 立即提前终止内层 `_run_agent`；
- 写入 `loop_break_reason=page_stall`；
- 同步记录：`stall_window`, `last_progress_turn`, `stall_page_sig`。

### 方案A2：多样化空转检测（新增）
触发条件（建议）：
- 最近 `M=10` 次动作覆盖元素数 >= 6（说明在“乱点”）；
- 且 `page_sig` 变化次数 <= 1；
- 且无验证进展。

触发动作：
- `loop_break_reason=action_churn_without_progress`。

### 方案A3：保留原精确重复检测（保底）
- 原逻辑继续保留，作为最小成本防线。

## P0B. 进展状态机（支撑断路器）
新增轻量状态：
- `progress_score`（每轮更新）
- `last_progress_turn`
- `verification_attempted`（是否调用过断言）

进展加分示例：
- 页面层级向目标页靠近 +2
- 成功进入多选模式 +3
- 触发验证工具 +3

停滞扣分示例：
- 同页重复点击且无页面变化 -1
- 取消/反取消来回切换 -2

当 `progress_score` 连续低于阈值时，可提前触发早停。

## P1A. 全局知识过滤收紧
### 方案B1：两段式门控
第一段（硬过滤）：
- 若规则文本与 `app_package/app_name/domain/scenario` 均不匹配，直接淘汰。
- 泛化词（如“系统”、“设置”）不单独构成通过条件。

第二段（软排序）：
- 综合分：
  - app/domain命中权重（高）
  - user_request token命中权重（中）
  - quality_score（中）
  - freshness（`last_verified_at`，中）

### 方案B2：阈值提升与兜底
- `_GLOBAL_RULE_MIN_SCORE` 从 2 提升到 3（先灰度）。
- 若全局规则全被过滤，允许回退到“仅app专属 + seed规则”，不强行补全全局规则。

### 方案B3：日志可观测性补齐
每条被检索规则输出：
- `rule_id`, `source_scope`, `score_detail`, `drop_reason`（若被过滤）

## P1B. max_turns 动态收缩
### 方案C1：基础公式收紧
- 从 `18 + page*10 + verification*10`
- 调整为 `12 + page*7 + verification*6`

### 方案C2：风险联动收缩
- 若触发“停滞预警”达到2次，则剩余turn上限直接乘以0.6。
- 若触发“多样化空转预警”达到1次，强制进入最后验证或终止分支。

## 5. 实施顺序（建议2天）
Day 1（先止血）：
1. 落地页面停滞断路器 + 空转检测（P0A）。
2. 接入进展状态机最小字段（P0B）。
3. 增加 loop/stall 观测日志字段。

Day 2（提质）：
1. 全局知识两段式门控 + 阈值灰度（P1A）。
2. max_turns 新公式 + 风险联动收缩（P1B）。
3. 回放图库场景，产出对比报告。

## 6. 验收标准（替换“仅代码完成”）
### 6.1 断路器有效性
- 在同类图库多选任务中：
  - 触发 `loop_break_reason=page_stall|action_churn_without_progress` 的比例 > 0（说明检测生效）；
  - 平均turn较基线下降 >= 35%；
  - 无需等到 MAX_TURNS_EXHAUSTED 才停止的比例 >= 80%。

### 6.2 RAG相关性
- 无关全局规则注入占比 < 20%。
- 每次注入日志中可见每条规则评分明细与过滤原因。

### 6.3 预算与稳定性
- `budget_violation_count=0` 保持。
- 多模态400错误维持低位（较基线下降 >= 95%）。

## 7. 需要新增的日志字段
- `loop_detected`
- `loop_break_reason`
- `loop_break_action`
- `stall_window`
- `last_progress_turn`
- `progress_score`
- `rule_score_detail`
- `rule_drop_reason`
- `turn_budget_before`
- `turn_budget_after`

## 8. 回滚与灰度
- 通过feature flag逐项灰度：
  - `loop_stall_breaker_enabled`
  - `rag_global_rule_strict_filter_enabled`
  - `dynamic_turn_budget_enabled`
- 任一指标恶化即单项回滚，不影响其它已验证能力。

## 9. 输出产物
- 代码改动（`agents/graph.py`, `data/knowledge.py`, `agents/state.py`, `agents/orchestrator.py`）
- 运行对比报告（基线 vs 新版，至少10次样本）
- 失败样例清单（用于下一轮P2治理）

---

结论：
本次差距不是“功能未实现”，而是“护栏策略粒度不够 + 观测闭环不完整”。优先补齐停滞断路与全局规则硬过滤，才能把Plan从“代码完成”推进到“效果达标”。

# RAG 相关性优先策略：现状分析与优化报告（2026-07-08）

> 角色视角：Agent 高级开发工程师  
> 目标：让 LLM 接收到“更少干扰 + 更高相关”的信息，提升执行稳定性与可解释性。

---

## 1. 结论摘要（Executive Summary）

基于当前代码实现，系统已经具备了**知识分层与基础治理能力**（全局规则 vs App 规则、scope 自动补齐、知识增删改防误删），但在 Planner/Agent 执行路径上仍属于**“预注入型 RAG”**：  
- Planner 阶段会拼接 RAG；  
- Agent 每轮也会拼接 RAG；  
- 再叠加 Prompt 中若干场景强约束。  

这会导致：当任务场景不需要某些知识时，仍然持续占用上下文预算，增加干扰。

**总体判断**：  
- 数据层：✅ 基本符合”按类型、按范围管理知识”  
- 执行层：⚠️ 尚未完全符合”涉及到才取知识”（Planner/Agent 仍为预注入模式）  
- 提示词层：✅ 场景硬编码已清零，agent.txt 仅含通用行为规则

---

## 2. 现状分析（按链路）

## 2.1 前端添加知识 → API → KnowledgeBase

### 已实现的正确点
- `api/knowledge_routes.py`
  - `curated_rule` 走 `save_curated_rule` 路径（统一校验）。
  - 空 `scope` 自动推断：
    - `app_package != ""` => `scope=app`
    - `app_package == ""` => `scope=universal`
- `data/knowledge.py`
  - `save_knowledge` 对 `curated_rule` 做 scope 防御性补齐。
  - metadata 已做清洗，避免空 list（如 `applicable_domains=[]`）触发 Chroma upsert 错误。

### 工程价值
- 保障“全局知识/应用知识”不会因前端字段缺失而失效；
- 降低知识入库不一致导致的“僵尸规则”概率。

---

## 2.2 Planner 阶段

- `agents/graph.py::planner_node`
  - 调用 `_rag_ctx(kb, state.app_package, user_request)`；
  - 将 RAG 塞入 `planner.txt` 输入（`rag_context`）。

### 风险点
- Planner 阶段对任务仍是粗粒度语义，尚未进入页面状态与操作分叉；
- 此时注入过多规则，可能提前把模型导向某条路径，影响后续自主探索。

---

## 2.3 Agent 阶段

- `agents/graph.py::agent_node`
  - 每轮都构建 `Goal + Page + History + RAG`，并追加到 HumanMessage。
  - RAG 内容来自 `_rag_ctx`（人工知识 + 操作经验）。
- `tools/__init__.py::query_app_knowledge`
  - 已有“按需查询工具”，但当前更多是“补充查询”，不是主路径。

### 风险点
- 每轮重复注入 RAG，信息冗余；
- 当规则与当前页面无关时，仍占 token 并增加选择噪声；
- 若 Prompt 里又有场景强约束，模型会面临多重约束叠加。

---

## 2.4 Prompt 角色边界

- `agents/prompts/agent.txt`
  - 通用执行规则（应保留）：
    - 执行前置检查（”hints 中有前提条件必须先验证”）；
    - 元素选择优先级（rid > role+label > path）；
    - 验证流程、错误流程、规则。
  - **已移除**场景型强约束（无限工作台切换流程等已迁入 RAG curated_rule）。
  - 设计原则：Prompt 负责 **WHEN**（何时该检查），RAG 负责 **WHAT**（具体怎么操作）。

### 当前状态
- ✅ 场景硬编码已清零。agent.txt 不再包含任何 App/场景特例。
- ✅ RAG 中已有对应全局知识（scope=universal），当前通过 Planner + Agent 每轮预注入方式提供；按需检索能力由 `query_app_knowledge` 工具提供但非当前主路径。

---

## 2.5 已实施的防御性修复（2026-07-08）

### API 层
- `api/knowledge_routes.py`：`curated_rule` 统一走 `_save_entry` → `save_curated_rule`，`scope` 自动推断（空 package → universal，非空 → app），`quality_score` 异常 → 400。
- PUT 更新改为 `old_entry_id` 精准定位 + `get_by_metadata` 精确匹配（多匹配拒绝 409），消除误删风险。
- 前端已传 `old_entry_id`，优先走精准删除路径。

### 知识库数据修复（环境验证结论，以当前库为准）
- 两条僵尸规则（无限工作台入口规则、桌面模式切换规则）已修复 `scope: “universal”`，`query_curated_rules` 恢复正常检索。

### Agent 层
- `agent_node` 的 `_rag_ctx` 已改为 `goal.app_package || state.app_package` 双 fallback。
- 热词过滤 `_should_skip_hotword_element` 当前阈值为 8，可覆盖较短热词标题（如”孙颖莎给勒布伦打趴下了” 11 字符）。
- `agent.txt` 场景硬编码移除，新增通用”执行前置检查”规则（不指向具体场景）。

### 预算与防浪费（`agents/graph.py`）
- `_calc_budget(goal)` 统一三层预算（工具总上限/迭代上限/每轮工具数），三处联动消除 magic number。
- `_NO_PROGRESS_LIMIT` 16→8，`_NO_PROGRESS_ACTIONS` 移除误伤项（launch_app 等）。
- 语义冷却 + FINALIZATION_HINT + 软上限检查 已上线。

---

## 3. 与目标原则的符合度评估

原则：**”涉及到了才从 RAG 获取对应知识，而不是直接塞一堆强约束。”**

- 知识入库与分层：**高符合** ✅ （scope 自动推断、API 校验、僵尸数据已修复）
- 执行时检索策略：**中低符合** ⚠️ （当前偏预注入，每轮仍重复注入 RAG）
- Prompt 去场景化：**高符合** ✅ （场景硬编码已清零，agent.txt 只有通用规则）

---

## 4. 优化方案（建议架构）

## 4.1 设计原则

1. **最小必要上下文**：默认只给通用规则 + 当前页结构。  
2. **事件触发检索**：发生“场景命中”或“不确定”时，再查 RAG。  
3. **分层责任清晰**：  
   - Prompt：通用行为规范；  
   - RAG：场景/应用知识；  
   - Tool：运行期按需查询与验证。

---

## 4.2 目标状态（To-Be）

### Planner
- 仅注入轻量信息：
  - 全局通用规则摘要（少量）；
  - 与用户请求显式相关的高置信规则（top-k 小）。
- 不注入大量 App 细节路径。

### Agent
- 默认不每轮注入完整 RAG，仅注入：
  - Goal + Page + History + 通用规则短摘要；
- 触发条件命中时才调用 `query_app_knowledge`：
  - 进入新 App；
  - 页面连续无进展/冲突；
  - 目标元素定位不稳定；
  - 发现“模式切换”类关键词（如桌面模式）。

### Prompt
- 移除场景特例硬编码（如某一模式固定流程）；
- 保留通用安全/验证规范；
- 场景路径交由 RAG 与运行时检索决定。

---

## 5. 分阶段落地计划

## Phase 1（低风险，建议先做）

1. **Agent RAG 降载**
   - `agent_node` 只在首轮注入精简 RAG，后续轮次不重复塞；
   - 通过状态位记录 `rag_injected_once`。
2. **触发式查询收口**
   - 由框架层在特定触发条件下自动建议/注入 `query_app_knowledge`。
3. **Prompt 去特例** ✅ 已完成（2026-07-08）
   - ~~把 `agent.txt` 场景硬编码迁移到 curated_rule。~~
   - 场景特例已全部移除，新增通用"执行前置检查"规则。
   - 对应 RAG curated_rule 已补充 scope=universal，通过 Planner hints + Agent _rag_ctx 按需注入。

## Phase 2（中风险，收益大）

1. **检索路由器（RAG Router）**
   - 基于 `goal + page_signature + action_history` 选择检索域：
     - universal / app / experience。
2. **证据打分与裁剪**
   - 仅保留高相关规则（限制条数 + 字数预算）。

## Phase 3（持续优化）

1. **离线评估集**
   - 对比“预注入 vs 触发注入”的成功率、步数、误点率。
2. **在线指标闭环**
   - 每次 run 落库 RAG 命中来源与最终效果，做策略迭代。

---

## 6. 可量化验收指标（KPIs）

1. **平均工具调用步数**：下降（目标 -15% ~ -25%）  
2. **无关点击率**：下降（目标 -30%）  
3. **RAG token 占比**：下降（目标 -40%）  
4. **首个验证项达成时间**：缩短（目标 -20%）  
5. **“步骤耗尽但已有部分验证通过”漏报率**：接近 0

---

## 7. 实施注意事项

1. 不建议一次性全量切换，先灰度：
   - 配置开关：`rag_strategy = preload | hybrid | on_demand`
2. 所有策略变更需有回归：
   - 关键场景：无限工作台切换、搜索入口识别、跨页面验证回传。
3. 保留异常兜底：
   - 当按需检索失败时，允许一次轻量全局规则补充，而不是无限重试。

---

## 8. 最终建议（管理层可读）

当前系统已经具备“知识可治理”的基础，但执行态仍偏“信息前置堆叠”。  
下一步应从“规则写死”转向“运行时检索决策”，让 Agent 在需要时才引入对应知识。  

这会更符合你提出的核心原则：  
**给 LLM 更少的干扰信息，给更高密度的相关信息。**


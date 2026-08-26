# Code Review 发现问题修复 Plan（2026-08-22）

> ⚠️ **本文档已 SUPERSEDED**：规划职能已于 2026-08-24 并入 [`agent_evolution_plan_20260824.md`](./agent_evolution_plan_20260824.md)，请以新文档为唯一活跃规划。本文件仅保留作为历史设计细节来源（如 §F2 完整修法、前端适配一等任务原表述），不再独立推进。
> 日期：2026-08-22
> 状态：~~评审通过，可推进第零批~~ → **已取代（2026-08-24）**
> 来源：v1 分支全量代码 + docs 通读后的独立评审（高级 Agent 开发 + 高级测试开发双视角）。
> F 系列为判定/规划层问题；R 系列为回放链路（direct/guided 模式机）问题，
> 针对「回放应快速重放同一用例、LLM 调用与时间大幅减少」的产品目标。
> 原则校准：契约收敛、补丁发散；代码管"稳"、LLM 管"活"；权威 ⟺ 确定性反证。
> 注：docs 中已登记的待办（plan 沉淀→guided 端到端验收、legacy 下线 L2/L3、耗时优化 P2/P3）不在此重复。

---

## 0. 问题总表（按价值排序）

| ID | 问题 | 影响 | 分级 | 位置 |
|---|---|---|---|---|
| F1 | 状态类 clause 无"强制确定性证据"的代码不变量 | 误判 passed（v5 类复发风险） | **P0** | `agents/verification.py` |
| F2 | `list_count` 全树子串计数 → 权威 FAIL 误报 fail-fast | 误停 run | **P0** | `agents/verification.py:681-848` |
| F3 | clause 机械拆分碎片化（逗号切散枚举） | unknown 爆炸 / 冗余验证通道 | **P1** | `agents/verification.py:14` + planner |
| F4 | planner 输出仍靠正则解析，结构化输出未接 | spec 映射质量地基脆 | **P1** | `agents/nodes.py:_parse_goal` |
| F5 | Agent 自身缺录制回放回归体系 | 迭代靠真机手测，安全垫薄 | **P1** | 新建基建 |
| F6 | 小项：execution_status 兜底过松 / 双 RPC / prompt 补丁化倾向 | 质量/性能/成本 | **P2** | 多处 |

### 回放链路（R 系列）

> 背景：产品语义上"回放 = 快速把同一条用例重新操作一遍，LLM 调用和时间大幅减少"。
> 当前 direct/guided/explore 模式机代码在位（沉淀/匹配/执行三层均已实现并有单测），
> 但真实 rerun 全部落在 `no_matching_plan` → explore（time_optimization 文档两份 trace 实证）。
> 结论：**不是没沉淀，是沉淀了也对不上、对上了也开不快**。根因 R1-R3 如下。

| ID | 问题 | 影响 | 分级 | 位置 |
|---|---|---|---|---|
| R1 | 匹配锚点要求 planner 输出逐字一致（fingerprint 硬门禁） | 回放从未生效的主因 | **P0** | `agents/plan_extractor.py:434` + `agents/rag_context.py:96` |
| R2 | direct 准入漏斗过长（6 条件 + 隐藏 API 人工批准） | 最快第 3 次跑才提速；guided 不省 LLM | **P0** | `agents/nodes.py:1875` |
| R3 | direct 动作耗尽后仍付一整轮 agent LLM 会话收尾验证 | 回放 LLM 无法归零 | **P0** | `agents/nodes.py:1974` |
| R4 | direct 执行质量：存量 index 漂移 / 3×RPC / activity 全名精确匹配 | 重放脆弱、误降级 | **P1** | `nodes.py:2008,2022,2040` |
| R5 | 沉淀保留全部成功动作（含绕路试错链） | 回放原样重演弯路 | **P2** | `agents/plan_extractor.py:510` |
| R6 | 环境键口径：匹配侧用启动前前台 app，沉淀侧用首动作前页面 | 弱信号，跨桌面误判 | **P2** | `agents/nodes.py:1791`（注释自认） |

---

## F1（P0）：判定层缺"状态类 claim 强制确定性证据"不变量

### 现状与根因

- `build_verification_contract` 给所有 clause 统一挂 `_ALL_CHANNELS_FALLBACK`
  （`agents/verification.py:204`），等于全通道放行。
- `evaluate_verification` 里 `positive = any(PASS/YES)`（`agents/verification.py:79-82`），
  对带 spec 的 clause 也接受 `click_and_check` / `visual_check` 等自由通道的 PASS。
- discrepancy_detection_core_plan §6 要点3「状态类 claim 必须存在确定性断言证据，
  缺失时禁止判 passed」**未在 evaluator 落地**——目前只靠 `agent_common.txt` 的
  prompt 约束（「状态类 claim 必须用专用谓词验证」）。prompt 是软约束，
  模型绕过即复发 v5 类误判。

「结束只能靠证据」是代码不变量，「什么证据算数」也应该是。

### 修法（契约式，不重新引入关键词推断）

- **只对带 spec 的 clause 收紧**：其有效证据通道收窄为 `{_spec_channel(spec.predicate)}`
  （写侧已有该函数，读侧复用同一映射，单一来源）。spec:null 的 clause 保持全通道
  回退不动——保住覆盖率，不做关键词猜测（M1-4 刚删掉的 `_default_channels_for_claim`
  不以任何形式回来）。
- `evaluate_verification` 中 matching 集合按上述通道集过滤（authoritative 事件仍无视
  通道直入，保留现有优先级链不变）。
- 效果：spec=element_disabled 的 clause 只能被 `element_state` 证据判 passed；
  visual_check/click_and_check 的 PASS 对它无效。

### 涉及文件

- `agents/verification.py::build_verification_contract`（spec clause 的 channels 由
  `_ALL_CHANNELS_FALLBACK` 改为谓词映射通道）
- `tests/test_verification_contract.py`（新增用例）

### 验收标准

1. 带 `element_disabled` spec 的 clause：仅注入 `vision_verify` PASS → 结果 unknown；
   注入 `element_state` PASS → passed；注入 `element_state` authoritative FAIL → failed。
2. spec:null clause 行为与现状完全一致（回归不破）。
3. 现有 291 测试全绿。

### 盲区与开闸门禁（2026-08-22 外部评审补充）

- **覆盖不到主战场**：v5 类误判多发生在 `spec:null` 的 clause——planner 不填 spec 时，
  自由通道 PASS 照样放行，F1 只保护已填 spec 的那部分。因此 F1 必须与 F4 联动
  （F4 提前至同批），先把 spec 填充率抬上去，收窄才有意义。
- **Gate-A（开闸 blocker，非缓解措施）**：F1 动码前先做只读统计——真实 run 中
  ①planner spec 填充率；②`_match_spec` 对已填 spec clause 的自动证据命中率
  （返回非 None 的比例）。命中率低时收紧通道 = 把假阳性换成更隐蔽的假阴性
  （大量 clause 卡 unknown），此时应先修填充率/匹配器，不开闸。
- **Gate-A 分母口径（2026-08-22 二次评审补充）**：`_match_spec` 返回 None 有两类
  截然不同的根因，统计脚本必须分桶——(a) 控件确实不在当前页面（真 unknown，
  合理）；(b) 谓词/参数不被匹配器支持、perceiver 结构化输出质量差（覆盖不足，
  Gate-A 真正要拦的对象）。阈值只对 (b) 类设：若混在一个分母里，「用例本就没
  走到那步」造成的假阴性会把本来安全的开闸错误拦掉。
- **spec:null 状态类 claim 的正面回应（原则冲突点，不留含糊）**：不重新引入关键词
  推断（M1-4 刚删）。路线分三层：①F4 + planner.txt 引导推高填充率（主路径）；
  ②填充率/命中率作为指标透出 trace，持续可观测；③长期填充率仍偏低的状态类误判，
  **承认只能由 prompt 软约束 + evaluator 的 unverified 标记兜底并暴露给人工复核**
  ——不假装代码已经管住，把缺口诚实留在报告里。

---

## F2（P0）：`list_count` 全树子串计数误报权威 FAIL

### 现状与根因

- `_find_elements`（`agents/verification.py:681-684`）对整棵元素树做子串匹配
  （rid 叶子 / label / text / desc 任一包含 target 即命中）。
- `list_count` 分支（`:828-847`）：`n == 0` → unknown；`n != want` → **authoritative FAIL**
  → 直接触发 fail-fast 终止 run。
- 误报路径：锚点词出现在容器 label + 子元素 label（父子重复计数）、或页面多处
  含同词（如"课程节"既是标题又是列表项前缀）→ 计数偏大 → 权威 FAIL 误停。
- 对照：enabled/disabled/checked 都有 `n != 1 → None` 歧义保护，list_count 没有。

### 修法

- 计数口径收敛为**叶子元素**（无可点击/无子元素的末端节点），剔除祖先容器重复计数；
- 锚点本身匹配到多个元素（>1 个候选锚）→ 返回 None（拿不准不写，与 enabled 系一致）；
- 保持「n==0 → unknown」不对称设计不变。

### 影响面与改前基线（2026-08-22 外部评审补充）

- `_find_elements` 是**全部元素类谓词的共用入口**（verification.py 单调用点 ：739，
  element_exists/absent/enabled/disabled/checked/list_count 全走它）——「叶子口径」
  改动波及所有元素谓词，不是 list_count 的局部改动。
- 改前必做两件事：①先跑全量 pytest 留基线，不能边改边信「应该不破」；
  ②统计真实 run 中 list_count 误报 FAIL 的实际频次——若属极低频误报，需对照
  「全谓词回归风险」重新权衡是否值得动共用入口（或只对 list_count 分支做口径收敛、
  不动 `_find_elements` 本体）。

### 验收标准

1. 单测：父容器 label 含锚点词 + N 个子元素含锚点词 → count 只算叶子数；
2. 锚点多匹配 → 不写证据（unknown）；
3. 正确计数场景 FAIL/PASS 语义不变；
4. **全谓词回归**：element_exists/absent/disabled/checked 在新口径下既有用例全数通过，
   且各补一条「容器+子元素同名」用例锁定新语义。

---

## F3（P1）：clause 机械拆分碎片化

### 现状与根因

- `_CLAUSE_BOUNDARY`（`agents/verification.py:14`）按 `[，,；;]+|并且|同时|以及|且`
  机械切分 verification 陈述句。
- 枚举型文本被切碎：「勾选周一,周三,周五后显示3节课」→ 3 条碎片 clause，
  每条独立走全套验证通道 → unknown 爆炸 + 冗余步骤。
- planner.txt:60-61 专门告诫模型避免碎片化——说明踩过坑，但防线在 prompt 侧，
  代码侧机械切分原样保留，两道防线互相打架。

### 修法（拆分权交还 planner，代码只兜底）

- planner 输出升级：`verification` 项支持 `{"claim": ..., "clauses": ["...", "..."]}`
  ——planner 在规划期完成**语义拆分**（它本来就被要求理解 clause 粒度），人工在
  plan_review 可审可改（一次性修好优于执行期反复猜）。
- `_split_claims` 降级为**仅处理旧字符串项**的 fallback，且去掉 ASCII `,` 边界
  （枚举逗号不拆；仅全角标点与连接词拆）。
- 与 M4 Phase 1（结构化 spec）同向：都是「规划期声明结构，执行期不再猜」。

### 验收标准

1. 「勾选周一,周三,周五后显示3节课」经新格式输出 → 1~2 条完整语义 clause；
2. 旧字符串输入回归不破（fallback 生效）；
3. `validate_contract_spans` 适配后无 gap/overlap 误报。

---

## F4（P1）：planner 结构化输出落地

### 现状与根因

- `TestGoalOutput`（`agents/state.py:18`）已定义但未使用；`_parse_goal`
  （`agents/nodes.py:2269`）用正则从自由文本抽数值 JSON。
- M3 已统一 OpenAI 兼容接入，`with_structured_output` 的前置障碍早已不存在。
- spec 映射（M4a 最大风险项）的地基建立在最脆的解析上。

### 修法

- planner LLM 接 `.with_structured_output(TestGoalOutput)`（扩展模型加
  `verification` 的对象形式以承接 F3/M4 结构）；解析失败 fallback 到现有正则路径；
- 失败可观测：解析降级计入 trace/metrics。

### 验收标准

1. planner 单测：典型用例结构化输出字段完整（goal/app_package/verification 含 spec 对象）；
2. 故意喂畸形输出 → fallback 路径生效且留痕；
3. 真机一次 run 无 `_parse_goal` 降级日志。

### 与 F1 的联动及前端耦合（2026-08-22 外部评审补充）

- **F4 从第三批提前至第一批，与 F1 同批**：F1 的通道收窄只有配合「普遍填 spec」
  才打到主战场（见 F1 盲区节）。planner 输出升级为对象形式后，
  `build_verification_contract` / `validate_contract_spans` 同步承接。
- **plan_review 前端适配是一等任务，不是附带项**：verification 从字符串变对象
  （含 clauses 数组、spec 结构）后，前端展示/编辑层必须同步改造——否则人工审的
  是旧格式、编辑写回会破坏新结构。与后端同批交付，验收包含前端用例；
  工作量按独立前端任务排期，不计入后端批次。

---

## F5（P1）：Agent 自身回归体系（录制回放集）

### 现状与根因

- 每次改动依赖「真机回放确认」，无可复放的自动化回归。
- migration §9.6 提出的三类回放集（歧义集/高频输入集/恢复集）从未建设；
  gate_check 只聚合历史指标做阈值判断，不做行为回归。
- trace JSON（`logs/runs/*_trace.json`）已高度结构化，离回放集只差采集与驱动层。

### 修法（分期）

1. **设备抽象层 mock**：`device/controller.py` / `device/perceiver.py` 接口已清晰。
   注意：`tests/test_verification_contract.py:377-399` 已有文件内局部的
   `FakePerceiver/FakeDevice`（且多处内联重复定义）——第一步是**抽取为共享测试基建**
   而非从零新建；再按 trace 里的页面快照序列扩展「回放感知结果」的能力；
2. **回放集沉淀**：从历史 passed trace 提炼「步骤 → 工具调用 → 页面快照」脚本，
   入库（歧义/高频/恢复三类目录）；
3. **CI 钩子**：核心改动（verification/tools/graph）跑回放集 diff 报告：
   verdict、clause 状态、step 数、证据通道四维对比。

### 验收标准

1. 任选 3 条历史 trace 录制后离线回放，verdict 与原 run 一致；
2. 人为注入一个 verification.py 回归 bug，回放集能红灯。

---

## F6（P2）：小项集合

| 项 | 现状 | 修法 |
|---|---|---|
| `_determine_execution_status` 兜底过松 | `len(history)>=3` 即 completed（verification.py:554），3 步崩溃也算 completed | 收窄：无 terminal_verdict/conclusion 且 step<阈值时维持 error |
| agent_node 双 `current_app()` RPC | nodes.py agent_node 内取 pkg 与喂 auto_record_evidence 各调一次 | 合并为一次复用 |
| prompt 补丁化倾向 | agent_common.txt 已出现引用具体 run 的规则（213229 seq109→110 反模式），83K token/调用是延迟大头。**注：矛盾 1-4 修复、clause 粒度规则等历史补丁属快速止血，与 F1/F3 的代码侧修复方向相反，治理时应迁移下沉而非继续新增** | 方向性治理：场景规则下沉到工具契约（报错+示例模式，如 toggled 校验），prompt 只留通用契约；每次加 prompt 规则前先问能否做成工具错误信息 |

---

## R1（P0）：匹配锚点改为「契约复用」，替代重新规划

### 现状与根因

- `verification_fingerprint`（`agents/plan_extractor.py:434-489`）对 **planner 本次
  运行输出的 verification 文本**（statement + clause claim + channels）做 sha256；
- 检索侧硬门禁要求与候选 plan 的指纹**完全相等**（`agents/rag_context.py:96-102`，
  不等直接 `continue`）；
- planner 是带温度的 LLM：同一用例两次运行，验证项措辞稍有差异
  （"日期为月/日/年" vs "日期显示为月/日/年格式"），哈希必然不同 → 候选被过滤 →
  `no_matching_plan`。**回放链路的身份锚建立在 LLM 输出逐字稳定这个不成立的前提上。**

### 修法（契约由人审、代码保复用一致——对齐 §0 原则）

1. **检索前移**：`planner_node` 之前先按 `user_request` 向量召回候选 task_plan
   （复用 `kb.query_task_plan_summaries`，此时只做召回不做指纹过滤）；
2. **高置信命中 → 跳过 planner LLM**：把存储 plan 的 `verification_contract_json`
   **原文**作为提案直接送入 plan_review（人工审批契约的环节本来每轮都存在，
   一次审批同时覆盖「用例理解」与「计划复用」两个决策）；
3. 低置信 / 无命中 → 现状路径不变（planner 正常跑）。
4. 契约原文复用后指纹自然逐字相等，R2/R3 的 direct/guided 匹配随之打通。

### 涉及文件

`agents/nodes.py::planner_node`（前置检索 + 条件跳过）、`mode_selection_node`、
`frontend` plan_review 展示来源标记。

### 验收标准

1. 同一用例第二次运行：plan_review 显示历史契约提案（标注来源=reused plan），
   planner LLM 调用为 0；
2. approve 后 `mode_selection_reason` ≠ no_matching_plan（进入 guided/direct 判定）;
3. 用户在 plan_review 修改契约 → 走现状路径（重新规划），不误用旧 plan。

---

## R2（P0）：显式回放意图解锁 direct

### 现状与根因

direct 准入需 6 条件同时满足（`agents/nodes.py:1875-1882`）：人工调用隐藏 API
`/api/execution_plans/{id}/direct-approval` + 全动作 direct_eligible +
env_score==1.0 + success_count≥2 + postcond≥0.8 + quality≥0.5。最短路径：
explore 过 → guided 过 → 人工批准 → 第 3 次 direct。而 **guided 本身不省 LLM**
（nodes.py:611-640 仅向 system 注入计划 JSON，仍是完整 agent 循环）。
用户点"回放"是明确的授权动作，不该再走三轮保守阶梯。

### 修法

- `RunRequest` 增加 `replay: bool`（前端报告页加「回放」按钮）；
- `replay=true` 时准入收窄为三条硬安全条件：verification_fingerprint 相等
  （R1 保证）+ env 兼容（score≥阈值）+ 存在可执行动作；命中即进 direct，
  现有「任一动作失败立即降级 guided」护栏保留；
- 非 replay 的隐式重跑维持现有保守阶梯不动。

### 涉及文件

`api/server.py`（RunRequest）、`agents/nodes.py::mode_selection_node`、
`frontend/spa/src`（报告详情页按钮）、`api/websocket_manager.py`（如需透传）。

### 验收标准

1. 已沉淀 plan 的用例带 `replay=true` 重跑：首跑即 `execution.mode=direct`；
2. 人为破坏 precondition（切到错误页面再回放）：首个动作即降级 guided，run 不失败；
3. 不带 replay 标志的普通重跑行为与现状完全一致（回归不破）；
4. replay 的提速指标以 Gate-A 达标（spec 填充率 ≥ 阈值）为前提；未达标期间
   放宽为「显著低于 explore 基线」，不让 R 批次背达不到的 KPI（详见「量化预期」
   前置耦合声明）。

---

## R3（P0）：direct 收尾零 LLM 收口

### 现状与根因

动作耗尽后 `direct_actions_exhausted → guided`（`agents/nodes.py:1974-1999`）：
导航零 LLM 重放完，验证又交回一整轮 agent LLM 循环。但 M4a 自动证据已能纯代码
判定 spec clause（perceive 后 `auto_record_evidence` + evaluator）——验证不需要 LLM
除非真有 unknown。

### 修法

- 动作耗尽分支改为：执行一次 perceive + `auto_record_evidence` 后路由 evaluator；
- verdict = passed/failed → 直接 reporter；unknown 才降级 agent 补验
  （route_after_evaluator 的 unknown 回环机制已存在，天然兜底）；
- 注意防回环：耗尽后需置显式标志（如 `_direct_finished=True` 或复用
  `_direct_downgrade_count=1`），否则 `route_after_evaluator` 的
  `execution_mode==direct` 分支会把动作从头再放一遍。
- **双护栏互斥（2026-08-22 二次评审补充）**：上述耗尽标志与 route_after_evaluator
  的 unknown 回环机制是**两套独立护栏**——若 unknown 回环触发时耗尽标志尚未置位，
  仍可能重放动作。因此耗尽标志必须在进入收尾分支时即刻置位（不等 evaluator 的
  verdict 结果），使互斥关系不依赖判定顺序；互斥逻辑以单测锁定（见验收 4）。

### 涉及文件

`agents/nodes.py::direct_node`（耗尽分支）、`agents/graph.py::route_after_evaluator`。

### 验收标准

1. 全 spec 覆盖的用例 replay：整轮 `llm_call_count == 0`、verdict 正确、
   trace 中无 guided 回退；
2. 含非 spec clause 的用例：导航零 LLM，仅收尾为补 unknown 起 agent；
3. 动作中途失败降级路径不受影响（回归不破）；
4. **末 clause unknown 压力用例**：故意构造最后一个 spec clause 为 unknown 的
   用例——验证其降级 agent 补验一次后即收敛，不会经 route_after_evaluator 的
   direct 分支把动作从头重放（`_direct_finished` 与 unknown 回环互斥由单测锁定）；
5. 第 1 条的 `llm_call_count == 0` 以 Gate-A 达标为前提（见「量化预期」前置耦合
   声明）；未达标期间放宽为「显著低于 explore 基线」。

---

## R4（P1）：direct 执行质量

| 问题 | 修法 | 位置 |
|---|---|---|
| 存量 `tool_input_json` 含全局 `index`（跨快照漂移，R2/N2 教训） | 沉淀时剥离 index，归一化为 rid/path_contains/label | `plan_extractor.extract_candidate_plan` |
| 单动作调 3 次 `current_app()` RPC | 合并为 before/after 各 1 次 | `nodes.py:2008,2022,2040` |
| precondition 要求 activity 全名精确相等 | package 必须相等 + activity 短名宽松匹配 | `nodes.py:2009-2016` |

验收：UI 轻微变化（元素增删致 index 漂移）时 replay 仍命中；单动作 RPC 3→2。

## R5（P2）：plan 蒸馏（沉淀时剪掉绕路）

沉淀保留全部成功非只读动作，包括「点错→back→重试成功」的试错链，回放原样重演。
修法：沉淀时启发式剪枝（no-op 绕路：该动作 page_after == 前一动作 page_before 且
后续动作不依赖它）；剪后产出新版本 plan（不原地改，保留原始版审计）。
验收：含已知绕路的历史 trace 沉淀后动作数明显少于原步数，蒸馏版 replay 通过。

## R6（P2）：环境键口径统一

matching 侧用运行起点前台 app（launcher），沉淀侧用 actions[0].page_before——
`nodes.py:1791` 注释自认弱信号。修法：双侧统一为「目标 App 冷启动后首屏
package+activity」（launch_app 后采集）；activity 从 critical 字段降级或改短名比较。
验收：更换桌面 launcher 后 replay 匹配不受影响。

---

## 落地顺序（2026-08-22 外部评审后调整）

```
第零批（只读数据摸底，Gate-A/Gate-B）：
   spec 填充率 + _match_spec 自动证据命中率（None 按「控件缺失类 / 谓词或结构
   不支持类」分桶统计，阈值只对后者设）+ list_count 误报频次统计
   （不改任何代码；结果决定 F1 是否开闸、F2 是否动共用入口）

第一批（正确性护栏 + spec 填充联动）：
   F4 结构化输出（含 plan_review 前端适配，一等任务）
   → Gate-A 达标后开闸 F1 通道收窄
   → F2 计数口径（改前跑全量 pytest 留基线，改后全谓词回归）

第二批（回放开闸，当前产品主线）：R1 → R2 → R3   → verify: 同类用例二跑 direct、LLM≈0

第三批（clause 拆分权交还）：F3（复用 F4 的对象输出格式）

第四批（迭代速度基建）：      F5 分期             → verify: 3 条 trace 回放一致

第五批（随手）：              F6 小项 + R4/R5/R6  → verify: 对应单测
```

依赖与调整说明：

- **F4 提前与 F1 同批**（评审意见）：F1 只保护已填 spec 的 clause，不先抬填充率
  就收窄通道 = 打不到主战场。Gate-A 未达标时 F1 不开闸，先修填充率/匹配器。
- **F2 前置基线**（评审意见）：`_find_elements` 为全谓词共用入口，改前必须留全量
  测试基线；若 Gate-B 显示误报极低频，考虑只在 list_count 分支收敛口径、不动本体。
- **F3 依赖 F4** 的对象输出格式，随其后单独一批（前端适配已在第一批交付）。
- **R1 仍是 R2/R3 前置**；R 批次与 F 批次相互独立，可由不同改动序列并行推进，
  但同一文件（nodes.py）注意合并顺序。
- 实施前校准文中行号（以函数名 + rg 搜索为准）。

### 量化预期（对照 time_optimization_plan 验收口径）

- 现状同类重跑：~1015s / 121 次 LLM 调用（explore 从零探索）；
- 第二批完成后目标：**几十秒量级、0-2 次 LLM 调用**（planner 跳过 + 导航零 LLM +
  收尾自动证据），优于该文档原定的 1015s→~200s 目标；
- 观测口径：trace `execution.mode` / `llm_call_count` / `duration_seconds` /
  `mode_transition_events`（埋点已在位，无需新增）。
- **前置耦合声明（2026-08-22 二次评审补充）**：「0-2 次 LLM」以 Gate-A 达标
  （spec 填充率 ≥ 阈值）为前提——R3 收尾自动证据的零 LLM 能力依赖 F4 抬起的
  填充率；填充率不足时收尾仍会大量 unknown → agent 补验 → LLM 归不了零。
  Gate-A 未达标期间，R 批次验收放宽为「duration 与 llm_call_count 显著低于
  explore 基线」（建议口径：较 ~1015s / 121 次基线下降 ≥70%）。

## 风险与权衡

1. **F1 收紧可能压低 spec clause 通过率**：若 planner 把大量 claim 映射成 spec 但
   自动匹配器又覆盖不足（如自绘控件误配 element_exists），clause 会卡 unknown。
   缓解：先统计真实 run 中 spec clause 的自动证据命中率（M1 覆盖率口径），再开闸。
2. **F3 动 planner 输出格式属行为类改动**：需真机回放 + plan_review 前端适配确认。
3. **F5 设备 mock 保真度**：mock 回放验不出感知竞态类问题（R1 类），边界要诚实——
   它保的是「决策/判定逻辑回归」，不是「设备时序」。

---

## 附：docs 目录治理（本次同步执行）

已完成/被取代的历史 plan 移入 `docs/archive/`（git 历史保留，随时可查）：

- `llm_native_architecture_migration_20260709.md`（原则已由各活跃 doc §0 承载；L2/L3 未做项转入本 plan 追踪）
- `综合评审与问题分类_20260711.md`（7 月问题全部闭环）
- `rag_data_quality_improvement_plan_20260709.md`、`rag_locator_accuracy_improvement_20260805.md`（RAG 项已被后续知识库方案取代/落地）
- `vision_tap_plan_20260730.md`、`vision_optimization_plan_20260803.md`（vision 链路已落地并稳定）
- `phase4_report_fix_plan_20260817.md`、`explore_mode_learning_gap_plan_20260818.md`（标记 closed，落地项均有代码与单测）
- `discrepancy_detection_core_plan_20260819.md`、`m4_auto_evidence_plan_20260819.md`、`checklist_m4a_plan_20260820.md`（M1-M4a 已实现：evaluate 优先级链、UNKNOWN 上限、差异报告、auto_record_evidence、checklist 注入均已在代码核对）

保留为活跃文档：

- `time_optimization_plan_20260821.md`（P2/P3 未做、战略层验收待跑）
- `explore_mode_learning_plan_20260813.md`（direct/guided 端到端准入验收仍未完成，是战略层唯一 spec）

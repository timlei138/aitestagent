# 验证检查项清单（Checklist）落地计划

> 日期：2026-08-20（v2：基于 Review 纠错修订）
> 关联：证据驱动终止机制（Layer 2 / Layer 3 / M4）、run `201539` 复盘
> 状态：**已对齐原则，采纳；作用域已修正为内层**

---

## 0. 指导原则（不可违背）

1. **结束只能靠证据**：agent 是否终止必须由"证据是否满足契约"判定，不能依赖 agent 的善意（主动 `report_done`）。
2. **证据必须可回放、可审计**：每一条验证项都应带"过/没过"的结论、证据事实、截图，供人工复核。
3. **代码兜底优先于 prompt 软约束**：凡是能用确定性代码强制的收敛，不交给 LLM 自行判断。
4. **机制必须挂在 agent 实际工作的作用域**：外层 loop 只跑 1 次，agent 的 144 步全在内层 `_run_agent` 的 `_tools_node` 里。任何约束若放外层，会和 M2/M3 一样"挂错作用域、跑一次、失效"。
5. **新增能力不破坏现有 Layer 2 / Layer 3**：Layer 2（待验证清单）与 Layer 3（inconclusive 回环）逻辑保留，新增"全✓强制收口"作为更高优先级出口。

---

## 1. 现象与证据

### 1.1 最新 run `201539` 的核心数据（来自 `204130_test-20260819_201539_trace.json` + `logs/service.log`）

| 指标 | 值 | 含义 |
|---|---|---|
| `display_steps` | 145（continue=144） | 144 步都是 `continue`，仅 1 步因手动停止终止 |
| `duration` | ~1480.8s（约 25 分钟） | 远超正常用例时长 |
| `llm_calls` | 119 | 高调用量 |
| `click` / `assert_page_contains` / `vision_tap` | 46 / 36 / 15 | 对一个仅 7 个 verification 的用例严重过量 |
| `verdict` | `inconclusive` → 实际 `conclusion=DONE: EVALUATOR_TERMINAL: passed` | 功能验证早已完成 |
| `report_done` 调用 | **0 次** | agent 全程从未主动声明 DONE |

### 1.2 关键结构纠错（Review 定论，必须先行）

**外层 loop 只跑了一次。** 精确按日期 grep 实测：

- 20:15–20:41 该 run 的 `"Agent #N"` 行数 = **1**（只有 Agent #1）
- 20:15–20:41 该 run 的 `"Route: agent"` 行数 = **0**

即：`agent_node` 被调用 **1 次**；其内层 `_run_agent`（`agents/llm_runtime.py:215`）跑了 **119 个 LLM turn**（46 click + 36 assert + 15 vision_tap = 144 个工具调用，全在这**一次内层循环**里）。最后 evaluator 跑一次判 passed，route 一次到 reporter。

**推论（修正原版根因 1 的错误假设）**：原 Plan 隐含"外层 loop 跑了 145 次"，错误。真实结构是——**外层（evaluator + route，含 M2/Layer 3/全✓收口）只跑 1 次；agent 的全部重复验证发生在内层 `_tools_node` 的 144 个工具调用里**。这意味着在 144 步重复期间，外层机制根本没机会介入。

### 1.3 循环形态（两类重复）

- **同默认值二次断言**：seq 21–31 先断言了一轮默认（50分钟/4节/08:00-08:50…），seq 35–46 **又把同样的默认值重新断言一遍**。
- **对同一控件反复 open 编辑器微调**：seq 47 之后进入 `click(第1节) → vision_tap 滚轮 → 确定 → assert` 循环，把 50→45→50、08:00→08:05→08:51 来回改；中间还穿插 `删除/新建` 早间节的摆动（service.log 71546/71581）。seq 142–143 仍在 `click(第1节) → vision_tap 分钟50→51`。

### 1.4 关键事实

- 最终 `v1–v8` 所有 clause 都 `status=passed` —— **功能验证早完成，但 agent 在内层不收口**。
- 内置熔断 `_LOOP_BREAK_CONSECUTIVE=3`（`agents/loop_control.py`）**未触发**：因 `page_signature` 随列表项数量变化（`删除 x2→x3`、`编辑 x6→x7`）而每次不同，连续 3 次相同签名永远凑不齐。
- **内层已有"全✓收口"雏形**：`llm_runtime.py:685-695` 的 `_tools_node` 每次工具调用后都 `evaluate_verification` 重算 `_ctx._clause_state`，`verdict` 到 `passed/failed` 即 `EVALUATOR_TERMINAL break`。本次 run 的最终 `DONE: EVALUATOR_TERMINAL: passed` 正是它触发的。所以**缺的不是"收口"，而是"实时进度"**——agent 在内层 119 turn 里看不到"v0 ✓、v6 ○"，只能反复确认当前页面。

---

## 2. 深度根因分析（因果链，表层 → 本质）

### 根因 1（直接，作用域错配 —— 原版表述已纠错）
**原版错误**：说"agent 不 DONE 导致判定权失效，每次从 `graph.py:123 return agent` 回环"——隐含外层 loop 跑 145 次，错误。

**修正后准确表述**：外层 loop（evaluator + route，含 M2/Layer 3/全✓收口）只跑 **1 次**；agent 的 144 步重复全部发生**内层 `_run_agent` 的 `_tools_node`** 里。外层机制在 144 步重复期间完全无法介入。因此"证据驱动终止"若继续挂在外层 route（如原 Task 4），只会像 M2/M3 的盲点一样"跑一次、失效"。

### 根因 2（机制，信号消失）：待验证清单是"外层快照"，内层冻结
`nodes.py:559-573`（`agent_node`）每轮由 `clause_state` 的 `unknown` clause 重建待验证清单，但 `agent_node` 只跑 1 次 → 这份清单在 119 个内层 turn 里**冻结成快照**，和现在的"待验证清单"一样是死的。一旦某项 `passed`，它从清单消失，配套提示（575-582 行）也只在 pending 非空时注入。结果：内层 agent 看不到"你该停了"的任何实时信号，只剩 `nodes.py:554` 一句弱提示，LLM 在长上下文里把它当历史而非约束 → 对已 passed 控件反复微调。

### 根因 3（熔断失效）：`page_signature` 把"语义同一步"错判为"不同页面"
`loop_control.py:15-32` 的 `page_signature = activity|title|visible_labels_hash`，`vis_hash` 对所有可点击元素 label 排序后哈希。agent 每次增删节导致列表项数量变化 → `visible_labels` 哈希变 → `page_signature` 每次不同 → 熔断永远凑不齐连续 3 次相同签名。此前为"+10 次连点"加的 `repeat` 参数解决了"同页面内连点"，却暴露了"跨页面语义重复"这一更深的盲区——而后者是本次 145 步的主因。

### 根因 4（本质，原则违背）：内层缺"实时进度 + 全✓强制退出"硬边界
验证完成态（全 passed）在内层没有任何"实时喂回 agent"的通道；`evaluate_verification` 虽每次重算 `clause_state`，但结果只用于最终 break，**没有渲染成勾选清单回灌给 agent**。这违反指导原则 1/4：终止不应依赖 agent 善意，且机制必须挂在 agent 实际工作的作用域（内层）。

---

## 3. 目标方案：持久化"检查项清单（Checklist）"，下沉到内层

### 3.1 核心思想

把"瞬时 pending/passed 文本"升级为**持久化、逐项勾选、带证据与截图的清单**，且**渲染点必须在内层 `_tools_node`**——每次工具调用后重算 `clause_state`，立刻把实时勾选清单作为增量消息 append 进 `outputs`。这样 agent 每做一个工具调用，就能看到最新的 ✓/○ 清单（"v0 已✓禁止再碰，还剩 v6–v15"），才会停止对已✓项的重复微调。

- 检查项 (= `verification_contract.verifications[].clauses[]`)，每项有 `claim`(预期)、`channels`(验证方式)。
- 每项状态 `passed / failed / unknown` 对应 ✓ / ✗ / ○。
- 每项挂载**证据列表**（含 `channel`、`status`、`authoritative`、`fact`、`artifact_ref` 截图路径）。
- **全✓时由内层 `_tools_node` 强制 break**（已有 `EVALUATOR_TERMINAL` 雏形，无需重造）；外层 route 仅留兜底。

### 3.2 数据现状（已具备，无需重建采集层）

| 能力 | 现有实现 | 位置 |
|---|---|---|
| 检查项定义 | `verification_contract.verifications[].clauses[]`，含 `id`/`claim`/`channels` | contract |
| 每项状态 | `evaluate_verification` 算 `result`(passed/failed/unknown) + clause `status` | `verification.py` / `llm_runtime.py:685-690` |
| 每项证据 | `_record_deterministic_check` 追加 `ctx._evidence_events`：`verification_key`/`clause_id`/`status`/`fact`/`authoritative`/`artifact_ref` | `tools/verify.py:94` |
| 每项截图 | `_save_evidence_screenshot` 落盘 `screenshots/{run_id}/evidence_{key}_{seq}.png`，写入 `artifact_ref` | `tools/verify.py:45` |
| **内层重算点** | `_tools_node` 每次工具调用后 `clause_state = evaluate_verification(...)` | `llm_runtime.py:687-690` |
| **内层收口雏形** | `verdict in {passed,failed}` → `EVALUATOR_TERMINAL break` | `llm_runtime.py:691-695` |

**缺口（G1–G3）即本计划要补**：
- **G1**：`clause_state` 重算后**没有渲染回灌给 agent**，内层 119 turn 看不到实时进度。
- **G2**：证据/截图没"挂到对应项"呈现给 agent，agent 只收到最终弱提示。
- **G3**：外层 `agent_node` 的待验证清单是冻结快照（根因 2），不能作为实时约束。

---

## 4. 实施任务（Task）—— 作用域已修正

### Task 1：`state.py` 新增持久 checklist 字段（供渲染用，可选但推荐）
```python
verification_checklist: list[dict]  # 每项:
# {
#   "key": str, "clause_id": str, "claim": str, "channels": list[str],
#   "status": "passed|failed|unknown",
#   "evidence": [{"channel": str, "status": str, "authoritative": bool,
#                 "artifact_ref": str, "fact": dict, "seq": int}],
#   "last_updated_seq": int,
# }
```
可由 `clause_state` 在 Task 2 渲染时即时派生，**不必**单独持久化（保持最小改动）；若需跨层回放审计，可在 `ctx` 上挂一份累加版。

### Task 2：渲染函数 `_render_checklist_view(clause_state, contract)`
新增纯函数（置于 `agents/verification.py` 或 `nodes.py`），输入 `clause_state` + `contract`，输出勾选视图文本：
```
检查项清单（✓=已通过 ✗=已失败 ○=待验证）:
 [✓] v1::v1.0 默认每节课45分钟 | 证据: ui_text PASS 截图 evidence_v1_2.png
 [✓] v3::v3.2 第1节课08:00-08:50 | 证据: ui_text PASS evidence_v3_5.png
 [○] v4::v4.1 可添加至10节 | 待 assert_behavior_effect 补齐
 [✗] v5::v5.0 ... | 反证截图 evidence_v5_1.png
规则：○ 项结束前必须补齐证据；✓/✗ 项禁止再 open 编辑器微调或重复 assert。
```
证据引用从 `ctx._evidence_events` 按 `key+clause_id` 取最新 `artifact_ref`。

### Task 3（**作用域：内层 `_tools_node`**，落点 `llm_runtime.py:690` 后）：每次工具调用后追加实时勾选清单
在 `llm_runtime.py:690`（`_ctx._clause_state = clause_state` 之后）紧接着：
```python
# 实时勾选清单：每次工具调用后重算并回灌 agent，治根因 2/4
_checklist_msg = _render_checklist_view(clause_state, contract)
if _checklist_msg:
    outputs.append(
        ToolMessage(content=_checklist_msg, name="verification_checklist")
    )
```
> **这是根治点**：agent 每做一次工具调用就看到最新 ✓/○ 清单，才会停止对已✓项的重复微调。原版"在 agent_node 渲染"的方案错误（agent_node 只跑 1 次 → 清单冻结），已废弃。

### Task 4（**内层已有雏形，仅加固 + 外层兜底**）
- **内层（主）**：`llm_runtime.py:691-695` 的 `EVALUATOR_TERMINAL break` 已是"全✓/全✗强制收口"。确认其判定覆盖"全部 clause passed"（当前 `verdict=="passed"` 即代表全 passed，已满足）。**无需重造**，但建议在该 break 前补一句日志明确"全✓内层中端"。
- **外层（兜底）**：`graph.py` 的 `route_after_evaluator` 在全✓时 `return "reporter"` 保留作为**次生兜底**（覆盖极端情况：内层未 break 但 evaluator 判 passed）。优先级：全✓先于 M2/Layer 3。

### Task 5：`agent_common.txt` 加硬规则（软约束，配合内层清单）
"当清单全为 ✓ 时，必须停止所有操作；对 ✓/✗ 项做任何微调或重复 assert 视为违规。系统会在每次工具调用后回灌最新勾选清单，请依据它决策。"

### Task 6：测试新增
- `tests/test_llm_runtime_checklist.py`（新文件）：`test_tools_node_appends_checklist_each_tool_call` —— mock contract+evaluate，断言 `_tools_node` 每个工具调用后 `outputs` 含 `verification_checklist` 消息且反映最新状态。
- `test_tools_node_terminates_on_all_passed` —— 构造全 passed `clause_state`，断言 `_tools_node` 置 `loop_break_reason=EVALUATOR_TERMINAL: passed` 并 break。
- 复用 `tests/test_verification_contract.py` 校验 `evidence[]` 挂载 `artifact_ref`。

---

## 5. 与既有机制的关系（不破坏原则 5）

| 既有机制 | 变化 |
|---|---|
| Layer 2（待验证清单 @ `nodes.py:547-582`） | 文本升级为 Task 2 勾选视图；但**明确它只是外层 1 次快照**，实时约束改由 Task 3 内层清单承担 |
| Layer 3（inconclusive 回环 @ `graph.py:97`） | 保留；仅新增 Task 4 外层兜底全✓ |
| M2（unknown 耗尽守卫） | 保留，deadlock 兜底；全✓先于 M2 |
| `repeat` 参数 + prompt 硬规则 | 保留，治"同页面内连点"；本计划治"跨页面语义重复 + 实时进度"，互补 |
| M4（语义证据匹配） | 长期加固：解决 `page_signature` 失效（根因 3）的终极方案，本计划不替代，建议并行推进 |

---

## 6. 根因覆盖矩阵（v2 修正）

| 根因 | 覆盖 Task | 作用域 |
|---|---|---|
| 根因 1（外层只跑 1 次，机制挂错作用域） | Task 3 下沉到 `_tools_node` | **内层** |
| 根因 2（待验证清单外层快照、信号消失） | Task 2 + Task 3（实时回灌 + ✓/✗ 禁碰） | **内层** |
| 根因 3（page_signature 失效） | 长期由 M4 解决；Task 3"✓/✗ 禁碰"缓解重复微调 | 内层缓解 / M4 根治 |
| 根因 4（内层缺实时进度 + 全✓硬边界） | Task 3（实时进度）+ Task 4（内层 EVALUATOR_TERMINAL 已有雏形） | **内层** |

---

## 7. 分阶段实施与验收

### Phase 1：渲染函数 + 内层回灌（Task 2 + Task 3）
- 验收：`_tools_node` 每个工具调用后 `outputs` 含最新勾选清单；agent 在第 2 次起可见 ✓/○ 变化。

### Phase 2：加固内层收口 + 外层兜底 + prompt（Task 4 + Task 5）
- 验收：全✓时内层 `EVALUATOR_TERMINAL` 命中；外层 route 兜底 `reporter`。

### Phase 3：测试与回归（Task 6）
- 验收：新增 2 个内层测试通过；既有 Layer 2/3 测试不受影响。

### 端到端验收（实测）
- 重跑联想日历"课程表"用例，预期 `display_steps` 从 145 降至 ≤ 40；agent 在证据齐全时由内层清单驱动停止重复微调；全✓由内层强制收口。

---

## 8. 不做的事（防止变成特例补丁 / 防作用域错配）

- **不**把勾选清单渲染放在 `agent_node`（外层只跑 1 次，会冻结 —— 这正是原版错误）。
- **不**重造内层"全✓收口"（`EVALUATOR_TERMINAL` 已有雏形，仅加固）。
- **不**重造证据/截图采集层（已存在，仅做挂载）。
- **不**修改 `verification_contract` 结构（checklist 由 contract 派生）。
- **不**用 RAG 解决本次循环（RAG 治"知识复用"，不治"运行时收口"）。
- **不**把 `page_signature` 直接改成"核心控件签名"（属 M4 范畴，避免本轮范围蔓延）。

---

## 9. Review 结论（已采纳）

1. **方向正确、对齐原则**：checklist 方案与原则 1（结束只能靠证据）一致，采纳。
2. **关键纠错**：外层 loop 只跑 1 次（Agent #1、Route:agent=0），144 步全在内层 `_tools_node`。原根因 1 的"agent 不 DONE"表述错误，已改为"作用域错配"。
3. **Task 3 作用域修正**：从"agent_node 渲染一次"改为"`_tools_node` 每次重算 clause_state 后追加勾选清单到 outputs"（落点 `llm_runtime.py:690`）。改动极小、作用域正确。
4. **Task 4 修正**：内层 `EVALUATOR_TERMINAL` 已是"全✓收口"雏形，无需重造；外层 route 仅兜底。
5. **待决问题定调**：
   - 待决 1（全✓是否绕过 agent）：**绕过，落实原则 1**，但绕过发生在**内层**（`_tools_node` 全✓即 break），外层 route 仅兜底。
   - 待决 2（Task 4 与 M2 优先级）：**全✓先于 M2**，正确。
   - 待决 3（M4 是否本轮）：**不纳入**，正确（M4 是 page_signature 的终极解，范围不同）。
6. **一句话根治点**：bug 的根治点不在外层 route，而在内层 `_run_agent` 的 `_tools_node` 那个"每次重算 clause_state"的位置（`llm_runtime.py:687-690`）——把实时勾选清单喂回去，agent 才会停止重复验证。

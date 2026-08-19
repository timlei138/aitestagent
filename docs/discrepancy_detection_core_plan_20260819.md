# 差异检测核心能力改造 Plan —— 让 Agent 稳定发现「用例 vs 实现」不一致

> 状态：**待 Review（未改任何代码）**
> 关联复盘：`docs/verification_no_bug_and_reopen_analysis_20260819.md`（根因分析）
> 原则来源：`docs/explore_mode_learning_gap_plan_20260818.md` §0（契约收敛、代码给 ground truth、LLM 负责活）

---

## 0. 目标与原则

### 目标

把 Agent 的**核心产物**从"执行用例 + 判 passed/failed"，升级为：

> **发现「用例期望」与「实现实际」不一致，并产出可读的差异报告（期望 vs 实际）。**

这是自动化测试 Agent 的核心价值所在。本次 run（`105856`）暴露的正是这个能力缺失：真实行为是"按钮可点 + 弹 Toast"，与用例"置灰不可点"相反，Agent 却判成了 passed、且无任何差异上报。

### 原则（每次改动都据此校准，不可违反）

1. **代码负责"稳"**：确定性事实的采集、期望 vs 实际的确定性对比、契约不变量。
2. **LLM 负责"活"**：理解用例语义、导航到目标、决定下一步。
3. **契约收敛，补丁发散**：每个改动是**有限、稳定**的契约/不变量，不是针对某个词/场景的 if/else。

> 试金石：改动是否让"稳"的部分收敛、让 LLM 更放得开？凡让代码陷入关键词清单/特例 if/else 的，都是补丁，停下来换思路。

---

## 1. 现状与问题（一句话回顾）

- 验证条件 `verification` 是**自由文本**，靠 `_default_channels_for_claim` 的**关键词 marker** 推断通道。
- 判定是"任意通道 PASS → passed"，LLM 判定工具（`click_and_check`/`visual_check`）的"合理化 PASS"能覆盖确定性事实。
- 没有"确定性事实与期望相反 → authoritative FAIL"的不变量，也没有"差异报告"这个一等产物。
- 结果：v5「置灰不可点」被判 passed（应为 failed/差异）；v4「显示…开关」因关键词优先级漂移判 unknown → 整体 verdict 卡 inconclusive → 图不终止。

详见复盘文档 §2/§3。

---

## 2. 核心架构决策（一次 shift）

```
当前：自由文本 claim → agent 用 LLM 判定工具 → LLM 判 passed/failed
目标：结构化期望 spec → agent 按 spec 执行确定性工具 → 代码读事实 + 确定性对比 → 差异报告
```

**关键 shift**：把"验证方法"从 agent 的 LLM 判断里拿出来，变成契约里的一段**结构化 spec**。Agent 职责从"自己决定怎么验 + 自己判结果"收窄为"**导航到目标 + 按 spec 执行确定性工具**"。

对齐原则：
- `spec`（谓词 + 目标 + 期望）= **契约** = 代码兜底。
- 导航 / 感知 / 决策下一步 = **LLM** = 活。

---

## 3. Phase 0：确定性事实词汇表（地基）

定义**有限、稳定**的谓词 DSL，每个谓词对应一个"可被程序核实"的事实：

```python
PREDICATES = {
    "enabled":       {"fact": "element.enabled",   "channel": "element_state",   "authoritative": True},
    "disabled":      {"fact": "element.enabled",   "channel": "element_state",   "authoritative": True, "invert": True},
    "checked":       {"fact": "element.checked",   "channel": "element_state",   "authoritative": True},  # 见风险②
    "exists":        {"fact": "element.exists",    "channel": "element_state",   "authoritative": True},
    "absent":        {"fact": "element.exists",    "channel": "element_state",   "authoritative": True, "invert": True},
    "contains_text": {"fact": "page.text",         "channel": "ui_text",         "authoritative": True},
    "page_is":       {"fact": "activity",          "channel": "page_state",      "authoritative": True},
    "list_count":    {"fact": "element.count",     "channel": "behavior_effect", "authoritative": True},
    "still_on":      {"fact": "activity.before==after",       "channel": "behavior_effect", "authoritative": True},
    "no_change":     {"fact": "screen_signature.before==after","channel": "behavior_effect", "authoritative": True},
}
```

要点：

- 这是**契约**，有限稳定；新场景要么复用谓词、要么加一个**通用**谓词，**不为某 app 加特例**。
- **删除 `_default_channels_for_claim` 的关键词推断**（`agents/verification.py:22-54`）——channel 由 spec 的谓词决定，不再靠"置灰/显示/开关"猜。这一举根治 v4 的 channel 漂移（根因就是 `_TEXT_CLAIM_MARKERS` 的"显示"先于 `_STATE_CLAIM_MARKERS` 命中）。
- 每个谓词产出结构化结果（见 Phase 2）。

涉及文件：`agents/verification.py`（删关键词推断）、`tools/verify.py`（补谓词）。

---

## 4. Phase 1：结构化验证契约（最大、最根本）

### 数据结构

`goal_description.verification` 从 `[string]` → `[{claim, spec}]`：

```python
# 正常映射
{
    "claim": "必填项为空时完成按钮置灰不可点击",
    "spec": {
        "predicate": "disabled",   # 来自 Phase 0 词汇表
        "target": "完成按钮",       # 语义目标，agent 用 locator 解析成具体元素
        "expected": True,          # 期望值（disabled=True 表示期望置灰）
    },
}
# 无法映射 → 优雅降级
{
    "claim": "……",
    "spec": None,                  # 走当前自由文本 + 全通道 + LLM 判定
}
```

### 改动点

| 文件 | 改动 |
|---|---|
| `agents/prompts/planner.txt` | 输出结构化 spec（用 Phase 0 词汇表）；映射不了的 claim 降级 `spec: null` |
| `agents/verification.py::build_verification_contract` | 拆分 clause 时保留 spec |
| `agents/verification.py::validate_contract_spans` | 适配 spec 字段 |
| `agents/nodes.py::plan_review_node` | 把 spec 随 interrupt 下发给前端 |
| `agents/plan_extractor.py` | `extract_verification_semantics` 等适配 spec |
| `frontend/spa/src/App.vue` + `ReportDetail.vue` | 人工可**看到并编辑** spec |

### 要点（原则）

- spec 用**有限 DSL**，是"契约"不是"补丁"。
- planner（LLM）映射错了 → 人类在 plan_review **一次修好**，比"agent 执行时每次自己猜"可靠一个数量级（一次性、可审、可改）。
- 降级路径必须保留，否则 planner 映射不了的 claim 覆盖率骤降（见风险③）。

---

## 5. Phase 2：确定性工具 DSL 补全

统一一个 `verify(spec)` 工具（或扩展现有 `assert_*` 到覆盖 Phase 0 词汇表），读确定性事实，返回结构化结果：

```python
{
    "status": "PASS" | "FAIL",
    "expected": ...,   # 期望值
    "actual": ...,     # 实际读到的值
    "matched": bool,
    "channel": "element_state",
    "authoritative": True,
}
```

**关键**：`enabled`/`checked` 这类**当前态**事实，现在是 `authoritative=False`（`tools/verify.py:301-303` 旧设计只信 before/after），必须**改标权威**——`enabled`（`View.isEnabled()`）是可靠当前态，不同于历史担心的 Accessibility `checked` 抖动。

**鲁棒性细则**：
- **target 解析失败/歧义 → 返回 `unknown`，不许猜、不许降级到 LLM 通道补判 passed**（否则 locator 解析到父容器/错误元素，读到的 `enabled` 就是错的 → 确定性 FAIL 变 false negative）。
- **抖动去重**：同一 target 连续两次相反读数（竞态）→ 标 `unknown` 而非直接 failed（取最后一次稳定读数；无法稳定则 unknown）。避免 flaky failed。

涉及文件：`tools/verify.py`、`tools/__init__.py`（`AGENT_TOOLS`）。

---

## 6. Phase 3：确定性对比 + authoritative FAIL（判定层）

`evaluate_verification`（`agents/verification.py:66-156`）改为：

```python
for clause in clauses:
    matching = [e for e in events
                if e.key == key and e.clause_id == cid]
    # ① 确定性矛盾优先：权威 FAIL 先于任何 PASS（LLM 通道的"合理化 PASS"压不过它）
    contradiction = any(e.status in ("FAIL", "NO") and e.authoritative for e in matching)
    if contradiction:
        clause.status = "failed"          # + 记录 contradiction（期望/实际）
    elif any(e.status in ("PASS", "YES") for e in matching):
        clause.status = "passed"
    else:
        clause.status = "unknown"
```

要点：

1. **确定性矛盾优先于任何 LLM 判定通道的 PASS**——`click_and_check`/`visual_check` 的"合理化 PASS"无法覆盖确定性 FAIL。
2. **Phase 3 不依赖 Phase 1（spec）**：确定性对比 key 在**证据自身的 `authoritative` 标志 + 结构化事实**（`{expected, actual}`）上，而不是 `clause.spec`。因此 M1（Phase 0+2+3，无 Phase 1）即可落地"置灰差异 → failed"；Phase 1 只是把 `expected` 的**来源**从"agent 执行时声明"升级为"planner 规划时声明"，二者解耦、互不阻塞。
3. **状态类 claim 强制确定性断言**：`enabled/disabled/checked` 等状态类 claim，**必须**存在确定性断言证据；缺失时禁止判 passed（视为未验证/unknown）。堵住"agent 为对冲故意不调断言工具、改用 click_and_check 走 LLM 通道"的洞。
4. channel 白名单的删除（Phase 0）由"确定性/权威标志"承接；在 Phase 1 未落地前，状态类 claim 由要点 3 的"强制确定性断言"兜底，不再依赖关键词推断。**spec:null 的非状态类 claim 回退通道 = 全通道（含 `behavior_effect`），与 C1 过渡态一致**——删除推断后若无此回退，非状态类 claim 也会 unknown（v4 类复发），M1 实施时必须钉死。
5. `authoritative_failure → failed` 的判定已存在于 `agents/verification.py:99-104`，只需让 spec 谓词产出的 FAIL 都带 `authoritative=True`。

涉及文件：`agents/verification.py`、`agents/nodes.py::evaluator_node`。

---

## 7. Phase 4：差异报告 + 终止（一等产物）

- failed clause → 报告呈现"**期望 vs 实际**"差异（复用 `deciding_evidence` 的 `actual`，`agents/nodes.py:1071-1086` 已有该字段）。
  - **呈现"实现方式差异"而非只"值差异"**：例如"期望置灰（enabled=false），实际 enabled=true + 点击弹 Toast 拦截"——把实现方式写清，否则 reviewer 会困惑"明明按钮能点，为什么报置灰失败"。
  - **差异报告可审计**：条目须含 期望谓词值、实际真值（含 `rid`/`bounds` 便于复核）、采集时间戳、当时页面 activity、可点开看 UI Tree 快照。差异必须是可复核的 bug 证据，不是一句 AI 文本。
- authoritative FAIL → 触发终止 + 差异上报（治问题1）。
- unknown → **解析上限后强制终**（inconclusive），不再无限探索（治问题2 图层面）。`route_after_evaluator`（`agents/graph.py:57-96`）已有 passed/failed→reporter 雏形，补 unknown 上限即可。
- **差异分类（bug vs 用例错误）留人工**：agent 只报"差异"，定性是下游——避免把核心功能（发现差异）和分类混在一起。

涉及文件：`agents/nodes.py::reporter_node`、`agents/graph.py`、`frontend/ReportDetail.vue`。

---

## 8. Phase 5：探索边界（prompt 软约束 + 代码护栏）

- **prompt 软约束**：契约外不越界；验证完成即停（治问题2 的 agent 侧惯性）。
- **代码护栏（堵 Root B 残余场景）**：验证契约全部 clause resolved 后，loop/工具层**拒绝执行"非契约内、非用户指令"的新探索性操作**（如 re-import、重新导航到契约外页面）。注意 `route_after_evaluator` 的 passed/failed→reporter 已是"全部 resolved→终止"的雏形，本护栏针对的是"clause 未全部 resolved 前 agent 自发探索契约外"的残余；C2 的 unknown 上限是兜底，本护栏是主动拦截。

涉及文件：`agents/prompts/agent_common.txt`、`agents/graph.py`（或工具层 `tools/__init__.py`）。

---

## 9. 落地顺序与里程碑

| 里程碑 | 内容 | 交付物 | 大小/风险 |
|---|---|---|---|
| **M1** | Phase 0 + 2 + 3 | "置灰"类确定性差异被抓到 → failed | 小~中，低风险 |
| **M2** | Phase 4 | 差异报告 + unknown 终止 | 中，低风险 |
| **M3** | Phase 5 | 探索边界 | 小，低风险 |
| **M4** | Phase 1 | 结构化 spec + 人工可编辑 | **大**，中风险 |

**建议顺序 M1 → M2 → M3 → M4**：前三个不依赖 Phase 1，就能让"确定性差异发现 + 报告 + 终止"闭环，改动小、风险低、先把核心能力立住；Phase 1 动 planner 输出 + 契约 schema，是最大架构升级，放最后单独验证。

---

## 10. 验收标准（成功判据）

1. **M1 验收**：构造"用例期望置灰、实现实际可点 + Toast"的用例，跑完应产出 `clause.status = failed`（而非 passed），且 `contradiction` 含 `{expected: disabled, actual: enabled}`。
2. **M2 验收**：上述用例在报告中呈现"期望：置灰不可点；实际：按钮 enabled + 点击弹 Toast"的差异；且不再出现 96 步/18 分钟的无限探索（unknown 有上限强制终）。
3. **M3 验收**：验证完成后不再自发 re-import / 契约外探索。
4. **M4 验收**：planner 对典型用例能产出正确 spec；映射不了的 claim 正确降级 `spec: null`，覆盖率不降。
5. **回归**：现有 266+ 测试全绿；`_default_channels_for_claim` 删除后，v4 类"显示…开关"不再 unknown（由 spec 驱动）。
6. **M1 覆盖率**：M1 上线即统计真实 run 里能被 Phase 0 词汇表覆盖的 clause 占比；若覆盖率低（如置灰/开关类大量漏映射），说明 M1 价值被降级路径稀释，需提前拉 Phase 1 或扩充词汇表。
7. **差异报告可读性**：差异条目可在 ReportDetail.vue 直接点开，看到 actual 的 UI Tree 快照（含 rid/bounds/activity/时间戳），可人工复核。

---

## 11. 风险与权衡（诚实）

1. **最大风险 = Phase 1 planner 映射质量**：LLM 把自由文本用例映射成 spec，可能映射错谓词/目标。缓解：spec 有限 DSL + plan_review 人工可改 + `spec:null` 降级。这是"彻底改"最贵的部分，收益是**根除"agent 执行时自己猜"的不确定性**。
2. **`checked` 的权威性**：历史 Accessibility `checked` 抖动导致当前态被标非权威。`enabled` 无此问题可安全标权威；`checked` 需先验证可靠性再标权威，否则误报。
3. **降级路径必须保留**：planner 映射不了的 claim 必须降级为"自由文本 + 全通道 + LLM 判定"，否则覆盖率骤降。
4. **"差异 ≠ bug"**：本 plan 只做到"报差异"，不自动定性 bug vs 用例错误——定性是下游/人工。
5. **locator 解析歧义**：`spec.target`（语义目标"完成按钮"）解析到错误元素/父容器 → `enabled` 读数错误 → false negative。缓解：Phase 2 的"target 解析失败/歧义 → unknown，不许 LLM 补判"。
6. **确定性读数竞态**：同一元素连续两次相反读数（页面刷新竞态）→ flaky failed。缓解：Phase 2 的"抖动去重 → unknown"。
7. **降级路径漏水口（问题1 的复发风险，不涉及问题2）**：M4（Phase 1）落地前，大量 clause 是 `spec:null` 走旧 LLM 逻辑，**问题1（置灰类误判/漏报差异）可能复发**。注意：**问题2（图不终止）已由 M2 的 unknown 上限治住，不依赖 Phase 1**（§9 里程碑 M1→M2 已闭环）。缓解：M1 阶段先统计覆盖率（§10.6），覆盖率低则提前拉 Phase 1。

---

## 12. 附录：关键代码位置（已核对）

| 位置 | 说明 |
|---|---|
| `agents/verification.py:22-54` | `_default_channels_for_claim` 关键词推断（Phase 0 删除） |
| `agents/verification.py:66-156` | `evaluate_verification`（Phase 3 改造） |
| `agents/verification.py:99-104` | 已有 `authoritative_failure → failed` 判定 |
| `tools/verify.py:293-352` | `assert_behavior_effect` 谓词 DSL + `authoritative` 标志（Phase 2 补全） |
| `tools/verify.py:301-303` | "当前态检查一律非权威"的旧注释（Phase 2 需对 `enabled` 改标权威） |
| `device/perceiver.py:372` | `enabled` 已 dump（Phase 2 直接复用） |
| `tools/click.py:695` | 已用 `enabled is False` 判禁用（`enabled` 事实已存在） |
| `agents/graph.py:57-96` | `route_after_evaluator`（Phase 4 补 unknown 上限） |
| `agents/nodes.py:1071-1086` | `deciding_evidence`（Phase 4 差异报告的原料） |
| `agents/prompts/planner.txt` | planner 输出 schema（Phase 1 改造） |

# M4 重定义 Plan —— 观察自动成证据（Phase 1 修订版）

> 状态：**待 Review（未改任何代码）**
> 关联：`docs/discrepancy_detection_core_plan_20260819.md`（Phase 1 = 本 M4 的原始版，本文为重定义）
> 原则来源：`docs/explore_mode_learning_gap_plan_20260818.md` §0（确定性事实 → 代码给 ground truth；LLM 负责导航/理解）
> 触发场景：最新 run `test-20260819_170616` 暴露"agent 观察到但未记录证据 → 误判 inconclusive → 需人工确定"

---

## 0. 价值一句话

M4 的价值不是"引导 agent 选工具"，而是**让确定性观察自动成为证据，把"记录证据"这个动作从 LLM 手里拿走、交给代码**。Agent 只负责**导航到目标页**（它的"活"），证据由**自动匹配器**（代码的"稳"）落盘。

## 1. 问题定性（为什么重定义）

- 根因（已定位）：agent 的**观察**（`get_screen_info` 看到 View Tree）和系统的**证据**（`_evidence_events`）之间隔着一个**手动 assert 步骤**。agent 看到 ≠ 记录，漏了就 `inconclusive` → 前端"待人工复核"。
- 本质矛盾：`activity` / `element` / `checked` / `enabled` 这些确定性事实，perceiver 已经抽出来了，却还要 agent **手动"念一遍"给系统听**。这个手动步骤是脆弱点。
- 原则对齐：**确定性事实 → 代码给 ground truth（自动匹配）；导航/理解 → LLM 负责。**

> 注意：这不是 LLM 幻觉。agent 通过 View Tree 看到的判断是**准确**的，问题只在"看到"没有自动变成"证据"。

## 2. 核心机制

每个 spec-able clause 挂一个结构化 spec。**每次 `perceive()` 拿到页面快照后**，自动匹配器（纯代码）遍历 spec：

- 当前页面**满足** spec → 自动写 `PASS` 证据；
- 当前页面**确定性矛盾** spec → 自动写 `authoritative FAIL` 证据（直接触发差异报告 + fail-fast）；
- 当前页面**无法判定**（如元素没找到/歧义）→ **什么都不写**（留给 agent 手动 verify，不误判）。

于是：agent 只要导航到正确的页面，证据自动落；导航到"实现与预期相反"的页面，差异自动报。

## 3. 数据模型：spec

```python
clause = {
    "id": "v0.0",
    "claim": "课程表页面成功打开",      # 保留自由文本，用于展示 + 降级兜底
    "spec": {                          # 结构化期望（有限谓词）
        "predicate": "page_is",
        "target": "TimetableActivity",
        "expected": None,              # page_is 无需 expected
    },
}
```

### 谓词词汇表（落地到 Phase 0 的 PREDICATES）

| 谓词 | 事实来源 | 满足条件 | 确定性矛盾（→ authoritative FAIL） |
|---|---|---|---|
| `page_is(target)` | current_app.activity | activity 匹配 | —（页面对不上不算"矛盾"，只是还没到） |
| `page_contains(text)` | 页面文本快照 | 含 text | — |
| `element_exists(target)` | View Tree | 找到 target | —（找不到=unknown，不是矛盾） |
| `element_absent(target)` | View Tree | **找不到** target | 找到 → FAIL |
| `element_disabled(target)` | element.enabled | enabled==False | enabled==True → FAIL |
| `element_enabled(target)` | element.enabled | enabled==True | enabled==False → FAIL |
| `element_checked(target,bool)` | element.checked | checked==expected | checked!=expected → FAIL |
| `list_count(anchor,n)` | View Tree 计数 | count==n | count!=n → FAIL |

关键设计：**"找不到元素"对 `element_exists` 是 unknown（不写证据），对 `element_absent` 是 PASS**——保持不对称，避免把"元素不在当前可见区"误判成矛盾。

## 4. 自动匹配器（核心组件，纯代码）

```python
def auto_record_evidence(ctx, contract, understanding, current_app):
    """perceive() 之后调用：把当前页面确定性事实自动落成证据。"""
    for v in contract["verifications"]:
        for clause in v["clauses"]:
            spec = clause.get("spec")
            if not spec:
                continue  # spec:null → 不自动，留给 LLM verify
            r = _match_spec(spec, understanding, current_app)
            if r is None:
                continue  # 无法判定 → 不写
            ctx._evidence_events.append({
                "verification_key": v["key"],
                "clause_id": clause["id"],
                "channel": _spec_channel(spec),   # page_is→page_state, element_*→element_state
                "status": r["status"],            # PASS / FAIL
                "authoritative": r["authoritative"],
                "fact": r["fact"],                # 含 anchor/rid/bounds/实际值，供差异报告审计
            })
```

`_match_spec` 只返回三种结果：`None`（未知）/ `PASS`（满足）/ `FAIL+authoritative`（矛盾）。**保守原则：拿不准就不写。**

## 5. 各组件改动

| 组件 | 改动 |
|---|---|
| `agents/prompts/planner.txt` | 输出 `verification: [{claim, spec:{predicate,target,expected}}]`；映射不了 → `spec:null`。spec 是结构化字段不是字符串，格式错误率低 |
| `agents/verification.py::build_verification_contract` | 拆分 clause 时保留 spec；**channel 由 spec 谓词在匹配器里决定，不再关键词推断** |
| `agents/nodes.py::agent_node` | 在 `perceive()` 之后（拿到 `u`/`current_app` 处）调 `auto_record_evidence` |
| `agents/nodes.py::evaluator_node` | **几乎不变**——读 `_evidence_events` 的逻辑原样工作；自动证据和手动证据对它是同一种东西 |
| `tools/verify.py` | 现有 `assert_*` 工具**保留为手动 fallback**（spec:null 或显式断言用） |
| `frontend/spa/src/App.vue` | plan_review 展示 spec（只读 + 可选编辑，不做 gate） |

**要点**：这里"spec 决定 channel"是**写侧**（匹配器写证据时选 channel），不是**读侧**（evaluator 白名单过滤）。所以不会重新引入 v4 那种"channel 漂移"——没有白名单，只有"匹配器往正确 channel 写"。

## 6. 降级路径

- `spec:null`：不做自动匹配，走现有 LLM verify（M1 的 `disabled` 谓词 + authoritative 旁路照常工作）。
- **spec:null 不是失败**，是"少了自动匹配能力"，M1 行为原样兜底。
- M4 是**增量**：spec 覆盖到的 clause 自动成证据，没覆盖到的走 M1。

## 7. 与 M1/M2/M3 的关系（明确：M1 不回退）

**M1（`disabled` 谓词 + authoritative）不被删除、不被回退，M4 是在其上叠加一条"自动路径"。**

| | M1（现状，手动路径） | M4（重定义后，自动路径） |
|---|---|---|
| 触发方式 | agent 手动调 `assert_behavior_effect(disabled(...))` | 代码自动匹配 `element_disabled` |
| 读的事实 | `element.enabled` | **同一个** `element.enabled` |
| 权威标志 | authoritative=True | **同一个** authoritative=True |
| 判定逻辑 | `evaluate_verification` authoritative 优先 | **同一个** 判定逻辑 |

**分工**：

- spec 覆盖到的 clause → **走 M4 自动匹配**，agent 不用手动调；
- spec:null 的 clause → **走 M1 手动 `disabled` 谓词**，原样兜底。

**保留清单**（M4 不删、不回退）：`assert_behavior_effect(disabled(...))` 手动工具、authoritative 标志机制、evaluate 优先级逻辑、M1 的全部回归测试。

**其他里程碑关系**：

- **M2**（差异报告 + unknown 上限）：不变。自动 FAIL 直接喂给 M2 的差异报告。
- **M3**（探索护栏）：不变。M4 让 verdict 更快收敛（自动证据），间接减少触发 M3 的场景。

## 8. 里程碑

- **M4a（状态 spec 自动匹配，本次范围）**：`page_is` / `page_contains` / `element_exists` / `element_absent` / `element_disabled` / `element_enabled` / `element_checked` / `list_count` 这些**纯状态谓词**。覆盖最新 run 的 v0~v4 大部分 clause。
- **M4b（行为 spec，可后置）**：`click→page_changed_to`、`click→element_appeared` 这类"动作+后置"谓词。复杂，先不做。

## 9. 风险与诚实评估

1. **planner 映射质量（最大风险）**：LLM 把"课程表页面成功打开"映射成 `page_is(TimetableActivity)`，target 可能写错（全类名 vs 短名）。缓解：plan_review 可改 + `_activity_match` 用短名模糊匹配（现有逻辑已有）+ spec:null 降级。
2. **自动 FAIL 的误报**：`element_disabled` 若读错元素（target 歧义）会误判 FAIL。缓解：匹配器里"元素没找到/歧义 → 不写"，只有**唯一确定**的元素才判定。
3. **自动匹配时机**：agent 页面跳转瞬间可能来不及 perceive 导致漏证据。缓解：匹配器每次 perceive 都跑，agent 仍可手动补。
4. **范围收窄**：M4a 只覆盖"状态谓词"，行为类（点击后变化）仍靠 LLM。这是**刻意**边界——先吃确定性的确定性部分。

## 10. 验收标准

1. **自动成证据**：构造 `v0=page_is(TimetableActivity)` 用例，agent 导航到该页后，**无需手动 assert**，evaluator 自动判 v0=passed。
2. **自动报差异**：构造 `v5=element_disabled(完成按钮)` 用例，agent 导航到该页（按钮 enabled=True），自动触发 authoritative FAIL + 差异报告，**无需 agent 手动调 disabled 谓词**。
3. **降级**：planner 映射不了的 clause → spec:null → 走 M1 手动 verify，不回归。
4. **回归**：现有 281 测试全绿；手动 `assert_behavior_effect(disabled(...))` 工具仍可用。

## 11. 与原始 Plan（discrepancy_detection_core_plan）的差异

| 维度 | 原始 Phase 1 | 本重定义 |
|---|---|---|
| spec 角色 | 引导 agent 选工具 | **让观察自动成证据** |
| channel | spec 推 channel（有白名单风险） | 写侧决定 channel（无白名单） |
| agent 手动 assert | 仍靠 agent 调工具 | **spec 覆盖到的自动落，agent 不参与** |
| 前端 | spec 编辑 gate | 只读 + 可选编辑（不做 gate） |
| M1 关系 | 未明确 | **明确 M1 保留、叠加** |

---

**一句话总结**：M4 重定义为"把确定性观察自动落成证据"，核心是一个纯代码的自动匹配器 + spec 数据模型；agent 只负责导航，证据由代码自动写。M1 的手动路径完全保留、叠加共存，spec:null 原样回退到 M1。

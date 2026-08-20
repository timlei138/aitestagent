# Checklist + M4a 联合实施 Plan（最终版）

> 日期：2026-08-20
> 状态：**已采纳，可实施**
> 合并自：`verification_checklist_plan_20260820.md`（v3）+ `m4_auto_evidence_plan_20260819.md`
> 决策：前期开发阶段，**checklist 与 M4a 一起实施**（风险可控，省掉"只渲染不填证据"的中间态）

---

## 0. 决策与原则

1. **结束只能靠证据**：agent 终止由"证据是否满足契约"判定，不依赖 agent 主动 `report_done`。
2. **证据可回放、可审计**：每项带过/没过结论、证据事实、截图。
3. **代码兜底优先于 prompt 软约束**。
4. **机制必须挂在 agent 实际工作的作用域**：外层 loop 只跑 1 次，agent 的全部重复工作在内层 `_run_agent` 的 `_llm`/`_tools_node`。任何约束挂外层都会像 M2/M3 一样"跑一次、失效"。
5. **一起做、按依赖顺序**：checklist（下游）先建渲染管线，M4a（上游）填证据；依赖单向，写/读互补不冲突。

---

## 1. 背景（根因，简）

run `201539`：外层 loop 只跑 **1 次**（`Agent #1`=1、`Route: agent`=0），agent 的 144 步工具调用全在内层 `_run_agent`（119 个 LLM turn）。agent 在内层看不到实时验证进度，对已 passed 控件反复微调，25 分钟才收口。

根因链：
- **R1 作用域错配**：M2/Layer 3/全✓收口都挂外层，外层只跑一次，失效。
- **R2 信号消失**：待验证清单是外层快照，内层冻结；项一旦 passed 就消失，agent 失去"该停"信号。
- **R3 page_signature 失效**：列表项数变化致哈希每次不同，`LOOP_DETECTED` 凑不齐 3 次相同签名。
- **R4 内层缺"实时进度 + 全✓强制退出"**：`evaluate_verification` 每次重算 clause_state 但只用于 break，不喂回 agent。

---

## 2. 核心机制：同一条管线

```
感知 → (M4a) auto_matcher → _evidence_events → evaluate_verification → _clause_state
                                                            → (checklist) _render_checklist_view → 喂 LLM
```

- **M4a（上游，写）**：自动匹配器把确定性观察落成证据（`_evidence_events`）。
- **checklist（下游，读）**：把 `_clause_state` 渲染成 ✓/○ 清单，实时喂回 LLM。

单向依赖：checklist 不依赖 M4a（今天就能从手动 assert 的 evidence 算出）；M4a 依赖 checklist 建好的渲染管线（填了证据清单自动就渲染出来）。

---

## 3. 依赖顺序与实施步骤

```
① checklist 渲染（下游，独立可上）   → _render_checklist_view + _llm 节点注入
② M4a spec（上游前置）              → planner.txt 输出 spec + _parse_goal + build_contract 存 spec
③ M4a matcher（上游，依赖②）        → PREDICATES + _match_spec + auto_record_evidence（含去重）
④ M4a 集成                        → agent_node perceive 后调 auto_record_evidence
⑤ 测试 + prompt
```

约束：③ 依赖 ②（matcher 需要 spec 才知道"v0 → page_is"），④ 依赖 ③，① 完全独立放最前。

**验收闸门（风险隔离）**：① 完成后**先做一次真实用例验证**（确认"实时进度清单把 145 步压下来"），再推进 ②③④。这样 planner 映射风险（②③）与渲染问题（①）不会混在一起难定位——与 §8 风险 1 呼应。

---

## 4. Checklist 任务（①）

### C1：渲染纯函数 `_render_checklist_view(clause_state, contract)`
置于 `agents/verification.py`（或 `nodes.py`），由 `clause_state` + `contract` 派生勾选视图，证据引用从 `ctx._evidence_events` 按 `key+clause_id` 取最新 `artifact_ref`：

```
检查项清单（✓=已通过 ✗=已失败 ○=待验证）:
 [✓] v1::v1.0 默认每节课45分钟 | 证据: ui_text PASS 截图 evidence_v1_2.png
 [○] v4::v4.1 可添加至10节 | 待 assert_behavior_effect 补齐
 [✗] v5::v5.0 ... | 反证截图 evidence_v5_1.png
规则：○ 项结束前必须补齐证据；✓/✗ 项禁止再 open 编辑器微调或重复 assert。
```

### C2：内层 `_llm` 节点注入（根治点）
落点 `llm_runtime.py:275` `_llm` 节点内，LLM 调用前读 `_ctx._clause_state`，清单作为 **`SystemMessage`** 拼进消息。当前代码是 `lc.invoke(s["messages"])` 直接调用，需改为先建本地 `messages` 再注入：

```python
# 在 _llm 节点内，替换原 lc.invoke(s["messages"]) 为：
messages = list(s["messages"])                                      # 建本地副本
contract = getattr(_ctx, "_verification_contract", {}) or {}        # contract 取自 ctx（_llm 无该局部变量）
_checklist_msg = _render_checklist_view(
    getattr(_ctx, "_clause_state", None) or {}, contract
)
if _checklist_msg:
    messages.append(SystemMessage(content=_checklist_msg))          # 注入清单
r = _call_retry(lc.invoke, messages, on_error=_on_llm_error)        # 用本地 messages invoke
# return 保持 {"messages": [r]} 不变——只返回 LLM 响应；清单 SystemMessage 仅用于本次 invoke，未被写回 state，故不跨 turn 残留。
```

- **协议正确**：绝不能用 `ToolMessage`（无 `tool_call_id` 破坏 OpenAI 配对）。
- **同 turn 去重**：只在 `_llm` 节点注入一次，不随每条 tool call 重复追加。
- **不残留（token 不膨胀）**：清单 SystemMessage 仅参与本次 `lc.invoke(messages)`，且 `_llm` 只 `return {"messages": [r]}`，清单消息**不写回 sub-state**，故每轮重算、不跨 turn 残留。⚠️ 若未来有人把 `_llm` 改成 `return {"messages": messages}`（完整对话写回），清单会跨 turn 堆积，此"不残留"结论即失效。

### C3：内层收口加固 + 外层兜底
- 内层（主）：`llm_runtime.py:691-695` 的 `EVALUATOR_TERMINAL break` 已是全✓收口，无需重造，仅补日志。
- 外层（兜底）：`graph.py route_after_evaluator` 全✓时 `return "reporter"`，优先级全✓ > M2 > Layer 3。

### C4：prompt 硬规则（软约束）
"当清单全为 ✓ 时，必须停止所有操作；对 ✓/✗ 项微调或重复 assert 视为违规。系统每轮 LLM 调用前回灌最新清单，请依据它决策。"

---

## 5. M4a 任务（②③④）

### M1：spec 数据模型（②）
```python
clause = {
    "id": "v0.0",
    "claim": "课程表页面成功打开",
    "spec": {"predicate": "page_is", "target": "TimetableActivity", "expected": None},
}
```
- `planner.txt` 输出 `verification: [{claim, spec:{predicate,target,expected}}]`；映射不了 → `spec:null`。
- `_parse_goal`（nodes.py）解析 spec；`build_verification_contract` 存 spec。

### M2：谓词词汇表（Phase 0 落地）
| 谓词 | 事实 | 满足 | 确定性矛盾→authoritative FAIL |
|---|---|---|---|
| `page_is(target)` | current_app.activity | 匹配 | — |
| `page_contains(text)` | 页面文本快照 | 含 text | — |
| `element_exists(target)` | View Tree | 找到 | —（找不到=unknown） |
| `element_absent(target)` | View Tree | 找不到 | 找到→FAIL |
| `element_disabled(target)` | element.enabled | enabled==False | enabled==True→FAIL |
| `element_enabled(target)` | element.enabled | enabled==True | enabled==False→FAIL |
| `element_checked(target,bool)` | element.checked | 匹配 | 不匹配→FAIL |
| `list_count(anchor,n)` | 计数 | 匹配 | 不匹配→FAIL |

关键：**"找不到元素"对 `element_exists` 是 unknown（不写），对 `element_absent` 是 PASS**——不对称，避免误判。

### M3：自动匹配器 `auto_record_evidence` + `_match_spec`（③）
```python
def auto_record_evidence(ctx, contract, understanding, current_app):
    for v in contract["verifications"]:
        for clause in v["clauses"]:
            spec = clause.get("spec")
            if not spec: continue
            r = _match_spec(spec, understanding, current_app)
            if r is None: continue  # 无法判定 → 不写
            ctx._evidence_events.append({...})  # 含 key/clause_id/channel/status/authoritative/fact
```

**状态转移去重（防证据泛滥）**：
- 只在 clause 状态变化时追加（unknown→passed/failed）；已 passed/failed 的同状态证据不重复追加。
- **权威矛盾例外**：新出现的 authoritative FAIL 必须追加覆盖（哪怕 clause 之前已 passed），复用"确定性矛盾优先"语义。

### M4：集成（④）
`agent_node` 在 `perceive()` 之后（拿到 `understanding`/`current_app` 处）调 `auto_record_evidence`。

---

## 6. 测试（⑤）

- `tests/test_llm_runtime_checklist.py`：
  - `test_llm_node_injects_checklist_system_message`：`_llm` 节点 LLM 调用前 messages 含 `SystemMessage` 且反映最新 `clause_state`（非 ToolMessage）。
  - `test_llm_node_terminates_on_all_passed`：全 passed → 内层 `EVALUATOR_TERMINAL` 命中 break。
- `tests/test_auto_evidence_m4a.py`：
  - `test_match_spec_page_is / element_disabled / element_absent_asymmetry`：各谓词判定正确，含"找不到→unknown"不对称。
  - `test_auto_record_evidence_state_transition_dedup`：同状态不重复追加；权威矛盾覆盖追加。
- **`tests/test_planner_spec_mapping.py`（隔离 M4a 唯一真实风险）**：
  - 构造典型 claim，断言映射的 `predicate`/`target` 正确；映射不了降级 `spec:null`。
- 复用 `tests/test_verification_contract.py` 校验 `evidence[]` 挂载 `artifact_ref`。

---

## 7. 验收

1. **实时进度**：agent 每轮 LLM 调用前看到最新 ✓/○ 清单；已✓项不再被重复微调。
2. **自动成证据**：构造 `v0=page_is(TimetableActivity)`，agent 导航到该页后无需手动 assert，evaluator 自动判 passed。
3. **自动报差异**：构造 `v5=element_disabled(完成按钮)`，导航到该页（enabled=True）自动触发 authoritative FAIL + 差异报告。
4. **降级**：planner 映射不了的 clause → `spec:null` → 走手动 verify，不回归。
5. **端到端**：重跑联想日历"课程表"用例，`display_steps` 从 145 显著下降（目标 ≤ 40），全✓由内层强制收口。
6. **回归**：现有 291 测试全绿；M2/M3/Layer 2/Layer 3 不受影响。

---

## 8. 风险与缓解

1. **planner 映射质量（M4a 最大风险）**：LLM 把自然语言映射成 spec，predicate/target 可能写错。缓解：spec 有限 DSL + plan_review 可改 + `spec:null` 降级 + 单独回归测试（§6 `test_planner_spec_mapping`）。
2. **证据泛滥**：auto_matcher 每次 perceive 都跑，同一页面重复 perceive 会重复追加。缓解：状态转移去重（§5 M3）。
3. **自动 FAIL 误报**：`element_disabled` 读错元素（target 歧义）误判。缓解：`_match_spec` 里"元素没找到/歧义→不写"，只有唯一确定的元素才判定。
4. **自动匹配时机**：页面跳转瞬间漏 perceive。缓解：匹配器每次 perceive 都跑，agent 仍可手动补。

---

## 9. 不做的事

- 不重造证据/截图采集层（已存在，仅挂载）。
- 不修改 `verification_contract` 结构（checklist/spec 由 contract 派生）。
- 不用 RAG 解决本次循环。
- 不把 `page_signature` 改成"核心控件签名"（属 M4b 范畴）。
- 不做行为类谓词（`click→page_changed_to` 等，属 M4b，本轮不做）。

---

**一句话**：checklist（下游，渲染进度）与 M4a（上游，自动填证据）走同一条管线，一起按 ①→②→③→④→⑤ 依赖顺序实施；根治点在 `_llm` 节点每轮回灌实时清单（软约束治标），加上 `auto_record_evidence` 让验证不需要 agent 手动做（硬约束治本）。

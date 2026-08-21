# Run 213229 时间损耗分析与优化方案

- 日期：2026-08-21
- 关联 run：`logs/runs/213229_test-20260820_213229_*`（trace `215049_test-20260820_213229_trace.json`）
- 关联缺陷：本轮 `test_verdict=inconclusive` + `execution.exhausted`，最终两条 verification 标 `review_required`（含 v8.1「1、3、5周不可选」`deciding_evidence=null`、`unverified=true`）。
- 结论先行（两层根因）：
  - **战略根因（最顶层）**：`execution.mode=explore` + `mode_selection_reason=no_matching_plan`（trace 逐字确认）。本次慢**不是 explore 写得不快，而是"同一个任务第 N 次从零探索"**。治本方向是"让这个任务下次不 explore"，而非"让 explore 每步少花 30%"。
  - **战术根因（explore 内部）**：即便在 explore 内，损耗也高度集中在"前提条件重演"（seq 0–53 占 40% 步数，仅到达用例起点）而非"20 周逐点"（seq 88–107 仅 15%）；且验证常因预算耗尽/动作未闭环而丢失。

---

## 1. 数据局限（先讲清楚，不隐瞒）

trace.json 与 langchain.log **都不含步级 wall-clock 时间戳**：

- `trace.steps[]` 只有 `seq/tool/target/status/intent/observation/screenshot/tool_input`，无 `elapsed`；
- `langchain.log` 搜 `took/elapsed/seconds/duration` 全 0（仅 6 处被动名词）；
- 截图目录未落盘，无法用文件名时间戳还原间隔。

因此**无法精确到「第 X 步耗 Y 秒」**。下文是基于 `duration_seconds=1015.4`、`step_count=135`、`metrics` 动作分布、以及已读 trace result（启动期 `lifecycle_state=Bootstrapping`、RPC 故障恢复段）做的**结构性归因**——这是在不加埋点的前提下能做到的最精确分析。落地优化前，建议先在 trace / log 补充步级耗时埋点（见 §6），把估算转为实测。

---

## 2. 时间归因（135 步 / 1015.4s）

平均 ≈ 7.5s/步，但分布极不均。按动作分布拆分：

### 损耗类别 A — 启动期环境故障恢复（最大单段黑洞）
trace result 明确：`execution.lifecycle=Bootstrapping`；进入后先遇**锁屏**（`unlock_screen`）→ 解锁后 **App RPC 断开（rpc disconnected）** → 反复「重试连接 / 重启 adb / 重连」→ 才拿到首屏（seq 8–22 约 15 步）。

这部分**零真实业务点击**，全是设备/链路恢复，且失败重试疑似「盲等 + 重连」模式。保守估算 **200–300s（占 ~25%）**，是与被测功能完全无关的环境损耗。

### 损耗类别 B — 设备交互动作密集且串行
`metrics`：`click_count=55 / exact_click=51 / fuzzy_click=51`、`get_screen_info=34`。

- 每点一次几乎都伴随一次 fuzzy 回退 + 一次 `get_screen_info`（重新截屏解析 UI 树）；
- `perceiver.Perceiver` 每次感知都走 `_stable_dump_hierarchy`（`device/perceiver.py:184`，含 `settle_timeout=1.5s` 的稳定等待 + 多次 dump 轮询，`perceiver.py:155-170`），单次感知本身 1–3s；
- 其中「逐个取消 20 周」（seq 88–107 连续 20 次 chip 点击）是纯机械重复。注意：trace 强烈推断这 19 个 click 在**同一 turn 批量下发**（intent 全空、无中间 get_screen_info、共享 AIMessage，`llm_call_count=121<step_count=135`；铁证需查 langchain.log 的 turn 边界），**LLM 早已批量，瓶颈在工具执行侧**——每次 click 都触发 perceive→点击→截图→page_sig 再 perceive 整条感知链（见 §4 方案 2 P0 修正）。无「连续同构操作批处理」契约（`click.repeat` 只覆盖同元素连点）。

估算：55 次点击 ×（点击往返 + 截屏感知，含重复 perceive）≈ **250–350s（占 ~30%）**。

> 注：B 类真正大头不是"20 周逐点"（仅 15% 步数），而是 §3 所指 seq 0–53 的 **54 步前提条件重演（40% 步数）**——那才是 explore 内最大损耗，且属确定性操作，应由 fixture 前置消除（§10）。

### 损耗类别 C — 视觉/断言通道偏慢
trace steps：`vision_tap=4`、`visual_check=7`、`assert_page_contains=5`、`assert_element_exists=10`，合计 26 步。

这些是 **LLM/RAG 多模态往返**，单次比纯 click 更慢。估算 **26 步 × 5–10s ≈ 130–260s（占 ~20%）**。

值得注意：`assert_behavior_effect` 仅 1 次、`assert_page_state` 仅 2 次——**确定性断言稀疏**，LLM 大量用视觉而非工具断言（与上次结论一致：v8 最终未产生断言）。

### 损耗类别 D — LLM 决策推理
`metrics.llm_call_count=121`（135 步却有 121 次 LLM 调用，部分步骤多次）。每次 LLM 往返含长 system prompt + 工具 schema。

> **（Review 修正·上下文膨胀）trace 的 `token_usage` 实证数据**：
> `input_tokens=10,095,276`、`cached_input_tokens=8,736,384`（**86.5%** 命中缓存）、`llm_calls=121`
> → **平均 83,432 input tokens / 次调用**。即每次 LLM 调用平均要喂 8.3 万 token 的上下文（消息历史 + 屏幕 dump + system prompt + 工具 schema 持续膨胀）。**即使 86.5% 命中缓存，模型仍对全量 83K token 做 attention**，单次往返远不止 2s——故原估算 **121 × ~2s ≈ 240s 严重低估**。LLM 延迟随输入 token 数增长，D 类真实占比应上调（待 §6.0 回合级 `llm_elapsed_ms` 实测）。

原估算 **121 × ~2s ≈ 240s（占 ~24%）** 仅作下界参考；**真实 D 类损耗大概率 > 24%**，且随 explore 步数累积（上下文线性膨胀）持续恶化。分散在上述各步。

### 汇总表

| 类别 | 估算占比 | 性质 |
|---|---|---|
| A 启动 RPC/锁屏故障恢复 | ~25% | 环境损耗，与用例无关 |
| **前提条件重演（seq 0–53 建空课表）** | **~40%（步数）** | **确定性操作，应由 fixture 前置消除（§10 想法#1）** |
| B 点击+截屏感知（含 20 周逐点） | ~30%（步数 15%） | 工具执行侧重复感知，可批处理/返回增强 |
| C 视觉/断言 LLM 往返 | ~20% | 频率偏高，部分由 toggled 误报诱发（§4 方案 3a） |
| D LLM 决策推理 | ~24%（**低估**，见类别 D 修正） | 基础成本；explore 内偏高因无 plan 复用（§10 想法#3）+ 上下文膨胀（平均 83K tokens/调用） |

> 战略提示：A+B+C+D 的战术优化最多回收 explore 内损耗；但若执行 §10 的 fixture + plan 沉淀，seq 0–53 的 40% 步数可能**直接消失**（下次 guided 无需从零探索），杠杆远大于战术层之和。

---

## 3. 根因 → 「人工验证」的传导链（两层）

`execution.mode=explore`、`mode_selection_reason=no_matching_plan`（trace 逐字确认）。

### 第 0 层（战略根因）：无计划可复用
模式状态机（Bootstrapping/Direct/Guided/Explore）已存在且正确地落到"无计划→explore"。**"每一步都 LLM 判断"的真相是"第 N 次从零探索"的代价，不是 explore 的固有属性。** 治本方向：让这个任务下次不 explore（plan 沉淀 + fixture 前置），见 §10。

### 第 1 层（战术根因，explore 内部）：预算被两类损耗吃光
1. **前提条件重演（seq 0–53，占 40% 步数）**：直到 seq 54 才开始第一条 assert。seq 0–7 冷启动+找"更多"(NOT_FOUND→越界)，seq 8–22 锁屏/RPC 恢复，seq 23–53 菜单→课程表→列表→新建课表→切换当前课表→点空白小节→点+号。"创建一个空课程表"是**确定性操作**，却让 LLM 花 54 步探索——让"活"干了"稳"的活（§0 违反）。
2. 损耗 A（~25%）+ B（工具执行侧，详见 §4 方案 2 P0 修正）把剩余预算耗干 → `execution_status=exhausted`（1015s / 135 步满额）；
3. **验证时机 + 动作未闭环**：`pending_items` 措辞（"结束前必须补齐"，`nodes.py:582-589`）训练 agent 推迟验证；且 v6.0/v6.1 因"建课复合动作未跑完保存+回列表"而从未验证（非预算问题）；LLM 在 v7（seq 134）就停，**v8 从未执行**；
4. `deciding_evidence` 为 null、证据数 0 → verification 判 unknown → `review_required=true` → 人工验证。

> 原 Plan 把火力对准"20 周逐点"(seq 88–107，仅 15% 步数) 是**抓错了主因**；真正大头是 seq 0–53 的 54 步前提重演（40%）。两者都治，但杠杆不同（§10 的 fixture/plan 沉淀可一次性消掉 40%，远大于方案 1–5 战术之和）。

**即：不是断言工具假 PASS 的问题（F1/F2/F3 已修，chip 渲染带 `[SELECTED]`、无 192037 式误报），而是预算被损耗吃光，验证场景没被执行到。另：`toggled()` 对 chip 的假 FAIL 已由方案 3a 落地修复。**

---

## 4. 优化方案（六条，按 ROI 排序）

### 方案 1（优先级最高）：启动期健康检查 + 自愈上限 + 快速失败
**定位**：`device/controller.py`（`unlock` / ATX init `timeout=120` / `controller.py:108-112`）、`agents/llm_runtime.py` 的启动引导段（`lifecycle_state=Bootstrapping` 相关）。

**做法**：
- run 开始前增加**设备健康预检**：确认已解锁、ATX agent 在线、被测 App 前台、RPC 连通。任一不满足则进入**有界自愈**（重试 N 次，每次带日志；超过上限**快速失败**而非盲等）；
- 给 RPC 重连 / adb 重启加**总超时与次数上限**，避免「无限重试」吞掉业务预算；
- 自愈期间**不计入 step 预算**（或单独记 `bootstrap_steps`，与业务 step 区分），避免把探索额度耗在环境恢复上。

**预期**：消除 A 类 ~25% 纯损耗，且让「设备异常」从「静默耗预算」变为「明确失败可归因」。

### 方案 2（优先级高）：同构连续操作批处理
> **状态：✅ 已落地**（`click.py:513-562`，code Review 核实）——`click` 已加 `targets: list[str]` + `stop_on_first_failure` + 与 `repeat` 互斥 + `BATCH_OK` 聚合摘要。下文「做法/落地方式」保留为设计记录，请勿重复实现。

**定位**：`agents/llm_runtime.py` 的工具调度（已查证：`t.invoke(args)` 在循环里逐个执行 `AIMessage.tool_calls` 列表，**原生支持单回合多工具调用**，无需改运行时）；`tools/click.py`（已有 `repeat: int = 1`，上限 50，仅同元素重复，且对 loop-breaker 豁免）。

**§0 原则校准**：「一次点多个」是 LLM 的**活**（理解哪些目标彼此独立、可批量），代码不应替它决定"该不该批量"。但代码应给一个**稳定落点（地基）**：让"多个不同目标一回合下发"有契约化的表达方式，而不是逼 LLM 为每个目标各发一轮、每轮都重跑感知+决策。我们要消除的是"每点一次都强制 LLM 决策+截屏感知"的隐性结构成本，而非替 LLM 选目标。

**背景（已查证 + Review 修正）**：`click.repeat` 只解决**同一元素**的重复（如 "+" 按钮连点 N 次，`click.py:867-882` 固定 bounds 连点同一元素）。但 **「20 个不同 chip 各自一次 LLM 回合」是错误诊断**——trace 证明 seq 89–107 的 intent 全为空、共享同一 AIMessage，即 **LLM 早就批量下发了**（`llm_call_count=121 < step_count=135`，至少 14 次 tool_call 在多工具 turn 里顺带发出）。**真正损耗在工具执行侧，不在 LLM 决策侧**：每次 click 内部 `perceive()`（`click.py:583`，含 settle 轮询）→ 点击 → 截图 → `_tools_node` 再 `_build_page_signature` 又 perceive 一次（`llm_runtime.py:479` → `loop_control.py`）→ 前后 `current_app()` 各查一次。20 次 click ≈ 40 次 hierarchy dump + 20 次截图 + 40 次 dumpsys，**与 LLM 决策次数无关**。

**落地方式**：
- **核心处方不变但理由修正**：`click` 加 `targets: list[str]`（与 `repeat` 互斥）把 **20 次工具执行合并成 1 次**，省掉 ~38 次 dump + 19 次截图 + 19 步预算。
  - ❌ 旧理由"省 LLM 决策轮次"（LLM 早已批量，此维度无误可省）；
  - ✅ 新理由"省工具执行开销 + 省 step 预算"（20 次 tool_call → 1 次 tool_call，每次 tool_call 都触发上面整条感知链）。
- **运行时原生支持单回合多工具调用**（`llm_runtime.py:361` `for tc in last_ai.tool_calls`），但每发一个 tool_call 都走一遍感知链——所以 `targets=` 的价值是**减少 tool_call 次数**，不是减少 LLM 回合。
- **首选：prompt 引导 + `click` 加 `targets` 地基（零新工具）**：后端顺序点击、返回聚合摘要（命中/翻转/未命中，非完整截屏）；loop-breaker 对批量同构点击豁免（类比 repeat）。这是**契约地基**，LLM 仍决定"传哪些 targets"。
- **不采用新增 `click_many` 独立工具**：与 `click` 职责重叠，违背"契约收敛"。

**预期**：20 次 click tool_call → 1 次 `click(targets=[...])` tool_call，工具执行侧感知开销降 ~95%。**验收维度见 §6.3（已修正为 tool_call 次数，非 LLM 轮次）。**

> 边界守护（防补丁发散）：`targets` 是**稳定契约**（一个参数），不是针对"周 chip"的特例；未来批量滑动/输入应抽象为对应工具的 `targets/items` 参数。

### 方案 3（优先级中）：修好确定性断言的 ground truth + 减少视觉往返
**定位**：`tools/verify.py`（`toggled` / `assert_behavior_effect`）+ prompts（`agent_explore.txt` / `agent_common.txt`）。

**§0 校准（关键）**：方案 3 的解法**不是"说服 LLM 换工具"**，而是**修好工具给错的事实**——否则等于把"确定性工具给错 ground truth"的责任推给 LLM 兜底，恰踩 §0「补丁发散」雷区。

**3a. 修 `toggled()` 对 chip 的误报（Review 发现，原 §5 标"独立待定"，现提升为正 scope）**
> **状态：已落地（代码 Review 核实）**——`verify.py:615-627` 已实现该修复（当 `matched.checked is None` 时优先读 `element.selected`，即 F3 渲染的 `[SELECTED]`，再回退文本推断）。`selected` 字段确实从 UI dump 读取（`device/perceiver.py:373 selected=node.get("selected","false")=="true"`）。**团队照本文档"实施 3a"会撞上"已经做完了"——请勿重复实现，仅补回归验证。** 下文「真凶」保留为对 run 213229 当时旧代码行为的记录。

- **真凶（已查证，记录当时旧代码行为）**：run 213229 seq 75 `assert_behavior_effect` 返回 `FAIL: toggled(全选,on)`（checked:false），但"全选"是 TextView **chip，不是 switch/checkbox**。`verify.py:593-619`（旧代码）匹配逻辑对无 switch 分支的 chip 落到普通元素 → `checked is None` → `_infer_state_from_text` 因无 on/off 字样返回 `None` → `actual_checked=False` → FAIL。LLM 被迫 seq 80 补 `visual_check` 纠正。**这是 Plan 想压降的"C 类视觉往返"的直接来源之一**。
- **修法（已落地，契约收敛）**：`toggled()` / `assert_behavior_effect` 已识别 chip 的选中态——当 `checked is None` 时优先读 `element.selected`（F3 已渲染 `[SELECTED]`），为 True 即判 `actual_checked=True`，再回退文本推断。这属于"代码给 ground truth"，ROI 高于纯 prompt 引导。代码注释直接引用了"方案 3a（Plan §4）"。
- **预期**：chip 类选中态不再误报 FAIL → 省掉一轮 `visual_check` 视觉往返。**待办：补一条回归用例，对"已选中 chip + checked=None"断言 `toggled(label,on)` 应 PASS（非 FAIL）。**

**3b. prompt 引导（辅助，不越界）**
- 引导 LLM 优先用 `disabled()` / `assert_page_state` / `[SELECTED]` 等确定性、低成本断言；关键状态看到 `[SELECTED]` 缺失+点击未翻转即可判定，不必走多模态视觉。
- 注意：这是**辅助**，根因在 3a 的工具事实正确性，prompt 只是让 LLM 善用已正确的工具。

**预期**：C 类视觉往返下降（3a 直接消除一类误报源），v8 类状态验证更可能被执行到。

### 方案 4（优先级高）：顺手验证（opportunistic / 命中即验）
> **状态：✅ 已落地**（`nodes.py:584-593`，code Review 核实）——"结束前必须补齐"已替换为【顺手验证提醒】。下文"做法"保留为设计记录，请勿重复实现。

**定位**：`agents/nodes.py` 的 `pending_items` 注入（已查证 `nodes.py:575-589`：原措辞是"结束前必须补齐"——这把"验证时机"用代码硬编码成轮次制结尾补验，违反 §0；**现已改为【顺手验证提醒】**）；`auto_record_evidence`（`nodes.py:382`，已在 perceive 后把确定性事实自动落盘，是好地基）。

**§0 原则校准（关键）**：**"何时验证"是 LLM 的活**（理解页面、判断某操作结果是否满足了某 clause），代码不该用"结束前必须补齐"这种硬约束把时机钉死在轮次末尾。代码该做的地基是：**把"哪些 clause 当前已可被验证"（即它的 channel 已被某操作结果命中）作为事实透出给 LLM**，让 LLM 自己决定"现在顺手验"还是"稍后"。当前 Plan 早期版把方案 4 写成"命中即强制验"是越界——那是替 LLM 做时机判断，会变成补丁式 if/else。

**背景（已查证 = 你的问题 1 的 root cause）**：`pending_items` 段（`nodes.py:582-589`）明确写"结束前必须用对应 assert 工具补齐"，且"不得仅用肉眼观察替代"——这**训练** agent 把验证推迟到结尾统一做，而非像人类测试员"点完即当场确认"。结果：① 验证动作被推到后续独立回合（多一次 LLM+感知）；② 若预算在返回前耗尽（213229 的 v8），验证直接丢失 → 人工验证。

**落地方式（修正为 §0 合规）**：
- **改 `pending_items` 措辞 + 透出"可验事实"，不强制时机**：
  - 去掉"结束前必须补齐"的硬约束；改为："以下 clause 尚未拿到证据；**若你本回合的操作结果已使其可验（如点击后出现/消失目标元素、状态翻转、出现 `[SELECTED]`），请在当回合直接 fire 对应 assert；若尚不可验，继续探索即可，无需特地回头**"。
  - 同时把 `auto_record_evidence` 已自动落盘的确定性事实（如 `disabled`/`[SELECTED]` 命中）在 prompt 里**显式标注为"已自动取证，你可直接引用"**，减少 LLM 重复发 assert 的冲动（避免信息冗余导致的犹豫）。
- **不采用"命中即强制验"（原 D2）**：那是代码替 LLM 决定时机，且需解析"命中"语义 → 补丁发散风险，违反 §0。

**与方案 3 的区别**：方案 3 = 用什么手段断言更省（断言工具选择，LLM 的活，代码只给确定性工具地基）；方案 4 = 何时发起断言（时机，LLM 的活，代码只透出"可验"事实、不钉死结尾）。两者正交。

**预期**：v8 这类"操作即验证"场景从"推迟到独立回合、可能因预算耗尽丢失"变为"操作同回合内 LLM 自行顺手验"，直接切断 213229 根因链第 2 步。且不引入任何时机相关的 if/else 补丁。

### 方案 5（优先级最高·基础）：每步耗时统计（可观测性先行）
> **状态：🟡 部分落地**——工具级 `elapsed_ms` ✅ 已落地（`llm_runtime.py:481,632` + `run_trace.py:98`）；但**回合级 `llm_elapsed_ms` ❌ 未实现**（上轮升为"必选"，D 类最大隐藏成本仍测不到）。下方正文保留工具级实现记录，"回合级"段标注为待做。

**定位**：`agents/llm_runtime.py`（工具执行唯一入口 `t.invoke(args)` @475-478；entry 落库 @611）；`agents/run_trace.py`（step 构造是字段白名单，需同步改，见下方落库字段）。

**§0 原则校准**：耗时统计是**运行基础设施（地基）**——给 LLM 和开发者一个"稳"的观测面，不替任何判断。它不随场景增长、不引入特例补丁，是干净的契约化埋点，符合"稳"的定义。

**做法（已查证落点，零歧义）**：
- 在 `t.invoke(args)` 前后包 `time.perf_counter()`，得到单次工具调用 wall-clock（`elapsed_ms`）；
- 在 `entry` 字典追加 `"elapsed_ms": _elapsed_ms`；
- **同步在 `run_trace.py:86-104` 的 step 白名单加 `step["elapsed_ms"] = e.get("elapsed_ms","")`**（白名单非透传，否则 trace.json 不出现该字段）；
- **（必选，Review 升格）回合级 LLM 耗时 `llm_elapsed_ms`**：在 `last_ai.tool_calls` 循环外、LLM 调用返回后记录 `llm_elapsed_ms`，附在 turn 级。**原标"可选"低估了其价值——§2 类别 D 已证明 D 类是隐藏最大成本（平均 83K input tokens/调用，延迟随 token 数增长），若只测工具 `elapsed_ms` 会漏掉这个主导项。升为必选。**

**为什么放最高优先级**：没有步级耗时，方案 1–4 的力度全是"估算"（§2 已声明此局限）。先有数据，才能回答你"时间耗在哪""批处理/顺手验到底省了多少"——否则优化是盲调。它零业务风险、不影响任何现有行为（只加字段），应第一个落地。

**预期**：每次运行自带 `steps[].elapsed_ms`；可写一次性聚合脚本按 `name` 算 sum/mean/max，直接校准 §2 的 A/B/C/D 占比，并作为方案 2/4 验收的客观基线。

### 方案 6（由 §9(a) 升格）：瞬时 UI 捕获契约（prompt 通道，非代码扩展）
> **状态：🟡 部分**（捕获能力已具备 `click_and_check`；缺 prompt 通道契约）。落点仅为 prompt 引导，**click_and_check 代码无需改动**。

**定位**：`tools/perceive_tools.py:1411` 的 `click_and_check`（已注册于 `tools/__init__.py:400`）= 点击 → 等待 `wait_ms` → `snapshot_for_vision()` 立即截图 → 送 vision，并带 `verification_key`/`clause_id` 落盘。run 213229 seq 109 正是它成功抓到「请选择上课周数」toast 的实例。

**真缺口（Review 修正·能力已在手）**：不是"缺捕获工具"，而是"缺 prompt 通道契约"——LLM 在 seq 109 用 `click_and_check` 一次性抓到瞬时 toast 并落证后，seq 110 又用 `assert_page_contains` 对同一已消失的瞬时 UI 重复断言（必 FAIL → 再 111/112 收尾，白费 3 步）。

**做法（§0 合规：扩展既有契约用法，不新增工具、不堆 if/else）**：
- 在 `agent_explore.txt` / `agent_common.txt` 加契约指引：**瞬时提示（toast/加载动画/一闪而过的结果）统一走 `click_and_check` 一次性捕获并存证；捕获成功后禁止再用 `assert_page_contains` 对同一条瞬时 UI 二次断言**（它已消失，必 FAIL）。
- 这与 §9(a) 原「扩展 click_and_check 默认留存帧 / 新增 capture_toast」两条**代码扩展**建议互斥——能力已满足，不必改代码，只补 prompt 契约。

**§0 原则校准**：这是"代码给事实（click_and_check 已一次性给 ground truth 并存证）"+"LLM 判断何时用、不再重复断言"的干净分工，属契约收敛，无场景特例。

**断言边界（与方案 3b 的"确定性断言优先"不冲突，二者按 UI 性质分工）**：
- **静态常驻文本**（页面稳定存在的标题/状态/标签）→ 用确定性断言 `assert_page_contains` / `assert_page_state` / `disabled()` / `[SELECTED]`，低成本、可重复校验；
- **瞬时 UI**（toast / snackbar / 一闪而过的加载动画 / 点击后才出现又快速消失的结果）→ **必须走 `click_and_check` 一次性捕获并存证**，不可（也不该）用 `assert_page_contains` 二次断言——它已消失，必 FAIL。
- 即：方案 3b 倡导的"确定性断言优先"仅覆盖**静态**场景；瞬时场景的"确定性"由 `click_and_check` 的即时截图+vision 一次性给出，属同一收敛思路的不同落点。

**预期**：消除 seq 110/111/112 这类"瞬时捕获后重复断言"的白费 3 步；与方案 4（顺手验证）协同，让瞬时结果一次到位。

**优先级**：中。零代码风险（仅 prompt），可作为战术补强批次的一项（见 §8.2）。

---

## 5. 范围边界与 §0 原则护栏

- **本次只做「时间/预算损耗」优化，不碰断言语义**（F1/F2/F3 已交付，本次无关）。
- **§0 原则贯穿所有方案**：代码只做"稳"的部分（契约地基、确定性事实、运行基础设施），把"活"的部分（理解页面、选目标、定时机、语义判断）留给 LLM。判定某方案是否越界的标准：**是否引入了随场景增长的特例 if/else 补丁**——若是，说明该换 prompt / 模型 / 工具设计，而非加补丁。
- 方案 2 的 `click(targets=...)` 是**契约收敛**（一个稳定参数表达"批量不同目标"），不是针对周 chip 的特例；未来批量滑动/输入应抽象为对应工具的 `targets/items` 参数，而非在 `llm_runtime` 堆 if/else。
- 方案 4 **不采用"命中即强制验"**：那是代码替 LLM 决定时机，会引入"若操作 X 命中 clause Y 则……"的补丁发散，违反 §0。
- `toggled()` 对 chip 的误报**已纳入方案 3a 正式范围**（Review 发现它是"C 类视觉往返"真凶之一，属 §0 该修的"工具给错 ground truth"）。
- 方案 1（启动自愈）涉及 `device/controller.py` 与 `llm_runtime._call_retry`，改动前需先确认既有「retry/timeout」配置语义（`llm_runtime._call_retry` 已用 `._call_with_retry`，`device/controller` 已有 `timeout=120`），避免叠加重试导致超时翻倍。（方案 2/3a 已落地，不再属"改动前确认"范围。）

---

## 6. 验证方式

### 6.0 先补「每步耗时统计」埋点（可观测性先行，所有方案的前提）
**目标**：让每次运行都自带步级耗时，把本文「估算占比」转为实测，且能直接回答「时间耗在哪」。

**落点（已查证，零歧义）**：
- **计时点**：`agents/llm_runtime.py:475-478`，`t.invoke(args)` 是工具执行的唯一入口。在前后包 `time.perf_counter()`：
  ```python
  import time
  _t0 = time.perf_counter()
  try:
      output = str(t.invoke(args)) if t else f"UNKNOWN_TOOL: {name}"
  except Exception as e:
      output = f"ERROR: {e}"
  _elapsed_ms = round((time.perf_counter() - _t0) * 1000, 1)
  ```
- **落库字段**：在 `entry` 字典（`llm_runtime.py:611`）追加 `"elapsed_ms": _elapsed_ms`。该 entry 进入 `_tool_calls_log`。
- **⚠️ 必须同步改 `run_trace.py`（白名单，非透传）**：`build_run_trace` 的 step 构造是字段白名单（`run_trace.py:86-104`，只取 `tool_seq/name/target/status/intent/observation/screenshot/tool_input`，click 额外取 `match_mode` 等），**不会自动透传 `elapsed_ms`**。需在该白名单处加一行：
  ```python
  step["elapsed_ms"] = e.get("elapsed_ms", "")
  ```
  （原 Plan 两处写"run_trace.py 无需改"是硬伤，落地会踩空——已修正。）
- **字段语义**：`elapsed_ms` = 单次工具调用（含感知/点击/断言内部耗时）的 wall-clock，不含 LLM 推理（LLM 推理耗时属「回合级」，另算，见下）。
- **（必选，Review 升格）回合级 LLM 耗时 `llm_elapsed_ms`**：在 `last_ai.tool_calls` 循环外、LLM 调用返回后记录 `llm_elapsed_ms`，附在 turn 级而非 step 级，便于区分 D 类（LLM）与 B/C 类（工具）。**理由：§2 类别 D 实证平均 83,432 input tokens/调用（86.5% 命中缓存仍全量 attention），LLM 延迟随 token 数显著增长，D 类很可能是最大隐藏成本；只测工具 `elapsed_ms` 会严重低估总耗时。** 落库时同样需在 `run_trace.py` 白名单加 `turn["llm_elapsed_ms"]`（如字段可用），否则 trace 不出现。

**产出**：trace.json 每条 `steps[]` 带 `elapsed_ms`；可写一次性脚本按 `name` 聚合 `sum/mean/max`，直接对照 §2 的 A/B/C/D 估算验证。

### 6.1 重跑 213229 同类用例，对比
- 总时长是否从 ~1015s 下降（目标 A+B 合计回收 30–40%；预计 ~700–800s）；
- `step_count` 是否下降（尤其「逐个取消周」批处理后）；
- **按 `elapsed_ms` 聚合**：确认 A 类（启动恢复）/ B 类（点击+感知）/ C 类（视觉断言）的实际占比，校准 §2 估算；
- v8「1、3、5周不可选」是否能在预算内触发断言、`deciding_evidence` 不再为 null、`review_required` 降为 false；
- `execution.exhausted` 是否不再出现（或仅在不该出现的真实探索耗尽时发生）。

### 6.2 回归
跑既有断言/契约测试（`tests/test_tools_verify_anchor_resolve.py`、`test_verification_contract.py`、`test_agent_prompts_m4.py`），确认方案 3/4 的 prompt 引导不破坏既有断言行为。

### 6.3 方案 2（批处理）验收
确认「20 次 click **tool_call**」降为「1 次 `click(targets=[...])` tool_call」（注意：验收维度是**工具调用次数**，不是 LLM 决策轮次——LLM 早已批量，本方案省的是工具执行侧，见方案 2 P0 修正）；`click(targets=[...])` 的 `elapsed_ms` 应显著低于 20 次单独 click 之和（感知合并，dump/截图次数从 ~40/~20 降为 ~2/~1）。

### 6.4 方案 4（顺手验证）验收
构造「操作即验证」场景，确认 agent 在**同回合**内 fire assert 的比例上升（用 trace 中"操作 step 与紧随其后的 assert step 同 turn"计数）；`pending_items` 顺手验不应导致重复断言（用 `verification_key::clause_id` 去重核对）。

### 6.5 方案 6（瞬时 UI 捕获契约）验收
- **主验收**：在重跑 trace 中，不再出现「`click_and_check` 成功捕获瞬时提示（带 `verification_key`/`clause_id` 落证）后，同一 turn 或紧邻 step 又用 `assert_page_contains` 对**同一瞬时文本**重复断言并 FAIL」的序列——即 run 213229 的 **seq 109→110 模式消失**（抓到了又重复断言已消失的瞬时 UI）。
- **辅助验收**：瞬时类断言（`click_and_check` 路径）占比上升、`assert_page_contains` 路径仅用于静态常驻文本（见方案 6「断言边界」）。

---

## 7. 已知风险

- 方案 1 的「快速失败」若阈值过严，可能把「偶发一次 RPC 抖动」误判为设备不可用 → 需保留合理的重试带宽；
- 方案 2 的「批处理」需保证中间步骤仍有轻量有效性校验，否则坐标漂移/页面跳变会导致静默错点；
- 方案 3 若过度强化「确定性断言优先」，可能在「确实只能靠视觉」的场景（如自绘控件）丢失判断能力——保持 F3「代码只给事实、LLM 判断」的原则，不强行把视觉判断替换为工具断言。
- 方案 2 风险：批量点击若目标间页面发生跳变（如点 chip A 弹窗覆盖了 chip B），`targets` 后续项会点错。缓解：后端顺序执行时每点前用 `_resolved_anchor` 重解析 + 校验可见性，命中失败即停（`stop_on_first_failure` 默认 True）；这与 §0「代码给事实（命中/未命中）」一致，LLM 据摘要在下一轮补救。
- 方案 4 风险：去掉"结束前必须补齐"后，若 LLM 始终"忘记"回头验，可能漏验。缓解：保留 `report_done` 驳回机制（pending 非空则 DONE 被拒，`nodes.py:593-598` 已存在）——这是**稳定不变量**（结束只能经证据齐全），不是特例补丁，符合 §0；它只兜底"彻底漏验"，不强制"何时验"。
- 方案 6 风险：若 LLM 对「瞬时 vs 静态」判别失误，可能把本该 `assert_page_contains` 的静态文本也误用 `click_and_check`（多一次点击/截图开销），或反之把瞬时 UI 漏抓。缓解：prompt 契约已写明边界（静态→确定性断言，瞬时→click_and_check），靠 LLM 判断取舍，符合 §0「代码给事实、LLM 判断」。

---

## 8. 决策结论与落地顺序（含 2026-08-21 讨论 + §0 原则校准）

基于你提的 3 个问题与 §0 原则，Plan 已从"讨论稿"收敛为"可执行"，关键结论如下（**不再有"待定"**）。

> **⚠️ doc-code 漂移提醒（2026-08-21 Review 终审）**：本 Plan 写成时多项"战术快赢"尚为提案，但代码已实现；而 Plan 自己标"最高优先级/最高杠杆"的项（方案 1、想法 #1、回合级 llm_elapsed_ms、想法 #3 闭环）反而未做。下表「实现状态」以代码为准，**团队照文档实施前务必先看此列**，避免重复造轮子或误以为高杠杆项已完成。

### 8.0 各项实现状态总表（以代码为准）

| 项 | Plan 内优先级 | 落点 | 实现状态 | 说明 |
|---|---|---|---|---|
| **方案 1 启动自愈/健康预检/快速失败** | §4「优先级最高」 | `orchestrator.py:_preflight_device_health` | ✅ **已落地** | 设备连通+亮屏探针，异常快速失败（A 类"启动即失败"子集已消）；探活不伤数据 |
| **方案 2 click(targets=)** | §4「优先级高」 | `click.py:513-562` | ✅ **已落地** | targets + stop_on_first_failure + 与 repeat 互斥 + BATCH_OK 聚合摘要 |
| **方案 3a toggled 读 chip 选中态** | §4 | `verify.py:615-627` | ✅ **已落地** | checked is None 时优先读 element.selected（[SELECTED]） |
| **方案 3b 确定性断言引导** | §4 | `agent_explore.txt:31,60` + `agent_common.txt:70-75` | ✅ **已落地** | 三通道边界已写明（状态类→谓词 / 文字类→assert_page_contains / 观感类→visual_check）+ 3a 修复 |
| **方案 4 顺手验证措辞** | §4 | `nodes.py:584-593` | ✅ **已落地** | "结束前必须补齐"已换为【顺手验证提醒】 |
| **方案 5 工具级 elapsed_ms** | §6「地基」 | `llm_runtime.py:481,632` + `run_trace.py:98` | ✅ **已落地** | 步级 elapsed_ms 已埋 |
| **方案 5 回合级 llm_elapsed_ms** | 上轮升「必选」 | `llm_runtime.py:331` + `nodes.py:796-1047` + reporter metrics | ✅ **已落地** | 包裹 LLM 调用累计 `llm_elapsed_ms`，经 loop_meta→state→reporter metrics 透出（D 类最大隐藏成本现已可测） |
| **想法 #2 mode/plan_id per step** | §10 | `llm_runtime.py:633-634` + `run_trace.py:99-100` | ✅ **已落地** | 每步带 mode + plan_id |
| **想法 #1 fixture 契约** | §10「最高杠杆」 | `orchestrator.py:_preflight_fixture` | ✅ **已落地** | 编排层接好：clear_app_data + 冷启动 + 已空预检；`config.run_fixture_precheck` 默认关（兼容既有 run），原语早已就绪 |
| **想法 #3 沉淀→检索→guided 闭环** | §10「10x 杠杆」 | `nodes.py:1365 extract_candidate_plan` | 🟡 **部分** | 沉淀管线已接；端到端未验证（213229 仍 no_matching_plan） |
| **方案 6（新）瞬时 UI 捕获契约** | 中（原 §9(a)） | `agent_common.txt` 断言边界 + `click_and_check`（已注册） | ✅ **已落地** | prompt 通道契约已写（静态→确定性断言，瞬时→click_and_check 且捕获后禁二次断言）；代码无需扩 |
| **§9(c) click 返回 [n] 列表** | 中 | `click.py:1046 _build_clickable_summary` + `click.py:875` | ✅ **已落地** | 单/多目标 click 成功消息均附"当前可点列表: [n]"；原表"❌"为误判 |

> **优先级错配结论（历史记录，2026-08-21 已闭环）**：Review 早期曾指出"已落地全是战术快赢、高杠杆项未做"。截至本轮实现，**方案 1 / 想法 #1 / 方案 5 回合级 / 方案 6 / 方案 3b / §9(c) 已全部落地**，仅剩**想法 #3（沉淀→guided 端到端）**为 🟡 部分（管线已接，需真实 run 验证闭环）。原"优先级错配"已通过本轮实现消除——余下唯一高杠杆未全闭环项是想法 #3。

### 8.1 决策结论

- **§6 步级耗时埋点（工具级）**：✅ 已实现，不再阻塞其他项。但**回合级 `llm_elapsed_ms` 仍缺**（❌），D 类成本仍盲——需补。
- **方案 1（启动自愈）**：ROI 最高、与业务无关，但 ❌ 未实现，应排进下一步。
- **方案 2（批处理）**：✅ 已实现（见 8.0 表）。**注意：省的是工具执行侧（tool_call 次数），非 LLM 决策轮次**（P0 修正）。
- **方案 4（顺手验证）**：✅ 已实现。
- **方案 3（确定性断言占比）**：🟡 部分实现（3a 已落地，3b 仅精神到位）。

### 8.2 推荐落地顺序（修正后，按"杠杆高且未做"优先）

**下一步应优先补高杠杆未做项**（而非重复做已完成的战术快赢）：

1. **方案 1（启动自愈）**：消除 A 类 ~25% 纯损耗，零业务耦合，最先做。
2. **方案 5 回合级 llm_elapsed_ms**：补上才能测到 D 类最大隐藏成本（平均 83K tokens/调用），否则 §2 占比校不准。
3. **想法 #1（fixture 前置）**：战略杠杆最高（消 40% 前提重演），且**落地成本低**——`clear_app_data` 原语已注册在手（`tools/__init__.py:395`）、冷启动指引已存在于 `agent_explore.txt:44,53`，只需在现有工具上包一层 pre-run 自动 clear + "已空"预检 + 唯一命名 + 可回收 fingerprint，**不是从零写 setup**。建议从"战略层慢慢确认"里单独拎出，作为紧接方案 1 的第二步优先做。
4. **想法 #3（plan 闭环验证）**：10x 杠杆（下次不 explore），沉淀管线已接（`nodes.py:1365`），需实跑验证 213229 能否转为 guided。
5. 战术补强（可选、低风险，仅 prompt）：**方案 6 瞬时 UI 捕获契约**（click_and_check 已具备，仅补 prompt 通道）、§9(c) click 返回列表、方案 3b 显式工具偏好。

> 战略层（1/想法#1/想法#3）杠杆 > 战术层之和；已落地的方案 2/3a/4/5(工具级)/想法#2 仅回收 explore 内部损耗。想法 #1 因原语已备，是战略层里**最快可落地**的一项。

**战术兜底（若战略层短期不可落地）**：方案 1（启动自愈）→ 方案 5 回合级 `llm_elapsed_ms` → 方案 6（瞬时 UI 捕获契约）/§9(c)/方案 3b，依次收 explore 内部损耗。（方案 2/3a/4/5工具级/想法#2 已落地，不在此兜底清单内。）

> 每一步落地后跑 §6 验收；战略层落地后重点看：下次同任务是否 `mode=guided`（而非 explore）、前提步骤是否从 54 步降至 ~0、总时长是否从 1015s 量级降至 ~200s 量级。

---

## 9. 日志遗漏问题（Review 新增，Plan 原未覆盖）

以下 4 点来自对 run 213229 trace 的逐段复核，原 Plan §2/§3 完全未触及；其中 (a)(b) 正是"耗时/视觉往返"的真凶，优先级不低。

### (a) 瞬时 toast 的"捕获能力已具备"，真缺口是 prompt 通道契约（→ 升为方案 6）
- **现象**：seq 109 先用 `click_and_check(wait_ms=500)` **正确抓到**瞬时 toast（这一步是对的）；但 LLM 之后 seq 110 仍用 `assert_page_contains("请选择上课周数")` 重复断言（FAIL，toast 已消失），再 seq 111/112 补 `get_screen_info`+`assert_page_state` 收尾——**后 3 步（110/111/112）白费**。
- **（Review 修正·能力已在手）**：瞬时捕获能力**不是空白**——`click_and_check`（`perceive_tools.py:1411`，已在 `tools/__init__.py:400` 注册）就是"点击→等待 wait_ms→`snapshot_for_vision()` 立即截图→送 vision"的完整瞬时捕获工具，seq 109 正是它成功抓到「请选择上课周数」toast 的那一步。**真实缺口不是"缺捕获工具"，而是"缺 prompt 通道契约"**：LLM 在 seq 109 已经用 `click_and_check` 抓到了瞬时 toast 并带 `verification_key`/`clause_id` 落盘，seq 110 却改用 `assert_page_contains` 对同一瞬时 UI 重复断言（必 FAIL）。
- **（Review 升格）由此独立为「方案 6：瞬时 UI 捕获契约」**，落点为 **prompt 引导**（告知 LLM：瞬时提示应走 `click_and_check` 一次性捕获并存证，不要再用 `assert_page_contains` 二次断言已消失的瞬时帧），**click_and_check 代码本身无需动**——符合 §0（扩展既有契约用法，而非新增工具或堆 if/else）。
- 原「扩展 click_and_check 默认留存帧 / 新增 capture_toast」两条代码扩展建议作废（能力已满足，不必改代码）。

### (b) v6.0/v6.1 未验证，且原因不是预算（复合动作未闭环）
- **现象**：trace 里 v6.0「周数弹框点击确定后」、v6.1「已选周数的课程显示在列表」同为 `evidence_count=0`。seq 125–134：agent 设完周数、测了"取消"（v6.2）、测了背景色（v7），但**始终没点主弹窗"保存"把课程真正建出来、回列表确认课程出现**。
- **根因**：复合动作（建课 = 填名+时间+周数+颜色+保存+回列表确认）被 agent 拆散、少跑"保存+回列表"一段。方案 4「顺手验证」治"验证时机"，**治不了"动作闭环缺失"**。
- **建议**：prompt 层强调"复合预期结果须跑完整个动作闭环再断言"（如建课须见列表出现该课程）；属 LLM 的活（理解闭环），代码只透出"当前不在列表页/课程未出现"的事实。

### (c) click → get_screen_info 的重复感知（比"20 周逐点"更普遍的冗余）
- **现象**：click 的 observation 只返回"操作后页面: X + 关键值"，不返回完整 `[n]` 可点列表；LLM 下一步按 `index=n` 点，几乎每次 click 后补 `get_screen_info`（34 次）。即"click 内部 perceive 一次 + `page_sig_after` 又 perceive 一次 + `get_screen_info` 第三次"。
- **建议**：click 成功时把 post-click 的 `[n]` 可点列表**一并返回**，复用 `page_sig_after` 已 dump 的那份，省掉独立 `get_screen_info`。这是地基（返回更全的事实），让 LLM 不必为拿列表再发一次感知。方案 2 的 `targets=` 能部分缓解，但本项是更直接的通用修复。

### (d) fuzzy_match 率 93%（51/55）— 定位质量信号，非时间问题
- **现象**：几乎每次点击走"模糊匹配"，`semantic_click_count=0`。index/rid 精确定位未稳定命中预期 label。
- **评估**：与本轮"耗时"主题弱相关，仅作信号单列，**不展开、不纳入本方案 scope**，待后续定位质量专项排查。

### §9 与既有方案的衔接
- (a) 已升格为**方案 6（瞬时 UI 捕获契约，prompt 通道）**，独立成项，不再并入方案 3；
- (b) 属 prompt 层闭环指引，建议并入方案 4 同批处理（都在 `nodes.py` prompt 注入侧）；
- (c) 属 click 工具返回增强，建议与方案 2 的 `targets=` 同批改 `click.py`；
- (d) 观察项，不动作。

---

## 10. 战略层：让这个任务"下次不 explore"（头脑风暴新增，杠杆最大）

战术层（方案 1–6）优化的是"explore 内部损耗"。但 trace 的 `execution.mode=explore` + `mode_selection_reason=no_matching_plan` 说明：**本次慢的根因是"无计划可复用"，不是 explore 慢**。治本方向是"让这个任务下次根本不需要 explore"——这正是 `explore_mode_learning` 大重构的立项目标。以下三个想法把它与该战术 Plan 焊接起来。

### 想法 #1（最高杠杆）：把"前提条件"做成 fixture 契约，而非 LLM 探索
> **状态：🟡 部分**（原语已具备，`clear_app_data` 已注册：`tools/__init__.py:395` + `device_ops.py:227` + `controller.py:250`；`agent_explore.txt:44,53` 已有冷启动/脏状态指引）。**缺的只是把它们升级成"fixture 契约"**（pre-run 自动 clear + "已空"预检 + 唯一命名 + 可回收 fingerprint）——落地成本远低于"从零实现"，应作为紧接方案 1 的第二步优先做。
- **真凶（§3 已确认）**：seq 0–53（占 40% 步数）只是"到达用例起点"——创建一个空课程表是**确定性操作**，却让 LLM 花 54 步探索。这等于让"活"干了"稳"的活，违反 §0。
- **做法**：提供 `setup_fixture`（或 `pm clear` + 唯一命名建表脚本 + "已空"预检），**跑 run 前先判定/建立干净前置，跑完可回收**。`explore_mode_learning §2.7.2` 已点名残留数据（"Zzz"/"Aa"）污染决策空间，run 213229 seq 27 就是活例子。
- **收益**：单独消掉 40% 步数，比方案 1–5 战术之和对这个用例的收益都大。且属"契约收敛"（fixture 指纹 + 可回收 cleanup，§2.7.2 已有设计），非补丁。
- **§0 合规**：「是否已有一个空课程表」是可核实事实；「创建空课程表」是确定性契约——正是最该代码兜底的部分。

### 想法 #2（焊接战术与战略）：方案 5 埋点加 mode + plan_id
> **状态：✅ 已落地**（`llm_runtime.py:633-634` + `run_trace.py:99-100`，code Review 核实）。
- **扩展方案 5**：每个 step 除 `elapsed_ms` 外，再带 `mode`(explore/guided/direct) + `plan_id`。`run_trace.py` 白名单同步加这两字段（与 `elapsed_ms` 同批改）。
- **价值**：立刻能算"explore 模式下导航/前提步骤占总耗时多少""guided 比 explore 快多少"——这正是 `explore_mode_learning §8` 成功指标（`llm_call_count`/`duration_seconds: direct < guided < explore`）**缺的实测基线**。
- **零风险、便宜**：把战术埋点和战略立项论证焊在一起，不两拨人各做各的。

### 想法 #3（10x 杠杆）：确认 passed run 能否自动沉淀为 candidate plan
> **状态：🟡 部分实现**（沉淀管线已接 `nodes.py:1365 extract_candidate_plan`；但端到端"沉淀→检索→guided"未验证，run 213229 仍 `no_matching_plan`）。属 10x 杠杆，应实跑验证闭环。
- **背景**：`explore_mode_learning §5.7` 已定义 `run_recorder → plan_extractor → candidate plan` 沉淀链路，`plan_extractor.py` 已存在（23KB）。§5.3.1 要求 contract 先 `contract_pending_review` 两级人工确认后才可执行。
- **§11 已拍板 A 的影响（关键）**：A 决定**成就而非阻塞**自动沉淀——`§5.7` 的沉淀前置是"contract 已人工确认"，A 天然满足（首跑人工审 contract 是一次性成本，审后永久复用）；相反 B/C 因 contract 未人工确认反而不满足前置。故链路畅通："首跑（人工审 contract）→ 跑通 → 自动沉淀 candidate plan → 再跑 ≥2 次全对齐 → 升 trusted → 之后全自动 guided"。
- **做法**：先确认该沉淀链路在本环境是否端到端可用。**若可用，run 213229 这 135 步买的不是"一次结论"，而是"下次 guided 的 ~30 步"**——这才是该用例真正的 10x 杠杆，比方案 1–5 加起来都值。
- **与想法 #1 协同**：fixture 消除前提重演 + plan 沉淀消除探索重演 = 下次 run 直接 guided，135 步→~30 步。
- **（Review 补充·二次降延迟，此前未量化）**：plan 沉淀的收益**不止"步数 135→30"**。explore 的上下文是"135 步全量历史 + 屏幕 dump + 长 system prompt"，实证平均 **83,432 input tokens/调用**（§2 类别 D）；而 guided/direct 的上下文只常驻 **plan 摘要（~几K tokens）**。故 plan 沉淀还带来 **上下文 83K → ~几K 的二次降延迟**——每次 LLM 调用都更快，D 类损耗随 step 数降的同时单位调用耗时也降，双重收益。这是 fixture/plan 战略杠杆被低估的第二维度，应在验收时一并比对 `llm_elapsed_ms`（方案 5 回合级埋点）的 explore vs guided 差异。

### 战略层与战术层的衔接
- 方案 5 埋点（想法 #2）**应第一个做**——它是战术与战略的共同地基，且零风险。
- 想法 #1（fixture）与想法 #3（plan 沉淀）属 `explore_mode_learning` 大重构范畴，**本 Plan 不重复设计**，仅标注"战术优化前先确认这两者的可用性"，并把它们列为比方案 1–5 优先级更高的杠杆。
- 若 fixture/plan 短期不可落地，则退回纯战术层（方案 1–5）作为兜底，但仍先埋点拿 mode 数据。

---

## 11. 战略拍板项（已决策，2026-08-21）

`explore_mode_learning §5.3.1` 要求：**所有新生成的 contract（含首次 explore）都先 `contract_pending_review`，人工确认两级语义覆盖后才允许执行**。

**已拍板结论**：
- **首次必审（A）**：新用例首次 explore 的 contract 必须先人工确认两级语义覆盖，才生成可运行 plan。这是**功能**（"测试工程师工具"定位），非缺陷；产品暂不做"零接触自动测"。
- **contract 确认后永久免审**：同一用例的 contract 审一次即复用，后续不再重复审。
- **plan 晋升 trusted = ≥2 独立通过**：compatible key + 参数/验收语义一致，不是"跑过 1 次"。
- **"全自动"的精确定义** = contract 已确认 + plan 已 trusted → 之后每次自动 guided，人退出。direct（人工 flip）只是"再省 LLM 调用"的优化，不是"全自动"的必要条件。

**对想法 #3（自动沉淀 candidate plan）的影响**：A 决定**不是阻塞、而是成就**自动沉淀——`§5.7` 的沉淀前置是"contract 已人工确认"，A 天然满足；相反 B/C 因 contract 未人工确认反而不满足前置。故"首跑（人工审 contract）→ 跑通 → 自动沉淀 candidate plan → 再跑 ≥2 次全对齐 → 升 trusted → 之后全自动"是畅通的。

**运营预期**：一个新用例要"跑过 2 次独立成功"才进全自动——第 1 次人工审 contract + 跑通，第 2 次再独立跑通，第 3 次起自动。中间若第 2 次因环境漂移失败，plan 停在 candidate 不晋升。

> 回填：此项结论应回填到 `explore_mode_learning_plan_20260813.md §5.3.1` 与 Phase 1 验收口径（新 contract 未人工确认不可执行；已确认 contract 复跑跳过人工）。

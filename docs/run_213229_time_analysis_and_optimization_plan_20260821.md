# Run 213229 时间损耗分析与优化方案

- 日期：2026-08-21
- 关联 run：`logs/runs/213229_test-20260820_213229_*`（trace `215049_test-20260820_213229_trace.json`）
- 关联缺陷：本轮 `test_verdict=inconclusive` + `execution.exhausted`，最终两条 verification 标 `review_required`（含 v8.1「1、3、5周不可选」`deciding_evidence=null`、`unverified=true`）。
- 结论先行：**「耗时过长」与「需要人工验证」是同一根因的两个表现**——探索预算（135 步 / 约 1015s）被非业务损耗吃干，LLM 没机会执行 v8「二次建课验证」断言。

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
- 其中「逐个取消 20 周」（seq 88–107 连续 20 次 chip 点击）是纯机械重复，却**每步都走完整 LLM 决策链路**（截图→解析→决策→点击），无「连续同构操作批处理」。

估算：55 次点击 ×（点击往返 + 截屏感知）≈ **250–350s（占 ~30%）**。

### 损耗类别 C — 视觉/断言通道偏慢
trace steps：`vision_tap=4`、`visual_check=7`、`assert_page_contains=5`、`assert_element_exists=10`，合计 26 步。

这些是 **LLM/RAG 多模态往返**，单次比纯 click 更慢。估算 **26 步 × 5–10s ≈ 130–260s（占 ~20%）**。

值得注意：`assert_behavior_effect` 仅 1 次、`assert_page_state` 仅 2 次——**确定性断言稀疏**，LLM 大量用视觉而非工具断言（与上次结论一致：v8 最终未产生断言）。

### 损耗类别 D — LLM 决策推理
`metrics.llm_call_count=121`（135 步却有 121 次 LLM 调用，部分步骤多次）。每次 LLM 往返含长 system prompt + 工具 schema。估算 **121 × ~2s ≈ 240s（占 ~24%）**，分散在上述各步。

### 汇总表

| 类别 | 估算占比 | 性质 |
|---|---|---|
| A 启动 RPC/锁屏故障恢复 | ~25% | 环境损耗，与用例无关 |
| B 点击+截屏感知（含 20 周逐点） | ~30% | 可串行优化 |
| C 视觉/断言 LLM 往返 | ~20% | 频率偏高 |
| D LLM 决策推理 | ~24% | 基础成本 |

---

## 3. 根因 → 「人工验证」的传导链

1. 损耗 A（~25%）+ B（逐点无批处理）把 135 步预算耗干 → `execution_status=exhausted`（1015s / 135 步满额）；
2. LLM 在 v7（背景色，seq 134）就停，**从未触发 v8「再次创建课程→打开周数弹框→验证不可选」的断言动作**；
3. `deciding_evidence` 为 null、证据数 0 → verification 判 unknown → `review_required=true` → 人工验证。

**即：不是断言工具坏了（F1/F2/F3 已生效，chip 渲染带 `[SELECTED]`、无 192037 式误报），而是预算被损耗吃光，验证场景没被执行到。**

---

## 4. 优化方案（三条，按 ROI 排序）

### 方案 1（优先级最高）：启动期健康检查 + 自愈上限 + 快速失败
**定位**：`device/controller.py`（`unlock` / ATX init `timeout=120` / `controller.py:108-112`）、`agents/llm_runtime.py` 的启动引导段（`lifecycle_state=Bootstrapping` 相关）。

**做法**：
- run 开始前增加**设备健康预检**：确认已解锁、ATX agent 在线、被测 App 前台、RPC 连通。任一不满足则进入**有界自愈**（重试 N 次，每次带日志；超过上限**快速失败**而非盲等）；
- 给 RPC 重连 / adb 重启加**总超时与次数上限**，避免「无限重试」吞掉业务预算；
- 自愈期间**不计入 step 预算**（或单独记 `bootstrap_steps`，与业务 step 区分），避免把探索额度耗在环境恢复上。

**预期**：消除 A 类 ~25% 纯损耗，且让「设备异常」从「静默耗预算」变为「明确失败可归因」。

### 方案 2（优先级高）：同构连续操作批处理
**定位**：`agents/llm_runtime.py` 的工具调度 / 探索循环（step 生成与决策处）。

**做法**：
- 识别「同构连续操作」（如「逐周取消勾选」这类重复点击同一类控件、目标列表已知）：允许 LLM 一次性给出**操作序列**，agent 按序列机械执行 N 次点击，**仅在序列首末做 LLM 决策 + 感知**，中间步骤只做轻量确认（坐标命中 / 状态翻转）；
- 对「机械重复点击」引入**批量执行通道**，不每点一次都重跑完整感知+决策。

**预期**：B 类中「20 周逐点」这类从 ~20 步 LLM 决策降为 ~2–3 步，回收 ~30% 里的相当比例。

### 方案 3（优先级中）：提高确定性断言占比、减少视觉往返
**定位**：prompts（`agents/prompts/agent_explore.txt` / `agent_common.txt`）+ 工具调用偏好。

**做法**：
- 引导 LLM 优先用 `disabled()` / `assert_page_state` / `[SELECTED]`（F3 已就绪）等**确定性、低成本**断言，而非 `vision_tap` / `visual_check`；
- 关键状态（如「不可选」）明确动作指引：看到 `[SELECTED]` 缺失 + 点击未翻转 → 即可判定，不必走多模态视觉。

**预期**：C 类视觉往返下降，且 v8 这类「状态验证」更可能被执行到（断言成本低→预算够用）。

---

## 5. 范围边界

- **本次只做「时间/预算损耗」优化，不碰断言语义**（F1/F2/F3 已交付，本次无关）。
- `toggled()` 统一接入 `_resolve_anchor` 仍是**独立待定项**（trace 显示「全选」TextView 走旧匹配器返回 FAIL→vision 补救），不在本方案范围，但可在方案 3 的「断言占比」讨论里一并评估。
- 方案 1–3 都涉及 `llm_runtime.py` 与 `device/*`，改动前需先确认既有「retry/timeout」配置语义（`llm_runtime._call_retry` 已用 `._call_with_retry`，`device/controller` 已有 `timeout=120`），避免叠加重试导致超时翻倍。

---

## 6. 验证方式

1. **先补埋点**（建议优先）：在 trace step 与 langchain.log 增加步级 `started_at/ended_at/elapsed_ms`，把本文「估算占比」转为实测，再据此调整方案 1–3 的力度。
2. **重跑 213229 同类用例**，对比：
   - 总时长是否从 ~1015s 下降（目标 A+B 合计回收 30–40%；预计 ~700–800s）；
   - `step_count` 是否下降（尤其「逐个取消周」批处理后）；
   - v8「1、3、5周不可选」是否能在预算内触发断言、`deciding_evidence` 不再为 null、`review_required` 降为 false；
   - `execution.exhausted` 是否不再出现（或仅在不该出现的真实探索耗尽时发生）。
3. **回归**：跑既有断言/契约测试（`tests/test_tools_verify_anchor_resolve.py`、`test_verification_contract.py`、`test_agent_prompts_m4.py`），确认方案 3 的 prompt 引导不破坏既有断言行为。

---

## 7. 已知风险

- 方案 1 的「快速失败」若阈值过严，可能把「偶发一次 RPC 抖动」误判为设备不可用 → 需保留合理的重试带宽；
- 方案 2 的「批处理」需保证中间步骤仍有轻量有效性校验，否则坐标漂移/页面跳变会导致静默错点；
- 方案 3 若过度强化「确定性断言优先」，可能在「确实只能靠视觉」的场景（如自绘控件）丢失判断能力——保持 F3「代码只给事实、LLM 判断」的原则，不强行把视觉判断替换为工具断言。

---

## 8. 决策待确认

- 是否先执行 §6 的**步级耗时埋点**（推荐，能把估算变实测，避免凭直觉定优化力度）？
- 三条方案是否全部纳入，还是先落地 ROI 最高的方案 1（启动自愈）？

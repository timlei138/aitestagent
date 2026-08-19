# 验证不符未报 Bug 与 Agent 重开探索 复盘（基于 105856_test-20260819_105856）

> 本文档只做根因分析与修复方案，**未修改任何代码**。
> 分析对象：`logs/runs/105856_test-20260819_105856_langchain.log` 与配套 `logs/runs/111808_test-20260819_105856_trace.json`（同一 run 的结构化 trace）。
> run 终态：`USER_STOPPED`（用户在 11:18 手动 `/api/run/stop`，run 被取消，非 bug 复发或崩溃）。
> 本文为 v2 修订版，已根据评审结论修正：① Y2 交互语义倒置；② Y1 关键词补丁违反契约收敛；③ 两处事实错误（模式判定、改动生效表述）；④ 补全 v4 channel 漂移作为问题2 的确定性根因。
> 本文为 v3 修订版（定稿），根据实施层代码核对补充三条实现口径：① `enabled` 已在模型中（D1 成本极低）；② v5 漏判的结构性入口 = 置灰/禁用不在 `_STATE_CLAIM_MARKERS`（D1/D2 须由结构化 `expected` 驱动、确定性矛盾先于 LLM PASS 检查）；③ C1 采用"通道取并集"而非 reorder（避免副作用漂移）；④ 明确读 `enabled` 而非 `clickable`。战略层（D1/D2/D3 + C1/C2）维持 v2 不变，仅细化实施细则。

---

## 0. 一句话结论（修订）

本次 run 暴露的不是上一版的"缺两条软规则"，而是**三个确定性的代码层缺陷**，叠加 Agent 的探索惯性：

1. **验证判定没有"确定性事实与 claim 相反 → authoritative FAIL"的不变量**，导致 v5 的反向证据被放过、未报 bug（问题1）。
2. **v4 因 `_default_channels_for_claim` 的 marker 优先级 bug 被错配 channel，最终 `unknown`（evidence_count=0），使整体 `verdict=inconclusive`**，架空了 `route_after_evaluator` 已有的"passed/failed → reporter"终止条件——这是问题2「图层面不自动终止」的确定性解释（问题2 根因之一）。
3. **Agent 在验证收尾后自发延展第二轮探索**（re-import 测日期清除），这是问题2 的 Agent 侧主因（问题1 的延续）。

修复重心必须从"加语义/关键词规则"移到"**加确定性元素状态工具 + authoritative FAIL 不变量 + 修 channel marker 优先级**"，并让终止条件落到代码（对 unknown 设解析上限强制终），而不是只写进 prompt。

---

## 1. 环境事实（已核对，修订版）

| 项 | 上一版错误表述 | 实际（已核对 trace） |
|---|---|---|
| 运行模式 | "direct 模式" | `execution_mode` 在 trace 未直接出现，但 `lifecycle_state=Bootstrapping`、`mode_selection_reason=no_matching_plan`、`CONTRACT_REVIEW_REQUIRED`/`verification_contract_not_approved` 均为 0 次 → 实为 **explore（探索）模式** |
| 改动 1+2 是否生效 | "direct 模式未触发，改动未触发" | **改动 1/2 是常驻生效的**：正是它们让契约在 plan_review 被 approved，所以 `mode_selection_reason` 才是 `no_matching_plan` 而**全程无 `CONTRACT_REVIEW_REQUIRED`**。未触发的是"旧契约漂移 bug 场景"，不是"改动没生效" |
| 改动 4 路由 | 生效 | 生效（`route_after_mode_selection` 仅一次，无二次 plan_review） |
| 整体 verdict | 未提 | `test_verdict: inconclusive`（因 v4 unknown 卡住） |

---

## 2. 问题1：验证条件与现实不符，却没有报 Bug

### 2.1 现象（证据，已核对）

被测页（课程表确认页）验证契约含：

- **v5**：清空课程表名称和学期开始时间后，**完成按钮置灰不可点击**。
- **v6**：信息完整时，点击完成可正常进入列表页。

v5 实际证据（trace）：

| seq | 动作 | 关键证据 |
|---|---|---|
| 67 | `get_screen_info` + `visual_check` | UI Tree 里完成按钮 `clickable=true`；视觉"按钮黑色（与标题同色），非蓝色、非明显置灰浅灰，通常表示可点击" |
| 74 | `click_and_check(完成)` | 点击完成 → **弹出 Toast「课程表名称不能为空」**，页面未跳转（被表单校验拦截） |
| 82 | `assert_behavior_effect` | 汇总："按钮黑色（未视觉置灰）即使名称为空，且 Toast 证明完成被拦截" |

**关键事实（评审纠正）**：Android 上
- 「置灰不可点」= `View.enabled=false` → 按钮**不响应点击**，压根不会弹 Toast；
- 「点击弹 Toast『课程表名称不能为空』」= `enabled=true` → 按钮**能点、点击处理器跑了、表单校验后才弹 Toast**。

所以"Toast 拦截"**恰恰证明按钮可点（未置灰）**，它与 claim「置灰不可点」**方向相反**——而不是什么"禁用语义的体现"。Agent 当初把"点击弹 Toast"合理化成"完成被拦截=不可点"，正是误判的根源。

但 `evaluate_verification` 最终把 **v5 判为 `passed`**，且全程**无 `report_bug`**。随后 Agent 还去验证 v6 来对冲 v5。

### 2.2 根因（修订）

1. **判定缺少"确定性事实与 claim 相反 → authoritative FAIL"的不变量。**
   当前判定允许"UI Tree clickable=true + Toast 可点"被归约为 passed（存在 `review_required`/模糊判定把"完成被拦截"误读成"不可点"的等价证据）。但真正的 ground truth 在 UI Tree：`clickable=true / enabled=true` 与 claim「不可点击/置灰」**直接矛盾**。缺少一条"确定性事实与 claim 期望相反 → 强制 failed"的稳定不变量。

2. **状态类 claim 的正确口径（纠正上一版的 Y2 倒置）。**
   「置灰/可点」是 UI Tree 里的 `enabled`/`clickable` **确定性事实**，应由工具直接读出 ground truth，**不是靠"颜色黑不黑"猜，也不是靠"点了弹不弹 Toast"反推**。
   - 上一版 Y2 说"当视觉与交互冲突时以交互（Toast）为准、Toast 体现禁用语义"——这是**反的**，会重演 Agent 误判（把 Toast 再次当成"禁用/拦截"）。
   - 正确口径：**UI Tree 的 `enabled/clickable` 才是权威**；Toast 只是"按钮可点且做了校验"的旁证，用于佐证"未置灰"，不能反向解读成禁用。

3. **failed 与 report_bug 脱钩**。claim 与真实行为稳定矛盾（真实是 Toast 而非置灰）本质是 **App 实现与需求预期不符 → 应判 bug**，但当前"验证失败"不会触发 bug 记录。

4. **Agent 用 v6 对冲 v5**。v5 证据已反向，Agent 仍去验 v6（填好→可点击），试图"凑齐相反状态让整体通过"，违背"每条 clause 独立判定"。

### 2.3 推荐修改（修订：确定性工具 + 不变量，非关键词补丁）

- **D1（确定性元素状态工具，P0，成本很低）**：加一个**断言工具**把已有的 `enabled` 暴露成结构化证据（如 `{enabled: true, clickable: true, expected_disabled: true}`）。**无需新建感知层**：`enabled` 已被 `perceiver.py:372` dump（`enabled=node.get("enabled","true")=="true"`），且 `click.py:695` 已用 `enabled is False` 判禁用并返回 `DISABLED` 提示——所以 D1 仅是"把既有 `enabled` 包装成可执行的事实断言"，代价极小。
  - **细则（防复发，评审口径①）**：状态类 claim 的"期望"必须来自**工具调用的结构化 `expected` 字段**（agent 声明 `expected_disabled=True`），**不是从 claim 文本抠关键词**——否则 D2 会退化成 Y1 的关键词补丁。
  - **细则（评审口径③）**：必须读 `enabled`（置灰 = `View.isEnabled()=false`），**不能只读 `clickable`**（`click.py:695` 注释已说明 disabled 按钮 clickable 仍可能为 true）。读错字段会把"置灰"误判成"可点"。
- **D2（authoritative FAIL 不变量，P0）**：判定层规则——**当确定性事实与 claim 期望相反 → authoritative FAIL**。例如 claim 期望"不可点/置灰"（即 `expected_disabled=true`），而工具读出 `enabled=true`，直接 FAIL。**这是一条有限稳定的不变量，不依赖关键词清单**（不写"置灰/灰显/disabled/置灰态…"的 if/else，避免补丁发散，守住"契约收敛"）。
  - **细则（评审口径①，关键）**：**确定性矛盾检查必须先于"任意 PASS → passed"**。当前 `evaluate_verification` 若先按"有任意通道 PASS 即 passed"汇总，LLM 通道（`click_and_check`/`vision_verify`）的"合理化 PASS"会压过确定性事实，D2 形同虚设。正确顺序：`evaluate_verification` 先查"确定性事实与 expected 相反" → 命中即 authoritative FAIL（**覆盖 LLM 判定通道的 PASS**）；再走普通通道汇总。这样 v5 的 click_and_check PASS 无法再把"enabled=true 可点"合理化回 passed。
  - **结构性入口（评审口径①事实）**：v5 当初漏判，根因是 `verification.py:16-18` 的 `_STATE_CLAIM_MARKERS` 只含"页面/activity/状态/开启/关闭/勾选/选中/打开/开关"，**无"置灰/禁用/不可点"**，导致 v5「完成按钮置灰不可点击」未命中任何 marker → 走 default 全通道（含 `click_and_check`/`vision_verify` 两个 LLM 通道）→ 误判从此漏入。D1/D2 落地后，状态类期望改由结构化 `expected` 字段驱动，不再依赖 marker 命中，该入口自然封闭。
- **D3（FAIL 即报差异，P0）**：`authoritative FAIL` 触发**差异上报**（含 claim、实测事实、工具证据引用），**只报"差异"、不自动定性 bug**——bug vs 用例错误的定性交下游/人工（与 `discrepancy_detection_core_plan_20260819.md` §7/§11.4 对齐）。让"验证不符"与"报差异"闭环，而非静默 failed。
- **D4（禁对冲式补验，P1，prompt 软约束）**：不得为证明整体通过去补验与已失败 clause 相反状态的 clause 来对冲。v5 FAIL 就记 v5 FAIL + 报差异；v6 是否验证由任务显式要求，不自动补。

---

## 3. 问题2：Agent 验证完后又从"导入图片"重新开始

### 3.1 现象（证据，已核对）

trace 收尾工具序列：

| seq | 动作 | intent 摘要 |
|---|---|---|
| 82 | `assert_behavior_effect` | v5 证据采集完成（toast + 页面未走） |
| 83-90 | `type_input(我的课程表)` → `click(完成)` → `assert_page_state` → `assert_page_contains` | **v6 验证完成**，导航到 TimetableListActivity，新课程表已建 |
| **91** | `click` | **"Back in TimetableActivity. Let me re-import to further test the date-clearing mechanism (p…"** |
| 92-94 | `click` ×2 | 重新进入导入流程（导入图片） |

即 v5/v6 全部完成后，Agent 自行决定"回到 TimetableActivity 重新导入图片，进一步测试日期清除机制"，开启第二轮。用户 11:18 手动 stop 时正在此第二轮。

**排除误判**：
- 非 App 重启：`launch_app`/`force_fresh` 真实 trace **0 次**（仅 system prompt 文本）。
- 非 run 重跑：service.log `10:58:56` start → `11:00:25` confirm → `11:18:06` stop，单次 run。

### 3.2 根因（修订：两个，确定性 + Agent 侧）

**根因 A（图层面不终止，确定性代码缺陷）——评审补充，上一版遗漏**：
- `test_verdict: inconclusive` 已核对 → 因为 **v4 是 `unknown`（`evidence_count=0`）**。
- v4「显示非本周课程开关切换」最终 `unknown` 的原因，是 `_default_channels_for_claim` 的 marker 优先级 bug（`agents/verification.py:14-15, 42-46`）：`_TEXT_CLAIM_MARKERS` 含 `"显示"`，**先于** `_STATE_CLAIM_MARKERS` 被检查，v4 命中 TEXT 分支 → channels = `[ui_text, click_and_check, page_state, element_state]`，**behavior_effect 被挤掉**。而 v4 是开关切换，本该靠 behavior_effect 判；结果 Agent 的 `assert_behavior_effect` PASS 被 channel 白名单过滤 → `evidence_count=0` → `unknown`。
- `verdict` 计算（`verification.py:128-135`）：任一 clause 非 passed/failed 即 `inconclusive`。**v4 unknown → 整体 verdict 卡 inconclusive → `route_after_evaluator` 已有的"passed/failed → reporter"终止条件永远不触发**。所以"所有 clause 已判 passed/failed"在当前不成立，X1 单独修不掉问题2——必须先让 v4 不再 unknown。
- 注：`verification.py:32` 注释本身已记录该已知 bug："is wrongly marked inconclusive while the frontend (which shows any PASS) looks..."。

**根因 B（Agent 侧探索惯性，问题1 的延续）**：
- v4/v5/v6 验完后 Agent 仍自发 re-import 去测"日期清除机制"（契约外）。这反映执行层缺"验证收尾即停"约束，且 Agent 对"已失败/已完成的 clause"不甘心、继续延展。

### 3.3 推荐修改（修订：代码层终止 + prompt 软约束）

- **C1（修 channel marker 取并集，P0 — 确定性根因）**：`_default_channels_for_claim`（`verification.py:22-54`）当前是"首命中即返回单组通道"的互斥逻辑，导致 v4「显示非本周课程**开关**切换」命中 TEXT 分支（"显示"在 `_TEXT_CLAIM_MARKERS:15`）丢失 `behavior_effect` → `unknown`。
  - **采用"通道取并集"而非"reorder STATE 到 TEXT 前"**（评审口径②）：把 STATE 提到 TEXT 前虽修好 v4，但会误伤「显示…状态/开关」这类同时含 TEXT+STATE 的混合 claim（丢掉 `ui_text` 可能又变 `unknown`），即换一个场景出新的漂移。更稳的是：**命中多类 marker 时合并各类通道（TEXT+STATE 取 `ui_text+click_and_check+page_state+element_state+behavior_effect+vision_verify`）；仍歧义时回退 default 全通道**。这是"单一稳定规则"，不会因场景不同反复漂移。
  - 目标：v4 这类开关切换能走 `behavior_effect` → 不再 `unknown` → 整体 verdict 不再卡 `inconclusive` → `route_after_evaluator` 终止条件恢复。
  - **注（与 plan 对齐）**：最终方案以 `discrepancy_detection_core_plan_20260819.md` Phase 0 的"**删除 `_default_channels_for_claim` 关键词推断、channel 改由确定性/权威标志决定**"为准；本处"取并集"是过渡/兜底态。二者不冲突——删推断更彻底，从根上消除漂移空间。
- **C2（终止落到代码，P0 — 认可 X1 方向但对齐代码）**：在 evaluator / loop 实现硬终止——**当所有 clause 均已 passed/failed → terminal（进入 reporter）**；且对 `unknown` 设**解析上限**（如重试 N 次或单 clause 停留步数上限）后**强制终**（unknown 不阻塞整体终止）。注意 `route_after_evaluator` 已有雏形，是被 v4+v5 两个问题架空了，修复后它即可正常工作，无需重写。
- **C3（契约外不越界，P1，prompt 软约束）**：验证契约未列的行为不在本轮自动测试范围；发现疑似问题只记录 observation / 建议，不自发的第二轮验证。
- **C4（探索预算护栏，P1，prompt 软约束）**：验证完成后额外操作数默认 = 0；任何契约外探索先停下来向用户/规划层确认，不自行续跑。

---

## 4. 两个问题的关联与共性结论（修订）

| 维度 | 问题1（不符未报 bug） | 问题2（验完重开探索） |
|---|---|---|
| 表现 | v5 反向证据判 passed，无 bug 上报 | v5/v6 完成后自发重走导入 |
| 根因 | 缺"确定性事实与 claim 相反→authoritative FAIL"不变量（D2） | A: v4 unknown→verdict inconclusive→route 终止被架空（C1）；B: Agent 探索惯性（C3/C4） |
| 共同点 | **都源于"验证层确定性不足 + 终止条件被架空"**，而非缺软 prompt 规则 | 同左 |

**专业结论（修订）**：

上一版把修复做成"关键词补丁 + 倒置的交互语义"，违背"代码给 ground truth / 契约收敛"。正确方向是：

1. **代码层给 ground truth**：用 UI Tree 的 `enabled/clickable` 确定性事实判定"置灰/可点"，不靠颜色或 Toast 反推（纠 Y2 倒置）。
2. **一条稳定不变量替代关键词补丁**：确定性事实与 claim 相反 → authoritative FAIL → 报差异（纠 Y1 发散）。
3. **修 v4 channel 漂移**：这是问题2「图层面不终止」的确定性根因，上一版遗漏；修好后 `route_after_evaluator` 雏形即可生效。
4. **终止落到代码**：clause 全 passed/failed → terminal；unknown 设解析上限强制终（X1 方向对，但必须落代码而非仅 prompt）。

---

## 5. 落地顺序（采用评审建议，修订）

**P0（消除本次两个现象 + 守原则）**：

1. **D1** 确定性元素状态工具（读 `enabled/clickable/checked`）→ 结构化事实；
2. **D2** 判定层：确定性事实与 claim 相反 → `authoritative FAIL`（一条不变量，非关键词）；
3. **D3** `authoritative FAIL` → 触发差异上报；
4. **C1** 修 `_default_channels_for_claim` marker 优先级（或保状态 claim 的 behavior_effect），让 v4 不再 `unknown`；
5. **C2** 终止落代码：clause 全 passed/failed → terminal；unknown 设解析上限后强制终。

**P1（prompt 软约束，次级加固）**：D4（禁对冲）、C3（契约外不越界）、C4（探索预算）。

**一句话总结**：现象抓得准，但旧方案的 Y1/Y2 把修复做成了"关键词补丁 + 倒置的交互语义"，违背"代码给 ground truth / 契约收敛"；方案重心应从"加语义规则"移到"加确定性元素状态工具 + authoritative FAIL 不变量 + 修 v4 channel 漂移"，并让终止条件落到代码（对 unknown 设解析上限强制终）。

---

## 6. 附录：关键证据索引

- 人工 confirm：service.log `11:00:25` `human_decision: confirm`（goal 含"必填项为空时完成按钮置灰"）。
- run 终止：service.log `11:18:06` `/api/run/stop` → `USER_STOPPED`。
- 模式事实：trace `lifecycle_state=Bootstrapping`、`mode_selection_reason=no_matching_plan`、`CONTRACT_REVIEW_REQUIRED`/`verification_contract_not_approved` 均 0 次 → explore 模式（非 direct）。
- 改动 1+2 常驻生效：全程无 `CONTRACT_REVIEW_REQUIRED`，因契约在 plan_review 已 approved。
- v5 反向证据：trace seq 67（UI Tree `clickable=true` + 视觉"黑色非置灰"）、seq 74（Toast「课程表名称不能为空」= enabled=true 可点）、seq 82（汇总"未置灰+Toast 拦截"）。
- 重开探索起点：trace seq 91 intent「re-import to further test the date-clearing mechanism」，seq 92-94 重新导入。
- **v4 unknown 根因**：`agents/verification.py:14-15` `_TEXT_CLAIM_MARKERS` 含"显示"先于 `_STATE_CLAIM_MARKERS` 命中 → v4 channels 漏 `behavior_effect` → `assert_behavior_effect` PASS 被过滤 → `evidence_count=0` → `unknown`；`verification.py:128-135` 任一 unknown → `verdict=inconclusive`，架空 `route_after_evaluator` 终止。
- 整体 verdict：trace `test_verdict: inconclusive`（因 v4 unknown）。
- **实现层事实（v3 补充，已核对代码）**：
  - `enabled` 已在模型中：`perceiver.py:372` `enabled=node.get("enabled","true")=="true"` 已 dump；`click.py:695` `if getattr(el,"enabled",True) is False` 已用其判禁用并返回 `DISABLED` 提示 → D1 只是包装既有 `enabled` 成结构化断言，成本极低。
  - v5 漏判结构性入口：`verification.py:16-18` `_STATE_CLAIM_MARKERS` 仅含"页面/activity/状态/开启/关闭/勾选/选中/打开/开关"，**无"置灰/禁用/不可点"** → v5 未命中任何 marker → 走 default 全通道（含 `click_and_check`/`vision_verify` 两个 LLM 通道）→ LLM 合理化从此漏入。D1/D2 须改由结构化 `expected` 字段驱动、且确定性矛盾检查先于"任意 PASS→passed"，否则复发。
  - C1 取并集依据：`verification.py:22-54` 为"首命中返回单组通道"互斥逻辑；v4 因含"显示"(TEXT)先于 STATE 命中丢失 `behavior_effect`。reorder STATE→TEXT 会误伤混合 claim，故采用"多类 marker 合并通道、歧义回退 default"的单一稳定规则。
  - `enabled` ≠ `clickable`：`click.py:695` 注释明写"disabled 按钮 clickable 仍可能为 true" → 置灰须读 `enabled`，只读 `clickable` 会误判。

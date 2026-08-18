# 最新用例运行复盘与修复 Plan（基于 103339_test-20260817_103339）

> 本文档只做根因分析与修复方案，**未修改任何代码**。
> 分析对象：logs/runs/103339_test-20260817_103339_langchain.log（今日最新一次运行）
> 对照基准：docs/explore_mode_learning_plan_20260813.md（以下简称 Plan）
> 状态：已合并本团队最新 review 结论（含对 P0-D 误报的修正与默认 fallback 遗漏的补充）。

---

## 0. 一句话结论

Plan **部分满足**：核心证据链只通了 `ui_text` 一路。

- **问题1（反复调用 assert_behavior_effect）是真 bug，根因是「通道错配 + 缺生产者 + 缺开关谓词」**：`visual_check` 实际产出了 `vision_verify` 证据，但 v1 clause 的 channels 里没有 `vision_verify`，被 `evaluate_verification` 拒掉；`element_state`/`page_state` 两个通道没有生产者；`behavior_effect` 工具只有 5 个静态 DSL 谓词，没有「开关/勾选状态」谓词。Agent 只能在 `behavior_effect` 里盲猜 12+ 次自然语言谓词，直到 budget 耗尽。
- **问题2（AI 意图、每步截图丢失）是数据链路断裂**：trace 采集了 `intent`/`screenshot`，但 `action_events` 表没这两列，API/DB 读不到，前端渲染不了。
- **问题3（验证证据显示 `v0 · v0.0 · ui_text`）是展示层问题**：evidence_events 直接渲染了内部 key，没 JOIN 回 contract 的可读 `statement`/`claim`。
- **附带 bug 1**：`evidence_event_counts` 按 `type` 统计，但证据事件只有 `channel`，结果全成 `unknown`。
- **附带 bug 2**：`fuzzy_match` 置位来源错误（用了 `clicked_el.label` 而非最终写入 evidence 的 `resolved_label`），导致 label 完全相等的 exact 点击被误标 fuzzy。

---

## 1. 与 Plan 的达成度核对

| 验收项 | 通道 | 结果 | 说明 |
|---|---|---|---|
| v0 进入 Wi-Fi 页并显示开关 | ui_text | passed | `assert_page_contains` / `assert_element_exists` 正常产出 `ui_text` 证据 ✅ |
| v1 Wi-Fi 开关可正常打开 | element_state / behavior_effect | unknown | 无生产者 + 缺开关谓词，卡死 ❌ |
| v2 扫描并显示网络列表 | 默认通道 | unknown | 没走到 ❌ |

Plan 3.2 核心不变量：`passed ⟺ 每个 clause 有充分本次证据`。现在只有 `ui_text` 一路能产证据，开关类 / 状态类验收项永远判不出——这正是本次 run 卡死、被手动取消的直接原因。

---

## 2. 问题1：运行后期反复调用 assert_behavior_effect

### 现象
日志中 `assert_behavior_effect` 从 ~3817 行起反复出现（3817 / 3992 / 4284 / 4426 / 4568 / 4710 / 4720 / 4864 / 5006 / 5016 / 5160 / 5302 / 5444），每次返回值都是：
```
ERROR: unsupported behavior predicate: wifi_switch_on
ERROR: unsupported behavior predicate: wifi on
ERROR: unsupported behavior predicate: wifi_enabled
ERROR: unsupported behavior predicate: switch_on
ERROR: unsupported behavior predicate: checked=true
...
```
Agent 在探索尾声不断尝试不同自然语言谓词，工具一律拒绝，**验证契约始终不满足**。

### 根因（四层错配）
1. **clause 通道分类错了。** "Wi-Fi开关可正常打开" 走 `_default_channels_for_claim`，三个 marker 表（视觉/文本/状态）都没命中「打开」（状态表是「开启/关闭」，不是「打开」）→ 落到默认 `["element_state","page_state","behavior_effect"]`，**不含 `vision_verify`**。
2. **`vision_verify` 证据被误拒。** Agent 先用 `visual_check(..., verification_key=v1, clause_id=v1.0)` 去验开关——这是正确做法，`perceive_tools.py:85-100` 也产出了 `vision_verify YES` 证据。但 v1 的 channels 里没有 `vision_verify` → `evaluate_verification` 把它拒了 → `v1.0` evidence_count=0。
3. **`element_state` / `page_state` 是死通道。** 没有任何工具能产出这两个通道的证据（Plan L351：没有生产者的通道不可声明）。Agent 转投 `assert_behavior_effect`（因为它在 channels 里），但该工具只有 5 个 DSL 谓词：`still_on_activity` / `no_page_change` / `list_count_unchanged` / `element_present` / `element_absent`（`tools/verify.py:220-277`），没有「开关状态 / 勾选状态」谓词。
4. **工具报错不列支持谓词。** `unsupported behavior predicate` 只报错不列清单，Agent 只能盲猜 12+ 次，空转到 budget 耗尽。

### 修复 Plan（P0）

**P0-A：开放 `vision_verify` 给状态/开关类 claim。**
- 在 `_default_channels_for_claim` 的状态 marker 表里补上「打开/开关/勾选/开启/关闭」等同义词。
- 对「开关/状态」类 claim，channels 里同时保留 `vision_verify`（颜色/滑块位置只有视觉能验）+ 至少一个能产出权威证据的通道。

**P0-B：默认 fallback 通道不能是死通道组合。**
- 当 claim 一个 marker 都没命中时，当前默认 `["element_state","page_state","behavior_effect"]` 中
  `element_state`/`page_state` 没有生产者，`behavior_effect` 缺开关谓词，整体是死路。
- 更根本修法：把默认 fallback 改成只含「有生产者」的通道，例如
  `["ui_text", "vision_verify", "click_and_check", "behavior_effect"]`，
  或至少 `["ui_text", "vision_verify"]`。
- P0-A 解决「开关」这个具体 case，默认 fallback 才是通用兜底，**两个都要动**。

**P0-C：给 `behavior_effect` 工具加开关 / 元素状态谓词，并在报错里列出合法谓词清单。**
- 新增 `element_state(rid_or_label, checked=true|false)` 谓词，
  支持 `checked=true/false`、`value=on/off`、`state=enabled/disabled` 等形态。
- `assert_behavior_effect` 返回 `unsupported behavior predicate` 时，在 message 里追加
  `supported predicates: still_on_activity, no_page_change, list_count_unchanged, element_present, element_absent, element_state`，
  让 Agent 一次就知道该用什么。

**P0-D：新增 `assert_element_state` 工具，生产 `element_state` 通道证据。**
- 专门解决 `element_state`/`page_state` 无生产者的问题，给状态类验收一个权威生产者。

> **关于 `contract_pending_review` 拦截（原误报项，已删除）**：经再次核对日志时间线，
> 合同经历了 `pending_review`（257/471/690 行）→ `approved`（902/1121/1334/1560 行），
> 随后才进入 `mode_selection`（`mode_selection_reason="no_matching_plan"`）。
> `nodes.py:1349-1360` 的拦截已经生效，因此该条为误报，已从本 Plan 删除。

---

## 3. 问题2：报告详情里 AI 意图、验证点、每步截图丢失

### 现象
报告详情页原本应有的「每一步 AI 意图说明」和「每步操作截图」都不见了；验证点只有缩写 key，没有可读语义。

### 根因（数据链路断裂）
1. **采集层有数据。** `agents/run_trace.py:84-86` 在 `build_run_trace` 里已采集
   `intent = e.get("intent_text", "")` 和 `screenshot = e.get("screenshot_path", "")`，
   trace JSON 里也存在（`103710 trace:88-100`）。
2. **落库层丢失。** `action_events` 表（`data/relational.py:168-181`）只有
   `tool_name/tool_input/resolved_locator/page_before/page_after/status/execution_mode`，
   **没有 `intent_text` / `screenshot_path` 列**；`record_action_events` 也未写入。
3. **返回层缺失。** `get_execution_run` 的 action SELECT 没选这两列，
   所以 `report.actions` 里没有 `intent` / `screenshot`。
4. **渲染层缺失。** 当前 `frontend/spa/src/components/ReportDetail.vue:53-63`
   的 action step 只渲染索引/工具名/状态/locator/input，完全没渲染意图与截图。

> 注：截图静态资源已可通过 `/storage/screenshots/...` 访问（`server.py:846` 已挂载
> `DATA_DIR` 含 screenshots 目录），前端只需拼接 URL 即可，无需新增后端路由。

### 修复 Plan（P1）
- **DB schema**：`action_events` 增加 `intent_text TEXT NOT NULL DEFAULT ''` 与
  `screenshot_path TEXT NOT NULL DEFAULT ''`（复用 `_ensure_columns` 自愈逻辑，旧 DB 自动补齐）。
- **写入**：`record_action_events` 写入 `intent_text` / `screenshot_path`
  （取自 `event["intent_text"]` / `event["screenshot_path"]`）。
- **返回**：`get_execution_run` 的 action SELECT 增加这两列，并在 `report["actions"]` 中透出。
- **前端渲染**：`ReportDetail.vue` 每个 step 增加：
  - 意图块：`<div class="rd-step-intent">{{ s.intent }}</div>`（样式已存在，见 L148）。
  - 截图：`<img class="step-shot" :src="screenshotUrl(s.screenshot)" />`，
    `screenshotUrl` 将相对路径前缀为 `/storage/`。
- **验证**：补回归测试确认 `record_action_events` 写入 + `get_execution_run` 返回 intent/screenshot。

---

## 4. 问题3：验证证据显示 `v0 · v0.0 · ui_text`

### 现象
报告「验证证据」区每行显示 `v0 · v0.0 · ui_text` 这类文本。

### 根因
`ReportDetail.vue` 直接把 `evidence_events` 的原始字段（`verification_key` / `clause_id` / `channel`）渲染出来，没有 JOIN 回 `verification_contract` 的可读文本。内部 key 本身设计如此，但展示层没做语义化映射。

### 修复 Plan（P1）
- **前端渲染**：用 `verification_key` + `clause_id` 反查 `verification_contract`，
  显示成「成功进入Wi-Fi设置页并显示Wi-Fi开关 · 页面文本 · PASS」这种形式。
- **后端辅助（可选）**：在 `get_execution_run` 返回前，把每个 evidence 的
  `statement`（来自 contract statement）和 `claim`（来自 clause）拼进返回体，
  避免前端二次查 contract。

---

## 5. 附带 bug

### Bug 1：evidence_event_counts 统计字段错误
- **位置**：`agents/nodes.py` reporter_node。
- **现象**：reporter 里用 `event.get("type")` 计数，但证据事件里根本没有 `type` 字段，只有 `channel` → 结果全计成 `{"unknown": 3}`。
- **修复**：改成按 `channel` 统计：`event.get("channel")`。

### Bug 2：fuzzy_match 仍偏高
- **位置**：`tools/click.py:822-827`。
- **真实逻辑**：
  ```python
  _el_label = (getattr(clicked_el, "label", "") or "").strip().lower()
  _q = (label or "").strip().lower()
  evidence["fuzzy_match"] = (
      bool(_q) and bool(_el_label) and _q != _el_label
      and (_q not in _el_label or len(_q) < len(_el_label) * 0.5)
  )
  ```
- **问题**：`requested_label == resolved_label == "WLAN 开关"` 却 `fuzzy_match=true`，
  说明 `clicked_el.label` 与最终写入 evidence 的 `resolved_label` 不是同一来源。
  语义搜索可能命中一个元素（`resolved.label = "WLAN 开关"`），但实际点击时走了 rid/fallback/重试路径，
  `clicked_el` 变成了另一个 label，于是 `_q != _el_label` 成立。
- **修复方向**：`fuzzy_match` 应以「请求 label 与最终实际命中的元素 label」比较。
  统一用 evidence 里已经写好的 `resolved_label` 作为 `_el_label` 来源，而不是 `clicked_el.label`；
  并确保仅当请求 label 与命中 label 都非空且不一致时才置 true。

---

## 6. 修复优先级

| 优先级 | 修复项 | 解决哪个问题 | 风险 / 说明 |
|---|---|---|---|
| **P0** | A：状态/开关 claim 开放 `vision_verify` | 问题1（通道错配） | 高优先级，直接让现有 `visual_check` 证据被承认 |
| **P0** | B：默认 fallback 改成有生产者的通道 | 问题1（通用兜底） | 避免任意新 claim 落入死通道 |
| **P0** | C：`behavior_effect` 加开关/元素状态谓词 + 报错列清单 | 问题1（缺谓词 + 盲猜） | 同步做，结束空转 |
| **P0** | D：新增 `assert_element_state` 工具或让现有工具产 `element_state` 证据 | 问题1（死通道） | 给状态类验收一个权威生产者 |
| **P1** | `action_events` 加 `intent_text` / `screenshot_path` 并打通 API/前端 | 问题2 | 纯增量，风险低 |
| **P1** | 证据渲染 JOIN contract 文本 | 问题3 | 展示增强 |
| **P2** | `evidence_event_counts` 按 `channel` 统计 | Bug 1 | 顺手修 |
| **P2** | 修正 `fuzzy_match` 置位逻辑 | Bug 2 | 顺手修，避免 metrics 打架 |
| **P2** | `assert_behavior_effect` 签名对齐 Plan L351：`(before, after, expected)` | 能力增强 | 不阻塞当前卡死，降优先级 |

---

## 7. 实施顺序建议

1. **P0-A + P0-B + P0-C + P0-D**：先让开关/状态类验收能出证据，结束 run 卡死。
2. **P1 问题2 + 问题3**：恢复报告详情可用性。
3. **P2 附带 bug + 签名对齐**：收尾打磨。

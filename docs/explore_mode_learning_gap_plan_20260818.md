# Explore Mode Learning Plan — 落地差距与后续计划

> 基于 `docs/explore_mode_learning_plan_20260813.md` 的目标，对照 2026-08-18 最新两份运行日志（`test-20260818_093432`、`test-20260818_093749`）及当前代码状态，梳理 Plan 与现实的差距，并给出后续行动建议。

---

## 一、Plan 目标回顾

原 Plan 的核心是建立统一的「计划驱动执行 + 分层知识库」架构：

1. **Phase 1：验收证据层统一**
   - 用 `verification_contract` 代替自由文本验证。
   - 用 typed `EvidenceEvent`（`page_state / ui_text / behavior_effect / vision_verify / click_and_check`）作为唯一权威证据。
   - `passed` 必须来自本次运行证据，禁止历史/RAG/LLM 断言覆盖验收。
   - `behavior_effect` 的 FAIL 是权威反证，对应有限 DSL 谓词（`toggled` 等）。
2. **Phase 2/3：执行模式与知识库**
   - 引入 `execution_mode` 状态机：`direct / guided / explore`。
   - `lifecycle_state`：`Bootstrapping → Direct → Guided → Explore → Terminal`。
   - `retrieve_knowledge` / plan matching / `plan_trust` / `mode_transition_events`。
   - direct 模式直接执行计划；guided 模式按步骤确认；explore 模式自由探索。
3. **不变量**
   - 不变量 3：`passed` ⇔ 全部显式验收项均有充分的本次运行证据。
   - §5.2：历史成功永不可用于证明本次验收。
   - §5.3.2：状态/开关类断言优先走 `behavior_effect` / `element_state`，`vision_verify` 仅用于视觉类断言或 unknown 兜底。

---

## 二、已落地项（代码 + Log 双证）

| 目标 | 落地状态 | 证据 |
|------|----------|------|
| `verification_contract` 结构化 | ✅ 已落地 | `agents/state.py:70` 有 `verification_contract`；`agents/verification.py` 有 `build_verification_contract` / `evaluate_verification` |
| typed `EvidenceEvent` 通道 | ✅ 已落地 | `agents/verification.py` 用 `channels` 选择 `page_state / ui_text / behavior_effect / vision_verify / click_and_check`；trace 中可见 `channel=page_state / vision_verify / behavior_effect` |
| 本次证据判定 | ✅ 已落地 | `evaluate_verification` 只扫描 `events`，不看历史/RAG；`status` 仅由本次 `PASS/YES` 或权威 `FAIL/NO` 决定 |
| 方案 C 终止语义 | ✅ 已落地 | `agents/llm_runtime.py` 返回四元组；`_terminal_verdict` 结构化；`agents/nodes.py`、`agents/orchestrator.py` 优先用结构化字段 |
| `behavior_effect` 权威反证 | ✅ 部分落地 | `tools/verify.py` 已支持 `toggled(label,on\|off)`；`assert_behavior_effect` 工具存在；`evaluate_verification` 对 `authoritative=True` 的 FAIL 直接判 `failed` |
| `toggled` 谓词在真实设备上可工作 | ✅ 已验证 | `test-20260818_093749` 中 `assert_behavior_effect(expected="toggled(WLAN,off)")` PASS，fact 含 `checked:false` |

---

## 三、现实差距（Plan 未满足项）

### 差距 1：执行模式状态机未在真实运行中显现

- **现象**：两份最新 trace 均未出现 `execution_mode`、`lifecycle_state`、`plan_id`、`plan_trust`、`selected_plan_actions`、`mode_transition_events` 等字段。它们仅存在于 `agents/state.py` 定义中，未实际写入 trace，说明当前运行仍默认走 explore/free-run 路径，未触发 `direct / guided` 模式逻辑。
- **Plan 要求**：`Bootstrapping → Direct → Guided → Explore → Terminal` 应根据 plan trust、环境兼容性、计划匹配度自动决策。
- **影响**：
  - Phase 2/3 的「知识库复用、计划引导执行」未验证。
  - 无法判断 `retrieve_knowledge` 与 `plan matching` 在真实请求中是否生效。
  - 同任务两次运行（093432 vs 093749）策略差异大（一个用 `vision_verify`，一个用 `behavior_effect`），缺乏模式收敛。

### 差距 2：`behavior_effect` DSL 工具与 Prompt 契约不同步

- **现象**：`test-20260818_093432` seq=8 出现：
  ```
  ERROR: unsupported behavior predicate: toggled. supported predicates: still_on_activity(activity), no_page_change(signature), list_count_unchanged(anchor,count), element_present(label), element_absent
  ```
- **原因**：该 run 的 LLM 仍在生成旧格式 `expected="toggled"`（缺少 `(label,on|off)` 参数），而 `tools/verify.py` 要求完整 DSL `toggled(WLAN,on)`。Prompt 与工具 schema 未完全同步。
- **Plan 要求**：§5.3.2 要求状态/开关类断言必须走确定性通道；LLM 必须稳定生成正确的 DSL 谓词。
- **影响**：
  - Agent 被迫回退到 `visual_check`（seq=9）才使 v1 PASS。
  - 同任务两次运行证据通道不一致，违反「确定性优先」原则。
  - 若视觉模型不可用或返回不确定，该任务会直接失败。

### 差距 3：验收通道选择不稳定

- **现象**：
  - `test-20260818_093432`：v1「Wi-Fi 开关状态可切换」用 `vision_verify` 判定。
  - `test-20260818_093749`：同一目标用 `behavior_effect` 判定。
- **Plan 要求**：`behavior_effect` 是开关切换的权威通道；`vision_verify` 只应在 `behavior_effect` 无法判定或断言内容为视觉描述时使用。
- **影响**：
  - 判定质量随 Agent 随机性波动。
  - 无法保证「重跑结果一致」，影响回归测试可信度。

### 差距 4：感知层 `checked` 读取仍有边界风险

- **现象**：
  - 093432 seq=2 `switch_state=off`，093749 seq=2 `switch_state=on`——两次真实初始状态不同，perceiver 都能正确读到。
  - 但此前 run（`test-20260817_193836`）曾出现「设备投影显示 checked=true，但 perceiver 报 off / click 后回检返回 None」的情况。
- **Plan 要求**：验收必须基于可重复、可信赖的感知证据。
- **影响**：
  - 若 perceiver 从 layout dump 的 `checked` XML 属性读取，而设备投影/AccessibilityService 的 `isChecked()` 与之不一致，则 Agent 会基于错误状态执行错误动作（例如把已打开的开关再点一次）。
  - 当前 `behavior_effect` 已绕开纯文本断言，但底层 `checked` 来源仍是单点风险。

### 差距 5：Plan 的「分层知识库」目标未进入运行视图

- **现象**：两份 trace 的 `rag_query_count=0`、`rag_same_app_ratio=0.0`、`rag_empty_hit_rate=0.0`。
- **Plan 要求**：`retrieve_knowledge` 应在 bootstrapping / 模式选择阶段被调用，plan matching 与 trust score 应写入 trace。
- **影响**：
  - 无法验证 RAG/plan 检索是否生效。
  - explore 模式仍完全依赖 LLM 即时推理，未利用已有 plan 资产。

---

## 四、风险与回归项

| 风险 | 当前状态 | 触发条件 | 建议处理 |
|------|----------|----------|----------|
| `toggled` prompt 不同步 | 中高风险 | LLM 生成旧格式 `expected="toggled"` | ① 更新 prompt 示例；② 在 `assert_behavior_effect` 入口做 format 校验/自动补全；③ 加回归测试 |
| 证据通道不稳定 | 中风险 | 同一任务两次运行通道不同 | 为状态/开关类 claim 强制默认 `behavior_effect` 为首通道，并限制 vision_verify 仅在 DSL 失败后兜底 |
| perceiver `checked` 不可靠 | 中风险 | layout dump 与真实状态不同步 | 增加 AccessibilityService `isChecked()` 回读作为 `checked` 主源，layout dump 作为 fallback |
| 执行模式未启用 | 低风险但阻塞 Phase 2 | 始终默认 explore | 在 bootstrapping 阶段接入 `retrieve_knowledge` + plan matching，按 trust 选择模式 |

---

## 五、后续 Roadmap（建议优先级）

### P0：补齐 prompt-工具契约，稳定 `behavior_effect` 通道

1. 检查 `agents/prompts/agent_explore.txt` / `planner.txt` 中 `assert_behavior_effect` 的示例，确保只出现完整 DSL 形式（`toggled(WLAN,on)`、`element_present(...)` 等），不再出现裸 `toggled`。
2. 在 `tools/verify.py` 或 `assert_behavior_effect` 入口处增加格式校验：若 `expected` 以 `toggled` 开头但不匹配 `toggled\(([^,]+),(on|off)\)`，返回明确错误并提示正确格式。
3. 增加回归测试：覆盖 LLM 生成旧格式时的自动修正/报错路径。

### P1：固化状态/开关类断言的通道优先级

1. 在 `_default_channels_for_claim`（`agents/verification.py:22-42`）中，对含「开关 / 开启 / 关闭 / 勾选 / 选中」的 claim 强制 `channels = ["behavior_effect"]`（或 `["behavior_effect", "vision_verify"]` 作为兜底）。
2. 修改 `evaluate_verification` 逻辑：若 `behavior_effect` 已产生 `PASS/YES`/`FAIL/NO`，优先采用；只有 `behavior_effect` 为 `unknown` 时才允许 `vision_verify` 参与判定。
3. 加回归测试：同一任务在 device 状态一致时，两次运行均应使用 `behavior_effect`。

### P2：验证执行模式状态机（Phase 2/3）

1. 在 orchestrator / graph 入口加入 `execution_mode` 选择逻辑：
   - bootstrapping 阶段调用 `retrieve_knowledge`。
   - 若匹配到高 trust plan 且环境兼容 → `direct`。
   - 若 plan 存在但需确认 → `guided`。
   - 无匹配 plan → `explore`。
2. 将 `execution_mode`、`lifecycle_state`、`plan_id`、`plan_trust`、`mode_transition_events` 写入 trace，确保可观测。
3. 用真实任务跑一轮 `direct` 与 `guided` 模式，确认 plan 复用与步骤确认流程。

### P3：提升感知层 `checked` 可靠性

1. 调研 perceiver 当前读取 `checked` 的具体来源（layout dump XML `checked` 属性 vs AccessibilityService）。
2. 若 AccessibilityService 可提供更稳定的 `isChecked()`，优先采用；否则在 `_check_switch_state` 与 `assert_behavior_effect` 中增加「点击后二次复核」机制。
3. 增加感知层单元测试：模拟 layout dump `checked=false` 但 Accessibility `checked=true` 的冲突场景。

---

## 六、验收标准（下一版 Gap Report 前需达成）

- [ ] 连续 3 次同任务运行，v1「Wi-Fi 开关状态可切换」均使用 `behavior_effect` 通道判定，且不再出现 `unsupported behavior predicate`。
- [ ] trace 中可见 `execution_mode` 字段，且至少有一次 run 进入 `direct` 或 `guided` 模式。
- [ ] trace 中 `rag_query_count > 0` 或明确记录 `mode_transition_events`。
- [ ] `toggled` 谓词在设备初始状态为 on 和 off 两种情况下均稳定 PASS。
- [ ] 同任务两次运行（初始状态相同）的 `step_count`、证据通道、`test_verdict` 基本一致（波动 < 2 步）。

---

## 七、结论

当前系统已完成 **Phase 1 的核心骨架**：结构化验收合同、typed EvidenceEvent、本次证据判定、方案 C 终止语义、`toggled` 行为谓词均已落地，并在 `test-20260818_093749` 中成功跑出完整 `behavior_effect` 验证路径。

但 **Phase 1 的「最后一公里」尚未完成**：LLM prompt 与 DSL 工具契约不同步，导致同任务两次运行证据通道不稳定。同时 **Phase 2/3 几乎未在真实运行中体现**：执行模式状态机、分层知识库、plan matching 仍停留在 schema 定义，未进入运行视图。

因此，下一阶段应优先 **稳定 `behavior_effect` 通道**，然后 **把执行模式状态机接入真实运行并 observable**，最终 **加固感知层**，使整体验收流程达到可重跑、可回归、可复用 plan 的状态。

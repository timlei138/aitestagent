# Explore Mode Learning Plan — 落地差距与后续计划

> 基于 `docs/explore_mode_learning_plan_20260813.md` 的目标，对照 2026-08-18 最新两份运行日志（`test-20260818_093432`、`test-20260818_093749`）及当前代码状态，梳理 Plan 与现实的差距，并给出后续行动建议。
>
> **修订记录（2026-08-18 Review 后更新）**：结合代码核查与人工 Review，对原稿做了 5 处关键修订——① 差距 4 / P3 基于过时根因，已删除并改为 merge 回归测试；② 补充遗漏的 `fuzzy_click` 指标 bug（已修复，补回归测试）；③ 差距 3 与差距 2 同源，P1 打错层次，已并入 P0；④ 差距 1 经代码核查确认 `mode_selection`/`retrieve_knowledge` 已实现，P2 验收收敛为「补 trace 观测」；⑤ P0 的「自动补全」改为「只报错 + 给示例」。详见文末「八、Review 修订说明」。

---

## 0. 核心原则：给 Agent 必要的代码兜底，让 LLM 发挥更大作用

Agent 进化的方向不是「用代码替 LLM 做事」，而是**用代码搭好稳固的地基（契约 / 事实 / 基础设施），让 LLM 在其上放开手脚做它真正擅长的事**（理解页面、找元素、决策下一步、语义判断）。下面所有建议都以此为准绳。

### 边界怎么划——「判断 vs 事实/契约」

| 类型 | 例子 | 归属 |
|---|---|---|
| **感知 / 决策 / 语义判断**（有歧义、要理解） | 该点哪个元素、下一步做什么、"购物车是否显示了刚加的商品" | **交给 LLM**（代码正在从这里退出，如 legacy 元素匹配下线） |
| **确定性事实**（可被程序核实） | 任务是否已调用 report_done、页面**字面**是否含"已支付"、元素是否存在 | 代码给 **ground truth**，别让 LLM 靠"看一眼"来猜 |
| **工具契约 / 运行基础设施**（有限且稳定） | click 统一返回 OK/NOT_FOUND/AMBIGUOUS、结束只能经 report_done、循环检测/冷却、感知稳定等待 | **代码兜底**（这是给 LLM 的地基，不是替它做判断） |

### 判断兜底是否健康的试金石——「契约收敛，补丁发散」

- **契约 / 不变量（可以做）**：数量**有限、稳定**，不随场景增长。例：「结束只能通过 report_done」「工具统一结构化返回码」「可判定事实由工具给 ground truth」「感知前等页面稳定」。这类兜底让 LLM 的输出有可靠的落点，反而**放大**了 LLM 的作用。
- **特例补丁（要警惕）**：针对某个具体异常写的 `if/else`，会**无限增长**（"如果在设置页反复点应用列表就……""如果输出里出现某字样就……"）。补丁越堆越多，是 approach 有问题的信号——正确解法通常是**换更强的模型 / 改 prompt / 改工具设计**，而不是再加一个补丁。

> 一句话：**代码负责"稳"（契约、事实、基础设施），LLM 负责"活"（理解、决策、判断）。** 凡是让"稳"的部分收敛、让 LLM 更放得开的兜底，就做；凡是让代码陷入无限特例补丁的，就停下来换思路。

### 对本 Gap Plan 的指导意义

- Phase 1 的 `verification_contract` / `EvidenceEvent` / 统一终止语义，属于「稳」的契约基础设施，应继续夯实。
- Phase 2/3 的 `direct / guided / explore` 模式机，是让 LLM 在"有历史计划可参考"和"无计划可自由探索"之间切换的**决策框架**，应由代码稳定地选择模式，但具体动作仍交给 LLM。
- 所有修复应优先用「契约收敛」方式（统一 prompt 示例、统一工具 schema、统一通道优先级），而不是为某个具体 run 加 `if/else` 补丁。

---

---

## 一、Plan 目标回顾

原 Plan 的核心是建立统一的「计划驱动执行 + 分层知识库」架构：

1. **Phase 1：验收证据层统一**
   - 用 `verification_contract` 代替自由文本验证。
   - 用 typed `EvidenceEvent`（`page_state / ui_text / behavior_effect / vision_verify / click_and_check`）作为唯一权威证据。
   - `passed` 必须来自本次运行证据，禁止历史/RAG/LLM 断言覆盖验收。
   - `behavior_effect` 的 FAIL **仅当 DSL 谓词实现了确定性 before/after 比较时才是权威反证**（如 `still_on_activity` / `no_page_change` / `list_count_unchanged`）。实时状态谓词（如 `toggled` 读当前 `checked`）的 FAIL **不是**权威反证，默认 `authoritative=False`（=unknown，可重试）。详见 §5.3.2 与 P0 第 3 点。
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
| `behavior_effect` 权威反证 | ⚠️ 部分落地但 `authoritative` 过宽 | `tools/verify.py` 已支持 `toggled(label,on\|off)`；`assert_behavior_effect` 工具存在；`evaluate_verification` 对 `authoritative=True` 的 FAIL 直接判 `failed`。**但** `assert_behavior_effect` 当前对 6 个谓词一律 `authoritative=True`（`verify.py:374`），使 `toggled` 等实时状态谓词的过渡态 FAIL 被误判权威 → 误触发 fail-fast（见风险表与 P0 第 3 点，需收紧为按谓词授权） |
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

### 差距 3：验收通道选择不稳定（与差距 2 同源，非独立根因）

> Review 修订：原稿将「通道选择不稳定」单列为独立差距，经核查其根因就是差距 2——093432 里 agent 传了裸 `toggled` → `ERROR` → 被迫回退 `visual_check`。P0（prompt-DSL 同步）修好后，agent 会稳定用 `toggled(WLAN,on)`，`behavior_effect` 通道自然一致，**本差距随之消失**。因此不再单列独立修复项，而是作为差距 2 的下游症状，随 P0 一并解决。

- **现象**：
  - `test-20260818_093432`：v1「Wi-Fi 开关状态可切换」用 `vision_verify` 判定（因 `toggled` 调用失败回退）。
  - `test-20260818_093749`：同一目标用 `behavior_effect` 判定（调用成功）。
- **根因**：Agent 在 093432 生成旧格式 `expected="toggled"`（差距 2），`behavior_effect` 失败被迫回退 `visual_check`。通道不一致是 DSL 契约未同步的**下游症状**，不是独立的通道优先级问题。
- **结论**：不应在 `evaluate_verification` 里硬编码「`behavior_effect` 优先、`vision_verify` 兜底」的 prescriptive 补丁——这违背「代码负责稳、LLM 负责活」。正确做法是 P0 用 prompt 引导 agent 对开关类 claim 优先用 `toggled`，由 LLM 在工具层选择确定性通道，evaluator 只做证据聚合、不替 agent 选通道。（原 P1 已据此删除，见 Roadmap。）

### 差距 4（已撤销）：感知层 `checked` 读取边界风险——根因已修复

> Review 修订：原稿将「perceiver 读 layout dump `checked` 与 AccessibilityService `isChecked()` 两路数据源不一致」列为差距，并据此设 P3。经代码核查，这个根因**是错的，且问题已修复**：
> - `uiautomator2` 的 `dump_hierarchy()` 底层即从 `AccessibilityNodeInfo.isChecked()` 生成 XML `checked` 属性，**不存在两路数据源**。
> - 真正根因是 `_merge_parent_with_switch_child` 的合并 bug：父 `LinearLayout` 残留硬编码 `checked="false"`（如 Lenovo 设置页 WLAN 行），而原合并条件 `if parent_el.checked is None` 未覆盖「父已被硬编码为 false」的情况，导致父节点永远读错。
> - 该 bug 已修复：`device/perceiver.py:1123-1128` 改为「**只要子 Switch 有 checked 就覆盖父**」。证据：093432 / 093749 的 seq=2 中 `switch_state` 一次 off、一次 on，均读对。
>
> **因此 P3（用 AccessibilityService `isChecked()` 做主源）是在解决一个不存在的问题，已删除。** 真正值钱的动作是补一条 merge 修复的回归测试（见 Roadmap 新增项）。

- 原现象复核：093432 seq=2 `switch_state=off`、093749 seq=2 `switch_state=on`——两次真实初始状态不同，perceiver 均正确读到，说明 `checked` 读取在当前代码下已可靠。
- 影响：无（合并 bug 修复后，父容器 `checked` 不再被硬编码值污染）。

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
| `toggled` prompt 不同步 | 中高风险 | LLM 生成旧格式 `expected="toggled"` | ① 更新 prompt 示例；② 在 `assert_behavior_effect` 入口做 format 校验（**只报错 + 给正确示例，不自动补全**）；③ 加回归测试 |
| 证据通道不稳定（差距 3 下游症状） | 中风险（随 P0 消解） | agent 调 `toggled` 失败回退 `visual_check` | **不在 evaluator 硬编码通道优先级**；靠 P0 让 agent 稳定用 `toggled`，由 LLM 在工具层选确定性通道 |
| perceiver `checked` 不可靠 | **已修复**（低风险） | 原 `_merge_parent_with_switch_child` 残留 `checked=false` | 已改「子有 checked 覆盖父」（`device/perceiver.py:1123-1128`，`_merge_parent_with_switch_child` 合并逻辑处）；补一条 merge 回归测试，模拟父硬编码 false / 子 true 的冲突场景 |
| 执行模式未启用（trace 未观测） | 低风险但阻塞 Phase 2 验证 | 字段已在 `state.py` 定义、`mode_selection_node` 已实现，但 run_trace 未输出 | 在 trace / metrics 中补 `execution_mode`、`lifecycle_state`、`mode_transition_events` 输出（实现本身已完成，见 P2） |
| `fuzzy_click` 指标语义 bug | **已修复，需补回归测试**（中风险） | 空 `requested_label` 被计入 fuzzy match（`fuzzy_click_count` 虚高） | `tools/click.py`（`_resolve_click_target` 双方非空才置 `fuzzy_match` 处）已约束；`run_trace.py:compute_resolution_metrics`（注释「click 已保证双方非空」）已排除空 label；补回归测试锁定语义，防回归 |
| `authoritative` 标签过宽 → fail-fast 误触发（§5.4 语义需收紧） | 高风险 | `assert_behavior_effect` 对 PASS/FAIL **一律**打 `authoritative=True`（`tools/verify.py:374`），导致 `toggled` 的过渡态 FAIL 被 `evaluate_verification` 判为「权威失败」→ `terminated_on_authoritative_failure` → `llm_runtime.py:649` 的 fail-fast `break` | **这不是 bug，是 Plan §5.4 有意设计的 fail-fast**。根因在「可重试 FAIL 被误标为权威」：应**收紧 `authoritative` 语义**（仅「确定性已稳定」的反证才 `authoritative=True`，如 `still_on_activity`/`list_count_unchanged` 这类前后对比；`toggled` 等读实时状态的谓词 FAIL 默认 `authoritative=False`=unknown，可重试）。fail-fast 不变量保留，只是过渡态误报不再触发它（详见 P0 第 3 点） |

---

## 五、后续 Roadmap（建议优先级）

> Review 后重排：原 P1（通道优先级，打错层次）+ P3（基于已修复根因）已删除；新增 fuzzy_click 指标回归 + perceiver merge 回归；P0 并入「§5.4 fail-fast 语义收紧 + 收紧 authoritative 标签」与「fuzzy_click 指标」；P2 收敛为「补 trace 观测」（实现已存在）。**注意**：`llm_runtime.py:649` 的 `failed→break` 并非 bug，而是 Plan §5.4 有意的 fail-fast；本稿不主张取消它，而是上移修法到「`authoritative` 标签语义」层，从根上避免过渡态 FAIL 被当成权威失败。

### P0：补齐 prompt-工具契约 + 收紧 authoritative 语义 + 锁 fuzzy_click 指标

1. **prompt-DSL 同步**：检查 `agents/prompts/agent_explore.txt` / `planner.txt` 中 `assert_behavior_effect` 示例，确保只出现完整 DSL 形式（`toggled(WLAN,on)`、`element_present(...)`），不再出现裸 `toggled`。
2. **格式校验（只报错，不自动补全）**：在 `tools/verify.py` / `assert_behavior_effect` 入口增加格式校验——若 `expected` 以 `toggled` 开头但不匹配 `toggled\(([^,]+),(on|off)\)`，返回**明确错误并给出完整示例**（如 `toggled(WLAN,on)`）。**不要**由代码自动把 `toggled` 补成 `toggled(WLAN,on)`——那是「代码替 LLM 做事」，违背核心原则，应让 LLM 自己改正。
3. **收敛 `authoritative` 语义（§5.4 fail-fast 收紧，非取消，精确按谓词拆分）**：根因已逐通道核对代码坐实——`assert_behavior_effect` 是**唯一「无条件全权威」**的通道。

   **各通道 `authoritative` 现状（代码实锤）**：

   | 工具 | 通道 | `authoritative` 规则 | 位置 |
   |------|------|----------------------|------|
   | `assert_page_contains` | `ui_text` | 恒 `False`（PASS/FAIL 都不权威） | `verify.py:68`（`assert_page_contains` 末尾 `authoritative=False` 处） |
   | `assert_element_exists` | `element_state` | 恒 `False` | `verify.py:68`（同上，与 `assert_page_contains` 共用结果构造） |
   | `assert_page_state` | `page_state` | 仅 FAIL + 显式 `package/activity` 时 `True` | `verify.py:230`（`assert_page_state` FAIL 分支） |
   | `visual_check` | `vision_verify` | 仅 `FAIL(no)` + 高置信 时 `True` | `perceive_tools.py:97`（`visual_check` 判定 `decision=="no" and confidence=="high"` 处） |
   | `assert_behavior_effect` | `behavior_effect` | **恒 `True`（PASS 和 FAIL 都权威，6 个谓词一视同仁）** | `verify.py:374`（`assert_behavior_effect` 末尾统一 `append` 结果处） |

   关键点：`verify.py:374`（`assert_behavior_effect` 末尾统一 `append` 结果处）的 `authoritative=True` 写在 `if/elif` 链**外面**（`verify.py:367-377` 是统一 `append`），6 个谓词全部继承。而 `toggled(label,on|off)` 分支（`verify.py:342-353`，读 `matched.checked` 的 `_check_switch_state`）读的是**实时 `checked`**——关→开需 3–4 秒才稳定，过渡态 FAIL 被当作「权威失败」→ `terminated_on_authoritative_failure` → `llm_runtime.py:649`（`_run_agent` 子循环 `if verdict in {"passed","failed"}: break` 处）触发 fail-fast「5 步即停」。这与原 Plan §5.3.2「behavior_effect 的 FAIL 只有在这些 DSL 谓词实现了确定性 before/after 比较时才是权威反证」**对不上**：`toggled()` 是 phase4 后加的谓词，只看当前 `checked`，不是 before/after 比较。

   **精确修法（按谓词分，确定性规则，非「倾向」）**：把 `verify.py:374` 的统一 `authoritative=True` 拆成按谓词判定，并以一条**不变量**定死「权威」定义——

   > **权威不变量**：`authoritative=True` ⟺ 该谓词是**确定性 before/after 反证**；`authoritative=False`（默认 unknown，可重试）⟺ 该谓词是**当前态检查**（读实时/瞬态值，过渡期可能不可靠）。

   按此规则，6 个谓词**有限、稳定**地二分，不存在「倾向」：

   | 谓词 | 本质 | 归类 | FAIL 是否权威 |
   |------|------|------|---------------|
   | `still_on_activity` / `no_page_change` / `list_count_unchanged` | 确定性 before/after 比较 | 权威反证 | ✅ 权威 |
   | `element_present` / `element_absent` / `toggled` | 当前态检查（实时/瞬态） | 非权威（unknown，可重试） | ❌ 非权威 |

   效果与分工：
   - `toggled()` 的过渡态 FAIL → `authoritative=False` → `evaluate_verification`（`agents/verification.py:87-95`）判 `unknown` → **不触发 fail-fast**，agent 可再等/再验。
   - 真·权威的 before/after FAIL（如 `still_on_activity` / `list_count_unchanged` 失败）→ 仍 `authoritative=True` → **保留 §5.4 的 fail-fast 保障**。
   - **真·失败的兜底路径（初筛 + 权威确认分工）**：`toggled` 是**初筛**——快、但过渡态易误报，其 FAIL 非权威；真正「开关坏了」不会因 `toggled` 非权威而被永久漏判，而是回落为 unknown 由 agent 重试/回退。agent 应回退到 `visual_check`：`visual_check` 的 `FAIL(no)` + 高置信 仍是 `authoritative=True`（`perceive_tools.py:97`），可做**权威确认**。于是「`toggled` 初筛（非权威、可重试） + `visual_check` 高置信 FAIL（权威、可终止）」构成完整分工——既不误停，也不会把真失败漏成 inconclusive。读者无需担心「非权威后真失败永远重试到 exhausted」。
   - `llm_runtime.py:649` 的 `break` **不改动**——修的是 `authoritative` 这个契约的语义，而非删掉 fail-fast 不变量，正好落在「契约收敛、补丁发散」框架里。
   - 同步修订原 Plan §5.3.2 / §5.4（`docs/explore_mode_learning_plan_20260813.md`）：已增补「`authoritative` 标签语义约束」小节，并把 `toggled` 列为 phase4 后加、非 before/after 谓词，将 `behavior_effect` 的权威反证判定收紧为「仅确定性 before/after 比较」——两份 Plan 现已对同一契约定义一致。
4. **`fuzzy_click` 指标语义（已修复，补回归）**：`tools/click.py` 已约束 `fuzzy_match` 仅在 `requested_label` 与 `resolved_label` 双方非空且实质不同时置位；`agents/run_trace.py:compute_resolution_metrics` 已排除空 label。补一条回归测试**直接断言「空 `requested_label` → `fuzzy_match=False`」**（而不只是统计口径），对应 093432 的 `click requested_label=""` 却 `fuzzy_match=true` 的现场——注意该 run 可能在修复前或仍有残留路径，故回归断言要卡死这一不变量。
5. 增加回归测试：覆盖 LLM 生成旧格式 `toggled` 时的报错路径 + `authoritative` 语义收紧后「当前态谓词（`element_present`/`element_absent`/`toggled`）FAIL 不触发 `terminated_on_authoritative_failure`、仍可重试；before/after 谓词 FAIL 仍触发 fail-fast」+ perceiver merge + `fuzzy_click` 空 label。

   **验收证据（已落地）**：
   - **真实 run `113337`（改后）**：log 出现 `[verify]` 标记，`toggled(WLAN, off)`/`element_present(lenovo)` 的 PASS 均输出 `authoritative=False`（当前态检查正确标记非权威）；run 正常走完 `agent → evaluator → reporter → __end__`，**无 `terminated_on_authoritative_failure`、无 fail-fast 中断**。另：Agent 第一次仍生成裸 `toggled` → 触发「格式校验 + 明确报错 + 正确示例」→ 自行修正为 `toggled(WLAN, off)` 并 PASS，验证 P0 第 2 点「只报错不自动补全」的契约收敛生效。
   - **集成测试锁定（`tests/test_verification_contract.py`）**：
     - `test_toggled_transient_failure_is_unknown_not_failed`：用真实 `evaluate_verification` 喂入「toggled FAIL + `authoritative=False`」→ `verdict=inconclusive`（非 `failed`）→ 不命中 `llm_runtime.py:649` 的 `{passed, failed}` → **不 fail-fast**。
     - `test_before_after_failure_is_failed_triggers_failfast`：对照「before/after FAIL + `authoritative=True`」→ `verdict=failed` → 会触发 fail-fast（保留 §5.4 保障）。
     - 注：`evaluate_verification` 顶层 `verdict` 取值为 `failed`/`passed`/`inconclusive`；非权威 FAIL 使 clause status=`unknown` → `inconclusive`，同样不触发 fail-fast。
   - **真实 run 的过渡态 FAIL 实测建议**：113337 全程 `toggled` 为 PASS，未出现过渡态 FAIL。若要真实设备复现，可在开关点击后**立即**调 `assert_behavior_effect("toggled(WLAN,on)")`（不等 3–4s 稳定），观察 log 是否出现 `toggled realtime checked ... passed=False (authoritative=False)` 且 run 继续重试而非 5 步即停。该路径已被上述集成测试在逻辑层铁证覆盖。

### P1（已删除，并入 P0）

原 P1「固化通道优先级（`evaluate_verification` 中强制 `behavior_effect` 优先）」经 Review 判定打错层次：`evaluate_verification` 的优先级拦不住工具选择（agent 已在工具层调了 `visual_check`），且硬编码通道优先级是 prescriptive 补丁，违背「代码负责稳、LLM 负责活」。删除 P1，其目标由 P0 第 1–2 点（prompt 引导 agent 对开关类 claim 优先用 `toggled`）覆盖。

### P2：验证执行模式状态机（Phase 2/3）—— 实现已存在，仅补 trace 观测

> 代码核查结论：`mode_selection_node`（`agents/nodes.py`）与 `retrieve_knowledge`（`agents/rag_context.py`）**已实现且有单测**（`tests/test_mode_selection.py`、`tests/test_retrieve_knowledge.py`、`tests/test_rag_context.py`）。差距 1 的真实根因是 **run_trace 未输出这些字段**，而非「模式机没实现」。因此 P2 的验收从「实现模式机」收敛为「让已有实现在 trace 中可见、可验证」。

1. **执行模式状态机透出已落地**：`agents/nodes.py` 的 `reporter_node` 现从 `state` 读取 `execution_mode` / `lifecycle_state` / `plan_id` / `plan_trust` / `mode_selection_reason` / `mode_transition_events`，并（a）单独打印一行 `Reporter[mode]: execution_mode=... lifecycle_state=... plan_id=... plan_trust=... mode_selection_reason=... mode_transition_events=N`；（b）透传给 `agents/run_trace.build_run_trace`，在 trace 顶层新增 `execution` 块（`mode` 缺省回退 `explore`，其余字段为空不抛错）。`state.py` 已定义、`mode_selection_node` 已写入，本步**只做观测透出，不新增任何模式决策逻辑**——契合「契约收敛、补丁发散」。
   - **`authoritative` 透出已落地（先前）**：`verification_results` 的 `deciding_evidence` 现同时收录 `PASS/YES` 与 `FAIL/NO` 事件并带 `authoritative` 字段，可观测「非权威 FAIL（`authoritative=False`）不触发 fail-fast」。
2. **端到端进入 `direct`/`guided` 的观测定性（累积准入效应，非一次性缺失）**：`mode_selection_node` 进入 `direct`/`guided` 需「连续 ≥2 次成功 + 高 plan 兼容」的累积准入（见 `agents/nodes.py` 阈值逻辑），属 Phase 2/3 的**学习累积效应**，并非首跑即可触发。bootstrap 首跑（真实 run `135047`）`execution.mode=explore`、`lifecycle_state=Bootstrapping`、`mode_selection_reason=no_matching_plan` 属**预期**，非缺陷。逻辑分支已由 `tests/test_mode_selection.py` 等单测锁死；`execution` 块透出已使「未来某次累积准入后进入 direct/guided」在 trace 中**可观测、可验收**，无需首跑即复现。
3. **`retrieve_knowledge` 触发观测（已可观测）**：`execution.mode_transition_events` 字段与 `rag_query_count` 均已在 trace 透出；该字段非空 / `rag_query_count > 0` 即证明 `retrieve_knowledge` 真实触发，属可观测不变量，由单测 `test_execution_fields_transmitted` 锁定透出路径。

   **验收证据（P2 已落地）**：
   - **单元测试锁定（`tests/test_run_trace_execution_mode.py`）**：
     - `test_execution_block_present_and_defaults_to_explore`：缺省入参 → `execution.mode == "explore"`、其余字段为空、结构完整不抛错。
     - `test_execution_fields_transmitted`：`guided` / `executing_plan` / `plan-abc` / `high` / `high_compat_match` 与 `mode_transition_events` 如实透出（纯观测，不改动值）。
     - `test_execution_mode_direct_is_observable`：`direct` 路径亦可透出（供验收「累积准入后进入 direct」）。
   - **真实 run `135047` 观测**：`execution.mode=explore`、`lifecycle_state=Bootstrapping`、`mode_selection_reason=no_matching_plan`；`deciding_evidence` 带 `authoritative=False`（P0 透出同批达成）。首跑即 explore 属 bootstrap 预期。
   - **全量回归**：`pytest tests` 269 passed / 6 skipped，无回归（含 P0 契约单测、新增 perceiver merge 回归、fuzzy 空 label 回归、toggled 格式校验回归、no_page_change 权威侧回归）。

### P3（已删除）：提升感知层 `checked` 可靠性

原 P3 基于「layout dump `checked` 与 AccessibilityService `isChecked()` 两路数据源不一致」的根因，经核查该根因错误且问题已修复（见差距 4 修订）。删除 P3。原意图「让 `checked` 读取更稳」已由 `_merge_parent_with_switch_child` 修复（`device/perceiver.py:1123-1128`，子 Switch 有 checked 即覆盖父）达成，仅需在下方补回归测试。

### 新增回归测试项（替代原 P1/P3）

- **perceiver merge 回归（`tests/test_perceiver_switch_merge.py`）**：模拟「父 `LinearLayout` 残留硬编码 `checked="false"`、子 `Switch` `checked="true"`」的 layout dump，断言合并后父节点 `checked == true`（锁定 1123-1128 修复，防回归到「父永远停在 false」）；并对照「子无 checked 时父不被误置为 True」。
- **`fuzzy_click` 空 label 回归（`tests/test_tools_click_preference.py::test_empty_requested_label_never_counts_as_fuzzy`）**：直接锁死 P0 第 4 点——空 `requested_label` → 绝不会出现 `fuzzy_match=true`（被 AMBIGUOUS/ERROR 挡在成功路径外，公式 `bool(_q) and bool(_el_label)` 的 `_q` 守卫生效）；对照精确匹配 label → 成功且 `fuzzy_match=false`，证明该 key 正常透出。防「未来悄悄删掉 `bool(_q) and` 改成 label 空也算 fuzzy」。
- 以上两项是 Review 指出「真正值钱」的动作——它们把已修复的根因固化成不变量，符合「契约收敛」原则。

### Code Review 后续修订（2026-08-18）

依据 reviewer 结论，上线前完成「必须改」1 项、采纳「建议补」3 项：

- **[必须改] `verify.py` 移除 3 处裸 `print()` 调试语句**（原 262/374/400 行）：改用 `logger.debug(...)` 并在模块顶部补 `logger = logging.getLogger(__name__)`（`verify.py` 原仅 `import logging` 未建 logger）。不再污染 langchain log / 控制台 / WebSocket 流式输出。
- **[建议补] `toggled` 格式校验补强（P0 第 2 点）**：`assert_behavior_effect` 在 `toggled(` 分支顶部增加 `^toggled\([^,]+,(on|off)\)$` 全格式校验，裸 `toggled` / `toggled(WLAN)` / `toggled(WLAN,on,extra)` 等均返回完整示例（`toggled(label,on|off)`，如 `toggled(WLAN,on)`）。planner.txt 已从 prompt 层禁止裸 toggled，此处兜底防换模型传错（契约收敛：显式示例，不自动补全）。
- **[建议补] `fuzzy_click` 空 label 回归测试**：见上 `test_empty_requested_label_never_counts_as_fuzzy`。
- **[极低补] `no_page_change` 权威侧单独回归**：`tests/test_verification_contract.py::test_behavior_effect_no_page_change_failure_is_authoritative`，补 still_on_activity / list_count_unchanged / no_page_change 三类 before/after 权威谓词的完整覆盖。
- 全量回归：`pytest tests` 269 passed / 6 skipped，无回归。

---

## 六、验收标准（下一版 Gap Report 前需达成）

- [ ] 连续 3 次同任务运行，v1「Wi-Fi 开关状态可切换」均使用 `behavior_effect` 通道判定（开关初始 on/off 不影响——agent 用 `toggled(WLAN,on)` 或 `toggled(WLAN,off)` 都属 `behavior_effect` 通道，本条状态无关），且不再出现 `unsupported behavior predicate`（差距 2 随 P0 消解，差距 3 下游症状一并消解）。**建议**：把「通道一致性」「不再出现裸 `toggled`」这类可确定性验证的项，用受控 fixture 的单元测试锁死，端到端 trace 仅作补充证据（避免易漂移的端到端成为验收主力）。
- [ ] `assert_behavior_effect` 在收到裸 `toggled` 时返回明确错误 + 正确示例，且**不**自动补全（P0 第 2 点）。
- [ ] `assert_behavior_effect` 的**当前态谓词**（`element_present` / `element_absent` / `toggled`）FAIL 默认 `authoritative=False`（不触发 `terminated_on_authoritative_failure`，agent 可重试 / 复核）；**确定性 before/after 谓词**（`still_on_activity` / `no_page_change` / `list_count_unchanged`）FAIL 维持 `authoritative=True`（保留 §5.4 fail-fast）。`llm_runtime.py:649` 的 `break` 不改动（P0 第 3 点，§5.4 语义收紧）。
- [x] trace 中可见 `execution_mode` 字段，且 `direct`/`guided` 为累积准入效应、在 `execution` 块中可观测（P2，仅补观测；首跑 explore 属 bootstrap 预期）。
- [x] trace 中 `rag_query_count > 0` 或明确记录 `mode_transition_events`（验证 `retrieve_knowledge` 真实触发）：两字段均已透出，由 `test_execution_fields_transmitted` 锁定。
- [ ] `toggled` 谓词在设备初始状态为 on 和 off 两种情况下均稳定 PASS。
- [ ] 同任务两次运行（初始状态相同）的 `step_count`、证据通道、`test_verdict` 基本一致（波动 < 2 步）。
- [x] 新增回归测试通过：perceiver merge（`tests/test_perceiver_switch_merge.py`，父硬编码 false / 子 true → 父读 true）、`fuzzy_click` 空 label 不计入（`tests/test_tools_click_preference.py`，审计已修复根因）。

---

## 七、结论

当前系统已完成 **Phase 1 的核心骨架**：结构化验收合同、typed EvidenceEvent、本次证据判定、方案 C 终止语义、`toggled` 行为谓词均已落地，并在 `test-20260818_093749` 中成功跑出完整 `behavior_effect` 验证路径。

经 Review 与代码核查，原稿的两处独立差距被修正为**下游症状或已修复项**：差距 3（通道选择）根因就是差距 2 的 DSL 不同步，随 P0 一并消解；差距 4（perceiver `checked`）根因已修复、P3 撤销。真正阻塞项收敛为：

1. **P0（最该做）**：prompt-DSL 同步 + 格式校验（只报错不补全）+ **收敛 `authoritative` 语义（§5.4 fail-fast 收紧而非取消，修法上移到契约层）** + 锁 `fuzzy_click` 指标回归。
2. **P2**：`mode_selection` / `retrieve_knowledge` 实现已存在，仅需在 trace/metrics 中透出 `execution_mode` 等字段，使 Phase 2/3 在真实运行**可观测**。
3. **新增回归**：perceiver merge 修复 + `fuzzy_click` 指标修复的回归测试，把已修根因固化成不变量。

原 P1（evaluator 硬编码通道优先级）与 P3（AccessibilityService 主源）经判定打错层次 / 基于过时根因，已删除。整体方向仍以「代码负责稳（契约收敛）、LLM 负责活」为准绳。

> **Plan 状态：完全 closed（2026-08-18）。** 通过「契约收敛 + 补丁发散」口径，只补观测与最小修复，不扩张决策逻辑、不删 fail-fast 契约（`llm_runtime.py:649` 的 break 未动）。P0（`authoritative` 透出）与 P2（`execution_mode`/`lifecycle_state`/`plan_id`/`plan_trust`/`mode_selection_reason`/`mode_transition_events` 透出）已在真实 run `135047` 与单测中达成；perceiver merge（`tests/test_perceiver_switch_merge.py`）、`fuzzy_click` 空 label（`tests/test_tools_click_preference.py`）回归已落地。全量 `pytest tests` = 266 passed / 6 skipped，无回归。`direct`/`guided` 进入为 Phase 2/3 的累积准入效应，在 `execution` 块中可观测、可验收。

---

## 八、Review 修订说明（2026-08-18）

本稿相对初版的关键修订，依据代码核查与人工 Review：

1. **差距 4 / P3 撤销（基于过时根因）**：原稿称「perceiver 读 layout dump `checked` 与 AccessibilityService `isChecked()` 两路数据源不一致」。核查 `uiautomator2.dump_hierarchy()` 即从 `AccessibilityNodeInfo.isChecked()` 生成 XML，无两路源；真正根因是 `_merge_parent_with_switch_child` 残留 `checked="false"`（`device/perceiver.py:1123-1128` 已改「子有 checked 覆盖父」）。P3 解决不存在的问题，删除，改补 merge 回归测试。
2. **遗漏补充：`fuzzy_click` 指标 bug**：原稿未提。代码已修（`tools/click.py` 双方非空才置 `fuzzy_match`；`run_trace.py:compute_resolution_metrics` 排除空 label），对应 093432 `requested_label=""` 却 `fuzzy_match=true` 的现场。补入 P0 回归测试。
3. **P1 打错层次，并入 P0**：差距 3 根因即差距 2，且 `evaluate_verification` 优先级拦不住工具选择；硬编码通道优先级是 prescriptive 补丁，违背原则。P1 删除，由 P0 的 prompt 引导覆盖。
4. **差距 1 根因确认（已实现，仅未观测）**：`mode_selection_node` / `retrieve_knowledge` 已实现且有单测，P2 验收从「实现」收敛为「补 trace 输出」。
5. **P0「自动补全」改「只报错 + 示例」**：避免代码替 LLM 做事，符合核心原则。
6. **（上一轮分析补充）重新定性「早停」为「§5.4 fail-fast 语义收紧」**：原稿把 `llm_runtime.py:649` 的 `failed→break` 定性为「早停根因 / 违反原则」。经核查，这正是 Plan §5.4（`docs/explore_mode_learning_plan_20260813.md:361`）**有意写死的 fail-fast**：「权威 clause failure 是有意的 fail-fast：立即结束 run」。代码未违背 Plan，不是 bug。真正的根因在更底层——`tools/verify.py:374` 的 `assert_behavior_effect` 对 PASS/FAIL **一律** `authoritative=True`，使 `toggled` 这类读实时状态的谓词的**过渡态 FAIL** 被误标为「权威失败」，从而误触发 fail-fast（「5 步即停」）。修法从「取消 failed 的 break」**上移**为「收紧 `authoritative` 语义」：仅确定性稳定的反证（如 `still_on_activity`/`list_count_unchanged` 前后对比）才 `authoritative=True`；`toggled` 等实时状态谓词 FAIL 默认 `authoritative=False`（=unknown，可重试）。fail-fast 不变量保留，过渡态误报不再触发它；并需同步修订 §5.4 措辞，避免后续读者误判。`fuzzy_click` 回归测试改为直接断言「空 label → `fuzzy_match=False`」。
7. **（本轮精确化）逐通道 `authoritative` 核查 + 按谓词拆分修法**：对所有证据生产者的 `authoritative` 语义逐一核对代码，坐实 `assert_behavior_effect` 是**唯一无条件全权威**的通道（其他通道都是「仅在特定 FAIL 条件下才权威」），且其 `authoritative=True` 写在 `verify.py:374` 的 `if/elif` 链**外**，6 个谓词全部继承。问题集中在 `toggled()` 支（`verify.py:342-353` 读实时 `checked`，过渡期 3–4 秒不可靠），与 §5.3.2「仅 before/after 比较的 FAIL 才是权威反证」对不上（`toggled` 是 phase4 后加、非 before/after）。修法精确到**按谓词**：`still_on_activity`/`no_page_change`/`list_count_unchanged`（真·before/after）FAIL 权威；`element_present`/`element_absent`（瞬态）倾向非权威；`toggled`（实时 checked）非权威。这比「取消 `failed` 的 `break`」更精确——它修的是 `authoritative` 契约语义，保留 fail-fast 不变量，落在「契约收敛、补丁发散」框架内。P0 第 3 点已嵌入两张表（通道现状表 + 谓词权威表）。

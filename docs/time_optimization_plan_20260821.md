# 时间损耗分析与优化方案（Run 153540 + 213229 合并版）

- 日期：2026-08-21（终稿，合并 `run_153540_time_reduction_plan` 与 `run_213229_time_analysis_and_optimization_plan`）
- 关联 run：
  - `155600_test-20260821_153540_trace.json`（`passed`，`duration_seconds=1147.1`，112 步）
  - `213229_test-20260820_213229_*`（`inconclusive` + `exhausted`，`duration_seconds=1015.4`，135 步）

---

## 1. 数据基础（实测，非估算）

### 1.1 Run 153540
| 指标 | 值 |
|---|---|
| `duration_seconds` | 1147.1 |
| `llm_call_count` | 95（avg `llm_elapsed≈4.25s`） |
| `llm_elapsed_ms` | 404171.8（占 35%） |
| `input_tokens` | 7,441,822（≈78K/次） |
| `cached_input_tokens` | 6,288,128（**84.5%**，非早期误写的 66%） |
| `step_count` | 112（trace 顶层与 steps 数组一致，seq 0–111） |

巨型步（实测）：seq62 `click(targets=19周)`=52.9s、seq68 `click(targets=20周)`=58.0s、seq24 vision_tap=52s、seq38 visual_check=28.4s、seq39 vision_tap=27s。

### 1.2 Run 213229
| 指标 | 值 |
|---|---|
| `duration_seconds` | 1015.4 |
| `step_count` | 135 |
| `input_tokens` | 10,095,276（≈83K/次） |
| `cached_input_tokens` | 8,736,384（**86.5%** 命中缓存） |
| `llm_call_count` | 121 |
| 动作分布 | `get_screen_info=34`、`click=55`、`vision_tap=4`、`visual_check=7`、`assert_page_contains=5`、`assert_element_exists=10`、`assert_behavior_effect=1`、`assert_page_state=2` |

> **数据可核性注**：213229 的原始 trace（`215049_test-20260820_213229_*`）已从 `logs/runs/` 清理，**上述数据取自历史分析、不可复验**；验收战略层时以"213229 同类重跑"为准，不依赖旧 trace 做 before/after 比对。

213229 trace 无步级 `elapsed`（已补工具级 + 回合级 `elapsed_ms` 埋点）；153540 的 trace 已带步级 `elapsed_ms`，故 153540 的根因可精确到步，213229 的占比为结构性归因。

---

## 2. 根因（两层：战略 + 战术）

### 2.1 战略根因（最顶层，两份 run 一致）
`execution.mode=explore` + `mode_selection_reason=no_matching_plan`（两份 trace 逐字确认）。**慢不是 explore 写得不快，而是"同一个任务第 N 次从零探索"**。治本方向是"让这个任务下次不 explore"（plan 沉淀 + fixture 前置），而非"让 explore 每步少花 30%"。

### 2.2 战术根因（explore 内部损耗）

**Run 153540（有步级耗时，根因精确）**
- **A. 批量点击感知膨胀**（最大战术大头）：`targets` 批处理（click.py:548-566）对每个 target 递归 `click.invoke`，每次重入完整 `click()`（含多次 `perceive()`，perceiver.py:184-203 的 settle 1.5s + ≥2 次 XML dump + 0.3s 轮询）。seq62/68 的 52–58s = 20×(2~3 次 dump+click)，**非** 20×sleep。现状"省掉 N−1 次感知"承诺未兑现。
- **B. 消息历史 token 膨胀**：增量来自 **29 次 `get_screen_info` + 33 次 `click`**（153540 实测计数）的完整输出重放进历史。真正膨胀源是 LangChain 消息列表里未截断的 `ToolMessage`（llm_runtime.py:553），`get_screen_info` 完整 dump（每元素带 bounds/path/rid/class，~2–3K/次）在后续每个 turn 完整重放。静态 system 已固定且缓存（84.5%），非大头。

**Run 213229（结构性归因，A/B/C/D 类别）**
- **A 类 启动 RPC/锁屏故障恢复**：~25%，零业务点击，环境损耗。
- **前提条件重演（seq 0–53，占 40% 步数）**："创建一个空课程表"是确定性操作却让 LLM 花 54 步探索——explore 内最大损耗，应由 fixture 前置消除。
- **B 类 点击+截屏感知（含 20 周逐点）**：~30% 步数（其中 20 周仅 15%）。LLM 早已批量下发（intent 全空、共享 AIMessage），瓶颈在**工具执行侧**每次 click 都触发 perceive→点击→截图→page_sig 再 perceive。
- **C 类 视觉/断言 LLM 往返**：~20%。部分由 `toggled()` 对 chip 误报诱发（已修，见 §4 方案 3a）。
- **D 类 LLM 决策推理**：~24%（**低估**，见下）。平均 83,432 input tokens/次调用，即使 86.5% 命中缓存，模型仍对全量 83K 做 attention，延迟随 token 数增长。

> 战略提示：A+B+C+D 战术优化最多回收 explore 内损耗；但若执行 fixture + plan 沉淀，seq 0–53 的 40% 步数可能**直接消失**（下次 guided 无需从零探索），杠杆远大于战术层之和。

---

## 3. 优化方案（战略层 + 战术层，按 ROI 合并排序）

### 3.1 战略层（10x 杠杆，优先于战术层）
- **想法 #1 fixture 契约（消除 40% 前提重演）**：曾设计为 pre-run 自动清空；**经自测确认已移除**（不存在"每次都要清空"的通用场景，强行清空反增步数与授权弹窗）。`clear_app_data` 原语保留，改由 prompt 驱动 LLM 按需调用。
- **想法 #3 plan 沉淀→guided（10x 杠杆）**：🟡 部分实现（沉淀管线 `nodes.py:1365 extract_candidate_plan` 已接，端到端未验证，213229 仍 `no_matching_plan`）。**二次降延迟**：explore 上下文 83K/调用 → guided 仅 plan 摘要（几 K），既降步数又降单位 LLM 调用耗时。验收重点：下次同任务是否 `mode=guided`、前提步 54→~0、总时长 1015s→~200s。
- **想法 #2 埋点加 mode + plan_id**：✅ 已落地（`llm_runtime.py:633-634` + `run_trace.py:99-100`），可量化 explore vs guided 差异。

### 3.2 战术层（explore 内部损耗，按杠杆）

| 项 | 优先级 | 状态 | 真实验收收益 |
|---|---|---|---|
| **P0 合并感知**（targets 真正只 perceive 一次） | 最高（确定性 ~100s） | ✅ 已落地（click.py:556-588 入口 perceive 一次 + 缓存 + 结尾单次快照） | seq62/68 110s → ~10s |
| **P0 get_screen_info 默认 compact + ToolMessage 输出上界** | 最高（累积） | 🟡 部分（compact 已落地；`_format_click_log` 去重未做） | D 类 `llm_elapsed` 降 15–25% |
| **方案 1 启动自愈/健康预检/快速失败** | 高（A 类） | ✅ 已落地 | 消启动故障恢复损耗 |
| **方案 2 click(targets=)** | 高（B 类） | ✅ 已落地 | **省 tool_call 20→1 + step 预算**（非感知，感知未合并） |
| **方案 3a toggled 读 chip 选中态** | 中（C 类） | ✅ 已落地 | 消 chip 误报→省视觉往返 |
| **方案 3b 确定性断言引导** | 中 | ✅ 已落地 | 视觉往返下降 |
| **方案 4 顺手验证措辞** | 高 | ✅ 已落地 | 操作同回合内验，免预算耗尽丢验 |
| **方案 5 步级/回合级 elapsed_ms** | 地基 | ✅ 已落地 | 可观测，校准占比 |
| **方案 6 瞬时 UI 捕获契约（prompt）** | 中 | ✅ 已落地 | 消瞬时捕获后重复断言 |
| **P1 截图去重（摘双截图 + key 去重）** | 中 | ✅ 已落地（assert 移出 `_SCREENSHOT_ACTIONS` + evidence 按 key 去重） | 79→~40 张 |
| **P2 缩短少数非 Switch 的 sleep** | 低 | ❌ 未做 | ~10–15s |
| **P3 全选按钮（本 App 无全不选）** | 低 | 仅 prompt 提示 | 有限 |

> **交叉纠错（关键）**：原 213229 方案 2 称 targets "省 ~38 次 dump + 19 次截图（感知合并）"已被 153540 §2-A 推翻——targets 仅省 tool_call 次数 + step 预算，**未合并 dump**（每个 sub-click 仍各自 perceive）。真正"合并感知"是上表 P0 项，**现已落地**（click.py targets 入口 perceive 一次 + 缓存复用，见 §4.1）。

---

## 4. 战术层待做项详情（P0 / P1 / P2 / P3）

### 4.1 P0-a targets 真正合并感知（替代早期 153540 方案中"方案1 sleep=200–300s"的错根因）
- **根因纠正**：原方案称"周数 chip 走 `_is_checkbox_like`→`sleep(2.0)`×20=40s"。实测周 chip `role=tab`/`class=TextView`，而 `_is_checkbox_like`（click.py:1093-1095）显式排除 TextView，**不进该分支**；单点芯片仅 2.2–3.8s（无 2s 下限）。`sleep(2.0)` 仅命中少数非 Switch 元素（空日历单元格/色块约 3–5 个，回收仅 ~10–15s）。
- **正确修法**：开头 `perceive()` 一次取 understanding → 对每个 target 在缓存内定位并 `click_bounds`（chip 切换后坐标不变，类比 repeat 固定 bounds 连点）→ 结尾仅一次 `_post_click_snapshot`。行为等价（命中/未命中摘要不变）。预期 seq62/68 ~110s → ~5–10s（净回收 ~100s）。
- **行为边界**：合并感知后，**仅 textview/tab/role 类目标**（如本用例周 chip `role=tab`、`class=TextView`）跳过"点后 re-perceive + `_check_switch_state`"，其反馈退化为"已点"而非"已勾选"；而 **checkbox/switch 类目标在批量内仍保留 `sleep(2.0)` + `_check_switch_state` 的个体 re-perceive 逻辑**（不享受快路径合并，仅随批量共用入口一次 perceive）。契约层须注明：switch/checkbox 类命中后 LLM 仍可用 `assert_behavior_effect` 单独补验。
- **契约层扩展（label→index 误解析防范）**：当前 `targets` 批量循环只转发 `label`（click.py:560），`index` 默认 -1、`exact_mode=False`，纯数字走语义搜索而非 index，当前**不会**误解析。若需 `path_contains="weeks_chip_group"` 结构化定位，须先扩展 targets 契约（每个 target 可携带定位依据），这是设计变更非 prompt 能解。

### 4.2 P0-b get_screen_info 默认 compact + ToolMessage 输出上界
- **compact 字段优先级**：`_format_element_line` 现状每行带 `bounds=`（~20 字符 × 元素数），**bounds 才是最大噪声、最该省**（LLM 从不消费 bounds）；`rid`/`path` 仍被 click 契约使用。→ **保留 `index`+`label`+`role`+`[SELECTED]/[DISABLED]/[CLICKABLE]`，`rid` 可选；省 `bounds`+`path`+`class`**。
- **方向校准（防优化成退化）**：**切勿删"当前可点列表"**（`_build_clickable_summary` click.py:1046 是反退化优化：`[n] [SELECTED] label` 已极紧凑，删它会导致 LLM 每次 click 后必再发 `get_screen_info` 拿 index，既多一次 perceive 又多一条完整 dump，净亏）。真正冗余在 `_format_click_log` 的 `role/region/label/rid/assoc/path` 与 evidence 块（`resolved_*`）**重复两遍**，应紧凑化 `_format_click_log`（去与 evidence 重复的 path/assoc/region）；可点列表**保留并提上限/分页**（现 `limit=12` 对 25 周弹窗不全）。
- 对重放进历史的 `ToolMessage` 输出做 token 上界，compact 输出在后续每轮都省 token（累积收益）。

### 4.3 P1 截图去重（先摘"双截图"，再按 key 去重）
- 实测 79 张 = 49 step（`{seq}_{ts}.png`）+ 30 evidence（`evidence_*.png`）。`assert_page_contains`/`assert_element_exists` 每次同时产 2 张几乎相同的图：step 截图（因在 `_SCREENSHOT_ACTIONS` 内，llm_runtime.py:60-68）+ evidence 截图（verify.py:205/306）。seq27–34 连发 8 次、落 8 张 step 截图（实为双截图，连 evidence 共 14 张）。
- 仅按 `verification_key` 去重 evidence 一路只能 14→~8 张，**砍不掉一半**。
- **正确修法（两步，低风险）**：(1) 把两个 assert 从 `_SCREENSHOT_ACTIONS` 移除——它们内部已 self-screenshot 成 evidence，step 截图纯重复，砍成单截图；(2) 对 `_save_evidence_screenshot` 按 key 去重（去掉全局 seq 后缀、`evidence_{key}.png` 覆盖写入）。本质对齐 `_save_perceive_evidence_screenshot`（`evidence_{key}_vision.png` 无 seq、天然覆盖，已去重）。**已落地**：llm_runtime.py:60-70 移除 assert 工具、verify.py:45 起 `evidence_{key}.png` 按 key 去重。预期 79→~40 张。

### 4.4 P2 / P3
- **P2**：`_is_checkbox_like` 内对非 Switch 的即时 toggle 缩短 `sleep`（空单元格/色块等），回收 ~10–15s。
- **P3**：全选按钮——本 App 周数弹窗仅有"全选/单周/双周"三个 radio，无"全不选"按钮，此用例收益有限；保留为 prompt 通用提示。

### 4.5 已落地战术项（勿重复实现）
- 方案 1 启动自愈：`orchestrator.py:_preflight_device_health` ✅
- 方案 2 `click(targets=)`：`click.py:513-562` ✅（省 tool_call，非感知）
- 方案 3a `toggled` 读 chip 选中态：`verify.py:615-627` ✅
- 方案 3b / 方案 4 / 方案 6 / 方案 5 / §9(c) click 返回 `[n]` 列表 / 想法 #2：均 ✅ 已落地
- 想法 #1 fixture：❌ 已移除（改为 prompt 驱动 clear_app_data）

---

## 5. 验收口径（合并两份）

- **战术 P0 验收（153540 同类重跑）**：`duration_seconds`（1147→~700 预期）、`input_tokens`、`seq62/68` 的 `elapsed_ms`（110→~10s）、截图数（79→~40）。P0-a 回收 ~100s 为确定性项，最先验证。
- **战略层验收（213229 同类重跑）**：下次同任务是否 `mode=guided`（非 explore）；前提步骤 54→~0；总时长 1015s→~200s 量级；对比 explore vs guided 的 `llm_elapsed_ms`（想法 #2 埋点）。
- **方案 2 验收（纠错后）**：验收维度是 **tool_call 次数 20→1 + step 预算**，`elapsed_ms` 不会显著低于 20 次单独之和（感知未合并，见 P0-a）。
- **回归**：跑 `tests/test_tools_verify_anchor_resolve.py`、`test_verification_contract.py`、`test_agent_prompts_m4.py`，确认 prompt 引导不破坏既有断言行为。

---

## 6. 已知风险与坑（合并）

- **batch 命中回报（当前准确，重构勿丢）**：`observation` 在 entry 被 `output[:200]` 截断（llm_runtime.py:626），但喂给 LLM 的 `ToolMessage` 是完整 `output`（llm_runtime.py:553），含完整 `BATCH_OK: 1: OK ... 20: OK`（seq64 `targets=["1","3","5"]` 正确返回 `BATCH_OK: 1: NOT_FOUND`）。当前无静默失败；P0-a 重构后须保证逐 target 摘要仍完整回传。
- **compact 兼容性**：`get_screen_info` 改默认 compact 须确认消费方（prompt、解析）不依赖被省略的 path/class 字段。
- **P0-b 防退化**：删"当前可点列表"会倒退回"click 后必发 get_screen_info"，属优化成退化，严禁。
- **targets 页面跳变风险（与 P0-a 快路径一致，勿重新 perceive）**：点 chip A 弹窗覆盖 chip B 时后续项会点错。缓解走**纯快路径**——P0-a 的「一次 perceive + 全部 `click_bounds` 缓存坐标」核心收益即来自"不重新感知"，故**每点前只在缓存的 understanding 内校验该 target 是否仍可见（不重新 perceive，便宜），不可见即停（`stop_on_first_failure` 默认 True）**；NOT_FOUND / 页面跳变导致的遮罩覆盖，由后续 `get_screen_info` 兜底发现，而非在批处理内逐个 re-perceive（那会把 ~100s 收益吐回）。
- **C 类多模态固有成本**：时间滚轮验证（Canvas 自绘，仅视觉）153540 seq24/38/39/40/41/42/43 合计 ~170s，非 sleep/token 可解；记入战略 backlog（fixture/guided 下次不重演，或给确定性滚轮结构化工具）。
- **数据质量**：早期文档 cached 66%、seq 号归属、vision_tap 截图描述均与 trace 不符，已更正；后续对比以本版数据为准。

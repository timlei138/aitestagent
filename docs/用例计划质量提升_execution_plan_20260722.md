# 用例计划质量提升（Replay Evidence 注入）— 改进方案 v2.0

> 文档版本：2026-07-22 v2.0
> v2.0 相对 v1.1 的变更：吸收 team1（3 项）+ team2（5 P0 + 6 P1 + 5 观测）= **19 条 review 全部采纳**。
> 核心重写点：执行事件统一契约（所有工具都记 `tool_input`）、launch_app 复用 `results.py` 统一契约（不再 JSON）、page_signature 改软提示、verification_evidence 不再硬编码、locator 全量保留（删伪优先级）、lineage 完整传递链、UI 改动 `evidence_stale` 语义、quality 改离散能力标签。
> 状态：方案稿 v2.0，待 Review
> 关联文档：
> - `docs/用例管理_复用计划重跑_20260721.md` v3.3（前置方案，已实现）
> - `docs/综合评审与问题分类_20260711.md` §0 指导原则 + M4 确定性断言 + N1/N2 click 定位
> - `docs/用例计划质量提升_execution_plan_review_20260722.md`（v1 review #1）
> - `docs/用例计划质量提升_review_20260722.md`（v1 review #2）
> - `docs/用例计划质量提升_review_team1_20260722.md`（v1.1 team1 review，3 项）
> - `docs/用例计划质量提升_review_team2_20260722.md`（v1.1 team2 review，5 P0 + 6 P1 + 5 观测）

---

## 0. 背景与现状

### 0.1 问题起源

v3.3 已落地「成功报告 → 保存为用例 → 复用计划重跑」全链路。复跑机制本身 work —— 通过 `route_start` 跳过 planner + `reuse_plan=True` 预填 `goal_description` + busy-guard 串行化，让复跑比普通 run 显著省耗。但 2026-07-22 实跑 3 条对照数据显示：**复跑节省的幅度完全取决于「plan 的精确度」**，而当前 plan 来源（planner LLM 输出口述）丢失了大量 steps_json 里已经成功验证过的执行细节。

### 0.2 三条对照数据（logs/runs/，2026-07-22 10:35-10:52）

**任务**：`检查Setting 中WLAN开关是否可以正常打开关闭`，应用 `com.android.settings`，三条都 `verdict=passed`。

| 维度 | 103153（普通） | 104626（复跑 #1，源 103153） | 105055（复跑 #2，源 581d1532b90e） |
|---|---|---|---|
| run_type | normal | rerun | rerun |
| source_run_id | — | test-20260722_103153 | `581d1532b90e` ⚠️ |
| **时长** | **191.3s** | **127.0s**（-34%） | **59.1s**（-69%） |
| **LLM 调用数** | **25** | **21**（-16%） | **9**（-64%） |
| **总 token** | **631,319** | **360,910**（-43%） | **98,669**（-84%） |
| 未缓存输入 token | 29,879 | 17,046 | 6,751 |
| 输出 token | 3,680 | 2,744 | 1,806 |
| **总步数** | 15 | 13 | 6 |
| **点击次数** | 4 | 4 | 2 |
| click exact / semantic | 4 / 0 | 3 / 1 | 2 / 0 |
| RAG query_app_knowledge 调用 | 1 | 0 | 0 |
| 进入 planner | 是 | 否（跳过） | 否（跳过） |
| NOT_FOUND 步 | seq4 (WLAN) + seq8 (WLAN rid) | seq1+2 (WLAN×2) | 0 |
| langchain.log 体积 | 2.1 MB | 1.2 MB | 0.33 MB |

### 0.3 关键观察（v2.0 修正：team2 P1-1）

1. **105055 的高效率不能拆开归因**（review #2 §3 + team2 P1-1 修正）：
   - 6 步（2 click + 2 assert_verification + 1 assert_page_contains + 1 report_done）全 exact，9 LLM / 99k token / 59s 跑完。
   - **起始 Activity 已经是 `Settings$WifiSettingsActivity`**（seq 1 page_to 即此），与 103153 seq 7 launch 之后才到达的页面完全相同。
   - **RAG=0 修正**：v1.1 归因"`_rag_query_cache` 跨 run 复用"**错误**。实测 `agents/orchestrator.py:20-48` 的 `_reset_run_scoped` **每次新执行前**都会 `ctx._rag_query_cache.clear()`（v3.3 R1 已落实）。所以 105055 的 RAG=0 **只意味着这次 run 没显式调 `query_app_knowledge`**，与 cache 跨 run 复用**无关**。
   - 105055 的 `source_run_id=581d1532b90e` 12 位 hex 极可能是 case id 而非 run id（team2 P0-5 已识别 lineage 字段混存问题）。
   - **结论**：105055 的高效 = 入口状态对齐 + 跳过 planner + 已有上下文/RAG 信息耦合，**当前无法分离归因**。需统一条件后受控 A/B 才能分别量化各贡献。

2. **104626 是典型的「plan 拖累复跑」** —— 21 LLM / 361k token / 13 步 / 1 个 semantic 匹配 + 2 次 NOT_FOUND，**只省 ~16% LLM 步数 / ~43% token / ~34% 时长**（注意：~43% token 中含 cache 贡献约 95% 的命中率，未缓存输入 token 实际只省 ~43%）。

3. **103153 的 `steps_json` 真实结构**（DB 原样，仅这些 keys：`index`/`action_type`/`target`/`intent`/`intent_text`/`observation`/`page_from`/`page_to`/`duration_ms`/`status`/`screenshot_path`/`anomaly`/`raw_observation`）：
   - `tool_input`/`match_mode`/`fallback_used` **在 `_build_display_steps` 中被丢弃**（v1.1 已识别，team2 P0-1 进一步指出：运行时**只有 click** 写了 `tool_input`，launch_app/assert_* 都没有 → 提炼器拿不到 `launch_app.activity` 等关键参数）。
   - seq 7 `launch_app com.android.settings` → observation `"已启动: com.android.settings/com.android.settings.Settings$WifiSettingsActivity"`
   - seq 8/9/12 `click WLAN` → `target="WLAN"`、observation 含 `"开关状态: 开启 | 操作后页面: Settings$WifiSettingsActivity"`
   - seq 16 `agent` 收尾：`page_from=CameraActivity → page_to=Settings$WifiSettingsActivity`

4. **103153 的 `goal_json`（也就是 104626 复跑拿到的）**：
   ```json
   {
     "goal": "验证Settings中WLAN开关可以正常打开和关闭",
     "app_package": "com.android.settings",
     "app_name": "Settings",
     "target_pages": ["Wifi设置页"],
     "verification": ["WLAN开关状态变为开启", "WLAN开关状态变为关闭"],
     "hints": [
       "设置使用双栏布局，左侧导航列表需滚动找到'WLAN'并点击",
       "进入Wifi设置页后，找到WLAN开关（Switch控件）并点击切换",
       "先点击打开开关，验证状态变为开启；再点击关闭开关，验证状态变为关闭"
     ]
   }
   ```

### 0.4 根因：plan 缺什么

| 关键信息 | 在 steps_json observation 里？ | 进 plan 了吗？ | 复跑时 LLM 知道吗？ |
|---|---|---|---|
| **进入入口 Activity**（`Settings$WifiSettingsActivity`） | 是 seq 7/16 | 否 | 否 → LLM 只能 `scroll_find_and_click`，104626 又花 5 步在主页找 |
| **元素定位参数集**（`label + index + class_name + path_contains`） | 否 steps_json 不存（仅 observation 摘要） | 否 | 否 → LLM 只能 `label="WLAN"`，104626 误点 `settings_entry` 进子页 |
| **稳定可点击的 role**（`switch_row`） | 否 | 否 | 否 → 104626 第一次点错 |
| **真实路径**（`content_frame > main_content > container_material`） | 否 | 否 | 否 |
| **入口 page_to 摘要** | 是 seq 16 | 否 | 否 |

**本质**：`goal_json` 是 **planner LLM 看了截图后口述给 LLM 的话**，**不是从已成功 steps_json 反向重建出的可执行契约**。复跑时 LLM 拿到的 hint 仍是「左侧导航需滚动找 WLAN」——它**不知道有 `Settings$WifiSettingsActivity` 这个直接入口**，于是又走了一遍 103153 早期（seq 1-5）那段弯路。

### 0.5 期望目标（v2.0 修正后）

**前提条件**（不满足则不能达成目标）：
1. **执行事件必须先结构化落库** —— `tool_input` / `status_code` / `page_before/after` / `resolved_target` 当前都没存完整（team2 P0-1）。本方案 §3.1 P0 前置。
2. **统一条件 A/B**（review #2 §3）：设备状态、App version code、ROM/分辨率、语言/方向、WLAN 初始状态、网络条件一致。
3. **RAG 缓存已通过 v3.3 R1 在 `_reset_run_scoped` 清空**（实测 `orchestrator.py:35-37`），**不存在跨 run cache 复用**（team2 P1-1 修正）。

**收益预估（仅参考，不作保证，team2 观测 2/3 采纳）**：
- **104626 类型 case**（起始在 Settings 主页、需导航到 WLAN 设置页）：v2.0 后**假设**未缓存输入 token 节省 ~47-59%、NOT_FOUND 消除、全 click exact / semantic（v1.1 推算范围）。
- **105055 类型 case**（起始已在目标页、起始 Activity 已对齐）：当前无法分离归因；A/B 后再单独标定。
- **样本要求**：单 case 3 遍取中位数为探索性，**要下"稳定收益"结论需增加样本 + 失败率记录**。

### 0.6 v1.0 / v1.1 全部 review 处置总表

| 编号 | 来源 | 内容 | 成立 | v2.0 处置 |
|---|---|---|---|---|
| R1 | review#1 §三.1 | EXECUTION_PLAN_BLOCK 不够"可执行"，缺 observation 摘要 | 是 | §3.5：每条 key_action 附 `last_observation` + `last_result` |
| R2 | review#1 §三.2 | "禁止 LLM 加步骤"踩 §0 | 是（红线） | §3.5：删除禁止句式；改"优先复用 + 失败时自主调整" |
| R3 | review#1 §三.3 + #2 §7 | `assert_page_contains` / `assert_element_exists` 进 key_actions | 是 | §2.2 / §3.4：3 类断言进 key_actions |
| R4.1 | review#1 §四.1 | 取第一个 launch_app 取错 | 是 | §3.4：取第一个 `args.activity != ""` 的 launch |
| R4.2 | review#1 §四.2 | click 提取未过滤 NOT_FOUND 步 | 是 | §3.4：`status_code`/observation 含 `NOT_FOUND`/`AMBIGUOUS` 跳过 |
| R4.3 | review#1 §四.3 | 105055 source_run_id 脏数据 | 部分（根因是 lineage 混存） | 并入 team2 P0-5 |
| R4.4 | review#1 §四.4 | NOT_FOUND 触发 RAG 提示 | **不采纳**（§0 红线） | 删 |
| R5 | review#2 §3 | 105055 高效归因错 | 是 | §0.3、§0.5 修正 |
| R6 | review#2 §4 | steps_json 缺结构化字段 | 是（v1.1 落地不完整） | §3.1 P0 前置：team2 P0-1 进一步扩展到**所有工具** |
| R7 | review#2 §5 | 不当脚本（EXECUTION_PLAN → REPLAY_EVIDENCE） | 是 | §3.5：块改名 + 措辞重写 |
| R8 | review#2 §6.1 | launch_app 渲染为 `launch_app(package=..., activity=...)` | 是 | §2.2：改 `entry.launch_app_args` |
| R8b | review#2 §6.1 | launch_app 不知真到目标 Activity | 是 | §3.3（**team2 P0-2/P0-3 进一步用统一契约**） |
| R9 | review#2 §6.2 | click 不支持 role | 是 | §2.2 / §3.1：role 进 `resolved_target`（**team2 P1-2 修正来源**） |
| R10 | review#2 §6.3 | index 不跨 run 稳定 | 是 | §3.4：locator 全量保留 6 字段（**team1 #2 修正**） |
| R11 | review#2 §7 | 客观断言不能只保留 assert_verification | 是 | 与 R3 合并 |
| R12 | review#2 §8 | 提炼算法污染 | 是 | §3.4：run 门控 + 失败步过滤 |
| R13 | review#2 §9 | source_run_id 混存 run/case | 是 | §3.2：拆 `source_case_id` + `execution_plan_revision`（**team2 P0-5 进一步补全传递链**） |
| R14 | review#2 §10 | plan_quality_json 不宜单独落库 | 是 | §3.1：删列；§3.6：API 派生 |
| R15 | review#2 §11 | token 通用基础设施优化 | 解耦 | §7.5：独立 P1 跟进项（不在本 PR） |
| R16 | review#2 §12 | click 质量 6 维指标 | 解耦 | §7.5：独立 P2 跟进项（不在本 PR） |
| R17 | review#2 §13 | 实施顺序 | 是 | §6：按 review#2 §13 顺序 |
| **T1-1** | team1 #1 | §3.1 行号错（`llm_runtime.py:11449-1153` 不存在） | 是 | §3.1 / §8：改为 `:437-454` 实际行号 |
| **T1-2** | team1 #2 | locator 优先级"伪规则"无效，全量保留 6 字段即可 | 是 | §3.4：删伪优先级；强调"全量保留，click 工具自处理歧义" |
| **T1-3** | team1 #3 | `_extract_status_code` 缺伪代码 | 是 | §3.1：补伪代码 |
| **T2-1** | team2 P0-1 | 运行时只有 click 写 tool_input，其他工具没有 | 是 | §3.1：所有工具统一记录 tool_input；click 额外 `resolved_target` |
| **T2-2** | team2 P0-2 | launch_app 改纯 JSON 违反 `results.py` 统一契约 | 是（红线） | §3.3：**复用 `make_result` 契约**，不发明 JSON；细化 `UNSPECIFIED` 默认而非 OK |
| **T2-3** | team2 P0-3 | `ok=True` 不等于真到 Activity，需 settle 等待 | 是 | §3.3：launch_app 内加**通用 settle 等待**（不是针对 Settings 的补丁） |
| **T2-4** | team2 P0-4 | page_signature 含动态 labels hash，跨 run 不稳定 | 是 | §3.5：拆 `expected_activity`（强）+ `required_anchors`（强）+ `soft_page_signature`（软提示） |
| **T2-5** | team2 P0-5 | lineage 改动的调用链未闭环；create_test_case 当前无 source_case_id 形参 | 是 | §3.2 / §4：列完整传递链；§3.4 伪代码改用已有 `source_run_id` 形参（**`source_case_id` 不在 test_cases**） |
| **T2-6** | team2 P1-1 | `_reset_run_scoped` 实际清 `_rag_query_cache`，v1.1 归因错 | 是 | §0.3：明确 RAG=0 与 cache 跨 run 无关 |
| **T2-7** | team2 P1-2 | `expected_target.role` 从 tool_input 永远取不到 | 是 | §3.1 / §3.4：role 改由 click.py 显式落库 `resolved_target`；提炼器从 `resolved_target` 读 |
| **T2-8** | team2 P1-3 | "取第一个 launch + entry 为空丢全部 evidence"不可取 | 是 | §3.4：entry 找不到时返回**无 entry 的 execution_plan**（保留 click/断言 evidence） |
| **T2-9** | team2 P1-4 | `verification_evidence.subjective` 从 verdict 反推，丢真实结果 | 是 | §3.4：从 `run.verification_json` 读每个 assertion 真实 `result`/`detail`/`review_required` |
| **T2-10** | team2 P1-5 | UI 编辑无 schema 校验 + evidence_stale 语义 | 是 | §3.6：后端 schema 校验；原始 evidence 不可变；人工修改 = 新 revision + `evidence_stale=true` |
| **T2-11** | team2 P1-6 | quality 仍把 index 与 rid 等价 | 是 | §3.6：改**离散能力标签**：`has_verified_entry` / `has_stable_locator` / `index_only_locator` / `has_objective_evidence` / `environment_compatible` / `evidence_stale` |
| **T2-12** | team2 观测 1 | exact / fuzzy 应改 exact / semantic | 是 | §0.2 表格已改 |
| **T2-13** | team2 观测 2 | "0 NOT_FOUND、60% 节省" 不应写成保证 | 是 | §0.5：明确"假设/验收参考值" |
| **T2-14** | team2 观测 3 | 3 遍中位数仅探索性 | 是 | §3.8：标注"探索性" |
| **T2-15** | team2 观测 4 | 应记录 App version code/ROM/分辨率/语言/方向 | 是 | §3.8：补环境记录项 |
| **T2-16** | team2 观测 5 | 文档单行 40733 字符，行号失效（**根因**） | 是 | 本 v2.0：恢复真实换行；所有行号引用基于写完后实测 |

**统计**：v1.0（17 条）+ team1（3 条）+ team2（16 条）= **36 条 review 全部处置**：35 条成立（其中 1 条 v1.1 R4.4 不采纳）+ 1 条 v1.1 R4.4 不采纳。

---

## 1. 设计原则（v2.0 修正后）

| 原则 | 落地含义 |
|---|---|
| **代码沉淀事实，LLM 决策路径** | 历史成功 run 中的入口 Activity / locator / 客观断言 / 页面事实沉淀；LLM 在事实上自主判断 |
| **Replay evidence ≠ 强制脚本** | REPLAY_EVIDENCE_BLOCK 是参考/事实，不当脚本（R2/R7）；失败时允许 LLM 回退/重感知 |
| **可执行契约 > 自然语言 hint** | 落库结构化执行事件（`tool_input` / `status_code` / `page_before/after` / `resolved_target`），不靠 observation 文本反推 |
| **统一工具契约** | 所有工具返回遵守 `tools/results.py` 统一 STATUS 契约（`make_result`/`parse_status`/`parse_evidence`）；不发明单独 JSON 协议（team2 P0-2） |
| **客观断言是 ground truth** | `assert_page_contains` / `assert_element_exists` PASS = 权威（M4），与 `assert_verification` 分层并存 |
| **手工可调 + 不可变 evidence** | 原始提炼 evidence 不可变；人工修改 = 新 revision + `evidence_stale=true`（team2 P1-5） |
| **schema 统一 > 多入口多 schema** | 3 入口（报告复制 / 从零新建 / 编辑）共用同一 `goal_json.execution_plan` 结构 |
| **lineage 类型化** | 拆 `test_runs.source_run_id`（仅 run 复跑）/ `source_case_id`（仅 case 复跑）/ `execution_plan_revision` |
| **页面前置条件分层** | `expected_activity`（强）+ `required_anchors`（强）+ `soft_page_signature`（软提示，team2 P0-4） |
| **quality 离散能力** | 不计"字段填充数"；用离散能力标签（`has_stable_locator` / `index_only_locator` 等，team2 P1-6） |
| **增量落地 > 全量重写** | v3.3 文件上最小侵入；指标层/token 优化不混入本 PR |
| **文档可读性** | 真实换行 + 行号引用基于写完后实测（team2 观测 5） |

---

## 2. 方案概览

### 2.1 数据流（端到端）

```
[运行时 _tools_node 入口，agents/llm_runtime.py:437-454]
        │
        │ 统一记录：tool_input (所有工具) + click 额外 resolved_target
        ▼
[_current_log: list of event dicts]            ← 含 tool_input / status_code / page_*/resolved_target
        │
        │ (新增) _build_display_steps 扩字段
        ▼
[test_runs.steps_json]                        ← 落库结构化执行事件
        │
        │ (新增) _extract_replay_evidence(steps_json, run_result, verification_json)
        ▼
[goal_json.execution_plan.replay_evidence]     ← 客观事实 + 软前置条件
        │
        │ create_test_case(run_id=...) / update / saveCase
        ▼
[test_cases.goal_json]                        ← 用例落库（lineage = source_run_id）
        │
        │ run_test_case / WS run_case / WS rerun
        ▼
[orchestrator.start(goal_description, source_case_id=..., source_run_id=...)]
        │
        │ (新增) system prompt 渲染：REPLAY_EVIDENCE_BLOCK
        ▼
[agent_node]                                  ← LLM 在事实上自主决策
```

### 2.2 execution_plan schema（v2.0 修正后）

```jsonc
{
  // 现有 v3.3 字段（保留）
  "goal": "...",
  "app_package": "...",
  "app_name": "...",
  "target_pages": ["..."],
  "verification": ["..."],
  "hints": ["..."],

  // v2.0 新增：Replay Evidence（事实/参考，不是脚本）
  "execution_plan": {
    "schema_version": 2,
    "extracted_from_run_id": "test-20260722_103153",
    "extracted_at": "2026-07-22T11:00:00",
    "extraction_revision": 1,

    "entry": null,        // 可空（team2 P1-3：起始已在目标页时 entry 为空但 key_actions 仍可复用）

    "preconditions": {              // team2 P0-4：分三层
      "expected_activity": "com.android.settings.Settings$WifiSettingsActivity",  // 强
      "required_anchors": [         // 强：必须存在这些元素才算"到位"
        {"label": "WLAN", "role": "switch_row"}
      ],
      "soft_page_signature": "..."  // 软提示：仅作辅助诊断，不匹配 = 重新感知，不直接否定
    },

    "key_actions": [                // 全量保留 click 接受的 6 字段，click 工具自处理歧义（team1 #2）
      {
        "step": "open_switch",
        "tool": "click",
        "locator": {                 // click 工具接受的 6 字段（v1.1 §2.2 保留）
          "label": "WLAN",
          "rid": "",
          "class_name": "LinearLayout",
          "path_contains": "content_frame > main_content > container_material",
          "index": 3,
          "alternatives": ""
        },
        "resolved_target": {         // team2 P1-2：role 等 click 解析事实必须来自 click 内部记录
          "label": "WLAN",
          "role": "switch_row",      // 来自 click 工具解析（不是 agent 调用参数）
          "rid": "android:id/switch_widget",
          "class_name": "LinearLayout",
          "path": "content_frame > main_content > ..."
        },
        "last_observation": "开关状态: 开启 ✓",
        "last_result": "OK"
      },
      {
        "step": "assert_on",
        "tool": "assert_page_contains",
        "args": { "text": "已连接" },
        "last_observation": "PASS",
        "last_result": "PASS"
      },
      {
        "step": "verify_v0",
        "tool": "assert_verification",
        "verify_key": "v0",
        "last_observation": "记录完成: WLAN开关状态变为开启 → passed",
        "last_result": "passed"
      },
      { "step": "close_switch", "tool": "click", "locator": { /* 同上 */ }, "resolved_target": { /* 同上 */ } },
      { "step": "assert_off",  "tool": "assert_page_contains", "args": { "text": "..." } },
      { "step": "verify_v1",   "tool": "assert_verification", "verify_key": "v1" },
      { "step": "done",        "tool": "report_done" }
    ],

    "verification_evidence": {      // team2 P1-4：从 run.verification_json 读真实结果
      "v0": {
        "item": "WLAN开关状态变为开启",
        "subjective": {              // agent 报告（不保证 ground truth）
          "result": "passed",
          "detail": "...",
          "review_required": false
        },
        "objective": [               // 确定性断言（M4 PASS = 权威）
          { "kind": "assert_page_contains", "text": "已连接", "result": "PASS" }
        ]
      },
      "v1": { /* 同上 */ }
    }
  }
}
```

**v2.0 相对 v1.1 的关键变化**：
- `entry` 可为 `null`（team2 P1-3）
- 新增 `preconditions` 三层（team2 P0-4），取代 `entry.page_*_signature` 单一字段
- `resolved_target` 独立存（team2 P1-2），role 不再从 tool_input 取
- locator 全量 6 字段（team1 #2），无伪优先级
- `verification_evidence.subjective` 改 dict（team2 P1-4）

### 2.3 落地分 9 步（按 review#2 §13 + team2 P0 顺序，v2.0 调整）

| Step | 改动文件 | 工作量 | 风险 | 关联 review |
|---|---|---|---|---|
| **1 (P0 前置)** | `agents/llm_runtime.py:437-454` + `agents/orchestrator.py::_build_display_steps` | M | 中 | R6 / T2-1 / T2-7 |
| **2 (P0)** | `tools/click.py`：落库 `resolved_target` | S | 低 | T2-7 |
| **3 (P0)** | `tools/device_ops.py::launch_app`：复用 `results.make_result` 契约 + 通用 settle 等待 | S | 低 | T2-2 / T2-3 |
| **4 (P0)** | `data/relational.py`：lineage 字段拆分 + 删 `test_cases.plan_quality_json` | S | 低 | R13 / R14 / T2-5 |
| **5 (P0)** | `api/test_cases_routes.py`：`_extract_replay_evidence`（完整版） | M | 中 | R1-R12 / T2-8 / T2-9 |
| **6 (P0)** | `agents/nodes.py`：`REPLAY_EVIDENCE_BLOCK` 渲染（分层前置条件） | S | 低 | R1/R2/R7 / T2-4 |
| **7 (P0)** | lineage 全链路传递（server.py WS / orchestrator.start / state / nodes.py reporter / record_test_run） | M | 中 | T2-5 |
| **8 (P1)** | UI 编辑 + quality 离散能力 + evidence_stale 语义 | M | 中 | R14 / T2-10 / T2-11 |
| **9 (P2)** | 受控 A/B 测试 | L | 低 | R5 / T2-13/14/15 |

---

## 3. 详细设计

### 3.1 Step 1 (P0 前置)：所有工具统一落库 `tool_input` + `status_code` + page signatures

**目标**（R6 + team2 P0-1）：让 `test_runs.steps_json` 含**所有工具**真实参数、状态码、页面前后签名。

**当前问题**（已实测）：
- `agents/llm_runtime.py:437-454` 只在 `if name == "click":` 内写 `entry["tool_input"]`（T2-1 准确指出其他工具没）。
- `_build_display_steps` 在 `agents/orchestrator.py:892` 取 `name`/`target`/`intent_text`/`observation`/`screenshot_path`/`tool_seq`——**把 `tool_input` 整个丢掉**。

**改动 A**（`agents/llm_runtime.py:437-454`，所有工具统一记录）：
```python
# 实时记录工具调用日志（过滤感知类，不去重）
if name not in _SKIP_EMIT:
    entry: dict[str, Any] = {
        "name": name,
        "target": target_hint,
        "intent_text": (getattr(last_ai, "content", "") or "").strip()[:200],
        "observation": output[:200],
        "screenshot_path": _screenshot_path,
        "tool_seq": len(_current_log),
        "tool_input": dict(args or {}),                  # v2.0：所有工具统一记录
        "status_code": _extract_status_code(output),    # v2.0：从 tools/results.parse_status
        "page_before_signature": page_sig_before,       # v2.0：调用前 page_signature
        "page_after_signature": page_sig_after,         # v2.0：调用后 page_signature
    }
    # click 额外：match_mode / fallback_used / resolved_target
    if name == "click":
        entry["match_mode"] = _resolve_click_match_mode(name, args, output)
        entry["fallback_used"] = _resolve_click_fallback(output)
        # resolved_target 由 click 工具自身在 args/results 中返回，落库见 Step 2
    _current_log.append(entry)
```

**`_extract_status_code` 补伪代码**（team1 #3 采纳）：
```python
def _extract_status_code(output: str) -> str:
    """从工具输出提取 L1 状态码（复用 tools/results.parse_status 契约）。
    未解析到规范状态时返回 "UNSPECIFIED"（不默认 OK，team2 P0-2）。"""
    from tools.results import parse_status
    parsed = parse_status(output)
    return parsed or "UNSPECIFIED"
```

**改动 B**（`agents/orchestrator.py::_build_display_steps`）：
```python
def _build_display_steps(history: list, tool_calls_log: list) -> list[dict]:
    result = []
    idx = 0
    for t in tool_calls_log:
        idx += 1
        step = {
            "index": idx,
            "intent": (f"{t['name']}('{t.get('target','')}')"
                       if t.get("target") else t["name"]),
            "intent_text": t.get("intent_text", ""),
            "action_type": t["name"],
            "target": t.get("target", ""),
            "page_from": "",
            "page_to": "",
            "duration_ms": 0,
            "status": "continue",
            "observation": t.get("observation", ""),
            "raw_observation": t.get("observation", ""),
            "screenshot_path": t.get("screenshot_path", ""),
            "anomaly": None,
            # v2.0 新增：结构化执行事件
            "tool_input": dict(t.get("tool_input") or {}),
            "tool_seq": t.get("tool_seq", idx),
            "status_code": t.get("status_code", "UNSPECIFIED"),
            "page_before_signature": t.get("page_before_signature", ""),
            "page_after_signature": t.get("page_after_signature", ""),
        }
        # click 额外
        if t.get("match_mode"):
            step["match_mode"] = t["match_mode"]
        if "fallback_used" in t:
            step["fallback_used"] = bool(t["fallback_used"])
        result.append(step)
    for s in history:
        idx += 1
        result.append({**s, "index": idx})
    return result if result else history
```

**回退兼容**：旧 v3.3 run 的 `steps_json` 不含新字段，提炼器读不到则跳过；不影响落库；这些 run 不能作为 evidence 源（必须 Step 1 部署后跑出的 run 才行）。

### 3.2 Step 2 (P0)：click 工具落库 `resolved_target`

**目标**（team2 P1-2）：role/rid/path 等**解析事实**（不是 agent 调用参数）必须由 click 工具自己记录。

**改动**（`tools/click.py`）：click 工具内部在 `make_result(...)` 时把 `resolved_target` 写进 evidence 段（`results.py` 已支持 `make_result(status, message, evidence={...})` 形式）：

```python
# 在 click() 内部，最终调用 tools/results.py:make_result：
return make_result(
    status,
    message,
    evidence={
        "match_mode": match_mode,
        "fallback_used": str(fallback_used),
        "resolved_label": resolved.get("label", ""),
        "resolved_role": resolved.get("role", ""),
        "resolved_rid": resolved.get("rid", ""),
        "resolved_class": resolved.get("class_name", ""),
        "resolved_path": resolved.get("path", ""),
    }
)
```

这样 `_extract_status_code` + `_run_agent` 现有解析路径**不破坏**（team2 P0-2 满足），且 `resolved_*` 经由 evidence 段进入 `tool_calls_log[entry].tool_input`（不对，应进 entry 顶层——具体在 Step 2 落库后由 `_run_agent` 解析 evidence 段填入 `entry.resolved_target`，与现有 `match_mode`/`fallback_used` 同样的解析路径）。

### 3.3 Step 3 (P0)：launch_app 复用 `results.make_result` 契约 + 通用 settle

**目标**（team2 P0-2 + P0-3）：
- 不发明 JSON 协议，**复用 `tools/results.make_result` 契约**
- 启动后**通用 settle 等待**（不是针对 Settings 的补丁）
- 返回结构化证据让 LLM 看到 `requested_activity`/`observed_activity`/`arrival_confirmed`

**改动**（`tools/device_ops.py:241`）：
```python
@tool
def launch_app(
    package: str,
    activity: str = "",
) -> str:
    """启动指定包名的 App。返回统一契约字符串：
       OK: ... || requested_package=...; requested_activity=...; observed_activity=...; arrival_confirmed=true
    """
    from tools.results import make_result
    from tools import _capture_page_id, _record_page_transition, _settle_after_action
    ctx = get_tool_context()
    if ctx.device is None:
        return make_result("ERROR", "未连接 Android 设备")

    _pre_page = _capture_page_id(ctx)
    target_activity = (activity or "").strip()
    try:
        if target_activity:
            ctx.device.app_start(package, activity=target_activity)
        else:
            ctx.device.app_start(package)
    except Exception as e:
        return make_result("ERROR", f"启动失败: {e}",
                           evidence={"requested_package": package, "requested_activity": target_activity})

    # v2.0 P0-3：通用 settle 等待（不针对 Settings）
    _settle_after_action(ctx, max_wait_ms=1500)

    # v2.0 P0-3：读取真实 observed_activity
    _post_page = _capture_page_id(ctx) or ""
    observed_activity = _post_page.split("|")[0] if "|" in _post_page else _post_page

    # v2.0 P0-3：arrival_confirmed 判定
    if target_activity:
        target_short = target_activity.split(".")[-1]
        arrival_confirmed = (target_short in _post_page) or (target_activity in _post_page)
    else:
        arrival_confirmed = bool(_post_page) and _post_page != "unknown"

    _record_page_transition(ctx, _pre_page, f"launch_app({package})")

    if arrival_confirmed:
        return make_result(
            "OK", f"已启动 {package}",
            evidence={
                "requested_package": package,
                "requested_activity": target_activity,
                "observed_activity": observed_activity,
                "arrival_confirmed": "true",
            }
        )
    else:
        # 启动未抛异常但实际未到目标 Activity：ERROR（不是 OK，team2 P0-3）
        return make_result(
            "ERROR", f"启动后未到达预期 Activity",
            evidence={
                "requested_package": package,
                "requested_activity": target_activity,
                "observed_activity": observed_activity,
                "arrival_confirmed": "false",
            }
        )
```

**`_settle_after_action` 通用函数**（`tools/__init__.py` 或新文件）—— 抽自现有循环检测 / 稳定等待工具，**不针对特定 App**，是通用运行基础设施：
```python
def _settle_after_action(ctx, max_wait_ms: int = 1500, poll_ms: int = 100) -> None:
    """通用动作后稳定等待：轮询 perceiver，直到 page_signature 连续 N 次未变。
    不针对特定 App；不修改 perceiver；不绕过设备交互。"""
    # 与 agents/loop_control._build_page_signature 协同
    import time as _time
    end = _time.monotonic() + max_wait_ms / 1000.0
    last = None
    stable_count = 0
    while _time.monotonic() < end and stable_count < 2:
        sig = _build_page_signature(ctx)
        if sig == last and sig != "unknown":
            stable_count += 1
        else:
            stable_count = 0
            last = sig
        _time.sleep(poll_ms / 1000.0)
```

### 3.4 Step 4 (P0)：lineage 字段拆分 + 删 `test_cases.plan_quality_json`

**目标**（R13/R14 + team2 P0-5）：lineage 类型化；`plan_quality_json` 删列（API 派生）。

**字段语义表**（team2 P0-5 采纳）：

| 字段 | 所属 | 语义 |
|---|---|---|
| `test_cases.source_run_id` | 用例 | 该用例最初提炼自哪条 run（v3.3 已有，保持） |
| `test_runs.source_run_id` | 运行 | 直接从报告列表点"复跑"时的来源 run id（v3.3 已有，保持） |
| `test_runs.source_case_id` | 运行 | **v2.0 新增**：从用例执行时的来源 case id |
| `test_runs.execution_plan_revision` | 运行 | **v2.0 新增**：本次执行使用的 evidence/plan 修订版本 |

**改动**（`data/relational.py` `_ensure_tables`）：
```sql
-- test_runs：lineage 拆分
CREATE TABLE test_runs (
    ... (现有字段) ...,
    goal_json         TEXT NOT NULL DEFAULT '{}',
    run_type          TEXT NOT NULL DEFAULT 'normal',
    source_run_id     TEXT,                   -- v3.3：仅报告复跑时填
    source_case_id    TEXT,                   -- v2.0 新增：仅 case 运行时填
    execution_plan_revision INTEGER DEFAULT 0  -- v2.0 新增
);
-- test_cases：删 plan_quality_json（R14，API 派生）
```

**`record_test_run` 同步扩参**（`data/relational.py:204`）：
```python
def record_test_run(
    self,
    run_id, user_request, app_package, app_name, status, conclusion, steps,
    duration_seconds=0, execution_status="", test_verdict="",
    verification_json="[]", llm_call_count=0, click_count=0, ...,
    # v3.3
    goal_json="{}", run_type="normal", source_run_id=None,
    # v2.0
    source_case_id=None, execution_plan_revision=0,
) -> None:
```

`reporter_node` 调 `record_test_run` 时同步透传两新字段（`source_case_id=state.get("_source_case_id")`、`execution_plan_revision=state.get("_execution_plan_revision", 0)`）。

### 3.5 Step 5 (P0)：`_extract_replay_evidence` 完整版

**目标**（R1/R3/R4.1/R4.2/R10/R12 + team2 P0-5/P1-2/P1-3/P1-4）：从成功 run 提炼 replay evidence。

**输入**：`run` dict（含 `steps_json` / `goal_json` / `verification_json` / `execution_status` / `test_verdict`）。

**输出**：完整 `execution_plan` dict（schema 见 §2.2），**可无 entry**（team2 P1-3）。

**算法**（v2.0 完整版）：
```
1. 门控：run.execution_status != 'completed' OR test_verdict != 'passed'  → 返回 None
2. 解析 steps_json + goal_json.verification + verification_json
3. 找 entry（可空，team2 P1-3）：
   - 找到第一个可验证业务动作片段（click/assert_*），向前找最近同 package 的 launch_app
   - 若有 launch_app，提取 launch_app_args.package / activity
   - 若无，entry=None（key_actions 仍可复用——起始已在目标页的 case 适用）
4. 提取 preconditions（team2 P0-4）：
   - expected_activity：entry 启动后 Activity 短名（entry 为空时用最后一步 agent 的 page_to Activity）
   - required_anchors：key_actions 中 click 的 resolved_target.{label, role, rid, class_name, path}
   - soft_page_signature：key_actions[0].page_before_signature
5. 找 key_actions：按时间顺序，仅当 status_code 在 {OK, PASS, passed} 且 observation 不含 NOT_FOUND/AMBIGUOUS/NEEDS_HUMAN/ERROR 才纳入
   - click：locator = tool_input 全量 6 字段（team1 #2）；resolved_target 从 entry.resolved_target 读（team2 P1-2）
   - assert_verification / assert_page_contains / assert_element_exists：纳入
   - launch / press_key / scroll / swipe / visual_check：跳过
6. 算 verification_evidence（team2 P1-4 修正）：
   - 从 run.verification_json 读每个 vN 的 {result, detail, review_required}
   - objective = 同一 verify_key 绑定的最近一条 assert_page_contains / assert_element_exists
7. 不写 plan_quality_json；UI 端 API 派生 quality 离散能力标签（Step 8）
```

**伪代码**（`api/test_cases_routes.py`）：
```python
import re, json
from datetime import datetime as _dt

# click 工具接受的 6 字段（team1 #2 修正：全量保留，无伪优先级）
_LOCATOR_FIELDS = ("label", "rid", "class_name", "path_contains", "index", "alternatives")
# 失败/无证据标记（team2 P0-2：未匹配契约默认 UNSPECIFIED，不是 OK）
_BAD_STATUS_CODES = ("NOT_FOUND", "AMBIGUOUS", "NEEDS_HUMAN", "ERROR", "UNSPECIFIED")
_BAD_OBSERVATION_TOKENS = ("NOT_FOUND", "AMBIGUOUS", "NEEDS_HUMAN", "ERROR:")


def _extract_replay_evidence(run: dict) -> dict | None:
    """从一条成功 run 提炼 replay evidence；返回 None 表示不应生成。"""
    if run.get("execution_status") != "completed" or run.get("test_verdict") != "passed":
        return None
    steps_raw = run.get("steps_json") or "[]"
    try:
        steps = json.loads(steps_raw) if isinstance(steps_raw, str) else steps_raw
    except (json.JSONDecodeError, TypeError):
        return None
    if not steps:
        return None
    goal = {}
    try:
        goal = json.loads(run.get("goal_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        pass
    verification = goal.get("verification") or []
    verification_json = []
    try:
        verification_json = json.loads(run.get("verification_json") or "[]")
    except (json.JSONDecodeError, TypeError):
        pass
    if isinstance(verification_json, dict):
        verification_json = list(verification_json.values())

    # ── 找第一个"可验证业务动作片段"前的最近 launch_app（team2 P1-3）──
    first_action_idx = None
    for i, st in enumerate(steps):
        if st.get("action_type") in ("click", "assert_verification", "assert_page_contains", "assert_element_exists"):
            first_action_idx = i
            break
    entry = None
    if first_action_idx is not None:
        for st in reversed(steps[:first_action_idx]):
            if st.get("action_type") == "launch_app":
                args = st.get("tool_input") or {}
                entry = {
                    "launch_app_args": {
                        "package": args.get("package") or run.get("app_package", ""),
                        "activity": (args.get("activity") or "").strip(),
                    },
                }
                break
    # team2 P1-3：entry 可空；key_actions 仍可复用

    # ── preconditions（team2 P0-4）──
    expected_activity = ""
    if entry and entry["launch_app_args"].get("activity"):
        expected_activity = entry["launch_app_args"]["activity"].split(".")[-1]
    else:
        # 起始已在目标页：取最后一步 agent 的 page_to
        for st in reversed(steps):
            if st.get("action_type") == "agent" and st.get("page_to"):
                expected_activity = st["page_to"].split("|")[0]
                break

    # ── key_actions ──
    key_actions = []
    verify_idx = 0
    for st in steps:
        at = st.get("action_type")
        status_code = st.get("status_code", "UNSPECIFIED")  # team2 P0-2
        observation = st.get("observation", "") or ""
        # 失败 / 模糊 / UNSPECIFIED 全部过滤
        if status_code in _BAD_STATUS_CODES:
            continue
        if any(bad in observation for bad in _BAD_OBSERVATION_TOKENS):
            continue

        if at == "click":
            ti = st.get("tool_input") or {}
            locator = {k: ti.get(k) for k in _LOCATOR_FIELDS if k in ti}  # team1 #2：全量
            if not locator.get("label"):
                continue
            verify_key = f"v{verify_idx}" if verify_idx < len(verification) else None
            # team2 P1-2：从 entry 顶层读 resolved_target（click 落库的结构化事实）
            resolved = st.get("resolved_target") or {}
            if isinstance(resolved, dict) and not resolved:
                # 兼容 evidence 段写法
                resolved = {
                    "label": ti.get("resolved_label", ""),
                    "role": ti.get("resolved_role", ""),
                    "rid": ti.get("resolved_rid", ""),
                    "class_name": ti.get("resolved_class", ""),
                    "path": ti.get("resolved_path", ""),
                }
            key_actions.append({
                "step": f"click_{str(locator.get('label','?')).lower().replace(' ','_')}",
                "tool": "click",
                "locator": locator,
                "resolved_target": {k: v for k, v in resolved.items() if v},
                "last_observation": observation[:200],
                "last_result": status_code or "OK",
                "verify": verify_key,
            })
        elif at == "assert_verification":
            if verify_idx < len(verification):
                key_actions.append({
                    "step": f"verify_v{verify_idx}",
                    "tool": "assert_verification",
                    "verify_key": f"v{verify_idx}",
                    "last_observation": observation[:200],
                    "last_result": status_code or "passed",
                })
                verify_idx += 1
        elif at == "assert_page_contains":
            ti = st.get("tool_input") or {}
            key_actions.append({
                "step": f"assert_pg_{verify_idx}",
                "tool": "assert_page_contains",
                "args": {"text": ti.get("text", "")},
                "last_observation": observation[:200],
                "last_result": status_code or "PASS",
                "verify": f"v{verify_idx}" if verify_idx < len(verification) else None,
            })
        elif at == "assert_element_exists":
            ti = st.get("tool_input") or {}
            key_actions.append({
                "step": f"assert_el_{verify_idx}",
                "tool": "assert_element_exists",
                "args": {"label": ti.get("label", "")},
                "last_observation": observation[:200],
                "last_result": status_code or "PASS",
                "verify": f"v{verify_idx}" if verify_idx < len(verification) else None,
            })

    if any(s.get("action_type") == "report_done" for s in steps):
        key_actions.append({"step": "done", "tool": "report_done"})

    if not key_actions:
        return None  # 关键：entry 可空，但 key_actions 仍必须有

    # ── preconditions 收尾 ──
    required_anchors = []
    for ka in key_actions:
        if ka.get("tool") == "click":
            rt = ka.get("resolved_target") or {}
            if rt.get("label") or rt.get("role") or rt.get("rid"):
                required_anchors.append({
                    "label": rt.get("label", ""),
                    "role": rt.get("role", ""),
                    "rid": rt.get("rid", ""),
                    "class_name": rt.get("class_name", ""),
                    "path": rt.get("path", ""),
                })

    soft_page_signature = key_actions[0].get("page_before_signature", "") if key_actions else ""

    # ── verification_evidence（team2 P1-4：从 verification_json 读真实结果）──
    verification_evidence = {}
    for i, item in enumerate(verification):
        vkey = f"v{i}"
        # 找 verification_json 里 key == vkey 或 item 匹配的记录
        subjective = {"result": "passed", "detail": "", "review_required": False}
        for ve in verification_json:
            if not isinstance(ve, dict):
                continue
            if ve.get("key") == vkey or (ve.get("item") and ve.get("item") == item):
                subjective = {
                    "result": ve.get("result", "passed"),
                    "detail": ve.get("detail", ""),
                    "review_required": bool(ve.get("review_required", False)),
                }
                break
        # objective：最近一条同 verify_key 的 assert_page_contains / assert_element_exists
        objective = []
        for ka in key_actions:
            if ka.get("verify") != vkey:
                continue
            if ka.get("tool") in ("assert_page_contains", "assert_element_exists"):
                objective.append({
                    "kind": ka["tool"],
                    "args": ka.get("args", {}),
                    "result": ka.get("last_result", ""),
                })
        verification_evidence[vkey] = {
            "item": item,
            "subjective": subjective,
            "objective": objective,
        }

    return {
        "schema_version": 2,
        "extracted_from_run_id": run.get("id"),
        "extracted_at": _dt.now().isoformat(),
        "extraction_revision": 1,
        "entry": entry,
        "preconditions": {
            "expected_activity": expected_activity,
            "required_anchors": required_anchors,
            "soft_page_signature": soft_page_signature,
        },
        "key_actions": key_actions,
        "verification_evidence": verification_evidence,
    }
```

**在 `create_test_case` 内**（**注意**：team2 P0-5，`create_test_case` 当前签名是 `create_test_case(name, user_request, app_package, app_name, goal_json, source_run_id)`，**没有 source_case_id 形参**——`source_case_id` 是 `test_runs` 字段，不在 `test_cases`）：
```python
if body.get("run_id"):
    run = _relational_db.get_test_run(body["run_id"])
    if not run:
        return {"status": "error", "message": "报告不存在"}
    try:
        goal = json.loads(run.get("goal_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        return {"status": "error", "message": "报告计划数据损坏"}
    evidence = _extract_replay_evidence(run)
    if evidence:
        goal["execution_plan"] = evidence
    case_id = _relational_db.create_test_case(
        name=body.get("name") or (run.get("user_request") or "未命名")[:40],
        source_run_id=run["id"],            # ← 这一字段在 test_cases（用例最初提炼自哪条 run）
        user_request=run.get("user_request", ""),
        app_package=run.get("app_package", ""),
        app_name=run.get("app_name", ""),
        goal_json=json.dumps(goal, ensure_ascii=False),
    )
    return {"status": "ok", "data": {"id": case_id, "has_replay_evidence": bool(evidence)}}
```

### 3.6 Step 6 (P0)：REPLAY_EVIDENCE_BLOCK 渲染（分层前置条件）

**目标**（R1/R2/R7 + team2 P0-4）：给 LLM 事实，让 LLM 决策；**前置条件分层**（强 / 强 / 软）。

**改动**（`agents/nodes.py`，找到 system prompt 拼装处）：

```python
def _render_replay_evidence_block(goal_desc: dict) -> str:
    """渲染 REPLAY_EVIDENCE_BLOCK（事实参考，不是强制脚本，team2 P0-4 分层前置）。"""
    if not isinstance(goal_desc, dict):
        return ""
    exec_plan = goal_desc.get("execution_plan")
    if not exec_plan:
        return ""
    lines = [
        "",
        "## REPLAY_EVIDENCE_BLOCK（来自历史成功运行的事实，**不是强制脚本**）",
        "下列为最近一次成功 run 的执行痕迹，供你判断当前页面是否满足前置条件、是否复用历史 locator、如何回退。",
        "**你不是必须按以下步骤执行**：",
        "- 如果 expected_activity 不匹配或 required_anchors 缺失，先 get_screen_info 重新感知；",
        "- 如果 locator 在当前页 NOT_FOUND / AMBIGUOUS，可回退到 label-only 语义点击、scroll_find_and_click；",
        "- soft_page_signature 是软提示，不匹配 ≠ 证据失效，仅提示重新感知；",
        "- 任何新感知都应优先使用 updated 当前页面事实，不要盲用历史 index。",
        "",
    ]

    # ── 前置条件（分层，team2 P0-4）──
    pc = exec_plan.get("preconditions") or {}
    entry = exec_plan.get("entry")
    lines.append("### 入口与前置条件")
    if entry and entry.get("launch_app_args"):
        args = entry["launch_app_args"]
        lines.append(f"- 入口: launch_app(package=\"{args.get('package','')}\", activity=\"{args.get('activity','')}\")")
    else:
        lines.append("- 入口: 无（起始已在目标页）")
    if pc.get("expected_activity"):
        lines.append(f"- 强前置 expected_activity: {pc['expected_activity']}")
    if pc.get("required_anchors"):
        for i, ra in enumerate(pc["required_anchors"]):
            lines.append(f"- 强前置 anchor[{i}]: label={ra.get('label','')!r} role={ra.get('role','')!r} rid={ra.get('rid','')!r}")
    if pc.get("soft_page_signature"):
        lines.append(f"- 软提示 page_signature: {pc['soft_page_signature']}（不匹配仅提示重新感知）")
    lines.append("")

    # ── 历史成功步骤（参考，team1 #2：locator 全量保留，click 自处理歧义）──
    lines.append("### 历史成功步骤（参考）")
    for i, ka in enumerate(exec_plan.get("key_actions") or []):
        tool = ka.get("tool")
        if tool == "click":
            loc = ka.get("locator") or {}
            loc_str = ", ".join(f"{k}={v!r}" for k, v in loc.items() if v not in ("", -1, None))
            rt = ka.get("resolved_target") or {}
            et_str = ""
            if rt.get("role") or rt.get("rid") or rt.get("path"):
                et = ", ".join(f"{k}={v!r}" for k, v in rt.items() if v)
                et_str = f"  // resolved: {et}"
            lines.append(f"{i+1}. click({loc_str}){et_str}")
            if ka.get("last_observation"):
                lines.append(f"     上次结果: {ka['last_observation']} [{ka.get('last_result','')}]")
        elif tool in ("assert_verification", "assert_page_contains", "assert_element_exists"):
            lines.append(f"{i+1}. {tool}({ka.get('args') or ka.get('verify_key','')})")
            if ka.get("last_observation"):
                lines.append(f"     上次结果: {ka['last_observation']} [{ka.get('last_result','')}]")
        else:
            lines.append(f"{i+1}. {tool}")

    # ── 验证证据（team2 P1-4：subjective 是 agent 报告，objective 是 PASS=权威）──
    ve = exec_plan.get("verification_evidence") or {}
    if ve:
        lines.append("")
        lines.append("### 验证证据（objective PASS = 权威 ground truth）")
        for vkey, info in ve.items():
            lines.append(f"- {vkey}: {info.get('item','')}")
            subj = info.get("subjective") or {}
            lines.append(f"    - agent 报告: {subj.get('result','?')} {('(review_required)' if subj.get('review_required') else '')}")
            for obj in info.get("objective", []):
                lines.append(f"    - 客观: {obj['kind']}({obj['args']}) → {obj['result']}")

    return "\n".join(lines)
```

**关键变更（相对 v1.1）**：
- 前置条件拆 `expected_activity`（强）+ `required_anchors`（强）+ `soft_page_signature`（软）
- locator 渲染**全量**（team1 #2）
- resolved_target 单独列（team2 P1-2）
- subjective 标注"agent 报告"、objective 标注"权威"（team2 P1-4）

**在 system prompt 拼装处插入**（**注**：当前是 `SystemMessage(AGENT_SYSTEM)` 拼装，不是函数）：
- 在 `agent_node` 第一条 `SystemMessage` 之后插入新 `SystemMessage(_render_replay_evidence_block(state.get("goal_description")))`

### 3.7 Step 7 (P0)：lineage 全链路传递

**目标**（team2 P0-5）：完整覆盖 WS rerun/run_case → orchestrator.start → state → reporter_node → record_test_run。

**改动清单**：

| 文件 | 行号（实测） | 改动 |
|---|---|---|
| `agents/state.py` | `TestState` 声明（v3.3 total=False） | 加 `_source_case_id: str` + `_execution_plan_revision: int` 字段 |
| `agents/orchestrator.py` | `start` (L187-) 和 `start_stream` (L386-) | 加 `source_case_id=None, execution_plan_revision=0` 形参；透传进 `initial_state["_source_case_id"]` / `["_execution_plan_revision"]` |
| `api/server.py` | WS `rerun` 分支 (L702-715) | `source_run_id=run_id if run else None`（不变），**加** `source_case_id=None`（report 不来自 case） |
| `api/server.py` | WS `run_case` 分支 (L723-744) | **改**：`source_run_id=None`，`source_case_id=case_id`（**拆分**） |
| `api/test_cases_routes.py` | `run_test_case` (L108-139) | **改**：`source_run_id=None`，`source_case_id=case_id` |
| `agents/nodes.py` | `reporter_node` 调 `record_test_run` (L855-867) | 加 `source_case_id=state.get("_source_case_id")` 和 `execution_plan_revision=state.get("_execution_plan_revision", 0)` 透传 |
| `data/relational.py` | `record_test_run` (L204-) | 加两形参（见 §3.4 改动） |
| `data/relational.py` | `list_test_runs` / `get_test_run` | SELECT 增两列 + COALESCE 兜底 |

**关键**：**`source_case_id` 不在 `test_cases` 表**——`test_cases.source_run_id` 才是用例的"原始来源 run"（保留 v3.3 不变）。

### 3.8 Step 8 (P1)：UI 编辑 + 离散能力 quality + evidence_stale

**改动**（`frontend/spa/src/components/TestCasePanel.vue`）：

`form` 扩展（与 §2.2 对齐）：
```js
form.value.execution_plan = {
  schema_version: 2,
  extracted_from_run_id: '',
  extracted_at: '',
  extraction_revision: 1,
  entry: null,
  preconditions: { expected_activity: '', required_anchors: [], soft_page_signature: '' },
  key_actions: [],
  verification_evidence: {},
}
```

新增/编辑弹窗底部追加 `<div class="tcp-exec-plan">` 块：
- 入口：可空
- preconditions：3 个分组 input
- key_actions：表格 + 增删改
- verification_evidence：折叠只读

`saveCase` 内 goalJson 包含 `execution_plan`；**调用后端 schema 校验**。

**后端 schema 校验**（team2 P1-5）：
- `api/test_cases_routes.py` `update_test_case` 加 `validate_execution_plan(plan)`：
  - 校验 `schema_version == 2`、tool 白名单、locator 字段类型与长度
- 原始 evidence 不可变；用户修改 → API 接收 `override: { execution_plan: ... }` + `auto_increment_revision: true`
- `execution_plan.extraction_revision += 1`
- **修改过的 key_action 标记 `evidence_stale=true`**（手动覆盖，无历史 PASS 证据）
- 只有新成功 run 才能为修改后的事实重新写入 PASS 证据

**API 派生 quality 离散能力**（team2 P1-6 + R14 修）：
```python
def _derive_plan_capabilities(case) -> dict:
    """离散能力标签，不计字段数量。"""
    goal = json.loads(case.get("goal_json") or "{}")
    ep = goal.get("execution_plan") or {}
    kas = ep.get("key_actions") or []
    entry = ep.get("entry") or {}
    pc = ep.get("preconditions") or {}

    has_verified_entry = bool(entry and entry.get("launch_app_args", {}).get("activity"))
    # stable locator: 有 rid 或有 class+path+label 组合
    has_stable_locator = False
    index_only_locator = False
    for ka in kas:
        if ka.get("tool") != "click":
            continue
        loc = ka.get("locator") or {}
        if loc.get("rid"):
            has_stable_locator = True
            break
        if loc.get("class_name") and loc.get("path_contains") and loc.get("label"):
            has_stable_locator = True
            break
        if loc.get("index") not in ("", -1, None) and not loc.get("label"):
            index_only_locator = True
    has_objective_evidence = any(
        info.get("objective") for info in (ep.get("verification_evidence") or {}).values()
    )
    environment_compatible = bool(pc.get("expected_activity"))
    # evidence_stale：人工修改过 或 step 含 override
    evidence_stale = any(ka.get("evidence_stale") for ka in kas)

    return {
        "has_replay_evidence": bool(ep),
        "key_actions_count": len(kas),
        "has_verified_entry": has_verified_entry,
        "has_stable_locator": has_stable_locator,
        "index_only_locator": index_only_locator,
        "has_objective_evidence": has_objective_evidence,
        "environment_compatible": environment_compatible,
        "evidence_stale": evidence_stale,
        "extraction_revision": ep.get("extraction_revision", 0),
    }
```

**前端列表行**用 `case.plan_capabilities` 显示 6 个小图标：
- `entry ✓/✗` `loc stable/index-only` `obj ✓/✗` `env ✓/✗` `stale ✓/✗`

### 3.9 Step 9 (P2)：受控 A/B 测试

**目标**（R5 + team2 观测 2/3/4/5）：统一条件后量化 v2.0 收益，**不预设保证**。

**测试矩阵**（同一 App、同一设备、统一环境）：
| Case | 模式 | 期望 |
|---|---|---|
| A | 普通 run | baseline（参考 103153） |
| B | 旧复跑（v3.3，replay evidence 未注入） | 参考 104626 |
| C | v2.0 复跑（replay evidence 注入） | **假设/验收参考值**（不保证）：≥ 50% 未缓存 token 节省、0 NOT_FOUND、0 semantic click |

**环境记录项**（team2 观测 4）：
- 设备 ROM / 分辨率 / DPI / 语言 / 方向
- App version code
- 网络条件（Wi-Fi/数据/无网）
- 起始 Activity / 起始 WLAN 状态
- **RAG 初始状态**（明确标注"每次 run 前 `_reset_run_scoped` 已清空 cache"，避免与 v1.1 同样归因错）

**样本与统计**（team2 观测 3）：
- 探索性：单 case 3 遍取中位数
- 下"稳定收益"结论：**增加样本（≥ 10 遍）+ 记录失败率 + 置信区间**
- 至少 3 个不同类型 case（单步 / 多步导航 / 弹窗交互 / 表单填写）

**写报告**：`docs/replay_evidence_ab_report_2026072X.md`（保留 §5 风险评估的"双盲 / 盲评"标注）。

---

## 4. 改动文件清单（v2.0，team2 P0-5 完整传递链）

| 文件 | 改动类型 | 关键改动 | 关联 review |
|---|---|---|---|
| `agents/llm_runtime.py` (L437-454) | MODIFY | 所有工具统一记录 `tool_input`/`status_code`/page signatures；click 额外 `resolved_target` | R6 / T2-1 / T1-1 |
| `agents/orchestrator.py` (`_build_display_steps` L892) | MODIFY | 扩字段：tool_input/status_code/page_*/match_mode/fallback_used | R6 / T1-1 |
| `tools/click.py` (L413) | MODIFY | `make_result(..., evidence={resolved_*})` 落库 click 解析事实 | T2-7 |
| `tools/device_ops.py` (`launch_app` L241) | MODIFY | 复用 `make_result` 契约 + 通用 settle 等待 + arrival_confirmed | T2-2 / T2-3 |
| `tools/__init__.py`（或新文件） | MODIFY | 新增 `_settle_after_action` 通用工具 | T2-3 |
| `agents/loop_control.py` (L14) | 无改 | `_build_page_signature` 不动（仅作"软提示"使用） | T2-4 |
| `data/relational.py` (`_ensure_tables` L62, L137) | MODIFY | `test_runs` 加 `source_case_id`/`execution_plan_revision`；`test_cases` 删 `plan_quality_json` 列 | R13 / R14 / T2-5 |
| `data/relational.py` (`record_test_run` L204) | MODIFY | 加 `source_case_id`/`execution_plan_revision` 形参 | T2-5 |
| `data/relational.py` (`create_test_case` L605) | 无改 | 签名保持（`source_case_id` 不在 `test_cases`） | T2-5 |
| `data/relational.py` (`list/get_test_run`) | MODIFY | SELECT 增两列 + COALESCE 兜底 | T2-5 |
| `api/test_cases_routes.py` (`create_test_case` L40) | MODIFY | 调用 `_extract_replay_evidence`；`update_test_case` 加 schema 校验 + override revision | R1-R12 / T2-8 / T2-9 / T2-10 |
| `api/test_cases_routes.py` (`run_test_case` L108) | MODIFY | `source_run_id=None`, `source_case_id=case_id` | T2-5 |
| `api/test_cases_routes.py`（新增 `_derive_plan_capabilities`） | MODIFY | API 派生 quality 离散能力 | R14 / T2-11 |
| `api/server.py` (WS `rerun` L702) | MODIFY | `source_case_id=None`（report 不来自 case） | T2-5 |
| `api/server.py` (WS `run_case` L723) | MODIFY | `source_run_id=None`, `source_case_id=case_id` | T2-5 |
| `agents/orchestrator.py` (`start` L187, `start_stream` L386) | MODIFY | 加 `source_case_id`/`execution_plan_revision` 形参 + initial_state 透传 | T2-5 |
| `agents/state.py` (`TestState` total=False) | MODIFY | 加 `_source_case_id: str` + `_execution_plan_revision: int` | T2-5 |
| `agents/nodes.py` (`reporter_node` L855) | MODIFY | 调 `record_test_run` 时透传两新字段 | T2-5 |
| `agents/nodes.py` (system prompt 拼装处) | MODIFY | 插入 `REPLAY_EVIDENCE_BLOCK`（分层前置条件） | R1/R2/R7 / T2-4 |
| `frontend/spa/src/components/TestCasePanel.vue` | MODIFY | form + 编辑 UI + 离散能力 tag + evidence_stale 显示 | R14 / T2-10 / T2-11 |
| `frontend/spa/src/App.vue` (`saveAsCase` L639) | 无改 | 兼容 v3.3 body | — |
| `tests/test_extract_replay_evidence.py` | NEW | 7 个 `_extract_replay_evidence` 单测 | R1-R12 / T2-8/9 |
| `tests/test_replay_evidence_prompt.py` | NEW | 4 个 REPLAY_EVIDENCE_BLOCK 渲染单测 | R1/R2/R7 / T2-4 |
| `tests/test_launch_app_contract.py` | NEW | 3 个 launch_app 契约返回 + settle 单测 | T2-2 / T2-3 |
| `tests/test_structured_step_logging.py` | NEW | 5 个 `_build_display_steps` + `_run_agent` 扩字段单测 | R6 / T2-1 / T1-1 |
| `tests/test_lineage_full_chain.py` | NEW | 4 个 lineage 全链路单测（WS rerun/run_case/orchestrator/reporter） | T2-5 |
| `tests/test_plan_capabilities.py` | NEW | 5 个 `_derive_plan_capabilities` 离散能力单测 | T2-11 |
| `docs/用例管理_复用计划重跑_20260721.md` | MODIFY | §3.4 / §3.5 加 v2.0 子节 | — |
| `docs/replay_evidence_ab_report_2026072X.md` | NEW | Step 9 A/B 测试报告 | R5 / T2-13/14/15 |

---

## 5. 风险与回滚

| 风险 | 缓解 |
|---|---|
| 所有工具都落库 `tool_input` 导致 steps_json 体积膨胀 | 实测 click 的 6 字段 ~100B/步；15 步 ~1.5KB/run，可接受 |
| launch_app 改 `make_result` 契约破坏现有调用方 | tools/results.parse_status() 对旧格式宽容（返回空）；现有解析路径不变 |
| settle 等待 1.5s 在某些 App 上不够 | `_settle_after_action` 暴露 `max_wait_ms` 参数；通用、不针对 Settings |
| page_signature 跨 run 不稳定导致 preconditions 失效 | 拆为强（activity/anchors）+ 软（signature），不匹配仅提示重感知 |
| lineage 拆分 7 处改动一处遗漏导致数据不一致 | Step 7 全链路单测覆盖；`source_run_id` 仍存但语义收窄 |
| `execution_plan` 人工修改导致 evidence_stale 误标 | 后端 schema 校验 + override revision 强约束；原始 evidence 不可变 |
| quality 离散能力标签语义用户不熟 | UI 显示 6 个小图标 + tooltip 说明每个标签含义 |
| A/B 测试耗时（每个 case 3 模式 × 探索性 3 遍 = 9 跑） | 单 case < 3 分钟，3 case < 10 分钟，可接受 |

**回滚**：
- 数据库：dev 库清空重建（v3.3 §0.8 口径），老数据不可用但开发期可接受
- 代码：每个 Step 独立 commit，按 Step 回滚
- Prompt：`_render_replay_evidence_block` 是新增函数，删一行 import 即可
- launch_app 改 make_result 契约：原文本前缀也保留（`OK: 已启动 {pkg}`），parse 失败 fallback

---

## 6. 落地顺序（review#2 §13 + team2 P0 顺序）

| Step | 改动 | 验证 | 阻断后续 Step |
|---|---|---|---|
| **1** | `llm_runtime.py` 扩字段 + `_build_display_steps` + `_extract_status_code` | 5 个单测通过；dev 库跑一次 run 后 inspect steps_json 含 `tool_input`/`status_code` | 是 |
| **2** | `tools/click.py` 落库 `resolved_target` | 2 个单测通过 | 是（Step 5 依赖） |
| **3** | `launch_app` 改 `make_result` 契约 + 通用 settle | 3 个单测通过；现有调用方无 break | 是 |
| **4** | `data/relational.py` lineage 拆分 + 删 `plan_quality_json` | list/get 返回新字段；dev 库清空重建 | 是 |
| **5** | `_extract_replay_evidence` 完整版 + `create_test_case` 调用 | 7 个单测；用 103153 样例 API 验证含 `preconditions.expected_activity` | 是 |
| **6** | REPLAY_EVIDENCE_BLOCK 渲染（分层前置条件） | 4 个单测；T2-4 措辞校验 | 是（必须） |
| **7** | lineage 全链路传递（7 个改动点） | 4 个单测；WS rerun/run_case 端到端 | 是 |
| **8** | UI 编辑 + 离散能力 + evidence_stale | 手动 UI + 5 个单测 | 否（可后置） |
| **9** | 受控 A/B 测试 | `replay_evidence_ab_report_2026072X.md` | 是（上线路径必须） |

---

## 7. 待确认决策点

1. **`source_case_id` 字段**进 `test_runs`，保留 `test_cases.source_run_id` 语义收窄 —— 方案选**采纳**（review#2 R13 + team2 P0-5）。**默认同意**。
2. **`test_cases.plan_quality_json` 删列**，改 API 派生离散能力 —— 方案选**采纳**（review#2 R14 + team2 P1-6）。**默认同意**。
3. **REPLAY_EVIDENCE_BLOCK 措辞**：明确"不是强制脚本"+ 删"禁止 LLM 加步骤" —— 方案选**采纳**（review#1 R2 + review#2 R7 + §0 红线）。**默认同意**。
4. **launch_app 复用 `make_result` 契约**，不发明 JSON 协议 —— 方案选**采纳**（team2 P0-2）。**默认同意**。
5. **`_extract_status_code` 未匹配默认 `UNSPECIFIED`**（不是 `OK`）—— 方案选**采纳**（team2 P0-2）。**默认同意**。
6. **`preconditions` 三层**：expected_activity（强）+ required_anchors（强）+ soft_page_signature（软）—— 方案选**采纳**（team2 P0-4）。**默认同意**。
7. **`_extract_replay_evidence` 在 entry 找不到时仍返回 evidence**（key_actions 仍可复用）—— 方案选**采纳**（team2 P1-3）。**默认同意**。
8. **`verification_evidence.subjective` 从 `run.verification_json` 读真实结果** —— 方案选**采纳**（team2 P1-4）。**默认同意**。
9. **UI 修改 = `override` + `extraction_revision` 自增 + `evidence_stale=true`** —— 方案选**采纳**（team2 P1-5）。**默认同意**。
10. **quality 改离散能力标签**（6 个）—— 方案选**采纳**（team2 P1-6）。**默认同意**。
11. **locator 全量 6 字段保留**，click 工具自处理歧义 —— 方案选**采纳**（team1 #2）。**默认同意**。
12. **R4.4 NOT_FOUND→RAG 提示** —— 方案选**不采纳**（§0 红线：禁止针对特例加 patch）。**默认不采纳**。
13. **R15 token 通用基础设施优化 / R16 click 质量 6 维指标** —— 方案选**列为独立 P1/P2 跟进项**，不混入本 PR。**默认同意**。
14. **Step 9 A/B 测试 case 选择** —— 至少 3 个不同类型；具体 case 列表待定。**默认同意**。
15. **回填存量 v3.3 用例（无 execution_plan）？** —— 方案选**不回填**。**默认此方案**。
16. **§0.3 105055 高效归因** —— 改"RAG=0 不代表 cache 跨 run 复用；105055 实际无法分离归因"。**默认同意**（team2 P1-1 采纳）。

---

## 8. 关联证据索引

| 证据 | 位置 |
|---|---|
| 3 条 run 的 trace.json | `logs/runs/{103516,104833,105155}_test-20260722_*.json` |
| 3 条 run 的 langchain.log | `logs/runs/{103153,104626,105055}_test-20260722_*_langchain.log` |
| v2.0 §3.1 改动的真实行号 | `agents/llm_runtime.py:437-454` `_tools_node` entry 构建（实测，T1-1 修正） |
| 提炼证据缺位 | `agents/orchestrator.py:892` `_build_display_steps`（实测） |
| 工具只 click 写 tool_input | `agents/llm_runtime.py:449-453` `if name == "click"`（实测，T2-1 确认） |
| `_reset_run_scoped` 清 RAG cache | `agents/orchestrator.py:35-37`（实测，T2-1 修正 v1.1 归因错） |
| `tools/results.py` 统一契约 | `tools/results.py:1-106` 完整文件（实测，T2-2 采纳） |
| `click` 工具签名（6 字段，无 role） | `tools/click.py:413` `click(label, alternatives, rid, class_name, path_contains, index)` |
| `launch_app` 工具签名 | `tools/device_ops.py:241` `launch_app(package, activity="")` |
| `_build_page_signature` 含 labels hash | `agents/loop_control.py:14-30`（实测，T2-4 确认） |
| `record_test_run` 当前签名 | `data/relational.py:204-238`（实测） |
| `create_test_case` 当前签名（无 source_case_id） | `data/relational.py:605-613`（实测，T2-5 确认） |
| WS rerun 当前实现 | `api/server.py:702-715`（实测） |
| WS run_case 当前实现 | `api/server.py:723-744`（实测，`source_run_id=case_id` 需拆） |
| HTTP run_test_case 当前实现 | `api/test_cases_routes.py:108-139`（实测） |
| `_tools_node` click entry 构建位置 | `agents/llm_runtime.py:450-453`（实测，T1-1 修正行号） |
| N1/N2 click 定位（index 是 page_info 全局 [n]） | `docs/综合评审与问题分类_20260711.md` §13 N1/N2 段 |
| M4 确定性断言（PASS=权威） | `docs/综合评审与问题分类_20260711.md` §13.① |
| v3.3 R1 ctx 重置 | `agents/orchestrator.py:20-48` `_reset_run_scoped` |
| 指导原则 §0 | `docs/综合评审与问题分类_20260711.md` §0 |
| v3.3 dev-reset 口径 | `docs/用例管理_复用计划重跑_20260721.md` §0.8 / §3.1 末段 |
| review#1 | `docs/用例计划质量提升_execution_plan_review_20260722.md` |
| review#2 | `docs/用例计划质量提升_review_20260722.md` |
| team1 review | `docs/用例计划质量提升_review_team1_20260722.md` |
| team2 review | `docs/用例计划质量提升_review_team2_20260722.md` |

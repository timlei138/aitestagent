# 用例计划质量提升（Replay Evidence 注入）— 改进方案 v4.1

> 文档版本：2026-07-22 v4.1

## v4.1 实施前闭环补充（本节优先级高于后文 v4.0 原文）

> **状态：方案稿 v4.1，待 Review。**
>
> v4.1 在 v4.0 的基础上补齐 API 写入边界、patch 语义、运行准入、revision 单调性、direct rerun lineage 与测试闭环。环境快照功能现已退役，不属于当前运行、持久化或 Replay Evidence 契约。后文标注为 v4.0 的历史变更说明保留用于追溯；如与本节冲突，以本节为准。

### A. 不变量与职责边界

- **代码负责**：输入白名单、schema/版本校验、patch 解析、服务端派生 `effective`、revision 分配、lineage、运行准入、持久化和确定性断言。
- **LLM 负责**：基于当前感知与 Replay Evidence 判断是否复用、选择路径、处理页面漂移/歧义以及恢复策略。`effective` 仍是参考证据，不是强制脚本。
- `base_evidence` 仅可由成功 run 的服务端提炼或受控的重新提炼操作产生；客户端不得直接覆盖。
- v4 的 `effective` 只能由服务端从 `deepcopy(base_evidence) + override.patch` 派生，客户端不得提交或修改。
- 环境快照功能已退役：不采集或传播运行状态，不写入新建 `test_runs`，不提炼为 `environment_fingerprint`，也不出现在能力契约中。

### B. P0：v4 用例编辑 API 是唯一写入口

**端点约定**：新增/使用 `PATCH /api/test_cases/{case_id}` 作为 v4 Replay Evidence 的专用编辑入口。现有 `PUT /api/test_cases/{case_id}` 不能继续接受任意 `goal_json` 来更新 v4 用例；它只能用于不带 v4 `execution_plan` 的 legacy 用例，或被拆为独立的 metadata 编辑接口。

v4 PATCH 只接受以下 DTO；未知字段一律拒绝：

```jsonc
{
  "expected_effective_revision": 7,
  "override_patch": [/* 完整的目标 patch 集，路径相对 base_evidence */],
  "changed_paths": ["key_actions[0].preferred_locator.path_contains"],
  "edited_by": "current-user"
}
```

具体约束：

1. 不接受 `goal_json`、`execution_plan`、`base_evidence`、`effective`，无论字段位于顶层还是嵌套对象；禁止通过完整对象替换绕过不可变证据约束。
2. 名称、请求、App 包名等 metadata 如需编辑，使用显式白名单字段或独立 metadata endpoint；不得与 evidence patch 混用。
3. 路由层必须调用 DB 层唯一的事务化方法，例如 `update_case_override_if_revision(case_id, expected_revision, patch, changed_paths, edited_by)`；不得继续通过通用 `update_test_case(goal_json=...)` 形成旁路。
4. 当前伪代码中的 `_relational_db.save_test_case(...)` 不是已有接口。实施时必须新增上述受控写方法，或以等价事务方法替换；不能引用不存在的方法。

### C. P0：patch 路径、patch 语义与 base 不可变

`override_patch` 的 path **相对于 `base_evidence` 根**，例如：

```json
{ "op": "replace", "path": "/key_actions/0/preferred_locator/path_contains", "value": "new_path" }
```

允许根节点为 `entry`、`pre_entry`、`key_actions`、`verification_evidence`。已退役的 `environment_fingerprint` 不属于当前 patch 契约；新 PATCH 请求命中该根必须拒绝。实现不得用带尾部 `/` 的字符串再拼接 `/` 判断前缀；应解析 JSON Pointer 后检查首段：

```python
_ALLOWED_PATCH_ROOTS = {
    "entry", "pre_entry", "key_actions", "verification_evidence",
}
parts = decode_json_pointer(path)
if not parts or parts[0] not in _ALLOWED_PATCH_ROOTS:
    raise ValueError("patch path 不在允许范围内")
```

- 实现的是 RFC 6902 **子集**：只支持 `add`、`replace`、`remove`；必须定义并测试 JSON Pointer 转义、数组下标、`-` 追加、目标路径不存在及类型不匹配的错误行为。
- `override_patch` 采用**完整目标 patch 集**语义，而不是“本次增量”。服务端以 `base_evidence` dry-run 完整 patch 后，才替换 `override.patch`；`expected_effective_revision` 用于防止并发编辑丢失。
- 服务端派生必须先 `deepcopy(base_evidence)`，然后 apply patch；任何失败都不能修改持久化的 `base_evidence`、`override` 或 `effective`。

### D. P0：v4 运行准入与统一的权威来源

v4 数据损坏不能退化成“无 Replay Evidence 的正常复跑”。HTTP `run_test_case`、WS `run_case` 和报告直接 `rerun` 都必须在调用 orchestrator 前完成同一套校验：

```python
validate_v4_execution_plan(goal):
    # schema_version == 4 时：
    # base_evidence / override / effective 均为合法 dict；
    # effective.schema_version == 4；
    # effective.effective_revision 为正整数；
    # effective 可被 schema 校验并包含服务端派生所需字段。
```

- 校验失败、JSON 损坏或 v4 缺少 `effective` 时，入口返回友好错误，**不得启动 orchestrator**。
- `_render_replay_evidence_block()` 保留“不渲染损坏 v4 evidence”的防御逻辑，但它不是运行准入的替代品。
- `_resolve_run_entry()` 必须返回“已校验的 entry 或错误”，不得以 revision `0` 静默继续。

**报告直接复跑（WS `rerun`）**同样以服务端为权威：客户端只传 `run_id`；服务端加载 source run、解析其 `goal_json` 并校验。不存在的 run、损坏计划或不完整 v4 plan 必须拒绝。客户端附带的 `goal` 不能作为 canonical replay evidence。

### E. P0：typed lineage 与 revision 传递

所有运行入口在持久化时必须使用下列互斥且可追溯的字段：

| 入口 | `source_run_id` | `source_case_id` | `execution_plan_revision` |
|---|---|---|---|
| 普通运行 | `NULL` | `NULL` | `0` |
| 报告直接复跑 | 原报告 run ID | `NULL` | 该报告 v4 `effective_revision`；legacy 为 `0` |
| 用例复跑 | 用例的原始 `source_run_id`（可空） | 当前 case ID | case v4 `effective_revision`；legacy 为 `0` |

- `test_runs` 增加 `source_case_id`、`execution_plan_revision`；开发数据库直接重建，不新增 migration/`ALTER TABLE` 代码。
- `TestOrchestrator.start/start_stream` 增加 `source_case_id`、`execution_plan_revision` 并写入 state。
- `TestState` 必须显式声明 `_source_case_id: str`、`_execution_plan_revision: int`。
- HTTP case run、WS case run、WS report rerun 必须共用解析/校验 helper，避免入口间 revision 或 source 字段再次漂移。

### F. P1：revision 的服务端单调分配与重新提炼规则

revision 不得以 `base_revision + override.revision` 公式推算。服务端在同一事务中读取当前 case 并分配：

```text
首次提炼：base_revision = 1，effective_revision = 1
人工编辑：base_revision 不变，effective_revision = old_effective_revision + 1
重新提炼：base_revision = old_base_revision + 1，
          effective_revision = old_effective_revision + 1
```

- PATCH 必须携带 `expected_effective_revision`；若与持久化值不一致，返回冲突并要求前端刷新，不能覆盖他人修改。
- 重新提炼是单独的服务端受控操作，不允许客户端提交新的 `base_evidence`。
- 重新提炼时，旧 `override.patch` 仅在能完整应用到新 base 且通过 schema 校验时保留；否则操作失败并保留旧 plan，要求人工显式处理，绝不静默丢弃人工修改。

### G. P1：环境快照已彻底移除

环境快照功能及其旧数据兼容路径均已删除。当前实现不采集、传播、持久化或提炼环境状态，也不在计划能力契约中暴露相关字段。

开发数据库按当前 DDL 直接重建；不迁移、回填或保留历史环境快照数据。

### H. v4.1 必测项（补充 §4 测试计划）

除 v4.0 已列测试外，必须新增或并入以下覆盖：

1. 成功报告保存为 v4 case 后，第一次读取/运行前已持久化非空 `effective`。
2. 合法 `/key_actions/...`、`/entry/...` patch 成功；非法 root、双斜杠、越权 path、非法 JSON Pointer 和类型不匹配全部拒绝。
3. `goal_json.execution_plan.base_evidence` 等嵌套写入、完整 `goal_json` 覆盖不能绕过 patch-only 边界。
4. HTTP `run_test_case` 与 WS `run_case` 都拒绝缺少或损坏 `effective` 的 v4 case，且不会调用 orchestrator。
5. WS `rerun` 拒绝不存在/损坏 source run，并从服务端 source run 而非客户端 goal 取得 canonical plan。
6. 正常报告复跑和用例复跑分别持久化正确的 `source_run_id`、`source_case_id`、`execution_plan_revision`。
7. 两个相同 `expected_effective_revision` 的编辑请求只有一个成功；重新提炼后 base/effective revision 均严格递增。
8. 新建开发数据库的 `test_runs` 不包含环境快照列，且当前源码没有环境快照采集、持久化、提炼或兼容逻辑。

---

> 以下为 v4.0 历史变更与原始设计；与上述 v4.1 实施前闭环补充冲突时，以 v4.1 为准。

> 文档版本：2026-07-22 v4.0
> v4.0 相对 v3.0 的变更：吸收 v3 review（4 P0 + 5 P1）= **9 条 review 全部采纳** + 8 项新单测补充（V3-T1 ~ V3-T8）。
> 核心重写点：
> 1. **page_sig_before 显式赋值**（闭合 V3-P0-1，`page_sig_before = page_sig_once` 必须在工具执行前赋值）
> 2. **precondition 从 page_before 推断，postcondition 从 page_after 推断**（闭合 V3-P0-2，方向正确）
> 3. **activity 作独立结构化字段记录**（不再 `split("|")` 反向解析；闭合 V3-P0-2 衍生）
> 4. **`_extract_replay_evidence` 兼容 `run.get("steps")`**（get_test_run 实际返回的 shape；闭合 V3-P0-3）
> 5. **click() 所有成功路径统一走 `_make_click_success` 构造器**（闭合 V3-P0-4，避免多分支返回 tuple/纯字符串）
> 6. **pre_entry 语义修正**：从 launch 的 page_before 提炼（前置页面），不再从 launch 的 activity 参数取（那是后置）
> 7. **arrival_confirmed 加 observed_package 校验**（package_matched AND activity_matched，闭合 V3-P1-2）
> 8. **`test_runs` 加 `environment_json` 列**（dev-reset 口径，闭合 V3-P1-3；提炼只读 source run 快照）
> 9. **execution_plan 拆 `base_evidence`（不可变）+ `override`（patch）+ `effective`（派生）**（闭合 V3-P1-4，事实真不可变）
> 10. **assert_verification 状态层次明确**：`status_code=OK`（工具成功）+ `result_evidence.reported_result`（agent 主观）+ `objective`（PASS/FAIL 客观）
> 11. §0.7 增 V3 review 处置表；§7 决策点 +6 项
> 状态：方案稿 v4.0，待 Review
> 关联文档：
> - `docs/用例管理_复用计划重跑_20260721.md` v3.3（前置方案，已实现）
> - `docs/综合评审与问题分类_20260711.md` §0 指导原则 + M4 确定性断言 + N1/N2 click 定位
> - `docs/用例计划质量提升_execution_plan_review_20260722.md`（v1 review #1）
> - `docs/用例计划质量提升_review_20260722.md`（v1 review #2）
> - `docs/用例计划质量提升_review_team1_20260722.md`（v1.1 team1 review，3 项）
> - `docs/用例计划质量提升_review_team2_20260722.md`（v1.1 team2 review，5 P0 + 6 P1 + 5 观测）
> - v2 review（v3.0 重写依据，11 P0/P1 + 10 测试，已全闭合）
> - v3 review（v4.0 重写依据，4 P0 + 5 P1 + 6 测试）

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

### 0.7 v2 review 处置（v3.0 重写依据）

| # | 来源 | 主张 | 是否成立 | v3.0 处置 |
|---|---|---|---|---|
| **V2-P0-1** | v2 review P0-1 | 全局 `required_anchors` 多页面失效 | 是 | §2.2 / §3.4 / §3.5：拆 `pre_entry`（仅 entry 用）+ `pre_action[N]`（每条 key_action 自带） |
| **V2-P0-2** | v2 review P0-2 | 保留 index 与稳定 locator 同时传 click 误点 | 是（实测 click.py:293-299 早返回） | §2.2 / §3.4 / §3.5：拆 `preferred_locator`（不含 index）+ `observed_index`；prompt 明确"index 仅 LLM 刚 verify 当前 [n] 一致时使用" |
| **V2-P0-3** | v2 review P0-3 | `resolved_target` 落库断链 | 是 | §3.1 / §3.4：`_run_agent` 解析 evidence 段写 `entry.resolved_target`（顶层）；提炼器从 entry 顶层读，删 `ti.get("resolved_label")` 错回退 |
| **V2-P0-4** | v2 review P0-4 | `assert_verification` / `report_done` 不在契约 | 是（实测 verify.py:352,363） | §3.3.1 新增子节：两工具改 `make_result` 契约；`_BAD_STATUS_CODES` 不再误杀 |
| **V2-P0-5** | v2 review P0-5 | 页面签名字段未进 key_action | 是 | §3.4：`key_action` dict 加 `precondition.page_before_signature` / `postcondition.page_after_signature` 字段 |
| V2-P1-1 | v2 review P1-1 | `observed_activity` 解析错 | 是 | §3.3：改读 `ctx.device.current_app().get("activity")`，不再 `split("|")` |
| V2-P1-2 | v2 review P1-2 | entry 缺 package + status 过滤 | 是 | §3.4：entry 过滤加 `package == run.app_package` + `status_code == "OK"` + `arrival_confirmed == "true"` |
| V2-P1-3 | v2 review P1-3 | subjective 默认 "passed" 错 | 是 | §3.4：缺记录默认 `{"result": "unknown", "review_required": True}` |
| V2-P1-4 | v2 review P1-4 | `environment_compatible` 只是"有 expected_activity" | 是 | §3.8：改 `has_environment_precondition`；execution_plan 增 `environment_fingerprint` 字段（package/version code/ROM/分辨率/DPI/语言/方向） |
| V2-P1-5 | v2 review P1-5 | `index_only_locator` 几乎不 true | 是 | §3.8：条件改为"有 `observed_index` 且 无 `preferred_locator` 的稳定组合" |
| V2-P1-6 | v2 review P1-6 | `evidence_stale` 只看 key_actions | 是 | §3.8：改 `override.evidence_stale` 顶层字段；任何 entry/precondition/assertion 修改都触发 plan 级 stale |
| V2-T1 | v2 review 测试补充 | launch_app 真实 tool_input + 契约 status_code | 是 | §6：新增 `test_launch_app_contract.py` 加 1 个用例 |
| V2-T2 | v2 review 测试补充 | assert_verification/report_done 落库 tool_input + 规范 status | 是 | §6：新增 `test_structured_step_logging.py` 加 1 个用例 |
| V2-T3 | v2 review 测试补充 | UNSPECIFIED step 永远不进 evidence | 是 | §6：新增 `test_extract_replay_evidence.py` 加 1 个用例 |
| V2-T4 | v2 review 测试补充 | `parse_evidence` 写 `resolved_*` 到 entry 顶层 | 是 | §6：新增 `test_structured_step_logging.py` 加 1 个用例 |
| V2-T5 | v2 review 测试补充 | 多页面轨迹不会把未来 anchor 作为入口强前置 | 是 | §6：新增 `test_extract_replay_evidence.py` 加 1 个用例 |
| V2-T6 | v2 review 测试补充 | 历史 index 与稳定 locator 同存时不被渲染成联合过滤 | 是 | §6：新增 `test_replay_evidence_prompt.py` 加 1 个用例 |
| V2-T7 | v2 review 测试补充 | 无 entry 成功 run 仍能提炼 action/verification evidence | 是 | §6：新增 `test_extract_replay_evidence.py` 加 1 个用例 |
| V2-T8 | v2 review 测试补充 | 缺 verification_json 项时结果为 `unknown` + `review_required` | 是 | §6：新增 `test_extract_replay_evidence.py` 加 1 个用例 |
| V2-T9 | v2 review 测试补充 | launch package 不匹配/arrival_confirmed=false 不可作 entry | 是 | §6：新增 `test_extract_replay_evidence.py` 加 1 个用例 |
| V2-T10 | v2 review 测试补充 | 编辑 entry/断言/前置条件触发 plan 级 `evidence_stale` | 是 | §6：新增 `test_plan_capabilities.py` 加 1 个用例 |

**统计**：v2 review（11 P0/P1 + 10 测试补充）= **21 条全部成立，全部采纳**。v3.0 相对 v2.0 增量 11 处 + 测试 10 个。

**v3.0 不采纳 0 条**。本轮 review 与 §0 指导原则完全一致——所有建议都属于"让代码沉淀事实/契约"，没有"用代码替 LLM 决策"的特例 patch。

### 0.8 v3 review 处置（v4.0 重写依据）

| # | 来源 | 主张 | 是否成立 | v4.0 处置 |
|---|---|---|---|---|
| **V3-P0-1** | v3 review P0-1 | `page_sig_before` 只用未赋 | 是（实测 `llm_runtime.py:267/318/435` 只有 `page_sig_once`） | §3.1：工具执行前显式 `page_sig_before = page_sig_once` |
| **V3-P0-2** | v3 review P0-2 | precondition 从 page_after 取（方向反） | 是（实测 v3.0 §3.5 L835 取 page_after，L837-838 取 page_before，**自相矛盾**） | §3.1/§3.5/§3.6：precondition 从 page_before 推断，postcondition 从 page_after 推断；activity 作独立结构化字段 |
| **V3-P0-3** | v3 review P0-3 | 提炼器与 `get_test_run` 返回 shape 不兼容 | 是（实测 `relational.py:372-375` `pop("steps_json")` + `d["steps"] = steps`） | §3.5：`steps_raw = run.get("steps") or run.get("steps_json") or "[]"` 双兼容 |
| **V3-P0-4** | v3 review P0-4 | click 多个成功路径不归 make_result | 是（实测 `click.py:606/614/621/623/703/766` 直接返回 tuple/纯字符串） | §3.2 增 `_make_click_success` 统一构造器，所有成功 return 走它 |
| V3-P1-1 | v3 review P1-1 | pre_entry 语义矛盾 | 是（v3.0 §3.5 从 launch activity 取（后置）但命名 pre_entry（前置）） | §3.5：pre_entry 从 launch 的 page_before_signature 取；entry 下加 postcondition 字段（启动后到达事实） |
| V3-P1-2 | v3 review P1-2 | arrival_confirmed 缺 observed_package | 是（v3.0 §3.3 L550-553 只读 activity） | §3.3：加 `observed_package`，`arrival_confirmed = package_matched AND activity_matched` |
| V3-P1-3 | v3 review P1-3 | environment_fingerprint 无 source-run 持久化 | 是（实测 `relational.py:62-69` test_runs 无 environment 字段） | §3.1/§3.4：`test_runs` 加 `environment_json TEXT DEFAULT '{}'`；提炼只读 source run 快照；UI 改"当前环境备注" |
| V3-P1-4 | v3 review P1-4 | base_evidence 不可变只是声明 | 是（v3.0 §2.2 flat structure 全可改） | §2.2：拆 `base_evidence`（不可变）+ `override`（patch）+ `effective`（服务端派生） |
| V3-P1-5 | v3 review P1-5 | assert_verification 状态层次不清 | 是（v3.0 §2.2 `last_result: "passed"` 与 status_code=OK 没区分） | §2.2/§3.3.1：明确 `status_code=OK`（工具成功）+ `result_evidence.reported_result`（agent 主观）+ `objective`（PASS/FAIL 客观） |
| V3-T1 | v3 review 测试补充 | `_build_display_steps` 持久化 result_evidence + resolved_target | 是 | §4：纳入 `test_structured_step_logging.py` |
| V3-T2 | v3 review 测试补充 | `page_sig_before` 在每次工具调用前被记录 | 是 | §4：纳入 `test_structured_step_logging.py` + 增 `_tools_node` 单测 |
| V3-T3 | v3 review 测试补充 | click precondition 来自 page-before，postcondition 来自 page-after | 是 | §4：纳入 `test_extract_replay_evidence.py` + 2 个用例 |
| V3-T4 | v3 review 测试补充 | `get_test_run` 返回 steps 时仍可正确提炼 | 是 | §4：纳入 `test_extract_replay_evidence.py` + 1 个用例 |
| V3-T5 | v3 review 测试补充 | 每一种成功 click 路径都生成 status_code=OK | 是 | §4：纳入 `test_structured_step_logging.py` + 增 `_make_click_success` 单测 |
| V3-T6 | v3 review 测试补充 | override patch 不可修改 base_evidence | 是 | §4：纳入 `test_plan_capabilities.py` + 1 个用例 |
| V3-T7 | v3 review 测试补充 | source run 环境缺失时展示"未记录" | 是 | §4：纳入 `test_extract_replay_evidence.py` + 1 个用例 |
| V3-T8 | v3 review 测试补充 | launch entry 必须满足 observed package + activity + arrival | 是 | §4：纳入 `test_launch_app_contract.py` + 1 个用例 |

**统计**：v3 review（4 P0 + 5 P1 + 8 测试补充）= **17 条全部成立，全部采纳**。v4.0 相对 v3.0 增量 9 处 + 测试 8 个。

**v4.0 不采纳 0 条**。本轮 review 触发了 v3.0 自身 4 个真实闭环 bug（与 v3.0 漏拷 resolved_target 同类）——证明每个 review 轮次都应继续做 code-ground-truth 核对，不应只读自己的 v3.0 文档。

**累计 review 处置**（4 轮）：
- v1.0 (17) + v1.1 team1 (3) + v1.1 team2 (16) = 36 条
- v2 (11 P0/P1 + 10 测试) = 21 条
- v3 (4 P0 + 5 P1 + 8 测试) = 17 条
- **合计 74 条，全部成立，全部采纳**。**不采纳：1 条**（v1.1 R4.4 NOT_FOUND→RAG 提示，违反 §0 禁止针对特例 patch）

---

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

### 2.2 execution_plan schema（v4.0 修正后）

**核心变化**（V3-P1-4 闭合）：execution_plan 拆 `base_evidence`（**不可变事实快照**）+ `override`（**人工 patch**）+ `effective`（**服务端派生**），实现真正的"历史事实不可变、人工调整可审计"。

```jsonc
{
  // 现有 v3.3 字段（保留）
  "goal": "...",
  "app_package": "...",
  "app_name": "...",
  "target_pages": ["..."],
  "verification": ["..."],
  "hints": ["..."],

  // v4.0 execution_plan：base + override + effective 三段式（V3-P1-4 闭合）
  "execution_plan": {
    "schema_version": 4,

    // ── BASE_EVIDENCE：从成功 run 提炼的"事实快照"（不可变）──
    "base_evidence": {
      "extracted_from_run_id": "test-20260722_103153",
      "extracted_at": "2026-07-22T11:00:00",
      "base_revision": 1,           // v4.0 P1-2：提取修订号（服务端单调递增；每次重新提炼 base 都产生新 base_revision，不与旧 evidence 冲突）

      "entry": {                     // 可空（null）：v2 review P1-3 起始已在目标页时 entry 为空但 key_actions 仍可复用
        "launch_app_args": { "package": "com.android.settings", "activity": "..." },
        "postcondition": {            // v4.0 V3-P1-1：entry 自身带 postcondition（启动后到达事实）
          "status_code": "OK",
          "observed_package": "com.android.settings",
          "observed_activity": "com.android.settings.Settings$WifiSettingsActivity",
          "arrival_confirmed": true
        }
      },
      "pre_entry": null,              // v4.0 V3-P1-1：从 launch 的 page_before_activity 取（前置页面）

      "key_actions": [                // 每条 action 自带 precondition / postcondition（V3-P0-2 方向修正）
        {
          "step": "open_switch",
          "tool": "click",
          "precondition": {           // v4.0 V3-P0-2：从 page_before 取（pre-state，方向正确）
            "expected_activity": "Settings",                    // page_before_activity
            "required_anchors": [{"label": "WLAN", "role": "switch_row"}],
            "soft_page_signature": "Settings|...|hash",
            "page_before_signature": "Settings|...|hash"
          },
          "preferred_locator": {       // v3.0 P0-2：不含 index 的稳定 locator
            "label": "WLAN",
            "rid": "",
            "class_name": "LinearLayout",
            "path_contains": "content_frame > main_content > container_material",
            "alternatives": ""
          },
          "observed_index": 3,        // v3.0 P0-2：历史观察的 page_info 全局 [n]；不与 preferred_locator 联合使用
          "resolved_target": {         // v3.0 P0-3：从 entry 顶层读（click 解析事实）
            "label": "WLAN",
            "role": "switch_row",
            "rid": "android:id/switch_widget",
            "class_name": "LinearLayout",
            "path": "content_frame > main_content > ..."
          },
          "postcondition": {           // v4.0 V3-P0-2：从 page_after 取（post-state，方向正确）
            "expected_activity": "com.android.settings.Settings$WifiSettingsActivity",
            "soft_page_signature": "Settings$WifiSettingsActivity|...|hash",
            "page_after_signature": "Settings$WifiSettingsActivity|...|hash"
          },
          "last_observation": "开关状态: 开启 ✓",
          "last_result": "OK"
        },
        {
          "step": "assert_on",
          "tool": "assert_page_contains",
          "precondition": { "expected_activity": "com.android.settings.Settings$WifiSettingsActivity" },
          "args": { "text": "已连接" },
          "last_observation": "PASS",
          "last_result": "PASS"
        },
        {
          "step": "verify_v0",
          "tool": "assert_verification",
          "precondition": { "expected_activity": "com.android.settings.Settings$WifiSettingsActivity" },
          "verify_key": "v0",
          // v4.0 V3-P1-5：状态层次分离
          "last_status_code": "OK",                       // 工具成功（make_result "OK"）
          "last_observation": "记录完成: WLAN开关状态变为开启 → passed",
          "last_result": "passed",                         // 实际是 result_evidence.reported_result
          "result_evidence": {                            // v4.0 显式分
            "reported_result": "passed",                  // agent 主观报告
            "review_required": false
          }
        },
        { "step": "close_switch", "tool": "click", "precondition": {...}, "preferred_locator": {...}, "observed_index": 3, "resolved_target": {...} },
        { "step": "assert_off",  "tool": "assert_page_contains", "precondition": {...}, "args": { "text": "..." } },
        { "step": "verify_v1",   "tool": "assert_verification", "precondition": {...}, "verify_key": "v1" },
        { "step": "done",        "tool": "report_done", "precondition": { "expected_activity": "com.android.settings.Settings$WifiSettingsActivity" } }
      ],

      "verification_evidence": {      // v3.0 P1-3：缺记录默认 unknown + review_required
        "v0": {
          "item": "WLAN开关状态变为开启",
          "subjective": {              // agent 报告（不保证 ground truth）
            "result": "passed",         // 缺记录时默认 "unknown"，review_required=true
            "detail": "...",
            "review_required": false
          },
          "objective": [               // 确定性断言（M4 PASS = 权威）
            { "kind": "assert_page_contains", "text": "已连接", "result": "PASS" }
          ]
        },
        "v1": { /* 同上 */ }
      },

      "environment_fingerprint": {    // v4.0 V3-P1-3：仅读 source run.environment_json
        "package": "com.android.settings",
        "version_code": "146780000",
        "rom": "...",
        "resolution": "1920x1080",
        "dpi": "420",
        "locale": "zh-CN",
        "orientation": "portrait"
      }
    },

    // ── OVERRIDE：人工 patch（V3-P1-4 闭合：base 不可变，人工改用 patch 表达）──
    "override": {
      "revision": 0,                 // v4.0：刚提炼时 override.revision=0（人工修改时 +1）
      "patch": [                       // JSON-Patch 形式（RFC 6902）的修改列表
        // { "op": "replace", "path": "/key_actions/0/preferred_locator/path_contains", "value": "new_path" },
        // { "op": "add",    "path": "/key_actions/2/precondition/required_anchors/-", "value": {...} }
      ],
      "changed_paths": [               // 服务端校验后写回，便于审计
        // "key_actions[0].preferred_locator.path_contains",
        // "verification_evidence.v0.objective[0].text"
      ],
      "evidence_stale": false,          // v3.0 P1-6：任何 override 改动都强制 true
      "edited_at": null,
      "edited_by": null
    },

    // ── EFFECTIVE：服务端派生，prompt 渲染只读这里（V3-P1-4 闭合）──
    // 客户端不可写；服务端 apply base_evidence + override.patch 后写回
    "effective": {
      "schema_version": 4,
      "effective_revision": 1,         // v4.0 P1-2：服务端单调递增（初值=base_revision；每次 override / 重新提炼 base 都 +1；绝不简单做两值相加）
      "applied_at": "2026-07-22T11:00:00",
      // 完整推导后的 key_actions / pre_entry / entry / environment_fingerprint ...
      "key_actions": [ /* merged from base + override */ ]
    }
  }
}
```

**v4.0 相对 v3.0 的关键变化**：
- execution_plan 拆 `base_evidence`（不可变事实）+ `override`（patch）+ `effective`（服务端派生，V3-P1-4 闭合）—— 真正"历史事实不可变"
- 启动后到达事实从"pre_entry 里"搬到"entry.postcondition"（V3-P1-1 修正语义）
- precondition 用 page_before 推断，postcondition 用 page_after 推断（V3-P0-2 方向修正）
- activity 作独立结构化字段（不再 split("|") 反向解析，V3-P0-2 衍生）
- `assert_verification` 状态层次分离：`last_status_code=OK`（工具成功）+ `result_evidence.reported_result`（agent 主观）+ `objective`（PASS/FAIL 客观，V3-P1-5 闭合）
- launch_app evidence 加 `observed_package` + `package_matched`（V3-P1-2 闭合）
- `test_runs.environment_json` 列新增（V3-P1-3 闭合）
- **before 事实在工具调用前读取**（`before_app = ctx.device.current_app()` 必须在 `t.invoke(args)` **之前**，P0-1 闭合，否则 page_before≈page_after 重新污染多页面 replay）
- **effective 在 create/update 时立刻 materialize**（P0-2 闭合），prompt 渲染只读 effective；v4 case 缺 effective 视为损坏、拒绝当 flat v3 evidence
- **env 快照在 run 开始采集**（`orchestrator.start/start_stream` 建 initial_state 前，`_environment_snapshot` 进 initial_state；reporter 持久化初始快照，P1-3 闭合）
- **effective_revision 服务端单调递增**（base_revision + override.revision 派生新序列，不简单两值相加；重新提炼 base 也 +1，P1-2 闭合）
- **execution_plan_revision 从 effective 下传所有 run 入口**（HTTP/WS 共用 `_resolve_run_entry`，P1-1 闭合）

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

**改动 A**（`agents/llm_runtime.py:_tools_node` 内，**v4.0 闭合 V3-P0-1 关键修复**——`page_sig_before` 必须显式赋值）：

```python
# v4.0 P0-1 关键修复：工具调用【前】必须先读取同一轮的前置事实（before_app / page_sig_before）
# ⚠️ 绝不能把 before_app 放到 t.invoke(args) 之后读 —— 否则 before 会被覆盖成 launch 后页面，
#    导致 page_before_activity ≈ page_after_activity，重新破坏 v4 想修复的核心逻辑：
#    launch 前页面无法真实记录 / pre_entry 仍可能变成 launch 后页面 /
#    click precondition 可能再次变成目的页面 / 多页面 replay evidence 会被污染。
before_app = _ctx.device.current_app() or {} if _ctx and _ctx.device else {}
page_sig_before = page_sig_once                                  # v4.0 V3-P0-1 闭合

# 执行工具（唯一的"事实分界点"）
output = str(t.invoke(args)) if t else f"UNKNOWN_TOOL: {name}"

# 工具调用【后】再读取后置事实（after_app / page_sig_after）
page_sig_after = _build_page_signature(_ctx)
after_app  = _ctx.device.current_app() or {} if _ctx and _ctx.device else {}

# v4.0 V3-P0-2：activity 作独立结构化字段（不再 split("|") 反向解析）
page_before_activity = (before_app.get("activity", "") or "").strip()
page_after_activity  = (after_app.get("activity", "")  or "").strip()
page_before_package  = (before_app.get("package", "") or "").strip()
page_after_package   = (after_app.get("package",  "") or "").strip()

# 实时记录工具调用日志（过滤感知类，不去重）
if name not in _SKIP_EMIT:
    entry: dict[str, Any] = {
        "name": name,
        "target": target_hint,
        "intent_text": (getattr(last_ai, "content", "") or "").strip()[:200],
        "observation": output[:200],
        "screenshot_path": _screenshot_path,
        "tool_seq": len(_current_log),
        "tool_input": dict(args or {}),
        "status_code": _extract_status_code(output),
        # v4.0：page_signature 是软诊断（labels hash 会变），activity 是确定性事实
        "page_before_signature": page_sig_before,
        "page_after_signature": page_sig_after,
        "page_before_activity": page_before_activity,    # v4.0 V3-P0-2：结构化字段
        "page_after_activity": page_after_activity,      # v4.0 V3-P0-2：结构化字段
        "page_before_package": page_before_package,      # v4.0 V3-P1-2：launch 校验需要
        "page_after_package": page_after_package,        # v4.0 V3-P1-2：launch 校验需要
        "result_evidence": _parse_evidence(output),
    }
    # click 额外：match_mode / fallback_used / resolved_target
    if name == "click":
        entry["match_mode"] = _resolve_click_match_mode(name, args, output)
        entry["fallback_used"] = _resolve_click_fallback(output)
        # V3-P0-3：click 解析事实从 evidence 段提取，写入 entry 顶层
        ev = entry.get("result_evidence") or {}
        entry["resolved_target"] = {
            "label":       ev.get("resolved_label", ""),
            "role":        ev.get("resolved_role", ""),
            "rid":         ev.get("resolved_rid", ""),
            "class_name":  ev.get("resolved_class", ""),
            "path":        ev.get("resolved_path", ""),
        }
    _current_log.append(entry)
```

**`_extract_status_code` 补伪代码**：
```python
def _extract_status_code(output: str) -> str:
    """从工具输出提取 L1 状态码（复用 tools/results.parse_status 契约）。
    未解析到规范状态时返回 "UNSPECIFIED"（不默认 OK）。"""
    from tools.results import parse_status
    parsed = parse_status(output)
    return parsed or "UNSPECIFIED"

def _parse_evidence(output: str) -> dict:
    """从工具输出 evidence 段（k=v; k=v）解析为 dict。"""
    from tools.results import parse_evidence
    return parse_evidence(output or "")
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
            # v4.0：结构化执行事件（V2-P0-3 / V2-P1-2 / V3-P0-2 闭合必需）
            "tool_input": dict(t.get("tool_input") or {}),
            "tool_seq": t.get("tool_seq", idx),
            "status_code": t.get("status_code", "UNSPECIFIED"),
            "page_before_signature": t.get("page_before_signature", ""),
            "page_after_signature": t.get("page_after_signature", ""),
            # v4.0 V3-P0-2：activity 作独立结构化字段（确定性事实，precondition 推断用）
            "page_before_activity": t.get("page_before_activity", ""),
            "page_after_activity": t.get("page_after_activity", ""),
            "page_before_package": t.get("page_before_package", ""),
            "page_after_package": t.get("page_after_package", ""),
            # v4.0 关键：所有工具统一保留 evidence 段解析结果
            "result_evidence": dict(t.get("result_evidence") or {}),
        }
        # click 额外：match_mode / fallback_used / resolved_target（顶层）
        if t.get("match_mode"):
            step["match_mode"] = t["match_mode"]
        if "fallback_used" in t:
            step["fallback_used"] = bool(t["fallback_used"])
        if t.get("resolved_target"):
            step["resolved_target"] = dict(t["resolved_target"])
        result.append(step)
    for s in history:
        idx += 1
        result.append({**s, "index": idx})
    return result if result else history
```

**回退兼容**：旧 v3.3 run 的 `steps_json` 不含新字段，提炼器读不到则跳过；不影响落库；这些 run 不能作为 evidence 源（必须 Step 1 部署后跑出的 run 才行）。

**v4.0 关键提醒**：
- `_build_display_steps` 漏拷 `result_evidence` / `resolved_target` / `page_*_activity` 会让 §3.5 提炼器完全失效。
- `page_sig_before` 必须在 `_tools_node` 每次工具执行前**显式赋值**（V3-P0-1）——不能依赖 step dict 顺序。
- **P0-1 闭环硬约束**：`before_app = _ctx.device.current_app()` 必须在 `output = str(t.invoke(args))` **之前**执行，`page_before_activity/package` 必须 == 工具调用前的真实设备状态。当前测试清单只验证 `page_sig_before` 被赋值，还不够——必须新增单测验证：**工具使 Activity 从 A 变为 B 时，`page_before_activity == A` 且 `page_after_activity == B`**（见 §4 `test_structured_step_logging.py` 的 P0-1 闭环单测、§6 Step 1 验证项）。

### 3.2 Step 2 (P0)：click 工具落库 `resolved_target` + 成功路径统一构造器（v4.0）

**目标**（team2 P1-2 + V3-P0-4）：
1. role/rid/path 等**解析事实**（不是 agent 调用参数）必须由 click 工具自己记录
2. click 工具**所有成功返回路径**必须走统一契约，否则提炼器会判 UNSPECIFIED 并过滤

**当前问题（V3-P0-4 已实测）**：
- `tools/click.py:606/614/621/623` 返回 `True, _format_click_log(...)` tuple（无 OK: 前缀）
- `click.py:703/766` 返回 `_with_snapshot(f"已点击: {label} (strategy=...)", None)` 纯字符串（无 OK: 前缀）
- 仅少数分支用 `make_result`；多数成功路径走 `result` 元组（`_with_snapshot` 包裹）

**改动 A**（`tools/click.py`）—— 新增**统一成功构造器**（所有成功 return 必须走它）：
```python
def _make_click_success(message: str, resolved: dict, *, match_mode: str, fallback_used: bool) -> str:
    """v4.0 V3-P0-4 闭合：click 所有成功路径都走此构造器。
    输入：人类可读 message（保留原文案）+ click 解析事实 resolved。
    输出：OK: ... || match_mode=...; fallback_used=...; resolved_label=...; resolved_role=...; ...
    """
    from tools.results import make_result
    return make_result(
        "OK",
        message,
        evidence={
            "match_mode": match_mode,
            "fallback_used": str(bool(fallback_used)).lower(),
            "resolved_label": resolved.get("label", ""),
            "resolved_role": resolved.get("role", ""),
            "resolved_rid": resolved.get("rid", ""),
            "resolved_class": resolved.get("class_name", ""),
            "resolved_path": resolved.get("path", ""),
        },
    )
```

**改动 B**（`tools/click.py`）—— 替换所有原成功 return：
```python
# 原：return _with_snapshot(f"已点击: {label} (strategy=text-fallback)", None)
# 新：
return _make_click_success(
    f"已点击: {label} (strategy=text-fallback)",
    resolved={"label": label, "role": "", "rid": "", "class_name": "", "path": ""},
    match_mode="text-fallback",
    fallback_used=True,
)

# 原：return _with_snapshot(f"已点击资源: {label} (strategy=rid-fallback)", None)
# 新：
return _make_click_success(
    f"已点击资源: {label} (strategy=rid-fallback)",
    resolved={"label": label, "rid": rid},
    match_mode="rid-fallback",
    fallback_used=True,
)

# 原：return _with_snapshot(result, best_el)  # result = "OK: ..." 或 "NOT_FOUND: ..."
# 新：保持（result 已是 make_result 形式）；仅在 best_el 已知时补 resolved_target
if best_el:
    return _make_click_success(
        message="已点击",
        resolved={
            "label": getattr(best_el, "label", label),
            "role": getattr(best_el, "role", ""),
            "rid": getattr(best_el, "resource_id", ""),
            "class_name": getattr(best_el, "class_name", ""),
            "path": getattr(best_el, "context_path", ""),
        },
        match_mode=match_mode,
        fallback_used=fallback_used,
    )
```

**改动 C**（`tools/click.py`）—— 保留 `make_result` 调用，但**统一证据结构**（v3.0 之前是直接 make_result，没走构造器）：
```python
# 在 click() 内部，最终调用 tools/results.py:make_result：
return _make_click_success(
    message=message,
    resolved=resolved,
    match_mode=match_mode,
    fallback_used=fallback_used,
)
```

**对提炼器的影响（V3-P0-4 闭合后）**：
- 所有 click 成功返回都是 `"OK: ..."` + `|| match_mode=...; resolved_label=...; ...` evidence 段
- `_extract_status_code` → `"OK"`（不被 `_BAD_STATUS_CODES` 过滤 ✓）
- `parse_evidence` → `{"match_mode": "...", "resolved_label": "...", "resolved_role": "...", ...}`
- 提炼器从 entry 顶层 `resolved_target` 读 `label/role/rid/class_name/path`（V3-P0-3 闭合）

**回退兼容**：`tools/results.parse_status` 对旧字符串（如 `"已点击: WLAN"`)返回 `""`（宽容），但旧 click 路径已被替换为新构造器，**实际无破坏面**。

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

    # v3.0 P1-1：直接读 device.current_app()，不再 split "_capture_page_id"
    # v4.0 V3-P1-2：同时读 package 和 activity（启动后被系统页/权限页拦截也判否）
    try:
        _app = ctx.device.current_app() or {}
        observed_package  = (_app.get("package",  "") or "").strip()
        observed_activity = (_app.get("activity", "") or "").strip()
    except Exception:
        observed_package = ""
        observed_activity = ""
    _post_page = _capture_page_id(ctx) or ""  # 仅作辅助 fingerprint

    # v4.0 V3-P1-2 闭合：arrival_confirmed = package_matched AND activity_matched
    def _normalize_activity(act: str) -> str:
        return (act or "").split(".")[-1].strip()

    package_matched = bool(observed_package) and observed_package == package
    if target_activity:
        activity_matched = (
            _normalize_activity(observed_activity) == _normalize_activity(target_activity)
            or target_activity in observed_activity
        )
    else:
        activity_matched = bool(observed_activity)
    arrival_confirmed = package_matched and activity_matched

    _record_page_transition(ctx, _pre_page, f"launch_app({package})")

    if arrival_confirmed:
        return make_result(
            "OK", f"已启动 {package}",
            evidence={
                "requested_package": package,
                "requested_activity": target_activity,
                "observed_package": observed_package,        # v4.0 V3-P1-2 闭合
                "observed_activity": observed_activity,
                "package_matched": "true",                   # v4.0 V3-P1-2
                "activity_matched": "true",                  # v4.0 V3-P1-2
                "arrival_confirmed": "true",
            }
        )
    else:
        # 启动未抛异常但实际未到目标 Activity：ERROR（不是 OK，team2 P0-3）
        return make_result(
            "ERROR", f"启动后未到达预期 Activity（package_matched={package_matched}, activity_matched={activity_matched}）",
            evidence={
                "requested_package": package,
                "requested_activity": target_activity,
                "observed_package": observed_package,        # v4.0 V3-P1-2 闭合
                "observed_activity": observed_activity,
                "package_matched": str(package_matched).lower(),   # v4.0 V3-P1-2
                "activity_matched": str(activity_matched).lower(),  # v4.0 V3-P1-2
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

### 3.3.1 Step 3b (P0)：`assert_verification` / `report_done` 改 `make_result` 契约（闭合 P0-4）

**目标**（v2 review P0-4）：两核心工具当前返回不符合 `tools/results.py` 统一契约，会被 `_extract_status_code` 判为 `UNSPECIFIED`，导致：
- `assert_verification` 被 `_BAD_STATUS_CODES` 过滤 → `verify_idx` 不增长 → 后续 click 全部错误绑定到 v0
- `report_done` 被过滤 → 没法识别终止信号

**当前实测**（`tools/verify.py`）：
- L352：`return f"记录完成: {condition} → {normalized_result}{suffix}"`
- L363：`return f"REPORTED: {status} | {summary}"`
- 均不在 `tools/results.py:32` 状态词表（`OK/NOT_FOUND/AMBIGUOUS/ERROR/NEEDS_HUMAN/PASS/FAIL`）

**改动 A**（`tools/verify.py:188 assert_verification`）：
```python
# 末尾 return 改为：
from tools.results import make_result
suffix = "（需要人工复核）" if normalized_result == "unknown" else ""
return make_result(
    "OK",  # 状态码用 OK（不是 PASS/FAIL——assert_verification 是主观报告，不参与 ground truth）
    f"记录完成: {condition} → {normalized_result}{suffix}",
    evidence={
        "verification_key": verification_key,
        "reported_result": normalized_result,   # passed/failed/unknown
        "review_required": "true" if normalized_result == "unknown" else "false",
        "detail_len": str(len(detail or "")),
    },
)
```

**改动 B**（`tools/verify.py:356 report_done`）：
```python
# 末尾 return 改为：
from tools.results import make_result
status_norm = (status or "").lower()
return make_result(
    "OK" if status_norm == "done" else "ERROR",
    f"已报告: {status_norm}",
    evidence={
        "terminal_status": status_norm,        # done / abort
        "summary_len": str(len(summary or "")),
    },
)
```

**对现有调用方的影响**：
- `_run_agent`（`llm_runtime.py:457-463`）当前用 `if name == "report_done"` + 解析 args —— **不依赖返回字符串前缀**，无 break。
- `_extract_status_code` 返回 `"OK"`（done）或 `"ERROR"`（abort）→ 不被 `_BAD_STATUS_CODES` 过滤 ✓
- `assert_verification` 返回 `"OK"` → 提炼器纳入 key_actions 且 `verify_idx` 推进 ✓
- M4 确定性断言（`assert_page_contains` / `assert_element_exists`）已用 `make_result` 返回 `PASS/FAIL`，不受影响

**回退兼容**：`tools/results.parse_status` 对旧字符串 `"记录完成: ..."` / `"REPORTED: ..."` 返回 `""`（宽容），不破坏现有解析路径。

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
-- test_runs：lineage 拆分 + 环境快照（v4.0 V3-P1-3 闭合）
CREATE TABLE test_runs (
    ... (现有字段) ...,
    goal_json         TEXT NOT NULL DEFAULT '{}',
    run_type          TEXT NOT NULL DEFAULT 'normal',
    source_run_id     TEXT,                   -- v3.3：仅报告复跑时填
    source_case_id    TEXT,                   -- v2.0 新增：仅 case 运行时填
    execution_plan_revision INTEGER DEFAULT 0,  -- v2.0 新增
    environment_json  TEXT NOT NULL DEFAULT '{}'   -- v4.0 V3-P1-3：环境快照（含 package/version_code/ROM/分辨率/DPI/语言/方向）
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
    # v4.0 V3-P1-3
    environment_json="{}",
) -> None:
```

`reporter_node` 调 `record_test_run` 时同步透传三新字段：
- `source_case_id=state.get("_source_case_id")`
- `execution_plan_revision=state.get("_execution_plan_revision", 0)`
- `environment_json=json.dumps(state.get("_environment_snapshot") or {})`（**v4.0 V3-P1-3 + P1-3 闭合**：快照在 run 开始时采集，不是 run 结束）

**环境快照采集时机（v4.0 P1-3 闭合）：必须在 `orchestrator.start()` / `start_stream()` 建立 `initial_state` 【之前】采集**，记录的是 replay 的**起始条件**（orientation / locale / 网络 / WLAN / 当前 App·Activity / 系统设置等），而非 reporter 阶段（运行结束后）的值：

```python
def _collect_environment_snapshot(ctx, app_package: str) -> dict:
    """v4.0 P1-3：run 开始时采集初始环境快照（start conditions）。"""
    return {
        "captured_at": _dt.now().isoformat(),
        "stage": "run_start",
        "package":      app_package or "",
        "version_code": _get_app_version_code(ctx, app_package),  # adb dumpsys package
        "rom":          _get_device_rom(ctx),                     # adb getprop ro.build.version.release
        "resolution":   f"{ctx.screen_size[0]}x{ctx.screen_size[1]}" if ctx.screen_size else "",
        "dpi":          str(getattr(ctx.device, "dpi", "") or ""),
        "locale":       _get_device_locale(ctx),                  # adb getprop persist.sys.locale
        "orientation":  "portrait" if _is_portrait(ctx) else "landscape",
    }

# orchestrator.start / start_stream 内部，建立 initial_state 之前：
snapshot = _collect_environment_snapshot(ctx, app_package)
initial_state["_environment_snapshot"] = snapshot
```

若需诊断运行中变化，可额外保存 `environment_end`（run 结束后再采集一次），但**绝不能让 end snapshot 代替 start conditions**。reporter 直接持久化 `state["_environment_snapshot"]` 这个初始快照。

### 3.5 Step 5 (P0)：`_extract_replay_evidence` 完整版（v4.0）

**目标**（R1/R3/R4.1/R4.2/R10/R12 + v2 review P0-1~P0-5 / P1-1~P1-6 + v3 review P0-1~P0-4 / P1-1~P1-5）：从成功 run 提炼 replay evidence。

**输入**：`run` dict（含 `steps` 或 `steps_json` / `goal_json` / `verification_json` / `execution_status` / `test_verdict` / `environment_json`）。

**输出**：`base_evidence` dict（schema 见 §2.2），**可无 entry**（team2 P1-3）。override 由 UI 修改触发服务端产生。

**算法**（v4.0 完整版）：
```
1. 门控：run.execution_status != 'completed' OR test_verdict != 'passed'  → 返回 None
2. 解析 steps（V3-P0-3 闭合）：run.get("steps") 优先（get_test_run 返回 shape），fallback run.get("steps_json")
3. 找 entry（V2-P1-2 + V3-P1-2 四重过滤）：
   - 找到第一个可验证业务动作片段（click/assert_*），向前找最近 launch_app
   - 必须满足全部四个条件：
     (a) ti.package == run.app_package
     (b) status_code == "OK"
     (c) result_evidence["arrival_confirmed"] == "true"
     (d) result_evidence["package_matched"] == "true"  (V3-P1-2 新增)
   - 若都不满足，entry=null（key_actions 仍可复用）
4. 提取 entry.postcondition + pre_entry（V3-P1-1 修正语义）：
   - entry.postcondition  = launch 后的到达事实（status_code="OK", observed_package, observed_activity, arrival_confirmed）
   - pre_entry            = launch 前的页面状态（从 launch 的 page_before_activity 取；空则 pre_entry=null）
5. 提取每条 key_action 的 precondition / postcondition（V3-P0-2 方向修正）：
   - precondition.expected_activity    ← st["page_before_activity"]（pre-state）
   - precondition.soft_page_signature  ← st["page_before_signature"]（pre-state）
   - postcondition.expected_activity   ← st["page_after_activity"]（post-state）
   - postcondition.soft_page_signature ← st["page_after_signature"]（post-state）
   - 每条 key_action.precondition.required_anchors = 该 click 的 resolved_target 单条
6. 找 key_actions（V2-P0-5 闭合：page_*_signature 写入 key_action 字典）：
   - status_code 在 {OK, PASS, passed} 且 observation 不含 NOT_FOUND/AMBIGUOUS/NEEDS_HUMAN/ERROR 才纳入
   - click：preferred_locator = tool_input 去掉 index；observed_index = ti["index"] 单独存
            resolved_target 从 entry 顶层读（V2-P0-3 闭合：不再回退 ti.get("resolved_label")）
   - assert_verification / assert_page_contains / assert_element_exists：纳入（V2-P0-4 闭合）
   - launch / press_key / scroll / swipe / visual_check：跳过
7. 算 verification_evidence（V2-P1-3 修正：缺记录默认 unknown + review_required=True）：
   - 从 run.verification_json 读每个 vN 的 {result, detail, review_required}
   - 缺记录默认：{"result": "unknown", "detail": "历史运行缺少该验证项的结构化报告", "review_required": True}
   - objective = 同一 verify_key 绑定的最近一条 assert_page_contains / assert_element_exists
7. 算 environment_fingerprint（V2-P1-4 + V3-P1-3 闭合）：仅读 source run 的 `environment_json` 字段，缺字段显示"未记录"（不伪造/不判不兼容）
8. 构造 base_evidence（V3-P1-4 闭合）：上述 1-7 步产出"事实快照"放入 `base_evidence` 块——**不可变**
9. 不在提炼时构造 override；override 由 UI 修改触发服务端产生（V3-P1-4）
10. 不写 plan_quality_json；UI 端 API 派生 quality 离散能力标签（Step 8）
```

**伪代码**（`api/test_cases_routes.py`）：
```python
import re, json
from datetime import datetime as _dt

# v3.0 P0-2：preferred_locator 不含 index；observed_index 单独存
_PREFERRED_LOCATOR_FIELDS = ("label", "rid", "class_name", "path_contains", "alternatives")
# 失败/无证据标记（V2-P0-4 闭合：assert_verification/report_done 改契约后不会被误杀）
_BAD_STATUS_CODES = ("NOT_FOUND", "AMBIGUOUS", "NEEDS_HUMAN", "ERROR", "UNSPECIFIED")
_BAD_OBSERVATION_TOKENS = ("NOT_FOUND", "AMBIGUOUS", "NEEDS_HUMAN", "ERROR:")


def _extract_replay_evidence(run: dict) -> dict | None:
    """从一条成功 run 提炼 replay evidence（仅 base_evidence，V3-P1-4 闭合）；返回 None 表示不应生成。"""
    if run.get("execution_status") != "completed" or run.get("test_verdict") != "passed":
        return None

    # v4.0 V3-P0-3 闭合：run.get("steps") 优先（get_test_run 实际返回 shape），fallback run.get("steps_json")
    steps_raw = run.get("steps")
    if steps_raw is None:
        steps_raw = run.get("steps_json") or "[]"
    try:
        steps = json.loads(steps_raw) if isinstance(steps_raw, str) else list(steps_raw)
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

    # ── 找第一个"可验证业务动作片段"前的最近 launch_app（V2-P1-2：三重过滤）──
    first_action_idx = None
    for i, st in enumerate(steps):
        if st.get("action_type") in ("click", "assert_verification", "assert_page_contains", "assert_element_exists"):
            first_action_idx = i
            break
    entry = None
    pre_entry = None
    if first_action_idx is not None:
        for st in reversed(steps[:first_action_idx]):
            if st.get("action_type") != "launch_app":
                continue
            ti = st.get("tool_input") or {}
            ev = st.get("result_evidence") or {}
            pkg = (ti.get("package") or "").strip()
            act = (ti.get("activity") or "").strip()
            # V2-P1-2 + V3-P1-2 闭合：四重过滤——package + status_code + arrival_confirmed + package_matched
            pkg_match = bool(pkg) and pkg == (run.get("app_package", "") or "")
            status_ok = st.get("status_code") == "OK"
            arrived = ev.get("arrival_confirmed") == "true"
            pkg_matched_ev = ev.get("package_matched") == "true"   # v4.0 V3-P1-2
            if pkg_match and status_ok and arrived and pkg_matched_ev:
                # v4.0 V3-P1-1 修正：entry 自身带 postcondition（启动后到达事实）
                entry = {
                    "launch_app_args": {"package": pkg, "activity": act},
                    "postcondition": {
                        "status_code": "OK",
                        "observed_package": ev.get("observed_package", ""),
                        "observed_activity": ev.get("observed_activity", ""),
                        "arrival_confirmed": True,
                    },
                }
                # v4.0 V3-P1-1 修正：pre_entry 从 launch 的 page_before_activity 取（前置页面），不从 launch 的 activity 参数取（那是后置）
                pre_act = (st.get("page_before_activity", "") or "").strip()
                if pre_act:
                    pre_entry = {
                        "expected_activity": pre_act.split(".")[-1],
                        "required_anchors": [],
                        "soft_page_signature": st.get("page_before_signature", ""),
                    }
                else:
                    pre_entry = None
                break
            # 不满足四重条件 → 不作 entry；继续向前找（不一定 break）

    # v2 review P1-3：entry 可空；key_actions 仍可复用

    # ── key_actions（V2-P0-1 闭合：precondition 分段；V2-P0-2：locator 拆 preferred + observed；V3-P0-2：方向修正）──
    key_actions = []
    verify_idx = 0
    for st in steps:
        at = st.get("action_type")
        status_code = st.get("status_code", "UNSPECIFIED")
        observation = st.get("observation", "") or ""
        if status_code in _BAD_STATUS_CODES:
            continue
        if any(bad in observation for bad in _BAD_OBSERVATION_TOKENS):
            continue

        # V2-P0-5 闭合 + V3-P0-2 方向修正：每条 action 都带 precondition / postcondition
        # precondition 从 page_before 取（pre-state），postcondition 从 page_after 取（post-state）
        precondition = {
            # v4.0 V3-P0-2：用结构化 page_before_activity 字段（不再 split("|") 反向解析）
            "expected_activity": (st.get("page_before_activity", "") or "").split(".")[-1] if st.get("page_before_activity") else "",
            "required_anchors": [],
            "soft_page_signature": st.get("page_before_signature", "") or "",
            "page_before_signature": st.get("page_before_signature", "") or "",
        }
        postcondition = {
            "expected_activity": (st.get("page_after_activity", "") or "").split(".")[-1] if st.get("page_after_activity") else "",
            "soft_page_signature": st.get("page_after_signature", "") or "",
            "page_after_signature": st.get("page_after_signature", "") or "",
        }

        if at == "click":
            ti = st.get("tool_input") or {}
            # V2-P0-2 闭合：preferred_locator 不含 index；observed_index 单独存
            preferred_locator = {k: ti.get(k) for k in _PREFERRED_LOCATOR_FIELDS if k in ti}
            raw_index = ti.get("index", -1)
            observed_index = raw_index if raw_index not in (None, -1, "") else None
            if not preferred_locator.get("label"):
                continue
            verify_key = f"v{verify_idx}" if verify_idx < len(verification) else None
            # V2-P0-3 闭合：从 entry 顶层读 resolved_target（不再回退 ti.get("resolved_label")）
            resolved = st.get("resolved_target") or {}
            # V2-P0-1 闭合：仅本 click 的 resolved_target 作为 required_anchor（不汇总到全局）
            if isinstance(resolved, dict) and (resolved.get("label") or resolved.get("role") or resolved.get("rid")):
                precondition["required_anchors"] = [{
                    "label": resolved.get("label", ""),
                    "role": resolved.get("role", ""),
                    "rid": resolved.get("rid", ""),
                    "class_name": resolved.get("class_name", ""),
                    "path": resolved.get("path", ""),
                }]
            key_actions.append({
                "step": f"click_{str(preferred_locator.get('label','?')).lower().replace(' ','_')}",
                "tool": "click",
                "precondition": precondition,
                "preferred_locator": preferred_locator,
                "observed_index": observed_index,
                "resolved_target": {k: v for k, v in (resolved or {}).items() if v},
                "postcondition": postcondition,
                "last_observation": observation[:200],
                "last_result": status_code or "OK",
                "verify": verify_key,
            })
        elif at == "assert_verification":
            # V2-P0-4 闭合：assert_verification 改 OK 契约后不被过滤；verify_idx 正确推进
            if verify_idx < len(verification):
                key_actions.append({
                    "step": f"verify_v{verify_idx}",
                    "tool": "assert_verification",
                    "precondition": precondition,
                    "postcondition": postcondition,
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
                "precondition": precondition,
                "postcondition": postcondition,
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
                "precondition": precondition,
                "postcondition": postcondition,
                "args": {"label": ti.get("label", "")},
                "last_observation": observation[:200],
                "last_result": status_code or "PASS",
                "verify": f"v{verify_idx}" if verify_idx < len(verification) else None,
            })

    if any(s.get("action_type") == "report_done" for s in steps):
        # V2-P0-4 闭合：report_done 改 OK 契约后不被过滤
        report_done_step = next(s for s in steps if s.get("action_type") == "report_done")
        ti = report_done_step.get("tool_input") or {}
        ev = report_done_step.get("result_evidence") or {}
        key_actions.append({
            "step": "done",
            "tool": "report_done",
            # v4.0 V3-P0-2 方向修正：precondition 用 page_before_activity（pre-state）
            "precondition": {
                "expected_activity": (report_done_step.get("page_before_activity", "") or "").split(".")[-1] if report_done_step.get("page_before_activity") else "",
                "required_anchors": [],
                "soft_page_signature": report_done_step.get("page_before_signature", "") or "",
                "page_before_signature": report_done_step.get("page_before_signature", "") or "",
            },
            "postcondition": {
                "expected_activity": (report_done_step.get("page_after_activity", "") or "").split(".")[-1] if report_done_step.get("page_after_activity") else "",
                "soft_page_signature": report_done_step.get("page_after_signature", "") or "",
                "page_after_signature": report_done_step.get("page_after_signature", "") or "",
            },
            "args": {"status": ti.get("status", "done"), "summary": ti.get("summary", "")},
            "last_observation": (report_done_step.get("observation", "") or "")[:200],
            "last_result": ev.get("terminal_status", "done"),
        })

    if not key_actions:
        return None  # 关键：entry 可空，但 key_actions 仍必须有

    # ── environment_fingerprint（V2-P1-4 + V3-P1-3 闭合）──
    # v4.0：仅读 source run 的 environment_json 字段（test_runs 新加列）；缺字段显式标"未记录"（不伪造/不判不兼容）
    env_fp_raw = {}
    try:
        env_fp_raw = json.loads(run.get("environment_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        pass
    env_fp = {
        "package":      env_fp_raw.get("package", "")      or run.get("app_package", "") or "",
        "version_code": env_fp_raw.get("version_code", "") or "",
        "rom":          env_fp_raw.get("rom", "")          or "",
        "resolution":   env_fp_raw.get("resolution", "")   or "",
        "dpi":          env_fp_raw.get("dpi", "")          or "",
        "locale":       env_fp_raw.get("locale", "")       or "",
        "orientation":  env_fp_raw.get("orientation", "")  or "",
    }

    # ── override 块（V3-P1-4 闭合：base_evidence 不可变，override 用 patch 表达人工调整）──
    override = {
        "revision": 0,                 # v4.0：base_evidence 刚提炼时 override.revision=0
        "patch": [],                   # v4.0：空 patch（无人工修改）
        "changed_paths": [],
        "evidence_stale": False,
        "edited_at": None,
        "edited_by": None,
    }

    # v4.0 V3-P1-4 闭合：提炼只产 base_evidence；override 留空；effective 由服务端派生
    base_evidence = {
        "extracted_from_run_id": run.get("id"),
        "extracted_at": _dt.now().isoformat(),
        "base_revision": 1,           # v4.0 P1-2：首次提炼 base_revision=1；重新提炼时必须 +1（新序列，不与旧冲突）
        "entry": entry,
        "pre_entry": pre_entry,
        "key_actions": key_actions,
        "verification_evidence": _build_verification_evidence(
            verification, verification_json, key_actions
        ),
        "environment_fingerprint": env_fp,
    }

    return {
        "schema_version": 4,
        "base_evidence": base_evidence,    # 不可变事实快照
        "override": override,               # 人工 patch（提炼时为空）
        # effective 由服务端在 API 出口（create_test_case / update_test_case）立刻 materialize（P0-2 闭合）
    }


def _build_verification_evidence(verification, verification_json, key_actions):
    """V2-P1-3 闭合：缺记录默认 unknown + review_required=True（不是 passed）。"""
    out = {}
    for i, item in enumerate(verification):
        vkey = f"v{i}"
        # 缺记录默认（V2-P1-3 修正）
        subjective = {
            "result": "unknown",
            "detail": "历史运行缺少该验证项的结构化报告",
            "review_required": True,
        }
        for ve in verification_json:
            if not isinstance(ve, dict):
                continue
            if ve.get("key") == vkey or (ve.get("item") and ve.get("item") == item):
                subjective = {
                    "result": ve.get("result", "unknown"),
                    "detail": ve.get("detail", ""),
                    "review_required": bool(ve.get("review_required", False)),
                }
                break
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
        out[vkey] = {
            "item": item,
            "subjective": subjective,
            "objective": objective,
        }
    return out
```

**派生入口（v4.0 V3-P1-4 + P0-2 闭合：服务端唯一 materialize effective 的地方）**：

```python
import copy

# patch path 仅允许作用于 base_evidence 的以下根前缀（P0-3 闭合：绝不接受 /execution_plan/... 这类越权路径）
_ALLOWED_PATCH_PREFIXES = (
    "/entry/", "/pre_entry/", "/key_actions/",
    "/verification_evidence/", "/environment_fingerprint/",
)

def _apply_json_patch(target: dict, patch: list) -> None:
    """极简 RFC6902 子集：仅支持 add / replace / remove；path 相对于 base_evidence 根。"""
    for op in (patch or []):
        path = op.get("path", "")
        if not path.startswith("/"):
            raise ValueError(f"非法 patch path: {path}")
        if not any(path == p or path.startswith(p + "/") or path.startswith(p + "[")
                   for p in _ALLOWED_PATCH_PREFIXES):
            raise ValueError(f"patch path 不在允许前缀内: {path}")
        parts = [p for p in path.split("/") if p != ""]
        node = target
        for p in parts[:-1]:
            node = node[int(p)] if p.isdigit() else node[p]
        last = parts[-1]
        kind = op.get("op")
        if kind in ("replace", "add"):
            if last == "-":
                node.append(op.get("value"))
            elif last.isdigit():
                node[int(last)] = op.get("value")
            else:
                node[last] = op.get("value")
        elif kind == "remove":
            if last.isdigit():
                del node[int(last)]
            else:
                del node[last]
        else:
            raise ValueError(f"不支持的 patch op: {kind}")

def derive_effective_plan(base_evidence: dict, patch: list, *, effective_revision: int) -> dict:
    """v4.0 V3-P1-4 + P0-2 闭合：服务端唯一派生 effective 的入口。
    - 从 base_evidence deepcopy 后 apply patch（绝不原地修改 base_evidence）；
    - 生成 monotonic 的 effective_revision（由调用方传入，服务端维护自增）；
    - 客户端永远不直接写 effective / base_evidence。"""
    merged = copy.deepcopy(base_evidence or {})
    _apply_json_patch(merged, patch or [])
    eff = {
        "schema_version": 4,
        "effective_revision": int(effective_revision or 0),
        "applied_at": _dt.now().isoformat(),
    }
    for k in ("entry", "pre_entry", "key_actions", "verification_evidence",
              "environment_fingerprint", "extracted_from_run_id", "extracted_at", "base_revision"):
        if k in merged:
            eff[k] = merged[k]
    return eff
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
        # v4.0 P0-2 闭合：创建时必须【立刻】materialize effective，
        # 否则新 case 的 execution_plan 缺 effective，首次"保存为用例→直接运行"时
        # prompt 渲染会把整个 execution_plan 当 flat v3 evidence（顶层无 entry/key_actions），
        # 导致 REPLAY_EVIDENCE_BLOCK 为空或缺少关键 action。
        base_rev = (evidence.get("base_evidence") or {}).get("base_revision", 0)
        evidence["effective"] = derive_effective_plan(
            base_evidence=evidence["base_evidence"],
            patch=evidence.get("override", {}).get("patch", []),
            effective_revision=base_rev,   # 初值 = base_revision，后续 override 时自增
        )
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

**`update_test_case`（v4.0 P0-2 / P0-3 闭合）**：服务端读已持久化 `base_evidence` → 校验 patch → `deepcopy` 后 apply → 重建并持久化 `effective`：**绝不原地改 `base_evidence`**：
```python
def update_test_case(case_id, body):
    case = _relational_db.get_test_case(case_id)
    goal = json.loads(case["goal_json"] or "{}")
    plan = goal.get("execution_plan") or {}
    base_evidence = plan.get("base_evidence") or {}
    override = plan.get("override") or {"revision": 0, "patch": [], "changed_paths": [], "evidence_stale": False}

    # P0-3：拒绝 body 中任何 base_evidence / effective 字段（破坏"客户端不能写 base/effective"不变量）
    if "base_evidence" in body or "effective" in body:
        return {"status": "error", "message": "不允许直接修改 base_evidence / effective，请改用 override_patch"}

    patch = body.get("override_patch") or []
    try:
        _apply_json_patch(copy.deepcopy(base_evidence), patch)   # 先 dry-run 校验
    except ValueError as e:
        return {"status": "error", "message": f"patch 非法: {e}"}

    # P1-2：effective_revision 单调递增（初值=base_revision，每次 override 都 +1，不与旧冲突）
    new_eff_rev = int(base_evidence.get("base_revision", 0) or 0) + 1 + int(override.get("revision", 0) or 0)
    effective = derive_effective_plan(
        base_evidence=base_evidence,
        patch=patch,
        effective_revision=new_eff_rev,
    )
    override = {
        "revision": int(override.get("revision", 0) or 0) + 1,
        "patch": patch,
        "changed_paths": body.get("changed_paths", []),
        "evidence_stale": True,                  # V2-P1-6：plan 级 stale
        "edited_at": _dt.now().isoformat(),
        "edited_by": body.get("edited_by"),
    }
    plan = {"schema_version": 4, "base_evidence": base_evidence,
            "override": override, "effective": effective}
    goal["execution_plan"] = plan
    _relational_db.save_test_case(case_id, json.dumps(goal, ensure_ascii=False))
    return {"status": "ok"}
```

### 3.6 Step 6 (P0)：REPLAY_EVIDENCE_BLOCK 渲染（per-action precondition + preferred_locator）

**目标**（R1/R2/R7 + v2 review P0-1/P0-2/P0-3/P0-4）：给 LLM 事实，让 LLM 决策；**前置条件分段到每条 action**（V2-P0-1）；locator 拆 preferred + observed（V2-P0-2）；不把全局 anchor 当强前置。

**改动**（`agents/nodes.py`，找到 system prompt 拼装处）：

```python
def _render_replay_evidence_block(goal_desc: dict) -> str:
    """渲染 REPLAY_EVIDENCE_BLOCK（事实参考，不是强制脚本）。
    v4.0：V3-P1-4 闭合——只读 effective（服务端派生），不直接读 base_evidence 或 override。
    v3.0：per-action precondition（V2-P0-1）；preferred_locator 与 observed_index 分离（V2-P0-2）。"""
    if not isinstance(goal_desc, dict):
        return ""
    exec_plan = goal_desc.get("execution_plan")
    if not exec_plan:
        return ""
    # v4.0 V3-P1-4：prompt 渲染只读 effective（服务端已在 create/update 时 materialize）
    # 防御性兜底（P0-2 闭合）：v4 case 必须 effective 是 dict；否则不应把 execution_plan
    # 顶层（含 base_evidence/override）误当 flat v3 evidence。
    eff = exec_plan.get("effective") if isinstance(exec_plan.get("effective"), dict) else None
    if eff is None:
        # v3.0 老 case（schema_version != 4 且顶层直接有 key_actions）才 fallback 当 flat evidence；
        # v4.0 case 若缺 effective，说明服务端 create/update 没 materialize —— 视为损坏数据，拒绝渲染，
        # 绝不把 execution_plan 顶层误当 flat v3 evidence（会导致 page_before_activity 被污染）。
        if exec_plan.get("schema_version") != 4 and isinstance(exec_plan.get("key_actions"), list):
            eff = exec_plan
        else:
            return ""  # 损坏数据：不渲染任何证据，避免污染 REPLAY_EVIDENCE_BLOCK
    lines = [
        "",
        "## REPLAY_EVIDENCE_BLOCK（来自历史成功运行的事实，**不是强制脚本**）",
        "下列为最近一次成功 run 的执行痕迹，供你判断当前页面是否满足前置条件、是否复用历史 locator、如何回退。",
        "**你不是必须按以下步骤执行**：",
        "- 每条 action 自带自己的 precondition（V2-P0-1），仅在准备复用该 action 时校验；",
        "- preferred_locator 是不含 index 的稳定 locator；observed_index 仅作历史位置参考，**不要与 preferred_locator 一起传入 click 期待联合过滤**（V2-P0-2）——只有你刚 verify 当前 page_info 的 [n] 与目标一致时才用 index；",
        "- 如果 preferred_locator 在当前页 NOT_FOUND / AMBIGUOUS，可回退到 label-only 语义点击、scroll_find_and_click；",
        "- soft_page_signature 是软提示，不匹配 ≠ 证据失效，仅提示重新感知；",
        "- 任何新感知都应优先使用 updated 当前页面事实，不要盲用历史 index。",
        "",
    ]

    # ── 入口（V4.0 V3-P1-1 修正：从 eff 读，pre_entry 是前置页面，entry.postcondition 是启动后到达）──
    entry = eff.get("entry")
    pre_entry = eff.get("pre_entry") or {}
    lines.append("### 入口")
    if entry and entry.get("launch_app_args"):
        args = entry["launch_app_args"]
        lines.append(
            f"- launch_app(package=\"{args.get('package','')}\", activity=\"{args.get('activity','')}\")"
        )
    else:
        lines.append("- 无（起始已在目标页）")
    # v4.0 V3-P1-1 修正语义：pre_entry 是 launch 前应处的页面（不是启动后）
    if pre_entry.get("expected_activity"):
        lines.append(f"  → 启动前 expected_activity: {pre_entry['expected_activity']}（强，作为 pre_entry）")
    if entry and entry.get("postcondition", {}).get("arrival_confirmed"):
        post = entry["postcondition"]
        lines.append(f"  → 启动后 observed: package={post.get('observed_package','')} activity={post.get('observed_activity','')}")
    lines.append("")

    # ── 历史成功步骤（V2-P0-1：每条 action 自带 precondition；V2-P0-2：preferred + observed 分离）──
    lines.append("### 历史成功步骤（每条带自己的 precondition，参考即可）")
    for i, ka in enumerate(eff.get("key_actions") or []):
        tool = ka.get("tool")
        # 每条 precondition（V2-P0-1）
        pre = ka.get("precondition") or {}
        if pre.get("expected_activity") or pre.get("required_anchors") or pre.get("soft_page_signature"):
            pre_bits = []
            if pre.get("expected_activity"):
                pre_bits.append(f"activity={pre['expected_activity']}(强)")
            for ra in pre.get("required_anchors") or []:
                pre_bits.append(f"anchor={ra.get('label','')!r}/{ra.get('role','')!r}(强)")
            if pre.get("soft_page_signature"):
                pre_bits.append(f"sig={pre['soft_page_signature']}(软)")
            lines.append(f"{i+1}. [前置] " + " | ".join(pre_bits))

        if tool == "click":
            # V2-P0-2：preferred_locator 不含 index
            loc = ka.get("preferred_locator") or {}
            loc_str = ", ".join(f"{k}={v!r}" for k, v in loc.items() if v not in ("", -1, None))
            obs_idx = ka.get("observed_index")
            # V2-P0-2：明确说"不要与 preferred_locator 同时传"
            idx_note = ""
            if obs_idx is not None and obs_idx != -1:
                idx_note = f"  // observed_index={obs_idx}（仅 LLM 刚 verify 当前 [n] 一致时使用，不要与上面 locator 同时传）"
            rt = ka.get("resolved_target") or {}
            et_str = ""
            if rt.get("role") or rt.get("rid") or rt.get("path"):
                et = ", ".join(f"{k}={v!r}" for k, v in rt.items() if v)
                et_str = f"  // resolved: {et}"
            lines.append(f"   click({loc_str}){idx_note}{et_str}")
            if ka.get("last_observation"):
                lines.append(f"   上次结果: {ka['last_observation']} [{ka.get('last_result','')}]")
        elif tool in ("assert_verification", "assert_page_contains", "assert_element_exists"):
            lines.append(f"   {tool}({ka.get('args') or ka.get('verify_key','')})")
            if ka.get("last_observation"):
                lines.append(f"   上次结果: {ka['last_observation']} [{ka.get('last_result','')}]")
        else:
            lines.append(f"   {tool}")

    # ── 验证证据（V2-P1-3：subjective 缺记录默认 unknown + review_required）──
    ve = eff.get("verification_evidence") or {}
    if ve:
        lines.append("")
        lines.append("### 验证证据（subjective=agent 报告，objective=PASS=权威 ground truth）")
        for vkey, info in ve.items():
            lines.append(f"- {vkey}: {info.get('item','')}")
            subj = info.get("subjective") or {}
            subj_res = subj.get("result", "unknown")
            subj_warn = " (review_required)" if subj.get("review_required") else ""
            lines.append(f"    - agent 报告: {subj_res}{subj_warn}")
            for obj in info.get("objective", []):
                lines.append(f"    - 客观: {obj['kind']}({obj['args']}) → {obj['result']}")

    # ── 环境指纹（V2-P1-4）──
    env_fp = eff.get("environment_fingerprint") or {}
    if env_fp:
        lines.append("")
        lines.append("### 环境指纹（仅供参考；执行时以当前 device 实际值为准）")
        for k, v in env_fp.items():
            if v:
                lines.append(f"- {k}: {v}")

    # ── override（V2-P1-6：plan 级 evidence_stale 提示）──
    ovr = exec_plan.get("override") or {}
    if ovr.get("evidence_stale"):
        lines.append("")
        lines.append("### ⚠️ evidence_stale：本 plan 被人为修改过（修订版本 " + str(ovr.get("revision", 1)) + "），历史 PASS 证据不再可信，建议重跑刷新。")

    return "\n".join(lines)
```

**关键变更（v3.0 相对 v2.0）**：
- 前置条件从全局 `preconditions.required_anchors` 改为**每条 action 自带 `precondition`**（V2-P0-1，多页面适配）
- locator 拆 `preferred_locator`（不含 index）+ `observed_index`（V2-P0-2，明确说"不要与 locator 同时传"）
- `pre_entry` 单独渲染入口前置（V2-P0-1）
- `environment_fingerprint` 渲染（V2-P1-4）
- `override.evidence_stale` 渲染 stale 警告（V2-P1-6）
- subjective 标注"review_required"（V2-P1-3）

**在 system prompt 拼装处插入**：
- 在 `agent_node` 第一条 `SystemMessage` 之后插入新 `SystemMessage(_render_replay_evidence_block(state.get("goal_description")))`

### 3.7 Step 7 (P0)：lineage 全链路传递

**目标**（team2 P0-5）：完整覆盖 WS rerun/run_case → orchestrator.start → state → reporter_node → record_test_run。

**改动清单**：

| 文件 | 行号（实测） | 改动 |
|---|---|---|
| `agents/state.py` | `TestState` 声明（v3.3 total=False） | 加 `_source_case_id: str` + `_execution_plan_revision: int` 字段 |
| `agents/orchestrator.py` | `start` (L187-) 和 `start_stream` (L386-) | 加 `source_case_id=None, execution_plan_revision=0` 形参；透传进 `initial_state["_source_case_id"]` / `["_execution_plan_revision"]`；建立 `initial_state` 前采集 `_environment_snapshot`（P1-3 闭合：run 开始快照） |
| `api/server.py` | WS `rerun` 分支 (L702-715) | `source_run_id=run_id if run else None`（不变），**加** `source_case_id=None`（report 不来自 case） |
| `api/server.py` | WS `run_case` 分支 (L723-744) | **改**：`source_run_id=None`，`source_case_id=case_id`（**拆分**）；`execution_plan_revision=_resolve_run_entry(case)["execution_plan_revision"]`（P1-1 共用 helper） |
| `api/test_cases_routes.py` | `run_test_case` (L108-139) | **改**：`source_run_id=None`，`source_case_id=case_id`；`execution_plan_revision=_resolve_run_entry(case)["execution_plan_revision"]`（P1-1 共用 helper） |
| `agents/nodes.py` | `reporter_node` 调 `record_test_run` (L855-867) | 加 `source_case_id=state.get("_source_case_id")` 和 `execution_plan_revision=state.get("_execution_plan_revision", 0)` 透传 |
| `data/relational.py` | `record_test_run` (L204-) | 加两形参（见 §3.4 改动） |
| `data/relational.py` | `list_test_runs` / `get_test_run` | SELECT 增两列 + COALESCE 兜底 |

**统一 helper（P1-1 闭合：HTTP/WS 两条入口共用，避免语义漂移）**：

```python
def _resolve_run_entry(case: dict) -> dict:
    """读出现有 case 后统一计算并下传 lineage（P1-1：revision 必须来自 effective）。"""
    goal = json.loads(case.get("goal_json") or "{}")
    plan = goal.get("execution_plan") or {}
    effective = plan.get("effective") or {}
    revision = int(effective.get("effective_revision", 0) or 0)   # 来自 effective，不再写默认值 0
    return {"source_case_id": case["id"], "execution_plan_revision": revision}
```

`api/test_cases_routes.py::run_test_case` 与 `api/server.py::WS run_case` **共用 `_resolve_run_entry`**（P1-1 闭合）：

```python
entry = _resolve_run_entry(case)
orchestrator.start(
    ...,
    source_case_id=entry["source_case_id"],
    execution_plan_revision=entry["execution_plan_revision"],   # ← P1-1：从 effective 传入，不再写默认值 0
)
```

HTTP 和 WS 都使用同一 helper，避免两条入口再次语义漂移（reporter 最终才会持续写正确 lineage，新字段才有价值）。

**关键**：**`source_case_id` 不在 `test_cases` 表**——`test_cases.source_run_id` 才是用例的"原始来源 run"（保留 v3.3 不变）。

### 3.8 Step 8 (P1)：UI 编辑 + 离散能力 quality + evidence_stale（v4.0 升级：base/patch/effective 契约）

**前端提交契约（v4.0 P0-3 闭合：客户端绝不提交 `base_evidence` / `effective`）**：

`TestCasePanel.vue` 编辑时**不提交完整 `execution_plan`**，只提交 `override_patch`：

```js
// 编辑弹窗只收集 patch，不碰 base_evidence / effective
const body = {
  override_patch: [
    { op: "replace", path: "/key_actions/0/preferred_locator/path_contains", value: "new_path" },
    { op: "add",    path: "/key_actions/2/precondition/required_anchors/-", value: { label: "WLAN", role: "switch_row" } }
  ],
  changed_paths: ["key_actions[0].preferred_locator.path_contains", "key_actions[2].precondition.required_anchors"],
  edited_by: currentUser,
};
await api.patch(`/api/test_cases/${caseId}`, body);
```

⚠️ **patch path 约定（全链路统一，P0-3 闭合）**：`/key_actions/...` 是**相对于 `base_evidence` 根路径**，
即等价于 `base_evidence.key_actions[0]...`，**不是** `/execution_plan/base_evidence/key_actions/...`。
服务端收到后 `deepcopy(base_evidence)` 再 apply patch，绝不直接改 DB 里的 `base_evidence`。

新增/编辑弹窗底部追加 `<div class="tcp-exec-plan">` 块（**只展示 `effective`，只读 `base_evidence`**）：
- 入口 + pre_entry：可空（来自 `effective`）
- key_actions：表格 + 增删改（**每条可折叠看自己的 precondition**）
- verification_evidence：折叠只读
- environment_fingerprint：可改（用户记录当前设备）—— 改动走 `override_patch`

**后端 `update_test_case`（v4.0 P0-3 闭合，详见 §3.5 派生入口）**：
1. 读 DB 中已持久化的 `execution_plan.base_evidence`；
2. 校验 patch 的 `op`（add/replace/remove）、`path`、`value` 合法，且 `path` 仅允许作用于 `base_evidence` 的允许前缀（`/entry/`、`/pre_entry/`、`/key_actions/`、`/verification_evidence/`、`/environment_fingerprint/`）；
3. **拒绝 body 中任何 `base_evidence` / `effective` 字段**（破坏"客户端不能写 base/effective"不变量）；
4. `deepcopy(base_evidence)` 后 `apply_json_patch`；
5. 写 `override.patch` / `override.changed_paths` / `override.evidence_stale=True` / `edited_at` / `edited_by`；
6. **重建并持久化 `effective = derive_effective_plan(...)`**（服务端唯一派生入口）；
7. **绝不原地修改 `base_evidence`**。

**后端 schema 校验**（V2-P1-5 / V2-P1-6 / V3-P1-4）：
- `validate_execution_plan(plan)` 校验 `schema_version == 4`、tool 白名单（`click/assert_verification/assert_page_contains/assert_element_exists/launch_app/report_done`）、locator 字段类型与长度；
- 原始 evidence 不可变：用户修改只产生 `override_patch` + `effective_revision` 自增；
- `override.revision += 1`、`override.evidence_stale = True`、`override.edited_at` / `edited_by` 记录；
- 只有新成功 run 才能为修改后的事实重新写入 PASS 证据。

**API 派生 quality 离散能力**（V2-P1-4/P1-5/P1-6 + v4.0 V3-P1-4 修正）：
```python
def _derive_plan_capabilities(case) -> dict:
    """离散能力标签，不计字段数量。
    v4.0 V3-P1-4：从 effective 读（服务端已 apply base+override+校验），不直接读 base/override。
    兼容 v3.0 老 case：fallback 读 base_evidence 或 ep 本身。"""
    goal = json.loads(case.get("goal_json") or "{}")
    ep = goal.get("execution_plan") or {}
    # v4.0 V3-P1-4：优先 effective，fallback base_evidence，最后 fallback ep 本身
    if isinstance(ep.get("effective"), dict):
        eff = ep["effective"]
    elif isinstance(ep.get("base_evidence"), dict):
        eff = ep["base_evidence"]
    else:
        eff = ep  # v3.0 老 case 兼容

    kas = eff.get("key_actions") or []
    entry = eff.get("entry") or {}
    env_fp = eff.get("environment_fingerprint") or {}
    # override 总是从 ep 顶层读（不在 effective 里）
    override = ep.get("override") or {}

    # 1. 入口是否带 verified activity（满足 V2-P1-2 + V3-P1-2 四重过滤的 entry）
    has_verified_entry = bool(
        entry
        and entry.get("launch_app_args", {}).get("activity")
        and entry.get("postcondition", {}).get("arrival_confirmed")
    )
    # 2. stable locator: 有 rid 或有 class+path+label 组合（V2-P1-5 修正：来自 preferred_locator）
    has_stable_locator = False
    index_only_locator = False
    for ka in kas:
        if ka.get("tool") != "click":
            continue
        loc = ka.get("preferred_locator") or {}
        # V2-P1-5：stable = rid OR (class+path+label)
        if loc.get("rid"):
            has_stable_locator = True
        elif loc.get("class_name") and loc.get("path_contains") and loc.get("label"):
            has_stable_locator = True
        # V2-P1-5：index_only = observed_index 有值 + 无任何 stable 组合
        if (
            ka.get("observed_index") is not None
            and not loc.get("rid")
            and not (loc.get("class_name") and loc.get("path_contains") and loc.get("label"))
        ):
            index_only_locator = True
    # 3. 客观断言（M4 PASS=权威）
    has_objective_evidence = any(
        info.get("objective") for info in (eff.get("verification_evidence") or {}).values()
    )
    # 4. V2-P1-4：环境前置条件是否记录
    has_environment_precondition = bool(env_fp.get("package") and env_fp.get("version_code"))
    # 5. V2-P1-6：plan 级 evidence_stale
    evidence_stale = bool(override.get("evidence_stale"))

    return {
        "has_replay_evidence": bool(ep),
        "key_actions_count": len(kas),
        "has_verified_entry": has_verified_entry,
        "has_stable_locator": has_stable_locator,
        "index_only_locator": index_only_locator,
        "has_objective_evidence": has_objective_evidence,
        "has_environment_precondition": has_environment_precondition,   # V2-P1-4 重命名
        "evidence_stale": evidence_stale,                              # V2-P1-6 改 plan 级
        "effective_revision": (ep.get("effective") or {}).get("effective_revision", 0),  # 来自 effective（P1-2 闭合，不再读 flat extraction_revision）
        "override_revision": override.get("revision", 0),              # 额外：可看 override 版本
    }
```

**前端列表行**用 `case.plan_capabilities` 显示 7 个小图标：
- `entry ✓/✗` `loc stable/index-only` `obj ✓/✗` `env ✓/✗` `stale ✓/✗` `rev N`

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

## 4. 改动文件清单（v4.0，v3 review 4 P0 + 5 P1 + 8 测试全闭合）

| 文件 | 改动类型 | 关键改动 | 关联 review |
|---|---|---|---|
| `agents/llm_runtime.py` (`_tools_node` 内) | MODIFY | 工具执行前显式 `page_sig_before = page_sig_once`（**V3-P0-1**）；记录 page_before/after_activity/package（**V3-P0-2/V3-P1-2**）；`result_evidence` 写 entry 顶层（V2-P0-3） | R6 / T2-1 / T1-1 / **V3-P0-1** / **V3-P0-2** / **V3-P1-2** |
| `agents/orchestrator.py` (`_build_display_steps`) | MODIFY | 扩字段：tool_input/status_code/page_before/after_signature/activity/package/result_evidence/match_mode/fallback_used/resolved_target | R6 / T1-1 |
| `tools/click.py` (L413) | MODIFY | 新增 `_make_click_success` 统一成功路径构造器（**V3-P0-4 闭合**：所有 click 成功 return 走它）；evidence 含 resolved_label/role/rid/class/path | T2-7 / V2-P0-3 / **V3-P0-4** |
| `tools/verify.py` (`assert_verification` L188, `report_done` L356) | MODIFY | 改 `make_result` 契约（V2-P0-4）；evidence 含 verification_key/reported_result/review_required（V3-P1-5 状态层次） | V2-P0-4 / **V3-P1-5** |
| `tools/device_ops.py` (`launch_app` L241) | MODIFY | 复用 `make_result` 契约 + 通用 settle + observed_package + package_matched/activity_matched（**V3-P1-2 闭合**） | T2-2 / T2-3 / V2-P1-1 / **V3-P1-2** |
| `tools/__init__.py`（或新文件） | MODIFY | 新增 `_settle_after_action` 通用工具 | T2-3 |
| `agents/loop_control.py` (L14) | 无改 | `_build_page_signature` 不动（仅作"软提示"使用；activity 用独立结构化字段） | T2-4 |
| `data/relational.py` (`_ensure_tables` L62) | MODIFY | `test_runs` 加 `source_case_id`/`execution_plan_revision`/`environment_json`（**V3-P1-3**）；`test_cases` 删 `plan_quality_json` | R13 / R14 / T2-5 / **V3-P1-3** |
| `data/relational.py` (`record_test_run` L204) | MODIFY | 加 `source_case_id`/`execution_plan_revision`/`environment_json` 形参 | T2-5 / **V3-P1-3** |
| `data/relational.py` (`create_test_case` L605) | MODIFY | 调用 `_extract_replay_evidence` 产 `base_evidence`；`update_test_case` 加 schema 校验 + override patch 派生 `effective`（**V3-P1-4**） | T2-5 / **V3-P1-4** |
| `data/relational.py` (`list/get_test_run`) | MODIFY | SELECT 增 `environment_json` 列 + COALESCE 兜底 | T2-5 / **V3-P1-3** |
| `api/test_cases_routes.py` (`_extract_replay_evidence`) | MODIFY | 完整版提炼（per-action precondition；preferred_locator/observed_index；`run.get("steps")` 优先 V3-P0-3；entry 四重过滤 V3-P1-2；precondition 方向修正 V3-P0-2） | R1-R12 / T2-8/9 / V2-P0-1 / V2-P1-2 / V2-P1-3 / **V3-P0-2** / **V3-P0-3** / **V3-P1-1** |
| `api/test_cases_routes.py` (`_derive_plan_capabilities`) | MODIFY | 从 `effective` 读（V3-P1-4 兼容 v3.0 fallback `base_evidence` 或 ep 本身） | T2-11 / **V3-P1-4** |
| `agents/nodes.py` (`_render_replay_evidence_block`) | MODIFY | 从 `effective` 读（V3-P1-4 兼容 v3.0 fallback）；per-action precondition 渲染；preferred/observed 分离 + "不要与 locator 同时传"；entry.postcondition + pre_entry 修正语义 | R1/R2/R7 / T2-4 / **V3-P0-2** / **V3-P1-1** / **V3-P1-4** |
| `agents/nodes.py` (`reporter_node` L855) | MODIFY | 调 `record_test_run` 时透传 source_case_id/execution_plan_revision/environment_json=`state["_environment_snapshot"]`（V3-P1-3 + P1-3 闭合：快照在 run 开始采集，reporter 直接持久化初始快照） | T2-5 / **V3-P1-3** / **P1-3** |
| `api/server.py` (WS `rerun` L702, `run_case` L723) | MODIFY | `source_case_id` 拆分（v2 review） | T2-5 |
| `agents/orchestrator.py` (`start` L187, `start_stream` L386) | MODIFY | 加 `source_case_id`/`execution_plan_revision` 形参 + initial_state 透传 | T2-5 |
| `agents/state.py` (`TestState`) | MODIFY | 加 `_source_case_id: str` + `_execution_plan_revision: int` | T2-5 |
| `frontend/spa/src/components/TestCasePanel.vue` | MODIFY | 只提交 `override_patch`（不直接改 `base_evidence`/`effective`）；展示 `effective` + 离散能力 tag + evidence_stale 显示 | R14 / T2-10 / T2-11 / **V3-P1-4** / **P0-3** |
| `frontend/spa/src/App.vue` (`saveAsCase` L639) | 无改 | 兼容 v3.3 body | — |
| `tests/test_structured_step_logging.py` | NEW | **5 + 2 + 1 个**（V3 review 增 V3-T1/T2/T5 + **P0-1 闭环单测**）：所有工具落库 tool_input；assert_verification/report_done 契约 status_code；parse_evidence 写 resolved_* 到 entry 顶层；page_sig_before 显式赋值；每种 click 成功路径 status_code=OK；**P0-1 闭环**：mock `ctx.device.current_app()` 在 `t.invoke` 前后返回不同 Activity（A→B），断言 `page_before_activity==A` 且 `page_after_activity==B`（验证 before 真发生在工具调用前） | R6 / T2-1 / T1-1 / V2-P0-3 / V2-P0-4 / **V3-P0-1** / **V3-P0-4** / **P0-1** |
| `tests/test_extract_replay_evidence.py` | NEW | **7 + 4 个**（V3 review 增 V3-T3/T4/T7）：多页面、缺 verification_json、UNSPECIFIED 过滤、package 不匹配不入选、无 entry 仍返回、precondition 方向（page-before vs page-after）、`get_test_run` 返回 steps 仍可提炼、source run 环境缺失展示"未记录" | R1-R12 / T2-8/9 / V2-P0-1 / V2-P1-2 / V2-P1-3 / **V3-P0-2** / **V3-P0-3** / **V3-P1-3** |
| `tests/test_replay_evidence_prompt.py` | NEW | **4 个**（V2 review 增 1 个，V2-T6） | R1/R2/R7 / T2-4 / V2-P0-2 |
| `tests/test_launch_app_contract.py` | NEW | **3 + 1 个**（V3 review 增 V3-T8）：launch_app 真实 tool_input + 契约 status_code + observed_package 校验 | T2-2 / T2-3 / V2-P1-1 / **V3-P1-2** |
| `tests/test_lineage_full_chain.py` | NEW | 4 个 lineage 全链路单测（WS rerun/run_case/orchestrator/reporter） | T2-5 |
| `tests/test_plan_capabilities.py` | NEW | **5 + 1 个**（V3 review 增 V3-T6）：index_only_locator 修正条件、plan 级 evidence_stale、has_environment_precondition、override patch 不可修改 base_evidence | T2-11 / V2-P1-4 / V2-P1-5 / V2-P1-6 / **V3-P1-4** |
| `docs/用例管理_复用计划重跑_20260721.md` | MODIFY | §3.4 / §3.5 加 v3.0 子节 | — |
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
| **1** | `llm_runtime.py` 扩字段 + `_build_display_steps` + `_extract_status_code` | **6 个单测**通过（含 P0-1 闭环：Activity A→B 时 `page_before_activity==A` 且 `page_after_activity==B`）；dev 库跑一次 run 后 inspect steps_json 含 `tool_input`/`status_code` | 是 |
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
9. **UI 修改 = `override_patch` + `override.revision` 自增 + `effective_revision` 服务端单调递增 + `evidence_stale=true`** —— 方案选**采纳**（team2 P1-5 + v4 P0-2/P0-3/P1-2）。**默认同意**。
10. **quality 改离散能力标签**（6 个）—— 方案选**采纳**（team2 P1-6）。**默认同意**。
11. **locator 全量 6 字段保留**，click 工具自处理歧义 —— 方案选**采纳**（team1 #2）。**默认同意**。
12. **R4.4 NOT_FOUND→RAG 提示** —— 方案选**不采纳**（§0 红线：禁止针对特例加 patch）。**默认不采纳**。
13. **R15 token 通用基础设施优化 / R16 click 质量 6 维指标** —— 方案选**列为独立 P1/P2 跟进项**，不混入本 PR。**默认同意**。
14. **Step 9 A/B 测试 case 选择** —— 至少 3 个不同类型；具体 case 列表待定。**默认同意**。
15. **回填存量 v3.3 用例（无 execution_plan）？** —— 方案选**不回填**。**默认此方案**。
16. **§0.3 105055 高效归因** —— 改"RAG=0 不代表 cache 跨 run 复用；105055 实际无法分离归因"。**默认同意**（team2 P1-1 采纳）。
17. **v2 review P0-1 拆 precondition 分段**（per-action）—— 方案选**采纳**（V2-P0-1）。**默认同意**。
18. **v2 review P0-2 拆 preferred_locator + observed_index** —— 方案选**采纳**（V2-P0-2）。**默认同意**。
19. **v2 review P0-3 resolved_target 显式落 entry 顶层**（不再回退 tool_input）—— 方案选**采纳**（V2-P0-3）。**默认同意**。
20. **v2 review P0-4 assert_verification / report_done 改 make_result 契约** —— 方案选**采纳**（V2-P0-4）。**默认同意**。
21. **v2 review P0-5 page_*_signature 写入 key_action 字典** —— 方案选**采纳**（V2-P0-5）。**默认同意**。
22. **v2 review P1-1 observed_activity 改读 device.current_app()** —— 方案选**采纳**（V2-P1-1）。**默认同意**。
23. **v2 review P1-2 entry 三重过滤（package + status + arrival_confirmed）** —— 方案选**采纳**（V2-P1-2）。**默认同意**。
24. **v2 review P1-3 subjective 缺记录默认 `unknown` + `review_required=True`** —— 方案选**采纳**（V2-P1-3）。**默认同意**。
25. **v2 review P1-4 environment_compatible 改 has_environment_precondition + env fingerprint** —— 方案选**采纳**（V2-P1-4）。**默认同意**。
26. **v2 review P1-5 index_only_locator 修正条件**（observed_index + 无 stable 组合）—— 方案选**采纳**（V2-P1-5）。**默认同意**。
27. **v2 review P1-6 evidence_stale 改 plan 级**（override 块顶层）—— 方案选**采纳**（V2-P1-6）。**默认同意**。
28. **v2 review 8 个新单测补充** —— 全部纳入 §4 测试清单（V2-T1~T10）。**默认同意**。
29. **v3 review P0-1 page_sig_before 显式赋值** —— 方案选**采纳**（V3-P0-1）。**默认同意**。
30. **v3 review P0-2 precondition 方向修正**（precondition 从 page_before，postcondition 从 page_after；activity 作独立结构化字段）—— 方案选**采纳**（V3-P0-2）。**默认同意**。
31. **v3 review P0-3 提炼器兼容 `run.get("steps")`** —— 方案选**采纳**（V3-P0-3）。**默认同意**。
32. **v3 review P0-4 click 成功路径统一构造器 `_make_click_success`** —— 方案选**采纳**（V3-P0-4）。**默认同意**。
33. **v3 review P1-1 pre_entry 语义修正**（pre_entry 从 launch 的 page_before_activity 取；entry.postcondition 启动后到达事实）—— 方案选**采纳**（V3-P1-1）。**默认同意**。
34. **v3 review P1-2 arrival_confirmed 加 observed_package** —— 方案选**采纳**（V3-P1-2）。**默认同意**。
35. **v3 review P1-3 `test_runs.environment_json` 列新增**（dev-reset 口径；提炼只读 source run 快照）—— 方案选**采纳**（V3-P1-3）。**默认同意**。
36. **v3 review P1-4 execution_plan 拆 base_evidence + override + effective** —— 方案选**采纳**（V3-P1-4）。**默认同意**。
37. **v3 review P1-5 assert_verification 状态层次分离**（status_code=OK + reported_result + objective）—— 方案选**采纳**（V3-P1-5）。**默认同意**。
38. **v3 review 8 个新单测补充** —— 全部纳入 §4 测试清单（V3-T1~T8）。**默认同意**。

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

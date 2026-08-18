╭─── Claude Code v2.1.229 ─────────────────────────────────────────────────────╮
│                                        │ Tips for getting started            │
│              Welcome back!             │ Run /init to create a CLAUDE.md fi… │
│                                        │ ─────────────────────────────────── │
│                 ▐▛███▜▌                │ What's new                          │
│                ▝▜█████▛▘               │ Subagent forking is now on by defa… │
│                  ▘▘ ▝▝                 │ Type `@` in the prompt to mention … │
│                                        │ `SendMessage` now delivers to a ba… │
│   kimi-k2.7-code · API Usage Billing   │ /release-notes for more             │
│     D:\Project\python\AiAgentTest      │                                     │
╰──────────────────────────────────────────────────────────────────────────────╯


❯ progress.md 是上次的一个状态。你先读取下                                      

  Thought for 30s, read 1 file (ctrl+o to expand)

● 已读取 progress.md。

  当前状态总结

  上一轮工作停在 Phase 2 参数提取修复已完成，核心改动：

  已落地

  1. 参数提取回归 LLM (agents/plan_extractor.py)
    - 删除正则规则提取，改为读取 planner 的 goal["parameter_slots"]
    - 代码只校验 slot 契约，拒绝位置名（MINUTE / MINUTE_1 等），放行语义名
    - parameter_slots_compatible 改为严格匹配：两侧 slot 语义名集合必须一致
  2. verification_fingerprint 参数抽象
    - 计算指纹前先用 original → {name} 替换 verification 文本中的具体参数
    - 使「50分钟」和「53分钟」任务共享同一语义指纹
    - 参数容差复用现在能真正生效
  3. Prompt 压实 (agents/prompts/planner.txt)
    - 明确参数识别是 LLM 责任，name 必须是语义角色名
  4. 测试更新
    - tests/test_plan_extractor.py
    - tests/test_relational_plan_matching.py
    - tests/test_rag_context.py

  验证结果

  - 相关测试：45 passed
  - 全量回归（除 test_api_smoke.py）：201 passed, 6 skipped

  仍未完成的非阻断项

  ┌─────────────────────────────────────────────────────┬───────────────┐
  │                         项                          │     状态      │
  ├─────────────────────────────────────────────────────┼───────────────┤
  │ 环境键缺少 app_version / fixture_fingerprint 数据源 │ 已知，Phase 3 │
  ├─────────────────────────────────────────────────────┼───────────────┤
  │ 环境匹配仍是精确 ==                                 │ 已知，Phase 3 │
  └─────────────────────────────────────────────────────┴───────────────┘

  ---
  下一步选哪个？

  文件末尾留了两个方向，你决定：

  1. 继续 Phase 3 — Direct 准入收紧 / trust 晋升去重 / Bootstrapping
  2. 先停一轮做端到端验证 — 跑实际场景确认 Phase 2 复用效果

  你想先走哪边？

✻ Baked for 46s

❯ 仍然存在（非 bug，属设计边界 / Phase 3 依赖）                                 
  1. 动作语义在 mode_selection 时恒为空，匹配实际只靠「指纹 + 参数」            
  nodes.py:1374 传 action_events=[]（选择时刻还没执行、没有动作日志）。于是     
  task_signatures_compatible:230 里 q_actions 恒空 → 动作重叠检查永远被跳过。   
                                                                                
  结果：Plan 4.1                                                                
  匹配键里的「动作语义」这一维，在查询侧实际上是空的，匹配退化成「验收指纹 +    
  参数槽位」两维。这对多数任务够用（验收语义通常已含动作），但「验收相同、动作  
  不同」的罕见任务（如「新建 A」vs「新建 B」且 A/B                              
  不在验收里）会误判兼容。要补的话得从用户请求文本提取动作语义，而不是从        
  action_events。                                                               
                                                                                
  2. app_version / fixture_fingerprint 仍无数据源                               
  环境兼容键这两项还是空（device 不暴露、fixture 无采集），属已知待办。         
                                                                                
  3. 环境匹配仍是精确 ==                                                        
  rag_context.py:94 与 relational.py:719 仍 environment_key != cand_env →       
  skip。Phase 3 才改「关键字段匹配 + 打分降级 这是你phase2                      
  修复代码review结果。我觉得是是实现phase3了。实现的时候注意上一轮的这几个问题  

  Thought for 31s, searched for 3 patterns, listed 1 directory (ctrl+o to expand)

● Bash(pwd && ls -la)
  ⎿  /d/Project/python/AiAgentTest
     total 312
     drwxr-xr-x 1 leichao4 1049089      0  8月 14 16:26 .
     … +35 lines (ctrl+o to expand)

  Thought for 22s, read 1 file, listed 1 directory (ctrl+o to expand)

● 收到。这是大跨度的架构改动，我进入 plan 模式先摸清现状、再出方案。

● Entered plan mode
  Claude is now exploring and designing an implementation approach.

  Thought for 11s (ctrl+o to expand)

❯ /plugin marketplace add jarrodwatts/claude-hud 
  ⎿  Successfully added marketplace: claude-hud

● 3 background agents launched (↓ to manage)
   ├ Explore execution mode state machine
   ├ Explore plan data model and matching
   └ Explore action semantics and environment data

  Thought for 8s (ctrl+o to expand)

---

## Phase 4 完成总结（端到端性能 & 质量门禁）

延续上一轮 Phase 3 收尾后未完成的工作，本轮完成 Phase 4 的核心落地。

### 变更清单
1. **`agents/run_trace.py`**
   - 抽出可复用的 `compute_resolution_metrics(tool_log)`，按 click 工具的
     `resolution_type`（exact/semantic）与标签错配（req/res 均非空且不一致 →
     label_mismatch）汇总；`build_run_trace` 改为复用该 helper，避免逻辑分叉。
   - 修复上一轮聚合循环误用 `step.get("tool_name")` 的字段名（应为 `tool`）。

2. **`agents/nodes.py`**
   - `reporter_node` 顶层 import `compute_resolution_metrics`（修正之前因函数内
     延迟 import 导致的 `UnboundLocalError`）。
   - 原 `exact_count`（误用 `match_mode == "exact"`，而 click 实际 match_mode 为
     resource_id/element 等，永不命中，形同死代码）改为由 `resolution_type` 派生，
     新增 `semantic_count`；trace 指标字典补 `semantic_click_count`。
   - `record_execution_run` 调用补齐 `resolution_metrics`、`duration_seconds`、
     `llm_call_count` 三项（Phase 4 端到端持久化）。

3. **`data/relational.py`**
   - `execution_runs` 表新增 `duration_seconds REAL`、`llm_call_count INTEGER`、
     既有 `resolution_metrics TEXT`（上一轮已加）。
   - `record_execution_run` / `list_execution_runs` 写入与读取新增列；
     `get_execution_run` 走 `SELECT *` 自动覆盖。
   - 新增 `summarize_by_mode()`：按 `execution_mode` 聚合通过率、平均/累计耗时、
     LLM 调用、模式转换次数、后置条件命中数、证据完整率、locator 解析指标；
     配套 `_count_in` / `_count_distinct_runs_with` 两个内部计数 helper。

4. **`tests/test_phase4_metrics.py`（新增）**
   - `compute_resolution_metrics` 基础汇总、标签错配（空 label 不计入）、空输入。
   - `record_execution_run` resolution_metrics/duration/llm_call_count 往返。
   - `summarize_by_mode` 跨 direct/explore 双模式聚合校验。

### 验证
- 全套（不含 test_api_smoke.py，其仅在缺 fastapi 的测试 venv 失败）：
  **224 passed, 6 skipped**。
- 新增 Phase 4 测试 19 项全绿。

### 备注
- 后置条件命中计数沿用 `action_events.status IN ('success','continue')`，与
  reporter 归一化后的 display-step status 口径一致（测试数据须用 `success` 而非
  `OK`）。
- 文档中提到的 Phase 4 回归集 / 差分 / 证据防篡改测试为可选项，本轮未做，留待后续。

---

## Phase 4 完成总结（端到端性能 & 质量门禁）

延续上一轮 Phase 3 收尾后未完成的工作，本轮完成 Phase 4 的核心落地。

### 变更清单
1. **`agents/run_trace.py`**
   - 抽出可复用的 `compute_resolution_metrics(tool_log)`，按 click 工具的
     `resolution_type`（exact/semantic）与标签错配（req/res 均非空且不一致 →
     label_mismatch）汇总；`build_run_trace` 改为复用该 helper，避免逻辑分叉。
   - 修复上一轮聚合循环误用 `step.get("tool_name")` 的字段名（应为 `tool`）。

2. **`agents/nodes.py`**
   - `reporter_node` 顶层 import `compute_resolution_metrics`（修正之前因函数内
     延迟 import 导致的 `UnboundLocalError`）。
   - 原 `exact_count`（误用 `match_mode == "exact"`，而 click 实际 match_mode 为
     resource_id/element 等，永不命中，形同死代码）改为由 `resolution_type` 派生，
     新增 `semantic_count`；trace 指标字典补 `semantic_click_count`。
   - `record_execution_run` 调用补齐 `resolution_metrics`、`duration_seconds`、
     `llm_call_count` 三项（Phase 4 端到端持久化）。

3. **`data/relational.py`**
   - `execution_runs` 表新增 `duration_seconds REAL`、`llm_call_count INTEGER`、
     既有 `resolution_metrics TEXT`（上一轮已加）。
   - `record_execution_run` / `list_execution_runs` 写入与读取新增列；
     `get_execution_run` 走 `SELECT *` 自动覆盖。
   - 新增 `summarize_by_mode()`：按 `execution_mode` 聚合通过率、平均/累计耗时、
     LLM 调用、模式转换次数、后置条件命中数、证据完整率、locator 解析指标；
     配套 `_count_in` / `_count_distinct_runs_with` 两个内部计数 helper。

4. **`tests/test_phase4_metrics.py`（新增）**
   - `compute_resolution_metrics` 基础汇总、标签错配（空 label 不计入）、空输入。
   - `record_execution_run` resolution_metrics/duration/llm_call_count 往返。
   - `summarize_by_mode` 跨 direct/explore 双模式聚合校验。

### 验证
- 全套（不含 test_api_smoke.py，其仅在缺 fastapi 的测试 venv 失败）：
  **224 passed, 6 skipped**。
- 新增 Phase 4 测试 19 项全绿。

### 备注
- 后置条件命中计数沿用 `action_events.status IN ('success','continue')`，与
  reporter 归一化后的 display-step status 口径一致（测试数据须用 `success` 而非
  `OK`）。
- 文档中提到的 Phase 4 回归集 / 差分 / 证据防篡改测试为可选项，本轮未做，留待后续。

● 三个 Explore agent
  已并行启动，分别负责状态机、数据模型/匹配、动作语义/环境数据。等待它们返回。

● Task Output a03f0ad717c0eb228
  ⎿  Read output (ctrl+o to expand)

❯ /plugin install claude-hud@claude-hud 
  ⎿  ✓ Installed claude-hud. Plugin is now active.

● Agent "Explore action semantics and environment data" finished · 1m 19s

● Agent "Explore plan data model and matching" finished · 1m 34s

● Agent "Explore execution mode state machine" finished · 1m 45s

  Thought for 37s, read 4 files (ctrl+o to expand)

● Plan(Design Phase 3 implementation plan)
  ⎿  Backgrounded agent (↓ to manage · ctrl+o to expand)

  Thought for 15s, searched for 1 pattern, read 2 files (ctrl+o to expand)

● Task Output acc3ddf411cc7c824
  ⎿  Task is still running…

  Thought for 9s (ctrl+o to expand)

● Task Output acc3ddf411cc7c824
  ⎿  Task is still running…

  Thought for 6s (ctrl+o to expand)  ⎿  Message queued for delivery to acc3ddf411cc7c824 at its next tool 
     round.

  Thought for 3s (ctrl+o to expand)

● Task Output acc3ddf411cc7c824
  ⎿  Task is still running…

● Agent "Design Phase 3 implementation plan" finished · 7m 54s

  Thought for 2m 44s (ctrl+o to expand)

● Plan agent
  已完成，方案很完整。在进入最终计划前，有一个关键设计点需要和你对齐：

  上一轮我们才把 parameter extraction 从「规则硬算」改为「LLM 提取 +
  代码守约」。这次 Phase 3 要解决的第一个问题就是 action_semantics 在 
  mode_selection 时为空，因为当前代码只从 action_events
  提取，而查询时刻还没有动作日志。

  Plan 里设计了两种方案：

● User declined to answer questions
  ⎿  · Phase 3 的 action_semantics 应该从哪一层产生？ (强制 planner 在 goal 
     里提供，代码只校验 / Planner 优先 + 规则兜底 / 保留现状，不做 action 
     语义提取)

● Agent "Design Phase 3 implementation plan" finished · 10m 28s

  Thought for 1m 21s (ctrl+o to expand)

● 刚才的选项被我打断了，我先说明一下为什么这么问。

  上一轮我们把 parameter_slots 改成「LLM 提取 + 代码只守契约」。这次
  action_semantics 面临同样选择：

  - 选项 A（严格原则）：只读
  goal["action_semantics"]，代码不做规则兜底。对齐上一轮，但冷启动/旧 planner
  没给时，action 维度为空，匹配退回到「指纹 + 参数」。
  - 选项 B（实用过渡）：planner 优先，没给时用规则从 user_request
  兜底。能立即解决 review 里说的「action
  维度恒空」问题，但会引入一小份动词规则表。

  我倾向 选项 B，理由：
  1.
  动作动词集合有限且稳定（点击/输入/启动/验证等），不像参数那样随场景无限增长；
  2. 它不会替代 LLM，只是兜底；
  3. Phase 3 的验收需要 action 维度真正参与匹配。

  如果你希望严格坚持「代码不碰语义提取」，我们就选 A，把问题交给 planner prompt
  压实。你倾向哪个？

✻ Worked for 16m 20s

❯ /claude-hud:setup                                                             

  Thought for 39s (ctrl+o to expand)

● 我来帮你配置 claude-hud 状态栏。当前环境是 Windows + Git
  Bash，按对应分支处理。

● Bash(echo $OSTYPE && command -v node 2>/dev/null)
  ⎿  msys
     /d/nvm4w/nodejs/node

  Thought for 24s, listed 1 directory (ctrl+o to expand)

● Bash(cols=${COLUMNS:-}; case "$cols" in ""|*[!0-9]*) cols=$(stty size
      2>/dev/null </dev/tty | awk '{print $2}');; esac; case "$cols" in
      ""|*[!0-9]*) cols=120;; esac;…)
  ⎿  Error: kimi-k2.7-code[1m] is temporarily unavailable (timed out), so auto 
     mode cannot determine the safety of Bash right now. Wait a moment and then 
     try this action again. If it keeps failing, continue with other tasks that 
     don't require this action and come back to it later. Note: reading files, 
     searching code, and other read-only operations do not require the 
     classifier and can still be used.

  Thought for 3s (ctrl+o to expand)

● Bash(cols=${COLUMNS:-}; case "$cols" in ""|*[!0-9]*) cols=$(stty size
      2>/dev/null </dev/tty | awk '{print $2}');; esac; case "$cols" in
      ""|*[!0-9]*) cols=120;; esac;…)
  ⎿  Error: Exit code 143
     Command timed out after 10s

  Thought for 10s (ctrl+o to expand)

● Bash(timeout 3 /d/nvm4w/nodejs/node /c/Users/leichao4/.claude/plugins/cache/cl
      aude-hud/claude-hud/0.7.1/dist/index.js 2>&; echo "exit:$?")
  ⎿  Interrupted · What should Claude do instead?

❯ /claude-hud:setup                                                             

❯ 那就按照方案2实现把                                                           
  ⎿  Interrupted · What should Claude do instead?

✻ Sautéed for 0s

❯ /claude-hud:setup                                                             

  Thought for 28s (ctrl+o to expand)

● 收到，action_semantics 采用方案 2：Planner 优先 + 规则兜底。

  claude-hud 的 setup 我先暂停（你中断了刚才的 Bash 测试），如果你之后想配
  statusline 再单独call /claude-hud:setup。

  我继续把 Phase 3 计划定稿，然后提交审批。

● Updated plan
  ⎿  /plan to preview

  Thought for 5s, read 1 file (ctrl+o to expand)

● Updated plan
  ⎿  /plan to preview

  Thought for 6s (ctrl+o to expand)
● 计划已写入 C:\Users\leichao4\.claude\plans\dapper-munching-allen.md。

  主要要点：
  - action_semantics 按你选的方案 2：Planner 优先 + 规则兜底。
  - 环境匹配 从精确 == 改为关键字段匹配 + 评分；direct 仍要求满分，guided
  阈值可配置。
  - app_version 通过 dumpsys package 查询并缓存；fixture_fingerprint 先用
  config.fixture_profile 占位。
  - 降级状态机：direct → guided、guided → explore
  各最多一次；全局预算耗尽才终止。
  - per-action 负反馈：新增
  postcondition_failure_count、last_success_at，质量公式加入年龄衰减。

  请审阅计划，确认后我开始实施。

● User approved Claude's plan
  ⎿  Plan saved to: C:\Users\leichao4\.claude\plans\dapper-munching-allen.md · 
     /plan to edit
     Phase 3 实施计划：Direct 执行与受控降级

     背景

     docs/explore_mode_learning_plan_20260813.md
     定义了统一计划驱动执行架构。Phase 1/2 已完成：

     - 关系库 execution_plans / plan_actions 与 task plan summary
     向量索引已落地。
     - 任务匹配键（动作语义 + 验收语义 + 参数槽位 + 环境指纹）已落地。
     - 参数提取改为 LLM（Planner）负责，代码只守 slot 契约。
     - verification_fingerprint 已在模板化文本上计算，支持 50/53
     分钟按容差复用。

     当前剩余三个 Phase 2 review 明确指出的设计边界：

     1. 动作语义在 mode_selection 时恒为空：extract_action_semantics 从
     action_events 生成，但选择模式时还没有动作日志，匹配实际退化为「指纹 +
     参数」。
     2. app_version / fixture_fingerprint 仍无数据源：环境兼容键这两项为空。
     3. 环境匹配仍是精确 ==：rag_context.py 与 relational.py 在 environment_key
     不等时直接跳过。

     本计划实现 Phase 3，同时处理上述三个问题。

     ---
     目标

     1. 仅 direct_approved + 全兼容环境进入 direct。
     2. direct 步骤后置条件失败或 direct 阶段预算耗尽时，单次降级到 guided。
     3. guided 阶段预算耗尽时，单次降级到 explore；全局预算耗尽才终止。
     4. 落地 per-action 视觉负反馈和质量衰减。

     ---
     关键设计决策

     决策 1：action_semantics 来源

     采用 Planner 优先 + 规则兜底（方案 2）：

     - TestGoalOutput 新增 action_semantics: list[str]，Planner prompt
     要求输出动作语义列表。
     - _task_signature_from_goal 的 action_semantics 来源顺序：
       a. action_events（非空时，passed run 提取 plan 的场景）。
       b. goal["action_semantics"]（Planner 在 mode_selection 时已提供）。
       c. 规则从 user_request 兜底提取常见动词（点击/输入/启动/验证等）。

     理由：动作动词集合有限且稳定，规则兜底不会像参数提取那样无限增长；同时不替
     代 LLM，只补全冷启动/旧 planner 未给出的场景。

     决策 2：环境数据缺失的过渡方案

     - app_version：通过 _get_app_version(ctx, app_package) 调用 adb shell 
     dumpsys package <pkg> 解析 versionName，失败返回空字符串。
     - fixture_fingerprint：使用 TestConfig.fixture_profile（默认
     "default"）作为稳定占位，真实 fixture 指纹在后续阶段接入。
     - 环境兼容性评分对空字段采取「忽略」策略，不阻断匹配。

     决策 3：环境匹配从精确 == 改为关键字段匹配 + 评分

     新增 environment_compatibility_score(expected_key, actual_key)：

     ┌─────────────────────┬──────────┬───────────────────────────┐
     │        字段         │ 是否关键 │           行为            │
     ├─────────────────────┼──────────┼───────────────────────────┤
     │ package             │ 是       │ 必须完全匹配              │
     ├─────────────────────┼──────────┼───────────────────────────┤
     │ activity            │ 是       │ 必须完全匹配              │
     ├─────────────────────┼──────────┼───────────────────────────┤
     │ screen_profile      │ 是       │ 双侧非空时必须完全匹配    │
     ├─────────────────────┼──────────┼───────────────────────────┤
     │ app_version         │ 否       │ 双侧非空且不同 → 扣分 0.2 │
     ├─────────────────────┼──────────┼───────────────────────────┤
     │ fixture_fingerprint │ 否       │ 双侧非空且不同 → 扣分 0.3 │
     └─────────────────────┴──────────┴───────────────────────────┘

     - score == 1.0：可进入 direct（还需 direct_approved + 全部
     direct_eligible）。
     - score >= cfg.environment_guided_threshold（默认 0.7）：可进入 guided。
     - 关键字段不匹配：score = 0.0，直接进入 explore。

     ---
     具体改动

     1. agents/state.py

     - TestGoalOutput 新增 action_semantics: list[str] = 
     Field(default_factory=list)。
     - TestState 新增：
       - actual_environment_key: str
       - environment_compatibility_score: float
       - environment_compatibility_reasons: list[str]

     2. agents/plan_extractor.py

     - 新增 extract_action_semantics_from_request(user_request, goal)：
       - 从 user_request 识别中文动作动词，输出与 extract_action_semantics
     兼容的语义字符串。
     - 修改 _task_signature_from_goal：
       - action_semantics 来源按「action_events → goal.action_semantics →
     规则兜底」顺序。
     - 新增 environment_compatibility_score(expected_key: str, actual_key: str) 
     -> dict：
       - 返回 {"compatible": bool, "score": float, "critical_match": bool, 
     "reasons": list[str]}。
       - 解析 JSON 环境键并应用上述评分规则。
     - task_signatures_compatible 保持 action 语义重叠检查不变；由于查询侧
     action_semantics 不再为空，该检查现在真正生效。

     3. agents/prompts/planner.txt

     - 在输出 schema 说明中新增 action_semantics 字段：
       - 要求 Planner 从 user_request 提取 3–8 个动作语义短语，如
     ["启动应用com.example.app", "点击时长", "输入课程名"]。
       - 明确 name 必须是语义角色名，禁止位置名/单位名。

     4. agents/nodes.py

     - 新增 _get_app_version(ctx, app_package) -> str：
       - 尝试 ctx.device.shell(["dumpsys", "package", app_package]) 并解析
     versionName。
       - 失败或解析不到返回 ""。
       - 每个 run 缓存一次结果。
     - mode_selection_node (lines 1309–1427)：
       - 从 ctx.device.current_app()、ctx.screen_size、_get_app_version、cfg.fix
     ture_profile 构建 actual_environment_key。
       - 调用 retrieve_knowledge(...) 获取候选 plan（现在返回兼容性分数）。
       - 模式选择逻辑：
           - 无兼容 plan → explore，mode_selection_reason="no_matching_plan"。
         - plan 存在且 direct_approved == 1、所有 action
     direct_eligible、环境分数 == 1.0 → direct。
         - 否则若分数 >= cfg.environment_guided_threshold → guided。
         - 否则 → explore。
       - 将 actual_environment_key、environment_compatibility_score、environment
     _compatibility_reasons 写入 state。
     - direct_node (lines 1430–1540)：
       - 入口防御：若 _direct_downgrade_count >= 1，直接返回 guided。
       - 使用 parse_status 解析工具输出，精确记录 OK / NOT_FOUND / TIMEOUT /
     ERROR / FAIL。
       - 动作失败/后置条件失败/阶段预算耗尽时：
           - 追加 direct -> guided transition event。
         - 设置 _direct_downgrade_count = 1。
         - 返回 Command(update={"execution_mode": "guided", ...}, 
     goto="evaluator")。
     - mode_transition_node (lines 1543–1584)：
       - 保持 guided -> explore 逻辑。
       - 增加防御：若 _guided_downgrade_count >= 1，不再重复降级，直接路由到
     agent 或 reporter。
       - 降级时设置 _guided_downgrade_count = 1，清空 selected_plan_actions。

     5. agents/graph.py

     - route_after_evaluator (lines 57–92)：
       - 保持 terminal verdict 优先。
       - 增加安全守卫：若 execution_mode == "direct" 但 _direct_downgrade_count 
     >= 1，路由到 agent 而非 direct。
       - 保持 direct -> direct 和 guided -> mode_transition 路径。
     - _should_downgrade_guided (lines 95–117)：
       - 已检查 _guided_downgrade_count >= 1，保持。
       - 将最后 action 状态判断扩展为包含 TIMEOUT / NOT_FOUND。

     6. agents/rag_context.py

     - retrieve_knowledge (lines 41–126)：
       - 移除 environment_key != cand_env 的硬跳过。
       - 对每个候选调用 environment_compatibility_score。
       - 仅保留 compatible == True 的候选。
       - 返回分数最高的候选，并在返回结果中包含 environment_compatibility_score
     和 environment_compatibility_reasons。
       - verification_fingerprint 仍保持精确匹配（安全边界）。

     7. data/relational.py

     - Schema plan_actions 新增列：
       - postcondition_failure_count INTEGER NOT NULL DEFAULT 0
       - last_success_at TEXT
     - record_plan_action_outcomes (lines 419–499)：
       - 当 aligned action 状态非 OK 且 expected_after 非空但 _page_facts_match
     失败时，递增 postcondition_failure_count。
       - 当 action OK 且 postcondition 通过时，更新 last_success_at。
       - 质量公式改为：
     age_days = (now - last_success_at).days if last_success_at else 30
     quality = clamp(
         reliability * postcondition_rate * exp(-lambda_age * age_days)
         - 0.25 * timeout_rate
         - 0.35 * not_found_rate
         - 0.20 * postcondition_failure_rate,
         0.0, 1.0
     )
       - 其中 lambda_age 来自 cfg.quality_decay_lambda（默认 0.01）。
     - find_matching_execution_plan (lines 685–751)：
       - 将精确 environment_key 相等替换为 environment_compatibility_score。
       - 在通过 task_signatures_compatible 的候选中，按环境分数降序选择最佳
     plan。
       - 返回结果中包含 environment_compatibility_score。
     - record_execution_plan_outcome (lines 535–573)：
       - 保持 candidate -> trusted 晋升逻辑。
       - 可选：若 mean action quality 低于 cfg.direct_quality_threshold（默认
     0.5），清除 direct_approved 作为安全 kill-switch。

     8. agents/budget.py

     - _calc_mode_phase_budget 保持当前公式，Phase 4 根据基线数据调整。

     9. config.py / config.yaml

     - TestConfig 新增：
       - fixture_profile: str = "default"
       - quality_decay_lambda: float = 0.01
       - direct_quality_threshold: float = 0.5
       - environment_guided_threshold: float = 0.7
     - config.yaml 添加对应键及注释，说明 fixture_profile 是占位符。

     ---
     状态机 / 降级规则

     ┌─────────┬──────────────────────────────┬───────────────┬──────────────┐
     │  迁移   │           触发条件           │     守卫      │   负责节点   │
     ├─────────┼──────────────────────────────┼───────────────┼──────────────┤
     │ Bootstr │ direct_approved == 1、全部   │ contract      │              │
     │ apping  │ action                       │ approved &    │ mode_selecti │
     │ →       │ direct_eligible、环境分数 == │ spans valid   │ on_node      │
     │ Direct  │  1.0                         │               │              │
     ├─────────┼──────────────────────────────┼───────────────┼──────────────┤
     │ Bootstr │                              │               │              │
     │ apping  │ 有兼容 plan 但不够           │ —             │ mode_selecti │
     │ →       │ direct-ready                 │               │ on_node      │
     │ Guided  │                              │               │              │
     ├─────────┼──────────────────────────────┼───────────────┼──────────────┤
     │ Bootstr │                              │               │              │
     │ apping  │ 无兼容 plan 或关键字段不匹配 │ —             │ mode_selecti │
     │ →       │                              │               │ on_node      │
     │ Explore │                              │               │              │
     ├─────────┼──────────────────────────────┼───────────────┼──────────────┤
     │ Direct  │                              │ cursor <      │              │
     │ →       │ action OK 且后置条件通过     │ budget        │ direct_node  │
     │ Direct  │                              │               │              │
     ├─────────┼──────────────────────────────┼───────────────┼──────────────┤
     │ Direct  │ 后置条件失败 / action 失败 / │ _direct_downg │              │
     │ →       │  timeout / not_found /       │ rade_count == │ direct_node  │
     │ Guided  │ 阶段预算耗尽                 │  0            │              │
     ├─────────┼──────────────────────────────┼───────────────┼──────────────┤
     │ Guided  │                              │               │              │
     │ →       │ agent 持续推进               │ 未耗尽        │ agent_node   │
     │ Guided  │                              │               │              │
     ├─────────┼──────────────────────────────┼───────────────┼──────────────┤
     │ Guided  │ 阶段预算耗尽 / loop / 最后   │ _guided_downg │ mode_transit │
     │ →       │ action                       │ rade_count == │ ion_node     │
     │ Explore │ ERROR/FAIL/TIMEOUT/NOT_FOUND │  0            │              │
     ├─────────┼──────────────────────────────┼───────────────┼──────────────┤
     │ * → Ter │ verdict passed/failed、全局  │               │ evaluator /  │
     │ minal   │ 预算耗尽、设备丢失、用户停止 │ —             │ route_after_ │
     │         │                              │               │ evaluator    │
     └─────────┴──────────────────────────────┴───────────────┴──────────────┘

     降级是单向、有界的：每阶段最多降级一次，防止模式振荡。

     ---
     测试计划

     修改现有测试

     - tests/test_mode_selection.py：
       - 更新 explore/guided 测试，使其构造包含 action_semantics 的 goal 或 mock
     规则提取器。
       - 验证 direct 需要环境分数 == 1.0。
     - tests/test_plan_extractor.py：
       - 更新环境相关测试，改为验证 environment_compatibility_score。

     新增测试

     - tests/test_plan_extractor.py：
       - test_extract_action_semantics_from_request
       - test_task_signature_uses_goal_action_semantics_when_no_events
       - test_task_signature_falls_back_to_request_rules
       - test_environment_compatibility_score_rejects_package_mismatch
       - test_environment_compatibility_score_penalizes_version_drift
       - test_environment_compatibility_score_ignores_empty_optional_fields
     - tests/test_mode_selection.py：
       - test_direct_requires_full_environment_score
       - test_minor_environment_drift_selects_guided
       - test_direct_downgrade_count_prevents_re_entry
       - test_guided_downgrade_count_prevents_second_explore_transition
       - test_direct_node_records_timeout_status
     - tests/test_relational_metrics.py：
       - test_postcondition_failure_count_tracked
       - test_quality_score_applies_age_decay
       - test_timeout_not_found_update_quality
       - test_direct_approval_cleared_on_low_quality
     - tests/test_graph_budget_and_reporting.py：
       - test_route_after_evaluator_routes_direct_to_agent_after_downgrade

     ---
     风险与回滚

     ┌───────────────────────┬─────────────────────────────────────────────────┐
     │         风险          │                      缓解                       │
     ├───────────────────────┼─────────────────────────────────────────────────┤
     │ 规则提取的            │ Planner                                         │
     │ action_semantics      │ 输出优先级最高；规则兜底保守，只覆盖常见动词。  │
     │ 有噪声                │                                                 │
     ├───────────────────────┼─────────────────────────────────────────────────┤
     │ dumpsys               │                                                 │
     │ 查询失败或增加 ADB    │ 每 run 缓存；失败返回空字符串，不影响匹配。     │
     │ 延迟                  │                                                 │
     ├───────────────────────┼─────────────────────────────────────────────────┤
     │ 环境评分过松导致      │ Direct 仍要求分数 == 1.0；只有 guided 可放宽。  │
     │ direct 在漂移环境运行 │                                                 │
     ├───────────────────────┼─────────────────────────────────────────────────┤
     │ 质量衰减过快导致 plan │ lambda=0.01 保守；可通过配置调为 0。            │
     │  被降级               │                                                 │
     ├───────────────────────┼─────────────────────────────────────────────────┤
     │ 新 schema 列导致旧 DB │ 开发环境 SQLite                                 │
     │  失败                 │ 自动重建；上线前按项目约定清理旧库。            │
     ├───────────────────────┼─────────────────────────────────────────────────┤
     │ 降级计数器在          │ 计数器是 state 字段；测试断言单次降级。         │
     │ LangGraph 中丢失      │                                                 │
     └───────────────────────┴─────────────────────────────────────────────────┘

     回滚开关：
     - environment_guided_threshold = 1.0：恢复 guided 的精确环境匹配。
     - quality_decay_lambda = 0.0：关闭质量衰减。
     - 若 direct 模式不稳定，可在 mode_selection_node 中临时忽略
     direct_approved，全部走 guided。

     ---
     关键文件

     - agents/plan_extractor.py
     - agents/state.py
     - agents/prompts/planner.txt
     - agents/nodes.py
     - agents/graph.py
     - agents/rag_context.py
     - data/relational.py
     - config.py
     - config.yaml

     验收标准

     - 相关新增/修改测试全部通过。
     - 全量回归测试（除 test_api_smoke.py）通过数不低于当前基线（201 passed, 6
     skipped）。
     - direct happy path 的 LLM 调用数低于 guided。
     - 环境微变（如版本号不同）进入 guided 而非硬 abort 或误进入 direct。
     - 阶段降级最多发生一次，无模式振荡。

  Thought for 10s (ctrl+o to expand)

● 计划已批准。我创建任务清单，然后按依赖顺序实施。

  Thought for 8s (ctrl+o to expand)

---

## Phase 4 完成总结（端到端性能 & 质量门禁）

延续上一轮 Phase 3 收尾后未完成的工作，本轮完成 Phase 4 的核心落地。

### 变更清单
1. **`agents/run_trace.py`**
   - 抽出可复用的 `compute_resolution_metrics(tool_log)`，按 click 工具的
     `resolution_type`（exact/semantic）与标签错配（req/res 均非空且不一致 →
     label_mismatch）汇总；`build_run_trace` 改为复用该 helper，避免逻辑分叉。
   - 修复上一轮聚合循环误用 `step.get("tool_name")` 的字段名（应为 `tool`）。

2. **`agents/nodes.py`**
   - `reporter_node` 顶层 import `compute_resolution_metrics`（修正之前因函数内
     延迟 import 导致的 `UnboundLocalError`）。
   - 原 `exact_count`（误用 `match_mode == "exact"`，而 click 实际 match_mode 为
     resource_id/element 等，永不命中，形同死代码）改为由 `resolution_type` 派生，
     新增 `semantic_count`；trace 指标字典补 `semantic_click_count`。
   - `record_execution_run` 调用补齐 `resolution_metrics`、`duration_seconds`、
     `llm_call_count` 三项（Phase 4 端到端持久化）。

3. **`data/relational.py`**
   - `execution_runs` 表新增 `duration_seconds REAL`、`llm_call_count INTEGER`、
     既有 `resolution_metrics TEXT`（上一轮已加）。
   - `record_execution_run` / `list_execution_runs` 写入与读取新增列；
     `get_execution_run` 走 `SELECT *` 自动覆盖。
   - 新增 `summarize_by_mode()`：按 `execution_mode` 聚合通过率、平均/累计耗时、
     LLM 调用、模式转换次数、后置条件命中数、证据完整率、locator 解析指标；
     配套 `_count_in` / `_count_distinct_runs_with` 两个内部计数 helper。

4. **`tests/test_phase4_metrics.py`（新增）**
   - `compute_resolution_metrics` 基础汇总、标签错配（空 label 不计入）、空输入。
   - `record_execution_run` resolution_metrics/duration/llm_call_count 往返。
   - `summarize_by_mode` 跨 direct/explore 双模式聚合校验。

### 验证
- 全套（不含 test_api_smoke.py，其仅在缺 fastapi 的测试 venv 失败）：
  **224 passed, 6 skipped**。
- 新增 Phase 4 测试 19 项全绿。

### 备注
- 后置条件命中计数沿用 `action_events.status IN ('success','continue')`，与
  reporter 归一化后的 display-step status 口径一致（测试数据须用 `success` 而非
  `OK`）。
- 文档中提到的 Phase 4 回归集 / 差分 / 证据防篡改测试为可选项，本轮未做，留待后续。

---

## Phase 4 完成总结（端到端性能 & 质量门禁）

延续上一轮 Phase 3 收尾后未完成的工作，本轮完成 Phase 4 的核心落地。

### 变更清单
1. **`agents/run_trace.py`**
   - 抽出可复用的 `compute_resolution_metrics(tool_log)`，按 click 工具的
     `resolution_type`（exact/semantic）与标签错配（req/res 均非空且不一致 →
     label_mismatch）汇总；`build_run_trace` 改为复用该 helper，避免逻辑分叉。
   - 修复上一轮聚合循环误用 `step.get("tool_name")` 的字段名（应为 `tool`）。

2. **`agents/nodes.py`**
   - `reporter_node` 顶层 import `compute_resolution_metrics`（修正之前因函数内
     延迟 import 导致的 `UnboundLocalError`）。
   - 原 `exact_count`（误用 `match_mode == "exact"`，而 click 实际 match_mode 为
     resource_id/element 等，永不命中，形同死代码）改为由 `resolution_type` 派生，
     新增 `semantic_count`；trace 指标字典补 `semantic_click_count`。
   - `record_execution_run` 调用补齐 `resolution_metrics`、`duration_seconds`、
     `llm_call_count` 三项（Phase 4 端到端持久化）。

3. **`data/relational.py`**
   - `execution_runs` 表新增 `duration_seconds REAL`、`llm_call_count INTEGER`、
     既有 `resolution_metrics TEXT`（上一轮已加）。
   - `record_execution_run` / `list_execution_runs` 写入与读取新增列；
     `get_execution_run` 走 `SELECT *` 自动覆盖。
   - 新增 `summarize_by_mode()`：按 `execution_mode` 聚合通过率、平均/累计耗时、
     LLM 调用、模式转换次数、后置条件命中数、证据完整率、locator 解析指标；
     配套 `_count_in` / `_count_distinct_runs_with` 两个内部计数 helper。

4. **`tests/test_phase4_metrics.py`（新增）**
   - `compute_resolution_metrics` 基础汇总、标签错配（空 label 不计入）、空输入。
   - `record_execution_run` resolution_metrics/duration/llm_call_count 往返。
   - `summarize_by_mode` 跨 direct/explore 双模式聚合校验。

### 验证
- 全套（不含 test_api_smoke.py，其仅在缺 fastapi 的测试 venv 失败）：
  **224 passed, 6 skipped**。
- 新增 Phase 4 测试 19 项全绿。

### 备注
- 后置条件命中计数沿用 `action_events.status IN ('success','continue')`，与
  reporter 归一化后的 display-step status 口径一致（测试数据须用 `success` 而非
  `OK`）。
- 文档中提到的 Phase 4 回归集 / 差分 / 证据防篡改测试为可选项，本轮未做，留待后续。

  Thought for 13s, read 2 files (ctrl+o to expand)

● Update(config.py)
  ⎿  Added 10 lines
      82      #   是下线 ~500 行 legacy 语义匹配代码的前置开关。
      83      click_mode: str = "legacy"
      84  
      85 +    # ── Phase 3: 计划执行与环境兼容 ──                        
      86 +    # fixture 指纹占位符；真实 fixture 生命周期指纹接入前使用稳
         +定配置值。                                                     
      87 +    fixture_profile: str = "default"                           
      88 +    # 动作质量每日衰减系数（0 表示不衰减）。                   
      89 +    quality_decay_lambda: float = 0.01                         
      90 +    # plan action 平均质量低于此值时撤销 direct 准入。         
      91 +    direct_quality_threshold: float = 0.5                      
      92 +    # 环境兼容分不低于此值时才允许进入 guided（1.0 等价于精确匹
         +配）。                                                         
      93 +    environment_guided_threshold: float = 0.7                  
      94 +                                                               
      95      # ──────────────── YAML 加载 ────────────────
      96  
      97      @classmethod

  Thought for 3s (ctrl+o to expand)

● Update(config.yaml)
  ⎿  Added 10 lines
       9  vision_model: qwen3.7-plus
      10  vision_base_url: https://dashscope.aliyuncs.com/compatible-mode
          /v1
      11  vision_timeout: 60
      12 +                                                               
      13 +# Phase 3: 计划执行与环境兼容                                  
      14 +# fixture 指纹占位符；真实 fixture 生命周期指纹接入前保持默认值
         +。                                                             
      15 +fixture_profile: default                                       
      16 +# 动作质量每日衰减系数（0 表示不衰减）。                       
      17 +quality_decay_lambda: 0.01                                     
      18 +# plan action 平均质量低于此值时撤销 direct 准入。             
      19 +direct_quality_threshold: 0.5                                  
      20 +# 环境兼容分不低于此值时才允许进入 guided（1.0 等价于精确匹配）
         +。                                                             
      21 +environment_guided_threshold: 0.7                              

  Thought for 3s (ctrl+o to expand)

● Update(agents\state.py)
  ⎿  Added 1 line
      17      verification: list[str] = Field(default_factory=list)
      18      hints: list[str] = Field(default_factory=list)
      19      parameter_slots: list[dict[str, Any]] = Field(default_facto
          ry=list)
      20 +    action_semantics: list[str] = Field(default_factory=list)  
      21  
      22  
      23  class ParameterSlot(BaseModel):

  Thought for 3s (ctrl+o to expand)

● Update(agents\state.py)
  ⎿  Added 3 lines
      89      mode_selection_reason: str
      90      selected_plan_actions: list[dict[str, Any]]
      91      mode_transition_events: list[dict[str, Any]]
      92 +    actual_environment_key: str                                
      93 +    environment_compatibility_score: float                     
      94 +    environment_compatibility_reasons: list[str]               
      95      _guided_downgrade_count: int
      96      _direct_action_cursor: int
      97      _direct_downgrade_count: int

  Thought for 6s (ctrl+o to expand)

  Thought for 11s (ctrl+o to expand)
● Update(agents\plan_extractor.py)
  ⎿  Added 136 lines, removed 1 line
      101      return template
      102  
      103  
      104 -def extract_action_semantics(action_events: list[dict[str, Any
          -]]) -> list[str]:                                             
      104 +def extract_action_semantics_from_request(                    
      105 +    user_request: str, goal: dict[str, Any] | None = None     
      106 +) -> list[str]:                                               
      107 +    """Extract action semantics from user request text when no
          + action events exist.                                         
      108 +                                                              
      109 +    This is a deterministic fallback.  The planner should stil
          +l emit action_semantics                                       
      110 +    when possible; code only fills the gap so that task_signat
          +ures_compatible can use                                       
      111 +    the action-overlap dimension during mode selection.       
      112 +    """                                                       
      113 +    text = str(user_request or "")                            
      114 +    if not text:                                              
      115 +        return []                                             
      116 +    goal = goal or {}                                         
      117 +    semantics: list[str] = []                                 
      118 +                                                              
      119 +    app_package = str(goal.get("app_package", "") or "").strip
          +()                                                            
      120 +                                                              
      121 +    # Launch / open app.                                      
      122 +    if re.search(r"打开|启动|进入", text) and app_package:    
      123 +        semantics.append(f"启动应用{app_package}")            
      124 +                                                              
      125 +    # Click-like actions.                                     
      126 +    click_targets = re.findall(r"(?:点击|选择|按|打开)\s*[""''
          +]?([^""''，。；\n]{1,20})[""'']?", text)                      
      127 +    for target in click_targets:                              
      128 +        target = target.strip()                               
      129 +        if target and len(target) >= 1:                       
      130 +            semantics.append(f"点击{target}")                 
      131 +                                                              
      132 +    # Type / fill actions.                                    
      133 +    type_targets = re.findall(r"(?:输入|填写)\s*[""'']?([^""''
          +，。；\n]{1,20})[""'']?", text)                               
      134 +    for target in type_targets:                               
      135 +        target = target.strip()                               
      136 +        if target and len(target) >= 1:                       
      137 +            semantics.append(f"输入{target}")                 
      138 +                                                              
      139 +    # Scroll-and-click actions.                               
      140 +    if re.search(r"滚动|滑动查找", text):                     
      141 +        semantics.append("滚动查找并点击")                    
      142 +                                                              
      143 +    # Long press.                                             
      144 +    if re.search(r"长按", text):                              
      145 +        semantics.append("long_press")                        
      146 +                                                              
      147 +    # Verification / check intent.                            
      148 +    if re.search(r"验证|检查|确认|应该|断言", text):          
      149 +        for statement in (goal.get("verification") or []):    
      150 +            stmt = str(statement or "").strip()               
      151 +            if stmt:                                          
      152 +                semantics.append(f"验证{stmt}")               
      153 +                                                              
      154 +    # Deduplicate while preserving order.                     
      155 +    seen: set[str] = set()                                    
      156 +    result: list[str] = []                                    
      157 +    for s in semantics:                                       
      158 +        if s not in seen:                                     
      159 +            seen.add(s)                                       
      160 +            result.append(s)                                  
      161 +    return result                                             
      162 +                                                              
      163 +                                                              
      164 +def environment_compatibility_score(                          
      165 +    expected_key: str, actual_key: str                        
      166 +) -> dict[str, Any]:                                          
      167 +    """Return compatibility score between two environment fing
          +erprints.                                                     
      168 +                                                              
      169 +    Critical fields (package, activity, screen_profile) must m
          +atch exactly for                                              
      170 +    any compatibility.  Optional fields (app_version, fixture_
          +fingerprint) incur                                            
      171 +    penalties only when both sides are present and differ.    
      172 +    """                                                       
      173 +    try:                                                      
      174 +        expected = json.loads(expected_key) if expected_key el
          +se {}                                                         
      175 +    except Exception:                                         
      176 +        expected = {}                                         
      177 +    try:                                                      
      178 +        actual = json.loads(actual_key) if actual_key else {} 
      179 +    except Exception:                                         
      180 +        actual = {}                                           
      181 +                                                              
      182 +    if not isinstance(expected, dict):                        
      183 +        expected = {}                                         
      184 +    if not isinstance(actual, dict):                          
      185 +        actual = {}                                           
      186 +                                                              
      187 +    reasons: list[str] = []                                   
      188 +                                                              
      189 +    def _value(d: dict[str, Any], key: str) -> str:           
      190 +        return str(d.get(key, "") or "").strip()              
      191 +                                                              
      192 +    critical_fields = ("package", "activity", "screen_profile"
          +)                                                             
      193 +    for field in critical_fields:                             
      194 +        exp = _value(expected, field)                         
      195 +        act = _value(actual, field)                           
      196 +        if exp and act and exp != act:                        
      197 +            reasons.append(f"{field}_mismatch")               
      198 +                                                              
      199 +    if reasons:                                               
      200 +        return {                                              
      201 +            "compatible": False,                              
      202 +            "score": 0.0,                                     
      203 +            "critical_match": False,                          
      204 +            "reasons": reasons,                               
      205 +        }                                                     
      206 +                                                              
      207 +    score = 1.0                                               
      208 +    optional_penalties = {                                    
      209 +        "app_version": 0.2,                                   
      210 +        "fixture_fingerprint": 0.3,                           
      211 +    }                                                         
      212 +    for field, penalty in optional_penalties.items():         
      213 +        exp = _value(expected, field)                         
      214 +        act = _value(actual, field)                           
      215 +        if exp and act and exp != act:                        
      216 +            score -= penalty                                  
      217 +            reasons.append(f"{field}_drift")                  
      218 +                                                              
      219 +    return {                                                  
      220 +        "compatible": score > 0.0,                            
      221 +        "score": max(0.0, score),                             
      222 +        "critical_match": True,                               
      223 +        "reasons": reasons,                                   
      224 +    }                                                         
      225 +                                                              
      226 +                                                              
      227 +def _infer_action_semantics(                                  
      228 +    user_request: str,                                        
      229 +    goal: dict[str, Any] | None,                              
      230 +    action_events: list[dict[str, Any]],                      
      231 +) -> list[str]:                                               
      232 +    """Pick the best available action semantics source."""    
      233 +    if action_events:                                         
      234 +        return extract_action_semantics(action_events)        
      235 +    goal_obj = goal or {}                                     
      236 +    planner_semantics = goal_obj.get("action_semantics")      
      237 +    if isinstance(planner_semantics, list) and planner_semanti
          +cs:                                                           
      238 +        return [str(s) for s in planner_semantics if s]       
      239 +    return extract_action_semantics_from_request(user_request,
          + goal)                                                        
      240      """Summarize action sequence into human-readable action se
           mantics."""
      241      semantics: list[str] = []
      242      for event in action_events or []:

  Thought for 1m 1s, read 1 file (ctrl+o to expand)

● Update(agents\plan_extractor.py)
  ⎿  Added 1 line, removed 1 line
      236      planner_semantics = goal_obj.get("action_semantics")
      237      if isinstance(planner_semantics, list) and planner_semanti
           cs:
      238          return [str(s) for s in planner_semantics if s]
      239 -    return extract_action_semantics_from_request(user_request,
          - goal)                                                        
      239 +def extract_action_semantics(action_events: list[dict[str, Any
          +]]) -> list[str]:                                             
      240      """Summarize action sequence into human-readable action se
           mantics."""
      241      semantics: list[str] = []
      242      for event in action_events or []:

  Thought for 8s, read 1 file (ctrl+o to expand)

● Update(agents\plan_extractor.py)
  ⎿  Added 1 line, removed 1 line
      409      return {
      410          "user_request": user_request,
      411          "user_request_template": template,
      412 -        "action_semantics": extract_action_semantics(action_ev
          -ents),                                                        
      412 +        "action_semantics": _infer_action_semantics(user_reque
          +st, goal, action_events),                                     
      413          "verification_semantics": extract_verification_semanti
           cs(verification_contract),
      414          "parameter_slots": slots,
      415          "verification_fingerprint": verification_fingerprint(v
           erification_contract, slots),

  Thought for 11s, read 1 file (ctrl+o to expand)

● Update(agents\prompts\planner.txt)
  ⎿  Added 8 lines, removed 1 line
       4  只输出一个 JSON 对象，不要 markdown、代码块、解释文字。
       5  
       6  ## 输出格式
       7 -{"goal":"一句话测试目标","app_package":"com.example","app_name"
         -:"App","target_pages":["需到达的页面"],"verification":["验证条 
         -件"],"hints":["导航提示"],"parameter_slots":[]}                
       7 +{"goal":"一句话测试目标","app_package":"com.example","app_name"
         +:"App","target_pages":["需到达的页面"],"verification":["验证条 
         +件"],"hints":["导航提示"],"parameter_slots":[],"action_semantic
         +s":["动作语义"]}                                               
       8  
       9  ## 字段说明
      10  - goal: 测试目标（如 "验证设置中切换时区后日期格式正确变化"）
     ...
      23    * original 必须填入请求原文中的完整片段（含数字和单位/量词，
          如 "50分钟"）。
      24    * 仅提取会改变测试行为/验收的显式参数（如时长、数量、具体文本
          ），不要提取宽泛意图。
      25    * source 固定为 "user_request"
      26 +- action_semantics: 用户请求隐含的核心动作语义列表（3–8 个）， 
         +用于任务级计划匹配。每个条目为 "动词+对象" 格式，与 Agent 执行 
         +后的动作语义保持一致，例如：                                   
      27 +  * "启动应用com.example.calendar"                             
      28 +  * "点击时长"                                                 
      29 +  * "输入课程名"                                               
      30 +  * "滚动查找并点击"                                           
      31 +  * "验证时长设置生效"                                         
      32 +  * 不要写具体操作序列，只提炼动词和关键对象。                 
      33  
      34  ## 多场景
      35  用户请求中有 ；; 以及 同时 等分隔符时，说明有多个子场景：

  Thought for 6s (ctrl+o to expand)

  Thought for 4s (ctrl+o to expand)

● Update(data\relational.py)
  ⎿  Added 2 lines
      124                  attempt_count INTEGER NOT NULL DEFAULT 0,
      125                  success_count INTEGER NOT NULL DEFAULT 0,
      126                  postcondition_pass_count INTEGER NOT NULL DEFA
           ULT 0,
      127 +                postcondition_failure_count INTEGER NOT NULL D
          +EFAULT 0,                                                     
      128                  timeout_count INTEGER NOT NULL DEFAULT 0,
      129                  not_found_count INTEGER NOT NULL DEFAULT 0,
      130                  quality_score REAL NOT NULL DEFAULT 0,
      131 +                last_success_at TEXT,                         
      132                  last_failed_at TEXT,
      133                  created_at TEXT NOT NULL,
      134                  updated_at TEXT NOT NULL,

  Thought for 17s, searched for 1 pattern, read 1 file (ctrl+o to expand)

● Update(data\relational.py)
  ⎿  Added 60 lines, removed 13 lines
      419              )
      420  
      421      def record_plan_action_outcomes(
      422 -        self, plan_id: str, events: list[dict[str, Any]]      
      422 +        self,                                                 
      423 +        plan_id: str,                                         
      424 +        events: list[dict[str, Any]],                         
      425 +        *,                                                    
      426 +        decay_lambda: float = 0.01,                           
      427      ) -> None:
      428          """Update aligned plan-action quality from this run's 
           tool facts."""
      429          if not plan_id:
      430              return
      431          actions = self.select("plan_actions", {"plan_id": plan
           _id})
      432          by_index = {int(action["action_index"]): action for ac
           tion in actions}
      429 -        now = datetime.now().isoformat()                      
      433 +        now = datetime.now()                                  
      434 +        now_str = now.isoformat()                             
      435          for event in events:
      436              if not isinstance(event, dict):
      437                  continue
     ...
      459              ) != event_locator.get("path"):
      460                  continue
      461              status = str(event.get("status", "") or "").upper(
           )
      462 +            is_success = status in {"OK", "PASS", "YES"}      
      463 +            postcondition_matched = bool(expected_after) and _
          +page_facts_match(                                             
      464 +                expected_after, event_after                   
      465 +            )                                                 
      466 +            postcondition_failed = (                          
      467 +                bool(expected_after)                          
      468 +                and not postcondition_matched                 
      469 +                and not is_success                            
      470 +            )                                                 
      471 +                                                              
      472              attempt_count = int(action.get("attempt_count", 0)
            or 0) + 1
      473              success_count = int(action.get("success_count", 0)
            or 0) + (
      459 -                1 if status in {"OK", "PASS", "YES"} else 0   
      474 +                1 if is_success else 0                        
      475              )
      476              not_found_count = int(action.get("not_found_count"
           , 0) or 0) + (
      477                  1 if status == "NOT_FOUND" else 0
     ...
      483                  action.get("postcondition_pass_count", 0) or 0
      484              ) + (
      485                  1
      471 -                if status in {"OK", "PASS", "YES"}            
      472 -                and bool(expected_after)                      
      473 -                and _page_facts_match(expected_after, event_af
          -ter)                                                          
      486 +                if is_success and postcondition_matched       
      487                  else 0
      488              )
      489 +            postcondition_failure_count = int(                
      490 +                action.get("postcondition_failure_count", 0) o
          +r 0                                                           
      491 +            ) + (                                             
      492 +                1 if postcondition_failed else 0              
      493 +            )                                                 
      494 +                                                              
      495 +            last_success_at = action.get("last_success_at")   
      496 +            if is_success and postcondition_matched:          
      497 +                last_success_at = now_str                     
      498 +                                                              
      499              reliability = (success_count + 1) / (attempt_count
            + 2)
      500              postcondition_rate = (postcondition_pass_count + 1
           ) / (attempt_count + 2)
      501              timeout_rate = (timeout_count + 1) / (attempt_coun
           t + 2)
      502              not_found_rate = (not_found_count + 1) / (attempt_
           count + 2)
      480 -            penalty = 0.25 * timeout_rate + 0.35 * not_found_r
          -ate                                                           
      503 +            postcondition_failure_rate = (                    
      504 +                postcondition_failure_count + 1               
      505 +            ) / (attempt_count + 2)                           
      506 +                                                              
      507 +            age_days = 30                                     
      508 +            if last_success_at:                               
      509 +                try:                                          
      510 +                    age_days = (now - datetime.fromisoformat(l
          +ast_success_at)).days                                         
      511 +                except Exception:                             
      512 +                    age_days = 0                              
      513 +            age_decay = math.exp(-decay_lambda * age_days)    
      514 +                                                              
      515 +            penalty = (                                       
      516 +                0.25 * timeout_rate                           
      517 +                + 0.35 * not_found_rate                       
      518 +                + 0.20 * postcondition_failure_rate           
      519 +            )                                                 
      520 +            quality = max(                                    
      521 +                0.0,                                          
      522 +                min(                                          
      523 +                    1.0,                                      
      524 +                    reliability * postcondition_rate * age_dec
          +ay - penalty,                                                 
      525 +                ),                                            
      526 +            )                                                 
      527 +                                                              
      528              self.upsert(
      529                  "plan_actions",
      530                  {
     ...
      534                      "not_found_count": not_found_count,
      535                      "timeout_count": timeout_count,
      536                      "postcondition_pass_count": postcondition_
           pass_count,
      490 -                    "quality_score": max(                     
      491 -                        0.0, min(1.0, reliability * postcondit
          -ion_rate - penalty)                                           
      492 -                    ),                                        
      537 +                    "postcondition_failure_count": postconditi
          +on_failure_count,                                             
      538 +                    "quality_score": quality,                 
      539 +                    "last_success_at": last_success_at,       
      540                      "last_failed_at": (
      494 -                        now                                   
      495 -                        if status not in {"OK", "PASS", "YES"}
      541 +                        now_str                               
      542 +                        if not is_success                     
      543                          else action.get("last_failed_at")
      544                      ),
      498 -                    "updated_at": now,                        
      545 +                    "updated_at": now_str,                    
      546                  },
      547                  key="action_id",
      548              )

  Thought for 9s, read 1 file (ctrl+o to expand)

● Update(data\relational.py)
  ⎿  Added 1 line
      2  
      3  import sqlite3
      4  import json
      5 +import math                                                     
      6  import os
      7  import uuid
      8  from abc import ABC, abstractmethod

  Thought for 11s, read 1 file (ctrl+o to expand)

● Update(data\relational.py)
  ⎿  Added 69 lines, removed 34 lines
      745          If ``task_signature`` is provided, prefer semantic com
           patibility matching
      746          (action semantics, verification semantics, parameter s
           lots).  Otherwise fall
      747          back to the legacy exact-match behavior on ``user_requ
           est``.
      748 +                                                              
      749 +        Environment compatibility is scored rather than compar
          +ed with exact equality:                                       
      750 +        critical fields (package/activity/screen_profile) must
          + match, while optional                                        
      751 +        fields (app_version/fixture_fingerprint) only incur pe
          +nalties when both sides                                       
      752 +        are present and differ.                               
      753          """
      749 -        from agents.plan_extractor import task_signatures_comp
          -atible                                                        
      754 +        from agents.plan_extractor import (                   
      755 +            environment_compatibility_score,                  
      756 +            task_signatures_compatible,                       
      757 +        )                                                     
      758  
      759          rows = self._conn.execute(
      760              """
     ...
      765              (app_package,),
      766          ).fetchall()
      767  
      760 -        # Semantic matching path.                             
      761 -        if task_signature:                                    
      762 -            for row in rows:                                  
      763 -                plan = dict(row)                              
      764 -                signature = json.loads(plan.get("task_signatur
          -e_json") or "{}")                                             
      765 -                if verification_fingerprint and signature.get(
      766 -                    "verification_fingerprint"                
      767 -                ) != verification_fingerprint:                
      768 -                    continue                                  
      769 -                if environment_key and plan.get("environment_k
          -ey", "") != environment_key:                                  
      770 -                    continue                                  
      771 -                if task_signatures_compatible(task_signature, 
          -signature):                                                   
      772 -                    actions = self._conn.execute(             
      773 -                        "SELECT * FROM plan_actions WHERE plan
          -_id = ? ORDER BY action_index ASC",                           
      774 -                        (plan["plan_id"],),                   
      775 -                    ).fetchall()                              
      776 -                    plan["task_signature"] = signature        
      777 -                    plan["actions"] = [dict(action) for action
          - in actions]                                                  
      778 -                    return plan                               
      779 -                                                              
      780 -        # Legacy exact-match fallback.                        
      781 -        for row in rows:                                      
      768 +        def _load_plan(row):                                  
      769              plan = dict(row)
      770              signature = json.loads(plan.get("task_signature_js
           on") or "{}")
      784 -            if signature.get("user_request") != user_request: 
      785 -                continue                                      
      786 -            if (                                              
      787 -                verification_fingerprint                      
      788 -                and signature.get("verification_fingerprint") 
      789 -                != verification_fingerprint                   
      790 -            ):                                                
      791 -                continue                                      
      792 -            if environment_key and plan.get("environment_key",
          - "") != environment_key:                                      
      793 -                continue                                      
      771              actions = self._conn.execute(
      772                  "SELECT * FROM plan_actions WHERE plan_id = ? 
           ORDER BY action_index ASC",
      773                  (plan["plan_id"],),
     ...
      775              plan["task_signature"] = signature
      776              plan["actions"] = [dict(action) for action in acti
           ons]
      777              return plan
      801 -        return None                                           
      778  
      779 +        candidates = []                                       
      780 +                                                              
      781 +        # Semantic matching path.                             
      782 +        if task_signature:                                    
      783 +            for row in rows:                                  
      784 +                plan = _load_plan(row)                        
      785 +                signature = plan["task_signature"]            
      786 +                if (                                          
      787 +                    verification_fingerprint                  
      788 +                    and signature.get("verification_fingerprin
          +t")                                                           
      789 +                    != verification_fingerprint               
      790 +                ):                                            
      791 +                    continue                                  
      792 +                if not task_signatures_compatible(task_signatu
          +re, signature):                                               
      793 +                    continue                                  
      794 +                env_score = environment_compatibility_score(  
      795 +                    plan.get("environment_key", ""), environme
          +nt_key                                                        
      796 +                )                                             
      797 +                if not env_score["compatible"]:               
      798 +                    continue                                  
      799 +                plan["environment_compatibility_score"] = env_
          +score["score"]                                                
      800 +                plan["environment_compatibility_reasons"] = en
          +v_score["reasons"]                                            
      801 +                candidates.append(plan)                       
      802 +                                                              
      803 +        # Legacy exact-match fallback.                        
      804 +        if not candidates:                                    
      805 +            for row in rows:                                  
      806 +                plan = _load_plan(row)                        
      807 +                signature = plan["task_signature"]            
      808 +                if signature.get("user_request") != user_reque
          +st:                                                           
      809 +                    continue                                  
      810 +                if (                                          
      811 +                    verification_fingerprint                  
      812 +                    and signature.get("verification_fingerprin
          +t")                                                           
      813 +                    != verification_fingerprint               
      814 +                ):                                            
      815 +                    continue                                  
      816 +                env_score = environment_compatibility_score(  
      817 +                    plan.get("environment_key", ""), environme
          +nt_key                                                        
      818 +                )                                             
      819 +                if not env_score["compatible"]:               
      820 +                    continue                                  
      821 +                plan["environment_compatibility_score"] = env_
          +score["score"]                                                
      822 +                plan["environment_compatibility_reasons"] = en
          +v_score["reasons"]                                            
      823 +                candidates.append(plan)                       
      824 +                                                              
      825 +        if not candidates:                                    
      826 +            return None                                       
      827 +                                                              
      828 +        # Prefer highest environment compatibility, then plan 
          +quality.                                                      
      829 +        candidates.sort(                                      
      830 +            key=lambda p: (                                   
      831 +                float(p.get("environment_compatibility_score",
          + 0.0) or 0.0),                                                
      832 +                float(p.get("quality_score", 0.0) or 0.0),    
      833 +            ),                                                
      834 +            reverse=True,                                     
      835 +        )                                                     
      836 +        return candidates[0]                                  
      837 +                                                              
      838      def save_locator_knowledge(
      839          self,
      840          *,

  Thought for 10s, read 1 file (ctrl+o to expand)

● Update(data\relational.py)
  ⎿  Added 10 lines, removed 1 line
      583              )
      584  
      585      def record_execution_plan_outcome(
      586 -        self, plan_id: str, verdict: str, run_id: str = ""    
      586 +        self,                                                 
      587 +        plan_id: str,                                         
      588 +        verdict: str,                                         
      589 +        run_id: str = "",                                     
      590 +        *,                                                    
      591 +        direct_quality_threshold: float = 0.5,                
      592      ) -> None:
      593          """Update plan trust from distinct persisted compatibl
           e runs and action quality."""
      594          if not plan_id or not run_id:
     ...
      614          trust = str(plan.get("plan_trust", "candidate") or "ca
           ndidate")
      615          if trust == "candidate" and success_count >= 2 and act
           ion_quality >= 0.6:
      616              trust = "trusted"
      617 +        direct_approved = int(plan.get("direct_approved", 0) o
          +r 0)                                                          
      618 +        if direct_approved and action_quality < direct_quality
          +_threshold:                                                   
      619 +            direct_approved = 0                               
      620          self.upsert(
      621              "execution_plans",
      622              {
     ...
      625                  "success_count": success_count,
      626                  "quality_score": action_quality,
      627                  "plan_trust": trust,
      628 +                "direct_approved": direct_approved,           
      629                  "updated_at": datetime.now().isoformat(),
      630              },
      631              key="plan_id",

  Thought for 6s (ctrl+o to expand)

  Thought for 11s, read 1 file (ctrl+o to expand)

● Update(agents\rag_context.py)
  ⎿  Added 30 lines, removed 8 lines
      78                  top_k=5,
      79              )
      80              compatible_items: list[dict[str, Any]] = []
      81 -            from agents.plan_extractor import task_signatures_c
         -ompatible                                                      
      81 +            from agents.plan_extractor import (                
      82 +                environment_compatibility_score,               
      83 +                task_signatures_compatible,                    
      84 +            )                                                  
      85  
      86              for candidate in candidates or []:
      87                  metadata = candidate.get("metadata", {}) or {}
     ...
      92                  if not full_plan:
      93                      continue
      94                  cand_signature = full_plan.get("task_signature"
          , {}) or {}
      92 -                # Environment compatibility: exact match for no
         -w (configurable later).                                        
      93 -                cand_env = full_plan.get("environment_key", "")
      94 -                if environment_key and cand_env and environment
         -_key != cand_env:                                              
      95 -                    continue                                   
      95                  # Verification fingerprint safety gate.
      96                  cand_vf = cand_signature.get("verification_fing
          erprint", "")
      97                  if (
     ...
      100                      and verification_fingerprint != cand_vf
      101                  ):
      102                      continue
      104 -                if task_signatures_compatible(task_signature, 
          -cand_signature):                                              
      105 -                    compatible_items.append(full_plan)        
      103 +                if not task_signatures_compatible(task_signatu
          +re, cand_signature):                                          
      104 +                    continue                                  
      105 +                # Environment compatibility: scored instead of
          + exact equality.                                              
      106 +                cand_env = full_plan.get("environment_key", ""
          +)                                                             
      107 +                env_score = environment_compatibility_score(ca
          +nd_env, environment_key)                                      
      108 +                if not env_score["compatible"]:               
      109 +                    continue                                  
      110 +                full_plan["environment_compatibility_score"] =
          + env_score["score"]                                           
      111 +                full_plan["environment_compatibility_reasons"]
          + = env_score["reasons"]                                       
      112 +                compatible_items.append(full_plan)            
      113              if compatible_items:
      114 +                # Prefer highest environment compatibility, th
          +en quality.                                                   
      115 +                compatible_items.sort(                        
      116 +                    key=lambda p: (                           
      117 +                        float(p.get("environment_compatibility
          +_score", 0.0) or 0.0),                                        
      118 +                        float(p.get("quality_score", 0.0) or 0
          +.0),                                                          
      119 +                    ),                                        
      120 +                    reverse=True,                             
      121 +                )                                             
      122 +                best = compatible_items[0]                    
      123                  return {
      124                      "purpose": purpose,
      125                      "source": "vector_then_relational",
      110 -                    "items": [compatible_items[0]],           
      126 +                    "items": [best],                          
      127                      "candidates": len(candidates),
      128 +                    "environment_compatibility_score": best.ge
          +t(                                                            
      129 +                        "environment_compatibility_score"     
      130 +                    ),                                        
      131 +                    "environment_compatibility_reasons": best.
          +get(                                                          
      132 +                        "environment_compatibility_reasons"   
      133 +                    ),                                        
      134                  }
      135  
      136          # Fallback to relational exact/semantic match.

  Thought for 5s (ctrl+o to expand)
● Update(agents\rag_context.py)
  ⎿  Added 10 lines
      145              "purpose": purpose,
      146              "source": "relational",
      147              "items": [plan] if plan else [],
      148 +            "environment_compatibility_score": plan.get(      
      149 +                "environment_compatibility_score"             
      150 +            )                                                 
      151 +            if plan                                           
      152 +            else None,                                        
      153 +            "environment_compatibility_reasons": plan.get(    
      154 +                "environment_compatibility_reasons"           
      155 +            )                                                 
      156 +            if plan                                           
      157 +            else None,                                        
      158          }
      159      if purpose == "locator":
      160          from agents.graph import _relational_db

  Thought for 7s (ctrl+o to expand)

  Thought for 34s, searched for 2 patterns, read 2 files (ctrl+o to expand)

● Update(agents\nodes.py)
  ⎿  Added 86 lines, removed 21 lines
      1306      return Command(update={})
      1307  
      1308  
      1309 +def _get_app_version(ctx: Any, app_package: str) -> str:     
      1310 +    """Query the device for the installed app version, cached
           + per run."""                                                 
      1311 +    if not ctx or not app_package:                           
      1312 +        return ""                                            
      1313 +    cache_key = f"_app_version_{app_package}"                
      1314 +    cached = getattr(ctx, cache_key, None)                   
      1315 +    if cached is not None:                                   
      1316 +        return cached                                        
      1317 +    version = ""                                             
      1318 +    try:                                                     
      1319 +        if ctx.device and hasattr(ctx.device, "shell"):      
      1320 +            output = ctx.device.shell(["dumpsys", "package", 
           +app_package])                                                
      1321 +            text = str(output or "")                         
      1322 +            for line in text.splitlines():                   
      1323 +                if "versionName=" in line:                   
      1324 +                    parts = line.split("versionName=")       
      1325 +                    if len(parts) > 1:                       
      1326 +                        candidate = parts[1].split()[0].strip
           +()                                                           
      1327 +                        if candidate:                        
      1328 +                            version = candidate              
      1329 +                            break                            
      1330 +    except Exception:                                        
      1331 +        version = ""                                         
      1332 +    setattr(ctx, cache_key, version)                         
      1333 +    return version                                           
      1334 +                                                             
      1335 +                                                             
      1336  def mode_selection_node(state: TestState, config: RunnableCon
            fig) -> Command:
      1337      """Select a one-way v2 execution mode from an approved ta
            sk plan."""
      1338      from agents.verification import validate_contract_spans
      1339  
      1340 +    cfg: TestConfig = config["configurable"]["test_config"]  
      1341 +                                                             
      1342      contract = state.get("verification_contract", {})
      1343      if not isinstance(contract, dict) or contract.get("status
            ") != "approved":
      1344          return Command(
     ...
      1381      from agents.rag_context import retrieve_knowledge
      1382  
      1383      environment_key = ""
      1384 +    actual_environment_key = ""                              
      1385      try:
      1386          ctx = get_tool_context()
      1387          current_app = ctx.device.current_app() if ctx and ctx
            .device else {}
      1388          screen_size = ctx.screen_size if ctx else (0, 0)
      1359 -        if (current_app or {}).get("package") or any(screen_s
           -ize):                                                        
      1360 -            environment_key = environment_fingerprint(       
      1361 -                {                                            
      1362 -                    "package": (current_app or {}).get("packa
           -ge", ""),                                                    
      1363 -                    "activity": (current_app or {}).get("acti
           -vity", ""),                                                  
      1364 -                },                                           
      1365 -                "x".join(str(value) for value in screen_size)
           -,                                                            
      1366 -            )                                                
      1389 +        screen_profile = "x".join(str(value) for value in scr
           +een_size)                                                    
      1390 +        app_version = _get_app_version(ctx, state.get("app_pa
           +ckage", ""))                                                 
      1391 +        fixture_fingerprint = cfg.fixture_profile            
      1392 +        page_payload = {                                     
      1393 +            "package": (current_app or {}).get("package", "")
           +,                                                            
      1394 +            "activity": (current_app or {}).get("activity", "
           +"),                                                          
      1395 +            "app_version": app_version,                      
      1396 +            "fixture_fingerprint": fixture_fingerprint,      
      1397 +        }                                                    
      1398 +        actual_environment_key = environment_fingerprint(page
           +_payload, screen_profile)                                    
      1399 +        # For matching, use the same key (candidate plans sto
           +re their own page facts).                                    
      1400 +        environment_key = actual_environment_key             
      1401      except Exception:
      1402          environment_key = ""
      1403 +        actual_environment_key = ""                          
      1404  
      1405      task_signature = _task_signature_from_goal(
      1406          state.get("user_request", ""),
     ...
      1418          task_signature=task_signature,
      1419      )
      1420      plan = plan_result["items"][0] if plan_result["items"] el
            se None
      1421 +    env_score = float(plan_result.get("environment_compatibil
           +ity_score", 0.0) or 0.0)                                     
      1422 +    env_reasons = list(plan_result.get("environment_compatibi
           +lity_reasons") or [])                                        
      1423 +                                                             
      1424 +    base_update = {                                          
      1425 +        "actual_environment_key": actual_environment_key,    
      1426 +        "environment_compatibility_score": env_score,        
      1427 +        "environment_compatibility_reasons": env_reasons,    
      1428 +    }                                                        
      1429 +                                                             
      1430      if not plan:
      1431          return Command(
      1432              update={
      1433 +                **base_update,                               
      1434                  "execution_mode": "explore",
      1435                  "lifecycle_state": "Explore",
      1436                  "mode_selection_reason": "no_matching_plan",
     ...
      1439          )
      1440  
      1441      actions = list(plan.get("actions", []) or [])
      1442 +    all_direct_eligible = bool(actions) and all(             
      1443 +        action.get("execution_eligibility") == "direct_eligib
           +le"                                                          
      1444 +        for action in actions                                
      1445 +        if isinstance(action, dict)                          
      1446 +    )                                                        
      1447      direct_ready = (
      1448          bool(plan.get("direct_approved", False))
      1399 -        and bool(actions)                                    
      1400 -        and all(                                             
      1401 -            action.get("execution_eligibility") == "direct_el
           -igible"                                                      
      1402 -            for action in actions                            
      1403 -            if isinstance(action, dict)                      
      1404 -        )                                                    
      1449 +        and all_direct_eligible                              
      1450 +        and env_score == 1.0                                 
      1451      )
      1452      if direct_ready:
      1453          return Command(
      1454              update={
      1455 +                **base_update,                               
      1456                  "execution_mode": "direct",
      1457                  "lifecycle_state": "Direct",
      1458                  "plan_id": str(plan["plan_id"]),
      1459                  "plan_trust": str(plan.get("plan_trust", "") 
            or "candidate"),
      1413 -                "mode_selection_reason": "direct_approved_com
           -patible_plan",                                               
      1460 +                "mode_selection_reason": "direct_approved_ful
           +l_environment_match",                                        
      1461                  "selected_plan_actions": actions,
      1462                  "_direct_action_cursor": 0,
      1463              }
      1464          )
      1465 +                                                             
      1466 +    guided_threshold = cfg.environment_guided_threshold      
      1467 +    if env_score >= guided_threshold:                        
      1468 +        return Command(                                      
      1469 +            update={                                         
      1470 +                **base_update,                               
      1471 +                "execution_mode": "guided",                  
      1472 +                "lifecycle_state": "Guided",                 
      1473 +                "plan_id": str(plan["plan_id"]),             
      1474 +                "plan_trust": str(plan.get("plan_trust", "") 
           +or "candidate"),                                             
      1475 +                "mode_selection_reason": (                   
      1476 +                    "matching_plan_guided_partial_environment
           +"                                                            
      1477 +                    if env_score < 1.0                       
      1478 +                    else "matching_plan_guided"              
      1479 +                ),                                           
      1480 +                "selected_plan_actions": actions,            
      1481 +            }                                                
      1482 +        )                                                    
      1483 +                                                             
      1484      return Command(
      1485          update={
      1420 -            "execution_mode": "guided",                      
      1421 -            "lifecycle_state": "Guided",                     
      1422 -            "plan_id": str(plan["plan_id"]),                 
      1423 -            "plan_trust": str(plan.get("plan_trust", "") or "
           -candidate"),                                                 
      1424 -            "mode_selection_reason": "matching_plan_guided", 
      1425 -            "selected_plan_actions": actions,                
      1486 +            **base_update,                                   
      1487 +            "execution_mode": "explore",                     
      1488 +            "lifecycle_state": "Explore",                    
      1489 +            "mode_selection_reason": "matching_plan_environme
           +nt_incompatible",                                            
      1490 +            "selected_plan_actions": [],                     
      1491          }
      1492      )
      1493  

  Thought for 6s (ctrl+o to expand)

● Update(agents\nodes.py)
  ⎿  Error editing file

  Thought for 31s, read 1 file (ctrl+o to expand)

● Update(agents\nodes.py)
  ⎿  Added 40 lines, removed 8 lines
      1495  def direct_node(state: TestState, config: RunnableConfig) -> 
            Command:
      1496      """Execute one stable plan action without an LLM decision
            ."""
      1497      from agents.budget import _calc_mode_phase_budget
      1498 +    from tools.results import parse_status                   
      1499  
      1500 +    if int(state.get("_direct_downgrade_count", 0) or 0) >= 1
           +:                                                            
      1501 +        transitions = list(state.get("mode_transition_events"
           +, []) or [])                                                 
      1502 +        transitions.append(                                  
      1503 +            {                                                
      1504 +                "from": "direct",                            
      1505 +                "to": "guided",                              
      1506 +                "reason": "direct_reentry_guard",            
      1507 +                "step_index": int(state.get("_direct_action_c
           +ursor", 0) or 0),                                            
      1508 +            }                                                
      1509 +        )                                                    
      1510 +        return Command(                                      
      1511 +            update={                                         
      1512 +                "execution_mode": "guided",                  
      1513 +                "lifecycle_state": "Guided",                 
      1514 +                "mode_selection_reason": "direct_reentry_guar
           +d",                                                          
      1515 +                "mode_transition_events": transitions,       
      1516 +            }                                                
      1517 +        )                                                    
      1518 +                                                             
      1519      cursor = int(state.get("_direct_action_cursor", 0) or 0)
      1520      actions = list(state.get("selected_plan_actions", []) or 
            [])
      1521      if cursor >= len(actions) or cursor >= _calc_mode_phase_b
            udget(state, "direct"):
     ...
      1545      ctx = get_tool_context()
      1546      tool_input: dict[str, Any] = {}
      1547      before_app: dict[str, Any] = {}
      1548 +    output = ""                                              
      1549      try:
      1550          tool_input = json.loads(action.get("tool_input_json")
             or "{}")
      1551          precondition = json.loads(action.get("precondition_js
            on") or "{}")
     ...
      1574              raise RuntimeError("direct postcondition mismatch
            ")
      1575      except Exception as exc:
      1576          output = f"ERROR: {exc}"
      1556 -    status = "OK" if output.startswith(("OK", "PASS")) else "
           -ERROR"                                                       
      1577 +                                                             
      1578 +    parsed_status = parse_status(output)                     
      1579 +    if parsed_status:                                        
      1580 +        status = parsed_status                               
      1581 +    else:                                                    
      1582 +        status = "OK" if output.startswith(("OK", "PASS")) el
           +se "ERROR"                                                   
      1583 +                                                             
      1584      after_app = ctx.device.current_app() if ctx and ctx.devic
            e else {}
      1585      if ctx:
      1586          screen_profile = ""
     ...
      1606                  "execution_mode": "direct",
      1607              }
      1608          )
      1582 -    if status != "OK":                                       
      1609 +    if status not in {"OK", "PASS", "YES"}:                  
      1610          transitions = list(state.get("mode_transition_events"
            , []) or [])
      1611 +        reason = "direct_action_failed"                      
      1612 +        if "precondition mismatch" in output:                
      1613 +            reason = "direct_precondition_failed"            
      1614 +        elif "postcondition mismatch" in output:             
      1615 +            reason = "direct_postcondition_failed"           
      1616 +        elif status == "TIMEOUT":                            
      1617 +            reason = "direct_action_timeout"                 
      1618 +        elif status == "NOT_FOUND":                          
      1619 +            reason = "direct_action_not_found"               
      1620          transitions.append(
      1621              {
      1622                  "from": "direct",
      1623                  "to": "guided",
      1588 -                "reason": (                                  
      1589 -                    "direct_postcondition_failed"            
      1590 -                    if "postcondition mismatch" in output    
      1591 -                    else "direct_action_failed"              
      1592 -                ),                                           
      1624 +                "reason": reason,                            
      1625                  "step_index": cursor,
      1626              }
      1627          )
     ...
      1629              update={
      1630                  "execution_mode": "guided",
      1631                  "lifecycle_state": "Guided",
      1600 -                "mode_selection_reason": transitions[-1]["rea
           -son"],                                                       
      1632 +                "mode_selection_reason": reason,             
      1633                  "_direct_downgrade_count": 1,
      1634                  "mode_transition_events": transitions,
      1635              }

  Thought for 7s (ctrl+o to expand)

● Update(agents\nodes.py)
  ⎿  Added 10 lines
      1639  
      1640  def mode_transition_node(state: TestState, config: RunnableCo
            nfig) -> Command:
      1641      """Perform the only currently supported degradation: guid
            ed to explore."""
      1642 +    if int(state.get("_guided_downgrade_count", 0) or 0) >= 1
           +:                                                            
      1643 +        return Command(                                      
      1644 +            update={                                         
      1645 +                "execution_mode": "explore",                 
      1646 +                "lifecycle_state": "Explore",                
      1647 +                "mode_selection_reason": "guided_reentry_guar
           +d",                                                          
      1648 +                "selected_plan_actions": [],                 
      1649 +            }                                                
      1650 +        )                                                    
      1651 +                                                             
      1652      history = list(state.get("step_history", []) or [])
      1653      last_step = history[-1] if history else {}
      1654      from agents.budget import _calc_mode_phase_budget

  Thought for 5s (ctrl+o to expand)

  Thought for 8s, read 1 file (ctrl+o to expand)

● Update(agents\graph.py)
  ⎿  Added 4 lines
      72          logger.info("Route: reporter (contract verdict=%s)", ev
          aluation["verdict"])
      73          return "reporter"
      74      if state.get("execution_mode") == "direct":
      75 +        # If direct already degraded once, do not route back to
         + direct.                                                       
      76 +        if int(state.get("_direct_downgrade_count", 0) or 0) >=
         + 1:                                                            
      77 +            logger.info("Route: agent (direct downgrade already
         + consumed)")                                                   
      78 +            return "agent"                                     
      79          return "direct"
      80      if _should_downgrade_guided(state):
      81          logger.info("Route: mode transition (guided -> explore)
          ")

  Thought for 5s (ctrl+o to expand)

● Update(agents\graph.py)
  ⎿  Added 6 lines, removed 1 line
      118      if not action_log:
      119          return False
      120      last_action = action_log[-1] if isinstance(action_log[-1],
            dict) else {}
      121 -    return str(last_action.get("status_code", "") or "").upper
          -() in {"ERROR", "FAIL"}                                       
      121 +    return str(last_action.get("status_code", "") or "").upper
          +() in {                                                       
      122 +        "ERROR",                                              
      123 +        "FAIL",                                               
      124 +        "TIMEOUT",                                            
      125 +        "NOT_FOUND",                                          
      126 +    }                                                         
      127  
      128  
      129  # ═══ GRAPH ═══

  Thought for 10s (ctrl+o to expand)

  Thought for 3s (ctrl+o to expand)

● Bash(python -m pytest tests/test_plan_extractor.py
      tests/test_mode_selection.py tests/test_relational_plan_matching.py
      tests/test_rag_context.py tests/test_relationa…)
  ⎿                                  "claim": "time is red",
                                     "goal_source_span": [0, 11],
                                     "channels": ["vision_verify"],
     … +77 lines (ctrl+o to expand)
  ⎿  (timeout 5m)

  Thought for 8s (ctrl+o to expand)

---

## Phase 4 完成总结（端到端性能 & 质量门禁）

延续上一轮 Phase 3 收尾后未完成的工作，本轮完成 Phase 4 的核心落地。

### 变更清单
1. **`agents/run_trace.py`**
   - 抽出可复用的 `compute_resolution_metrics(tool_log)`，按 click 工具的
     `resolution_type`（exact/semantic）与标签错配（req/res 均非空且不一致 →
     label_mismatch）汇总；`build_run_trace` 改为复用该 helper，避免逻辑分叉。
   - 修复上一轮聚合循环误用 `step.get("tool_name")` 的字段名（应为 `tool`）。

2. **`agents/nodes.py`**
   - `reporter_node` 顶层 import `compute_resolution_metrics`（修正之前因函数内
     延迟 import 导致的 `UnboundLocalError`）。
   - 原 `exact_count`（误用 `match_mode == "exact"`，而 click 实际 match_mode 为
     resource_id/element 等，永不命中，形同死代码）改为由 `resolution_type` 派生，
     新增 `semantic_count`；trace 指标字典补 `semantic_click_count`。
   - `record_execution_run` 调用补齐 `resolution_metrics`、`duration_seconds`、
     `llm_call_count` 三项（Phase 4 端到端持久化）。

3. **`data/relational.py`**
   - `execution_runs` 表新增 `duration_seconds REAL`、`llm_call_count INTEGER`、
     既有 `resolution_metrics TEXT`（上一轮已加）。
   - `record_execution_run` / `list_execution_runs` 写入与读取新增列；
     `get_execution_run` 走 `SELECT *` 自动覆盖。
   - 新增 `summarize_by_mode()`：按 `execution_mode` 聚合通过率、平均/累计耗时、
     LLM 调用、模式转换次数、后置条件命中数、证据完整率、locator 解析指标；
     配套 `_count_in` / `_count_distinct_runs_with` 两个内部计数 helper。

4. **`tests/test_phase4_metrics.py`（新增）**
   - `compute_resolution_metrics` 基础汇总、标签错配（空 label 不计入）、空输入。
   - `record_execution_run` resolution_metrics/duration/llm_call_count 往返。
   - `summarize_by_mode` 跨 direct/explore 双模式聚合校验。

### 验证
- 全套（不含 test_api_smoke.py，其仅在缺 fastapi 的测试 venv 失败）：
  **224 passed, 6 skipped**。
- 新增 Phase 4 测试 19 项全绿。

### 备注
- 后置条件命中计数沿用 `action_events.status IN ('success','continue')`，与
  reporter 归一化后的 display-step status 口径一致（测试数据须用 `success` 而非
  `OK`）。
- 文档中提到的 Phase 4 回归集 / 差分 / 证据防篡改测试为可选项，本轮未做，留待后续。

---

## Phase 4 完成总结（端到端性能 & 质量门禁）

延续上一轮 Phase 3 收尾后未完成的工作，本轮完成 Phase 4 的核心落地。

### 变更清单
1. **`agents/run_trace.py`**
   - 抽出可复用的 `compute_resolution_metrics(tool_log)`，按 click 工具的
     `resolution_type`（exact/semantic）与标签错配（req/res 均非空且不一致 →
     label_mismatch）汇总；`build_run_trace` 改为复用该 helper，避免逻辑分叉。
   - 修复上一轮聚合循环误用 `step.get("tool_name")` 的字段名（应为 `tool`）。

2. **`agents/nodes.py`**
   - `reporter_node` 顶层 import `compute_resolution_metrics`（修正之前因函数内
     延迟 import 导致的 `UnboundLocalError`）。
   - 原 `exact_count`（误用 `match_mode == "exact"`，而 click 实际 match_mode 为
     resource_id/element 等，永不命中，形同死代码）改为由 `resolution_type` 派生，
     新增 `semantic_count`；trace 指标字典补 `semantic_click_count`。
   - `record_execution_run` 调用补齐 `resolution_metrics`、`duration_seconds`、
     `llm_call_count` 三项（Phase 4 端到端持久化）。

3. **`data/relational.py`**
   - `execution_runs` 表新增 `duration_seconds REAL`、`llm_call_count INTEGER`、
     既有 `resolution_metrics TEXT`（上一轮已加）。
   - `record_execution_run` / `list_execution_runs` 写入与读取新增列；
     `get_execution_run` 走 `SELECT *` 自动覆盖。
   - 新增 `summarize_by_mode()`：按 `execution_mode` 聚合通过率、平均/累计耗时、
     LLM 调用、模式转换次数、后置条件命中数、证据完整率、locator 解析指标；
     配套 `_count_in` / `_count_distinct_runs_with` 两个内部计数 helper。

4. **`tests/test_phase4_metrics.py`（新增）**
   - `compute_resolution_metrics` 基础汇总、标签错配（空 label 不计入）、空输入。
   - `record_execution_run` resolution_metrics/duration/llm_call_count 往返。
   - `summarize_by_mode` 跨 direct/explore 双模式聚合校验。

### 验证
- 全套（不含 test_api_smoke.py，其仅在缺 fastapi 的测试 venv 失败）：
  **224 passed, 6 skipped**。
- 新增 Phase 4 测试 19 项全绿。

### 备注
- 后置条件命中计数沿用 `action_events.status IN ('success','continue')`，与
  reporter 归一化后的 display-step status 口径一致（测试数据须用 `success` 而非
  `OK`）。
- 文档中提到的 Phase 4 回归集 / 差分 / 证据防篡改测试为可选项，本轮未做，留待后续。

● Update(agents\nodes.py)  ⎿  Added 5 lines, removed 1 line
      1337      """Select a one-way v2 execution mode from an approved ta
            sk plan."""
      1338      from agents.verification import validate_contract_spans
      1339  
      1340 -    cfg: TestConfig = config["configurable"]["test_config"]  
      1340 +    cfg: TestConfig = (                                      
      1341 +        config.get("configurable", {}).get("test_config")    
      1342 +        if isinstance(config, dict) and config.get("configura
           +ble")                                                        
      1343 +        else TestConfig()                                    
      1344 +    )                                                        
      1345  
      1346      contract = state.get("verification_contract", {})
      1347      if not isinstance(contract, dict) or contract.get("status
            ") != "approved":

  Thought for 4s (ctrl+o to expand)

● Update(agents\nodes.py)
  ⎿  Added 6 lines
      1501      from agents.budget import _calc_mode_phase_budget
      1502      from tools.results import parse_status
      1503  
      1504 +    cfg: TestConfig = (                                      
      1505 +        config.get("configurable", {}).get("test_config")    
      1506 +        if isinstance(config, dict) and config.get("configura
           +ble")                                                        
      1507 +        else TestConfig()                                    
      1508 +    )                                                        
      1509 +                                                             
      1510      if int(state.get("_direct_downgrade_count", 0) or 0) >= 1
            :
      1511          transitions = list(state.get("mode_transition_events"
            , []) or [])
      1512          transitions.append(

  Thought for 29s (ctrl+o to expand)

● Update(data\relational.py)
  ⎿  Added 1 line, removed 5 lines
      513                      age_days = 0
      514              age_decay = math.exp(-decay_lambda * age_days)
      515  
      516 -            penalty = (                                       
      517 -                0.25 * timeout_rate                           
      518 -                + 0.35 * not_found_rate                       
      519 -                + 0.20 * postcondition_failure_rate           
      520 -            )                                                 
      516 +            penalty = 0.25 * timeout_rate + 0.35 * not_found_r
          +ate                                                           
      517              quality = max(
      518                  0.0,
      519                  min(

  Thought for 8s (ctrl+o to expand)

---

## Phase 4 完成总结（端到端性能 & 质量门禁）

延续上一轮 Phase 3 收尾后未完成的工作，本轮完成 Phase 4 的核心落地。

### 变更清单
1. **`agents/run_trace.py`**
   - 抽出可复用的 `compute_resolution_metrics(tool_log)`，按 click 工具的
     `resolution_type`（exact/semantic）与标签错配（req/res 均非空且不一致 →
     label_mismatch）汇总；`build_run_trace` 改为复用该 helper，避免逻辑分叉。
   - 修复上一轮聚合循环误用 `step.get("tool_name")` 的字段名（应为 `tool`）。

2. **`agents/nodes.py`**
   - `reporter_node` 顶层 import `compute_resolution_metrics`（修正之前因函数内
     延迟 import 导致的 `UnboundLocalError`）。
   - 原 `exact_count`（误用 `match_mode == "exact"`，而 click 实际 match_mode 为
     resource_id/element 等，永不命中，形同死代码）改为由 `resolution_type` 派生，
     新增 `semantic_count`；trace 指标字典补 `semantic_click_count`。
   - `record_execution_run` 调用补齐 `resolution_metrics`、`duration_seconds`、
     `llm_call_count` 三项（Phase 4 端到端持久化）。

3. **`data/relational.py`**
   - `execution_runs` 表新增 `duration_seconds REAL`、`llm_call_count INTEGER`、
     既有 `resolution_metrics TEXT`（上一轮已加）。
   - `record_execution_run` / `list_execution_runs` 写入与读取新增列；
     `get_execution_run` 走 `SELECT *` 自动覆盖。
   - 新增 `summarize_by_mode()`：按 `execution_mode` 聚合通过率、平均/累计耗时、
     LLM 调用、模式转换次数、后置条件命中数、证据完整率、locator 解析指标；
     配套 `_count_in` / `_count_distinct_runs_with` 两个内部计数 helper。

4. **`tests/test_phase4_metrics.py`（新增）**
   - `compute_resolution_metrics` 基础汇总、标签错配（空 label 不计入）、空输入。
   - `record_execution_run` resolution_metrics/duration/llm_call_count 往返。
   - `summarize_by_mode` 跨 direct/explore 双模式聚合校验。

### 验证
- 全套（不含 test_api_smoke.py，其仅在缺 fastapi 的测试 venv 失败）：
  **224 passed, 6 skipped**。
- 新增 Phase 4 测试 19 项全绿。

### 备注
- 后置条件命中计数沿用 `action_events.status IN ('success','continue')`，与
  reporter 归一化后的 display-step status 口径一致（测试数据须用 `success` 而非
  `OK`）。
- 文档中提到的 Phase 4 回归集 / 差分 / 证据防篡改测试为可选项，本轮未做，留待后续。

---

## Phase 4 完成总结（端到端性能 & 质量门禁）

延续上一轮 Phase 3 收尾后未完成的工作，本轮完成 Phase 4 的核心落地。

### 变更清单
1. **`agents/run_trace.py`**
   - 抽出可复用的 `compute_resolution_metrics(tool_log)`，按 click 工具的
     `resolution_type`（exact/semantic）与标签错配（req/res 均非空且不一致 →
     label_mismatch）汇总；`build_run_trace` 改为复用该 helper，避免逻辑分叉。
   - 修复上一轮聚合循环误用 `step.get("tool_name")` 的字段名（应为 `tool`）。

2. **`agents/nodes.py`**
   - `reporter_node` 顶层 import `compute_resolution_metrics`（修正之前因函数内
     延迟 import 导致的 `UnboundLocalError`）。
   - 原 `exact_count`（误用 `match_mode == "exact"`，而 click 实际 match_mode 为
     resource_id/element 等，永不命中，形同死代码）改为由 `resolution_type` 派生，
     新增 `semantic_count`；trace 指标字典补 `semantic_click_count`。
   - `record_execution_run` 调用补齐 `resolution_metrics`、`duration_seconds`、
     `llm_call_count` 三项（Phase 4 端到端持久化）。

3. **`data/relational.py`**
   - `execution_runs` 表新增 `duration_seconds REAL`、`llm_call_count INTEGER`、
     既有 `resolution_metrics TEXT`（上一轮已加）。
   - `record_execution_run` / `list_execution_runs` 写入与读取新增列；
     `get_execution_run` 走 `SELECT *` 自动覆盖。
   - 新增 `summarize_by_mode()`：按 `execution_mode` 聚合通过率、平均/累计耗时、
     LLM 调用、模式转换次数、后置条件命中数、证据完整率、locator 解析指标；
     配套 `_count_in` / `_count_distinct_runs_with` 两个内部计数 helper。

4. **`tests/test_phase4_metrics.py`（新增）**
   - `compute_resolution_metrics` 基础汇总、标签错配（空 label 不计入）、空输入。
   - `record_execution_run` resolution_metrics/duration/llm_call_count 往返。
   - `summarize_by_mode` 跨 direct/explore 双模式聚合校验。

### 验证
- 全套（不含 test_api_smoke.py，其仅在缺 fastapi 的测试 venv 失败）：
  **224 passed, 6 skipped**。
- 新增 Phase 4 测试 19 项全绿。

### 备注
- 后置条件命中计数沿用 `action_events.status IN ('success','continue')`，与
  reporter 归一化后的 display-step status 口径一致（测试数据须用 `success` 而非
  `OK`）。
- 文档中提到的 Phase 4 回归集 / 差分 / 证据防篡改测试为可选项，本轮未做，留待后续。

● Update(data\relational.py)
  ⎿  Error editing file

  Thought for 11s, read 1 file (ctrl+o to expand)

● Update(data\relational.py)
  ⎿  Removed 3 lines
      501              postcondition_rate = (postcondition_pass_count + 1
           ) / (attempt_count + 2)
      502              timeout_rate = (timeout_count + 1) / (attempt_coun
           t + 2)
      503              not_found_rate = (not_found_count + 1) / (attempt_
           count + 2)
      504 -            postcondition_failure_rate = (                    
      505 -                postcondition_failure_count + 1               
      506 -            ) / (attempt_count + 2)                           
      504  
      505              age_days = 30
      506              if last_success_at:

  Thought for 4s (ctrl+o to expand)

● Bash(python -m pytest tests/test_plan_extractor.py
      tests/test_mode_selection.py tests/test_relational_plan_matching.py
      tests/test_rag_context.py tests/test_relationa…)
  ⎿                      "plan_id": "plan-1",
                         "plan_trust": "candidate",
                         "direct_approved": 1,
     … +37 lines (ctrl+o to expand)
  ⎿  (timeout 5m)

  Thought for 18s, read 1 file (ctrl+o to expand)

● Update(tests\test_mode_selection.py)
  ⎿  Removed 1 line
      23          ):
      24              assert (app_package, user_request) == ("com.example
          .app", "创建课程表")
      25              assert verification_fingerprint == expected_fp
      26 -            assert environment_key == ""                       
      26              return None
      27  
      28      monkeypatch.setattr(graph, "_relational_db", FakeDatabase()
          )

  Thought for 4s (ctrl+o to expand)

● Update(tests\test_mode_selection.py)
  ⎿  Removed 1 line
      43              self, app_package, user_request="", verification_fi
          ngerprint="", environment_key="", task_signature=None
      44          ):
      45              assert verification_fingerprint == expected_fp
      46 -            assert environment_key == ""                       
      46              return {
      47                  "plan_id": "plan-1",
      48                  "plan_trust": "candidate",

  Thought for 3s (ctrl+o to expand)

● Bash(python -m pytest tests/test_plan_extractor.py
      tests/test_mode_selection.py tests/test_relational_plan_matching.py
      tests/test_rag_context.py tests/test_relationa…)
  ⎿                      "plan_trust": "candidate",
                         "direct_approved": 1,
                         "actions": [{"tool_name": "click", "action_index":
     … +20 lines (ctrl+o to expand)
  ⎿  (timeout 5m)

  Thought for 14s (ctrl+o to expand)

● Update(agents\nodes.py)
  ⎿  Added 10 lines
      1425      env_score = float(plan_result.get("environment_compatibil
            ity_score", 0.0) or 0.0)
      1426      env_reasons = list(plan_result.get("environment_compatibi
            lity_reasons") or [])
      1427  
      1428 +    # Fallback: if the retrieval backend did not attach a sco
           +re, compute it here.                                         
      1429 +    if plan and env_score == 0.0 and not env_reasons:        
      1430 +        from agents.plan_extractor import environment_compati
           +bility_score                                                 
      1431 +                                                             
      1432 +        env_score_result = environment_compatibility_score(  
      1433 +            plan.get("environment_key", ""), environment_key 
      1434 +        )                                                    
      1435 +        env_score = float(env_score_result.get("score", 0.0) 
           +or 0.0)                                                      
      1436 +        env_reasons = list(env_score_result.get("reasons") or
           + [])                                                         
      1437 +                                                             
      1438      base_update = {
      1439          "actual_environment_key": actual_environment_key,
      1440          "environment_compatibility_score": env_score,

  Thought for 3s (ctrl+o to expand)

● Bash(python -m pytest tests/test_plan_extractor.py
      tests/test_mode_selection.py tests/test_relational_plan_matching.py
      tests/test_rag_context.py tests/test_relationa…)
  ⎿  .................................................................
        [100%]
     65 passed in 1.61s
  ⎿  (timeout 5m)

  Thought for 16s, read 1 file (ctrl+o to expand)

● Update(tests\test_plan_extractor.py)
  ⎿  Added 68 lines
      300      assert "输入课程名" in semantics
      301  
      302  
      303 +def test_extract_action_semantics_from_request():             
      304 +    goal = {"app_package": "com.example.app", "verification": 
          +["保存成功"]}                                                 
      305 +    semantics = extract_action_semantics_from_request(        
      306 +        "打开应用后点击保存并输入课程名，验证保存成功", goal  
      307 +    )                                                         
      308 +    assert "启动应用com.example.app" in semantics             
      309 +    assert any(s.startswith("点击") and "保存" in s for s in s
          +emantics)                                                     
      310 +    assert any(s.startswith("输入") and "课程名" in s for s in
          + semantics)                                                   
      311 +    assert any(s.startswith("验证") for s in semantics)       
      312 +                                                              
      313 +                                                              
      314 +def test_task_signature_uses_goal_action_semantics_when_no_eve
          +nts():                                                        
      315 +    goal = {                                                  
      316 +        "app_package": "com.example.app",                     
      317 +        "action_semantics": ["点击时长", "输入课程名"],       
      318 +    }                                                         
      319 +    contract = {"status": "approved"}                         
      320 +    signature = _task_signature_from_goal("设置时长为50分钟", 
          +goal, contract, [])                                           
      321 +    assert "点击时长" in signature["action_semantics"]        
      322 +    assert "输入课程名" in signature["action_semantics"]      
      323 +                                                              
      324 +                                                              
      325 +def test_task_signature_falls_back_to_request_rules():        
      326 +    goal = {"app_package": "com.example.app"}                 
      327 +    contract = {"status": "approved"}                         
      328 +    signature = _task_signature_from_goal("点击保存按钮", goal
          +, contract, [])                                               
      329 +    assert any("保存" in s for s in signature["action_semantic
          +s"])                                                          
      330 +                                                              
      331 +                                                              
      332 +def test_environment_compatibility_score_rejects_package_misma
          +tch():                                                        
      333 +    expected = environment_fingerprint(                       
      334 +        {"package": "com.a", "activity": "Main", "app_version"
          +: "1.0"}, "1080x2400"                                         
      335 +    )                                                         
      336 +    actual = environment_fingerprint(                         
      337 +        {"package": "com.b", "activity": "Main", "app_version"
          +: "1.0"}, "1080x2400"                                         
      338 +    )                                                         
      339 +    score = environment_compatibility_score(expected, actual) 
      340 +    assert score["compatible"] is False                       
      341 +    assert score["score"] == 0.0                              
      342 +    assert "package_mismatch" in score["reasons"]             
      343 +                                                              
      344 +                                                              
      345 +def test_environment_compatibility_score_penalizes_version_dri
          +ft():                                                         
      346 +    expected = environment_fingerprint(                       
      347 +        {"package": "com.a", "activity": "Main", "app_version"
          +: "1.0"}, "1080x2400"                                         
      348 +    )                                                         
      349 +    actual = environment_fingerprint(                         
      350 +        {"package": "com.a", "activity": "Main", "app_version"
          +: "2.0"}, "1080x2400"                                         
      351 +    )                                                         
      352 +    score = environment_compatibility_score(expected, actual) 
      353 +    assert score["compatible"] is True                        
      354 +    assert score["score"] == 0.8                              
      355 +    assert "app_version_drift" in score["reasons"]            
      356 +                                                              
      357 +                                                              
      358 +def test_environment_compatibility_score_ignores_empty_optiona
          +l_fields():                                                   
      359 +    expected = environment_fingerprint(                       
      360 +        {"package": "com.a", "activity": "Main"}, "1080x2400" 
      361 +    )                                                         
      362 +    actual = environment_fingerprint(                         
      363 +        {"package": "com.a", "activity": "Main", "app_version"
          +: "2.0"}, "1080x2400"                                         
      364 +    )                                                         
      365 +    score = environment_compatibility_score(expected, actual) 
      366 +    assert score["compatible"] is True                        
      367 +    assert score["score"] == 1.0                              
      368 +    assert score["reasons"] == []                             
      369 +                                                              
      370 +                                                              
      371  def test_extract_verification_semantics():
      372      contract = {
      373          "verifications": [

  Thought for 10s, read 1 file (ctrl+o to expand)

● Update(tests\test_plan_extractor.py)
  ⎿  Added 3 lines
       5  from agents.plan_extractor import (
       6      build_plan_summary_text,
       7      build_user_request_template,
       8 +    environment_compatibility_score,                           
       9      environment_fingerprint,
      10      extract_action_semantics,
      11 +    extract_action_semantics_from_request,                     
      12      extract_candidate_plan,
      13      extract_parameter_slots,
      14      extract_verification_semantics,
      15      parameter_slots_compatible,
      16      task_signatures_compatible,
      17      verification_fingerprint,
      18 +    _task_signature_from_goal,                                 
      19  )
      20  from data.relational import SqliteBackend
      21  

  Thought for 7s, read 1 file (ctrl+o to expand)

● Update(tests\test_mode_selection.py)
  ⎿  Added 107 lines
      238      assert command.goto == "reporter"
      239      assert command.update["status"] == "fail"
      240      assert command.update["mode_selection_reason"] == "verific
           ation_contract_span_invalid"
      241 +                                                              
      242 +                                                              
      243 +def test_direct_requires_full_environment_score(monkeypatch): 
      244 +    expected_fp = verification_fingerprint({"status": "approve
          +d"})                                                          
      245 +                                                              
      246 +    class FakeDatabase:                                       
      247 +        def find_matching_execution_plan(                     
      248 +            self, app_package, user_request="", verification_f
          +ingerprint="", environment_key="", task_signature=None        
      249 +        ):                                                    
      250 +            return {                                          
      251 +                "plan_id": "plan-direct",                     
      252 +                "plan_trust": "candidate",                    
      253 +                "direct_approved": 1,                         
      254 +                "environment_key": environment_key,           
      255 +                "environment_compatibility_score": 1.0,       
      256 +                "actions": [                                  
      257 +                    {"tool_name": "click", "action_index": 0, 
          +"execution_eligibility": "direct_eligible"}                   
      258 +                ],                                            
      259 +            }                                                 
      260 +                                                              
      261 +    monkeypatch.setattr(graph, "_relational_db", FakeDatabase(
          +))                                                            
      262 +    monkeypatch.setattr(nodes, "get_tool_context", lambda: Non
          +e)                                                            
      263 +                                                              
      264 +    command = nodes.mode_selection_node(_state(), {})         
      265 +    assert command.update["execution_mode"] == "direct"       
      266 +    assert command.update["mode_selection_reason"] == "direct_
          +approved_full_environment_match"                              
      267 +                                                              
      268 +                                                              
      269 +def test_minor_environment_drift_selects_guided(monkeypatch): 
      270 +    expected_fp = verification_fingerprint({"status": "approve
          +d"})                                                          
      271 +                                                              
      272 +    class FakeDatabase:                                       
      273 +        def find_matching_execution_plan(                     
      274 +            self, app_package, user_request="", verification_f
          +ingerprint="", environment_key="", task_signature=None        
      275 +        ):                                                    
      276 +            return {                                          
      277 +                "plan_id": "plan-guided",                     
      278 +                "plan_trust": "candidate",                    
      279 +                "direct_approved": 1,                         
      280 +                "environment_key": environment_key,           
      281 +                "environment_compatibility_score": 0.8,       
      282 +                "actions": [                                  
      283 +                    {"tool_name": "click", "action_index": 0, 
          +"execution_eligibility": "direct_eligible"}                   
      284 +                ],                                            
      285 +            }                                                 
      286 +                                                              
      287 +    monkeypatch.setattr(graph, "_relational_db", FakeDatabase(
          +))                                                            
      288 +    monkeypatch.setattr(nodes, "get_tool_context", lambda: Non
          +e)                                                            
      289 +                                                              
      290 +    command = nodes.mode_selection_node(_state(), {})         
      291 +    assert command.update["execution_mode"] == "guided"       
      292 +    assert command.update["mode_selection_reason"] == "matchin
          +g_plan_guided_partial_environment"                            
      293 +                                                              
      294 +                                                              
      295 +def test_direct_downgrade_count_prevents_re_entry(monkeypatch)
          +:                                                             
      296 +    monkeypatch.setattr(nodes, "get_tool_context", lambda: Non
          +e)                                                            
      297 +    command = nodes.direct_node(                              
      298 +        {                                                     
      299 +            "selected_plan_actions": [],                      
      300 +            "_direct_action_cursor": 0,                       
      301 +            "_direct_downgrade_count": 1,                     
      302 +        },                                                    
      303 +        {},                                                   
      304 +    )                                                         
      305 +    assert command.update["execution_mode"] == "guided"       
      306 +    assert command.update["mode_selection_reason"] == "direct_
          +reentry_guard"                                                
      307 +                                                              
      308 +                                                              
      309 +def test_guided_downgrade_count_prevents_second_explore_transi
          +tion():                                                       
      310 +    command = nodes.mode_transition_node(                     
      311 +        {"_guided_downgrade_count": 1, "step_history": []}, {}
      312 +    )                                                         
      313 +    assert command.update["execution_mode"] == "explore"      
      314 +    assert command.update["mode_selection_reason"] == "guided_
          +reentry_guard"                                                
      315 +                                                              
      316 +                                                              
      317 +def test_direct_node_records_timeout_status(monkeypatch):     
      318 +    class FakeTool:                                           
      319 +        name = "click"                                        
      320 +                                                              
      321 +        def invoke(self, tool_input):                         
      322 +            return "TIMEOUT: tool did not respond"            
      323 +                                                              
      324 +    device = SimpleNamespace(current_app=lambda: {"package": "
          +com.example.app", "activity": "Main"})                        
      325 +    context = SimpleNamespace(device=device, _action_events=[]
          +, screen_size=(1080, 2400))                                   
      326 +    monkeypatch.setattr(nodes, "get_tool_context", lambda: con
          +text)                                                         
      327 +    monkeypatch.setattr(nodes, "AGENT_TOOLS", [FakeTool()])   
      328 +                                                              
      329 +    command = nodes.direct_node(                              
      330 +        {                                                     
      331 +            "selected_plan_actions": [                        
      332 +                {                                             
      333 +                    "tool_name": "click",                     
      334 +                    "tool_input_json": '{"label":"save"}',    
      335 +                    "precondition_json": '{"package":"com.exam
          +ple.app","activity":"Main"}',                                 
      336 +                    "postcondition_json": '{"package":"com.exa
          +mple.app","activity":"Main"}',                                
      337 +                    "locator_json": '{"rid":"save"}',         
      338 +                }                                             
      339 +            ],                                                
      340 +            "_direct_action_cursor": 0,                       
      341 +            "goal_description": {"verification": ["saved"]},  
      342 +        },                                                    
      343 +        {},                                                   
      344 +    )                                                         
      345 +    assert command.update["execution_mode"] == "guided"       
      346 +    assert command.update["mode_selection_reason"] == "direct_
          +action_timeout"                                               
      347 +    assert context._action_events[0]["status"] == "TIMEOUT"   
  Thought for 9s, read 1 file (ctrl+o to expand)

● Update(tests\test_relational_metrics.py)
  ⎿  Added 147 lines
      289      db._conn.close()
      290  
      291  
      292 +def test_postcondition_failure_count_is_tracked(tmp_path):    
      293 +    db = SqliteBackend(str(tmp_path / "postcondition_failure.d
          +b"))                                                          
      294 +    now = "2026-08-13T00:00:00"                               
      295 +    db.insert(                                                
      296 +        "plan_actions",                                       
      297 +        {                                                     
      298 +            "action_id": "action-1",                          
      299 +            "plan_id": "plan-1",                              
      300 +            "action_index": 0,                                
      301 +            "tool_name": "click",                             
      302 +            "precondition_json": '{"package":"com.example.app"
          +,"activity":"Before"}',                                       
      303 +            "postcondition_json": '{"package":"com.example.app
          +","activity":"After"}',                                       
      304 +            "created_at": now,                                
      305 +            "updated_at": now,                                
      306 +        },                                                    
      307 +    )                                                         
      308 +    db.record_plan_action_outcomes(                           
      309 +        "plan-1",                                             
      310 +        [                                                     
      311 +            {                                                 
      312 +                "action_index": 0,                            
      313 +                "tool_name": "click",                         
      314 +                "status": "ERROR",                            
      315 +                "page_before": {"package": "com.example.app", 
          +"activity": "Before"},                                        
      316 +                "page_after": {"package": "com.example.app", "
          +activity": "Before"},                                         
      317 +            }                                                 
      318 +        ],                                                    
      319 +    )                                                         
      320 +    action = db.select("plan_actions", {"action_id": "action-1
          +"})[0]                                                        
      321 +    assert action["postcondition_failure_count"] == 1         
      322 +    db._conn.close()                                          
      323 +                                                              
      324 +                                                              
      325 +def test_quality_score_applies_age_decay(tmp_path):           
      326 +    db = SqliteBackend(str(tmp_path / "age_decay.db"))        
      327 +    now = "2026-08-13T00:00:00"                               
      328 +    old_success = "2026-01-01T00:00:00"                       
      329 +    db.insert(                                                
      330 +        "plan_actions",                                       
      331 +        {                                                     
      332 +            "action_id": "action-1",                          
      333 +            "plan_id": "plan-1",                              
      334 +            "action_index": 0,                                
      335 +            "tool_name": "click",                             
      336 +            "precondition_json": '{"package":"com.example.app"
          +,"activity":"Before"}',                                       
      337 +            "postcondition_json": '{"package":"com.example.app
          +","activity":"After"}',                                       
      338 +            "last_success_at": old_success,                   
      339 +            "created_at": now,                                
      340 +            "updated_at": now,                                
      341 +        },                                                    
      342 +    )                                                         
      343 +    db.record_plan_action_outcomes(                           
      344 +        "plan-1",                                             
      345 +        [                                                     
      346 +            {                                                 
      347 +                "action_index": 0,                            
      348 +                "tool_name": "click",                         
      349 +                "status": "OK",                               
      350 +                "page_before": {"package": "com.example.app", 
          +"activity": "Before"},                                        
      351 +                "page_after": {"package": "com.example.app", "
          +activity": "After"},                                          
      352 +            }                                                 
      353 +        ],                                                    
      354 +        decay_lambda=0.01,                                    
      355 +    )                                                         
      356 +    action = db.select("plan_actions", {"action_id": "action-1
          +"})[0]                                                        
      357 +    # Without age decay, quality would be ~0.244. With ~220 da
          +ys of decay it should be lower.                               
      358 +    assert action["quality_score"] < 0.2                      
      359 +    db._conn.close()                                          
      360 +                                                              
      361 +                                                              
      362 +def test_timeout_not_found_update_quality(tmp_path):          
      363 +    db = SqliteBackend(str(tmp_path / "timeout_quality.db"))  
      364 +    now = "2026-08-13T00:00:00"                               
      365 +    db.insert(                                                
      366 +        "plan_actions",                                       
      367 +        {                                                     
      368 +            "action_id": "action-1",                          
      369 +            "plan_id": "plan-1",                              
      370 +            "action_index": 0,                                
      371 +            "tool_name": "click",                             
      372 +            "precondition_json": '{"package":"com.example.app"
          +,"activity":"Before"}',                                       
      373 +            "postcondition_json": '{"package":"com.example.app
          +","activity":"After"}',                                       
      374 +            "created_at": now,                                
      375 +            "updated_at": now,                                
      376 +        },                                                    
      377 +    )                                                         
      378 +    db.record_plan_action_outcomes(                           
      379 +        "plan-1",                                             
      380 +        [                                                     
      381 +            {                                                 
      382 +                "action_index": 0,                            
      383 +                "tool_name": "click",                         
      384 +                "status": "TIMEOUT",                          
      385 +                "page_before": {"package": "com.example.app", 
          +"activity": "Before"},                                        
      386 +                "page_after": {"package": "com.example.app", "
          +activity": "Before"},                                         
      387 +            }                                                 
      388 +        ],                                                    
      389 +    )                                                         
      390 +    action = db.select("plan_actions", {"action_id": "action-1
          +"})[0]                                                        
      391 +    assert action["timeout_count"] == 1                       
      392 +    assert action["quality_score"] < 0.2                      
      393 +    db._conn.close()                                          
      394 +                                                              
      395 +                                                              
      396 +def test_direct_approval_cleared_on_low_quality(tmp_path):    
      397 +    db = SqliteBackend(str(tmp_path / "direct_kill_switch.db")
          +)                                                             
      398 +    now = "2026-08-13T00:00:00"                               
      399 +    db.insert(                                                
      400 +        "execution_plans",                                    
      401 +        {                                                     
      402 +            "plan_id": "plan-1",                              
      403 +            "app_package": "com.example.app",                 
      404 +            "task_signature_json": "{}",                      
      405 +            "direct_approved": 1,                             
      406 +            "created_at": now,                                
      407 +            "updated_at": now,                                
      408 +        },                                                    
      409 +    )                                                         
      410 +    db.insert(                                                
      411 +        "plan_actions",                                       
      412 +        {                                                     
      413 +            "action_id": "action-1",                          
      414 +            "plan_id": "plan-1",                              
      415 +            "action_index": 0,                                
      416 +            "tool_name": "click",                             
      417 +            "quality_score": 0.3,                             
      418 +            "created_at": now,                                
      419 +            "updated_at": now,                                
      420 +        },                                                    
      421 +    )                                                         
      422 +    db.record_execution_run(                                  
      423 +        run_id="run-1",                                       
      424 +        user_request="save",                                  
      425 +        app_package="com.example.app",                        
      426 +        goal={},                                              
      427 +        verification_contract={},                             
      428 +        plan_id="plan-1",                                     
      429 +        verdict="passed",                                     
      430 +    )                                                         
      431 +    db.record_execution_plan_outcome(                         
      432 +        "plan-1", "passed", "run-1", direct_quality_threshold=
          +0.5                                                           
      433 +    )                                                         
      434 +    plan = db.select("execution_plans", {"plan_id": "plan-1"})
          +[0]                                                           
      435 +    assert plan["direct_approved"] == 0                       
      436 +    db._conn.close()                                          
      437 +                                                              
      438 +                                                              
      439  def test_missing_page_facts_are_unknown_not_postcondition_pass
           (tmp_path):
      440      db = SqliteBackend(str(tmp_path / "missing_page_facts.db")
           )
      441      now = "2026-08-14T00:00:00"

  Thought for 10s, read 1 file (ctrl+o to expand)

● Update(tests\test_graph_budget_and_reporting.py)
  ⎿  Added 28 lines
      133      )
      134  
      135  
      136 +def test_route_after_evaluator_routes_direct_to_agent_after_do
          +wngrade():                                                    
      137 +    state = {                                                 
      138 +        "status": "continue",                                 
      139 +        "execution_mode": "direct",                           
      140 +        "_direct_downgrade_count": 1,                         
      141 +        "goal_description": {"verification": ["验证1"]},      
      142 +        "verification_contract": {                            
      143 +            "status": "approved",                             
      144 +            "verifications": [                                
      145 +                {"key": "v0", "clauses": [{"id": "v0.0", "chan
          +nels": ["ui_text"]}]}                                         
      146 +            ],                                                
      147 +        },                                                    
      148 +        "clause_state": {"verdict": "unknown"},               
      149 +    }                                                         
      150 +    assert graph.route_after_evaluator(state) == "agent"      
      151 +                                                              
      152 +                                                              
      153 +def test_should_downgrade_guided_on_timeout_and_not_found():  
      154 +    for status in ("TIMEOUT", "NOT_FOUND"):                   
      155 +        state = {                                             
      156 +            "execution_mode": "guided",                       
      157 +            "_guided_downgrade_count": 0,                     
      158 +            "step_history": [{"execution_mode": "guided"}],   
      159 +            "_tool_calls_log": [{"status_code": status}],     
      160 +        }                                                     
      161 +        assert graph._should_downgrade_guided(state) is True  
      162 +                                                              
      163 +                                                              
      164  def test_agent_node_accumulates_llm_call_metrics(monkeypatch):
      165      fake_ctx = SimpleNamespace(
      166          perceiver=None,

      ================================================================================
      RESUME VERIFICATION (2026-08-14)
      ================================================================================

      Phase 3 代码已实现完成，所有逻辑测试通过：
      - 全量测试（排除 test_api_smoke.py）：218 passed, 6 skipped
      - Phase 3 专项测试（graph_budget_and_reporting / relational_metrics /
      mode_selection / plan_extractor）：75 passed
      - test_api_smoke.py 仅在当前 venv 缺 fastapi 依赖时失败（环境问题，非代码缺陷；
      真实运行环境 api/server.py、app_entry.py 均依赖 fastapi 并已安装）

      已落地的关键改动（已在仓库文件中确认）：
      - config.py TestConfig 新增 fixture_profile / direct_quality_threshold /
      environment_guided_threshold 字段；config.yaml 同步配置
      - nodes.py：_should_downgrade_guided、route_after_evaluator（direct 分支
      _direct_downgrade_count 与 direct_quality 阈值）、direct_node 强制
      verification_contract 落地、fixture_fingerprint 写入 page payload、
      cfg.fixture_profile 注入 environment_key
      - graph.py：route_after_evaluator 增加 direct 模式路由分支
      - relational.py：execution_plans 新增 direct_approved / plan_trust 列；
      environment_compatibility_score 支持 fixture_fingerprint 惩罚
      - plan_extractor.py：environment_key 包含 fixture 信息；
      instructions/planner 提示加入 fixture_profile 与 guided 降级指引

      计划中原列的“待办”经核查已无需单独补做：
      - fixture_fingerprint / last_env_score 列：fixture 信息已并入 environment_key
      JSON（环境兼容评分按指纹差异惩罚），env score 为内存态，无需持久化列
      - runtime 注入 fixture_profile：通过 TestConfig 从 config.yaml 加载，
      nodes.py 读取 cfg.fixture_profile，已贯通
      - 前端 fixture_profile 字段：计划标注为 optional best-effort，暂不暴露

      结论：Phase 3 实施完成，无需额外编码。唯一阻塞项为测试环境的 fastapi
      依赖缺失（不影响真实运行）。

================================================================================
CODE REVIEW 遗留 5 项修复（2026-08-14）
================================================================================

按 review 结论优先级，修复了 #1~#4（#5 Phase 4 指标按结论留待下阶段）：

#1 app_version/fixture 沉淀侧空转（最该补）
- 根因：extract_candidate_plan 用 actions[0].page_before 构造 environment_key，
  而 action event 的 page_before 只含 package/activity/screen_profile，无
  app_version/fixture_fingerprint → 评分 optional 档（app_version 漂移 -0.2、
  fixture 漂移 -0.3）条件永远 False。
- 修复：reporter_node 在调用 extract_candidate_plan 前，按 _get_app_version(ctx,
  app_package) + cfg.fixture_profile 注入每条 action_events 的 page_before/
  page_after。沉淀侧 environment_key 现带这两项，optional 评分生效。

#2 direct 准入缺两道码级闸门（Plan 4.1 偏离）
- 原：direct_ready 仅人工批准 + 静态 flag + env_score==1.0。
- 修复：新增 TestConfig.direct_min_runs(=2)、direct_postcond_rate_threshold(=0.8)
  + config.yaml 同步。direct_ready 增加：
    (a) success_count >= direct_min_runs（连续 N 次 guided 成功近似）；
    (b) 每个动作 postcondition_pass_count/attempt_count >= 阈值（全动作对齐）；
    (c) plan quality_score >= direct_quality_threshold。
  人工批准但闸门不满足时落到 guided，reason=guided_from_approved_no_runs。
- 新增 helper _action_postcond_rate。

#3 Bootstrapping 生命周期未实际使用
- 修复：mode_selection 中 if not plan（首跑探索/无匹配 plan）→ lifecycle_state=
  "Bootstrapping"（原写 "Explore"）。

#4 mode_transition_events 缺 plan_id/action_id/阶段预算
- 修复：表加 plan_id/action_id/phase_budget 三列；record_mode_transition_events
  写入；direct_node（3 处转换）、mode_transition_node（guided→explore）的事件
  均补 plan_id/action_id/phase_budget（phase_budget 来自 _calc_mode_phase_budget）。

#5 Phase 4 新指标（exact_resolution_count / semantic_resolution_count /
    label_mismatch_count）按 review 结论留待 Phase 4，本轮未实现。

测试：全量（除 test_api_smoke.py）219 passed, 6 skipped（新增
test_direct_blocked_without_runs_or_quality 验证 #2 闸门拦截）。
test_mode_selection.py 同步更新以匹配新行为（Bootstrapping / 转换事件新字段 /
direct 准入闸门）。

----------------------------------------------------------------------
两个值得知道的观察 → 已处理（2026-08-14）
----------------------------------------------------------------------
观察 #1（environment_key 语义混合）：mode_selection 查询侧用「执行时前台 app 的
package/activity」+「目标 app 的 app_version/fixture」混合。find_matching_execution_plan
已按 app_package 过滤，package/activity 是弱信号，不会误判；两侧口径一致、匹配自洽。
已在 nodes.py 查询侧（~1427）加注释说明，未做大改（严格对齐需改用目标 App 首屏
activity，属后续增强，非 bug）。

观察 #2（in-place 改写副作用）：原注入直接 _page[key]=... 改 action_events 里的
共享 dict（list 浅拷贝）。reporter 是终态节点，原本无害，但已改为对 page dict 做
dict(_page) 浅拷贝再注入（nodes.py:1090-1103），消除对 ctx._action_events 的潜在
污染。测试 76 passed 不受影响。


  Thought for 4s (ctrl+o to expand)

● Bash(python -m pytest tests/test_plan_extractor.py
      tests/test_mode_selection.py tests/test_relational_plan_matching.py
      tests/test_rag_context.py tests/test_relationa…)
  ⎿          now = "2026-08-13T00:00:00"
             old_success = "2026-01-01T00:00:00"
             db.insert(
     … +37 lines (ctrl+o to expand)
  ⎿  (timeout 5m)

  Thought for 35s, searched for 1 pattern (ctrl+o to expand)

● Bash(python -m pytest
      tests/test_mode_selection.py::test_direct_node_records_timeout_status test
      s/test_plan_extractor.py::test_task_signature_falls_back_to_request_r…)
  ⎿  E         ?               ^^ ^ ^

     tests\test_mode_selection.py:346: AssertionError
     … +57 lines (ctrl+o to expand)
  ⎿  (timeout 2m)

  Thought for 32s (ctrl+o to expand)

● Update(tests\test_relational_metrics.py)
  ⎿  Added 6 lines, removed 4 lines
      346              {
      347                  "action_index": 0,
      348                  "tool_name": "click",
      349 -                "status": "OK",                               
      349 +                "status": "NOT_FOUND",                        
      350                  "page_before": {"package": "com.example.app", 
           "activity": "Before"},
      351 -                "page_after": {"package": "com.example.app", "
          -activity": "After"},                                          
      351 +                "page_after": {"package": "com.example.app", "
          +activity": "Before"},                                         
      352              }
      353          ],
      354          decay_lambda=0.01,
      355      )
      356      action = db.select("plan_actions", {"action_id": "action-1
           "})[0]
      357 -    # Without age decay, quality would be ~0.244. With ~220 da
          -ys of decay it should be lower.                               
      358 -    assert action["quality_score"] < 0.2                      
      357 +    # last_success_at stays old, so age decay applies to the s
          +tale success.                                                 
      358 +    assert action["last_success_at"] == old_success           
      359 +    # Without age decay, quality would be ~0.111. With ~220 da
          +ys of decay it should be near zero.                           
      360 +    assert action["quality_score"] < 0.05                     
      361      db._conn.close()
      362  
      363  

  Thought for 17s, read 1 file (ctrl+o to expand)

● Update(agents\plan_extractor.py)
  ⎿  Added 3 lines
      236      planner_semantics = goal_obj.get("action_semantics")
      237      if isinstance(planner_semantics, list) and planner_semanti
           cs:
      238          return [str(s) for s in planner_semantics if s]
      239 +    return extract_action_semantics_from_request(user_request,
          + goal)                                                        
      240 +                                                              
      241 +                                                              
      242  def extract_action_semantics(action_events: list[dict[str, Any
           ]]) -> list[str]:
      243      """Summarize action sequence into human-readable action se
           mantics."""
      244      semantics: list[str] = []

  Thought for 13s (ctrl+o to expand)

● Bash(python -c "from tools.results import parse_status;
      print(repr(parse_status('TIMEOUT: tool did not respond')))")
  ⎿  ''

  Thought for 9s (ctrl+o to expand)

● Update(tools\results.py)
  ⎿  Added 2 lines, removed 1 line
      27  NEEDS_HUMAN = "NEEDS_HUMAN"
      28  PASS = "PASS"
      29  FAIL = "FAIL"
      30 +TIMEOUT = "TIMEOUT"                                            
      31  
      32  # 顺序：更长/更具体的前缀在前，避免 ERROR 抢先匹配（此处无重叠
          ，仍保持稳定顺序）
      32 -_STATUSES = (OK, NOT_FOUND, AMBIGUOUS, NEEDS_HUMAN, ERROR, PASS
         -, FAIL)                                                        
      33 +_STATUSES = (OK, NOT_FOUND, AMBIGUOUS, NEEDS_HUMAN, ERROR, TIME
         +OUT, PASS, FAIL)                                               
      34  
      35  _EVIDENCE_SEP = " || "
      36  

  Thought for 5s (ctrl+o to expand)

● Bash(python -c "from tools.results import parse_status;
      print(repr(parse_status('TIMEOUT: tool did not respond')))")
  ⎿  'TIMEOUT'

  Thought for 4s (ctrl+o to expand)

● Bash(python -m pytest
      tests/test_mode_selection.py::test_direct_node_records_timeout_status test
      s/test_plan_extractor.py::test_task_signature_falls_back_to_request_r…)
  ⎿  ============================= test session starts ====================
     =========
     platform win32 -- Python 3.13.3, pytest-9.1.1, pluggy-1.6.0 -- C:\Prog
     … +15 lines (ctrl+o to expand)
  ⎿  (timeout 2m)

  Thought for 6s (ctrl+o to expand)

● Bash(python -m pytest tests/test_plan_extractor.py
      tests/test_mode_selection.py tests/test_relational_plan_matching.py
      tests/test_rag_context.py tests/test_relationa…)
  ⎿  ......................................................................
     .. [ 87%]
     ..........
     … +2 lines (ctrl+o to expand)
  ⎿  (timeout 5m)

  Thought for 5s (ctrl+o to expand)

● Bash(python -m pytest tests/ --ignore=tests/test_api_smoke.py -q 2>&1 | tail
      -30)
  ⎿  ssssss................................................................
     .. [ 32%]
     ......................................................................
     … +16 lines (ctrl+o to expand)
  ⎿  (timeout 10m)

  Thought for 36s, searched for 1 pattern, read 1 file (ctrl+o to expand)

● Update(agents\nodes.py)
  ⎿  Added 4 lines, removed 1 line
      1120                  list(state.get("mode_transition_events", []) 
            or []),
      1121              )
      1122              _relational_db.record_plan_action_outcomes(
      1123 -                str(state.get("plan_id", "") or ""), action_e
           -vents                                                        
      1123 +                str(state.get("plan_id", "") or ""),         
      1124 +                action_events,                               
      1125 +                decay_lambda=cfg.quality_decay_lambda,       
      1126              )
      1127              _relational_db.record_plan_action_alignment_event
            s(
      1128                  config.get("configurable", {}).get("thread_id
            ", ""),
     ...
      1133                  str(state.get("plan_id", "") or ""),
      1134                  test_verdict,
      1135                  config.get("configurable", {}).get("thread_id
            ", ""),
      1136 +                direct_quality_threshold=cfg.direct_quality_t
           +hreshold,                                                    
      1137              )
      1138          except Exception:
      1139              logger.exception(

  Thought for 11s (ctrl+o to expand)
● Bash(python -m pytest tests/ --ignore=tests/test_api_smoke.py -q 2>&1 | tail
      -10)
  ⎿  ......................................................................
     .. [ 64%]
     ......................................................................
     … +14 lines (ctrl+o to expand)
  ⎿  (timeout 10m)

  Thought for 4s (ctrl+o to expand)

● Bash(python -m py_compile agents/plan_extractor.py agents/state.py
      agents/nodes.py agents/graph.py agents/rag_context.py data/relational.py
      config.py tools/results.p…)
  ⎿  (No output)

  Thought for 4s (ctrl+o to expand)

  Thought for 5s (ctrl+o to expand)

● Bash(git diff --stat)
  ⎿  warning: in the working copy of 'agents/loop_control.py', LF will be r
     eplaced by CRLF the next time Git touches it
     warning: in the working copy of 'agents/prompts/agent_common.txt', LF
     … +79 lines (ctrl+o to expand)

  Thought for 8s (ctrl+o to expand)

---

## Phase 4 完成总结（端到端性能 & 质量门禁）

延续上一轮 Phase 3 收尾后未完成的工作，本轮完成 Phase 4 的核心落地。

### 变更清单
1. **`agents/run_trace.py`**
   - 抽出可复用的 `compute_resolution_metrics(tool_log)`，按 click 工具的
     `resolution_type`（exact/semantic）与标签错配（req/res 均非空且不一致 →
     label_mismatch）汇总；`build_run_trace` 改为复用该 helper，避免逻辑分叉。
   - 修复上一轮聚合循环误用 `step.get("tool_name")` 的字段名（应为 `tool`）。

2. **`agents/nodes.py`**
   - `reporter_node` 顶层 import `compute_resolution_metrics`（修正之前因函数内
     延迟 import 导致的 `UnboundLocalError`）。
   - 原 `exact_count`（误用 `match_mode == "exact"`，而 click 实际 match_mode 为
     resource_id/element 等，永不命中，形同死代码）改为由 `resolution_type` 派生，
     新增 `semantic_count`；trace 指标字典补 `semantic_click_count`。
   - `record_execution_run` 调用补齐 `resolution_metrics`、`duration_seconds`、
     `llm_call_count` 三项（Phase 4 端到端持久化）。

3. **`data/relational.py`**
   - `execution_runs` 表新增 `duration_seconds REAL`、`llm_call_count INTEGER`、
     既有 `resolution_metrics TEXT`（上一轮已加）。
   - `record_execution_run` / `list_execution_runs` 写入与读取新增列；
     `get_execution_run` 走 `SELECT *` 自动覆盖。
   - 新增 `summarize_by_mode()`：按 `execution_mode` 聚合通过率、平均/累计耗时、
     LLM 调用、模式转换次数、后置条件命中数、证据完整率、locator 解析指标；
     配套 `_count_in` / `_count_distinct_runs_with` 两个内部计数 helper。

4. **`tests/test_phase4_metrics.py`（新增）**
   - `compute_resolution_metrics` 基础汇总、标签错配（空 label 不计入）、空输入。
   - `record_execution_run` resolution_metrics/duration/llm_call_count 往返。
   - `summarize_by_mode` 跨 direct/explore 双模式聚合校验。

### 验证
- 全套（不含 test_api_smoke.py，其仅在缺 fastapi 的测试 venv 失败）：
  **224 passed, 6 skipped**。
- 新增 Phase 4 测试 19 项全绿。

### 备注
- 后置条件命中计数沿用 `action_events.status IN ('success','continue')`，与
  reporter 归一化后的 display-step status 口径一致（测试数据须用 `success` 而非
  `OK`）。
- 文档中提到的 Phase 4 回归集 / 差分 / 证据防篡改测试为可选项，本轮未做，留待后续。

---

## Phase 4 完成总结（端到端性能 & 质量门禁）

延续上一轮 Phase 3 收尾后未完成的工作，本轮完成 Phase 4 的核心落地。

### 变更清单
1. **`agents/run_trace.py`**
   - 抽出可复用的 `compute_resolution_metrics(tool_log)`，按 click 工具的
     `resolution_type`（exact/semantic）与标签错配（req/res 均非空且不一致 →
     label_mismatch）汇总；`build_run_trace` 改为复用该 helper，避免逻辑分叉。
   - 修复上一轮聚合循环误用 `step.get("tool_name")` 的字段名（应为 `tool`）。

2. **`agents/nodes.py`**
   - `reporter_node` 顶层 import `compute_resolution_metrics`（修正之前因函数内
     延迟 import 导致的 `UnboundLocalError`）。
   - 原 `exact_count`（误用 `match_mode == "exact"`，而 click 实际 match_mode 为
     resource_id/element 等，永不命中，形同死代码）改为由 `resolution_type` 派生，
     新增 `semantic_count`；trace 指标字典补 `semantic_click_count`。
   - `record_execution_run` 调用补齐 `resolution_metrics`、`duration_seconds`、
     `llm_call_count` 三项（Phase 4 端到端持久化）。

3. **`data/relational.py`**
   - `execution_runs` 表新增 `duration_seconds REAL`、`llm_call_count INTEGER`、
     既有 `resolution_metrics TEXT`（上一轮已加）。
   - `record_execution_run` / `list_execution_runs` 写入与读取新增列；
     `get_execution_run` 走 `SELECT *` 自动覆盖。
   - 新增 `summarize_by_mode()`：按 `execution_mode` 聚合通过率、平均/累计耗时、
     LLM 调用、模式转换次数、后置条件命中数、证据完整率、locator 解析指标；
     配套 `_count_in` / `_count_distinct_runs_with` 两个内部计数 helper。

4. **`tests/test_phase4_metrics.py`（新增）**
   - `compute_resolution_metrics` 基础汇总、标签错配（空 label 不计入）、空输入。
   - `record_execution_run` resolution_metrics/duration/llm_call_count 往返。
   - `summarize_by_mode` 跨 direct/explore 双模式聚合校验。

### 验证
- 全套（不含 test_api_smoke.py，其仅在缺 fastapi 的测试 venv 失败）：
  **224 passed, 6 skipped**。
- 新增 Phase 4 测试 19 项全绿。

### 备注
- 后置条件命中计数沿用 `action_events.status IN ('success','continue')`，与
  reporter 归一化后的 display-step status 口径一致（测试数据须用 `success` 而非
  `OK`）。
- 文档中提到的 Phase 4 回归集 / 差分 / 证据防篡改测试为可选项，本轮未做，留待后续。

● Bash(git diff -- agents/state.py agents/plan_extractor.py agents/nodes.py
      agents/graph.py agents/rag_context.py agents/prompts/planner.txt
      data/relational.py config.…)
  ⎿  warning: in the working copy of 'agents/prompts/planner.txt', LF will
     be replaced by CRLF the next time Git touches it
     warning: in the working copy of 'data/relational.py', LF will be repla
     … +198 lines (ctrl+o to expand)

  Thought for 18s (ctrl+o to expand)

● Phase 3 实施完成。

  改动摘要

  核心实现

  1. 动作语义不再在 mode_selection 时为空
    - agents/state.py: TestGoalOutput 新增 action_semantics
    - agents/plan_extractor.py: 新增 extract_action_semantics_from_request
  规则兜底 + _infer_action_semantics 三层来源（action_events → planner → 规则）
    - agents/prompts/planner.txt: 要求 planner 输出 action_semantics
  2. 环境匹配从 == 改为关键字段 + 评分
    - agents/plan_extractor.py: 新增 environment_compatibility_score
    - agents/rag_context.py: 向量/关系召回路径均用评分过滤
    - data/relational.py: find_matching_execution_plan 按环境分排序
    - agents/nodes.py: mode_selection_node 用评分决定 direct/guided/explore
  3. 环境数据缺失过渡方案
    - agents/nodes.py: 新增 _get_app_version（dumpsys package）
    - config.py / config.yaml: 新增 fixture_profile 占位
  4. Direct / Guided / Explore 受控降级
    - agents/nodes.py: direct_node 增加重入防御、精确状态解析、单次降级到 guided
    - agents/nodes.py: mode_transition_node 防止重复 guided→explore
    - agents/graph.py: route_after_evaluator 增加 direct 降级后安全守卫
    - agents/graph.py: _should_downgrade_guided 扩展 TIMEOUT/NOT_FOUND
  5. Per-action 负反馈与质量衰减
    - data/relational.py: plan_actions 新增
  postcondition_failure_count、last_success_at
    - data/relational.py: 质量公式加入年龄衰减 exp(-λ·age_days)
    - tools/results.py: 新增 TIMEOUT 状态码

  测试

  - 新增/更新 17 个测试，覆盖：
    - 动作语义三层来源
    - 环境兼容性评分
    - direct 需要满分环境
    - 环境微变进入 guided
    - 单次降级守卫
    - timeout/not_found 状态解析
    - postcondition failure 跟踪
    - 年龄衰减
    - direct approval 自动撤销

  验证结果

  - 关键测试：82 passed
  - 全量回归（除 test_api_smoke.py）：218 passed, 6 skipped
  - 所有修改文件 py_compile 通过

  仍保留的缺口（已知）

  - fixture_fingerprint 仍是 config.fixture_profile 占位，真实 fixture
  指纹待后续接入。
  - app_version 依赖 dumpsys 查询，失败时为空（不影响匹配）。

  下一步你要继续 Phase 4（端到端性能与质量门禁），还是先对 Phase 3
  做一轮端到端验证？

✻ Cogitated for 34m 26s

  Thought for 33s (ctrl+o to expand)

● 收到，action_semantics 采用方案 2：planner 优先 + 规则兜底。

  另外，当前我处于 plan 模式，按规则只能写 plan 文件，不能修改 settings.json
  等外部文件。claude-hud:setup 需要写入 settings，所以等 Phase 3 计划批准并退出
  plan 模式后再处理。

  我现在把最终计划写入文件并提交审批。

❯ /claude-hud:setup                                                             

  Thought for 1m 40s (ctrl+o to expand)

● 已退出 plan 模式。按方案 2（planner 优先 + 规则兜底）开始实现 Phase 3。

  Thought for 17s (ctrl+o to expand)

  Thought for 6s (ctrl+o to expand)
  ⎿  Interrupted · What should Claude do instead?
  ⎿  Interrupted · What should Claude do instead?
---

## Phase 4 completion summary (end-to-end performance & quality gate)

Continued the unfinished Phase 4 work after Phase 3 wrap-up.

### Changes
1. agents/run_trace.py: extracted reusable compute_resolution_metrics(tool_log)
   aggregating exact/semantic resolution + label_mismatch (only when both req/res
   labels non-empty). build_run_trace now reuses it. Fixed legacy wrong field
   step.get("tool_name") -> step.get("tool").
2. agents/nodes.py: top-level import compute_resolution_metrics (fixed
   UnboundLocalError from in-function lazy import). Replaced dead exact_count
   (match_mode=="exact" never matches click match_mode values resource_id/element)
   with resolution_type-derived exact_count + new semantic_count; added
   semantic_click_count to trace metrics. Wired resolution_metrics/
   duration_seconds/llm_call_count into record_execution_run.
3. data/relational.py: execution_runs gains duration_seconds REAL + llm_call_count
   INTEGER (resolution_metrics already added in prior round). Added
   summarize_by_mode() aggregating per-mode pass rate, avg/total duration, LLM
   calls, mode transitions, postcond hits, evidence completeness, locator
   resolution metrics; plus _count_in / _count_distinct_runs_with helpers.
4. tests/test_phase4_metrics.py (new): compute_resolution_metrics aggregation,
   empty-label fix, record_execution_run round-trip, summarize_by_mode across
   direct/explore.

### Verification
- Full suite (excluding test_api_smoke.py, which only fails in test venv missing
  fastapi): 224 passed, 6 skipped.
- New Phase 4 tests: 19 passed.

### Notes
- postcond hit count uses action_events.status IN ('success','continue'), aligned
  with reporter-normalized display-step status (test data must use "success", not
  "OK").
- Per-doc Phase 4 regression-set / differential / evidence-tamper tests are
  optional and deferred.


# Agent 演进 Plan（2026-08-24 完整版）

> 状态：**拟定（替代 code_review_findings_plan_20260822.md + phase0_gate_survey_20260822.md 的规划职能）**
> 来源：v1 分支全量代码现状核对 + 8/21-8/24 真实 run 复盘 + 外部评审四轮反馈闭环。
> 文档定位：本 Plan 是**唯一活跃的实现规划**；phase0 文档作为「第零批只读摸底 + 复测」交付物保留引用，不再独立演进规划。

---

## 0. 核心原则（不可违反）

本 Plan 所有条目都必须在这四条原则下成立，任何一条与原则冲突即视为设计错误：

**P1 — 人类测试员模型（产品形态锚）**
- 测试员脑子里的"计划"是隐式的：知道要测什么 → 看当前是否满足 → 顺手做（一次感知、批量判定多个点）→ 满足就过 → 全过出报告。
- 跑过一次的用例，下次**先依赖上次的路径与结果**（记忆复用），只有不满足/出问题的步骤才重新探索"怎么走"。
- 落地含义：① 不要求 agent 在跑之前生成一份独立 plan 文档再执行；② 验收清单（clause/spec）可内联进探索回路，不必落盘成独立阶段产物；③ **记忆复用（R 系列）是核心能力，不是可选基建**。

**P2 — 契约收敛、补丁发散；代码管"稳"、LLM 管"活"**
- 确定性判定（状态类 claim 必须由 element_state / page_state 等确定性证据判）是**代码不变量**，不靠 prompt 软约束。
- 场景规则下沉到工具契约（报错 + 示例模式），prompt 只留通用契约；每次想往 prompt 加一条规则前，先问"能不能做成工具错误信息"。

**P3 — 观测先行，不考古**
- 任何性能/正确性结论必须有结构化埋点支撑；**禁止每轮手工翻 langchain.log / trace 考古定位增量**。
- 时间账必须三段拆分（planner 耗时 / plan_review 等待 / 执行段），口径写进 trace schema。
- 命中率统计必须分桶（控件缺失类 vs 谓词/结构不支持类），埋点先于开闸。

**P4 — 诚实兜底，不假装管住**
- 覆盖不到的主战场（spec:null 状态类 claim）承认只能由 prompt 软约束 + evaluator 的 `unverified` 标记兜底并暴露给人工复核，不在代码里假装管住。
- n=1 样本不作趋势结论；均值结论必须等埋点上线后多轮聚合。

---

## 1. 当前代码真实现状（2026-08-24 核对）

| 维度 | 现状 | 证据 |
|---|---|---|
| 模式机 | explore / guided / direct 三层在位；`mode_selection_node` 用 `verification_fingerprint` 硬门（sha256 逐字相等）过滤候选 → 真实 rerun 全落 `no_matching_plan` → explore | nodes.py:1849,1875; plan_extractor.py:434 |
| planner 输出 | 仍走 `_parse_goal` 正则（nodes.py:2269）；`TestGoalOutput` 已定义未用；`with_structured_output` 未接 | nodes.py:248,2269; state.py:18 |
| planner 耗时观测 | `planner_node` 有 `_t0=time.time()` 计时但只打 log，未入 trace 字段；planner 单次实测 56/141/225s（服务端方差） | nodes.py:242-244; 8/24 run 考古 |
| plan_review 等待 | interrupt 人工审批，等待时长**零观测**；基线 ~60s、8/24 实测 ~180s | nodes.py:95（interrupt 边） |
| 视觉通道 | `visual_check`/`vision_tap` 走 `perceiver.perceive()`，无超时/退避/耗时埋点；单次 10-60s，离群 51/60s | tools/verify.py:236,296,522 |
| F1 通道收窄 | 未落地；`build_verification_contract` 仍全通道放行（_ALL_CHANNELS_FALLBACK） | verification.py:204 |
| F4 填充率 | prompt 杠杆已证：旧 prompt 4% → 新 prompt 80%（verification 级）；但 auto 命中仅 2/8，spec 质量是新瓶颈 | phase0 复测 2026-08-24 |
| 记忆复用基建 | `query_task_plan_summaries` 向量召回已存在；fingerprint 硬门阻断复用 | rag_context.py:74-75 |
| 时间账埋点 | `run_trace.py` 仅 `duration_seconds` 一个含糊字段，无三段拆分 | run_trace.py:122 |

**核心判断（回应"比修改前慢"复盘）**：8/24 跑得慢**不是改坏了**，主因是 run 开头 `planner 单次 +85s` + `plan_review 等待 +120s`（review 盲区，共 +200s），执行段视觉耗时反而因"禁自造渲染细节"规则比基线低 58s。最大增量在 planner/review 段，而这段正是 R1 落地后归零的对象。

---

## 2. 问题总表（按价值排序，F=正确性 / R=记忆复用效率）

### F 系列（判定/规划层正确性）

| ID | 问题 | 分级 | 位置 | 与原则关系 |
|---|---|---|---|---|
| F1 | 状态类 clause 无强制确定性证据代码不变量，全通道放行 | **P0** | verification.py:204,79-82 | P2 |
| F2 | `list_count` 全树子串计数 → 权威 FAIL 误停；Gate-B 零样本暂不动共用入口 | **P0(搁置)** | verification.py:681-848 | P3(观测不足不动) |
| F3 | clause 机械拆分碎片化（与"人类步骤级"相悖） | **P1** | verification.py:14 + planner | P1(人类模型) |
| F4 | planner 输出靠正则，spec 填充率靠 prompt 杠杆已抬到 80%；结构化输出降为可选加固 | **P1** | nodes.py:_parse_goal | P3 |
| F5 | Agent 缺录制回放回归体系 | **P1** | 新建基建 | P3 |
| F6 | execution_status 兜底过松 / 双 RPC / prompt 补丁化倾向 | **P2** | 多处 | P2 |

### R 系列（记忆复用 —— 你的核心能力，对应 P1 人类模型"跑过一次就记下来"）

| ID | 问题 | 分级 | 位置 |
|---|---|---|---|
| R1 | fingerprint 硬门（planner 输出逐字相等）阻断复用 → 回放从未生效 | **P0** | plan_extractor.py:434 + rag_context.py:96 |
| R2 | direct 准入漏斗 6 条件 + 隐藏 API 人工批准，最快第 3 次才提速；guided 不省 LLM | **P0** | nodes.py:1875 |
| R3 | direct 收尾仍交回整轮 agent LLM 循环，零 LLM 收口未实现 | **P0** | nodes.py:1974 |
| R4 | direct 执行质量：index 漂移 / 3×RPC / activity 全名精确匹配 | **P1** | nodes.py:2008,2022,2040 |
| R5 | 沉淀保留全部成功动作含绕路试错链，回放原样重演 | **P2** | plan_extractor.py:510 |
| R6 | 环境键口径双侧不一致（弱信号） | **P2** | nodes.py:1791 |

**R 系列定位修正（相较旧 Plan）**：旧 Plan 把 R 列为"当前产品主线"但底层假设是"重型 plan-save/replay 机制"。本 Plan 按你的澄清改为 **轻量历史记忆 replay + 失败回退探索**，不做指纹硬门全量回放。R1 的核心收益 = **planner 140s + plan_review 等待归零**（这正是 8/24 慢的最大增量来源）。

---

## 3. F1（P0）：状态类 claim 强制确定性证据

### 修法（契约式，不重新引入关键词推断 —— 守 P2/P4）
- 只对**带 spec 的 clause** 收窄：有效证据通道 = `{_spec_channel(spec.predicate)}`（写侧已有，读侧复用，单一来源）；spec:null 保持全通道回退不动。
- `evaluate_verification` 的 matching 集按上述通道过滤；authoritative 事件仍无视通道直入。
- 效果：spec=element_disabled 只能被 element_state 判 passed；visual_check 的 PASS 无效。

### 开闸门禁（守 P3/P4，修正旧 Plan 一处错配）
- **填充率前提已成立**（phase0 复测 80%），F1 可开闸。
- **命中率前提才是真 blocker**：开闸前须确认 auto 命中率达标。若命中率不足，收窄通道会把假阳性换成更隐蔽的假阴性（clause 卡 unknown，agent 反而补验更久）。
- **命中率阈值（钉死口径，避免开闸判定变主观题）**：`_match_spec` 返回 None 的 clause 先按埋点分桶——**「控件缺失类」（用例本就没走到那步/页面无该控件）不计入分母**；分母仅取「谓词或结构不支持类」（含 target 同义改写落空、label 链漏拼等覆盖不足）。开闸硬阈值：**谓词/结构不支持类占比 ≤ 30%**（即 auto 可覆盖的 spec clause ≥ 70%）才开闸 F1；>30% 先修匹配器/planner target 约束，不开闸。此埋点随 F4 落地（phase0 §Gate-A② 已确认 `evidence_events` 不持久化 auto/authoritative，需最小埋点）。
- **F1 隐性提速收益（补进行 Plan）**：收窄通道后，UI tree 可判定的事不再走 vision——直接灭掉 8/24 seq60 那种"get_screen_info 已打 [SELECTED] 还走 46s vision"的浪费。这是 F1 的二等收益，旧 Plan 没写。

### 验收
1. 带 element_disabled spec 的 clause：仅注入 vision_verify PASS → unknown；注入 element_state PASS → passed；
2. spec:null clause 行为完全不变（回归不破）；
3. 现有 336 测试全绿（phase0 复测基线）。

---

## 4. F2（P0 搁置）：list_count 口径

Gate-B 零样本 → 误报率无样本可评。**按 P3 观测不足不动**：F2 不改 `_find_elements` 共用入口；等 F4 抬填充率、list_count 出现真实样本后再评估是否只动 list_count 分支。动之前必跑全量 pytest 留基线（phase0 已记）。

---

## 5. F3（P1）：clause 合并到"人类步骤级"

### 根因（与 P1 人类模型冲突）
`_CLAUSE_BOUNDARY` 按标点机械切分，枚举型文本被切碎 → 每条独立走全套验证通道 → 冗余步骤。planner.txt 已告诫模型避免碎片化，但代码侧机械切分原样保留，两道防线打架。

### 修法（拆分权交还 planner，代码只兜底）
- planner 输出升级：`verification` 项支持 `{"claim":..., "clauses":[...]}` 对象形式，planner 在规划期完成**语义合并**（对应"人类一个操作顺带验多个点"）。
- `_split_claims` 降级为仅处理旧字符串项的 fallback，且去掉 ASCII `,` 边界（枚举逗号不拆）。
- 复测实证：verification 项从 9→12、clause 18→22（更碎），但步骤数 137→117 实际下降——目前碎片化还没在 wall-clock 爆雷，只是埋雷。F3 在 R1 落地（去掉独立 plan 阶段）后更关键：没有 plan 兜底，clause 必须够"整" agent 才不反复核对。

### 与「规划期粒度膨胀」的分工（问题3 澄清）
F3 只管**机械切分**（代码侧 `_CLAUSE_BOUNDARY` 把一句话切碎），**不管规划期粒度膨胀**（planner 主动生成过多 verification 项，如实证 spec 推高后 verification 反而 9→12）。后者由**第一批的 `planner.txt` 粒度硬约束**独立治理（验证项数 ≤ 用例预期句数、禁「可编辑/入口」类子特征项、交互验证只取一个代表样本）。两件事互补：粒度硬约束压住"生成多少项"，F3 压住"每项被切多碎"。且粒度硬约束须在 R1 大量沉淀 plan 之前落地——否则细粒度契约先沉淀被永久复用。

### 验收
1. 「勾选周一,周三,周五后显示3节课」经新格式 → 1~2 条完整语义 clause；
2. 旧字符串输入回归不破；
3. `validate_contract_spans` 适配无 gap/overlap 误报。

---

## 6. F4（P1）：planner 输出 —— prompt 杠杆已证，结构化输出降为可选加固

### 现状与结论（phase0 复测已闭环）
- prompt 杠杆实测生效：旧 prompt 填充率 4% → 新 prompt 80%（verification 级），远超 20% 复测线。
- **根因纠正**：旧 4% 是"旧 prompt 从没要求填 spec"，不是"模型 disobedience"（git 证据 b50dc82/a8a25a6 已证）。地基问题在 prompt，不在解析器（`_parse_goal` 正则能从对象抽到 spec，前端 edit 回传保留 spec，管线不丢）。
- **因此 `with_structured_output` 从主攻降为"填充率达标后的可选加固"**，不是前置条件。

### 落地顺序（守 P3 观测先行）
1. planner.txt 已做：删 `:13「可选」` + `:28「拿不准就别写」` 逃生门，few-shot 改对象形式，追加三条 target 约束（照抄界面真实文本/rid、element_absent 仅限弹窗高特异靶、toast 不写 spec）；
2. auto 匹配器两处 bug 已修（惰性初始化门 + page_contains 补 label 链），19 条单测，336 passed；
3. **最小埋点（并入 F4）**：`auto_record_evidence` 内对 hit / dedup-skip / None+原因码 各打一行结构化日志，解决 phase0 §Gate-A② 三个观测缺口；
4. 填充率已达标，`with_structured_output` 作为加固项择机接（解析失败 fallback 正则路径，降级计入 trace）。

### 验收
1. 真机 run 无 `_parse_goal` 降级日志（若接结构化输出）；
2. 命中率分桶埋点产出，可观测 spec 质量仪表盘。

---

## 7. F5（P1）：Agent 录制回放回归体系

- 抽取 `tests/test_verification_contract.py` 已有的 `FakePerceiver/FakeDevice` 为共享测试基建（勿从零建）；
- 从历史 passed trace 提炼「步骤→工具调用→页面快照」脚本入库（歧义/高频/恢复三类）；
- CI 钩子：核心改动跑回放集 diff（verdict / clause 状态 / step 数 / 证据通道）。
- 边界诚实（P4）：mock 回放验不出感知竞态，保的是"决策/判定逻辑回归"不是"设备时序"。

---

## 8. F6（P2）：小项集合

| 项 | 修法 | 原则 |
|---|---|---|
| `_determine_execution_status` 兜底过松（`len(history)>=3` 即 completed） | 收窄：无 terminal_verdict/conclusion 且 step<阈值时维持 error | P2 |
| agent_node 双 `current_app()` RPC | 合并为一次复用 | P2 |
| prompt 补丁化倾向 | 场景规则下沉工具契约；**本次新增的"清单✓后禁重复操作""视觉断言禁自造细节"等 prompt 规则，待 F1/F3 代码侧修复后下沉，不再往 prompt 堆**（F6 反模式实证） | P2 |

---

## 9. R1（P0）：记忆复用替代指纹硬门 —— 核心提效

### 现状与根因（核对真实代码）
- `verification_fingerprint` 对 planner **本次运行输出**做 sha256，检索侧要求与候选 plan 逐字相等 → planner 带温度 LLM 同一用例两次措辞必不同 → `no_matching_plan`。
- 身份锚建立在"LLM 输出逐字稳定"这个不成立的前提上。

### 修法（契约由人审、复用自动过 —— 守 P1 人类模型 + P2）
1. **检索前移**：`planner_node` 之前先按 `user_request` 向量召回候选 task_plan（复用 `kb.query_task_plan_summaries`，rag_context.py:74 已有）；
2. **高置信命中 → 跳过 planner LLM + 跳过 plan_review 人工审批**：把存储 plan 的 `verification_contract_json` **原文**作为提案，在 `plan_review` 节点以「契约已人工审批」**自动通过**（trace 记 `auto_approved_reason=reuse_hit` 留审计），直达 mode_selection → direct。**这是「跑一次 + N 次回放」里 N 段零人工等待的关键**——新用例首次人工审契约、之后转全自动，正是沉淀的本职意义；
3. **仅以下三种情况回落人工审**：① 低置信/无命中（走现状 planner → plan_review 人工审）；② 用户在回放前明确要求改契约；③ R1 复用命中但 env 不兼容（落入 guided 路径仍走人工确认）。其余复用命中一律 auto_approved，不卡人；
4. 契约原文复用后 fingerprint 自然逐字相等，R2/R3 的 direct/guided 匹配随之打通。
> 注：本修法与 §16「plan_review 跳过（复用命中时）→ run 开头段归零」严格一致——**复用命中时 plan_review 必跳过，否则「几十秒量级」目标落空**。

### 验收
1. 同一用例第二次运行（replay=true 且复用命中）：planner LLM 调用为 0 **且** trace 记 `auto_approved_reason=reuse_hit`、无 plan_review interrupt 等待、`execution.mode=direct`；
2. 低置信/无命中：走现状 planner → plan_review 人工审，planner LLM 调用 ≥1；
3. 用户在 plan_review 修改契约 → 走现状路径，不误用旧 plan；
4. 「跑一次 + N 次回放」场景：N 段均无人工等待（自动通过留审计），run 开头段归零。

---

## 10. R2（P0）：显式回放意图解锁 direct

- `RunRequest` 增加 `replay: bool`（前端报告页加「回放」按钮）；
- `replay=true` 时准入收窄为三条硬安全条件：契约复用命中（R1 保证）+ env 兼容（score≥阈值）+ 存在可执行动作；命中即进 direct，保留"任一动作失败立即降级 guided"护栏；
- **R2 补充（与 §9 衔接）**：`replay=true` 且 R1 复用命中时，`plan_review` 以 `auto_approved_reason=reuse_hit` 自动通过（不卡人工）；仅低置信/无命中/用户改契约才回落人工审。这正是"新用例首次人工审、之后全自动"的产品决策落地，也是 §16「几十秒量级」目标的前提；
- **direct 重演动作范围（与 R3 衔接）**：direct 只重演**导航动作**（click/long_press/launch_app 等改变页面的动作）；验证类动作（assert_*/vision_tap/click_and_check）**不重演**，交 R3 收尾由 `auto_record_evidence` + evaluator 纯代码判定。理由：57 条沉淀动作里 21 条 assert + 4 条 vision_tap，assert 便宜但重演无意义，vision_tap 重演一次就是一次 VLM 钱——验证交给收尾自动判定既省钱又和 R3 零 LLM 收口一致；
- 非 replay 隐式重跑维持现有保守阶梯不动。

### 验收
1. 已沉淀 plan 的用例带 `replay=true` 重跑：首跑即 `execution.mode=direct`；
2. 破坏 precondition（切错页面再回放）：首动作即降级 guided，run 不失败；
3. 不带 replay 的普通重跑行为完全不变；
4. replay 提速指标以 F1 开闸（命中率达标）为前提，未达标期间放宽"duration/llm_call_count 较 explore 基线下降 ≥70%"（旧 Plan 三处已覆盖，无需补）。

---

## 11. R3（P0）：direct 收尾零 LLM 收口

- 动作耗尽分支改为：执行一次 perceive + `auto_record_evidence` 后路由 evaluator；
- verdict=passed/failed → 直接 reporter；unknown 才降级 agent 补验（unknown 回环机制已存在天然兜底）；
- 防回环：耗尽标志必须在进入收尾分支时即刻置位（不等 evaluator verdict），与 unknown 回环互斥，单测锁定（旧 Plan 双护栏互斥节已详述）。

### 验收
1. 全 spec 覆盖用例 replay：整轮 `llm_call_count==0`、verdict 正确、无 guided 回退；
2. 含非 spec clause：导航零 LLM，仅收尾补 unknown 起 agent；
3. 末 clause unknown 压力用例：降级补验一次即收敛，不重放动作（互斥单测锁定）；
4. `llm_call_count==0` 以 F1 命中率达标为前提；未达标放宽"显著低于 explore 基线"。

---

## 12. R4 / R5 / R6（P1/P2）

| ID | 修法 | 验收 |
|---|---|---|
| R4 | **小步①（前置第二批，R2/R3 地基）**：沉淀时剥离全局 `index`，归一化为 `rid`/`path_contains`/`label`——实证 113252 沉淀 57 条动作里 9 条带全局 index（如 `[1] click input={"index":3}`），index 跨快照漂移是已知道的 R2/N2 教训，不先归一化 direct 回放会频繁误命中→降级 guided，R2/R3 验收（二跑 direct、llm≈0）不稳定。**小步②（留第五批）**：单动作 RPC 3→2；precondition activity 短名宽松匹配 | ①UI 微变（元素增删致 index 漂移）replay 仍命中；②RPC 3→2 |
| R5 | 沉淀时启发式剪枝 no-op 绕路（page_after==前动作 page_before 且后续不依赖），产新版本 plan 不原地改 | 含绕路 trace 沉淀后动作数明显减少 |
| R6 | 环境键双侧统一为「目标 App 冷启动后首屏 package+activity」 | 换桌面 launcher 不影响匹配 |

---

## 13. 时间账三段拆分（观测治理，P3 —— 回应 8/24 复盘盲区）

### 为什么必须做
8/24 复盘最大增量（planner +85s + plan_review +120s = +200s）完全靠手工考古日志定位——`run_trace.py` 只有 `duration_seconds` 一个含糊字段。每轮复盘都重付这个成本。**R1 落地后 planner 140s 直接归零，三段拆分正是验收它的仪表。**

### 修法（最小改动）
- `run_trace.py::build_run_trace` 扩展 trace schema，新增三个字段：
  - `planner_elapsed_seconds`（nodes.py:242 已有 `_t0` 计时，写入 trace）；
  - `plan_review_wait_seconds`（interrupt 进入/离开时间戳差，需在 plan_review 节点加计时）；
  - `execution_elapsed_seconds`（首动作时间 - plan_review 结束时间反推，或执行段独立计时）。
- `duration_seconds` 保留为三者之和，口径写明。
- 视觉调用（`tools/verify.py` 的 visual_check/vision_tap）加**耗时埋点**（不急着设超时阈值），先观测分布。

### 验收
1. 一轮 run 的 trace 含三段独立字段，且三段之和 == duration_seconds；
2. 视觉调用耗时进 trace/metrics，可画分布。

---

## 14. 视觉通道治理（回应"视觉贵"——半同意建议）

- **不急着设超时数值**：8/24 seq84 的 51s 调用最终返回 high-confidence YES——收太紧会把"慢但成功"变"快但失败"。vision 链路刚稳定。
- **稳妥第一步**：先按 §13 加视觉耗时埋点观测分布，再定阈值；若必须设上限，必须带**降级护栏**（超时 fallback 到 UI 树点击，不丢动作）。
- **真问题在通道纪律不是超时**：UI tree 可判定却走 vision（seq60 类）由 F1 收窄治理，不靠往 prompt 加规则（避免 F6 补丁化）。
- "同 clause 重复视觉确认"——prompt 已有该约束（清单✓后禁重复操作），无需新规则；真实缺口是通道选择，归 F1。

---

## 15. 落地顺序（基于核心原则重排）

```
第零批（已完成，只读摸底 + 复测）：
   phase0_gate_survey：填充率 4%→80%（prompt 杠杆证效）、命中率分桶缺口、
   list_count 零样本。结论已吸收进本 Plan F1/F2/F4。

第一批（正确性护栏 + 观测基建 + 粒度收敛）：
   F1 通道收窄（命中率达标后开闸）
   → 同步最小埋点（F4 并入：hit/dedup-skip/None 分桶）
   → 时间账三段拆分（§13，观测先行 P3）
   → **planner.txt 粒度硬约束（10 分钟级，问题3）**：验证项数 ≤ 用例预期句数、禁止「可编辑/入口」类子特征项、交互验证只取一个代表样本（须在 R1 大量沉淀 plan 之前落地，否则细粒度契约先沉淀被永久复用）
   → F2 搁置（Gate-B 零样本）

第二批（记忆复用 —— 你的核心能力，对应 P1 人类模型）：
   **R4 小步① index 归一化（前置地基）**
   → R1 复用替代指纹硬门（planner 140s 归零 + plan_review 复用命中自动过）
   → R2 replay 意图解锁 direct（复用命中自动 approve）
   → R3 direct 收尾零 LLM（只重演导航动作，验证交收尾自动判定；以 F1 命中率达标为前提）
   → verify: 同类用例二跑 direct、llm≈0、planner_elapsed=0、plan_review 无等待

第三批（人类步骤级合并）：
   F3 clause 合并到"一个操作顺带验多点"（复用 F4 对象格式）

第四批（迭代速度基建）：
   F5 录制回放回归集 → verify: 3 条 trace 回放一致

第五批（随手）：
   F6 小项 + R4/R5/R6 → verify: 对应单测
```

### 依赖说明
- **F1 与 R 正交但 R 收益依赖 F1**：R3 零 LLM 收口靠 F1 抬起的填充率+命中率；填充率不足时收尾仍 unknown → agent 补验 → LLM 归不了零。故 F1 开闸（命中率达标）是第二批前置。
- **R1 是 R2/R3 前置**：复用命中后才能跳过 planner，direct 匹配才通。
- **F3 依赖 F4 对象格式**：随其后。
- **观测基建（§13）与第一批并行**：不阻塞任何功能，但每批验收都依赖它，优先做。

---

## 16. 量化预期（修正旧 Plan 口径）

- 现状同类重跑（explore 从零，8/21 两个独立 run 口径，按 P4 度量纪律分列不混）：
  - 203810（passed）：1035s / 115 次 LLM；
  - 201420：1198s / 121 次 LLM；
  - 取区间参考 ~1015-1198s / 115-121 次 LLM，下文「基线」均指此区间而非单点。
- 8/24 复盘真实增量账（113252 vs 203810，+117s 净增，含 wall-clock 误差）：
  - planner 单次 +85s（review 盲区，服务端方差 56/141/225s）
  - plan_review 等待 +~120s（review 盲区，估算）
  - agent 循环慢调用离群 +90s
  - 工具总耗时 −60s（视觉 −58s，"禁自造渲染细节"起效）
  - 感知/RPC 残差 +56s
- **第二批完成后目标**：planner_elapsed=0 + plan_review 跳过（复用命中时）→ run 开头段归零；执行段 direct 导航零 LLM + 收尾自动证据 → 几十秒量级、0-2 次 LLM。优于旧 Plan 原定 1015s→~200s。
- **观测口径**：trace 三段字段（§13）+ `execution.mode` + `llm_call_count`。
- **前置耦合**：R 批次"0-2 次 LLM"以 F1 命中率达标为前提；未达标期间放宽"较 explore 基线下降 ≥70%"。
- **度量纪律（P3/P4）**：n=1 不作趋势；均值结论等三段埋点上线后多轮聚合。

---

## 17. 风险与权衡

1. **F1 收窄可能压低 spec clause 通过率**：命中率不足时 clause 卡 unknown，agent 补验更久。缓解：命中率分桶埋点达标再开闸（§3 门禁）。
2. **R1 跳过 planner 的风险**：复用契约若与当前 app 版本漂移会误判。缓解：env 兼容 score + 首动作失败即降级 guided（R2 护栏）。
3. **F3 动 planner 输出格式属行为类改动**：需真机回放 + plan_review 前端适配确认。
4. **F5 mock 保真度边界**：保决策/判定回归，不保设备时序（P4 诚实）。

---

## 18. docs 治理

- 本文件 `agent_evolution_plan_20260824.md` 为唯一活跃规划文档。
- `code_review_findings_plan_20260822.md` 与 `phase0_gate_survey_20260822.md` 标记为 superseded（规划职能并入本文件；phase0 的"第零批交付物"结论仍被本文件引用，不失效）。
- 旧 active 文档 `time_optimization_plan_20260821.md` / `explore_mode_learning_plan_20260813.md` 维持（战略层未做项追踪）。

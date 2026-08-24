# 第零批只读摸底结果（Gate-A / Gate-B）

> 日期：2026-08-22
> 状态：**已完成**（未改任何产品代码；新增只读脚本 [scripts/phase0_gate_survey.py](../scripts/phase0_gate_survey.py)）
> 数据源：`storage/test_history.db`（21 条 execution_runs + 16 条 execution_plans + 310 条 evidence_events）+ `logs/runs/*_langchain.log`（M4a 后 9 个 run 有 planner 原始输出可对账）
> 结论速览：**Gate-A 不达标 → F1 不开闸，主战场确认为 F4 抬填充率；Gate-B 零样本无法评估 → F2 暂不动共用入口。**

---

## Gate-A ① spec 填充率

| 口径 | runs (21条) | plans (16条, 全部 approved) |
|---|---|---|
| verification 级（首 clause 有 spec / 总 verification 数） | **5/124 = 4%** | 6/89 = 7% |
| clause 级（有 spec 的 clause / 总 clause 数） | 5/218 = 2% | 6/149 = 4% |

- clause 级天然低于 verification 级：[verification.py:193-195](../agents/verification.py#L193-L195) 只把 spec 挂在每条 verification 的**首 clause**，其余子 clause 恒为 null——clause 级天花板受平均拆分粒度压制，**评估 planner 服从度应以 verification 级为准**。
- 时间线注：M4a（`_match_spec` + `auto_record_evidence`）于 08-20 14:01 落地（b50dc82），此前 run 无 spec 输出格式属预期；落地后的 9 个 run 中仅 3 个写了 spec（共 5 个），prompt 引导效果差。

## Raw 对账：低填充率的根因归属

逐 run 对比 planner 原始输出中的 spec 数 vs 落库 contract 的 spec 数：

- **全部一致**（0=0 × 15、1=1、2=2 × 2），无一例「写了但丢」。
- 管线各环节均不丢 spec：[build_verification_contract](../agents/verification.py#L155-L178) 支持对象形式并保留 spec；plan_review 编辑后由后端重建 contract 时前端会回传 `{claim, spec}` 对象（[App.vue:1042-1048](../frontend/spa/src/App.vue#L1042-L1048)）；持久化原样落库。
- **结论：填充率 4% 完全是 planner 模型行为问题**（F4 的主攻点），不是解析/编辑/沉淀链路问题。
- 反例佐证：test-20260821_152828 的 planner 给 v1.0 写了 `element_enabled` 谓词——planner.txt 明说 enabled 态不要单列谓词。即 planner 对词汇表的整体服从度都偏低，不只是"懒得写 spec"。

## Gate-A ② `_match_spec` 命中率

分母 = 已填 spec 的 clause（仅 5 个，样本不足，以下为个案记录而非统计结论）：

| run.clause | 谓词 | auto 签名写入 | 通道级覆盖 | 说明 |
|---|---|---|---|---|
| 183526 v0.0 | page_is | ✗ | ✓ page_state PASS | 覆盖证据来自其他写入方 |
| 152828 v0.0 | element_exists | ✗ | ✓ element_state PASS | 手动 verify 先写 |
| 152828 v1.0 | element_enabled | ✗ | **✗ 无任何元素态证据** | 仅 behavior_effect/ui_text |
| 160432 v0.0 | page_contains | **✓** | ✓ | 唯一一条 auto 写入 |
| 160432 v1.0 | element_enabled | ✗ | ✓ element_state PASS | 手动 verify 先写 |

**离线统计在结构上不可靠，原因有三（本次摸底的观测缺口发现）：**

1. `evidence_events` 表未持久化 `auto` / `authoritative` 字段（[relational.py:385-400](../data/relational.py#L385-L400) 只存白名单列），事后无法归因证据来源；
2. [_has_same_evidence](../agents/verification.py#L859-L874) 去重不区分写入方：手动 verify 先写过同通道同状态时，auto 写入被静默吞掉——"DB 里没看到 auto 记录" ≠ "auto 没命中"；
3. `_match_spec` 返回 None 时不落任何记录，且 trace 只落页面签名（`page_after_json`）无结构化控件树，None 分桶（控件缺失类 vs 谓词/结构不支持类）无法离线完成。

→ **若要精确测命中率与分桶，需一个最小埋点**（`auto_record_evidence` 内对 hit / dedup-skip / None+原因码 各打一行结构化日志即可，不改行为）。建议并入 F4 一并实施，避免二次开闸摸底。

## Gate-B list_count 使用频次

- runs contract 中 list_count spec：**0 条**；plans contract：**0 条**；自动证据中 list_count 记录：**0 条**。
- 全链路零使用 → **误报率无样本可评**。按 Plan 决策规则保守处理：**F2 不动 `_find_elements` 共用入口**；等 F4 抬填充率、list_count 出现真实样本后再评估是否只动 list_count 分支。

## 开闸判定与下一步

| 门 | 判定 | 后续动作 |
|---|---|---|
| Gate-A 填充率 | **不达标**（4% vs 任何合理阈值） | F1 不开闸；第一批按 Plan 做 **F4**（planner 引导强化 + plan_review 前端适配一等任务） |
| Gate-A 命中率 | 样本不足 + 结构上不可测 | 最小埋点随 F4 落地，下批数据再测 |
| Gate-B list_count | 零样本 | F2 搁置共用入口改动 |

- R 批次前置耦合声明生效：填充率未达标期间，replay 提速指标按 Plan 放宽口径（较 explore 基线下降 ≥70%）。
- Plan 全文见 [code_review_findings_plan_20260822.md](code_review_findings_plan_20260822.md)（已入库）；本文档即其「第零批」的交付物，开闸判定直接引用 Plan 落地顺序节。

---

## 更正与补充（2026-08-22 复核，git 实证）

> 外部 review 指出统计时间窗疑点，经 git 时间线核实**属实且比指出的更彻底**，
> 本文前两处归因据此更正；Gate-A/Gate-B 的数字与判定结论不变。

### 时间线事实

```
b50dc82  08-20 14:01  planner.txt 加入「谓词词汇表」节，element_enabled 仍列为合法谓词；
                     同提交落地 M4a 代码（_match_spec / auto_record_evidence）
   （08-21 全天无任何提交动过 planner.txt）
a8a25a6  08-22 11:12  planner.txt 删除 element_enabled + 新增 clause 粒度规则（矛盾3修复）
```

本摸底全部 21 条 run 最晚到 **08-21 20:56** —— 即全部跑在 b50dc82 版 prompt 上；
HEAD 版 prompt（含矛盾3修复与 clause 粒度规则）**从未被任何已入库 run 测过**。

### 更正 1：「prompt 引导效果差」归因撤回

原文（Gate-A① 节）：「落地后的 9 个 run 中仅 3 个写了 spec……prompt 引导效果差」。

更正：b50dc82 版 prompt 对 spec 的措辞是「**可选**」，且无任何强制/默认填写要求。
在可选语境下「9 个 run 仅 3 个写 spec」不能作为模型服从度的证据。
填充率 4% 的根因从「planner 模型行为」修正为「**旧 prompt 从未要求填，prompt 杠杆尚未拉过**」。
raw 对账「管线不丢 spec」的结论不受影响（那是结构性验证，与 prompt 版本无关）。

### 更正 2：「152828 写 element_enabled 反例佐证服从度低」撤回

原文（Raw 对账节）：「test-20260821_152828 的 planner 给 v1.0 写了 element_enabled——
planner.txt 明说 enabled 态不要单列谓词」。

更正：152828 运行于 b50dc82 版 prompt，当时 `element_enabled` 是词汇表中的**合法谓词**，
该输出完全合规，「旧习惯/服从度低」的说法不成立。（「F4 引导中点名严格用 `_PREDICATES`
词汇表、不臆造谓词」作为廉价防御仍值得并入。）

### 对下一步的影响：F4 实施顺序微调

原 Plan 第一批把 F4 写成「结构化输出 + 引导强化」。经此复核调整为：

1. **先做 prompt 实验**（最便宜杠杆）：planner.txt 把 spec 从「可选」改为「终态/状态类
   claim 默认填」（收紧 :13「可选」与 ：28「拿不准就别写」两个逃生门），同步把
   「输出格式」行与示例改为演示对象形式——当前 few-shot 示例输出的 verification 全是
   纯字符串，本身在反向示范；
2. 真机跑 1-2 条用例后**重跑 [phase0_gate_survey.py](../scripts/phase0_gate_survey.py)**
   （脚本零改动可复用）看填充率是否跳升；
3. 填充率仍 <20% 才坐实主攻 `with_structured_output`（F4 原方案）。

另：review 提议的「Plan 补一行未达标 KPI 放宽口径」**不需要**——Plan 已在三处显式覆盖
（R2 验收4 :304-306、R3 验收5 :344-346、量化预期·前置耦合声明 ：416-420，均含
「下降 ≥70%」放宽条款）。

---

## 复测（2026-08-24）：prompt 杠杆实验结果

> 按「更正与补充」节方案执行：planner.txt 三处改动后，真机重跑联想日历_182
> （历史三次 0 spec 的样本）。run 因耗时过长手动停止（verdict=inconclusive，
> 不影响测量——planner 输出发生在 run 最早期，raw 输出与落库 contract 完整）。

### 填充率：达标

| 口径 | 旧 prompt（_182 × 3 次） | 新 prompt（test-20260824_100054） |
|---|---|---|
| verification 级 | 0%（0/6、0/8、0/9） | **80%**（8/10） |
| clause 级 | 0% | 33%（8/24） |

远超 20% 复测线 → **prompt 杠杆生效，「先 prompt 后结构化输出」的顺序调整被证实，
`with_structured_output` 维持为可选加固而非主攻；F1 开闸的填充率前提成立**。

### spec 质量成为新瓶颈（Plan 风险 1 应验）

8 个 spec 中实际产出 auto 证据的仅 **2/8**（均为 element_absent），问题三类：

1. **target 同义改写 → auto 必落空**（最普遍）：
   - `element_checked 全部`——真实控件叫「全选」（behavior_effect anchor 与 manual verify 双重实证）；
   - `element_exists 课程名称`——实际 label 是「课程名」，子串反向不匹配 → `_match_spec` 恒 None。
2. **泛化 target + element_absent（唯一 authoritative FAIL 谓词）= 误停地雷**：
   - v6/v7 以 `element_absent 全部` 间接证「弹窗关闭」，本次侥幸 PASS；
     编辑页他处一旦出现「全部」字样即权威 FAIL 误杀 run。
3. **注定落空型（无害）**：v5 将 toast 文本映射 `page_contains`——toast 不进 UI Tree，恒 None。

### 处置

- planner.txt「规则」行追加三条实证约束：target 照抄界面真实文本/rid 不得同义改写、
  element_absent 仅限弹窗独有高特异靶、toast 不写 spec；
- agent_common.txt 新增视觉断言禁自造渲染细节规则（run103147 权威 FAIL 根因：
  agent 预言了用例未声明的具体样式，被 VLM 字面对照否决）；
- **auto 匹配器两处 bug 修复**（2026-08-24，原则「能过的都给过，过不了的标记出来」）：
  1. [verification.py](../agents/verification.py) `auto_record_evidence` 初始化门：
     原以 `hasattr(ctx, "_evidence_events")` 早退——该属性由首个手动 verify 才创建，
     导致此前所有 perceive 的自动匹配静默空转；改为缺属性时惰性初始化；
  2. `_match_spec` 的 `page_contains` 拼页文本只取 text/desc/rid，漏掉 label 兜底链
     （含 associated_label），字段标签类元素永远命中不了；补拼 `e.label`。
     各补一条锁定单测（[test_auto_evidence_m4a.py](../tests/test_auto_evidence_m4a.py)
     共 19 条）；全量回归 336 passed / 6 skipped 无破坏。
- auto 命中率作为指标透出（原计划项）继续有效——它是观测 spec 质量的仪表盘；
- 时间成本注记：本次 explore 全量验证约 22 分钟被手动停止——正是 R 批次（回放开闸）
  要解的产品痛点，与本实验无关，但构成 R1-R3 优先级的又一实证。

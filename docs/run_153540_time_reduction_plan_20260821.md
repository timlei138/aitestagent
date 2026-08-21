# Run 153540 时间损耗精确定位与削减方案（含 Prompt 模板统一管理）

- 日期：2026-08-21
- 关联 run：`logs/runs/155600_test-20260821_153540_trace.json`（已完成 `passed`，`duration_seconds=1147.1`）
- 前置 Plan：`run_213229_time_analysis_and_optimization_plan_20260821.md`（战略根因 explore / fixture 已分析，本 Plan 聚焦**战术层"为什么还这么长"的精确根因**，基于 153540 实测埋点）
- 已落地改动（本 Plan 讨论期间）：`context_history_steps` 5→4（`config.py`/`config.yaml`/`nodes.py` 兜底同步）；`agent_explore.txt` / `agent_common.txt` 新增多选组段（全选优先 + toggle 取消）。
- 结论先行：
  - 153540 是修完 4 个致命 bug + 批处理 `targets` 落地后的**首个干净跑完**的 run（`passed`）。但 `duration=1147.1s` 仍偏长。
  - 时间损耗已从"战略 explore 重演"转为**两个可精确测量的战术根因**：
    1. **设备交互侧 sleep 膨胀**：`click.py:762` 对**所有 checkbox-like 控件**点击后统一 `sleep(2.0)`，而周数 chip 是即时 toggle，不需 2s → 批量选 20 周 = 20×2s = 40s 纯等待（实测 seq69=58s / seq95=52.9s）。
    2. **LLM 上下文膨胀**：`input_tokens=7.44M / 95 calls ≈ 78K tokens/次`，`cached=6.29M`（66%）。单次往返 avg `llm_elapsed_ms≈4.25s` —— token 越多 prefill 越慢，这是 `llm_elapsed_ms=404s`（占 35%）的主因。
  - 这两个根因的**统一承载骨架 = Prompt 模板统一管理**（现状 agent 用裸字符串拼接，无变量、无分层、无法裁剪）。本 Plan 把"降时方案"与"模板改造"合并实现，避免打补丁。

---

## 1. 数据基础（153540 实测，非估算）

| 指标 | 值 | 说明 |
|---|---|---|
| `duration_seconds` | 1147.1 | 总时长 |
| `llm_call_count` | 95 | LLM 调用次数 |
| `llm_elapsed_ms` | 404171.8 | LLM 墙钟合计 ≈ 404s（占 35%） |
| `click_count` | 33 | 点击（含 batch） |
| `step_count` | 159 | 总 step |
| `input_tokens` | 7,441,822 | ≈ 78,335 tokens/次调用 |
| `cached_input_tokens` | 6,293,195 | 66% 命中缓存 |
| `output_tokens` | 1,631,070 | ≈ 17K/次 |

**步级巨型 step（trace 实证，单步 >10s）：**
- seq 69 `click(targets=[1..20])` → **58.0s**
- seq 95 `click(targets=[1..20])` → **52.9s**
- seq 29 `vision_tap` → **52.0s**
- seq 101 `click(targets=[1..20])` → **50.9s**
- seq 27 `vision_tap` → **28.4s**
- seq 28 `vision_tap` → **26.9s**
- seq 1 / seq 83 `click` → **16s**
- seq 3 `click(课程表设置)` → **6.1s**（单点即 6s）

> 仅上述 8 步合计 ≈ **344s**，占 1147s 的 **30%**。其余为 LLM 推理（404s，35%）+ 大量 1–6s 的普通点击/感知。

**关键结论**：批处理（`targets`）已生效（153540 出现 `BATCH_OK` 多条），但**只省了 LLM 轮次与 step 预算，没省设备物理时间**——因为每点一个 chip 仍各自 `sleep(2.0)` + 各自重感知。这是方案 1 的靶心。

---

## 2. 时间归因（1147s 拆分）

| 类别 | 估算 | 占比 | 性质 |
|---|---|---|---|
| **D LLM 上下文膨胀** | 404s（95×4.25s） | 35% | 78K tokens/次 prefill；缓存只省钱不省延迟 |
| **B 设备交互 sleep 膨胀** | 20 周批量 ×2s + 普通点击 settle ≈ 实测 seq69/95 各 52–58s | ~30% | `click.py:762` 统一 sleep(2.0) 误用于即时 toggle |
| **C 视觉/断言多模态** | seq29/101 等 vision_tap 52s、28s | ~15% | 多模态往返天然慢 |
| **E 截图开销** | 每步 `_take_step_screenshot` + 每次 vision_verify/assert 各存盘截图；落盘 + 视觉推理均占 wall-clock | 未单列（含于 C 与普通点击） | 每次截图本身耗时；同页重复截图=纯浪费 |
| **A 启动/环境** | 153540 未复现 RPC 故障（preflight 已生效） | ~5% | 较 213229 已改善 |
| 普通点击+感知 | 余量 | ~15% | 固有成本 |

> 战术层可削减的三块：**B（sleep 膨胀，约 300s+）**、**D（上下文膨胀，404s 中可裁剪部分）**、**E（截图开销，含重复截图浪费）**。B+D 合计约 700s；E 通过去重减少无谓截图次数，既省 wall-clock 又省存储/前端噪声。

---

## 3. §0 契约约束（实施不可违反）

- **代码给事实 / LLM 判断**：sleep 策略、token 裁剪是代码层优化，不替 LLM 决定"选哪些周"；prompt 引导（全选优先）保留给 LLM。
- **不引入 scenario-specific patch**：通用规则放 prompt（已落地 `agent_explore.txt` 多选组段），不写死"周数"业务词。
- **不破坏既有批处理契约**：`targets` 与 `repeat` 互斥规则保留。
- **裁剪可观测、可回退**：token 裁剪需保留 `truncated` 标记（已有 `_clip_to_token_budget` 基础设施），不静默丢信息。
- **向后兼容**：planner 现有 `{user_request}`/`{rag_context}` 变量保留；agent 改为模板后行为须与现状一致（pytest 329 passed 回归）。

---

## 4. 承载骨架：Prompt 模板统一管理

> 方案 2/3/4 都依赖"动态段可分层裁剪"，而现状 agent 用裸拼接无法支撑。故先改造模板层，再挂裁减逻辑。

### 4.1 现状与问题
```python
# planner —— 已用模板 ✅
PLANNER_TEMPLATE = ChatPromptTemplate.from_messages([
    SystemMessage(content=PLANNER_SYSTEM),
    ("user", "Create a test goal for:\nRequest: {user_request}\nTarget app: {app_name} ({app_package})\n\n{rag_context}"),
])

# agent —— 裸拼接 ❌（无模板、无变量）
AGENT_SYSTEM = _load_prompt("agent_common.txt") + "\n" + _load_prompt("agent_explore.txt")
def _select_agent_system(state): return AGENT_SYSTEM
# 调用处：msgs = [SystemMessage(content=_select_agent_system(state))]
```
问题：① 无法注入变量（mode/app/step_budget/history 只能硬编码或重复）；② 无法分层裁剪（稳定规则与易变上下文混在一团，方案 2/4 无挂载点）；③ 缓存命中率低且不可控；④ 维护靠手动拼接（common/explore 两处写 `targets` 即重复信号）；⑤ token 不可观测。

### 4.2 目标设计
**全量统一 `ChatPromptTemplate` + 分层（稳定 system / 动态 user 段）+ 集中 registry + token 可观测。**

```python
# agent 由裸拼接改为模板
AGENT_TEMPLATE = ChatPromptTemplate.from_messages([
    ("system", _load_prompt("agent_common.txt") + "\n" + _load_prompt("agent_explore.txt")),
    ("user",
     "Mode: {mode}\n"
     "App: {app_name}\n"
     "Step budget left: {step_budget}\n"
     "Round: {round_no}\n\n"
     "{rag_context}\n\n"
     "Step history (recent):\n{history}\n\n"
     "Current screen:\n{screen}"),
])
```
**分层原则**：
- `system` 段 = 稳定行为规则（targets / repeat / 全选优先 / 断言边界）—— 不随 turn 变 → 持续命中缓存；
- `user` 段 = 易变上下文（`{rag_context}` / `{history}` / `{screen}` / `{step_budget}`）—— 随 turn 变 → **此处挂方案 2/4 裁剪，只裁 user、不动 system，缓存更稳**。

**集中 registry**（`agents/prompts/__init__.py` 新建）：
```python
TEMPLATES = {"planner": PLANNER_TEMPLATE, "agent": AGENT_TEMPLATE}  # 预留 reporter/preflight
def load_template(name) -> ChatPromptTemplate: ...
def build_messages(name, **vars) -> list[BaseMessage]:
    t = TEMPLATES[name]
    return t.format_messages(**_clip_vars(vars))   # 对 user 段变量做 token 预算裁剪
def template_token_report() -> dict: ...           # 启动时打印 system/user 各自 token 数
```
所有节点（planner_node / agent loop / reporter）改为 `build_messages("agent", mode=..., screen=...)`，删除散落的 `_load_prompt` + 拼接。

**token 可观测**：registry 加载时估算 `system_tokens` / `user_static_tokens`，启动 `logger.info` 打印；设阈值（如 system > 40K 告警），接方案 3 精简闭环。

---

## 5. 优化方案（按 ROI 排序，统一实现）

### 方案 1（优先级最高）：区分"switch 过渡态"与"即时 toggle"，缩短 checkbox-like 点击后的等待
**定位**：`tools/click.py:762`（`time.sleep(2.0)`，注释明言为 `com.android.settings` 的"关→开"开关留 2s 过渡）。

**根因**：`_is_checkbox_like` 把周数 chip、普通 CheckBox、Switch 全归为"checkbox-like"，点击后统一 `sleep(2.0)` 再 `_check_switch_state`。但**周数 chip 是即时 toggle，无过渡态**，2s 等待纯浪费。批量 20 周 = 40s 空等（实测 seq69=58s）。

**做法**：
- 按 `element.get("class")` 判定：class 含 `Switch` → 保留 `sleep(2.0)`；否则（周数 chip / `CheckBox` / 普通 `CompoundButton`）→ `sleep(0.3)`（仅给 UI 刷新）+ 直接 `_check_switch_state`；
- batch 同样受益：`targets` 内每点一个 chip 都走该路径，20 周从 40s 等待 → 6s。

**预期**：seq69/95 的 52–58s → ~8–12s；B 类整体回收约 200–300s（占 1147s 的 ~20–25%）。

**风险**：过低 sleep 可能漏掉极慢设备渲染；用 0.3s 而非 0，且 `_check_switch_state` 本身再读一次状态兜底。

---

### 方案 2（优先级高）：screen dump 默认 compact 模式（经模板 user 段下发）
**定位**：`tools/__init__.py:159` `get_screen_info`（默认 `full`：最多 40 路径 + 60 元素，全字段 label/role/class/bounds/path）。

**根因**：每个 agent turn 都带全字段 screen dump（数千 token）。绝大多数轮次 LLM 只看 `label`+`index`。path/bounds 全量回传价值低、token 成本高，且每次重发（即便缓存仍计入 78K/次 prefill）。

**做法**（结合 §4 模板改造）：
- `get_screen_info` 新增 `mode: "compact" | "full"`，**默认 `compact`**：只回 `index` + `label` + `role` + `selected`（如 `[3] 周一(周) [SELECTED]`），不回 `bounds`/`path`/`class` 全量；
- `full` 仅在 LLM 显式需要坐标/层级（如 `vision_tap` 前定位）时传；
- compact 单行 token 约为 full 的 1/3 → 单轮 screen dump 从 ~3–5K 降到 ~1–1.5K；
- 在 `build_messages` 内，`{screen}` 变量默认传 compact 结果，LLM 需坐标时显式请求 `full`——天然落在 user 段裁剪。

**预期**：单次请求 78K → 约 70K（叠加方案 3 显著）；更关键的是**降低 prefill 抖动**，`llm_elapsed_ms` 更稳定。

**风险**：LLM 偶需 bounds 时 compact 不足；靠显式 `full` 参数兜底，prompt 说明"需坐标时传 `mode='full'"`。

---

### 方案 3（优先级高）：system 段集中精简 + 工具 schema 去重
**定位**：`agents/prompts/agent_common.txt` / `agent_explore.txt` / `agent_planner.txt` + 各 StructuredTool 的 `description`/`args_schema`。

**根因**：78K/次中静态部分（system + 全部工具 JSON schema + planner 模板）是大头且每轮必发。近期 common/explore 两处写 `targets` 已重复；工具 schema（如 `click` 的 `repeat`/`targets`/`permission_hint` 长描述）偏大。

**做法**（结合 §4 registry）：
- system 段集中后，合并 common/explore 重复的 `targets`/`全选` 表述，保留单处权威定义；
- 工具 `description` 砍到"最小可用"（保留参数语义与互斥约束，删示例冗余）；
- 保持 system 段文本**稳定**（不随 turn 变），持续命中缓存；
- registry 的 `template_token_report` 量化验证"删了多少"。

**预期**：静态部分从 ~50K 降到 ~35K，单次请求 ~78K → ~63K；prefill 线性下降，`llm_elapsed_ms` 整体降约 15–20%。

**风险**：过度精简可能削弱 LLM 判断；以"删重复、不删约束"为原则，删除后跑回归确认行为不变。

---

### 方案 4（优先级中）：history / post-check 文本统一走 token 预算裁剪（经模板 user 段）
**定位**：`agents/budget.py:_clip_to_token_budget`（现有，仅用于 RAG 500 token + 个别 post-check）。

**根因**：`step_history` 每步 `observation[:100]` 字符截断，但**未按 token 预算统一裁剪**；长验证证据 / assertion 返回可能随 history 累积放大上下文。

**做法**（结合 §4 registry）：
- 在 `build_messages` 的 `_clip_vars` 中，对 `{history}` / `{rag_context}` / `{screen}` 统一套 `_clip_to_token_budget(上限)`；
- 设单步 observation 上限（如 200 token）、history 总上限（如 1500 token，已落实 `context_history_steps=4`），超限标 `[truncated]`；
- system 段不动，缓存更稳。

**预期**：抑制上下文随 step 线性膨胀（explore 后期最明显），晚轮 `llm_elapsed_ms` 不再爬升。

**风险**：极端长证据被截可能影响验证；保留关键 `deciding_evidence` 不裁。

---

### 方案 5（优先级中）：优先识别"全选/全不选"控件，替代逐个 targets
**定位**：`agent_explore.txt` 多选组段（已落地"用 targets 批量"），补充"先找全选按钮"。

**根因**：20 周即便用 `targets=[1..20]` 仍是 20 次物理点击。多数周数页有独立"全选"按钮，一次点击 = 20 周，省掉 19 次 toggle 判定。

**做法**：
- prompt 增补："遇到周数/多选组，**先扫页面是否有『全选 / 全不选 / 反选』按钮**，有则一次点击替代 `targets` 枚举；无再退回 `click(targets=[...])`"；
- 配合方案 1 短 sleep，单次"全选"点击 ≈ 0.3s 而非 20×2s。

**预期**：seq69/95 的 20 周批量从 ~52s（即便方案1后仍 ~8s）→ ~1s（一次全选按钮）。

**风险**：部分 App 无全选按钮；退回 targets 逻辑保留。

---

### 方案 6（优先级中）：截图去重 + 按需落盘（E 类开销）
**定位**：`tools/verify.py:_save_evidence_screenshot` / `tools/perceive_tools.py:_save_perceive_evidence_screenshot` / `agents/llm_runtime.py:_take_step_screenshot`。

**根因（来自真实 run `test-20260821_170830` 实测）**：
- 该 run 落盘 **32 张截图 / 11.7MB**，但前端只展示其中一部分；大量截图**内容几乎相同**（同页连续操作：点击前拍一张、点击后拍一张、验证再拍一张）。
- 三处无去重的截图源叠加：`_take_step_screenshot`（每次 click/scroll/swipe/launch/assert 都拍，**动作前拍**，连续同页操作→两张几乎一样）、`vision_tap`（**每次**调用都拍）、`vision_verify`/`assert`（每次验证都拍）。
- **每次截图本身耗时**：`device.screenshot().save()` + 后续视觉推理都是 wall-clock。同页重复截图 = 既费时又费存储、且前端噪声大。

**用户决策（已确认）**：点击前/后截图保留（合理）；但**同一验证条件/同一页面只用一张截图**即可，无需每次验证调用都落新图。

**做法**：
- **验证截图按 `verification_key` 去重**：`evidence_{key}.png`（verify）/ `evidence_{key}_vision.png`（perceive）命名去掉 seq；在 `ctx._evidence_shots` 缓存同 key 路径，重复调用复用已存路径、不再落盘。效果：170830 的 `evidence_v1_1..7` 折叠为 `evidence_v1.png` 一张。
- **`vision_tap` 截图按需**：仅在 `decision != already_passes` 或首次 vision_tap 时落盘，后续同页 vision_tap 复用。
- **步骤截图时机**：保持"点击前拍"（用户认可），不强行改动作后；去重焦点放在验证截图（重复率最高）。
- 点击前/后截图保留，不做跨步去重（避免丢失过程回放信息）。

**预期**：
- 截图落盘次数显著下降（验证密集场景预计砍 40–60% 重复图），直接减少每次截图占用的 wall-clock（E 类）；
- 存储/前端噪声同步下降；过程回放（点击前/后）不受影响。

**风险**：去重若误合并"不同页面同 key"会丢证据；以 `verification_key` 为键（同一验证条件本就对应同一目标状态），风险低；必要时可在 key 内缀入页面签名进一步保险。

---

## 6. 预期收益汇总

| 方案 | 目标损耗 | 预计回收 | 实施风险 |
|---|---|---|---|
| 0 模板统一管理（骨架） | 结构性 | 使 2/3/4 可落地 | 低（行为等价） |
| 1 checkbox-like 短 sleep | B 类 ~300s | 200–300s | 低 |
| 2 screen dump compact | D 类部分 | 间接（prefill 稳定） | 低 |
| 3 prompt/工具 schema 精简 | D 类静态 ~15K/次 | llm_elapsed 降 15–20% | 低（需回归） |
| 4 history token 裁剪 | D 类膨胀 | 晚轮不再爬升 | 低 |
| 5 全选按钮优先 | B 类批量 | 再省 ~7s/批 | 低 |
| 6 截图去重 + 按需落盘 | E 类开销 | 砍 40–60% 重复截图（省 wall-clock + 存储） | 低 |

**叠加方案 1+5**：20 周批量 52s → ~1s，单此一项每次用例省 ~100s；
**叠加方案 3**：`llm_elapsed_ms` 404s 降约 60–80s；
**合计战术层可回收 ~350–450s（1147s → ~700–800s，降 30–40%）**，且不触及战略 explore 根因（那部分由 fixture/plan 沉淀解决，见上 Plan §10）。

---

## 7. 统一落地步骤（按依赖排序）

| 步骤 | 内容 | 风险 | 依赖 |
|---|---|---|---|
| 1 | 新建 `agents/prompts/__init__.py` registry + `load_template`/`build_messages`/`template_token_report` | 低 | — |
| 2 | agent 由 `AGENT_SYSTEM` 裸拼接改为 `AGENT_TEMPLATE`（变量：mode/app/step_budget/rag/history/screen） | 低（行为等价） | 步骤1 |
| 3 | 节点调用统一改 `build_messages(...)`，删 `_select_agent_system` 拼接 | 低 | 步骤2 |
| 4 | `click.py:762` 按 class 区分 Switch(2.0s)/toggle(0.3s) | 低 | — |
| 5 | `get_screen_info` 加 `mode=compact/full` 默认 compact；`build_messages` 内 `{screen}` 默认 compact | 低 | 步骤1 |
| 6 | `build_messages._clip_vars` 对 user 段（history/rag/screen）套 `_clip_to_token_budget`；common/explore 去重；工具 schema 精简 | 低（需回归） | 步骤1 |
| 7 | `agent_explore.txt` 多选组段补"先找全选按钮" | 低 | — |
| 8 | registry 启动打印 token 报告 + 超阈值告警 | 低 | 步骤1 |
| 9 | `verify.py`/`perceive_tools.py` 验证截图按 `verification_key` 去重（`ctx._evidence_shots` 复用路径）；`vision_tap` 截图按需 | 低 | — |
| 10 | `pytest` 回归 329 passed + 重跑 153540 / 170830 同类用例对比 tokens/elapsed/截图数 | 低 | 全部 |

---

## 8. 验证方式

- 启动日志出现 `template_token_report`：agent system ≈ X tokens、user 静态 ≈ Y tokens；
- 重启 agent 服务（prompt 改动需重启）后重跑同一用例；
- 对比新 trace 的 `duration_seconds` / `llm_elapsed_ms` / `input_tokens` / 巨型 step（`click(targets=...)` 单步 elapsed）；
- `pytest` 329 passed 不退化；
- 检查 `step_history` 是否出现 `[truncated by token budget]` 标记，确认裁剪生效；
- 巨型 step 单步 `elapsed_ms` 不因模板改造而变化（行为等价）。

---

## 9. 优先级错配提示（与上 Plan 对齐）

- **不要**再把火力对准"20 周逐点"（已靠 `targets` + 本 Plan 方案 1/5 解决）；
- **真正剩余大头** = LLM 上下文膨胀（方案 0/2/3/4）+ checkbox-like 统一 2s 等待（方案 1）；
- 战略层（explore→guided、fixture 前置）仍是最长杠杆，但属另一 Plan，本 Plan 只收战术层。

# LLM-Native 架构迁移计划：从补丁驱动到决策驱动（2026-07-09）

## 1) 核心原则

> LLM 擅长的事交给 LLM，代码只做 LLM 做不了的事——设备通信、pixel 级执行、安全检查、关键兜底。

当前 5135 行代码中，~800 行是在做 LLM 天生就能做的事。目标是逐步删除这些代码，把决策权还给 LLM。

## 2) 现状全景

```
┌─────────────────────────────────────────────────────────────┐
│                      tools/__init__.py (2770 行)             │
│  LLM 能做的：                                               │
│  ├── 语义理解 (150 行)                                      │
│  │   ├── _expand_zh_keywords     "开关"→"switch" 翻译       │
│  │   ├── _extract_click_preferences_from_rag  正则解 RAG    │
│  │   ├── _is_volatile_label      时间/网速/充电识别          │
│  ├── 匹配/排序 (300 行)                                     │
│  │   ├── _score_element           6 字段加权                │
│  │   ├── _rank_click_candidates   候选排序+去重              │
│  │   ├── _pref_bonus_for_element  RAG 加分                  │
│  │   ├── _find_best_element_with_known  历史身份匹配         │
│  │   ├── _disambiguate_container  容器 vs 子项               │
│  │   └── _is_container_like       class 判断                │
│  └── 兜底/回退 (150 行)                                     │
│      ├── fallback 回退循环        点错→试探→再点错           │
│      ├── text-fallback            文本点击兜底               │
│      ├── known-rid-fallback       历史 rid 兜底              │
│      ├── pct-bounds-fallback      百分比坐标兜底             │
│      └── rid-fallback             label 当 rid 点           │
├─────────────────────────────────────────────────────────────┤
│                      agents/graph.py (1872 行)               │
│  LLM 能做的：                                               │
│  ├── 规则/策略 (200 行)                                     │
│  │   ├── _should_skip_hotword_element  热词过滤              │
│  │   ├── _ROLE_PRIORITY              11 种角色优先级          │
│  │   ├── _ZH_CONTROL_TOKENS          中文控件词映射          │
│  │   ├── _NO_PROGRESS_ACTIONS        15 种无效动作分类        │
│  │   └── _cooldown_group             语义动作分组            │
│  └── 语义理解 (50 行)                                       │
│      └── _output_has_page_change (文本解析)                  │
├─────────────────────────────────────────────────────────────┤
│                    agents/prompts/agent.txt (65 行)           │
│  └── 元素选择优先级 6 条硬规则                               │
└─────────────────────────────────────────────────────────────┘

总计可下线:  ~590 行
总计可缩减:  ~210 行
────────────────────
净减:        ~800 行
```

## 3) 分阶段下线计划

### Phase 1：无害清理（本周，~100 行）

这些代码已经不再被主路径使用，直接删：

| 删除项 | 位置 | 原因 |
|--------|------|------|
| `_should_skip_hotword_element` | graph L299-306 | 精确模式下 LLM 不点热词；page_info 构建已不调用 |
| `_expand_zh_keywords` | tools L230-238 | LLM 不需要中文→英文翻译 |
| `_ZH_CONTROL_TOKENS` | tools L197-207 | 同上 |
| `_is_volatile_label` | tools L1311-1316 | page_info hash 过滤逻辑并入 `_capture_page_id`，独立函数删除 |
| `agent.txt` 元素选择优先级 6 行 | prompt L29-38 | 缩为 2 行："用精确参数（index/rid/class/path），不要猜" |

### Phase 2：精确模式接管后（观察 1-2 周，~300 行）

确认精确模式（`index`/`rid`/`class`/`path`）覆盖主要场景后：

| 删除项 | 位置 | 前提 |
|--------|------|------|
| `_score_element` | tools L331-392 | 精确模式不调用 |
| `_rank_click_candidates` | tools L549-588 | 同上 |
| `_pref_bonus_for_element` | tools L289-328 | 同上 |
| `_prefs_active_for_description` | tools L279-287 | 同上 |
| `_extract_click_preferences_from_rag` | tools L248-305 | 同上 |
| `_find_best_element_with_known` | tools L591-680 | 同上 |
| `_disambiguate_container` | tools L701-768 | 同上 |
| `_is_container_like` | tools L1086-1092 | 同上 |
| `_ROLE_PRIORITY` | tools L210-219 | 同上 |
| `_CLICK_PREF_DEFAULT_WEIGHTS` | tools L222-227 | 同上 |
| 4 个 fallback 路径 | tools L1449-1520 | 精确模式失败返回错误码，不让代码猜 |
| fallback 回退循环 | tools L1376-1402 | 同上 |
| `_capture_page_id` 文本解析 | tools L1321 | `_build_page_signature` hash 已足够 |
| `_output_has_page_change` 文本解析段 | graph L285-293 | 同上 |
| `_apply_click_preferences` | graph L844-862 | RAG 偏好不再需要翻译到代码 |

### Phase 3：策略收敛（持续，~100 行）

| 操作 | 涉及 | 说明 |
|------|------|------|
| `_cooldown_group` → 简单计数器 | graph L264-282 | "语义分组"改为"同类动作超过 N 次就警告"，LLM 自己判断语义 |
| `_NO_PROGRESS_ACTIONS` 集合移除 | graph L165-181 | Fix 2 改页面变化驱动，不再需要分类 |
| NO_PROGRESS 改为进展事件 | graph L402-414 | 已计划（calculator_no_progress_fix） |
| `agent.txt` 精确参数 + 自验证规则强化 | prompt | 防患层 |

## 4) 精确模式与 Legacy 的分界线

当前 click 函数已经双轨，分界线清晰：

```
click(label, rid, class_name, path_contains, index)
│
├── 精确参数存在 → exact_mode = True
│   ├── _exact_clickable_candidates → 精确匹配
│   ├── 唯一 → 执行 ✅
│   ├── 多个 → 返回 AMBIGUOUS 让 LLM 缩小范围 ✅
│   └── 无匹配 → 返回错误让 LLM 重试 ✅
│       绝不进入 legacy 管线 ❌
│
└── 精确参数不存在 → legacy mode
    │
    ├── 前置歧义闸门（新增，通用防错）:
    │   - rid 唯一但语义不匹配 → AMBIGUOUS（不执行，透传给 LLM）
    │   - 回退循环 → 删除（不替 LLM 猜次优候选）
    │
    └── 最小安全匹配（待最终下线）:
        _score_element → _rank_click_candidates → _disambiguate_container
```

**核心变化**：legacy 模式下也加前置闸门。代码不再"成功点错"（rid 放行但 label 不对），也不"失败后乱试"（回退到无关候选）。歧义一律透传给 LLM。

## 5) 保护措施

删除 legacy 代码时保留两样东西：

1. **`check_dangerous`**（安全检查）—— LLM 不能做，代码必须做
2. **pixel 级执行**（`click_bounds`）—— 精确模式匹配到元素后最终走 bounds 点击，确定性执行

## 6) 下线决策条件

每阶段代码删除前满足：

- Phase 1：全量 pytest 通过
- Phase 2：精确模式连续 50 次 run 零 WARNING（模糊匹配触发 legacy），零 fallback 触发
- Phase 3：进展事件驱动的 NO_PROGRESS 稳定运行 1 周

## 7) 验收标准

- `tools/__init__.py` 从 2770 行降到 ≤ 2200 行
- `agents/graph.py` 从 1872 行降到 ≤ 1700 行
- 精确模式占比 ≥ 95%（总 click 次数中精确参数的占比）
- 模糊匹配 WARNING 率 ≤ 2%
- 全量 pytest + 回放集通过

---

## 8) 当前实现偏差（2026-07-09 实测补充）

以下两项与“LLM-Native 决策优先”仍有偏差，需纳入下一轮收敛：

1. **legacy 猜测兜底未完全下线**
   - 现状：“次优候选回退循环”已在迁移计划中列为删除项（落地状态以当前代码分支为准），`click` 仍保留
     `text-fallback` / `known-rid-fallback` / `pct-bounds-fallback` / `rid-fallback`。
   - 问题：这些路径仍在工具层替 LLM 决策，可能产生“非精确参数下的隐式误点”。
   - 调整：当非精确模式无法稳定命中时，统一返回结构化错误
     （`AMBIGUOUS` / `NOT_FOUND`），由 LLM 基于 page_info 重新下发
     `index/rid/class/path` 精确参数。

2. **Fix 4 埋点未形成 click 侧闭环**
   - 现状：`fuzzy_click_count` / `fuzzy_click_rate` / `no_progress_abort_rate`
     在报告与数据库侧尚未全部打通。
   - 问题：无法量化“legacy 依赖度”和“模糊匹配回归风险”。
   - 调整：将上述指标纳入 run 结果、报告列表与详情，并持久化到 test_runs。

### 8.1 补充验收门槛

- 非精确参数场景下，点击工具不再执行任何“自动猜次优”行为；
- `AMBIGUOUS` 返回后，后续一次 LLM 重试应出现精确参数（index/rid/class/path）；
- 报告可见并可查询：
  - `fuzzy_click_count`
  - `fuzzy_click_rate`
  - `no_progress_abort_rate`

---

## 9) 高级工程师视角补充（系统级）

在“LLM 擅长的事交给 LLM”原则下，当前还建议补齐以下系统能力：

### 9.0 优先级分层（P0/P1/P2）

| 优先级 | 条目 | 理由 |
|---|---|---|
| P0 | 9.5 可证伪假设、9.7 DoD | 决定“能不能删代码”的标尺，Phase 1 即需要 |
| P1 | 9.1 Tool Contract、9.4 Kill Switch | Phase 2 精确模式接管前必须具备的工程护栏 |
| P2 | 9.2 单一规则源、9.3 Trace、9.6 回放集 | 长期工程能力，建议并行推进但不阻塞迁移 |

### 9.1 Tool Contract 先于策略迁移

工具契约标准化应尽早推进，否则 LLM 决策会因返回格式漂移而不稳定。
但 **Phase 1 无害清理不受契约标准化阻塞**：已不调用的死代码可直接删除；
契约标准化应在 Phase 2（精确模式接管前）完成。

建议统一 click/verify 等核心工具返回结构（至少包含）：
- `code`: `OK | AMBIGUOUS | NOT_FOUND | SAFETY_BLOCKED | DEVICE_OFFLINE`
- `message`: 人类可读描述
- `hints`: 下一步建议参数（如 `index/rid/class/path`）
- `evidence`: 候选摘要（最多 N 条，避免超长）

> 先“结构化输出稳定”，再“下线启发式”。

### 9.2 Prompt 规则与运行时规则单一来源

当前 prompt 与 graph/tool 内规则并行维护，存在双写漂移风险。

建议：
- 将关键硬规则（如精确参数优先、歧义透传）沉淀为 machine-readable policy（YAML/JSON）；
- prompt 由 policy 生成摘要；
- graph/tool 在同一 policy 上执行校验。

### 9.3 观测升级：从计数到因果链

仅有率值不够定位根因。建议每次关键失败保留 trace 片段：
- `decision_trace_id`（一次工具决策链唯一 ID）
- `tool_input`（摘要）
- `tool_output_code`
- `page_signature_before/after`
- `llm_turn_index`

目标：能回答“这次错点是模型决策错、工具契约错，还是页面感知错”。

### 9.4 迁移防护：双跑对比 + Kill Switch

建议引入运行期开关：
- `CLICK_NATIVE_STRICT=true/false`（严格透传）
- `LEGACY_FALLBACK_ENABLED=true/false`（遗留兜底总开关）

并进行 shadow run：
- 同输入在新旧路径并行决策，不实际执行旧路径，仅对比输出差异；
- 达到阈值后再彻底关闭 legacy。

### 9.5 数据闭环：把“可删代码”变成“可证伪假设”

每个待删除模块都绑定一个可量化假设，例如：
- 删除 `_score_element` 后，`AMBIGUOUS` 率上升不超过 X%，最终通过率不下降；
- 删除 fallback 后，平均重试轮数不超过 Y，误点率下降 Z%。

无指标不删除，避免“删完才发现隐含依赖”。

### 9.6 回归体系补齐（不仅 pytest）

建议新增三类回放集：
1. **歧义集**：同名元素/容器子项冲突；
2. **高频输入集**：连续输入、多步验证；
3. **恢复集**：resume/中断后继续执行。

每次迁移必须跑这三类回放，并输出差异报告。

### 9.7 最终目标定义（Definition of Done）

建议补一条架构级 DoD：
- Legacy 路径只保留设备执行与安全相关代码；
- 所有”语义判断/候选排序/回退策略”不再在工具层实现；
- 线上 2 周满足：
  - 通过率不降
  - 误点率下降
  - `AMBIGUOUS` 重试成功率达标
  - 无高危安全回退。

---

## 10) 实施细节

### 10.1 Kill Switch（运行期开关）

**开关设计 & 冲突规则**

| 配置项 | 取值 | 说明 |
|--------|------|------|
| `click_mode` | `native_strict \| legacy` | 总开关，控制 click 入口分流 |

仅使用单开关 `click_mode`，不再引入 `LEGACY_FALLBACK_ENABLED`（双开关增加状态组合复杂度且无实际收益）。§9.4 的双开关描述为迁移期建议，最终落地收敛为单开关。优先级规则：`click_mode=native_strict` 时忽略所有 legacy 路径。

**配置**：`config.yaml`

```yaml
click_mode: native_strict  # native_strict | legacy
```

**注入**：`tools/context.py` → `ToolContext`

```python
class ToolContext:
    click_mode: str = “legacy”  # 由 server.py 从 config 注入
```

**分流**：`tools/__init__.py` → `click()`

```python
def click(label, ..., rid=””, class_name=””, path_contains=””, index=-1):
    ctx = get_tool_context()
    if getattr(ctx, “click_mode”, “legacy”) == “native_strict”:
        return _native_strict(label, rid, class_name, path_contains, index,
                             alternatives, ctx)
    return _legacy_click(label, rid, class_name, path_contains, index,
                         alternatives, ctx)
```

**`_native_strict` 逻辑**：

```python
def _native_strict(label, rid, class_name, path_contains, index, alternatives, ctx):
    exact_mode = bool(rid or class_name or path_contains or index >= 0)

    if exact_mode:
        candidates, err = _exact_clickable_candidates(
            understanding, rid=rid, class_name=class_name,
            path_contains=path_contains, index=index)
        if err:
            return err  # AMBIGUOUS / NOT_FOUND -> LLM 重决策
        return _execute(candidates[0])

    # 无精确参数 -> 直接返回 AMBIGUOUS，不猜
    return “AMBIGUOUS: 未提供精确参数（index/rid/class_name/path_contains），请在 page_info 中选择后重试”
```

**回滚**（Windows PowerShell / Linux 通用）：

```powershell
# Windows PowerShell
(Get-Content config.yaml) -replace 'native_strict', 'legacy' | Set-Content config.yaml
```

```bash
# Linux / WSL
sed -i 's/native_strict/legacy/' config.yaml
```

重启服务后秒级生效。

### 10.2 指标门禁

**click 工具侧**：新增结构化输出字段，避免依赖日志文案字符串匹配。

`_format_click_log` 返回结果中追加：

```python
# 输出格式追加 match_mode 和 fallback_used
# 例: “已点击: 应用列表 | strategy=bounds | match_mode=exact | fallback_used=false | ...”
```

**数据采集**：`agents/graph.py` → `reporter_node`

```python
import re

# 从 tool_calls_log 的结构化字段 + observation 提取
click_count = sum(1 for s in dd if s.get(“action_type”) == “click”)

# 优先读结构化字段，缺失时回退 observation 匹配（向后兼容）
def _is_fuzzy(s):
    return bool(s.get(“fallback_used”)) or (
        not s.get(“match_mode”) and “WARNING” in str(s.get(“observation”, “”))
    )

def _is_ambiguous(s):
    return bool(s.get(“match_mode”) == “ambiguous”) or (
        not s.get(“match_mode”) and “AMBIGUOUS” in str(s.get(“observation”, “”))
    )

fuzzy_count = sum(1 for s in dd if _is_fuzzy(s))
ambiguous_count = sum(1 for s in dd if _is_ambiguous(s))

# 检测 AMBIGUOUS 后同一 agent 迭代内下一次 click 是否带精确参数
# 允许中间插入 get_screen_info / query_app_knowledge 等非 click 动作
ambiguous_retry_ok = 0
pending_ambiguous = False
for s in dd:
    act = s.get(“action_type”, “”)
    if act == “click”:
        if pending_ambiguous:
            obs = str(s.get(“observation”, “”) or “”)
            if any(k in obs for k in (“rid=”, “class=”, “path_contains=”, “index=”)):
                ambiguous_retry_ok += 1
            pending_ambiguous = False
        if _is_ambiguous(s):
            pending_ambiguous = True
    elif act in (“get_screen_info”, “query_app_knowledge”, “check_page_health”):
        continue  # 中间插入感知/查询动作，不打断跟踪
    else:
        pending_ambiguous = False  # 其他动作打断跟踪

_relational_db.record_test_run(
    ...,
    fuzzy_click_count=fuzzy_count,
    click_count=click_count,
    ambiguous_count=ambiguous_count,
    ambiguous_retry_ok=ambiguous_retry_ok,
    no_progress_abort=(“NO_PROGRESS” in str(conclusion)),
)
```

**门禁脚本**：`scripts/gate_check.py`

```python
# 跑 50 次用例，汇总指标
# fuzzy_click_rate <= 2%
# ambiguous_rate <= 10%
# retry_success_rate >= 90%
# pass_rate >= 基线
# avg_steps <= 基线 * 1.1
# 全部通过 -> 允许进入下一 Phase
```

### 10.3 前置歧义闸门（过渡态，Phase 2 替换）

**`tools/__init__.py` → `_perform_click_on_element`**

```python
# 改前：rid 唯一 -> 直接点
if rid_is_unique and ctx.device.click_resource_id(rid):
    return True, _format_click_log(desc, el, strategy=”resource_id”)

# 改后：rid 唯一 -> 语义验证 -> 放行或透传（Phase 1 过渡态，依赖 _score_element）
if rid_is_unique:
    if _score_element(el, [desc], prefs=None, description=””) >= 3:
        if ctx.device.click_resource_id(rid):
            return True, _format_click_log(desc, el, strategy=”resource_id”)
    # 语义不匹配 -> 不执行，透传歧义
    return (False,
        f”AMBIGUOUS: rid={rid} label={getattr(el, 'label', '')} “
        f”与目标 '{desc}' 不匹配，请用 index/class_name 精确定位”)
```

> **过渡态说明**：Phase 1 依赖 `_score_element >= 3` 做歧义判断。Phase 2 `_score_element` 下线后，改为 contract-only 判断：精确参数存在则执行，不存在则直接 AMBIGUOUS。

同时删除 `click()` 中的 fallback 回退循环（tools L1376-1402），改为直接透传失败。

### 10.4 Tool Contract 标准化

**统一返回结构**（字段名统一为 `class_name`）：

```python
# 成功
{“code”: “OK”, “message”: “已点击: 应用列表”,
 “hints”: {“index”: 2, “class_name”: “TextView”}}

# 歧义
{“code”: “AMBIGUOUS”, “message”: “3 个候选匹配”,
 “evidence”: [
    {“index”: 0, “label”: “应用列表”, “class_name”: “FrameLayout”, “rid”: “taskbar_view”},
    {“index”: 2, “label”: “应用列表”, “class_name”: “TextView”,
     “path”: “taskbar_container > taskbar_view”}
]}

# 未找到
{“code”: “NOT_FOUND”, “message”: “未找到匹配元素”}

# 安全拦截
{“code”: “SAFETY_BLOCKED”, “message”: “操作被安全策略拦截”}
```

**实现阶段**：Phase 1 不做格式变更，Phase 2 精确模式接管前完成切换。

### 10.5 Phase 1 清理检查清单

```bash
# 删除前确认（Windows PowerShell 替代 grep）
rg “_should_skip_hotword_element” agents/ tools/   # 确认无调用
rg “_expand_zh_keywords” agents/ tools/             # 确认无调用
rg “_ZH_CONTROL_TOKENS” agents/ tools/              # 确认无调用
rg “_is_volatile_label” agents/ tools/              # 确认只有 _capture_page_id 调用

# 删除后验证
pytest -q                                           # 全量通过
python scripts/gate_check.py --phase 1              # 门禁通过
git diff --stat                                     # 确认净删行数
```

> 注：Windows 下 `rg` = [ripgrep](https://github.com/BurntSushi/ripgrep)，`grep` 也可用 Git Bash 自带的版本。

### 10.6 测试补充

| 测试文件 | 新增用例 | 覆盖 |
|---------|---------|------|
| `tests/test_tools_click_preference.py` | `test_ambiguous_on_rid_mismatch` | 闸门：rid 唯一但 label 不匹配 -> AMBIGUOUS |
| 同上 | `test_native_strict_no_legacy_fallback` | 精确模式失败不进入 legacy |
| `tests/test_graph_budget_and_reporting.py` | `test_kill_switch_routing` | click_mode=legacy vs native_strict 分流正确 |
| 同上 | `test_metric_collection` | fuzzy/ambiguous 计数准确 |
| 同上 | `test_ambiguous_retry_spans_gap` | get_screen_info 夹在中间不打断 AMBIGUOUS 重试跟踪 |

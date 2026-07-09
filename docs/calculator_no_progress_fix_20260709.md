# 通用性问题：rid 误匹配 & NO_PROGRESS 误触发（2026-07-09）

## 1) 问题

计算器测试中暴露了两个通用机制缺陷：
- `rid_is_unique` 将"唯一性"等同于"正确性"，`click("AC")` 被 `rid=op_fact`（label=`x!`）抢占
- `NO_PROGRESS` 只认 `assert_verification` 为进展信号，长时间连续有效操作被误判为停滞

## 2) 根因（非 App 特例）

1. **rid 快速路径把"唯一"当"正确"**：`_perform_click_on_element` 中 `rid_is_unique` 仅检查出现次数，不验证 label 语义。这在任何 App 都可能触发——只要存在一个非目标元素拥有唯一 rid。
2. **NO_PROGRESS 的信号源单一**：当前只有 `assert_verification` 能重置计数器。但页面结构变化（通过 `_capture_page_id` 已经能检测到）同样是有效的进展信号。
3. **Agent 缺少"连续同类操作需验证"的通用意识**：不是计算器特有问题，任何需要连续输入/连续选择的场景（表单填写、多选操作、滚动列表逐条处理）都会出现。

## 3) 修改点

### Fix 1：点击前置歧义闸门（通用防错 + 禁止自动回退）

**文件**：`tools/__init__.py` → `_perform_click_on_element` + 回退循环

**原则**：不在工具内"猜"次优候选，歧义/冲突时透传给 LLM 重决策。

当前问题分两层：
1. **成功但错点**（`AC`→`x!`）：rid 唯一 → 代码认为没问题 → 执行点错 → Agent 不知道
2. **失败后自动回退**（容器→图库）：点完没反应 → 代码自己试下一个 → 点错

两层都要管。

```python
# ── 闸门 1：语义阈值（防"成功但错点"） ──
# 改前（rid 唯一 → 跳过所有匹配直接用）
if rid_is_unique and ctx.device.click_resource_id(rid):
    return True, _format_click_log(desc, el, strategy="resource_id")

# 改后（rid 唯一 → 语义验证通过后才放行，否则透传给 LLM）
if rid_is_unique:
    if _score_element(el, [desc], prefs=None, description="") >= 3:
        if ctx.device.click_resource_id(rid):
            return True, _format_click_log(desc, el, strategy="resource_id")
    # 语义不匹配 → 不执行，返回歧义信息让 LLM 重决策
    return False, f"AMBIGUOUS: rid={rid} label={el.label} 与目标 '{desc}' 不匹配，请用 index/class 精确定位"

# ── 闸门 2：禁止自动回退（防"失败后乱点"） ──
# 改前：fallback 循环尝试 ranked_candidates[1:4]，点到页面变了就算成功
# 改后：删除整个 fallback 循环。点击失败就返回错误，让 LLM 决定下一步
```

说明：
- 闸门 1 用已有 `_score_element`（阈值 >=3 = 至少 label 或 rid 匹配），不引入新概念
- 闸门 2 删除回退循环——legacy 模式下也透传，不替 LLM 猜
- `AC` vs `x!`：label 零重叠 → 0 分 → 返回 `AMBIGUOUS` 而非点错
- **不绑定场景**：任何 rid 唯一但语义不符的元素都会被拦截

**不绑定场景**：适用于所有 App，不管什么页面什么 rid。

### Fix 2：NO_PROGRESS 改为“进展事件驱动”

**文件**：`agents/graph.py` → `_tools_node`

**原则**："无进展"判断不再绑定某个工具名，而是绑定是否出现 `progress_event`。  
当前阶段仅保留两类进展事件（避免冗余）：
1. `assert_verification` 结果提交；
2. 页面签名变化（`_output_has_page_change(...)`）。

```python
# 改前：只有 assert_verification 能重置
if name == "assert_verification":
    no_progress_count = 0

# 改后：任一 progress_event 出现即可重置
if (
    name == "assert_verification"
    or _output_has_page_change(output, page_sig_once, page_sig_after)
):
    no_progress_count = 0
```

注意 `page_sig_after` 已在每轮循环中通过 `_build_page_signature(ctx)` 更新（之前语义冷却的实现已经加了这个变量）。

后续若引入“非 assert 的 verification 自动推进”，再扩展第三类事件，不在当前版本提前引入。

**不绑定场景**：计算器按键每下都改变显示值（关键值也会变 → `_build_page_signature` 的 label 列表变化 → hash 变化），自动重置；图库翻页、设置切换也同理。

### Fix 3：Prompt 与运行时互补（防患 + 兜底）

**文件**：`agents/prompts/agent.txt`

**原则**：Prompt 负责前置引导，运行时机制负责 correctness 兜底。  
不是"输入 5 个数字后验证"，而是"连续多个同类操作用一个工具调用验证"。不绑定输入、计算器或任何 App。

```
- 连续执行同类操作（如连续输入、连续选择）超过 5 步后，暂停并调 get_screen_info 确认当前状态正确再继续。
- 若确认后发现操作结果不符合预期，立即修正而非继续投入更多步骤。
```

### Fix 4（P1）：观测埋点（不加场景模式开关）

**文件**：`agents/graph.py` / 报表汇总

新增指标：
- `fuzzy_click_count`（出现 `WARNING: 模糊匹配` 次数）
- `fuzzy_click_rate`（`fuzzy_click_count / click_count`）
- `no_progress_abort_rate`

说明：
- 不引入“强制精确定位模式”这种新模式开关；
- 先以指标驱动治理，再决定是否需要更强控制策略。

## 4) 验收标准

- `click("AC")` 不再误点到 `x!`（rid 不同 label 不匹配时跳过）
- `(1.5+(-0.5))×80%` 不再触发 NO_PROGRESS ABORT（进展事件可重置计数器）
- 代码中无 App/package 特判逻辑（不出现 `if "calculator" in package`）
- 报表可看到 `fuzzy_click_rate` 与 `no_progress_abort_rate`，可持续观测回归

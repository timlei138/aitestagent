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

### Fix 1：rid 唯一性 ≠ 语义正确性

**文件**：`tools/__init__.py` → `_perform_click_on_element`

**原则**：rid 是精度增强信号，不是匹配绕过许可。当 `rid_is_unique` 时，先用 rid 筛选候选，再在候选上做 label 语义验证——而非跳过验证直接点击。

```python
# 改前（rid 唯一 → 跳过所有匹配直接用）
if rid_is_unique and ctx.device.click_resource_id(rid):
    return True, _format_click_log(desc, el, strategy="resource_id")

# 改后（rid 唯一 → 验证 label 匹配后再用）
if rid_is_unique and _label_matches(el.label, desc) and ctx.device.click_resource_id(rid):
    return True, _format_click_log(desc, el, strategy="resource_id")
```

`_label_matches` 做简单字符重叠判断：目标文本与 element label 有 ≥ 50% 的共同字符即通过（`AC` vs `x!` → 0% → 不通过）。

**不绑定场景**：适用于所有 App，不管什么页面什么 rid。

### Fix 2：NO_PROGRESS 以页面变化为进展信号

**文件**：`agents/graph.py` → `_tools_node`

**原则**："无进展"的判断基准是页面是否发生了变化，而非是否调用了某个特定工具。`_capture_page_id`（含元素签名哈希）已经能检测视图切换——用它作为重置条件。

```python
# 改前：只有 assert_verification 能重置
if name == "assert_verification":
    no_progress_count = 0

# 改后：页面签名变化也可重置
if name == "assert_verification" or _output_has_page_change(output, page_sig_once, page_sig_after):
    no_progress_count = 0
```

注意 `page_sig_after` 已在每轮循环中通过 `_build_page_signature(ctx)` 更新（之前语义冷却的实现已经加了这个变量）。

**不绑定场景**：计算器按键每下都改变显示值（关键值也会变 → `_build_page_signature` 的 label 列表变化 → hash 变化），自动重置；图库翻页、设置切换也同理。

### Fix 3：Agent 通用验证意识

**文件**：`agents/prompts/agent.txt`

**原则**：不是"输入 5 个数字后验证"，而是"连续多个同类操作用一个工具调用验证"。不绑定输入、计算器或任何 App。

```
- 连续执行同类操作（如连续输入、连续选择）超过 5 步后，暂停并调 get_screen_info 确认当前状态正确再继续。
- 若确认后发现操作结果不符合预期，立即修正而非继续投入更多步骤。
```

## 4) 验收标准

- `click("AC")` 不再误点到 `x!`（rid 不同 label 不匹配时跳过）
- `(1.5+(-0.5))×80%` 不再触发 NO_PROGRESS ABORT（每步页面变化自动重置计数器）
- 代码中无 App/package 特判逻辑（不出现 `if "calculator" in package`）

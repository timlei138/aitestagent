# Policy-Driven 执行优化计划（Review 收敛版，2026-07-08）

## 1. 背景与问题

当前 click 路径补丁较多（容器/子项、回退、热词等），继续叠加会提高副作用和维护成本。  
目标是让 Agent 负责语义决策，工具负责确定性执行，减少工具层“猜测”。

## 2. 本次收敛结论

1. **Phase 1“兼容旧参数”采用双轨互斥，不做叠加**  
   - 传入 `rid/class/path_contains` 任一参数时：走**精确模式**，跳过 legacy 匹配/回退管线。  
   - 未传入精确参数时：走 legacy 模式（保留兼容）。

2. **postcondition 不放在工具层做语义判断**  
   - click 返回执行结果和页面快照。  
   - 是否“达到业务预期”由 Agent 在原生循环里通过 `get_screen_info` 判断。

3. **策略外置延期**  
   - 先验证精确参数方案是否覆盖主要失败场景。  
   - 仅当仍存在大量规则残留时，再引入策略外置（YAML/DB）。

4. **Prompt 文本必须具体落地**（见第 5 节）。

## 3. click 双轨执行设计

### 3.1 精确模式（新主路径）

触发条件：`rid || class || path_contains` 任一存在。

执行流程：

1. 从当前 UI tree 过滤候选（按 rid/class/path_contains）。
2. 命中唯一候选后直接执行 click。
3. 返回执行结果（含实际命中元素信息 + 操作后页面摘要）。
4. 若命中多个候选，返回明确错误（不自动选第一个）。
5. 不进入 legacy 语义打分、容器判定、fallback 回退。

伪代码：

```python
if rid or class_name or path_contains:
    matches = _find_exact_candidates(understanding, rid, class_name, path_contains)
    if len(matches) == 0:
        return "ERROR: 未找到匹配元素，请调整 rid/class/path_contains"
    if len(matches) > 1:
        return "ERROR: N 个候选匹配（...），请追加 path_contains 或 rid 缩小范围"
    el = matches[0]
    return _execute(el)  # no ranking, no fallback
```

### 3.2 Legacy 模式（兼容路径）

触发条件：未提供精确参数。  
保留当前语义匹配逻辑，标记为 legacy，后续逐步下线。

## 4. 实施顺序（调整后）

### Phase 1：click 参数化 + 精确模式互斥分流（1~2 天）

- `click` 新增可选参数：`rid`, `class`, `path_contains`
- 新参数触发精确模式，跳过旧管线
- 旧参数 `label/alternatives` 走 legacy
- 增加单测：精确模式不触发 fallback/容器管线

### Phase 3：删减补丁分支并验证覆盖（2 天）

- 在 Phase 1 稳定后，移除或下线：
  - 容器回退链路
  - 目标一致性补丁链路
  - 目的地语义判定补丁链路
- 保留最小安全网（危险操作校验、参数校验、错误透出）

### Phase 4：回放评测与门禁（持续）

- 建立 replay 集（真实失败日志）
  - 应用列表误点图库
  - 热词误点
  - 计算器误点日历
- 门禁：`pytest + replay` 通过才合并

### Phase 2（可选，后置）：策略外置

- 仅在验证后仍有大量规则需要维护时再做。

## 5. Prompt 追加文本（可直接落地）

```text
## 精确点击
click 支持以下可选参数精确定位元素，避免匹配到错误的同类元素：
- class: 指定目标类名（如 "textview" 排除容器 FrameLayout）
- rid: 指定资源 ID（如 "com.zui.launcher:id/search_input_all_apps"）
- path_contains: 指定路径片段（如 "taskbar_container > taskbar_view"）
当页面有多个同名元素时，必须用这些参数区分。
若返回“N 个候选匹配”错误，说明参数不够精确，请在 page_info 中找到唯一区分特征后重试。

click 执行后，先看工具返回的操作后页面信息；若不符合预期，立即调用 get_screen_info 复核并调整下一步，不要盲目重复点击。
```

## 6. 验收标准

1. 精确模式下，不触发 legacy 回退链路（日志可验证）。
2. 无限工作台“应用列表”场景连续 20 次无“图库/日历误点”。
3. 回放集通过率 >= 95%，且全量 pytest 通过。
4. click 核心逻辑可读性提升（删除补丁分支后净减少代码量）。

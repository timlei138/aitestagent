# 执行步骤细化 + 截图增强计划（2026-07-07 v2）

> v2 变更：合入 Review 反馈，修复 ToolContext 重建丢日志、screenshot API、去重冲突、截图清理等问题。

## 1. 问题描述

### 1.1 报告只有 1 步
前端测试报告的"执行详情"只显示 1 步（ABORT），但 Agent 实际执行了 8 次工具调用（launch_app、get_screen_info×4、click×3）。

### 1.2 数据链路分析

```
_run_agent 内部子图:
  L378-395: 从 messages 提取 _tool_calls_log（过滤掉感知类 → 4 条）
  → 返回 (result, _tool_calls_log, loop_meta)

agent_node:
  L772-776: ctx._tool_calls_log.extend(tool_calls_log)  ← 存入 4 条

reporter_node:
  L1032: dd = _build_display_steps(history)
  L1040: record_test_run(steps=dd)

_build_display_steps (orchestrator.py L496):
  从 ctx._tool_calls_log 生成工具步骤 + 追加 history
  理论输出: 4 个工具步骤 + 1 个 ABORT = 5 步
```

### 1.3 根因（经 Review 确认的 5 个问题）

**问题 A：`_build_display_steps` 字段过于稀疏**
```python
# 当前代码 — 每步只有 action_type 和 target
{
    "index": 1,
    "action_type": "launch_app",
    "target": "com.zui.gallery",
    "observation": "",      # 空 → 前端不显示详情
    "screenshot_path": "",  # 空 → 无截图
}
```

**问题 B：ToolContext 重建导致 `_tool_calls_log` 丢失**
`api/server.py::_rebuild_tool_context()` (L164) 会新建 ToolContext，丢失之前积累的 `_tool_calls_log`。
日志显示测试过程中发生了配置热更（L282-285），触发了 rebuild。
**解决**：日志不再依赖全局 ctx，改为存入 graph state（随 run_id 传递）。

**问题 C：screenshot() API 不匹配**
`DeviceController.screenshot()` 无参，返回 PIL Image。方案写 `device.screenshot(path)` 会报错。
**解决**：改为 `img = device.screenshot(); img.save(path)`。

**问题 D：去重 merge 与"显示所有步骤"冲突**
`_build_display_steps` L510-519 会折叠连续重复步骤。
**解决**：去掉去重逻辑（或加 `merge_duplicates=False` 参数）。

**问题 E：缺少截图清理策略**
截图文件会持续积累，无自动清理。
**解决**：按 run_id 归档 + 保留最近 N 个 run 的截图，启动时清理旧文件。

## 2. 修复目标

1. **报告步骤可见性**：每次工具调用都生成一个完整的报告步骤（含 observation、duration、page 信息）
2. **每步截图**：每次工具执行后自动截取屏幕状态，存入 `screenshot_path`
3. **前端正常展示**：确保前端能看到所有步骤及其截图

## 3. 具体修改点

### 3.1 `_tools_node` — 实时记录（解决 A+B+C）

**文件**：`agents/graph.py`

**核心变更**：不再事后从 messages 提取，改为在 `_tools_node` 中实时记录工具调用到 `_SubState`：

```python
def _tools_node(s: _SubState) -> dict:
    # ... 现有逻辑 ...
    _realtime_log = list(s.get("_tool_calls_log", []))  # 从 state 读取

    for tc in last_ai.tool_calls or []:
        name = tc["name"]
        args = tc.get("args", {}) or {}
        output = str(t.invoke(args)) if t else f"UNKNOWN_TOOL: {name}"

        # 截图：仅关键操作，使用正确的 API
        _screenshot_path = ""
        if name in _SCREENSHOT_ACTIONS and _ctx and _ctx.device:
            try:
                from datetime import datetime as _dt
                ts = _dt.now().strftime("%Y%m%d_%H%M%S_%f")
                _screenshot_path = f"storage/screenshots/step_{ts}.png"
                img = _ctx.device.screenshot()  # 返回 PIL Image
                img.save(_screenshot_path)
            except Exception as e:
                logger.warning("Step screenshot failed for %s: %s", name, e)
                _screenshot_path = ""

        # 实时记录（过滤感知类，不去重）
        if name not in ("get_screen_info", "check_page_health", "query_app_knowledge"):
            _realtime_log.append({
                "name": name,
                "target": _build_tool_target(name, args),
                "observation": output[:200],
                "screenshot_path": _screenshot_path,
            })
        # ... 断路器逻辑 ...

    return {
        "messages": outputs,
        "_tool_calls_log": _realtime_log,  # 写回 state（不依赖全局 ctx）
        # ... 其他字段 ...
    }
```

**关键**：`_tool_calls_log` 存入 `_SubState`（graph state），不依赖全局 ToolContext，
解决 rebuild 导致日志丢失的问题。

需要同时在 `_SubState` 定义中增加 `_tool_calls_log: list` 字段。

### 3.2 `_build_display_steps` — 丰富字段 + 去掉去重（解决 A+D）

**文件**：`agents/orchestrator.py` L496-552

**修改**：
1. 传递 `observation` 和 `screenshot_path`
2. **去掉连续去重 merge**（L510-519），改为保留所有步骤
3. 从 graph state 获取 `_tool_calls_log`（通过参数传入，不再从全局 ctx 读）

```python
def _build_display_steps(history: list, tool_calls_log: list) -> list[dict]:
    """从工具调用日志生成展示步骤。不再依赖全局 ToolContext。"""
    # 不再做去重 merge — 每次工具调用都可见
    result = []
    idx = 0
    for t in tool_calls_log:
        idx += 1
        result.append({
            "index": idx,
            "intent": f"{t['name']}('{t.get('target', '')}')" if t.get("target") else t["name"],
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
        })
    # 追加 history 中的 Agent 结论步骤
    for s in history:
        idx += 1
        result.append({**s, "index": idx})
    return result if result else history
```
```

### 3.3 agent_node → reporter_node — 日志传递链路（解决 B）

**文件**：`agents/graph.py`

**agent_node**：从 `_run_agent` 返回值获取 `_tool_calls_log`，存入 graph state：
```python
result, tool_calls_log, loop_meta = _run_agent(...)
# 不再写入 ctx._tool_calls_log（避免 rebuild 丢失）
# 改为存入 state
return Command(update={
    "step_history": nh,
    "_tool_calls_log": list(state.get("_tool_calls_log", [])) + tool_calls_log,
    ...
})
```

**reporter_node**：从 state 读取日志传给 `_build_display_steps`：
```python
tool_log = state.get("_tool_calls_log", [])
dd = _build_display_steps(history, tool_log)
# 统计基于 dd
pc = sum(1 for s in dd if s.get("status") in ("success", "continue"))
fc = sum(1 for s in dd if s.get("status") == "fail")
logger.info("Reporter: ... display_steps=%d", len(dd))
```

需要在 `TestState` 定义中增加 `_tool_calls_log: list` 字段。

### 3.4 前端 ReportDetail.vue — 步骤截图展示

**文件**：`frontend/spa/src/components/ReportDetail.vue`

在步骤展示区域（L43-56）增加截图缩略图：

```html
<div class="rd-step" v-for="s in (report.steps || [])" :key="s.index">
  <div class="rd-step-head">
    <span class="rd-step-idx">{{ s.index }}</span>
    <code class="rd-step-action">{{ s.action_type }}</code>
    <span v-if="s.target" class="rd-step-target">→ {{ s.target }}</span>
    <!-- 新增：步骤截图 -->
    <el-image v-if="s.screenshot_path"
              :src="shotUrl(s.screenshot_path, s.index)"
              :preview-src-list="[shotUrl(s.screenshot_path, s.index)]"
              fit="cover" class="step-shot" />
  </div>
  <div v-if="s.observation" class="rd-step-obs">{{ stripDONE(s.observation) }}</div>
</div>
```

新增样式：
```css
.step-shot { width: 40px; height: 30px; border-radius: 4px; cursor: pointer; 
             object-fit: cover; border: 1px solid var(--line); margin-left: 8px; }
```

### 3.5 截图性能优化 + 清理策略（解决 C+E）

**截图 API 正确使用**：
```python
_SCREENSHOT_ACTIONS = {
    "click", "long_press", "scroll_find_and_click",
    "launch_app", "assert_verification", "swipe",
}

def _take_step_screenshot(ctx) -> str:
    """截取当前屏幕，返回相对路径（前端可直接访问）。"""
    from datetime import datetime as _dt
    ts = _dt.now().strftime("%Y%m%d_%H%M%S_%f")
    path = f"storage/screenshots/step_{ts}.png"
    img = ctx.device.screenshot()  # 返回 PIL Image（无参）
    img.save(path)
    return path
```

**截图失败不影响主流程**：
```python
try:
    _screenshot_path = _take_step_screenshot(_ctx)
except Exception as e:
    logger.warning("Step screenshot failed for %s: %s", name, e)
    _screenshot_path = ""
```

**清理策略**（在服务启动时执行）：
```python
def _cleanup_old_screenshots(keep_runs: int = 20):
    """保留最近 N 个 run 的截图，删除更早的。"""
    import glob, os
    # 按时间排序，保留最新的
    files = sorted(glob.glob("storage/screenshots/step_*.png"))
    if len(files) > keep_runs * 15:  # 每个 run 约 15 张
        for f in files[:len(files) - keep_runs * 15]:
            os.remove(f)
```

**截图路径统一为相对路径**（`storage/screenshots/...`），前端 `shotUrl` 可直接拼接访问。

预估性能影响：
- 每次截图 ~200ms（`device.screenshot()` + `img.save()`）
- 一次测试约 5-10 个关键操作 → 额外 1-2 秒
- 总耗时占比 <5%，可接受

## 4. 代码改动量

| 文件 | 操作 | 行数变化 |
|------|------|--------|
| `agents/graph.py` | `_SubState` 增加 `_tool_calls_log` 字段 | +3行 |
| `agents/graph.py` | `_tools_node` 实时记录 + 截图 | +35行 |
| `agents/graph.py` | `_run_agent` 返回改用实时日志 | -15行 / +5行 |
| `agents/graph.py` | `TestState` 增加 `_tool_calls_log` 字段 | +2行 |
| `agents/graph.py` | `agent_node` 写入 state | ~5行 |
| `agents/graph.py` | `reporter_node` 统计修正 | ~8行 |
| `agents/orchestrator.py` | `_build_display_steps` 参数化 + 去 merge | -10行 / +15行 |
| `api/server.py` | 启动时 `_cleanup_old_screenshots` | +10行 |
| `frontend/.../ReportDetail.vue` | 步骤截图展示 | +10行 |
| **合计** | | **+88行净增** |

## 5. 验收标准

- [ ] 报告"执行详情"显示所有工具调用步骤（≥4 步）
- [ ] 每步显示 observation（工具输出摘要）
- [ ] 关键操作（click/launch_app）步骤有截图缩略图
- [ ] 截图可点击放大预览
- [ ] 总耗时增加 <5%（截图开销）
- [ ] Reporter 日志 display_steps 数与实际展示步骤数一致
- [ ] ToolContext rebuild 后日志不丢失（`_tool_calls_log` 在 state 中）
- [ ] 截图失败时打 warning 而非静默吞掉
- [ ] 旧截图按保留策略清理（最近 20 run）

## 6. 回滚方案

- 代码：`git revert`
- 截图功能可通过 `_SCREENSHOT_ACTIONS` 设为空集合快速关闭

## 7. Review 问题修复清单

| # | 问题 | 解决位置 | 方案 |
|---|------|---------|------|
| A | 步骤字段稀疏 | §3.1 + §3.2 | 实时记录 observation，传递到 display steps |
| B | ToolContext rebuild 丢日志 | §3.1 + §3.3 | 日志存入 graph state（`_tool_calls_log` 字段），不依赖全局 ctx |
| C | screenshot() 无参 | §3.5 | `img = device.screenshot(); img.save(path)` |
| D | 去重 merge 折叠步骤 | §3.2 | 去掉 merge 逻辑，保留所有步骤 |
| E | 截图无清理 | §3.5 | 服务启动时按时间清理旧文件 |

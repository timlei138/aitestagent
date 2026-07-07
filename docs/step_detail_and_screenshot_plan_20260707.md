# 执行步骤细化 + 截图增强计划（2026-07-07 v6）

> v6 变更：清理白名单放宽为合法目录名、链路分析标注“改造前”。
> v4 变更：合入第三轮 Review 反馈，明确 run_id 来源与路径清洗、tool_seq 对齐、清理按 mtime 排序、补强验收项。
> v3 变更：合入第二轮 Review 反馈，补全 `_build_display_steps` 调用点、State 字段初始化、截图目录保证、run_id 归档路径。
> v2 变更：合入 Review 反馈，修复 ToolContext 重建丢日志、screenshot API、去重冲突、截图清理等问题。

## 1. 问题描述

### 1.1 报告只有 1 步
前端测试报告的"执行详情"只显示 1 步（ABORT），但 Agent 实际执行了 8 次工具调用（launch_app、get_screen_info×4、click×3）。

### 1.2 数据链路分析（改造前）

> 以下为改造前链路，改造后见 §3。

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

**前置改动：State 字段补齐**

`_SubState` (L113-118) 和 `TestState` (state.py L26) 都需要增加字段：

```python
# agents/state.py — TestState
class TestState(TypedDict, total=False):
    # ... 现有字段 ...
    _tool_calls_log: list  # 工具调用实时日志（存入 state，不依赖 ctx）

# agents/graph.py — _SubState
class _SubState(_TD):
    messages: Annotated[list, add_messages]
    _turn_count: int
    _recent_call_sigs: list[str]
    _loop_break_reason: str
    _no_progress_count: int
    _tool_calls_log: list  # 新增
    _run_id: str           # 新增（截图目录需要）
```

**`_run_agent` 入参显式传 run_id**（解决 `_tools_node` 拿不到外层 thread_id 的问题）：
```python
# _run_agent 签名增加 run_id
def _run_agent(messages, tools, ..., run_id: str = "") -> tuple:
    ...
    initial_state = {
        "messages": [HumanMessage(content=human_prompt)],
        "_turn_count": 0,
        "_recent_call_sigs": [],
        "_loop_break_reason": "",
        "_no_progress_count": 0,
        "_tool_calls_log": [],  # 新增
        "_run_id": run_id,      # 新增
    }
```

外层 `agent_node` 调用时传入：
```python
# TestState 无 thread_id 字段，从 RunnableConfig 取
thread_id = config.get("configurable", {}).get("thread_id", "unknown")
result, tool_calls_log, loop_meta = _run_agent(
    ..., run_id=thread_id
)
```

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
                run_id = s.get("_run_id", "unknown")
                step_idx = len(_realtime_log) + 1  # tool_seq 序号
                _screenshot_path = _take_step_screenshot(_ctx, run_id, step_idx)
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
                "tool_seq": len(_realtime_log) + 1,  # 工具调用序号（与截图对齐）
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

### 3.2 `_build_display_steps` — 丰富字段 + 去掉去重（解决 A+D）

**文件**：`agents/orchestrator.py` L496-552

**修改**：
1. 传递 `observation` 和 `screenshot_path`
2. **去掉连续去重 merge**（L510-519），改为保留所有步骤
3. 从 graph state 获取 `_tool_calls_log`（通过参数传入，不再从全局 ctx 读）
4. 兼容空参数（保留 `history` 单参签名默认值）

```python
def _build_display_steps(history: list, tool_calls_log: list | None = None) -> list[dict]:
    """从工具调用日志生成展示步骤。不再依赖全局 ToolContext。
    tool_calls_log 为 None 时回退旧行为（兼容调用点未改的场景）。
    """
    if tool_calls_log is None:
        tool_calls_log = []
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

**所有调用点必须同步修改**：

| 调用点 | 文件 | 修改 |
|--------|------|------|
| `reporter_node` | `graph.py:L1032` | `dd = _build_display_steps(history, tool_log)` |
| `_build_result` | `orchestrator.py:L402` | `display_steps = _build_display_steps(state.get("step_history", []), state.get("_tool_calls_log", []))` |

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

`TestState` 字段已在 §3.1 中补齐。

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

def _take_step_screenshot(ctx, run_id: str, tool_seq: int) -> str:
    """截取当前屏幕，返回相对路径（前端可直接访问）。
    路径格式：storage/screenshots/{safe_run_id}/{tool_seq}_{ts}.png
    """
    import os, re
    from datetime import datetime as _dt
    # 路径安全清洗：只保留 [a-zA-Z0-9_-]
    safe_run_id = re.sub(r"[^\w\-]", "_", run_id)
    if not safe_run_id:
        safe_run_id = "unknown"
    shot_dir = os.path.join("storage", "screenshots", safe_run_id)
    os.makedirs(shot_dir, exist_ok=True)  # 确保目录存在
    ts = _dt.now().strftime("%Y%m%d_%H%M%S_%f")
    path = os.path.join(shot_dir, f"{tool_seq}_{ts}.png")
    img = ctx.device.screenshot()  # 返回 PIL Image（无参）
    img.save(path)
    return path  # 返回相对路径，前端 shotUrl 直接拼接
```

**截图失败不影响主流程**：
```python
try:
    _screenshot_path = _take_step_screenshot(_ctx, run_id, tool_seq)
except Exception as e:
    logger.warning("Step screenshot failed for %s: %s", name, e)
    _screenshot_path = ""
```

**清理策略**（在服务启动时执行）：
```python
def _cleanup_old_screenshots(keep_runs: int = 20):
    """保留最近 N 个 run 的截图目录（按 mtime 排序），删除更早的。"""
    import os, shutil
    base = os.path.join("storage", "screenshots")
    if not os.path.isdir(base):
        return
    # 只清理符合 run_id 命名规则的目录（合法目录名，排除手工目录）
    import re
    _run_dir_re = re.compile(r"^[A-Za-z0-9_\-]+$")
    dirs = [
        (d, os.path.getmtime(os.path.join(base, d)))
        for d in os.listdir(base)
        if os.path.isdir(os.path.join(base, d)) and _run_dir_re.match(d)
    ]
    dirs.sort(key=lambda x: x[1], reverse=True)
    for old_dir, _ in dirs[keep_runs:]:
        shutil.rmtree(os.path.join(base, old_dir), ignore_errors=True)
```

截图路径统一为 `storage/screenshots/{safe_run_id}/{tool_seq}_{ts}.png` 结构，
按 run 目录 mtime 排序清理，逻辑更稳、可追踪。
前端 `shotUrl` 可直接拼接访问。

预估性能影响：
- 每次截图 ~200ms（`device.screenshot()` + `img.save()`）
- 一次测试约 5-10 个关键操作 → 额外 1-2 秒
- 总耗时占比 <5%，可接受

## 4. 代码改动量

| 文件 | 操作 | 行数变化 |
|------|------|--------|
| `agents/state.py` | `TestState` 增加 `_tool_calls_log` 字段 | +2行 |
| `agents/graph.py` | `_SubState` 增加 `_tool_calls_log` 字段 | +1行 |
| `agents/graph.py` | `_tools_node` 实时记录 + 截图 | +35行 |
| `agents/graph.py` | `_run_agent` 初始 state + 返回改用实时日志 | -15行 / +8行 |
| `agents/graph.py` | `agent_node` 写入 state | ~5行 |
| `agents/graph.py` | `reporter_node` 统计修正 | ~8行 |
| `agents/orchestrator.py` | `_build_display_steps` 参数化 + 去 merge | -10行 / +15行 |
| `agents/orchestrator.py` | `_build_result` 调用点传 `tool_calls_log` | ~2行 |
| `api/server.py` | 启动时 `_cleanup_old_screenshots` | +10行 |
| `frontend/.../ReportDetail.vue` | 步骤截图展示 | +10行 |
| **合计** | | **+91行净增** |

## 5. 验收标准

### 前端可见性
- [ ] 报告"执行详情"显示所有工具调用步骤（≥4 步）
- [ ] 每步显示 observation（工具输出摘要）
- [ ] 关键操作（click/launch_app）步骤有截图缩略图
- [ ] 截图可点击放大预览
- [ ] 总耗时增加 <5%（截图开销）

### 数据一致性
- [ ] Reporter 日志 display_steps 数与 DB `total_steps` 一致
- [ ] `test_runs.steps_json` 步骤数 > 1
- [ ] 至少一条 `screenshot_path` 文件真实存在（`os.path.exists` 验证）

### 健壮性
- [ ] ToolContext rebuild 后日志不丢失（`_tool_calls_log` 在 state 中）
- [ ] 截图失败时打 warning 而非静默吞掉
- [ ] run_id 经路径安全清洗（无非法字符）
- [ ] 旧截图按 mtime 保留最近 20 run 目录

## 6. 回滚方案

- 代码：`git revert`
- 截图功能可通过 `_SCREENSHOT_ACTIONS` 设为空集合快速关闭

## 7. Review 问题修复清单

### 第一轮（5 个问题）
| # | 问题 | 解决位置 | 方案 |
|---|------|---------|------|
| A | 步骤字段稀疏 | §3.1 + §3.2 | 实时记录 observation，传递到 display steps |
| B | ToolContext rebuild 丢日志 | §3.1 + §3.3 | 日志存入 graph state（`_tool_calls_log` 字段），不依赖全局 ctx |
| C | screenshot() 无参 | §3.5 | `img = device.screenshot(); img.save(path)` |
| D | 去重 merge 折叠步骤 | §3.2 | 去掉 merge 逻辑，保留所有步骤 |
| E | 截图无清理 | §3.5 | 服务启动时按 run 目录清理旧文件 |

### 第二轮（4 个问题）
| # | 问题 | 解决位置 | 方案 |
|---|------|---------|------|
| F | `_build_display_steps` 两个调用点 | §3.2 | reporter + `_build_result` 都改为传 `tool_calls_log` |
| G | State 字段缺失 | §3.1 | `_SubState` + `TestState` 都加 `_tool_calls_log`，`_run_agent` 初始 state 补 `[]` |
| H | 截图目录不存在 | §3.5 | `os.makedirs(shot_dir, exist_ok=True)` |
| I | 截图路径平铺不清理 | §3.5 | 改为 `storage/screenshots/{safe_run_id}/{tool_seq}_{ts}.png`，按目录清理 |

### 第三轮（5 个问题）
| # | 问题 | 解决位置 | 方案 |
|---|------|---------|------|
| J | run_id 来源不准确 | §3.1 | 从 `config["configurable"]["thread_id"]` 取（TestState 无该字段），传给 `_run_agent` |
| K | run_id 路径注入风险 | §3.5 | `re.sub(r"[^\w\-]", "_", run_id)` 清洗为安全目录名 |
| L | tool_seq 与 display index 错位 | §3.1 + §3.2 | 日志记录 `tool_seq`，截图文件名用 `tool_seq`，前后端用它关联 |
| M | 验收标准缺强验证 | §5 | 补 DB steps_json > 1、screenshot 文件存在、display_steps 与 total_steps 一致 |
| N | 清理按名称排序可能删错 | §3.5 | 改为 `os.path.getmtime()` 按目录修改时间排序 |

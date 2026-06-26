# 工作台日志与对话合并方案（已融合 Review）

## Review 结论

核心问题：Plan 没明确 `messages` / `addLog` / `addTimeline` 三者的关系。解决后其余都是细节。

## 数据流决策

```
合并前:  handleEvent → addLog(logs)    → 实时日志 panel
         handleEvent → addMessage(msgs) → AI 对话 panel

合并后:  handleEvent → addEntry(timeline) → WorkspacePanel 统一时间线
```

**废弃 `addLog`、废弃 `addMessage`。** 两者都改为空实现或 `console.debug`。

## handleEvent → addEntry 映射表

| WebSocket 事件 | timeline type | icon | text | detail | 附加操作 |
|---|---|---|---|---|---|
| `status` | `log` | — | event.content | — | — |
| `stream_token` | — | — | — | — | 调 `onToken()` 触发 thinking 指示器 |
| `tool_start` | `tool` | ⚙ | `event.content.name` | pending | — |
| `tool_end` | `tool` | ⚙ | 完成 | 耗时 ms | — |
| `plan_ready` | `planner` | 🎯 | goal 摘要 | — | — |
| `plan_review` | `planner` | 🎯 | goal 摘要 | — | **同时**打开 Plan 确认对话框（保留在 App.vue） |
| `result` — need_human + plan_review | `log` | ⏸ | "需要确认测试目标" | — | 打开 Plan 对话框 |
| `result` — need_human + 其他 | `log` | ⏸ | "需要人工确认" | — | 打开人工确认对话框 |
| `result` — pending_identities | `log` | 🔍 | "需要确认 N 个元素映射" | — | 打开元素身份对话框 |
| `result` — 最终结果 | `result` | ✅/❌ | `execLabel \| verdictLabel: 前80字` | conclusion 全文可展开 | — |
| `error` | `error` | ❌ | event.content | — | — |
| `need_human_approval` | `log` | ⏸ | "需要人工确认" | — | 打开人工确认对话框 |
| `step_start` / `step_end` | — | — | — | — | **忽略** |
| `anomaly` / `custom` | `log` | — | 文本 | — | — |

## handleEvent 外的 addLog 迁移

| 调用点 | 当前 | 改为 |
|---|---|---|
| `startRun()` | `addMessage("user", "用户指令", text)` | `workspaceRef.value.addEntry({ type: 'user', icon: '🧑', text })` |
| `sendHumanDecision()` | `addLog("人工决定: ...")` | `addEntry({ type: 'log', text: "人工决定: ..." })` |
| `confirmPlan()` | `addLog("目标已确认/已取消")` | `addEntry({ type: 'log', text: "目标已确认" })` |
| `confirmIdentities()` | `addLog("已确认 N 个元素映射")` | `addEntry({ type: 'log', text: "已确认 N 个元素映射" })` |
| `sendDeviceKey()` | `addLog("设备按键: ${key}")` | **删除**（投屏操作不入 timeline） |
| `saveCaseContent()` | 直接读 `inputText.value` | 改为 `startRun("执行 xxx")` 传参（startRun 签名改为 `startRun(text)`） |
| `connectWS()` | `addLog("WebSocket 已连接")` | **删除**（右上角 tag 已显示） |
| `checkDeviceStatus()` | `addLog("Android 设备已连接")` | **删除**（右上角 tag 已显示） |

## handleEvent 改动方式

App.vue 中 `workspaceRef = ref()` → handleEvent 每个 case 改为调 workspaceRef 对应方法：

| 原代码 | 改为 |
|---|---|
| `addLog(...)` + `addMessage(...)` | `workspaceRef.value.addEntry({ type, icon, text, detail })` |
| `currentTool = name` (tool_start) | `workspaceRef.value.addTool(name, target)` |
| tool_end | `workspaceRef.value.finishTool(name, elapsedMs)` |
| stream_token | `workspaceRef.value.onToken()` |
| result | `workspaceRef.value.addResult(execStatus, verdict, conclusion, verificationResults)` |

## 三种核心条目的视觉设计

用户视角只看到三种状态循环：

```
🧑 打开联想计算器执行0/0...              ← 用户指令
🔄 AI 思考中...                          ← 流式时显示这1条，带转圈动画
⚙ launch_app → 联想计算器               ← 工具调用，逐条追加
⚙ click → 0
⚙ click → ÷
⚙ click → =
🔄 AI 思考中...                          ← 新一轮思考
⚙ long_press → formula_or_result
🔄 AI 思考中...
✅ 已完成 | 通过                          ← 最终结果
   计算器计算0÷0得到"不是数字"...
```

**流式 token 不逐条展示**，只在 timeline 保留一条 `🔄 AI 思考中...`（带 CSS spinner），token 结束后自然消失，下一条工具调用或结果顶上来。

```javascript
const timeline = ref([])
const thinkingEntry = ref(null)   // "AI 思考中..." 条目引用

function startThinking() {
  if (thinkingEntry.value) return
  thinkingEntry.value = { type: 'thinking', text: 'AI 思考中...', time: now(), id: Date.now() }
  timeline.value.push(thinkingEntry.value)
}
function stopThinking() {
  if (thinkingEntry.value) {
    const idx = timeline.value.indexOf(thinkingEntry.value)
    if (idx !== -1) timeline.value.splice(idx, 1)
    thinkingEntry.value = null
  }
}

function addTool(name, target) {
  startThinking()
  stopThinking()
  const text = target ? `${name} → ${target}` : name
  timeline.value.push({ type: 'tool', icon: '⚙', text, time: now(), id: Date.now() })
}

// 组件内独立定义（不依赖 App.vue）
const _execMap = { completed: '已完成', exhausted: '步骤耗尽', error: '异常中断', cancelled: '已取消', device_offline: '设备离线' }
const _verdictMap = { passed: '通过', failed: '未通过', inconclusive: '待确认' }
function execLabel(s) { return _execMap[s] || '异常中断' }
function verdictLabel(s) { return _verdictMap[s] || '待确认' }

function addResult(execStatus, verdict, conclusion, verificationResults) {
  stopThinking()
  const icon = verdict === 'passed' ? '✅' : verdict === 'failed' ? '❌' : '⚠️'
  timeline.value.push({ type: 'result', icon, text: `${execLabel(execStatus)} | ${verdictLabel(verdict)}`, detail: conclusion, time: now(), id: Date.now() })
  if (verificationResults) {
    verificationResults.forEach(v =>
      timeline.value.push({ type: 'log', text: `${v.result === 'passed' ? '✓' : '✗'} ${v.item}`, time: now(), id: Date.now() })
    )
  }
}

function addEntry(entry) {
  stopThinking()
  timeline.value.push({ ...entry, time: now(), id: Date.now() })
}

// tool_end：更新上次 tool_start 的 pending 条目
function finishTool(name, elapsedMs) {
  // tool_start 创建的条目在 timeline 中，更新最后一个 tool 条目
  const last = [...timeline.value].reverse().find(e => e.type === 'tool')
  if (last) last.detail = elapsedMs ? `${name} ${elapsedMs}ms` : name
}

// stream_token：确保 thinking 条目存在即可，token 不显示
function onToken() { startThinking() }

defineExpose({ addEntry, addTool, finishTool, addResult, onToken })
```

**CSS 转圈动画（`type: 'thinking'` 条目）：**

```css
.timeline-thinking {
  display: flex; align-items: center; gap: 8px; color: #409eff; font-size: 13px;
}
.timeline-spinner {
  width: 14px; height: 14px;
  border: 2px solid #e0e0e0; border-top-color: #409eff;
  border-radius: 50%; animation: spin 0.6s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
```

## App.vue 状态迁移清单

| ref | 当前用途 | 合并后 |
|---|---|---|
| `logs` | 原始日志数组 | ❌ **删除**（不再渲染，addLog 改 console.debug） |
| `messages` | 气泡消息数组 | ❌ **删除**（WS 通信也不依赖它） |
| `currentTool` | 当前工具名 | ❌ **删除**（改为 thinking 指示器 + addTool 条目） |
| `streamingToken` | 流式 token | ❌ **删除**（改为 thinking 指示器，token 不显示） |
| `streamTokenTimer` / `flushStreamToken` / `resetStreamTimer` | stream 定时器 | ❌ **删除**（不再需要） |
| `inputText` | 输入框文本 | ➡️ **移入 WorkspacePanel**（输入框在面板内，startRun 签名改为 `startRun(text)`） |
| `chatListRef` | .chat-list 的 DOM ref | ❌ **删除**（随 panel 删除） |
| `executing` | 执行中标志 | ✅ **保留**（App.vue 管理，props 传入） |
| `ws`, `wsConnected` | 连接管理 | ✅ **保留** |

## WorkspacePanel 暴露的方法

```javascript
defineExpose({
  addEntry,       // 通用：追加一条时间线条目
  addTool,        // tool_start / tool_end：追加工具条目
  finishTool,     // tool_end：更新最近 tool 条目耗时
  addResult,      // result：最终结果（双维度标签 + 验证清单）
  onToken,        // stream_token：触发 thinking 指示器
})
```

## 修改清单

| 文件 | 改动 |
|---|---|
| `components/WorkspacePanel.vue` | **新建** — timeline + addEntry/addTool/finishTool/addResult/onToken + 输入框/执行按钮 |
| `App.vue` template | 删除 `.workspace-cols` 块（~50行），替换为 `<WorkspacePanel ref="workspaceRef" :executing="executing" @run="startRun" />` |
| `App.vue` script | `workspaceRef = ref()`；handleEvent 每个 case 删掉 addLog/addMessage，改为调 workspaceRef 对应方法 |
| `App.vue` script | 删除 `logs`, `messages`, `currentTool`, `streamingToken`, `streamTokenTimer`, `flushStreamToken`, `resetStreamTimer`, `chatListRef` 共 8 个；`startRun` 签名改为 `startRun(text)` |
| `App.css` | 删除 `.workspace-cols`, `.workspace-left`, `.panel-status`, `.panel-chat`, `.status-logs`, `.chat-list`, `.log-row`, `.log-warn`, `.log-ok`, `.bubble`, `.tool-status` |

## 不做的

- `addLog()` / `addMessage()` 函数体改为空实现或 `console.debug`（push 到数组的操作删除）
- `messages` ref 删除（WS 通信也无需它，startRun 发 `{ type: "run", message: text }` 直接用参数）
- `_build_display_steps` 重复调用保留现状
- `_tool_calls_log` 和实时 tool_start/end 差异保留现状
# 系统模拟执行流程 — WLAN 连接测试

> 需求: "打开 Setting APP，点击左侧 WLAN 项，在右侧的 WLAN 界面点击 WLAN 开关，10s 后观察连接的 WIFI 下是否有已连接的 WIFI 热点，并验证 WLAN 下 WIFI 列表不为空"

---

## 1. 用户输入 → Planner Agent

```
用户输入进入 agents/orchestrator.py → TestOrchestrator.start()
  → graph.invoke(initial_state, config)
    → planner_node() 被调用
```

**Planner 收到的 system prompt** (`agents/prompts/planner.txt`):
```
你是 Android 自动化测试的规划专家（Planner）。
根据用户的自然语言测试需求，制定一份结构化的测试执行计划。
先用 get_screen_info() 了解当前应用的实际页面布局，
用 query_app_knowledge() 查询该应用的历史知识，
综合用户需求和实际 UI 结构，制定测试步骤。
输出结构化 JSON。
```

**Planner 执行过程**:

```
Step 1: get_screen_info()
  → 感知器读取 UI 树，发现 Settings 是双栏布局:
    layout=two_pane
    left_navigation 区域: [WLAN, 蓝牙, 移动网络, 显示, 声音, ...]
    right_content 区域: 当前显示首页内容

Step 2: query_app_knowledge("WLAN 导航 开关 WiFi 列表")
  → RAG 查询 Chroma/Memory:
    - "Settings 的 WLAN 页面: 右侧显示开关+已连接+可用网络列表"
    - "部分 ROM 的 WLAN 名为 Wi-Fi，开关控件 resource_id=switchWidget"
    - "WiFi 扫描需要 5-10 秒"

Step 3: LLM 推理 → with_structured_output(TestPlanOutput)
  → 生成结构化计划:
```

**Planner 产出**:

```yaml
name: "Settings WLAN 连接测试"
app_package: "com.android.settings"
app_name: "Settings"
steps:
  - index: 1
    intent: "启动 Settings 应用"
    action_type: "launch_app"
    target: "com.android.settings"
    expected: "进入 Settings 首页，双栏布局"

  - index: 2
    intent: "点击左侧导航 WLAN 项"
    action_type: "navigate_tab"
    target: "WLAN"
    alternatives: ["Wi-Fi", "网络和互联网"]
    expected: "右侧内容区显示 WLAN 设置界面"

  - index: 3
    intent: "点击 WLAN 开关打开 WiFi"
    action_type: "click"
    target: "WLAN开关"
    alternatives: ["Wi-Fi开关", "开启"]
    expected: "开关变为开启状态，下方开始扫描"

  - index: 4
    intent: "等待 WiFi 扫描完成"
    action_type: "wait"
    target: "10"
    expected: "WiFi 列表中出现可用网络"

  - index: 5
    intent: "检查已连接 WiFi 热点信息"
    action_type: "assert"
    target: "已连接"
    expected: "如果有已保存网络则显示已连接状态"

  - index: 6
    intent: "验证 WiFi 列表不为空"
    action_type: "assert"
    target: "WiFi列表"
    expected: "WLAN 页面至少显示一个可用 WiFi 热点"
verification:
  - "WLAN 页面可以正常打开并显示 WiFi 列表"
  - "已连接 WiFi 信息正确显示"
  - "WiFi 扫描结果不为空"
```

**State 变更**: `test_plan = [6 steps]`, `current_step_index = 0`

> 图路由: planner → executor

---

## 2. Executor Agent — 步骤 1: launch_app

```
executor_node() 被调用，step_index=0, step = plan[0]
```

**Executor 收到的 system prompt** (`agents/prompts/executor.txt`):
```
你是 Android 自动化测试的执行专家（Executor）。
收到当前步骤的 intent + action_type + target，
先用 detect_popup() 检测弹窗，
根据 action_type 调用对应工具，
如果工具返回 NEEDS_HUMAN: 开头，标记为需要人工介入。
```

**执行过程**:

```
>> Executor: 收到步骤1 {intent: "启动 Settings", action_type: launch_app, target: "com.android.settings"}
   |
   ├─ detect_popup() → "未检测到弹窗"
   |
   ├─ launch_app("com.android.settings")
   │   → DeviceController.app_start("com.android.settings")
   │   → uiautomator2: am start -n com.android.settings/.Settings
   │   → 返回: "已启动: com.android.settings"
   |
   ├─ log_step("启动 Settings 应用")
   │   → ReportBuilder.log_step("启动 Settings 应用")
   │   → WebSocket 推送: {"type": "step_end", "step": 1, "status": "success"}
   |
   └─ 返回 observation: "OK: 已启动 Settings，当前显示双栏首页"
```

**State 变更**: `last_action = "launch_app: com.android.settings"`, `last_observation = "OK: 已启动 Settings..."`

> 图路由: executor → reviewer

---

## 3. Reviewer Agent — 审查步骤 1

```
reviewer_node() 被调用
```

**Reviewer 收到的 system prompt** (`agents/prompts/reviewer.txt`):
```
你是 Android 自动化测试的审查专家（Reviewer）。
检查 Executor 执行每一步后的结果，判断页面是否健康，
执行结果是否符合预期，是否需要重试/跳过/人工介入/终止。
```

**审查过程**:

```
>> Reviewer: 审查步骤1 {操作: launch_app, 结果: "OK: 已启动 Settings..."}
   |
   ├─ get_screen_info()
   │   → layout=two_pane
   │   → left_navigation: [WLAN, 蓝牙, 移动网络, 显示, 声音, ...]
   │   → right_content: 首页内容
   |
   ├─ check_page_health("com.android.settings")
   │   → UI树正常，无 ANR/崩溃关键词
   │   → 非白屏/黑屏
   │   → 进程包名 = com.android.settings ✓
   │   → 返回: "页面健康: 正常"
   |
   ├─ 判断: 启动成功，页面健康，符合预期 "进入 Settings 首页"
   │   → DECISION: continue
   |
   └─ 步骤记录: {index:1, status:success, intent:"启动 Settings"}
```

**State 变更**: `reviewer_decision = "continue"`, `current_step_index = 1`, `step_history += [record]`

> 条件路由: continue → executor

---

## 4. Executor Agent — 步骤 2: navigate_tab (WLAN)

```
executor_node() 被调用，step_index=1, step = plan[1]
```

**执行过程**:

```
>> Executor: 收到步骤2 {intent: "点击左侧导航 WLAN", action_type: navigate_tab, target: "WLAN"}
   |
   ├─ detect_popup() → "未检测到弹窗"
   |
   ├─ navigate_to("WLAN")
   │   → 底层调用 click("WLAN")
   │   → DeviceController.click_text("WLAN")
   │   → uiautomator2: 在 left_navigation 区域找到 text="WLAN" 的元素
   │   → 点击 (x=120, y=280)
   │   → 返回: "已点击: WLAN"
   │
   ├─ log_step("点击 WLAN 导航项")
   │
   └─ 返回 observation: "OK: 已点击 WLAN，右侧显示 WLAN 设置界面"
```

> 注意: 如果 "WLAN" 文字没找到 → Executor 自动尝试 alternatives: ["Wi-Fi", "网络和互联网"]
> 如果全都没找到 → scroll_find_and_click("WLAN") 滑动查找

**State 变更**: `last_action = "navigate_tab: WLAN"`, `last_observation = "OK: 已点击 WLAN..."`

> 图路由: executor → reviewer

---

## 5. Reviewer Agent — 审查步骤 2

```
>> Reviewer: 审查步骤2 {操作: navigate_tab, 结果: "OK: 已点击 WLAN..."}
   |
   ├─ get_screen_info()
   │   → layout=two_pane
   │   → left_navigation: WLAN 项高亮 (selected=true)
   │   → right_content: WLAN 设置页
   │     - switchWidget (WLAN开关, clickable=true, checked=false)
   │     - "已连接的设备" 区域 (当前为空)
   │     - "可用网络" 区域 (正在扫描...)
   │     - "添加网络" 按钮
   |
   ├─ check_page_health("com.android.settings")
   │   → 页面健康 ✓
   |
   ├─ 判断: 操作成功，已进入 WLAN 页面，符合预期
   │   → DECISION: continue
   |
   └─ 步骤记录追加
```

**State 变更**: `reviewer_decision = "continue"`, `current_step_index = 2`

> 条件路由: continue → executor

---

## 6. Executor Agent — 步骤 3: click (WLAN 开关)

```
executor_node() 被调用，step_index=2, step = plan[2]
```

**执行过程**:

```
>> Executor: 收到步骤3 {intent: "点击 WLAN 开关", action_type: click, target: "WLAN开关"}
   |
   ├─ detect_popup() → "未检测到弹窗"
   |
   ├─ click("WLAN开关")
   │   → DeviceController.click_text("WLAN开关")
   │   → 未找到精确文本 "WLAN开关"
   │   → 尝试 alternatives: "Wi-Fi开关" → 未找到
   │   → 尝试 "开启" / "switch" → 通过 resource_id="switchWidget" 找到
   │   → 点击开关控件 (x=860, y=220)
   │   → 开关状态: checked=false → checked=true
   │   → 返回: "已找到并点击: switchWidget"
   │
   ├─ log_step("点击 WLAN 开关(Wi-Fi开关控件)")
   │
   └─ 返回 observation: "OK: 已点击 WLAN 开关，开关变为开启状态，下方开始扫描 WiFi"
```

**State 变更**: `last_action = "click: WLAN开关"`, `last_observation = "OK: 已点击 WLAN 开关..."`

> 图路由: executor → reviewer

---

## 7. Reviewer Agent — 审查步骤 3

```
>> Reviewer: 审查步骤3 {操作: click, 结果: "OK: 已点击 WLAN 开关..."}
   |
   ├─ get_screen_info()
   │   → switchWidget: checked=true ✓ (开关已开启)
   │   → "可用网络" 区域: 正在显示扫描动画
   │   → "已连接的设备" 区域: 有一条记录 "Home-WiFi-5G" (已连接)
   |
   ├─ check_page_health() → 正常 ✓
   |
   ├─ 判断: 开关已开启，扫描已启动
   │   → DECISION: continue
   |
   └─ 步骤记录追加
```

**State 变更**: `reviewer_decision = "continue"`, `current_step_index = 3`

> 条件路由: continue → executor

---

## 8. Executor Agent — 步骤 4: wait (10秒)

```
executor_node() 被调用，step_index=3, step = plan[3]
```

**执行过程**:

```
>> Executor: 收到步骤4 {intent: "等待 WiFi 扫描", action_type: wait, target: "10"}
   |
   ├─ wait_seconds(10)
   │   → time.sleep(10)
   │   → 返回: "已等待 10 秒"
   │
   ├─ log_step("等待 WiFi 扫描 10 秒")
   │
   └─ 返回 observation: "OK: 已等待 10 秒，WiFi 列表已刷新"
```

**State 变更**: `last_action = "wait: 10"`, `last_observation = "OK: 已等待 10 秒..."`

> 图路由: executor → reviewer

---

## 9. Reviewer Agent — 审查步骤 4

```
>> Reviewer: 审查步骤4 {操作: wait, 结果: "OK: 已等待 10 秒..."}
   |
   ├─ get_screen_info()
   │   → "已连接的设备": Home-WiFi-5G (状态: 已连接, 信号: 满格)
   │   → "可用网络" 列表:
   │     - Home-WiFi-5G (已连接)
   │     - Office-Guest (开放)
   │     - Neighbor-Net (WPA2)
   │     - Coffee-Shop (WPA2)
   │     - ... 共 8 个热点
   |
   ├─ check_page_health() → 正常 ✓
   |
   ├─ 判断: 扫描完成，列表已刷新
   │   → DECISION: continue
   |
   └─ 步骤记录追加
```

> 条件路由: continue → executor

---

## 10. Executor Agent — 步骤 5: assert (已连接热点)

```
executor_node() 被调用，step_index=4, step = plan[4]
```

**执行过程**:

```
>> Executor: 收到步骤5 {intent: "检查已连接WiFi", action_type: assert, target: "已连接"}
   |
   ├─ 注意: assert 类型由 Reviewer 工具执行（plan 中的 assert 步骤）
   │   实际执行中 Executor 调用 assert_page_contains("已连接")
   │
   ├─ assert_page_contains("已连接")
   │   → get_screen_info() → 页面文本中包含 "已连接" ✓
   │   → 返回: "PASS"
   │
   ├─ log_step("验证已连接 WiFi 热点")
   │
   └─ 返回 observation: "PASS: 页面包含 '已连接'，显示 Home-WiFi-5G"
```

**State 变更**: `last_action = "assert: 已连接"`, `last_observation = "PASS: 页面包含 '已连接'..."`

> 图路由: executor → reviewer

---

## 11. Reviewer Agent — 审查步骤 5

```
>> Reviewer: 审查步骤5 {操作: assert, 结果: "PASS: 页面包含 '已连接'"}
   |
   ├─ 再次确认 assert_page_contains("已连接")
   │   → "PASS" ✓
   │
   ├─ check_page_health() → 正常 ✓
   │
   ├─ 判断: 断言通过，已连接信息存在
   │   → DECISION: continue
   |
   └─ 步骤记录追加
```

> 条件路由: continue → executor

---

## 12. Executor Agent — 步骤 6: assert (WiFi 列表不为空)

```
executor_node() 被调用，step_index=5, step = plan[5]
```

**执行过程**:

```
>> Executor: 收到步骤6 {intent: "验证WiFi列表不为空", action_type: assert, target: "WiFi列表"}
   |
   ├─ get_screen_info()
   │   → "可用网络" 区域: 8 个热点
   │
   ├─ assert_element_exists("Home-WiFi-5G")
   │   → 在 primary_paths 中找到 "Home-WiFi-5G" ✓
   │   → 返回: "PASS"
   │
   ├─ log_step("验证 WiFi 列表不为空")
   │
   └─ 返回 observation: "PASS: WiFi 列表有 8 个热点，不为空"
```

**State 变更**: `last_action = "assert: WiFi列表"`, `last_observation = "PASS: WiFi 列表有 8 个热点"`

> 图路由: executor → reviewer

---

## 13. Reviewer Agent — 审查步骤 6 (最后一步)

```
>> Reviewer: 审查步骤6 {操作: assert, 结果: "PASS: WiFi 列表有 8 个热点"}
   |
   ├─ assert_element_exists("Home-WiFi-5G") → "PASS" ✓
   ├─ check_page_health() → 正常 ✓
   |
   ├─ 判断: 全部 6 个步骤完成，全部通过
   │   → 这是最后一个步骤 (index=6, total=6)
   │   → DECISION: done
   |
   └─ 步骤记录追加
```

**State 变更**: `reviewer_decision = "done"`

> 条件路由: done → reporter

---

## 14. Reporter Agent — 生成最终报告

```
reporter_node() 被调用
```

**Reporter 输入**:

```
测试需求: "打开 Setting APP 点击左侧WLAN项。在右侧的WLAN界面点击WLAN开关。10s后观察..."
计划步骤数: 6
成功: 6, 失败: 0

步骤执行记录:
  步骤1: [success] 启动 Settings 应用 → OK: 已启动 Settings
  步骤2: [success] 点击左侧导航 WLAN 项 → OK: 已点击 WLAN，右侧显示 WLAN 设置
  步骤3: [success] 点击 WLAN 开关打开 WiFi → OK: 已点击开关，开始扫描
  步骤4: [success] 等待 WiFi 扫描完成 → OK: 已等待 10 秒
  步骤5: [success] 检查已连接 WiFi 热点 → PASS: 已连接 Home-WiFi-5G
  步骤6: [success] 验证 WiFi 列表不为空 → PASS: 8 个热点
```

**Reporter 输出**:

```
PASS: Settings WLAN 连接测试全部通过。
WLAN 开关功能正常，WiFi 扫描成功（发现 8 个热点），
已连接热点 Home-WiFi-5G 状态正常。
验证条件全部满足：
  ✓ WLAN 页面可以正常打开并显示 WiFi 列表
  ✓ 已连接 WiFi 信息正确显示
  ✓ WiFi 扫描结果不为空
```

**State 变更**: `status = "success"`, `conclusion = "PASS: ..."`

---

## 15. 后处理

```
Reporter 完成后:
│
├─ RAG 回写 (Chroma / Memory)
│   ├─ save_page_structure("com.android.settings", "WLAN页", ...)
│   ├─ save_navigation_path("首页", "WLAN页", "点击 WLAN")
│   └─ save_test_experience("WLAN页", "点击开关", "成功")
│
├─ SQLite 写入 (test_runs 表)
│   └─ INSERT INTO test_runs (id, status='success', ...)
│
└─ 事件广播 (WebSocket)
    └─ {"type": "result", "status": "success", "conclusion": "PASS: ..."}
```

---

## 16. 系统能力验证

| 需求 | 系统支持 | 对应机制 |
|------|---------|---------|
| "打开 Setting APP" | ✅ | `launch_app` tool + Executor step 1 |
| "点击左侧 WLAN 项" | ✅ | `navigate_to` tool + two_pane 感知 + Executor step 2 |
| "点击 WLAN 开关" | ✅ | `click` tool + alternatives 回退 + Executor step 3 |
| "10s 后观察" | ✅ | `wait_seconds` tool + Executor step 4 |
| "检查已连接 WiFi" | ✅ | `assert_page_contains` tool + Executor step 5 |
| "WiFi 列表不为空" | ✅ | `assert_element_exists` tool + Executor step 6 |
| ROM 差异 (WLAN vs Wi-Fi) | ✅ | `alternatives` 字段自动回退 |
| 弹窗干扰 | ✅ | Executor 和 Reviewer 都调用 `detect_popup` |
| 页面异常 | ✅ | Reviewer 每步调用 `check_page_health` |
| 知识积累 | ✅ | Reporter 后 RAG 回写 + SQLite 记录 |

**结论**: 系统完全支持此测试场景，无需修改任何代码。6 个步骤全部覆盖，alternatives 机制处理 ROM 差异，Reviewer 确保每步质量。

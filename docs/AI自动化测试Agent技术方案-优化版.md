# AI 自动化测试 Agent 技术方案（优化版）

> 基于原始《AI 自动化测试 Agent 技术方案》整理，并补充工程化落地建议。  
> 目标：从“可运行原型”升级为“稳定、可调试、可扩展的自动化测试控制台”。

---

## 1. 项目定位

本项目旨在构建一个面向 Android 应用的 AI 自动化测试 Agent。用户可以通过自然语言、Web Chat 或 YAML 用例描述测试目标，系统自动完成意图解析、设备操作、页面感知、异常检测、基线对比、测试报告生成和历史知识沉淀。

项目不应只定位为“让大模型点手机”，而应定位为：

> 自然语言驱动的 Android 自动化测试平台，结合 Agent 决策、设备可视化预览、基线回归检测和测试知识库。

---

## 2. 核心目标

### 2.1 必须支持

1. 自然语言解析为结构化测试 Intent。
2. 用户确认或修改 Intent 后再执行。
3. 支持启动 App、点击、输入、滑动、返回、断言等基础自动化操作。
4. 支持 UI 树感知和截图视觉感知。
5. 支持遍历 App 页面并建立页面基线。
6. 支持基于 YAML 用例回放测试。
7. 支持异常检测，包括白屏、黑屏、ANR、崩溃、显示不全、进程丢失。
8. 支持测试报告输出。
9. 支持 Web Chat 交互。
10. 支持设备实时预览和元素查看。

### 2.2 建议支持

1. 执行过程实时推送步骤和截图。
2. 固定测试账号和测试数据管理。
3. 危险操作防护。
4. Baseline 差异可视化。
5. 测试知识自动沉淀到 RAG。
6. Mock Device，用于无真机环境下的单元测试。

---

## 3. 优化后的整体架构

```text
用户入口
├── CLI
├── Web Chat
└── Web 测试控制台
    ├── Chat 对话区
    ├── Intent 确认卡片
    ├── 设备实时预览
    ├── UI 元素树
    └── 执行日志 / 报告

核心服务
├── Intent Parser
├── Chat Runner / Test Runner
├── ReAct Agent
├── Tool Context
├── Device Controller
├── SmartPerceiver
├── Baseline Store
├── Baseline Traverser
├── Anomaly Detector
├── Knowledge Base
├── State Machine
└── Report Builder

存储
├── storage/baselines
├── storage/screenshots
├── storage/results
├── storage/knowledge
└── reports
```

相比原方案，优化重点是增加：

1. **Tool Context**：统一管理 device、perceiver、baseline、detector、logger。
2. **Web 测试控制台**：加入 weditor-like 设备预览能力。
3. **Report Builder**：统一生成结构化报告。
4. **Safety Guard**：限制危险动作。
5. **Execution Event Stream**：执行过程实时推送。

---

## 4. 运行模式

### 4.1 traverse：遍历建基线

输入：

- App 包名
- App 名称
- 最大深度
- 最大页面数
- 遍历范围

输出：

- 页面截图
- UI 树
- 元素快照
- 页面 pHash
- 页面 manifest
- 遍历报告

适用场景：

- 初次接入 App。
- 为后续回放测试建立视觉和结构基线。

### 4.2 replay：回放对比

输入：

- YAML 用例
- 已存在 baseline

过程：

- 按用例步骤执行。
- 关键步骤截图。
- 与 baseline 对比。
- 记录异常和差异。

输出：

- 回放报告
- baseline diff
- 异常列表

### 4.3 run：自然语言执行

输入：

- 用户自然语言任务

过程：

- Intent Parser 解析。
- 用户确认。
- Agent 自主规划并执行。

适用场景：

- 探索性测试。
- 简单临时测试任务。

### 4.4 run_case：执行 YAML 用例

输入：

- YAML 文件路径

适用场景：

- 稳定回归测试。
- CI 或人工触发的标准测试流程。

---

## 5. Intent Parser 优化

原方案中 Intent Parser 已经具备基本解析能力，但建议补充以下字段：

```json
{
  "intent": "traverse|replay|run|run_case",
  "confidence": 0.86,
  "need_confirmation": true,
  "missing_fields": [],
  "app_package": "com.android.settings",
  "app_name": "Settings",
  "task_description": "测试 Settings 的 WiFi 开关",
  "case_file": "",
  "scope": "full|partial",
  "target_pages": [],
  "extra_context": "",
  "traversal_max_depth": 5,
  "traversal_max_pages": 50,
  "safety_level": "normal|strict"
}
```

### 5.1 解析规则建议

1. `run_case` 应优先识别 `.yaml` 或 `.yml` 文件。
2. `traverse` 不能只靠“全部”关键词判断，避免误判“测试全部按钮”。
3. 包名缺失时不要直接执行，应返回 `missing_fields: ["app_package"]`。
4. Intent 置信度低于阈值时，前端必须要求用户确认。
5. 解析结果应保留原始用户输入，便于排查。

---

## 6. Tool Context 设计

原方案中各工具依赖 device、perceiver、detector、baseline_store，但文档没有明确工具如何拿到这些实例。建议增加统一上下文。

```python
class ToolContext:
    def __init__(
        self,
        device,
        perceiver,
        baseline_store,
        anomaly_detector,
        knowledge_base=None,
        report_logger=None,
        safety_guard=None,
    ):
        self.device = device
        self.perceiver = perceiver
        self.baseline_store = baseline_store
        self.anomaly_detector = anomaly_detector
        self.knowledge_base = knowledge_base
        self.report_logger = report_logger
        self.safety_guard = safety_guard
```

工具初始化时应绑定上下文：

```python
def init_tools(context: ToolContext):
    return [
        make_click_tool(context),
        make_type_input_tool(context),
        make_get_screen_info_tool(context),
        make_check_page_health_tool(context),
        make_log_step_tool(context),
    ]
```

这样可以避免多个模块各自持有不同 device 实例，导致状态不一致。

---

## 7. weditor-like 设备预览面板

直接将原版 weditor 嵌入前端并不是最佳方案。更推荐实现一个内置的 weditor-like 预览面板。

### 7.1 前端布局建议

```text
┌──────────────────────┬──────────────────────────────┐
│ Chat / 测试计划       │ 设备预览                      │
│ Intent 确认卡片       │ ┌──────────────────────────┐ │
│ 执行日志              │ │ 实时截图 + 元素框选       │ │
│ 测试结果              │ └──────────────────────────┘ │
│                      │ UI 树 / 元素属性              │
└──────────────────────┴──────────────────────────────┘
```

### 7.2 后端 API

```text
GET  /api/device/snapshot
POST /api/device/click
POST /api/device/input
POST /api/device/key
GET  /api/device/current
```

`/api/device/snapshot` 建议一次返回截图和 UI 树，保证同一时刻的数据一致：

```json
{
  "timestamp": "2026-06-06T09:00:00",
  "screen": {
    "width": 1080,
    "height": 2400,
    "image_base64": "..."
  },
  "activity": ".Settings",
  "package": "com.android.settings",
  "elements": [
    {
      "text": "Wi-Fi",
      "resource_id": "android:id/title",
      "class_name": "android.widget.TextView",
      "clickable": true,
      "bounds": [0, 320, 1080, 440]
    }
  ]
}
```

### 7.3 前端能力

1. 显示实时截图。
2. 按 UI 节点 bounds 绘制元素框。
3. 点击元素框后展示属性。
4. 支持手动点击设备。
5. 支持刷新当前页面。
6. 支持执行过程中只读预览。
7. 支持高亮 Agent 最近操作的目标元素。

### 7.4 注意事项

1. 截图和 UI 树必须同步采集。
2. 前端 overlay 需要处理截图缩放比例。
3. Agent 执行中应禁止手动点击，避免抢设备控制权。
4. 预览接口需要限流，避免高频截图拖慢设备。

---

## 8. Baseline 遍历优化

原方案的 BFS 遍历是合理起点，但需要增加安全和稳定性策略。

### 8.1 遍历边界

1. 只允许在目标包名内遍历。
2. 跳出目标 App 后自动返回或重启 App。
3. 限制最大深度、最大页面数、最大点击次数。
4. 支持指定页面或指定 Tab 遍历。

### 8.2 点击策略

建议增加危险文本黑名单：

```python
DANGEROUS_TEXTS = [
    "删除", "移除", "清空", "提交", "发送", "支付", "购买",
    "退出登录", "注销", "拨打", "确认订单",
    "Delete", "Remove", "Submit", "Send", "Pay", "Buy", "Logout"
]
```

默认遍历时跳过危险动作。若确实需要点击，应由 YAML 用例明确指定，或由用户确认。

### 8.3 页面去重

页面唯一性不应只依赖 activity。建议组合：

```text
page_key = package + activity + selected_tab + ui_tree_hash + screenshot_phash
```

这样可以更好地区分同一 activity 下的不同页面状态。

---

## 9. 异常检测优化

异常检测应坚持“本地优先”，不要依赖 LLM 判断基础异常。

### 9.1 检测类型

必须支持：

1. ANR。
2. 崩溃弹窗。
3. 进程不在前台。
4. 白屏。
5. 黑屏。
6. 单色屏。
7. 元素数量显著下降。
8. pHash 差异过大。
9. 页面关键文本缺失。

### 9.2 阈值配置化

所有阈值应来自配置文件：

```yaml
white_screen_threshold: 0.95
black_screen_threshold: 0.95
solid_color_threshold: 10
incomplete_display_ratio: 0.5
critical_incomplete_ratio: 0.3
phash_distance_low: 15
phash_distance_medium: 20
```

报告中应记录触发异常的原始指标，方便判断误报。

---

## 10. RAG 知识库优化

RAG 适合存储历史测试经验，但不应替代当前页面感知。

### 10.1 存储内容

1. 页面结构摘要。
2. 导航路径。
3. 常见弹窗处理方式。
4. 历史失败原因。
5. 稳定可用的元素定位信息。

### 10.2 使用原则

1. 当前 UI 状态优先。
2. RAG 只作为辅助上下文。
3. RAG 结果需要带时间戳和 App 版本。
4. App 更新后应允许清理或重建知识。

---

## 11. 测试账号与测试数据

建议为自动化测试准备固定测试账号。

目的：

1. 保证测试可重复。
2. 避免污染真实用户数据。
3. 方便准备前置条件。
4. 方便测试后清理。

示例配置：

```yaml
test_account:
  username: "auto_test_user"
  password_env: "AUTO_TEST_PASSWORD"
  reset_before_run: true
```

注意：密码不应写入代码或 YAML，应通过环境变量或密钥管理系统读取。

---

## 12. 安全控制

自动操作真实设备存在副作用风险，必须加入安全控制。

### 12.1 安全等级

```text
strict：默认跳过所有危险操作
normal：危险操作需要用户确认
manual：仅执行用户明确指定的步骤
```

### 12.2 危险动作

1. 删除数据。
2. 提交表单。
3. 发送消息。
4. 支付购买。
5. 拨打电话。
6. 退出登录。
7. 修改账号信息。

Agent 在调用工具前应经过 `SafetyGuard` 检查。

---

## 13. 实时执行事件流

原方案 WebSocket 协议有 `step` 消息，但实际执行是同步阻塞。建议引入事件流。

### 13.1 事件类型

```json
{"type": "status", "content": "开始执行"}
{"type": "step_start", "step": 1, "content": "点击 Wi-Fi"}
{"type": "snapshot", "content": {"image": "...", "elements": []}}
{"type": "anomaly", "content": {"severity": "high", "message": "白屏"}}
{"type": "step_end", "step": 1, "status": "success"}
{"type": "result", "content": {"status": "success"}}
```

### 13.2 实现建议

1. `log_step` 工具写入 report logger。
2. report logger 同时向 WebSocket 推送事件。
3. Agent 每步执行后刷新 snapshot。
4. 前端实时更新截图、日志和结果。

---

## 14. 报告设计

报告不应只有最终结论，应包含完整执行轨迹。

```json
{
  "name": "反馈提交流程测试",
  "mode": "run_case",
  "app_package": "com.lenovo.service",
  "status": "success",
  "conclusion": "PASS",
  "duration_seconds": 42.5,
  "steps": [
    {
      "index": 1,
      "intent": "点击反馈",
      "action": "click",
      "target": "反馈",
      "status": "success",
      "screenshot": "storage/screenshots/step_001.png",
      "anomalies": []
    }
  ],
  "baseline_diffs": [],
  "anomalies": [],
  "created_at": "2026-06-06T09:00:00"
}
```

---

## 15. 推荐 MVP 范围

第一阶段不要一次性实现所有能力，建议先做窄场景闭环。

### 15.1 第一阶段

目标：跑通核心链路。

范围：

1. 固定 1 到 2 个 App。
2. 支持 `run_case`。
3. 支持基础设备工具。
4. 支持 UI 树感知。
5. 支持测试报告。
6. 支持 Web Chat 的 Intent 确认。
7. 支持设备截图预览。

### 15.2 第二阶段

目标：增强稳定性。

范围：

1. 支持 traverse 建基线。
2. 支持 replay 对比。
3. 支持异常检测和恢复。
4. 支持 UI 元素 overlay。
5. 支持执行事件实时推送。

### 15.3 第三阶段

目标：增强智能化。

范围：

1. 支持 Vision 感知。
2. 支持 RAG 知识库。
3. 支持更复杂自然语言测试。
4. 支持 baseline diff 可视化。
5. 支持更多 App 和测试账号管理。

---

## 16. 建议目录结构

```text
ai-test-agent/
├── main.py
├── config.py
├── requirements.txt
├── core/
│   ├── agent.py
│   ├── chat_runner.py
│   ├── intent_parser.py
│   ├── tool_context.py
│   ├── tools.py
│   ├── device_controller.py
│   ├── smart_perceiver.py
│   ├── baseline_store.py
│   ├── baseline_traverser.py
│   ├── anomaly_detector.py
│   ├── state_machine.py
│   ├── knowledge_base.py
│   ├── safety_guard.py
│   └── report_builder.py
├── api/
│   ├── server.py
│   ├── device_routes.py
│   └── websocket_manager.py
├── frontend/
│   ├── index.html
│   ├── app.js
│   └── styles.css
├── prompts/
│   ├── system_prompt.txt
│   └── vision_prompt.txt
├── test_cases/
├── tests/
├── storage/
│   ├── baselines/
│   ├── screenshots/
│   ├── results/
│   └── knowledge/
└── reports/
```

---

## 17. 风险与注意事项

1. 真机自动化不稳定，需要重试、恢复和超时控制。
2. LLM 自主执行不可完全信任，关键动作必须受工具和安全策略约束。
3. Vision 成本较高，应作为 UI 树失败后的补充。
4. Baseline 容易受主题、分辨率、语言、数据状态影响，需要区分环境。
5. Web 实时预览会增加设备压力，需要限流。
6. 测试账号、Token、密码等敏感信息不能写入仓库。

---

## 18. 总结

原方案的主链路完整，适合作为 AI 自动化测试 Agent 的原型基础。优化后的落地重点应放在：

1. 统一工具上下文。
2. 增加设备可视化预览。
3. 强化安全控制。
4. 打通执行事件流。
5. 完善报告和异常检测。
6. 用窄场景 MVP 先验证稳定性。

建议优先实现 `run_case + Web Chat + 设备预览 + 报告` 的最小闭环，然后再扩展遍历、基线、RAG 和 Vision 能力。

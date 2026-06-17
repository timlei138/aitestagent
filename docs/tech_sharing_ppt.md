---
marp: true
theme: default
paginate: true
size: 16:9
style: |
  section { font-size: 22px; }
  section.title { text-align: center; }
  h1 { color: #1a1a2e; border-bottom: 3px solid #409eff; padding-bottom: 8px; }
  h2 { color: #409eff; }
  code { background: #f0f0f0; padding: 2px 6px; border-radius: 4px; }
  .two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
  .highlight { background: #ecf5ff; border-left: 4px solid #409eff; padding: 12px 16px; border-radius: 0 8px 8px 0; }
  .small { font-size: 16px; }
---

<!-- _class: title -->

# 🧠 AI Agent 驱动 Android 自动化测试
## 基于 LangChain + LangGraph 的技术实践

**技术分享会**

2026-06-17

---

# 📋 项目概览

## 痛点 & 方案

<div class="two-col">
<div>

### 传统方式
- ✍️ 手工编写测试脚本
- 🔧 每个 App UI 变化都要改代码
- ❌ 元素定位脆弱 (XPath/ID)
- ⏰ 维护成本高

### AI Agent 方式
- 🗣️ 自然语言描述测试目标
- 🤖 LLM 自主理解页面 + 决策操作
- 🎯 语义匹配元素（不依赖 ID）
- 🔄 UI 变化自适应

</div>
<div class="highlight">

### 核心能力
- **自然语言输入**： "把时区设置为英国"
- **LLM 规划**： 自动拆解测试步骤
- **Agent 执行**： 理解 UI 树 → 决策 → 操作
- **自动验证**： 检查执行结果
- **人机协作**： 关键节点暂停确认

</div>
</div>

---

# 🏗️ 技术架构 — LangGraph 状态图

## 核心技术栈

| 层级 | 技术 | 作用 |
|------|------|------|
| 🧠 LLM | DeepSeek V4 Pro + 智谱 GLM-4.6V | 推理决策 + 视觉分析 |
| 🔗 编排 | **LangGraph StateGraph** | 多节点状态流转 |
| 🔧 工具 | **LangChain @tool** 17个 | Function Calling |
| 📱 设备 | **uiautomator2** | Android 操控 |
| 💾 数据 | **ChromaDB** + SQLite | RAG + 持久化 |
| 🌐 API | **FastAPI** + WebSocket | 前后端通信 |
| 🖥️ 前端 | **Vue 3** + Element Plus | SPA 界面 |

## 架构图 (文字版核心流程)

```
用户指令 → FastAPI → Orchestrator
                       ↓
            ╔═══════════════════════════╗
            ║   LangGraph StateGraph    ║
            ║                           ║
            ║  Planner ──→ PlanReview   ║
            ║    (LLM)      (interrupt) ║
            ║                ↓          ║
            ║              Agent        ║
            ║        (Tool-calling LLM) ║
            ║          ↓       ↓        ║
            ║     Tools ←→ Device       ║
            ║          ↓                ║
            ║       Reporter ──→ SQLite ║
            ╚═══════════════════════════╝
                       ↓
              WebSocket → 前端实时展示
```

---

# 🔑 LangGraph 核心概念实践

## 1. StateGraph — 状态机编排

```python
# agents/graph.py
def build_graph(config):
    g = StateGraph(TestState)
    g.add_node("planner", planner_node)     # LLM 规划
    g.add_node("plan_review", plan_review_node)  # 人机交互
    g.add_node("agent", agent_node)         # Tool-calling Agent
    g.add_node("reporter", reporter_node)   # 结果统计
    g.add_edge(START, "planner")
    g.add_edge("planner", "plan_review")
    g.add_conditional_edges("plan_review", route_after_plan_review, {...})
    g.add_conditional_edges("agent", route_after_agent, {...})
    g.add_edge("reporter", END)
    return g.compile(checkpointer=MemorySaver())
```

## 2. interrupt() — 人机协作暂停

Planner 生成计划后，调用 `interrupt()` 暂停执行 → 前端弹窗 → 用户确认/编辑 → `Command(resume=...)` 恢复。

## 3. Tool-calling Agent 子图

Agent Node 内部又是一个 `StateGraph`：LLM ↔ ToolNode 循环，`max_turns=8` 限制步数。

## 4. Checkpointer — 断点续执

`MemorySaver` 保存每一步的状态，支持 interrupt → resume 无缝恢复。

---

# 🛠️ LangChain 工具链实践

## 17 个 @tool 工具

<div class="two-col">
<div>

### 感知类
- `get_screen_info` — UI 树解析
- `query_app_knowledge` — RAG 检索
- `query_element_identity` — 元素身份

### 操作类
- `click` — **语义点击** (核心)
- `scroll_find_and_click` — 滚动查找
- `launch_app` / `type_input` / `press_key`

</div>
<div>

### 验证类
- `assert_page_contains`
- `assert_element_exists`
- `check_page_health`
- `detect_popup`
- `recover_from_anomaly`

### 核心设计
- **@tool 装饰器** 包装 Python 函数
- **bind_tools()** 绑定到 LLM
- **Function Calling** 自动解析参数
- **语义匹配**：通过 UI 树属性 (label, role, resource_id) 智能定位元素

</div>
</div>

## Agent Prompt 设计原则
- 🎯 **结构化决策流程**：先检查 DONE，再行动，行动后验证
- 🚫 **禁止循环清单**：具体行为描述
- ✅ **正反示例**：DONE 正确流程 vs 错误循环

---

# 🎬 Demo & Q&A

## 执行流程演示

```
1. 用户输入: "打开Settings, 通用设置→日期和时间→时区→地区→英国"

2. Planner (LLM)
   → {"goal": "验证将系统时区设置为英国...", "verification": ["时区显示为英国"]}

3. PlanReview (人机确认)
   → 前端弹窗，用户可编辑目标、页面、验证条件

4. Agent Loop (自主执行)
   → perceive: 获取 UI 树 (169 个元素, 24 clickable)
   → click("通用设置") → click("日期和时间") → click("时区")
   → scroll_find_and_click("英国")
   → get_screen_info → 验证: 时间从 18:28 → 11:29 ✅
   → DONE: 英国时区设置成功

5. Reporter → SQLite → 前端报告 (3步骤, 100%通过)
```

## 关键指标
- ⚡ 平均执行时间: **~80s** / 用例
- 🎯 元素定位准确率: **语义匹配 95%+**
- 🔄 最大迭代: 12 轮 Agent + 每轮 8 次工具调用
- 💾 知识积累: RAG 自动提取测试经验

---

<!-- _class: title -->

# 🙋 Q & A

## 谢谢！

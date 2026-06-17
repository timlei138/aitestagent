# AI Agent 自动化测试平台 — 架构文档

> 基于 **LangChain + LangGraph** 的 Android UI 自动化测试 Agent

---

## 1. 系统架构总览

```mermaid
graph TB
    subgraph Frontend["🖥️ 前端 (Vue 3 + Element Plus)"]
        WS["WebSocket 客户端"]
        UI["工作台 / 报告中心 / 知识库"]
    end

    subgraph API["🌐 API 层 (FastAPI)"]
        Server["api/server.py<br/>REST + WebSocket"]
        WSM["api/websocket_manager.py<br/>连接管理 + 广播"]
        DR["api/device_routes.py<br/>设备截图/按键"]
        AR["api/apps_routes.py<br/>APP 管理"]
        KR["api/knowledge_routes.py<br/>知识库 CRUD"]
    end

    subgraph Orchestrator["🎯 编排层 (LangGraph)"]
        Graph["agents/graph.py<br/>StateGraph 状态图"]
        Planner["Planner Node<br/>LLM 规划测试目标"]
        PlanReview["PlanReview Node<br/>interrupt() 人机确认"]
        Agent["Agent Node<br/>Tool-calling Agent<br/>max_turns=8"]
        Reporter["Reporter Node<br/>结果统计 + 持久化"]
        State["agents/state.py<br/>TestState 状态定义"]
    end

    subgraph Tools["🔧 工具层 (LangChain Tools)"]
        TClick["click / scroll_find_and_click"]
        TScreen["get_screen_info"]
        TNav["navigate_to / launch_app"]
        TInput["type_input / press_key / swipe"]
        TVerify["assert_page_contains / check_page_health"]
    end

    subgraph Device["📱 设备层"]
        Ctrl["device/controller.py<br/>uiautomator2 封装"]
        Percv["device/perceiver.py<br/>SmartPerceiver<br/>UI树 / Vision 双模式"]
    end

    subgraph Data["💾 数据层"]
        SQLite["data/relational.py<br/>SqliteBackend<br/>测试记录 / 元素身份"]
        Vector["data/vector_store.py<br/>ChromaDB / MemoryBackend<br/>语义检索"]
        KB["data/knowledge.py<br/>KnowledgeBase<br/>知识提取 / RAG"]
    end

    subgraph LLM["🤖 LLM 层"]
        DeepSeek["DeepSeek v4 Pro<br/>(OpenAI 兼容 API)"]
        Zhipu["智谱 GLM-4.6V-Flash<br/>(Vision 模型)"]
        BGE["BAAI/bge-large-zh-v1.5<br/>(Embedding 本地)"]
    end

    Frontend -->|WebSocket| Server
    Frontend -->|REST API| Server
    Server --> WSM
    Server --> Orchestrator
    Server --> Device
    Orchestrator --> Tools
    Orchestrator --> Data
    Tools --> Device
    Tools --> Data
    Device --> Ctrl
    Device --> Percv
    Percv -->|截图分析| Zhipu
    Graph -->|LLM 调用| DeepSeek
    Data --> Vector
    Vector -->|Embedding| BGE
```

---

## 2. LangGraph 状态图 — 核心执行流程

```mermaid
stateDiagram-v2
    [*] --> Planner: START

    Planner --> PlanReview: goal_description
    PlanReview --> Agent: 用户确认 (resume)
    PlanReview --> Reporter: 用户取消

    Agent --> Agent: iteration < 12<br/>status = continue
    Agent --> Reporter: status = success/fail<br/>OR iteration >= 12

    Reporter --> [*]: END

    note right of Planner
        LLM 调用: DeepSeek
        输出: goal, target_pages,
        verification, hints
    end note

    note right of PlanReview
        LangGraph interrupt()
        暂停等待前端用户确认
        支持编辑目标后恢复
    end note

    note right of Agent
        Tool-calling Agent
        max_turns = 8 次工具调用
        工具: click, get_screen_info,
        scroll_find_and_click, 等 17 个
    end note
```

### 2.1 Agent Node 内部流程

```mermaid
sequenceDiagram
    participant AN as Agent Node
    participant P as SmartPerceiver
    participant LLM as DeepSeek LLM
    participant T as ToolNode
    participant D as Device

    AN->>P: perceive() 获取页面 UI 树
    P-->>AN: PageInfo (元素列表 + 状态栏时间)

    loop max_turns=8
        AN->>LLM: System Prompt + Goal + Page + History
        LLM-->>AN: ToolCall 或 Text

        alt LLM 返回 ToolCall
            AN->>T: 执行工具
            T->>D: uiautomator2 操作
            D-->>T: 操作结果
            T-->>AN: ToolResult
            AN->>AN: _turn_count++
            note right of AN: _limit() 检查:<br/>turn >= 8 ? END : continue
        else LLM 返回 Text (DONE/ABORT)
            AN-->>AN: 解析 DONE:/ABORT:
        end
    end

    alt 关键操作后自动注入
        AN->>P: perceive() 重新获取页面
        AN->>AN: 注入 [关键操作后页面状态]
    end
```

---

## 3. LangGraph 关键概念在本项目中的应用

| LangGraph 概念 | 本项目对应 | 文件位置 |
|---------------|-----------|---------|
| **StateGraph** | 测试执行状态机，定义 Planner → PlanReview → Agent → Reporter 节点 | [agents/graph.py:340](agents/graph.py#L340) |
| **State (TypedDict)** | `TestState`: user_request, goal_description, step_history, messages, status | [agents/state.py](agents/state.py) |
| **Command** | 节点返回值，支持 `goto` 路由和 `update` 状态更新 | [agents/graph.py:287](agents/graph.py#L287) |
| **interrupt()** | PlanReview 节点暂停等待用户确认计划 | [agents/graph.py:294](agents/graph.py#L294) |
| **checkpointer (MemorySaver)** | 保存图状态，支持 interrupt 后 resume | [agents/graph.py:351](agents/graph.py#L351) |
| **ToolNode** | Agent 子图中执行工具调用的节点 | [agents/graph.py:96](agents/graph.py#L96) |
| **tools_condition** | 判断 LLM 输出是工具调用还是文本，决定 next node | [agents/graph.py:92](agents/graph.py#L92) |
| **Subgraph (Agent)** | Agent 内部使用独立 StateGraph 管理 tool-calling 循环 | [agents/graph.py:95-99](agents/graph.py#L95-L99) |
| **Command(resume=...)** | 从 interrupt 恢复执行 | [agents/orchestrator.py:192](agents/orchestrator.py#L192) |

---

## 4. LangChain 关键概念在本项目中的应用

| LangChain 概念 | 本项目对应 | 文件位置 |
|---------------|-----------|---------|
| **ChatOpenAI** | 统一的 LLM 调用接口（兼容 DeepSeek） | [agents/graph.py:84](agents/graph.py#L84) |
| **bind_tools()** | 将 17 个 @tool 函数绑定到 LLM，支持 Function Calling | [agents/graph.py:84](agents/graph.py#L84) |
| **SystemMessage / HumanMessage / AIMessage** | 多轮对话消息管理 | [agents/graph.py:214-216](agents/graph.py#L214-L216) |
| **@tool 装饰器** | 将 Python 函数包装为 LLM 可调用的工具 | [tools/__init__.py](tools/__init__.py) |
| **RunnableConfig** | 传递 thread_id / test_config 等上下文 | [agents/graph.py:143](agents/graph.py#L143) |
| **ChromaDB** | 向量存储，RAG 知识检索 | [data/vector_store.py](data/vector_store.py) |
| **HuggingFace Embeddings** | 本地 Embedding (bge-large-zh-v1.5) | [data/vector_store.py](data/vector_store.py) |
| **PromptTemplate** | Planner 的结构化 Prompt | [agents/graph.py:47](agents/graph.py#L47) |

---

## 5. 工具层 (LangChain Tools) — 17 个工具

```mermaid
graph LR
    subgraph Perception["感知类"]
        T1["get_screen_info<br/>获取页面 UI 树"]
        T2["query_app_knowledge<br/>RAG 知识检索"]
        T3["query_element_identity<br/>元素身份查询"]
    end

    subgraph Action["操作类"]
        T4["click<br/>语义点击"]
        T5["scroll_find_and_click<br/>滚动查找点击"]
        T6["scroll_panel<br/>面板滚动"]
        T7["navigate_to<br/>页面导航"]
        T8["launch_app<br/>启动应用"]
        T9["type_input<br/>文本输入"]
        T10["press_key<br/>按键"]
        T11["swipe<br/>滑动"]
        T12["wait_seconds<br/>等待"]
    end

    subgraph Verify["验证类"]
        T13["check_page_health<br/>页面健康检查"]
        T14["assert_page_contains<br/>页面包含断言"]
        T15["assert_element_exists<br/>元素存在断言"]
        T16["detect_popup<br/>弹窗检测"]
        T17["recover_from_anomaly<br/>异常恢复"]
    end
```

---

## 6. 数据流 — 一次完整测试的生命周期

```mermaid
sequenceDiagram
    participant U as 用户 (前端)
    participant WS as WebSocket
    participant O as Orchestrator
    participant G as LangGraph
    participant D as Device
    participant DB as SQLite

    U->>WS: {type: "run", message: "设置时区为英国"}
    WS->>O: start(user_request, app_package)

    O->>G: graph.invoke(initial_state)
    G->>G: Planner Node → LLM 生成目标
    G-->>O: GraphInterrupt (plan_review)

    O->>WS: broadcast {type: "plan_review", plan: {...}}
    WS->>U: 弹窗确认目标

    U->>WS: {type: "human_decision", decision: "confirm"}
    WS->>O: resume(thread_id, decision)

    O->>G: graph.invoke(Command(resume=...))
    loop Agent Loop (max 12 iterations)
        G->>D: perceive() 获取 UI 树
        G->>G: Agent Node → LLM 决策
        G->>D: click / scroll / type
        G->>G: 检查 DONE / ABORT
    end

    G->>G: Reporter Node → 统计结果
    G->>DB: record_test_run(steps, status)
    G-->>O: final_state

    O->>WS: broadcast {type: "result", status: "success"}
    WS->>U: 显示结果 + 刷新报告
```

---

## 7. 前端架构

```mermaid
graph TB
    subgraph Vue["Vue 3 SPA"]
        WS_Hook["useWebSocket()<br/>事件驱动"]
        Views["工作台 / 报告中心<br/>APP管理 / 知识库"]
        Dialogs["PlanReview 确认<br/>人工确认 / 元素确认<br/>用例编辑器"]
        Float["设备投屏悬浮窗<br/>Canvas 元素覆盖"]
    end

    subgraph Events["WebSocket 事件"]
        E1["plan_review → 弹窗确认"]
        E2["stream_token → 流式输出"]
        E3["tool_start/end → 工具状态"]
        E4["result → 执行结果"]
        E5["snapshot → 截图更新"]
        E6["need_human_approval → 人工确认"]
    end

    WS_Hook --> Events
    Events --> Views
    Events --> Dialogs
    Events --> Float
```

---

## 8. 目录结构

```
AiAgentTest/
├── main.py                   # 命令行入口
├── config.py                 # TestConfig 数据类 + YAML 加载
├── config.yaml               # 配置文件
├── agents/
│   ├── graph.py              # ⭐ LangGraph 状态图核心
│   ├── orchestrator.py       # 编排器 (start/resume/stream)
│   ├── state.py              # TestState 定义
│   └── prompts/
│       ├── agent.txt         # Agent System Prompt
│       └── planner.txt       # Planner System Prompt
├── tools/
│   ├── __init__.py           # 17 个 @tool 工具 + ToolContext
│   └── context.py            # ToolContext 数据类
├── api/
│   ├── server.py             # FastAPI + WebSocket
│   ├── websocket_manager.py  # 连接池 + 广播
│   ├── device_routes.py      # 设备 REST API
│   ├── apps_routes.py        # APP 管理 API
│   └── knowledge_routes.py   # 知识库 API
├── device/
│   ├── controller.py         # uiautomator2 封装
│   └── perceiver.py          # SmartPerceiver (UI树/Vision)
├── data/
│   ├── __init__.py           # 工厂函数
│   ├── relational.py         # SQLite (测试记录/元素身份)
│   ├── vector_store.py       # ChromaDB / MemoryBackend
│   └── knowledge.py          # RAG 知识管理
├── llm/
│   └── clients.py            # LLM 客户端 (重试/容错)
├── frontend/spa/src/
│   ├── App.vue               # 主组件 (~1470 行)
│   └── App.css               # 样式
├── storage/                  # 运行时数据
└── docs/                     # 文档
```

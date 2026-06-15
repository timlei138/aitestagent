# AI 自动化测试 Agent — PPT 模板

> 可直接用于生成 PPT 的结构化内容。每个 `---` 分隔一页幻灯片。

---

## 封面

**标题**: AI 驱动的 Android 自动化测试 Agent  
**副标题**: 多 Agent 协作 · LangGraph 编排 · RAG 知识积累  
**技术标签**: `LangChain` `LangGraph` `Chroma` `Android` `Multi-Agent`

---

## 目录

1. 项目背景与目标
2. 整体架构
3. 核心技术栈
4. 多 Agent 协作流程
5. 规划 Agent (Planner)
6. 执行 Agent (Executor)
7. 审查 Agent (Reviewer)
8. 人工介入机制
9. 数据层设计
10. 流式实时反馈
11. 代码包结构
12. 总结

---

## Slide 1: 项目背景

**痛点**:
- Android 手工回归测试耗时、易遗漏
- 传统脚本维护成本高，UI 变化即失效
- 测试结果缺乏结构化沉淀

**目标**:
- 自然语言 → 自动执行 → 智能审查 → 报告输出
- LLM 驱动，无需手写测试脚本
- 知识持续积累，越用越智能

**一句话**: 让 AI Agent 代替人完成 Android 自动化测试

---

## Slide 2: 整体架构 (分层视图)

```
┌─────────────────────────────────────────┐
│          用户交互层 (User Layer)         │
│   Web UI (Vue 3)  │  CLI  │  API       │
├─────────────────────────────────────────┤
│          编排层 (Orchestration)          │
│   TestOrchestrator ← agents/orchestrator│
├─────────────────────────────────────────┤
│        Agent 协作层 (Agent Layer)        │
│  ┌──────┐ ┌──────┐ ┌──────┐ ┌───────┐  │
│  │Plan  │→│Exec  │→│Review│→│Report │  │
│  │  2T  │ │ 12T  │ │  9T  │ │  0T   │  │
│  └──────┘ └──────┘ └──────┘ └───────┘  │
│        ↑ LangGraph StateGraph ↑         │
├─────────────────────────────────────────┤
│          工具层 (Tool Layer)             │
│   20 个 LangChain Tool — tools/__init__ │
├─────────────────────────────────────────┤
│          基础服务 (Infrastructure)       │
│  ┌──────┐ ┌──────────┐ ┌────────────┐  │
│  │Device│ │Perceiver │ │ Data Layer │  │
│  │ADB   │ │UI树+Vision│ │Chroma+SQLite│ │
│  └──────┘ └──────────┘ └────────────┘  │
└─────────────────────────────────────────┘
```

**代码映射**: `agents/` `tools/` `device/` `data/` `llm/` `api/`

---

## Slide 3: 核心技术栈

| 层次 | 技术 | 代码位置 |
|------|------|---------|
| Agent 编排 | LangGraph StateGraph | `agents/graph.py` |
| LLM 推理 | ChatOpenAI / ZhipuAI | `llm/clients.py` |
| 工具系统 | LangChain @tool + ToolNode | `tools/__init__.py` |
| 提示词模板 | ChatPromptTemplate + SystemMessage | `agents/graph.py` |
| 结构化输出 | with_structured_output (Pydantic) | `agents/state.py` |
| 向量数据库 | Chroma | `data/vector_store.py` |
| 关系型存储 | SQLite | `data/relational.py` |
| 流式输出 | astream_events (SSE) | `agents/orchestrator.py` |
| 断点恢复 | MemorySaver + interrupt | `agents/graph.py` |
| 设备控制 | uiautomator2 (ADB) | `device/controller.py` |
| 多模态感知 | UI 树 + Vision LLM | `device/perceiver.py` |
| Web 服务 | FastAPI + WebSocket | `api/server.py` |

---

## Slide 4: 多 Agent 协作流程

```
用户: "检查 Settings 的 Wi-Fi 开关是否正常"
                    │
                    ▼
┌──────────────────────────────────────────┐
│            Planner Agent                 │
│  输入: 用户需求 + RAG 历史知识             │
│  工具: get_screen_info, query_app_knowledge│
│  输出: 结构化测试计划 (YAML)              │
│  代码: agents/graph.py → planner_node    │
└──────────────┬───────────────────────────┘
               │ test_plan (步骤列表)
               ▼
┌──────────────────────────────────────────┐
│           Executor Agent                 │
│  输入: 当前步骤 + 历史摘要                │
│  工具: click, type_input, launch_app...  │
│  输出: 操作结果观察                       │
│  代码: agents/graph.py → executor_node   │
└──────────────┬───────────────────────────┘
               │ last_observation
               ▼
┌──────────────────────────────────────────┐
│           Reviewer Agent                 │
│  输入: 执行结果 + 页面状态                │
│  工具: check_page_health, assert_*...     │
│  输出: 决策 (continue/retry/skip/...)     │
│  代码: agents/graph.py → reviewer_node   │
└──────────────┬───────────────────────────┘
               │
    ┌──────────┼──────────┐
    ▼          ▼          ▼
 continue   ask_human   done/abort
    │          │          │
    │     ┌────▼────┐     │
    │     │ Human   │     │
    │     │Approval │     │
    │     │interrupt│     │
    │     └────┬────┘     │
    │          │          │
    └──────────┴──────────┘
               │
               ▼
┌──────────────────────────────────────────┐
│           Reporter Agent                 │
│  汇总所有步骤 → PASS/FAIL 结论            │
│  + RAG 回写 + SQLite 记录                │
│  代码: agents/graph.py → reporter_node   │
└──────────────────────────────────────────┘
```

---

## Slide 5: Planner Agent — 规划专家

**职责**: 将自然语言需求转化为结构化测试计划

**流程**:
```
用户输入
  │
  ├─→ get_screen_info()     了解当前 App 页面布局
  ├─→ query_app_knowledge()  查询 RAG 历史知识
  │
  └─→ LLM 推理 (with_structured_output)
       │
       ▼
     TestPlanOutput (Pydantic Model)
     ├── name: "Wi-Fi 开关测试"
     ├── steps: [{index, intent, action_type, target, alternatives}]
     └── verification: ["Wi-Fi 页面可正常打开"]
       │
       ▼
     自动持久化 → test_cases/*.yaml
```

**关键设计**:
- OpenAI: `ChatOpenAI.with_structured_output(TestPlanOutput)` → 类型安全
- Zhipu: ToolNode 驱动 + 正则解析 → 降级兼容
- `alternatives` 字段处理多版本差异 (如 "Wi-Fi" vs "WLAN")

**代码**: `agents/graph.py` planner_node, `agents/state.py` TestPlanOutput

---

## Slide 6: Executor Agent — 执行专家

**职责**: 严格执行单步操作，不越界

**流程**:
```
当前步骤: {intent: "点击 Wi-Fi", action_type: click, target: "Wi-Fi"}
  │
  ├─→ detect_popup()      弹窗检测
  │     └─→ dismiss_popup()  有弹窗先关闭
  │
  ├─→ click("Wi-Fi")      主操作
  │     ├─ 成功 → 返回 "已点击: Wi-Fi"
  │     └─ 失败 → 尝试 alternatives: ["WLAN"]
  │
  ├─→ log_step()           记录步骤
  └─→ 返回: "OK: 点击成功"
```

**关键设计**:
- LLM + ToolNode 子图驱动工具调用 (3 轮上限)
- 上下文注入: 最近完成的步骤摘要 + 当前步骤
- 危险操作自动标记 `NEEDS_HUMAN:`

**代码**: `agents/graph.py` executor_node, `tools/__init__.py`

---

## Slide 7: Reviewer Agent — 审查专家

**职责**: 判断执行结果，做出下一步决策

**决策矩阵**:

| 情况 | 决策 | 下一步 |
|------|------|--------|
| 操作成功, 页面健康 | `continue` | → Executor (下一步) |
| 操作失败, 页面健康 | `retry` (≤2) | → Executor (重试) |
| 重试均失败, 非关键 | `skip` | → Executor (跳过) |
| ANR/崩溃不可恢复 | `abort` | → Reporter (终止) |
| 命中危险关键词 | `ask_human` | → interrupt (暂停) |
| 所有步骤完成 | `done` | → Reporter (出报告) |

**使用的工具**:
- `check_page_health` — 白屏/黑屏/ANR/崩溃/进程丢失检测
- `recover_from_anomaly` — 关弹窗 → 按返回 → 重启 App
- `get_detailed_screen` — Vision LLM 详细页面分析
- `assert_page_contains` / `assert_element_exists` — 断言验证

**代码**: `agents/graph.py` reviewer_node, `tools/__init__.py`

---

## Slide 8: 人工介入机制 (Human-in-the-Loop)

**触发条件**:
- 危险操作: 删除、支付、重置、注销等
- 连续失败: 同一关键步骤重试 2 次仍失败
- 页面失控: 跳转到完全未知的应用

**流程**:
```
Reviewer: decision=ask_human
  │
  ▼
LangGraph interrupt() 暂停图执行
  │
  ▼
WebSocket → 前端弹窗
  "是否继续执行危险操作「删除xxx」?"
  [允许执行] [跳过此步] [终止测试]
  │
  ▼
用户点击 → API POST /api/human_decision
  │
  ▼
Command(resume="允许执行")
  │
  ▼
图从断点恢复 → Executor
```

**代码**: `agents/graph.py` human_approval_node, `agents/orchestrator.py` resume(), `api/server.py`

---

## Slide 9: 数据层 — 双存储架构

```
┌─────────────────────────────────────────────┐
│              KnowledgeBase                   │
│         (RAG 知识库业务逻辑)                  │
│          data/knowledge.py                   │
└────────┬───────────────────┬────────────────┘
         │                   │
  ┌──────▼──────┐    ┌───────▼────────┐
  │Vector Store │    │  Relational DB │
  │   (ABC)     │    │     (ABC)      │
  └──────┬──────┘    └───────┬────────┘
         │                   │
  ┌──────▼──────┐    ┌───────▼────────┐
  │  Chroma     │    │   SQLite       │
  │ (有API Key) │    │ storage/       │
  │ 或 Memory   │    │ test_history   │
  │ (降级模式)  │    │ .db            │
  └─────────────┘    └────────────────┘
```

**向量存储 (RAG)**:
| 知识类型 | 内容 |
|---------|------|
| page_structure | 页面 UI 元素列表 |
| navigation_path | 操作 → 页面跳转 |
| test_experience | 成功/失败经验 |

**关系型存储 (SQLite)**:
| 表 | 内容 |
|----|------|
| test_runs | 测试执行记录 |
| human_decisions | 人工决策审计 |
| test_plans | 测试计划元数据 |

**设计原则**: 接口抽象 (ABC)，后期可切换 Pinecone / PostgreSQL

**代码**: `data/vector_store.py` `data/relational.py` `data/knowledge.py`

---

## Slide 10: 流式实时反馈

**技术**: LangGraph `astream_events` + Server-Sent Events (SSE)

```
时间线:
  │
  ├─ [stream_token] "正在分析需求..."        ← LLM token
  ├─ [plan_ready]   {steps: 3}             ← Planner 完成
  ├─ [step_start]   "步骤1: 启动 Settings"
  ├─ [tool_start]   {name: "launch_app"}
  ├─ [tool_end]     {name: "launch_app"}
  ├─ [stream_token] "应用已启动"
  ├─ [step_end]     "continue"
  ├─ [tool_start]   {name: "click"}
  ├─ [stream_token] "点击 Wi-Fi 入口"
  ├─ [tool_end]     {name: "click"}
  │   ...
  ├─ [stream_token] "PASS: 所有步骤通过"
  └─ [result]       {status: "success"}
```

**效果**: 用户实时看到 Agent 的每一步思考和操作，不再是黑盒等待

**代码**: `agents/orchestrator.py` start_stream(), `api/server.py` /api/run/stream

---

## Slide 11: 代码包结构

```
AiAgentTest/
│
├── agents/           ← Agent 编排层
│   ├── graph.py          LangGraph 5节点图 + ToolNode子图
│   ├── orchestrator.py   对外入口 (同步+流式+恢复)
│   ├── state.py          TestState + Pydantic模型
│   ├── report_builder.py 报告生成 + 事件广播
│   └── prompts/          Planner/Executor/Reviewer 提示词
│
├── tools/            ← 工具层
│   ├── __init__.py       20个LangChain Tool (3组)
│   └── context.py        ToolContext 依赖注入
│
├── device/           ← 设备抽象层
│   ├── controller.py     ADB 设备控制 (uiautomator2)
│   └── perceiver.py      UI树解析 + Vision LLM多模态
│
├── data/             ← 数据层
│   ├── __init__.py       工厂函数
│   ├── vector_store.py   VectorStoreBackend + Chroma/Memory
│   ├── relational.py     RelationalBackend + SQLite
│   └── knowledge.py      RAG知识库业务逻辑
│
├── llm/              ← LLM抽象层
│   ├── clients.py        LLMClient/VLMClient + OpenAI/Zhipu
│   └── safety.py         危险操作检测
│
├── api/              ← Web接口层
│   ├── server.py         FastAPI + WebSocket + SSE
│   ├── device_routes.py  设备控制 REST API
│   └── websocket_manager.py
│
├── main.py           ← CLI 入口
├── config.py         ← 全局配置
└── config.yaml
```

---

## Slide 12: 总结

**核心创新**:

| 维度 | 传统方案 | 本项目 |
|------|---------|--------|
| 测试编写 | 手写脚本 | 自然语言 → Agent 自动规划 |
| 执行方式 | if/else 硬编码 | LLM 推理 + 工具调用 |
| 异常处理 | 脚本崩溃 | Reviewer 自动判断 + 恢复 |
| 危险操作 | 无保护 | LangGraph interrupt 人工确认 |
| 知识沉淀 | 无 | RAG 向量库 + 经验积累 |
| 可观测性 | 最终日志 | Token 级流式输出 + SQLite 审计 |

**技术亮点**:
- **LangGraph StateGraph**: 5 节点 Agent 编排图
- **ToolNode**: 标准化工具调用子图
- **with_structured_output**: 类型安全的 Planner 输出
- **interrupt + MemorySaver**: 断点暂停与恢复
- **双存储抽象**: VectorStoreBackend + RelationalBackend 接口
- **多 Provider**: OpenAI / Zhipu 可插拔切换

**代码量**: ~2000 行核心 Python | **Agent 数**: 4 | **工具数**: 20 | **测试覆盖**: 11/11

---

> 📄 源文件: `docs/AI自动化测试Agent架构说明.md` (完整架构文档)

# AI 自动化测试 Agent — 架构说明

> **版本**: 2.1  
> **技术栈**: Python 3.13 · LangChain 1.3 · LangGraph 1.2 · Chroma 1.1 · SQLite 3  
> **更新日期**: 2026-06-11

---

## 目录

1. [系统概览](#1-系统概览)
2. [项目包结构](#2-项目包结构)
3. [整体架构](#3-整体架构)
4. [LangChain 集成](#4-langchain-集成)
5. [LangGraph 集成](#5-langgraph-集成)
6. [多 Agent 协作](#6-多-agent-协作)
7. [数据层设计](#7-数据层设计)
8. [工具层设计](#8-工具层设计)
9. [状态管理](#9-状态管理)
10. [数据流](#10-数据流)
11. [配置与环境](#11-配置与环境)

---

## 1. 系统概览

AI 自动化测试 Agent 是一个 **多 Agent 协作的 Android 自动化测试平台**。用户用自然语言描述测试需求，系统自动规划、执行、审查并生成报告。

### 核心能力

| 能力 | 实现 |
|------|------|
| **自然语言驱动** | `python main.py run "检查 Settings 的 Wi-Fi 开关"` — 全流程自动化 |
| **自动规划** | Planner Agent 制定测试步骤并持久化为 YAML |
| **自主执行** | Executor Agent 调用 12 个设备操作工具 |
| **智能审查** | Reviewer Agent 检查结果、自动处理弹窗、重试失败 |
| **人工介入** | 危险操作通过 LangGraph `interrupt()` 暂停等待用户确认 |
| **知识积累** | RAG 知识库存储页面结构、导航路径、测试经验 |
| **执行记录** | SQLite 持久化所有测试运行记录 |

---

## 2. 项目包结构

```
AiAgentTest/
│
├── main.py                       # CLI 入口
├── config.py                     # 全局配置 (TestConfig dataclass)
├── config.yaml                   # YAML 配置文件
├── requirements.txt
│
├── agents/                       # 多 Agent 编排层
│   ├── graph.py                  # LangGraph StateGraph 定义 (5 节点 + 路由)
│   ├── orchestrator.py           # TestOrchestrator 对外入口
│   ├── state.py                  # TestState + Pydantic 结构化输出模型
│   ├── report_builder.py         # 测试报告生成 + WebSocket 事件广播
│   └── prompts/
│       ├── planner.txt           # Planner Agent 系统提示词
│       ├── executor.txt          # Executor Agent 系统提示词
│       └── reviewer.txt          # Reviewer Agent 系统提示词
│
├── tools/                        # LangChain 工具层
│   ├── __init__.py               # 20 Tools 定义 + 按 Agent 角色分组
│   └── context.py                # ToolContext 统一依赖注入
│
├── device/                       # Android 设备抽象层
│   ├── controller.py             # DeviceController (uiautomator2)
│   └── perceiver.py              # SmartPerceiver (UI 树 + Vision 多模态)
│
├── data/                         # 数据层
│   ├── __init__.py               # 工厂函数 (create_vector_store / create_relational_db)
│   ├── vector_store.py           # VectorStoreBackend (ABC) + MemoryBackend + ChromaBackend
│   ├── relational.py             # RelationalBackend (ABC) + SqliteBackend
│   └── knowledge.py              # KnowledgeBase RAG 知识库业务逻辑
│
├── llm/                          # LLM 抽象层
│   ├── clients.py                # LLMClient / VLMClient + OpenAI / Zhipu 实现
│   └── safety.py                 # 危险操作检测函数
│
├── api/                          # Web 接口层
│   ├── server.py                 # FastAPI + WebSocket + SSE 流式
│   ├── device_routes.py          # 设备控制 REST API
│   └── websocket_manager.py      # WebSocket 连接管理
│
├── tests/                        # 单元测试
├── test_cases/                   # 测试计划持久化 (YAML)
├── docs/                         # 文档
├── storage/                      # 运行时数据
│   ├── knowledge/                # Chroma 向量数据库文件
│   ├── screenshots/              # 截图保存
│   └── test_history.db           # SQLite 测试执行历史
└── reports/                      # JSON 测试报告输出
```

### 包间依赖

```
agents/  ──→ tools/ ──→ device/
    │           │          │
    ├───────────┼──────────┼──→ data/
    │           │          │
    └───────────┴──────────┼──→ llm/
                           │
api/ ──→ agents/ ──→ tools/ ──→ ...
```

依赖方向自上而下，无循环引用。

---

## 3. 整体架构

```
┌──────────────────────────────────────────────────────────────────┐
│                         用户交互层                                │
│  Web 前端 (Vue 3 + Element Plus)    CLI (python main.py run)     │
│  WebSocket 实时推送                  JSON 结果输出                │
└───────────────────────────┬──────────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────────┐
│                  API 层 (FastAPI)                                 │
│  POST /api/run         同步执行                                   │
│  POST /api/run/stream  流式执行 (SSE)                             │
│  POST /api/human_decision  人工确认恢复                            │
│  WS  /ws/chat          WebSocket 双向通信                         │
│  GET  /api/device/snapshot  设备截图+页面感知                      │
└───────────────────────────┬──────────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────────┐
│           agents/orchestrator.py — TestOrchestrator               │
│  start() / start_stream() / resume()                              │
│  注入: event_callback → WebSocket 实时广播                         │
└───────────────────────────┬──────────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────────┐
│            agents/graph.py — LangGraph StateGraph                 │
│                                                                   │
│  PLANNER ──→ EXECUTOR ──→ REVIEWER ──→ REPORTER ──→ END          │
│               ↑    ↑                       │                      │
│               │    └─── continue/retry ────┘                      │
│               │                                                   │
│               └──── HUMAN_APPROVAL ←── ask_human                  │
│                     (interrupt 暂停 → 人工确认 → resume)           │
│                                                                   │
│  Checkpointer: MemorySaver (断点持久化)                            │
│  Streaming: astream_events (token 级实时输出)                      │
│  State Update: Command(update=...) 声明式                          │
└───────────────────────────┬──────────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────────┐
│                   tools/ — 工具层 (20 Tools)                      │
│  ┌────────────┐  ┌──────────────┐  ┌──────────────────┐          │
│  │ Planner(2) │  │ Executor(12) │  │  Reviewer(9)     │          │
│  └────────────┘  └──────────────┘  └──────────────────┘          │
└───────────────────────────┬──────────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────────┐
│                    基础服务层                                      │
│  device/controller.py  │  device/perceiver.py  │  data/           │
│  uiautomator2          │  UI树 + Vision LLM   │  Chroma + SQLite  │
└──────────────────────────────────────────────────────────────────┘
```

---

## 4. LangChain 集成

### 4.1 使用的组件

| 组件 | 版本 | 位置 | 用途 |
|------|------|------|------|
| `langchain-core` | 1.4.1 | `tools/` `agents/graph.py` | Tool 装饰器、ChatPromptTemplate、SystemMessage |
| `langchain` | 1.3.4 | `data/knowledge.py` | VectorStoreRetrieverMemory |
| `langchain-openai` | 1.2.2 | `llm/clients.py` `agents/graph.py` | ChatOpenAI 客户端 |
| `langchain-chroma` | 1.1.0 | `data/vector_store.py` | Chroma 向量数据库 |
| `langgraph` | 1.2.4 | `agents/graph.py` | StateGraph、ToolNode、MemorySaver、interrupt |

### 4.2 ChatPromptTemplate — 提示词模板

系统提示词用 `SystemMessage(content=...)` 直接传入，用户消息用 `ChatPromptTemplate` 构建，支持变量校验和 `MessagesPlaceholder` 动态插入对话历史：

```python
# agents/graph.py
EXECUTOR_TEMPLATE = ChatPromptTemplate.from_messages([
    SystemMessage(content=EXECUTOR_SYSTEM),   # 不经过模板引擎，避免 JSON 示例 {} 冲突
    MessagesPlaceholder("recent_history", optional=True),  # 步骤摘要自动注入
    ("user", "步骤 {step_index}/{total_steps}: {intent}\n操作目标: {target}"),
])
```

### 4.3 @tool 装饰器 — 工具注册

20 个工具函数用 `langchain_core.tools.@tool` 装饰，LangGraph ToolNode 和 OpenAI `bind_tools()` 都直接消费：

```python
# tools/__init__.py
@tool
def click(label: str) -> str:
    """点击页面上指定文本、描述或资源 id 的元素。"""
    ctx = get_tool_context()
    if ctx.device.click_text(label):
        return f"已点击: {label}"
    return f"未找到可点击元素: {label}"
```

### 4.4 with_structured_output — Planner 结构化输出

Planner Agent (OpenAI provider) 使用 `ChatOpenAI.with_structured_output()` 直接输出 Pydantic 模型，Zhipu provider 回退到 ToolNode + JSON 手动解析：

```python
# agents/state.py
class TestPlanOutput(BaseModel):
    name: str
    description: str
    app_package: str
    steps: list[StepDefModel]
    verification: list[str]

# agents/graph.py
structured_llm = ChatOpenAI(...).with_structured_output(TestPlanOutput)
plan: TestPlanOutput = structured_llm.invoke(messages)
```

### 4.5 LLMClient / VLMClient — 多 Provider 抽象

```python
# llm/clients.py
class LLMClient(ABC):
    @abstractmethod
    def invoke(self, messages: list[dict]) -> str: ...

class VLMClient(ABC):
    @abstractmethod
    def describe(self, prompt: str, image_base64: str, context: str) -> str: ...
```

| 实现 | 底层 SDK | 用途 |
|------|---------|------|
| `OpenAITextClient` | `langchain_openai.ChatOpenAI` | Agent 推理 |
| `ZhipuTextClient` | `zhipuai.ZhipuAI` | Agent 推理 (智谱) |
| `OpenAIVisionClient` | `langchain_openai.ChatOpenAI` | 截图分析 |
| `ZhipuVisionClient` | `zhipuai.ZhipuAI` | 截图分析 (智谱) |

---

## 5. LangGraph 集成

### 5.1 StateGraph — 5 节点编排图

```
START → planner → executor → reviewer ──┬── continue/retry/skip → executor
                                         ├── done/abort → reporter → END
                                         └── ask_human → human_approval ──┬── executor
                                                                           └── reporter
```

```python
# agents/graph.py
def build_graph(config):
    graph = StateGraph(TestState)
    graph.add_node("planner", planner_node)
    graph.add_node("executor", executor_node)
    graph.add_node("reviewer", reviewer_node)
    graph.add_node("human_approval", human_approval_node)
    graph.add_node("reporter", reporter_node)

    graph.set_entry_point("planner")
    graph.add_edge("planner", "executor")
    graph.add_edge("executor", "reviewer")
    graph.add_conditional_edges("reviewer", route_after_reviewer, {...})
    graph.add_conditional_edges("human_approval", route_after_human, {...})
    graph.add_edge("reporter", END)

    return graph.compile(checkpointer=MemorySaver())  # 断点持久化
```

### 5.2 ToolNode — 工具调用子图

每个 Agent 节点内部使用 `_run_agent_with_tools()` 创建 ToolNode 驱动的子图，替代旧版 120 行手写循环：

```
LLM Node ──(has tool_calls?)──→ ToolNode ──→ LLM Node
    │                                ↑
    └──(no tool_calls)──→ return result
```

```python
sub = StateGraph(_AgentState)
sub.add_node("llm", llm_node)      # ChatOpenAI.bind_tools() 或 ZhipuAI
sub.add_node("tools", ToolNode(tools))
sub.add_conditional_edges("llm", tools_condition)  # LangGraph 内置路由
sub.add_edge("tools", "llm")
```

### 5.3 interrupt — 人工介入

```python
# agents/graph.py
def human_approval_node(state, config):
    decision = interrupt({
        "type": "need_human_approval",
        "question": "是否继续执行危险操作？",
        "options": ["允许执行", "跳过此步", "终止测试"],
    })
    # 图暂停 → 前端展示确认框 → 用户选择 → Command(resume=decision) 恢复
```

### 5.4 astream_events — 流式输出

```python
# agents/orchestrator.py
async def start_stream(self, ...):
    async for event in self.graph.astream_events(state, config, version="v2"):
        if event["event"] == "on_chat_model_stream":
            yield {"type": "stream_token", "content": chunk.content}   # LLM token
        elif event["event"] == "on_tool_start":
            yield {"type": "tool_start", "content": {"name": event["name"]}}  # 工具开始
        elif event["event"] == "on_tool_end":
            yield {"type": "tool_end", "content": {"name": event["name"]}}    # 工具结束
```

### 5.5 MemorySaver — 断点持久化

`graph.compile(checkpointer=MemorySaver())` 在每个节点执行后自动保存状态快照。`interrupt()` 暂停时状态已持久化，恢复时从快照继续。

### 5.6 Command — 声明式状态更新

所有节点返回 `Command(update={...})` 而非直接修改 state 字典：

```python
return Command(update={
    "step_history": new_history,
    "current_step_index": new_idx,
    "reviewer_decision": decision,
})
```

---

## 6. 多 Agent 协作

### 6.1 角色定义

| Agent | 节点 | 工具数 | 推荐模型 | 最大轮次 |
|-------|------|--------|---------|---------|
| **Planner** | `planner_node` | 2 | 强推理模型 | 5 |
| **Executor** | `executor_node` | 12 | 快速模型 | 3 |
| **Reviewer** | `reviewer_node` | 9 | 中等模型 | 5 |
| **Reporter** | `reporter_node` | 0 (纯文本) | 复用 Planner 模型 | 1 |

### 6.2 Planner — 规划专家

**输入**: 用户自然语言 + RAG 历史知识  
**输出**: `TestPlanOutput` (结构化计划，自动持久化为 YAML)

```
输入: "检查 Settings 的 Wi-Fi 开关是否正常"

流程:
1. get_screen_info() → 了解 Settings 首页
2. query_app_knowledge() → 查询 Wi-Fi 页面历史经验
3. with_structured_output → 生成:

name: "Settings Wi-Fi 开关测试"
steps:
  - index: 1, intent: "启动 Settings", action_type: launch_app, target: com.android.settings
  - index: 2, intent: "点击 Wi-Fi", action_type: click, target: Wi-Fi, alternatives: [WLAN]
  - index: 3, intent: "验证加载", action_type: assert, target: Wi-Fi
verification: ["Wi-Fi 页面可正常打开"]
```

### 6.3 Executor — 执行专家

只看**当前这一步**，不超前执行：
1. `detect_popup()` → 检测弹窗 → `dismiss_popup()` 关闭
2. 调用对应工具（click/type_input/swipe 等）
3. 如果 target 失败，尝试 alternatives 中的替代文字
4. `log_step()` 记录

### 6.4 Reviewer — 审查专家

决策逻辑：

| 情况 | 决策 | 路由 |
|------|------|------|
| 操作成功 + 页面健康 | `continue` | → executor (下一步) |
| 操作失败 + 可重试 | `retry` (≤2次) | → executor |
| 重试仍失败 + 非关键 | `skip` | → executor |
| ANR/崩溃且不可恢复 | `abort` | → reporter |
| 危险操作 | `ask_human` | → interrupt |
| 所有步骤完成 | `done` | → reporter |

### 6.5 对话摘要记忆

超过 8 步时自动摘要旧步骤，保持上下文窗口可控：

```
步骤 ≤ 8: 传入最近 3 步完整记录
步骤 > 8: 摘要(前 N-3 步) + 最近 3 步完整记录
```

---

## 7. 数据层设计

### 7.1 抽象接口

```python
# data/vector_store.py
class VectorStoreBackend(ABC):
    def add(self, content, metadata)           # 添加向量
    def search(self, query, filter, top_k)     # 相似度搜索
    def delete(self, filter)                   # 删除
    def count(self)                            # 计数

# data/relational.py
class RelationalBackend(ABC):
    def execute(self, sql, params)             # 原生 SQL
    def insert(self, table, data)              # 插入
    def select(self, table, where, ...)        # 查询
    def upsert(self, table, data, key)         # 插入或更新
    def count(self, table, where)              # 计数
```

### 7.2 实现

| 接口 | 实现 | 存储 |
|------|------|------|
| `VectorStoreBackend` | `MemoryBackend` | 内存 (关键词匹配) |
| `VectorStoreBackend` | `ChromaBackend` | `storage/knowledge/` (向量检索) |
| `RelationalBackend` | `SqliteBackend` | `storage/test_history.db` |

### 7.3 KnowledgeBase — RAG 业务逻辑

封装在 `VectorStoreBackend` 之上，提供领域方法：

| 方法 | 写入的知识类型 |
|------|-------------|
| `save_page_structure()` | `page_structure` |
| `save_navigation_path()` | `navigation_path` |
| `save_test_experience()` | `test_experience` |
| `extract_from_test_result()` | 批量提取以上三类 |
| `load_memory_context()` | 格式化查询结果供 Agent Prompt 注入 |

### 7.4 SQLite 表设计

```sql
-- 测试执行记录
test_runs(id, user_request, app_package, status, conclusion, steps_json, created_at)

-- 人工决策审计
human_decisions(id, run_id, step_index, question, decision, created_at)

-- 测试计划元数据
test_plans(id, name, app_package, yaml_path, steps_count, created_at, updated_at)
```

### 7.5 工厂函数

```python
# data/__init__.py
def create_vector_store(config) -> VectorStoreBackend:
    if config.enable_rag and config.api_key:
        return ChromaBackend(...)
    return MemoryBackend()

def create_relational_db(config) -> RelationalBackend:
    return SqliteBackend(db_path=config.db_path)
```

### 7.6 数据流向

```
Reporter 节点
  ├── KnowledgeBase.extract_from_test_result() → Chroma / Memory
  └── SqliteBackend.record_test_run()          → SQLite

Planner 节点
  └── KnowledgeBase.load_memory_context()      ← Chroma / Memory
```

---

## 8. 工具层设计

### 8.1 工具分组 (20 Tools)

**Planner (2)**:

| 工具 | 用途 |
|------|------|
| `get_screen_info` | 页面结构化信息 |
| `query_app_knowledge` | RAG 知识查询 |

**Executor (12)**:

| 工具 | 用途 |
|------|------|
| `click` | 点击元素 |
| `navigate_to` | 切换导航 |
| `scroll_find_and_click` | 滑动查找点击 |
| `type_input` | 输入文字 |
| `press_key` | 系统按键 |
| `swipe` | 滑动 |
| `launch_app` | 启动应用 |
| `detect_popup` | 弹窗检测 |
| `dismiss_popup` | 弹窗关闭 |
| `wait_seconds` | 等待 |
| `log_step` | 记录步骤 |
| `save_screenshot` | 截图 |

**Reviewer (9)**:

| 工具 | 用途 |
|------|------|
| `get_screen_info` | 页面检查 |
| `get_detailed_screen` | 详细视觉分析 |
| `switch_perception_mode` | 感知模式切换 |
| `check_page_health` | 异常检测 |
| `recover_from_anomaly` | 异常恢复 |
| `assert_page_contains` | 文本断言 |
| `assert_element_exists` | 元素断言 |
| `log_step` | 记录 (共用) |
| `save_screenshot` | 截图 (共用) |

### 8.2 依赖注入

```python
# tools/context.py
@dataclass
class ToolContext:
    device: object           # DeviceController
    perceiver: object        # SmartPerceiver
    knowledge_base: object   # KnowledgeBase
    safety_level: str

# tools/__init__.py
def get_tool_context() -> ToolContext:  # 全局单例
```

---

## 9. 状态管理

### TestState

```python
# agents/state.py
class TestState(TypedDict, total=False):
    # 用户输入
    user_request: str
    app_package: str
    
    # Planner 产出
    test_plan: list[dict]              # 步骤列表
    current_step_index: int
    
    # Executor 产出
    last_action: str
    last_observation: str
    step_history: list[dict]           # 已完成步骤记录
    
    # Reviewer 产出
    reviewer_decision: str             # continue|retry|skip|abort|ask_human|done
    human_question: str
    retry_count: int
    
    # Reporter 产出
    conclusion: str
    status: str                        # success|fail
```

### 状态流转

```
Planner   → test_plan, current_step_index = 0
Executor  → last_action, last_observation
Reviewer  → step_history += [record], reviewer_decision, current_step_index 更新
Reporter  → conclusion, status
```

---

## 10. 数据流

### 完整执行链路

```
1. 用户输入 "检查 Settings 的 Wi-Fi 开关"
2. Planner (LLM + get_screen_info + query_app_knowledge)
   → TestPlanOutput YAML
3. Executor (LLM + ToolNode[12 tools]) × N 次
   → last_observation
4. Reviewer (LLM + ToolNode[9 tools])
   → decision → route
5. 循环 3→4→3... 直到 done / abort / ask_human
6. Reporter (LLM 纯文本)
   → conclusion + status
7. 后处理
   → RAG 回写 (Chroma)
   → SQLite 记录 (test_runs 表)
```

### 实时事件流 (SSE)

```
客户端              API                     LangGraph
  │ POST /run/stream ─→                              │
  │                   ─→ orchestrator.start_stream() │
  │                                              ─→ graph.astream_events()
  │← stream_token ────────────────────────────── "分析需求..."
  │← plan_ready   ────────────────────────────── {steps: 3}
  │← tool_start   ────────────────────────────── "click"
  │← stream_token ────────────────────────────── "点击 Wi-Fi"
  │← tool_end     ────────────────────────────── "click done"
  │← result       ────────────────────────────── {status: "success"}
```

---

## 11. 配置与环境

### config.yaml

```yaml
llm_provider: "openai"
model: "deepseek-v4-pro"

# 各 Agent 独立模型 (留空继承默认)
planner_model: ""
executor_model: ""
reviewer_model: ""

# Vision
vision_provider: "zhipu"
vision_model: "glm-4.6v-flash"

# 存储
db_path: "storage/test_history.db"
rag_persist_dir: "storage/knowledge"

# 安全
safety_level: "strict"
```

### 环境变量

| 变量 | 用途 |
|------|------|
| `OPENAI_API_KEY` | 默认 LLM API 密钥 |
| `OPENAI_BASE_URL` | 默认 LLM API 地址 |
| `ZHIPU_API_KEY` | 智谱 API 密钥 |
| `ANDROID_SERIAL` | ADB 设备序列号 |
| `LANGCHAIN_DEBUG` | LangChain 调试模式 |
| `LANGSMITH_API_KEY` | LangSmith 追踪 (可选) |

### 依赖

```
langchain>=0.3.0           # 核心框架
langchain-openai>=0.2.0    # OpenAI LLM
langchain-chroma>=0.2.0    # Chroma 向量库
langgraph>=0.2.0           # 图编排引擎
langchain-core>=0.3.0      # 基础类型
uiautomator2>=3.0.0        # Android 设备控制
Pillow>=10.0.0             # 图像处理
numpy>=1.24.0              # 数值计算
pyyaml>=6.0                # YAML
chromadb>=0.5.0            # 向量数据库
fastapi>=0.110.0           # Web 框架
uvicorn>=0.27.0            # ASGI 服务器
websockets>=12.0           # WebSocket
zhipuai>=2.0.0             # 智谱 SDK
```

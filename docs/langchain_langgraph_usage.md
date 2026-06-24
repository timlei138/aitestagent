# LangGraph + LangChain 在项目中的使用

## 一、LangGraph — 编排层（核心）

### 1. StateGraph — 定义工作流

项目有两个 StateGraph：

**外层图** — 4 节点测试流程（[graph.py:341-351](agents/graph.py#L341-L351)）

```python
g = StateGraph(TestState)

g.add_node("planner", planner_node)         # 节点1: LLM 生成计划
g.add_node("plan_review", plan_review_node) # 节点2: 人机确认
g.add_node("agent", agent_node)             # 节点3: 循环自主执行
g.add_node("reporter", reporter_node)       # 节点4: 统计入库

g.add_edge(START, "planner")
g.add_edge("planner", "plan_review")
g.add_conditional_edges("plan_review", ...)  # 确认→agent / 取消→reporter
g.add_conditional_edges("agent", ...)        # DONE→reporter / 继续→agent
g.add_edge("reporter", END)
```

**内层子图** — LLM ↔ 工具循环（[graph.py:95-98](agents/graph.py#L95-L98)）

```python
g = StateGraph(_SubState)

g.add_node("llm", llm_node)           # LLM 推理
g.add_node("tools", ToolNode(tools))  # 执行工具
g.add_node("inc", _inc)               # 计数器

g.add_edge(START, "llm")
g.add_conditional_edges("llm", _limit, {"tools":"inc", END:END})  # 超限→END
g.add_edge("inc", "tools")
g.add_edge("tools", "llm")            # 工具结果→回 LLM 继续
```

### 2. interrupt() — 人机协作暂停

[graph.py:293-294](agents/graph.py#L293-L294)：Planner 生成计划后暂停，等用户确认。

```python
result = interrupt({
    "type": "plan_review",
    "plan": goal,
    "goal": goal.get("goal", ""),
    "pages": goal.get("target_pages", []),
    "verification": goal.get("verification", []),
})
```

### 3. Command(resume=) — 中断后恢复

[orchestrator.py:192](agents/orchestrator.py#L192)：用户确认后，用 Command 从中断点继续。

```python
final_state = self.graph.invoke(Command(resume=resume_value), config_ctx)
```

### 4. MemorySaver — 状态持久化

[graph.py:351](agents/graph.py#L351)：保存每一步状态，interrupt/resume 靠它无缝衔接。

```python
return g.compile(checkpointer=MemorySaver())
```

### 5. ToolNode — 自动执行工具

[graph.py:96](agents/graph.py#L96)：LLM 输出 tool_call → ToolNode 自动解析参数并执行。

```python
g.add_node("tools", ToolNode(tools))
```

### 6. tools_condition — 路由判断

[graph.py:91-93](agents/graph.py#L91-L93)：判断 LLM 输出是工具调用还是纯文本，决定下一步走向。

```python
r = tools_condition(s)
return END if r == "tools" and count >= max_turns else r
```

### 7. add_messages — 消息自动合并

[graph.py:62](agents/graph.py#L62)：StateGraph 的 reducer，新消息自动追加到消息列表。

```python
class _SubState(TypedDict):
    messages: Annotated[list, add_messages]
    _turn_count: int
```

---

## 二、LangChain — 组件层

### 1. @tool — 定义工具

[tools/__init__.py](tools/__init__.py)：17 个 `@tool` 装饰器把 Python 函数变成 LLM 可调用的工具。

```python
@tool
def click(label: str) -> str:
    """点击页面上指定文本的元素"""
    ...

@tool
def get_screen_info(mode: str = "summary") -> str:
    """获取当前页面 UI 树信息"""
    ...
```

### 2. ChatOpenAI + bind_tools — 绑定工具到 LLM

[graph.py:83-84](agents/graph.py#L83-L84)：`bind_tools()` 把工具的 name/description/parameters 注入到 LLM 的 function calling。

```python
lc = ChatOpenAI(model=model, temperature=0.1, api_key=api_key, base_url=base_url)
    .bind_tools(tools)
```

### 3. Message 类型 — 构建对话

[graph.py:11](agents/graph.py#L11)：三种消息类型区分角色。

```python
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

msgs = [SystemMessage(content=AGENT_SYSTEM)]
msgs.append(HumanMessage(content="Goal:\n" + goal_str))
msgs.append(AIMessage(content=result))
```

### 4. ChatPromptTemplate — Planner 的提示模板

[graph.py:55-67](agents/graph.py#L55-L67)：`{user_request}` `{rag_context}` 等变量动态替换。

```python
PLANNER_TEMPLATE = ChatPromptTemplate.from_messages([
    SystemMessage(content=PLANNER_SYSTEM),
    ("user", "Create a test goal for:\nRequest: {user_request}\n{rag_context}"),
])
```

### 5. HuggingFaceEmbeddings — Embedding 模型

[vector_store.py:96-101](data/vector_store.py#L96-L101)：BGE-large-zh-v1.5 把知识文本转 1024 维向量。

```python
embeddings = HuggingFaceEmbeddings(
    model_name="BAAI/bge-large-zh-v1.5",
    model_kwargs={"device": "cpu"},
    encode_kwargs={"normalize_embeddings": True},
)
```

### 6. Chroma — 向量存储

[vector_store.py:109-113](data/vector_store.py#L109-L113)：LangChain 的 Chroma 包装器，自动处理 embedding + 相似度搜索。

```python
self._store = Chroma(
    collection_name="app_knowledge",
    embedding_function=embeddings,
    persist_directory=persist_dir,
)
```

---

## 三、一张图总结

```
LangGraph                          LangChain
─────────                          ─────────
StateGraph ─── 定义流程             @tool ─── 17 个工具
interrupt() ─ 人机暂停             ChatOpenAI ─ LLM 客户端
Command(resume=) ─ 恢复执行        bind_tools() ─ 绑定工具
MemorySaver ─ 状态持久化           SystemMessage/HumanMessage/AIMessage ─ 对话
ToolNode ─ 自动执行工具            ChatPromptTemplate ─ Planner 提示
tools_condition ─ 路由判断         HuggingFaceEmbeddings ─ 文本向量化
add_messages ─ 消息合并            Chroma ─ 向量存储+检索
```
